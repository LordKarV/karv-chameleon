"""Paired suite runner (GRADER_DESIGN section 9 step 6): champion vs challenger over one task set.

    python -m eval.arena.suite --champion ROOT --challenger ROOT --set TASKS_DIR [--out DIR] [--flag collisions:major]
    python -m eval.arena.suite --selftest

1. Each engine checkout runs as a subprocess (cwd = its root, CHAMELEON_LLM=off) over TASKS_DIR and is
   rendered with that checkout's official renderer: OUT/{champion,challenger}/<task>.{json,png}.
   Arms run one after the other: B's wall-clock budgets make outputs load-dependent until L0 lands.
2. L1: eval/arena/strict.py check_dir on both arms, if the module exists.
3. L2: B's deterministic grader (eval.grader grade(), hard + soft) on every pair of both arms.
4. Render-hash diff: OUT/changed_pairs.json lists the tasks whose PNG bytes changed (the only pairs the
   judge sees). With --flag, a changed pair is stratum "target" when the champion's render fires that
   pre-registered soft flag ("criterion" or "criterion:severity"), else "collateral".
5. OUT/summary.json: per bucket (eval.run.bucket) the changed count and soft major-flag deltas.
Never imports engine/*.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Pool
from pathlib import Path

from eval.run import bucket

ROOT = Path(__file__).resolve().parents[2]
ARMS = ("champion", "challenger")

# Same contract as the scratchpad run_engine.py: argv = TASKS_DIR OUT_DIR, cwd = engine root.
RUNNER = r"""
import json, sys, time, traceback
from pathlib import Path
sys.path.insert(0, ".")
from engine.resize import resize
from renderer.converter.convert import render_design_to_png
tasks, out = Path(sys.argv[1]), Path(sys.argv[2])
k, n = (int(sys.argv[3]), int(sys.argv[4])) if len(sys.argv) > 4 else (0, 1)
out.mkdir(parents=True, exist_ok=True)
with open(out / f"results_{k}.jsonl", "w") as log:
    for f in sorted(tasks.glob("*.json"))[k::n]:
        task, rec, t0 = json.loads(f.read_text()), {"task": f.stem}, time.time()
        try:
            res = resize(task["source"], task["target_canvas"])
            rec["engine_s"] = round(time.time() - t0, 3)
            (out / f"{f.stem}.json").write_text(json.dumps(res, indent=1))
            render_design_to_png(res, "brand_kit/assets", str(out / f"{f.stem}.png"))
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
            rec["trace"] = traceback.format_exc()[-800:]
        log.write(json.dumps(rec) + "\n")
        log.flush()
"""


def run_engine(root: Path, tasks: Path, out: Path, python: str, shards: int = 1) -> list[dict]:
    """Runs the engine over TASKS in SHARDS parallel subprocesses (task i goes to shard i % SHARDS)."""
    shutil.rmtree(out, ignore_errors=True)   # suite-owned arm dir: never mix with a stale run
    env = {**os.environ, "CHAMELEON_LLM": "off", "PYTHONDONTWRITEBYTECODE": "1"}
    procs = [subprocess.Popen([python, "-c", RUNNER, str(tasks), str(out), str(k), str(shards)], cwd=root, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True) for k in range(shards)]
    errs = [(p.wait(), p.stderr.read()) for p in procs]
    for code, err in errs:
        if code:
            raise RuntimeError(f"engine at {root} crashed: {err[-1500:]}")
    return [json.loads(x) for k in range(shards) for x in (out / f"results_{k}.jsonl").read_text().splitlines()]


def sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def _soft(job):
    """B's deterministic grader on one pair; runs in a worker process."""
    import numpy as np
    from PIL import Image
    from eval.grader.__main__ import grade
    tf, of, pf = map(Path, job)
    if not of.is_file():
        return {"error": "no output"}
    task = json.loads(tf.read_text())
    try:
        png = np.asarray(Image.open(pf).convert("RGB")) if pf.is_file() else None
        g = grade(task["source"], json.loads(of.read_text()), task["target_canvas"], png)
    except Exception as ex:   # noqa: BLE001 - one broken pair must not sink the suite
        return {"error": f"{type(ex).__name__}: {ex}"[:300]}
    flags = [f for c in g["criteria"].values() for f in c["flags"]]
    return {"verdict": g["verdict"], "hard_fails": [r["id"] for r in g["hard"] if r["status"] == "FAIL"],
            "major": sorted(f["criterion"] for f in flags if f["severity"] == "major"),
            "flags": sorted({f"{f['criterion']}:{f['severity']}" for f in flags})}


def _strict(arm_dir: Path, tasks: Path) -> dict:
    if not (Path(__file__).parent / "strict.py").is_file():
        return {"skipped": "eval/arena/strict.py not present"}
    try:
        from eval.arena import strict   # lazy: written separately
        r = strict.check_dir(arm_dir, tasks)
        return {"fail_pairs": r["fail_pairs"], "counts": r["counts"], "disagree": r["disagree"],
                "failing": sorted(x["task"] for x in r["rows"] if x["verdict"] == "FAIL")}
    except Exception as ex:   # noqa: BLE001 - report loudly, the gate result is then unknown
        print(f"WARNING strict check failed on {arm_dir}: {ex!r}", file=sys.stderr)
        return {"error": f"{type(ex).__name__}: {ex}"[:300]}


def _design(stem: str, t: dict) -> str:
    suffix = f"_{t['width']}x{t['height']}"
    return (stem[: -len(suffix)] if stem.endswith(suffix) else stem).rstrip("_")   # sets use "<design>__WxH"


def summarize(rows: list[dict]) -> dict:
    """Per bucket: pairs, changed, soft major counts per arm, and which criteria moved."""
    by = defaultdict(list)
    for r in rows:
        by[r["bucket"]].append(r)
    out = {}
    for b, rs in sorted(by.items()):
        m0 = Counter(c for r in rs for c in r["champion"].get("major", []))
        m1 = Counter(c for r in rs for c in r["challenger"].get("major", []))
        out[b] = {"pairs": len(rs), "changed": sum(r["changed"] for r in rs),
                  "majors": [sum(m0.values()), sum(m1.values())],
                  "major_delta_by_criterion": {k: m1[k] - m0[k] for k in sorted(m0 | m1) if m1[k] != m0[k]},
                  "pairs_fewer_majors": sum(len(r["challenger"].get("major", [])) < len(r["champion"].get("major", []))
                                            for r in rs),
                  "pairs_more_majors": sum(len(r["challenger"].get("major", [])) > len(r["champion"].get("major", []))
                                           for r in rs)}
    return out


def run(champion: Path, challenger: Path, tasks: Path, out: Path, python: str, flag: str | None, j: int) -> dict:
    task_files = sorted(tasks.glob("*.json"))
    if not task_files:
        raise SystemExit(f"no task files in {tasks}")
    shards = max(1, j // 2)   # both arms run at once, j processes in total
    print(f"running champion {champion} and challenger {challenger}, {shards} shards each", flush=True)
    with ThreadPoolExecutor(2) as ex:
        futs = {arm: ex.submit(run_engine, root, tasks, out / arm, python, shards) for arm, root in zip(ARMS, (champion, challenger))}
        logs = {arm: {r["task"]: r for r in f.result()} for arm, f in futs.items()}
    strict = {arm: _strict(out / arm, tasks) for arm in ARMS}
    jobs = [(str(tf), str(out / arm / tf.name), str(out / arm / f"{tf.stem}.png")) for arm in ARMS for tf in task_files]
    print(f"soft grading {len(jobs)} renders", flush=True)
    with Pool(j) as p:
        grades = p.map(_soft, jobs)
    n = len(task_files)
    rows = []
    for i, tf in enumerate(task_files):
        t = json.loads(tf.read_text())["target_canvas"]
        h = [sha(out / arm / f"{tf.stem}.png") for arm in ARMS]
        rows.append({"task": tf.stem, "design": _design(tf.stem, t), "target": f"{t['width']}x{t['height']}",
                     "bucket": bucket(t["width"], t["height"]), "hash": h, "changed": h[0] != h[1],
                     "errors": [logs[arm].get(tf.stem, {}).get("error") for arm in ARMS],
                     "champion": grades[i], "challenger": grades[n + i]})
    changed = [{"task": r["task"], "design": r["design"], "target": r["target"], "bucket": r["bucket"],
                "stratum": ("target" if flag and any(flag in (f, f.split(":")[0]) for f in r["champion"].get("flags", []))
                            else "collateral"),
                "source_png": str(tasks / "_src_png" / f"{r['task']}.png"),
                "champion_png": str(out / "champion" / f"{r['task']}.png"),
                "challenger_png": str(out / "challenger" / f"{r['task']}.png")} for r in rows if r["changed"]]
    summary = {"champion": str(champion), "challenger": str(challenger), "set": str(tasks), "flag": flag,
               "pairs": n, "changed": len(changed), "target_changed": sum(c["stratum"] == "target" for c in changed),
               "engine_errors": {arm: sum(bool(r["errors"][k]) for r in rows) for k, arm in enumerate(ARMS)},
               "grader_errors": {arm: sum("error" in r[arm] for r in rows) for arm in ARMS},
               "b_hard_fails": {arm: dict(Counter(x for r in rows for x in r[arm].get("hard_fails", []))) for arm in ARMS},
               "strict": {arm: {k: v for k, v in strict[arm].items() if k != "failing"} for arm in ARMS},
               "strict_new_fails": sorted(set(strict["challenger"].get("failing", []))
                                          - set(strict["champion"].get("failing", []))),
               "buckets": summarize(rows)}
    (out / "changed_pairs.json").write_text(json.dumps(changed, indent=1))
    (out / "pairs.json").write_text(json.dumps(rows, indent=1))
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


def selftest():
    a = {"champion": {"major": ["collisions"]}, "challenger": {"major": []}}
    rows = [{"bucket": "wide", "changed": True, **a},
            {"bucket": "wide", "changed": False, "champion": {"major": []}, "challenger": {"major": []}},
            {"bucket": "tall", "changed": True, "champion": {}, "challenger": {"major": ["presence", "presence"]}}]
    s = summarize(rows)
    assert s["wide"] == {"pairs": 2, "changed": 1, "majors": [1, 0], "major_delta_by_criterion": {"collisions": -1},
                         "pairs_fewer_majors": 1, "pairs_more_majors": 0}, s
    assert s["tall"]["major_delta_by_criterion"] == {"presence": 2} and s["tall"]["pairs_more_majors"] == 1
    assert _design("a_synth_05005_728x90", {"width": 728, "height": 90}) == "a_synth_05005"
    assert _design("o_sample_level1", {"width": 1, "height": 1}) == "o_sample_level1"
    assert _design("sample_l1_warmup__728x90", {"width": 728, "height": 90}) == "sample_l1_warmup"
    print("selftest ok")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m eval.arena.suite")
    ap.add_argument("--champion", type=Path)
    ap.add_argument("--challenger", type=Path)
    ap.add_argument("--set", type=Path, help="task dir of {source, target_canvas} files")
    ap.add_argument("--out", type=Path, default=ROOT / "eval" / "arena" / "runs" / "suite")
    ap.add_argument("--python", default=sys.executable, help="engine interpreter (default: this one, B's venv)")
    ap.add_argument("--flag", help="pre-registered L2 stratum flag, e.g. collisions:major")
    ap.add_argument("-j", type=int, default=7)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not (a.champion and a.challenger and a.set):
        ap.error("give --champion ROOT --challenger ROOT --set DIR")
    for p in (a.champion, a.challenger, a.set):
        if not p.is_dir():
            ap.error(f"not a directory: {p}")
    s = run(a.champion.resolve(), a.challenger.resolve(), a.set.resolve(), a.out.resolve(), a.python, a.flag, a.j)
    print(json.dumps(s, indent=1))
    print(f"-> {a.out / 'changed_pairs.json'} ({s['changed']} changed of {s['pairs']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
