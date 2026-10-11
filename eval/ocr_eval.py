"""OCR legibility of engine outputs at their TRUE pixel size (no API).

    python -m eval.ocr_eval [--n 60 --seed 11]

Renders each output 1:1, crops every text element, reads it with EasyOCR (from
the ImageSorcery venv) and scores how much of the real copy comes back.
Reports mean legibility, weighted by role (headline/cta matter most), and the
share of elements a reader could not make out (score < 0.6)."""
import argparse
import glob
import json
import os
import random
import statistics as st
import subprocess
import tempfile
from pathlib import Path

PY = os.environ.get("OCR_PYTHON", str(Path.home() / ".local/pipx/venvs/imagesorcery-mcp/bin/python"))
ROLE_W = {"headline": 3.0, "cta": 2.5, "subhead": 1.5, "badge": 1.0, "body": 1.0, "legal": 0.3}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    from engine.analyze import analyze
    from engine.resize import resize
    from renderer.converter.convert import render_design_to_png
    designs = {Path(f).stem: json.load(open(f))["source"] for f in sorted(glob.glob("eval/corpus/designs/*.json"))}
    targets = json.load(open("eval/targets.json"))
    pairs = random.Random(a.seed).sample([(k, t["width"], t["height"]) for k in designs for t in targets], a.n)
    tmp = Path(tempfile.mkdtemp())
    jobs, roles, sizes = [], {}, {}
    for i, (did, W, H) in enumerate(pairs):
        src = designs[did]
        out = resize(src, {"width": W, "height": H})
        png = tmp / f"{i}.png"
        render_design_to_png(out, "brand_kit/assets", png, max_size=max(W, H))   # 1:1
        d = analyze(src)
        role = {e["id"]: u.role for u in d.units for e in u.elements + [m for k in u.children for m in k.elements]}
        boxes = []
        for e in out["elements"]:
            if e["type"] != "text" or not str(e.get("content", "")).strip():
                continue
            boxes.append({"id": e["id"], "box": [e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"]],
                          "text": e["content"], "rotation": e.get("rotation", 0)})
            roles[(i, e["id"])] = role.get(e["id"], "other")
            sizes[(i, e["id"])] = (e["font_size"], W, H, did)
        jobs.append({"png": str(png), "boxes": boxes})
    (tmp / "jobs.json").write_text(json.dumps(jobs))
    subprocess.run([PY, "eval/ocr/worker.py", str(tmp / "jobs.json"), str(tmp / "res.json")], check=True)
    res = json.load(open(tmp / "res.json"))
    rows = [(roles[(i, r["id"])], r["score"]) for i, j in enumerate(res) for r in j["results"]]
    wsum = sum(ROLE_W.get(rl, 1.0) for rl, _ in rows)
    summary = {"tag": a.tag, "n_text": len(rows),
               "ocr_weighted": round(sum(ROLE_W.get(rl, 1.0) * s for rl, s in rows) / wsum, 4),
               "ocr_mean": round(st.mean(s for _, s in rows), 4),
               "unreadable_share": round(sum(s < 0.6 for _, s in rows) / len(rows), 4),
               "by_role": {rl: round(st.mean(s for r2, s in rows if r2 == rl), 3) for rl in sorted({r for r, _ in rows})}}
    detail = [{"role": roles[(i, r["id"])], "fs": sizes[(i, r["id"])][0], "W": sizes[(i, r["id"])][1],
               "H": sizes[(i, r["id"])][2], "design": sizes[(i, r["id"])][3], "id": r["id"], "score": r["score"],
               "read": r["read"]} for i, j in enumerate(res) for r in j["results"]]
    Path("eval/runs/ocr_detail.json").write_text(json.dumps(detail, indent=0))
    with open("eval/runs/ocr.jsonl", "a") as f:
        f.write(json.dumps(summary) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
