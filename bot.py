"""
NeyroTokBot — Telegram-бот для генерации видео SD 2 / SD 2 Fast через сайт OkroshkAI.

Команды:
  /sd2 [промпт]      — открыть панель генерации с моделью SD 2
  /sd2fast [промпт]  — то же, модель SD 2 Fast
  /cancel            — закрыть черновик
  /queue             — очередь
  /help              — справка

Быстрые параметры прямо в команде (в любом порядке, в начале текста):
  /sd2fast 8s 9:16 котик катается на скейте
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)
from aiohttp import web

from engine import REF_LIMITS, Cancelled, GenerationError, make_engine

# ----------------------------------------------------------------- настройки
_env = Path(__file__).with_name(".env")  # для запуска на своём ПК; на хостинге задавайте переменные в панели
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

BOT_TOKEN = os.environ["BOT_TOKEN"]
SITE_URL = os.getenv("SITE_URL", "https://okroshka-su6j.onrender.com/")
ENGINE = os.getenv("ENGINE", "browser")  # browser | direct
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "1"))  # сколько видео генерить одновременно
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "0"))  # видео в сутки на человека, 0 = без лимита
MAX_ACTIVE_PER_USER = int(os.getenv("MAX_ACTIVE_PER_USER", "1"))  # заданий в очереди на человека
ADMIN_IDS = {int(x) for x in re.findall(r"-?\d+", os.getenv("ADMIN_IDS", ""))}
ALLOWED_CHATS = {int(x) for x in re.findall(r"-?\d+", os.getenv("ALLOWED_CHATS", ""))}  # пусто = все
CHANNEL_CLEANUP = os.getenv("CHANNEL_CLEANUP", "1") == "1"  # чистить служебные посты в канале
DEFAULT_DURATION = int(os.getenv("DEFAULT_DURATION", "5"))
DEFAULT_RATIO = os.getenv("DEFAULT_RATIO", "16:9")
WEBHOOK_BASE = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
PORT = int(os.getenv("PORT", "0") or 0)
BOT_NAME = os.getenv("BOT_NAME", "NeyroTokBot")

MODELS = {"sd2": "SD 2", "fast": "SD 2 Fast"}
RATIOS = ["16:9", "9:16", "1:1"]
DUR_MIN, DUR_MAX = 4, 15
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API отдаёт боту файлы до 20 МБ
TG_UPLOAD_LIMIT = 49 * 1024 * 1024  # и принимает до 50 МБ

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)
engine = make_engine(ENGINE, SITE_URL)
BOT_USERNAME = ""


# ----------------------------------------------------------------- модели данных
@dataclass
class Ref:
    kind: str  # imgs | vids | auds
    file_id: str
    name: str


@dataclass
class Draft:
    id: str
    chat_id: int
    chat_type: str
    owner_id: int  # 0 = канал (жать кнопки могут админы канала)
    owner_name: str
    model: str
    prompt: str = ""
    duration: int = DEFAULT_DURATION
    ratio: str = DEFAULT_RATIO
    refs: list[Ref] = field(default_factory=list)
    panel_id: int | None = None
    source_msg_id: int | None = None
    service_msgs: list[int] = field(default_factory=list)  # что удалить в канале после публикации

    def count(self, kind: str) -> int:
        return sum(1 for r in self.refs if r.kind == kind)


@dataclass
class Job:
    id: str
    draft: Draft
    status_msg_id: int
    user_id: int = 0  # кто запустил (для лимитов)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: float = 0.0
    running: bool = False


drafts: dict[tuple[int, int], Draft] = {}
drafts_by_id: dict[str, Draft] = {}
jobs: dict[str, Job] = {}  # активные + недавние (для «Повторить»)
pending: list[Job] = []
queue: asyncio.Queue[Job] = asyncio.Queue()
usage: dict[tuple[str, int], int] = {}
_albums: dict[str, list[Message]] = {}


# ----------------------------------------------------------------- утилиты
def owner_of(m: Message) -> tuple[int, str]:
    if m.chat.type == ChatType.CHANNEL or not m.from_user:
        return 0, m.chat.title or "канал"
    u = m.from_user
    return u.id, u.full_name


def is_private(chat_type: str) -> bool:
    return chat_type == ChatType.PRIVATE


CMD_RE = re.compile(r"^/(\w+)(?:@(\w+))?(?:\s+|$)(.*)", re.S)


def parse_command(text: str | None) -> tuple[str | None, str]:
    if not text:
        return None, ""
    m = CMD_RE.match(text.strip())
    if not m:
        return None, text
    cmd, mention, rest = m.group(1).lower(), m.group(2), m.group(3)
    if mention and BOT_USERNAME and mention.lower() != BOT_USERNAME.lower():
        return None, ""  # команда другому боту
    return cmd, rest.strip()


DUR_TOKEN = re.compile(r"^(\d{1,2})\s*(s|с|sec|сек)?$", re.I)


def parse_quick_args(text: str) -> tuple[str, int | None, str | None]:
    """'8s 9:16 котик' -> ('котик', 8, '9:16'). Параметры ищем только в начале."""
    words = text.split()
    dur = ratio = None
    i = 0
    while i < len(words) and i < 3:
        w = words[i].strip(",;")
        if ratio is None and w in RATIOS:
            ratio = w
        elif dur is None and (mt := DUR_TOKEN.match(w)) and DUR_MIN <= int(mt.group(1)) <= DUR_MAX:
            dur = int(mt.group(1))
        else:
            break
        i += 1
    if i == 0:
        return text.strip(), None, None
    # если после параметров ничего нет — это был не параметр, а промпт ("8")
    rest = " ".join(words[i:]).strip()
    return rest, dur, ratio


def extract_ref(m: Message) -> tuple[Ref | None, str | None]:
    """Вернёт (референс, ошибка)."""
    size = None
    if m.photo:
        p = m.photo[-1]
        ref, size = Ref("imgs", p.file_id, f"{p.file_unique_id}.jpg"), p.file_size
    elif m.video:
        v = m.video
        ref, size = Ref("vids", v.file_id, v.file_name or f"{v.file_unique_id}.mp4"), v.file_size
    elif m.animation:
        a = m.animation
        ref, size = Ref("vids", a.file_id, f"{a.file_unique_id}.mp4"), a.file_size
    elif m.video_note:
        v = m.video_note
        ref, size = Ref("vids", v.file_id, f"{v.file_unique_id}.mp4"), v.file_size
    elif m.audio:
        a = m.audio
        ref, size = Ref("auds", a.file_id, a.file_name or f"{a.file_unique_id}.mp3"), a.file_size
    elif m.voice:
        v = m.voice
        ref, size = Ref("auds", v.file_id, f"{v.file_unique_id}.ogg"), v.file_size
    elif m.document:
        d = m.document
        mime = d.mime_type or ""
        kind = {"image": "imgs", "video": "vids", "audio": "auds"}.get(mime.split("/")[0])
        if not kind:
            return None, f"Файл «{d.file_name}» не похож на фото, видео или аудио."
        ref, size = Ref(kind, d.file_id, d.file_name or d.file_unique_id), d.file_size
    else:
        return None, None
    if size and size > TG_DOWNLOAD_LIMIT:
        return None, "Файл больше 20 МБ — Telegram не отдаёт ботам такие файлы. Сожмите его и пришлите снова."
    return ref, None


def short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def today() -> str:
    return dt.date.today().isoformat()


def used_today(uid: int) -> int:
    return usage.get((today(), uid), 0)


async def safe(coro):
    """Некритичные вызовы (правка статуса, удаление): ошибки глотаем."""
    try:
        return await coro
    except TelegramRetryAfter as e:
        log.info("flood control, пропускаю правку (%s сек)", e.retry_after)
        return None
    except TelegramAPIError as e:
        if "message is not modified" not in str(e):
            log.debug("telegram: %s", e)
        return None


async def tg(factory, tries: int = 4):
    """Важные вызовы (отправка видео): повторяем при флуд-лимите и сетевых сбоях."""
    for i in range(tries):
        try:
            return await factory()
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramBadRequest as e:
            log.warning("telegram bad request: %s", e)
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("telegram error (попытка %s): %s", i + 1, e)
            await asyncio.sleep(3)
    return None


async def is_chat_admin(chat_id: int, user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    try:
        mem = await bot.get_chat_member(chat_id, user_id)
        return mem.status in ("creator", "administrator")
    except TelegramAPIError:
        return False


# ----------------------------------------------------------------- панель
def panel_text(d: Draft) -> str:
    refs = " · ".join(
        f"{d.count(k)} {name}" for k, name in (("imgs", "фото"), ("vids", "видео"), ("auds", "аудио"))
    )
    prompt = html.escape(short(d.prompt, 700)) if d.prompt else "<i>не задан — пришлите текстом</i>"
    where = "" if is_private(d.chat_type) else "\n<i>В группе/канале отправляйте текст и файлы ответом (reply) на эту панель.</i>"
    return (
        f"🎬 <b>{MODELS[d.model]}</b> · черновик\n\n"
        f"📝 <b>Промпт:</b> {prompt}\n"
        f"⏱ <b>Длительность:</b> {d.duration} сек\n"
        f"🖼 <b>Формат:</b> {d.ratio}\n"
        f"📎 <b>Референсы:</b> {refs}\n\n"
        f"Пришлите фото / видео / аудио — добавлю как референсы "
        f"(до {REF_LIMITS['imgs']} фото, {REF_LIMITS['vids']} видео, {REF_LIMITS['auds']} аудио).\n"
        f"Пришлите текст — заменю промпт.{where}"
    )


def panel_kb(d: Draft) -> InlineKeyboardMarkup:
    p = f"d:{d.id}:"
    mark = lambda ok, t: ("✅ " + t) if ok else t  # noqa: E731
    rows = [
        [
            InlineKeyboardButton(text=mark(d.model == "fast", "⚡ SD 2 Fast"), callback_data=p + "m:fast"),
            InlineKeyboardButton(text=mark(d.model == "sd2", "💎 SD 2"), callback_data=p + "m:sd2"),
        ],
        [
            InlineKeyboardButton(text="➖", callback_data=p + "dur:-1"),
            InlineKeyboardButton(text=f"⏱ {d.duration} сек", callback_data=p + "noop"),
            InlineKeyboardButton(text="➕", callback_data=p + "dur:+1"),
        ],
        [
            InlineKeyboardButton(text=mark(d.duration == v, f"{v}с"), callback_data=p + f"dur:={v}")
            for v in (4, 5, 8, 10, 15)
        ],
        [InlineKeyboardButton(text=mark(d.ratio == r, r), callback_data=p + f"r:{r}") for r in RATIOS],
    ]
    if d.refs:
        rows.append([InlineKeyboardButton(text=f"🗑 Убрать референсы ({len(d.refs)})", callback_data=p + "clr")])
    rows.append(
        [
            InlineKeyboardButton(text="🚀 Сгенерировать", callback_data=p + "go"),
            InlineKeyboardButton(text="✖️ Отмена", callback_data=p + "x"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_panel(d: Draft, *, resend: bool = False):
    """В личке/группе перекидываем панель вниз чата, в канале — правим на месте."""
    text, kb = panel_text(d), panel_kb(d)
    if d.panel_id and (not resend or d.chat_type == ChatType.CHANNEL):
        r = await safe(bot.edit_message_text(text=text, chat_id=d.chat_id, message_id=d.panel_id, reply_markup=kb))
        if r is not None or not resend:
            return
    if d.panel_id:
        await safe(bot.delete_message(d.chat_id, d.panel_id))
    msg = await bot.send_message(d.chat_id, text, reply_markup=kb)
    d.panel_id = msg.message_id


def drop_draft(d: Draft):
    drafts.pop((d.chat_id, d.owner_id), None)
    drafts_by_id.pop(d.id, None)


# ----------------------------------------------------------------- входящие сообщения
HELP = (
    "🎬 <b>NeyroTokBot</b> — генерация видео SD 2\n\n"
    "<b>/sd2fast</b> — быстрая модель\n"
    "<b>/sd2</b> — больше деталей в кадре\n\n"
    "<b>Как пользоваться</b>\n"
    "1. Напишите команду, можно сразу с промптом:\n"
    "<code>/sd2fast кот в скафандре идёт по Луне</code>\n"
    "2. Откроется панель: выберите длительность (4–15 сек) и формат (16:9, 9:16, 1:1).\n"
    "3. Если нужно, пришлите референсы: фото (до 8), видео (до 3), аудио (до 3).\n"
    "4. Нажмите «🚀 Сгенерировать», и бот пришлёт готовый ролик.\n\n"
    "<b>Быстрый ввод</b>: длительность и формат можно указать прямо в команде:\n"
    "<code>/sd2 10s 9:16 неоновый Токио под дождём</code>\n\n"
    "Можно отправить альбом фото с подписью <code>/sd2 промпт</code>, тогда бот сразу добавит их как референсы.\n\n"
    "/queue — очередь · /cancel — закрыть черновик"
)


@router.message()
@router.channel_post()
async def on_any(m: Message):
    if ALLOWED_CHATS and m.chat.id not in ALLOWED_CHATS and (not m.from_user or m.from_user.id not in ADMIN_IDS):
        return
    if m.media_group_id:  # альбом приходит пачкой отдельных сообщений — собираем
        lst = _albums.setdefault(m.media_group_id, [])
        lst.append(m)
        if len(lst) == 1:
            await asyncio.sleep(1.5)
            await handle_batch(_albums.pop(m.media_group_id, lst))
        return
    await handle_batch([m])


async def handle_batch(msgs: list[Message]):
    msgs.sort(key=lambda x: x.message_id)
    first = msgs[0]
    chat = first.chat
    text_msg = next((x for x in msgs if x.text or x.caption), None)
    raw = (text_msg.text or text_msg.caption) if text_msg else ""
    cmd, rest = parse_command(raw)
    uid, uname = owner_of(first)
    key = (chat.id, uid)

    if cmd in ("start", "help"):
        await first.answer(HELP)
        return
    if cmd == "queue":
        await first.answer(queue_text())
        return
    if cmd == "cancel":
        d = drafts.get(key)
        if d:
            if d.panel_id:
                await safe(bot.delete_message(d.chat_id, d.panel_id))
            drop_draft(d)
            await first.answer("Черновик закрыт.")
        else:
            await first.answer("Открытого черновика нет.")
        return

    if cmd in ("sd2", "sd2fast"):
        old = drafts.get(key)
        if old:
            if old.panel_id:
                await safe(bot.delete_message(old.chat_id, old.panel_id))
            drop_draft(old)
        prompt, dur, ratio = parse_quick_args(rest)
        d = Draft(
            id=uuid.uuid4().hex[:10],
            chat_id=chat.id,
            chat_type=chat.type,
            owner_id=uid,
            owner_name=uname,
            model="fast" if cmd == "sd2fast" else "sd2",
            prompt=prompt,
            duration=dur or DEFAULT_DURATION,
            ratio=ratio or DEFAULT_RATIO,
            source_msg_id=first.message_id,
        )
        d.service_msgs.extend(x.message_id for x in msgs)
        drafts[key] = d
        drafts_by_id[d.id] = d
        warn = add_refs(d, msgs)
        await show_panel(d)
        if warn:
            await answer_service(d, first, warn)
        return

    if cmd:  # чужая/неизвестная команда
        return

    # обычное сообщение: текст = промпт, файлы = референсы
    d = drafts.get(key)
    if not d:
        has_media = any(extract_ref(x)[0] or extract_ref(x)[1] for x in msgs)
        if is_private(chat.type) and (has_media or raw):
            await first.answer("Сначала выберите модель: /sd2fast или /sd2 (можно сразу с промптом).\n/help — подробнее")
        return
    if not is_private(chat.type):
        replied = any(x.reply_to_message and x.reply_to_message.message_id == d.panel_id for x in msgs)
        if not replied:
            return
    d.service_msgs.extend(x.message_id for x in msgs)
    warn = add_refs(d, msgs)
    if raw:
        d.prompt = raw.strip()
    await show_panel(d, resend=True)
    if warn:
        await answer_service(d, first, warn)


def add_refs(d: Draft, msgs: list[Message]) -> str:
    warns = []
    for x in msgs:
        ref, err = extract_ref(x)
        if err:
            warns.append(err)
        if not ref:
            continue
        if d.count(ref.kind) >= REF_LIMITS[ref.kind]:
            name = {"imgs": "фото", "vids": "видео", "auds": "аудио"}[ref.kind]
            warns.append(f"Лимит {name}: не больше {REF_LIMITS[ref.kind]}, лишнее не добавил.")
            continue
        d.refs.append(ref)
    return "\n".join(dict.fromkeys(warns))


async def answer_service(d: Draft, m: Message, text: str):
    r = await safe(m.answer(text))
    if r:
        d.service_msgs.append(r.message_id)


# ----------------------------------------------------------------- кнопки
@router.callback_query(F.data.startswith("d:"))
async def on_panel_button(cb: CallbackQuery):
    _, did, action, *arg = cb.data.split(":", 3)
    arg = arg[0] if arg else ""
    d = drafts_by_id.get(did)
    if not d:
        await cb.answer("Черновик устарел — начните заново: /sd2 или /sd2fast", show_alert=True)
        return
    if not await may_control(d, cb):
        await cb.answer("Это не ваша панель 🙂 Свою можно открыть командой /sd2 или /sd2fast", show_alert=True)
        return

    if action == "noop":
        await cb.answer()
        return
    if action == "m":
        d.model = arg if arg in MODELS else d.model
    elif action == "dur":
        if arg.startswith("="):
            d.duration = int(arg[1:])
        else:
            d.duration = d.duration + int(arg)
        d.duration = max(DUR_MIN, min(DUR_MAX, d.duration))
    elif action == "r":
        d.ratio = arg if arg in RATIOS else d.ratio
    elif action == "clr":
        d.refs.clear()
    elif action == "x":
        await safe(cb.message.delete())
        drop_draft(d)
        await cb.answer("Отменено")
        return
    elif action == "go":
        await start_job(d, cb)
        return
    await safe(cb.message.edit_text(panel_text(d), reply_markup=panel_kb(d)))
    await cb.answer()


async def may_control(d: Draft, cb: CallbackQuery) -> bool:
    uid = cb.from_user.id
    if uid in ADMIN_IDS:
        return True
    if d.owner_id == 0:  # канал: управлять могут только админы канала
        return await is_chat_admin(d.chat_id, uid)
    return uid == d.owner_id


def user_active(uid: int) -> int:
    return sum(1 for j in [*pending, *running_jobs()] if j.user_id == uid and not j.cancel.is_set())


def running_jobs() -> list[Job]:
    return [j for j in jobs.values() if j.running]


async def start_job(d: Draft, cb: CallbackQuery):
    if not d.prompt.strip():
        await cb.answer("Сначала пришлите промпт — текстом в чат.", show_alert=True)
        return
    uid = cb.from_user.id
    if uid not in ADMIN_IDS:
        if DAILY_LIMIT and used_today(uid) >= DAILY_LIMIT:
            await cb.answer(f"Лимит на сегодня исчерпан ({DAILY_LIMIT} видео). Возвращайтесь завтра!", show_alert=True)
            return
        if MAX_ACTIVE_PER_USER and user_active(uid) >= MAX_ACTIVE_PER_USER:
            await cb.answer("У вас уже есть видео в работе — дождитесь его.", show_alert=True)
            return
    usage[(today(), uid)] = used_today(uid) + 1
    drop_draft(d)
    job = Job(id=uuid.uuid4().hex[:10], draft=d, status_msg_id=d.panel_id, user_id=uid)
    jobs[job.id] = job
    pending.append(job)
    await queue.put(job)
    await cb.answer("Поставил в очередь 🚀")
    await set_status(job, queued_text(job))


def job_header(d: Draft) -> str:
    refs = ", ".join(
        f"{d.count(k)} {n}" for k, n in (("imgs", "фото"), ("vids", "видео"), ("auds", "аудио")) if d.count(k)
    )
    return (
        f"🎬 <b>{MODELS[d.model]}</b> · {d.duration} сек · {d.ratio}"
        + (f" · 📎 {refs}" if refs else "")
        + f"\n📝 {html.escape(short(d.prompt, 300))}\n\n"
    )


def queued_text(job: Job) -> str:
    pos = pending.index(job) + 1 if job in pending else 1
    busy = len(running_jobs())
    if pos == 1 and busy < MAX_PARALLEL:
        return job_header(job.draft) + "⏳ Сейчас начну…"
    return job_header(job.draft) + f"⏳ В очереди: <b>{pos}</b>-й (сейчас в работе: {busy})"


def status_kb(job: Job) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отменить", callback_data=f"j:{job.id}:cancel")]]
    )


async def set_status(job: Job, text: str, kb: InlineKeyboardMarkup | None | bool = True):
    markup = status_kb(job) if kb is True else (kb or None)
    await safe(bot.edit_message_text(text=text, chat_id=job.draft.chat_id, message_id=job.status_msg_id, reply_markup=markup))


@router.callback_query(F.data.startswith("j:"))
async def on_job_button(cb: CallbackQuery):
    _, jid, action = cb.data.split(":", 2)
    job = jobs.get(jid)
    if not job:
        await cb.answer("Задание не найдено (бот мог перезапуститься).", show_alert=True)
        return
    if not await may_control(job.draft, cb):
        await cb.answer("Это не ваше задание.", show_alert=True)
        return
    if action == "cancel":
        job.cancel.set()
        if job in pending:
            pending.remove(job)
            await set_status(job, job_header(job.draft) + "✖️ Отменено", kb=None)
            usage[(today(), job.user_id)] = max(0, used_today(job.user_id) - 1)
        await cb.answer("Отменяю…")
    elif action == "retry":
        d = job.draft
        if job.running or job in pending:
            await cb.answer("Уже в работе")
            return
        new = Job(id=uuid.uuid4().hex[:10], draft=d, status_msg_id=job.status_msg_id, user_id=cb.from_user.id)
        jobs.pop(jid, None)
        jobs[new.id] = new
        pending.append(new)
        await queue.put(new)
        await cb.answer("Пробую ещё раз")
        await set_status(new, queued_text(new))
    elif action == "edit":
        # вернуть задание в черновик, чтобы поменять параметры
        d = job.draft
        jobs.pop(jid, None)
        d.id = uuid.uuid4().hex[:10]
        d.panel_id = job.status_msg_id
        drafts[(d.chat_id, d.owner_id)] = d
        drafts_by_id[d.id] = d
        await cb.answer()
        await show_panel(d)


# ----------------------------------------------------------------- воркер
async def worker(n: int):
    while True:
        job = await queue.get()
        try:
            if job.cancel.is_set():
                continue
            if job in pending:
                pending.remove(job)
            job.running = True
            await refresh_queue_positions()
            await run_job(job)
        except Exception:  # noqa: BLE001
            log.exception("worker %s упал на задании %s", n, job.id)
        finally:
            job.running = False
            queue.task_done()


async def refresh_queue_positions():
    for j in list(pending):
        await set_status(j, queued_text(j))


async def run_job(job: Job):
    d = job.draft
    job.started_at = time.monotonic()
    head = job_header(d)
    tmp = Path(tempfile.mkdtemp(prefix="okr_"))
    last_edit = [0.0, ""]

    async def on_status(text: str, force: bool = False):
        now = time.monotonic()
        if not force and (now - last_edit[0] < 6 or text == last_edit[1]):
            return
        last_edit[0], last_edit[1] = now, text
        await set_status(job, head + text)

    try:
        files: dict[str, list[Path]] = {"imgs": [], "vids": [], "auds": []}
        if d.refs:
            await on_status(f"📥 Скачиваю референсы из Telegram (0/{len(d.refs)})…", force=True)
            for i, r in enumerate(d.refs, 1):
                safe_name = re.sub(r"[^\w.\-]+", "_", r.name)[-80:] or f"ref{i}"
                p = tmp / f"{i:02d}_{safe_name}"
                await bot.download(r.file_id, destination=p)
                files[r.kind].append(p)
        url = await engine.generate(
            model=d.model,
            prompt=d.prompt,
            duration=d.duration,
            ratio=d.ratio,
            refs=files,
            on_status=on_status,
            cancel=job.cancel,
        )
        await on_status("📦 Видео готово, загружаю в Telegram…", force=True)
        await deliver(job, url, tmp)
    except Cancelled:
        await set_status(job, head + "✖️ Отменено", kb=None)
        usage[(today(), job.user_id)] = max(0, used_today(job.user_id) - 1)
    except GenerationError as e:
        await fail(job, str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("job %s", job.id)
        await fail(job, f"внутренняя ошибка: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def fail(job: Job, reason: str):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔁 Повторить", callback_data=f"j:{job.id}:retry"),
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"j:{job.id}:edit"),
            ]
        ]
    )
    await set_status(job, job_header(job.draft) + f"❌ Не получилось: {html.escape(short(reason, 500))}", kb=kb)


async def deliver(job: Job, url: str, tmp: Path):
    d = job.draft
    took = int(time.monotonic() - job.started_at)
    caption = (
        f"🎬 <b>{MODELS[d.model]}</b> · {d.duration} сек · {d.ratio}\n"
        f"<blockquote expandable>{html.escape(short(d.prompt, 800))}</blockquote>"
    )
    if not is_private(d.chat_type) and d.owner_id:
        caption += f"\n👤 {html.escape(d.owner_name)}"
    reply = None
    if d.chat_type != ChatType.CHANNEL and d.source_msg_id:
        reply = ReplyParameters(message_id=d.source_msg_id, allow_sending_without_reply=True)

    path = tmp / "result.mp4"
    size = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=60), follow_redirects=True) as c:
            async with c.stream("GET", url) as r:
                r.raise_for_status()
                with open(path, "wb") as fh:
                    async for chunk in r.aiter_bytes(1 << 16):
                        size += len(chunk)
                        if size > TG_UPLOAD_LIMIT:
                            break
                        fh.write(chunk)
    except Exception as e:  # noqa: BLE001
        log.warning("не скачал видео %s: %s", url, e)
        size = -1

    link_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬇️ Скачать видео", url=url)]])
    sent = None
    if 0 < size <= TG_UPLOAD_LIMIT:
        sent = await tg(
            lambda: bot.send_video(
                d.chat_id, FSInputFile(path, filename="video.mp4"), caption=caption,
                supports_streaming=True, reply_parameters=reply, reply_markup=link_kb,
                request_timeout=300,
            )
        )
    if not sent:  # большой файл или не скачался — пробуем отдать ссылкой
        sent = await tg(lambda: bot.send_video(d.chat_id, url, caption=caption, reply_parameters=reply, reply_markup=link_kb), tries=2)
    if not sent:
        await tg(lambda: bot.send_message(
            d.chat_id, caption + "\n\nВидео готово, но не влезло в Telegram (больше 50 МБ). Скачайте по кнопке:",
            reply_markup=link_kb, reply_parameters=reply,
        ))

    if d.chat_type == ChatType.CHANNEL and CHANNEL_CLEANUP:
        for mid in [job.status_msg_id, *d.service_msgs]:
            await safe(bot.delete_message(d.chat_id, mid))
    else:
        await set_status(job, job_header(d) + f"✅ Готово за {took // 60} мин {took % 60} сек", kb=False)
    jobs.pop(job.id, None)


def queue_text() -> str:
    run = len(running_jobs())
    return f"📊 В работе: <b>{run}</b> из {MAX_PARALLEL}\n⏳ В очереди: <b>{len(pending)}</b>"


# ----------------------------------------------------------------- запуск
async def on_startup():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    log.info("%s (@%s), движок: %s, сайт: %s", BOT_NAME, BOT_USERNAME, ENGINE, SITE_URL)
    # имя и описание, которые видят пользователи в профиле бота
    try:
        if (await bot.get_my_name()).name != BOT_NAME:
            await bot.set_my_name(name=BOT_NAME)
        await bot.set_my_short_description(short_description="🎬 Генерация видео SD 2 и SD 2 Fast прямо в Telegram")
        await bot.set_my_description(
            description=f"{BOT_NAME} делает видео по описанию.\n\n"
            "⚡ /sd2fast — быстрая модель\n💎 /sd2 — больше деталей\n\n"
            "Длительность 4–15 сек, форматы 16:9, 9:16 и 1:1, референсы: фото, видео, аудио. "
            "Можно добавить в группу или канал."
        )
    except Exception as e:  # noqa: BLE001 — профиль не критичен, бот работает дальше
        log.warning("не удалось обновить имя/описание бота: %s", e)
    await engine.start()
    for i in range(MAX_PARALLEL):
        asyncio.create_task(worker(i))
    cmds = [
        BotCommand(command="sd2fast", description="⚡ Видео SD 2 Fast"),
        BotCommand(command="sd2", description="💎 Видео SD 2"),
        BotCommand(command="queue", description="Очередь"),
        BotCommand(command="cancel", description="Закрыть черновик"),
        BotCommand(command="help", description="Как пользоваться"),
    ]
    await safe(bot.set_my_commands(cmds))


async def on_shutdown():
    await engine.stop()


async def health(_request):
    return web.json_response({"ok": True, "running": len(running_jobs()), "queued": len(pending)})


def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    allowed = ["message", "channel_post", "callback_query"]

    if WEBHOOK_BASE:  # режим вебхука (Render, Koyeb и т.п.)
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

        secret = re.sub(r"[^A-Za-z0-9_-]", "", BOT_TOKEN.split(":")[-1])[:64]
        path = "/tg/webhook"

        async def set_hook(*_):
            await bot.set_webhook(WEBHOOK_BASE.rstrip("/") + path, secret_token=secret, allowed_updates=allowed)

        dp.startup.register(set_hook)
        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path=path)
        setup_application(app, dp, bot=bot)
        web.run_app(app, host="0.0.0.0", port=PORT or 10000)
    else:  # long polling (VPS, свой ПК)
        async def run():
            if PORT:  # на всякий случай поднимем /health для хостингов, которые его ждут
                app = web.Application()
                app.router.add_get("/", health)
                app.router.add_get("/health", health)
                runner = web.AppRunner(app)
                await runner.setup()
                await web.TCPSite(runner, "0.0.0.0", PORT).start()
            await bot.delete_webhook(drop_pending_updates=False)
            await dp.start_polling(bot, allowed_updates=allowed)

        asyncio.run(run())


if __name__ == "__main__":
    main()
