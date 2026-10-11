"""Vision tier plumbing: overlay/crop sizes and a clean ABSTAIN when Ollama is down (no Ollama needed)."""
import json
from pathlib import Path

import numpy as np
from PIL import Image

from eval.grader import vlm

SRC = json.loads((Path(__file__).resolve().parents[1] / "input" / "chameleon_sample_level2_core.json")
                 .read_text())["source"]


def test_overlay_adds_margins_only():
    img = Image.new("RGB", (300, 200), "white")
    o = vlm.overlay(img, {1: (10, 10, 100, 80), 2: (200, 50, 290, 190)})
    assert o.size == (300 + 2 * vlm.MARGIN, 200)
    assert np.array_equal(np.asarray(o)[100:101, vlm.MARGIN + 120:vlm.MARGIN + 180],
                          np.asarray(img)[100:101, 120:180])       # content between boxes untouched


def test_frame_and_crop_sizes():
    W, H = SRC["canvas"]["width"], SRC["canvas"]["height"]
    render = np.zeros((round(H / 2), round(W / 2), 3), np.uint8)
    nums = vlm._numbers(SRC, SRC)
    f = vlm.frame(render, SRC, nums)
    assert max(f.size) <= vlm.THUMB
    ids = list(nums)[:2]
    c = vlm.crop(render, SRC, nums, set(ids))
    assert c.size[0] <= vlm.THUMB and (min(c.size[0] - 2 * vlm.MARGIN, c.size[1]) >= vlm.CROP_MIN
                                       or max(c.size[0] - 2 * vlm.MARGIN, c.size[1]) == vlm.THUMB - 2 * vlm.MARGIN)


def test_review_abstains_when_ollama_down(monkeypatch, tmp_path):
    monkeypatch.setattr(vlm, "OLLAMA_URL", "http://127.0.0.1:9")      # discard port: connection refused
    monkeypatch.setattr(vlm, "CACHE", tmp_path)
    r = vlm.review(SRC, SRC, SRC["canvas"], qtypes=("wrong",))
    assert r["flags"] == [] and set(r["abstain"]) == set(vlm.MODELS)
    assert all("unreachable" in v for v in r["abstain"].values())
