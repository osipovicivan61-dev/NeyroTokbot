"""
Наложение сетки на фото и видео.

Сетка рисуется в прозрачный PNG (Pillow) под размер кадра, затем:
  фото  → накладывается прямо в Pillow;
  видео → ffmpeg overlay (звук сохраняется), прогресс читается из -progress.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from PIL import Image, ImageDraw, ImageFont, ImageOps

log = logging.getLogger("grid")

COLORS = {
    "white": ("⚪", "белый", (255, 255, 255)),
    "black": ("⚫", "чёрный", (0, 0, 0)),
    "red": ("🔴", "красный", (255, 59, 48)),
    "green": ("🟢", "зелёный", (52, 199, 89)),
    "blue": ("🔵", "синий", (0, 122, 255)),
    "yellow": ("🟡", "жёлтый", (255, 214, 10)),
    "purple": ("🟣", "фиолетовый", (175, 82, 222)),
    "orange": ("🟠", "оранжевый", (255, 149, 0)),
}
STYLES = {"solid": "линия", "dash": "пунктир", "dot": "точки"}
SIZES = {"orig": "оригинал", "1080": "1080p", "720": "720p"}
PHI = 0.381966  # 1 - 1/φ


class GridStopped(Exception):
    """Обработку видео остановили кнопкой."""


@dataclass
class GridCfg:
    cols: int = 3
    rows: int = 3
    golden: bool = False  # линии по золотому сечению вместо равных клеток
    color: str = "white"  # ключ COLORS или "#RRGGBB"
    thickness: int = 2  # px для кадра 1080p, масштабируется под размер
    opacity: int = 75  # %
    style: str = "solid"
    diagonals: bool = False
    center: bool = False
    border: bool = False
    labels: bool = False
    size: str = "orig"  # для видео
    as_file: bool = False  # фото: отправить файлом без сжатия

    def rgb(self) -> tuple[int, int, int]:
        if self.color in COLORS:
            return COLORS[self.color][2]
        return parse_hex(self.color) or (255, 255, 255)

    def color_name(self) -> str:
        return COLORS[self.color][1] if self.color in COLORS else self.color.upper()

    def summary(self) -> str:
        g = "золотое сечение" if self.golden else f"{self.cols}×{self.rows}"
        extra = [n for f, n in ((self.diagonals, "диагонали"), (self.center, "центр"),
                                (self.border, "рамка"), (self.labels, "подписи")) if f]
        return (f"{g} · {self.color_name()} · {self.thickness} px · {self.opacity}% · {STYLES[self.style]}"
                + (f" · {', '.join(extra)}" if extra else ""))


def parse_hex(text: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"\s*#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\s*", text or "")
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _positions(n: int, golden: bool) -> list[float]:
    if golden:
        return [PHI, 1 - PHI]
    return [i / n for i in range(1, n)]


def _line(draw: ImageDraw.ImageDraw, p1, p2, width: int, style: str):
    if style == "solid":
        draw.line([p1, p2], fill=(255, 255, 255, 255), width=width)
        return
    (x1, y1), (x2, y2) = p1, p2
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if length == 0:
        return
    dash, gap = (width * 5, width * 3) if style == "dash" else (width, width * 2)
    dash, gap = max(dash, 2), max(gap, 2)
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    t = 0.0
    while t < length:
        e = min(t + dash, length)
        a, b = (x1 + ux * t, y1 + uy * t), (x1 + ux * e, y1 + uy * e)
        if style == "dot":
            r = width / 2 + 0.5
            cx, cy = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
        else:
            draw.line([a, b], fill=(255, 255, 255, 255), width=width)
        t += dash + gap


def make_overlay(w: int, h: int, cfg: GridCfg) -> Image.Image:
    """Прозрачный слой с сеткой размером w×h."""
    scale = min(w, h) / 1080
    lw = max(1, round(cfg.thickness * scale))
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    xs = [round(w * p) for p in _positions(cfg.cols, cfg.golden)]
    ys = [round(h * p) for p in _positions(cfg.rows, cfg.golden)]
    for x in xs:
        _line(d, (x, 0), (x, h), lw, cfg.style)
    for y in ys:
        _line(d, (0, y), (w, y), lw, cfg.style)
    if cfg.diagonals:
        _line(d, (0, 0), (w, h), lw, cfg.style)
        _line(d, (w, 0), (0, h), lw, cfg.style)
    if cfg.center:
        s = round(min(w, h) * 0.04)
        cx, cy = w // 2, h // 2
        d.line([(cx - s, cy), (cx + s, cy)], fill=(255, 255, 255, 255), width=lw + 1)
        d.line([(cx, cy - s), (cx, cy + s)], fill=(255, 255, 255, 255), width=lw + 1)
    if cfg.border:
        o = lw // 2
        d.rectangle([o, o, w - 1 - o, h - 1 - o], outline=(255, 255, 255, 255), width=lw)

    shape = layer.getchannel("A")
    r, g, b = cfg.rgb()
    alpha = shape.point(lambda a: a * cfg.opacity // 100)
    out = Image.new("RGBA", (w, h), (r, g, b, 0))
    out.putalpha(alpha)

    if cfg.labels and not cfg.golden:
        cell_w, cell_h = w / cfg.cols, h / cfg.rows
        fs = max(10, int(min(cell_w, cell_h) * 0.12))
        font = _font(fs)
        td = ImageDraw.Draw(out)
        pad = max(4, fs // 3)
        shadow = (0, 0, 0, 170) if sum((r, g, b)) > 380 else (255, 255, 255, 170)
        for ci in range(cfg.cols):
            for ri in range(cfg.rows):
                label = f"{_col_name(ci)}{ri + 1}"
                x, y = int(ci * cell_w) + pad + lw, int(ri * cell_h) + pad + lw
                td.text((x + 1, y + 1), label, font=font, fill=shadow)
                td.text((x, y), label, font=font, fill=(r, g, b, max(160, 255 * cfg.opacity // 100)))
    return out


def _col_name(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


# ----------------------------------------------------------------- фото
def apply_photo(src: Path, dst_dir: Path, cfg: GridCfg) -> Path:
    img = ImageOps.exif_transpose(Image.open(src))
    has_alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info
    base = img.convert("RGBA")
    base.alpha_composite(make_overlay(base.width, base.height, cfg))
    if cfg.as_file or has_alpha:
        out = dst_dir / "grid.png"
        base.save(out, "PNG", optimize=True)
    else:
        out = dst_dir / "grid.jpg"
        base.convert("RGB").save(out, "JPEG", quality=95, subsampling=0)
    return out


# ----------------------------------------------------------------- видео
async def _run(*args: str) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def probe(src: Path) -> dict:
    code, out, err = await _run(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(src)
    )
    if code != 0:
        raise RuntimeError("не удалось прочитать видео: " + err.strip()[-200:])
    info = json.loads(out)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise RuntimeError("в файле нет видеодорожки")
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    return {"has_audio": has_audio, "duration": dur}


def _scale_filter(size: str) -> str:
    if size == "orig":
        return ""
    t = int(size)
    # меньшая сторона = t, только уменьшаем
    return f"scale='if(lt(iw,ih),min({t},iw),-2)':'if(lt(iw,ih),-2,min({t},ih))',"


async def first_frame(src: Path, dst: Path, size: str) -> Path:
    sf = _scale_filter(size)
    vf = (sf + "scale=trunc(iw/2)*2:trunc(ih/2)*2") if sf else "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    code, _, err = await _run("ffmpeg", "-y", "-v", "error", "-ss", "0", "-i", str(src), "-frames:v", "1", "-vf", vf, str(dst))
    if code != 0 or not dst.exists():
        raise RuntimeError("не удалось взять кадр из видео: " + err.strip()[-200:])
    return dst


async def preview_video(src: Path, dst_dir: Path, cfg: GridCfg) -> Path:
    frame = await first_frame(src, dst_dir / "frame.png", cfg.size)
    img = Image.open(frame).convert("RGBA")
    img.alpha_composite(make_overlay(img.width, img.height, cfg))
    out = dst_dir / "preview.jpg"
    img.convert("RGB").save(out, "JPEG", quality=90)
    return out


async def apply_video(
    src: Path, dst_dir: Path, cfg: GridCfg, on_progress: Callable[[float], Awaitable[None]],
    cancel: asyncio.Event | None = None,
) -> Path:
    info = await probe(src)
    frame = await first_frame(src, dst_dir / "frame.png", cfg.size)
    w, h = Image.open(frame).size
    overlay = dst_dir / "overlay.png"
    make_overlay(w, h, cfg).save(overlay)
    out = dst_dir / "grid.mp4"
    sf = _scale_filter(cfg.size)
    fc = f"[0:v]{sf}scale=trunc(iw/2)*2:trunc(ih/2)*2[v];[v][1:v]overlay=0:0:format=auto,format=yuv420p[o]"
    args = [
        "ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
        "-i", str(src), "-i", str(overlay), "-filter_complex", fc, "-map", "[o]",
    ]
    if info["has_audio"]:
        args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart", str(out)]
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    dur = max(0.1, info["duration"])
    assert p.stdout
    try:
        while True:
            if cancel and cancel.is_set():
                p.kill()
                raise GridStopped()
            try:
                line = await asyncio.wait_for(p.stdout.readline(), timeout=2)
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            k, _, v = line.decode(errors="replace").strip().partition("=")
            if k == "out_time_us" and v.isdigit():
                await on_progress(min(0.99, int(v) / 1e6 / dur))
        err = (await p.stderr.read()).decode(errors="replace") if p.stderr else ""
        code = await p.wait()
    finally:
        if p.returncode is None:
            p.kill()
    if code != 0 or not out.exists():
        raise RuntimeError("ffmpeg: " + err.strip()[-300:])
    return out
