"""L3 blind pairwise panel and L4 pointwise P-OFF (GRADER_DESIGN.md sections 2 L3/L4, build step 4).

Forked from eval/grader/claude_judge.py and reuses its headless `claude -p` driver (_run_claude):
subscription billing, API keys stripped, empty cwd, no tools.

    python -m eval.arena.judge pairs --a CHAMP_DIR --b CHALL_DIR --tasks TASKS --out records.jsonl [--limit N] [--workers K]
    python -m eval.arena.judge poff --renders DIR --tasks TASKS --out poff.jsonl
    python -m eval.arena.judge --selftest

Pairs: every task whose render differs between --a (champion) and --b (challenger) gets 4 calls,
conditions R (brief rubric verbatim, SOURCE shown) and D (designer prompt, no source) x 2 orders, with
the A/B letters re-randomised per call. A vote is +1 when the --b render wins, -1 when --a wins, 0 tie.
The decided sign d is +/-1 only when all 4 votes agree. Records are in the eval/arena/accept.py format;
run-level canaries (identity X vs X, corrupt.py defect vs clean) go to <out>.canaries.jsonl, and the
run is VOID when defect accuracy < 0.9 or more than 0.10 of identity pairs come out decided.
Images go in at native size (only downscaled to the vision cap); --upscale restores the old 1000 px floor.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from eval.grader.claude_judge import MAX_LONG_SIDE, MAX_PIXELS, MIN_LONG_SIDE, _run_claude
from eval.run import bucket

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "eval" / ".cache" / "arena_judge"
CANARY_DIR = ROOT / "eval" / ".cache" / "arena_canary"
MODEL, EFFORT, VERSION = "sonnet", "medium", "arena-v1"
MAX_WORKERS = 4   # quota: keep parallel claude -p calls modest
A_RUBRIC = Path.home() / "projects/adobe-hackathon/starter/grader/rubric.py"

# The four brief section 2.5 rows, verbatim from A grader/rubric.py ROW_TEXT (selftest checks they still match).
ROWS = {
    "collisions": "No unwanted collisions. Overlaps are fine when intentional (layered graphics, deliberate stacking); elements that shouldn't overlap don't, and text never sits on a busy image.",
    "readability": "Everything is readable. Text is large enough and has enough contrast; nothing is cut off.",
    "brand": "It stays on brand. Colours, fonts, and the logo survive the resize intact and in proportion.",
    "intentional": "It looks intentional. Things line up, spacing is even, the most important element still stands out, pictures keep their proportions.",
}
_ROW_LINES = "\n".join(f"- {k}: {v}" for k, v in ROWS.items())
DEFECTS = ["tiny_text", "text_cut_off", "text_on_busy_image", "low_contrast", "text_collision", "object_collision",
           "logo_small", "logo_cropped_or_distorted", "element_lost_or_speck", "hierarchy_lost", "grouping_broken",
           "misaligned", "uneven_spacing", "dead_space", "cramped_edges", "stranded_element", "picture_distorted",
           "placeholder_prominent"]
# Frozen canary pool (calibrated 2026-10-30, canary-v2). Every entry is an M-severity defect a viewer sees at the
# judge's view size; the v1 pool missed a 1.5x stretch of a drawn shape at 300x250. Changing the pool needs a
# re-calibration run (12 identity + 12 defect: accuracy >= 0.9, identity decided <= 0.1).
CANARY_POOL = "canary-v2"
CANARY_DEFECTS = ["stretch_asset_1.6", "text_on_text", "tiny_text_4px", "half_off_canvas", "headline_on_busy_image"]
CANARY_MIN_CHANGED = 0.01   # a defect render must differ from the clean one on >= 1% of pixels at view size

_PAIR_TAIL = """Different aspect ratio is expected: elements may move, scale and reflow. Judge the results, not pixel fidelity.
Write observations first. Then, for each criterion (collisions, readability, brand, intentional), say which candidate
does better: A, B or same. Then say which candidate is better overall: A, B or tie, with a confidence from 0.5 to 1.
Use same or tie only when you see no real difference."""
PAIR_R = f"""You are the automated evaluator of a design-resizing challenge. You get the SOURCE design and two
resizes of it, candidate A and candidate B, made for the same new canvas size. Compare A and B on these criteria:
{_ROW_LINES}
{_PAIR_TAIL}"""
PAIR_D = f"""You are a senior visual designer choosing between two automatic resizes, candidate A and candidate B, of the
same layered ad design for the same new canvas size. Pick the one you would rather ship, judging each the way a picky
designer would. Check: key content lost, cropped or shrunk so it no longer reads; text or logo covered, elements
colliding; text too small, cut off, badly wrapped or low contrast; things that belong together came apart (label
inside its pill, button text inside the button, icon next to its text); headline no longer dominant, CTA lost,
reading order broken; large dead areas, lopsided layout, elements jammed against edges; pictures stretched.
{_PAIR_TAIL}"""
CONDITIONS = {"R": PAIR_R, "D": PAIR_D}
PAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {"type": "string"},
        "rows": {"type": "object", "properties": {k: {"enum": ["A", "B", "same"]} for k in ROWS},
                 "required": list(ROWS)},
        "overall": {"enum": ["A", "B", "tie"]},
        "confidence": {"type": "number", "minimum": 0.5, "maximum": 1},
    },
    "required": ["observations", "rows", "overall", "confidence"],
}
POFF = f"""You are the automated evaluator of a design-resizing challenge. You get one design that an engine resized
to a new canvas size. Score it on these criteria, each from 1 (fails) to 5 (excellent):
{_ROW_LINES}
Then give an overall score from 1 to 10 and a verdict: FAIL when a criterion is clearly violated in a way a client
would reject, REVIEW when it is acceptable with minor fixes, PASS when it would ship as is.
Write observations first, then the scores, the overall score and the verdict, and only then list the concrete defects
you see (class, the element: quote its text or name the picture, severity). An empty list is fine."""
POFF_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {"type": "string"},
        "scores": {"type": "object", "properties": {k: {"type": "integer", "minimum": 1, "maximum": 5} for k in ROWS},
                   "required": list(ROWS)},
        "overall": {"type": "integer", "minimum": 1, "maximum": 10},
        "verdict": {"enum": ["PASS", "REVIEW", "FAIL"]},
        "defects": {"type": "array", "items": {
            "type": "object",
            "properties": {"class": {"enum": DEFECTS}, "element": {"type": "string"},
                           "severity": {"enum": ["major", "minor"]}},
            "required": ["class", "element", "severity"]}},
    },
    "required": ["observations", "scores", "overall", "verdict", "defects"],
}


def decided(votes):
    """+1 / -1 only when every judgement agrees (ties and splits are 0)."""
    return votes[0] if votes and votes[0] != 0 and all(v == votes[0] for v in votes) else 0


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _image(path, upscale=False) -> dict:
    """Native size; only downscaled to the vision cap (claude_judge also upscaled small renders, here opt-in)."""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    scale = min(1.0, MAX_LONG_SIDE / max(w, h), math.sqrt(MAX_PIXELS / (w * h)))
    if upscale and max(w, h) < MIN_LONG_SIDE:
        scale = min(MIN_LONG_SIDE / max(w, h), MAX_LONG_SIDE / max(w, h), math.sqrt(MAX_PIXELS / (w * h)))
    if abs(scale - 1) > 0.01:
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(buf.getvalue()).decode()}}


def _cached_call(key_parts: list, content_fn, system: str, schema: dict, model: str, use_cache: bool) -> dict:
    """Cache by the given key under a per-day folder: a verdict from another day is never reused (drift > effect)."""
    key = hashlib.sha256("|".join(map(str, key_parts)).encode()).hexdigest()[:24]
    path = CACHE_DIR / time.strftime("%Y-%m-%d") / f"{key}.json"
    if use_cache and path.is_file():
        return {**json.loads(path.read_text()), "cached": True}
    res = _run_claude(content_fn(), model, EFFORT, system, schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(res, indent=1))
    return {**res, "cached": False}


def pair_call(src, png_a, png_b, target, cond, order, rep=0, model=MODEL, use_cache=True, upscale=False) -> dict:
    """One judgement. order 0 shows the --a render first, order 1 the --b render first. Returns vote +1 if b wins."""
    ha, hb = _sha(png_a), _sha(png_b)
    prompt = hashlib.sha256((VERSION + CONDITIONS[cond] + json.dumps(PAIR_SCHEMA) + str(upscale)).encode()).hexdigest()[:12]
    key = [ha, hb, order, prompt, cond, model, rep, target, _sha(src) if cond == "R" else "-"]
    shown = [("a", png_a), ("b", png_b)][:: 1 if order == 0 else -1]
    letters = ["A", "B"] if random.Random("|".join(map(str, key))).random() < 0.5 else ["B", "A"]

    def content():
        c, n = [], 1
        if cond == "R":
            c += [{"type": "text", "text": "Image 1 - SOURCE (original design):"}, _image(src, upscale)]
            n = 2
        for i, (_, p) in enumerate(shown):
            c += [{"type": "text", "text": f"Image {n + i} - candidate {letters[i]} (resized to {target}):"}, _image(p, upscale)]
        return c + [{"type": "text", "text": f"Both candidates target {target}. Compare candidate A and candidate B."}]

    res = _cached_call(key, content, CONDITIONS[cond], PAIR_SCHEMA, model, use_cache)
    side = {letters[i]: s for i, (s, _) in enumerate(shown)}

    def vote(ans):
        return {"a": -1, "b": 1}.get(side.get(ans), 0)
    return {"cond": cond, "order": order, "letters": "".join(letters), "vote": vote(res["overall"]),
            "rows": {k: vote(v) for k, v in res["rows"].items()}, "confidence": res.get("confidence"),
            "observations": res.get("observations"), "cached": res["cached"]}


def poff_call(png, target, rep=0, model=MODEL, use_cache=True, upscale=False) -> dict:
    prompt = hashlib.sha256((VERSION + POFF + json.dumps(POFF_SCHEMA) + str(upscale)).encode()).hexdigest()[:12]
    key = [_sha(png), "poff", prompt, model, rep, target]
    content = lambda: [{"type": "text", "text": f"Image 1 - RESIZED for {target}:"}, _image(png, upscale),  # noqa: E731
                       {"type": "text", "text": f"The target canvas is {target}. Score this resize."}]
    res = _cached_call(key, content, POFF, POFF_SCHEMA, model, use_cache)
    return {k: res.get(k) for k in ("scores", "overall", "verdict", "defects", "observations", "cached")}


# ---------------------------------------------------------------- task plumbing

def task_info(stem: str, task: dict) -> dict:
    W, H = task["target_canvas"]["width"], task["target_canvas"]["height"]
    return {"task": stem, "design": re.sub(rf"_+{W}x{H}$", "", stem), "target": f"{W}x{H}", "bucket": bucket(W, H)}


def source_png(tasks: Path, stem: str, task: dict) -> Path:
    p = tasks / "_src_png" / f"{stem}.png"
    if p.is_file():
        return p
    return _render(task["source"], CANARY_DIR / "src" / f"{hashlib.sha256(json.dumps(task['source'], sort_keys=True).encode()).hexdigest()[:16]}.png")


def _render(design: dict, out: Path) -> Path:
    if not out.is_file():
        from eval.grader.forensics import render_png   # lazy: pulls in the renderer
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(render_png(design)).save(out)
    return out


def load_tasks(tasks: Path, dirs: list[Path], limit: int | None) -> list[dict]:
    items = []
    for f in sorted(tasks.glob("*.json")):
        missing = [str(d) for d in dirs if not (d / f"{f.stem}.png").is_file()]
        if missing:
            print(f"skip {f.stem}: no render in {missing}", file=sys.stderr)
            continue
        task = json.loads(f.read_text())
        items.append({**task_info(f.stem, task), "_task": task, "src": str(source_png(tasks, f.stem, task)),
                      **{f"png{i}": str(d / f"{f.stem}.png") for i, d in enumerate(dirs)}})
        if limit and len(items) >= limit:
            break
    return items


def _stretch_asset(src, out, f=1.6):
    """Stretch the biggest real image asset (product, logo; never a cover crop) by f on an axis that fits.
    fit becomes "fill": the renderer letterboxes "contain", so a stretched contain box shows no distortion (v1 miss)."""
    from eval.grader import corrupt
    o = json.loads(json.dumps(out))
    W, H = o["canvas"]["width"], o["canvas"]["height"]
    for e in sorted(corrupt._non_bg_images(o), key=lambda e: -e["width"] * e["height"]):
        for k, x, lim in (("width", "x", W), ("height", "y", H)):
            if e[k] * f <= lim:
                e[x] = min(max(0, e[x] - e[k] * (f - 1) / 2), lim - e[k] * f)   # keep the centre where it fits
                e[k] *= f
                e["fit"] = "fill"
                return o
    return None


def _tiny_text_4px(src, out):
    from eval.grader import corrupt
    o = json.loads(json.dumps(out))
    e = corrupt._biggest_text(o)
    if e is None:
        return None
    e["font_size"] = 4.0
    return o


def _headline_on_busy_image(src, out):
    """Centre the headline on a busy picture (asset grey std >= 40) painted below it that it did not touch."""
    import numpy as np
    from eval.grader import corrupt
    from eval.grader.forensics import ASSETS
    o = json.loads(json.dumps(out))
    t = corrupt._biggest_text(o)
    if t is None:
        return None

    def busy(e):
        p = ASSETS / str(e.get("asset_id"))
        return p.is_file() and np.asarray(Image.open(p).convert("L"), dtype=float).std() >= 40

    def touches(a, b):
        return a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"] and \
            a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"]
    pics = [e for e in o["elements"] if e["type"] == "image" and e.get("z_index", 0) < t.get("z_index", 0)
            and e["width"] * e["height"] >= 0.5 * t["width"] * t["height"] and not touches(e, t) and busy(e)]
    if not pics:
        return None
    corrupt._center_on(o, t, max(pics, key=lambda e: e["width"] * e["height"]))
    return o


def canary_fns() -> dict:
    from eval.grader import corrupt
    return {"stretch_asset_1.6": _stretch_asset, "text_on_text": corrupt.text_on_text, "tiny_text_4px": _tiny_text_4px,
            "half_off_canvas": corrupt.off_canvas, "headline_on_busy_image": _headline_on_busy_image}


def changed_share(a: Path, b: Path) -> float:
    """Share of pixels that differ visibly (max channel delta > 40) at the judge's view size."""
    import numpy as np
    x, y = (np.asarray(Image.open(p).convert("RGB"), dtype=int) for p in (a, b))
    if x.shape != y.shape:
        return 1.0
    s = max(1, round(max(x.shape[:2]) / MAX_LONG_SIDE))
    return float((np.abs(x - y).max(axis=2) > 40)[::s, ::s].mean())


def canaries(items: list[dict], b_dir: Path, n: int, seed: int = 0) -> list[dict]:
    """n identity pairs (X vs X) and n defect-vs-clean pairs from the frozen pool, both rendered by
    forensics.render_png. Defects rotate through the pool; one that changes < CANARY_MIN_CHANGED is skipped."""
    fns = canary_fns()
    rng, out = random.Random(seed), []
    pool = items[:]
    rng.shuffle(pool)
    for it in pool[:n]:
        out.append({**it, "canary": "identity", "expect": 0, "png0": it["png1"], "png1": it["png1"]})
    k = 0
    for it in pool:
        if k >= n:
            break
        js = b_dir / f"{it['task']}.json"
        if not js.is_file():
            continue
        clean = json.loads(js.read_text())
        for name in CANARY_DEFECTS[k % len(CANARY_DEFECTS):] + CANARY_DEFECTS[: k % len(CANARY_DEFECTS)]:
            bad = fns[name](it["_task"]["source"], clean)
            if bad is None:
                continue
            try:
                pc = _render(clean, CANARY_DIR / f"{it['task']}_clean_{_sha(js)[:8]}.png")
                pd = _render(bad, CANARY_DIR / f"{it['task']}_{CANARY_POOL}_{name}_{_sha(js)[:8]}.png")
            except Exception as e:   # report and try the next defect
                print(f"canary render failed {it['task']} {name}: {e}", file=sys.stderr)
                continue
            if changed_share(pc, pd) < CANARY_MIN_CHANGED:
                continue
            flip = rng.random() < 0.5   # defect on the a side half the time
            out.append({**it, "canary": f"defect:{name}", "expect": 1 if flip else -1, "pool": CANARY_POOL,
                        "png0": str(pd if flip else pc), "png1": str(pc if flip else pd)})
            k += 1
            break
    return out


def run_pairs(items: list[dict], workers: int, rep=0, model=MODEL, use_cache=True, upscale=False) -> list[dict]:
    jobs = [(i, c, o) for i in range(len(items)) for c in ("R", "D") for o in (0, 1)]

    def one(job):
        i, c, o = job
        it = items[i]
        try:
            return pair_call(it["src"], it["png0"], it["png1"], it["target"], c, o, rep, model, use_cache, upscale)
        except Exception as e:   # one bad call must not sink the run; it is counted and printed
            print(f"ERROR {it['task']} {c}{o}: {e}", file=sys.stderr)
            return {"cond": c, "order": o, "vote": 0, "rows": {}, "error": str(e)}

    with ThreadPoolExecutor(min(workers, MAX_WORKERS)) as pool:
        calls = list(pool.map(one, jobs))
    recs = []
    for i, it in enumerate(items):
        cs = calls[4 * i: 4 * i + 4]
        votes = [c["vote"] for c in cs]
        rec = {k: v for k, v in it.items() if not k.startswith("_") and k not in ("src",)}
        recs.append({**rec, "stratum": it.get("stratum", "collateral"), "votes": votes, "d": decided(votes),
                     "rows": {k: [c["rows"].get(k, 0) for c in cs] for k in ROWS},
                     "confidence": [c.get("confidence") for c in cs], "errors": sum("error" in c for c in cs),
                     "fail0": False, "fail1": False, "poff": False, "calls": cs})
    return recs


def add_poff(recs: list[dict], workers: int, model=MODEL, use_cache=True) -> None:
    """P-OFF on both renders of each decided pair: fail0/fail1 feed accept.py's FAIL-flip guard."""
    todo = [(r, s) for r in recs if r["d"] for s in (0, 1)]

    def one(job):
        r, s = job
        try:
            return poff_call(r[f"png{s}"], r["target"], model=model, use_cache=use_cache)
        except Exception as e:
            print(f"ERROR poff {r['task']} {s}: {e}", file=sys.stderr)
            return {"verdict": None, "error": str(e)}

    with ThreadPoolExecutor(min(workers, MAX_WORKERS)) as pool:
        for (r, s), res in zip(todo, pool.map(one, todo)):
            r[f"fail{s}"] = res.get("verdict") == "FAIL"
            r[f"poff{s}"] = res
            r["poff"] = True


def canary_summary(crecs: list[dict]) -> dict:
    ident = [c for c in crecs if c["canary"] == "identity"]
    defect = [c for c in crecs if c["canary"] != "identity"]
    id_rate = sum(c["d"] != 0 for c in ident) / len(ident) if ident else None
    acc = sum(c["d"] == c["expect"] for c in defect) / len(defect) if defect else None
    void = (acc is not None and acc < 0.9) or (id_rate is not None and id_rate > 0.10)
    return {"identity_n": len(ident), "identity_decided": id_rate, "defect_n": len(defect),
            "defect_accuracy": acc, "void": void}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("pairs")
    p.add_argument("--a", required=True, type=Path, help="champion render dir (<task>.png, <task>.json)")
    p.add_argument("--b", required=True, type=Path, help="challenger render dir")
    q = sub.add_parser("poff")
    q.add_argument("--renders", required=True, type=Path)
    for s in (p, q):
        s.add_argument("--tasks", required=True, type=Path)
        s.add_argument("--out", required=True, type=Path)
        s.add_argument("--limit", type=int)
        s.add_argument("--workers", type=int, default=3)
        s.add_argument("--model", default=MODEL)
        s.add_argument("--rep", type=int, default=0, help="repetition index (part of the cache key)")
        s.add_argument("--no-cache", action="store_true")
        s.add_argument("--upscale", action="store_true", help="upscale renders under 1000 px (off: native view)")
    p.add_argument("--canaries", type=int, help="identity and defect canaries each (default: 5%% of pairs, min 1)")
    p.add_argument("--poff", action="store_true", help="P-OFF both renders of decided pairs (FAIL flips)")
    p.add_argument("--changed", type=Path, help="suite.py changed_pairs.json: copies each pair's pre-registered stratum")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.cmd is None:
        ap.error("pairs, poff or --selftest")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    kw = {"model": a.model, "use_cache": not a.no_cache}

    if a.cmd == "poff":
        items = load_tasks(a.tasks, [a.renders], a.limit)
        with ThreadPoolExecutor(min(a.workers, MAX_WORKERS)) as pool:
            res = list(pool.map(lambda it: poff_call(it["png0"], it["target"], a.rep, upscale=a.upscale, **kw), items))
        with a.out.open("w") as f:
            for it, r in zip(items, res):
                f.write(json.dumps({**{k: it[k] for k in ("task", "design", "target", "bucket", "png0")}, **r}) + "\n")
                print(f"{it['task']}\t{r['verdict']}\t{r['overall']}\t{[d['class'] for d in r['defects']]}")
        return 0

    items = load_tasks(a.tasks, [a.a, a.b], None)
    changed = [it for it in items if _sha(it["png0"]) != _sha(it["png1"])]
    if a.changed:   # stratum is fixed by suite.py before judging; unknown tasks stay collateral
        strata = {c["task"]: c["stratum"] for c in json.loads(a.changed.read_text())}
        changed = [{**it, "stratum": strata.get(it["task"], "collateral")} for it in changed]
    print(f"{len(items)} tasks, {len(items) - len(changed)} unchanged (not judged)", file=sys.stderr)
    changed = changed[: a.limit] if a.limit else changed
    n_can = a.canaries if a.canaries is not None else max(1, round(0.05 * len(changed)))
    can = canaries(changed or items, a.b, n_can) if n_can else []
    recs = run_pairs(changed + can, a.workers, a.rep, upscale=a.upscale, **kw)
    real, crecs = recs[: len(changed)], recs[len(changed):]
    if a.poff:
        add_poff(real, a.workers, **kw)
    with a.out.open("w") as f:
        for r in real:
            f.write(json.dumps(r) + "\n")
            print(f"{r['task']}\t{r['bucket']}\tvotes={r['votes']}\td={r['d']:+d}\terrors={r['errors']}")
    with a.out.with_suffix(".canaries.jsonl").open("w") as f:
        for r in crecs:
            f.write(json.dumps(r) + "\n")
            print(f"canary {r['canary']}\t{r['task']}\tvotes={r['votes']}\td={r['d']:+d}\texpect={r['expect']:+d}")
    summ = canary_summary(crecs)
    print(json.dumps({"pairs": len(real), "wins_b": sum(r["d"] > 0 for r in real),
                      "wins_a": sum(r["d"] < 0 for r in real), "canaries": summ}))
    if summ["void"]:
        print("RUN VOID: canaries failed (see GRADER_DESIGN.md L3)", file=sys.stderr)
        return 2
    return 0


def selftest() -> int:
    """No claude calls: the real driver is replaced by a stub that always prefers one known image."""
    import tempfile
    import eval.arena.judge as J

    assert decided([1, 1, 1, 1]) == 1 and decided([-1] * 4) == -1
    assert decided([1, 1, 0, 1]) == 0 and decided([1, -1, 1, 1]) == 0 and decided([0] * 4) == 0
    assert len(DEFECTS) == 18 == len(set(DEFECTS))
    if A_RUBRIC.is_file():
        ns: dict = {}
        exec(compile(A_RUBRIC.read_text(), str(A_RUBRIC), "exec"), ns)
        assert all(ns["ROW_TEXT"][k] == v for k, v in ROWS.items()), "ROWS drifted from A grader/rubric.py"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        J.CACHE_DIR = td / "cache"
        imgs = {}
        for name, col in (("src", "white"), ("a", "red"), ("b", "blue")):
            imgs[name] = td / f"{name}.png"
            Image.new("RGB", (300, 250), col).save(imgs[name])
        assert base64.b64decode(_image(imgs["a"])["source"]["data"])[16:24] == (300).to_bytes(4, "big") + (250).to_bytes(4, "big")
        blue = _image(imgs["b"])["source"]["data"]
        seen = []

        def stub(content, model, effort, system, schema):   # prefers whichever letter labels the blue (b) image
            seen.append(content)
            if "verdict" in schema["properties"]:
                return {"observations": "", "scores": dict.fromkeys(ROWS, 3), "overall": 5, "verdict": "FAIL", "defects": []}
            label = next(content[i - 1]["text"] for i, c in enumerate(content)
                         if c["type"] == "image" and c["source"]["data"] == blue)
            win = re.search(r"candidate ([AB])", label).group(1)
            return {"observations": "", "rows": dict.fromkeys(ROWS, win), "overall": win, "confidence": 0.9}
        J._run_claude = stub
        item = {"task": "t_300x250", "design": "t", "target": "300x250", "bucket": "square",
                "src": str(imgs["src"]), "png0": str(imgs["a"]), "png1": str(imgs["b"])}
        rec = J.run_pairs([item], workers=2)[0]
        assert rec["votes"] == [1, 1, 1, 1] and rec["d"] == 1, rec["votes"]
        assert rec["rows"]["brand"] == [1, 1, 1, 1]
        assert len({c["letters"] for c in rec["calls"]}) == 2, "letters never re-randomised"   # seeded per key
        assert sum(c[0]["text"].startswith("Image 1 - SOURCE") for c in seen) == 2   # source only in condition R
        flipped = J.run_pairs([{**item, "png0": item["png1"], "png1": item["png0"]}], workers=2)[0]
        assert flipped["d"] == -1
        n = len(seen)
        assert J.run_pairs([item], workers=2)[0]["d"] == 1 and len(seen) == n, "cache miss on identical call"
        J.run_pairs([item], workers=2, rep=1)
        assert len(seen) == n + 4, "rep must be part of the cache key"
        J.add_poff([rec], workers=2)
        assert rec["fail0"] and rec["fail1"] and rec["poff"]
    des = {"canvas": {"width": 300, "height": 250}, "elements": [
        {"id": "p", "type": "image", "asset_id": "solstice_bottle.png", "fit": "contain", "x": 200, "y": 20,
         "width": 80, "height": 120, "z_index": 0},
        {"id": "h", "type": "text", "content": "Hi", "font_size": 30, "x": 10, "y": 180, "width": 100, "height": 40,
         "z_index": 1}]}
    st = _stretch_asset(des, des)["elements"][0]
    assert st["fit"] == "fill" and st["width"] == 128 and 0 <= st["x"] and st["x"] + st["width"] <= 300, st
    assert _tiny_text_4px(des, des)["elements"][1]["font_size"] == 4 and des["elements"][1]["font_size"] == 30
    assert set(canary_fns()) == set(CANARY_DEFECTS)
    assert canary_summary([{"canary": "identity", "d": 0, "expect": 0},
                           {"canary": "defect:x", "d": -1, "expect": -1}])["void"] is False
    assert canary_summary([{"canary": "identity", "d": 1, "expect": 0}])["void"] is True
    print("judge selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
