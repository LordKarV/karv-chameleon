"""Leaderboard across runs, computed on the (design, target) pairs all runs share.

  python -m eval.compare eval/runs/a eval/runs/b ...
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

BUCKETS = ["tall", "square", "wide", "ultrawide"]


def load(run: str) -> dict:
    recs = [json.loads(line) for line in (Path(run) / "results.jsonl").read_text().splitlines() if line.strip()]
    return {(r["design_id"], r["target"]): r for r in recs}


def _mean(vals: list) -> str:
    return f"{np.mean(vals):.1f}" if vals else "-"


def main(runs: list[str]) -> int:
    if not runs:
        print(__doc__)
        return 1
    data = {r: load(r) for r in runs}
    common = set.intersection(*(set(d) for d in data.values()))
    print(f"{len(common)} common pairs\n")
    head = f"{'run':<28}{'crash':>6}{'ok%':>6}{'det':>7}{'judge':>7}" + "".join(f"{b:>10}" for b in BUCKETS) + f"{'eng_s':>7}"
    print(head + "\n" + "-" * len(head))
    rows = []
    for run, d in data.items():
        recs = [d[k] for k in common]
        judged = [r["judge"]["overall"] for r in recs if "judge" in r]
        rows.append((np.mean([r["det_score"] for r in recs]) if recs else 0, run, recs, judged))
    for det, run, recs, judged in sorted(rows, key=lambda x: -x[0]):
        per_b = "".join(f"{_mean([r['det_score'] for r in recs if r['bucket'] == b]):>10}" for b in BUCKETS)
        print(f"{Path(run).name[:27]:<28}{sum(bool(r.get('error')) for r in recs):>6}"
              f"{100 * np.mean([bool(r.get('structural_ok')) for r in recs]):>6.0f}{det:>7.1f}"
              f"{(f'{np.mean(judged):.2f}' if judged else '-'):>7}{per_b}"
              f"{np.mean([r.get('engine_s', 0) for r in recs]):>7.2f}")
    print("\n(bucket columns = det_score; judge = mean overall 1-10 where judged)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
