"""Step 4 gate: does a small local VLM agree with the Claude judge? (PLAN_V3 §4 step 4)

Takes same-pair render pairs whose judge major counts differ (from eval/refit's dataset), asks a local
llama.cpp server (OpenAI-compatible, e.g. Qwen3-VL-2B in ghcr.io/ggml-org/llama.cpp:full) to rate each render
1-10, and reports how often the VLM's ordering matches the judge's, plus seconds per image.

    python -m eval.vlm_agree --url http://localhost:8089 --n 24 --min-side 400 --min-delta 2
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
import time
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "eval" / "runs" / "vision_bench"
OUT = ROOT / "eval" / "runs" / "refit"
PROMPT = ("This is a marketing design that was automatically resized to a new canvas. Rate the layout quality "
          "from 1 (broken: overlapping or unreadable elements, things cut off) to 10 (flawless: clear hierarchy, "
          "legible text, balanced, nothing colliding). Answer with the number only.")


def _b64(path: Path, max_side: int) -> str:
    im = Image.open(path).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def score(url: str, png: Path, max_side: int) -> tuple[float | None, float, str]:
    body = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _b64(png, max_side)}},
        {"type": "text", "text": PROMPT}]}], "max_tokens": 8, "temperature": 0.0}
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1800) as r:
        text = json.load(r)["choices"][0]["message"]["content"]
    m = re.search(r"\d+(\.\d+)?", text)
    return (float(m.group()) if m else None), time.time() - t0, text.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8089")
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--min-side", type=int, default=400)
    ap.add_argument("--min-delta", type=int, default=2)
    ap.add_argument("--max-side", type=int, default=768)
    a = ap.parse_args()
    rows = json.load(open(OUT / "dataset.json"))
    by_pair: dict[str, list[dict]] = {}
    for r in rows:
        W, H = (int(x) for x in r["pair"].split("@")[1].split("x"))
        if min(W, H) >= a.min_side:
            by_pair.setdefault(r["pair"], []).append(r)
    comps = []
    for pair, rs in sorted(by_pair.items()):
        rs = sorted(rs, key=lambda r: r["majors"])
        if rs and rs[-1]["majors"] - rs[0]["majors"] >= a.min_delta:
            comps.append((rs[0], rs[-1]))            # one comparison per pair: its best vs its worst render
    comps = comps[:a.n]
    cache: dict[tuple[str, str], tuple] = {}
    out, agree, ties, secs = [], 0, 0, []
    for better, worse in comps:
        res = []
        for r in (better, worse):
            key = (r["tag"], r["pair"])
            if key not in cache:
                cache[key] = score(a.url, RUNS / r["tag"] / f"{r['pair']}.png", a.max_side)
                secs.append(cache[key][1])
            res.append(cache[key])
        sb, sw = res[0][0], res[1][0]
        row = {"pair": better["pair"], "tag_a": better["tag"], "tag_b": worse["tag"], "majors_a": better["majors"],
               "majors_b": worse["majors"], "score_a": sb, "score_b": sw, "raw_a": res[0][2], "raw_b": res[1][2]}
        out.append(row)
        if sb is not None and sw is not None:
            agree += sb > sw
            ties += sb == sw
        print(f"{better['pair']:40s} judge {better['majors']} vs {worse['majors']} majors | vlm {sb} vs {sw} "
              f"({res[0][1]:.0f}s, {res[1][1]:.0f}s)", flush=True)
    n = len(out)
    summary = {"n": n, "agree": agree / n if n else None, "ties": ties / n if n else None,
               "seconds_per_image": sum(secs) / len(secs) if secs else None, "rows": out}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "vlm_agree.json").write_text(json.dumps(summary, indent=1))
    print(f"\nagreement {summary['agree']}, ties {summary['ties']}, {summary['seconds_per_image']:.0f} s/image, n={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
