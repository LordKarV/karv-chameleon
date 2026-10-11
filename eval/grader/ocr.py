"""C2b: can a reader actually read it? OCR every text at the official PNG size, in
the source and in the output, and flag text the source let you read but the output
does not (too small, covered, on a clashing background, clipped). Source-relative:
a script font EasyOCR never reads is not the output's fault.

Runs eval/ocr/worker.py (EasyOCR) in the ImageSorcery venv; results cached on disk.
No OCR available -> no flags, plus a note (never a crash).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from eval.grader import forensics

ROOT = Path(__file__).resolve().parents[2]
PY = os.environ.get("OCR_PYTHON", str(Path.home() / ".local/pipx/venvs/imagesorcery-mcp/bin/python"))
CACHE = ROOT / "eval" / ".cache" / "grader_ocr"
READ_OK = 0.8        # source reads this well -> a reader is expected to read it
LOST = 0.5           # output reads below this ...
DROP = 0.3           # ... and at least this much worse than the source -> flag
GONE = 0.3           # below this the text is effectively unreadable -> major


def _boxes(design: dict, s: float) -> list:
    return [{"id": e["id"], "text": e["content"], "rotation": e.get("rotation", 0),
             "box": [e["x"] * s, e["y"] * s, (e["x"] + e["width"]) * s, (e["y"] + e["height"]) * s]}
            for e in design["elements"] if e["type"] == "text" and str(e.get("content", "")).strip()]


def read(designs: list[dict], assets: Path = forensics.ASSETS) -> list[dict] | None:
    """{id: score 0-1} per design (official-size render), or None if OCR is unavailable."""
    tmp = Path(tempfile.mkdtemp(prefix="grader_ocr_"))
    jobs, keys, res = [], [], [None] * len(designs)
    for i, d in enumerate(designs):
        png = forensics.render_png(d, assets)
        k = hashlib.sha256(png.tobytes() + json.dumps(_boxes(d, 1), sort_keys=True).encode()).hexdigest()[:24]
        if (CACHE / f"{k}.json").is_file():
            res[i] = json.loads((CACHE / f"{k}.json").read_text())
            continue
        p = tmp / f"{i}.png"
        Image.fromarray(png).save(p)
        jobs.append({"png": str(p), "boxes": _boxes(d, png.shape[1] / d["canvas"]["width"])})
        keys.append((i, k))
    if jobs:
        if not Path(PY).is_file():
            return None
        (tmp / "jobs.json").write_text(json.dumps(jobs))
        r = subprocess.run([PY, str(ROOT / "eval/ocr/worker.py"), str(tmp / "jobs.json"), str(tmp / "res.json")],
                           capture_output=True, text=True)
        if r.returncode:
            return None
        CACHE.mkdir(parents=True, exist_ok=True)
        for (i, k), j in zip(keys, json.loads((tmp / "res.json").read_text())):
            res[i] = {x["id"]: x["score"] for x in j["results"]}
            (CACHE / f"{k}.json").write_text(json.dumps(res[i]))
    return res


def flags(src: dict, out: dict, assets: Path = forensics.ASSETS) -> list | None:
    from eval.grader.soft import _flag
    got = read([src, out], assets)
    if got is None:
        return None
    s0, s1 = got
    texts = [e for e in src["elements"] if e["type"] == "text" and str(e.get("content", "")).strip()]
    top = max(texts, key=lambda e: e["font_size"])["id"] if texts else None
    res = []
    for k, was in s0.items():
        now = s1.get(k)
        if now is None or was < READ_OK or now >= LOST or was - now < DROP:
            continue
        res.append(_flag("readability", "major" if now < GONE or k == top else "minor",
                         f"{k} no longer reads (OCR {now:.0%} vs {was:.0%} in the source)", (k,), ocr=now, was=was))
    return res


if __name__ == "__main__":     # self-check: identical design -> no flags; 3px headline -> flagged
    from eval.grader import corrupt
    src = json.loads((ROOT / "input" / "chameleon_sample_level2_core.json").read_text())["source"]
    assert flags(src, src) == [], flags(src, src)
    bad = flags(src, corrupt.tiny_text(src, src))
    assert bad and bad[0]["severity"] == "major", bad
    print("ok", bad[0]["msg"])
