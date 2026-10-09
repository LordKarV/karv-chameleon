"""VLM-as-judge for retargeted designs.

judge(source_png, output_png, target_name, w, h, backend="gemini") -> dict with 1-10
scores (collisions, readability, brand_fidelity, intentional_layout,
content_preservation, overall), `issues`, and `meta` (model, latency, tokens, cost).

Backends: gemini (default; newest non-lite "gemini-X-flash" found via models.list,
override with JUDGE_GEMINI_MODEL), anthropic (claude-sonnet-5-5), ollama (qwen3-vl:4b).
Results are disk-cached under eval/.cache/ keyed by sha256(images + prompt + model).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.request
from functools import lru_cache
from pathlib import Path

from PIL import Image

CACHE_DIR = Path(__file__).parent / ".cache"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
ANTHROPIC_MODEL = "claude-sonnet-5-5"
OLLAMA_MODEL = os.environ.get("JUDGE_OLLAMA_MODEL", "qwen3-vl:4b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MAX_IMAGE_SIDE = 1536
# USD per 1M tokens (input, output). ponytail: Gemini prices are assumptions -- update if billing disagrees.
PRICES = {ANTHROPIC_MODEL: (2.00, 10.00), "gemini": (0.30, 2.50), "ollama": (0.0, 0.0)}
SCORE_KEYS = ["collisions", "readability", "brand_fidelity", "intentional_layout", "content_preservation", "overall"]

SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {"type": "array", "items": {"type": "string"}},
        **{k: {"type": "integer"} for k in SCORE_KEYS},
    },
    "required": ["issues", *SCORE_KEYS],
    "additionalProperties": False,
}

PROMPT = """You are the client's art director reviewing an automatic resize of THEIR ad. Look at it the way a
picky human would on first glance, not as a checklist. Your job is to catch anything a person would
immediately say "that looks broken / wrong / off" about.

IMAGE 1 is the ORIGINAL design. IMAGE 2 is the engine's version resized to "{target_name}" ({w}x{h} px).
The engine may only move/resize/re-stack the given elements and change font sizes; it may not add,
remove, recolor, rewrite or regenerate anything. Compare IMAGE 2 against IMAGE 1 side by side: the
original shows what the designer clearly intended.

FIRST, hunt for BROKEN RELATIONSHIPS from the original (each one is an obvious defect):
- a shadow, halo, disc, circle or backdrop that belonged under/behind a product is now detached,
  off-center, misplaced, or the product no longer sits on/in it
- a sticker, badge, stamp or ribbon that was attached to a product/card/corner now floats loose
- text or a button that sat on a specific panel/background color now sits on a different color, or a
  colored panel disappeared, merged, or got covered by another panel (lost color blocking)
- a button lost its visible pill (label floating without its shape) or blends into the background
- something centered in the original (logo, headline, product) is now visibly off-center, or the
  layout mixes alignments (e.g. centered logo over left-aligned text) without a clear axis
- grouped items (a product trio, a badge row, cards in a grid) split up, misaligned or unevenly sized
- an element became illegible, a tiny speck, cropped by the canvas edge, or overlaps text
- a missing-asset grey placeholder box is given prominent space
- huge empty areas or awkward dead zones a designer would never leave
- if IMAGE 2 has the same aspect ratio as IMAGE 1, it should look essentially identical; any
  rearrangement there is a defect

THEN score each criterion from 1 to 10:
- collisions: unwanted overlaps (intentional layering from the original is fine)
- readability: size, contrast with what is actually behind the text, nothing clipped
- brand_fidelity: colors, color-blocking, typefaces, logo and product intact and in proportion
- intentional_layout: alignment axis, spacing, balance, hierarchy; looks designed for this format
- content_preservation: everything important present AND its relationships to other elements kept
- overall: would the client accept this as their {target_name} ad?

HARD CAPS (apply them honestly): one obvious defect from the list above -> overall <= 5;
two or more -> overall <= 3; a layout that looks broken at first glance -> overall <= 2.
Calibration: 7 = clean, a client would accept with no comments; 8+ = genuinely well designed for the
format; 5 = usable but a designer would fix things; 3 = clearly broken.
List the concrete problems in `issues` (short phrases, most severe first; empty only if truly none).
Respond with JSON only."""


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except ImportError:
        pass


def _png_bytes(path: str) -> bytes:
    img = Image.open(path).convert("RGB")
    if max(img.size) > MAX_IMAGE_SIDE:
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0) if m else text)
    out = {k: max(1, min(10, int(round(float(data.get(k, 1)))))) for k in SCORE_KEYS}
    out["issues"] = [str(i) for i in data.get("issues", [])][:12]
    return out


_LOCK = threading.Lock()  # judge() runs in a thread pool; build the client/model once


@lru_cache(maxsize=1)
def _gemini_client_unlocked():
    _load_env()
    from google import genai
    return genai.Client()  # keep one alive: temporaries get GC'd and close their transport


def _gemini_client():
    with _LOCK:
        return _gemini_client_unlocked()


def gemini_model() -> str:
    with _LOCK:
        return _gemini_model_unlocked()


@lru_cache(maxsize=1)
def _gemini_model_unlocked() -> str:
    """Newest non-lite, non-preview 'gemini-<ver>-flash' this key can use."""
    if os.environ.get("JUDGE_GEMINI_MODEL"):
        return os.environ["JUDGE_GEMINI_MODEL"]
    best = (0.0, "gemini-flash-latest")
    for m in _gemini_client_unlocked().models.list():
        name = m.name.split("/")[-1]
        hit = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash", name)
        if hit and float(hit.group(1)) > best[0]:
            best = (float(hit.group(1)), name)
    print(f"[judge] gemini model: {best[1]}")
    return best[1]


def _call_gemini(prompt: str, src: bytes, out: bytes) -> tuple[str, int, int, str]:
    from google.genai import types
    model = gemini_model()
    resp = _gemini_client().models.generate_content(
        model=model,
        contents=[
            "IMAGE 1 (original):", types.Part.from_bytes(data=src, mime_type="image/png"),
            "IMAGE 2 (retargeted):", types.Part.from_bytes(data=out, mime_type="image/png"),
            prompt,
        ],
        config=types.GenerateContentConfig(
            temperature=0.0, response_mime_type="application/json", response_json_schema=SCHEMA,
        ),
    )
    u = resp.usage_metadata
    out_tokens = (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
    return resp.text, u.prompt_token_count or 0, out_tokens, model


def _call_anthropic(prompt: str, src: bytes, out: bytes) -> tuple[str, int, int, str]:
    import anthropic

    def img(b: bytes) -> dict:
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": base64.standard_b64encode(b).decode()}}

    resp = anthropic.Anthropic().beta.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # re-run on a fallback model if a safety classifier declines
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "IMAGE 1 (original):"}, img(src),
            {"type": "text", "text": "IMAGE 2 (retargeted):"}, img(out),
            {"type": "text", "text": prompt},
        ]}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"anthropic refusal: {resp.stop_details}")
    text = next(b.text for b in resp.content if b.type == "text")
    return text, resp.usage.input_tokens, resp.usage.output_tokens, resp.model


def _small(png: bytes, side: int = 640) -> str:
    img = Image.open(io.BytesIO(png))
    img.thumbnail((side, side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _call_ollama(prompt: str, src: bytes, out: bytes) -> tuple[str, int, int, str]:
    # ponytail: qwen3-vl:4b ignores think=false and spends 1.5k+ tokens thinking (~3 min/judgment
    # on an M-series laptop); we let it think, shrink images, and read the JSON after the thinking.
    body = {
        "model": OLLAMA_MODEL, "stream": False, "think": True, "format": SCHEMA,
        "options": {"temperature": 0, "num_predict": 8192, "num_ctx": 16384},
        "messages": [{"role": "user", "content": "IMAGE 1 is the original, IMAGE 2 the retargeted version.\n" + prompt,
                      "images": [_small(src), _small(out)]}],
    }
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1200) as r:
        data = json.loads(r.read())
    if not data["message"].get("content"):
        raise RuntimeError(f"ollama returned no answer (done_reason={data.get('done_reason')})")
    return data["message"]["content"], data.get("prompt_eval_count", 0), data.get("eval_count", 0), OLLAMA_MODEL


BACKENDS = {"gemini": _call_gemini, "anthropic": _call_anthropic, "ollama": _call_ollama}


def _model_id(backend: str) -> str:
    return {"gemini": gemini_model, "anthropic": lambda: ANTHROPIC_MODEL, "ollama": lambda: OLLAMA_MODEL}[backend]()


JSON_SECTION = """

You also get both designs as JSON (pixel geometry per element, z = stacking order, g = group id).
Use them together with the images to check what the eye may miss: every element still present and
not shrunk to a speck or pushed behind something, grouped/attached elements (g, overlapping stickers,
shadows, backdrop discs) still together in the same relative position, text sizes keeping the
original hierarchy, centred/aligned elements still on a common axis. Judge the IMAGES first; the
JSON is evidence for the issues you report.

ORIGINAL JSON:
{src_json}

RESIZED JSON:
{out_json}"""


def compact_design(design: dict) -> str:
    """The fields a reviewer needs, rounded, one element per line."""
    rows = []
    for e in sorted(design["elements"], key=lambda e: e.get("z_index", 0)):
        r = {"id": e["id"], "t": e["type"], "x": round(e["x"]), "y": round(e["y"]),
             "w": round(e["width"]), "h": round(e["height"]), "z": e.get("z_index", 0)}
        for k, short in (("group_id", "g"), ("rotation", "rot"), ("opacity", "op"), ("font_size", "fs"),
                         ("text_align", "al"), ("asset_id", "asset"), ("fit", "fit"), ("shape_type", "shape"),
                         ("fill_color", "fill"), ("color", "color")):
            if e.get(k) not in (None, 0, "", 1) or (k == "opacity" and e.get(k) not in (None, 1)):
                r[short] = e[k]
        if e.get("content"):
            c = str(e["content"])
            r["text"] = c if len(c) <= 60 else c[:57] + "..."
        rows.append(json.dumps(r, separators=(",", ":")))
    return json.dumps(design["canvas"]) + "\n" + "\n".join(rows)


def judge(source_png: str, output_png: str, target_name: str, w: int, h: int, backend: str = "gemini",
          source_json: dict | None = None, output_json: dict | None = None) -> dict:
    if backend not in BACKENDS:
        raise ValueError(f"unknown judge backend {backend!r}; choose from {sorted(BACKENDS)}")
    _load_env()
    src, out = _png_bytes(source_png), _png_bytes(output_png)
    prompt = PROMPT.format(target_name=target_name, w=w, h=h)
    if source_json is not None and output_json is not None:
        prompt = prompt.replace("Respond with JSON only.", "").rstrip() + JSON_SECTION.format(
            src_json=compact_design(source_json), out_json=compact_design(output_json)) + "\n\nRespond with JSON only."
    model = _model_id(backend)
    key = hashlib.sha256(b"|".join([src, out, prompt.encode(), backend.encode(), model.encode()])).hexdigest()
    cache = CACHE_DIR / f"{key}.json"
    if cache.is_file():
        cached = json.loads(cache.read_text())
        return {**cached, "meta": {**cached["meta"], "cached": True}}

    t0 = time.time()
    text, tin, tout, served = BACKENDS[backend](prompt, src, out)
    latency = time.time() - t0
    pin, pout = PRICES.get(served, PRICES.get(backend, (0.0, 0.0)))
    result = {
        **_parse(text),
        "meta": {"backend": backend, "model": served, "latency_s": round(latency, 2),
                 "input_tokens": tin, "output_tokens": tout,
                 "cost_usd": round((tin * pin + tout * pout) / 1e6, 5), "cached": False},
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, indent=1))
    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Judge one source/output PNG pair")
    ap.add_argument("source_png")
    ap.add_argument("output_png")
    ap.add_argument("--target-name", default="target")
    ap.add_argument("--backend", default="gemini", choices=sorted(BACKENDS))
    a = ap.parse_args()
    w, h = Image.open(a.output_png).size
    print(json.dumps(judge(a.source_png, a.output_png, a.target_name, w, h, a.backend), indent=1))
