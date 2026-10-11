import json

from eval.grader.agree import confusion, fired, kappa, main


def test_kappa_perfect_and_chance():
    a = ["PASS", "REVIEW", "FAIL", "PASS"]
    assert kappa(a, a) == 1.0
    assert abs(kappa(["PASS", "FAIL"] * 2, ["PASS", "PASS", "FAIL", "FAIL"])) < 1e-9   # po 0.5 = pe 0.5


def test_kappa_textbook():
    # 2x2 [[20,5],[10,15]]: po 0.7, pe 0.5 -> 0.4
    a = ["y"] * 25 + ["n"] * 25
    b = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    assert abs(kappa(a, b, ("y", "n")) - 0.4) < 1e-9


def test_confusion_rows_are_first_arg():
    cm = confusion(["PASS", "PASS", "FAIL"], ["PASS", "FAIL", "FAIL"])
    assert cm == [[1, 0, 1], [0, 0, 0], [0, 0, 1]]


def test_fired_maps_hard_prefix_and_criteria():
    m = {"hard_fail": ["H6b"], "fired": ["collisions"]}
    assert fired(m, "H6") and fired(m, "collisions") and not fired(m, "readability")


def test_main_reports(tmp_path, capsys):
    man = {"a": {"design": "d", "target": "10x10", "kind": "engine", "defect": None, "verdict": "PASS",
                 "hard_fail": [], "fired": [], "flags": []},
           "b": {"design": "d", "target": "10x10", "kind": "defect", "defect": "text_on_text", "verdict": "REVIEW",
                 "hard_fail": [], "fired": ["collisions"], "flags": ["collisions: x"]}}
    lab = {"a": {"verdict": "FAIL", "tags": ["overlap"], "rating": 1}, "b": {"verdict": "REVIEW", "tags": ["overlap"]}}
    (tmp_path / "m.json").write_text(json.dumps(man))
    (tmp_path / "l.json").write_text(json.dumps(lab))
    assert main([str(tmp_path / "l.json"), "--manifest", str(tmp_path / "m.json"), "--relabel", str(tmp_path / "l.json")]) == 0
    out = capsys.readouterr().out
    assert "1 disagreements" in out and "1/2  " in out and "test-retest on 2 items: 2/2 same, kappa 1.000" in out
