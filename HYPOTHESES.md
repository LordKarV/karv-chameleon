# Chameleon — Hypotheses, Strategies & Architecture

## 1. The problem, restated precisely

Input: `{source: {canvas, elements[]}, target_canvas}`. Elements are `text | image | shape` with geometry,
z_index, optional `group_id`, rotation, opacity. **No semantic labels** (ids may be opaque).
Output: same ids, new `x, y, width, height` (+ `font_size` for text). Content and font family fixed.
Pixels are never regenerated (disqualifying).

Scored on two artifacts:
1. **Structural JSON** — same ids, required fields, nothing off-canvas, non-cover images keep aspect (±5%),
   content/font unchanged.
2. **Rendered PNG → AI judge** — no unwanted collisions (text never on a busy image), readable (size,
   contrast, nothing cut off), on brand (colors, fonts, logo intact and in proportion), intentional
   (alignment, spacing, hierarchy), holds up across many designs and shapes.

Runtime: offline Docker (`--network none`), `linux/amd64`, Apple M5 Pro reference machine (amd64 is
**emulated**, no GPU), permitted small open models only. Latency not disqualifying; "speed and
reliability" is a rubric row.

### Renderer facts the engine must exploit (from `renderer/` and `cr_renderer`)
- Text is drawn **top-aligned** in its box; line pitch = `int(font_size) * line_height`; greedy word wrap
  at box width using skia glyph widths; a word wider than the box overflows; nothing is clipped.
  → the engine measures text *exactly* by importing `renderer.converter.wrap`.
- `font_size` is truncated to int at draw time.
- `fit: contain` letterboxes, `cover` center-crops, `fill` stretches.
- Brand assets carry large transparent padding (bottle visible ≈ 43%×69% of its box, logo ≈ 40%×72%),
  so true collisions and visual sizes must use the alpha bbox as fitted into the box.
- Missing assets render as a grey "missing asset" placeholder — keep it small and out of the way.
- Rotation is about the element center; the self-check tests only the unrotated box.

## 2. What research says (sources in §8)

- Production tools (Canva Magic Resize, Express, Figma) publish no algorithm; Express's own resize is
  crop-based (the brief's failure example).
- No open, small model does *conditional retargeting* off the shelf. Learned layout models (LayoutDM,
  FlexDM, LayoutNUWA, PosterLlama) are fixed-canvas and/or large. LayoutPrompter / LayoutCoT
  (LLM + exemplars + rank-by-metric) is the most stealable LLM pipeline.
- Best classical basis: O'Donovan et al. 2014 energy layout (alignment, overlap, white space, balance,
  importance) + ORC layouts (OR-constraints switch row/stack per aspect) + ReLayout relation graph
  (preserve left-of/above/aligned/contains) + PosterLayout metrics (overlay, alignment, underlay
  effectiveness, occlusion, readability).
- Small LLMs (≤4B) are poor at coordinate arithmetic, fine at labelling roles / picking templates. A 4B Q4
  model costs ~30–60 s/design natively, 2–5× more under Rosetta, 10×+ under QEMU → any model must be
  optional, time-boxed, with deterministic fallback.
- GPT/Gemini-style judges reward gross properties (hierarchy, no collisions, balanced whitespace) and miss
  fine detail.

## 3. Lessons from the sister repo (`../adobe-hackathon`) — inspiration only, not the architecture

That project built a ~6k-line pipeline (LLM semantic pass, kiwisolver, 20+ "geo" generators, learned
judges, gates). Its own verdict: **no LLM/decision-model geometry beat the deterministic engine**; every
extra generator lost on paired A/Bs. Held-out grader ≈ 3.9/5. Concrete observations worth keeping:
- Be **bold**: the hero product and logo end up timid when they keep source-relative size; a designer
  sizes the hero to ~30–45% of the short side when room allows.
- Headlines wrapping into tall narrow stacks look bad → widen the measure before shrinking; avoid orphans.
- Pills/buttons must hug their label; badge rows read best as one row with equal gaps.
- Decorative circles should scale with the short side and never land over text.
- Avoid top-heavy compositions with a dead lower half: distribute or center the content block.
- Tiny canvases: give headline/CTA/logo legible sizes first; 3 px "slivers" of secondary items are
  read by judges as broken — worse than modestly small text.
- Degenerate inputs (zero-size elements, out-of-canvas sources) must never crash; fallback must itself
  satisfy the structural contract.
→ Our bet: a **much smaller** deterministic core, with a stronger *composition* model (blocks + flex
  distribution + bold hero sizing) and selection by a rubric-mirroring score. Models are tested as
  plug-ins and shipped only if they win.

## 4. Hypotheses

| # | Hypothesis | Mechanism | Expected | Risk |
|---|---|---|---|---|
| H0 | Uniform stretch (given baseline) | scale x, y independently | worst | — |
| H1 | **Scale-to-fit + full-bleed background** | uniform `s=min(sx,sy)`, centered; background covers canvas | strong on nearby ratios (L1); fails extreme ratios | low |
| H2 | **Roles + units + aspect-class templates** (deterministic) | infer roles (background, decor, logo, hero, headline, sub, body, legal, button, badge, ribbon, underlay); groups/underlays → rigid units; template by aspect (stack / split / row / column) with flex distribution and exact text fitting | strong on L2/L3; the core bet | template brittleness |
| H3 | **Generate-and-score** over H1+H2 variants | many candidates (templates × alignment × split ratio × hero size × text measure), internal energy mirroring the rubric, keep best | most robust ("holds up everywhere") | scorer mis-specification |
| H4 | **Local LLM role labeller** (Qwen3-1.7B/4B, llama.cpp, JSON schema) | LLM labels roles/importance; H3 does geometry | helps only where role heuristics fail | emulated speed; approved-list uncertainty; sister repo saw no gain |
| H5 | **Local VLM rerank** (Qwen3-VL-4B) of top-k renders | VLM picks among best candidates | marginal | very slow under emulation |
| H6 | Learned layout model (LayoutDM/FlexDM on Crello) | train conditional model | research-grade | GPU/data/time; out of scope |

### Design convictions
1. **Geometry is deterministic and exact** — measure text with the renderer's own skia code.
2. **Units, not elements** — a `group_id` set, or text sitting on a shape (button/underlay), moves and
   scales rigidly; a pill may re-hug its label.
3. **Decor is anchored, not laid out** — low-opacity / bleeding shapes re-anchor to their nearest corner,
   scale with the short side, clamp in-canvas, stay behind content.
4. **One type scale** — all text scales by a shared factor with per-role readability floors; on overflow,
   lower-priority text shrinks first.
5. **Topology switches with aspect** — stack (tall), split (wide), single row (≥3:1 banners), single
   column (≤1:3 skyscrapers).
6. **Rules generate, the score decides** — no single heuristic has to be right for every design.

## 5. Architecture (H2 + H3; H4/H5 optional plug-ins)

```
source, target
   │
analyze ── background · decor · units (groups, underlays) · roles · importance · reading order
   │            ▲ optional H4 LLM role labeller (timeout → heuristic)
candidates ── H1 scale-fit │ stack │ split-L/R │ row (banner) │ column (skyscraper) — variants per template
   │            each: block layout + flex distribution + exact text fitting
energy ── overlap (true extents) · readability floors · hierarchy · utilization/balance · alignment
   │       · relation preservation · in-bounds · aspect
best ── (optional H5 VLM rerank top-k) ── finalize (clamp, schema-safe, contract asserts) ──► output
```

## 6. Evaluation plan

- **Corpus**: 3 official samples + ~24 hand-built designs (L1–L3, half with opaque ids) × ~25 real
  platform formats.
- **Deterministic metrics** (`eval/metrics.py`): structural hard checks, unintended overlap with true text
  extents and alpha bboxes, overflow, min font px, WCAG contrast on the render, busy background under
  text, utilization, alignment, hierarchy rank correlation, group integrity → `det_score` 0–100.
- **AI judge** (`eval/judge.py`): Gemini (default) and Claude Sonnet 5.5 (cross-check), local Qwen3-VL-4B
  as a cheap option; source + output renders, rubric scores 1–10, cached.
- **Protocol**: every hypothesis runs on the same sampled design×target grid; leaderboard; contact sheets
  reviewed by eye; never tune to a single design.

## 7. Build order
1. Eval harness + corpus (parallel agents).
2. H1 → numbers.  3. Core analyze/measure/finalize.  4. H2 templates → H3 search.
5. H4/H5 plug-ins measured against H3; ship only if they win and fit the runtime.
6. Docker `--network none` run; README approach section.

## 8. Sources
- O'Donovan, Agarwala, Hertzmann — Learning Layouts for Single-Page Graphic Designs (TVCG 2014)
- Jiang et al. — ORC Layout / ORCSolver / ReverseORC (CHI'19–'21)
- Hoffswell et al. — Techniques for Flexible Responsive Visualization Design (CHI 2020, Adobe)
- Warner et al. — VLT: Interactively Optimizing Layout Transfer for Vector Graphics (2023)
- Repairing Graphic Design Layouts After Content, Asset, and Canvas Updates (ICML 2026 WS)
- ReLayout (arXiv 2602.01046); Graphic-Design-Bench (arXiv 2604.04192)
- LayoutPrompter (NeurIPS'23); LayoutCoT (arXiv 2504.10829); LayoutDM / FlexDM (CVPR'23)
- PosterLayout metrics (CVPR'23); Haraguchi et al. — Can GPTs Evaluate Graphic Design (SIGGRAPH Asia'24)
- Bylinskii et al. — Learning Visual Importance for Graphic Designs (2017)
- Tools: kiwisolver, OR-Tools CP-SAT, llama.cpp JSON-schema decoding, OpenCV saliency, u2netp

## 9. Outcomes (2026-10-08)

| # | Verdict | Evidence |
|---|---|---|
| H0 | Baseline | Fails the aspect checks: only 13% of outputs are structurally OK; det 43.8 |
| H1 | Strong floor | Gemini judge 4.65, Claude judge 3.95; letterboxes wide/tall targets (ultrawide det 58) |
| H2+H3 | **Shipped** | Gemini 5.73 / Claude 5.33 against a self-score ceiling of 7; det 90.4; 100% contract |
| H4 | Not shipped | Qwen3-4B role accuracy is 89.6% vs heuristics 85.5% (90.8% after porting its wins into rules); +2.5 GB; minutes per design under emulation |
| H5 | Not shipped | A Claude rerank of the top 3 gives +0.05 judge points (5.65 → 5.70); the top-3 oracle is +0.225 |
| H6 | Not built | No open conditional-retargeting model fits CPU/offline |

The biggest single gains came from these changes, in order:
- **Exact renderer-matched measurement.** Measuring per integer size, as Linux skia does, and counting letter spacing in the painted width.
- **XY-cut reading order.** Wide sources were being read wrongly.
- **Structure inference.** Clusters, stickers, corner ribbons, sibling families and grid reflow.
- **Energy fixes.** The energy now ignores overlap the source already had, treats panels as backdrops, and penalizes bare canvas.
- **Priority-ordered readability floors,** with image and logo floors.
