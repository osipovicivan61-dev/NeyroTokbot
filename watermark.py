"""
Анимированный водяной знак NeyroTok для готовых видео.

    ╭──────────────────╮
    │    NeyroTok      │   ← Unbounded, металлический серо-белый градиент + бегущий блик
    │     PRIVATE      │   ← мелко, с разрядкой
    │    @username     │   ← кто генерировал
    ╰──────────────────╯   ← полупрозрачная тёмная «таблетка» для читаемости на любом фоне

Чтобы было быстро, Pillow рисует только один бесшовный цикл блика (~2.4 с),
а ffmpeg его зацикливает, плавно проявляет в начале (подъём + fade) и гасит в конце.
Звук копируется без перекодирования.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("watermark")

FONTS = Path(__file__).with_name("fonts")
STYLE = os.getenv("WATERMARK_STYLE", "minimal")  # minimal | bold
TITLE_FONT = FONTS / ("Unbounded-500.ttf" if STYLE == "minimal" else "Unbounded-800.ttf")
SMALL_FONT = FONTS / "Montserrat-600.ttf"
USER_FONT = FONTS / "Montserrat-500.ttf"

OPACITY = float(os.getenv("WATERMARK_OPACITY", "0.88"))  # видимость текста
PILL_OPACITY = float(os.getenv("WATERMARK_BG", "0"))  # тёмная подложка: 0 = без неё, например 0.32 — с ней
POSITION = os.getenv("WATERMARK_POS", "br")  # br | bl | tr | tl | bc
PRESET = os.getenv("WATERMARK_PRESET", "ultrafast")  # скорость кодирования x264
STAR = os.getenv("WATERMARK_STAR", "0") == "1"  # искра ✦ перед NeyroTok
LOOP_SEC = 2.4  # длина цикла блика
LOOP_FPS = 20  # кадров в секунду у слоя знака (ffmpeg повторит под fps видео)


def _font(path: Path, size: int):
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default(size=size)


def _text_w(font, text: str, tracking: float = 0.0) -> float:
    d = ImageDraw.Draw(Image.new("L", (1, 1)))
    return sum(d.textlength(c, font=font) for c in text) + tracking * max(0, len(text) - 1)


def _draw_spaced(d: ImageDraw.ImageDraw, x: float, y: float, text: str, font, tracking: float, fill: int):
    for c in text:
        d.text((x, y), c, font=font, fill=fill)
        x += d.textlength(c, font=font) + tracking


def _star(size: float, k: float = 0.22) -> list[tuple[float, float]]:
    """Четырёхлучевая искра ✦ (многоугольник) с центром в (0,0)."""
    pts = []
    for i in range(8):
        a = math.pi / 4 * i - math.pi / 2
        r = size if i % 2 == 0 else size * k
        pts.append((r * math.cos(a), r * math.sin(a)))
    return pts


class Layout:
    """Геометрия знака под конкретный размер кадра."""

    def __init__(self, short_side: int, user: str):
        s = short_side
        self.s = s
        mini = STYLE == "minimal"
        self.f_title = _font(TITLE_FONT, max(16, round(s * (0.048 if mini else 0.060))))
        self.f_small = _font(SMALL_FONT, max(8, round(s * (0.016 if mini else 0.019))))
        self.f_user = _font(USER_FONT, max(9, round(s * (0.020 if mini else 0.023))))
        self.title, self.small, self.user = "NeyroTok", "PRIVATE", user
        self.t_track = self.f_title.size * (0.06 if mini else 0.01)
        self.s_track = self.f_small.size * 0.55
        self.star_r = self.f_title.size * 0.36 if STAR else 0.0
        self.star_gap = self.f_title.size * 0.30 if STAR else 0.0

        tb = self.f_title.getbbox(self.title)
        self.t_top, self.t_h = tb[1], tb[3] - tb[1]
        self.t_w = _text_w(self.f_title, self.title, self.t_track)
        sb = self.f_small.getbbox(self.small)
        self.s_top, self.s_h = sb[1], sb[3] - sb[1]
        self.s_w = _text_w(self.f_small, self.small, self.s_track)
        ub = self.f_user.getbbox(user or "A")
        self.u_top, self.u_h = ub[1], ub[3] - ub[1]
        self.u_w = _text_w(self.f_user, user) if user else 0

        self.line1_w = self.star_r * 2 + self.star_gap + self.t_w
        self.pad_x = round(s * 0.028)
        self.pad_y = round(s * 0.020)
        self.gap1 = round(s * (0.011 if mini else 0.014))
        self.gap2 = round(s * (0.006 if mini else 0.008))
        inner_w = max(self.line1_w, self.s_w, self.u_w)
        inner_h = self.t_h + self.gap1 + self.s_h + ((self.gap2 + self.u_h) if user else 0)
        self.w = int(inner_w + self.pad_x * 2)
        self.h = int(inner_h + self.pad_y * 2)
        self.shadow = max(2, round(s * 0.006))
        self.margin = self.shadow * 3  # запас вокруг под тень
        self.cw, self.ch = self.w + self.margin * 2, self.h + self.margin * 2


def _masks(L: Layout) -> tuple[Image.Image, Image.Image, Image.Image, tuple[int, int, int, int]]:
    """Маски: заголовок (для градиента), мелкий текст, подложка. + bbox звезды."""
    title = Image.new("L", (L.cw, L.ch), 0)
    small = Image.new("L", (L.cw, L.ch), 0)
    pill = Image.new("L", (L.cw, L.ch), 0)
    ox, oy = L.margin, L.margin
    ImageDraw.Draw(pill).rounded_rectangle(
        [ox, oy, ox + L.w - 1, oy + L.h - 1], radius=round(min(L.h * 0.30, L.s * 0.035)), fill=255
    )
    cx = ox + L.w / 2
    # строки прижаты к тому краю экрана, у которого стоит знак
    align = {"br": "right", "tr": "right", "bl": "left", "tl": "left"}.get(POSITION, "center")

    def at(width: float) -> float:
        if align == "right":
            return ox + L.w - L.pad_x - width
        if align == "left":
            return ox + L.pad_x
        return cx - width / 2

    y = oy + L.pad_y
    x1 = at(L.line1_w)
    td = ImageDraw.Draw(title)
    _draw_spaced(td, x1 + L.star_r * 2 + L.star_gap, y - L.t_top, L.title, L.f_title, L.t_track, 255)
    star_c = (x1 + L.star_r, y + L.t_h * 0.48)
    star_box = (int(star_c[0] - L.star_r * 1.4), int(star_c[1] - L.star_r * 1.4),
                int(star_c[0] + L.star_r * 1.4), int(star_c[1] + L.star_r * 1.4))
    y += L.t_h + L.gap1
    sd = ImageDraw.Draw(small)
    mini = STYLE == "minimal"
    _draw_spaced(sd, at(L.s_w), y - L.s_top, L.small, L.f_small, L.s_track, 165 if mini else 190)
    if L.user:
        y += L.s_h + L.gap2
        sd.text((at(L.u_w), y - L.u_top), L.user, font=L.f_user, fill=205 if mini else 235)
    return title, small, pill, star_box


def _metal(w: int, h: int, y0: int, y1: int) -> Image.Image:
    """Вертикальный «металлический» градиент: светлый верх → серая середина → светлый низ."""
    col = Image.new("L", (1, h))
    span = max(1, y1 - y0)
    vals = []
    for y in range(h):
        p = min(1.0, max(0.0, (y - y0) / span))
        v = 0.78 + 0.22 * math.cos(p * math.pi * 2) * 0.9  # 1.0 → 0.58 → 1.0
        vals.append(int(255 * max(0.5, min(1.0, v))))
    col.putdata(vals)
    return col.resize((w, h))


def render_loop(out_dir: Path, w: int, h: int, user: str) -> tuple[Layout, int]:
    """Рисует бесшовный цикл кадров знака. Возвращает (геометрию, число кадров)."""
    L = Layout(min(w, h), user)
    title, small, pill, star_box = _masks(L)
    mini = STYLE == "minimal"
    if mini:  # ровный светло-серый, без металлических полос
        metal = Image.new("L", (L.cw, L.ch), 200)
    else:
        metal = _metal(L.cw, L.ch, L.margin + L.pad_y, L.margin + L.pad_y + L.t_h)

    # тень под текстом
    text_all = Image.new("L", (L.cw, L.ch), 0)
    text_all.paste(title, (0, 0), title)
    text_all.paste(small, (0, 0), small)
    # без подложки тень плотнее, чтобы светлый текст читался и на светлом фоне
    sh_k = 0.55 if PILL_OPACITY > 0 else 1.0
    if mini:  # мягкая широкая тень, почти незаметная, только для читаемости
        blurred = text_all.filter(ImageFilter.GaussianBlur(L.shadow * 1.8))
        shadow_a = blurred.point(lambda v: min(255, int(v * 1.1 * sh_k * OPACITY)))
    else:
        blurred = text_all.filter(ImageFilter.GaussianBlur(L.shadow))
        shadow_a = blurred.point(lambda v: min(255, int(v * 1.6 * sh_k * OPACITY)))
    black = Image.new("L", (L.cw, L.ch), 0)
    base = Image.new("RGBA", (L.cw, L.ch), (0, 0, 0, 0))
    if PILL_OPACITY > 0:
        pill_rgba = Image.merge("RGBA", (black, black, black, pill.point(lambda v: int(v * PILL_OPACITY))))
        base.alpha_composite(pill_rgba)
        # тонкая светлая обводка подложки
        edge = Image.new("L", (L.cw, L.ch), 0)
        r = round(min(L.h * 0.30, L.s * 0.035))
        ImageDraw.Draw(edge).rounded_rectangle(
            [L.margin, L.margin, L.margin + L.w - 1, L.margin + L.h - 1], radius=r, outline=255,
            width=max(1, round(L.s * 0.0016)),
        )
        white = Image.new("L", (L.cw, L.ch), 255)
        base.alpha_composite(Image.merge("RGBA", (white, white, white, edge.point(lambda v: int(v * 0.16)))))
    base.alpha_composite(Image.merge("RGBA", (black, black, black, shadow_a)))
    small_rgba = Image.merge("RGBA", (Image.new("L", (L.cw, L.ch), 235),) * 3 + (small.point(lambda v: int(v * OPACITY)),))
    base.alpha_composite(small_rgba)

    n = max(2, round(LOOP_SEC * LOOP_FPS))
    band = L.w * (0.30 if mini else 0.16)  # в минимализме переход шире и мягче
    title_a = title.point(lambda v: int(v * OPACITY))
    sx0, sy0, sx1, sy1 = star_box
    scx, scy = (sx0 + sx1) / 2, (sy0 + sy1) / 2
    xs = list(range(L.cw))
    for i in range(n):
        ph = i / n
        # блик: проходит по знаку за первые ~65% цикла, потом пауза
        pos = -band * 2 + (L.cw + band * 4) * min(1.0, ph / 0.65)
        row = []
        for x in xs:
            dx = (x - pos)
            k = math.exp(-(dx / band) ** 2 * 1.6) if ph < 0.65 else 0.0
            if mini:
                k *= 0.85
            row.append(int(255 * k))
        shine = Image.new("L", (L.cw, 1))
        shine.putdata(row)
        shine = shine.resize((L.cw, L.ch))
        shine = shine.transform((L.cw, L.ch), Image.AFFINE, (1, 0.35, -0.35 * L.ch / 2, 0, 1, 0), resample=Image.BILINEAR)
        lum = Image.composite(Image.new("L", (L.cw, L.ch), 255), metal, shine)  # металл + белый блик
        frame = base.copy()
        frame.alpha_composite(Image.merge("RGBA", (lum, lum, lum, title_a)))
        if not STAR:
            frame.save(out_dir / f"wm_{i:04d}.png", compress_level=1)
            continue
        # искра: мерцание и лёгкий поворот, бесшовно по циклу
        tw = 0.5 - 0.5 * math.cos(ph * 2 * math.pi)
        sc = 0.82 + 0.28 * tw
        rot = math.radians(15 * math.sin(ph * 2 * math.pi))
        pts = [(scx + (x * math.cos(rot) - y * math.sin(rot)) * sc, scy + (x * math.sin(rot) + y * math.cos(rot)) * sc)
               for x, y in _star(L.star_r)]
        star = Image.new("L", (L.cw, L.ch), 0)
        ImageDraw.Draw(star).polygon(pts, fill=255)
        glow = star.filter(ImageFilter.GaussianBlur(max(1, L.star_r * 0.35))).point(lambda v, t=tw: int(v * (0.25 + 0.45 * t) * OPACITY))
        white = Image.new("L", (L.cw, L.ch), 255)
        frame.alpha_composite(Image.merge("RGBA", (white, white, white, glow)))
        sv = int(205 + 50 * tw)
        frame.alpha_composite(Image.merge("RGBA", (Image.new("L", (L.cw, L.ch), sv),) * 3 + (star.point(lambda v: int(v * OPACITY)),)))
        frame.save(out_dir / f"wm_{i:04d}.png", compress_level=1)
    return L, n


async def _run(*args: str) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def video_info(src: Path) -> dict:
    code, out, err = await _run("ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(src))
    if code != 0:
        raise RuntimeError(err.strip()[-200:])
    info = json.loads(out)
    v = next(s for s in info["streams"] if s.get("codec_type") == "video")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    rot = rot or int((v.get("tags") or {}).get("rotate", 0) or 0)
    if abs(rot) % 180 == 90:  # ffmpeg повернёт кадр при декодировании
        w, h = h, w
    dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    audio = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    return {"w": w, "h": h, "dur": dur, "audio": audio.get("codec_name") if audio else None}


def _timings(dur: float) -> tuple[float, float, float, float]:
    """(начало появления, длительность появления, начало исчезновения, длительность исчезновения)."""
    if dur < 2.5:
        a_len, d_len = min(0.5, dur * 0.25), min(0.4, dur * 0.2)
        return 0.05, a_len, max(0.1, dur - d_len - 0.05), d_len
    return 0.2, 0.8, dur - 0.95, 0.75


async def apply_watermark(src: Path, dst: Path, user: str,
                          on_progress: Callable[[float], Awaitable[None]] | None = None) -> Path:
    info = await video_info(src)
    dur = info["dur"]
    if dur <= 0:
        raise RuntimeError("не удалось узнать длительность видео")
    tmp = Path(tempfile.mkdtemp(prefix="wm_"))
    try:
        L, n = await asyncio.to_thread(render_loop, tmp, info["w"], info["h"], user)
        m = round(min(info["w"], info["h"]) * 0.035) - L.margin
        rise = round(L.s * 0.025)
        a0, al, d0, dl = _timings(dur)
        # подъём при появлении (ease-out) и уход вверх при исчезновении (ease-in)
        dy = (f"if(lt(t,{a0}),{rise},if(lt(t,{a0 + al}),{rise}*pow(1-(t-{a0})/{al},3),"
              f"if(gt(t,{d0}),-{rise * 0.6}*pow(min(1,(t-{d0})/{dl}),3),0)))")
        xpos = {"br": f"W-w-{m}", "bl": f"{m}", "tr": f"W-w-{m}", "tl": f"{m}", "bc": "(W-w)/2"}.get(POSITION, f"W-w-{m}")
        ybase = f"{m}" if POSITION in ("tr", "tl") else f"H-h-{m}"
        even = info["w"] % 2 == 0 and info["h"] % 2 == 0
        fc = (
            # знак переводим в yuva420p один раз на кадр цикла, наложение — сразу в yuv420p (дёшево)
            f"[1:v]format=yuva420p,loop=loop=-1:size={n}:start=0,setpts=N/{LOOP_FPS}/TB,"
            f"fade=t=in:st={a0}:d={al}:alpha=1,fade=t=out:st={d0}:d={dl}:alpha=1[wm];"
            f"[0:v]{'' if even else 'scale=trunc(iw/2)*2:trunc(ih/2)*2,'}format=yuv420p[base];"
            f"[base][wm]overlay=x='{xpos}':y='{ybase}+{dy}':eval=frame:shortest=1:format=yuv420[v]"
        )
        args = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1", "-i", str(src),
                "-framerate", str(LOOP_FPS), "-i", str(tmp / "wm_%04d.png"),
                "-filter_complex", fc, "-map", "[v]"]
        if info["audio"]:
            args += ["-map", "0:a:0"] + (["-c:a", "copy"] if info["audio"] in ("aac", "mp3") else ["-c:a", "aac", "-b:a", "160k"])
        args += ["-c:v", "libx264", "-preset", PRESET, "-crf", "21", "-threads", "0",
                 "-movflags", "+faststart", "-t", f"{dur:.3f}", str(dst)]
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert p.stdout
        while True:
            line = await p.stdout.readline()
            if not line:
                break
            k, _, v = line.decode(errors="replace").strip().partition("=")
            if k == "out_time_us" and v.isdigit() and on_progress:
                await on_progress(min(0.99, int(v) / 1e6 / dur))
        err = (await p.stderr.read()).decode(errors="replace") if p.stderr else ""
        if await p.wait() != 0 or not dst.exists():
            raise RuntimeError("ffmpeg: " + err.strip()[-300:])
        return dst
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
