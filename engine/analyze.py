"""Turn a flat element list into a design model with no reliance on ids:
background, decoration, flow units (groups / underlay containers / singles),
semantic roles, importance, and source reading rows."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from engine import measure

BG_COVERAGE = 0.85          # fraction of canvas an element must cover to be background
DECOR_ALPHA = 0.5           # shapes fainter than this are decoration
ROW_OVERLAP = 0.5           # vertical overlap (of the smaller) to share a reading row

IMPORTANCE = {"headline": 1.0, "hero": 0.9, "cta": 0.85, "logo": 0.8, "subhead": 0.6,
              "image": 0.55, "body": 0.5, "button": 0.5, "badge": 0.4, "ribbon": 0.4,
              "panel": 0.5, "line": 0.3, "legal": 0.2, "missing": 0.45}


@dataclass
class Unit:
    uid: str
    kind: str                    # text | image | group | container | line
    elements: list               # source element dicts (container: just the shape)
    rect: tuple                  # visual rect in source (x0, y0, x1, y1)
    role: str = "other"
    children: list = field(default_factory=list)   # container only: child Units
    pad: tuple = (0, 0, 0, 0)    # container only: source padding l, t, r, b
    edges: tuple = ()            # container only: canvas edges its shape touched in the source
    parent: str | None = None    # set on children of an unpacked container
    host: str | None = None      # sticker: uid of the unit it is pinned to
    corner: str | None = None    # corner ribbon/stamp: which canvas corner it hugs ("tl","tr","bl","br")
    rel: tuple = (0.0, 0.0)      # sticker: centre relative to host rect (fractions)

    @property
    def w(self): return max(1e-6, self.rect[2] - self.rect[0])
    @property
    def h(self): return max(1e-6, self.rect[3] - self.rect[1])
    @property
    def importance(self): return IMPORTANCE.get(self.role, 0.5)
    @property
    def texts(self):
        out = [e for e in self.elements if e["type"] == "text"]
        for c in self.children:
            out += c.texts
        return out
    @property
    def max_font(self):
        return max((float(e["font_size"]) for e in self.texts), default=0.0)


@dataclass
class Design:
    W: float
    H: float
    background: list             # element dicts
    decor: list                  # element dicts
    units: list                  # top-level flow Units
    rows: list                   # list[list[Unit]] in reading order
    missing: list                # Units for images with missing assets (also in units)
    open_rows: list = field(default_factory=list)  # rows with containers unpacked into children


def num(v, default: float = 0.0) -> float:
    """Junk-tolerant number: ints, floats and numeric strings; anything else
    (None, NaN, inf, 'abc', lists) becomes the default."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def norm(el: dict) -> dict:
    e = dict(el)
    e["rotation"] = num(e.get("rotation"), 0.0) % 360
    e["opacity"] = min(1.0, max(0.0, num(e.get("opacity"), 1.0)))
    e["z_index"] = int(num(e.get("z_index"), 0.0))
    for k in ("x", "y", "width", "height"):
        e[k] = num(e.get(k), 0.0)
    e["width"], e["height"] = max(0.0, e["width"]), max(0.0, e["height"])
    if e.get("type") == "text":
        e["font_size"] = max(1.0, num(e.get("font_size"), 16.0) or 16.0)
        e["content"] = str(e.get("content", "") if e.get("content") is not None else "")
    return e


def rot_aabb(x0, y0, x1, y1, deg):
    if not deg:
        return x0, y0, x1, y1
    cx, cy, a = (x0 + x1) / 2, (y0 + y1) / 2, math.radians(deg)
    hw = (abs((x1 - x0) * math.cos(a)) + abs((y1 - y0) * math.sin(a))) / 2
    hh = (abs((x1 - x0) * math.sin(a)) + abs((y1 - y0) * math.cos(a))) / 2
    return cx - hw, cy - hh, cx + hw, cy + hh


def text_rect(e: dict) -> tuple:
    """Where the text's ink actually is (alignment inside its box applied)."""
    n, widest, _ = measure.layout_text(e, e["font_size"], max(1.0, e["width"]))
    h = measure.text_height(e, e["font_size"], n)
    al = e.get("text_align", "left")
    off = {"center": (e["width"] - widest) / 2, "right": e["width"] - widest}.get(al, 0.0)
    return e["x"] + max(0.0, off), e["y"], e["x"] + max(0.0, off) + min(widest, max(widest, 1)), e["y"] + h


def visual_rect(e: dict) -> tuple:
    if e["type"] == "text":
        r = text_rect(e)
    elif e["type"] == "image":
        r = measure.image_visible_rect(e)
    else:
        r = (e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"])
    return rot_aabb(*r, e.get("rotation", 0))


def union(rects):
    rects = list(rects)
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def placed_visual(e: dict, p: dict) -> tuple:
    """Visual rect of element e after applying placement p."""
    return visual_rect({**e, **p})


def area(r):
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def inter(a, b):
    return area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def alpha(e: dict) -> float:
    from renderer.converter.colorutil import parse_rgba  # same parser as renderer
    try:
        a = parse_rgba(e.get("fill_color"), default=(0, 0, 0, 1.0))[3]
    except Exception:
        a = 1.0
    return float(e.get("opacity", 1)) * a


def _contains(outer, inner, tol=2.0):
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def analyze(source: dict) -> Design:
    W = max(1.0, num(source["canvas"].get("width"), 1.0))
    H = max(1.0, num(source["canvas"].get("height"), 1.0))
    els = [norm(e) for e in source["elements"]]
    canvas = (0.0, 0.0, W, H)
    vis = {e["id"]: visual_rect(e) for e in els}
    min_text_z = min((e["z_index"] for e in els if e["type"] == "text"), default=1e9)

    background, decor, rest = [], [], []
    for e in sorted(els, key=lambda e: e["z_index"]):
        box = (e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"])
        full = inter(box, canvas) >= BG_COVERAGE * W * H
        if e["type"] != "text" and full and e["z_index"] <= min_text_z and not e.get("group_id"):
            background.append(e)
        else:
            rest.append(e)

    # explicit groups become rigid units
    units, grouped = [], {}
    for e in rest:
        if e.get("group_id"):
            grouped.setdefault(e["group_id"], []).append(e)
    for gid, members in grouped.items():
        units.append(Unit(f"g:{gid}", "group", members, union(vis[m["id"]] for m in members)))
    singles = [e for e in rest if not e.get("group_id")]

    # shapes: decoration vs container (an opaque-ish shape that holds content)
    def bleeds(e):
        return e["x"] < -1 or e["y"] < -1 or e["x"] + e["width"] > W + 1 or e["y"] + e["height"] > H + 1

    shapes = [e for e in singles if e["type"] == "shape" and e.get("shape_type") != "line"]
    others = [e for e in singles if e not in shapes]
    containers = []
    for s in sorted(shapes, key=lambda e: e["width"] * e["height"]):
        r = vis[s["id"]]
        held = [o for o in others if o["z_index"] >= s["z_index"] and _contains(r, vis[o["id"]])]
        held_units = [u for u in units if all(m["z_index"] >= s["z_index"] for m in u.elements)
                      and _contains(r, u.rect)]
        is_ellipse = s.get("shape_type") == "ellipse"
        if (held or held_units) and not bleeds(s) and not (is_ellipse and alpha(s) < DECOR_ALPHA) \
                and not s.get("rotation"):
            containers.append((s, held, held_units))
        else:
            decor.append(s)

    taken = set()
    for s, held, held_units in containers:     # smallest first: innermost wins
        kids = []
        for o in held:
            if o["id"] not in taken:
                taken.add(o["id"])
                kids.append(_single(o, vis))
        for u in held_units:
            if u.uid not in taken:
                taken.add(u.uid)
                kids.append(u)
        if not kids:
            decor.append(s)
            continue
        r = vis[s["id"]]
        inner = union(k.rect for k in kids)
        pad = (inner[0] - r[0], inner[1] - r[1], r[2] - inner[2], r[3] - inner[3])
        tol = 0.01 * max(W, H)
        edges = tuple(n for n, hit in (("l", s["x"] <= tol), ("t", s["y"] <= tol),
                                       ("r", s["x"] + s["width"] >= W - tol),
                                       ("b", s["y"] + s["height"] >= H - tol)) if hit)
        cu = Unit(f"c:{s['id']}", "container", [s], r, children=kids, pad=pad, edges=edges)
        for k in kids:
            k.parent = cu.uid
        units.append(cu)
    units = [u for u in units if u.uid not in taken]
    # nested containers: drop any container already adopted by a bigger one
    adopted = {k.uid for u in units if u.kind == "container" for k in u.children}
    units = [u for u in units if u.uid not in adopted]
    for o in others:
        if o["id"] not in taken:
            units.append(_single(o, vis))

    _assign_roles(units + [k for u in units if u.kind == "container" for k in _descendants(u)], W, H)
    units = _cluster_pictures(units, W, H)
    decor = _pin_decor_to_pictures(units, decor)
    _attach_stickers(units)
    _mark_corners(units, W, H)
    # a missing main picture still leads the layout; a minor missing asset goes to a quiet corner
    missing = [u for u in units if u.role == "missing"]
    rows = xy_rows([u for u in units if not u.host and not u.corner and u.role != "missing"], W, H)
    open_rows = []
    for row in rows:
        if any(u.kind == "container" for u in row):
            flat = [k for u in row for k in (u.children if u.kind == "container" else [u])]
            open_rows += xy_rows(flat, W, H)
        else:
            open_rows.append(row)
    return Design(W, H, background, decor, units, rows, missing, open_rows)


PIC = ("hero", "image", "missing")


def _cluster_pictures(units, W, H):
    """Pictures that overlap each other in the source (a fanned trio of bottles)
    are one composition: merge them into a single rigid unit."""
    pics = [u for u in units if u.kind == "image" and u.role in PIC]
    parent = {u.uid: u.uid for u in pics}

    def find(a):
        while parent[a] != a:
            a = parent[a]
        return a
    for i, a in enumerate(pics):
        for b in pics[i + 1:]:
            ea, eb = a.elements[0], b.elements[0]
            vov = min(a.rect[3], b.rect[3]) - max(a.rect[1], b.rect[1])
            hgap = max(a.rect[0], b.rect[0]) - min(a.rect[2], b.rect[2])
            same_set = (ea.get("asset_id") == eb.get("asset_id") and vov > 0.5 * min(a.h, b.h)
                        and hgap < 0.08 * max(W, H))
            if inter(a.rect, b.rect) > 0.05 * min(area(a.rect), area(b.rect)) or same_set:
                parent[find(a.uid)] = find(b.uid)
    groups = {}
    for u in pics:
        groups.setdefault(find(u.uid), []).append(u)
    out = [u for u in units if u not in pics]
    for members in groups.values():
        if len(members) == 1:
            out.append(members[0])
            continue
        role = "hero" if any(m.role == "hero" for m in members) else "image"
        out.append(Unit("k:" + "+".join(m.uid for m in members), "group",
                        [e for m in members for e in m.elements], union(m.rect for m in members), role=role))
    return out


def _pin_decor_to_pictures(units, decor):
    """A decoration mostly under a picture and painted behind it (a drop shadow,
    a halo disc) belongs to that picture: pin it as a sticker."""
    keep = []
    every = units + [k for u in units if u.kind == "container" for k in _descendants(u)]
    pics = [u for u in every if u.role in PIC]

    def grown(u, f=0.25):
        return (u.rect[0] - f * u.w, u.rect[1] - f * u.h, u.rect[2] + f * u.w, u.rect[3] + f * u.h)
    for e in decor:
        r = (e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"])
        # a shadow/halo/disc: painted behind the picture, mostly around it, not vastly bigger
        def shadow_of(u):  # sits just under the picture, horizontally beneath it
            xo = min(r[2], u.rect[2]) - max(r[0], u.rect[0])
            return xo >= 0.5 * (r[2] - r[0]) and -0.1 * u.h <= r[1] - u.rect[3] <= 0.35 * u.h
        hosts = [u for u in pics if (inter(r, grown(u)) >= 0.25 * area(r) or inter(r, u.rect) >= 0.6 * area(u.rect)
                                     or shadow_of(u))
                 and e["z_index"] < min(x["z_index"] for x in u.elements) and area(r) < 8 * area(u.rect)]
        if not hosts:
            keep.append(e)
            continue
        h = max(hosts, key=lambda u: inter(r, u.rect) + inter(r, grown(u)))
        su = Unit(e["id"], "image", [e], r, role="decor")
        su.host = h.uid
        su.rel = (((r[0] + r[2]) / 2 - h.rect[0]) / h.w, ((r[1] + r[3]) / 2 - h.rect[1]) / h.h)
        units.append(su)
    return keep


def _attach_stickers(units):
    """A small group/image painted over a bigger picture, card or panel in the
    source (a 'SOLD OUT' badge on a card, a stamp on a product) is pinned to
    that host: it travels with it instead of being laid out on its own."""
    def zmin(u): return min(e["z_index"] for e in _flat(u))
    def zmax(u): return max(e["z_index"] for e in _flat(u))
    for u in units:
        if u.kind not in ("group", "image") or u.role in ("cta",) or u.host:
            continue
        hosts = [h for h in units if h is not u and not h.host and h.kind in ("image", "group", "container")
                 and area(h.rect) > 2 * area(u.rect) and inter(u.rect, h.rect) >= 0.2 * area(u.rect)
                 and zmin(u) > zmax(h)]
        if hosts:
            h = min(hosts, key=lambda h: area(h.rect))
            u.host = h.uid
            cx, cy = (u.rect[0] + u.rect[2]) / 2, (u.rect[1] + u.rect[3]) / 2
            u.rel = ((cx - h.rect[0]) / h.w, (cy - h.rect[1]) / h.h)


def _mark_corners(units, W, H):
    """A ribbon/stamp hugging (or bleeding off) a canvas corner stays anchored to
    that corner in every format instead of floating in the flow."""
    tol = 0.1
    badges = [u for u in units if u.role == "badge" and not u.host]
    for u in units:
        lone_badge = u.role == "badge" and len(badges) == 1 and area(u.rect) < 0.05 * W * H   # a "NEW" pill in a corner
        if (u.role != "ribbon" and not lone_badge) or u.host:
            continue
        x0, y0, x1, y1 = u.rect
        v = "t" if y0 < tol * H else ("b" if y1 > (1 - tol) * H else None)
        h = "l" if x0 < tol * W else ("r" if x1 > (1 - tol) * W else None)
        if v and h:
            u.corner = v + h


def _flat(u):
    return u.elements + [e for k in u.children for e in _flat(k)]


BAND_MAX = 0.35   # a band is thin: at most this share of the source canvas on its thin axis


def band_axis(u: Unit, W: float, H: float) -> str | None:
    """'h' for a header/footer band (spans left to right, thin), 'v' for a side band (spans top to
    bottom, thin), None otherwise. A half-page side panel (49% of the width) is not a band: in a portrait
    target it becomes a top block and must not be forced thin (l3_linkedin_banner)."""
    if u.kind != "container":
        return None
    edges = set(u.edges)
    thin_h = (u.rect[3] - u.rect[1]) <= BAND_MAX * H
    thin_v = (u.rect[2] - u.rect[0]) <= BAND_MAX * W
    if {"l", "r"} <= edges and thin_h:
        return "h"
    if {"t", "b"} <= edges and thin_v:
        return "v"
    return None


def _single(e, vis) -> Unit:
    kind = "line" if e["type"] == "shape" and e.get("shape_type") == "line" else e["type"]
    if kind == "shape":
        kind = "image"   # an opaque free shape behaves like a picture block
    return Unit(e["id"], kind, [e], vis[e["id"]])


def _descendants(u):
    return [d for k in u.children for d in [k] + _descendants(k)]


ACTION_VERBS = {"shop", "buy", "get", "order", "discover", "find", "reserve", "learn", "explore", "try", "book",
                "join", "sign", "start", "see", "download", "subscribe", "claim", "grab", "taste", "apply",
                "register", "watch", "view", "visit", "call", "contact", "save", "pre-order", "preorder"}


def _is_logo_like(e, W, H):
    r = measure.image_visible_rect(e)
    return (r[2] - r[0]) * (r[3] - r[1]) < 0.06 * W * H and (r[1] < 0.25 * H or r[3] > 0.8 * H)


def _assign_roles(units, W, H):
    texts = [u for u in units if u.kind == "text"]
    images = [u for u in units if u.kind == "image" and u.elements[0]["type"] == "image"]
    for u in units:
        if u.kind == "line":
            u.role = "line"
        elif u.kind == "image" and u.elements[0]["type"] == "shape":
            u.role = "panel"
    for u in images:
        if measure.asset_alpha_bbox(u.elements[0].get("asset_id", "")) is None:
            u.role = "missing"
    present = sorted([u for u in images if u.role != "missing"], key=lambda u: -area(u.rect))
    # a missing asset is shrunk to a speck in finalize (resize.MISSING_PX), so it never leads the layout
    if present:
        hero = present[0]
        hero.role = "hero"
        for u in present[1:]:
            u.role = "image"
        # logo: a wide-ish or small image near an edge, clearly smaller than the hero
        for u in present:
            near_edge = u.rect[1] < 0.25 * H or u.rect[3] > 0.8 * H or u.rect[0] < 0.2 * W or u.rect[2] > 0.8 * W
            small = area(u.rect) < 0.06 * W * H
            wide = u.w / u.h > 1.6
            same_as_hero = u is not hero and u.elements[0].get("asset_id") == hero.elements[0].get("asset_id")
            if (u is not hero or len(present) == 1) and near_edge and (small or wide) and not same_as_hero \
                    and (u is not hero or area(u.rect) < 0.04 * W * H):
                u.role = "logo"
                break
        if hero.role == "logo" and len(present) > 1:
            present[1].role = "hero"
    buttons = [u for u in units if u.kind in ("group", "container") and u.texts]
    for u in buttons:
        rotated = any(e.get("rotation") for e in u.elements if e["type"] != "image")
        u.role = "ribbon" if rotated else ("panel" if len(u.texts) > 1 else "button")
    btn = [u for u in buttons if u.role == "button"]
    if btn:
        # CTA: the button with the largest label font, ties -> larger area, then lower on canvas
        def fill_alpha(u):  # a solid button outranks a translucent secondary one
            return max((alpha(e) for e in _flat(u) if e["type"] == "shape"), default=1.0)
        for u in btn:       # round stamps ("NEW") are badges, never the call to action
            if any(e.get("shape_type") == "ellipse" for e in u.elements if e["type"] == "shape"):
                u.role = "badge"
        btn = [u for u in btn if u.role == "button"]
        ranked = sorted(btn, key=lambda u: (fill_alpha(u) >= 0.9, round(u.max_font), area(u.rect), u.rect[3]),
                        reverse=True)
        if ranked:      # all buttons may have been round stamps (now badges)
            cta = ranked[0]
            distinct = len(ranked) < 3 or (cta.max_font >= 1.1 * ranked[1].max_font
                                           or area(cta.rect) >= 1.4 * area(ranked[1].rect))
            for u in btn:   # a row of look-alike chips has no single call to action
                u.role = "cta" if (u is cta and distinct) else "badge"
    for u in units:
        if u.kind in ("group", "container") and not u.texts and u.role == "other":
            kids = [e for e in _flat(u) if e["type"] == "image"]
            logo_only = len(kids) == 1 and any(x.role == "logo" and x.elements[0] is kids[0] for x in units)
            u.role = "logo" if logo_only or (len(kids) == 1 and _is_logo_like(kids[0], W, H)) else "image"
    if texts:
        top = max(t.max_font for t in texts)
        ranked = sorted(texts, key=lambda u: (-u.max_font, -area(u.rect)))
        ranked[0].role = "headline"
        head = ranked[0]
        has_cta = any(u.role == "cta" for u in units)
        for u in ranked[1:]:
            fs, n = u.max_font, len(str(u.elements[0].get("content", "")))
            words = str(u.elements[0].get("content", "")).strip().split()
            gap = max(u.rect[1] - head.rect[3], head.rect[1] - u.rect[3])
            if not has_cta and 0 < len(words) <= 5 and words[0].lower().strip("→>") in ACTION_VERBS:
                u.role, has_cta = "cta", True      # a text-only call to action ("Shop now")
            elif gap < 1.2 * head.max_font and n <= 120 and fs >= 0.3 * top:
                u.role = "subhead"                 # the line hugging the headline
            elif (fs <= 0.4 * top and ((u.rect[3] > 0.87 * H and n > 25) or (u.rect[3] > 0.8 * H and n > 60))) \
                    or (fs <= 13 and n > 60):
                u.role = "legal"      # a 55-char line at 82% height with the CTA below it is a subhead (brief_poster)
            elif fs >= 0.55 * top and u.role == "other":
                u.role = "subhead" if fs < 0.85 * top else "headline2"
            else:
                u.role = "subhead" if n <= 90 and not any(x.role == "subhead" for x in ranked) else "body"
    for u in units:
        if u.role == "headline2":
            u.role = "subhead"


def _cuts(units, axis):
    """Whitespace gaps along an axis (0 = x, 1 = y): list of (gap_size, split_index)
    over units sorted by their start on that axis."""
    lo, hi = (0, 2) if axis == 0 else (1, 3)
    us = sorted(units, key=lambda u: u.rect[lo])
    out, end = [], us[0].rect[hi]
    for i, u in enumerate(us[1:], 1):
        if u.rect[lo] > end + 1.0:
            out.append((u.rect[lo] - end, i))
        end = max(end, u.rect[hi])
    return us, out


def xy_rows(units, W=1.0, H=1.0):
    """Reading order by recursive XY-cut (document-layout analysis): split at
    whitespace gaps, preferring the axis with the larger relative gap; bands read
    top-to-bottom, columns left-to-right. Columns of single units in the same
    band form one row (a badge row); taller column stacks are read in sequence."""
    if not units:
        return []
    if len(units) == 1:
        return [list(units)]
    ys, gy = _cuts(units, 1)
    xs, gx = _cuts(units, 0)
    by = max((g for g, _ in gy), default=0) / H
    bx = max((g for g, _ in gx), default=0) / W
    if not gy and not gx:
        return reading_rows(units)
    if by >= bx * 0.8:
        idx = [i for _, i in gy]
        parts = [ys[a:b] for a, b in zip([0] + idx, idx + [len(ys)])]
        return [r for p in parts for r in xy_rows(p, W, H)]
    idx = [i for _, i in gx]
    parts = [xs[a:b] for a, b in zip([0] + idx, idx + [len(xs)])]
    sub = [xy_rows(p, W, H) for p in parts]
    if all(len(rows) == 1 for rows in sub):
        return [[u for rows in sub for u in rows[0]]]
    return [r for rows in sub for r in rows]


def reading_rows(units):
    rows = []
    for u in sorted(units, key=lambda u: ((u.rect[1] + u.rect[3]) / 2, u.rect[0])):
        placed = False
        for row in rows:
            r = union(x.rect for x in row)
            ov = min(r[3], u.rect[3]) - max(r[1], u.rect[1])
            disjoint_x = all(min(x.rect[2], u.rect[2]) - max(x.rect[0], u.rect[0]) <= 2 for x in row)
            similar = max(u.h, r[3] - r[1]) < 2.5 * max(1e-6, min(u.h, r[3] - r[1]))
            if ov > ROW_OVERLAP * min(u.h, r[3] - r[1]) and disjoint_x and similar:
                row.append(u)
                placed = True
                break
        if not placed:
            rows.append([u])
    # a unit spanning two or more other rows (a product beside a text column)
    # is a side unit: give it its own row instead of gluing it to one text row
    changed = True
    while changed:
        changed = False
        for row in rows:
            if len(row) < 2:
                continue
            for u in row:
                spans = sum(1 for other in rows if other is not row and any(
                    min(u.rect[3], x.rect[3]) - max(u.rect[1], x.rect[1]) > 0.3 * x.h for x in other))
                if spans >= 2:
                    row.remove(u)
                    rows.append([u])
                    changed = True
                    break
            if changed:
                break
    for row in rows:
        row.sort(key=lambda u: u.rect[0])
    rows.sort(key=lambda row: (min(u.rect[1] for u in row) + max(u.rect[3] for u in row)) / 2)
    return rows


if __name__ == "__main__":
    import json, glob
    for f in sorted(glob.glob("input/*.json")):
        d = analyze(json.load(open(f))["source"])
        print(f.split("/")[-1], "bg", [e["id"] for e in d.background], "decor", [e["id"] for e in d.decor])
        for row in d.rows:
            print("   ", [(u.uid, u.role) for u in row])
        print("    missing", [u.uid for u in d.missing])
