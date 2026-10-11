"""Compare engine outputs with HUMAN-made resizes (Crello template families from
the adobedata repo): same template, redesigned by a designer at another size.

    python -m eval.gt_pairs --pairs ../adobedata/data/pairs [--limit N]

Per matched element (ids line up across a family): box IoU with the human
placement, centre error (fraction of the canvas diagonal), and pairwise order
agreement (is A left-of / above B in both?). Baselines: uniform scale-to-fit and
naive stretch. No API needed."""
import argparse
import glob
import json
import math
import os
import statistics as st
import sys
from pathlib import Path


def boxes(design):
    return {e["id"]: (e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"]) for e in design["elements"]}


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def compare(out, exp, W, H):
    bo, be = boxes(out), boxes(exp)
    ids = [i for i in be if i in bo]
    diag = math.hypot(W, H)
    c = lambda r: ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)
    ious = [iou(bo[i], be[i]) for i in ids]
    cerr = [math.dist(c(bo[i]), c(be[i])) / diag for i in ids]
    agree = total = 0
    for k, a in enumerate(ids):
        for b in ids[k + 1:]:
            for ax, tol in ((0, 0.02 * W), (1, 0.02 * H)):
                de = c(be[a])[ax] - c(be[b])[ax]
                if abs(de) < tol:
                    continue
                total += 1
                agree += (c(bo[a])[ax] - c(bo[b])[ax]) * de > 0
    return st.mean(ious) if ious else 0, st.mean(cerr) if cerr else 0, agree / total if total else 1.0


def stretch(src, W, H):
    sx, sy = W / src["canvas"]["width"], H / src["canvas"]["height"]
    return {"canvas": {"width": W, "height": H}, "elements": [
        {**e, "x": e["x"] * sx, "y": e["y"] * sy, "width": e["width"] * sx, "height": e["height"] * sy}
        for e in src["elements"]]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="../adobedata/data/pairs")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="eval/runs/gt_pairs")
    a = ap.parse_args()
    os.environ.setdefault("CHAMELEON_BRAND_KIT", str(Path(a.pairs) / "brand_kit"))
    from engine.resize import resize_with_info
    files = sorted(glob.glob(f"{a.pairs}/input/*.json"))[: a.limit or None]
    rows = []
    for f in files:
        t = json.load(open(f))
        src, exp, tc = t["source"], t["expected"], t["target_canvas"]
        W, H = tc["width"], tc["height"]
        res = {"task": Path(f).stem}
        for name, fn in (("engine", lambda: resize_with_info(src, tc)[0]),
                         ("scale_fit", lambda: resize_with_info(src, tc, only="scale_fit")[0]),
                         ("proportional", lambda: resize_with_info(src, tc, only="proportional")[0]),
                         ("stretch", lambda: stretch(src, W, H))):
            try:
                res[name] = compare(fn(), exp, W, H)
            except Exception as exc:
                res[name] = None
                print(f"  {name} failed on {res['task']}: {exc!r}"[:160], file=sys.stderr)
        rows.append(res)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    Path(a.out, "results.json").write_text(json.dumps(rows, indent=1))
    print(f"{len(rows)} human-made resize pairs\n")
    print(f"{'method':10s} {'box IoU':>8s} {'centre err':>11s} {'order agree':>12s}")
    for name in ("engine", "proportional", "scale_fit", "stretch"):
        v = [r[name] for r in rows if r.get(name)]
        print(f"{name:10s} {st.mean(x[0] for x in v):8.3f} {st.mean(x[1] for x in v):11.3f} {st.mean(x[2] for x in v):12.3f}")
    wins = sum(1 for r in rows if r.get("engine") and r.get("scale_fit") and r["engine"][0] > r["scale_fit"][0] + 0.01)
    loss = sum(1 for r in rows if r.get("engine") and r.get("scale_fit") and r["engine"][0] < r["scale_fit"][0] - 0.01)
    print(f"\nengine vs scale_fit on box IoU: closer to the human on {wins}, further on {loss}")


if __name__ == "__main__":
    main()
