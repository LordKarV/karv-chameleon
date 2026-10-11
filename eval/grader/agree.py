"""Grader vs human agreement on the blind labels (docs/GRADER_PLAN.md §5.2).

    python -m eval.grader.agree labels.json [--regrade] [--relabel labels2.json] [--manifest eval/grader_labels/manifest.json]

labels.json is the "Export JSON" of eval/grader_labels/index.html: {item_id: {verdict, tags, rating}}.
Reports the 3-way confusion matrix, binary fine (PASS) vs not-fine agreement, Cohen's kappa,
per-tag recall of the matching grader criterion, the human's test-retest kappa, and every disagreement.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "eval" / "grader_labels"   # label_page.OUT, without its render imports

VERDICTS = ("PASS", "REVIEW", "FAIL")
# human tag -> grader criteria / hard-rule prefixes that should fire on it
TAG_CHECKS = {"overlap": ["collisions"], "text": ["readability"], "lost": ["presence", "H5", "H2"],
              "hierarchy": ["hierarchy"], "apart": ["relations"], "space": ["composition"], "distorted": ["H6"]}


def confusion(a: list, b: list, cats=VERDICTS) -> list[list[int]]:
    """Rows: a's label, columns: b's label."""
    return [[sum(x == r and y == c for x, y in zip(a, b)) for c in cats] for r in cats]


def kappa(a: list, b: list, cats=VERDICTS) -> float:
    n = len(a)
    if not n:
        return float("nan")
    po = sum(x == y for x, y in zip(a, b)) / n
    pe = sum((a.count(c) / n) * (b.count(c) / n) for c in cats)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def fired(m: dict, check: str) -> bool:
    return any(h.startswith(check) for h in m["hard_fail"]) if check.startswith("H") else check in m["fired"]


def _matrix(cm, cats=VERDICTS, rows="human", cols="grader"):
    print(f"{rows + ' / ' + cols:16}" + "".join(f"{c:>8}" for c in cats))
    for c, r in zip(cats, cm):
        print(f"{c:16}" + "".join(f"{v:8}" for v in r))


def _job(kv):
    from eval.grader.label_page import grade_row
    k, p = kv
    return k, grade_row(p["src"], p["out"], p["target"])


def _regrade(man: dict, pairs_path: Path) -> dict:
    """Re-score with the grader as it is now (labels stay fixed: the honest way to tune it)."""
    from multiprocessing import Pool
    pairs = json.loads(pairs_path.read_text())
    with Pool(6) as p:
        rows = dict(p.map(_job, pairs.items()))
    return {k: {**v, **rows.get(k, {})} for k, v in man.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.grader.agree")
    ap.add_argument("labels", type=Path)
    ap.add_argument("--relabel", type=Path)
    ap.add_argument("--manifest", type=Path, default=OUT / "manifest.json")
    ap.add_argument("--regrade", action="store_true", help="grade pairs.json with the current grader first")
    a = ap.parse_args(argv)
    man = json.loads(a.manifest.read_text())
    if a.regrade:
        man = _regrade(man, a.manifest.parent / "pairs.json")
    lab = {k: v for k, v in json.loads(a.labels.read_text()).items() if (v or {}).get("verdict") in VERDICTS}
    ids = [k for k in lab if k in man and man[k]["verdict"] in VERDICTS]
    if not ids:
        raise SystemExit("no labelled items match the manifest (wrong manifest, or labels from another --seed?)")
    skipped = len(lab) - len(ids)
    h, g = [lab[k]["verdict"] for k in ids], [man[k]["verdict"] for k in ids]
    print(f"{len(ids)} labelled items" + (f" ({skipped} skipped: not in manifest or grader ERROR)" if skipped else ""))
    print("\nconfusion (3-way):")
    _matrix(confusion(h, g))
    exact = sum(x == y for x, y in zip(h, g))
    print(f"exact agreement {exact}/{len(ids)} = {exact / len(ids):.3f}   Cohen's kappa {kappa(h, g):.3f}")
    hb, gb = [["fine", "not"][v != "PASS"] for v in h], [["fine", "not"][v != "PASS"] for v in g]
    nb = sum(x == y for x, y in zip(hb, gb))
    print(f"\nbinary fine (PASS) vs not (REVIEW|FAIL): {nb}/{len(ids)} = {nb / len(ids):.3f}"
          f"   kappa {kappa(hb, gb, ('fine', 'not')):.3f}")
    _matrix(confusion(hb, gb, ("fine", "not")), ("fine", "not"))
    print("\nper-tag recall (human ticked the tag -> grader's matching check fired):")
    for t, checks in TAG_CHECKS.items():
        hit = [k for k in ids if t in lab[k].get("tags", [])]
        if hit:
            k = sum(any(fired(man[i], c) for c in checks) for i in hit)
            print(f"  {t:10} -> {'/'.join(checks):22} {k:3}/{len(hit):<3} = {k / len(hit):.3f}")
    if a.relabel:
        r2 = {k: v for k, v in json.loads(a.relabel.read_text()).items() if (v or {}).get("verdict") in VERDICTS}
        both = [k for k in r2 if k in lab]
        x, y = [lab[k]["verdict"] for k in both], [r2[k]["verdict"] for k in both]
        same = sum(p == q for p, q in zip(x, y))
        print(f"\nhuman test-retest on {len(both)} items: {same}/{len(both)} same, kappa {kappa(x, y):.3f}"
              f" (ceiling for grader kappa {kappa(h, g):.3f})")
        _matrix(confusion(x, y), rows="first", cols="second")
    dis = [k for k, x, y in zip(ids, h, g) if x != y]
    print(f"\n{len(dis)} disagreements:")
    for k in sorted(dis, key=lambda k: (lab[k]["verdict"], man[k]["verdict"])):
        m = man[k]
        print(f"  {k}  {m['design']}@{m['target']}  human {lab[k]['verdict']:6} grader {m['verdict']:6}"
              f"  [{m['kind']}{':' + m['defect'] if m['defect'] else ''}]  tags {lab[k].get('tags', [])}")
        for f in m["flags"]:
            print("      - " + f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
