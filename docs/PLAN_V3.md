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

### 2026-10-14, evening: Docker bench, loop mining, local search

- **Docker evaluation path** (`eval/in_container.py`, `vision_bench --docker`): resize + render inside the amd64
  image with the working tree's `engine/` mounted; `CHAMELEON_*` flags pass through. Control run `v3base`
  (search off) scores **0.55**, the same as the macOS `iter40` panel (10 pairs better, 10 worse: pick flips plus
  judge noise). All decisions from here use `--docker` tags.
- **Role fix:** a 55-char line at 82% height with the CTA below it was classed `legal` (brief_poster); legal now
  needs bottom > 87% (or > 80% with > 60 chars). Only that design's roles changed.
- **Loop mining** (`eval/loop_mine.py`, run `m1`, 17 pairs): 7 reached PASS; on 10 the loop's final layout has a
  *lower* engine energy than the engine's pick, i.e. generation gaps (longcopy_story 300x250 49.6 -> 22.9,
  split_panels 1000x1500 1.26 -> 0.17, tall_pin 728x90 49.1 -> 37.8). On the 320-wide strips the loop's layouts
  score worse on `illegible`/`read` (2-5 px copy) and the energy is right to resist them.
- **Local search** (`engine/search.py`, step 5c, `CHAMELEON_SEARCH=0` disables): energy-guided best-improvement
  descent over unit shifts / scales / rewraps, family-joint moves for look-alike cards, block recentre / spread /
  equalize, panel children movable. It reproduces the loop's energy wins (longcopy 49.6 -> 22.8 in 1.3 s).
  - `v3a` (first version, 91/120 renders changed): **0.50**, 3 better / 15 worse vs `v3base`. It grew text edge
    to edge, pushed a subtext to the bottom edge, shifted a header band off the top edge, nudged a left-aligned
    column sideways.
  - Guards added, in order: never under readability / logo floors; no new crowding (< 0.8% short side); no new
    edge contact; growth capped at 1.3x proportional; edge-anchored panels fixed; run only while the critic names
    defects and stop when clean; no stranding (nearest neighbour > max(8%, 1.5x source)); source alignments
    locked (left / centre / right, row top / centre / bottom) with scaling anchored on the locked edge.
  - `v3b` (floors, crowding, edge, cap, critic gate): **0.546**, 3 better / 4 worse on 44 changed renders:
    neutral. `v3c` (stranding + alignment locks) pending.
- **Lesson so far:** the energy is the ceiling for any search. Where it agrees with Claude (hero too small, text
  too small, dead band) the search helps; where it is blind (alignment, stranding, margins) the search finds
  degenerate layouts. Each blind spot found is now a guard; if `v3c` is still neutral the search ships off by
  default and the effort moves to deterministic fixes for the top complaint classes on `v3base`: tiny text (47
  majors, mostly < 350 px canvases), overlap / crowding (32), edge / margin (25), stranded / dead space (13), logo
  size (11).
- `v3c` (stranding + alignment locks): **0.55**, 3 better / 3 worse on 43 changed renders; majors 87 vs 80.
  `v3d` (band fixes + search): **0.546**, 4 better / 5 worse (all 5 PASS->REVIEW with no major issue: boundary
  noise); majors 83. **Decision: the search ships off by default** (`CHAMELEON_SEARCH=1` enables). It never
  beat the control on majors (84 / 87 / 83 vs 80); every guard closed one blind spot and the next one appeared.
- **Deterministic fixes measured next (`v3e`, search off):** thin bands (`analyze.band_axis`: edge-spanning,
  <= 35% of the source on the thin axis) keep their thickness-to-content ratio, keep centred children centred
  when they grow, and push intruders out with an order-preserving cascade (`layout._band_keep_out`); bands
  collide on their whole shape in the energy while they stay thin in the target; a text-only CTA stays in the
  banner text stack instead of a far-right slot; the margin is floored at 3% of the long side (capped at 10%
  of the short side; strips unchanged); `LOGO_MIN_PX` 40 -> 52. `tests/test_contract.py::test_same_aspect_is_identity`
  guards the identity path (a first thickness re-fit compared visual rects with element boxes and pushed a
  1080x1080 header from 140 to 200 px).
- `v3e` (bands + margin floor + logo 52 px, search off): 0.554, majors 86; "detached/dead space" 13 -> 22, the
  52 px logo floor pushed sample_l1 728x90 into a vertical stack. Both reverted. `v3f` (bands + pills + text CTA):
  0.542, majors 84; the text-CTA rule removed four "stranded CTA" majors but broke the L1 strips (a plain-text
  CTA on a 728x90 needs its slot), and un-widened pills lost breathing room on 1920x1080 / 1080x1920.
- **`v3g` = bands + text-CTA-in-stack on non-strips + secondary floors +25% on canvases >= 400 px: majors 76
  (control 80); on the 25 changed renders 11 -> 7 majors, 8 pairs lost a major, 3 gained one.** Verdict score
  0.5375 vs 0.55 is PASS/REVIEW boundary noise (the 5 "worse" pairs have no major issue). Shipped. Remaining
  gains on those pairs: type still small on roomy portrait targets (tall_pin 1275x1650, editorial 2480x3508).
- **Measurement note:** the verdict score moves +-0.01 on identical renders (cache hits aside); count majors and
  read the changed pairs' issues instead. One change per bench from here.
- **State at 21:15:** HEAD = `v3g` engine + an all-or-nothing guard on the band keep-out (no pick changes on the
  panel designs). `scripts/docker_check.sh` builds the image, runs the official tasks offline in it and grades
  the container's outputs; use it before any submission. Open leads, in order: type too small on roomy
  portrait targets (stack candidates overlap the hero with the CTA on editorial 2480x3508, so proportional
  s0.7 wins); the L3 sample's subhead at 2 px on 728x90 (inherent: 17 elements in 90 px); strips at 320 px.
- `scripts/docker_check.sh` on HEAD: all three official tasks run offline in the image (0.2 / 3.7 / 12 s). The
  grader then re-renders on macOS, so its H7 (PNG equals the official render) fails by 0.5-13% of pixels and
  the L2 "subhead overlaps headline" flag is the same metrics mismatch (the container PNG shows a clear gap);
  read H1-H6 and the container PNGs. The L3 728x90 subhead at 2 px is the known ceiling.

### 2026-10-14, late evening: one change per bench

- **`v3h` (stack overhang, shipped): majors 72 (v3g 76).** The stack solver fitted visual heights only; a
  contain-fitted picture's element box overhangs its visual rect, and the canvas clamp slid the bottle up over
  the CTA on l2_editorial_wide 2480x3508 (the "stack overlaps hero with CTA" lead). `layout.stack` now counts
  the first / last row's overhang beyond the margin as stack height. 8 changed renders, 4 pairs lost a major
  (collage 820x312, fb_link 2480x3508, linkedin 320x50 x2), none gained one.
  Guard: `test_stack_letterboxed_hero_stays_below_cta`.
- Why the editorial stacks still lose after v3h: the 900 px 10%-alpha halo ring is pinned to the bottle and
  scales with it, so in every stack it covers the copy; the energy scores that as (a) a backdrop change for
  every text unit (`_backdrop_changes` tested `opacity` but not the fill alpha) and (b) a hard pair overlap at
  the picture weight. (a) is a plain bug -> `v3i`; (b) is a weighting choice -> `v3j`, measured alone.
- **`v3i` (backdrop uses the fill alpha, shipped): majors 72, 2 renders changed, none moved.** A bug fix, kept.
- **`v3j` (a pinned halo collides in proportion to its alpha, shipped): majors 70.** 6 renders changed:
  editorial 300x250 -2, 300x600 -1 (bigger proportional layouts were being suppressed by the ring's "overlap"),
  collage 1200x630 +1 (split flipped to the mirror side; headline now grazes the product panel).
- **`v3k` (a sticker keeps its source margin to the canvas edges, shipped): majors 69, score 0.546.** The
  split_panels NEW badge straddled a panel edge in the source and was clamped flush to the canvas when the
  panel grew to full width. 15 renders changed (every sticker moved a little): trio 970x250 -1, catalog
  300x250 -1, collage 1200x630 -1; collage 820x312 +1 (the vertical label is back at the top edge), light_story
  1584x396 +1 (peach circle now under the headline). Guard: `test_sticker_keeps_margin_to_canvas_edge`.
  Also: `eval/in_container.py` now mounts a per-run snapshot of `engine/`, so the tree can change while a
  bench renders.
- Looked at and left: l3_long_word_square 3440x1440 ("narrow centre column") - the only wide alternative is a
  split that blows the 94 px can up to the full height; l3_longcopy_story 1200x627 (CTA small under the pills)
  - seven rows in 627 px, the long-copy ceiling; l3_missing_hero (7 majors: "product missing") - deliberate,
  see HANDOFF §2.
- Next, one per bench: `v3l` corner radius follows the shape's scale (pills grew into rounded rectangles on
  2480x3508; the brief fixes content, fonts, colours and assets, not geometry - the grader and contract test
  now treat `corner_radius` like `letter_spacing`); `v3m` a side-by-side row that still overflows its column
  at its floors goes one per line (three pills clamped to the canvas edges on sample_l3 300x600).
- **`v3l` (corner radius follows the shape's scale, shipped): majors 65 (v3k 69), score 0.5375.** 35 renders
  changed (every pill). Lost a major: underlay 2480x3508 (the restyled-pill complaint itself), tall_pin
  1275x1650 and 728x90, long_word 3440x1440, editorial 2480x3508, catalog 320x50; gained one: badges5
  1080x1920 (pill column "crowded"), tall_pin 2560x1440 (tagline at the header's bottom edge). The verdict
  score dip is PASS->REVIEW flips without majors. Assumption to confirm with the user: `corner_radius` is
  geometry (like `letter_spacing`), not a styling property the brief fixes; `eval/grader/hard.py` and the
  contract test allow it scaled by min(width, height) ratio.
- Incident: the per-run engine snapshot copied `engine/models` (1 GB of GGUF files) into three run folders,
  filled the disk, and took Docker Desktop's VM down with it; the first v3m attempt died in the copy. Fixed
  (`shutil.ignore_patterns("__pycache__", "models")`), snapshots removed, Docker recovered by killing the
  stale `com.docker.backend` and relaunching. Full suite on v3l: 76 passed.
- **`v3m` (a side-by-side row that overflows its column at its floors goes one per line): no render changed,
  65 majors.** Kept as a guard. The sample_l3 300x600 pills came from the *proportional* layout, not the
  stack: a cohesive badge row wider than the canvas was torn apart by the separation pass -> `v3o`.
- **`v3n` (stack gap capped by the smaller neighbour, shipped): majors 60 (v3m 65).** 59 renders changed.
  Lost a major: brief_poster 970x250 (the "detached subtext" lead) and 1500x500, sample_l2_core 1920x1080,
  longcopy 1584x396, right_aligned 1200x627, badges5 1080x1920, collage 1280x720, poster_a4 3440x1440 and
  728x90, fb_link 1275x1650, missing_hero 300x600 and 600x200. Gained one: light_story 1280x720 (logo pill on
  the circle), underlay 1280x720 (garnish on the panel edge), tall_pin 1275x1650 and 2560x1440 (tagline flush
  with the header band's bottom: the band keeps its thickness while its tightened children no longer sit
  centred in it), fb_link 2480x3508, brief_poster 600x200 (headline grazes the CTA), linkedin 320x50.
- `eval/in_container.py` now writes `<pair>.info.json` (chosen candidate + top ranking) next to each render,
  so a Docker pick can be read on the host without re-running.
- **`v3o` (proportional wraps a cohesive row wider than the canvas, shipped): majors 59.** Only the target
  pair changed (sample_l3 300x600, pills centred in two lines).
- **`v3p` (a stack gap across an unpacked panel's edge keeps its scaled source size, shipped): majors 59.**
  tall_pin 1275x1650 -1 (the tagline now has padding below it) and 2560x1440 -1 (card text), badges5
  1080x1920 +1 (footnote close to the NEW pill), linkedin 320x50 +1 (an all-illegible strip whose count
  wanders 2-4). Kept: principled, and the targeted complaint went away on one of its two pairs.
- Next (`v3q`): the snap pass (`relations.snap`) shifted the brief poster's headline to within 5 px of the
  CTA on 600x200 for an exact alignment; a shift that brings a unit inside the minimum gap of a neighbour is
  now reverted.
- **`v3q` (guarded snap shifts): majors 61 (v3p 59), reverted.** The brief poster 600x200 did not even
  change under Docker metrics; the changes landed on strips (right_aligned 320x50 gained a real headline/CTA
  collision, poster_a4 320x50 +1) and badges5 1080x1920 stayed at one major.
- Found while reading tall_pin 2560x1440: `_pad_room` capped an unpacked band's padding against *every*
  outside unit, so a regridded card row put the third card (1000 px away in the source) beside the first and
  capped the header's bottom padding at 11 px. `v3r`: the nearest source neighbour alone sets the share.
- **`v3r` (`_pad_room` caps by the nearest source neighbour only, shipped): majors 59.** Only the two
  tall_pin pairs changed; the 2560x1440 "tagline flush with the header" complaint is gone (its remaining major
  is the card text size). Guard: `test_header_band_keeps_bottom_padding_beside_regridded_cards`.
- Read in the container (`resize_with_info` + `info["propose"]`): the light story 1280x720 logo pill at y=12
  is the propose pass's "spread content 8%" edit, which moves units past the layout margin. `v3s`: the spread
  factor is capped so no unit crosses the margin.
- **`v3s` (propose's spread stops at the layout margin): light_story 1280x720 1 -> 2 majors (the pill now
  collides with the opaque peach circle at the margin, and the headline straddles its bottom), reverted.**
  The pair's real defect is the circle pinned to the hero as a halo although it is opaque.
- Read from the v3r Docker outputs: split_panels 1500x500's "Find a store" link is right-aligned to its text
  column because its source right edge matched the text block's edge, so a widened column separates it from
  its button (`v3u`: units adjacent in the source stay packed); fb_link 2480x3508's hero takes the copy's
  left alignment in its own stack row (`v3t`: a lone picture row centres).
- **`v3t` (every lone picture row centres): majors 59, score 0.533.** Targets fixed (split_panels 1000x1500 and
  2480x3508, fb_link 2480x3508 each -1) but two picks flipped: editorial 300x250 proportional -> stack (FAIL,
  +2: a few-pixel logo and a tiny can at the bottom) and fb_link 1275x1650 source -> bottom (+1: the CTA
  stranded under a centred mid-column hero). `v3u`: only a picture at either end of a roomy column
  (short side >= 400) centres.
- **`v3u` (a lone picture at either end of a roomy column centres, shipped): majors 57 (v3r 59).** The three
  targets lost their major (split_panels 1000x1500 and 2480x3508, fb_link 2480x3508); fb_link 1275x1650
  gained one ("can off-centre right"). The two v3t pick flips did not recur.
- `v3v`: units adjacent in the source (a button and its "Find a store" link) stay packed in a widened column,
  the first keeping its alignment. Guard: `test_adjacent_button_and_link_stay_packed`.
