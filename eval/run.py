"""Run an engine over designs x targets, score, judge, summarize.

  python -m eval.run --engine engine.resize:resize --designs eval/corpus/designs \\
      --targets eval/targets.json [--sample N --seed S] [--judge gemini|anthropic|ollama|none] \\
      [--env CHAMELEON_STRATEGY=scale_fit] [--jobs 6] [--timeout 120] --out eval/runs/<name>

Writes <out>/outputs/*.json|png, <out>/sources/*.png, results.jsonl, summary.json and
contact_sheet_*.png. A crash or timeout counts as det_score=0 and judge scores=0.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import multiprocessing as mp
import os
import random
import re
import signal
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

PNG_MAX_SIZE = 1024
SHEET_ROWS, THUMB_H = 40, 150
JUDGE_KEYS = ["collisions", "readability", "brand_fidelity", "intentional_layout", "content_preservation", "overall"]


def bucket(w: int, h: int) -> str:
    ar = w / h
    return "tall" if ar < 0.8 else "square" if ar <= 1.25 else "wide" if ar <= 2.5 else "ultrawide"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def load_tasks(designs: str, targets_path: str | None) -> list[dict]:
    """Corpus files ({design_id, level, source}) are crossed with all targets;
    task files ({source, target_canvas}) use their own target."""
    paths = sorted(Path(designs).glob("*.json")) if Path(designs).is_dir() else [Path(designs)]
    targets = json.loads(Path(targets_path).read_text()) if targets_path else []
    tasks = []
    for p in paths:
        d = json.loads(p.read_text())
        if "source" not in d:
            continue
        did = d.get("design_id") or d.get("task_id") or p.stem
        lvl = d.get("level") or int((re.search(r"level_(\d)", str(d.get("difficulty", ""))) or [0, 0])[1])
        tlist = ([{"name": "task_target", **d["target_canvas"]}] if "target_canvas" in d else targets)
        tasks += [{"design_id": did, "level": lvl, "source": d["source"], "target": t} for t in tlist]
    return tasks


# --------------------------------------------------------------- workers --

_ENGINE = None


def _engine(spec: str):
    global _ENGINE
    if _ENGINE is None:
        mod, fn = spec.split(":")
        _ENGINE = getattr(importlib.import_module(mod), fn)
    return _ENGINE


class _Timeout(Exception):
    pass


def _alarm(_sig, _frm):
    raise _Timeout()


def _render(design: dict, png: Path, assets: str, fonts: str) -> None:
    from renderer.converter.convert import render_design_to_png
    render_design_to_png(design, assets, str(png), fonts_dir=fonts, max_size=PNG_MAX_SIZE)


def render_source(args: tuple) -> None:
    source, png, assets, fonts = args
    if not Path(png).is_file():
        _render(source, Path(png), assets, fonts)


def run_pair(args: tuple) -> dict:
    task, spec, out_dir, assets, fonts, timeout = args
    from eval.metrics import score
    t = task["target"]
    stem = f"{task['design_id']}__{slug(t['name'])}"
    rec = {"design_id": task["design_id"], "level": task["level"], "target": t["name"],
           "w": t["width"], "h": t["height"], "bucket": bucket(t["width"], t["height"]), "stem": stem}
    signal.signal(signal.SIGALRM, _alarm)
    t0 = time.time()
    try:
        signal.alarm(timeout)
        output = _engine(spec)(copy.deepcopy(task["source"]), {"width": t["width"], "height": t["height"]})
        signal.alarm(0)
    except BaseException as e:  # noqa: BLE001 -- any engine failure (incl. timeout) is a zero
        signal.alarm(0)
        msg = "timeout" if isinstance(e, _Timeout) else f"{type(e).__name__}: {e}"
        return {**rec, "engine_s": round(time.time() - t0, 3), "error": msg,
                "trace": traceback.format_exc()[-1500:], "det_score": 0.0, "structural_ok": False}
    rec["engine_s"] = round(time.time() - t0, 3)
    out_json, out_png = Path(out_dir) / "outputs" / f"{stem}.json", Path(out_dir) / "outputs" / f"{stem}.png"
    try:
        out_json.write_text(json.dumps(output, indent=1))
        _render(output, out_png, assets, fonts)
        m = score(task["source"], output, str(out_png), assets_dir=assets, fonts_dir=fonts, target=t)
    except Exception as e:  # noqa: BLE001 -- unrenderable/unscorable output also counts as zero
        return {**rec, "error": f"eval: {type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:],
                "det_score": 0.0, "structural_ok": False}
    return {**rec, "error": None, **m}


# --------------------------------------------------------------- judging --

def judge_all(recs: list[dict], out: Path, backend: str, workers: int) -> None:
    from eval.judge import judge

    def one(r):
        if r.get("error"):
            r["judge"] = {k: 0 for k in JUDGE_KEYS}
            return
        try:
            r["judge"] = judge(str(out / "sources" / f"{r['design_id']}.png"),
                               str(out / "outputs" / f"{r['stem']}.png"), r["target"], r["w"], r["h"], backend)
        except Exception as e:  # noqa: BLE001 -- judge failure: excluded from judge means, not a zero
            r["judge_error"] = f"{type(e).__name__}: {e}"[:500]
            print(f"  judge failed {r['stem']}: {r['judge_error']}", file=sys.stderr)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, recs))


# --------------------------------------------------------------- summary --

def _means(recs: list[dict]) -> dict:
    j = [r["judge"] for r in recs if "judge" in r]
    out = {
        "n": len(recs),
        "crashes": sum(bool(r.get("error")) for r in recs),
        "structural_ok_rate": round(float(np.mean([bool(r.get("structural_ok")) for r in recs])), 3) if recs else None,
        "det_score": round(float(np.mean([r["det_score"] for r in recs])), 2) if recs else None,
        "engine_s": round(float(np.mean([r.get("engine_s", 0) for r in recs])), 3) if recs else None,
    }
    if j:
        out["judge"] = {k: round(float(np.mean([x[k] for x in j])), 2) for k in JUDGE_KEYS}
    return out


def summarize(recs: list[dict]) -> dict:
    by = lambda key: {str(v): _means([r for r in recs if r[key] == v]) for v in sorted({r[key] for r in recs}, key=str)}  # noqa: E731
    cost = sum(r["judge"]["meta"]["cost_usd"] for r in recs if "meta" in r.get("judge", {}) and not r["judge"]["meta"]["cached"])
    return {"all": _means(recs), "by_level": by("level"), "by_bucket": by("bucket"), "judge_cost_usd": round(cost, 4)}


def print_summary(s: dict) -> None:
    def row(name, m):
        jo = m.get("judge", {}).get("overall")
        print(f"  {name:<12} n={m['n']:<4} crash={m['crashes']:<3} struct_ok={m['structural_ok_rate']:<6} "
              f"det={m['det_score']:<6} judge={jo if jo is not None else '-'}")
    row("ALL", s["all"])
    for k, m in s["by_level"].items():
        row(f"level {k}", m)
    for k, m in s["by_bucket"].items():
        row(k, m)


def contact_sheets(recs: list[dict], out: Path) -> None:
    rows = sorted(recs, key=lambda r: (r["design_id"], r["w"] / r["h"]))
    for page in range(0, len(rows), SHEET_ROWS):
        chunk = rows[page:page + SHEET_ROWS]
        sheet = Image.new("RGB", (1400, THUMB_H * len(chunk)), "white")
        draw = ImageDraw.Draw(sheet)
        for i, r in enumerate(chunk):
            y = i * THUMB_H
            x = 0
            for p in (out / "sources" / f"{r['design_id']}.png", out / "outputs" / f"{r['stem']}.png"):
                if p.is_file():
                    im = Image.open(p).convert("RGB")
                    im.thumbnail((520, THUMB_H - 10))
                    sheet.paste(im, (x + 5, y + 5))
                x += 530
            jo = r.get("judge", {}).get("overall", "-")
            label = (f"{r['design_id']}\n{r['target']} {r['w']}x{r['h']}\ndet={r['det_score']} judge={jo}\n"
                     f"{'CRASH ' + r['error'][:40] if r.get('error') else ('OK' if r.get('structural_ok') else 'STRUCT FAIL')}")
            draw.multiline_text((1065, y + 8), label, fill="black")
            draw.line([(0, y + THUMB_H - 1), (1400, y + THUMB_H - 1)], fill=(200, 200, 200))
        sheet.save(out / f"contact_sheet_{page // SHEET_ROWS + 1}.png")


# ------------------------------------------------------------------ main --

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", required=True, help="module:function, e.g. engine.resize:resize")
    ap.add_argument("--designs", default="eval/corpus/designs")
    ap.add_argument("--targets", default="eval/targets.json")
    ap.add_argument("--sample", type=int, default=0, help="random subset of design x target pairs")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--judge", default="none", choices=["gemini", "anthropic", "ollama", "none"])
    ap.add_argument("--judge-workers", type=int, default=4)
    ap.add_argument("--env", action="append", default=[], help="KEY=VAL set for the engine (repeatable)")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--timeout", type=int, default=120, help="seconds per engine call")
    ap.add_argument("--assets", default="brand_kit/assets")
    ap.add_argument("--fonts", default="brand_kit/fonts")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    for kv in a.env:
        k, _, v = kv.partition("=")
        os.environ[k] = v  # inherited by spawned workers
    tasks = load_tasks(a.designs, a.targets)
    if not tasks:
        print(f"no tasks found in {a.designs}", file=sys.stderr)
        return 1
    if a.sample and a.sample < len(tasks):
        tasks = random.Random(a.seed).sample(tasks, a.sample)
    out = Path(a.out)
    (out / "outputs").mkdir(parents=True, exist_ok=True)
    (out / "sources").mkdir(parents=True, exist_ok=True)
    srcs = {t["design_id"]: t["source"] for t in tasks}
    print(f"{len(tasks)} pairs, {len(srcs)} designs, engine={a.engine} env={a.env} jobs={a.jobs}")

    t0 = time.time()
    with mp.get_context("spawn").Pool(a.jobs) as pool:
        pool.map(render_source, [(s, str(out / "sources" / f"{d}.png"), a.assets, a.fonts) for d, s in srcs.items()])
        recs = []
        for i, r in enumerate(pool.imap_unordered(
                run_pair, [(t, a.engine, str(out), a.assets, a.fonts, a.timeout) for t in tasks]), 1):
            recs.append(r)
            if i % 25 == 0 or i == len(tasks):
                print(f"  {i}/{len(tasks)} scored ({time.time() - t0:.0f}s)")
    if a.judge != "none":
        judge_all(recs, out, a.judge, a.judge_workers)

    recs.sort(key=lambda r: r["stem"])
    with open(out / "results.jsonl", "w") as f:
        for r in recs:
            f.write(json.dumps(r, default=float) + "\n")
    summary = {"engine": a.engine, "env": a.env, "judge": a.judge, "wall_s": round(time.time() - t0, 1), **summarize(recs)}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    contact_sheets(recs, out)
    print_summary(summary)
    print(f"judge cost ${summary['judge_cost_usd']}  wall {summary['wall_s']}s  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
