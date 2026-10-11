"""Robustness sweep over the adobedata task set (7,818 real tasks).

    python -m eval.sweep [--limit N]   -> eval/runs/sweep.jsonl + summary

Per task: contract check (ids, in-canvas, image aspect, untouched fields),
whether the full engine or a fallback produced the output, and runtime."""
import argparse
import glob
import json
import os
import time
from multiprocessing import Pool
from pathlib import Path

DATA = Path(os.environ.get("CHAMELEON_TASKS", "../adobedata/data/tasks"))


def _init():
    os.environ["CHAMELEON_BRAND_KIT"] = str(DATA / "brand_kit")


def _one(path):
    from engine.resize import resize_with_info, resize
    t = json.load(open(path))
    src, tc = t["source"], t["target_canvas"]
    W, H = tc["width"], tc["height"]
    t0 = time.time()
    try:
        out, info = resize_with_info(src, tc)
        tier = "engine"
    except Exception as exc:
        out, info, tier = resize(src, tc), {"error": repr(exc)[:200]}, "fallback"
    dt = time.time() - t0
    errs = []
    sid = {e["id"]: e for e in src["elements"]}
    if sorted(e["id"] for e in out["elements"]) != sorted(sid):
        errs.append("ids")
    for e in out["elements"]:
        if e["x"] < -0.5 or e["y"] < -0.5 or e["x"] + e["width"] > W + 0.5 or e["y"] + e["height"] > H + 0.5:
            errs.append(f"off-canvas {e['id']}")
        s = sid.get(e["id"], {})
        full = e["width"] >= W - 1 and e["height"] >= H - 1
        if e["type"] == "image" and s.get("fit") != "cover" and not full and s.get("width", 0) > 0 \
                and s.get("height", 0) > 0 and e["width"] > 0 and e["height"] > 0:
            if abs((e["width"] / e["height"]) / (s["width"] / s["height"]) - 1) > 0.05:
                errs.append(f"aspect {e['id']}")
    return {"task": Path(path).stem, "tier": tier, "chosen": info.get("chosen"), "sec": round(dt, 2),
            "errors": errs[:5], "error": info.get("error"), "n": len(src["elements"])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--only", default="", help="file with task ids (one per line) to re-run")
    ap.add_argument("--out", default="eval/runs/sweep.jsonl")
    a = ap.parse_args()
    files = sorted(glob.glob(str(DATA / "input" / "*.json")))[: a.limit or None]
    if a.only:
        keep = {l.strip() for l in open(a.only) if l.strip()}
        files = [f for f in files if Path(f).stem in keep]
    out = Path(a.out)
    with Pool(a.workers, initializer=_init) as p, open(out, "w") as f:
        rows = []
        for r in p.imap_unordered(_one, files, chunksize=4):
            rows.append(r)
            f.write(json.dumps(r) + "\n")
    bad = [r for r in rows if r["errors"]]
    fb = [r for r in rows if r["tier"] != "engine"]
    secs = sorted(r["sec"] for r in rows)
    print(f"tasks {len(rows)} | contract violations {len(bad)} | fallbacks {len(fb)} | "
          f"sec median {secs[len(secs) // 2]} p95 {secs[int(len(secs) * .95)]} max {secs[-1]}")
    from collections import Counter
    print("violation kinds:", Counter(e.split()[0] for r in bad for e in r["errors"]).most_common())
    print("fallback errors:", Counter((r["error"] or "")[:80] for r in fb).most_common(5))


if __name__ == "__main__":
    main()
