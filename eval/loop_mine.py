"""Mine the Claude vision loop for template gaps (docs/PLAN_V3.md step 2).

    python -m eval.loop_mine --tag m1 l3_tall_pin_cards@728x90 l2_right_aligned@320x100 ...
    python -m eval.loop_mine --tag m1 --fails eval/runs/vision_bench/iter40.json   # every FAIL pair

For each pair: run the loop (Claude edits, engine finalize enforces the brief), then score the
loop's final layout with the engine's own energy next to the engine's pick.
  loop energy < engine energy  -> "generation" gap: no template produces that layout; add one.
  loop energy >= engine energy -> "selection" gap: the energy prefers the worse layout; fix a term.
Results land in eval/runs/vision_loop/mine_<tag>.json; the per-pair renders and edit logs are the
evidence for the next engine rule.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "eval" / "runs" / "vision_loop"
GEOM = ("x", "y", "width", "height", "font_size")


def _place(out: dict) -> dict:
    return {e["id"]: {k: float(e[k]) for k in GEOM if k in e} for e in out["elements"]}


def compare(design: str, W: int, H: int, loop_out: dict) -> dict:
    from engine.analyze import analyze
    from engine.energy import energy
    from engine.resize import resize_with_info
    src = json.loads((ROOT / "eval" / "corpus" / "designs" / f"{design}.json").read_text())["source"]
    d = analyze(src)
    eng_out, info = resize_with_info(src, {"width": W, "height": H})
    e_eng, _ = energy(d, _place(eng_out), W, H)
    e_loop, terms = energy(d, _place(loop_out), W, H)
    return {"engine_pick": info.get("chosen"), "engine_energy": round(e_eng, 2), "loop_energy": round(e_loop, 2),
            "gap": "generation" if e_loop < e_eng else "selection",
            "loop_terms": {k: round(v, 2) for k, v in terms.items() if v}}


def mine(pair: str, rounds: int, model: str) -> tuple[str, dict]:
    from eval.vision_loop import run
    design, size = pair.split("@")
    W, H = map(int, size.lower().split("x"))
    res = run(design, W, H, rounds, model)
    verdicts = [r["verdict"] for r in res["log"]]
    rec = {"verdicts": verdicts, "rounds": len(res["log"]) - 1}
    if rec["rounds"] > 0:
        rec.update(compare(design, W, H, res["out"]))
    else:
        rec["gap"] = "none" if verdicts[0] == "PASS" else "no-edits"
    rec["issues_final"] = res["log"][-1]["issues"][:4]
    return pair, rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.loop_mine")
    ap.add_argument("pairs", nargs="*")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--fails", help="vision_bench result json: add its FAIL pairs")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("-j", type=int, default=3)
    a = ap.parse_args(argv)
    pairs = list(a.pairs)
    if a.fails:
        res = json.loads(Path(a.fails).read_text())["pairs"]
        pairs += [k for k, v in res.items() if v["verdict"] == "FAIL" and k not in pairs]
    with ThreadPoolExecutor(a.j) as ex:
        out = dict(ex.map(lambda p: mine(p, a.rounds, a.model), pairs))
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"mine_{a.tag}.json").write_text(json.dumps(out, indent=1))
    for k, v in out.items():
        print(f"{k:40s} {'>'.join(v['verdicts']):22s} gap={v.get('gap'):10s} "
              f"engine={v.get('engine_energy', '-')} loop={v.get('loop_energy', '-')}  {v.get('engine_pick', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
