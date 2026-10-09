"""Step 2 · Insight (optional LLM): a model reviews the deterministic analysis and
the source render and returns structured corrections. Everything it returns is
validated; anything invalid is ignored; any failure falls back to the analysis
unchanged. Off by default (CHAMELEON_INSIGHT=anthropic|gemini|ollama to enable).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from engine.analyze import Design, IMPORTANCE, xy_rows
from engine.relations import extract_graph

CACHE = Path(os.environ.get("CHAMELEON_CACHE", Path.home() / ".cache" / "chameleon")) / "insight"
ROLES = sorted(IMPORTANCE)

PROMPT = """You are a senior designer checking an automatic analysis of an ad before it is resized
from {sw}x{sh} to {tw}x{th}. The image is the original. Below is what the engine inferred.

{analysis}

Return JSON only:
{{"roles": {{"<unit id>": "<role>"}},
  "keep_together": [["<small unit id>", "<unit it belongs to>"]],
  "fill_page": true,
  "notes": ["<short nuance a designer would keep in mind for this resize>"]}}
- roles: ONLY units whose inferred role is wrong. Allowed roles: {roles}
- keep_together: things that must move as one (a badge on a product, a caption under its picture)
  that are not grouped yet
- fill_page: should the content fill the new canvas (true) or stay a compact block like the original (false)?
Only list corrections you are confident about; empty lists are fine."""


def describe(d: Design) -> str:
    g = getattr(d, "_graph", None) or extract_graph(d)
    lines = ["UNITS (id | kind | role | box x,y,w,h as % of canvas | content):"]
    for u in d.units:
        r = u.rect
        txt = ""
        if u.texts:
            txt = '"' + str(u.texts[0].get("content", ""))[:60] + '"'
        elif u.elements and u.elements[0].get("asset_id"):
            txt = u.elements[0]["asset_id"]
        extra = f" pinned_to={u.host}" if u.host else (f" corner={u.corner}" if u.corner else "")
        lines.append(f"{u.uid} | {u.kind} | {u.role} | {r[0] / d.W * 100:.0f},{r[1] / d.H * 100:.0f},"
                     f"{(r[2] - r[0]) / d.W * 100:.0f},{(r[3] - r[1]) / d.H * 100:.0f} | {txt}{extra}")
    lines.append("READING ORDER: " + " -> ".join("[" + ",".join(u.uid for u in r) + "]" for r in d.rows))
    lines.append("ALIGNMENT PAIRS: " + ", ".join(f"{a}~{b}:{k}" for a, b, k in g["pairs"][:30]))
    lines.append("DECORATIONS: " + ", ".join(e["id"] for e in d.decor))
    return "\n".join(lines)


def _call(backend: str, prompt: str, png: bytes | None) -> str:
    if backend == "anthropic":
        import base64
        import anthropic
        content = [{"type": "text", "text": prompt}]
        if png:
            content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                            "data": base64.b64encode(png).decode()}})
        r = anthropic.Anthropic().messages.create(model="claude-sonnet-5-5", max_tokens=2000,
                                                  messages=[{"role": "user", "content": content}])
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    if backend == "gemini":
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        parts = [prompt] + ([types.Part.from_bytes(data=png, mime_type="image/png")] if png else [])
        model = os.environ.get("CHAMELEON_INSIGHT_GEMINI", "gemini-3.8-flash")
        return client.models.generate_content(model=model, contents=parts,
                                              config={"response_mime_type": "application/json"}).text
    import urllib.request
    body = json.dumps({"model": os.environ.get("CHAMELEON_INSIGHT_OLLAMA", "qwen3:4b-instruct-2507-q4_K_M"),
                       "stream": False, "format": "json", "think": False, "options": {"temperature": 0},
                       "messages": [{"role": "user", "content": prompt}]})
    req = urllib.request.Request("http://localhost:11434/api/chat", body.encode(),
                                 {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=300).read())["message"]["content"]


def _render_png(source: dict) -> bytes | None:
    try:
        import tempfile
        from renderer.converter.convert import render_design_to_png
        assets = Path(__file__).resolve().parent.parent / "brand_kit" / "assets"
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "s.png"
            render_design_to_png(source, str(assets), p, max_size=768)
            return p.read_bytes()
    except Exception:
        return None


def get_insight(d: Design, source: dict, W: int, H: int, backend: str) -> dict | None:
    analysis = describe(d)
    prompt = PROMPT.format(sw=int(d.W), sh=int(d.H), tw=W, th=H, analysis=analysis, roles=", ".join(ROLES))
    key = hashlib.sha256((backend + prompt).encode()).hexdigest()
    cache = CACHE / f"{key}.json"
    if cache.is_file():
        return json.loads(cache.read_text())
    try:
        text = _call(backend, prompt, None if backend == "ollama" else _render_png(source))
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else None
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data))
    return data


def apply_insight(d: Design, ins: dict) -> list[str]:
    """Apply validated corrections in place; returns a log of what changed."""
    log = []
    every = {u.uid: u for u in d.units}
    for k in [x for u in d.units for x in u.children]:
        every.setdefault(k.uid, k)
    for uid, role in (ins.get("roles") or {}).items():
        u = every.get(uid)
        if u is not None and role in IMPORTANCE and u.role != role and u.role != "missing":
            log.append(f"role {uid}: {u.role} -> {role}")
            u.role = role
    changed = False
    for pair in ins.get("keep_together") or []:
        if not (isinstance(pair, list) and len(pair) == 2):
            continue
        a, b = every.get(pair[0]), every.get(pair[1])
        if a is None or b is None or a is b or a.host or b.host or a.parent or b.parent:
            continue
        a.host = b.uid
        a.rel = (((a.rect[0] + a.rect[2]) / 2 - b.rect[0]) / b.w, ((a.rect[1] + a.rect[3]) / 2 - b.rect[1]) / b.h)
        log.append(f"keep {a.uid} with {b.uid}")
        changed = True
    if changed:
        d.rows = xy_rows([u for u in d.units if not u.host and not u.corner and u not in d.missing], d.W, d.H)
    if isinstance(ins.get("fill_page"), bool):
        d._fill_intent = ins["fill_page"]
        log.append(f"fill page: {ins['fill_page']}")
    for n in (ins.get("notes") or [])[:5]:
        log.append("note: " + str(n)[:160])
    return log
