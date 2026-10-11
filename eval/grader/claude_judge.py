"""Claude-as-vision-judge via headless Claude Code (`claude -p`), billed to the logged-in
claude.ai subscription (no API credits). Dev-time only; see docs/CLAUDE_VISION_JUDGE.md.

    judge_pair(original_png, resized_png, target) -> {"verdict", "issues", "observations", "meta"}
    judge_pair(None, composite_png, target)       # composite: ORIGINAL left, RESIZED right

CLI:
    .venv/bin/python -m eval.grader.claude_judge a.png b.png ... --target-json targets.json -j 4
(each positional PNG is a composite; --pairs takes "orig.png:resized.png" items instead)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache" / "claude_judge"
MAX_LONG_SIDE = 2576    # Claude 4.7+ high-res tier: 2576px long edge / 4784 visual tokens (28x28 patches)
MAX_PIXELS = 4784 * 28 * 28
MIN_LONG_SIDE = 1000    # upscale tiny renders so small text gets more vision tokens
TIMEOUT_S = 300
PROMPT_VERSION = "v2"   # v2: the brief forbids dropping elements

SYSTEM = """You are a senior visual designer reviewing automatic resizes of layered ad designs.
You get the ORIGINAL design and a RESIZED version made for a new canvas size. Judge the RESIZED
version the way a picky human designer would before shipping it. Different aspect ratio is expected:
elements may move, scale and reflow, but nothing may be dropped, restyled or stretched (images keep their
aspect). Judge the result, not faithfulness of pixels.

Check, in this order:
1. Inventory: list the key elements of the ORIGINAL (logo, headline, subtext, CTA/button, product
   image, badges/pills, decorative shapes, background). For each, find it in the RESIZED version.
2. Lost or shrunk: a key element missing, cropped off-canvas, or shrunk so it no longer reads.
3. Overlap: text or logo covered by another element, or elements colliding that did not in the original.
4. Readability: text too small for the canvas, cut off, bad wraps, low contrast against its new backdrop.
5. Grouping: things that belonged together came apart (shadow under product, label inside its pill,
   icon next to its text, button text inside the button).
6. Hierarchy: headline no longer dominant, CTA lost, reading order broken.
7. Composition: large dead empty areas, lopsided/off-centre layout, elements jammed against edges.

Verdict:
- FAIL: a designer would not ship it (key content lost/unreadable/covered, broken grouping, badly
  broken composition).
- REVIEW: shippable after a small touch-up (minor crowding, slightly small text, mild imbalance).
- PASS: a designer would ship it as is. Do not invent problems; a clean resize is PASS.

Write observations first, then issues (one per concrete problem: which element, what is wrong, the
specific move/resize/restyle that fixes it), then the verdict. Be concrete and brief."""

SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {"type": "string"},
        "issues": {"type": "array", "items": {
            "type": "object",
            "properties": {"element_hint": {"type": "string"}, "problem": {"type": "string"},
                           "fix": {"type": "string"}, "severity": {"enum": ["minor", "major"]}},
            "required": ["element_hint", "problem", "fix", "severity"]}},
        "verdict": {"enum": ["PASS", "REVIEW", "FAIL"]},
    },
    "required": ["observations", "issues", "verdict"],
}


def _png_b64(path: str) -> str:
    img = Image.open(path).convert("RGB")
    w, h = img.size
    scale = min(MAX_LONG_SIDE / max(w, h), (MAX_PIXELS / (w * h)) ** 0.5)
    if max(w, h) < MIN_LONG_SIDE:
        scale = min(MIN_LONG_SIDE / max(w, h), scale)
    if abs(scale - 1) > 0.01:
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _image(path: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _png_b64(path)}}


def _content(original: str | None, resized: str, target: str) -> list[dict]:
    if original is None:
        return [_image(resized), {"type": "text", "text":
                f"This one image shows the ORIGINAL design on the LEFT and the RESIZED version on the RIGHT. "
                f"The resize target is {target}. Review the RESIZED (right) version."}]
    return [{"type": "text", "text": "Image 1 - ORIGINAL (reference):"}, _image(original),
            {"type": "text", "text": f"Image 2 - RESIZED to {target} (judge this one):"}, _image(resized),
            {"type": "text", "text": "Review the RESIZED version (Image 2)."}]


def _cache_key(original: str | None, resized: str, target: str, model: str, effort: str) -> str:
    h = hashlib.sha256(f"{PROMPT_VERSION}|{SYSTEM}|{target}|{model}|{effort}".encode())
    for p in (original, resized):
        h.update(Path(p).read_bytes() if p else b"-")
    return h.hexdigest()[:24]


def _run_claude(content: list[dict], model: str, effort: str, system: str = SYSTEM, schema: dict = SCHEMA) -> dict:
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": content}})
    cmd = ["claude", "-p", "--safe-mode", "--tools", "", "--no-session-persistence",
           "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--system-prompt", system, "--json-schema", json.dumps(schema),
           "--model", model, "--effort", effort]
    # Never let a stray API key switch billing away from the subscription login.
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    with tempfile.TemporaryDirectory() as cwd:  # empty cwd: no project CLAUDE.md / settings
        proc = subprocess.run(cmd, input=msg, capture_output=True, text=True, env=env, cwd=cwd,
                              timeout=TIMEOUT_S)
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    result = next((e for e in events if e.get("type") == "result"), None)
    if result is None or result.get("is_error"):
        raise RuntimeError(f"claude -p failed (rc={proc.returncode}): "
                           f"{(result or {}).get('result') or proc.stderr[-500:]}")
    out = result.get("structured_output")
    if out is None:  # fallback: parse JSON from the text result
        text = result.get("result", "")
        out = json.loads(text[text.find("{"): text.rfind("}") + 1])
    rate = next((e["rate_limit_info"] for e in events if e.get("type") == "rate_limit_event"), {})
    return {**out, "meta": {"model": model, "effort": effort, "duration_ms": result.get("duration_ms"),
                            "notional_cost_usd": result.get("total_cost_usd"),
                            "usage": {k: result.get("usage", {}).get(k) for k in
                                      ("input_tokens", "cache_creation_input_tokens",
                                       "cache_read_input_tokens", "output_tokens")},
                            "quota": {k: v.get("utilization") for k, v in
                                      rate.get("unifiedWindows", {}).items()}}}


def judge_pair(original_png: str | None, resized_png: str, target: str, model: str = "opus",
               effort: str = "medium", use_cache: bool = True) -> dict:
    """Judge RESIZED vs ORIGINAL. Pass original_png=None if resized_png is a side-by-side composite."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{_cache_key(original_png, resized_png, target, model, effort)}.json"
    if use_cache and cache.is_file():
        return {**json.loads(cache.read_text()), "cached": True}
    t0 = time.time()
    res = _run_claude(_content(original_png, resized_png, target), model, effort)
    res["meta"]["wall_s"] = round(time.time() - t0, 1)
    cache.write_text(json.dumps(res, indent=1))
    return {**res, "cached": False}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="*", help="composite PNGs (ORIGINAL left, RESIZED right)")
    ap.add_argument("--pairs", nargs="*", default=[], help="orig.png:resized.png items")
    ap.add_argument("--target", default="the new canvas size", help="target used for every item")
    ap.add_argument("--target-json", help="JSON {png stem: target} overriding --target")
    ap.add_argument("-j", "--concurrency", type=int, default=4)
    ap.add_argument("--model", default="opus")
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("-o", "--out", help="write {stem: result} JSON here")
    a = ap.parse_args()

    jobs = [(None, p) for p in a.images] + [tuple(p.split(":", 1)) for p in a.pairs]
    if not jobs:
        ap.error("no images given")
    targets = json.loads(Path(a.target_json).read_text()) if a.target_json else {}

    def run(job):
        orig, resized = job
        stem = Path(resized).stem
        try:
            return stem, judge_pair(orig, resized, targets.get(stem, a.target), a.model, a.effort,
                                    not a.no_cache)
        except Exception as e:  # keep the batch going; report per item
            return stem, {"verdict": "ERROR", "error": str(e)}

    results = {}
    with ThreadPoolExecutor(a.concurrency) as pool:
        for stem, r in pool.map(run, jobs):
            results[stem] = r
            meta = r.get("meta", {})
            print(f"{stem}\t{r['verdict']}\t{len(r.get('issues', []))} issues\t"
                  f"{'cached' if r.get('cached') else str(meta.get('wall_s')) + 's'}", flush=True)
    if a.out:
        Path(a.out).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    sys.exit(main())
