"""Trivial reference engines for sanity-checking the metrics (not real solutions)."""


def stretch(source: dict, target: dict) -> dict:
    """Non-uniform stretch (the starter baseline)."""
    sx = target["width"] / source["canvas"]["width"]
    sy = target["height"] / source["canvas"]["height"]
    els = [{**e, "x": e["x"] * sx, "y": e["y"] * sy, "width": e["width"] * sx, "height": e["height"] * sy,
            **({"font_size": max(1.0, e["font_size"] * min(sx, sy))} if e["type"] == "text" else {})}
           for e in source["elements"]]
    return {"canvas": {"width": target["width"], "height": target["height"]}, "elements": els}


def fit_center(source: dict, target: dict) -> dict:
    """Uniform scale-to-fit, centred (letterbox). Full-canvas cover images are stretched to fill."""
    cw, ch = source["canvas"]["width"], source["canvas"]["height"]
    s = min(target["width"] / cw, target["height"] / ch)
    ox, oy = (target["width"] - cw * s) / 2, (target["height"] - ch * s) / 2
    els = []
    for e in source["elements"]:
        if e["type"] in ("image", "shape") and e.get("fit") == "cover" and e["width"] >= 0.85 * cw and e["height"] >= 0.85 * ch:
            els.append({**e, "x": 0, "y": 0, "width": target["width"], "height": target["height"]})
            continue
        x0, y0 = max(0.0, e["x"]), max(0.0, e["y"])  # clamp source bleed so we stay on-canvas
        x1, y1 = min(cw, e["x"] + e["width"]), min(ch, e["y"] + e["height"])
        n = {**e, "x": ox + x0 * s, "y": oy + y0 * s, "width": (x1 - x0) * s, "height": (y1 - y0) * s}
        if e["type"] == "text":
            n["font_size"] = max(1.0, e["font_size"] * s)
        els.append(n)
    return {"canvas": {"width": target["width"], "height": target["height"]}, "elements": els}


def crash(source: dict, target: dict) -> dict:
    raise RuntimeError("intentional crash")


def hang(source: dict, target: dict) -> dict:
    import time
    time.sleep(3600)
