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

ROUNDS = 3
MIN_GAIN = 0.01
MAX_EDITS = 16


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
    by = {u.uid: u for u in _units(d)}
    us = [by[i] for i in ids if i in by]
    step = 0.02 * min(W, H)
    res = []
    if kind in ("collision", "crowded") and len(us) == 2:
        a, b = sorted(us, key=lambda u: u.importance)       # move / shrink the less important one
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
