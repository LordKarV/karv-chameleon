"""Freeze the arena benchmark sets D, V, L, H (GRADER_DESIGN.md section 5, build step 3).

  python -m eval.arena.sets             # (re)build eval/arena/sets/{D,V,L,H}.json + sets/tasks/<set>/
  python -m eval.arena.sets --report    # bucket x level counts of the frozen manifests
  python -m eval.arena.sets --selftest  # rebuild in a temp dir twice, check it matches the frozen sets

Each manifest entry: id, design, source (origin, "A:" or "B:" relative path or generator call),
source_sha256 (sha256 of the canonical source JSON, sort_keys and compact separators), target
{width, height}, level, bucket (eval.run.bucket) and task (task file path relative to the repo).
Task files hold only {source, target_canvas}, so a set folder feeds run_engine.py directly.

D  B eval/corpus (25 designs, which already include the 3 official samples) x 25 eval/targets.json,
   plus the 3 official tasks on their own targets. Crossing the samples with the 25 targets again
   would only duplicate 75 corpus pairs.
V  A grader/tasks_agent unique sources x 5 targets: 1 catalogue strip, 3 catalogue targets from
   3 distinct buckets (the dropped bucket rotates), 1 unseen real format (cycled).
L  A grader/tasks_big_holdout + tasks_holdout x 2 targets: 1 catalogue (bucket cycled), 1 unseen.
H  A grader.qa_configs._edge_tasks(500) (fuzz seeds, mutate_sample, samples on extreme targets,
   degenerate sources) + A grader/fuzz_failures that load. Run in A's own venv as a subprocess so
   A's `engine` package never enters this process.
"""
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from eval.run import bucket

ROOT = Path(__file__).resolve().parents[2]
A_ROOT = Path.home() / "projects/adobe-hackathon/starter"
A_PY = A_ROOT / ".venv/bin/python3.13"
OUT = ROOT / "eval/arena/sets"
SEED = 20261030
N_FUZZ = 500
UNSEEN = [(468, 60), (970, 90), (120, 600), (300, 1050), (336, 280), (250, 250), (1080, 566),
          (1000, 2100), (1128, 191), (1920, 1005), (1242, 2688), (3840, 2160)]
BUCKETS = ["tall", "square", "wide", "ultrawide"]
CATALOGUE = [(t["width"], t["height"]) for t in json.loads((ROOT / "eval/targets.json").read_text())]


def sha(source) -> str:
    return hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def is_strip(w: int, h: int) -> bool:
    return max(w / h, h / w) >= 3


def level_of(task: dict, default: int) -> int:
    m = re.search(r"(?:level_|^L)(\d)", str(task.get("difficulty", "")))
    if m:
        return int(m[1])
    if task.get("difficulty") == "synthetic":
        # ponytail: element-count proxy for A synth tasks (tasks_agent: L1 <= 8, L2 11-15, L3 >= 16)
        n = len(task["source"]["elements"])
        return 1 if n <= 9 else 2 if n <= 15 else 3
    return default


def _entry(set_name, tid, design, origin, source, w, h, level):
    return {"id": tid, "design": design, "source": origin, "source_sha256": sha(source),
            "target": {"width": w, "height": h}, "level": level, "bucket": bucket(w, h),
            "task": f"eval/arena/sets/tasks/{set_name}/{tid}.json", "_source": source}


def _unique_sources(paths):
    """First task file per distinct source, keyed by its design id (task_id minus the WxH suffix)."""
    seen, out = set(), []
    for p in paths:
        t = json.loads(p.read_text())
        if sha(t["source"]) not in seen:
            seen.add(sha(t["source"]))
            out.append((re.sub(r"_\d+x\d+$", "", t.get("task_id") or p.stem), p, t))
    return out


def build_d():
    out = []
    for p in sorted((ROOT / "eval/corpus/designs").glob("*.json")):
        d = json.loads(p.read_text())
        for w, h in CATALOGUE:
            out.append(_entry("D", f"{d['design_id']}__{w}x{h}", d["design_id"], f"B:{p.relative_to(ROOT)}",
                              d["source"], w, h, d["level"]))
    for p in sorted((ROOT / "input").glob("chameleon_sample_*.json")):
        t = json.loads(p.read_text())
        w, h = t["target_canvas"]["width"], t["target_canvas"]["height"]
        out.append(_entry("D", f"official_{t['task_id']}__{w}x{h}", f"official_{t['task_id']}",
                          f"B:{p.relative_to(ROOT)}", t["source"], w, h, level_of(t, 0)))
    return out


def build_v(rng):
    srcs = _unique_sources(sorted((A_ROOT / "grader/tasks_agent").glob("*.json")))
    by_bucket = {b: [t for t in CATALOGUE if bucket(*t) == b] for b in BUCKETS}
    strips = [t for t in CATALOGUE if is_strip(*t)]
    unseen = rng.sample(UNSEEN, len(UNSEEN))
    out = []
    for i, (design, p, t) in enumerate(srcs):
        canvas = (t["source"]["canvas"]["width"], t["source"]["canvas"]["height"])
        picks = [rng.choice([s for s in strips if s != canvas])]
        for b in BUCKETS[:i % 4] + BUCKETS[i % 4 + 1:]:  # 3 buckets, the dropped one rotates
            picks.append(rng.choice([s for s in by_bucket[b] if s not in picks and s != canvas]))
        picks.append(unseen[i % len(unseen)])
        out += [_entry("V", f"{design}__{w}x{h}", design, f"A:{p.relative_to(A_ROOT)}", t["source"], w, h,
                       level_of(t, 0)) for w, h in picks]
    return out


def build_l(rng):
    paths = sorted((A_ROOT / "grader/tasks_big_holdout").glob("*.json")) + sorted((A_ROOT / "grader/tasks_holdout").glob("*.json"))
    unseen = rng.sample(UNSEEN, len(UNSEEN))
    out = []
    for i, (design, p, t) in enumerate(_unique_sources(paths)):
        canvas = (t["source"]["canvas"]["width"], t["source"]["canvas"]["height"])
        cat = rng.choice([s for s in CATALOGUE if bucket(*s) == BUCKETS[i % 4] and s != canvas])
        out += [_entry("L", f"{design}__{w}x{h}", design, f"A:{p.relative_to(A_ROOT)}", t["source"], w, h,
                       level_of(t, 0)) for w, h in (cat, unseen[i % len(unseen)])]
    return out


def _valid(t) -> bool:
    tc, s = t.get("target_canvas"), t.get("source")
    return (isinstance(s, dict) and isinstance(tc, dict) and all(isinstance(tc.get(k), int) and tc[k] > 0
                                                                  for k in ("width", "height")))


def build_h():
    code = f"import json; from grader.qa_configs import _edge_tasks; print(json.dumps(_edge_tasks({N_FUZZ})))"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "CHAMELEON_LLM": "off"}
    res = subprocess.run([str(A_PY), "-c", code], cwd=A_ROOT, env=env, capture_output=True, text=True, check=True)
    tasks = [(t["task_id"], f"A:grader.qa_configs._edge_tasks({N_FUZZ})[{t['task_id']}]", t)
             for t in json.loads(res.stdout)]
    skipped = []
    for p in sorted((A_ROOT / "grader/fuzz_failures").glob("*.json")):
        try:
            t = json.loads(p.read_text())
        except ValueError:
            t = {}
        if _valid(t):
            tasks.append((f"ff_{p.stem}", f"A:{p.relative_to(A_ROOT)}", t))
        else:
            skipped.append(p.name)
    out, seen = [], set()
    for tid, origin, t in tasks:  # fuzz_failures often repeat a fuzz seed: keep the first copy
        w, h = t["target_canvas"]["width"], t["target_canvas"]["height"]
        if not _valid(t) or (sha(t["source"]), w, h) in seen:
            continue
        seen.add((sha(t["source"]), w, h))
        # ponytail: fuzz and degenerate sources count as L3 (the brief's arbitrary-content level)
        out.append(_entry("H", f"{tid}__{w}x{h}", tid, origin, t["source"], w, h, level_of(t, 3)))
    if skipped:
        print(f"H: skipped {len(skipped)} fuzz_failures that do not load: {skipped[:5]}", file=sys.stderr)
    return out


def build(out_dir: Path) -> dict:
    rng = random.Random(SEED)
    sets = {"D": build_d(), "V": build_v(rng), "L": build_l(rng), "H": build_h()}
    for name, entries in sets.items():
        ids = [e["id"] for e in entries]
        assert len(ids) == len(set(ids)), f"{name}: duplicate ids"
        tdir = out_dir / "tasks" / name
        shutil.rmtree(tdir, ignore_errors=True)
        tdir.mkdir(parents=True)
        for e in entries:
            (tdir / f"{e['id']}.json").write_text(json.dumps(
                {"source": e.pop("_source"), "target_canvas": e["target"]}, separators=(",", ":")))
        (out_dir / f"{name}.json").write_text(json.dumps(
            {"set": name, "seed": SEED, "n": len(entries), "entries": entries}, indent=1, sort_keys=True) + "\n")
    return sets


def report(out_dir: Path = OUT) -> str:
    lines = []
    for name in "DVLH":
        m = json.loads((out_dir / f"{name}.json").read_text())
        c = Counter((e["bucket"], e["level"]) for e in m["entries"])
        levels = sorted({lv for _, lv in c})
        lines.append(f"{name}: {m['n']} tasks, {len({e['design'] for e in m['entries']})} designs")
        lines.append("  bucket     " + " ".join(f"L{lv:<4}" for lv in levels))
        lines += [f"  {b:<10} " + " ".join(f"{c[b, lv]:<5}" for lv in levels) for b in BUCKETS]
    return "\n".join(lines)


def selftest():
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        build(Path(a))
        build(Path(b))
        for name in "DVLH":
            ma, mb = (json.loads((Path(d) / f"{name}.json").read_text()) for d in (a, b))
            assert ma == mb, f"{name}: build is not deterministic"
            if (OUT / f"{name}.json").is_file():
                assert ma == json.loads((OUT / f"{name}.json").read_text()), f"{name}: frozen manifest drifted"
            for e in ma["entries"]:
                t = json.loads((Path(a) / "tasks" / name / f"{e['id']}.json").read_text())
                assert set(t) == {"source", "target_canvas"} and t["target_canvas"] == e["target"]
                assert sha(t["source"]) == e["source_sha256"]
                assert e["bucket"] == bucket(e["target"]["width"], e["target"]["height"])
                assert e["level"] in (1, 2, 3), e
        d, v, l = (json.loads((Path(a) / f"{n}.json").read_text())["entries"] for n in "DVL")
        assert len(d) == 25 * 25 + 3
        assert all(sum(1 for x in v if x["design"] == g) == 5 for g in {x["design"] for x in v})
        assert all(sum(1 for x in l if x["design"] == g) == 2 for g in {x["design"] for x in l})
        strip_share = sum(is_strip(x["target"]["width"], x["target"]["height"]) for x in v) / len(v)
        assert strip_share > 0.25, strip_share  # strips over-weighted vs 8/25 of 4 catalogue slots
        print(report(Path(a)))
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--report" in sys.argv:
        print(report())
    else:
        build(OUT)
        print(report())
