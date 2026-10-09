"""Render a showcase: python scripts/showcase.py [outdir]
For each design: source.png, <target>.json/.png, and sheet.png (source + all outputs).
Also writes index.html to browse everything."""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from engine.resize import resize_with_info
from renderer.converter.convert import render_design_to_png

DESIGNS = ["sample_l1_warmup", "sample_l2_core", "sample_l3_advanced", "brief_poster",
           "l3_catalog_grid", "l2_split_panels", "l2_trio_products", "l3_linkedin_banner"]
TARGETS = [("Instagram Post", 1080, 1080), ("Instagram Story", 1080, 1920), ("YouTube Thumbnail", 1280, 720),
           ("LinkedIn Banner", 1584, 396), ("Leaderboard", 728, 90), ("Medium Rectangle", 300, 250),
           ("Skyscraper", 160, 600)]


def sheet(paths, labels, out):
    ims = []
    for p in paths:
        im = Image.open(p).convert("RGB")
        s = min(360 / im.width, 360 / im.height)
        ims.append(im.resize((max(1, int(im.width * s)), max(1, int(im.height * s)))))
    W = sum(i.width for i in ims) + 16 * (len(ims) + 1)
    H = max(i.height for i in ims) + 50
    canvas = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(canvas)
    x = 16
    for im, lab in zip(ims, labels):
        canvas.paste(im, (x, 34))
        d.text((x, 10), lab, fill=(40, 40, 40))
        x += im.width + 16
    canvas.save(out)


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "showcase")
    html = ["<html><body style='font-family:sans-serif'><h1>Chameleon showcase</h1>"]
    for did in DESIGNS:
        src = json.load(open(f"eval/corpus/designs/{did}.json"))["source"]
        d = root / did
        d.mkdir(parents=True, exist_ok=True)
        (d / "source.json").write_text(json.dumps(src, indent=2))
        render_design_to_png(src, "brand_kit/assets", d / "source.png", max_size=1600)
        paths, labels = [d / "source.png"], [f"SOURCE {src['canvas']['width']}x{src['canvas']['height']}"]
        for name, W, H in TARGETS:
            out, info = resize_with_info(src, {"width": W, "height": H})
            stem = f"{W}x{H}_{name.lower().replace(' ', '_')}"
            (d / f"{stem}.json").write_text(json.dumps(out, indent=2))
            render_design_to_png(out, "brand_kit/assets", d / f"{stem}.png", max_size=1600)
            paths.append(d / f"{stem}.png")
            labels.append(f"{name} {W}x{H} [{info['chosen'].split()[0]}]")
            print(f"{did:22s} {name:18s} -> {info['chosen']}")
        sheet(paths, labels, d / "sheet.png")
        html.append(f"<h2>{did}</h2><img src='{did}/sheet.png' style='max-width:100%'>")
    (root / "index.html").write_text("\n".join(html) + "</body></html>")
    print(f"open {root / 'index.html'}")


if __name__ == "__main__":
    main()
