"""The grader catches what it claims to catch (fast subset of eval/grader/validate.py)."""
import json
from pathlib import Path

import pytest

from eval.grader import corrupt
from eval.grader.__main__ import grade

TASK = json.loads((Path(__file__).resolve().parents[1] / "input" / "chameleon_sample_level2_core.json").read_text())
SRC = TASK["source"]
CANVAS = {"width": SRC["canvas"]["width"], "height": SRC["canvas"]["height"]}


def test_identity_passes():
    g = grade(SRC, SRC, CANVAS)
    assert g["verdict"] == "PASS", g


@pytest.mark.parametrize("name", ["stretch_1.5", "content_changed", "element_dropped", "detach_label",
                                  "tiny_text_3px", "text_on_text", "text_off_canvas"])
def test_defect_is_caught(name):
    fn, check = corrupt.DEFECTS[name]
    g = grade(SRC, fn(SRC, SRC), CANVAS)
    fired = any(r["id"] == check and r["status"] == "FAIL" for r in g["hard"]) if check.startswith("H") \
        else bool(g["criteria"][check]["flags"])
    assert fired and g["verdict"] != "PASS", (name, g["verdict"])


def test_harmless_nudge_still_passes():
    assert grade(SRC, corrupt.nudge(SRC, SRC), CANVAS)["verdict"] == "PASS"
