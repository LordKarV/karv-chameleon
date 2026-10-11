"""Visual review sheets for disciplined self-grading.

    PYTHONPATH=. python scripts/review_sheet.py OUTDIR [--sample N --seed S] [--pairs design:WxH ...]

Per item, one PNG: ORIGINAL | OUTPUT | OUTPUT with labelled unit boxes (roles),
plus a 2x zoom of the output when the target's short side is under 300 px.
Also writes OUTDIR/items.md: per item the element table (role, font size before ->
after, source and output box) used for the counting / verification step."""
import argparse
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw

from engine.analyze import analyze
from engine.resize import resize_with_info
from engine.trace import ROLE_COLORS
from renderer.converter.convert import render_design_to_png


def render(design, path, side=900):
    render_design_to_png(design, "brand_kit/assets", path, max_size=side)
    return Image.open(path).convert("RGB")


def labelled(im, design, d):
    im = im.copy()
    W, H = design["canvas"]["width"], design["canvas"]["height"]
    sx, sy = im.width / W, im.height / H
    dr = ImageDraw.Draw(im)
    by = {e["id"]: e for e in design["elements"]}
    for i, u in enumerate([u for u in d.units if not u.host]):
        els = [by[e["id"]] for e in u.elements + [m for k in u.children for m in k.elements] if e["id"] in by]
        if not els:
            continue
        x0, y0 = min(e["x"] for e in els), min(e["y"] for e in els)
        x1, y1 = max(e["x"] + e["width"] for e in els), max(e["y"] + e["height"] for e in els)
        c = ROLE_COLORS.get(u.role, "#212529")
        dr.rectangle([x0 * sx, y0 * sy, x1 * sx, y1 * sy], outline=c, width=2)
        lab = f"U{i} {u.role}"
        dr.rectangle([x0 * sx, y0 * sy, x0 * sx + 7 * len(lab) + 4, y0 * sy + 13], fill=c)
        dr.text((x0 * sx + 2, y0 * sy), lab, fill="white")
    return im


def panel(imgs, labels, h=420):
    ims = []
    for im in imgs:
        s = min(h / im.height, 620 / im.width)
        ims.append(im.resize((max(1, int(im.width * s)), max(1, int(im.height * s)))))
    W = sum(i.width for i in ims) + 16 * (len(ims) + 1)
    out = Image.new("RGB", (W, max(i.height for i in ims) + 34), "white")
    dr = ImageDraw.Draw(out)
    x = 16
    for im, lab in zip(ims, labels):
        out.paste(im, (x, 28))
        dr.text((x, 8), lab, fill=(30, 30, 30))
        x += im.width + 16
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--sample", type=int, default=8)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--pairs", nargs="*", default=[])
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    targets = json.load(open("eval/targets.json"))
    designs = {Path(f).stem: json.load(open(f))["source"] for f in sorted(Path("eval/corpus/designs").glob("*.json"))}
    if a.pairs:
        jobs = [(p.split(":")[0], *map(int, p.split(":")[1].split("x"))) for p in a.pairs]
    else:
        allj = [(k, t["width"], t["height"]) for k in designs for t in targets]
        jobs = random.Random(a.seed).sample(allj, a.sample)
    md = []
    for n, (did, W, H) in enumerate(jobs):
        src = designs[did]
        o, info = resize_with_info(src, {"width": W, "height": H})
        d = analyze(src)
        si, oi = render(src, out / "_s.png"), render(o, out / "_o.png")
        ims, labs = [si, oi, labelled(oi, o, d)], ["ORIGINAL", f"OUTPUT {W}x{H} [{info['chosen']}]", "OUTPUT + units"]
        if min(W, H) < 300:
            ims.append(oi.resize((oi.width * 2, oi.height * 2)))
            labs.append("OUTPUT zoom 2x")
        panel(ims, labs).save(out / f"{n:02d}_{did}_{W}x{H}.png")
        by = {e["id"]: e for e in o["elements"]}
        md.append(f"## {n:02d} {did} {W}x{H} ({info['chosen']})\n| unit | role | font src→out | src box | out box |\n|---|---|---|---|---|")
        for i, u in enumerate([u for u in d.units if not u.host]):
            e = u.elements[0]
            oe = by.get(e["id"], e)
            fs = f"{e.get('font_size', '')}→{oe.get('font_size', '')}" if e["type"] == "text" else ""
            md.append(f"| U{i} | {u.role} | {fs} | {round(u.rect[0])},{round(u.rect[1])},{round(u.w)}x{round(u.h)} | "
                      f"{round(oe['x'])},{round(oe['y'])},{round(oe['width'])}x{round(oe['height'])} |")
        md.append("")
    (out / "items.md").write_text("\n".join(md))
    for f in ("_s.png", "_o.png"):
        (out / f).unlink(missing_ok=True)
    print(f"wrote {len(jobs)} review sheets to {out}")


if __name__ == "__main__":
    main()
