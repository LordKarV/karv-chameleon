"""Internal objective used to pick among candidate layouts (H3). Mirrors the
rubric: collisions, readability, hierarchy/boldness, use of space, balance.
Lower is better. Independent from eval/metrics.py on purpose."""
from __future__ import annotations

import math

from engine import measure
from engine.analyze import Design, Unit, union, area, inter, num, rot_aabb, alpha, text_rect
from engine.layout import Ctx

W_OVERLAP, W_DECOR, W_READ, W_SIZE, W_HIER, W_SPACE, W_BAL, W_OOB = 6.0, 0.6, 3.0, 1.5, 1.0, 2.0, 0.8, 50.0
W_BARE = 10.0
W_CLASH = 6.0     # per (text, element) pair that overlaps now but was apart in the source (clash_pairs)
W_BACKDROP = 4.0
W_BURIED = 15.0   # per (importance-weighted) text newly hidden under another unit: worse than tiny
W_VANISH = 10.0   # per (importance-weighted) unit that shrank to a speck vs its source share of the canvas
VANISH_KEEP = 0.25  # mirrors the grader's presence check
W_COVER = 8.0     # content covering less of the canvas than the source did (vision loop: cards at half width)
W_GAP = 10.0      # a dead band inside the content much wider than the source's own gaps
W_DEAD = 8.0      # empty margin at a canvas side much wider than the source's (design shrunk into the centre)
DEAD_SLACK = 0.05
W_INVISIBLE = 10.0   # per (importance-weighted) light text that moved off its backdrop onto the white canvas
COVER_KEEP, GAP_SLACK = 0.85, 0.03
W_ILLEGIBLE = 6.0
HIER_DRIFT = 1.4   # a text may grow up to 1.4x relative to the headline (vs the source) before it competes  # per (importance-weighted) text element below its legible minimum
W_REL = {"concentric": 3.0, "focal": 2.0, "proportion": 1.0, "deadzone": 5.0, "edge": 1.5,
         "proximity": 0.5, "lines": 1.0}   # engine/relations.py composition terms   # text/button now sits on a different panel colour / picture than in the source      # canvas left uncovered (white) when the design has no full-bleed background


from engine.analyze import placed_visual as _vis


def _members(u: Unit):
    return u.elements + [m for c in u.children for m in _members(c)]


def _is_band(u: Unit, d: Design) -> bool:
    """A header/footer/side band: an opaque thin panel whose shape spans the canvas edge to edge."""
    from engine.analyze import band_axis
    return band_axis(u, d.W, d.H) is not None and num(u.elements[0].get("opacity"), 1.0) >= 0.5


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


CLASH_MIN = 0.03   # new overlap (share of the smaller box) below this is a touch, not a collision


def _core(e: dict, g: dict) -> tuple:
    """Box to collide on. A text: its ink columns (a rule beside centred copy in a wide box is clear of
    it) by the glyph band of its lines, line gap trimmed (a rule right under the copy is clear of it).
    Anything else: its visible box, at least 2 px thick (a hairline rule has no area)."""
    if e["type"] == "text":
        x, y, w, h = (num(g.get(k)) for k in ("x", "y", "width", "height"))
        if e.get("rotation"):
            return rot_aabb(x, y, x + w, y + h, e["rotation"])
        el = {**e, **g}
        fs = num(el.get("font_size"), 16.0)
        n = measure.layout_text(el, fs, max(1.0, w))[0]
        r = text_rect(el)
        return r[0], y + 0.15 * fs, r[2], y + (max(1, n) - 1) * measure.line_pitch(el, fs) + 0.85 * fs
    r = _vis(e, g)
    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
    hw, hh = max(1.0, (r[2] - r[0]) / 2), max(1.0, (r[3] - r[1]) / 2)
    return cx - hw, cy - hh, cx + hw, cy + hh


def clash_pairs(d: Design, place: dict) -> list:
    """Role-blind collisions: (text id, other id, overlap) for every text and every other visible
    element (rule, icon, panel, picture, decoration, another text) that overlap now but were apart in
    the source. Unit rects miss these: a frame or cluster's union box already 'held' the text."""
    els = [e for e in (d.decor + [m for u in d.units for m in _members(u)])
           if e["id"] in place and (e["type"] == "text" or alpha(e) >= 0.1)]

    def boxes(geom):
        return {e["id"]: _core(e, geom(e)) for e in els}
    src = getattr(d, "_clash_src", None)
    if src is None:
        src = d._clash_src = boxes(lambda e: e)
    now = boxes(lambda e: place[e["id"]])

    def frac(b, i, j):
        return inter(b[i], b[j]) / max(1.0, min(area(b[i]), area(b[j])))
    out = []
    for i, t in enumerate(els):
        if t["type"] != "text":
            continue
        for j, o in enumerate(els):
            if o is t or (o["type"] == "text" and j < i):
                continue
            a, b = t["id"], o["id"]
            if inter(src[a], src[b]) > 0:   # overlapping in the source is the designer's layering, not ours
                continue
            extra = frac(now, a, b)
            if extra > CLASH_MIN:
                out.append((a, b, extra))
    return out


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


def _pair_overlap(a, b, now, src) -> float:
    """Overlap of a and b beyond what the source already had (intentional overlap is free)."""
    ov = inter(now[a.uid], now[b.uid])
    if ov <= 0:
        return 0.0
    frac = ov / max(1.0, min(area(now[a.uid]), area(now[b.uid])))
    frac0 = inter(src[a.uid], src[b.uid]) / max(1.0, min(area(src[a.uid]), area(src[b.uid])))
    extra = frac - frac0
    pen = extra * (1.0 if (a.texts or b.texts) else 0.4) + 0.05 if extra > 0.01 else 0.0
    halo = a if a.role == "decor" else b if b.role == "decor" else None
    if halo is not None:   # a pinned halo / shadow is translucent layering: it collides in proportion to its alpha
        from engine.analyze import alpha, DECOR_ALPHA
        pen *= min(1.0, max(alpha(e) for e in halo.elements) / DECOR_ALPHA)
    return pen


def _band(rects, axis) -> float:
    """Largest empty band strictly inside the content span along axis (0 = x, 1 = y)."""
    iv = sorted((r[axis], r[axis + 2]) for r in rects)
    best, cur = 0.0, iv[0][1]
    for a, b in iv[1:]:
        best, cur = max(best, a - cur), max(cur, b)
    return best


def _shipped(e: dict, p: dict, W: float, H: float) -> dict:
    """The box finalize ships: a non-cover image keeps its source aspect inside its placed box (a full-canvas
    backdrop is centred, like the renderer's contain; other boxes keep their top-left)."""
    sw, sh, w, h = num(e.get("width")), num(e.get("height")), p["width"], p["height"]
    if e.get("type") != "image" or e.get("fit") == "cover" or min(sw, sh, w, h) <= 0:
        return p
    a = sw / sh
    w2, h2 = (w, w / a) if w / a <= h else (h * a, h)
    full = w >= W - 1 and h >= H - 1
    return {**p, "x": p["x"] + ((w - w2) / 2 if full else 0.0), "y": p["y"] + ((h - h2) / 2 if full else 0.0),
            "width": w2, "height": h2}


def _dead(d, units, rects, place, W, H) -> float:
    """Empty margin at each canvas side beyond the source's own margin there (a whole design letterboxed into the
    centre of a strip). Margins a contained backdrop image leaves anyway (pillarbox bars) are allowed.
    Only on extreme reshapes (|log aspect change| 1.0 -> 1.5 fades it in): on R_dev the term won on strips and
    strip -> 16:9, and lost on milder reshapes where the source's own layout already uses the page."""
    fade = min(1.0, max(0.0, (abs(math.log((W / H) / (d.W / d.H))) - 1.0) / 0.5))
    flow = [u for u in units if not u.host and u.uid in rects]
    if not flow or not fade:
        return 0.0
    c, s = union(rects[u.uid] for u in flow), union(u.rect for u in flow)
    bg = [_vis(e, _shipped(e, place[e["id"]], W, H)) for e in d.background]
    b = union(bg) if bg else (0.0, 0.0, W, H)

    def sides(r, w, h):
        return max(0.0, r[0]) / w, max(0.0, w - r[2]) / w, max(0.0, r[1]) / h, max(0.0, h - r[3]) / h
    return fade * sum(max(0.0, n - max(o, g) - DEAD_SLACK)
                      for n, o, g in zip(sides(c, W, H), sides(s, d.W, d.H), sides(b, W, H)))


def _fill_terms(d, units, rects, W, H) -> tuple[float, float]:
    """Vision-loop findings (eval/vision_loop): good resizes keep the source's coverage of the
    canvas and don't open dead bands inside the composition."""
    flow = [u for u in units if not u.host and not u.corner and u.uid in rects]
    if len(flow) < 2:
        return 0.0, 0.0
    now = [rects[u.uid] for u in flow]
    was = [u.rect for u in flow]
    cov, cov0 = sum(area(r) for r in now) / (W * H), sum(area(r) for r in was) / (d.W * d.H)
    cover = max(0.0, COVER_KEEP * cov0 - cov) / max(cov0, 1e-6)
    gap = sum(max(0.0, _band(now, ax) / L - max(_band(was, ax) / L0, 0.05) - GAP_SLACK)
              for ax, L, L0 in ((0, W, d.W), (1, H, d.H)))
    return cover, gap


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
             "illegible": W_ILLEGIBLE, "buried": W_BURIED, "clash": W_CLASH, "vanish": W_VANISH, "cover": W_COVER, "gap": W_GAP,
             "dead": W_DEAD, "invisible": W_INVISIBLE, **W_REL}
        path = os.environ.get("CHAMELEON_WEIGHTS")
        if path:
            w.update({k: float(v) for k, v in json.load(open(path)).items() if k in w})
        _WEIGHTS = w
    return _WEIGHTS


def _backdrop_key(e: dict) -> str:
    if e["type"] == "image":
        return "img:" + str(e.get("asset_id"))
    return "shape:" + str(e.get("fill_color"))


def _backdrop_changes(d: Design, place: dict, W: float, H: float) -> float:
    """For every text-bearing unit, what is directly behind its centre and ends (topmost
    lower-z panel, picture or the background) must be the same as in the source:
    'text that sat on the dark panel now sits on orange' is an obvious break."""
    every = d.background + [e for e in d.decor] + [e for u in d.units for e in _members(u)]
    from engine.analyze import alpha   # fill alpha counts too: a 10% halo ring behind the copy is not a panel
    backs = [e for e in every if e["type"] != "text" and (e["type"] == "image" or
             (alpha(e) >= 0.3 and e.get("shape_type") != "line"))]

    def behind(pt, zmin, geom):
        best = None
        for e in backs:
            if e["z_index"] >= zmin:
                continue
            x0, y0, x1, y1 = geom(e)
            if x0 <= pt[0] <= x1 and y0 <= pt[1] <= y1 and (best is None or e["z_index"] > best["z_index"]):
                best = e
        if best is None or (best["type"] == "shape" and _light(best, "fill_color")):
            return "none"    # bare (white) canvas or a white-ish panel: differs from a contained photo's bars
        if best in d.background:
            return "bg"
        return _backdrop_key(best)

    src_geom = lambda e: _vis(e, e)   # rotation applied: a portrait photo turned 90 deg covers a landscape band
    out_geom = lambda e: _vis(e, _shipped(e, place.get(e["id"], e), W, H))
    pen = gone = 0.0
    for u in d.units:
        for x in ([u] if u.kind != "container" else [k for k in u.children]):
            if not x.texts or x.host:
                continue
            zmin = min(e["z_index"] for e in _members(x))
            r0 = x.rect
            r1 = union(_vis(e, place[e["id"]]) for e in _members(x))
            # the centre; when it held, each end at a quarter weight (copy spilling off its panel or picture)
            ks = [(behind((r0[0] + f * (r0[2] - r0[0]), (r0[1] + r0[3]) / 2), zmin, src_geom),
                   behind((r1[0] + f * (r1[2] - r1[0]), (r1[1] + r1[3]) / 2), zmin, out_geom)) for f in (0.5, 0.1, 0.9)]
            moved = [a != b for a, b in ks]
            pen += x.importance * (1.0 if moved[0] else 0.25 * sum(moved[1:]))
            if any(_light(e) for e in x.texts):   # light copy moved onto white canvas or a white panel: unreadable
                gone += x.importance * sum(a != "none" and b == "none" for a, b in ks) / 3
    return pen, gone


LIGHT = 200   # text luminance (0-255) that disappears on the renderer's white canvas


def _light(e: dict, key: str = "color") -> bool:
    from renderer.converter.colorutil import parse_rgba
    r, g, b, _ = parse_rgba(e.get(key))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b >= LIGHT


def energy(d: Design, place: dict, W: float, H: float) -> tuple[float, dict]:
    from engine.analyze import band_axis, BAND_MAX
    ctx = Ctx(W, H, d.W, d.H, 0)

    def _thin(u, r):   # the placed band is still thin on its axis (on a strip it may fill the canvas)
        ax = band_axis(u, d.W, d.H)
        return (r[3] - r[1] <= BAND_MAX * H) if ax == "h" else (r[2] - r[0] <= BAND_MAX * W)
    s_area = math.sqrt(W * H / (d.W * d.H))
    units = d.units
    rects = {u.uid: union(_vis(e, place[e["id"]]) for e in _members(u)) for u in units}
    terms = dict.fromkeys(("overlap", "decor", "read", "size", "hier", "space", "bal", "oob", "bare", "backdrop",
                           "illegible"), 0.0)

    def content_rect(u, r_of):  # a panel is a backdrop: collide on what it holds
        if u.kind == "container" and u.children and u.texts and not (_is_band(u, d) and _thin(u, r_of(u))):
            return union(r_of(k) for k in u.children)
        # a band spanning the canvas, or a disc holding only a picture (a halo), is a surface of its own:
        # it collides on its whole shape, not just on what it holds (halo sliding onto the header band)
        return r_of(u)
    now = {u.uid: content_rect(u, lambda x: rects.get(x.uid) or union(_vis(e, place[e["id"]]) for e in _members(x)))
           for u in units}
    src = {u.uid: content_rect(u, lambda x: x.rect) for u in units}
    for i, a in enumerate(units):
        for b in units[i + 1:]:
            terms["overlap"] += _pair_overlap(a, b, now, src)
    for u in units:      # children of one panel collide too (CTA over the headline inside a card)
        if u.kind == "container":
            kids = u.children
            kn = {k.uid: union(_vis(e, place[e["id"]]) for e in _members(k)) for k in kids}
            ks = {k.uid: k.rect for k in kids}
            for i, a in enumerate(kids):
                for b in kids[i + 1:]:
                    terms["overlap"] += _pair_overlap(a, b, kn, ks)
    terms["clash"] = sum(min(1.0, x) + 0.05 for _, _, x in clash_pairs(d, place))
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
    terms["vanish"] = _vanish(d, leaf_units, rects, W, H)   # a logo inside a header panel vanishes too
    terms["cover"], terms["gap"] = _fill_terms(d, units, rects, W, H)
    terms["dead"] = _dead(d, units, rects, place, W, H)
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

    terms["backdrop"], terms["invisible"] = _backdrop_changes(d, place, W, H)
    from engine.relations import relation_terms
    rel = relation_terms(d, place, W, H, rects)
    terms.update(rel)

    for i, p in place.items():
        over = max(0, -p["x"]) + max(0, -p["y"]) + max(0, p["x"] + p["width"] - W) + max(0, p["y"] + p["height"] - H)
        terms["oob"] += over / max(W, H)

    w = weights()
    total = sum(w[k] * terms.get(k, 0.0) for k in w)
    return total, terms
