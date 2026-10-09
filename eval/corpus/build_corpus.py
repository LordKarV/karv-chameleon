"""Generate eval/corpus/designs/*.json, previews, and a contact sheet.

Run from repo root:  .venv/bin/python eval/corpus/build_corpus.py

Text boxes are auto-sized with the renderer's own wrap (renderer.converter.wrap)
so source designs never overflow. Half the designs get opaque ids (el_01, g1...)
so engines can't lean on descriptive names.
"""
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import skia  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from renderer.converter.brand_fonts import load_brand_font_manager  # noqa: E402
from renderer.converter.convert import render_design_to_png  # noqa: E402
from renderer.converter.wrap import char_widths, wrap_text  # noqa: E402

CORPUS = Path(__file__).resolve().parent
DESIGNS = CORPUS / "designs"
PREVIEWS = CORPUS / "previews"
ASSETS = ROOT / "brand_kit" / "assets"
FM = load_brand_font_manager(str(ROOT / "brand_kit" / "fonts"))

SERIF, SANS = "Solstice Serif", "Solstice Sans"
WHITE = "rgba(255,255,255,1)"
CREAM = "rgba(250,243,232,1)"
SAND = "rgba(242,230,214,1)"
ORANGE = "rgba(232,106,58,1)"
PLUM = "rgba(43,27,46,1)"
GOLD = "rgba(240,178,66,1)"
RED = "rgba(220,60,50,1)"
TEAL = "rgba(28,84,92,1)"
INK = "rgba(33,24,36,1)"
BG, BOTTLE, LOGO = "solstice_bg_gradient.png", "solstice_bottle.png", "solstice_logo_lockup.png"

LINT: list = []


def rgba(c: str, a: float) -> str:
    return c.rsplit(",", 1)[0] + f",{a})"


def _style(weight: str, italic: bool) -> str:
    if weight == "bold":
        return "bolditalic" if italic else "bold"
    return "italic" if italic else "regular"


def measure(content, family, fs, weight="regular", italic=False, width=1e9):
    """-> (n_lines, widest_line_px) exactly as the renderer will wrap it."""
    widths = char_widths(FM, family, weight, _style(weight, italic), fs, content)
    lines, start = [], 0
    for line in wrap_text(content, widths, width).split("\n"):
        lines.append(sum(widths[start:start + len(line)]))
        start += len(line) + 1
    return len(lines), max(lines)


def T(id, x, y, w, content, fs, color, family=SANS, weight="regular", align="left",
      lh=1.2, italic=False, h=None, **extra):
    n, widest = measure(content, family, fs, weight, italic, w)
    widest += extra.get("letter_spacing", 0) * len(content)
    need = (n - 1) * fs * lh + 1.4 * fs
    if widest > w + 0.5:
        LINT.append(f"{id}: line {widest:.0f}px wider than box {w}")
    if h is not None and h < need - 1:
        LINT.append(f"{id}: box h {h} < needed {need:.0f} ({n} lines)")
    el = {"id": id, "type": "text", "x": x, "y": y, "width": w, "height": h or math.ceil(need),
          "content": content, "font_family": family, "font_size": fs, "color": color,
          "text_align": align}
    if weight != "regular":
        el["font_weight"] = weight
    if italic:
        el["font_style"] = "italic"
    if lh != 1.2:
        el["line_height"] = lh
    el.update(extra)
    return el


def S(id, x, y, w, h, fill, shape="rectangle", **extra):
    return {"id": id, "type": "shape", "x": x, "y": y, "width": w, "height": h,
            "shape_type": shape, "fill_color": fill, **extra}


def I(id, x, y, w, h, asset, fit="contain", **extra):
    return {"id": id, "type": "image", "x": x, "y": y, "width": w, "height": h,
            "asset_id": asset, "fit": fit, **extra}


def line(id, x, y, w, color, thickness=4, **extra):
    """Divider: the renderer strokes a horizontal line through the box's vertical middle."""
    return S(id, x, y, w, thickness * 3, color, "line", stroke_color=color,
             stroke_width=thickness, **extra)


def btn(gid, x, y, w, h, label, fill, fs, color=WHITE, radius=None, family=SANS,
        weight="bold", **extra):
    """Pill/rect + centered label sharing a group_id. Label box is vertically
    positioned so the cap-height centre sits on the shape's centre."""
    r = h / 2 if radius is None else radius
    bg = S(f"{gid}_bg", x, y, w, h, fill, corner_radius=r, group_id=gid, **extra)
    cap_mid = 0.7 if family == SANS else 0.65
    lbl = T(f"{gid}_label", x, round(y + h / 2 - cap_mid * fs), w, label, fs, color, family,
            weight, "center", group_id=gid, **extra)
    return [bg, lbl]


def below(el, gap):
    return el["y"] + el["height"] + gap


def bg_image(cw, ch, **extra):
    return I("bg", 0, 0, cw, ch, BG, "cover", **extra)


# ---------------------------------------------------------------------------
# Designs: each returns (canvas_w, canvas_h, elements). Paint order = list order.
# ---------------------------------------------------------------------------

def d_hero_portrait():
    cw, ch = 1080, 1350
    h = T("headline", 90, 830, 900, "Golden Hour, Bottled", 76, WHITE, SERIF, "bold", "center")
    s = T("subhead", 160, below(h, 24), 760, "Sparkling botanicals for long summer evenings.", 32,
          rgba(CREAM, 0.85), align="center")
    c = T("cta", 240, below(s, 40), 600, "SHOP THE SUMMER LINE", 28, GOLD, weight="bold",
          align="center", letter_spacing=3)
    return cw, ch, [bg_image(cw, ch), I("logo", 390, 70, 300, 100, LOGO),
                    I("product", 270, 220, 540, 560, BOTTLE), h, s, c]


def d_minimal_pair():
    cw, ch = 1080, 1080
    return cw, ch, [bg_image(cw, ch),
                    T("headline", 120, 440, 840, "Less noise. More light.", 96, WHITE, SERIF,
                      "bold", "center")]


def d_fb_link():
    cw, ch = 1200, 628
    h = T("headline", 80, 180, 560, "Taste the Longest Day of the Year", 58, WHITE, SERIF, "bold")
    s = T("subhead", 80, below(h, 22), 520, "Four limited flavours, one golden evening.", 26,
          rgba(WHITE, 0.85))
    c = T("cta", 80, below(s, 30), 400, "Shop now", 28, GOLD, weight="bold")
    return cw, ch, [bg_image(cw, ch), I("logo", 10, 50, 240, 80, LOGO), h, s, c,
                    I("product", 660, 34, 540, 560, BOTTLE)]


def d_text_only_solid():
    cw, ch = 1080, 1080
    e = T("eyebrow", 140, 330, 800, "SOLSTICE BOTANICALS", 28, GOLD, weight="bold",
          align="center", letter_spacing=8)
    h = T("headline", 120, below(e, 50), 840, "Summer is a state of mind.", 92, CREAM, SERIF,
          "regular", "center", italic=True, lh=1.1)
    return cw, ch, [S("bg", 0, 0, cw, ch, PLUM), e, line("rule", 470, below(h, 40), 140, GOLD, 4), h]


def d_light_story():
    cw, ch = 1080, 1920
    h = T("headline", 90, 1080, 900, "Light, Bright & Botanical", 92, INK, SERIF, "bold", "center")
    s = T("subhead", 140, below(h, 30), 800,
          "Crisp sparkling infusions with real fruit and nothing artificial.", 36,
          rgba(INK, 0.75), align="center")
    rule = line("divider", 440, below(s, 40), 200, ORANGE, 4)
    b = btn("cta", 320, below(rule, 50), 440, 110, "Find Your Flavor", ORANGE, 34)
    foot = T("footnote", 190, below(b[0], 40), 700, "Available at select retailers nationwide",
             26, rgba(INK, 0.6), align="center")
    return cw, ch, [S("bg", 0, 0, cw, ch, CREAM),
                    S("halo", 140, 260, 800, 800, rgba(ORANGE, 0.18), "ellipse"),
                    I("product", 190, 300, 700, 740, BOTTLE),
                    S("logo_plate", 380, 90, 320, 100, PLUM, corner_radius=50, group_id="logo"),
                    I("logo", 400, 95, 280, 90, LOGO, group_id="logo"),
                    h, s, rule, *b, foot]


def d_editorial_wide():
    cw, ch = 1920, 1080
    e = T("eyebrow", 140, 220, 700, "NEW SEASON  /  2026", 26, GOLD, weight="bold",
          letter_spacing=5)
    h = T("headline", 140, below(e, 20), 900, "Chase the Light", 132, WHITE, SERIF, "bold",
          style_runs=[{"start": 10, "end": 15, "color": GOLD}])
    r = line("divider", 140, below(h, 10), 160, GOLD, 5)
    s = T("subhead", 140, below(r, 30), 760,
          "Botanical sparkling infusions with blood orange, wild honey and smoked rosemary. "
          "Crafted for golden hour.", 34, rgba(WHITE, 0.85), lh=1.35)
    b = btn("cta", 140, below(s, 50), 400, 100, "Shop the Collection", ORANGE, 30)
    return cw, ch, [bg_image(cw, ch),
                    S("deco_ring", 1300, -260, 900, 900, rgba(WHITE, 0.10), "ellipse"),
                    S("deco_dot", -200, 760, 520, 520, rgba(GOLD, 0.14), "ellipse"),
                    I("product", 1160, 120, 760, 840, BOTTLE), e, h, r, s, *b,
                    I("logo", 70, 60, 270, 90, LOGO)]


def d_split_panels():
    cw, ch = 1600, 900
    h = T("headline", 100, 230, 620, "Two Halves, One Golden Hour", 84, CREAM, SERIF, "bold", lh=1.1)
    s = T("subhead", 100, below(h, 28), 560, "Citrus up front, honey on the finish.", 30,
          rgba(CREAM, 0.8))
    b = btn("cta", 100, below(s, 50), 340, 90, "Discover More", ORANGE, 28)
    link = T("link", 470, b[1]["y"], 260, "Find a store", 28, GOLD, weight="bold")
    stamp = [S("stamp_bg", 690, 640, 200, 200, GOLD, "ellipse", group_id="stamp"),
             T("stamp_label", 690, 712, 200, "NEW", 44, PLUM, SERIF, "bold", "center",
               group_id="stamp")]
    return cw, ch, [S("panel_left", 0, 0, 800, 900, PLUM), S("panel_right", 800, 0, 800, 900, ORANGE),
                    S("glow", 900, 150, 600, 600, rgba(GOLD, 0.35), "ellipse"),
                    I("product", 850, 80, 700, 740, BOTTLE), I("logo", 40, 70, 270, 90, LOGO),
                    h, s, *b, link, *stamp]


def d_underlay_portrait():
    cw, ch = 1080, 1350
    panel = S("panel", 60, 700, 960, 590, "rgba(20,12,24,0.55)", corner_radius=36)
    h = T("headline", 120, 760, 840, "Meet the Golden Hour Spritz", 76, WHITE, SERIF, "bold",
          "center", lh=1.1, style_runs=[{"start": 9, "end": 20, "color": GOLD}])
    s = T("subhead", 160, below(h, 24), 760,
          "Blood orange, wild honey and a whisper of rosemary over fine bubbles.", 30,
          rgba(WHITE, 0.85), align="center", lh=1.3)
    b = btn("cta", 330, below(s, 40), 420, 96, "Shop the Summer Line", ORANGE, 28)
    legal = T("legal", 120, below(b[0], 30), 840, "Non-alcoholic. 330 ml. Serve chilled.", 20,
              rgba(WHITE, 0.6), align="center")
    tag = btn("tag", 840, 60, 190, 56, "NEW", RED, 22)
    return cw, ch, [bg_image(cw, ch), I("logo", 40, 50, 270, 90, LOGO), *tag,
                    I("product", 240, 120, 600, 620, BOTTLE), panel, h, s, *b, legal]


def d_trio_products():
    cw, ch = 1080, 1080
    h = T("headline", 90, 60, 900, "The Summer Trio", 84, WHITE, SERIF, "bold", "center")
    s = T("subhead", 90, below(h, 6), 900, "Blood Orange  |  Wild Honey  |  Smoked Rosemary", 28,
          GOLD, align="center")
    save = [S("save_bg", 820, 250, 190, 190, RED, "ellipse", rotation=-12, group_id="save"),
            T("save_label", 820, 296, 190, "SAVE 20%", 34, WHITE, SANS, "bold", "center",
              lh=1.0, rotation=-12, group_id="save"),
            T("save_sub", 820, 342, 190, "this week", 22, WHITE, SANS, "regular", "center",
              rotation=-12, group_id="save")]
    return cw, ch, [bg_image(cw, ch),
                    S("shadow", 160, 840, 760, 90, "rgba(0,0,0,0.35)", "ellipse"),
                    I("product_left", 70, 380, 420, 480, BOTTLE, opacity=0.9),
                    I("product_right", 590, 380, 420, 480, BOTTLE, opacity=0.9),
                    I("product_center", 270, 250, 540, 640, BOTTLE), h, s, *save,
                    *btn("cta", 340, 950, 400, 90, "Get the Trio", ORANGE, 30)]


def d_story_two_ctas():
    cw, ch = 1080, 1920
    h = T("headline", 90, 290, 900, "Your Evening, Upgraded", 100, WHITE, SERIF, "bold", "center",
          lh=1.05)
    s = T("subhead", 140, 1330, 800, "Sparkling botanicals, crafted in small batches.", 34,
          rgba(WHITE, 0.85), align="center")
    b1 = btn("cta_primary", 240, below(s, 50), 600, 110, "Shop the Collection", ORANGE, 34)
    b2 = btn("cta_secondary", 240, below(b1[0], 30), 600, 110, "Find a Store", rgba(WHITE, 0.18), 34)
    return cw, ch, [bg_image(cw, ch),
                    S("deco_a", -300, -200, 900, 900, rgba(WHITE, 0.08), "ellipse"),
                    S("deco_b", 600, 1400, 800, 800, rgba(ORANGE, 0.15), "ellipse"),
                    I("logo", 390, 110, 300, 100, LOGO), h,
                    I("product", 190, below(h, 20), 700, 720, BOTTLE), s, *b1, *b2,
                    T("swipe", 340, below(b2[0], 40), 400, "Swipe up to shop", 24, rgba(WHITE, 0.6),
                      align="center")]


def d_light_badges5():
    cw, ch = 1080, 1080
    labels = ["VEGAN", "ORGANIC", "ZERO SUGAR", "LOCAL", "NEW"]
    fs, pad, gap, bh = 20, 26, 14, 52
    widths = [round(measure(t, SANS, fs, "bold")[1] + 2 * pad) for t in labels]
    x = (cw - sum(widths) - gap * (len(labels) - 1)) / 2
    badges = []
    for i, (t, w) in enumerate(zip(labels, widths)):
        badges += btn(f"badge{i + 1}", round(x), 810, w, bh, t, TEAL if i % 2 else ORANGE, fs)
        x += w + gap
    h = T("headline", 90, 690, 900, "Five Reasons to Love It", 64, INK, SERIF, "bold", "center")
    return cw, ch, [S("bg", 0, 0, cw, ch, SAND), S("band", 0, 0, cw, 140, PLUM),
                    I("logo", 400, 22, 280, 96, LOGO),
                    S("disc", 320, 190, 440, 440, rgba(ORANGE, 0.9), "ellipse"),
                    I("product", 280, 160, 520, 520, BOTTLE), h, *badges,
                    T("legal", 140, 920, 800, "Nutrition facts per 330 ml serving. Contains naturally "
                      "occurring sugars from fruit.", 20, rgba(INK, 0.6), align="center")]


def d_right_aligned():
    cw, ch = 1600, 900
    x, w = 760, 780
    h = T("headline", x, 210, w, "Made for the Longest Day", 92, WHITE, SERIF, "bold", "right", lh=1.05)
    r = line("divider", 1380, below(h, 20), 160, GOLD, 5)
    s = T("subhead", 860, below(r, 26), 680,
          "Sun-ripened citrus and garden herbs, sparkling and ice-cold.", 32, rgba(WHITE, 0.85),
          align="right", lh=1.3)
    b = btn("cta", 1540 - 360, below(s, 44), 360, 96, "Order Online", ORANGE, 30)
    return cw, ch, [bg_image(cw, ch), I("product", 40, 60, 760, 780, BOTTLE),
                    I("logo", 1290, 50, 270, 90, LOGO), h, r, s, *b]


def d_poster_a4():
    cw, ch = 2480, 3508
    e = T("eyebrow", 240, 470, 2000, "SOLSTICE PRESENTS", 64, GOLD, weight="bold", align="center",
          letter_spacing=14)
    h = T("headline", 140, below(e, 30), 2200, "Midsummer Night Market", 250, WHITE, SERIF, "bold",
          "center", lh=1.0)
    r = line("divider", 1040, below(h, 50), 400, GOLD, 8)
    d = T("date", 240, below(r, 50), 2000, "Saturday, June 20  |  6 PM until late", 84, WHITE,
          align="center")
    v = T("venue", 240, below(d, 10), 2000, "Harbourfront Gardens, Pier 7", 64, rgba(WHITE, 0.8),
          align="center")
    p = I("product", 640, below(v, 40), 1200, 1300, BOTTLE)
    b = btn("cta", 790, below(p, 30), 900, 200, "Get Free Tickets", ORANGE, 76)
    legal = T("legal", 240, 3280, 2000, "Free entry with registration. Under-18s must be accompanied "
              "by an adult. Programme subject to change; see solstice.com/market for updates.", 40,
              rgba(WHITE, 0.6), align="center")
    return cw, ch, [bg_image(cw, ch), S("deco", -600, 2300, 2000, 2000, rgba(WHITE, 0.07), "ellipse"),
                    I("logo", 940, 150, 600, 200, LOGO), e, h, r, d, v, p, *b, legal]


def d_thumbnail():
    cw, ch = 1280, 720
    h = T("headline", 60, 150, 600, "5 Mocktails in 5 Minutes", 96, WHITE, SERIF, "bold", lh=1.0,
          style_runs=[{"start": 15, "end": 24, "color": GOLD}])
    tag = [S("tag_bg", 60, 390, 240, 84, RED, rotation=-6, group_id="tag"),
           T("tag_label", 60, 415, 240, "EASY!", 36, WHITE, SANS, "bold", "center", rotation=-6,
             group_id="tag")]
    return cw, ch, [bg_image(cw, ch), S("panel", 0, 0, 700, 720, rgba(PLUM, 0.7)),
                    S("blob_a", 520, -80, 500, 500, rgba(ORANGE, 0.35), "ellipse"),
                    S("blob_b", 880, 360, 480, 480, rgba(GOLD, 0.25), "ellipse"),
                    I("product", 700, 40, 580, 640, BOTTLE), h, *tag]


def d_longcopy_story():
    cw, ch = 1080, 1920
    h = T("headline", 70, 800, 940,
          "Introducing the All-New Limited-Batch Solstice Botanical Sparkling Collection, Crafted "
          "Exclusively for Golden Hour Gatherings With Friends", 58, WHITE, SERIF, "bold", "center",
          lh=1.15)
    s = T("subhead", 100, below(h, 30), 880,
          "Small-batch infusions blended with wild honey, blood orange zest and a whisper of smoked "
          "rosemary, bottled at the peak of golden hour for a taste that lingers long after sunset.",
          30, rgba(WHITE, 0.85), align="center", lh=1.3)
    labels = [("LIMITED EDITION", ORANGE), ("AWARD WINNING 2026", PLUM), ("SMALL BATCH", ORANGE)]
    fs, ws = 20, [round(measure(t, SANS, 20, "bold")[1] + 56) for t, _ in labels]
    x, badges = (cw - sum(ws) - 40) / 2, []
    for i, ((t, c), w) in enumerate(zip(labels, ws)):
        badges += btn(f"badge{i + 1}", round(x), below(s, 40), w, 56, t, c, fs)
        x += w + 20
    b = btn("cta", 290, below(badges[0], 40), 500, 104, "Reserve Yours", ORANGE, 32)
    legal = T("legal", 80, 1790, 920,
              "Please enjoy responsibly. Limited quantities; while supplies last. Offer valid in "
              "participating stores only. See solstice.com/terms for full details and exclusions.",
              18, rgba(WHITE, 0.6), align="center")
    ribbon = [S("ribbon_bg", 720, 110, 480, 72, RED, rotation=40, group_id="ribbon"),
              T("ribbon_label", 720, 128, 480, "LIMITED RUN", 30, WHITE, SANS, "bold", "center",
                rotation=40, group_id="ribbon")]
    stamp = [S("stamp_bg", 770, 520, 230, 230, GOLD, "ellipse", rotation=-15, group_id="stamp"),
             T("stamp_label", 790, 584, 190, "BEST OF 2026", 34, PLUM, SERIF, "bold", "center",
               lh=1.0, rotation=-15, group_id="stamp")]
    return cw, ch, [bg_image(cw, ch), I("logo", 70, 60, 300, 100, LOGO), *ribbon,
                    I("product", 290, 220, 500, 560, BOTTLE), *stamp, h, s, *badges, *b,
                    I("retailers", 140, 1660, 800, 100, "solstice_retailer_strip.png"), legal]


def d_long_word():
    cw, ch = 1080, 1080
    hw = T("hashtag", 60, 380, 960, "#SolsticeSummerSpritzSeason", 58, WHITE, SANS, "bold", "center")
    sub = T("subhead", 140, below(hw, 20), 800, "Tag your golden-hour moments for a chance to win.", 32,
            rgba(WHITE, 0.85), align="center")
    labels = ["WIN", "SHARE", "TAG", "REPEAT"]
    fs, ws = 22, [round(measure(t, SANS, 22, "bold")[1] + 60) for t in labels]
    x, badges = (cw - sum(ws) - 3 * 16) / 2, []
    for i, (t, w) in enumerate(zip(labels, ws)):
        badges += btn(f"chip{i + 1}", round(x), below(sub, 50), w, 58, t, GOLD if i == 3 else ORANGE,
                      fs, PLUM if i == 3 else WHITE)
        x += w + 16
    url = T("url", 60, 930, 960, "solstice.com/sparklingbotanicalsummercollection2026", 28, GOLD,
            align="center")
    ribbon = [S("ribbon_bg", -90, 70, 420, 70, RED, rotation=-45, group_id="ribbon"),
              T("ribbon_label", -90, 85, 420, "CONTEST", 32, WHITE, SANS, "bold", "center",
                rotation=-45, group_id="ribbon")]
    return cw, ch, [bg_image(cw, ch), S("tint", 0, 0, cw, ch, rgba(PLUM, 0.4)), *ribbon,
                    I("logo", 390, 70, 300, 100, LOGO),
                    I("product", 400, 170, 280, 220, BOTTLE), hw, sub, *badges, url]


def d_linkedin_banner():
    cw, ch = 1584, 396
    h = T("headline", 60, 120, 680, "Now Hiring: Golden Hour Makers", 50, WHITE, SERIF, "bold")
    s = T("subhead", 60, below(h, 12), 640, "Join our small-batch team in Portland, OR.", 24,
          rgba(WHITE, 0.85))
    return cw, ch, [bg_image(cw, ch), S("panel", 0, 0, 780, 396, rgba(PLUM, 0.6)),
                    I("logo", 10, 24, 240, 80, LOGO), h, s,
                    *btn("cta", 840, 130, 290, 80, "See Open Roles", ORANGE, 26),
                    *btn("chip", 840, 236, 190, 44, "REMOTE OK", TEAL, 16),
                    I("product", 1170, 8, 380, 380, BOTTLE),
                    T("legal", 840, 352, 400, "Solstice Botanicals is an equal opportunity employer.",
                      14, rgba(WHITE, 0.6))]


def d_tall_pin():
    cw, ch = 1000, 2100
    h = T("headline", 80, 190, 840, "Three Spritzes for Midsummer", 84, CREAM, SERIF, "bold",
          "center", lh=1.05)
    cards = []
    recipes = [("The Sunset", "Blood orange, soda and a twist of thyme."),
               ("The Meadow", "Wild honey, lemon and fresh mint leaves."),
               ("The Ember", "Smoked rosemary, grapefruit and sea salt.")]
    for i, (title, desc) in enumerate(recipes):
        y, g = 560 + i * 450, f"card{i + 1}"
        cards += [S(f"{g}_bg", 60, y, 880, 420, WHITE, corner_radius=28, group_id=g),
                  I(f"{g}_img", 80, y + 20, 340, 380, BOTTLE, group_id=g,
                    **({"rotation": 8} if i == 1 else {})),
                  T(f"{g}_title", 440, y + 120, 460, title, 52, INK, SERIF, "bold", group_id=g)]
        cards.append(T(f"{g}_desc", 440, below(cards[-1], 14), 440, desc, 28, rgba(INK, 0.7),
                       lh=1.3, group_id=g))
    b = btn("cta", 300, 1930, 400, 96, "Get the Recipes", ORANGE, 30)
    return cw, ch, [S("bg", 0, 0, cw, ch, CREAM), S("band", 0, 0, cw, 520, PLUM),
                    I("logo", 360, 50, 280, 94, LOGO), h,
                    T("kicker", 200, 420, 600, "RECIPES  /  SERVES 2", 24, GOLD, weight="bold",
                      align="center", letter_spacing=4), *cards, *b]


def d_catalog_grid():
    cw, ch = 1080, 1350
    h = T("headline", 60, 70, 960, "The Summer Shelf", 84, INK, SERIF, "bold", "center")
    s = T("subhead", 140, below(h, 6), 800, "Four seasonal flavours, back for a limited time.", 30,
          rgba(INK, 0.7), align="center")
    cards = []
    items = [("Blood Orange", "$4.99"), ("Wild Honey", "$4.99"), ("Smoked Rosemary", "$5.49"),
             ("Midnight Plum", "$5.49")]
    for i, (name, price) in enumerate(items):
        x, y, g = 50 + (i % 2) * 510, 290 + (i // 2) * 480, f"item{i + 1}"
        cards += [S(f"{g}_bg", x, y, 470, 450, WHITE, corner_radius=24, group_id=g),
                  I(f"{g}_img", x + 85, y + 10, 300, 300, BOTTLE, group_id=g,
                    **({"rotation": -8} if i == 2 else {})),
                  T(f"{g}_name", x + 20, y + 320, 430, name, 36, INK, SERIF, "bold", "center",
                    group_id=g),
                  T(f"{g}_price", x + 20, y + 378, 430, price, 30, ORANGE, SANS, "bold", "center",
                    group_id=g)]
    sold = btn("soldout", 50 + 510 + 300, 290 + 480 + 20, 150, 44, "SOLD OUT", RED, 16, rotation=12)
    legal = T("legal", 60, 1260, 960, "Prices in USD, excluding tax and deposit. Flavours rotate "
              "seasonally and may vary by region. Images for illustration only.", 18,
              rgba(INK, 0.55), align="center")
    return cw, ch, [S("bg", 0, 0, cw, ch, SAND), h, s, *cards, *sold, legal]


def d_collage_rotated():
    cw, ch = 1080, 1080
    h = T("headline", 60, 770, 960, "Shake. Pour. Glow.", 104, WHITE, SERIF, "bold", "center",
          style_runs=[{"start": 7, "end": 12, "color": GOLD, "font_style": "italic"},
                      {"start": 13, "end": 18, "color": ORANGE}])
    return cw, ch, [bg_image(cw, ch),
                    S("card_a", 140, 120, 520, 640, rgba(WHITE, 0.12), corner_radius=20, rotation=-6),
                    S("card_b", 420, 140, 520, 640, rgba(ORANGE, 0.25), corner_radius=20, rotation=5),
                    I("bottle_l", 130, 220, 460, 520, BOTTLE, rotation=-14),
                    I("bottle_r", 490, 220, 460, 520, BOTTLE, rotation=14),
                    I("bottle_c", 300, 160, 480, 580, BOTTLE), h,
                    T("est", -110, 520, 320, "EST. 2019", 26, rgba(WHITE, 0.7), SANS, "bold", "center",
                      rotation=-90, letter_spacing=6),
                    *btn("cta", 390, 930, 300, 84, "Mix Yours", ORANGE, 28)]


def d_missing_hero():
    cw, ch = 1920, 1080
    h = T("headline", 120, 300, 900, "The Can Has Landed", 120, WHITE, SERIF, "bold", lh=1.05)
    s = T("subhead", 120, below(h, 30), 800, "All your favourite Solstice flavours, now in a 12 oz "
          "recyclable can.", 36, rgba(WHITE, 0.85), lh=1.3)
    b = btn("cta", 120, below(s, 50), 380, 100, "Find Cans Near You", ORANGE, 30)
    return cw, ch, [bg_image(cw, ch), I("logo", 60, 60, 300, 100, LOGO),
                    *btn("badge1", 120, 220, 220, 52, "JUST LAUNCHED", RED, 20), h, s, *b,
                    I("hero_can", 1150, 100, 640, 880, "solstice_can_12oz.png"),
                    T("legal", 120, 1010, 1000, "12 oz = 355 ml. Recycle where facilities exist.",
                      20, rgba(WHITE, 0.55))]


# name, level, opaque_ids, builder
NEW = [
    ("l1_hero_portrait", 1, False, d_hero_portrait),
    ("l1_minimal_pair", 1, True, d_minimal_pair),
    ("l1_fb_link_landscape", 1, True, d_fb_link),
    ("l1_text_only_solid", 1, False, d_text_only_solid),
    ("l2_light_story", 2, True, d_light_story),
    ("l2_editorial_wide", 2, False, d_editorial_wide),
    ("l2_split_panels", 2, True, d_split_panels),
    ("l2_underlay_portrait", 2, False, d_underlay_portrait),
    ("l2_trio_products", 2, True, d_trio_products),
    ("l2_story_two_ctas", 2, False, d_story_two_ctas),
    ("l2_light_badges5", 2, True, d_light_badges5),
    ("l2_right_aligned", 2, False, d_right_aligned),
    ("l2_poster_a4", 2, True, d_poster_a4),
    ("l2_thumbnail_overlaps", 2, False, d_thumbnail),
    ("l3_longcopy_story", 3, True, d_longcopy_story),
    ("l3_long_word_square", 3, False, d_long_word),
    ("l3_linkedin_banner", 3, True, d_linkedin_banner),
    ("l3_tall_pin_cards", 3, False, d_tall_pin),
    ("l3_catalog_grid", 3, True, d_catalog_grid),
    ("l3_collage_rotated", 3, False, d_collage_rotated),
    ("l3_missing_hero", 3, True, d_missing_hero),
]
SAMPLES = [("sample_l1_warmup", 1, "chameleon_sample_level1_warmup.json"),
           ("sample_l2_core", 2, "chameleon_sample_level2_core.json"),
           ("sample_l3_advanced", 3, "chameleon_sample_level3_advanced.json")]


def finalize(elements, opaque):
    out = []
    ids, groups = {}, {}
    for z, el in enumerate(elements):
        el = {k: v for k, v in el.items() if not k.startswith("_")}
        el.setdefault("z_index", z)
        for k in ("x", "y", "width", "height"):
            el[k] = round(el[k], 1) if isinstance(el[k], float) else el[k]
        if opaque:
            el["id"] = ids.setdefault(el["id"], f"el_{len(ids) + 1:02d}")
            if el.get("group_id"):
                el["group_id"] = groups.setdefault(el["group_id"], f"g{len(groups) + 1}")
        out.append(el)
    return out


def contact_sheet(names):
    cell, label = 360, 28
    cols = 6
    rows = math.ceil(len(names) / cols)
    sheet = Image.new("RGB", (cols * cell, rows * (cell + label)), (235, 235, 235))
    draw = ImageDraw.Draw(sheet)
    for i, n in enumerate(names):
        im = Image.open(PREVIEWS / f"{n}.png").convert("RGB")
        im.thumbnail((cell - 12, cell - 12))
        cx, cy = (i % cols) * cell, (i // cols) * (cell + label)
        sheet.paste(im, (cx + (cell - im.width) // 2, cy + (cell - im.height) // 2))
        draw.text((cx + 6, cy + cell + 4), n, fill=(0, 0, 0))
    sheet.save(PREVIEWS / "_sheet.png")


def main():
    DESIGNS.mkdir(parents=True, exist_ok=True)
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    docs = []
    for name, level, fname in SAMPLES:
        src = json.loads((ROOT / "input" / fname).read_text())["source"]
        docs.append((name, level, {"canvas": src["canvas"], "elements": finalize(src["elements"], False)}))
    for name, level, opaque, fn in NEW:
        cw, ch, els = fn()
        docs.append((name, level, {"canvas": {"width": cw, "height": ch},
                                   "elements": finalize(els, opaque)}))
    for name, level, src in docs:
        (DESIGNS / f"{name}.json").write_text(json.dumps(
            {"design_id": name, "level": level, "source": src}, indent=1) + "\n")
        warns = render_design_to_png(src, str(ASSETS), str(PREVIEWS / f"{name}.png"))
        print(f"{name:28s} L{level} {src['canvas']['width']}x{src['canvas']['height']} "
              f"{len(src['elements']):2d} els {'; '.join(warns)}")
    contact_sheet([d[0] for d in docs])
    print("\n".join(["LINT:"] + LINT) if LINT else "lint clean")


if __name__ == "__main__":
    main()
