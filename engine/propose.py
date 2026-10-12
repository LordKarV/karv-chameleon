"""Step 5 · Propose: touch-up after scoring.

A deterministic critic names concrete defects in the chosen layout (with the
units involved); each defect maps to a small menu of typed edits; every edit is
applied to a copy, re-scored with the full energy, and kept only if the energy
drops. A model may later choose among the same edits; it never writes pixels.
"""
from __future__ import annotations

import math

from engine.analyze import Design, Unit, area, inter, union, placed_visual
from engine.energy import energy
from engine.relations import _units, _unit_els, _center, extract_graph

ROUNDS = 8       # repeat while any edit still improves the score (converge), capped
MIN_GAIN = 0.01
MAX_EDITS = 32
REWRAP_MARGIN = 0.05   # side margin (share of the short side) a rewrap may not widen into: full-width text read as clipped


def _rect(u: Unit, p: dict):
    els = [e for e in _unit_els(u) if e["id"] in p]
    return union(placed_visual(e, p[e["id"]]) for e in els) if els else None


def _move(p, u, dx, dy, W, H):
    q = dict(p)
    els = [e for e in _unit_els(u) if e["id"] in q]
    x0 = min(q[e["id"]]["x"] for e in els)
    x1 = max(q[e["id"]]["x"] + q[e["id"]]["width"] for e in els)
    y0 = min(q[e["id"]]["y"] for e in els)
    y1 = max(q[e["id"]]["y"] + q[e["id"]]["height"] for e in els)
    dx, dy = min(max(dx, -x0), W - x1), min(max(dy, -y0), H - y1)
    for e in els:
        q[e["id"]] = {**q[e["id"]], "x": q[e["id"]]["x"] + dx, "y": q[e["id"]]["y"] + dy}
    return q


def _scale(p, u, f, W, H):
    """Scale a unit about its centre (text: font and box together)."""
    q = dict(p)
    els = [e for e in _unit_els(u) if e["id"] in q]
    cx, cy = _center(_rect(u, q))
    for e in els:
        o = q[e["id"]]
        n = {**o, "x": cx + (o["x"] - cx) * f, "y": cy + (o["y"] - cy) * f,
             "width": o["width"] * f, "height": o["height"] * f}
        if "font_size" in o:
            n["font_size"] = float(max(1, math.floor(o["font_size"] * f)))
        q[e["id"]] = n
    r2 = _rect(u, q)
    dx = -min(0.0, r2[0]) - max(0.0, r2[2] - W)
    dy = -min(0.0, r2[1]) - max(0.0, r2[3] - H)
    return _move(q, u, dx, dy, W, H) if (dx or dy) else q


def _rewrap_text(p, u, f, W, H):
    """Change a text block's measure (width x f) and re-fit its height exactly; keep its centre."""
    from engine import measure
    q = dict(p)
    e = u.elements[0]
    o = q[e["id"]]
    w = min(max(o["width"], W - 2 * REWRAP_MARGIN * min(W, H)), max(8.0, o["width"] * f))   # never into the margin
    el = {**e, **o}
    n, widest, longest = measure.layout_text(el, o["font_size"], w)
    if longest > w:
        return p
    h = measure.text_height(el, o["font_size"], n)
    cx, cy = o["x"] + o["width"] / 2, o["y"] + o["height"] / 2
    q[e["id"]] = {**o, "x": min(max(0.0, cx - w / 2), W - w), "y": min(max(0.0, cy - h / 2), H - h), "width": w, "height": h}
    return q


def _recenter(p, units, W, H):
    from engine.analyze import union as _union
    rs = [_rect(u, p) for u in units]
    c = _union(rs)
    dx, dy = W / 2 - (c[0] + c[2]) / 2, H / 2 - (c[1] + c[3]) / 2
    q = p
    for u in units:
        q = _move(q, u, dx, dy, W, H)
    return q


def _spread(p, units, f, W, H):
    """Scale the gaps (unit centres about the block centre), not the units."""
    from engine.analyze import union as _union
    c = _union(_rect(u, p) for u in units)
    cx, cy = (c[0] + c[2]) / 2, (c[1] + c[3]) / 2
    q = p
    for u in units:
        r = _rect(u, q)
        ux, uy = _center(r)
        q = _move(q, u, (ux - cx) * (f - 1), (uy - cy) * (f - 1), W, H)
    return q


def _texts_of(u) -> list:
    return [e for e in u.elements if e["type"] == "text"] + [e for c in u.children for e in _texts_of(c)]


def _top_fs(u, p) -> float:
    return max((p[e["id"]].get("font_size", 0) for e in _texts_of(u) if e["id"] in p), default=0.0)


def _nested(units) -> list:
    return [x for u in units for x in [u] + _nested(u.children)]


def _drift_cap(d, u, p) -> float:
    """Largest font u may have before it outgrows its source ratio to the headline (energy.HIER_DRIFT)."""
    from engine.energy import HIER_DRIFT
    heads = [e for x in _nested(d.units) if x.role == "headline" for e in _texts_of(x)]
    mine = _texts_of(u)
    if not heads or not mine:
        return float("inf")
    h0 = max(e["font_size"] for e in heads)
    return HIER_DRIFT * max(e["font_size"] for e in mine) / h0 * _head_fs(_nested(d.units), p)


def _head_fs(units, p) -> float:
    return max((_top_fs(u, p) for u in units if u.role == "headline"), default=0.0)


def critique(d: Design, p: dict, W: float, H: float) -> list[tuple[str, tuple, str]]:
    """Defects as (kind, unit uids, human-readable note)."""
    units = _units(d)
    rects = {u.uid: _rect(u, p) for u in units}
    short = min(W, H)
    out = []
    for i, a in enumerate(units):
        for b in units[i + 1:]:
            ra, rb = rects[a.uid], rects[b.uid]
            if not ra or not rb:
                continue
            if inter(ra, rb) > 0 and inter(a.rect, b.rect) == 0:
                out.append(("collision", (a.uid, b.uid), f"{a.uid} overlaps {b.uid}"))
            elif inter(ra, rb) == 0:
                gap = max(rb[0] - ra[2], ra[0] - rb[2], rb[1] - ra[3], ra[1] - rb[3])
                if gap < 0.008 * short:
                    out.append(("crowded", (a.uid, b.uid), f"{a.uid} crammed against {b.uid}"))
    for u in units:
        r = rects[u.uid]
        if r and min(r[0], r[1], W - r[2], H - r[3]) < 0.015 * short and \
                min(u.rect[0], u.rect[1], d.W - u.rect[2], d.H - u.rect[3]) >= 0.015 * min(d.W, d.H):
            out.append(("edge", (u.uid,), f"{u.uid} touches the canvas edge"))
    from engine.layout import Ctx
    from engine import measure
    ctx = Ctx(W, H, d.W, d.H, 0)
    for u in units:      # text below its readable floor
        if u.kind == "text" and u.elements[0]["id"] in p:
            fs = p[u.elements[0]["id"]].get("font_size", 0)
            if fs < ctx.floor(u.role) * 0.95:
                out.append(("small_text", (u.uid,), f"{u.uid} text {fs:.0f}px is below its readable size"))
    nested = _nested(d.units)     # stickers (badges pinned to a panel) included
    head = _head_fs(nested, p)
    for u in nested:     # hierarchy: nothing outshouts the headline (children of panels too)
        if head and u.role != "headline" and _top_fs(u, p) > min(head + 0.5, _drift_cap(d, u, p)):
            out.append(("outranks", (u.uid,), f"{u.uid} text is larger than the headline"))
    flow = [r for r in rects.values() if r]
    if flow:
        from engine.analyze import union as _union
        c = _union(flow)
        src_c = _union(u.rect for u in units)
        src_off = abs((src_c[0] + src_c[2]) / 2 / d.W - 0.5) + abs((src_c[1] + src_c[3]) / 2 / d.H - 0.5)
        off = abs((c[0] + c[2]) / 2 / W - 0.5) + abs((c[1] + c[3]) / 2 / H - 0.5)
        if off > src_off + 0.06:
            out.append(("offcenter", (), "content block sits off-centre compared with the source"))
        from engine.relations import _ler
        if _ler(flow, W, H) > max(_ler([u.rect for u in units], d.W, d.H), 0.15) + 0.03:
            out.append(("deadzone", (), "a large empty area the source did not have"))
    g = getattr(d, "_graph", None) or extract_graph(d)
    for a, b, kind in g["pairs"]:
        ra, rb = rects.get(a), rects.get(b)
        if ra and rb and kind == "cx":
            dx = abs(_center(ra)[0] - _center(rb)[0])
            stacked = min(ra[3], rb[3]) - max(ra[1], rb[1]) <= 0
            if stacked and 0.004 * W < dx < 0.12 * W:
                out.append(("misaligned", (a, b), f"{a} and {b} were centred on one axis"))
    return out


def _edits(d, p, defect, W, H):
    kind, ids, _ = defect
    by = {u.uid: u for u in _nested(d.units)}
    us = [by[i] for i in ids if i in by]
    step = 0.02 * min(W, H)
    res = []
    if kind in ("collision", "crowded") and len(us) == 2:
        a, b = sorted(us, key=lambda u: u.importance)       # move / shrink the less important one
        for t in (a, b):
            if t.kind == "text":
                res.append((f"rewrap {t.uid} narrower", _rewrap_text(p, t, 0.8, W, H)))
                res.append((f"rewrap {t.uid} wider", _rewrap_text(p, t, 1.25, W, H)))
        ca, cb = _center(_rect(a, p)), _center(_rect(b, p))
        dx = step * 2 if ca[0] >= cb[0] else -step * 2
        dy = step * 2 if ca[1] >= cb[1] else -step * 2
        res += [(f"nudge {a.uid} x{dx:+.0f}", _move(p, a, dx, 0, W, H)),
                (f"nudge {a.uid} y{dy:+.0f}", _move(p, a, 0, dy, W, H)),
                (f"shrink {a.uid} 10%", _scale(p, a, 0.9, W, H))]
    elif kind == "edge" and us:
        u = us[0]
        r = _rect(u, p)
        dx = step if r[0] < step else (-step if W - r[2] < step else 0)
        dy = step if r[1] < step else (-step if H - r[3] < step else 0)
        res += [(f"pull {u.uid} inward", _move(p, u, dx, dy, W, H)), (f"shrink {u.uid} 8%", _scale(p, u, 0.92, W, H))]
    elif kind == "outranks" and us:
        u = us[0]
        f = min(0.9 * _head_fs(_nested(d.units), p), _drift_cap(d, u, p) * 0.95) / max(1.0, _top_fs(u, p))
        res += [(f"shrink {u.uid} 15%", _scale(p, u, 0.85, W, H)), (f"shrink {u.uid} below the headline", _scale(p, u, f, W, H))]
    elif kind == "small_text" and us:
        u = us[0]
        res += [(f"grow {u.uid} text 10%", _scale(p, u, 1.1, W, H)), (f"rewrap {u.uid} wider", _rewrap_text(p, u, 1.25, W, H))]
    elif kind in ("offcenter", "deadzone"):
        flow_units = [u for u in by.values() if _rect(u, p)]
        res.append(("re-centre content block", _recenter(p, flow_units, W, H)))
        res.append(("spread content 8%", _spread(p, flow_units, 1.08, W, H)))
        heroes = [u for u in flow_units if u.role == "hero"]
        if heroes:
            res.append((f"grow hero {heroes[0].uid} 10%", _scale(p, heroes[0], 1.1, W, H)))
    elif kind == "misaligned" and len(us) == 2:
        a, b = sorted(us, key=lambda u: area(_rect(u, p)))
        dx = _center(_rect(b, p))[0] - _center(_rect(a, p))[0]
        res.append((f"re-centre {a.uid} on {b.uid}", _move(p, a, dx, 0, W, H)))
    return res


def propose(d: Design, p: dict, W: float, H: float, base_energy: float) -> tuple[dict, list[dict]]:
    """Greedy touch-up. Returns (placements, log of rounds)."""
    log, cur, cur_e = [], p, base_energy
    for rnd in range(ROUNDS):
        defects = critique(d, cur, W, H)
        if not defects:
            break
        tried = []
        for defect in defects:
            for name, q in _edits(d, cur, defect, W, H):
                try:
                    tried.append((energy(d, q, W, H)[0], name, q))
                except Exception:
                    continue
            if len(tried) >= MAX_EDITS:
                break
        tried.sort(key=lambda t: t[0])
        entry = {"round": rnd + 1, "defects": [x[2] for x in defects], "energy_before": round(cur_e, 3),
                 "tried": [(round(e, 3), n) for e, n, _ in tried[:8]], "accepted": None}
        if tried and tried[0][0] < cur_e - MIN_GAIN:
            cur_e, cur = tried[0][0], tried[0][2]
            entry["accepted"], entry["energy_after"] = tried[0][1], round(cur_e, 3)
            log.append(entry)
        else:
            log.append(entry)
            break
    return cur, log
