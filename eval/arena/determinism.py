"""L0 determinism gate (GRADER_DESIGN section 2, L0): the layout measured must be the layout scored.

    python -m eval.arena.determinism [--engine ROOT] [--tasks DIR | --dev 20] [--load 6] [--out DIR]
    python -m eval.arena.determinism --selftest

Runs the engine checkout as subprocesses over one task set (default: the 3 official samples plus --dev
pairs drawn with a fixed seed from eval/corpus/designs x eval/targets.json) and checks:
- seed:  output JSON byte-identical under PYTHONHASHSEED=0 and =12345;
- load:  byte-identical again with --load busy processes hogging the CPUs;
- ids:   renaming every id and group_id (the mr_probe mapping) gives the same output once mapped back;
- order: reversing the element list gives the same element dicts, matched by id;
- msgs:  no "budget", "timeout" or "cap ... reached" lines on the engine's stdout/stderr.
Exit code 0 only when every check is clean. Never imports engine/*.
ponytail: busy processes stand in for the design's docker --cpus=1 run; add docker when a release needs it.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEEDS = ("0", "12345")
BUDGET_MSG = re.compile(r"budget|timeout|timed out|cap of \d+ reached", re.I)

# JSON only (no render): argv = TASKS_DIR OUT_DIR, cwd = engine root. Engine log lines go to stderr.
RUNNER = r"""
import json, sys, traceback
from pathlib import Path
sys.path.insert(0, ".")
from engine.resize import resize
tasks, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
for f in sorted(tasks.glob("*.json")):
    task = json.loads(f.read_text())
    try:
        (out / f.name).write_text(json.dumps(resize(task["source"], task["target_canvas"]), indent=1))
    except Exception:
        print(f"ERROR {f.stem}: {traceback.format_exc()[-600:]}", file=sys.stderr, flush=True)
"""


def run_engine(root: Path, tasks: Path, out: Path, python: str, seed: str) -> str:
    """Run the engine over tasks into out; returns its stdout+stderr."""
    shutil.rmtree(out, ignore_errors=True)
    env = {**os.environ, "CHAMELEON_LLM": "off", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": seed}
    p = subprocess.run([python, "-c", RUNNER, str(tasks), str(out)], cwd=root, env=env, capture_output=True,
                       text=True)
    if p.returncode:
        raise RuntimeError(f"engine at {root} crashed: {p.stderr[-1500:]}")
    return p.stdout + p.stderr


def rename(src: dict) -> tuple[dict, dict]:
    """Opaque rename of ids and group_ids (same mapping as $S/design/mr_probe.py). Returns (source, new->old)."""
    s = copy.deepcopy(src)
    rng = random.Random(7)
    ids = [e["id"] for e in s["elements"]]
    new = {i: f"n{j:02d}" for j, i in enumerate(rng.sample(ids, len(ids)))}
    gids = sorted({e["group_id"] for e in s["elements"] if e.get("group_id")})
    gnew = {g: f"g{j}" for j, g in enumerate(rng.sample(gids, len(gids)))}
    for e in s["elements"]:
        e["id"] = new[e["id"]]
        if e.get("group_id"):
            e["group_id"] = gnew[e["group_id"]]
    back = {v: k for k, v in new.items()} | {v: k for k, v in gnew.items()}
    return s, back


def reverse(src: dict) -> dict:
    return {**src, "elements": src["elements"][::-1]}


def unmap(out: dict, back: dict) -> dict:
    els = [{**e, "id": back[e["id"]], **({"group_id": back[e["group_id"]]} if e.get("group_id") else {})}
           for e in out["elements"]]
    return {**out, "elements": els}


def same_by_id(a: dict, b: dict) -> bool:
    """Exact equality of canvas and every element dict, matched by id (element order ignored)."""
    return a.get("canvas") == b.get("canvas") and {e["id"]: e for e in a["elements"]} == \
        {e["id"]: e for e in b["elements"]}


def count_msgs(text: str) -> int:
    return sum(bool(BUDGET_MSG.search(line)) for line in text.splitlines())


def build_tasks(dest: Path, n_dev: int) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for p in sorted((ROOT / "input").glob("*.json")):
        shutil.copy(p, dest / f"official_{p.name}")
    targets = json.loads((ROOT / "eval/targets.json").read_text())
    designs = sorted((ROOT / "eval/corpus/designs").glob("*.json"))
    for d, t in random.Random(0).sample([(d, t) for d in designs for t in targets], n_dev):
        task = {"source": json.loads(d.read_text())["source"],
                "target_canvas": {"width": t["width"], "height": t["height"]}}
        (dest / f"{d.stem}@{t['width']}x{t['height']}.json").write_text(json.dumps(task))
    return dest


def build_mr(tasks: Path, dest: Path) -> dict[str, dict]:
    """Writes <task>~ids.json and <task>~order.json; returns the ids back-maps per task."""
    dest.mkdir(parents=True, exist_ok=True)
    maps = {}
    for f in sorted(tasks.glob("*.json")):
        task = json.loads(f.read_text())
        s, maps[f.stem] = rename(task["source"])
        (dest / f"{f.stem}~ids.json").write_text(json.dumps({**task, "source": s}))
        (dest / f"{f.stem}~order.json").write_text(json.dumps({**task, "source": reverse(task["source"])}))
    return maps


def busy(n: int, python: str) -> list[subprocess.Popen]:
    return [subprocess.Popen([python, "-c", "while True: pass"]) for _ in range(n)]


def check(root: Path, tasks: Path, out: Path, python: str, load: int) -> dict:
    maps = build_mr(tasks, out / "mr_tasks")
    jobs = {"base": (tasks, SEEDS[0]), "seed": (tasks, SEEDS[1]), "mr": (out / "mr_tasks", SEEDS[0])}
    with ThreadPoolExecutor(len(jobs)) as ex:   # the three runs share the CPUs, a mild load test of their own
        logs = dict(zip(jobs, ex.map(lambda k: run_engine(root, jobs[k][0], out / k, python, jobs[k][1]), jobs)))
    hogs = busy(load, python)
    try:
        logs["load"] = run_engine(root, tasks, out / "load", python, SEEDS[0])
    finally:
        for h in hogs:
            h.kill()
    names = [f.name for f in sorted(tasks.glob("*.json"))]
    rep = {"tasks": len(names), "missing": [n for n in names if not (out / "base" / n).is_file()]}
    for arm in ("seed", "load"):
        rep[arm] = [n for n in names if (out / "base" / n).is_file()
                    and (out / arm / n).read_bytes() != (out / "base" / n).read_bytes()]
    rep["ids"], rep["order"] = [], []
    for n in names:
        stem, base = n[:-5], out / "base" / n
        if not base.is_file():
            continue
        b = json.loads(base.read_text())
        for rel in ("ids", "order"):
            f = out / "mr" / f"{stem}~{rel}.json"
            o = json.loads(f.read_text()) if f.is_file() else None
            if o is None or not same_by_id(b, unmap(o, maps[stem]) if rel == "ids" else o):
                rep[rel].append(stem)
    rep["budget_msgs"] = {k: count_msgs(v) for k, v in logs.items()}
    rep["errors"] = {k: v.count("ERROR ") for k, v in logs.items()}
    rep["ok"] = not (rep["missing"] or rep["seed"] or rep["load"] or rep["ids"] or rep["order"]
                     or any(rep["budget_msgs"].values()) or any(rep["errors"].values()))
    return rep


def selftest():
    src = {"canvas": {"width": 10, "height": 10},
           "elements": [{"id": "a", "x": 1, "group_id": "g"}, {"id": "b", "x": 2, "group_id": "g"}, {"id": "c", "x": 3}]}
    s, back = rename(src)
    assert {e["id"] for e in s["elements"]}.isdisjoint({"a", "b", "c"})
    assert unmap(s, back) == src
    assert same_by_id(src, reverse(src)) and reverse(reverse(src)) == src
    moved = copy.deepcopy(src)
    moved["elements"][2]["x"] = 3.0001
    assert not same_by_id(src, moved)
    assert count_msgs("x\ncandidate budget of 30s spent after 93 candidates\ncandidate cap of 256 reached; picking\nok") == 2
    with tempfile.TemporaryDirectory() as t:
        tasks = build_tasks(Path(t) / "tasks", 2)
        assert len(list(tasks.glob("*.json"))) == len(list((ROOT / "input").glob("*.json"))) + 2
        maps = build_mr(tasks, Path(t) / "mr")
        assert len(list((Path(t) / "mr").glob("*.json"))) == 2 * len(maps)
    print("determinism selftest ok")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--engine", type=Path, default=ROOT, help="engine checkout root")
    ap.add_argument("--tasks", type=Path, help="task dir ({source, target_canvas}); default: official + --dev")
    ap.add_argument("--dev", type=int, default=20, help="dev pairs added to the 3 official samples")
    ap.add_argument("--load", type=int, default=6, help="busy processes during the load run")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", type=Path, default=ROOT / "eval/arena/runs/determinism")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    for sub in ("tasks", "mr_tasks"):   # only our own subdirs: --out may be a shared folder
        shutil.rmtree(a.out / sub, ignore_errors=True)
    tasks = a.tasks or build_tasks(a.out / "tasks", a.dev)
    rep = check(a.engine.resolve(), tasks.resolve(), a.out.resolve(), a.python, a.load)
    (a.out / "report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep, indent=1))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
