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

PROMPT = """You are a senior brand designer grading an automatic layout-retargeting engine.

IMAGE 1 is the ORIGINAL design. IMAGE 2 is the engine's version resized to the target format
"{target_name}" ({w}x{h} px). The engine may only move/resize/re-stack the given elements and
change font sizes; it may not add, remove, recolor, rewrite or regenerate anything. Judge IMAGE 2
as a finished ad in that format, using IMAGE 1 as the reference for intent and content.

Score each criterion from 1 (unacceptable) to 10 (a professional designer would ship it):
- collisions: no unwanted overlaps. Intentional layering (text on its own button/badge, decorative
  translucent shapes, a full-bleed background) is fine. Text overlapping other text, text sitting on a
  busy part of a photo/product, or elements obscuring each other is bad.
- readability: every piece of text is large enough to read at this format's real size, has enough
  contrast with what is behind it, and nothing is cut off, clipped by the canvas edge, or overflowing.
- brand_fidelity: colors, typefaces and logo match the original; logo and product images are intact,
  undistorted (not stretched/squashed) and in proportion.
- intentional_layout: looks designed, not mechanically scaled: clean alignment, even spacing and
  margins, balanced use of the canvas (no awkward empty bands or cramming), clear hierarchy where the
  most important element (usually headline/product) still stands out.
- content_preservation: everything important from the original (headline, product, logo, CTA,
  key badges/text) is still present and noticeable; nothing important hidden or shrunk to a speck.
  Shrinking minor items (legal text, decorations) is acceptable if they remain visible.
- overall: your holistic grade of IMAGE 2 as a {target_name} deliverable.

Be strict and calibrated: 5 = acceptable but visibly automated; 8+ only if it looks hand-designed for this format.
List the concrete problems you see in `issues` (short phrases, most severe first; empty if none).
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


def judge(source_png: str, output_png: str, target_name: str, w: int, h: int, backend: str = "gemini") -> dict:
    if backend not in BACKENDS:
        raise ValueError(f"unknown judge backend {backend!r}; choose from {sorted(BACKENDS)}")
    _load_env()
    src, out = _png_bytes(source_png), _png_bytes(output_png)
    prompt = PROMPT.format(target_name=target_name, w=w, h=h)
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
