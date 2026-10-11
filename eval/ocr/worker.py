"""OCR worker (runs in the ImageSorcery venv, which has EasyOCR):
    <imagesorcery python> eval/ocr/worker.py jobs.json results.json
jobs: [{"png": path, "boxes": [{"id", "box": [x0,y0,x1,y1], "text": expected}]}]
Standalone on purpose: it does not import the engine (different environment)."""
import difflib
import json
import re
import sys

import easyocr
from PIL import Image
import numpy as np

reader = easyocr.Reader(["en"], gpu=False, verbose=False)


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


jobs = json.load(open(sys.argv[1]))
out = []
for j in jobs:
    im = Image.open(j["png"]).convert("RGB")
    res = []
    for b in j["boxes"]:
        x0, y0, x1, y1 = b["box"]
        pad = 4
        rot = float(b.get("rotation", 0) or 0)
        if rot:   # rotated text: crop around the rotated box, then turn it upright
            cx, cy, r = (x0 + x1) / 2, (y0 + y1) / 2, max(x1 - x0, y1 - y0) / 2 + pad
            crop = im.crop((max(0, int(cx - r)), max(0, int(cy - r)), min(im.width, int(cx + r)), min(im.height, int(cy + r))))
            crop = crop.rotate(rot, expand=True, fillcolor=(128, 128, 128))
        else:
            crop = im.crop((max(0, int(x0) - pad), max(0, int(y0) - pad),
                            min(im.width, int(x1) + pad), min(im.height, int(y1) + pad)))
        if crop.width < 4 or crop.height < 4:
            res.append({"id": b["id"], "read": "", "score": 0.0})
            continue
        got = " ".join(t for _, t, _ in reader.readtext(np.array(crop)))
        exp = norm(b["text"])
        score = difflib.SequenceMatcher(None, norm(got), exp).ratio() if exp else 1.0
        res.append({"id": b["id"], "read": got[:120], "score": round(score, 3)})
    out.append({"png": j["png"], "results": res})
json.dump(out, open(sys.argv[2], "w"))
