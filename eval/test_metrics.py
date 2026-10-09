"""Sanity check: python -m eval.test_metrics (or pytest eval/test_metrics.py)."""
import json
from pathlib import Path

from eval._test_engines import fit_center, stretch
from eval.metrics import score


def test_fit_beats_stretch_and_stretch_is_flagged():
    for p in sorted(Path("input").glob("*.json")):
        task = json.loads(p.read_text())
        tgt = task["target_canvas"]
        fit = score(task, fit_center(task["source"], tgt), target=tgt)
        st = score(task, stretch(task["source"], tgt), target=tgt)
        assert fit["structural_ok"], (p.name, fit["structural_errors"])
        assert not st["structural_ok"] and any("aspect" in e for e in st["structural_errors"]), p.name
        assert fit["det_score"] > st["det_score"], (p.name, fit["det_score"], st["det_score"])


def test_missing_id_is_zero():
    task = json.loads(Path("input/chameleon_sample_level1_warmup.json").read_text())
    out = fit_center(task["source"], task["target_canvas"])
    out = {**out, "elements": out["elements"][1:]}
    r = score(task, out)
    assert not r["structural_ok"] and r["det_score"] == 0.0


if __name__ == "__main__":
    test_fit_beats_stretch_and_stretch_is_flagged()
    test_missing_id_is_zero()
    print("ok")
