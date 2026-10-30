"""Any-input fuzz with a floor: random schema-valid tasks from a seed, run through an engine copy.

    python -m eval.arena.fuzz_any --base CHAMPION_ROOT --work DIR --seeds 0:400 --render-seeds 400:460
    python -m eval.arena.fuzz_any --selftest

Per seed: 1-60 elements of every type (the schema forbids 0), awkward text (empty, one char, unbroken
words, 2,000-char paragraphs, emoji/CJK/Arabic/zero-width), font sizes 1-400, images on brand assets,
missing names and new fuzz assets (ratios 1:20..20:1, opaque/transparent; test data only), rotations,
opacity, off-canvas and zero-size boxes, duplicate z_index, nested-looking group ids, source canvases
50..8000 px, targets 50..8000 px at 1:20..20:1 (even seeds walk the 25-entry catalogue instead).
Checks: no crash, no hidden scale-fit fallback, no 120 s timeout, eval.arena.strict clean (failures a
non-cover image's aspect makes unavoidable are tagged inherent), byte-identical rerun under another
PYTHONHASHSEED. Render seeds also run the official renderer and a floor: B's soft collisions and
readability rows for the engine vs a uniform scale-to-fit letterbox of the same source.
Writes WORK/fuzz_any.md, WORK/fuzz_results.jsonl and shrunk repro tasks in WORK/fuzz_repros/.
Never imports engine/*; the engine runs through the scratchpad run_engine.py in a subprocess.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import jsonschema
from PIL import Image, ImageDraw

from eval.arena import strict

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = Path("/private/tmp/claude-501/-Users-karthikvattem-projects-adobe-hackathon/"
               "483c8251-269f-47a3-9653-b3f7cc634cf5/scratchpad")
RUNNER = SCRATCH / "run_engine.py"
CATALOGUE = json.loads((ROOT / "eval" / "targets.json").read_text())
BRAND = ["solstice_bg_gradient.png", "solstice_logo_lockup.png", "solstice_bottle.png"]
MISSING = ["solstice_holiday_badge_2025.png", "missing.png", "", "sub/dir/x.png", "../../etc/passwd",
           "UPPER.PNG", "no_extension", "space name.png"]
TIMEOUT_S = 120
FLOOR_LOSS = 0.5            # penalty units (major 0.5, minor 0.2): >= one extra major AND >= 25% more than letterbox
FALLBACK = re.compile(r"resize failed|scale-fit failed")
BUDGET = re.compile(r"budget|timeout|timed out|cap of \d+ reached", re.I)

WORDS = ("golden hour botanical sparkling limited edition small batch infusion wild honey blood orange "
         "rosemary crafted exclusively for gatherings sunset taste lingers award winning new").split()
TEXTS = [
    lambda r: "", lambda r: " ", lambda r: r.choice("AxW.!1"), lambda r: "\n",
    lambda r: "".join(r.choice("abcdefghijklmnopqrstuvwxyzABCDEFG") for _ in range(r.randint(40, 700))),
    lambda r: " ".join(r.choice(WORDS) for _ in range(400))[:2000],
    lambda r: " ".join(r.choice(WORDS) for _ in range(r.randint(2, 12))),
    lambda r: " ".join(r.choice(WORDS) for _ in range(r.randint(2, 6))).upper(),
    lambda r: "\U0001F389\U0001F525✨ Sale 50% off \U0001F680\U0001F468‍\U0001F469‍\U0001F467",
    lambda r: "Crème brûlée à la façon de Zoë — ½ prix © ™",
    lambda r: "夏季限定セール 新品上市 여름 세일" * r.randint(1, 20),
    lambda r: "تخفيضات الصيف שלום",
    lambda r: "Line1\nLine2\n\n\nLine5\tTabbed\r\nCRLF",
    lambda r: "a​b‍c﻿d Z̷a̶l̴g̸o̵",
    lambda r: "&<>\"' https://example.com/a/very/long/path?with=query&and=more#frag",
]
FONTS = ["Solstice Sans", "Solstice Serif", "Solstice Sans", "Solstice Serif", "Comic Sans MS", "", "solstice sans"]
GROUPS = ["g1", "g1/inner", "g1/inner/deep", "g1.a", "card", "card", "équipe", "0"]


# ---- generation ------------------------------------------------------------------------------------------

def _logu(r, lo, hi):
    return math.exp(r.uniform(math.log(lo), math.log(hi)))


def _dims(r):
    """Integer canvas 50..8000 per side, aspect 1:20..20:1, long side log-uniform."""
    ar, long = _logu(r, 1 / 20, 20), _logu(r, 50, 8000)
    w, h = (long, long / ar) if ar >= 1 else (long * ar, long)
    return max(50, min(8000, round(w))), max(50, min(8000, round(h)))


def _color(r):
    if r.random() < 0.1:
        return "#%06x" % r.randrange(1 << 24)
    return f"rgba({r.randrange(256)},{r.randrange(256)},{r.randrange(256)},{round(r.random(), 2)})"


def make_assets(d: Path) -> list[tuple[str, int, int]]:
    """Deterministic pool of fuzz assets (ratios 1:20..20:1, opaque/transparent/empty) written into d."""
    r, out = random.Random(20261030), []
    ratios = [1 / 20, 1 / 8, 1 / 3, 2 / 3, 1, 1.5, 3, 8, 20, 1, 1 / 20, 20, 1]
    for i, ar in enumerate(ratios):
        long = [40, 160, 600, 1200, 2000][i % 5] if i < 12 else 1      # last one: a 1x1 pixel
        w, h = (long, max(1, round(long / ar))) if ar >= 1 else (max(1, round(long * ar)), long)
        mode = ["opaque", "alpha", "empty", "opaque_jpg"][i % 4]
        name = f"fz_{i:02d}_{w}x{h}_{mode}." + ("jpg" if mode.endswith("jpg") else "png")
        if mode == "alpha":
            im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(im).ellipse([w * 0.1, h * 0.1, w * 0.9, h * 0.9], fill=(200, 60, 40, 255))
        elif mode == "empty":
            im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        else:
            im = Image.new("RGB", (w, h), tuple(r.randrange(256) for _ in range(3)))
            ImageDraw.Draw(im).rectangle([0, 0, w // 2, h // 2], fill=(20, 20, 20))
        im.save(d / name, quality=90) if name.endswith("jpg") else im.save(d / name)
        out.append((name, w, h))
    return out


def _box(r, sW, sH):
    w, h = sW * _logu(r, 0.005, 1.0), sH * _logu(r, 0.005, 1.0)
    x, y = r.uniform(0, max(0, sW - w)), r.uniform(0, max(0, sH - h))
    m = r.random()
    if m < 0.07:                                      # partly off-canvas
        x, y = x + r.choice([-1, 1]) * w * 0.6, y - h * r.random()
    elif m < 0.12:                                    # wholly off-canvas
        x, y = r.choice([-w - r.uniform(1, sW), sW + r.uniform(1, sW)]), y
    elif m < 0.17:                                    # zero-size on one or both axes
        w, h = r.choice([(0, h), (w, 0), (0, 0)])
    elif m < 0.20:                                    # far bigger than the canvas
        w, h, x, y = sW * 3, sH * 2, -sW, -sH / 2
    nd = r.choice([0, 0, 1, 3])
    return [round(v, nd) if nd else round(v) for v in (x, y, w, h)]


def gen_task(seed: int, fuzz_assets: list) -> dict:
    r = random.Random(seed)
    sW, sH = _dims(r)
    if seed % 2 == 0:
        t = CATALOGUE[(seed // 2) % len(CATALOGUE)]
        W, H = t["width"], t["height"]
    else:
        W, H = _dims(r)
    n = 1 + int(59 * r.random() ** 2)
    els = []
    for i in range(n):
        typ = r.choices(["text", "image", "shape"], [4, 3, 3])[0]
        x, y, w, h = _box(r, sW, sH)
        eid = r.choice([f"e{i}", f"e{i}", f"élément {i}", str(i), f"{typ}_{i}"])
        e = {"id": eid, "type": typ, "x": x, "y": y, "width": w, "height": h}
        if r.random() < 0.8:
            e["z_index"] = r.choice([r.randint(0, 4), r.randint(-10 ** 6, 10 ** 6)]) if r.random() < 0.9 else 0
        if r.random() < 0.25:
            e["rotation"] = r.choice([round(r.uniform(-180, 180), 2), 90, -90, 180, 0, 45])
        if r.random() < 0.3:
            e["opacity"] = r.choice([round(r.random(), 3), 0, 1])
        if r.random() < 0.3:
            e["group_id"] = r.choice(GROUPS + [None])
        if r.random() < 0.05:
            e |= {"name": f"layer {i}", "locked": True}
        if typ == "text":
            e |= {"content": r.choice(TEXTS)(r), "font_family": r.choice(FONTS),
                  "font_size": round(_logu(r, 1, 400), r.choice([0, 1])) or 1, "color": _color(r)}
            for k, vals in (("font_weight", ["regular", "bold"]), ("font_style", ["normal", "italic"]),
                            ("text_align", ["left", "center", "right"]), ("line_height", [0.5, 1, 1.2, 3]),
                            ("letter_spacing", [-5, 0, 2, 20]), ("max_lines", [None, 1, 3])):
                if r.random() < 0.4:
                    e[k] = r.choice(vals)
            if r.random() < 0.1:
                L = len(e["content"]) + 5
                e["style_runs"] = [{"start": a, "end": a + r.randint(0, 10), "color": _color(r)}
                                   for a in sorted(r.randrange(L) for _ in range(r.randint(1, 3)))]
        elif typ == "image":
            p = r.random()
            if p < 0.4:
                e["asset_id"] = r.choice(BRAND)
            elif p < 0.6:
                e["asset_id"] = r.choice(MISSING)
            else:
                a, aw, ah = r.choice(fuzz_assets)
                e["asset_id"] = a
                if r.random() < 0.5 and w and h:          # box matches the asset's own aspect
                    e["height"] = round(w * ah / aw, 2)
            if r.random() < 0.85:
                e["fit"] = r.choice(["contain", "cover", "fill"])
        else:
            st = r.choice(["rectangle", "ellipse", "line"])
            e |= {"shape_type": st, "fill_color": _color(r)}
            if st == "line":
                e["height"] = r.choice([0, 1, 2, e["height"]])
            if r.random() < 0.4:
                e |= {"stroke_color": r.choice([None, _color(r)]), "stroke_width": r.choice([0, 1, 5, 40])}
            if r.random() < 0.3:
                e["corner_radius"] = r.choice([0, 8, 500])
        els.append(e)
    ids = collections.Counter(e["id"] for e in els)
    for i, e in enumerate(els):                      # ids must stay unique
        if ids[e["id"]] > 1:
            e["id"] = f"{e['id']}#{i}"
    return {"seed": seed, "source": {"canvas": {"width": sW, "height": sH}, "elements": els},
            "target_canvas": {"width": W, "height": H}}


# ---- checks ----------------------------------------------------------------------------------------------

def inherent(src_el: dict, target: dict) -> bool:
    """A non-cover image keeps its aspect, so its largest in-canvas size (rotated AABB) may be < 2 px."""
    if src_el.get("type") != "image" or src_el.get("fit") == "cover":
        return False
    sw, sh = src_el["width"], src_el["height"]
    if min(sw, sh) <= 0:
        return False
    a = math.radians(src_el.get("rotation") or 0)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    k = min(target["width"] / (sw * c + sh * s), target["height"] / (sw * s + sh * c))
    return k * min(sw, sh) < strict.MIN_PX * 1.01


def signature(rule: dict, src_el: dict | None, src: dict, tgt: dict) -> str:
    """Rule + element type, plus whether a uniform scale-to-fit would hit the same rule (context, not an excuse)."""
    e = src_el or {}
    tags = [e.get("type", "?")]
    if e.get("rotation"):
        tags.append("rotated")
    if e.get("type") == "image" and not (strict.ASSETS / str(e.get("asset_id"))).is_file():
        tags.append("missing-asset")
    k = min(tgt["width"] / src["canvas"]["width"], tgt["height"] / src["canvas"]["height"])
    if rule["id"] == "S6" and e:
        q = rule["msg"].split()[0]                    # "width collapsed ..." / "height collapsed ..."
        tags.append("uniform-fit also <2px" if e[q] * k < strict.MIN_PX
                    else "engine-made collapse")
    if rule["id"] in ("S4a", "S4b") and e:
        sw, sh = src["canvas"]["width"], src["canvas"]["height"]
        tags.append("source already off-canvas" if strict._outside(strict.hard._rot_aabb(e), sw, sh) else "in-canvas source")
    return f"strict {strict.kind(rule)} [{' '.join(tags)}]"


def crash_sig(trace: str, err: str) -> str:
    frames = re.findall(r'File "([^"]+)", line (\d+), in (\S+)', trace or "")
    eng = [f for f in frames if "/engine/" in f[0]] or frames
    where = f"{Path(eng[-1][0]).name}:{eng[-1][2]}" if eng else "?"
    return f"crash {err.split(':')[0]} at {where}"


def render_sig(err: str, src: dict, assets: Path) -> str:
    """The official renderer failed on the engine's JSON; say whether it fails on the source too."""
    from renderer.converter.convert import render_design_to_png
    msg = " ".join(err.split("\n")[:2])[:90]
    try:
        with tempfile.TemporaryDirectory(dir=SCRATCH / "guards" / "tmp") as td:
            render_design_to_png(src, str(assets), str(Path(td) / "s.png"))
        return f"render crash: {msg}"
    except Exception:  # noqa: BLE001 - only whether it fails matters
        return f"render crash: {msg} [source also unrenderable]"


def run_engine(engine: Path, task: dict, render: bool, hashseed: str, timeout=TIMEOUT_S) -> dict:
    with tempfile.TemporaryDirectory(dir=SCRATCH / "guards" / "tmp") as td:
        td = Path(td)
        (td / "t").mkdir()
        (td / "t" / "task.json").write_text(json.dumps(task))
        cmd = [sys.executable, str(RUNNER), str(engine), str(td / "t"), str(td / "o")] + ([] if render else ["--no-render"])
        env = {**os.environ, "CHAMELEON_LLM": "off", "PYTHONHASHSEED": hashseed, "PYTHONDONTWRITEBYTECODE": "1"}
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=engine, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "secs": round(time.time() - t0, 1)}
        res = {"status": "ok", "secs": round(time.time() - t0, 1), "log": (p.stdout + p.stderr)[-3000:]}
        rec = json.loads((td / "o" / "results.jsonl").read_text().splitlines()[-1]) if (td / "o" / "results.jsonl").is_file() else {}
        if (td / "o" / "task.json").is_file():
            res["out_text"] = (td / "o" / "task.json").read_text()
        if p.returncode or "error" in rec:
            res |= {"status": "crash", "error": rec.get("error") or p.stderr[-300:], "trace": rec.get("trace") or p.stderr[-800:]}
        if render and res["status"] == "ok" and not (td / "o" / "task.png").is_file():
            res |= {"status": "crash", "error": "NoPNG: renderer wrote nothing", "trace": ""}
        return res


def failures(engine: Path, task: dict, render: bool) -> tuple[list[str], dict]:
    """Signatures of every failure on one run (first PYTHONHASHSEED only) plus the run record."""
    src, tgt = task["source"], task["target_canvas"]
    res, sigs = run_engine(engine, task, render, "0"), []
    if res["status"] == "timeout":                   # the box is shared and loaded: tell slow from hung
        res = run_engine(engine, task, render, "0", timeout=5 * TIMEOUT_S)
        if res["status"] == "timeout":
            return [f"timeout (no output in {5 * TIMEOUT_S} s)"], res
        sigs.append(f"timeout >{TIMEOUT_S} s (finished on a {5 * TIMEOUT_S} s rerun)")
    if res["status"] == "crash":                     # out_text present = resize worked, the render failed
        sigs.append(render_sig(res.get("error", ""), src, strict.ASSETS) if "out_text" in res
                    else crash_sig(res.get("trace", ""), res.get("error", "")))
    if FALLBACK.search(res.get("log", "")):
        sigs.append("hidden crash: resize fell back to scale-fit")
    if "out_text" in res:
        out = json.loads(res["out_text"])
        by = {e["id"]: e for e in src["elements"]}
        for f in strict.check(src, out, tgt)["fails"]:
            el = by.get(f.get("element"))
            sigs.append(signature(f, el, src, tgt) + (" INHERENT" if el and f["id"] in ("S5", "S6") and inherent(el, tgt) else ""))
    return sorted(set(sigs)), res


# ---- floor -----------------------------------------------------------------------------------------------

def letterbox(src: dict, tgt: dict) -> dict:
    sW, sH, W, H = src["canvas"]["width"], src["canvas"]["height"], tgt["width"], tgt["height"]
    s = min(W / sW, H / sH)
    ox, oy = (W - sW * s) / 2, (H - sH * s) / 2
    els = []
    for e in src["elements"]:
        d = {**e, "x": e["x"] * s + ox, "y": e["y"] * s + oy, "width": e["width"] * s, "height": e["height"] * s}
        if e["type"] == "text":
            d["font_size"] = max(1.0, e["font_size"] * s)
        els.append(d)
    return {"canvas": {"width": W, "height": H}, "elements": els}


def floor(task: dict, out: dict) -> dict:
    from eval.grader import __main__ as grader, forensics
    cache = SCRATCH / "guards" / "grader_cache"      # keep the grader's render cache out of the repo
    forensics._cache_path = lambda d, tag: cache / (hashlib.sha256((json.dumps(d, sort_keys=True) + tag).encode()).hexdigest()[:24] + ".npz")
    src, tgt, row = task["source"], task["target_canvas"], {}
    zero = {e["id"] for e in src["elements"] if min(e["width"], e["height"]) <= 0}
    keep = lambda d: {**d, "elements": [e for e in d["elements"] if e["id"] not in zero]}   # noqa: E731
    lb, src, out = letterbox(keep(src), tgt), keep(src), keep(out)
    row["dropped_zero_size"] = len(zero)
    if not src["elements"]:
        return row | {"loss": False, "engine": {"cr": None}, "letterbox": {"cr": None}}
    for arm, design in (("engine", out), ("letterbox", lb)):
        g = grader.grade(src, design, tgt, assets=strict.ASSETS)
        c = g["criteria"]
        fl = [f for k in ("collisions", "readability") for f in c.get(k, {}).get("flags", [])]
        # the soft score clamps at 0 once a busy design has 2 majors; the raw penalty keeps counting
        row[arm] = {"cr": round(sum(c[k]["score"] for k in ("collisions", "readability")), 3) if c else None,
                    "pen": round(sum(0.5 if f["severity"] == "major" else 0.2 for f in fl), 2) if c else None,
                    "majors": collections.Counter(f["criterion"] for f in fl if f["severity"] == "major"),
                    "examples": [f["msg"] for f in fl if f["severity"] == "major"][:3]}
    e, l = row["engine"]["pen"], row["letterbox"]["pen"]
    row["loss"] = e is not None and l is not None and e - l >= max(FLOOR_LOSS, 0.25 * l)
    return row


# ---- per-seed driver -------------------------------------------------------------------------------------

def one_seed(engine: Path, seed: int, render: bool, fuzz_assets: list) -> dict:
    task = gen_task(seed, fuzz_assets)
    rec = {"seed": seed, "n": len(task["source"]["elements"]), "src": list(task["source"]["canvas"].values()),
           "tgt": list(task["target_canvas"].values()), "render": render}
    sigs, res = failures(engine, task, render)
    rec |= {"sigs": sigs, "secs": res.get("secs"), "budget_msg": bool(BUDGET.search(res.get("log", "")))}
    if "out_text" in res:
        again = run_engine(engine, task, False, "12345")
        if again.get("status") == "timeout":
            rec["sigs"].append("timeout (rerun)")
        elif again.get("out_text") != res["out_text"]:
            rec["sigs"].append("nondeterministic" + (" (budget hit)" if rec["budget_msg"] or BUDGET.search(again.get("log", "")) else ""))
        if render:
            try:
                rec["floor"] = floor(task, json.loads(res["out_text"]))
            except Exception as ex:  # noqa: BLE001 - a grader failure is reported, not fatal
                rec["floor"] = {"error": f"{type(ex).__name__}: {ex}"[:200]}
    return rec


def shrink(engine: Path, task: dict, sig: str, budget: int = 16) -> dict:
    """Greedy ddmin over elements: drop chunks while `sig` still reproduces. ponytail: budget-capped."""
    els, chunk, tries = list(task["source"]["elements"]), max(1, len(task["source"]["elements"]) // 2), 0
    while chunk >= 1 and tries < budget and len(els) > 1:
        i, progressed = 0, False
        while i < len(els) and tries < budget and len(els) > 1:
            cand = els[:i] + els[i + chunk:]
            if not cand:
                break
            t = {**task, "source": {**task["source"], "elements": cand}}
            tries += 1
            if sig in failures(engine, t, sig.startswith("render"))[0]:
                els, progressed = cand, True
            else:
                i += chunk
        chunk = chunk if progressed and chunk == 1 else chunk // 2
    return {**task, "source": {**task["source"], "elements": els}, "shrunk_from": len(task["source"]["elements"])}


def _shrink_job(args):
    engine, seed, sig, budget, assets, path = args
    strict.ASSETS = engine / "brand_kit" / "assets"
    task = gen_task(seed, assets)
    if budget and not sig.startswith(("timeout", "nondeterministic", "harness")):
        try:
            task = shrink(engine, task, sig, budget)
        except Exception as ex:  # noqa: BLE001 - keep the unshrunk task
            task["shrink_error"] = f"{type(ex).__name__}: {ex}"[:200]
    path.write_text(json.dumps({**task, "signature": sig}, indent=1, ensure_ascii=False))


def setup_engine(base: Path, engine: Path) -> list:
    if not engine.is_dir():
        shutil.copytree(base, engine, ignore=shutil.ignore_patterns("__pycache__", ".cache"))
    (SCRATCH / "guards" / "tmp").mkdir(parents=True, exist_ok=True)
    assets = make_assets(engine / "brand_kit" / "assets")
    strict.ASSETS = engine / "brand_kit" / "assets"   # strict's missing-asset test must see the fuzz assets
    return assets


def _worker(args):
    engine, seed, render, assets = args
    strict.ASSETS = engine / "brand_kit" / "assets"
    try:
        return one_seed(engine, seed, render, assets)
    except Exception as ex:  # noqa: BLE001 - a harness bug must not lose the sweep
        return {"seed": seed, "render": render, "sigs": [f"harness error {type(ex).__name__}: {ex}"[:200]]}


def report(recs: list, repros: dict, path: Path):
    real = lambda s: "INHERENT" not in s and "source also unrenderable" not in s     # noqa: E731
    cnt = collections.Counter(s for r in recs for s in r["sigs"])
    by_cls = collections.Counter()
    for r in recs:
        for cls in ("crash", "render crash", "hidden crash", "timeout", "strict", "nondeterministic", "harness"):
            if any(s.startswith(cls) and real(s) for s in r["sigs"]):
                by_cls[cls] += 1
        if any(not real(s) for s in r["sigs"]):
            by_cls["inherent to the source only"] += 0 if any(real(s) for s in r["sigs"]) else 1
    fl = [r for r in recs if "floor" in r]
    losses = [r for r in fl if r["floor"].get("loss")]
    pens = lambda r: tuple((r["floor"].get(k) or {}).get("pen") for k in ("engine", "letterbox"))   # noqa: E731
    lines = [f"# Any-input fuzz ({len(recs)} seeds, {sum(r['render'] for r in recs)} rendered)", "",
             "Seeds with at least one non-inherent failure, by class: "
             + (", ".join(f"{k} {v}" for k, v in by_cls.most_common()) or "none"), "",
             f"Floor: {len(losses)} of {len(fl)} rendered seeds where the collisions+readability penalty is "
             f">= max({FLOOR_LOSS}, 25%) above a uniform letterbox; engine better on "
             f"{sum(pens(r)[0] < pens(r)[1] for r in fl if None not in pens(r))}, "
             f"grader errors {sum('error' in r['floor'] for r in fl)}.", "",
             "## Failure signatures (seeds affected, first seeds, repro)", ""]
    for s, n in cnt.most_common():
        seeds = [r["seed"] for r in recs if s in r["sigs"]][:6]
        lines.append(f"- `{s}`: {n} seeds, e.g. {seeds}" + (f", repro `{repros[s]}`" if s in repros else ""))
    lines += ["", "## Floor losses", ""]
    for r in losses:
        f = r["floor"]
        lines.append(f"- seed {r['seed']} src {r['src']} -> {r['tgt']}, n={r['n']}: penalty engine {f['engine']['pen']} "
                     f"vs letterbox {f['letterbox']['pen']}; engine majors {dict(f['engine']['majors'])}, "
                     f"e.g. {f['engine']['examples'][:2]}")
    secs = sorted(r["secs"] for r in recs if r.get("secs"))
    if secs:
        lines += ["", f"Engine wall time (first run, load-dependent): median {secs[len(secs) // 2]} s, "
                      f"max {secs[-1]} s; budget messages on {sum(r.get('budget_msg', False) for r in recs)} seeds."]
    path.write_text("\n".join(lines) + "\n")


def selftest():
    import tempfile as tf
    schema = jsonschema.Draft202012Validator(strict.SCHEMA)
    with tf.TemporaryDirectory() as d:
        assets = make_assets(Path(d))
        ratios = [w / h for _, w, h in assets]
        assert min(ratios) <= 1 / 19 and max(ratios) >= 19, ratios
        for seed in range(300):
            t = gen_task(seed, assets)
            errs = list(schema.iter_errors(t["source"]))
            assert not errs, (seed, errs[0].message)
            assert len({e["id"] for e in t["source"]["elements"]}) == len(t["source"]["elements"])
            W, H = t["target_canvas"].values()
            assert 50 <= W <= 8000 and 50 <= H <= 8000 and 1 / 20.5 <= W / H <= 20.5
            assert gen_task(seed, assets) == t                      # seed-deterministic
            lb = letterbox(t["source"], t["target_canvas"])
            assert not list(schema.iter_errors(lb)), seed
    assert inherent({"type": "image", "width": 1000, "height": 10}, {"width": 50, "height": 50})
    assert not inherent({"type": "image", "width": 100, "height": 100}, {"width": 50, "height": 50})
    assert not inherent({"type": "image", "fit": "cover", "width": 1000, "height": 1}, {"width": 50, "height": 50})
    print("selftest OK: 300 generated tasks schema-valid, unique ids, targets in range, letterbox schema-valid")


def _range(s):
    a, b = s.split(":")
    return range(int(a), int(b))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=Path, default=SCRATCH / "champion_v4")
    ap.add_argument("--work", type=Path, default=SCRATCH / "guards")
    ap.add_argument("--seeds", type=_range, default=range(0, 400))
    ap.add_argument("--render-seeds", type=_range, default=range(400, 460))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--shrink", type=int, default=16, help="engine runs per distinct signature (0 = off)")
    ap.add_argument("--resume", action="store_true", help="keep WORK/fuzz_results.jsonl rows, run the rest")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    engine = a.work / "fuzz_engine"
    assets = setup_engine(a.base, engine)
    res_path = a.work / "fuzz_results.jsonl"
    recs = [json.loads(x) for x in res_path.read_text().splitlines()] if a.resume and res_path.is_file() else []
    done = {(r["seed"], r["render"]) for r in recs}
    jobs = [(engine, s, False, assets) for s in a.seeds] + [(engine, s, True, assets) for s in a.render_seeds]
    jobs = [j for j in jobs if (j[1], j[2]) not in done]
    log = open(res_path, "a" if a.resume else "w")
    with ProcessPoolExecutor(a.workers) as ex:
        for rec in ex.map(_worker, jobs):
            recs.append(rec)
            log.write(json.dumps(rec) + "\n")
            log.flush()
            print(rec["seed"], rec.get("secs"), rec["sigs"], (rec.get("floor") or {}).get("loss", ""), flush=True)
    repro_dir, repros = a.work / "fuzz_repros", {}
    repro_dir.mkdir(exist_ok=True)
    sjobs = []
    for sig in sorted({s for r in recs for s in r["sigs"]}):
        seed = next(r["seed"] for r in recs if sig in r["sigs"])
        name = re.sub(r"[^A-Za-z0-9]+", "_", sig).strip("_")[:70] + f"_seed{seed}.json"
        repros[sig] = str(repro_dir / name)
        sjobs.append((engine, seed, sig, a.shrink, assets, repro_dir / name))
    with ProcessPoolExecutor(a.workers) as ex:
        list(ex.map(_shrink_job, sjobs))
    report(recs, repros, a.work / "fuzz_any.md")
    print((a.work / "fuzz_any.md").read_text())


if __name__ == "__main__":
    main()
