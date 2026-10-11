"""Labelled defects for proving the grader's accuracy (docs/GRADER_PLAN.md §5).

Each corruption is a pure function (src, out) -> corrupted copy of out, or None when
it does not apply. DEFECTS maps name -> (function, check that must fire). A check is a
hard-rule id ("H6") or a soft criterion ("readability"). CONTROLS must NOT change the
verdict: they are harmless variations that measure false positives.
PNG corruptions return (out, png_transform).
"""
from __future__ import annotations

import copy

import numpy as np
from PIL import Image, ImageFilter


def _texts(d):
    return [e for e in d["elements"] if e["type"] == "text" and str(e.get("content", "")).strip()]


def _biggest_text(d):
    t = _texts(d)
    return max(t, key=lambda e: e["font_size"]) if t else None


def _non_bg_images(d):
    from eval.grader.forensics import ASSETS
    W, H = d["canvas"]["width"], d["canvas"]["height"]
    return [e for e in d["elements"] if e["type"] == "image" and e.get("fit") != "cover"
            and (ASSETS / str(e.get("asset_id"))).is_file()
            and e["width"] * e["height"] < 0.5 * W * H and e["width"] > 4 and e["height"] > 4]


def stretch(f):
    def go(src, out):
        o = copy.deepcopy(out)
        im = [e for e in _non_bg_images(o) if e["x"] + e["width"] * f <= o["canvas"]["width"]]
        if not im:
            return None
        e = max(im, key=lambda e: e["width"] * e["height"])
        e["width"] *= f
        return o
    return go


def off_canvas(src, out):
    o = copy.deepcopy(out)
    sw, sh = src["canvas"]["width"], src["canvas"]["height"]
    inside = {e["id"] for e in src["elements"] if e["x"] >= 0 and e["y"] >= 0 and e["x"] + e["width"] <= sw
              and e["y"] + e["height"] <= sh and not e.get("rotation")}
    t = [e for e in _texts(o) if e["id"] in inside]
    e = max(t, key=lambda e: e["font_size"]) if t else None
    if e is None:
        return None
    e["x"] = o["canvas"]["width"] - e["width"] * 0.5
    return o


def tiny_text(src, out):
    o = copy.deepcopy(out)
    e = _biggest_text(o)
    if e is None:
        return None
    e["font_size"] = 3.0
    return o


def _center_on(o, mover, target):
    W, H = o["canvas"]["width"], o["canvas"]["height"]
    mover["x"] = min(max(0, target["x"] + target["width"] / 2 - mover["width"] / 2), max(0, W - mover["width"]))
    mover["y"] = min(max(0, target["y"] + target["height"] / 2 - mover["height"] / 2), max(0, H - mover["height"]))


def text_on_text(src, out):
    o = copy.deepcopy(out)
    t = sorted(_texts(o), key=lambda e: -e["font_size"])
    if len(t) < 2:
        return None
    _center_on(o, t[1], t[0])
    return o


def bury_text(src, out):
    """Move an opaque picture/shape painted ABOVE some text onto that text."""
    o = copy.deepcopy(out)
    z = {e["id"]: (e.get("z_index", 0), i) for i, e in enumerate(o["elements"])}
    shapes = [e for e in o["elements"] if e["type"] == "shape" and e.get("opacity", 1) >= 0.9]
    for t in sorted(_texts(o), key=lambda e: -e["font_size"]):
        above = [e for e in _non_bg_images(o) + shapes
                 if z[e["id"]] > z[t["id"]] and (not t.get("group_id") or e.get("group_id") != t["group_id"])
                 and e["width"] >= t["width"] * 0.5 and e["height"] >= t["height"] * 0.5]
        if above:
            _center_on(o, max(above, key=lambda e: e["width"] * e["height"]), t)
            return o
    return None


def detach_label(src, out):
    """Pull a grouped label out of its pill/button (shape in the same group)."""
    o = copy.deepcopy(out)
    W, H = o["canvas"]["width"], o["canvas"]["height"]
    for t in _texts(o):
        g = t.get("group_id")
        pill = [e for e in o["elements"] if g and e.get("group_id") == g and e["type"] == "shape"]
        if not pill:
            continue
        p = pill[0]
        if p["y"] + p["height"] * 2.2 + t["height"] < H:
            t["y"] = p["y"] + p["height"] * 1.6
        elif p["y"] - p["height"] * 1.6 >= 0:
            t["y"] = p["y"] - p["height"] * 1.6
        elif p["x"] + p["width"] * 2.2 + t["width"] < W:
            t["x"] = p["x"] + p["width"] * 1.6
        elif p["x"] - p["width"] * 1.6 >= 0:
            t["x"] = p["x"] - p["width"] * 1.6
        else:
            continue
        return o
    return None


def collapse(src, out):
    o = copy.deepcopy(out)
    im = _non_bg_images(o)
    if not im:
        return None
    e = max(im, key=lambda e: e["width"] * e["height"])
    e["width"], e["height"] = e["width"] * 0.02, e["height"] * 0.02
    return o


def hierarchy_swap(src, out):
    o = copy.deepcopy(out)
    t = sorted(_texts(o), key=lambda e: e["font_size"])
    if len(t) < 2 or t[0]["font_size"] >= t[-1]["font_size"]:
        return None
    t[0]["font_size"] = float(int(t[-1]["font_size"] * 1.5))
    return o


def field(key, value):
    def go(src, out):
        o = copy.deepcopy(out)
        for e in o["elements"]:
            if key in e:
                e[key] = value(e[key]) if callable(value) else value
                return o
        return None
    return go


def drop(src, out):
    o = copy.deepcopy(out)
    o["elements"] = o["elements"][:-1]
    return o


def dupe(src, out):
    o = copy.deepcopy(out)
    o["elements"].append(copy.deepcopy(o["elements"][-1]))
    return o


def wrong_canvas(src, out):
    o = copy.deepcopy(out)
    o["canvas"] = {**o["canvas"], "width": o["canvas"]["width"] + 10}
    return o


def png_blur(src, out):
    """Edit the delivered PNG (blur one picture's region): pixels no longer the official render."""
    im = _non_bg_images(out)
    if not im:
        return None
    e = max(im, key=lambda e: e["width"] * e["height"])

    def tamper(a: np.ndarray) -> np.ndarray:
        s = a.shape[1] / out["canvas"]["width"]
        box = tuple(int(v * s) for v in (e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"]))
        img = Image.fromarray(a)
        img.paste(img.crop(box).filter(ImageFilter.GaussianBlur(3)), box[:2])
        return np.asarray(img)
    return copy.deepcopy(out), tamper


def rotated_into_edge(src, out):
    """A rotated element whose rotated box now pokes 5% past the right edge."""
    from eval.grader.hard import _outside
    o = copy.deepcopy(out)
    ok = {e["id"] for e in src["elements"] if not _outside(e, src["canvas"]["width"], src["canvas"]["height"])}
    for e in o["elements"]:
        if float(e.get("rotation") or 0) % 90 and e["width"] > 10 and e["id"] in ok:
            from eval.grader.hard import _rot_aabb
            e["x"] += o["canvas"]["width"] - _rot_aabb(e)[2] + 0.05 * e["width"]   # rotated box 5% past the edge
            return o
    return None


def nudge(src, out):
    o = copy.deepcopy(out)
    for e in o["elements"]:
        if e["type"] == "text" and e["x"] >= 2:
            e["x"] -= 1
            return o
    return None


DEFECTS = {
    "stretch_1.5": (stretch(1.5), "H6"),
    "stretch_1.08": (stretch(1.08), "H6"),
    "text_off_canvas": (off_canvas, "H5a"),
    "tiny_text_3px": (tiny_text, "readability"),
    "text_on_text": (text_on_text, "collisions"),
    "bury_text": (bury_text, "collisions"),
    "detach_label": (detach_label, "relations"),
    "collapse_picture": (collapse, "presence"),
    "hierarchy_swap": (hierarchy_swap, "hierarchy"),
    "content_changed": (field("content", lambda c: str(c) + "!"), "H4"),
    "font_changed": (field("font_family", "Comic Sans MS"), "H4"),
    "asset_changed": (field("asset_id", "other.png"), "H4"),
    "color_changed": (field("color", "rgba(1,2,3,1)"), "H4"),
    "element_dropped": (drop, "H2"),
    "id_duplicated": (dupe, "H2"),
    "wrong_canvas": (wrong_canvas, "H3"),
    "png_tampered": (png_blur, "H7"),
    "rotated_into_edge": (rotated_into_edge, "H5"),
}
CONTROLS = {"nudge_1px": nudge, "stretch_1.03": stretch(1.03)}   # must not change the verdict
