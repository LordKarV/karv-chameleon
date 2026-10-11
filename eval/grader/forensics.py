"""Layer forensics: exact per-element pixels from the official renderer.

For a design we render (privately, never touching the output):
- full                  the design as the judge sees it
- alone[e] on 2 keys    only e, over a magenta / green full-canvas key, on a canvas
                        padded by PAD on every side -> ink mask M_e (incl. ink that
                        falls outside the canvas)
- without[e]            the design minus e -> visible mask V_e = full != without[e]
Everything is at one fixed scale so masks line up pixel for pixel.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from renderer.converter.convert import _get_renderer, convert_chameleon_to_crello

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "brand_kit" / "assets"
FONTS = ROOT / "brand_kit" / "fonts"
MAX_SIDE = 1024          # forensics scale: long side of the canvas in px (official PNG ≤ 2048)
PAD = 0.15               # padding (fraction of the long side) to catch ink that leaves the canvas
KEYS = ((255, 0, 255), (0, 255, 0))
DIFF = 8                 # 0-255 per-channel difference that counts as a changed pixel
KEY_ID = "__grader_key__"


@dataclass
class Forensics:
    W: float
    H: float
    scale: float                        # px per design unit
    full: np.ndarray                    # HxWx3 uint8, canvas only
    ink: dict = field(default_factory=dict)       # id -> bool mask on the PADDED canvas
    visible: dict = field(default_factory=dict)   # id -> bool mask on the canvas
    pad_px: int = 0

    def inner(self, m: np.ndarray) -> np.ndarray:
        p = self.pad_px
        return m[p:p + self.full.shape[0], p:p + self.full.shape[1]]


def _render(design: dict, scale: float, W: float, H: float, assets: Path, fonts: Path) -> np.ndarray:
    r = _get_renderer(str(fonts))
    ex, _ = convert_chameleon_to_crello(design, r.font_manager, str(assets))
    side = max(1, round(max(W, H) * scale))
    png = r.render(ex, max_size=side, format="png")
    img = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    want = (max(1, round(H * scale)), max(1, round(W * scale)))
    if img.shape[:2] != want:     # renderer never upscales: force the exact scale
        img = np.asarray(Image.fromarray(img).resize(want[::-1], Image.NEAREST))
    return img


def _shifted(design: dict, dx: float, dy: float, W: float, H: float) -> dict:
    d = copy.deepcopy(design)
    d["canvas"] = {"width": W, "height": H}
    for e in d["elements"]:
        e["x"] = float(e.get("x", 0)) + dx
        e["y"] = float(e.get("y", 0)) + dy
    return d


def _key(color, W, H) -> dict:
    return {"id": KEY_ID, "type": "shape", "shape_type": "rectangle", "x": 0, "y": 0, "width": W, "height": H,
            "z_index": -10 ** 6, "fill_color": f"rgba({color[0]},{color[1]},{color[2]},1)"}


def _cache_path(design: dict, tag: str) -> Path:
    h = hashlib.sha256((json.dumps(design, sort_keys=True, default=str) + tag).encode()).hexdigest()[:24]
    return ROOT / "eval" / ".cache" / "grader" / f"{h}.npz"


def analyze(design: dict, assets: Path = ASSETS, fonts: Path = FONTS) -> Forensics:
    """Masks for every element. Cached on disk by design content."""
    W, H = float(design["canvas"]["width"]), float(design["canvas"]["height"])
    scale = min(1.0, MAX_SIDE / max(W, H))
    cp = _cache_path(design, f"{scale}|{PAD}|{assets}|v2")
    if cp.is_file():
        z = np.load(cp, allow_pickle=True)
        return Forensics(W, H, scale, z["full"], dict(z["ink"].item()), dict(z["visible"].item()), int(z["pad"]))
    full = _render(design, scale, W, H, assets, fonts)
    p = round(PAD * max(W, H))
    PW, PH = W + 2 * p, H + 2 * p
    els = design["elements"]
    f = Forensics(W, H, scale, full, pad_px=round(p * scale))
    for e in els:
        alone = []
        for k in KEYS:
            d = _shifted({"canvas": design["canvas"], "elements": [_key(k, W, H), e]}, p, p, PW, PH)
            d["elements"][0].update(x=-4, y=-4, width=PW + 8, height=PH + 8)   # overdraw: no anti-aliased frame
            img = _render(d, scale, PW, PH, assets, fonts).astype(int)
            alone.append((np.abs(img - np.array(k)) > DIFF).any(-1))
        f.ink[e["id"]] = alone[0] | alone[1]
        rest = {"canvas": design["canvas"], "elements": [x for x in els if x is not e]}
        f.visible[e["id"]] = (np.abs(full.astype(int) - _render(rest, scale, W, H, assets, fonts)) > DIFF).any(-1)
    cp.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cp, full=full, ink=np.array(f.ink, dtype=object), visible=np.array(f.visible, dtype=object),
                        pad=f.pad_px)
    return f


def render_png(design: dict, assets: Path = ASSETS, fonts: Path = FONTS) -> np.ndarray:
    """Official-size render (what engine/main.py writes): long side capped at 2048."""
    r = _get_renderer(str(fonts))
    ex, _ = convert_chameleon_to_crello(design, r.font_manager, str(assets))
    return np.asarray(Image.open(io.BytesIO(r.render(ex, max_size=2048, format="png"))).convert("RGB"))
