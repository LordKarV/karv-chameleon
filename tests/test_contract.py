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
            if k not in ("x", "y", "width", "height", "font_size", "letter_spacing"):
                assert e[k] == v, (e["id"], k)
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


def test_refine_edit_language():
    """The model's JSON edit program is validated and applied exactly; junk is skipped; all stays on canvas."""
    from engine.analyze import analyze
    from engine.refine import apply_edits, _ids
    from engine.resize import candidates
    source = json.load(open("eval/corpus/designs/sample_l2_core.json"))["source"]
    d = analyze(source)
    W, H = 1080, 1920
    p = next(iter(candidates(d, source, W, H)))[1]
    ids = list(_ids(d))
    edits = [{"op": "move", "unit": ids[0], "dx": 0.5, "dy": 0.5}, {"op": "teleport", "unit": ids[0]},
             {"op": "anchor", "unit": "U999", "to": "top"}]
    q, log = apply_edits(d, p, edits, W, H)
    assert sum(x.startswith("applied") for x in log) == 1 and sum(x.startswith("skipped") for x in log) == 2
    q, log = apply_edits(d, q, [{"op": "scale", "unit": ids[1], "factor": 9}], W, H)  # clamped to 2x
    assert log[0].startswith("applied")
    from engine.resize import finalize
    check(source, finalize(source, q, W, H), W, H)   # what ships stays contract-safe


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
