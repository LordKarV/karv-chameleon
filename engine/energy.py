"""Internal objective used to pick among candidate layouts (H3). Mirrors the
rubric: collisions, readability, hierarchy/boldness, use of space, balance.
Lower is better. Independent from eval/metrics.py on purpose."""
from __future__ import annotations

import math

from engine import measure
from engine.analyze import Design, Unit, union, area, inter, num, rot_aabb
from engine.layout import Ctx

W_OVERLAP, W_DECOR, W_READ, W_SIZE, W_HIER, W_SPACE, W_BAL, W_OOB = 6.0, 0.6, 3.0, 1.5, 1.0, 2.0, 0.8, 50.0
W_BARE = 10.0
W_BACKDROP = 4.0
W_BURIED = 15.0   # per (importance-weighted) text newly hidden under another unit: worse than tiny
W_VANISH = 10.0   # per (importance-weighted) unit that shrank to a speck vs its source share of the canvas
VANISH_KEEP = 0.25  # mirrors the grader's presence check
W_ILLEGIBLE = 6.0
HIER_DRIFT = 1.4   # a text may grow up to 1.4x relative to the headline (vs the source) before it competes  # per (importance-weighted) text element below its legible minimum
W_REL = {"concentric": 3.0, "focal": 2.0, "proportion": 1.0, "deadzone": 5.0, "edge": 1.5,
         "proximity": 0.5, "lines": 1.0}   # engine/relations.py composition terms   # text/button now sits on a different panel colour / picture than in the source      # canvas left uncovered (white) when the design has no full-bleed background


from engine.analyze import placed_visual as _vis


def _members(u: Unit):
    return u.elements + [m for c in u.children for m in _members(c)]


BURIED = 0.2      # share of a text's box newly under another unit's opaque shape/image painted above it


def _buried(units, place) -> float:
    """Text hidden under an opaque card/picture of ANOTHER unit painted above it counts as
    illegible (box overlap alone undercounts it: cards stacked on cards hide their labels)."""
    owner, els = {}, []
    for u in units:
        for e in _members(u):
            owner[e["id"]] = u.uid
            els.append(e)
    rank = {e["id"]: (num(e.get("z_index")), i) for i, e in enumerate(els)}
    cover = [e for e in els if e["type"] in ("shape", "image") and num(e.get("opacity"), 1.0) >= 0.5]
    res = 0.0
    for u in units:
        for t in (x for x in _members(u) if x["type"] == "text"):
            r, r0 = _vis(t, place[t["id"]]), _vis(t, t)
            worst = 0.0
            for c in cover:
                if owner[c["id"]] == u.uid or rank[c["id"]] <= rank[t["id"]]:
                    continue
                now = inter(r, _vis(c, place[c["id"]])) / max(1.0, area(r))
                worst = max(worst, now - inter(r0, _vis(c, c)) / max(1.0, area(r0)))
            if worst > BURIED:
                res += getattr(u, "importance", 1.0)
    return res


def _vanish(d, units, rects, W, H) -> float:
    """Pictures and logos that keep < VANISH_KEEP of their source share of the canvas
    (a hero shrunk to a speck reads as missing, whatever the layout gained)."""
    res = 0.0
    for u in units:
        if u.kind not in ("image", "group") or u.texts or u.role == "missing" or u.uid not in rects:
            continue
        was = area(u.rect) / (d.W * d.H)
        now = area(rects[u.uid]) / (W * H)
        if was > 0.0005 and now < VANISH_KEEP * was:
            res += u.importance * (1 - now / (VANISH_KEEP * was))
    return res


_WEIGHTS = None


def weights() -> dict:
    """Term -> weight. CHAMELEON_WEIGHTS=<json file> overrides individual weights
    (used by the selection-tuning experiments); defaults are the hand-set values."""
    global _WEIGHTS
    if _WEIGHTS is None:
        import json
        import os
        w = {"overlap": W_OVERLAP, "decor": W_DECOR, "read": W_READ, "size": W_SIZE, "hier": W_HIER,
             "space": W_SPACE, "bal": W_BAL, "oob": W_OOB, "bare": W_BARE, "backdrop": W_BACKDROP,
             "illegible": W_ILLEGIBLE, "buried": W_BURIED, "vanish": W_VANISH, **W_REL}
        path = os.environ.get("CHAMELEON_WEIGHTS")
        if path:
            w.update({k: float(v) for k, v in json.load(open(path)).items() if k in w})
        _WEIGHTS = w
    return _WEIGHTS


def _backdrop_key(e: dict) -> str:
    if e["type"] == "image":
        return "img:" + str(e.get("asset_id"))
    return "shape:" + str(e.get("fill_color"))


def _backdrop_changes(d: Design, place: dict) -> float:
    """For every text-bearing unit, what is directly behind its centre (topmost
    lower-z panel, picture or the background) must be the same as in the source:
    'text that sat on the dark panel now sits on orange' is an obvious break."""
    every = d.background + [e for e in d.decor] + [e for u in d.units for e in _members(u)]
    backs = [e for e in every if e["type"] != "text" and (e["type"] == "image" or
             (float(e.get("opacity", 1) or 0) >= 0.3 and e.get("shape_type") != "line"))]

    def behind(pt, zmin, geom):
        best = None
        for e in backs:
            if e["z_index"] >= zmin:
                continue
            x, y, w, h = geom(e)
            if x <= pt[0] <= x + w and y <= pt[1] <= y + h and (best is None or e["z_index"] > best["z_index"]):
                best = e
        if best is None or best in d.background:
            return "bg"
        return _backdrop_key(best)

    src_geom = lambda e: (e["x"], e["y"], e["width"], e["height"])
    out_geom = lambda e: tuple(place.get(e["id"], e)[k] for k in ("x", "y", "width", "height"))
    pen = 0.0
    for u in d.units:
        for x in ([u] if u.kind != "container" else [k for k in u.children]):
            if not x.texts or x.host:
                continue
            zmin = min(e["z_index"] for e in _members(x))
            r0 = x.rect
            r1 = union(_vis(e, place[e["id"]]) for e in _members(x))
            k0 = behind(((r0[0] + r0[2]) / 2, (r0[1] + r0[3]) / 2), zmin, src_geom)
            k1 = behind(((r1[0] + r1[2]) / 2, (r1[1] + r1[3]) / 2), zmin, out_geom)
            if k0 != k1:
                pen += x.importance
    return pen


def energy(d: Design, place: dict, W: float, H: float) -> tuple[float, dict]:
    ctx = Ctx(W, H, d.W, d.H, 0)
    s_area = math.sqrt(W * H / (d.W * d.H))
    units = d.units
    rects = {u.uid: union(_vis(e, place[e["id"]]) for e in _members(u)) for u in units}
    terms = dict.fromkeys(("overlap", "decor", "read", "size", "hier", "space", "bal", "oob", "bare", "backdrop",
                           "illegible"), 0.0)

    def content_rect(u, r_of):  # a panel is a backdrop: collide on what it holds
        return union(r_of(k) for k in u.children) if u.kind == "container" and u.children else r_of(u)
    now = {u.uid: content_rect(u, lambda x: rects.get(x.uid) or union(_vis(e, place[e["id"]]) for e in _members(x)))
           for u in units}
    src = {u.uid: content_rect(u, lambda x: x.rect) for u in units}
    for i, a in enumerate(units):
        for b in units[i + 1:]:
            ov = inter(now[a.uid], now[b.uid])
            if ov > 0:
                frac = ov / max(1.0, min(area(now[a.uid]), area(now[b.uid])))
                frac0 = inter(src[a.uid], src[b.uid]) / max(1.0, min(area(src[a.uid]), area(src[b.uid])))
                extra = frac - frac0          # overlap the source already had is intentional
                if extra > 0.01:
                    textish = bool(a.texts or b.texts)
                    terms["overlap"] += extra * (1.0 if textish else 0.4) + 0.05
    pics = [u for u in units if u.kind in ("image", "group") and not u.texts]
    for u in units:      # a panel's own shape now covering a picture it did not cover in the source
        if u.kind != "container" or not u.elements:
            continue
        sh, sh0 = _vis(u.elements[0], place[u.elements[0]["id"]]), _vis(u.elements[0], u.elements[0])
        for v in pics:
            extra = inter(sh, rects[v.uid]) / max(1.0, area(rects[v.uid])) - inter(sh0, v.rect) / max(1.0, area(v.rect))
            if extra > 0.05:
                terms["overlap"] += 0.6 * extra + 0.05
    for e in d.decor:
        r = _vis(e, place[e["id"]])
        for u in units:
            if u.texts:
                terms["decor"] += inter(r, rects[u.uid]) / max(1.0, area(rects[u.uid]))

    def leaves(u):
        return [x for k in u.children for x in leaves(k)] if u.kind == "container" else [u]
    leaf_units = [x for u in units for x in leaves(u)]
    for x in leaf_units:
        if x.uid not in rects:
            rects[x.uid] = union(_vis(e, place[e["id"]]) for e in _members(x))
    fonts, src_fs = {}, {}
    for u in leaf_units:
        for e in u.texts:
            fs = place[e["id"]]["font_size"]
            fonts[e["id"]] = (fs, u.role)
            src_fs[e["id"]] = num(e.get("font_size"), fs)
            fl = ctx.floor(u.role)
            terms["read"] += u.importance * max(0.0, fl - fs) / fl
            if fs < 6:
                terms["read"] += 1.0
            from engine.layout import ABS_MIN_TEXT
            if fs < ABS_MIN_TEXT.get(u.role, 8.0):   # OCR: below this nobody can read it
                terms["illegible"] += u.importance
        if u.texts:
            t = (min(place[e["id"]]["font_size"] / e["font_size"] for e in u.texts)) / s_area
        else:
            t = (rects[u.uid][2] - rects[u.uid][0]) / u.w / s_area
        if not u.host:
            terms["size"] += u.importance * (max(0.0, -math.log(max(t, 1e-3) / 0.8)) + max(0.0, math.log(max(t, 1e-3) / 2.4)))
    terms["buried"] = _buried(units, place)
    terms["vanish"] = _vanish(d, units, rects, W, H)
    head = [fs for fs, r in fonts.values() if r == "headline"]
    if head:
        terms["hier"] += sum(1 for fs, r in fonts.values() if r != "headline" and fs > max(head) + 0.5)
        h0 = max(src_fs[k] for k, (_, r) in fonts.items() if r == "headline")
        terms["hier"] += sum(max(0.0, (fs / max(head)) / (src_fs[k] / h0) - HIER_DRIFT)   # badge balloons vs headline
                             for k, (fs, r) in fonts.items() if r != "headline" and src_fs[k] > 0)

    flow = [rects[u.uid] for u in units if not u.host]
    if flow:
        c = union(flow)
        src = union(u.rect for u in units if not u.host)
        want_w = min(0.85, (src[2] - src[0]) / d.W)
        want_h = min(0.85, (src[3] - src[1]) / d.H)
        if getattr(d, "_fill_intent", None):          # insight: this design should fill the canvas
            want_w, want_h = 0.85, 0.85
        terms["space"] += max(0.0, want_w - (c[2] - c[0]) / W) + max(0.0, want_h - (c[3] - c[1]) / H)
        fill = sum(area(r) for r in flow) / (W * H)
        terms["space"] += max(0.0, 0.22 - fill) * 2
        mass = sum(area(r) for r in flow) or 1.0
        mx = sum(area(r) * (r[0] + r[2]) / 2 for r in flow) / mass
        my = sum(area(r) * (r[1] + r[3]) / 2 for r in flow) / mass
        terms["bal"] += abs(mx - W / 2) / W + abs(my - H / 2) / H

    if not d.background:   # no full-bleed background: panels must still cover the canvas
        cover = [_vis(e, place[e["id"]]) for u in units for e in _members(u)
                 if e["type"] != "text" and (e.get("opacity", 1) or 0) >= 0.5]
        n, bare = 16, 0
        for gx in range(n):
            for gy in range(n):
                px, py = (gx + 0.5) * W / n, (gy + 0.5) * H / n
                if not any(r[0] <= px <= r[2] and r[1] <= py <= r[3] for r in cover):
                    bare += 1
        src_bare = 0
        src_cover = [(e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"]) for u in units for e in _members(u)
                     if e["type"] != "text" and (e.get("opacity", 1) or 0) >= 0.5]
        for gx in range(n):
            for gy in range(n):
                px, py = (gx + 0.5) * d.W / n, (gy + 0.5) * d.H / n
                if not any(r[0] <= px <= r[2] and r[1] <= py <= r[3] for r in src_cover):
                    src_bare += 1
        terms["bare"] = max(0, bare - src_bare) / (n * n)

    terms["backdrop"] = _backdrop_changes(d, place)
    from engine.relations import relation_terms
    rel = relation_terms(d, place, W, H, rects)
    terms.update(rel)

    for i, p in place.items():
        over = max(0, -p["x"]) + max(0, -p["y"]) + max(0, p["x"] + p["width"] - W) + max(0, p["y"] + p["height"] - H)
        terms["oob"] += over / max(W, H)

    w = weights()
    total = sum(w[k] * terms.get(k, 0.0) for k in w)
    return total, terms
