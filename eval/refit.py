"""Refit the energy weights on the Claude judge's labels (PLAN_V3 §4 step 3).

Every Docker bench run judged the shipped render of each pair; across runs a pair has several distinct
renders with different numbers of major issues. Within a pair, the render with fewer majors should get the
lower energy, so the weights are fit on those ordered comparisons (pairwise logistic loss, weights kept
non-negative, ridge-pulled toward the hand-set values) and checked by leave-one-design-out accuracy.

    python -m eval.refit collect           # -> eval/runs/refit/dataset.json (terms + labels per render)
    python -m eval.refit fit [--lam 1.0]   # -> prints CV accuracy; writes engine/weights_refit.json
Then bench: CHAMELEON_WEIGHTS=/app/engine/weights_refit.json python -m eval.vision_bench --tag X --docker
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "eval" / "runs" / "vision_bench"
OUT = ROOT / "eval" / "runs" / "refit"


def collect() -> list[dict]:
    from engine.analyze import analyze
    from engine.energy import energy
    designs: dict[str, tuple] = {}
    seen: set[tuple[str, str]] = set()
    rows = []
    for run in sorted(glob.glob(str(RUNS / "v[34]*.json"))):
        tag = Path(run).stem
        for pair, v in json.load(open(run))["pairs"].items():
            png = RUNS / tag / f"{pair}.png"
            out_json = RUNS / tag / f"{pair}.json"
            if not png.is_file() or not out_json.is_file():
                continue
            h = hashlib.md5(png.read_bytes()).hexdigest()
            if (pair, h) in seen:
                continue
            seen.add((pair, h))
            name, wh = pair.split("@")
            W, H = (float(x) for x in wh.split("x"))
            if name not in designs:
                src = json.load(open(ROOT / "eval" / "corpus" / "designs" / f"{name}.json"))["source"]
                designs[name] = (src, analyze(src))
            src, d = designs[name]
            out = json.load(open(out_json))
            place = {e["id"]: e for e in out["elements"]}
            try:
                _, terms = energy(d, place, W, H)
            except Exception as exc:  # a render from an older engine may hold ids the analysis no longer places
                print("skip", tag, pair, repr(exc))
                continue
            majors = sum(1 for i in v.get("issues", []) if i.get("severity") == "major")
            rows.append({"pair": pair, "design": name, "tag": tag, "hash": h, "majors": majors,
                         "verdict": v.get("verdict"), "terms": {k: float(x) for k, x in terms.items()}})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "dataset.json").write_text(json.dumps(rows))
    print(f"{len(rows)} judged renders over {len({r['pair'] for r in rows})} pairs -> {OUT / 'dataset.json'}")
    return rows


VERDICT = {"PASS": 0, "REVIEW": 1, "FAIL": 2}


def _score(r: dict, label: str) -> float:
    return r["majors"] if label == "majors" else VERDICT.get(r["verdict"], 1) * 10 + r["majors"]


def _comparisons(rows: list[dict], keys: list[str], min_delta: int = 1, min_side: int = 0, label: str = "majors"):
    """(x, design) per ordered comparison: x = terms(worse) - terms(better), so w.x should be > 0."""
    by_pair: dict[str, list[dict]] = {}
    for r in rows:
        W, H = (int(x) for x in r["pair"].split("@")[1].split("x"))
        if min(W, H) >= min_side:
            by_pair.setdefault(r["pair"], []).append(r)
    xs, groups = [], []
    for pair, rs in by_pair.items():
        for i, a in enumerate(rs):
            for b in rs[i + 1:]:
                if abs(a["majors"] - b["majors"]) < min_delta or _score(a, label) == _score(b, label):
                    continue
                better, worse = (a, b) if _score(a, label) < _score(b, label) else (b, a)
                xs.append([worse["terms"].get(k, 0.0) - better["terms"].get(k, 0.0) for k in keys])
                groups.append(a["design"])
    return np.array(xs, dtype=float), np.array(groups)


def _fit(X: np.ndarray, w0: np.ndarray, scale: np.ndarray, lam: float, steps: int = 4000, lr: float = 0.05) -> np.ndarray:
    """Pairwise logistic loss on scaled features; w >= 0; ridge toward w0 (in scaled units)."""
    Xs = X * scale                      # scaled feature diff: w_scaled . Xs == w . X with w = w_scaled * scale
    w = w0 / scale
    n = max(1, len(Xs))
    for _ in range(steps):
        z = Xs @ w
        p = 1.0 / (1.0 + np.exp(np.clip(z, -30, 30)))     # sigmoid(-z): probability of ordering the pair wrong
        grad = -(Xs * p[:, None]).sum(axis=0) / n + lam * (w - w0 / scale)
        w = np.maximum(0.0, w - lr * grad)
    return w * scale


def _accuracy(X: np.ndarray, w: np.ndarray) -> float:
    return float((X @ w > 0).mean()) if len(X) else float("nan")


def fit(lam: float, write: bool, min_delta: int = 1, min_side: int = 0, label: str = "majors") -> None:
    from engine.energy import weights
    rows = json.load(open(OUT / "dataset.json"))
    w_hand = weights()
    keys = list(w_hand)
    w0 = np.array([w_hand[k] for k in keys], dtype=float)
    X, groups = _comparisons(rows, keys, min_delta, min_side, label)
    for k, col in zip(keys, X.T):   # how often each raw term alone orders the pair right (ties excluded)
        nz = col != 0
        if nz.any():
            print(f"  term {k:12s} alone: {float((col[nz] > 0).mean()):.2f} on {int(nz.sum())} comparisons")
    allterms = np.array([[r["terms"].get(k, 0.0) for k in keys] for r in rows])
    scale = allterms.std(axis=0)
    scale[scale == 0] = 1.0
    scale = 1.0 / scale
    print(f"{len(rows)} renders, {len(X)} ordered comparisons, {len(set(groups))} designs")
    print(f"hand weights: pairwise accuracy {_accuracy(X, w0):.3f}")
    # leave-one-design-out
    hits_hand = hits_fit = total = 0
    for g in sorted(set(groups)):
        tr, te = groups != g, groups == g
        w = _fit(X[tr], w0, scale, lam)
        hits_fit += int((X[te] @ w > 0).sum())
        hits_hand += int((X[te] @ w0 > 0).sum())
        total += int(te.sum())
    print(f"leave-one-design-out: hand {hits_hand / total:.3f}  refit {hits_fit / total:.3f}  (lam={lam})")
    w = _fit(X, w0, scale, lam)
    print(f"refit on all: pairwise accuracy {_accuracy(X, w):.3f}")
    for k, a, b in sorted(zip(keys, w0, w), key=lambda t: -abs(t[2] - t[1]) / max(t[1], 1e-6)):
        print(f"  {k:12s} {a:7.2f} -> {b:7.2f}")
    if write:
        path = ROOT / "engine" / "weights_refit.json"
        path.write_text(json.dumps({k: round(float(x), 4) for k, x in zip(keys, w)}, indent=1))
        print("wrote", path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["collect", "fit"])
    ap.add_argument("--lam", type=float, default=1.0)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--min-delta", type=int, default=1, help="ignore comparisons closer than this in majors")
    ap.add_argument("--min-side", type=int, default=0, help="ignore canvases with a shorter short side")
    ap.add_argument("--label", choices=["majors", "verdict"], default="majors")
    a = ap.parse_args()
    if a.cmd == "collect":
        collect()
    else:
        fit(a.lam, not a.no_write, a.min_delta, a.min_side, a.label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
