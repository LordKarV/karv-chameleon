"""Failure mining on D (GRADER_DESIGN.md section 7 step 1): sample, cluster P-OFF defect tags, contact sheet.

    python -m eval.arena.mine sample --n 150 --out SAMPLE_DIR          # stratified task copies
    python -m eval.arena.mine cluster --poff POFF.jsonl --tasks SAMPLE_DIR --renders DIR --out MD [--sheet PNG]
    python -m eval.arena.mine --selftest

Strata are level x shape, where shape is eval.run.bucket with strips split out (aspect >= 3.5 or <= 0.3);
strips get STRIP_WEIGHT x their share. A defect tag is joined to an element by its quoted text, else by keywords,
and the element's role comes from B engine/analyze.py run on the source (importing engine/* is fine here: mining
never judges a change). Clusters are (defect class, role, shape), ranked by count x distinct designs.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
from itertools import zip_longest
import sys
from collections import defaultdict
from pathlib import Path

from eval.run import bucket

ROOT = Path(__file__).resolve().parents[2]
D_SET = ROOT / "eval" / "arena" / "sets" / "D.json"
STRIP_WEIGHT = 2.0
SEED = 20261030


def shape(w: int, h: int) -> str:
    return "strip" if w / h >= 3.5 or w / h <= 0.3 else bucket(w, h)


def sample(n: int, seed: int = SEED) -> list[dict]:
    entries = json.loads(D_SET.read_text())["entries"]
    cells = defaultdict(list)
    for e in entries:
        cells[(e["level"], shape(e["target"]["width"], e["target"]["height"]))].append(e)
    weight = {k: len(v) * (STRIP_WEIGHT if k[1] == "strip" else 1) for k, v in cells.items()}
    tot = sum(weight.values())
    quota = {k: min(len(cells[k]), max(2, round(n * w / tot))) for k, w in weight.items()}
    rng, out = random.Random(seed), []
    for k in sorted(cells):
        by_design = defaultdict(list)          # round-robin over designs: spread, not 5 targets of one design
        for e in cells[k]:
            by_design[e["design"]].append(e)
        lists = [rng.sample(v, len(v)) for _, v in sorted(by_design.items())]
        rng.shuffle(lists)
        picked = [x for row in zip_longest(*lists) for x in row if x]
        out += picked[: quota[k]]
    return out[:n] if len(out) > n else out


def write_sample(n: int, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for e in sample(n):
        shutil.copy(ROOT / e["task"], out / Path(e["task"]).name)
    (out / "_sample.json").write_text(json.dumps([{k: e[k] for k in ("id", "design", "level", "bucket")} for e in sample(n)]))


# ---------------------------------------------------------------- tag -> element -> role

KEYWORDS = [(k, role) for role, ks in (     # first match wins: specific nouns before generic ones
    ("logo", "logo wordmark"), ("cta", "button cta"), ("badge", "badge pill"),
    ("image", "bottle can jar product illustration photo picture image graphic"),
    ("headline", "headline title"), ("legal", "legal fine footnote disclaimer nutrition"),
    ("subhead", "subhead subline tagline"), ("body", "body"),
    ("decor", "dot bokeh blob bubble sparkle speck circle"), ("panel", "shape panel card divider line"),
    ("layout", "empty gap area margin"), ("text", "text copy")) for k in ks.split()]


def roles(source: dict) -> dict:
    """element id -> analyze.py role (background / decor for the non-flow elements)."""
    from engine.analyze import _flat, analyze
    d = analyze(source)
    r = {e["id"]: "background" for e in d.background} | {e["id"]: "decor" for e in d.decor}
    def walk(u, outer):
        role = u.role if u.role != "other" else outer
        for c in u.children:
            walk(c, role)
        for e in _flat(u):
            r.setdefault(e["id"], role)
    for u in d.units:
        walk(u, "other")
    return r


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()


def join(tag_element: str, source: dict, role_of: dict) -> str:
    """Role of the element a P-OFF defect names: its quoted text first, then keywords, else 'unknown'."""
    said = _norm(tag_element)
    for q in [m for m in re.findall(r"[\"'‘“]([^\"'’”]{3,})", tag_element)] + [tag_element]:
        q = _norm(q)
        if len(q) < 3:
            continue
        for e in source["elements"]:
            c = _norm(str(e.get("content", "")))
            if e["type"] == "text" and c and (q in c or (len(c) >= 4 and c in q)):
                return role_of.get(e["id"], "text")
    for k, role in KEYWORDS:
        if re.search(rf"\b{k}", said):
            return role
    return "unknown"


def cluster(poff: Path, tasks: Path) -> list[dict]:
    rows = [json.loads(x) for x in poff.read_text().splitlines() if x.strip()]
    cl = defaultdict(lambda: {"tasks": [], "designs": set(), "major": 0})
    cache = {}
    for r in rows:
        task = json.loads((tasks / f"{r['task']}.json").read_text())
        W, H = task["target_canvas"]["width"], task["target_canvas"]["height"]
        if r["design"] not in cache:
            cache[r["design"]] = roles(task["source"])
        seen = set()
        for d in r.get("defects") or []:
            key = (d["class"], join(d["element"], task["source"], cache[r["design"]]), shape(W, H))
            if key in seen:       # one count per render per cluster
                continue
            seen.add(key)
            c = cl[key]
            c["tasks"].append(r["task"])
            c["designs"].add(r["design"])
            c["major"] += d["severity"] == "major"
    out = [{"class": k[0], "role": k[1], "shape": k[2], "count": len(v["tasks"]), "designs": len(v["designs"]),
            "major": v["major"], "tasks": v["tasks"]} for k, v in cl.items()]
    return sorted(out, key=lambda c: (-c["count"] * c["designs"], -c["major"], c["class"]))


def sheet(top: list[dict], tasks: Path, renders: Path, out: Path, per=3, thumb=220) -> None:
    """Rows = clusters, each cell = source | output for one example."""
    from PIL import Image, ImageDraw
    from eval.arena.judge import source_png
    cell_w, lab = 2 * thumb + 10, 18
    img = Image.new("RGB", (per * (cell_w + 12) + 10, len(top) * (thumb + 2 * lab + 12) + 10), "white")
    dr = ImageDraw.Draw(img)
    for i, c in enumerate(top):
        y = 10 + i * (thumb + 2 * lab + 12)
        dr.text((10, y), f"#{i + 1} {c['class']} / {c['role']} / {c['shape']}  n={c['count']} designs={c['designs']}",
                fill="black")
        for j, t in enumerate(list(dict.fromkeys(c["tasks"]))[:per]):
            x = 10 + j * (cell_w + 12)
            task = json.loads((tasks / f"{t}.json").read_text())
            for k, p in enumerate((source_png(tasks, t, task), renders / f"{t}.png")):
                im = Image.open(p).convert("RGB")
                im.thumbnail((thumb, thumb))
                img.paste(im, (x + k * (thumb + 10), y + lab))
                dr.rectangle([x + k * (thumb + 10) - 1, y + lab - 1, x + k * (thumb + 10) + im.width, y + lab + im.height],
                             outline="#999")
            dr.text((x, y + lab + thumb + 2), t[:60], fill="#333")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)


def report(cl: list[dict], n_renders: int, top=10) -> str:
    lines = [f"# Mined clusters (P-OFF on {n_renders} champion renders of D)", "",
             "Key = (defect class, analyze.py role of the named element, shape). Rank = count x distinct designs.", "",
             "| # | class | role | shape | count | major | designs | examples |", "|---|---|---|---|---|---|---|---|"]
    for i, c in enumerate(cl[:top]):
        ex = ", ".join(list(dict.fromkeys(c["tasks"]))[:3])
        lines.append(f"| {i + 1} | {c['class']} | {c['role']} | {c['shape']} | {c['count']} | {c['major']} | "
                     f"{c['designs']} | {ex} |")
    return "\n".join(lines) + "\n"


def selftest() -> int:
    s = sample(150)
    assert len(s) == 150 == len({e["id"] for e in s}), len(s)
    sh = [shape(e["target"]["width"], e["target"]["height"]) for e in s]
    share = sh.count("strip") / len(s)
    pop = [shape(e["target"]["width"], e["target"]["height"]) for e in json.loads(D_SET.read_text())["entries"]]
    assert share > 1.5 * pop.count("strip") / len(pop), share     # strips over-weighted
    assert {e["level"] for e in s} == {1, 2, 3} and len({e["design"] for e in s}) >= 25
    assert sample(150) == s, "sample must be deterministic"
    src = {"canvas": {"width": 100, "height": 100}, "elements": [
        {"id": "t1", "type": "text", "content": "Summer Sale!"}, {"id": "i1", "type": "image"}]}
    assert join("'SUMMER SALE' headline", src, {"t1": "headline"}) == "headline"
    assert join("the brand logo", src, {}) == "logo" and join("something", src, {}) == "unknown"
    print("mine selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("sample")
    p.add_argument("--n", type=int, default=150)
    p.add_argument("--out", type=Path, required=True)
    q = sub.add_parser("cluster")
    q.add_argument("--poff", type=Path, required=True)
    q.add_argument("--tasks", type=Path, required=True)
    q.add_argument("--renders", type=Path, required=True)
    q.add_argument("--out", type=Path, required=True, help="markdown report")
    q.add_argument("--sheet", type=Path)
    q.add_argument("--json", type=Path, help="all clusters as JSON")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.cmd == "sample":
        write_sample(a.n, a.out)
        return 0
    if a.cmd == "cluster":
        cl = cluster(a.poff, a.tasks)
        n = sum(1 for x in a.poff.read_text().splitlines() if x.strip())
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(report(cl, n))
        if a.json:
            a.json.write_text(json.dumps(cl, indent=1))
        if a.sheet:
            sheet(cl[:5], a.tasks, a.renders, a.sheet)
        print(report(cl, n))
        return 0
    ap.error("sample, cluster or --selftest")
    return 2


if __name__ == "__main__":
    sys.exit(main())
