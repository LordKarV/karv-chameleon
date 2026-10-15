"""Structural contract for every corpus design x every catalogue target, plus
degenerate inputs. Run: .venv/bin/python -m pytest -q tests"""
import copy
import glob
import json

import pytest

from engine.resize import resize

DESIGNS = sorted(glob.glob("eval/corpus/designs/*.json"))
TARGETS = json.load(open("eval/targets.json"))
TOL = 0.5


def check(source, out, W, H):
    assert out["canvas"] == {"width": W, "height": H}
    src = {e["id"]: e for e in source["elements"]}
    ids = [e["id"] for e in out["elements"]]
    assert sorted(ids) == sorted(src) and len(ids) == len(set(ids))
    for e in out["elements"]:
        s = src[e["id"]]
        for k in ("x", "y", "width", "height"):
            assert isinstance(e[k], (int, float)) and e[k] == e[k], (e["id"], k)
        assert e["x"] >= -TOL and e["y"] >= -TOL, e["id"]
        assert e["x"] + e["width"] <= W + TOL and e["y"] + e["height"] <= H + TOL, e["id"]
        for k, v in s.items():  # everything except geometry/font size is untouched
            if k not in ("x", "y", "width", "height", "font_size", "letter_spacing", "corner_radius"):
                assert e[k] == v, (e["id"], k)
        if e["type"] == "shape" and s.get("corner_radius") and s["width"] > 0 and s["height"] > 0:
            want = s["corner_radius"] * min(e["width"] / s["width"], e["height"] / s["height"])
            assert abs(e["corner_radius"] - want) <= 0.01 * want + 0.01, (e["id"], e["corner_radius"], want)
        if e["type"] == "text":
            assert e["font_size"] >= 1
        full = e["width"] >= W - 1 and e["height"] >= H - 1
        if e["type"] == "image" and s.get("fit") != "cover" and not full and s["width"] > 0 and s["height"] > 0 \
                and e["width"] > 0 and e["height"] > 0:
            a0, a1 = s["width"] / s["height"], e["width"] / e["height"]
            assert abs(a1 / a0 - 1) <= 0.05, (e["id"], a0, a1)


@pytest.mark.parametrize("path", DESIGNS)
def test_corpus_all_targets(path):
    source = json.load(open(path))["source"]
    before = copy.deepcopy(source)
    for t in TARGETS:
        out = resize(source, {"width": t["width"], "height": t["height"]})
        check(source, out, t["width"], t["height"])
    assert source == before  # input not mutated


def test_deterministic():
    source = json.load(open(DESIGNS[0]))["source"]
    assert resize(source, {"width": 728, "height": 90}) == resize(source, {"width": 728, "height": 90})


DEGENERATE = [
    [{"id": "a", "type": "text", "x": 0, "y": 0, "width": 0, "height": 0, "content": "", "font_family": "Solstice Sans",
      "font_size": 1, "color": "rgba(0,0,0,1)"}],
    [{"id": "a", "type": "image", "x": 50, "y": 50, "width": 0, "height": 300, "asset_id": "solstice_bottle.png"}],
    [{"id": "a", "type": "image", "x": -500, "y": -500, "width": 3000, "height": 900, "asset_id": "nope.png"},
     {"id": "b", "type": "shape", "x": 2000, "y": 2000, "width": 10, "height": 10, "shape_type": "line",
      "fill_color": "rgba(1,2,3,1)"}],
    [{"id": "t", "type": "text", "x": 10, "y": 10, "width": 50, "height": 20, "font_family": "Solstice Serif",
      "content": "Supercalifragilisticexpialidocious" * 3, "font_size": 200, "color": "rgba(0,0,0,1)",
      "rotation": 45}],
    [{"id": f"s{i}", "type": "shape", "x": 10 * i, "y": 10 * i, "width": 100, "height": 100, "shape_type": "ellipse",
      "fill_color": "rgba(9,9,9,0.5)", "group_id": "g"} for i in range(30)],
]


@pytest.mark.parametrize("elements", DEGENERATE)
@pytest.mark.parametrize("target", [(728, 90), (160, 600), (1080, 1080), (50, 50)])
def test_degenerate(elements, target):
    source = {"canvas": {"width": 1000, "height": 1000}, "elements": elements}
    out = resize(source, {"width": target[0], "height": target[1]})
    check(source, out, *target)


def test_candidates_place_every_element():
    """Every template candidate must place every element; a silent KeyError
    would drop good layouts and leave only the scale-fit fallback."""
    from engine.analyze import analyze
    from engine.energy import energy
    from engine.resize import candidates
    for path in DESIGNS:
        source = json.load(open(path))["source"]
        d = analyze(source)
        ids = {e["id"] for e in source["elements"]}
        for W, H in ((1080, 1920), (1584, 396), (300, 250)):
            for name, p in candidates(d, source, W, H):
                assert ids <= set(p), (path, name, sorted(ids - set(p)))
                energy(d, p, W, H)


JUNK = [
    {"canvas": {"width": 0, "height": 0}, "elements": [{"id": "t", "type": "text", "x": 1, "y": 1, "width": 50,
     "height": 20, "content": "Hi", "font_family": "Solstice Sans", "font_size": 20, "color": "rgba(0,0,0,1)"}]},
    {"canvas": {"width": 1000, "height": 1000}, "elements": [{"id": "t", "type": "text", "x": "10", "y": "abc",
     "width": "200", "height": None, "content": None, "font_family": "Nope", "font_size": "20", "color": "#zzz",
     "rotation": "7200", "opacity": 5}]},
    {"canvas": {"width": 1000, "height": 1000}, "elements": []},
    {"canvas": {"width": 1000, "height": 1000}, "elements": [{"id": "v", "type": "video", "x": 0, "y": 0,
     "width": 10, "height": 10}, {"id": "i", "type": "image", "x": float("nan"), "y": 0, "width": float("inf"),
     "height": 10, "asset_id": "../../../etc/passwd"}]},
]


@pytest.mark.parametrize("source", JUNK)
@pytest.mark.parametrize("target", [(728, 90), (1, 1), (4000, 4000)])
def test_junk_never_raises(source, target):
    out = resize(source, {"width": target[0], "height": target[1]})
    assert sorted(e.get("id") for e in out["elements"]) == sorted(e.get("id") for e in source["elements"])
    for e in out["elements"]:
        assert -0.5 <= e["x"] and e["x"] + e["width"] <= target[0] + 0.5
        assert -0.5 <= e["y"] and e["y"] + e["height"] <= target[1] + 0.5




def test_only_round_stamps_no_crash():
    """Regression (adobedata sweep): a design whose only button-like groups are round stamps."""
    els = [{"id": "s", "type": "shape", "x": 10, "y": 10, "width": 100, "height": 100, "shape_type": "ellipse",
            "fill_color": "rgba(200,0,0,1)", "group_id": "g", "z_index": 1},
           {"id": "t", "type": "text", "x": 20, "y": 40, "width": 80, "height": 30, "content": "NEW",
            "font_family": "Solstice Sans", "font_size": 20, "color": "rgba(255,255,255,1)", "group_id": "g", "z_index": 2}]
    from engine.resize import resize_with_info
    resize_with_info({"canvas": {"width": 500, "height": 500}, "elements": els}, {"width": 728, "height": 90})


def test_hairline_image_keeps_aspect():
    """Regression (adobedata sweep): a 646x10 hairline image shrunk to sub-pixel height kept its aspect."""
    from engine.resize import finalize
    src = {"canvas": {"width": 1000, "height": 1000}, "elements": [
        {"id": "h", "type": "image", "x": 0, "y": 500, "width": 646, "height": 10, "asset_id": "x.png", "fit": "fill"}]}
    out = finalize(src, {"h": {"x": 10, "y": 10, "width": 4.83, "height": 0.07}}, 728, 90)
    e = out["elements"][0]
    assert abs((e["width"] / e["height"]) / 64.6 - 1) <= 0.05
    check(src, out, 728, 90)


def test_header_panel_keeps_gap_to_first_card():
    """Regression (iter40): an unpacked header panel's padding grew into the first card below it."""
    src = json.load(open("eval/corpus/designs/l3_tall_pin_cards.json"))["source"]
    from engine.analyze import analyze
    from engine.resize import candidates
    out = dict(candidates(analyze(src), src, 1275, 1650))["stack k1.0 spread m1.0 u1 source"]
    band, card = out["band"], out["card1_bg"]
    assert card["y"] - (band["y"] + band["height"]) >= 10


def test_same_aspect_is_identity():
    """A target with the source's own aspect is the designer's layout scaled: bands, panels and pictures must
    not drift (a band thickness re-fit once pushed a 1080x1080 header from 140 to 200 px)."""
    import glob
    from engine.resize import resize
    for path in sorted(glob.glob("eval/corpus/designs/*.json")):
        source = json.load(open(path))["source"]
        W, H = source["canvas"]["width"], source["canvas"]["height"]
        out = resize(source, {"width": W, "height": H})
        from engine.analyze import analyze
        missing = {e["id"] for u in analyze(source).missing for e in u.elements}
        for a, b in zip(source["elements"], out["elements"]):
            if a["x"] < -0.5 or a["y"] < -0.5 or a["x"] + a["width"] > W + 0.5 or a["y"] + a["height"] > H + 0.5:
                continue      # bled off the source canvas: finalize must pull it in (nothing off-canvas)
            if a["id"] in missing:
                continue      # a missing asset is shrunk to a speck in a quiet corner on purpose
            for k in ("x", "y", "width", "height"):
                assert abs(float(a[k]) - float(b[k])) <= 0.6, (path, a["id"], k)


def test_stack_letterboxed_hero_stays_below_cta():
    """Regression (v3h): the stack solver fitted visual heights only, so a contain-fitted bottle whose element
    box overhangs the canvas bottom was clamped up over the CTA (l2_editorial_wide 2480x3508)."""
    from engine.analyze import analyze, inter, placed_visual
    from engine.resize import candidates
    src = json.load(open("eval/corpus/designs/l2_editorial_wide.json"))["source"]
    els = {e["id"]: e for e in src["elements"]}
    out = dict(candidates(analyze(src), src, 2480, 3508))["stack k1.3 center m1.5 u0 source"]
    hero, cta = placed_visual(els["product"], out["product"]), placed_visual(els["cta_bg"], out["cta_bg"])
    assert inter(hero, cta) == 0, (hero, cta)


def test_translucent_ring_is_not_a_backdrop():
    """A 10%-alpha halo ring sliding behind the copy must not count as a change of backdrop (v3i)."""
    from engine.analyze import analyze
    from engine.energy import _backdrop_changes
    src = json.load(open("eval/corpus/designs/l2_editorial_wide.json"))["source"]
    d = analyze(src)
    ring = next(e for e in src["elements"] if e["id"] == "deco_ring")
    place = {e["id"]: {k: e[k] for k in ("x", "y", "width", "height")} for e in src["elements"]}
    place[ring["id"]] = {"x": 0, "y": 0, "width": src["canvas"]["width"], "height": src["canvas"]["height"]}
    assert _backdrop_changes(d, place) == 0


def test_sticker_keeps_margin_to_canvas_edge():
    """A badge straddling a panel edge in the source must not be clamped flush to the canvas edge when the
    panel grows to full width (l2_split_panels 2480x3508, v3k)."""
    from engine.analyze import analyze
    from engine.resize import candidates
    src = json.load(open("eval/corpus/designs/l2_split_panels.json"))["source"]
    out = dict(candidates(analyze(src), src, 2480, 3508))["stack k1.0 center m1.0 u0 explode"]
    badge = out["el_11"]
    assert 2480 - (badge["x"] + badge["width"]) >= 60, badge


def test_overflowing_pill_row_goes_one_per_line():
    """Three pills that only fit a 300 px column by clamping to the canvas edges go one per line (v3m)."""
    from engine.analyze import analyze
    from engine.resize import candidates
    src = json.load(open("eval/corpus/designs/sample_l3_advanced.json"))["source"]
    d = analyze(src)
    for name, out in candidates(d, src, 300, 600, "stack"):
        pills = [out[i] for i in ("badge1_bg", "badge2_bg", "badge3_bg")]
        for p in pills:
            assert p["x"] >= 10 and p["x"] + p["width"] <= 290, (name, p)


def test_proportional_wraps_overflowing_badge_row():
    """Three pills wider than a 300 px canvas wrap into centred lines instead of hugging both edges (v3o)."""
    from engine.analyze import analyze
    from engine.layout import Ctx, proportional_layout
    src = json.load(open("eval/corpus/designs/sample_l3_advanced.json"))["source"]
    d = analyze(src)
    out = proportional_layout(d, 300, 600, Ctx(300, 600, d.W, d.H, 15.0), 0.7)
    pills = [out[i] for i in ("badge1_bg", "badge2_bg", "badge3_bg")]
    for p in pills:
        assert p["x"] >= 7 and p["x"] + p["width"] <= 293, p
    assert len({round(p["y"]) for p in pills}) >= 2   # wrapped onto more than one line


def test_header_band_keeps_bottom_padding_beside_regridded_cards():
    """The header band's bottom padding is capped by its nearest source neighbour (card 1), not by a far card
    that a regrid put in the same row (l3_tall_pin_cards 2560x1440 grid3: kicker 9 px from the band's edge)."""
    from engine.analyze import analyze
    from engine.layout import Ctx, stack_layout
    src = json.load(open("eval/corpus/designs/l3_tall_pin_cards.json"))["source"]
    d = analyze(src)
    out = stack_layout(d, 2560, 1440, Ctx(2560, 1440, d.W, d.H, 72.0, 1.0), 1.3, "center", True, "source", cols=3)
    band, kicker = out["band"], out["kicker"]
    assert band["y"] + band["height"] - (kicker["y"] + kicker["height"]) >= 40


def test_lone_picture_row_centres_in_stack():
    """A hero on its own stack row centres instead of taking the copy's left alignment (fb_link 2480x3508, v3t)."""
    from engine.analyze import analyze, placed_visual
    from engine.resize import candidates
    src = json.load(open("eval/corpus/designs/l1_fb_link_landscape.json"))["source"]
    els = {e["id"]: e for e in src["elements"]}
    out = dict(candidates(analyze(src), src, 2480, 3508))["stack k1.3 spread m1.5 u0 source"]
    r = placed_visual(els["el_06"], out["el_06"])
    assert abs((r[0] + r[2]) / 2 - 1240) < 0.08 * 2480, r


def test_adjacent_button_and_link_stay_packed():
    """A button and the text link beside it in the source stay side by side in a widened split column instead
    of the link right-aligning to the column's far edge (l2_split_panels 1500x500, v3v)."""
    from engine.analyze import analyze
    from engine.resize import candidates
    src = json.load(open("eval/corpus/designs/l2_split_panels.json"))["source"]
    out = dict(candidates(analyze(src), src, 1500, 500))["split 0.55 auto u1"]
    btn, link = out["el_08"], out["el_10"]
    assert link["x"] - (btn["x"] + btn["width"]) < 0.08 * 1500, (btn, link)


def test_proportional_cta_keeps_copy_left_edge():
    """A CTA that shared the copy's left edge in the source keeps it in the proportional layout (editorial
    2480x3508: the pill sat 120 px left of the headline, v3y)."""
    from engine.analyze import analyze
    from engine.layout import Ctx, proportional_layout
    src = json.load(open("eval/corpus/designs/l2_editorial_wide.json"))["source"]
    d = analyze(src)
    out = proportional_layout(d, 2480, 3508, Ctx(2480, 3508, d.W, d.H, 124.0), 1.0)
    assert abs(out["cta_bg"]["x"] - out["headline"]["x"]) < 12, (out["cta_bg"]["x"], out["headline"]["x"])


def test_rotated_label_reserves_its_standing_extent():
    """A 90-degree label's rotated ink stays inside the canvas and clear of the headline (collage 820x312, v4a)."""
    from engine.analyze import analyze, rot_aabb, inter
    from engine.resize import candidates
    src = json.load(open("eval/corpus/designs/l3_collage_rotated.json"))["source"]
    els = {e["id"]: e for e in src["elements"]}
    out = dict(candidates(analyze(src), src, 820, 312))["split 0.35 auto u0"]
    p = out["est"]
    r = rot_aabb(p["x"], p["y"], p["x"] + p["width"], p["y"] + p["height"], float(els["est"]["rotation"]))
    assert r[1] >= -0.5, r
    h = out["headline"]
    assert inter(r, (h["x"], h["y"], h["x"] + h["width"], h["y"] + h["height"])) == 0, (r, h)


def test_badge_beside_the_hero_rides_with_it():
    """A stamp sitting right beside the can in the source is pinned to it (l3_longcopy_story, v4d)."""
    from engine.analyze import analyze
    d = analyze(json.load(open("eval/corpus/designs/l3_longcopy_story.json"))["source"])
    assert next(u.host for u in d.units if u.uid == "g:g2") == "el_05"
