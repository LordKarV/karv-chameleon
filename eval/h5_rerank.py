"""H5 upper-bound experiment: does reranking the engine's top-3 distinct
candidates with a strong VLM (Claude) beat the internal energy's pick, as scored
by an independent judge (Gemini)? If even this upper bound gains little, a small
local VLM reranker inside the container is not worth its cost.

    python -m eval.h5_rerank --n 40 --seed 11 --out eval/runs/h5
"""
import argparse
import json
import random
import statistics
from pathlib import Path

from engine.analyze import analyze
from engine.energy import energy
from engine.resize import candidates, finalize, prior
from eval.judge import judge
from renderer.converter.convert import render_design_to_png


def top_distinct(src, W, H, k=3):
    d = analyze(src)
    scored = sorted(((energy(d, p, W, H)[0] + prior(n), n, p) for n, p in candidates(d, src, W, H)),
                    key=lambda t: t[0])
    out, seen = [], set()
    for e, n, p in scored:
        key = tuple(sorted((i, round(v["x"]), round(v["y"]), round(v["width"])) for i, v in p.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append((e, n, finalize(src, p, W, H)))
        if len(out) == k:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out", default="eval/runs/h5")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    designs = [json.load(open(f)) for f in sorted(Path("eval/corpus/designs").glob("*.json"))]
    targets = json.load(open("eval/targets.json"))
    rng = random.Random(a.seed)
    pairs = rng.sample([(d, t) for d in designs for t in targets], a.n)
    rows = []
    for d, t in pairs:
        W, H, stem = t["width"], t["height"], f"{d['design_id']}__{t['name'].replace(' ', '_')}"
        src_png = out / f"{stem}__src.png"
        render_design_to_png(d["source"], "brand_kit/assets", src_png, max_size=1024)
        cands = top_distinct(d["source"], W, H)
        pngs = []
        for i, (_, n, o) in enumerate(cands):
            p = out / f"{stem}__c{i}.png"
            render_design_to_png(o, "brand_kit/assets", p, max_size=1024)
            pngs.append(str(p))
        claude = [judge(str(src_png), p, t["name"], W, H, backend="anthropic")["overall"] for p in pngs]
        gem = [judge(str(src_png), p, t["name"], W, H, backend="gemini")["overall"] for p in pngs]
        pick = max(range(len(pngs)), key=lambda i: (claude[i], -i))
        rows.append({"stem": stem, "names": [c[1] for c in cands], "claude": claude, "gemini": gem,
                     "energy_pick_gemini": gem[0], "rerank_pick_gemini": gem[pick], "oracle_gemini": max(gem)})
        print(stem, claude, gem, flush=True)
    (out / "results.json").write_text(json.dumps(rows, indent=1))
    for k in ("energy_pick_gemini", "rerank_pick_gemini", "oracle_gemini"):
        print(k, round(statistics.mean(r[k] for r in rows), 3))


if __name__ == "__main__":
    main()
