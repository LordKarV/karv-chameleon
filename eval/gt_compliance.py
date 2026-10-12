"""Which human resize pairs follow the hackathon's rules? Non-compliant pairs must never guide the engine.

    python -m eval.gt_compliance [PAIRS_DIR]     # default ../adobedata/data/pairs

Runs the grader's JSON hard rules (eval/grader/hard.py: ids, unchanged content/fonts/colours/assets,
in-canvas, image aspect) on each pair's human `expected` design. An image whose asset id changed but whose
pixels are identical (a scraper rename) counts as unchanged; different pixels are a replaced asset.
"""
from __future__ import annotations

import glob
import json
import sys
from collections import Counter

import numpy as np
from PIL import Image

from eval.grader.hard import json_rules

SAME_PX = 3.0   # mean abs difference (0-255) at 64x64 below which two assets are the same picture


def _pixels(assets: str, asset_id: str) -> np.ndarray:
    path = glob.glob(f"{assets}/{asset_id}*")[0]
    return np.asarray(Image.open(path).convert("RGBA").resize((64, 64))).astype(int)


def violations(pair: dict, assets: str) -> list[str]:
    src = {e["id"]: e for e in pair["source"]["elements"]}
    exp = json.loads(json.dumps(pair["expected"]))
    for e in exp["elements"]:
        s = src.get(e["id"])
        if s and e.get("type") == "image" and s.get("asset_id") != e.get("asset_id") and \
                np.abs(_pixels(assets, s["asset_id"]) - _pixels(assets, e["asset_id"])).mean() < SAME_PX:
            e["asset_id"] = s["asset_id"]
    return sorted({f"{r['id']} {r['msg']}"[:60] for r in json_rules(pair["source"], exp, pair["target_canvas"])
                   if r["status"] == "FAIL"})


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "../adobedata/data/pairs"
    files = sorted(glob.glob(f"{root}/input/*.json"))
    why, ok = Counter(), 0
    for f in files:
        bad = violations(json.load(open(f)), f"{root}/brand_kit/assets")
        ok += not bad
        why.update({b.split(" [")[0].split(" ×")[0] for b in bad})
    print(f"compliant {ok} of {len(files)}")
    for k, n in why.most_common():
        print(f"  {n:4d}  {k}")


if __name__ == "__main__":
    main()
