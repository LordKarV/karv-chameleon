"""Acceptance decision for one engine change (GRADER_DESIGN section 6.1), from the blind pairwise panel
on CHANGED pairs only. Unchanged pairs (same render hash) are never judged: their paired difference is 0.

    python -m eval.arena.accept register CHANGE --commit SHA [--cluster C --flag F --direction better]
    python -m eval.arena.accept decide CHANGE records.jsonl [--canary]
    python -m eval.arena.accept --sim         # operating characteristics, asserted against section 6.2
    python -m eval.arena.accept --selftest

`register` must run before any judging; `decide` refuses a change with no registration. A registration
without --flag is a compliance / bug-fix / refactor change: non-inferiority only, every pair collateral.
Both append to eval/arena/ledger.jsonl.

records.jsonl, one line per changed (design, target) pair:
  {"design": "agent_00012", "target": "728x90", "bucket": "ultrawide", "stratum": "target"|"collateral",
   "votes": [1, 1, 0, -1],           # one per judgement: +1 challenger, -1 champion, 0 tie
   "fail0": false, "fail1": false}   # optional: unanimous P-OFF FAIL before / after
--canary: a Goodhart canary moved toward hiding beyond its A/A band and no inventory P-OFF has cleared it.
Steps 1-3 (L0, L1, render-hash diff) are gates in suite.py; this module covers steps 4-9.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

P0, P1, ALPHA, BETA = 0.5, 0.65, 0.05, 0.20
UP, DOWN = math.log((1 - BETA) / ALPHA), math.log(BETA / (1 - ALPHA))   # +2.77 / -1.56
STEP_W, STEP_L = math.log(P1 / P0), math.log((1 - P1) / (1 - P0))       # +0.262 / -0.357
MAX_DECIDED = 150     # truncate the SPRT here, then a sign test decides
N_BOOT = 2000         # design-cluster bootstrap resamples
MARGIN = 0.10         # collateral non-inferiority: 5th pct of mean d >= -MARGIN
MIN_COL = 60          # below this many collateral pairs, use the net-loss count rule
MAX_COL_NET_LOSS = 3
BUCKET_NET = 4        # a shape bucket losing this many decided pairs net rejects
LEDGER = Path(__file__).with_name("ledger.jsonl")

# section 6.2 table: (name, (n_tgt, better, worse, n_col, better, worse[, shared taste p]), expected P(accept))
OC_ROWS = [
    ("null: 60 tgt + 120 col near-ties", (60, 0, 0, 120, 0, 0), 0.01),
    ("different-not-better 10/10 both", (60, .10, .10, 120, .10, .10), 0.02),
    ("cluster fix 60 tgt 35/5, col null", (60, .35, .05, 120, 0, 0), 0.84),
    ("same fix, collateral sample 80", (60, .35, .05, 80, 0, 0), 0.75),
    ("same fix, 30 collateral (count rule)", (60, .35, .05, 30, 0, 0), 0.81),
    ("same fix, collateral 10/10", (60, .35, .05, 120, .10, .10), 0.67),
    ("cluster fix 40 tgt 35/5", (40, .35, .05, 120, 0, 0), 0.60),
    ("weaker fix 60 tgt 20/5", (60, .20, .05, 120, 0, 0), 0.38),
    ("weaker fix 100 tgt 20/5", (100, .20, .05, 120, 0, 0), 0.54),
    ("weaker fix 120 tgt 20/5", (120, .20, .05, 120, 0, 0), 0.63),
    ("fix, collateral harm 0/10", (60, .35, .05, 120, 0, .10), 0.20),
    ("fix, collateral harm 5/15", (60, .35, .05, 120, .05, .15), 0.18),
    ("diffuse 10/5 over 250, all target", (250, .10, .05, 0, 0, 0), 0.29),
    ("Claude-only taste: near-ties p=0.65", (60, 0, 0, 120, 0, 0, 0.65), 0.90),
]
OC_TOL = 0.10   # 300 trials: Monte Carlo SE <= 0.03, and the bootstrap draws differ from oc_sim.py


def decided(votes):
    """+1 / -1 only when every judgement agrees (ties and splits are 0): filters near-ties, which judges coin-flip."""
    return votes[0] if votes and votes[0] != 0 and all(v == votes[0] for v in votes) else 0


def binom_tail(k, n):
    """P(X >= k), X ~ Bin(n, 0.5): one-sided sign-test p-value."""
    return sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n if n else 1.0


def sprt_blocked(tgt, rng):
    """Truncated SPRT on decided signs, boundaries checked only between design blocks."""
    by = defaultdict(list)
    for r in tgt:
        by[r["design"]].append(r["d"])
    order = list(by)
    rng.shuffle(order)   # ponytail: random block order on a finished file; feed arrival order once judging streams
    llr = w = l = 0
    for k in order:
        for s in by[k]:
            if s:
                w, l = w + (s > 0), l + (s < 0)
                llr += STEP_W if s > 0 else STEP_L
        if llr >= UP:
            return "accept", w, l
        if llr <= DOWN:
            return "reject", w, l
        if w + l >= MAX_DECIDED:
            break
    return ("accept" if binom_tail(w, w + l) < ALPHA else "reject"), w, l


def boot_p05(recs, rng, n_boot=N_BOOT):
    """5th percentile of mean d, resampling DESIGNS (pairs of one design are not independent)."""
    by = defaultdict(lambda: [0, 0])
    for r in recs:
        by[r["design"]][0] += r["d"]
        by[r["design"]][1] += 1
    cells = list(by.values())
    means = sorted(sum(c[0] for c in pick) / sum(c[1] for c in pick)
                   for pick in (rng.choices(cells, k=len(cells)) for _ in range(n_boot)))
    return means[int(0.05 * n_boot)]


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def decide(recs, superiority=True, canary=False, n_boot=N_BOOT, seed=0):
    """Steps 4-9 of section 6.1. Returns every check, so a reject says which step failed."""
    rng = random.Random(seed)
    recs = [{**r, "d": decided(r["votes"])} for r in recs]
    tgt = [r for r in recs if superiority and r.get("stratum") == "target"]
    col = [r for r in recs if not (superiority and r.get("stratum") == "target")]
    out, checks = {"n_target": len(tgt), "n_collateral": len(col)}, {}
    if superiority:
        verdict, w, l = sprt_blocked(tgt, rng) if tgt else ("reject", 0, 0)
        out.update(sprt=verdict, wins=w, losses=l)
        checks["4_sprt"] = verdict == "accept"
        if tgt:
            out["target_p05"] = boot_p05(tgt, rng, n_boot)
            designs = {r["design"] for r in tgt}
            out["leave_one_min"] = min(_mean([r["d"] for r in tgt if r["design"] != d]) if len(designs) > 1
                                       else 0.0 for d in designs)
        checks["4_target_p05>0"] = out.get("target_p05", 0) > 0
        checks["8_leave_one_design_out"] = out.get("leave_one_min", 0) > 0
    cw, cl = sum(r["d"] > 0 for r in col), sum(r["d"] < 0 for r in col)
    out["collateral_wins_losses"] = [cw, cl]
    if len(col) >= MIN_COL:
        out["collateral_p05"] = boot_p05(col, rng, n_boot)
        checks["5_collateral_noninferior"] = out["collateral_p05"] >= -MARGIN
    else:
        checks["5_collateral_noninferior"] = cl - cw <= MAX_COL_NET_LOSS
    net = defaultdict(int)
    for r in recs:
        net[r.get("bucket", "?")] -= r["d"]
    out["bucket_net_losses"] = dict(net)
    checks["6_bucket_guard"] = max(net.values(), default=0) < BUCKET_NET
    fixed = sum(bool(r.get("fail0")) and not r.get("fail1") for r in recs)
    broke = sum(bool(r.get("fail1")) and not r.get("fail0") for r in recs)
    out["fail_flips_fixed_broke"] = [fixed, broke]
    checks["7_fail_flips"] = broke <= fixed
    ok = all(checks.values())
    checks["9_canary_hold"] = not canary
    out["checks"] = checks
    out["decision"] = "reject" if not ok else "hold" if canary else "accept"
    return out


# ---------- simulation (section 6.2) ----------

def _make(rng, n, b, w, stratum, taste=None, per_design=3, acc=0.92):
    recs = []
    for i in range(n):
        u = rng.random()
        p = acc if u < b else 1 - acc if u < b + w else (0.5 if taste is None else taste)
        recs.append({"design": f"{stratum}{i // per_design}", "stratum": stratum, "bucket": rng.randrange(5),
                     "votes": [1 if rng.random() < p else -1 for _ in range(4)]})
    return recs


def oc(n_t, b_t, w_t, n_c, b_c, w_c, taste=None, trials=300, seed=0):
    rng, ok = random.Random(seed), 0
    for _ in range(trials):
        recs = _make(rng, n_t, b_t, w_t, "target", taste) + _make(rng, n_c, b_c, w_c, "collateral", taste)
        ok += decide(recs, n_boot=400, seed=rng.randrange(2 ** 31))["decision"] == "accept"
    return ok / trials


def sim():
    bad = []
    for name, args, want in OC_ROWS:
        got = oc(*args)
        print(f"{name:40} P(accept) = {got:.2f}   design {want:.2f}")
        if abs(got - want) > OC_TOL:
            bad.append(name)
    assert not bad, f"operating characteristics off by > {OC_TOL}: {bad}"
    print("sim ok")


# ---------- ledger ----------

def _grader_version():
    try:
        return subprocess.run(["git", "-C", str(Path(__file__).parent), "describe", "--always", "--dirty", "--tags"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as ex:
        print(f"warning: no grader version ({ex})", file=sys.stderr)
        return "unknown"


def _append(entry, ledger):
    with open(ledger, "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}) + "\n")


def _registration(change, ledger):
    if not Path(ledger).is_file():
        return None
    regs = [e for e in map(json.loads, Path(ledger).read_text().splitlines())
            if e.get("event") == "register" and e.get("change") == change]
    return regs[-1] if regs else None


def register(change, commit, cluster=None, flag=None, direction="better", ledger=LEDGER):
    if _registration(change, ledger):
        raise SystemExit(f"{change} is already registered; pick a new change name")
    _append({"event": "register", "change": change, "commit": commit, "cluster": cluster, "flag": flag,
             "direction": direction, "grader": _grader_version()}, ledger)


def decide_change(change, records_path, canary=False, ledger=LEDGER):
    reg = _registration(change, ledger)
    if reg is None:
        raise SystemExit(f"{change} has no ledger registration: register before judging")
    recs = [json.loads(x) for x in Path(records_path).read_text().splitlines() if x.strip()]
    for i, r in enumerate(recs):
        if not ({"design", "votes"} <= r.keys() and isinstance(r["votes"], list)):
            raise SystemExit(f"{records_path}:{i + 1}: record needs 'design' and a 'votes' list")
    can = Path(records_path).with_suffix(".canaries.jsonl")   # written next to records by judge.py pairs
    if can.is_file():
        from eval.arena.judge import canary_summary   # lazy: judge pulls in PIL and the claude driver
        summ = canary_summary([json.loads(x) for x in can.read_text().splitlines() if x.strip()])
        if summ["void"]:
            raise SystemExit(f"{can}: run is VOID (canaries {summ}); re-judge before deciding")
    res = decide(recs, superiority=bool(reg.get("flag")), canary=canary)
    _append({"event": "decision", "change": change, "records": str(records_path), "grader": _grader_version(),
             **res}, ledger)
    return res


def selftest():
    import tempfile
    assert round(UP, 2) == 2.77 and round(DOWN, 2) == -1.56 and round(STEP_W, 3) == 0.262
    assert decided([1, 1, 1, 1]) == 1 and decided([1, 1, 0, 1]) == 0 and decided([-1, 1, 1, 1]) == 0
    assert abs(binom_tail(8, 9) - 10 / 512) < 1e-12
    rng = random.Random(0)
    win = [{"design": f"d{i // 3}", "stratum": "target", "bucket": "wide", "votes": [1] * 4} for i in range(30)]
    col = [{"design": f"c{i // 3}", "stratum": "collateral", "bucket": "tall", "votes": [1, -1, 1, -1]}
           for i in range(30)]
    assert sprt_blocked([{**r, "d": 1} for r in win], rng)[0] == "accept"
    assert decide(win + col)["decision"] == "accept"
    assert decide(win + col, canary=True)["decision"] == "hold"
    lose = [{**r, "votes": [-1] * 4} for r in win]
    assert decide(lose + col)["decision"] == "reject"
    # collateral count rule: 4 net losses among < 60 collateral pairs rejects
    harm = [{**r, "votes": [-1] * 4, "bucket": f"b{i}"} for i, r in enumerate(col[:4])]
    assert not decide(win + harm)["checks"]["5_collateral_noninferior"]
    # bucket guard: 4 net losses in one bucket rejects even when the collateral count rule passes
    one_bucket = [{**r, "bucket": "tall"} for r in harm]
    res = decide(win + one_bucket[:3] + [{**col[5], "votes": [1] * 4, "bucket": "x"}] + one_bucket[3:])
    assert not res["checks"]["6_bucket_guard"], res
    # FAIL flips: breaking more unanimous FAILs than fixed rejects
    assert decide(win + [{**col[0], "fail1": True}])["checks"]["7_fail_flips"] is False
    # leave-one-design-out: a single winning design carries nothing
    assert not decide(win[:3] + col)["checks"]["8_leave_one_design_out"]
    # non-inferiority mode: no target needed, all pairs collateral
    assert decide(col, superiority=False)["decision"] == "accept"
    assert decide(col)["decision"] == "reject"   # a targeted change with no target pairs never passes
    with tempfile.TemporaryDirectory() as d:
        led, rp = Path(d) / "ledger.jsonl", Path(d) / "records.jsonl"
        rp.write_text("\n".join(json.dumps(r) for r in win + col))
        try:
            decide_change("x", rp, ledger=led)
            raise AssertionError("decide without registration must refuse")
        except SystemExit:
            pass
        register("x", "abc123", cluster="strip-stack", flag="collisions", ledger=led)
        assert decide_change("x", rp, ledger=led)["decision"] == "accept"
        assert [json.loads(x)["event"] for x in led.read_text().splitlines()] == ["register", "decision"]
    print("selftest ok")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m eval.arena.accept")
    ap.add_argument("cmd", nargs="?", choices=("register", "decide"))
    ap.add_argument("change", nargs="?")
    ap.add_argument("records", nargs="?")
    ap.add_argument("--commit")
    ap.add_argument("--cluster")
    ap.add_argument("--flag", help="pre-registered L2 stratum flag; omit for a non-inferiority-only change")
    ap.add_argument("--direction", default="better")
    ap.add_argument("--canary", action="store_true", help="a canary moved toward hiding and is not yet cleared")
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.sim:
        return sim()
    if a.cmd == "register" and a.change and a.commit:
        return register(a.change, a.commit, a.cluster, a.flag, a.direction)
    if a.cmd == "decide" and a.change and a.records:
        res = decide_change(a.change, a.records, a.canary)
        print(json.dumps(res, indent=1))
        return 0 if res["decision"] == "accept" else 1
    ap.error("give register CHANGE --commit SHA, decide CHANGE RECORDS, --sim or --selftest")


if __name__ == "__main__":
    sys.exit(main())
