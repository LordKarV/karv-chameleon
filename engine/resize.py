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

from engine.analyze import analyze
from engine.energy import energy
from engine.layout import Ctx, scale_fit, stack_layout, split_layout, banner_layout, grid_family_size

log = logging.getLogger("chameleon")
BANNER_ASPECT = 2.2
SAME_ASPECT = 0.06   # |log aspect ratio| below which the target counts as the same shape
# Fidelity prior: departing from the source's structure must buy a real gain.
PRIORS = {" flip": 0.4, " grid": 0.2, " top": 0.25, " after_headline": 0.25, " bottom": 0.25, " explode": 0.3, " u1": 0.1}


def prior(name: str) -> float:
    return sum(v for k, v in PRIORS.items() if k in name)


def candidates(d, source, W, H, only: str | None = None):
    def ctx(margin=0.05, meas=1.0):
        return Ctx(W, H, d.W, d.H, margin * min(W, H) if min(W, H) > 150 else 0.07 * min(W, H), meas)
    ar = W / H
    gens = []
    if only in (None, "scale_fit"):
        gens.append(("scale_fit", lambda: scale_fit(d, source, W, H)))
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
    if only in (None, "stack"):
        for c in range(1, min(6, grid_family_size(d.rows)) + 1):
            for kc in (1.0, 1.3):
                for up in unpacks:
                    gens.append((f"stack k{kc} grid{c} u{int(up)}", lambda kc=kc, c=c, up=up:
                                 stack_layout(d, W, H, ctx(), kc, "center", up, cols=c)))
    if only in (None, "split") and ar > 0.9:
        for frac in (0.35, 0.45, 0.55):
            for side in ("auto", "flip"):
                for up in unpacks:
                    gens.append((f"split {frac} {side} u{int(up)}",
                                 lambda frac=frac, side=side, up=up: split_layout(d, W, H, ctx(meas=1.5), frac, side,
                                                                                  unpack=up)))
    if only in (None, "banner") and ar >= BANNER_ASPECT:
        for bit in (True, False):
            for ls in (True, False):
                for cf in (1.0, 1.4):
                    gens.append((f"banner b{bit} l{ls} c{cf}",
                                 lambda bit=bit, ls=ls, cf=cf: banner_layout(d, W, H, ctx(0.06, 3.0), bit, ls, cf)))
    for name, g in gens:
        try:
            p = g()
        except Exception as exc:  # a template failing must never sink the task
            log.debug("candidate %s failed: %r", name, exc)
            continue
        if p:
            yield name, p


def finalize(source: dict, place: dict, W: float, H: float) -> dict:
    """Copy every source element, apply new geometry, and enforce the contract:
    same ids, in-bounds boxes, image aspect kept, integer font sizes >= 1."""
    out = []
    for el in source["elements"]:
        e = dict(el)
        p = place.get(el["id"])
        if p is None:
            p = scale_fit(analyze(source), source, W, H)[el["id"]]
        x, y, w, h = (float(p[k]) if math.isfinite(float(p[k])) else 0.0 for k in ("x", "y", "width", "height"))
        w, h = max(0.0, w), max(0.0, h)
        f = min(1.0, W / w if w > W else 1.0, H / h if h > H else 1.0)
        w, h = w * f, h * f
        x, y = min(max(0.0, x), W - w), min(max(0.0, y), H - h)
        e.update(x=round(x, 2), y=round(y, 2), width=math.floor(w * 100) / 100, height=math.floor(h * 100) / 100)
        if e["x"] + e["width"] > W:
            e["x"] = round(W - e["width"], 2)
        if e["y"] + e["height"] > H:
            e["y"] = round(H - e["height"], 2)
        if e.get("type") == "text":
            fs = p.get("font_size", el.get("font_size", 16)) * f
            e["font_size"] = float(max(1, math.floor(fs + 1e-6)))
            if el.get("letter_spacing"):  # spacing scales with the font (em-relative)
                e["letter_spacing"] = round(float(el["letter_spacing"]) * e["font_size"] / float(el["font_size"]), 3)
        out.append(e)
    return {"canvas": {"width": int(W), "height": int(H)}, "elements": out}


def resize_with_info(source: dict[str, Any], target_canvas: dict[str, Any], only: str | None = None):
    W, H = float(target_canvas["width"]), float(target_canvas["height"])
    d = analyze(source)
    insight_log = []
    backend = os.environ.get("CHAMELEON_INSIGHT", "off")
    if backend != "off":   # step 2: optional model review of the analysis
        from engine.insight import get_insight, apply_insight
        ins = get_insight(d, source, int(W), int(H), backend)
        insight_log = apply_insight(d, ins) if ins else ["insight unavailable: deterministic analysis used"]
    if only is None and abs(math.log((W / H) / (d.W / d.H))) < SAME_ASPECT:
        # same shape: the designer's own layout is the best layout; just scale it
        return finalize(source, scale_fit(d, source, W, H), W, H), {"chosen": "scale_fit(same aspect)",
                                                                     "energy": 0.0, "terms": {}, "ranking": []}
    scored = []
    for name, p in candidates(d, source, W, H, only):
        try:
            e, terms = energy(d, p, W, H)
        except Exception as exc:
            log.debug("energy %s failed: %r", name, exc)
            continue
        scored.append((e + prior(name), name, p, terms))
    if not scored:
        p = scale_fit(d, source, W, H)
        return finalize(source, p, W, H), {"chosen": "scale_fit(fallback)", "ranking": []}
    scored.sort(key=lambda t: t[0])
    best = scored[0]
    info = {"chosen": best[1], "energy": best[0], "terms": best[3],
            "ranking": [(round(e, 3), n) for e, n, _, _ in scored], "insight": insight_log}
    place = best[2]
    if os.environ.get("CHAMELEON_PROPOSE", "1") != "0":   # step 5: critic -> typed edits -> re-score
        from engine.propose import propose
        try:
            raw = best[0] - prior(best[1])
            place, info["propose"] = propose(d, place, W, H, raw)
        except Exception as exc:  # touch-up is an improvement pass; never sink the task
            log.debug("propose failed: %r", exc)
    return finalize(source, place, W, H), info


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
