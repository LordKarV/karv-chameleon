"""Calibrate the vision tier (docs/GRADER_PLAN.md §3.2 step 10). Sets nothing itself: copy
the printed precision into vlm.ENABLED (>= 0.9 -> enabled).

    python -m eval.grader.vlm_validate [--n 8] [--budget 80]

Cases per base pair (engine output, may import the engine): identity (src vs src) and
corrupt.CONTROLS must not flag; the visual corrupt.DEFECTS must. Labels: Q-wrong is
positive on a defect, negative on identity/controls, unlabelled on engine outputs (flag
rate reported); Q-rel is labelled by the exact-mask relation rule (evidence.broken).
Rows append to eval/runs/vlm_val/rows.jsonl, so an interrupted run keeps its data.
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from eval.grader import corrupt, vlm

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "eval" / "runs" / "vlm_val"
VISUAL = ("text_on_text", "bury_text", "detach_label", "collapse_picture", "tiny_text_3px", "stretch_1.5")


def _cases(src, out, target):
    cases = [("identity", "neg", src, src, src["canvas"]), ("engine", "unlabelled", src, out, target)]
    cases += [(k, "neg", src, fn(src, out), target) for k, fn in corrupt.CONTROLS.items()]
    cases += [(k, "pos", src, corrupt.DEFECTS[k][0](src, out), target) for k in VISUAL]
    return [c for c in cases if c[3] is not None]


def _bases(n: int, seed: int):
    targets = json.loads((ROOT / "eval" / "targets.json").read_text())
    designs = sorted((ROOT / "eval" / "corpus" / "designs").glob("*.json"))
    rnd = random.Random(seed)
    rnd.shuffle(designs)
    for d in designs[:n]:
        t = rnd.choice(targets)
        yield f"{d.stem}@{t['width']}x{t['height']}", json.loads(d.read_text())["source"], \
            {"width": t["width"], "height": t["height"]}


def run(n: int, seed: int, budget_min: float) -> None:
    """All cases of all bases in a seeded random order, so a budget cut still leaves a mix."""
    from engine.resize import resize      # validator only
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [(pair, c) for pair, src, target in _bases(n, seed) for c in _cases(src, resize(src, target), target)]
    random.Random(seed).shuffle(jobs)
    t0 = time.time()
    with (OUT / "rows.jsonl").open("a") as fh:
        for pair, (case, label, s, o, tgt) in jobs:
            if time.time() - t0 > budget_min * 60:
                return
            r = vlm.review(s, o, tgt, qtypes=vlm.QTYPES)
            row = {"pair": pair, "case": case, "label": label,
                   **{k: r[k] for k in ("abstain", "queries", "seconds", "questions")}}
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()
            print(f"{time.time() - t0:6.0f}s {pair:40} {case:16} {label:10} "
                  f"{[(q['qtype'], q['outcome'], q['votes']) for q in r['questions']]} abstain={list(r['abstain'])}",
                  flush=True)


def _rate(k, n):
    return round(k / n, 3) if n else None


def summarize(rows: list) -> dict:
    models = vlm.MODELS
    summary = {}
    for qt in vlm.QTYPES:
        tp = fp = fn = tn = unl = unl_flag = 0
        swaps, vote_yes = [], {m: [0, 0] for m in models}       # [yes on a positive, all yes]
        for r in rows:
            for q in (q for q in r["questions"] if q["qtype"] == qt):
                pos = (str(q["evidence"]["broken"]) == "True" if qt == "rel"       # str: older rows stored "True"
                       else {"pos": True, "neg": False}.get(r["label"]))
                swaps += q["swap_consistent"]
                for m, v in q["votes"].items():
                    for a in v.values():
                        if a == "yes" and pos is not None:
                            vote_yes[m][1] += 1
                            vote_yes[m][0] += pos
                flag = q["outcome"] == "flag"
                if pos is None:
                    unl, unl_flag = unl + 1, unl_flag + flag
                else:
                    tp, fp, fn, tn = tp + (pos and flag), fp + (flag and not pos), fn + (pos and not flag), \
                        tn + (not pos and not flag)
        summary[qt] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": _rate(tp, tp + fp),
                       "recall": _rate(tp, tp + fn), "swap_consistency": _rate(sum(swaps), len(swaps)),
                       "single_vote_yes_precision": {m: _rate(*v) for m, v in vote_yes.items()},
                       "engine_flag_rate": _rate(unl_flag, unl), "n_questions": tp + fp + fn + tn + unl}
    summary["abstain_rate"] = {m: _rate(sum(m in r["abstain"] for r in rows), len(rows)) for m in models}
    summary["abstain_reasons"] = sorted({v for r in rows for v in r["abstain"].values()})[:8]
    summary["cases"] = len(rows)
    live = [r["seconds"] for r in rows if r["seconds"] > 5]
    summary["seconds_per_pair"] = round(sum(live) / len(live), 1) if live else None
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--budget", type=float, default=80, help="minutes; no new case starts after this")
    ap.add_argument("--summary-only", action="store_true")
    a = ap.parse_args(argv)
    if not a.summary_only:
        run(a.n, a.seed, a.budget)
    rows = [json.loads(x) for x in (OUT / "rows.jsonl").read_text().splitlines() if x.strip()]
    rows = list({(r["pair"], r["case"]): r for r in rows}.values())      # re-runs: keep the latest row
    s = summarize(rows)
    (OUT / "summary.json").write_text(json.dumps(s, indent=1))
    print(json.dumps(s, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
