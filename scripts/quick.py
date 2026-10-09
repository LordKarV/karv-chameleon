"""Quick sweep: python scripts/quick.py OUTDIR [design_glob] — runs resize on
corpus designs x a few targets, renders, prints chosen template, contract issues."""
import glob, json, sys, time
from pathlib import Path
from engine.main import self_check
from engine.resize import resize_with_info
from renderer.converter.convert import render_design_to_png

TARGETS = [(1080, 1920), (1200, 628), (728, 90), (300, 250), (160, 600)]
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
pat = sys.argv[2] if len(sys.argv) > 2 else "*"
for f in sorted(glob.glob(f"eval/corpus/designs/{pat}.json")):
    d = json.load(open(f))
    for W, H in TARGETS:
        t0 = time.time()
        o, info = resize_with_info(d["source"], {"width": W, "height": H})
        dt = time.time() - t0
        probs = self_check(d["source"], o)
        name = f"{d['design_id']}_{W}x{H}"
        render_design_to_png(o, "brand_kit/assets", out / f"{name}.png")
        print(f"{name:40s} {info['chosen']:28s} E={info.get('energy', 0):6.2f} {dt:5.2f}s {probs if probs else ''}")
