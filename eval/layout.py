"""Geometry helpers that mirror what the official renderer actually draws.

- Text: wrapped with the renderer's own `char_widths`/`wrap_text`, glyphs drawn
  at int(font_size), top-aligned, line pitch font_size*line_height, never clipped.
- Images: visible extent = alpha bbox of the asset mapped through contain/cover/fill
  (same math as renderer/converter/assets.py). Missing assets render as a full-box
  placeholder.
- Rotation: elements rotate about their box centre; we use the rotated AABB.
"""

from __future__ import annotations

import io
import math
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np
import skia  # type: ignore
from PIL import Image

from renderer.converter.colorutil import parse_rgba
from renderer.converter.convert import (
    _BOLD_WEIGHTS,
    _get_renderer,
    _is_italic,
    _normalize_weight,
    _weight_style_to_font_style,
    convert_chameleon_to_crello,
)
from renderer.converter.wrap import char_widths, wrap_text

RENDER_MAX_SIZE = 1024  # run.py renders PNGs at this size too (disk); metrics resample to match
ALPHA_VISIBLE = 8  # alpha above this counts as visible ink

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1


def as_design(doc: dict) -> dict:
    """Accept a flat design, a {"source": ...} task, or a corpus design file."""
    return doc["source"] if "source" in doc else doc


def font_manager(fonts_dir: str):
    return _get_renderer(str(fonts_dir)).font_manager


def rect_area(r: Optional[Rect]) -> float:
    return 0.0 if r is None else max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def intersect(a: Rect, b: Rect) -> Optional[Rect]:
    r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return r if r[2] > r[0] and r[3] > r[1] else None


def box(el: dict) -> Rect:
    x, y = float(el.get("x", 0)), float(el.get("y", 0))
    return (x, y, x + float(el.get("width", 0)), y + float(el.get("height", 0)))


def _rotated_aabb(local: Rect, el: dict) -> Rect:
    """Map a rect in element-local coords to canvas coords (rotated AABB)."""
    x, y = float(el.get("x", 0)), float(el.get("y", 0))
    deg = float(el.get("rotation", 0) or 0)
    if deg == 0:
        return (x + local[0], y + local[1], x + local[2], y + local[3])
    cx, cy = float(el.get("width", 0)) / 2, float(el.get("height", 0)) / 2
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    pts = [(px - cx, py - cy) for px in (local[0], local[2]) for py in (local[1], local[3])]
    xs = [x + cx + px * c - py * s for px, py in pts]
    ys = [y + cy + px * s + py * c for px, py in pts]
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------- text ----

_TYPEFACES: dict[tuple, object] = {}


def _typeface(fm, family: str, weight: str, style: str):
    key = (id(fm), family, weight, style)
    if key not in _TYPEFACES:
        _TYPEFACES[key] = skia.Typeface.MakeFromData(fm.lookup(family, weight, style))
    return _TYPEFACES[key]


def _font_args(el: dict) -> tuple[str, str, str]:
    weight = _normalize_weight(el.get("font_weight"))
    style = _weight_style_to_font_style(
        weight in _BOLD_WEIGHTS, _is_italic(el.get("font_style") or el.get("italic"))
    )
    return str(el.get("font_family", "Montserrat")), weight, style


def text_layout(el: dict, fm) -> dict:
    """Rendered text metrics for a text element (local coords, unrotated)."""
    content = str(el.get("content", ""))
    size = float(el.get("font_size", 24))
    isize = max(1, int(size))
    width = float(el.get("width", 1.0))
    family, weight, style = _font_args(el)
    spacing = float(el.get("letter_spacing", 0.0) or 0.0)
    lh = float(el.get("line_height", 1.2) or 1.2)

    wrapped = wrap_text(content, char_widths(fm, family, weight, style, size, content), width)
    render_w = char_widths(fm, family, weight, style, isize, content)  # glyphs drawn at int size

    line_widths, start = [], 0
    for line in wrapped.split("\n"):
        line_widths.append(sum(render_w[start:start + len(line)]) + spacing * max(0, len(line) - 1))
        start += len(line) + 1

    metrics = skia.Font(_typeface(fm, family, weight, style), isize).getMetrics()
    glyph_h = metrics.fDescent - metrics.fAscent
    n = len(line_widths)
    height = (n - 1) * size * lh + glyph_h

    align = el.get("text_align", "left")
    offsets = [0.0 if align == "left" else (width - lw) / (2.0 if align == "center" else 1.0)
               for lw in line_widths]
    x0 = min(offsets) if offsets else 0.0
    x1 = max((o + lw for o, lw in zip(offsets, line_widths)), default=0.0)
    return {
        "lines": n,
        "height": height,
        "max_line_width": max(line_widths, default=0.0),
        "local_rect": (x0, 0.0, x1, height),
    }


# --------------------------------------------------------------- images ---

@lru_cache(maxsize=128)
def _alpha_bbox(path: str) -> Rect:
    """Visible (alpha>8) bbox of an asset as fractions of its size."""
    img = Image.open(path)
    w, h = img.size
    if "A" not in img.getbands():
        return (0.0, 0.0, 1.0, 1.0)
    bbox = img.getchannel("A").point(lambda a: 255 if a > ALPHA_VISIBLE else 0).getbbox()
    if bbox is None:
        return (0.0, 0.0, 0.0, 0.0)
    return (bbox[0] / w, bbox[1] / h, bbox[2] / w, bbox[3] / h)


@lru_cache(maxsize=128)
def _asset_size(path: str) -> tuple[int, int]:
    return Image.open(path).size


def _image_local_rect(el: dict, assets_dir: str) -> Rect:
    w, h = float(el.get("width", 0)), float(el.get("height", 0))
    path = Path(assets_dir) / str(el.get("asset_id", ""))
    if not el.get("asset_id") or not path.is_file():
        return (0.0, 0.0, w, h)  # placeholder fills the box
    fx0, fy0, fx1, fy1 = _alpha_bbox(str(path))
    fit = el.get("fit", "contain")
    if fit == "fill":
        return (fx0 * w, fy0 * h, fx1 * w, fy1 * h)
    iw, ih = _asset_size(str(path))
    s = (min if fit == "contain" else max)(w / iw, h / ih)
    nw, nh = iw * s, ih * s
    ox, oy = (w - nw) / 2, (h - nh) / 2
    r = (ox + fx0 * nw, oy + fy0 * nh, ox + fx1 * nw, oy + fy1 * nh)
    if fit == "cover":  # cropped to the box
        return intersect(r, (0.0, 0.0, w, h)) or (0.0, 0.0, 0.0, 0.0)
    return r


def visible_rect(el: dict, fm, assets_dir: str) -> Rect:
    """Canvas-space AABB of what actually gets painted for this element."""
    etype = el.get("type")
    if etype == "text":
        local = text_layout(el, fm)["local_rect"]
    elif etype == "image":
        local = _image_local_rect(el, assets_dir)
    else:
        local = (0.0, 0.0, float(el.get("width", 0)), float(el.get("height", 0)))
    return _rotated_aabb(local, el)


def effective_opacity(el: dict) -> float:
    op = float(el.get("opacity", 1.0) if el.get("opacity") is not None else 1.0)
    if el.get("type") == "shape":
        op *= parse_rgba(el.get("fill_color"), default=(0, 0, 0, 1.0))[3]
    return op


def text_colors(el: dict) -> list[tuple[int, int, int, float]]:
    colors = {el.get("color", "rgba(0,0,0,1)")} | {r["color"] for r in el.get("style_runs") or [] if "color" in r}
    return [parse_rgba(c) for c in sorted(colors)]


# -------------------------------------------------------------- render ----

def render(design: dict, assets_dir: str, fonts_dir: str, with_text: bool = True) -> Image.Image:
    """Render with the official renderer; optionally skip text (for backgrounds)."""
    renderer = _get_renderer(str(fonts_dir))
    example, _warnings = convert_chameleon_to_crello(design, renderer.font_manager, str(assets_dir))
    png = renderer.render(example, max_size=RENDER_MAX_SIZE, render_text=with_text, format="png")
    return Image.open(io.BytesIO(png)).convert("RGB")


def to_array(img: Image.Image) -> np.ndarray:
    return np.asarray(img, dtype=np.float32) / 255.0
