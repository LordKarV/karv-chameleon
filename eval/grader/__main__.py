"""Brief-compliance grader (docs/GRADER_PLAN.md).

    python -m eval.grader --batch output --tasks input [--report output/grader_report]
    python -m eval.grader --input task.json --output out.json [--output-png out.png]

Verdict: FAIL if a hard rule fails or a major soft flag fires · REVIEW if a minor flag fires · else PASS.
Exit code 1 if any FAIL, 2 on a grader error. Never imports engine/*.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

import numpy as np
from PIL import Image

from eval.grader import forensics, hard, ocr, soft

WEIGHTS = {"collisions": 0.25, "readability": 0.25, "presence": 0.15, "hierarchy": 0.1, "relations": 0.15,
           "composition": 0.1}


def _missing(src: dict, assets: Path) -> set:
    return {e["id"] for e in src["elements"] if e.get("type") == "image" and not (assets / str(e.get("asset_id"))).is_file()}


def grade(src: dict, out: dict, target: dict, png: np.ndarray | None = None, assets: Path = forensics.ASSETS,
          use_ocr: bool = False, vision: str | None = None) -> dict:
    t = time.time()
    rules = hard.json_rules(src, out, target)
    res = {"target": [target["width"], target["height"]], "hard": rules, "criteria": {}}
    if not any(r["status"] == "FAIL" and r["id"] in ("H1", "H2", "H3") for r in rules):
        fs, fo = forensics.analyze(src, assets), forensics.analyze(out, assets)
        rules += hard.ink_rules(fs, fo)
        if png is not None:
            rules.append(hard.png_rule(png, forensics.render_png(out, assets)))
        extra = ocr.flags(src, out, assets) if use_ocr else []
        if extra is None:
            res["notes"] = ["OCR unavailable: readability judged by font size only"]
        res["criteria"] = soft.grade_soft(src, out, fs, fo, _missing(src, assets), extra or [])
    failed = [r for r in rules if r["status"] == "FAIL"]
    major = [f for c in res["criteria"].values() for f in c["flags"] if f["severity"] == "major"]
    minor = [f for c in res["criteria"].values() for f in c["flags"] if f["severity"] == "minor"]
    # human scale (eval/grader_labels): clearly broken -> FAIL, noticeable -> REVIEW. Brief violations are FAILs too.
    res["verdict"] = "FAIL" if failed or major else ("REVIEW" if minor else "PASS")
    if vision and not failed:
        res["vision"] = _vision(src, out, target, assets, vision)
        if res["vision"].get("verdict") in ("PASS", "REVIEW", "FAIL"):
            res["rules_verdict"] = res["verdict"]
            # measured on the user's 120 labels: Claude's eyes, with rule FAILs kept (86.7% vs 80% rules alone)
            res["verdict"] = "FAIL" if res["verdict"] == "FAIL" else res["vision"]["verdict"]
    res["overall"] = 0.0 if failed else round(100 * sum(WEIGHTS[k] * c["score"] for k, c in res["criteria"].items()), 1)
    res["seconds"] = round(time.time() - t, 2)
    return res


def _old_judge(src: dict, out: dict, target: dict, backend: str, assets: Path) -> dict:
    """The previous 1-10 VLM judge (eval/judge.py), kept as a separate opinion to compare against."""
    import tempfile
    from eval.judge import SCORE_KEYS, judge
    try:
        d = Path(tempfile.mkdtemp(prefix="grader_judge_"))
        for name, des in (("src", src), ("out", out)):
            Image.fromarray(forensics.render_png(des, assets)).save(d / f"{name}.png")
        j = judge(str(d / "src.png"), str(d / "out.png"), "target", target["width"], target["height"], backend, src, out)
        return {"model": j["meta"]["model"], "scores": {k: j.get(k) for k in SCORE_KEYS}, "issues": j.get("issues", [])}
    except Exception as ex:    # noqa: BLE001 - a comparison opinion must never break grading
        return {"error": str(ex)[:200]}


def _vision(src: dict, out: dict, target: dict, assets: Path, model: str) -> dict:
    """Claude vision judge (headless Claude Code, docs/CLAUDE_VISION_JUDGE.md). Dev-time only."""
    import tempfile
    from eval.grader.claude_judge import judge_pair
    try:
        d = Path(tempfile.mkdtemp(prefix="grader_vision_"))
        for name, des in (("original", src), ("resized", out)):
            Image.fromarray(forensics.render_png(des, assets)).save(d / f"{name}.png")
        j = judge_pair(str(d / "original.png"), str(d / "resized.png"), f"{target['width']}x{target['height']}", model)
        return {k: j.get(k) for k in ("verdict", "issues", "observations")}
    except Exception as ex:    # noqa: BLE001 - no vision -> the rules verdict stands
        return {"error": str(ex)[:200]}


def _load_png(p: Path | None):
    return np.asarray(Image.open(p).convert("RGB")) if p and p.is_file() else None


def _pairs(args):
    if args.batch:
        for tf in sorted(Path(args.tasks).glob("*.json")):
            yield tf, Path(args.batch) / f"{tf.stem}_output.json", Path(args.batch) / f"{tf.stem}_output.png"
    else:
        yield Path(args.input), Path(args.output), Path(args.output_png) if args.output_png else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.grader")
    ap.add_argument("--input")
    ap.add_argument("--output")
    ap.add_argument("--output-png")
    ap.add_argument("--batch")
    ap.add_argument("--tasks", default="input")
    ap.add_argument("--report", default="output/grader_report")
    ap.add_argument("--assets", default=str(forensics.ASSETS))
    ap.add_argument("--ocr", action="store_true", help="also OCR every text (EasyOCR, ~10 s per pair)")
    ap.add_argument("--vision", choices=("opus", "sonnet"), help="add the Claude vision judge (claude -p, subscription)")
    ap.add_argument("--judge", choices=("ollama", "gemini", "anthropic"),
                    help="also run the previous 1-10 VLM judge (eval/judge.py) for comparison; never changes the verdict")
    args = ap.parse_args(argv)
    if not args.batch and not (args.input and args.output):
        ap.error("give --batch DIR or --input TASK --output OUT")
    grades, status = [], 0
    for tf, of, pf in _pairs(args):
        try:
            task = json.loads(tf.read_text())
            src = task.get("source", task)
            target = task.get("target_canvas") or json.loads(of.read_text())["canvas"]
            g = grade(src, json.loads(of.read_text()), target, _load_png(pf), Path(args.assets), args.ocr, args.vision)
        except Exception as ex:    # noqa: BLE001 - one broken pair must not hide the others
            traceback.print_exc()
            g, status = {"verdict": "ERROR", "error": str(ex), "hard": [], "criteria": {}, "overall": 0}, 2
        g.update(task=tf.stem, input=str(tf), output=str(of), png=str(pf) if pf else None)
        if args.judge and g["verdict"] != "ERROR":
            g["judge"] = _old_judge(src, json.loads(of.read_text()), target, args.judge, Path(args.assets))
            print(f"        old judge: {g['judge'].get('scores', g['judge'])}")
        grades.append(g)
        fails = [f"{r['id']}:{r['element'] or ''} {r['msg']}" for r in g["hard"] if r["status"] == "FAIL"]
        flags = [f"{f['criterion']}: {f['msg']}" for c in g["criteria"].values() for f in c["flags"]
                 if f["severity"] == "major"]
        print(f"{g['verdict']:6} {g['overall']:5.1f}  {tf.stem}")
        for line in fails + flags:
            print("        - " + line)
        if g["verdict"] == "FAIL":
            status = max(status, 1)
    rep = Path(args.report)
    rep.mkdir(parents=True, exist_ok=True)
    (rep / "grades.json").write_text(json.dumps(grades, indent=1, default=float))
    from eval.grader.report import write_html
    write_html(grades, rep / "index.html")
    n = {v: sum(g["verdict"] == v for g in grades) for v in ("PASS", "REVIEW", "FAIL", "ERROR")}
    print(f"grader: {n} -> {rep / 'index.html'}")
    return status


if __name__ == "__main__":
    sys.exit(main())
