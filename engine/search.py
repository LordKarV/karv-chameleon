"""Step 5c · Search: energy-guided local search over the chosen layout.

`propose` only tries edits for defects its critic names. The Claude vision loop (eval/vision_loop)
reaches layouts the energy itself prefers but no template or defect-edit produces: a hero grown into
free space, a column re-spaced evenly, a unit nudged clear, a text re-measured. The energy costs under
a millisecond, so instead of guessing the fix we search: every unit x {shift, scale, rewrap} plus block
moves, accept the best improving move, shrink the step when nothing improves, stop after ROUNDS rounds.
Stickers follow their host; finalize still enforces the contract afterwards.
"""
from __future__ import annotations

import math

from engine.analyze import Design
from engine.energy import energy
from engine.propose import _move, _nested, _recenter, _rect, _rewrap_text, _scale, _spread
from engine.relations import _center, _units

ROUNDS = 60          # the work cap; a count, not a clock, so the result does not depend on machine speed
MIN_GAIN = 0.02
STEP0 = 0.05         # first shift step, share of the short side
STEP_MIN = 0.006
SCALES = (0.85, 0.92, 1.08, 1.2)
REWRAPS = (0.8, 1.25)
GROW_CAP = 1.3       # a unit may end at most 30% larger than its proportional size (text ran edge to edge)
EDGE = 0.015         # share of the short side: the critic's "touches the canvas edge" threshold


def _follow(d: Design, p: dict, q: dict, host) -> dict:
    """Stickers pinned to `host` take the host's move and scale (p -> q)."""
    r0, r1 = _rect(host, p), _rect(host, q)
    if not r0 or not r1 or (r0 == r1):
        return q
    s = min((r1[2] - r1[0]) / max(1e-6, r0[2] - r0[0]), (r1[3] - r1[1]) / max(1e-6, r0[3] - r0[1]))
    out = dict(q)
    for u in d.units:
        if u.host != host.uid:
            continue
        for e in u.elements:
            o = out.get(e["id"])
            if o is None:
                continue
            cx = r1[0] + (o["x"] + o["width"] / 2 - r0[0]) * (r1[2] - r1[0]) / max(1e-6, r0[2] - r0[0])
            cy = r1[1] + (o["y"] + o["height"] / 2 - r0[1]) * (r1[3] - r1[1]) / max(1e-6, r0[3] - r0[1])
            n = {**o, "width": o["width"] * s, "height": o["height"] * s}
            n["x"], n["y"] = cx - n["width"] / 2, cy - n["height"] / 2
            if "font_size" in o:
                n["font_size"] = float(max(1, math.floor(o["font_size"] * s)))
            out[e["id"]] = n
    return out


def _equalize(p: dict, units, W: float, H: float) -> dict:
    """Even vertical gaps for a single column (every pair x-overlapping); block extent kept."""
    rs = sorted(((_rect(u, p), u) for u in units if _rect(u, p)), key=lambda t: t[0][1])
    if len(rs) < 3:
        return p
    for i in range(len(rs) - 1):
        a, b = rs[i][0], rs[i + 1][0]
        if min(a[2], b[2]) - max(a[0], b[0]) <= 0 or b[1] < a[3] - 1:   # side by side, or overlapping
            return p
    total = rs[-1][0][3] - rs[0][0][1]
    gap = (total - sum(r[3] - r[1] for r, _ in rs)) / (len(rs) - 1)
    q, y = p, rs[0][0][1]
    for r, u in rs:
        q = _move(q, u, 0, y - r[1], W, H)
        y += (r[3] - r[1]) + gap
    return q


def _below_floor(d: Design, u, q: dict, ctx) -> bool:
    """Would the unit sit under its readability / picture floor after the move? (The energy's size
    term is too soft here: it let a strip's logo shrink to 61% while the judge already called it tiny.)"""
    from engine.layout import _img_floor, PIC_ROLES
    for e in _nested([u]) and [x for k in _nested([u]) for x in k.texts]:
        if e["id"] in q and q[e["id"]].get("font_size", 99) < ctx.floor(k_role(d, e)):
            return True
    for x in _nested([u]):
        if x.role in PIC_ROLES or x.role == "logo":
            r = _rect(x, q)
            if r and max(r[2] - r[0], r[3] - r[1]) < _img_floor("hero" if x.role not in ("logo", "image", "missing") else x.role, ctx):
                return True
    return False


def k_role(d: Design, e: dict) -> str:
    for x in _nested(d.units):
        if any(t is e for t in x.texts):
            return x.role
    return "other"


def _flat(d: Design) -> list:
    """Top-level flow units plus the children of panels (a headline inside a card moves on its own)."""
    out = []
    for u in _units(d):
        out.append(u)
        if u.kind == "container":
            out += _nested(u.children)
    return out


def _locks(d: Design) -> dict:
    """Source alignments a move must keep: uid -> (x anchor or None, y anchor or None).
    x anchor 'l'/'c'/'r' when the unit shares a left edge / centre / right edge with a stacked neighbour,
    y anchor 't'/'c'/'b' when it shares a top / centre / bottom with a side-by-side neighbour."""
    us = _flat(d)
    tx, ty = 0.012 * d.W, 0.012 * d.H
    out = {}
    for a in us:
        ra, cxa, cya = a.rect, _center(a.rect)[0], _center(a.rect)[1]
        lx = ly = None
        for b in us:
            if b is a or b.uid == a.parent or a.uid == b.parent:
                continue
            rb = b.rect
            if min(ra[3], rb[3]) - max(ra[1], rb[1]) <= 0:          # stacked
                if abs(cxa - _center(rb)[0]) < tx:
                    lx = "c"
                elif lx is None and abs(ra[0] - rb[0]) < tx:
                    lx = "l"
                elif lx is None and abs(ra[2] - rb[2]) < tx:
                    lx = "r"
            if min(ra[2], rb[2]) - max(ra[0], rb[0]) <= 0:          # side by side
                if abs(cya - _center(rb)[1]) < ty:
                    ly = "c"
                elif ly is None and abs(ra[1] - rb[1]) < ty:
                    ly = "t"
                elif ly is None and abs(ra[3] - rb[3]) < ty:
                    ly = "b"
        out[a.uid] = (lx, ly)
    return out


def _scale_kept(p: dict, u, f: float, lock, W: float, H: float) -> dict:
    """Scale about the centre, then slide back so the locked edge (or centre) stays where it was."""
    r0 = _rect(u, p)
    q = _scale(p, u, f, W, H)
    r1 = _rect(u, q)
    lx, ly = lock
    dx = {"l": r0[0] - r1[0], "r": r0[2] - r1[2], "c": 0.0, None: 0.0}[lx]
    dy = {"t": r0[1] - r1[1], "b": r0[3] - r1[3], "c": 0.0, None: 0.0}[ly]
    return _move(q, u, dx, dy, W, H) if (dx or dy) else q


def _moves(d: Design, p: dict, W: float, H: float, step: float):
    from engine.layout import Ctx, _sibling_key
    ctx = Ctx(W, H, d.W, d.H, 0)
    locks = getattr(d, "_search_locks", None)
    if locks is None:
        locks = d._search_locks = _locks(d)
    units = [u for u in _flat(d) if _rect(u, p) and u.role != "missing"]
    s = step * min(W, H)
    fams = {}
    for u in units:
        if u.kind in ("group", "container"):
            fams.setdefault(_sibling_key(u), []).append(u)
    fams = {k: v for k, v in fams.items() if len(v) >= 2}
    in_family = {u.uid for v in fams.values() for u in v}
    for members in fams.values():          # look-alike cards / badges move and scale as one set
        for f in SCALES:
            q = p
            for u in members:
                q = _follow(d, q, _scale(q, u, f, W, H), u)
            if f < 1 and any(_below_floor(d, u, q, ctx) and not _below_floor(d, u, p, ctx) for u in members):
                continue
            yield f"scale family {members[0].uid}+{len(members) - 1} x{f}", q
        for dx, dy in ((s, 0), (-s, 0), (0, s), (0, -s)):
            q = p
            for u in members:
                q = _follow(d, q, _move(q, u, dx, dy, W, H), u)
            yield f"shift family {members[0].uid}+{len(members) - 1} {dx:+.0f},{dy:+.0f}", q
    s_area = math.sqrt(W * H / (d.W * d.H))
    for u in units:
        if u.kind == "container" and u.edges:      # a band flush to a canvas edge stays flush (light_badges5 header)
            continue
        lock = locks.get(u.uid, (None, None))
        for dx, dy in ((s, 0), (-s, 0), (0, s), (0, -s)):
            if (dx and lock[0]) or (dy and lock[1]):                 # keep the source's alignments
                continue
            yield f"shift {u.uid} {dx:+.0f},{dy:+.0f}", _follow(d, p, _move(p, u, dx, dy, W, H), u)
        under = _below_floor(d, u, p, ctx)
        r = _rect(u, p)
        grown = max(r[2] - r[0], r[3] - r[1]) / max(1e-6, max(u.w, u.h)) / s_area   # size vs proportional
        for f in (() if u.uid in in_family else SCALES):
            if f > 1 and grown * f > GROW_CAP:                      # never oversized vs the source's proportion
                continue
            q = _scale_kept(p, u, f, lock, W, H)
            if f < 1 and not under and _below_floor(d, u, q, ctx):   # never shrink a unit under its floor
                continue
            if f < 1 and under:                                      # already under: only growing is allowed
                continue
            yield f"scale {u.uid} x{f}", _follow(d, p, q, u)
        if u.kind == "text":
            for f in REWRAPS:
                yield f"rewrap {u.uid} x{f}", _rewrap_text(p, u, f, W, H)
    texts = [u for u in units if u.kind == "text"]
    for f in (0.92, 1.08):                                           # one type scale for all copy
        q = p
        for u in texts:
            q = _scale(q, u, f, W, H)
        yield f"scale all text x{f}", q
    top = [u for u in _units(d) if _rect(u, p) and u.role != "missing"]
    yield "recentre block", _recenter(p, top, W, H)
    for f in (0.92, 1.08):
        yield f"spread block x{f}", _spread(p, top, f, W, H)
    yield "equalize column gaps", _equalize(p, top, W, H)
    for u in top:                                                    # a panel's children re-spaced evenly inside it
        if u.kind == "container" and len(u.children) >= 3:
            yield f"equalize {u.uid} children", _equalize(p, u.children, W, H)


CROWD = 0.008   # share of the short side: the critic's "crammed" threshold


def _gaps(d: Design, p: dict) -> dict:
    us = [u for u in _flat(d) if u.role != "missing" and u.kind != "container"]
    rs = {u.uid: _rect(u, p) for u in us}
    out = {}
    for i, a in enumerate(us):
        for b in us[i + 1:]:
            ra, rb = rs[a.uid], rs[b.uid]
            if ra and rb:
                out[(a.uid, b.uid)] = max(rb[0] - ra[2], ra[0] - rb[2], rb[1] - ra[3], ra[1] - rb[3])
    return out


def _crowds(before: dict, after: dict, thr: float) -> bool:
    """A move may not create a new near-touch or overlap between units that had clearance."""
    return any(g < thr <= before.get(k, thr) for k, g in after.items())


def _margins(d: Design, p: dict, W: float, H: float) -> dict:
    """Each leaf unit's clearance from the canvas edge, as a share of the short side."""
    out = {}
    for u in _flat(d):
        if u.kind == "container" or u.role == "missing":
            continue
        r = _rect(u, p)
        if r:
            out[u.uid] = min(r[0], r[1], W - r[2], H - r[3]) / min(W, H)
    return out


def _hugs_edge(before: dict, after: dict, src: dict) -> bool:
    """A move may not push a unit against the canvas edge unless it sat there in the source."""
    return any(m < EDGE <= before.get(k, EDGE) and src.get(k, 0.0) >= EDGE for k, m in after.items())


def _nearest(d: Design, p: dict, W: float, H: float) -> dict:
    """Each leaf unit's distance to its nearest other unit, as a share of the short side."""
    us = [u for u in _flat(d) if u.kind != "container" and u.role != "missing"]
    rs = {u.uid: _rect(u, p) for u in us}
    out = {}
    for a in us:
        ra = rs[a.uid]
        if not ra:
            continue
        gaps = [max(rb[0] - ra[2], ra[0] - rb[2], rb[1] - ra[3], ra[1] - rb[3])
                for b, rb in ((b, rs[b.uid]) for b in us if b is not a) if rb]
        if gaps:
            out[a.uid] = max(0.0, min(gaps)) / min(W, H)
    return out


STRANDED = 0.08   # a unit drifting this far (share of the short side) from everything else reads as detached


def _strands(before: dict, after: dict, src: dict) -> bool:
    """A move may not strand a unit: its nearest neighbour may not end farther than 1.5x the source's
    (relative) distance and STRANDED, if it was closer before (fb_link 1500x500: CTA pushed to the far right)."""
    return any(g > max(STRANDED, 1.5 * src.get(k, 0.0)) and g > before.get(k, 0.0) + 1e-6 for k, g in after.items())


def search(d: Design, p: dict, W: float, H: float, base_energy: float | None = None) -> tuple[dict, list[dict]]:
    """Greedy best-improvement descent with step halving, only while the critic still names defects
    (a clean layout is left alone). Returns (placements, log)."""
    from engine.propose import critique
    if not critique(d, p, W, H):
        return p, []
    cur, cur_e = p, energy(d, p, W, H)[0] if base_energy is None else base_energy
    step, log = STEP0, []
    thr = CROWD * min(W, H)
    src_m = {u.uid: min(u.rect[0], u.rect[1], d.W - u.rect[2], d.H - u.rect[3]) / min(d.W, d.H) for u in _flat(d)}
    from engine.relations import _unit_els
    src_n = _nearest(d, {e["id"]: e for u in d.units for e in _unit_els(u)}, d.W, d.H)
    for rnd in range(ROUNDS):
        best = None
        g0, m0, n0 = _gaps(d, cur), _margins(d, cur, W, H), _nearest(d, cur, W, H)
        for name, q in _moves(d, cur, W, H, step):
            if q is cur or q == cur or _crowds(g0, _gaps(d, q), thr) or _hugs_edge(m0, _margins(d, q, W, H), src_m) \
                    or _strands(n0, _nearest(d, q, W, H), src_n):
                continue
            try:
                e = energy(d, q, W, H)[0]
            except Exception:   # noqa: BLE001 - a bad move must never sink the task
                continue
            if best is None or e < best[0]:
                best = (e, name, q)
        if best is not None and best[0] < cur_e - MIN_GAIN:
            log.append({"round": rnd + 1, "move": best[1], "energy": round(best[0], 3), "step": round(step, 4)})
            cur, cur_e = best[2], best[0]
            if not critique(d, cur, W, H):      # defects gone: stop, don't polish a clean layout into a strange one
                log.append({"round": rnd + 1, "move": "critic clean", "energy": round(cur_e, 3)})
                break
        else:
            step /= 2
            if step < STEP_MIN:
                break
    return cur, log
