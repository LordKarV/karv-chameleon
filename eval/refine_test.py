"""Evaluate the local-model refine loop (engine/refine.py) offline.

    CHAMELEON_REFINE_MODEL=qwen2.5vl:7b python -m eval.refine_test --worst 20 --mid 10

Takes the lowest-judged pairs of eval/runs/base100 (plus some mid-range ones as a
do-no-harm check), runs the engine with refine on, and reports per pair: edits
accepted, model latency, deterministic score before/after. Builds a blind
before/after page (eval/blind_refine/index.html) for every pair it changed."""
import argparse
import json
import os
import time
from pathlib import Path

os.environ["CHAMELEON_REFINE"] = "ollama"
from engine.resize import resize_with_info          # noqa: E402
from eval.metrics import score                       # noqa: E402
from eval.blind_ab import TEMPLATE, b64              # noqa: E402
from renderer.converter.convert import render_design_to_png  # noqa: E402

BASE = Path("eval/runs/base100")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worst", type=int, default=20)
    ap.add_argument("--mid", type=int, default=10)
    ap.add_argument("--out", default="eval/runs/refine_local")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(l) for l in open(BASE / "results.jsonl")]
    rows = [r for r in rows if "judge" in r]
    rows.sort(key=lambda r: r["judge"]["overall"])
    pick = rows[:a.worst] + rows[len(rows) // 2: len(rows) // 2 + a.mid]
    results, items = [], []
    for r in pick:
        src = json.loads((BASE / "sources" / f"{r['design_id']}.json").read_text())
        before = json.loads((BASE / "outputs" / f"{r['stem']}.json").read_text())
        t0 = time.time()
        after, info = resize_with_info(src, {"width": r["w"], "height": r["h"]})
        dt = time.time() - t0
        log = info.get("refine", [])
        changed = json.dumps(after) != json.dumps(before)
        ap_png = out / f"{r['stem']}__after.png"
        render_design_to_png(after, "brand_kit/assets", ap_png, max_size=1024)
        m0 = score(src, before, str(BASE / "outputs" / f"{r['stem']}.png"))
        m1 = score(src, after, str(ap_png))
        res = {"stem": r["stem"], "judge_before": r["judge"]["overall"], "det_before": m0["det_score"],
               "det_after": m1["det_score"], "accepted": sum(x.get("accepted", False) for x in log),
               "rounds": len(log), "changed": changed, "seconds": round(dt, 1),
               "log": [{k: v for k, v in x.items()} for x in log]}
        (out / f"{r['stem']}__after.json").write_text(json.dumps(after))
        results.append(res)
        print(f"{r['stem'][:60]:60s} judge0={r['judge']['overall']} det {m0['det_score']:.1f}->{m1['det_score']:.1f} "
              f"accepted={res['accepted']}/{res['rounds']} {dt:.0f}s", flush=True)
        if changed:
            left_before = len(items) % 2 == 0
            imgs = {"before": b64(BASE / "outputs" / f"{r['stem']}.png"), "after": b64(ap_png)}
            items.append({"id": r["stem"], "design": r["design_id"], "target": r["target"],
                          "src": b64(BASE / "sources" / f"{r['design_id']}.png", 420),
                          "left": imgs["before" if left_before else "after"],
                          "right": imgs["after" if left_before else "before"],
                          "left_is": "engine" if left_before else "model", "model": "refined",
                          "judge": {"engine": round(m0["det_score"], 1), "model": round(m1["det_score"], 1),
                                    "pref": "engine" if m0["det_score"] > m1["det_score"] + 0.5 else
                                    ("model" if m1["det_score"] > m0["det_score"] + 0.5 else "tie")}})
    (out / "results.json").write_text(json.dumps(results, indent=1))
    if items:
        page = Path("eval/blind_refine")
        page.mkdir(parents=True, exist_ok=True)
        html = TEMPLATE.replace("__ITEMS__", json.dumps(items)).replace("blind_ab_votes_v1", "blind_refine_v1")
        html = html.replace("You preferred the engine", "You preferred BEFORE refine").replace(
            "You preferred the model", "You preferred AFTER refine").replace(
            "Agreement with the strict judge", "Agreement with the deterministic score")
        (page / "index.html").write_text(html)
    n = len(results)
    acc = sum(r["accepted"] > 0 for r in results)
    print(f"\npairs {n}; refined (>=1 edit accepted) {acc}; changed outputs {len(items)}; "
          f"mean det {sum(r['det_before'] for r in results) / n:.1f} -> {sum(r['det_after'] for r in results) / n:.1f}; "
          f"mean seconds {sum(r['seconds'] for r in results) / n:.0f}")


if __name__ == "__main__":
    main()
