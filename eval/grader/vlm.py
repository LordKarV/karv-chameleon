"""Human-eye vision tier (docs/GRADER_PLAN.md §3): local VLMs answer targeted binary
questions and may only ESCALATE a pass to REVIEW, never decide PASS/FAIL.

Protocol: set-of-mark overlay with labels in a margin strip · competence probe per model
(read the text, count the pictures; fail -> ABSTAIN) · images before text · JSON schema
with observations first · every A/B question asked in both orders (a model's vote counts
only if both orders agree) · 2 models x 2 orders, FLAG needs >= 3 counted yes votes and
no ABSTAIN, else UNSURE (report only). Ollama over HTTP, temperature 0, responses cached
under eval/.cache/vlm/. Never imports engine/*.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from eval.grader import forensics, soft

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODELS = ("qwen2.5vl:7b", "gemma3:4b")
CACHE = forensics.ROOT / "eval" / ".cache" / "vlm"
THUMB = 768                # long side of every image sent, margins included (<= 1024; smaller = faster)
MARGIN = 56                # label strip on the left and right of an overlay
CROP_PAD, CROP_MIN = 0.2, 448
MAX_REL = 2                # Q-rel questions per pair (largest contact drop first): runtime
RECALL_MIN, PICS_TOL = 0.8, 1
FLAG_VOTES = 3
TIMEOUT = 300
QTYPES = ("wrong", "rel")
# Measured by eval/grader/vlm_validate.py (2026-10-11, 21 cases on 8 bases, 75 min budget, machine shared with
# another job: ~244 s/pair). precision >= 0.9 -> enabled. Both OFF:
#   wrong: 2 ensemble flags, precision 0.50 (1/2), recall 0.08 (1/12), swap-consistency 0.94; single-vote yes
#          precision qwen 0.66 -> qwen says "yes" on harmless 3% stretches in both orders.
#   rel:   0 flags, recall 0.00 (0/3), swap-consistency 1.00; single-vote yes precision qwen 0.19.
#   abstain (picture-count probe off by > 1): qwen 29%, gemma 90% -> the 3-of-4 vote is almost never reachable.
ENABLED = {"wrong": False, "rel": False}
COLORS = ((230, 25, 75), (60, 140, 60), (0, 90, 200), (245, 130, 48), (145, 30, 180), (0, 150, 150),
          (200, 0, 140), (120, 90, 0), (0, 0, 128), (128, 0, 0))

PROBE = {"type": "object", "properties": {"texts": {"type": "array", "items": {"type": "string"}},
                                          "pictures": {"type": "integer"}}, "required": ["texts", "pictures"]}
ANSWER = {"type": "object", "properties": {
    "observations": {"type": "string"}, "answer": {"type": "string", "enum": ["yes", "no", "unsure"]},
    "confidence": {"type": "integer", "minimum": 1, "maximum": 3},
    "boxes": {"type": "array", "items": {"type": "integer"}}},
    "required": ["observations", "answer", "confidence", "boxes"]}
NOTE = ("Coloured outlines and the numbers in the side strips are annotations added for reference, not part of "
        "the design. ")
WRONG = ("{a} Box legend: {legend}.\nWould a professional designer say something is OBVIOUSLY broken in the "
         "RESIZED version compared with the original: text overlapping other text, text hidden under a picture or "
         "shape, a label pulled out of its button, an element shrunk to a speck, a squashed or stretched picture, "
         "unreadably tiny text? Moving, rescaling, re-cropping and re-arranging elements to fit the new canvas is "
         "expected and NOT broken. First write your observations (at most 3 short sentences), then answer yes, no "
         "or unsure, your confidence 1-3, and the box numbers of the broken elements (empty list if none).")
REL = ("{a} In the original, box {i} ({di}) sits {how} box {j} ({dj}).\nIn the RESIZED version, is that "
       "relationship clearly broken, with box {i} now detached from or outside box {j}? First write your "
       "observations (at most 3 short sentences), then answer yes, no or unsure, your confidence 1-3, and the "
       "box numbers involved.")
ORDER = {"AB": "Image 1 is the ORIGINAL design, image 2 the RESIZED version of it for a new canvas. ",
         "BA": "Image 1 is the RESIZED version of a design for a new canvas, image 2 the ORIGINAL design. "}


class Unreachable(RuntimeError):
    pass


def _ask(model: str, prompt: str, images: list[bytes], schema: dict) -> dict:
    """One Ollama chat call (images first, then the question). Cached on disk."""
    h = hashlib.sha256(json.dumps([model, prompt, schema]).encode())
    for b in images:
        h.update(b)
    cp = CACHE / f"{h.hexdigest()}.json"
    if cp.is_file():
        return json.loads(cp.read_text())
    body = {"model": model, "stream": False, "format": schema,
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 400},
            "messages": [{"role": "user", "content": prompt,
                          "images": [base64.b64encode(b).decode() for b in images]}]}
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            content = json.loads(r.read())["message"]["content"]
    except (urllib.error.URLError, ConnectionError, TimeoutError) as ex:
        raise Unreachable(f"ollama unreachable at {OLLAMA_URL}: {ex}") from ex
    ans = json.loads(content)      # format=schema: malformed JSON is a real error, surfaced to the caller
    CACHE.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(ans))
    return ans


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _fit(img: Image.Image, long_side: int, short_min: int = 0) -> Image.Image:
    w, h = img.size
    s = min(long_side / max(w, h), max(1.0, short_min / min(w, h)) if short_min else 1.0)
    return img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS) if s != 1 else img.copy()


def overlay(img: Image.Image, boxes: dict) -> Image.Image:
    """Copy of img with a thin numbered outline per box {num: (x0,y0,x1,y1) in img px}. Numbers sit in
    side strips (nearest side, stacked by y) with a leader line, so they never cover content."""
    w, h = img.size
    canvas = Image.new("RGB", (w + 2 * MARGIN, h), (238, 238, 238))
    canvas.paste(img, (MARGIN, 0))
    d = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=15)
    step = max(10, min(20, h // max(1, len(boxes))))
    next_y = {"L": 0, "R": 0}
    for n, (x0, y0, x1, y1) in sorted(boxes.items(), key=lambda kv: kv[1][1]):
        c = COLORS[(n - 1) % len(COLORS)]
        x0, x1 = max(0, x0) + MARGIN, min(w - 1, x1) + MARGIN
        y0, y1 = max(0, y0), min(h - 1, y1)
        d.rectangle((x0, y0, x1, y1), outline=c, width=2)
        side = "L" if (x0 + x1) / 2 < MARGIN + w / 2 else "R"
        ly = min(max(next_y[side], int(y0)), h - step)
        next_y[side] = ly + step
        lx = 4 if side == "L" else w + MARGIN + 6
        d.line(((MARGIN if side == "L" else w + MARGIN, ly + step // 2), (x0 if side == "L" else x1, y0)), fill=c)
        d.text((lx, ly), str(n), fill=c, font=font)
    return canvas


def _numbers(src: dict, out: dict) -> dict:
    """id -> box number (output list order), skipping full-canvas backgrounds."""
    W, H = out["canvas"]["width"], out["canvas"]["height"]
    keep = [e for e in out["elements"] if e["width"] * e["height"] < soft.BG_COVER * W * H]
    return {e["id"]: i + 1 for i, e in enumerate(keep)}


def _desc(e: dict) -> str:
    c = str(e.get("content") or e.get("asset_id") or e.get("shape_type") or "")[:20]
    return f'{e["type"]} "{c}"' if c else e["type"]


def _boxes(design: dict, nums: dict, scale: float, ids=None, origin=(0, 0)) -> dict:
    ox, oy = origin
    return {nums[e["id"]]: ((e["x"] - ox) * scale, (e["y"] - oy) * scale, (e["x"] + e["width"] - ox) * scale,
                            (e["y"] + e["height"] - oy) * scale)
            for e in design["elements"] if e["id"] in nums and (ids is None or e["id"] in ids)}


def frame(render: np.ndarray, design: dict, nums: dict) -> Image.Image:
    """Full-frame overlay, long side incl. margins <= THUMB."""
    img = Image.fromarray(render)
    img = _fit(img, THUMB - 2 * MARGIN)
    return overlay(img, _boxes(design, nums, img.size[0] / design["canvas"]["width"]))


def crop(render: np.ndarray, design: dict, nums: dict, ids) -> Image.Image:
    """Union bbox of ids + 20% margin, upscaled so the short side >= CROP_MIN (long side capped)."""
    els = [e for e in design["elements"] if e["id"] in ids]
    W = design["canvas"]["width"]
    x0, y0 = min(e["x"] for e in els), min(e["y"] for e in els)
    x1, y1 = max(e["x"] + e["width"] for e in els), max(e["y"] + e["height"] for e in els)
    mx, my = CROP_PAD * (x1 - x0), CROP_PAD * (y1 - y0)
    x0, y0 = max(0, x0 - mx), max(0, y0 - my)
    x1, y1 = min(W, x1 + mx), min(design["canvas"]["height"], y1 + my)
    s = render.shape[1] / W
    img = Image.fromarray(render[int(y0 * s):max(int(y0 * s) + 1, int(y1 * s)),
                                 int(x0 * s):max(int(x0 * s) + 1, int(x1 * s))])
    img = _fit(img, THUMB - 2 * MARGIN, CROP_MIN)
    return overlay(img, _boxes(design, nums, img.size[0] / (x1 - x0), ids, (x0, y0)))


def _words(s: str) -> list:
    return re.findall(r"[a-z0-9]+", s.lower())


def probe_ok(ans: dict, design: dict) -> tuple[bool, str]:
    """Text recall (fuzzy, word level) >= 0.8 and picture count within +-1 of the JSON truth."""
    truth = [w for e in design["elements"] if e["type"] == "text" for w in _words(str(e.get("content", "")))]
    seen = set(_words(" ".join(map(str, ans.get("texts", [])))))
    rec = sum(bool(difflib.get_close_matches(w, seen, 1, 0.8)) for w in truth) / max(1, len(truth))
    W, H = design["canvas"]["width"], design["canvas"]["height"]
    pics = sum(e["type"] == "image" and e["width"] * e["height"] < 0.5 * W * H for e in design["elements"])
    said = ans.get("pictures", -99)
    ok = rec >= RECALL_MIN and isinstance(said, int) and abs(said - pics) <= PICS_TOL
    return ok, f"text recall {rec:.2f}, pictures {said} vs {pics}"


def _rels(src, out, fs, fo) -> list:
    """Source containment/contact pairs (soft.relations' rule), largest drop first."""
    si, oi = soft._canvas_ink(fs), soft._canvas_ink(fo)
    bg = soft._backgrounds(fs) | soft._backgrounds(fo)
    faint = {e["id"] for e in src["elements"] if soft._faint(e)}
    ids = [k for k in si if k not in bg and k in oi and si[k].sum()]
    rels = []
    for a in ids:
        for b in ids:
            if a == b or b in faint or si[a].sum() > si[b].sum():
                continue
            was = soft._frac(si[a], si[b])
            if was >= soft.CONTACT:
                now = soft._frac(oi[a], oi[b])
                broken = now < 0.75 if was >= soft.CONTAIN else now < 0.35 * was
                rels.append({"a": a, "b": b, "was": round(float(was), 3), "now": round(float(now), 3),
                             "broken": bool(broken)})
    return sorted(rels, key=lambda r: r["now"] / r["was"])[:MAX_REL]


def _questions(src, out, fs, fo, qtypes, nums, rs, ro) -> list:
    qs = []
    se, oe = {e["id"]: e for e in src["elements"]}, {e["id"]: e for e in out["elements"]}
    if "wrong" in qtypes:
        legend = "; ".join(f"{n} = {_desc(oe[k])}" for k, n in nums.items())
        qs.append({"qtype": "wrong", "ids": [], "text": lambda a, lg=legend: WRONG.format(a=a, legend=lg),
                   "imgs": (_png(frame(rs, src, nums)), _png(frame(ro, out, nums)))})
    if "rel" in qtypes:
        for r in _rels(src, out, fs, fo):
            a, b = r["a"], r["b"]
            if a not in nums or b not in nums:
                continue
            how = "inside" if r["was"] >= soft.CONTAIN else "on / touching"
            text = (lambda x, a=a, b=b, how=how: REL.format(a=x, i=nums[a], di=_desc(se[a]), how=how, j=nums[b],
                                                         dj=_desc(se[b])))
            qs.append({"qtype": "rel", "ids": [a, b], "text": text, "evidence": r,
                       "imgs": (_png(crop(rs, src, nums, {a, b})), _png(crop(ro, out, nums, {a, b})))})
    return qs


def _vote(raw: dict, q: dict, valid: set) -> tuple[str, list]:
    """Normalise one answer; Q-wrong 'yes' counts only at confidence 3 naming valid boxes."""
    a = raw.get("answer", "unsure")
    named = [b for b in raw.get("boxes", []) if isinstance(b, int) and b in valid]
    if a == "yes" and q["qtype"] == "wrong" and (raw.get("confidence") != 3 or not named):
        a = "unsure"
    return a, named


def _combine(q: dict, votes: dict, abstain: dict, inv: dict) -> dict:
    """votes: model -> {"AB": (answer, boxes), "BA": ...}. A model's votes count only if both orders agree."""
    counted = [v for m in votes.values() if len(m) == 2 and m["AB"][0] == m["BA"][0] for v in m.values()]
    yes = [v for v in counted if v[0] == "yes"]
    swap = [len(m) == 2 and m["AB"][0] == m["BA"][0] for m in votes.values()]
    flagged = len(yes) >= FLAG_VOTES and not abstain
    any_yes = any(v[0] == "yes" for m in votes.values() for v in m.values())
    ids = q["ids"]
    if q["qtype"] == "wrong":
        named = [b for v in yes for b in v[1]]
        ids = [inv[b] for b in sorted(set(named)) if named.count(b) >= 2] or [inv[b] for b in sorted(set(named))]
    return {"qtype": q["qtype"], "ids": ids, "outcome": "flag" if flagged else ("unsure" if any_yes else "no"),
            "yes": len(yes), "swap_consistent": swap, "evidence": q.get("evidence", {}),
            "votes": {m: {o: v[0] for o, v in mv.items()} for m, mv in votes.items()}}


def review(src: dict, out: dict, target: dict, fs=None, fo=None, models=MODELS, qtypes=None) -> dict:
    """Ask the enabled question types (qtypes=None) with every model; see the module docstring."""
    t = time.time()
    qtypes = [q for q in QTYPES if ENABLED[q]] if qtypes is None else list(qtypes)
    res = {"flags": [], "unsure": [], "abstain": {}, "queries": 0, "seconds": 0.0, "questions": []}
    if not qtypes:
        return res
    nums = _numbers(src, out)
    inv = {n: k for k, n in nums.items()}
    rs, ro = forensics.render_png(src), forensics.render_png(out)
    if "rel" in qtypes and (fs is None or fo is None):
        fs, fo = forensics.analyze(src), forensics.analyze(out)
    qs = _questions(src, out, fs, fo, qtypes, nums, rs, ro)
    plain = _png(_fit(Image.fromarray(rs), THUMB))
    votes = [dict() for _ in qs]
    for m in models:        # one model at a time: no model swapping in Ollama memory
        try:
            res["queries"] += 1
            ok, why = probe_ok(_ask(m, "List every separate block of text you can read in this image, verbatim, "
                                       "and count the pictures (photos, product shots, logos, illustrations; not "
                                       "plain colour shapes or the background).", [plain], PROBE), src)
            if not ok:
                res["abstain"][m] = f"competence probe failed: {why}"
                continue
            for q, v in zip(qs, votes):
                v[m] = {}
                for o in ("AB", "BA"):
                    imgs = list(q["imgs"]) if o == "AB" else list(q["imgs"][::-1])
                    res["queries"] += 1
                    v[m][o] = _vote(_ask(m, q["text"](NOTE + ORDER[o]), imgs, ANSWER), q, set(inv))
        except Unreachable as ex:
            res["abstain"].update({x: str(ex) for x in models if x not in res["abstain"]})
            break
    for q, v in zip(qs, votes):
        r = _combine(q, {m: x for m, x in v.items() if m not in res["abstain"]}, res["abstain"], inv)
        res["questions"].append(r)
        if r["outcome"] == "no":
            continue
        msg = (f"vision: a designer would see something broken in {', '.join(r['ids']) or 'the layout'}"
               if r["qtype"] == "wrong" else f"vision: {r['ids'][0]} looks detached from {r['ids'][1]}")
        f = soft._flag("vision", "major" if r["outcome"] == "flag" else "info", msg, r["ids"], qtype=r["qtype"],
                       yes_votes=r["yes"], votes=r["votes"])
        res["flags" if r["outcome"] == "flag" else "unsure"].append(f)
    res["seconds"] = round(time.time() - t, 1)
    return res
