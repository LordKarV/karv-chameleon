"""Soft rubric rows, scored 0-1 with flags. Every check is relative to the source:
overlap / occlusion / empty space the designer already had is intentional.

C1 collisions (ink on ink, text occlusion) · C2 readability (px size at the official
PNG size, ink overflowing its box) · C3 presence (nothing shrunk to a speck or buried)
· C4 hierarchy (largest text stays largest) · C5 relations (containment / contact from
the source masks: a label in its pill, a shadow touching its product) · C6 dead zones.
Independent of engine/*: no roles, only pixels and JSON.
"""
from __future__ import annotations

import numpy as np

BG_COVER = 0.85            # an element whose ink covers this share of the canvas is background
COLLIDE_EXCESS = 0.02
OCCLUDE_DROP = 0.10
MIN_PX, MIN_PX_LARGEST = 8.0, 10.0   # OCR study: 10px reads almost always, 6px never
UNREADABLE_PX = 6.0
OVERFLOW_PX = 3
PRESENCE_KEEP = 0.25
CONTAIN = 0.9
CONTACT = 0.15
GRID = 32
DEADZONE_EXTRA = 0.15
FAINT = 0.5


def _flag(crit, sev, msg, ids=(), **ev):
    return {"criterion": crit, "severity": sev, "msg": msg, "ids": list(ids), "evidence": ev}


def _canvas_ink(f):
    return {k: f.inner(m) for k, m in f.ink.items()}


def _backgrounds(f) -> set:
    n = f.full.shape[0] * f.full.shape[1]
    return {k for k, m in _canvas_ink(f).items() if m.sum() >= BG_COVER * n}


def _frac(a, b) -> float:
    """Share of a's ink lying on b's ink."""
    return (a & b).sum() / max(1, a.sum())


def _covered(design, ink, k, bg) -> float:
    """Share of k's ink under the ink of elements painted above it (renderer order: z_index, then list order)."""
    order = sorted(range(len(design["elements"])), key=lambda i: (design["elements"][i].get("z_index", 0), i))
    rank = {design["elements"][i]["id"]: r for r, i in enumerate(order)}
    above = np.zeros_like(ink[k])
    for j, m in ink.items():
        if j != k and j not in bg and rank.get(j, -1) > rank[k]:
            above |= m
    return _frac(ink[k], above)


def _dilate(m: np.ndarray, r: int) -> np.ndarray:
    out = m.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= np.roll(np.roll(m, dy, 0), dx, 1)
    return out


def _text_body(design, ink, scale):
    """Text as the reader sees it: glyphs plus the space between them (overlapping
    lines rarely share glyph pixels, but they are unreadable)."""
    res = dict(ink)
    for e in design["elements"]:
        if e["type"] == "text" and e["id"] in ink:
            res[e["id"]] = _dilate(ink[e["id"]], max(1, min(6, round(0.3 * e["font_size"] * scale))))
    return res


def collisions(src, out, fs, fo, bg) -> list:
    flags = []
    si, oi = _text_body(src, _canvas_ink(fs), fs.scale), _text_body(out, _canvas_ink(fo), fo.scale)
    els = {e["id"]: e for e in out["elements"]}
    ids = [k for k in oi if k not in bg]
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            ga, gb = els[a].get("group_id"), els[b].get("group_id")
            if ga and ga == gb:
                continue
            small, big = (a, b) if oi[a].sum() <= oi[b].sum() else (b, a)
            now = _frac(oi[small], oi[big])
            was = _frac(si[small], si[big]) if small in si and big in si else 0.0
            if now - was > COLLIDE_EXCESS:
                text = "text" in (els[a]["type"], els[b]["type"])
                flags.append(_flag("collisions", "major" if text and now - was > 0.1 else "minor",
                                   f"{small} now overlaps {big} ({now:.0%} vs {was:.0%} in the source)",
                                   (small, big), now=now, was=was))
    for k, e in els.items():          # text hidden under ink drawn on top of it (z order)
        if e["type"] != "text" or k not in si:
            continue
        oc, sc = _canvas_ink(fo), _canvas_ink(fs)
        vis, vis0 = 1 - _covered(out, oc, k, bg), 1 - _covered(src, sc, k, bg)
        if vis0 - vis > OCCLUDE_DROP:
            flags.append(_flag("collisions", "major", f"{k} text is covered: {vis:.0%} visible vs {vis0:.0%}",
                               (k,), visible=vis, was=vis0))
    return flags


def readability(src, out, fo) -> list:
    flags = []
    W, H = out["canvas"]["width"], out["canvas"]["height"]
    official = min(1.0, 2048 / max(W, H))      # engine/main.py writes PNGs at most 2048 on the long side
    texts = [e for e in out["elements"] if e["type"] == "text" and str(e.get("content", "")).strip()]
    if not texts:
        return flags
    biggest = max(texts, key=lambda e: e["font_size"])
    for e in texts:
        px = e["font_size"] * official
        floor = MIN_PX_LARGEST if e is biggest else MIN_PX
        if px < floor:
            flags.append(_flag("readability", "major" if px < UNREADABLE_PX or e is biggest else "minor",
                               f"{e['id']} text is {px:.0f}px at the official size (floor {floor:.0f})",
                               (e["id"],), px=px))
        m = fo.ink.get(e["id"])
        if m is None or not m.sum() or e.get("rotation"):
            continue                     # rotated boxes: clipping is H5b's job
        s, p = fo.scale, fo.pad_px
        x0, y0 = int(e["x"] * s) + p - OVERFLOW_PX, int(e["y"] * s) + p - OVERFLOW_PX
        x1, y1 = int((e["x"] + e["width"]) * s) + p + OVERFLOW_PX, int((e["y"] + e["height"]) * s) + p + OVERFLOW_PX
        box = np.zeros_like(m)
        box[max(0, y0):y1, max(0, x0):x1] = True
        over = (m & ~box).sum() / m.sum()
        if over > 0.02:
            flags.append(_flag("readability", "minor", f"{e['id']} ink spills {over:.0%} outside its box",
                               (e["id"],), share=over))
    return flags


def presence(src, out, fs, fo, bg, missing: set) -> list:
    flags = []
    ns, no = fs.full.shape[0] * fs.full.shape[1], fo.full.shape[0] * fo.full.shape[1]
    for k in fo.visible:
        if k in bg or k in missing or k not in fs.visible:
            continue
        was, now = fs.visible[k].sum() / ns, fo.visible[k].sum() / no
        if was > 0.0005 and now < PRESENCE_KEEP * was:
            flags.append(_flag("presence", "major" if now < 0.1 * was else "minor",
                               f"{k} has almost vanished: {now:.2%} of the canvas vs {was:.2%}", (k,),
                               now=now, was=was))
    return flags


def hierarchy(src, out) -> list:
    st = {e["id"]: e["font_size"] for e in src["elements"] if e["type"] == "text"}
    ot = {e["id"]: e["font_size"] for e in out["elements"] if e["type"] == "text"}
    if len(st) < 2:
        return []
    top = max(st, key=st.get)
    beat = [k for k in ot if k != top and ot[k] > ot[top] + 0.5 and st[k] < st[top]]
    return [_flag("hierarchy", "major", f"{beat[0]} is now larger than the headline {top}", (beat[0], top))] if beat \
        else []


def _faint(e: dict) -> bool:
    from renderer.converter.colorutil import parse_rgba
    try:
        a = parse_rgba(e.get("fill_color"), default=(0, 0, 0, 1.0))[3]
    except Exception:       # noqa: BLE001
        a = 1.0
    return float(e.get("opacity", 1) or 0) * a < FAINT


def relations(src, out, fs, fo, bg) -> list:
    """Containment and contact recorded from the source must survive. Faint shapes
    (glows, watermark circles) are scenery, not hosts."""
    flags = []
    si, oi = _canvas_ink(fs), _canvas_ink(fo)
    faint = {e["id"] for e in src["elements"] if _faint(e)}
    ids = [k for k in si if k not in bg and k in oi and si[k].sum()]
    for a in ids:
        for b in ids:
            if a == b or b in faint or si[a].sum() > si[b].sum():
                continue
            was = _frac(si[a], si[b])
            if was < CONTACT:
                continue
            now = _frac(oi[a], oi[b])
            if was >= CONTAIN and now < 0.75:
                flags.append(_flag("relations", "major", f"{a} sat inside {b} and no longer does ({now:.0%})",
                                   (a, b), now=now, was=was))
            elif was < CONTAIN and now < 0.35 * was:
                flags.append(_flag("relations", "minor", f"{a} touched {b} ({was:.0%}) and is now detached ({now:.0%})",
                                   (a, b), now=now, was=was))
    return flags


def _largest_empty(occ: np.ndarray) -> float:
    """Largest all-False rectangle on the grid, as a share of the grid (histogram method)."""
    h = np.zeros(occ.shape[1], int)
    best = 0
    for row in occ:
        h = np.where(row, 0, h + 1)
        stack = []
        for i, v in enumerate(list(h) + [0]):
            start = i
            while stack and stack[-1][1] >= v:
                start, hv = stack.pop()
                best = max(best, hv * (i - start))
            stack.append((start, v))
    return best / occ.size


def _occupancy(f, bg) -> np.ndarray:
    m = np.zeros(f.full.shape[:2], bool)
    for k, v in f.visible.items():
        if k not in bg:
            m |= v
    H, W = m.shape
    ys, xs = np.linspace(0, H, GRID + 1).astype(int), np.linspace(0, W, GRID + 1).astype(int)
    return np.array([[m[ys[i]:ys[i + 1], xs[j]:xs[j + 1]].any() for j in range(GRID)] for i in range(GRID)])


def composition(fs, fo, bg_s, bg_o) -> list:
    was, now = _largest_empty(_occupancy(fs, bg_s)), _largest_empty(_occupancy(fo, bg_o))
    if now - was > DEADZONE_EXTRA:
        return [_flag("composition", "minor", f"a new empty area covers {now:.0%} of the canvas (source {was:.0%})",
                      now=now, was=was)]
    return []


def grade_soft(src, out, fs, fo, missing: set, extra=()) -> dict:
    bg = _backgrounds(fs) | _backgrounds(fo)
    flags = (collisions(src, out, fs, fo, bg) + readability(src, out, fo) + presence(src, out, fs, fo, bg, missing)
             + hierarchy(src, out) + relations(src, out, fs, fo, bg) + composition(fs, fo, bg, bg))
    seen = {(f["criterion"], tuple(f["ids"])) for f in flags}
    flags += [f for f in extra if (f["criterion"], tuple(f["ids"])) not in seen]   # OCR/VLM: new evidence only
    crits = {}
    for c in ("collisions", "readability", "presence", "hierarchy", "relations", "composition"):
        fl = [f for f in flags if f["criterion"] == c]
        pen = sum(0.5 if f["severity"] == "major" else 0.2 for f in fl)
        crits[c] = {"score": round(max(0.0, 1.0 - pen), 3), "flags": fl}
    return crits
