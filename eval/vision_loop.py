"""Agentic vision loop (dev-time): engine output -> Claude looks -> move/resize edits -> repeat.

    python -m eval.vision_loop DESIGN WxH [--rounds 3] [--model sonnet]

Each round Claude (headless `claude -p`, subscription billing; see docs/CLAUDE_VISION_JUDGE.md)
sees the ORIGINAL, the current RESIZED render and a marked copy (numbered boxes = element ids),
judges it and returns geometry edits only. Edits go through engine.resize.finalize, so the brief
holds by construction (ids kept, image aspect locked, in-canvas, integer fonts). Stops at PASS or
when a round changes nothing. The edit log is the evidence for the next engine rule; the submitted
engine stays offline.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

from engine.resize import finalize, resize
from eval.grader.claude_judge import _image, _run_claude
from eval.grader.forensics import render_png

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "eval" / "runs" / "vision_loop"
GEOM = ("x", "y", "width", "height", "font_size")

SYSTEM = """You are a senior visual designer fixing an automatic resize of a layered ad.
You see the ORIGINAL design, the current RESIZED version, and the RESIZED version with every
element outlined and numbered; the legend maps numbers to element ids, types and geometry.

Rules you must keep: you may only MOVE and RESIZE elements (x, y, width, height, font_size).
Never drop, restyle or stretch anything: images keep their aspect ratio (give the width you want,
the height follows), text keeps its content and font. Everything must stay inside the canvas.

Judge the RESIZED version like a picky designer (lost or tiny elements, overlaps, unreadable text,
things that belonged together drifting apart - e.g. a shadow under its product, a label in its
pill -, broken hierarchy, dead empty areas, lopsided composition). Elements that belong to another
object must move with it; elements anchored to the canvas (logo corner, edge ribbon) stay anchored.
Then give the smallest set of edits that fixes the problems, in canvas pixels of the RESIZED canvas.
If it is already shippable, verdict PASS and no edits. Observations first, then issues, then edits."""

SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "verdict": {"enum": ["PASS", "REVIEW", "FAIL"]},
        "edits": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
                           "width": {"type": "number"}, "height": {"type": "number"},
                           "font_size": {"type": "number"}, "why": {"type": "string"}},
            "required": ["id", "why"]}},
    },
    "required": ["observations", "issues", "verdict", "edits"],
}


def _marked(png: Path, out: dict, path: Path) -> list[str]:
    """Outline + number every element (numbers sit at the box corner); return the legend."""
    im = Image.open(png).convert("RGB")
    s = im.width / out["canvas"]["width"]
    d = ImageDraw.Draw(im)
    legend = []
    for i, e in enumerate(out["elements"]):
        x0, y0 = e["x"] * s, e["y"] * s
        d.rectangle((x0, y0, x0 + e["width"] * s, y0 + e["height"] * s), outline=(255, 0, 180), width=1)
        d.text((x0 + 2, y0 + 1), str(i), fill=(255, 0, 180))
        what = str(e.get("content", ""))[:30] or e.get("asset_id") or e.get("shape_type") or ""
        legend.append(f"{i}: id={e['id']} {e['type']} '{what}' x={e['x']:.0f} y={e['y']:.0f} "
                      f"w={e['width']:.0f} h={e['height']:.0f}" + (f" font={e['font_size']:.0f}" if "font_size" in e else ""))
    im.save(path)
    return legend


def _apply(src: dict, out: dict, edits: list[dict]) -> dict:
    """Edits -> placements -> engine finalize (the contract enforcer)."""
    W, H = out["canvas"]["width"], out["canvas"]["height"]
    place = {e["id"]: {k: float(e[k]) for k in GEOM if k in e} for e in out["elements"]}
    by_src = {e["id"]: e for e in src["elements"]}
    for ed in edits:
        p = place.get(ed.get("id"))
        if p is None:
            continue
        p.update({k: float(ed[k]) for k in GEOM if ed.get(k) is not None and (k != "font_size" or "font_size" in p)})
        se = by_src[ed["id"]]
        if se["type"] == "image" and ed.get("width") is not None and se.get("width") and se.get("height"):
            p["height"] = p["width"] * se["height"] / se["width"]          # aspect locked
    return finalize(src, place, W, H)


def run(design: str, W: int, H: int, rounds: int = 3, model: str = "sonnet") -> dict:
    src = json.loads((ROOT / "eval" / "corpus" / "designs" / f"{design}.json").read_text())["source"]
    out = resize(src, {"width": W, "height": H})
    d = RUNS / f"{design}@{W}x{H}"
    d.mkdir(parents=True, exist_ok=True)
    Image.fromarray(render_png(src)).save(d / "original.png")
    log = []
    for r in range(rounds + 1):
        png = d / f"round{r}.png"
        Image.fromarray(render_png(out)).save(png)
        (d / f"round{r}.json").write_text(json.dumps(out))
        legend = _marked(png, out, d / f"round{r}_marked.png")
        content = [{"type": "text", "text": "Image 1 - ORIGINAL:"}, _image(str(d / "original.png")),
                   {"type": "text", "text": f"Image 2 - RESIZED to {W}x{H} (current):"}, _image(str(png)),
                   {"type": "text", "text": "Image 3 - RESIZED with numbered element boxes:"},
                   _image(str(d / f"round{r}_marked.png")),
                   {"type": "text", "text": f"Canvas {W}x{H}. Legend:\n" + "\n".join(legend)}]
        res = _run_claude(content, model, "medium", SYSTEM, SCHEMA)
        log.append({"round": r, "verdict": res["verdict"], "issues": res["issues"], "edits": res["edits"]})
        print(f"{design}@{W}x{H} round {r}: {res['verdict']}  {len(res['edits'])} edits  " + " | ".join(res["issues"])[:300])
        if res["verdict"] == "PASS" or not res["edits"] or r == rounds:
            break
        nxt = _apply(src, out, res["edits"])
        if nxt == out:
            break
        out = nxt
    (d / "log.json").write_text(json.dumps(log, indent=1))
    (d / "out.json").write_text(json.dumps(out))
    return {"log": log, "out": out}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.vision_loop")
    ap.add_argument("design")
    ap.add_argument("size", help="WxH")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--model", default="sonnet")
    a = ap.parse_args(argv)
    W, H = map(int, a.size.lower().split("x"))
    run(a.design, W, H, a.rounds, a.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
