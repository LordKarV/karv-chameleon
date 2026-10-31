"""Chameleon engine entry point.

Pipeline: analyze the design (roles, units, reading rows; no reliance on ids)
-> generate candidate layouts from several templates (scale-fit, stack, split,
banner) -> score each with an internal rubric-mirroring energy -> finalize the
best into a schema-safe, in-bounds design. CHAMELEON_STRATEGY selects a single
template for experiments (scale_fit | stack | split | banner | search).
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any

from engine.analyze import analyze, num
from engine.energy import energy
from engine.layout import (Ctx, scale_fit, backdrop_frame, stack_layout, split_layout, banner_layout, grid_family_size,
                           proportional_layout)

log = logging.getLogger("chameleon")
BANNER_ASPECT = 2.2
SAME_ASPECT = 0.06   # |log aspect ratio| below which the target counts as the same shape
# Fidelity prior: departing from the source's structure must buy a real gain.
PRIORS = {"proportional": -1.0, " flip": 0.4, " grid": 0.2, " top": 0.25, " after_headline": 0.25, " bottom": 0.25, " explode": 0.3, " u1": 0.1}


def proportional_fade(name: str, W: float, H: float, d) -> float:
    """Proportional placement is what designers do for small/moderate aspect
    changes; on extreme changes it spreads content thin. Its bonus applies fully up
    to |log aspect change| 0.4 and fades to zero at 1.0 (review batch 1)."""
    if not name.startswith("proportional"):
        return 0.0
    ch = abs(math.log((W / H) / (d.W / d.H)))
    fade = min(1.0, max(0.0, (ch - 0.4) / 0.6))
    return -PRIORS["proportional"] * fade


def prior(name: str) -> float:
    return sum(v for k, v in PRIORS.items() if k in name)


def candidates(d, source, W, H, only: str | None = None):
    def ctx(margin=0.05, meas=1.0):
        # (a margin floor of 3% of the long side was measured on the Docker panel, v3e: "detached / dead
        # space" majors 13 -> 22, edge/margin unchanged at 25; reverted)
        return Ctx(W, H, d.W, d.H, margin * min(W, H) if min(W, H) > 150 else 0.07 * min(W, H), meas)
    ar = W / H
    gens = []
    if only in (None, "scale_fit"):
        gens.append(("scale_fit", lambda: scale_fit(d, source, W, H)))
        fr = backdrop_frame(d)
        if fr != (0.0, 0.0, d.W, d.H):   # the copy keeps its place on a backdrop photo that bled off the source
            gens.append(("scale_fit frame", lambda: scale_fit(d, source, W, H, fr)))
    if only in (None, "proportional"):          # what human designers do (adobedata Crello families)
        for sm in (1.0, 0.85, 0.7):
            gens.append((f"proportional s{sm}", lambda sm=sm: proportional_layout(d, W, H, ctx(), sm)))
    has_cont = any(u.kind == "container" for u in d.units)
    unpacks = (False, True) if has_cont else (False,)
    if only in (None, "stack"):
        for kc in (1.0, 1.3):
            for mode in ("center", "spread"):
                for meas in (1.0, 1.5):
                    for up in unpacks:
                        for hp in ("source", "top", "after_headline", "bottom", "explode"):
                            gens.append((f"stack k{kc} {mode} m{meas} u{int(up)} {hp}",
                                         lambda kc=kc, mode=mode, meas=meas, up=up, hp=hp:
                                         stack_layout(d, W, H, ctx(meas=meas), kc, mode, up,
                                                      "source" if hp == "explode" else hp, hp == "explode")))
    if only in (None, "stack") and any(u.role == "logo" for u in d.units for u in [u, *u.children]):
        for kc in (1.0, 1.3):     # the logo keeps its floor; the other copy pays, so energy decides
            for meas in (1.0, 1.5):
                for up in unpacks:
                    gens.append((f"stack k{kc} center m{meas} u{int(up)} source keeplogo",
                                 lambda kc=kc, meas=meas, up=up: stack_layout(d, W, H, ctx(meas=meas), kc, "center", up,
                                                                              "source", keep_logo=True)))
    if only in (None, "stack"):
        for c in range(1, min(6, grid_family_size(d.rows)) + 1):
            for kc in (1.0, 1.3):
                for up in unpacks:
                    for mode in ("center", "spread"):   # spread: a regridded pill column keeps its source row's
                        gens.append((f"stack k{kc} grid{c} u{int(up)}{' spread' if mode == 'spread' else ''}",
                                     lambda kc=kc, c=c, up=up, mode=mode:   # zero gap otherwise (badges5 1080x1920, v4b)
                                     stack_layout(d, W, H, ctx(), kc, mode, up, cols=c)))
    if only in (None, "split") and ar > 0.9:
        for frac in (0.35, 0.45, 0.55):
            for side in ("auto", "flip"):
                for up in unpacks:
                    gens.append((f"split {frac} {side} u{int(up)}",
                                 lambda frac=frac, side=side, up=up: split_layout(d, W, H, ctx(meas=1.5), frac, side,
                                                                                  unpack=up)))
        if ar > 1.5:   # wide: a split whose picture keeps a designer's proportion instead of filling its column
            for frac in (0.25, 0.35):
                for side in ("auto", "flip"):
                    gens.append((f"split {frac} {side} u0 modest",
                                 lambda frac=frac, side=side: split_layout(d, W, H, ctx(meas=1.5), frac, side,
                                                                           modest=True)))
    if only in (None, "stack") and abs(math.log(ar / (d.W / d.H))) > 1.0:
        # an extreme reshape (16:9 -> 160x600): the column is width-bound, so a higher type cap fills the height
        for mode in ("center", "spread"):
            for up in unpacks:
                gens.append((f"stack k2.0 {mode} m1.0 u{int(up)} source",
                             lambda mode=mode, up=up: stack_layout(d, W, H, ctx(), 2.0, mode, up, "source")))
    if only in (None, "banner") and ar >= BANNER_ASPECT:
        for bit in (True, False):
            for ls in (True, False):
                for cf in (1.0, 1.4):
                    gens.append((f"banner b{bit} l{ls} c{cf}",
                                 lambda bit=bit, ls=ls, cf=cf: banner_layout(d, W, H, ctx(0.06, 3.0), bit, ls, cf)))
        # roomy strips: copy as big as the height allows, logo in proportion to it. Only on a real reshape:
        # banner -> banner keeps the designer's layout (human resizes, 600x200 <-> 851x315)
        for kc in ((2.0, 3.0) if abs(math.log(ar / (d.W / d.H))) > 0.4 else ()):
            for lk in (99.0, 1.5):
                for bit in (True, False):
                    gens.append((f"banner b{bit} lTrue c1.0 k{kc} lk{lk}",
                                 lambda kc=kc, lk=lk, bit=bit: banner_layout(d, W, H, ctx(0.06, 3.0), bit, True, 1.0,
                                                                            k_cap=kc, logo_k=lk)))
        if any(u.corner for u in d.units):    # corner ribbons get their own width instead of landing on the CTA
            for bit in (True, False):
                for kc in (1.25, 2.0):
                    gens.append((f"banner b{bit} lTrue c1.0 k{kc} corner",
                                 lambda bit=bit, kc=kc: banner_layout(d, W, H, ctx(0.06, 3.0), bit, True, 1.0,
                                                                      k_cap=kc, logo_k=1.5, corner_room=True)))
        for bit in (True, False):     # the logo keeps its floor; the other copy pays, so energy decides
            for cr in ((False, True) if any(u.corner for u in d.units) else (False,)):
                gens.append((f"banner b{bit} lTrue c1.0 k2.0 keeplogo{' corner' if cr else ''}",
                             lambda bit=bit, cr=cr: banner_layout(d, W, H, ctx(0.06, 3.0), bit, True, 1.0, k_cap=2.0,
                                                                  logo_k=1.5, corner_room=cr, keep_logo=True)))
        for ls in (True, False):      # crowded strips: legal footer + badges side by side
            gens.append((f"banner footer l{ls}", lambda ls=ls: banner_layout(
                d, W, H, ctx(0.06, 3.0), False, ls, 1.0, footer=True, badge_row=True)))
        if any(u.corner for u in d.units):   # footer row clear of a corner ribbon; the 7% strip margin can't afford it
            for ls in (True, False):          # (6% sits on a floor cliff: Linux text metrics tip it over, L3 728x90)
                for mf in (0.04, 0.05):
                    gens.append((f"banner footer l{ls} corner m{mf}", lambda ls=ls, mf=mf: banner_layout(
                        d, W, H, Ctx(W, H, d.W, d.H, mf * min(W, H), 3.0), False, ls, 1.0, footer=True, badge_row=True,
                        corner_room=True)))
        for ft in (False, True):      # repeated cards side by side in the row
            gens.append((f"banner cards f{ft}", lambda ft=ft: banner_layout(
                d, W, H, ctx(0.06, 3.0), False, True, 1.0, footer=ft, badge_row=ft, card_row=True)))
    for name, g in gens:
        try:
            p = g()
        except Exception as exc:  # a template failing must never sink the task
            log.debug("candidate %s failed: %r", name, exc)
            continue
        if p:
            yield name, p


# A count, not a clock: a wall-clock budget made the pick depend on machine speed and load (L3 728x90 shipped a
# broken stack at 3 s, a clean banner at 60 s). The generators emit at most 192 candidates today (148 seen on the
# 625 dev pairs), so this cap never binds; it only stops a future generator explosion. CHAMELEON_MAX_CANDIDATES
# overrides it for experiments.
MAX_CANDIDATES = 256
MISSING_PX = 2.0   # short side of a missing asset's box: present in the JSON, invisible in the render
MIN_SIDE = 2.01    # no side that was >= 2 px in the source ships under 2 px (strict S6); the 0.01 survives rounding


def _ink(el: dict, w: float, h: float) -> tuple[float, float]:
    """Width and height of the union of a w x h box and its rotated ink (same centre)."""
    from engine.analyze import rot_aabb
    r = rot_aabb(0.0, 0.0, w, h, num(el.get("rotation")) % 360)
    return max(w, r[2] - r[0]), max(h, r[3] - r[1])


def _min_side(el: dict, w: float, h: float) -> tuple[float, float]:
    """Grow a side that was >= 2 px in the source back to MIN_SIDE: a sub-2 px element is a dishonest speck.
    Missing assets too: they stay small and quiet, but present. Images grow uniformly to keep their aspect.
    A zero side (even a zero source side) also grows: the official renderer rejects width/height 0."""
    need = [(v, num(el.get(k))) for v, k in ((w, "width"), (h, "height"))]
    if el.get("type") == "image" and el.get("fit") != "cover" and w > 0 and h > 0:
        g = max([MIN_SIDE / v for v, s in need if s >= 2 and v < MIN_SIDE] or [1.0])
        return w * g, h * g
    return tuple(max(v, MIN_SIDE) if s >= 2 or v <= 0 else v for v, s in need)


LEGIBLE_PX = 8.0    # floor guard: text below this many px counts as a readability shortfall
COLLIDE = 0.1       # floor guard: a text pair collides when its overlap (share of the smaller box) grew by this much


def _floor_pen(source: dict, out: dict) -> float:
    """Role-blind penalty for the scale-to-fit floor: text pairs that overlap more than in the source (text on
    text is unreadable whatever the roles; other pairs are often designed overlaps, so they do not count), plus
    each text's shortfall below LEGIBLE_PX. Unrotated boxes; no roles, ids or copy involved."""
    def boxes(els):
        return [(num(e.get("x")), num(e.get("y")), num(e.get("x")) + num(e.get("width")),
                 num(e.get("y")) + num(e.get("height"))) for e in els]
    def ov(a, b):
        i = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        return i / max(1e-9, min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])))
    t = [i for i, e in enumerate(out["elements"]) if e.get("type") == "text"]
    s, o = boxes(source["elements"]), boxes(out["elements"])   # ponytail: O(n^2) text pairs; designs carry few
    pen = sum(1.0 for a, i in enumerate(t) for j in t[a + 1:] if ov(o[i], o[j]) - ov(s[i], s[j]) > COLLIDE)
    return pen + sum(max(0.0, LEGIBLE_PX - num(e.get("font_size"), LEGIBLE_PX)) / LEGIBLE_PX
                     for e in out["elements"] if e.get("type") == "text")


def letterbox(source: dict, W: float, H: float) -> dict:
    """Pure uniform scale-to-fit, centred: no roles at all (scale_fit still stretches guessed backgrounds/panels)."""
    c = source.get("canvas") or {}
    sw, sh = max(1.0, num(c.get("width"), W)), max(1.0, num(c.get("height"), H))
    s = min(W / sw, H / sh)
    ox, oy = (W - sw * s) / 2, (H - sh * s) / 2
    return {e.get("id"): {"x": num(e.get("x")) * s + ox, "y": num(e.get("y")) * s + oy,
                          "width": max(0.0, num(e.get("width"))) * s, "height": max(0.0, num(e.get("height"))) * s,
                          "font_size": max(1.0, num(e.get("font_size"), 16.0) * s)}
            for e in source["elements"]}


def floor_guard(source: dict, out: dict, W: float, H: float):
    """Ship the uniform letterbox instead when the chosen layout is clearly worse on role-blind collisions and
    readability (>= 2 penalty units and >= 25% over the letterbox). Returns the letterbox output or None."""
    lb = finalize(source, letterbox(source, W, H), W, H)
    a, b = _floor_pen(source, out), _floor_pen(source, lb)
    return lb if a - b >= max(2.0, 0.25 * b) else None


def finalize(source: dict, place: dict, W: float, H: float) -> dict:
    """Copy every source element, apply new geometry, and enforce the contract:
    same ids, in-bounds boxes, image aspect kept, integer font sizes >= 1."""
    out = []
    missing = {e["id"] for u in analyze(source).missing for e in u.elements}
    for el in source["elements"]:
        e = dict(el)
        p = place.get(el["id"])
        if p is None:
            p = scale_fit(analyze(source), source, W, H)[el["id"]]
        x, y, w, h = (float(p[k]) if math.isfinite(float(p[k])) else 0.0 for k in ("x", "y", "width", "height"))
        w, h = max(0.0, w), max(0.0, h)
        if el["id"] in missing and min(w, h) > MISSING_PX:   # no asset: the grey placeholder reads as a broken image
            f = MISSING_PX / min(w, h)
            x, y, w, h = x + w * (1 - f) / 2, y + h * (1 - f) / 2, w * f, h * f
        f = 1.0
        for _ in range(4):   # min size vs canvas fit: they only fight on thin rotated boxes; a few rounds settle it
            w, h = _min_side(el, w, h)
            bw, bh = _ink(el, w, h)   # the box and its rotated ink fit the canvas, bleeding sources too
            g = min(1.0, W / bw if bw > W else 1.0, H / bh if bh > H else 1.0)   # on-canvas wins if both cannot hold
            w, h, f = w * g, h * g, f * g
        sw, sh = el.get("width") or 0, el.get("height") or 0
        # a full-bleed image keeps its aspect too unless it is fit=cover (strict S5: no full-bleed exemption)
        keep_aspect = (el.get("type") == "image" and el.get("fit") != "cover"
                       and isinstance(sw, (int, float)) and isinstance(sh, (int, float)) and sw > 0 and sh > 0
                       and w > 0 and h > 0)
        if keep_aspect:   # re-derive the second side from the source aspect (hairlines lose it to rounding)
            a, full = sw / sh, w >= W - 1 and h >= H - 1
            w0, h0 = w, h
            w, h = (w, w / a) if w / a <= h * 1.0001 else (h * a, h)
            w, h = min(w, W), min(h, H)
            if full:   # a non-cover backdrop is contained, centred like the renderer's contain
                x, y = x + (w0 - w) / 2, y + (h0 - h) / 2
        x, y = min(max(0.0, x), W - w), min(max(0.0, y), H - h)
        q = 10000.0       # 4 decimals: 2 decimals distorted sub-pixel hairlines by up to 25%
        e.update(x=round(x, 4), y=round(y, 4), width=math.floor(w * q) / q, height=math.floor(h * q) / q)
        if keep_aspect and e["width"] > 0 and e["height"] > 0 and \
                abs((e["width"] / e["height"]) / (sw / sh) - 1) > 0.004:   # still off (strict: 0.5%): grow the short side
            if e["width"] / e["height"] > sw / sh:
                e["height"] = min(float(H), math.ceil(e["width"] / (sw / sh) * q) / q)
            else:
                e["width"] = min(float(W), math.ceil(e["height"] * (sw / sh) * q) / q)
        if e["x"] + e["width"] > W:
            e["x"] = round(W - e["width"], 4)
        if e["y"] + e["height"] > H:
            e["y"] = round(H - e["height"], 4)
        if num(el.get("rotation")) % 360:   # keep box and rotated ink on the page: same centre, clamp by the larger
            hx, hy = (v / 2 for v in _ink(el, e["width"], e["height"]))
            cx, cy = e["x"] + e["width"] / 2, e["y"] + e["height"] / 2
            e["x"] = round((W / 2 if 2 * hx >= W else min(max(cx, hx), W - hx)) - e["width"] / 2, 4)
            e["y"] = round((H / 2 if 2 * hy >= H else min(max(cy, hy), H - hy)) - e["height"] / 2, 4)
        if e.get("type") == "text":
            fs = p.get("font_size", el.get("font_size", 16)) * f
            e["font_size"] = float(max(1, math.floor(fs + 1e-6)))
            # letter_spacing (px) and corner_radius stay as authored: the brief lets the engine change position,
            # size and font size only; the renderer clamps a radius to half the shape, so a shrunk pill stays a pill
        out.append(e)
    return {"canvas": {"width": int(W), "height": int(H)}, "elements": out}


def resize_with_info(source: dict[str, Any], target_canvas: dict[str, Any], only: str | None = None):
    W, H = float(target_canvas["width"]), float(target_canvas["height"])
    d = analyze(source)
    if only is None and abs(math.log((W / H) / (d.W / d.H))) < SAME_ASPECT:
        # same shape: the designer's own layout is the best layout; just scale it
        return finalize(source, scale_fit(d, source, W, H), W, H), {"chosen": "scale_fit(same aspect)",
                                                                     "energy": 0.0, "terms": {}, "ranking": []}
    scored = []
    cap = int(os.environ.get("CHAMELEON_MAX_CANDIDATES", MAX_CANDIDATES))   # candidates come cheap-and-safe first
    for i, (name, p) in enumerate(candidates(d, source, W, H, only)):
        if i >= cap and scored:
            log.warning("candidate cap of %d reached; picking from %d scored", cap, len(scored))
            break
        try:
            e, terms = energy(d, p, W, H)
        except Exception as exc:
            log.debug("energy %s failed: %r", name, exc)
            continue
        scored.append((e + prior(name) + proportional_fade(name, W, H, d), name, p, terms))
    if not scored:
        p = scale_fit(d, source, W, H)
        return finalize(source, p, W, H), {"chosen": "scale_fit(fallback)", "ranking": []}
    scored.sort(key=lambda t: t[0])
    best = scored[0]
    info = {"chosen": best[1], "energy": best[0], "terms": best[3],
            "ranking": [(round(e, 3), n) for e, n, _, _ in scored]}
    place = best[2]
    if os.environ.get("CHAMELEON_PROPOSE", "1") != "0":   # step 5: critic -> typed edits -> re-score
        from engine.propose import propose
        try:
            raw = best[0] - prior(best[1]) - proportional_fade(best[1], W, H, d)   # the pure energy of the winner
            place, info["propose"] = propose(d, place, W, H, raw)
        except Exception as exc:  # touch-up is an improvement pass; never sink the task
            log.debug("propose failed: %r", exc)
    # step 5c: energy-guided local search. Off by default: on the Docker vision panel it was neutral on verdicts
    # and +3..+7 major issues (v3b/v3c/v3d vs v3base, docs/PLAN_V3.md); the energy's blind spots are its ceiling.
    if os.environ.get("CHAMELEON_SEARCH", "0") == "1":
        from engine.search import search
        try:
            place, info["search"] = search(d, place, W, H)
        except Exception as exc:
            log.debug("search failed: %r", exc)
    out = finalize(source, place, W, H)
    lb = floor_guard(source, out, W, H)
    if lb is not None:
        info["floor"] = info["chosen"]
        info["chosen"] = "letterbox(floor)"
        return lb, info
    return out, info


def resize(source: dict[str, Any], target_canvas: dict[str, Any]) -> dict[str, Any]:
    """Never raises on junk input: the full engine, then scale-fit, then a raw
    uniform scale with every number coerced; each tier satisfies the contract."""
    from engine.analyze import num
    only = os.environ.get("CHAMELEON_STRATEGY") or None
    W = max(1.0, num(target_canvas.get("width"), 1.0))
    H = max(1.0, num(target_canvas.get("height"), 1.0))
    tgt = {"width": W, "height": H}
    try:
        return resize_with_info(source, tgt, None if only == "search" else only)[0]
    except Exception as exc:  # last resort: uniform scale-to-fit always satisfies the contract
        log.warning("resize failed (%r); falling back to scale-fit", exc)
        try:
            d = analyze(source)
            return finalize(source, scale_fit(d, source, W, H), W, H)
        except Exception as exc2:
            log.warning("scale-fit failed (%r); raw uniform scale", exc2)
            canvas = source.get("canvas") or {}
            s = min(W / max(1.0, num(canvas.get("width"), W)), H / max(1.0, num(canvas.get("height"), H)))
            place = {e.get("id"): {"x": num(e.get("x")) * s, "y": num(e.get("y")) * s,
                                   "width": max(0.0, num(e.get("width"))) * s,
                                   "height": max(0.0, num(e.get("height"))) * s,
                                   "font_size": max(1.0, num(e.get("font_size"), 16.0) * s)}
                     for e in source.get("elements") or []}
            return finalize(source, place, W, H)
