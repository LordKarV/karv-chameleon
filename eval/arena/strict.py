"""L1 strict hard gate: B's hard.py JSON rules (H1-H6) plus the red-team extras.

    python -m eval.arena.strict --tasks TASKS_DIR OUT_DIR [OUT_DIR ...] [--no-a]
    python -m eval.arena.strict --selftest

Extras (all exact, any FAIL vetoes):
S1 schema, Draft 2020-12 (hard.py uses Draft7) · S2 element key set equal to the source's
(closes the null-valued extra key that passes H4) · S3 element order kept · S4a unrotated box
and S4b rotated AABB inside the canvas at 0.01 px, no exemption for sources already outside ·
S5 non-cover image box aspect within 0.5%, no full-bleed exemption (case F2) · S6 no element
collapsed below 2 px on an axis that was >= 2 px, except images whose asset is missing ·
S7 text font_size >= 1. "Only x/y/width/height/font_size changed" is H4 plus S2.
H7 (PNG honesty) needs a render and is not run here.

A's engine/pipeline/validate.py runs read-only in a subprocess as a second implementation.
Strict must flag everything A flags; an A-only flag is a disagreement (grader bug or policy gap).
"""
from __future__ import annotations

import argparse
import collections
import copy
import json
import math
import os
import subprocess
from pathlib import Path

import jsonschema

from eval.grader import hard

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "chameleon_design_schema_v0.9.json").read_text())
ASSETS = ROOT / "brand_kit" / "assets"
A_ROOT = Path.home() / "projects/adobe-hackathon/starter"
A_PY = A_ROOT / ".venv/bin/python3.13"
TOL_PX = 0.01
ASPECT_TOL = 0.005
MIN_PX = 2.0


def _fail(id_, msg, el=None, **ev):
    return hard._r(id_, "FAIL", msg, el, **ev)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _outside(box, W, H):
    x0, y0, x1, y1 = box
    return x0 < -TOL_PX or y0 < -TOL_PX or x1 > W + TOL_PX or y1 > H + TOL_PX


def _element_rules(s: dict, e: dict, W, H) -> list[dict]:
    res, eid = [], e["id"]
    if set(e) != set(s):
        res.append(_fail("S2", f"keys added {sorted(set(e) - set(s))}, removed {sorted(set(s) - set(e))}", eid))
    g = [e.get(k) for k in ("x", "y", "width", "height")]
    if not all(map(_num, g)):
        return res                                  # hard H1 reports it
    x, y, w, h = g
    if _outside((x, y, x + w, y + h), W, H):
        res.append(_fail("S4a", "unrotated box leaves the canvas", eid, box=g))
    if (e.get("rotation") or 0) and _outside(hard._rot_aabb(e), W, H):
        res.append(_fail("S4b", "rotated box leaves the canvas", eid, box=[round(v, 2) for v in hard._rot_aabb(e)]))
    sw, sh = s.get("width"), s.get("height")
    if e.get("type") == "image" and e.get("fit") != "cover" and _num(sw) and _num(sh) and sw > 0 and sh > 0:
        ratio = (w / h) / (sw / sh) if w > 0 and h > 0 else 0.0
        if abs(ratio - 1) > ASPECT_TOL:
            res.append(_fail("S5", f"image aspect x{ratio:.4f} (fit={e.get('fit', 'absent')})", eid, ratio=ratio))
    missing = e.get("type") == "image" and not (ASSETS / str(e.get("asset_id"))).is_file()
    for k, v in (("width", w), ("height", h)):
        if not missing and _num(s.get(k)) and s[k] >= MIN_PX and v < MIN_PX:
            res.append(_fail("S6", f"{k} collapsed {s[k]} -> {v}", eid))
    if e.get("type") == "text" and not (_num(e.get("font_size")) and e["font_size"] >= 1):
        res.append(_fail("S7", f"font_size {e.get('font_size')!r} < 1", eid))
    return res


def strict_rules(src: dict, out: dict, target: dict) -> list[dict]:
    """hard.json_rules results plus the S-rule FAILs. Same result dicts as hard.py."""
    res = hard.json_rules(src, out, target)
    errs = sorted(jsonschema.Draft202012Validator(SCHEMA).iter_errors(out), key=str)
    if errs:
        res.append(_fail("S1", "; ".join(f"{e.message} at {'/'.join(map(str, e.absolute_path))}" for e in errs[:3])))
    els = out.get("elements") if isinstance(out, dict) else None
    if not isinstance(els, list) or not all(isinstance(e, dict) for e in els):
        return res + [_fail("S3", "no element list")]
    if [e.get("id") for e in els] != [e["id"] for e in src["elements"]]:
        res.append(_fail("S3", "element ids or order differ from the source"))
    by = {e["id"]: e for e in src["elements"]}
    W, H = target["width"], target["height"]
    for e in els:
        if e.get("id") in by:
            res += _element_rules(by[e["id"]], e, W, H)
    return res


def kind(r: dict) -> str:
    """Violation type: rule id, plus the changed fields for H4."""
    return r["id"] + (":" + ",".join(r["evidence"]["fields"]) if r["id"] == "H4" else "")


def check(src: dict, out: dict, target: dict) -> dict:
    fails = [r for r in strict_rules(src, out, target) if r["status"] == "FAIL"]
    return {"verdict": "FAIL" if fails else "PASS", "fails": fails}


# ---- engine A cross-check -------------------------------------------------------------------------------

_A_SNIPPET = """
import json, sys
from engine.pipeline.validate import problems
print(json.dumps([problems(s, t, o) for s, t, o in json.load(sys.stdin)]))
"""
# A problem substring -> strict rule ids that must also fire on the same pair (first match wins)
A_MAP = (("schema:", {"S1", "H1"}), ("ids differ", {"H2", "S3"}), ("off-canvas", {"S4a"}), ("canvas", {"H3"}),
         ("geometry not numeric", {"H1", "S1"}), ("non-positive size", {"S6", "S1", "H1"}),
         ("font_size < 1", {"S7", "S1"}), ("aspect drift", {"S5"}), ("changed", {"H4", "S2"}))


def a_problems(triples: list) -> list[list[str]]:
    """Run A's validate.problems read-only (cwd A, no bytecode writes) on [(src, target, out), ...]."""
    p = subprocess.run([str(A_PY), "-B", "-c", _A_SNIPPET], cwd=A_ROOT, input=json.dumps(triples),
                       capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if p.returncode:
        raise RuntimeError(f"A validate failed: {p.stderr[-800:]}")
    return json.loads(p.stdout)


def disagreements(a_probs: list[str], fails: list[dict]) -> list[str]:
    """A flags that no mapped strict rule backs up. (Strict-only flags are expected: it is stricter.)"""
    ids = {r["id"] for r in fails}
    need = lambda p: next((n for k, n in A_MAP if k in p), set())   # noqa: E731 - unmapped -> always disagrees
    return [p for p in a_probs if not ids & need(p)]


# ---- directory sweep -------------------------------------------------------------------------------------

def check_dir(out_dir: Path, tasks_dir: Path, use_a: bool = True) -> dict:
    rows, triples = [], []
    for tf in sorted(tasks_dir.glob("*.json")):
        task, of = json.loads(tf.read_text()), out_dir / tf.name
        if not of.is_file():
            rows.append({"task": tf.stem, "verdict": "FAIL", "kinds": ["no-output"], "a": []})
            continue
        out = json.loads(of.read_text())
        c = check(task["source"], out, task["target_canvas"])
        rows.append({"task": tf.stem, "verdict": c["verdict"], "kinds": [kind(r) for r in c["fails"]],
                     "fails": c["fails"], "a": None})
        triples.append((len(rows) - 1, (task["source"], task["target_canvas"], out)))
    if use_a and triples:
        for (i, _), probs in zip(triples, a_problems([t for _, t in triples])):
            rows[i]["a"] = probs
            rows[i]["disagree"] = disagreements(probs, rows[i]["fails"])
    return {"dir": str(out_dir), "n": len(rows), "fail_pairs": sum(r["verdict"] == "FAIL" for r in rows),
            "counts": dict(collections.Counter(k for r in rows for k in r["kinds"]).most_common()),
            "a_flag_pairs": sum(bool(r.get("a")) for r in rows),
            "disagree": [(r["task"], d) for r in rows for d in r.get("disagree") or []], "rows": rows}


# ---- self-check: corruptions that must FAIL, controls that must PASS -------------------------------------

def _base():
    """L2 sample letterboxed at scale 1 into a 2x-wide canvas, clamped inside: a strictly clean pair."""
    src = json.loads((ROOT / "input/chameleon_sample_level2_core.json").read_text())["source"]
    W, H = src["canvas"]["width"] * 2, src["canvas"]["height"]
    out = {"canvas": {"width": W, "height": H}, "elements": []}
    for e in src["elements"]:
        x = min(max(e["x"] + W / 4, 0), W - e["width"])
        y = min(max(e["y"], 0), H - e["height"])
        out["elements"].append({**e, "x": x, "y": y})
    return src, out, {"width": W, "height": H}


def _edit(out, eid, **kv):
    o = copy.deepcopy(out)
    next(e for e in o["elements"] if e["id"] == eid).update(kv)
    return o


def _bg_fit(src, out, fit):
    """F2: background with fit absent / contain / fill / cover, stretched full-bleed to the 2:1 canvas."""
    s, o = copy.deepcopy(src), _edit(out, "bg", x=0, y=0, width=out["canvas"]["width"], height=out["canvas"]["height"])
    for d in (s, o):
        bg = next(e for e in d["elements"] if e["id"] == "bg")
        bg.pop("fit", None)
        if fit:
            bg["fit"] = fit
    return s, o


def selftest():
    src, out, tgt = _base()
    W = tgt["width"]
    prod = next(e for e in out["elements"] if e["id"] == "product")
    defects = {
        "null extra key": (src, _edit(out, "headline", shadow=None)),
        "F2 bg fit absent": _bg_fit(src, out, None),
        "F2 bg fit contain": _bg_fit(src, out, "contain"),
        "F2 bg fit fill": _bg_fit(src, out, "fill"),
        "stretched image x1.2": (src, _edit(out, "product", width=prod["width"] * 1.2)),
        "stretched image x1.01": (src, _edit(out, "product", width=prod["width"] * 1.01)),
        "moved off-canvas": (src, _edit(out, "product", x=-120)),
        "0.05 px overhang": (src, _edit(out, "product", x=W - prod["width"] + 0.05)),
        "changed letter_spacing": (src, _edit(out, "headline", letter_spacing=3)),
        "element order swapped": (src, {**out, "elements": out["elements"][::-1]}),
        "collapsed to 1.5 px": (src, _edit(out, "logo", width=1.5, height=1.5 * 110 / 320)),
        "font_size 0.5": (src, _edit(out, "subhead", font_size=0.5)),
    }
    controls = {"clean base": (src, out), "F2 bg fit cover": _bg_fit(src, out, "cover")}
    cases = [(n, s, o, "FAIL") for n, (s, o) in defects.items()] + [(n, s, o, "PASS") for n, (s, o) in controls.items()]
    a = a_problems([(s, tgt, o) for _, s, o, _ in cases])
    bad = []
    for (name, s, o, want), ap in zip(cases, a):
        c = check(s, o, tgt)
        hard_fail = any(r["status"] == "FAIL" for r in hard.json_rules(s, o, tgt))
        print(f"{name:24} strict={c['verdict']:4} want={want:4} hard.py={'FAIL' if hard_fail else 'PASS':4} "
              f"A={'FAIL' if ap else 'PASS':4} {sorted({kind(r) for r in c['fails']})} "
              f"{'A-only: ' + str(disagreements(ap, c['fails'])) if disagreements(ap, c['fails']) else ''}")
        if c["verdict"] != want or disagreements(ap, c["fails"]):
            bad.append(name)
    assert not bad, f"selftest failed: {bad}"
    print(f"selftest OK: {len(defects)} corruptions FAIL, {len(controls)} controls PASS, 0 A-only disagreements")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dirs", nargs="*", type=Path)
    ap.add_argument("--tasks", type=Path)
    ap.add_argument("--no-a", action="store_true", help="skip the engine A cross-check")
    ap.add_argument("--json", type=Path, help="write the full per-pair report here")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.tasks or not a.out_dirs:
        ap.error("need --tasks and at least one OUT_DIR")
    reports = [check_dir(d, a.tasks, not a.no_a) for d in a.out_dirs]
    for r in reports:
        print(f"{Path(r['dir']).name:6} pairs={r['n']} strict_FAIL={r['fail_pairs']} A_flagged={r['a_flag_pairs']} "
              f"A-only disagreements={len(r['disagree'])}")
        for k, n in r["counts"].items():
            print(f"    {k:28} {n}")
        for t, d in r["disagree"][:10]:
            print(f"    A-only {t}: {d}")
    print("distinct violation types:", sorted({k for r in reports for k in r["counts"]}))
    if a.json:
        a.json.write_text(json.dumps(reports, indent=1, default=str))


if __name__ == "__main__":
    main()
