"""Collect per-candidate energy terms + closeness to the human resize, then tune
selection weights on the tune families and report the held-out families.

    python -m eval.gt_select collect      # -> eval/runs/gt_select.jsonl
    python -m eval.gt_select fit          # coordinate search on multipliers"""
import glob
import hashlib
import json
import math
import os
import random
import statistics as st
import sys
from multiprocessing import Pool
from pathlib import Path

PAIRS = Path(os.environ.get("CHAMELEON_GT_PAIRS", "../adobedata/data/pairs"))
OUT = Path("eval/runs/gt_select.jsonl")


def _init():
    os.environ["CHAMELEON_BRAND_KIT"] = str(PAIRS / "brand_kit")


def _one(path):
    from engine.analyze import analyze
    from engine.energy import energy
    from engine.resize import candidates, finalize, prior, SAME_ASPECT
    from eval.gt_pairs import compare
    t = json.load(open(path))
    src, exp, tc = t["source"], t["expected"], t["target_canvas"]
    W, H = tc["width"], tc["height"]
    if abs(math.log((W / H) / (src["canvas"]["width"] / src["canvas"]["height"]))) < SAME_ASPECT:
        return None
    d = analyze(src)
    cands = []
    for name, p in candidates(d, src, W, H):
        try:
            e, terms = energy(d, p, W, H)
        except Exception:
            continue
        iou, cerr, order = compare(finalize(src, p, W, H), exp, W, H)
        cands.append({"name": name, "prior": prior(name), "terms": {k: float(v) for k, v in terms.items()},
                      "iou": iou, "cerr": cerr, "order": order})
    return {"task": Path(path).stem, "cands": cands}


def family_split():
    fam = {json.loads(l)["task_id"]: json.loads(l)["family"] for l in open(PAIRS / "manifest.jsonl")}
    return lambda task: "hold" if int(hashlib.md5(fam.get(task, task).encode()).hexdigest(), 16) % 2 else "tune"


def collect():
    files = sorted(glob.glob(str(PAIRS / "input" / "*.json")))
    with Pool(8, initializer=_init) as p:
        rows = [r for r in p.map(_one, files) if r and r["cands"]]
    OUT.write_text("\n".join(json.dumps(r) for r in rows))
    print(f"collected {len(rows)} tasks, {sum(len(r['cands']) for r in rows)} candidates")


def fit():
    from engine.energy import weights
    base = weights()
    rows = [json.loads(l) for l in open(OUT)]
    part = family_split()
    tune = [r for r in rows if part(r["task"]) == "tune"]
    hold = [r for r in rows if part(r["task"]) == "hold"]
    keys = sorted({k for r in rows for c in r["cands"] for k in c["terms"]} & set(base))
    locked = {"overlap", "read", "oob", "hier", "backdrop", "bare", "decor"}   # rubric quality: never traded away
    free = [k for k in keys if k not in locked]

    def pick(r, mult, pb):
        def e(c):
            return (sum(base[k] * mult.get(k, 1.0) * c["terms"].get(k, 0.0) for k in keys)
                    + pb * c["prior"])
        return min(r["cands"], key=e)

    def score(rs, mult, pb):
        ch = [pick(r, mult, pb) for r in rs]
        return st.mean(c["iou"] for c in ch), st.mean(c["order"] for c in ch)

    mult, pb = {k: 1.0 for k in keys}, 1.0
    best = score(tune, mult, pb)[0]
    print(f"start: tune IoU {best:.4f} held-out {score(hold, mult, pb)}")
    grid = (0.25, 0.5, 2.0, 4.0)
    for _ in range(3):                        # coordinate search, few sweeps
        for k in free + ["__prior__"]:
            for g in grid:
                m2, p2 = dict(mult), pb
                if k == "__prior__":
                    p2 = min(4.0, max(0.25, pb * g))
                else:
                    m2[k] = min(4.0, max(0.25, mult[k] * g))
                s = score(tune, m2, p2)[0]
                if s > best + 1e-4:
                    best, mult, pb = s, m2, p2
    print(f"fitted: tune IoU {best:.4f} held-out {score(hold, mult, pb)} (n tune {len(tune)}, hold {len(hold)})")
    print("multipliers:", {k: round(v, 3) for k, v in mult.items() if abs(v - 1) > 1e-9}, "prior x", pb)
    Path("eval/runs/gt_select_fit.json").write_text(json.dumps({"mult": mult, "prior": pb}))


if __name__ == "__main__":
    {"collect": collect, "fit": fit}[sys.argv[1]]()
