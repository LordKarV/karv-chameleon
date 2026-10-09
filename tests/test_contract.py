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
