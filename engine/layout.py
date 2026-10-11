"""Layout templates. Every template returns {element_id: placement} where a
placement is {"x","y","width","height"[, "font_size"]} for flow + background +
decor elements. Templates: scale_fit (H1), stack, split, banner (H2)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from engine import measure
from engine.analyze import Design, Unit, union, area, placed_visual

FLOOR = {  # (absolute px, fraction of target short side) readability floors per role
    "headline": (14, 0.045), "cta": (10, 0.022), "subhead": (10, 0.02), "body": (9, 0.017),
    "button": (9, 0.017), "badge": (8, 0.015), "ribbon": (8, 0.015), "panel": (9, 0.017),
    "legal": (7, 0.011), "other": (9, 0.016),
}
ABS_MIN_TEXT = {"headline": 9.0, "cta": 8.0, "legal": 5.0}
LOW_PRIORITY = {"legal", "badge", "ribbon", "body", "missing", "line"}
SECONDARY = LOW_PRIORITY | {"subhead", "button", "panel", "other"}
KEEP_FLOOR = {"headline", "cta", "hero", "logo"}
IMG_FLOOR = {"hero": 0.3, "image": 0.18, "logo": 0.2, "missing": 0.18}
PIC_ROLES = ("hero", "image", "missing")   # min longest side, fraction of target short side


@dataclass
class Ctx:
    W: float
    H: float
    sW: float
    sH: float
    margin: float
    measure: float = 1.0        # text measure multiplier vs source (ems)
    hero_boost_max: float = 1.8
    floor_level: int = 0        # 0 all floors; 1 low-priority off; 2 secondary off; 3 only headline/cta; 4 none
    gap_mult: float = 1.0

    def squeeze(self, role: str) -> float:
        """Extra shrink for de-prioritised roles once floors are being relaxed."""
        if self.floor_level >= 1 and role in LOW_PRIORITY:
            return 0.75
        if self.floor_level >= 2 and role in SECONDARY:
            return 0.85
        return 1.0

    def floor(self, role: str) -> float:
        lvl = self.floor_level
        if (lvl >= 4 or (lvl >= 3 and role not in KEEP_FLOOR) or (lvl >= 2 and role in SECONDARY)
                or (lvl >= 1 and role in LOW_PRIORITY)):
            # relaxed, never below what a person can still read (judges call 3 px text "broken")
            return ABS_MIN_TEXT.get(role, 6.0) if role in FLOOR or role == "other" else 1.0
        a, f = FLOOR.get(role, FLOOR["other"])
        return max(a, f * min(self.W, self.H))

    @property
    def min_gap(self):
        return 0.012 * min(self.W, self.H) + 2


@dataclass
class Sized:
    w: float
    h: float
    place: callable                       # (x, y) -> {id: placement}
    box: tuple = (0.0, 0.0, 0.0, 0.0)     # element-box overhang beyond visual: l, t, r, b


def _text(u: Unit, k: float, max_w: float, ctx: Ctx) -> Sized:
    e = u.elements[0]
    fs0 = float(e["font_size"])
    k = k * ctx.squeeze(u.role)
    fs = max(fs0 * k, min(ctx.floor(u.role), fs0 * max(k, 1.0) * 1.5))
    ems = max(1.0, e["width"]) / fs0 * ctx.measure
    mw = max(4.0, min(max_w, ems * fs)) if u.role not in ("headline",) else max_w
    if u.role == "headline":   # the headline may use the full column; balanced wrapping keeps it tidy
        mw = max_w
    fit = measure.fit_text(e, mw, 1e9, fs, 1)
    if fit is None:
        fit = (1, mw, measure.text_height(e, 1, 1))
    size, w, h = fit

    def place(x, y, size=size, w=w, h=h):
        p = {"x": x, "y": y, "width": w, "height": h, "font_size": float(size)}
        if e.get("letter_spacing"):
            p["letter_spacing"] = measure.at_size(e, size)["letter_spacing"]
        return {e["id"]: p}
    return Sized(w, h, place)


def _rigid(u: Unit, s: float) -> Sized:
    els = u.elements + [m for c in u.children for m in _all_elements(c)]
    vx0, vy0 = u.rect[0], u.rect[1]
    boxes = [(e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"]) for e in els]
    from engine.analyze import rot_aabb
    bb = union(rot_aabb(*b, e.get("rotation", 0)) for b, e in zip(boxes, els))
    over = ((u.rect[0] - bb[0]) * s, (u.rect[1] - bb[1]) * s, (bb[2] - u.rect[2]) * s, (bb[3] - u.rect[3]) * s)

    def place(x, y):
        out = {}
        for e in els:
            p = {"x": x + (e["x"] - vx0) * s, "y": y + (e["y"] - vy0) * s,
                 "width": e["width"] * s, "height": e["height"] * s}
            if e["type"] == "text":
                p["font_size"] = float(max(1, math.floor(e["font_size"] * s + 1e-6)))
                if e.get("letter_spacing"):
                    p["letter_spacing"] = measure.at_size(e, p["font_size"])["letter_spacing"]
            out[e["id"]] = p
        return out
    return Sized(u.w * s, u.h * s, place, tuple(max(0.0, o) for o in over))


def _all_elements(u: Unit):
    return u.elements + [m for c in u.children for m in _all_elements(c)]


def rigid_floor(u: Unit, ctx: Ctx) -> float:
    """Smallest rigid scale that keeps every label legible and every logo /
    picture inside the unit at its floor size."""
    need = 0.0
    if u.kind == "text":
        return 0.0
    if u.texts and u.kind == "group":
        min_fs = min(float(e["font_size"]) for e in u.texts)
        need = max(need, ctx.floor(u.role) / min_fs)
    if u.role in IMG_FLOOR and ctx.floor(u.role) > 1.0 and not u.texts:
        need = max(need, IMG_FLOOR[u.role] * min(ctx.W, ctx.H) / max(u.w, u.h))
    for k in u.children:
        if k.kind == "text":
            need = max(need, ctx.floor(k.role) / float(k.elements[0]["font_size"]))
        elif k.role in IMG_FLOOR and ctx.floor(k.role) > 1.0:
            need = max(need, IMG_FLOOR[k.role] * min(ctx.W, ctx.H) / max(k.w, k.h))
        else:
            need = max(need, rigid_floor(k, ctx))
    return need


def size_unit(u: Unit, k: float, max_w: float, ctx: Ctx, img_boost: float = 1.0, max_h: float = 1e9) -> Sized:
    if u.kind == "text":
        return _text(u, k, max_w, ctx)
    s = k * ctx.squeeze(u.role) * (img_boost if u.role in PIC_ROLES else 1.0)
    s = max(s, rigid_floor(u, ctx))
    probe = _rigid(u, 1.0)
    full_w, full_h = u.w + probe.box[0] + probe.box[2], u.h + probe.box[1] + probe.box[3]
    s = min(s, max_w / u.w, max_h / u.h, ctx.W / full_w, ctx.H / full_h)
    return _clamped_rigid(u, max(s, 1e-3), ctx)  # padding / rotation overhang stays on canvas


# ---------------------------------------------------------------- rows / stacks

def _row_targets(row, sized, x0, x1, src_x0, src_x1, gap, family=frozenset(), canvas_w=1e9, dominant=None,
                 src_canvas=None):
    """Horizontal positions for a row: keep source alignment (centered / left /
    right / proportional), then push apart and clamp. Members of a look-alike
    family (cards, badges) are always centred as a cluster."""
    span = max(1e-6, src_x1 - src_x0)
    want = []
    in_family = all(_sibling_key(u) in family for u in row) and u_kinds_ok(row)
    for u, s in zip(row, sized):
        cx = (u.rect[0] + u.rect[2]) / 2
        if src_canvas and abs(cx - src_canvas / 2) < 0.03 * src_canvas and not in_family \
                and (len(row) == 1 or dominant in (None, "center")):
            want.append((x0 + x1) / 2 - s.w / 2)           # centred on the source canvas -> centred
        elif len(row) == 1 and dominant and not in_family:   # one alignment axis per column
            want.append({"left": x0, "right": x1 - s.w}.get(dominant, (x0 + x1) / 2 - s.w / 2))
        elif len(row) == 1 and (in_family or abs(cx - (src_x0 + src_x1) / 2) < 0.04 * span):
            want.append((x0 + x1) / 2 - s.w / 2)
        elif abs(u.rect[0] - src_x0) < 0.03 * span:
            want.append(x0)
        elif abs(u.rect[2] - src_x1) < 0.03 * span:
            want.append(x1 - s.w)
        else:
            want.append(x0 + (cx - src_x0) / span * (x1 - x0) - s.w / 2)
    if len(row) > 1 and (in_family or all(abs(((u.rect[0] + u.rect[2]) / 2) - (src_x0 + src_x1) / 2) < 0.3 * span
                                          for u in row)):
        total = sum(s.w for s in sized) + gap * (len(row) - 1)   # a centered cluster (badge row)
        cur, want = (x0 + x1) / 2 - total / 2, []
        for s in sized:
            want.append(cur)
            cur += s.w + gap
    xs = list(want)
    for i in range(len(xs)):   # an element box's transparent overhang must stay on the canvas
        lo = max(x0, sized[i].box[0]) if i == 0 else xs[i - 1] + sized[i - 1].w + gap
        xs[i] = max(xs[i], lo)
    for i in reversed(range(len(xs))):
        edge = min(x1, canvas_w - sized[i].box[2])
        hi = edge - sized[i].w if i == len(xs) - 1 else xs[i + 1] - gap - sized[i].w
        xs[i] = min(xs[i], hi)
    return [max(x0, s.box[0], x) for x, s in zip(xs, sized)]


def _dominant_alignment(rows, src_box):
    """How the column's text is aligned in the source (area-weighted):
    'left' / 'right' / 'center'."""
    votes = {"left": 0.0, "right": 0.0, "center": 0.0}
    span = max(1e-6, src_box[2] - src_box[0])
    for r in rows:
        if len(r) != 1 or r[0].kind != "text":
            continue
        u = r[0]
        al = u.elements[0].get("text_align", "left")
        if al == "left" and abs(u.rect[0] - src_box[0]) > 0.1 * span:
            al = "center" if abs((u.rect[0] + u.rect[2]) / 2 - (src_box[0] + src_box[2]) / 2) < 0.1 * span else al
        votes[al] += area(u.rect)
    best = max(votes, key=votes.get)
    return best if votes[best] > 0 else None


def u_kinds_ok(row):
    return all(u.kind in ("group", "container", "image") for u in row)


def _families(rows):
    count = {}
    for r in rows:
        for u in r:
            if u.kind in ("group", "container", "image"):
                count[_sibling_key(u)] = count.get(_sibling_key(u), 0) + 1
    return frozenset(k for k, n in count.items() if n >= 3)


def _size_row(row, k, box_w, ctx, img_boost, gap):
    n = len(row)
    sized = [size_unit(u, k, box_w if n == 1 else box_w / n - gap, ctx, img_boost) for u in row]
    total = sum(s.w for s in sized) + gap * (n - 1)
    if total > box_w + 0.5 and n > 1:
        f = (box_w - gap * (n - 1)) / max(1e-6, sum(s.w for s in sized))
        sized = [size_unit(u, k * f, s.w * f, ctx, img_boost) for u, s in zip(row, sized)]
    return sized


def _sibling_key(u: Unit):
    """Look-alike units: same kind and structure and similar heights (widths may
    differ, e.g. badges whose labels have different lengths)."""
    els = sorted(_elements_of(u), key=lambda e: e["z_index"])
    return (u.kind, len(els), tuple((e["type"], e.get("shape_type"), round(e["height"] / 20)) for e in els))


def _equalize_siblings(rows, sized_rows, ctx):
    """Look-alike rigid units (cards of a grid, badges of a set) share one scale
    -- the smallest any of them needed."""
    classes = {}
    for ri, (r, sr) in enumerate(zip(rows, sized_rows)):
        for ui, (u, s) in enumerate(zip(r, sr)):
            if u.kind in ("group", "container"):
                classes.setdefault(_sibling_key(u), []).append((ri, ui, u, s.w / u.w))
    out = [list(sr) for sr in sized_rows]
    for members in classes.values():
        if len(members) < 2:
            continue
        smin = min(m[3] for m in members)
        for ri, ui, u, sc in members:
            if sc > smin * 1.001:
                out[ri][ui] = _clamped_rigid(u, smin, ctx)
    return out


def _clamped_rigid(u, s, ctx):
    r = _rigid(u, s)
    raw = r.place

    def clamped(x, y):
        x = min(max(x, r.box[0]), ctx.W - r.w - r.box[2])
        y = min(max(y, r.box[1]), ctx.H - r.h - r.box[3])
        return raw(x, y)
    r.place = clamped
    return r


def _src_gaps(rows):
    gaps = []
    for a, b in zip(rows, rows[1:]):
        ra, rb = union(u.rect for u in a), union(u.rect for u in b)
        gaps.append(rb[1] - ra[3])
    return gaps


def stack(rows, box, k, ctx, img_boost=1.0, src_box=None):
    """Vertical stack of rows inside box=(x0,y0,x1,y1) at type scale k.
    Returns (total_height, placer(mode) -> placements)."""
    x0, y0, x1, y1 = box
    bw = x1 - x0
    src_box = src_box or union(u.rect for r in rows for u in r)
    gap_px = max(ctx.min_gap, 0.02 * bw)
    sized_rows = [_size_row(r, k, bw, ctx, img_boost, gap_px) for r in rows]
    sized_rows = _equalize_siblings(rows, sized_rows, ctx)
    heights = [max(s.h for s in sr) for sr in sized_rows]
    gaps = []
    for i, g in enumerate(_src_gaps(rows)):
        cap = 0.6 * (heights[i] + heights[i + 1]) / 2 + ctx.min_gap
        gaps.append(min(max(ctx.min_gap, g * k * ctx.gap_mult), cap))
    total = sum(heights) + sum(gaps)

    fams = _families(rows)
    dom = _dominant_alignment(rows, src_box)

    def placer(mode="center"):
        free = max(0.0, (y1 - y0) - total)
        g2 = list(gaps)
        if mode == "spread" and gaps:
            add = min(free, sum(gaps) * 0.6)
            g2 = [g + add * g / max(1e-6, sum(gaps)) for g in gaps]
            free -= add
        y = y0 + (free / 2 if mode in ("center", "spread") else 0.0)
        out = {}
        for i, (r, sr, h) in enumerate(zip(rows, sized_rows, heights)):
            xs = _row_targets(r, sr, x0, x1, src_box[0], src_box[2], gap_px, fams, ctx.W, dom,
                              ctx.sW if (x1 - x0) > 0.7 * ctx.W else None)
            for x, s in zip(xs, sr):
                out.update(s.place(x, y + (h - s.h) / 2))
            y += h + (g2[i] if i < len(g2) else 0)
        return out
    return total, placer


def solve_stack(rows, box, ctx, k_lo, k_hi, hero_fill=True):
    """Largest type scale k (<= k_hi) whose stack fits the box height; then grow
    images into leftover height. Relaxes floors if even k_lo overflows."""
    bh = box[3] - box[1]
    for level in range(ctx.floor_level, 5):
        ctx.floor_level = level
        lo, hi = k_lo * 0.25, k_hi
        if stack(rows, box, lo, ctx)[0] <= bh or level == 4:
            break
    best = lo
    for _ in range(18):
        mid = (lo + hi) / 2
        if stack(rows, box, mid, ctx)[0] <= bh:
            best, lo = mid, mid
        else:
            hi = mid
    boost = 1.0
    if hero_fill and any(u.role in PIC_ROLES for r in rows for u in r):
        lo_b, hi_b = 1.0, ctx.hero_boost_max
        for _ in range(12):
            mid = (lo_b + hi_b) / 2
            if stack(rows, box, best, ctx, mid)[0] <= bh:
                boost, lo_b = mid, mid
            else:
                hi_b = mid
    return best, boost, stack(rows, box, best, ctx, boost)


# ---------------------------------------------------------------- templates

def scale_ref(d: Design, W, H):
    s_min = min(W / d.W, H / d.H)
    s_area = math.sqrt(W * H / (d.W * d.H))
    return s_min, s_area


def place_background(d: Design, W, H):
    return {e["id"]: {"x": 0.0, "y": 0.0, "width": float(W), "height": float(H)} for e in d.background}


def place_anchored(elements, d: Design, W, H, s):
    """Decor / missing assets: keep relative anchor (center mapped proportionally),
    scale uniformly by s, keep fully inside the canvas."""
    out = {}
    for e in elements:
        w, h = e["width"] * s, e["height"] * s
        if w > W or h > H:
            f = min(W / max(w, 1e-6), H / max(h, 1e-6))
            w, h = w * f, h * f
        cx = (e["x"] + e["width"] / 2) / d.W * W
        cy = (e["y"] + e["height"] / 2) / d.H * H
        out[e["id"]] = {"x": min(max(0.0, cx - w / 2), W - w), "y": min(max(0.0, cy - h / 2), H - h),
                        "width": w, "height": h}
    return out


def scale_fit(d: Design, source, W, H):
    """H1: uniform scale of the whole composition, centered; background full-bleed."""
    s = min(W / d.W, H / d.H)
    ox, oy = (W - d.W * s) / 2, (H - d.H * s) / 2
    out = {}
    for e in source["elements"]:
        p = {"x": e["x"] * s + ox, "y": e["y"] * s + oy, "width": e["width"] * s, "height": e["height"] * s}
        if e["type"] == "text":
            p["font_size"] = float(max(1, math.floor(e["font_size"] * s + 1e-6)))
        out[e["id"]] = p
    out.update(place_background(d, W, H))
    return _container_shapes(d, W, H, out, False)  # edge panels still reach the canvas edges


def _container_shapes(d, W, H, flow, unpacked):
    """Unpacked containers: the shape hugs its laid-out children with padding
    scaled like the content. Then edge-touching panels grow to their canvas
    edges (all directions when there is no full-bleed background) and meet
    neighbouring panels halfway."""
    out = dict(flow)
    conts = [u for u in d.units if u.kind == "container"]
    def kids_rect(u):
        els = [e for k in u.children for e in _elements_of(k)]
        if not all(e["id"] in out for e in els):
            return None
        return union(placed_visual(e, out[e["id"]]) for e in els)
    if unpacked:
        for u in conts:
            r = kids_rect(u)
            if r is None:
                continue
            src = union(k.rect for k in u.children)
            sc = math.sqrt(max(1e-6, area(r)) / max(1e-6, area(src)))
            sc = min(3.0, max(0.15, sc))
            l, t, rr, b = (p * sc for p in u.pad)
            box = (max(0.0, r[0] - l), max(0.0, r[1] - t), min(W, r[2] + rr), min(H, r[3] + b))
            out[u.elements[0]["id"]] = {"x": box[0], "y": box[1], "width": box[2] - box[0], "height": box[3] - box[1]}
    regions = [u for u in conts if len(u.edges) >= 2 and u.elements[0]["id"] in out]
    if not regions:
        return out
    rects = {u.uid: _rect(out[u.elements[0]["id"]]) for u in regions}
    done = set()
    for u in regions:
        x0, y0, x1, y1 = rects[u.uid]
        dirs = "ltrb" if not d.background else "".join(u.edges)
        others = [(v.uid, rects[v.uid]) for v in regions if v is not u]
        def stop(lo_or_hi, cands, mine):
            return cands
        if "l" in dirs:
            bl = [r[2] if vid in done else (r[2] + x0) / 2 for vid, r in others
                  if r[2] <= x0 + 1 and min(r[3], y1) - max(r[1], y0) > 0]
            x0 = max(bl) if bl else 0.0
        if "r" in dirs:
            bl = [r[0] if vid in done else (r[0] + x1) / 2 for vid, r in others
                  if r[0] >= x1 - 1 and min(r[3], y1) - max(r[1], y0) > 0]
            x1 = min(bl) if bl else W
        if "t" in dirs:
            bl = [r[3] if vid in done else (r[3] + y0) / 2 for vid, r in others
                  if r[3] <= y0 + 1 and min(r[2], x1) - max(r[0], x0) > 0]
            y0 = max(bl) if bl else 0.0
        if "b" in dirs:
            bl = [r[1] if vid in done else (r[1] + y1) / 2 for vid, r in others
                  if r[1] >= y1 - 1 and min(r[2], x1) - max(r[0], x0) > 0]
            y1 = min(bl) if bl else H
        rects[u.uid] = (x0, y0, x1, y1)
        done.add(u.uid)
        out[u.elements[0]["id"]] = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
    return out


def _rect(p):
    return p["x"], p["y"], p["x"] + p["width"], p["y"] + p["height"]


def _elements_of(u: Unit):
    return u.elements + [e for k in u.children for e in _elements_of(k)]


def _avoid_text(d, out, decor_place, W, H):
    """Move each decoration to the nearest spot (from a grid) where it does not
    sit on text; decorations under images/shapes are fine (intentional layering)."""
    from engine.analyze import inter
    text_rects = [placed_visual(e, out[e["id"]]) for u in d.units for e in _elements_of(u)
                  if e["type"] == "text" and e["id"] in out]
    res = {}
    for i, p in decor_place.items():
        w, h = p["width"], p["height"]
        ax, ay = p["x"], p["y"]
        def cost(x, y):
            r = (x, y, x + w, y + h)
            hit = sum(inter(r, t) for t in text_rects)
            return hit * 1e3 / max(1.0, w * h) + math.hypot(x - ax, y - ay) / max(W, H)
        best = (cost(ax, ay), ax, ay)
        if best[0] > 1.0:
            n = 12
            for gx in range(n + 1):
                for gy in range(n + 1):
                    x, y = (W - w) * gx / n, (H - h) * gy / n
                    c = cost(x, y)
                    if c < best[0]:
                        best = (c, x, y)
        res[i] = {**p, "x": best[1], "y": best[2]}
    return res


def _finish(d, W, H, flow, unpacked=False):
    s_min, s_area = scale_ref(d, W, H)
    out = _container_shapes(d, W, H, flow, unpacked)
    out.update(place_background(d, W, H))
    from engine.relations import anchor_decor, snap   # decorations follow their source relation
    out = snap(d, out, W, H)                           # exact alignment where the layout is close
    out.update(_avoid_text(d, out, anchor_decor(d, W, H, out, (s_min + s_area) / 2), W, H))
    out.update(_place_stickers(d, W, H, out))
    out.update(_place_corners(d, W, H))
    out.update(_place_missing(d, W, H, out))
    return out


def _place_corners(d, W, H):
    """Corner ribbons: flush to the same canvas corner, sized like the design."""
    res = {}
    s_min, s_area = scale_ref(d, W, H)
    ctx = Ctx(W, H, d.W, d.H, 0)
    for u in d.units:
        if not u.corner:
            continue
        sc = max(math.sqrt(s_min * s_area), rigid_floor(u, ctx))
        sz = _rigid(u, sc)
        full_w, full_h = sz.w + sz.box[0] + sz.box[2], sz.h + sz.box[1] + sz.box[3]
        if full_w > 0.45 * W or full_h > 0.45 * H:
            f = min(0.45 * W / full_w, 0.45 * H / full_h)
            sz = _rigid(u, sc * f)
        x = sz.box[0] if u.corner[1] == "l" else W - sz.w - sz.box[2]
        y = sz.box[1] if u.corner[0] == "t" else H - sz.h - sz.box[3]
        res.update(sz.place(x, y))
    return res


def _place_stickers(d, W, H, out):
    """Pinned stickers follow their host: same relative centre, host's scale."""
    res = {}
    def walk(us):
        for u in us:
            yield u
            yield from walk(u.children)
    by_uid = {u.uid: u for u in walk(d.units)}
    for u in d.units:
        h = by_uid.get(u.host) if u.host else None
        if h is None:
            continue
        els = _elements_of(h)
        if not all(e["id"] in out for e in els):
            continue
        r = union(placed_visual(e, out[e["id"]]) for e in els)
        sc = (r[2] - r[0]) / h.w
        ctx = Ctx(W, H, d.W, d.H, 0)
        sz = _rigid(u, sc)
        x = r[0] + u.rel[0] * (r[2] - r[0]) - sz.w / 2
        y = r[1] + u.rel[1] * (r[3] - r[1]) - sz.h / 2
        x = min(max(x, sz.box[0]), W - sz.w - sz.box[2])
        y = min(max(y, sz.box[1]), H - sz.h - sz.box[3])
        res.update(sz.place(x, y))
    return res


def _place_missing(d, W, H, taken):
    """Missing assets render as grey placeholders: keep them small, in the
    emptiest corner."""
    out = {}
    s_min, _ = scale_ref(d, W, H)
    occupied = [(p["x"], p["y"], p["x"] + p["width"], p["y"] + p["height"]) for i, p in taken.items()
                if i not in {e["id"] for e in d.background + d.decor}]
    for u in d.missing:
        e = u.elements[0]
        w, h = e["width"] * s_min, e["height"] * s_min
        cap = 0.12 * min(W, H) / max(1e-6, min(w, h)) if min(w, h) > 0.12 * min(W, H) else 1.0
        w, h = w * cap, h * cap
        m = 0.02 * min(W, H)
        corners = [(W - w - m, H - h - m), (m, H - h - m), (W - w - m, m), (m, m)]
        def clash(c):
            r = (c[0], c[1], c[0] + w, c[1] + h)
            from engine.analyze import inter
            return sum(inter(r, o) for o in occupied)
        x, y = min(corners, key=clash)
        out[e["id"]] = {"x": max(0.0, x), "y": max(0.0, y), "width": w, "height": h}
        occupied.append((x, y, x + w, y + h))
    return out


def _reorder_pictures(rows, hero_pos):
    """Move picture-only rows (a side product) to the top / after the headline /
    to the bottom of the reading order."""
    if hero_pos == "source":
        return rows
    pic = [r for r in rows if all(u.role in PIC_ROLES for u in r)]
    if not pic:
        return None
    rest = [r for r in rows if r not in pic]
    if hero_pos == "top":
        i = 1 if rest and any(u.role == "logo" for u in rest[0]) else 0
    elif hero_pos == "after_headline":
        i = next((j + 1 for j, r in enumerate(rest) if any(u.role == "headline" for u in r)), len(rest))
    else:
        i = next((j for j, r in enumerate(rest) if any(u.role in ("cta", "legal") for u in r)), len(rest))
    return rest[:i] + pic + rest[i:]


def explode_rows(rows):
    """Rows holding a large unit (a panel or a product beside text) become one
    row per unit, left-to-right order -> top-to-bottom. Small-item rows (badges)
    stay. Returns None when nothing changes."""
    span = union(u.rect for r in rows for u in r)
    cw = max(1e-6, span[2] - span[0])
    out, changed = [], False
    for r in rows:
        if len(r) > 1 and any(u.w > 0.35 * cw for u in r):
            out += [[u] for u in r]
            changed = True
        else:
            out.append(r)
    return out if changed else None


def regrid_rows(rows, cols):
    """Re-flow a family of >=3 look-alike units (cards, badges) occupying
    consecutive rows into rows of `cols` units. None if no such family."""
    fam = {}
    for i, r in enumerate(rows):
        for u in r:
            if u.kind in ("group", "container", "image"):
                fam.setdefault(_sibling_key(u), []).append(i)
    key = next((k for k, idx in fam.items() if len(idx) >= 3), None)
    if key is None:
        return None
    idx = sorted(set(fam[key]))
    if idx != list(range(idx[0], idx[-1] + 1)):
        return None
    block = [u for i in idx for u in rows[i]]
    if any(_sibling_key(u) != key for u in block):
        return None
    new = [block[j:j + cols] for j in range(0, len(block), cols)]
    if [len(r) for r in new] == [len(rows[i]) for i in idx]:
        return None
    return rows[:idx[0]] + new + rows[idx[-1] + 1:]


def grid_family_size(rows):
    fam = {}
    for r in rows:
        for u in r:
            if u.kind in ("group", "container", "image"):
                fam[_sibling_key(u)] = fam.get(_sibling_key(u), 0) + 1
    return max(fam.values(), default=0)


def proportional_layout(d: Design, W, H, ctx: Ctx, size_mult=1.0):
    """What human designers do in Crello template families (178 human resizes in
    the adobedata set): every element keeps its RELATIVE position on the page and
    is resized by the geometric mean of the axis scales sqrt(sx*sy) (median human
    ratio x1.00). Images keep their proportions, text re-fits a box whose width
    scales with the canvas width, overlaps the source did not have are pushed apart."""
    sx, sy = W / d.W, H / d.H
    s = math.sqrt(sx * sy) * size_mult
    units = [u for r in d.rows for u in r]
    placed = []                                   # (unit, Sized, x, y)
    for u in units:
        cx, cy = (u.rect[0] + u.rect[2]) / 2 * sx, (u.rect[1] + u.rect[3]) / 2 * sy
        if u.kind == "text":
            e = u.elements[0]
            box_w = min(W - 2 * ctx.margin, max(8.0, e["width"] * sx))
            sz = _text(u, s, box_w, ctx)
            al = e.get("text_align", "left")
            x = {"left": u.rect[0] * sx, "right": u.rect[2] * sx - sz.w}.get(al, cx - sz.w / 2)
        else:
            sz = size_unit(u, s, W - 2 * ctx.margin, ctx, max_h=H - 2 * ctx.margin)
            x = cx - sz.w / 2
        placed.append([u, sz, x, cy - sz.h / 2])
    for _ in range(12):                           # push apart new overlaps (smaller unit moves)
        moved = False
        for i, a in enumerate(placed):
            for b in placed[i + 1:]:
                ra = (a[2], a[3], a[2] + a[1].w, a[3] + a[1].h)
                rb = (b[2], b[3], b[2] + b[1].w, b[3] + b[1].h)
                ox, oy = min(ra[2], rb[2]) - max(ra[0], rb[0]), min(ra[3], rb[3]) - max(ra[1], rb[1])
                if ox <= 0 or oy <= 0:
                    continue
                from engine.analyze import inter
                if inter(a[0].rect, b[0].rect) > 0:     # they overlapped in the source: intentional
                    continue
                mv = a if a[1].w * a[1].h < b[1].w * b[1].h else b
                other = b if mv is a else a
                if ox < oy:
                    mv[2] += ox + 2 if (mv[2] + mv[1].w / 2) >= (other[2] + other[1].w / 2) else -(ox + 2)
                else:
                    mv[3] += oy + 2 if (mv[3] + mv[1].h / 2) >= (other[3] + other[1].h / 2) else -(oy + 2)
                moved = True
        if not moved:
            break
    flow = {}
    for u, sz, x, y in placed:
        x = min(max(x, ctx.margin * 0.5), W - sz.w - ctx.margin * 0.5)
        y = min(max(y, ctx.margin * 0.5), H - sz.h - ctx.margin * 0.5)
        flow.update(sz.place(x, y))
    return _finish(d, W, H, flow)


def stack_layout(d: Design, W, H, ctx: Ctx, k_cap=1.25, mode="center", unpack=False, hero_pos="source",
                 explode=False, cols=None):
    s_min, s_area = scale_ref(d, W, H)
    m = ctx.margin
    box = (m, m, W - m, H - m)
    rows = d.open_rows if unpack else d.rows
    if explode:
        rows = explode_rows(rows)
        if rows is None:
            return None
    if cols:
        rows = regrid_rows(rows, cols)
        if rows is None:
            return None
    rows = _reorder_pictures(rows, hero_pos)
    if rows is None:
        return None
    k, boost, (total, placer) = solve_stack(rows, box, ctx, s_min, max(s_min, s_area) * k_cap)
    return _finish(d, W, H, placer(mode), unpack)


def split_layout(d: Design, W, H, ctx: Ctx, frac=0.45, image_side="auto", k_cap=1.25, mode="center", unpack=False):
    """H2 split: picture column + text column side by side (wide targets)."""
    rows0 = d.open_rows if unpack else d.rows
    pics = [u for r in rows0 for u in r if u.role in PIC_ROLES]
    if not pics:
        return None
    rest = [[u for u in r if u not in pics] for r in rows0]
    rest = [r for r in rest if r]
    if not rest:
        return None
    s_min, s_area = scale_ref(d, W, H)
    m = ctx.margin
    if image_side == "auto":
        pc = sum((u.rect[0] + u.rect[2]) / 2 for u in pics) / len(pics)
        tc = sum((u.rect[0] + u.rect[2]) / 2 for r in rest for u in r) / sum(len(r) for r in rest)
        image_side = "left" if pc <= tc + 1 else "right"
    elif image_side == "flip":   # the mirror of the source arrangement
        pc = sum((u.rect[0] + u.rect[2]) / 2 for u in pics) / len(pics)
        tc = sum((u.rect[0] + u.rect[2]) / 2 for r in rest for u in r) / sum(len(r) for r in rest)
        image_side = "right" if pc <= tc + 1 else "left"
    gutter = max(ctx.min_gap, 0.03 * W)
    iw = (W - 2 * m - gutter) * frac
    ibox = (m, m, m + iw, H - m) if image_side == "left" else (W - m - iw, m, W - m, H - m)
    tbox = (m + iw + gutter, m, W - m, H - m) if image_side == "left" else (m, m, W - m - iw - gutter, H - m)
    k, _, (_, tplacer) = solve_stack(rest, tbox, ctx, s_min, max(s_min, s_area) * k_cap, hero_fill=False)
    prow = [[u for u in r if u in pics] for r in rows0]   # keep pictures that shared a row together
    prow = [r for r in prow if r]
    _, _, (_, iplacer) = solve_stack(prow, ibox, Ctx(**{**ctx.__dict__, "hero_boost_max": 4.0}),
                                     s_min, max(s_min, s_area) * 3)
    flow = tplacer(mode)
    flow.update(iplacer("center"))
    return _finish(d, W, H, flow, unpack)


def banner_layout(d: Design, W, H, ctx: Ctx, badges_in_text=True, logo_slot=True, cta_boost=1.0, k_cap=1.25,
                  unpack=True):
    """H2 banner: one horizontal row of slots for extreme wide targets:
    [logo] [pictures] [text stack] [badges] [cta, ribbon], all at one shared
    type scale k, maximised so the row fits the width."""
    units = [u for r in (d.open_rows if unpack else d.rows) for u in r]
    m = ctx.margin
    bh = H - 2 * m
    gap = max(ctx.min_gap, 0.02 * W)
    s_min, s_area = scale_ref(d, W, H)
    logo = [u for u in units if u.role == "logo"] if logo_slot else []
    pics = [u for u in units if u.role in ("hero", "image")]  # a missing non-hero asset rides in the text stack
    ctas = [u for u in units if u.role in ("cta", "ribbon") and not u.parent]
    badges = [u for u in units if u.role in ("badge", "button") and not u.parent] if not badges_in_text else []
    pics = [u for u in pics if not u.parent]
    logo = [u for u in logo if not u.parent]
    text_units = [u for u in units if u not in logo + pics + ctas + badges]
    if not text_units:
        return None
    rows = _merge_badge_rows([[u] for u in sorted(text_units, key=lambda u: (u.rect[1], u.rect[0]))])
    left = [size_unit(u, 99, 0.18 * W, ctx, max_h=0.75 * bh) for u in logo]
    left += [size_unit(u, 99, 0.25 * W, ctx, max_h=bh) for u in pics]
    fixed_w = sum(s.w for s in left) + (max(0.0, left[0].box[0] - m) if left else 0.0)

    def right_slots(k):
        out = []
        if badges:
            bs = [size_unit(u, k, 0.2 * W, ctx, max_h=(bh - 2 * (len(badges) - 1)) / len(badges)) for u in badges]
            out.append(("col", bs))
        for u in ctas:
            out.append(("one", [size_unit(u, k * cta_boost, 0.25 * W, ctx, max_h=0.8 * bh)]))
        return out

    def text_width(k):
        """Narrowest text column that holds the stack at scale k (None if none)."""
        lo, hi = 0.08 * W, W - 2 * m - fixed_w
        if hi <= lo or stack(rows, (0, 0, hi, bh), k, ctx)[0] > bh:
            return None
        for _ in range(12):
            mid = (lo + hi) / 2
            if stack(rows, (0, 0, mid, bh), k, ctx)[0] <= bh:
                hi = mid
            else:
                lo = mid
        return hi

    def total_w(k):
        tw = text_width(k)
        if tw is None:
            return None, None
        rs = right_slots(k)
        n = len(left) + len(rs) + 1
        return fixed_w + tw + sum(max(x.w for x in ss) for _, ss in rs) + gap * (n - 1), tw

    lo, hi, best = s_min * 0.2, max(s_min, s_area) * k_cap, None
    for level in range(ctx.floor_level, 5):
        ctx.floor_level = level
        if total_w(lo)[0] is not None and total_w(lo)[0] <= W - 2 * m:
            break
    for _ in range(16):
        mid = (lo + hi) / 2
        tot, tw = total_w(mid)
        if tot is not None and tot <= W - 2 * m:
            best, lo = (mid, tw, tot), mid
        else:
            hi = mid
    if best is None:
        return None
    k, tw, tot = best
    extra = (W - 2 * m - tot)
    tw += extra * 0.6                      # spare width: widen the text column
    g = gap + extra * 0.4 / max(1, len(left) + len(right_slots(k)))
    x, flow = m, {}
    for s in left:
        x = max(x, s.box[0])
        flow.update(s.place(x, m + (bh - s.h) / 2))
        x += s.w + g
    _, tplacer = stack(rows, (x, m, x + tw, H - m), k, ctx)
    flow.update(tplacer("center"))
    x += tw + g
    for kind, ss in right_slots(k):
        if kind == "col":
            total = sum(s.h for s in ss) + 2 * (len(ss) - 1)
            y, colw = m + (bh - total) / 2, max(s.w for s in ss)
            for s in ss:
                flow.update(s.place(x + (colw - s.w) / 2, y))
                y += s.h + 2
            x += colw + g
        else:
            s = ss[0]
            flow.update(s.place(x, m + (bh - s.h) / 2))
            x += s.w + g
    return _finish(d, W, H, flow, unpack)


def _merge_badge_rows(rows):
    """Re-merge units that sat side by side in the source into one row."""
    out = []
    for r in rows:
        u = r[0]
        if out and abs(out[-1][0].rect[1] - u.rect[1]) < 0.5 * min(u.h, out[-1][0].h):
            out[-1].append(u)
        else:
            out.append([u])
    for r in out:
        r.sort(key=lambda u: u.rect[0])
    return out
