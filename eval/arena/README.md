# eval/arena

A grader for CHAMELEON engine changes that needs no human labels. It compares a champion
checkout with a challenger checkout. The design is in the scratchpad's `design/GRADER_DESIGN.md`.
Engines run as subprocesses and nothing here imports `engine/*`. Run every command from the repo
root with B's venv: `PY=~/projects/adobe-hackathon-claude/chameleon/.venv/bin/python`.

## Layers and modules

| Layer | Module | What it does |
|---|---|---|
| L0 determinism | `determinism.py` | byte-identical output across PYTHONHASHSEED values and under CPU load; id-rename and element-reversal relations; no budget/timeout messages |
| L1 hard gate | `strict.py` | B `hard.py` H1-H6 plus S1-S7 (Draft 2020-12, key set, order, unrotated and rotated box, aspect without exemption, collapse < 2 px, font_size >= 1); A `validate.py` as a second implementation |
| sets | `sets.py`, `sets/{D,V,L,H}.json` | frozen task sets; task files in `sets/tasks/<set>/` |
| L2 + diff | `suite.py` | runs both arms, then strict, B soft grading, render-hash diff -> `changed_pairs.json`, `summary.json` |
| L3/L4 judge | `judge.py` | 4 `claude -p` calls per changed pair (R/D condition x 2 orders), identity and defect canaries, optional P-OFF |
| decision | `accept.py` | register, then decide: design-blocked SPRT, bootstrap, collateral non-inferiority, bucket guard, FAIL flips; appends to `ledger.jsonl` |

## Commands

The frozen task sets are not committed; `python -m eval.arena.sets` rebuilds them under `eval/arena/sets/`.

```
$PY -m eval.arena.<module> --selftest          # every module has one
$PY -m eval.arena.accept register CHANGE --commit SHA [--cluster C --flag crit:sev]
$PY -m eval.arena.suite --champion ROOT --challenger ROOT --set eval/arena/sets/tasks/D --out OUT [--flag crit:sev]
$PY -m eval.arena.judge pairs --a OUT/champion --b OUT/challenger --tasks SET --changed OUT/changed_pairs.json --out REC.jsonl [--poff]
$PY -m eval.arena.accept decide CHANGE REC.jsonl
```

`decide` refuses a change with no registration, and refuses a run whose `REC.canaries.jsonl`
is VOID (defect accuracy < 0.9 or identity decided > 0.10).

## Current numbers (2026-10-30)

- Self-tests: all six pass. Engine `pytest tests`: 89 passed (18 min).
- L0: 0 diffs on 3 official + 20 dev pairs, across two PYTHONHASHSEED values and under load.
- Suite, champion_v4 vs this checkout, 3 official samples x 25 targets: 0 of 75 renders changed,
  0 engine errors, 0 B hard failures. strict fails 3 pairs in both arms (the same 3, so not a
  regression): `sample_l3_advanced` at 1080x1350 (rotated box off-canvas) and at 320x100 and
  320x50 (an element collapsed to 1.4 px).
- strict on h2h (43 tasks): B4 0 failing, A 3, B40 7, BCE 9.
- Judge, h2h B4 (b) vs A (a), first 12 tasks: 6/12 decided (B4 2, A 4). Identity canaries
  0/3 decided. Defect canaries 2/3, so the run is VOID. The miss was `stretch_1.5` on a drawn
  product shape at 300x250, where the stretch is barely visible. The canary pool needs freezing
  in calibration (build step 7).
- Canary pool frozen as `canary-v2` (judge.py `CANARY_DEFECTS`): stretch_asset_1.6 (a real product/logo asset,
  fit forced to fill, since the renderer letterboxes `contain`, which is why the v1 stretch was invisible),
  text_on_text, tiny_text_4px, half_off_canvas, headline_on_busy_image. A defect render that changes < 1% of
  pixels at view size is skipped. Calibration on 12 identity + 12 defect canaries from the D150 champion
  renders: defect accuracy 12/12, identity decided 0/12 (`judge pairs --a X --b X --canaries 12`).
- `mine.py`: stratified D sample (level x shape, strips x2), P-OFF defect tags joined to analyze.py roles,
  clustered by (class, role, shape), contact sheet.

## Not built yet

calibrate.py, milestone.py, the Gemini wrapper, the S set, F2 stress tasks, docker
`--cpus=1` determinism, canary distributions (L2 job 3), and the 100-target / 120-collateral sampling.
