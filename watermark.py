"""
Анимированный водяной знак NeyroTok для готовых видео.

  NeyroTok        ← Unbounded, серо-белый градиент с бегущим бликом
 ── PRIVATE ──    ← мелко, разрядка
   @username      ← кто генерировал

Знак полупрозрачный, плавно появляется в начале ролика (проявление + подъём)
и плавно исчезает в конце. Кадры знака рисуются в Pillow, на видео
накладываются через ffmpeg; звук копируется без перекодирования.
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
TITLE_FONT = FONTS / "Unbounded-800.ttf"
SMALL_FONT = FONTS / "Montserrat-600.ttf"
USER_FONT = FONTS / "Montserrat-500.ttf"

OPACITY = float(os.getenv("WATERMARK_OPACITY", "0.55"))  # общая прозрачность знака
POSITION = os.getenv("WATERMARK_POS", "br")  # br | bl | tr | tl | bc
SHIMMER_PERIOD = 2.8  # сек на один проход блика
GRAY, WHITE = 150, 255


def _font(path: Path, size: int):
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default(size=size)


def _spaced(draw: ImageDraw.ImageDraw, xy, text: str, font, tracking: float, fill=255):
    """Текст с разрядкой; возвращает ширину."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x - xy[0] - tracking


def _spaced_width(font, text: str, tracking: float) -> float:
    tmp = ImageDraw.Draw(Image.new("L", (1, 1)))
    return sum(tmp.textlength(ch, font=font) for ch in text) + tracking * max(0, len(text) - 1)


def build_mask(short_side: int, user: str) -> Image.Image:
    """Маска текста (L): белое = текст."""
    s = short_side
    f_title = _font(TITLE_FONT, max(18, round(s * 0.066)))
    f_small = _font(SMALL_FONT, max(9, round(s * 0.021)))
    f_user = _font(USER_FONT, max(10, round(s * 0.025)))
    title, small = "NeyroTok", "PRIVATE"
    t_track = f_title.size * 0.02
    s_track = f_small.size * 0.42

    tw = _spaced_width(f_title, title, t_track)
    sw = _spaced_width(f_small, small, s_track)
    line = f_small.size * 1.6
    gap = f_small.size * 0.7
    uw = _spaced_width(f_user, user, 0) if user else 0
    w = int(max(tw, sw + 2 * (line + gap), uw)) + 8
    t_box = f_title.getbbox(title)
    s_box = f_small.getbbox(small)
    u_box = f_user.getbbox(user or "A")
    th = t_box[3]
    sh = s_box[3] - s_box[1]
    uh = u_box[3] - u_box[1]
    pad = round(s * 0.012)
    h = int(th + pad * 1.3 + sh + (pad + uh if user else 0)) + 6

    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    _spaced(d, ((w - tw) / 2, 0), title, f_title, t_track)
    y = th + pad * 1.3
    sx = (w - sw) / 2
    _spaced(d, (sx, y - s_box[1]), small, f_small, s_track)
    ly = y + sh / 2
    lw = max(1, round(s * 0.0018))
    d.line([(sx - gap - line, ly), (sx - gap, ly)], fill=200, width=lw)
    d.line([(sx + sw + gap, ly), (sx + sw + gap + line, ly)], fill=200, width=lw)
    if user:
        y += sh + pad
        d.text(((w - uw) / 2, y - u_box[1]), user, font=f_user, fill=235)
    return m


def build_shimmer(w: int, h: int) -> Image.Image:
    """Широкая полоса серого градиента с белым бликом под наклоном; кадр = её сдвиг."""
    W = w * 3
    band = w * 0.28
    row = []
    for x in range(W):
        # три блика на ширину полосы, чтобы цикл был бесшовным при сдвиге на w
        dx = min(abs(x - c) for c in (w * 0.5, w * 1.5, w * 2.5))
        k = math.exp(-((dx / band) ** 2) * 2.2)
        base = GRAY + (WHITE - GRAY) * 0.25 * (0.5 + 0.5 * math.sin(x / w * math.pi * 2))
        row.append(int(min(255, base + (WHITE - base) * k)))
    strip = Image.new("L", (W, 1))
    strip.putdata(row)
    strip = strip.resize((W, h))
    # наклон блика ~20°
    shear = math.tan(math.radians(20))
    return strip.transform((W, h), Image.AFFINE, (1, shear, -shear * h, 0, 1, 0), resample=Image.BILINEAR)


def _ease_out(p: float) -> float:
    return 1 - (1 - p) ** 3


def _ease_in(p: float) -> float:
    return p ** 3


def anim(t: float, dur: float) -> tuple[float, float]:
    """(прозрачность 0..1, сдвиг по Y в долях высоты подъёма)."""
    if dur < 2.5:
        a_in, a_len, d_len = 0.1, min(0.6, dur * 0.25), min(0.5, dur * 0.2)
    else:
        a_in, a_len, d_len = 0.25, 0.9, 0.8
    d_start = dur - d_len - 0.1
    if t < a_in:
        return 0.0, 1.0
    if t < a_in + a_len:
        e = _ease_out((t - a_in) / a_len)
        return e, 1 - e
    if t > d_start + d_len:
        return 0.0, -0.6
    if t > d_start:
        e = _ease_in((t - d_start) / d_len)
        return 1 - e, -0.6 * e
    return 1.0, 0.0


def render_frames(out_dir: Path, w: int, h: int, fps: float, dur: float, user: str) -> tuple[int, int, int]:
    """Рисует PNG-кадры знака. Возвращает (ширина, высота, кадров)."""
    s = min(w, h)
    mask = build_mask(s, user)
    mw, mh = mask.size
    rise = round(s * 0.03)
    blur = max(2, round(s * 0.006))
    pad = blur * 3
    up = int(rise * 0.6)  # запас сверху: при исчезновении знак уплывает вверх
    cw, ch = mw + pad * 2, mh + pad * 2 + rise + up
    shadow = Image.new("L", (cw, mh + pad * 2), 0)
    shadow.paste(mask, (pad, pad))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    shimmer = build_shimmer(mw, mh)
    zero = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    black = Image.new("L", shadow.size, 0)

    n = max(1, int(math.ceil(dur * fps)))
    for i in range(n):
        t = i / fps
        a, dy = anim(t, dur)
        frame = zero.copy()
        if a > 0.003:
            op = a * OPACITY
            off = int((t % SHIMMER_PERIOD) / SHIMMER_PERIOD * mw)
            g = shimmer.crop((mw * 2 - off, 0, mw * 3 - off, mh))
            ta = mask.point(lambda v, k=op: int(v * k))
            text = Image.merge("RGBA", (g, g, g, ta))
            sh = Image.merge("RGBA", (black, black, black, shadow.point(lambda v, k=op * 0.45: int(v * k))))
            y = max(0, min(ch - shadow.height, int(up + dy * rise)))
            frame.alpha_composite(sh, (0, y))
            frame.alpha_composite(text, (pad, pad + y))
        frame.save(out_dir / f"wm_{i:05d}.png", compress_level=1)
    return cw, ch, rise


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
    num, _, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "25/1").partition("/")
    fps = float(num) / float(den or 1) if float(num or 0) else 25.0
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
    return {"w": w, "h": h, "fps": max(12.0, min(30.0, fps)), "dur": dur, "audio": audio.get("codec_name") if audio else None}


async def apply_watermark(src: Path, dst: Path, user: str,
                          on_progress: Callable[[float], Awaitable[None]] | None = None) -> Path:
    info = await video_info(src)
    if info["dur"] <= 0:
        raise RuntimeError("не удалось узнать длительность видео")
    tmp = Path(tempfile.mkdtemp(prefix="wm_"))
    try:
        cw, ch, rise = await asyncio.to_thread(render_frames, tmp, info["w"], info["h"], info["fps"], info["dur"], user)
        m = round(min(info["w"], info["h"]) * 0.04)
        pos = {
            "br": (f"W-w-{m}", f"H-h-{m - rise}"), "bl": (f"{m}", f"H-h-{m - rise}"),
            "tr": (f"W-w-{m}", f"{m}"), "tl": (f"{m}", f"{m}"), "bc": ("(W-w)/2", f"H-h-{m - rise}"),
        }.get(POSITION, (f"W-w-{m}", f"H-h-{m - rise}"))
        fc = (f"[0:v][1:v]overlay={pos[0]}:{pos[1]}:format=auto:eof_action=pass,"
              f"scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p[v]")
        args = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1", "-i", str(src),
                "-framerate", f"{info['fps']:.3f}", "-i", str(tmp / "wm_%05d.png"),
                "-filter_complex", fc, "-map", "[v]"]
        if info["audio"]:
            args += ["-map", "0:a:0"] + (["-c:a", "copy"] if info["audio"] in ("aac", "mp3") else ["-c:a", "aac", "-b:a", "160k"])
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-movflags", "+faststart", str(dst)]
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert p.stdout
        while True:
            line = await p.stdout.readline()
            if not line:
                break
            k, _, v = line.decode(errors="replace").strip().partition("=")
            if k == "out_time_us" and v.isdigit() and on_progress:
                await on_progress(min(0.99, int(v) / 1e6 / info["dur"]))
        err = (await p.stderr.read()).decode(errors="replace") if p.stderr else ""
        if await p.wait() != 0 or not dst.exists():
            raise RuntimeError("ffmpeg: " + err.strip()[-300:])
        return dst
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
