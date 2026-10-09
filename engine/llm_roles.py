"""H4: optional local-LLM role labeller (llama.cpp, CPU, JSON-schema constrained).

label_roles() returns {unit uid: role} or None on any failure/timeout; callers keep
the heuristic roles from analyze() on None. Roles are only those in analyze.IMPORTANCE;
"missing" is decided by the asset check, never by the model.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from engine.analyze import IMPORTANCE, Design

MODEL_DIR = Path(__file__).parent / "models"
DEFAULT_MODEL = os.environ.get("CHAMELEON_LLM", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf")
ROLES = [r for r in IMPORTANCE if r != "missing"]
SNIPPET = 60

SYSTEM = """You label the semantic role of each unit in a marketing graphic (poster / social ad / banner).
Roles:
headline = the single main title text (usually the largest type)
subhead = secondary heading: tagline under the headline, eyebrow/kicker above it, key info line
body = longer descriptive copy
legal = fine print, disclaimers, tiny micro-copy
logo = brand logo image (or a small band/pill whose only content is the logo)
hero = the single main product/visual image (or a block whose only content is it)
image = any other picture
cta = the ONE primary call-to-action (button or action link like "Shop now")
button = a secondary button
badge = small tag/chip/sticker label (e.g. NEW, VEGAN), not the main action
ribbon = rotated sash/sticker
panel = card or container holding several items (text+image)
line = divider rule
Exactly one headline. At most one hero and one cta. "hint" is a heuristic guess that is often wrong for cta/badge/hero/logo; override it when the evidence disagrees.
Geometry is x0,y0,x1,y1 normalised to 0-1000 of the canvas. Answer with JSON {unit_key: role}."""

_llm = None
_deadline = [math.inf]      # read by llama.cpp's abort callback during prefill and decode


def _get_llm(model: str):
    global _llm
    if _llm is None or _llm[0] != model:
        import llama_cpp
        llm = llama_cpp.Llama(model_path=str(MODEL_DIR / model), n_ctx=4096, n_gpu_layers=0,
                              n_threads=os.cpu_count() or 4, verbose=False)
        cb = llama_cpp.ggml_abort_callback(lambda _: time.monotonic() > _deadline[0])
        llama_cpp.llama_set_abort_callback(llm._ctx.ctx, cb, None)
        _llm = (model, llm, cb)     # keep cb referenced
    return _llm[1]


def _unit_line(u, W, H) -> str:
    els = u.elements + [e for c in u.children for e in c.elements]
    g = [round(1000 * v / s) for v, s in zip(u.rect, (W, H, W, H))]
    types = "+".join(sorted({e.get("shape_type") or e["type"] for e in els}))
    text = " / ".join(str(e.get("content", "")) for e in u.texts)[:SNIPPET]
    assets = ",".join(e.get("asset_id", "") for e in els if e["type"] == "image")
    rot = max((abs(float(e.get("rotation") or 0)) for e in els), default=0)
    op = min((float(e.get("opacity", 1)) for e in els), default=1)
    parts = [u.uid, f"{u.kind}[{types}]", ",".join(map(str, g))]
    if u.texts:
        parts.append(f"font={u.max_font:.0f}px")
    if text:
        parts.append(json.dumps(text, ensure_ascii=False))
    if assets:
        parts.append(f"asset={assets}")
    if op < 1:
        parts.append(f"opacity={op:.2f}")
    if rot:
        parts.append(f"rot={rot:.0f}")
    parts.append(f"hint={u.role}")
    return " | ".join(parts)


def all_units(design: Design) -> list:
    """Top-level units plus container children (which analyze() also assigns roles to)."""
    def walk(u):
        return [u] + [d for c in u.children for d in walk(c)]
    return [d for u in design.units for d in walk(u)]


def _labelled(design: Design) -> list:
    # sorted by uid so prompt order == JSON key order (Ollama/llama.cpp grammars may sort keys)
    return sorted((u for u in all_units(design) if u.role != "missing"), key=lambda u: u.uid)


def build_prompt(design: Design) -> str:
    lines = [_unit_line(u, design.W, design.H) for u in _labelled(design)]
    return f"canvas {design.W:.0f}x{design.H:.0f}\n" + "\n".join(lines)


def label_roles(design: Design, source: dict, timeout_s: float = 60.0,
                model: str = DEFAULT_MODEL) -> dict | None:
    """{uid: role} for every non-missing unit (incl. container children); None on any
    failure or when timeout_s (wall clock incl. model load and prefill) is exceeded."""
    keys = [u.uid for u in _labelled(design)]
    if not keys:
        return None
    _deadline[0] = time.monotonic() + timeout_s
    try:
        llm = _get_llm(model)
        schema = {"type": "object", "additionalProperties": False, "required": keys,
                  "properties": {k: {"type": "string", "enum": ROLES} for k in keys}}
        out = llm.create_chat_completion(
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": build_prompt(design) + "\n/no_think"}],
            response_format={"type": "json_object", "schema": schema},
            temperature=0.0, max_tokens=24 * len(keys) + 32)
        roles = json.loads(out["choices"][0]["message"]["content"])
        if time.monotonic() > _deadline[0] or set(roles) != set(keys) \
                or not all(r in ROLES for r in roles.values()):
            return None
        return roles
    except Exception:   # ponytail: any model/runtime failure or abort -> heuristic roles
        if os.environ.get("CHAMELEON_LLM_DEBUG"):
            import traceback
            traceback.print_exc()
        return None
    finally:
        _deadline[0] = math.inf


def apply_llm_roles(design: Design, source: dict, timeout_s: float = 60.0) -> Design:
    """Integration point (NOT wired in; H4 verdict: don't ship): call right after analyze().
    Overwrites unit roles in place; rows/open_rows hold the same Unit objects."""
    roles = label_roles(design, source, timeout_s)
    for u in all_units(design) if roles else []:
        u.role = roles.get(u.uid, u.role)
    return design
