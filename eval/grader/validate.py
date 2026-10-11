"""Prove the grader's accuracy on labelled defects (docs/GRADER_PLAN.md §5).

    python -m eval.grader.validate [--n 40] [-j 6]

Base pairs: corpus designs x catalogue targets, resized by the engine (the validator,
not the grader, may import the engine). For each base we grade:
- identity      the source on its own canvas, unchanged          -> must PASS
- controls      harmless 1px nudge / 3% stretch                    -> must not add a FAIL or new major flag
- defects       corrupt.DEFECTS, each labelled with the check that must fire, and the verdict must not be PASS
Writes eval/runs/grader_val/summary.json and prints recall per defect with a Wilson 95% interval.
"""
from __future__ import annotations

import argparse
import json
import math
import random
from multiprocessing import Pool
from pathlib import Path

from eval.grader import corrupt, forensics
from eval.grader.__main__ import grade

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "eval" / "runs" / "grader_val"


def _fired(g: dict, check: str) -> bool:
    if check.startswith("H"):
        return any(r["id"].startswith(check) and r["status"] == "FAIL" for r in g["hard"])
    return bool(g["criteria"].get(check, {}).get("flags"))


def _majors(g):
    return {(f["criterion"], tuple(f["ids"])) for c in g["criteria"].values() for f in c["flags"]
            if f["severity"] == "major"}


def _one(job):
    from engine.resize import resize      # validator only
    name, src, target = job
    out = resize(src, target)
    rows = []
    base = grade(src, out, target)
    rows.append({"kind": "base", "pair": name, "verdict": base["verdict"]})
    sc = {"width": src["canvas"]["width"], "height": src["canvas"]["height"]}
    ident = grade(src, src, sc)
    rows.append({"kind": "identity", "pair": name, "ok": ident["verdict"] == "PASS", "verdict": ident["verdict"],
                 "why": [f["msg"] for c in ident["criteria"].values() for f in c["flags"]]})
    for cname, fn in corrupt.CONTROLS.items():
        o = fn(src, out)
        if o is None:
            continue
        g = grade(src, o, target)
        ok = (g["verdict"] != "FAIL" or base["verdict"] == "FAIL") and not (_majors(g) - _majors(base))
        rows.append({"kind": "control", "name": cname, "pair": name, "ok": ok, "verdict": g["verdict"]})
    png = forensics.render_png(out)
    for dname, (fn, check) in corrupt.DEFECTS.items():
        r = fn(src, out)
        if r is None:
            continue
        o, tamper = r if isinstance(r, tuple) else (r, None)
        try:
            g = grade(src, o, target, tamper(png) if tamper else None)
            ok = _fired(g, check) and g["verdict"] != "PASS"
        except Exception as ex:   # noqa: BLE001 - a grader crash is a miss, recorded
            g, ok = {"verdict": f"ERROR {ex}"}, False
        rows.append({"kind": "defect", "name": dname, "check": check, "pair": name, "ok": ok, "verdict": g["verdict"]})
    return rows


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("-j", type=int, default=6)
    ap.add_argument("--seed", type=int, default=3)
    a = ap.parse_args(argv)
    targets = json.loads((ROOT / "eval" / "targets.json").read_text())
    designs = sorted((ROOT / "eval" / "corpus" / "designs").glob("*.json"))
    rnd = random.Random(a.seed)
    jobs = []
    for i in range(a.n):
        d = designs[i % len(designs)]
        t = rnd.choice(targets)
        jobs.append((f"{d.stem}@{t['width']}x{t['height']}", json.loads(d.read_text())["source"],
                     {"width": t["width"], "height": t["height"]}))
    with Pool(a.j) as p:
        rows = [r for rs in p.imap_unordered(_one, jobs) for r in rs]
    summary = {}
    for key in sorted({(r["kind"], r.get("name", "")) for r in rows if r["kind"] != "base"}):
        rs = [r for r in rows if (r["kind"], r.get("name", "")) == key]
        k = sum(r["ok"] for r in rs)
        lo, hi = wilson(k, len(rs))
        summary["/".join(x for x in key if x)] = {"n": len(rs), "ok": k, "rate": round(k / len(rs), 3),
                                                  "ci95": [round(lo, 3), round(hi, 3)],
                                                  "misses": [(r["pair"], r["verdict"]) for r in rs if not r["ok"]][:5]}
    summary["base_verdicts"] = {v: sum(r["verdict"] == v for r in rows if r["kind"] == "base")
                                for v in ("PASS", "REVIEW", "FAIL")}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1))
    (OUT / "rows.json").write_text(json.dumps(rows, indent=1))
    for k, v in summary.items():
        if k != "base_verdicts":
            print(f"{k:28} {v['ok']:3}/{v['n']:<3} {v['rate']:.3f}  CI95 {v['ci95']}  {v['misses'][:2] if v['misses'] else ''}")
    print("engine outputs:", summary["base_verdicts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
