"""Hard rules (PASS/FAIL with evidence). Pure JSON plus pixel proof; never a model.

H1 schema · H2 ids · H3 canvas · H4 non-geometry fields unchanged · H5a in-canvas (JSON,
rotated box) · H5b in-canvas (rendered ink) · H6 image aspect · H7 PNG honesty (the
supplied PNG is exactly the official render of the JSON: no regenerated pixels).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
GEOMETRY = {"x", "y", "width", "height", "font_size", "letter_spacing", "corner_radius"}
TOL_PX = 0.5
ASPECT_TOL = 0.05
INK_OUT_FRAC = 0.005       # share of an element's ink allowed outside the canvas (beyond the source's)
INK_OUT_DEPTH = 2          # px: anti-aliasing slack
PNG_DIFF = 2               # 0-255
PNG_OK_SHARE = 0.999


def _r(id_, status, msg, element=None, **ev):
    return {"id": id_, "status": status, "msg": msg, "element": element, "evidence": ev}


def _rot_aabb(e):
    x, y, w, h = (float(e.get(k, 0)) for k in ("x", "y", "width", "height"))
    a = math.radians(float(e.get("rotation") or 0))
    c, s = abs(math.cos(a)), abs(math.sin(a))
    rw, rh = w * c + h * s, w * s + h * c
    cx, cy = x + w / 2, y + h / 2
    return cx - rw / 2, cy - rh / 2, cx + rw / 2, cy + rh / 2


def _outside(e, W, H):
    r = _rot_aabb(e)
    return r[0] < -TOL_PX or r[1] < -TOL_PX or r[2] > W + TOL_PX or r[3] > H + TOL_PX


def json_rules(src: dict, out: dict, target: dict) -> list[dict]:
    res = []
    try:
        import jsonschema
        schema = json.loads((ROOT / "chameleon_design_schema_v0.9.json").read_text())
        errs = sorted(jsonschema.Draft7Validator(schema).iter_errors(out), key=str)
        res.append(_r("H1", "FAIL" if errs else "PASS", "; ".join(e.message for e in errs[:3]) or "schema-valid"))
    except Exception as ex:   # noqa: BLE001 - schema problems are reported, not fatal
        res.append(_r("H1", "WARN", f"schema check skipped: {ex}"))
    if not isinstance(out, dict) or not isinstance(out.get("elements"), list):
        return res + [_r("H2", "FAIL", "no elements list")]
    ids = [e.get("id") for e in out["elements"]]
    sids = [e["id"] for e in src["elements"]]
    bad = len(ids) != len(set(ids)) or set(ids) != set(sids)
    res.append(_r("H2", "FAIL" if bad else "PASS",
                  f"missing {sorted(set(sids) - set(ids))}, extra {sorted(map(str, set(ids) - set(sids)))}, "
                  f"dupes {len(ids) - len(set(ids))}" if bad else "same ids"))
    cv = out.get("canvas") or {}
    W, H = cv.get("width"), cv.get("height")
    ok = (W, H) == (target["width"], target["height"])
    res.append(_r("H3", "PASS" if ok else "FAIL", f"canvas {W}x{H} vs target {target['width']}x{target['height']}"))
    if not ok or bad:
        return res
    by = {e["id"]: e for e in src["elements"]}
    sW, sH = src["canvas"]["width"], src["canvas"]["height"]
    for e in out["elements"]:
        s = by[e["id"]]
        nums = [e.get(k) for k in ("x", "y", "width", "height")]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in nums) or min(nums[2:]) < 0:
            res.append(_r("H1", "FAIL", "non-finite or negative geometry", e["id"]))
            continue
        changed = sorted(k for k in set(s) | set(e) if k not in GEOMETRY and s.get(k) != e.get(k))
        if changed:
            res.append(_r("H4", "FAIL", f"changed {changed}", e["id"], fields=changed))
        if e.get("type") == "text" and s.get("letter_spacing") and s.get("font_size") and e.get("font_size"):
            want = float(s["letter_spacing"]) * float(e["font_size"]) / float(s["font_size"])
            if abs(float(e.get("letter_spacing", 0)) - want) > 0.05 * abs(want) + 0.01:
                res.append(_r("H4", "WARN", "letter_spacing not scaled with font", e["id"]))
        if _outside(e, W, H):
            res.append(_r("H5a", "WARN" if _outside(s, sW, sH) else "FAIL", "box leaves the canvas", e["id"],
                          box=[round(v, 1) for v in _rot_aabb(e)]))
        if e.get("type") == "image" and e.get("fit") != "cover" and min(s["width"], s["height"]) > 0 \
                and not (e["width"] >= W - 1 and e["height"] >= H - 1):
            if min(e["width"], e["height"]) <= 0:
                res.append(_r("H6", "FAIL", "zero-size image", e["id"]))
            else:
                ratio = (e["width"] / e["height"]) / (s["width"] / s["height"])
                if abs(ratio - 1) > ASPECT_TOL:
                    res.append(_r("H6", "FAIL", f"aspect changed ×{ratio:.3f}", e["id"], ratio=ratio))
    for k in ("H4", "H5a", "H6"):
        if not any(r["id"] == k for r in res):
            res.append(_r(k, "PASS", "ok"))
    return res


def _outside_share(f, m) -> float:
    d, p = INK_OUT_DEPTH, f.pad_px
    inner = np.zeros_like(m)
    inner[max(0, p - d):p + f.full.shape[0] + d, max(0, p - d):p + f.full.shape[1] + d] = True
    return (m & ~inner).sum() / max(1, m.sum())


def ink_rules(fs, fo) -> list[dict]:
    """H5b: ink that the canvas cuts off, beyond what the source already cut off."""
    res = []
    for eid, m in fo.ink.items():
        if not m.sum():
            continue
        extra = _outside_share(fo, m) - (_outside_share(fs, fs.ink[eid]) if eid in fs.ink else 0.0)
        if extra > INK_OUT_FRAC:
            res.append(_r("H5b", "FAIL", f"{extra:.1%} of its ink is cut off by the canvas", eid, share=extra))
    return res or [_r("H5b", "PASS", "all ink inside the canvas")]


def png_rule(png: np.ndarray | None, rendered: np.ndarray) -> dict:
    """H7: the supplied PNG must be the official render of the JSON (no edited/regenerated pixels)."""
    if png is None:
        return _r("H7", "PASS", "no PNG supplied; the JSON is rendered by the official renderer")
    if png.shape != rendered.shape:
        return _r("H7", "FAIL", f"PNG size {png.shape[:2]} != render {rendered.shape[:2]}")
    diff = np.abs(png.astype(int) - rendered.astype(int)).max(-1)
    ok = (diff <= PNG_DIFF).mean()
    if ok >= PNG_OK_SHARE:
        return _r("H7", "PASS", f"PNG matches the official render ({ok:.3%} of pixels)")
    ys, xs = np.nonzero(diff > PNG_DIFF)
    return _r("H7", "FAIL", f"PNG differs from the official render on {1 - ok:.2%} of pixels",
              bbox=[int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())])
