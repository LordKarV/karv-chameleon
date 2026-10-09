"""Exact text and asset measurement.

Text is measured with the same skia font lookup the official renderer uses
(renderer.converter.wrap), so wrap/line-count decisions match the final PNG.
Falls back to a Pillow/heuristic estimate if skia is unavailable.
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "brand_kit" / "fonts"
ASSETS_DIR = ROOT / "brand_kit" / "assets"
DEFAULT_LINE_HEIGHT = 1.2

try:  # the vendored renderer ships in the container next to engine/
    import skia  # type: ignore
    from renderer.converter.brand_fonts import load_brand_font_manager
    from renderer.converter.wrap import wrap_text

    _FM = load_brand_font_manager(str(FONTS_DIR))
except Exception:  # pragma: no cover - degraded mode
    skia = None
    _FM = None

    def wrap_text(text, widths, max_width):  # minimal greedy wrap, same contract
        chars, line_w, last_sp = list(text), 0.0, None
        for i, c in enumerate(chars):
            w = widths[i] if i < len(widths) else 0.0
            if line_w + w > max_width and last_sp is not None:
                chars[last_sp] = "\n"
                line_w = sum(widths[last_sp + 1 : i + 1])
                last_sp = None
            else:
                line_w += w
            if c == " ":
                last_sp = i
        return "".join(chars)


def _style(el: dict) -> tuple[str, str]:
    bold = (el.get("font_weight") or "regular").lower() == "bold"
    italic = el.get("font_style") == "italic"
    weight = "bold" if bold else "regular"
    if bold and italic:
        return weight, "bolditalic"
    if bold:
        return weight, "bold"
    return weight, ("italic" if italic else "regular")


@lru_cache(maxsize=64)
def _typeface(family: str, weight: str, style: str):
    return skia.Typeface.MakeFromData(_FM.lookup(family, weight, style))


@lru_cache(maxsize=16384)
def _widths(family: str, weight: str, style: str, size: int, text: str) -> tuple:
    """Advances at the exact integer size the renderer draws with. Not scaled
    from a base size: on Linux (the scoring container) skia rounds advances
    per size, so linear scaling drifts by ~0.5 px per string."""
    if skia is None or _FM is None:
        return tuple(size * (0.32 if c == " " else 0.56) for c in text)
    font = skia.Font(_typeface(family, weight, style), size)
    return tuple(font.getWidths(font.textToGlyphs(text)))


def char_widths(el: dict, size: float) -> tuple:
    weight, style = _style(el)
    return _widths(el.get("font_family", ""), weight, style, max(1, int(size)), str(el.get("content", "")))


def line_pitch(el: dict, size: float) -> float:
    return max(1, int(size)) * float(el.get("line_height", DEFAULT_LINE_HEIGHT) or DEFAULT_LINE_HEIGHT)


def layout_text(el: dict, size: float, width: float) -> tuple[int, float, float]:
    """(n_lines, widest_line_px, longest_word_px) as the renderer will draw it."""
    weight, style = _style(el)
    return _layout(el.get("font_family", ""), weight, style, str(el.get("content", "")),
                   max(1, int(size)), math.floor(float(width) * 10) / 10, float(el.get("letter_spacing", 0) or 0))


@lru_cache(maxsize=65536)
def _layout(family, weight, style, text, size, width, ls) -> tuple[int, float, float]:
    el = {"font_family": family, "content": text, "letter_spacing": ls,
          "font_weight": weight, "font_style": "italic" if "italic" in style else "normal"}
    if not text:
        return 1, 0.0, 0.0
    widths = char_widths(el, size)
    ls = float(el.get("letter_spacing", 0) or 0)
    wrapped = wrap_text(text, list(widths), width)
    widest, cur, word, longest = 0.0, 0.0, 0.0, 0.0
    for c, w in zip(wrapped, widths):
        if c == "\n":
            widest, cur = max(widest, cur), 0.0
            longest, word = max(longest, word), 0.0
            continue
        cur += w + ls
        word = 0.0 if c == " " else word + w + ls
        longest = max(longest, word)
    return wrapped.count("\n") + 1, max(widest, cur), longest


@lru_cache(maxsize=64)
def _ink_em(family: str, weight: str, style: str) -> float:
    if skia is None or _FM is None:
        return 1.35
    m = skia.Font(_typeface(family, weight, style), 100).getMetrics()
    return (m.fDescent - m.fAscent) / 100.0


def text_height(el: dict, size: float, n_lines: int) -> float:
    """Ink height: lines are drawn top-aligned at pitch intervals; the last line
    adds ascent+descent rather than a full pitch."""
    weight, style = _style(el)
    ink = _ink_em(el.get("font_family", ""), weight, style) * max(1, int(size))
    return max(0, n_lines - 1) * line_pitch(el, size) + max(ink, line_pitch(el, size) * 0.9)


def single_line_width(el: dict, size: float) -> float:
    return layout_text(el, size, 1e9)[1]


def at_size(el: dict, size: float) -> dict:
    """The element as drawn at `size`: letter spacing scales with the font
    (it is a typographic em-relative choice), so spaced text keeps its look."""
    ls = float(el.get("letter_spacing", 0) or 0)
    if not ls:
        return el
    return {**el, "letter_spacing": ls * int(max(1, size)) / float(el.get("font_size") or size)}


def drawn_width(el: dict, size: float, width: float) -> float:
    """Widest line as painted (letter spacing between glyphs, not after the last)."""
    e = at_size(el, size)
    return layout_text(e, size, width)[1] - float(e.get("letter_spacing", 0) or 0)


def fit_text(el: dict, max_w: float, max_h: float, size_hi: float, size_lo: float = 1.0,
             max_lines: int | None = None) -> tuple[int, float, float] | None:
    """Largest integer font size in [size_lo, size_hi] whose wrapped text fits
    max_w x max_h with no word overflowing. Returns (size, width, height) with
    width shrunk to the balanced measure (same line count, fewer orphans)."""
    hi, lo = int(max(1, size_hi)), int(max(1, size_lo))
    if hi < lo:
        return None
    best = None
    while lo <= hi:  # monotone: bigger font never fits better
        mid = (lo + hi) // 2
        e = at_size(el, mid)
        n, widest, longest = layout_text(e, mid, max_w)
        ls = float(e.get("letter_spacing", 0) or 0)
        # the renderer wraps ignoring letter spacing but paints with it: the
        # painted line must fit too, not just the wrap
        ok = (longest <= max_w + 0.5 and widest - ls <= max_w + 0.5
              and text_height(e, mid, n) <= max_h + 0.5)
        if ok and max_lines:
            ok = n <= max_lines
        if ok:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if best is None:
        return None
    e = at_size(el, best)
    width = balanced_width(e, best, max_w)
    n, _, _ = layout_text(e, best, width)
    return best, width, text_height(e, best, n)


def balanced_width(el: dict, size: float, max_w: float) -> float:
    """Narrowest box width that keeps the same line count as max_w — evens out
    line lengths (no orphan words) and makes the box hug its text."""
    n0, widest0, _ = layout_text(el, size, max_w)
    if n0 == 1:
        return min(max_w, widest0 + 2)
    lo, hi = widest0 / n0, max_w
    for _ in range(14):
        mid = (lo + hi) / 2
        n, _, longest = layout_text(el, size, mid)
        if n <= n0 and longest <= mid:
            hi = mid
        else:
            lo = mid
    _, widest, _ = layout_text(el, size, hi)
    return min(max_w, widest + 2)


@lru_cache(maxsize=256)
def asset_alpha_bbox(asset_id: str) -> tuple[float, float, float, float, float] | None:
    """Visible-content bbox of an asset as fractions (l, t, r, b) + intrinsic aspect,
    or None if the asset is missing/unreadable."""
    try:
        from PIL import Image

        path = ASSETS_DIR / asset_id
        if not asset_id or not path.is_file():
            return None
        im = Image.open(path)
        w, h = im.size
        bbox = im.getchannel("A").getbbox() if im.mode in ("RGBA", "LA") else None
        bbox = bbox or (0, 0, w, h)
        return bbox[0] / w, bbox[1] / h, bbox[2] / w, bbox[3] / h, w / h
    except Exception:
        return None


def image_visible_rect(el: dict) -> tuple[float, float, float, float]:
    """Visible rect (x0, y0, x1, y1) of an image element as rendered (fit applied)."""
    x, y, w, h = el["x"], el["y"], el["width"], el["height"]
    info = asset_alpha_bbox(el.get("asset_id", ""))
    if info is None or w <= 0 or h <= 0:
        return x, y, x + w, y + h
    l, t, r, b, aspect = info
    fit = el.get("fit", "contain")
    if fit == "contain":
        cw, ch = (w, w / aspect) if w / h < aspect else (h * aspect, h)
        ox, oy = x + (w - cw) / 2, y + (h - ch) / 2
        return ox + l * cw, oy + t * ch, ox + r * cw, oy + b * ch
    if fit == "fill":
        return x + l * w, y + t * h, x + r * w, y + b * h
    return x, y, x + w, y + h  # cover: crops, treat as full box


if __name__ == "__main__":
    el = {"content": "Chase the Light", "font_family": "Solstice Serif", "font_weight": "bold", "font_size": 60}
    n, widest, _ = layout_text(el, 60, 800)
    assert n == 1 and 300 < widest < 800, (n, widest)
    fs, w, h = fit_text(el, 200, 400, 120)
    assert layout_text(el, fs, w)[0] * line_pitch(el, fs) <= 400 and w <= 200
    assert asset_alpha_bbox("solstice_bottle.png") is not None and asset_alpha_bbox("nope.png") is None
    print("measure ok", fs, round(w), h)
