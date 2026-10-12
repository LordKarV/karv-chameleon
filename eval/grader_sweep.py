"""Grade the current engine on every corpus design x catalogue target (625 pairs) with the
deterministic grader, or on a subset, and compare with an earlier sweep.

    python -m eval.grader_sweep TAG                          # all 625 pairs  (~15 min, 7 procs)
    python -m eval.grader_sweep TAG --base OLDTAG            # ... and diff against an earlier sweep
    python -m eval.grader_sweep TAG --pairs a@300x250,b@728x90 --base OLDTAG   # quick subset re-check

Writes eval/runs/grader_sweep/TAG.json. CHAMELEON_WEIGHTS etc. apply as usual.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval" / "runs" / "grader_sweep"


def _one(job):
    from engine.resize import resize
    from eval.grader.__main__ import grade
    name, W, H = job
    src = json.loads((ROOT / "eval" / "corpus" / "designs" / f"{name}.json").read_text())["source"]
    t = {"width": W, "height": H}
    try:
        g = grade(src, resize(src, t), t)
        return {"pair": f"{name}@{W}x{H}", "verdict": g["verdict"],
                "hard": [r for r in g["hard"] if r["status"] == "FAIL"],
                "flags": [f for c in g["criteria"].values() for f in c["flags"]]}
    except Exception as ex:   # noqa: BLE001 - one broken pair must not sink the sweep
        return {"pair": f"{name}@{W}x{H}", "verdict": f"ERROR {ex!r}"[:200], "hard": [], "flags": []}


def _majors(r) -> int:
    return sum(f["severity"] == "major" for f in r["flags"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.grader_sweep")
    ap.add_argument("tag")
    ap.add_argument("--pairs", help="comma-separated design@WxH subset")
    ap.add_argument("--base", help="earlier sweep tag to diff against")
    ap.add_argument("-j", type=int, default=7)
    a = ap.parse_args(argv)
    if a.pairs:
        jobs = [(p.split("@")[0], *map(int, p.split("@")[1].split("x"))) for p in a.pairs.split(",")]
    else:
        targets = json.loads((ROOT / "eval" / "targets.json").read_text())
        jobs = [(d.stem, t["width"], t["height"]) for d in sorted((ROOT / "eval" / "corpus" / "designs").glob("*.json"))
                for t in targets]
    with Pool(a.j) as p:
        rows = list(p.imap_unordered(_one, jobs))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{a.tag}.json").write_text(json.dumps(rows, indent=1))
    print(Counter(r["verdict"][:6] for r in rows), "majors", sum(map(_majors, rows)),
          "hard", dict(Counter(h["id"] for r in rows for h in r["hard"])))
    if a.base:
        base = {r["pair"]: r for r in json.loads((OUT / f"{a.base}.json").read_text())}
        both = [r for r in rows if r["pair"] in base]
        print(f"vs {a.base}: majors {sum(_majors(base[r['pair']]) for r in both)} -> {sum(map(_majors, both))}",
              "better", sum(_majors(r) < _majors(base[r["pair"]]) for r in both),
              "worse", sum(_majors(r) > _majors(base[r["pair"]]) for r in both))
        for r in both:
            if _majors(r) != _majors(base[r["pair"]]):
                print(f"  {r['pair']:36} {_majors(base[r['pair']]):3} -> {_majors(r):3}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
