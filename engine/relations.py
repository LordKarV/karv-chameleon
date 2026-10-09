"""Source relation graph + composition checks.

General principles a human reviewer applies when comparing a resize with its
original, turned into energy terms so every template is held to them:

- concentric: things centred on each other in the source (a product on its halo,
  a label on its disc) stay centred on each other
- focal: the product/headline that sat on the background's glow stays near it
- proportion: size ratios between headline, hero, logo and call to action stay
  close to the designer's
- deadzone: no empty hole bigger than the source's own whitespace
- edge / proximity: visible content keeps clearance from the canvas edge and from
  its neighbours unless it already touched them in the source
- lines: the headline does not explode into many short lines
"""
from __future__ import annotations

import math
from functools import lru_cache

from engine import measure
from engine.analyze import Design, Unit, area, inter, union, placed_visual

TOL_RATIO = math.log(1.6)       # proportion drift tolerated before it costs
EDGE_CLEAR = 0.015              # fraction of the short side
PROX_CLEAR = 0.008
GRID = 24
KEY_ROLES = ("headline", "hero", "logo", "cta")


def _center(r):
    return (r[0] + r[2]) / 2, (r[1] + r[3]) / 2


def _size(r):
    return max(r[2] - r[0], r[3] - r[1], 1e-6)


@lru_cache(maxsize=64)
def _asset_focal(asset_id: str):
    """Luminance-weighted centre of the brightest 10% of a background asset
    (fractions of the asset), or None."""
    try:
        from PIL import Image
        path = measure.ASSETS_DIR / asset_id
        if not path.is_file():
            return None
        im = Image.open(path).convert("L").resize((96, 96))
        px = list(im.getdata())
        thr = sorted(px)[int(len(px) * 0.9)]
        sx = sy = n = 0.0
        for i, v in enumerate(px):
            if v >= thr:
                w = v - thr + 1
                sx += (i % 96 + 0.5) * w
                sy += (i // 96 + 0.5) * w
                n += w
        if not n:
            return None
        fx, fy = sx / n / 96, sy / n / 96
        return (fx, fy) if math.hypot(fx - 0.5, fy - 0.5) < 0.45 else None
    except Exception:
        return None


def background_focal(d: Design, W: float, H: float):
    """Where the background's glow lands on a W x H canvas (cover crop, centred)."""
    for e in d.background:
        if e["type"] != "image" or e.get("fit", "contain") != "cover":
            continue
        f = _asset_focal(e.get("asset_id", ""))
        info = measure.asset_alpha_bbox(e.get("asset_id", ""))
        if f is None or info is None:
            continue
        aspect = info[4]
        s = max(W / aspect, H)            # scaled asset height so it covers W x H
        sw, sh = s * aspect, s
        return (W - sw) / 2 + f[0] * sw, (H - sh) / 2 + f[1] * sh
    return None


def _units(d: Design):
    return [u for u in d.units if not u.host and not u.corner]


def _font(u: Unit, place):
    t = u.texts
    return float(place[t[0]["id"]]["font_size"]) if place is not None else float(t[0]["font_size"])


def _key_size(u: Unit, rect, place=None):
    if u.kind == "text":
        return _font(u, place)
    return math.sqrt(max(1.0, area(rect)))


def _ler(rects, W, H):
    """Largest empty axis-aligned rectangle on a GRID x GRID occupancy map,
    as a fraction of the canvas (classic histogram/stack algorithm)."""
    occ = [[False] * GRID for _ in range(GRID)]
    for r in rects:
        x0, x1 = int(max(0, r[0]) / W * GRID), int(math.ceil(min(W, r[2]) / W * GRID))
        y0, y1 = int(max(0, r[1]) / H * GRID), int(math.ceil(min(H, r[3]) / H * GRID))
        for y in range(max(0, y0), min(GRID, y1)):
            for x in range(max(0, x0), min(GRID, x1)):
                occ[y][x] = True
    heights, best = [0] * GRID, 0
    for y in range(GRID):
        heights = [0 if occ[y][x] else heights[x] + 1 for x in range(GRID)]
        stack = []
        for x in range(GRID + 1):
            h = heights[x] if x < GRID else 0
            while stack and heights[stack[-1]] >= h:
                top = stack.pop()
                left = stack[-1] + 1 if stack else 0
                best = max(best, heights[top] * (x - left))
            stack.append(x)
    return best / (GRID * GRID)


def _decor_host(e: dict, units):
    """The flow unit a decoration belongs to in the source: centred on it, wrapped
    around it, or floating near it (bubbles around a product). Decorations that
    touch or bleed off the page edge belong to the page instead (returns None)."""
    r = (e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"])
    c = _center(r)
    best, best_d = None, None
    for u in units:
        cu = _center(u.rect)
        size = max(_size(r), _size(u.rect))
        if math.dist(c, cu) < 0.2 * size or inter(r, u.rect) >= 0.5 * area(u.rect):
            if best is None or area(u.rect) > area(best.rect):
                best, best_d = u, -1.0
    if best is not None:
        return best
    for u in units:  # 'near': centre within 0.6x the unit's size of its box
        if u.kind == "text" or _size(r) > 0.6 * _size(u.rect):
            continue
        dx = max(u.rect[0] - c[0], 0, c[0] - u.rect[2])
        dy = max(u.rect[1] - c[1], 0, c[1] - u.rect[3])
        dn = math.hypot(dx, dy) / _size(u.rect)
        if dn < 0.6 and (best_d is None or dn < best_d):
            best, best_d = u, dn
    return best


def _touches_page(e, W, H, tol=0.02):
    return (e["x"] <= tol * W or e["y"] <= tol * H or e["x"] + e["width"] >= (1 - tol) * W
            or e["y"] + e["height"] >= (1 - tol) * H)


def extract_graph(d: Design) -> dict:
    """Source relation graph over flow units: pairwise alignment and page relations."""
    units = _units(d)
    tx, ty = 0.012 * d.W, 0.012 * d.H
    pairs, page = [], {}
    for u in units:
        r = u.rect
        cx, cy = _center(r)
        page[u.uid] = {"cx": abs(cx - d.W / 2) < 0.015 * d.W, "cy": abs(cy - d.H / 2) < 0.015 * d.H,
                       "span_x": r[2] - r[0] >= 0.92 * d.W, "span_y": r[3] - r[1] >= 0.92 * d.H}
    for i, a in enumerate(units):
        for b in units[i + 1:]:
            ra, rb = a.rect, b.rect
            stacked = min(ra[3], rb[3]) - max(ra[1], rb[1]) <= 0     # one above the other
            side = min(ra[2], rb[2]) - max(ra[0], rb[0]) <= 0         # side by side
            if stacked:
                if abs(_center(ra)[0] - _center(rb)[0]) < tx:
                    pairs.append((a.uid, b.uid, "cx"))
                elif abs(ra[0] - rb[0]) < tx:
                    pairs.append((a.uid, b.uid, "left"))
                elif abs(ra[2] - rb[2]) < tx:
                    pairs.append((a.uid, b.uid, "right"))
            if side:
                if abs(_center(ra)[1] - _center(rb)[1]) < ty:
                    pairs.append((a.uid, b.uid, "cy"))
                elif abs(ra[1] - rb[1]) < ty:
                    pairs.append((a.uid, b.uid, "top"))
                elif abs(ra[3] - rb[3]) < ty:
                    pairs.append((a.uid, b.uid, "bottom"))
    flow = [u.rect for u in units]
    fill = bool(flow) and (max(r[2] for r in flow) - min(r[0] for r in flow) >= 0.85 * d.W
                           and max(r[3] for r in flow) - min(r[1] for r in flow) >= 0.85 * d.H)
    return {"pairs": pairs, "page": page, "fill": fill}


def _unit_els(u):
    return u.elements + [e for k in u.children for e in _unit_els(k)]


def _shift(out, u, dx, dy, W, H):
    els = [e for e in _unit_els(u) if e["id"] in out]
    if not els:
        return
    xs0 = min(out[e["id"]]["x"] for e in els)
    xs1 = max(out[e["id"]]["x"] + out[e["id"]]["width"] for e in els)
    ys0 = min(out[e["id"]]["y"] for e in els)
    ys1 = max(out[e["id"]]["y"] + out[e["id"]]["height"] for e in els)
    dx = min(max(dx, -xs0), W - xs1)
    dy = min(max(dy, -ys0), H - ys1)
    for e in els:
        p = dict(out[e["id"]])
        p["x"] += dx
        p["y"] += dy
        out[e["id"]] = p


def snap(d: Design, out: dict, W: float, H: float) -> dict:
    """Enforce source relations exactly when the layout is already close:
    page-centred units -> page centre; centre/edge-aligned stacked pairs -> one
    exact axis. Returns a new placement dict (the input is not mutated)."""
    g = getattr(d, "_graph", None)
    if g is None:
        g = d._graph = extract_graph(d)
    out = dict(out)
    by = {u.uid: u for u in _units(d)}

    def rect(u):
        els = [e for e in _unit_els(u) if e["id"] in out]
        return union(placed_visual(e, out[e["id"]]) for e in els) if els else None

    for uid, rel in g["page"].items():
        u = by[uid]
        r = rect(u)
        if r is None:
            continue
        cx = _center(r)[0]
        if rel["cx"] and abs(cx - W / 2) < 0.06 * W:
            _shift(out, u, W / 2 - cx, 0, W, H)
    for a, b, kind in g["pairs"]:
        ua, ub = by.get(a), by.get(b)
        if ua is None or ub is None:
            continue
        ra, rb = rect(ua), rect(ub)
        if ra is None or rb is None:
            continue
        follower, leader = (ua, ub) if area(ra) < area(rb) else (ub, ua)
        rf, rl = rect(follower), rect(leader)
        if kind == "cx" and abs(_center(rf)[0] - _center(rl)[0]) < 0.05 * W:
            _shift(out, follower, _center(rl)[0] - _center(rf)[0], 0, W, H)
        elif kind == "left" and abs(rf[0] - rl[0]) < 0.04 * W:
            _shift(out, follower, rl[0] - rf[0], 0, W, H)
        elif kind == "right" and abs(rf[2] - rl[2]) < 0.04 * W:
            _shift(out, follower, rl[2] - rf[2], 0, W, H)
        elif kind == "cy" and abs(_center(rf)[1] - _center(rl)[1]) < 0.05 * H:
            _shift(out, follower, 0, _center(rl)[1] - _center(rf)[1], W, H)
        elif kind == "top" and abs(rf[1] - rl[1]) < 0.04 * H:
            _shift(out, follower, 0, rl[1] - rf[1], W, H)
        elif kind == "bottom" and abs(rf[3] - rl[3]) < 0.04 * H:
            _shift(out, follower, 0, rl[3] - rf[3], W, H)
    return out


def relation_terms(d: Design, place: dict, W: float, H: float, rects: dict) -> dict:
    units = _units(d)
    src_short, out_short = min(d.W, d.H), min(W, H)
    t = dict.fromkeys(("concentric", "focal", "proportion", "deadzone", "edge", "proximity", "lines"), 0.0)

    # concentric: unit-unit and decor-unit pairs centred together in the source
    nodes = [(u.uid, u.rect, rects[u.uid]) for u in units]
    for e in d.decor:
        if e["id"] in place:
            r0 = (e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"])
            nodes.append((e["id"], r0, placed_visual(e, place[e["id"]])))
    for i, (a, a0, a1) in enumerate(nodes):
        for b, b0, b1 in nodes[i + 1:]:
            d0 = math.dist(_center(a0), _center(b0)) / max(_size(a0), _size(b0))
            if d0 < 0.12:
                d1 = math.dist(_center(a1), _center(b1)) / max(_size(a1), _size(b1))
                t["concentric"] += max(0.0, d1 - max(d0, 0.06))

    # focal: the hero/headline that sat on the background glow stays near it
    f0, f1 = background_focal(d, d.W, d.H), background_focal(d, W, H)
    if f0 and f1:
        for u in units:
            if u.role in ("hero", "headline"):
                n0 = math.dist(_center(u.rect), f0) / math.hypot(d.W, d.H)
                if n0 < 0.12:
                    n1 = math.dist(_center(rects[u.uid]), f1) / math.hypot(W, H)
                    t["focal"] += max(0.0, n1 - max(n0, 0.08)) * u.importance

    # proportion: size ratios among the key elements
    keys = [u for u in units if u.role in KEY_ROLES]
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            r0 = _key_size(a, a.rect) / _key_size(b, b.rect)
            r1 = _key_size(a, rects[a.uid], place) / _key_size(b, rects[b.uid], place)
            t["proportion"] += max(0.0, abs(math.log(max(r1, 1e-6) / max(r0, 1e-6))) - TOL_RATIO)

    # deadzone: largest empty rectangle vs the source's own
    ler1 = _ler([rects[u.uid] for u in d.units], W, H)
    ler0 = _ler([u.rect for u in d.units], d.W, d.H)
    t["deadzone"] = max(0.0, ler1 - max(ler0, 0.15))

    # edge clearance and neighbour proximity (only if not already so in the source)
    for u in units:
        r0, r1 = u.rect, rects[u.uid]
        e0 = min(r0[0], r0[1], d.W - r0[2], d.H - r0[3]) / src_short
        e1 = min(r1[0], r1[1], W - r1[2], H - r1[3]) / out_short
        if e0 >= EDGE_CLEAR and e1 < EDGE_CLEAR:
            t["edge"] += (EDGE_CLEAR - max(e1, 0.0)) / EDGE_CLEAR * u.importance
    for i, a in enumerate(units):
        for b in units[i + 1:]:
            ra, rb = rects[a.uid], rects[b.uid]
            if inter(ra, rb) > 0:
                continue
            g1 = max(rb[0] - ra[2], ra[0] - rb[2], rb[1] - ra[3], ra[1] - rb[3]) / out_short
            g0 = max(b.rect[0] - a.rect[2], a.rect[0] - b.rect[2], b.rect[1] - a.rect[3],
                     a.rect[1] - b.rect[3]) / src_short
            if g1 < PROX_CLEAR <= g0:
                t["proximity"] += (PROX_CLEAR - max(g1, 0.0)) / PROX_CLEAR

    # headline line count: no explosion into many short lines
    for u in units:
        if u.role != "headline" or u.kind != "text":
            continue
        e = u.elements[0]
        p = place[e["id"]]
        q = {**e, **p}
        n0 = measure.layout_text(e, e["font_size"], max(1.0, e["width"]))[0]
        lines = [ln.strip() for ln in measure._lines(q, q["font_size"], max(1.0, q["width"]))]
        n1 = len(lines)
        words = len(str(e.get("content", "")).split())
        t["lines"] += max(0, n1 - max(n0 + 1, 2)) * 0.5
        if words >= 3 and n1 > 1 and any(len(ln.split()) == 1 for ln in lines[:-1]):
            t["lines"] += 0.5
    return t


def anchor_decor(d: Design, W: float, H: float, flow: dict, s: float) -> dict:
    """Place each decoration by its source relation: centred on / wrapped around a
    unit -> follows that unit at its scale; touching a canvas edge or corner ->
    keeps that edge; otherwise proportional position."""
    units = _units(d)
    out = {}
    for e in d.decor:
        host = None if _touches_page(e, d.W, d.H) else _decor_host(e, units)
        w, h = e["width"] * s, e["height"] * s
        if e["width"] >= 0.92 * d.W or e["height"] >= 0.92 * d.H:   # a band spanning the page keeps spanning
            fx, fy = e["width"] >= 0.92 * d.W, e["height"] >= 0.92 * d.H
            out[e["id"]] = {"x": 0.0 if fx else min(max(0.0, e["x"] / d.W * W), W - w),
                            "y": 0.0 if fy else min(max(0.0, e["y"] / d.H * H), H - h),
                            "width": float(W) if fx else min(w, W), "height": float(H) if fy else min(h, H)}
            continue
        if host is not None and any(m["id"] in flow for m in _unit_els(host)):
            hr = union(placed_visual(m, flow[m["id"]]) for m in _unit_els(host) if m["id"] in flow)
            k = _size(hr) / _size(host.rect)
            w, h = e["width"] * k, e["height"] * k
            cx = hr[0] + ((e["x"] + e["width"] / 2) - host.rect[0]) * k
            cy = hr[1] + ((e["y"] + e["height"] / 2) - host.rect[1]) * k
        else:
            tol = 0.02
            left, right = e["x"] <= tol * d.W, e["x"] + e["width"] >= (1 - tol) * d.W
            top, bottom = e["y"] <= tol * d.H, e["y"] + e["height"] >= (1 - tol) * d.H
            cx = (e["x"] + e["width"] / 2) / d.W * W
            cy = (e["y"] + e["height"] / 2) / d.H * H
            if left and not right:
                cx = w / 2 + max(0.0, e["x"]) * s
            elif right and not left:
                cx = W - w / 2 - max(0.0, d.W - e["x"] - e["width"]) * s
            if top and not bottom:
                cy = h / 2 + max(0.0, e["y"]) * s
            elif bottom and not top:
                cy = H - h / 2 - max(0.0, d.H - e["y"] - e["height"]) * s
        if w > W or h > H:
            f = min(W / max(w, 1e-6), H / max(h, 1e-6))
            w, h = w * f, h * f
        out[e["id"]] = {"x": min(max(0.0, cx - w / 2), W - w), "y": min(max(0.0, cy - h / 2), H - h),
                        "width": w, "height": h}
    return out
