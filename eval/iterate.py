"""One-command scorecard for overnight iteration (no API needed).

    python -m eval.iterate [--tag NAME]

- Human resizes (adobedata pairs), split by template FAMILY into tune / held-out:
  box IoU, centre error and order agreement vs the designer's version.
- Deterministic rubric score (eval/metrics.py) on 100 corpus pairs (seed 11).
- Runtime per design.
Appends one JSON line to eval/runs/iterate.jsonl so runs can be compared."""
import argparse
import glob
import hashlib
import json
import os
import statistics as st
import time
from multiprocessing import Pool
from pathlib import Path

PAIRS = Path(os.environ.get("CHAMELEON_GT_PAIRS", "../adobedata/data/pairs"))


def _gt_init():
    os.environ["CHAMELEON_BRAND_KIT"] = str(PAIRS / "brand_kit")


def _gt_one(path):
    from engine.resize import resize
    from eval.gt_pairs import compare
    t = json.load(open(path))
    tc = t["target_canvas"]
    t0 = time.time()
    out = resize(t["source"], tc)
    return path, compare(out, t["expected"], tc["width"], tc["height"]), time.time() - t0


def _det_one(job):
    from engine.resize import resize
    from eval.metrics import score
    src, tgt = job
    out = resize(src, tgt)
    return score(src, out, None)["det_score"]


def families():
    fam = {}
    for line in open(PAIRS / "manifest.jsonl"):
        r = json.loads(line)
        fam[r["task_id"]] = r["family"]
    return fam


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    fam = families()
    files = sorted(glob.glob(str(PAIRS / "input" / "*.json")))
    split = {f: ("hold" if int(hashlib.md5(fam.get(Path(f).stem, Path(f).stem).encode()).hexdigest(), 16) % 2
                 else "tune") for f in files}
    with Pool(8, initializer=_gt_init) as p:
        gt = p.map(_gt_one, files)
    res = {"tag": a.tag, "time": time.strftime("%Y-%m-%d %H:%M")}
    for part in ("tune", "hold"):
        v = [m for f, m, _ in gt if split[f] == part]
        res[f"gt_{part}_iou"] = round(st.mean(x[0] for x in v), 4)
        res[f"gt_{part}_cerr"] = round(st.mean(x[1] for x in v), 4)
        res[f"gt_{part}_order"] = round(st.mean(x[2] for x in v), 4)
        res[f"gt_{part}_n"] = len(v)
    res["sec_per_design"] = round(st.mean(t for _, _, t in gt), 2)
    import random
    designs = [json.load(open(f)) for f in sorted(glob.glob("eval/corpus/designs/*.json"))]
    targets = json.load(open("eval/targets.json"))
    jobs = random.Random(11).sample([(d["source"], {"width": t["width"], "height": t["height"]})
                                     for d in designs for t in targets], 100)
    with Pool(8) as p:
        det = p.map(_det_one, jobs)
    res["det_mean"] = round(st.mean(det), 2)
    Path("eval/runs").mkdir(parents=True, exist_ok=True)
    with open("eval/runs/iterate.jsonl", "a") as f:
        f.write(json.dumps(res) + "\n")
    print(json.dumps(res))


if __name__ == "__main__":
    main()
