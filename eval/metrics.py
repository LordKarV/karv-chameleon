"""Deterministic layout-quality metrics for Chameleon outputs.

`score(source, output, png_path=None, assets_dir="brand_kit/assets")` -> dict.

HARD structural checks (any failure -> structural_ok=False, det_score halved):
  - output has canvas + elements; canvas == target (if `target` given)
  - same element id set, no duplicates
  - required fields per type present; numbers finite; font_size >= 1
  - nothing off-canvas (unrotated box, 0.5px tolerance) -- an element that was
    already off-canvas in the source is reported in `warnings` only
  - non-cover images keep JSON box aspect within 5%
  - type / content / font_family / asset_id unchanged
Soft brand drift (color, fill_color, font_weight, font_style, fit changed) -> `warnings`.

SOFT components, each in 0..1:
  soft_score = 100 * sum(weight * component)
  det_score  = soft_score * (0.5 + 0.5*min(font_size, collapsed)) * (0.5 if structural fail)
  (the readability gate stops "everything shrunk to 2px" from scoring well on geometry alone)
  collisions     0.18  Unintended overlap of *rendered* extents (wrapped text
                       box, alpha bbox of images through fit) for pairs not sharing
                       group_id, background excluded. Only overlap in excess of what
                       the same pair had in the source counts (intentional layering).
                       Pair weights: text-text / text-image / anything opaque
                       painted over text 1.0, text-shape 0.6, image-image 0.5,
                       image-shape 0.3, shape-shape 0.2; x0.25 if either is
                       a low-opacity (<0.5) decoration. 1 - sum(w * excess_ratio).
  font_size      0.10  per text: (int(font_size) / max(10px, 1.1% of short side))^2,
                       capped 1; 0.5*mean + 0.5*min.
  contrast       0.08  WCAG ratio of declared text colour (alpha-composited) vs the
                       text-free render under the actual glyph pixels (25th pct);
                       target 4.5 (3.0 for >=24px), capped at 95% of what the same
                       element achieved in the source render. 0.5*mean + 0.5*min.
  overflow       0.10  rendered text taller than box (+2px) or a line wider than the
                       box (-0.25 each); rendered text cut off by canvas (-0.5 each).
  busy           0.05  edge density of the text-free render under each text extent;
                       1 at <=4%, 0 at >=24%. 0.5*mean + 0.5*min.
  alignment      0.05  fraction of elements sharing a left/centre/right (or
                       top/middle/bottom) anchor with another non-group element or
                       the canvas centre, relative to the source's fraction.
  utilization    0.14  0.35 * covered-area (vs 60% of source's, clipped 0.1..0.6)
                       + 0.40 * spread (content-bbox share of canvas vs 90% of source's,
                         ^1.5 -- punishes letterbox bands) + 0.25 * balance
                         (centre-of-mass drift vs source).
  hierarchy      0.10  0.4*Spearman(font sizes) + 0.3*Spearman(visible areas), each
                       clipped at 0, + 0.3 * largest-source-text still largest.
  groups         0.10  per group: member offsets/sizes vs uniform-scaled source
                       (err / 10% of group diag) x exp(-|log aspect change|).
  collapsed      0.10  element hidden/collapsed (<2px side, opacity ~0, font <6px,
                       visible area <0.05% of canvas and <10% of its source share,
                       or fully off-canvas): -0.34 each.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from eval.layout import (
    as_design, box, effective_opacity, font_manager, intersect, rect_area,
    render, text_colors, text_layout, to_array, visible_rect,
)

WEIGHTS = {
    "collisions": 0.18, "font_size": 0.10, "contrast": 0.08, "overflow": 0.10,
    "busy": 0.05, "alignment": 0.05, "utilization": 0.14, "hierarchy": 0.10,
    "groups": 0.10, "collapsed": 0.10,
}
STRUCTURAL_FAIL_FACTOR = 0.5
BG_COVERAGE = 0.85
DECO_OPACITY = 0.5
ASPECT_TOL = 0.05
OFFCANVAS_TOL = 0.5
MIN_FONT_PX, MIN_FONT_REL = 10.0, 0.011
GLYPH_DIFF = 0.15  # summed RGB diff (0..3) marking a glyph pixel
EDGE_THRESH, BUSY_LO, BUSY_HI = 0.08, 0.04, 0.24

REQUIRED_COMMON = ["id", "type", "x", "y", "width", "height"]
REQUIRED_BY_TYPE = {
    "text": ["content", "font_family", "font_size", "color"],
    "image": ["asset_id"],
    "shape": ["shape_type", "fill_color"],
}
IMMUTABLE = ["type", "content", "font_family", "asset_id"]
BRAND_SOFT = ["color", "fill_color", "font_weight", "font_style", "fit"]
PAIR_W = {
    ("text", "text"): 1.0, ("image", "text"): 1.0, ("shape", "text"): 0.6,
    ("image", "image"): 0.5, ("image", "shape"): 0.3, ("shape", "shape"): 0.2,
}


# ------------------------------------------------------------ structural --

def _offcanvas(el: dict, cw: float, ch: float) -> bool:
    x0, y0, x1, y1 = box(el)
    return x0 < -OFFCANVAS_TOL or y0 < -OFFCANVAS_TOL or x1 > cw + OFFCANVAS_TOL or y1 > ch + OFFCANVAS_TOL


def structural(src: dict, out: dict, target: Optional[dict]) -> tuple[list, list]:
    errors, warnings = [], []
    if not isinstance(out, dict) or "canvas" not in out or "elements" not in out:
        return ["output missing canvas/elements"], warnings
    cw, ch = out["canvas"].get("width"), out["canvas"].get("height")
    if target and (cw, ch) != (target["width"], target["height"]):
        errors.append(f"canvas {cw}x{ch} != target {target['width']}x{target['height']}")
    out_ids = [e.get("id") for e in out["elements"]]
    src_by_id = {e["id"]: e for e in src["elements"]}
    if len(out_ids) != len(set(out_ids)):
        errors.append("duplicate element ids")
    if set(out_ids) != set(src_by_id):
        errors.append(f"id mismatch: missing {sorted(set(src_by_id) - set(out_ids))}, "
                      f"extra {sorted(map(str, set(out_ids) - set(src_by_id)))}")
    scw, sch = src["canvas"]["width"], src["canvas"]["height"]
    for el in out["elements"]:
        eid = el.get("id")
        missing = [f for f in REQUIRED_COMMON + REQUIRED_BY_TYPE.get(el.get("type"), []) if f not in el]
        if missing:
            errors.append(f"{eid}: missing {missing}")
            continue
        nums = [el[k] for k in ("x", "y", "width", "height")] + ([el["font_size"]] if el["type"] == "text" else [])
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in nums):
            errors.append(f"{eid}: non-finite geometry")
            continue
        if el["width"] < 0 or el["height"] < 0 or (el["type"] == "text" and el["font_size"] < 1):
            errors.append(f"{eid}: negative size or font_size < 1")
        s = src_by_id.get(eid)
        if s is None:
            continue
        if cw and ch and _offcanvas(el, cw, ch):
            (warnings if _offcanvas(s, scw, sch) else errors).append(f"{eid}: off-canvas")
        for f in IMMUTABLE:
            if s.get(f) != el.get(f):
                errors.append(f"{eid}: {f} changed")
        for f in BRAND_SOFT:
            if s.get(f) != el.get(f):
                warnings.append(f"{eid}: {f} changed")
        if el["type"] == "image" and el.get("fit", "contain") != "cover" and min(s["width"], s["height"]) > 0:
            if min(el["width"], el["height"]) <= 0:
                errors.append(f"{eid}: zero-size image")
            else:
                ratio = (el["width"] / el["height"]) / (s["width"] / s["height"])
                if abs(ratio - 1) > ASPECT_TOL:
                    errors.append(f"{eid}: aspect changed {ratio:.2f}x")
    return errors, warnings


# --------------------------------------------------------------- helpers --

def background_ids(design: dict) -> set:
    cw, ch = design["canvas"]["width"], design["canvas"]["height"]
    canvas = (0.0, 0.0, float(cw), float(ch))
    zmin = min(e.get("z_index", 0) for e in design["elements"])
    return {
        e["id"] for e in design["elements"]
        if e.get("type") in ("image", "shape") and e.get("z_index", 0) == zmin
        and rect_area(intersect(box(e), canvas)) >= BG_COVERAGE * cw * ch
    }


def _rects(design: dict, fm, assets_dir: str) -> dict:
    return {e["id"]: visible_rect(e, fm, assets_dir) for e in design["elements"]}


def _overlap_ratio(a, b) -> float:
    inter = rect_area(intersect(a, b))
    return inter / max(1e-6, min(rect_area(a), rect_area(b))) if inter else 0.0


def _spearman(a: list, b: list) -> float:
    def rank(v):
        v = np.asarray(v, float)
        r = np.empty(len(v))
        r[v.argsort(kind="stable")] = np.arange(len(v))
        for u in np.unique(v):
            r[v == u] = r[v == u].mean()
        return r
    if len(a) < 2:
        return 1.0
    ra, rb = rank(a), rank(b)
    if ra.std() == 0:
        return 1.0
    return 0.0 if rb.std() == 0 else float(np.corrcoef(ra, rb)[0, 1])


def _agg(vals: list) -> float:
    return 1.0 if not vals else 0.5 * float(np.mean(vals)) + 0.5 * float(min(vals))


def _luminance(rgb: np.ndarray) -> np.ndarray:
    c = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    return c @ np.array([0.2126, 0.7152, 0.0722])


# --------------------------------------------------------------- metrics --

def collisions(src, out, rs, ro, bg) -> tuple[float, list]:
    els = [e for e in out["elements"] if e["id"] not in bg]
    src_by_id = {e["id"]: e for e in src["elements"]}
    penalty, pairs = 0.0, []
    for i, a in enumerate(els):
        for b in els[i + 1:]:
            if a.get("group_id") and a.get("group_id") == b.get("group_id"):
                continue
            r_out = _overlap_ratio(ro[a["id"]], ro[b["id"]])
            if r_out == 0:
                continue
            excess = max(0.0, r_out - _overlap_ratio(rs[a["id"]], rs[b["id"]]) - 0.02)
            if excess == 0:
                continue
            w = PAIR_W[tuple(sorted((a["type"], b["type"])))]
            top, under = (a, b) if a.get("z_index", 0) >= b.get("z_index", 0) else (b, a)
            if under["type"] == "text" and top["type"] != "text" and effective_opacity(top) >= DECO_OPACITY:
                w = 1.0  # opaque thing painted over text
            if min(effective_opacity(a), effective_opacity(b)) < DECO_OPACITY:
                w *= 0.25
            if w * excess > 0.02:
                pairs.append((a["id"], b["id"], round(w * excess, 3)))
            penalty += w * excess
    return max(0.0, 1.0 - penalty), sorted(pairs, key=lambda p: -p[2])


def text_checks(out, fm, ro) -> tuple[float, float, dict]:
    cw, ch = out["canvas"]["width"], out["canvas"]["height"]
    thresh = max(MIN_FONT_PX, MIN_FONT_REL * min(cw, ch))
    sizes, issues, n_over, n_cut = [], [], 0, 0
    for e in out["elements"]:
        if e["type"] != "text":
            continue
        isz = max(1, int(e["font_size"]))
        sizes.append(min(1.0, isz / thresh) ** 2)
        lay = text_layout(e, fm)
        if lay["height"] > e["height"] + 2 or lay["max_line_width"] > e["width"] + 1:
            n_over += 1
            issues.append(f"{e['id']}: overflow ({lay['lines']} lines, {lay['height']:.0f}px tall in {e['height']:.0f}px box)")
        r = ro[e["id"]]
        if r[0] < -1 or r[1] < -1 or r[2] > cw + 1 or r[3] > ch + 1:
            n_cut += 1
            issues.append(f"{e['id']}: rendered text cut off by canvas")
    min_px = min((max(1, int(e["font_size"])) for e in out["elements"] if e["type"] == "text"), default=None)
    overflow = max(0.0, 1.0 - 0.25 * n_over - 0.5 * n_cut)
    return _agg(sizes), overflow, {"min_font_px": min_px, "font_thresh_px": round(thresh, 1), "text_issues": issues}


def _glyph_contrasts(design, rects, full: np.ndarray, bgimg: np.ndarray) -> dict:
    """{text id: (25th-pct WCAG ratio under glyphs, edge density of background)}."""
    k = full.shape[1] / design["canvas"]["width"]
    res = {}
    for e in design["elements"]:
        if e["type"] != "text":
            continue
        x0, y0, x1, y1 = (int(round(v * k)) for v in rects[e["id"]])
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(full.shape[1], x1), min(full.shape[0], y1)
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        f, b = full[y0:y1, x0:x1], bgimg[y0:y1, x0:x1]
        mask = np.abs(f - b).sum(-1) > GLYPH_DIFF
        under = b[mask] if mask.sum() >= 10 else b.reshape(-1, 3)
        lum_bg = _luminance(under)
        ratios = []
        for (r, g, bl, a) in text_colors(e):
            alpha = a * float(e.get("opacity", 1.0) if e.get("opacity") is not None else 1.0)
            col = alpha * np.array([r, g, bl]) / 255.0 + (1 - alpha) * under
            lum_t = _luminance(col)
            hi, lo = np.maximum(lum_t, lum_bg), np.minimum(lum_t, lum_bg)
            ratios.append(float(np.percentile((hi + 0.05) / (lo + 0.05), 25)))
        gray = _luminance(b)
        edges = (np.abs(np.diff(gray, axis=1))[:-1, :] + np.abs(np.diff(gray, axis=0))[:, :-1]) > EDGE_THRESH
        res[e["id"]] = (min(ratios), float(edges.mean()) if edges.size else 0.0)
    return res


def pixel_checks(out: dict, measured: dict, measured_src: dict) -> tuple[float, float, dict]:
    """Contrast target is WCAG (4.5, or 3.0 for >=24px) but never above what the
    source itself achieved -- brand colours are fixed, so only *lost* contrast counts."""
    by_id = {e["id"]: e for e in out["elements"]}
    contrasts, busy, low, busy_ids = [], [], [], []
    for eid, (ratio, density) in measured.items():
        target = 3.0 if by_id[eid]["font_size"] >= 24 else 4.5
        if eid in measured_src:
            target = min(target, 0.95 * measured_src[eid][0])
        contrasts.append(min(1.0, ratio / target))
        if ratio < target:
            low.append((eid, round(ratio, 2)))
        b = 1.0 - min(1.0, max(0.0, (density - BUSY_LO) / (BUSY_HI - BUSY_LO)))
        busy.append(b)
        if b < 1:
            busy_ids.append((eid, round(density, 3)))
    return _agg(contrasts), _agg(busy), {"low_contrast": low, "busy_text": busy_ids}


def _mask_stats(design, rects, bg) -> tuple[float, float, float]:
    cw, ch = design["canvas"]["width"], design["canvas"]["height"]
    s = 200.0 / max(cw, ch)
    m = np.zeros((max(1, round(ch * s)), max(1, round(cw * s))), bool)
    for eid, r in rects.items():
        if eid in bg:
            continue
        x0, y0, x1, y1 = (max(0, round(v * s)) for v in r)
        m[y0:y1, x0:x1] = True
    if not m.any():
        return 0.0, 1.0, 0.0
    ys, xs = np.nonzero(m)
    d = math.hypot(xs.mean() / m.shape[1] - 0.5, ys.mean() / m.shape[0] - 0.5)
    spread = (np.ptp(xs) + 1) / m.shape[1] * (np.ptp(ys) + 1) / m.shape[0]  # content bbox / canvas
    return float(m.mean()), d, float(spread)


def utilization(src, out, rs, ro, bg) -> tuple[float, dict]:
    us, ds, ss = _mask_stats(src, rs, bg)
    uo, do, so = _mask_stats(out, ro, bg)
    util = min(1.0, uo / (0.6 * min(0.6, max(0.1, us))))
    spread = min(1.0, so / max(0.05, 0.9 * ss)) ** 1.5  # letterboxing leaves empty bands
    balance = 1.0 - min(1.0, max(0.0, (do - ds) / 0.3))
    return 0.35 * util + 0.4 * spread + 0.25 * balance, {
        "coverage": round(uo, 3), "coverage_src": round(us, 3), "spread": round(so, 3), "spread_src": round(ss, 3)}


def _aligned_fraction(design, rects, bg) -> float:
    cw, ch = design["canvas"]["width"], design["canvas"]["height"]
    els = [e for e in design["elements"] if e["id"] not in bg and effective_opacity(e) >= DECO_OPACITY]
    if len(els) < 2:
        return 1.0
    tx, ty = max(2.0, 0.006 * cw), max(2.0, 0.006 * ch)

    def anchors(r):
        return (r[0], (r[0] + r[2]) / 2, r[2]), (r[1], (r[1] + r[3]) / 2, r[3])

    hits = 0.0
    for e in els:
        ax, ay = anchors(rects[e["id"]])
        ok_x = abs(ax[1] - cw / 2) <= tx
        ok_y = abs(ay[1] - ch / 2) <= ty
        for o in els:
            if o is e or (e.get("group_id") and e.get("group_id") == o.get("group_id")):
                continue
            bx, by = anchors(rects[o["id"]])
            ok_x |= any(abs(p - q) <= tx for p, q in zip(ax, bx))
            ok_y |= any(abs(p - q) <= ty for p, q in zip(ay, by))
        hits += (ok_x + ok_y) / 2
    return hits / len(els)


def alignment(src, out, rs, ro, bg) -> tuple[float, dict]:
    fs, fo = _aligned_fraction(src, rs, bg), _aligned_fraction(out, ro, bg)
    return min(1.0, fo / max(0.3, fs)), {"aligned_frac": round(fo, 3), "aligned_frac_src": round(fs, 3)}


def hierarchy(src, out, rs, ro, bg) -> tuple[float, dict]:
    out_by = {e["id"]: e for e in out["elements"]}
    texts = [e for e in src["elements"] if e["type"] == "text" and e["id"] in out_by]
    fs = [e["font_size"] for e in texts]
    fo = [out_by[e["id"]]["font_size"] for e in texts]
    rho_f = _spearman(fs, fo)
    ids = [e["id"] for e in src["elements"] if e["id"] not in bg and e["id"] in ro]
    rho_a = _spearman([rect_area(rs[i]) for i in ids], [rect_area(ro[i]) for i in ids])
    top = 1.0
    if fs:
        top_ids = [e["id"] for e in texts if e["font_size"] == max(fs)]
        top = float(max(out_by[i]["font_size"] for i in top_ids) >= 0.95 * max(fo))
    score = 0.4 * max(0.0, rho_f) + 0.3 * max(0.0, rho_a) + 0.3 * top
    return score, {"rho_font": round(rho_f, 3), "rho_area": round(rho_a, 3), "top_text_kept": bool(top)}


def groups(src, out) -> tuple[float, dict]:
    out_by = {e["id"]: e for e in out["elements"]}
    gids = {}
    for e in src["elements"]:
        if e.get("group_id") and e["id"] in out_by:
            gids.setdefault(e["group_id"], []).append(e["id"])
    scores = {}
    for g, ids in gids.items():
        if len(ids) < 2:
            continue
        sb = [box(e) for e in src["elements"] if e["id"] in ids]
        ob = [box(out_by[i]) for i in ids]
        sg = (min(r[0] for r in sb), min(r[1] for r in sb), max(r[2] for r in sb), max(r[3] for r in sb))
        og = (min(r[0] for r in ob), min(r[1] for r in ob), max(r[2] for r in ob), max(r[3] for r in ob))
        s = math.sqrt(rect_area(og) / max(1e-6, rect_area(sg)))
        diag = max(1.0, math.hypot(og[2] - og[0], og[3] - og[1]))
        err = np.mean([
            abs((o[0] - og[0]) - s * (r[0] - sg[0])) + abs((o[1] - og[1]) - s * (r[1] - sg[1]))
            + abs((o[2] - o[0]) - s * (r[2] - r[0])) + abs((o[3] - o[1]) - s * (r[3] - r[1]))
            for r, o in zip(sb, ob)
        ]) / diag
        ar = lambda r: max(1e-6, r[2] - r[0]) / max(1e-6, r[3] - r[1])  # noqa: E731
        scores[g] = float(max(0.0, 1.0 - err / 0.1) * math.exp(-abs(math.log(ar(og) / ar(sg)))))
    return (float(np.mean(list(scores.values()))) if scores else 1.0), {"group_scores": {k: round(v, 3) for k, v in scores.items()}}


def collapsed(src, out, rs, ro) -> tuple[float, dict]:
    scw, sch = src["canvas"]["width"], src["canvas"]["height"]
    cw, ch = out["canvas"]["width"], out["canvas"]["height"]
    src_by = {e["id"]: e for e in src["elements"]}
    bad = []
    for e in out["elements"]:
        s = src_by.get(e["id"])
        if s is None:
            continue
        vis = rect_area(intersect(ro[e["id"]], (0, 0, cw, ch))) / (cw * ch)
        vis_src = rect_area(rs[e["id"]]) / (scw * sch)
        if (e["width"] < 2 or e["height"] < 2 or vis == 0
                or (effective_opacity(e) < 0.05 <= effective_opacity(s))
                or (e["type"] == "text" and e["font_size"] < 6)
                or (vis < 0.0005 and vis < 0.1 * vis_src)):
            bad.append(e["id"])
    return max(0.0, 1.0 - 0.34 * len(bad)), {"collapsed_ids": bad}


# ------------------------------------------------------------------ main --

_SRC_CACHE: dict[str, dict] = {}


def _source_contrasts(src: dict, assets_dir: str, fonts_dir: str) -> dict:
    key = json.dumps(src, sort_keys=True)
    if key not in _SRC_CACHE:
        fm = font_manager(fonts_dir)
        full = to_array(render(src, assets_dir, fonts_dir))
        bgimg = to_array(render(src, assets_dir, fonts_dir, with_text=False))
        _SRC_CACHE[key] = _glyph_contrasts(src, _rects(src, fm, assets_dir), full, bgimg)
    return _SRC_CACHE[key]

def score(source: dict, output: dict, png_path: Optional[str] = None,
          assets_dir: str = "brand_kit/assets", fonts_dir: Optional[str] = None,
          target: Optional[dict] = None) -> dict:
    src = as_design(source)
    fonts_dir = fonts_dir or str(Path(assets_dir).parent / "fonts")
    errors, warnings = structural(src, output, target)
    result = {"structural_ok": not errors, "structural_errors": errors, "warnings": warnings}
    if any(e.startswith(("output missing", "id mismatch", "duplicate")) or ": missing" in e
           or "non-finite" in e for e in errors):
        return {**result, "det_score": 0.0, "components": {}}  # can't measure layout

    fm = font_manager(fonts_dir)
    bg = background_ids(src)
    rs, ro = _rects(src, fm, assets_dir), _rects(output, fm, assets_dir)
    full_img = Image.open(png_path).convert("RGB") if png_path else render(output, assets_dir, fonts_dir)
    full = to_array(full_img)
    bgimg = to_array(render(output, assets_dir, fonts_dir, with_text=False).resize(full_img.size))
    measured = _glyph_contrasts(output, ro, full, bgimg)
    measured_src = _source_contrasts(src, assets_dir, fonts_dir)

    comp, info = {}, {}
    comp["collisions"], info["collision_pairs"] = collisions(src, output, rs, ro, bg)
    comp["font_size"], comp["overflow"], i1 = text_checks(output, fm, ro)
    comp["contrast"], comp["busy"], i2 = pixel_checks(output, measured, measured_src)
    comp["utilization"], i3 = utilization(src, output, rs, ro, bg)
    comp["alignment"], i4 = alignment(src, output, rs, ro, bg)
    comp["hierarchy"], i5 = hierarchy(src, output, rs, ro, bg)
    comp["groups"], i6 = groups(src, output)
    comp["collapsed"], i7 = collapsed(src, output, rs, ro)
    for d in (i1, i2, i3, i4, i5, i6, i7):
        info.update(d)

    soft = 100.0 * sum(WEIGHTS[k] * comp[k] for k in WEIGHTS)
    gate = 0.5 + 0.5 * min(comp["font_size"], comp["collapsed"])  # unreadable/hidden content caps the score
    det = soft * gate * (1.0 if not errors else STRUCTURAL_FAIL_FACTOR)
    return {
        **result,
        "det_score": round(det, 2),
        "soft_score": round(soft, 2),
        "components": {k: round(v, 3) for k, v in comp.items()},
        **info,
    }
