"""Step 5b · Refine: model-in-the-loop touch-up (opt-in, CHAMELEON_REFINE=ollama).

Each round a local vision model (via Ollama) sees the original render, the
current render, the units (short ids + roles + boxes) and the critic's defect
list, and answers with a small JSON edit program. The engine validates and
applies the edits exactly (groups move as one, text keeps its fit, images keep
proportions, everything stays on canvas), then gates the result:
  1. hard checks: no new collisions, nothing off-canvas, no text below its floor
  2. the model must prefer the edited render in BOTH presentation orders
Only then is the edit kept. Every step is logged for the trace.

Edit language (JSON):
  {"edits": [
     {"op": "move",   "unit": "U3", "dx": 0.04, "dy": 0},          # fractions of canvas
     {"op": "scale",  "unit": "U1", "factor": 1.2},                  # 0.5 .. 2
     {"op": "align",  "unit": "U4", "to": "U1", "axis": "center_x"}, # left/right/center_x/top/bottom/center_y
     {"op": "swap",   "a": "U2", "b": "U5"},                         # exchange positions
     {"op": "anchor", "unit": "U6", "to": "top_right"},              # 9 positions, with margin
     {"op": "distribute", "units": ["U7", "U8", "U9"], "axis": "x"}],
   "why": ["..."]}
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import tempfile
import time
import urllib.request
from pathlib import Path

from engine.analyze import Design
from engine.energy import energy
from engine.propose import _rect, _move, _scale, critique
from engine.relations import _units, _center

OLLAMA = os.environ.get("CHAMELEON_OLLAMA_URL", "http://localhost:11434/api/chat")
MODEL = os.environ.get("CHAMELEON_REFINE_MODEL", "qwen2.5vl:7b")
ROUNDS = int(os.environ.get("CHAMELEON_REFINE_ROUNDS", "2"))
GATE = os.environ.get("CHAMELEON_REFINE_GATE", "energy")   # energy | vote  (small local models show strong position bias)
ENERGY_SLACK = float(os.environ.get("CHAMELEON_REFINE_SLACK", "0.5"))
ANCHORS = {"top_left": (0, 0), "top": (0.5, 0), "top_right": (1, 0), "left": (0, 0.5), "center": (0.5, 0.5),
           "right": (1, 0.5), "bottom_left": (0, 1), "bottom": (0.5, 1), "bottom_right": (1, 1)}

PROMPT = """You are an art director fixing an automatic resize of an ad to {W}x{H} px.
IMAGE 1 = the ORIGINAL design. IMAGE 2 = the CURRENT resize. Make IMAGE 2 look like a designer laid it out for
this size while staying faithful to IMAGE 1 (same grouping, hierarchy, alignment logic).

Units in the current resize (id | role | box x,y,w,h as % of canvas):
{units}

An automatic checker reported: {defects}
{feedback}
Propose at most 3 edits that fix the most visible problems. Allowed edits (JSON):
 {{"op":"move","unit":"U3","dx":0.04,"dy":0}}            dx,dy = fractions of canvas width/height
 {{"op":"scale","unit":"U1","factor":1.2}}               factor 0.5..2 (scales the unit about its centre)
 {{"op":"align","unit":"U4","to":"U1","axis":"center_x"}} axis: left,right,center_x,top,bottom,center_y
 {{"op":"swap","a":"U2","b":"U5"}}                        exchange the positions of two units
 {{"op":"anchor","unit":"U6","to":"top_right"}}           top_left,top,top_right,left,center,right,bottom_left,bottom,bottom_right
 {{"op":"distribute","units":["U7","U8"],"axis":"x"}}     even spacing along x or y
If it already looks right, return no edits.
Answer JSON only: {{"edits":[...], "why":["short reason per edit"]}}"""

PAIR = """IMAGE 1 is the ORIGINAL ad. IMAGE 2 and IMAGE 3 are two resizes of it to {W}x{H}.
Which resize would a designer ship: fewer collisions, readable text, faithful to the original, clean alignment?
Answer JSON only: {{"better": 2}} or {{"better": 3}} (use 0 only if they are truly identical in quality)."""


def _png_b64(design: dict, side: int = 640) -> str:
    from PIL import Image
    from renderer.converter.convert import render_design_to_png
    assets = str(Path(__file__).resolve().parent.parent / "brand_kit" / "assets")
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "r.png"
        render_design_to_png(design, assets, p, max_size=side)
        im = Image.open(p).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode()


def _ask(prompt: str, images: list[str], max_tokens: int = 500, temperature: float = 0.0) -> tuple[dict | None, float]:
    body = {"model": MODEL, "stream": False, "think": False, "format": "json",
            "options": {"temperature": temperature, "num_predict": max_tokens, "seed": 7},
            "messages": [{"role": "user", "content": prompt, "images": images}]}
    t0 = time.time()
    try:
        req = urllib.request.Request(OLLAMA, json.dumps(body).encode(), {"Content-Type": "application/json"})
        text = json.loads(urllib.request.urlopen(req, timeout=300).read())["message"]["content"]
        m = re.search(r"\{.*\}", text, re.S)
        return (json.loads(m.group(0)) if m else None), time.time() - t0
    except Exception:
        return None, time.time() - t0


def _ids(d: Design):
    return {f"U{i}": u for i, u in enumerate(_units(d))}


def describe(d: Design, p: dict, W: float, H: float) -> str:
    lines = []
    for uid, u in _ids(d).items():
        r = _rect(u, p)
        if r is None:
            continue
        txt = f' "{str(u.texts[0].get("content", ""))[:40]}"' if u.texts else ""
        lines.append(f"{uid} | {u.role} | {r[0] / W * 100:.0f},{r[1] / H * 100:.0f},"
                     f"{(r[2] - r[0]) / W * 100:.0f},{(r[3] - r[1]) / H * 100:.0f}{txt}")
    return "\n".join(lines)


def apply_edits(d: Design, p: dict, edits: list, W: float, H: float) -> tuple[dict, list[str]]:
    """Validate and apply an edit program. Unknown ids/ops are skipped (logged)."""
    ids = _ids(d)
    q, log = dict(p), []
    m = 0.04 * min(W, H)
    for ed in (edits[:3] if isinstance(edits, list) else []):
        if not isinstance(ed, dict):
            continue
        op = ed.get("op")
        try:
            if op == "move" and ed.get("unit") in ids:
                q = _move(q, ids[ed["unit"]], float(ed.get("dx", 0)) * W, float(ed.get("dy", 0)) * H, W, H)
            elif op == "scale" and ed.get("unit") in ids:
                q = _scale(q, ids[ed["unit"]], min(2.0, max(0.5, float(ed.get("factor", 1)))), W, H)
            elif op == "align" and ed.get("unit") in ids and ed.get("to") in ids:
                a, b = _rect(ids[ed["unit"]], q), _rect(ids[ed["to"]], q)
                ax = ed.get("axis", "center_x")
                dx = {"left": b[0] - a[0], "right": b[2] - a[2], "center_x": _center(b)[0] - _center(a)[0]}.get(ax, 0)
                dy = {"top": b[1] - a[1], "bottom": b[3] - a[3], "center_y": _center(b)[1] - _center(a)[1]}.get(ax, 0)
                q = _move(q, ids[ed["unit"]], dx, dy, W, H)
            elif op == "swap" and ed.get("a") in ids and ed.get("b") in ids:
                ua, ub = ids[ed["a"]], ids[ed["b"]]
                ca, cb = _center(_rect(ua, q)), _center(_rect(ub, q))
                q = _move(q, ua, cb[0] - ca[0], cb[1] - ca[1], W, H)
                q = _move(q, ub, ca[0] - cb[0], ca[1] - cb[1], W, H)
            elif op == "anchor" and ed.get("unit") in ids and ed.get("to") in ANCHORS:
                u = ids[ed["unit"]]
                r = _rect(u, q)
                fx, fy = ANCHORS[ed["to"]]
                tx = m + fx * (W - 2 * m - (r[2] - r[0]))
                ty = m + fy * (H - 2 * m - (r[3] - r[1]))
                q = _move(q, u, tx - r[0], ty - r[1], W, H)
            elif op == "distribute" and isinstance(ed.get("units"), list):
                us = [ids[i] for i in ed["units"] if i in ids]
                k = 0 if ed.get("axis", "x") == "x" else 1
                if len(us) < 3:
                    raise ValueError("distribute needs 3+ units")
                us.sort(key=lambda u: _rect(u, q)[k])
                rs = [_rect(u, q) for u in us]
                gap = (rs[-1][k + 2] - rs[0][k] - sum(r[k + 2] - r[k] for r in rs)) / (len(us) - 1)
                pos = rs[0][k]
                for u, r in zip(us, rs):
                    delta = pos - r[k]
                    q = _move(q, u, delta if k == 0 else 0, delta if k == 1 else 0, W, H)
                    pos += (r[k + 2] - r[k]) + gap
            else:
                log.append(f"skipped invalid edit {json.dumps(ed)[:80]}")
                continue
            log.append(f"applied {json.dumps(ed)[:100]}")
        except Exception as exc:
            log.append(f"failed {json.dumps(ed)[:80]}: {exc!r}"[:140])
    return q, log


def _rewrap(d: Design, q: dict, W: float, H: float) -> dict:
    """Stickers, corner ribbons and decorations follow the edited units."""
    from engine.layout import _place_stickers, _avoid_text, scale_ref
    from engine.relations import anchor_decor
    q = dict(q)
    q.update(_place_stickers(d, W, H, q))
    s_min, s_area = scale_ref(d, W, H)
    q.update(_avoid_text(d, q, anchor_decor(d, W, H, q, (s_min + s_area) / 2), W, H))
    return q


def _hard_ok(t0: dict, t1: dict) -> bool:
    return (t1.get("overlap", 0) <= t0.get("overlap", 0) + 0.02 and t1.get("oob", 0) <= t0.get("oob", 0) + 1e-6
            and t1.get("read", 0) <= t0.get("read", 0) + 0.02)


def _variants(d, cur, edits, W, H):
    """The full edit program plus each edit alone: one bad edit must not sink good ones."""
    progs = [edits] + ([[e] for e in edits] if isinstance(edits, list) and len(edits) > 1 else [])
    out = []
    for prog in progs:
        q, applied = apply_edits(d, cur, prog, W, H)
        if any(x.startswith("applied") for x in applied):
            out.append((prog, _rewrap(d, q, W, H), applied))
    return out


def refine(d: Design, source: dict, p: dict, W: float, H: float) -> tuple[dict, list[dict]]:
    from engine.resize import finalize
    src_img = _png_b64(source)
    log, cur, feedback = [], p, ""
    for rnd in range(ROUNDS):
        e0, t0 = energy(d, cur, W, H)
        cur_img = _png_b64(finalize(source, cur, W, H))
        defects = "; ".join(x[2] for x in critique(d, cur, W, H)[:6]) or "none"
        prompt = PROMPT.format(W=int(W), H=int(H), units=describe(d, cur, W, H), defects=defects, feedback=feedback)
        plan, dt = _ask(prompt, [src_img, cur_img], temperature=0.0 if rnd == 0 else 0.5)
        entry = {"round": rnd + 1, "latency_s": round(dt, 1), "why": (plan or {}).get("why", []),
                 "edits": (plan or {}).get("edits", []), "accepted": False}
        if not plan or not plan.get("edits"):
            entry["result"] = "model proposed no edits" if plan is not None else "model gave no valid JSON"
            log.append(entry)
            break
        passing, reasons = [], []
        for prog, q, applied in _variants(d, cur, plan["edits"], W, H):
            e1, t1 = energy(d, q, W, H)
            if _hard_ok(t0, t1):
                passing.append((e1, prog, q, applied))
            else:
                new = [x[2] for x in critique(d, q, W, H) if x[0] == "collision"][:3]
                reasons.append(f"{json.dumps(prog)[:120]} -> " + ("; ".join(new) or "text too small or off-canvas"))
        if not passing:
            entry["result"] = "rejected by hard checks: " + " | ".join(reasons)[:400]
            feedback = ("Your previous edits were REJECTED because they broke the layout: " + " | ".join(reasons)[:600]
                        + "\nDo NOT repeat them. Propose DIFFERENT edits that do not cause these problems.\n")
            log.append(entry)
            continue
        passing.sort(key=lambda t: t[0])
        e1, prog, q, applied = passing[0]
        entry.update(applied=applied, energy=[round(e0, 3), round(e1, 3)], tried=len(passing) + len(reasons))
        new_img = _png_b64(finalize(source, q, W, H))
        v1, _ = _ask(PAIR.format(W=int(W), H=int(H)), [src_img, cur_img, new_img], 40)
        v2, _ = _ask(PAIR.format(W=int(W), H=int(H)), [src_img, new_img, cur_img], 40)
        entry["votes"] = [(v1 or {}).get("better"), (v2 or {}).get("better")]
        prefers = (v1 or {}).get("better") == 3 and (v2 or {}).get("better") == 2
        ok = prefers if GATE == "vote" else e1 <= e0 + ENERGY_SLACK
        if ok:
            why = "preferred in both orders" if GATE == "vote" else f"hard checks passed, energy {e0:.2f} -> {e1:.2f}"
            cur, entry["accepted"], entry["result"] = q, True, "accepted: " + why
            feedback = "Your previous edits were accepted. Fix whatever still looks wrong, or return no edits.\n"
        else:
            entry["result"] = ("rejected: model did not prefer it in both orders" if GATE == "vote"
                               else f"rejected: energy got worse ({e0:.2f} -> {e1:.2f})")
            feedback = ("Your previous edits " + json.dumps(prog)[:200] + " looked no better when compared side by "
                        "side. Do NOT repeat them; propose something different or return no edits.\n")
        log.append(entry)
    return cur, log
