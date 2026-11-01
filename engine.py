"""
Генерация видео через сайт OkroshkAI.

BrowserEngine  — открывает сайт в headless-Chromium (Playwright), сам заполняет
                 форму (модель, промпт, референсы, длительность, формат),
                 жмёт «Сгенерировать видео» и ждёт готовый ролик.
DirectEngine   — лёгкий режим для слабого хостинга: делает ровно те же запросы,
                 что и кнопка на сайте (/api/keys → /api/upload → /api/run →
                 /api/status), но без браузера. Токены админа не нужны.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx

log = logging.getLogger("engine")

StatusCb = Callable[[str], Awaitable[None]]

# id инпутов на сайте → (type, role) для референсов
REF_GROUPS = {
    "imgs": ("image", "reference_image"),
    "vids": ("video", "reference_video"),
    "auds": ("audio", "reference_audio"),
}
REF_LIMITS = {"imgs": 8, "vids": 3, "auds": 3}
GEN_TIMEOUT = 20 * 60  # сайт сам ждёт максимум 20 минут


class GenerationError(Exception):
    pass


class Cancelled(Exception):
    pass


class BaseEngine:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def generate(
        self,
        *,
        model: str,  # "fast" | "sd2"
        prompt: str,
        duration: int,
        ratio: str,
        refs: dict[str, list[Path]],
        on_status: StatusCb,
        cancel: asyncio.Event,
    ) -> str:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
#                               Через браузер                                 #
# --------------------------------------------------------------------------- #
class BrowserEngine(BaseEngine):
    def __init__(self, site_url: str, headless: bool = True):
        self.site_url = site_url.rstrip("/") + "/"
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self._ensure_browser()

    async def _ensure_browser(self):
        async with self._lock:
            if self._browser and self._browser.is_connected():
                return
            from playwright.async_api import async_playwright

            if self._pw is None:
                self._pw = await async_playwright().start()
            log.info("Запускаю Chromium…")
            self._browser = await self._pw.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--mute-audio",
                ],
            )
            # Один контекст на всё время работы: сайт хранит свой ключ
            # в localStorage, и мы не дёргаем /api/keys на каждое видео.
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 900}, locale="ru-RU"
            )

    async def stop(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def generate(self, *, model, prompt, duration, ratio, refs, on_status, cancel) -> str:
        await self._ensure_browser()
        page = await self._context.new_page()
        try:
            await on_status("🌐 Открываю сайт…")
            # Сайт на Render free может «спать» — даём до 3 минут на пробуждение.
            for attempt in range(3):
                try:
                    await page.goto(self.site_url, wait_until="domcontentloaded", timeout=180_000)
                    await page.wait_for_selector("#go", timeout=60_000)
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 2:
                        raise GenerationError(f"сайт не открылся: {e}") from e
                    await on_status("😴 Сайт просыпается, пробую ещё раз…")
                    await asyncio.sleep(5)

            # ждём, пока сайт сам получит ключ доступа
            try:
                await page.wait_for_function(
                    "document.getElementById('key') && document.getElementById('key').value.length>0",
                    timeout=60_000,
                )
            except Exception:  # noqa: BLE001
                log.warning("ключ не появился за 60 сек — сайт попробует получить его сам при запуске")

            await on_status("✍️ Заполняю форму…")
            # модель (радио-кнопки могут быть скрыты стилями — выставляем через JS)
            ok = await page.evaluate(
                """(m) => {
                    const r = document.querySelector(`input[name=model][value="${m}"]`);
                    if (!r) return false;
                    r.checked = true;
                    r.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }""",
                model,
            )
            if not ok:
                raise GenerationError(f"на сайте нет модели «{model}» — возможно, сайт изменился")

            await page.fill("#prompt", prompt)
            for input_id, files in refs.items():
                if files:
                    await page.set_input_files(f"#{input_id}", [str(p) for p in files])
            await page.fill("#dur", str(duration))
            await page.select_option("#ratio", ratio)

            await page.click("#go")
            await on_status("🚀 Запустил генерацию…")

            deadline = time.monotonic() + GEN_TIMEOUT + 60
            last_timer = ""
            started = False
            idle = 0
            while time.monotonic() < deadline:
                if cancel.is_set():
                    raise Cancelled()
                st = await page.evaluate(
                    """() => {
                        const v = document.querySelector('#done video');
                        const e = document.getElementById('error');
                        const t = document.getElementById('timer');
                        const g = document.getElementById('go');
                        const vis = el => el && getComputedStyle(el).display !== 'none' && el.textContent.trim();
                        return {
                            url: v ? (v.currentSrc || v.src) : null,
                            err: vis(e) ? e.textContent.trim() : null,
                            timer: vis(t) ? t.textContent.trim() : null,
                            busy: g ? g.disabled : false,
                        };
                    }"""
                )
                if st["busy"]:
                    started = True
                if st["url"]:
                    return st["url"]
                if st["err"]:
                    raise GenerationError(st["err"].removeprefix("ошибка:").strip())
                if started and not st["busy"]:
                    # кнопка снова активна, но нет ни видео, ни ошибки
                    idle += 1
                    if idle >= 3:
                        raise GenerationError("сайт завершил генерацию без результата")
                if st["timer"] and st["timer"] != last_timer:
                    last_timer = st["timer"]
                    await on_status("🎬 " + st["timer"])
                await asyncio.sleep(2)
            raise GenerationError("время ожидания истекло")
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
#                 Без браузера (те же запросы, что делает сайт)               #
# --------------------------------------------------------------------------- #
class DirectEngine(BaseEngine):
    def __init__(self, site_url: str):
        self.site_url = site_url.rstrip("/")
        self._key = ""

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.site_url,
            timeout=httpx.Timeout(180, connect=180),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (OkroshkaTelegramBot)"},
        )

    async def _new_key(self, c: httpx.AsyncClient) -> str:
        r = await c.post("/api/keys")
        key = (r.json() or {}).get("key", "")
        if not key:
            raise GenerationError("сайт не выдал ключ доступа")
        self._key = key
        return key

    async def generate(self, *, model, prompt, duration, ratio, refs, on_status, cancel) -> str:
        async with self._client() as c:
            await on_status("🌐 Подключаюсь к сайту…")
            key = self._key or await self._new_key(c)

            uploaded = []
            total = sum(len(v) for v in refs.values())
            n = 0
            for input_id, (rtype, role) in REF_GROUPS.items():
                for p in refs.get(input_id, []):
                    n += 1
                    await on_status(f"📤 Загрузка референса {n} из {total}")
                    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                    with open(p, "rb") as fh:
                        r = await c.post("/api/upload", files={"files[]": (p.name, fh, mime)})
                    j = _json(r)
                    if r.status_code >= 400:
                        raise GenerationError(f"{p.name}: {j.get('error') or r.status_code}")
                    url = (j.get("files") or [{}])[0].get("url")
                    if not url:
                        raise GenerationError(f"{p.name}: ссылка на файл не получена")
                    uploaded.append({"url": url, "type": rtype, "role": role})

            await on_status("🎬 Генерация видео")
            body = {"prompt": prompt, "duration": duration, "ratio": ratio, "model": model, "refs": uploaded}
            r = await c.post("/api/run", json={"key": key, **body}, headers={"X-Api-Key": key})
            if r.status_code == 403:
                key = await self._new_key(c)
                r = await c.post("/api/run", json={"key": key, **body}, headers={"X-Api-Key": key})
            j = _json(r)
            if r.status_code >= 400 or not j.get("job"):
                raise GenerationError(j.get("error") or f"HTTP {r.status_code}")
            job_id = j["job"]

            t0 = time.monotonic()
            fails = 0
            while time.monotonic() - t0 < GEN_TIMEOUT:
                for _ in range(4):
                    if cancel.is_set():
                        raise Cancelled()
                    await asyncio.sleep(1)
                try:
                    s = await c.get("/api/status", params={"id": job_id})
                except httpx.HTTPError:
                    fails += 1
                    if fails >= 5:
                        raise GenerationError("не удалось связаться с сайтом для проверки результата")
                    continue
                if s.status_code == 404:
                    raise GenerationError("задание не найдено; возможно, сайт перезапустился")
                if s.status_code >= 500:
                    fails += 1
                    if fails >= 5:
                        raise GenerationError(f"сайт не отвечает: HTTP {s.status_code}")
                    continue
                fails = 0
                sj = _json(s)
                if sj.get("state") == "done":
                    if not sj.get("url"):
                        raise GenerationError("генерация завершена, но ссылка на видео не получена")
                    return str(httpx.URL(self.site_url + "/").join(sj["url"]))
                if sj.get("state") == "error":
                    raise GenerationError(sj.get("message") or "ошибка генерации")
                await on_status(f"🎬 Генерация видео · {int(time.monotonic() - t0)} сек")
            raise GenerationError("время ожидания истекло")


def _json(r: httpx.Response) -> dict:
    try:
        return r.json() or {}
    except Exception:  # noqa: BLE001
        return {}


def make_engine(kind: str, site_url: str) -> BaseEngine:
    kind = (kind or "browser").lower()
    if kind == "direct":
        return DirectEngine(site_url)
    return BrowserEngine(site_url)
