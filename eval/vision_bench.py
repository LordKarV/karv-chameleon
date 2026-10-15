"""Claude-vision benchmark: a fixed panel of engine outputs, judged like a designer would.

    python -m eval.vision_bench --tag iter18 [--n 120] [-j 6] [--model sonnet] [--docker]

--docker resizes and renders inside the amd64 scoring container (eval/in_container.py): Linux text
metrics flip ~12% of picks, so decisions about what ships use this mode.

The panel (seeded, spread over designs x targets) is resized by the current engine,
rendered, and judged by Claude via headless `claude -p` (subscription; see
docs/CLAUDE_VISION_JUDGE.md). Judgements are cached by image, so unchanged outputs cost
nothing on the next run. Score = mean of PASS 1 / REVIEW 0.5 / FAIL 0. Compare tags to
decide whether an engine change helped. CHAMELEON_WEIGHTS etc. apply as usual.
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter
import time
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Pool
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "eval" / "runs" / "vision_bench"
SCORE = {"PASS": 1.0, "REVIEW": 0.5, "FAIL": 0.0}


def panel(n: int, seed: int = 21) -> list[tuple[str, int, int]]:
    """n distinct design x target pairs, every design before any repeats (seeded)."""
    designs = sorted(p.stem for p in (ROOT / "eval" / "corpus" / "designs").glob("*.json"))
    targets = json.loads((ROOT / "eval" / "targets.json").read_text())
    rng = random.Random(seed)
    pairs = [(d, t["width"], t["height"]) for d in designs for t in targets]
    rng.shuffle(pairs)
    seen, rank = {}, []
    for x in pairs:                      # round-robin: each design's k-th pick sorts into round k
        seen[x[0]] = seen.get(x[0], -1) + 1
        rank.append(seen[x[0]])
    order = [x for _, x in sorted(zip(rank, pairs), key=lambda t: t[0])]
    # shape mix follows the catalogue (targets.json): cap each bucket at its share, fill leftovers in order
    from eval.run import bucket
    share = Counter(bucket(t["width"], t["height"]) for t in targets)
    left = {b: round(n * c / len(targets)) for b, c in share.items()}
    out = []
    for x in order:
        b = bucket(x[1], x[2])
        if len(out) < n and left[b] > 0:
            out.append(x)
            left[b] -= 1
    return out + [x for x in order if x not in out][:n - len(out)]


def _render(job):
    from engine.resize import resize
    from eval.grader.forensics import render_png
    d, W, H, folder = job
    src = json.loads((ROOT / "eval" / "corpus" / "designs" / f"{d}.json").read_text())["source"]
    orig, out = Path(folder) / f"{d}.png", Path(folder) / f"{d}@{W}x{H}.png"
    if not orig.is_file():
        Image.fromarray(render_png(src)).save(orig)
    Image.fromarray(render_png(resize(src, {"width": W, "height": H}))).save(out)
    return d, W, H, str(orig), str(out)


def main(argv=None) -> int:
    from eval.grader.claude_judge import judge_pair
    ap = argparse.ArgumentParser(prog="python -m eval.vision_bench")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("-j", type=int, default=6)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--docker", action="store_true", help="resize + render inside the scoring container")
    ap.add_argument("--prompt", default="designer", choices=["designer", "rubric"],
                    help="judge prompt: the designer review, or the brief's rubric words (cached separately)")
    ap.add_argument("--renders", default=None, help="judge another tag's renders instead of rendering anew")
    a = ap.parse_args(argv)
    folder = RUNS / (a.renders or a.tag)
    folder.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    if a.docker:
        from eval.in_container import render_in_docker
        rendered = render_in_docker(panel(a.n), folder)
    else:
        with Pool(6) as p:
            rendered = p.map(_render, [(d, W, H, str(folder)) for d, W, H in panel(a.n)])

    def judge(r):
        d, W, H, orig, out = r
        try:
            j = judge_pair(orig, out, f"{W}x{H}", a.model, prompt=a.prompt)
            return f"{d}@{W}x{H}", {"verdict": j["verdict"], "issues": j.get("issues", []), "cached": j.get("cached")}
        except Exception as ex:   # noqa: BLE001 - one failed call must not sink the panel
            return f"{d}@{W}x{H}", {"verdict": "ERROR", "error": str(ex)[:200]}

    with ThreadPoolExecutor(a.j) as ex:
        res = dict(ex.map(judge, rendered))
    ok = [v for v in res.values() if v["verdict"] in SCORE]
    counts = {k: sum(v["verdict"] == k for v in res.values()) for k in (*SCORE, "ERROR")}
    summary = {"tag": a.tag, "time": time.strftime("%Y-%m-%d %H:%M"), "n": len(res), "counts": counts,
               "score": round(sum(SCORE[v["verdict"]] for v in ok) / max(1, len(ok)), 4),
               "fresh_calls": sum(not v.get("cached") for v in ok), "seconds": round(time.time() - t0)}
    (RUNS / f"{a.tag}.json").write_text(json.dumps({"summary": summary, "pairs": res}, indent=1))
    with open(RUNS / "vision_bench.jsonl", "a") as f:
        f.write(json.dumps(summary) + "\n")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
