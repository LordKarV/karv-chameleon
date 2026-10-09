"""H7: LLM layout generation (LayoutPrompter-style), dev-time experiment.

The model plans the composition: one box per unit (groups/panels move as one)
in target pixels. A deterministic realizer then fits text and pictures exactly
into those boxes and the engine's finishing step places background, decorations,
stickers and corner ribbons, so every output still satisfies the contract.

Backends: anthropic (Claude Sonnet 5.5), gemini (Flash), ollama (local Qwen3-4B).
Few-shot mode retrieves worked examples: other corpus designs laid out by the
engine for a target of similar shape.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path

from engine.analyze import analyze, Design, Unit
from engine.layout import Ctx, size_unit, _finish
from engine.resize import finalize, resize_with_info
from engine import measure

ANTHROPIC_MODEL = "claude-sonnet-5-5"
OLLAMA_MODEL = os.environ.get("H7_OLLAMA_MODEL", "qwen3:4b-instruct-2507-q4_K_M")


def plan_units(d: Design) -> list[Unit]:
    return [u for u in d.units if not u.host and not u.corner and u not in d.missing]


def describe(d: Design, W: int, H: int) -> str:
    lines = [f"SOURCE canvas {int(d.W)}x{int(d.H)}; TARGET canvas {W}x{H}.",
             "Units (move as a whole). src = x,y,w,h as fractions of the source canvas:"]
    for i, u in enumerate(plan_units(d)):
        r = u.rect
        src = f"{r[0] / d.W:.2f},{r[1] / d.H:.2f},{(r[2] - r[0]) / d.W:.2f},{(r[3] - r[1]) / d.H:.2f}"
        bits = [f"U{i}", f"role={u.role}", f"kind={u.kind}", f"src={src}"]
        if u.kind == "text":
            e = u.elements[0]
            c = str(e.get("content", ""))
            bits += [f"font={e['font_size']:.0f}px", f"chars={len(c)}", f'text="{c[:70]}"']
        else:
            bits.append(f"aspect={u.w / u.h:.2f} (keep it)")
            if u.texts:
                bits.append(f'label="{str(u.texts[0].get("content", ""))[:40]}"')
        lines.append(" ".join(bits))
    return "\n".join(lines)


RULES = """You are a senior graphic designer re-laying-out an ad for a new canvas size.
Return a box for EVERY unit as JSON: {"U0":[x,y,w,h], ...} in TARGET pixels.
Rules: keep every box inside the canvas with a comfortable margin; pictures/groups keep their aspect
(w/h = given aspect); text boxes must be wide enough for the text at a readable size; no unit may
overlap another unless they overlapped in the source; keep the visual hierarchy (headline and hero
prominent, legal small); keep the reading order and alignment logic of the source; use the canvas
well - no huge empty areas, no cramming; extreme strips (very wide/short) need one horizontal row.
Answer with the JSON object only."""


def example_block(target: tuple[int, int], exclude: str, k: int = 2) -> str:
    """Retrieved worked examples: corpus designs laid out by the engine for the same target."""
    W, H = target
    pool = []
    for f in sorted(Path("eval/corpus/designs").glob("*.json")):
        if f.stem == exclude:
            continue
        src = json.load(open(f))["source"]
        pool.append((abs(math.log(src["canvas"]["width"] / src["canvas"]["height"])), f.stem, src))
    out = []
    for _, did, src in sorted(pool)[:: max(1, len(pool) // (k * 3))][:k]:
        d = analyze(src)
        o, _ = resize_with_info(src, {"width": W, "height": H})
        oe = {e["id"]: e for e in o["elements"]}
        ans = {}
        for i, u in enumerate(plan_units(d)):
            ids = [e["id"] for e in u.elements] + [e["id"] for k_ in u.children for e in k_.elements]
            xs = [oe[j]["x"] for j in ids] + [oe[j]["x"] + oe[j]["width"] for j in ids]
            ys = [oe[j]["y"] for j in ids] + [oe[j]["y"] + oe[j]["height"] for j in ids]
            ans[f"U{i}"] = [round(min(xs)), round(min(ys)), round(max(xs) - min(xs)), round(max(ys) - min(ys))]
        out.append(f"EXAMPLE\n{describe(d, W, H)}\nANSWER {json.dumps(ans)}")
    return "\n\n".join(out)


def _call(backend: str, prompt: str) -> tuple[str, float]:
    from eval.judge import _load_env
    _load_env()
    t0 = time.time()
    if backend == "anthropic":
        import anthropic
        r = anthropic.Anthropic().messages.create(model=ANTHROPIC_MODEL, max_tokens=4000,
                                                  messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    elif backend == "gemini":
        from eval.judge import _gemini_client, gemini_model
        r = _gemini_client().models.generate_content(model=gemini_model(), contents=prompt,
                                                     config={"response_mime_type": "application/json"})
        text = r.text
    else:
        import urllib.request
        body = json.dumps({"model": OLLAMA_MODEL, "stream": False, "format": "json", "think": False,
                           "messages": [{"role": "user", "content": prompt}], "options": {"temperature": 0}})
        req = urllib.request.Request("http://localhost:11434/api/chat", body.encode(),
                                     {"Content-Type": "application/json"})
        text = json.loads(urllib.request.urlopen(req, timeout=900).read())["message"]["content"]
    return text, time.time() - t0


def parse_plan(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        raw = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        raw = {}
    return {k: v for k, v in raw.items() if isinstance(v, list) and len(v) == 4
            and all(isinstance(x, (int, float)) for x in v)}


def realize(d: Design, source: dict, plan: dict, W: int, H: int) -> dict:
    """Fit every unit exactly into its planned box (a unit the model skipped
    keeps a proportional box). Returns a finalized, contract-safe design."""
    ctx = Ctx(W, H, d.W, d.H, 0.04 * min(W, H))
    flow = {}
    for i, u in enumerate(plan_units(d)):
        box = plan.get(f"U{i}")
        if box is None:
            r = u.rect
            box = [r[0] / d.W * W, r[1] / d.H * H, (r[2] - r[0]) / d.W * W, (r[3] - r[1]) / d.H * H]
        x, y, bw, bh = (float(v) for v in box)
        x, y = min(max(0.0, x), W - 2), min(max(0.0, y), H - 2)
        bw, bh = max(2.0, min(bw, W - x)), max(2.0, min(bh, H - y))
        if u.kind == "text":
            e = u.elements[0]
            fit = measure.fit_text(e, bw, bh, e["font_size"] * 3, 6) or measure.fit_text(e, bw, 1e9, 6, 1)
            fs, w, h = fit if fit else (6, bw, bh)
            px = x + {"center": (bw - w) / 2, "right": bw - w}.get(e.get("text_align", "left"), 0.0)
            p = {"x": px, "y": y + max(0.0, (bh - h) / 2), "width": w, "height": h, "font_size": float(fs)}
            if e.get("letter_spacing"):
                p["letter_spacing"] = measure.at_size(e, fs)["letter_spacing"]
            flow[e["id"]] = p
        else:
            s = size_unit(u, 99, bw, ctx, max_h=bh)
            flow.update(s.place(x + (bw - s.w) / 2, y + (bh - s.h) / 2))
    return finalize(source, _finish(d, W, H, flow), W, H)


def llm_layout(source: dict, W: int, H: int, backend: str, few_shot: bool, design_id: str = "") -> tuple[dict, dict]:
    d = analyze(source)
    prompt = RULES + "\n\n"
    if few_shot:
        prompt += example_block((W, H), design_id) + "\n\nNOW THIS ONE\n"
    prompt += describe(d, W, H) + "\nANSWER"
    text, dt = _call(backend, prompt)
    plan = parse_plan(text)
    out = realize(d, source, plan, W, H)
    return out, {"latency_s": round(dt, 1), "units": len(plan_units(d)), "planned": len(plan), "raw": text[:2000]}
