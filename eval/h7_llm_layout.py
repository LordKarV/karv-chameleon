"""H7 experiment: LLM layout generation vs the deterministic engine.

    python -m eval.h7_llm_layout --n 30 --seed 31 --out eval/runs/h7

Configs per pair: engine; claude zero-shot; claude few-shot; gemini few-shot;
qwen (local) few-shot; pool = the engine's best candidate vs the claude few-shot
plan, chosen by the engine's own energy. All graded by the strict judge (images
+ both JSONs)."""
import argparse
import json
import random
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from engine.analyze import analyze
from engine.energy import energy
from engine.resize import resize_with_info
from eval.judge import judge
from eval.llm_layout import llm_layout
from renderer.converter.convert import render_design_to_png

CONFIGS = [("claude0", "anthropic", False), ("claude1", "anthropic", True),
           ("gemini1", "gemini", True), ("qwen1", "ollama", True)]


def place_of(out):
    return {e["id"]: {k: e[k] for k in ("x", "y", "width", "height", "font_size", "letter_spacing") if k in e}
            for e in out["elements"]}


def run_pair(d, t, out):
    src, W, H = d["source"], t["width"], t["height"]
    stem = d['design_id'] + '__' + re.sub(r'[^A-Za-z0-9]+', '_', t['name'])
    sp = out / f"{stem}__src.png"
    render_design_to_png(src, "brand_kit/assets", sp, max_size=1024)
    outs, meta = {}, {}
    eo, info = resize_with_info(src, {"width": W, "height": H})
    outs["engine"] = eo
    for name, backend, fs in CONFIGS:
        try:
            outs[name], meta[name] = llm_layout(src, W, H, backend, fs, d["design_id"])
        except Exception as exc:
            meta[name] = {"error": repr(exc)[:200]}
    dd = analyze(src)
    e_eng = info.get("energy", 0.0)
    if "claude1" in outs:
        e_llm = energy(dd, place_of(outs["claude1"]), W, H)[0]
        outs["pool"] = outs["claude1"] if e_llm < e_eng else eo
        meta["pool"] = {"picked": "claude1" if e_llm < e_eng else "engine", "e_engine": e_eng, "e_llm": e_llm}
    row = {"stem": stem, "meta": {k: {x: y for x, y in v.items() if x != "raw"} for k, v in meta.items()}, "judge": {}}
    for name, o in outs.items():
        p = out / f"{stem}__{name}.png"
        (out / f"{stem}__{name}.json").write_text(json.dumps(o))
        render_design_to_png(o, "brand_kit/assets", p, max_size=1024)
        row["judge"][name] = judge(str(sp), str(p), t["name"], W, H, backend="gemini",
                                   source_json=src, output_json=o)["overall"]
    print(stem, row["judge"], flush=True)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=31)
    ap.add_argument("--out", default="eval/runs/h7")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    designs = [json.load(open(f)) for f in sorted(Path("eval/corpus/designs").glob("*.json"))]
    targets = json.load(open("eval/targets.json"))
    pairs = random.Random(a.seed).sample([(d, t) for d in designs for t in targets], a.n)
    with ThreadPoolExecutor(3) as ex:
        rows = list(ex.map(lambda p: run_pair(*p, out), pairs))
    (out / "results.json").write_text(json.dumps(rows, indent=1))
    names = ["engine"] + [c[0] for c in CONFIGS] + ["pool"]
    print("\nconfig     mean   vs engine (win/tie/loss)")
    for n in names:
        vals = [r["judge"][n] for r in rows if n in r["judge"]]
        w = sum(r["judge"][n] > r["judge"]["engine"] for r in rows if n in r["judge"])
        l = sum(r["judge"][n] < r["judge"]["engine"] for r in rows if n in r["judge"])
        print(f"{n:9s} {statistics.mean(vals):5.2f}   {w}/{len(vals) - w - l}/{l}  (n={len(vals)})")
    lat = {n: [r["meta"][n].get("latency_s") for r in rows if n in r["meta"] and "latency_s" in r["meta"][n]]
           for n in [c[0] for c in CONFIGS]}
    print("latency s:", {n: round(statistics.mean(v), 1) for n, v in lat.items() if v})
    print("pool picked llm on", sum(r["meta"].get("pool", {}).get("picked") == "claude1" for r in rows), "pairs")


if __name__ == "__main__":
    main()
