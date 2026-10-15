# Plan v3 (2026-10-14): where the engine stands and what moves it

Read `HANDOFF.md` for the tooling; this file is the decision record for the `v3-plan-oct14` branch.

## 1. The problem, reduced to what scores

- Input: a flat element list (text / image / shape) plus a target size. Output: the same ids with new
  `x, y, width, height` and, for text, `font_size`. Content, font family, colours and assets are fixed.
- Scored twice on hidden designs (three levels, targets from a fixed catalogue of real formats):
  1. JSON structure: ids unchanged, required fields, nothing off-canvas, non-cover images within ~5% of
     their aspect.
  2. The official render, graded by an AI evaluator: no unwanted collisions, readable, on brand,
     intentional, holds up everywhere. "Speed and reliability" is a row, not a gate.
- Regenerating pixels disqualifies. A local model is allowed, offline, in an amd64 container on an Apple
  M5 Pro; size cap and approved list are still unpublished.
- Renderer facts that bind harder than the brief: greedy word-wrap at box width with skia glyph widths,
  nothing clipped (overflow is invisible to the JSON check, visible to the judge); `font_size` truncated to
  an integer; `contain` images letterboxed, so a wrong box aspect only shows in the JSON check.

## 2. Hypothesis vs what exists

Hypothesis formed from the brief and starter alone: infer roles and rigid units from geometry and z-order,
switch topology by target aspect class, solve each layout exactly with the renderer's own text measurement,
pick with a rubric-mirroring energy, keep models out of the geometry. That is what `engine/` is after 40
iterations, and the sister repo converged on the same thing. The architecture is not the open question.

| Measure (engine iter40) | Value |
|---|---|
| Claude vision bench, 120 pairs | 0.55 (25 PASS / 82 REVIEW / 13 FAIL) |
| By shape | wide 0.67, square 0.60, tall 0.50, ultrawide strips 0.41 |
| Rule grader, 625 pairs | 0 hard fails, 271 major flags |
| Official samples | L1 PASS, L2 REVIEW, L3 at 728x90 FAIL |
| macOS vs Docker | 12% of pairs pick a different layout (Linux text metrics 1–2% wider) |

Mid and large formats are good; their REVIEWs are touch-ups (orphan words in wrapped subheads, badge rows
turned into columns on tall canvases, near-touches). Losses concentrate on 320x50 / 320x100 / 728x90 with
long copy, and on the missing-hero design, which no legal move fixes.

**Diagnosis (agreed with the logs): generation, not selection.** A Claude rerank of the top 3 gave +0.05/10;
the dev-time Claude edit loop fixed 5 of 6 human-failed outputs and its results beat the engine's own
energy. The templates cannot produce those layouts.

## 3. Models: what they are, what was measured, where they fit

**Jev** (TypeSafe) is a typed-decision model: state + questions (`choice` / `score` / `noul`) in, per-option
probabilities out from a prefill-only pass, so it cannot go off-schema. Hosted-only, closed weights, so it
cannot run under `--network none`. Independent tests put it level with mid-price LLMs; gains come from
decomposing a judgement into several small typed questions; weak at counting, arithmetic and images.
Local equivalents: decider-4b, JevK5, SemIf (option-logit readout on a stock 4B), Clef-flash 9B
(`/v1/systemone` in llama.cpp). The sister repo tried the whole family on geometry and judging: text-only
decisions on layout state were at chance; the one positive config was a hosted Claude judge panel with
abstention (+0.04).

**Measured in the amd64 container on the M2 (Rosetta, AVX2 exposed, 8 cores, 8 GB):**

| Model | Prompt | Generation | One design |
|---|---|---|---|
| Qwen3-4B Q4 text | 9.6 tok/s | 4.6 tok/s | ~75 s for a role-labelling call |
| Qwen3-VL-4B Q4 | 5.8 tok/s | 3.9 tok/s | 2 min 17 s for one 1600x400 render |

The M5 Pro is perhaps 2–3x faster, so a vision round still costs about a minute. The 4B critique of the
L2 banner invented an overlap that is not there and called the logo too small (matches the earlier 0.5
precision finding). Rules already label roles at 90.8%.

Integration map:
- **Container (stage one):** deterministic by default. The only model step worth a *gated* experiment is a
  bounded vision critic proposing typed edits from the existing menu, enforced by the engine and vetoed by
  the energy. Ship only if it beats the deterministic pick on the Claude bench and fits the size cap.
- **Dev time:** Claude as judge is ground truth; the Claude edit loop is the generator of new templates.
- **Finale add-on:** the agentic loop over the Claude API with structured outputs, steering, locks, options.

## 4. Work plan, in order

1. **Score-safe pipeline.** Evaluate inside the container (the official path), keep the official samples
   and the 13 panel FAILs as a Docker regression, ask the organisers for the model list, size cap, M5 Pro
   RAM and the deadline.
2. **Mine the Claude loop for templates.** Run `eval/vision_loop` on the 13 FAILs and the worst REVIEWs,
   score each result with `energy()`, and turn every energy-preferred loop result into a template variant.
   Known gaps: badge rows that stay rows on tall canvases; product-over-text for right-aligned designs; a
   portrait band for full-height side panels; balanced wrapping for subheads (no orphan word); a two-tier
   strip for content-heavy 728x90.
3. **Refit the energy on Claude labels** by pairwise ranking over judged candidates, held out by design.
4. **Gated local-VLM critic experiment** on the 33 worst pairs; prior about 1 in 3 that it helps.
5. **Finale add-on** on the vision-loop backend with Sonnet 5.5 and structured outputs.

## 5. Trimmed on this branch

Dead or retired code removed (history keeps it): H4 role labeller, insight and ollama-refine steps and
their flags, H5/H7 experiment drivers, the Crello human-resize tools (`gt_*`, `iterate`), the local-VLM
grader tier and its tests, the human-label pages, generated viewers and old judged runs.

## 6. Log

Entries are appended below as iterations land, newest last.
