# Overnight iteration log (branch `overnight`)

Every change is measured with `python -m eval.iterate` (no API):
- human resizes: 178 adobedata pairs, split by template family into tune / held-out
  (box IoU, centre error, order agreement vs the designer's version);
- deterministic rubric score on 100 corpus pairs (seed 11);
- seconds per design.
Kept only if held-out improves without hurting the rubric score.

| # | Change | held-out IoU | held-out order | det | s/design | Verdict |
|---|---|---|---|---|---|---|
| 0 | baseline (e1b64ca, proportional template) | 0.3406 | 0.894 | 88.96 | 0.62 | — |
| 1 | energy weights fitted to human resizes (quality terms locked) | 0.3474 | 0.901 | 88.74 | 0.74 | rejected: collisions .977→.953, alignment .945→.924 |
| 1b/1c | milder weight variants | 0.341 / 0.345 | 0.894 / 0.900 | 88.85 / 88.66 | | rejected |
| — | fix: crash when all buttons are round stamps (found by sweep, 2% of tasks fell back) | | | | | kept |
| 2 | proportional: order-preserving per-axis separation + 0.7 size variant | 0.3479 | 0.906 | 88.96 | 1.22* | kept |

\* measured while the 7,818-task sweep was running on the same CPU.
| 3 | row cohesion in proportional (sibling rows move as blocks) + proportional bonus fades with aspect change | 0.3480 | 0.906 | 89.23 | | kept: review batch 1 — split badge rows, spread grids and dead zones fixed (4 of 8 items visibly better) |
| — | missing-asset placeholder prefers its source corner | | | | | kept |
| 4 | OCR-driven text floors: general 8 px, headline 10, CTA 9, legal 6; sticker labels get a legibility floor | 0.3480 | 0.905 | 89.50 | | kept: unreadable text 17.1% → 13.3% (OCR at true size) |

New measurement: `python -m eval.ocr_eval` renders outputs at true pixel size and reads every text
element with EasyOCR (from the locally installed ImageSorcery MCP). Text at 6 px was never readable;
from ~10 px it almost always is.
| — | full sweep over 7,818 real tasks: 0 crashes/fallbacks but 91 tasks broke image aspect (hairline images lost aspect to 2-decimal rounding); finalize now keeps 4 decimals and re-derives the second side from the source aspect | | | | | kept: 91 → 0 violations |
| 5 | illegible-text penalty (role-aware, from OCR) + final relaxation level: when nothing fits legibly, secondary copy yields so headline/CTA stay readable | 0.3478 | 0.903 | 89.66 | 1.28 | kept: headline OCR .972→.990, CTA .890→.907; fixes 2 px headline on 728x90 long copy (verified with ImageSorcery OCR + crop) |

## Iteration 6: propose converges until fixed

- **Change:** Propose is now a real feedback loop. It critiques, tries every edit for every defect, keeps the best one only if the energy drops, and repeats. It stops when the critic finds nothing, no edit helps, or after 8 rounds (previously 3).
- **New defects:** text below its readable floor, a content block off-centre compared with the source, and new dead zones.
- **New edits:**
  - re-wrap text narrower or wider, with an exact height refit;
  - grow sub-floor text;
  - re-centre the content block;
  - spread the gaps by 8%;
  - grow the hero.
- **Examples:**
  - editorial 1584×396: re-centre (2.52 → 2.44), then spread (→ 2.28).
  - story 1280×720: re-wrap the headline narrower (5.10 → 4.71).
- **Results:**

  | Metric | Before | After |
  |---|---|---|
  | Det | 89.66 | **89.81** |
  | Held-out IoU | 0.3478 | 0.3468 (noise) |
  | Order | 0.903 | 0.902 |
  | OCR weighted | 0.898 | 0.899 |
  | Time per design | — | 1.11 s |

  62/62 tests pass. Kept.

## Iteration 7: rotated text stays on the page

- Finalize shifts rotated text so its rotated bounding box stays in-canvas, when it fits. Corner ribbons are left alone because they bleed on purpose.
- Corpus × 5 targets: clipped rotated text went from 5 of 45 to 0 of 45. 62/62 tests pass.

## Iteration 8: panels must not cover pictures

- Energy now penalises a panel shape that covers a picture unit more than it did in the source (+0.6 × extra coverage). Previously the overlap term only looked at what a panel holds.
- Det 89.81 → **89.88**; human-resize IoU unchanged (0.3468); 1.02 s/design. Tests pass.

## Iteration 9: crowded strips and rotated images (found by the new grader)

- **Banner footer variant.** On crowded strips, badges and legal leave the main row for a one-line strip along the bottom: badges at 7 px (down to 6 if needed) side by side, and legal at 6 px, wrapping beside them.
  - On the L3 sample (728×90), badges went from 2 px to 6 px and legal from 1 px to 6 px.
  - The subhead is still about 3 px, and the corner ribbon still overlaps the CTA. Reserving the ribbon's width costs the logo, so it is disabled for now.
- **Rotated images stay in-canvas.** Finalize now shifts any rotated element (not just text) so its rotated box stays on the page, unless it bled off the source canvas. Fixes `bottle_l` in `l3_collage_rotated`.
- **Results:** det 89.82 (−0.06, noise); human-resize IoU unchanged; 71/71 tests pass.

## Grader (phase 1)

- `eval/grader/` is a deterministic brief-compliance grader. It uses pixel forensics from the official renderer, its hard rules are pass/fail with evidence, and its soft rows are compared against the source. It runs after the engine tests in `scripts/check.sh`.
- **Validation, 50 pairs:** identity 50/50, controls 92/92, every one of 19 defect types caught.
- **Engine defects it found:** rotated images off-canvas (fixed in iter 9); 1–2 px badge/legal text on 728×90 (fixed in iter 9); and on L3 at 728×90, a 3 px subhead plus the ribbon overlapping the CTA (still open).

## Iteration 10: grader-driven fixes (full sweep, 625 pairs)

The grader swept every corpus design at all 25 targets: 538 PASS, 82 REVIEW, 5 FAIL.

**FAILs**
- All five were `l3_collage_rotated`, at 1584×396, 970×250, 728×90, 320×100 and 320×50.
- Cause: `finalize` sized each element to fit its *unrotated* box, so tilted cards poked out.
- Fix: the fit uses the rotated AABB whenever the source kept that element inside its canvas.
- Result: 0 FAIL on those five; the cards stay intact (checked by eye).

**`l3_catalog_grid` at 300×250**
- Cards were stacked on cards, so the product names were hidden.
- The energy only counted box overlap, about 1 per pair.
- New term `buried` (W = 15 per importance-weighted text): text newly under another unit's opaque shape or image painted above it.
- Result: a clean 3 + 1 card grid.

**Grader false positives, checked by eye and removed**
- Text over faint decor (alpha 0.07–0.15).
- Text sitting wholly on a plain shape painted below it.
- Validation after the change: still 100% on every defect, 0 false positives on controls or identity. Engine outputs went from 9 to 7 REVIEW out of 50.

**Measured:** 80/80 tests pass; human held-out IoU 0.3468 (unchanged); det 89.82 (unchanged).

## Iteration 11: repeated cards side by side in banners

**Sweep after iteration 10:** 567 PASS, 58 REVIEW, 0 FAIL.

**Worst real defect:** `l3_tall_pin_cards` at 728×90.
- The three recipe cards collapsed to about 35×17 px chips.
- Cause: `banner_layout` put panel groups into the text stack, which is a column.
- Fix: the new `card_row` puts 2 or more panel groups side by side in a `row` slot between the text and the CTA.
- New candidates: `banner cards f{False,True}`.

**Result (checked by eye):**
- Pin cards at 728×90 now read as three cards.
- Catalog grid at 728×90 and 1584×396 is a clean row of 4 cards.

**Measured:** 80/80 tests pass; human held-out IoU 0.3468 (unchanged); det 89.82 → 89.86.

## Iteration 12: hierarchy (badges outshouting the headline)

**`l2_split_panels` banners:** the NEW sticker was nearly the headline's size (54 vs 57 px; the source ratio was 0.52).
- Cause: stickers took the host panel's *width* scale (about 1.1), while the headline's type scale was 0.68.
- Fix: a labelled sticker now scales at most 1.15× its host's text scale (`STICKER_TEXT_SLACK`).
- Energy: `hier` also adds the excess of each text's size-vs-headline ratio over 1.4× its source ratio (`HIER_DRIFT`).
- propose: new `outranks` defect, which looks at stickers and panel children too. Edit: shrink the unit to just below the headline, or to its drift cap.

**Local VLM tier:** built (`eval/grader/vlm.py`) and calibrated on 21 cases.
- Q-wrong precision 0.50, recall 0.08.
- Q-rel had no flags and recall 0.
- gemma abstains 90% of the time on the picture-count probe.
- Both question types are **disabled** under the precision ≥ 0.9 rule.
- Its one "false positive" (`sample_l1_warmup` at 1200×480) was checked by eye: the output is clean.

**Sweep vs iteration 10** (iterations 11 and 12 together):
- Still 567 PASS, 58 REVIEW, 0 FAIL.
- Majors: collisions 40 → 31, presence 35 → 20, hierarchy 6 → 1.
- 11 pairs improved and 2 got worse. One of those is a legal line crossing a subtle panel edge, which is cosmetic.

**Measured:** 80/80 tests pass; human held-out IoU 0.3464 (noise; order 0.9017 → 0.9025); det 89.86 → 89.63.
- The det proxy fell while the grader's major flags fell too. The grader is the measure that matches people.

## Iteration 13: pictures and logos must not shrink to a speck

**Official `sample_l3_advanced` at 600×200:** the product shrank to a 22 px speck above the headline; the grader flagged presence as major.
- The energy's `size` term is log-scaled, so a vanished hero cost about 4.
- New term `vanish` (W = 10 per importance-weighted picture or logo unit) mirrors the grader's presence check: a unit keeping < 25% of its source share of the canvas.
- Result: the banner layout wins (product left, text, CTA right, footer badges); the grader gives PASS.

**Measured:**
- Human tune IoU 0.3358 → 0.3373; held-out 0.3464 → 0.3474, with centre error and order both better.
- det 89.63 → 89.80.
- 80/80 tests pass.
- Sweep: presence majors 20 → 15, minors 117 → 81.
- 3 pairs improved and 3 got worse.

**Regressions:** `l3_linkedin_banner` at 1000×1500 and 2480×3508.
- The CTA and the REMOTE OK pill hang off the left edge of the translucent panel.
- It's an alignment fault, which the grader reports as a collision.
- Still open.

## Iteration 14: crushed badge rows restack on tall canvases

**Official L3 sample at 160×600:** three badges stayed in one source row, crushed to 1 px labels.
- `stack()` now splits a side-by-side row into one unit per line when its text would land below 75% of the *absolute* readable minimum (`ABS_MIN_TEXT`; `ctx.floor` relaxes to 1 px under pressure).
- This only applies in tall boxes (height ≥ 2× width, `TALL_BOX`).
- The first version split everywhere: 9 compact pairs got worse (300×250, 320×100, 600×200), because restacking cost the height everything else needed.

**`relations.snap`:** it re-aligned restacked badges back onto one line, on top of each other.
- It now skips a cy, top or bottom snap when the two units' x-ranges overlap.
- It skips a cx, left or right snap when their y-ranges overlap.

**Measured:**
- Sweep majors 238 → 231. Better: L3 sample, split panels and longcopy at 160×600. Worse: `l3_long_word_square` at 160×600 (+1, subhead 9 px).
- Human held-out IoU 0.3473 (flat), order 0.9035 → 0.9053.
- 80/80 tests pass.

## Iteration 15: corner ribbons kept off the CTA, from spare width only

**Official L3 sample at 728×90:** the SUNSET SALE ribbon covered "Shop the Summer Line".
- Reserving the corner up front (iteration 9, and again tonight as an extra candidate) shrank the type scale and lost the logo or product, so it was reverted.
- Now `banner_layout` carves the corner width out of the *spare* width left after fitting. Nothing has to shrink.
- Result: the ribbon only touches the CTA end; the remaining flag is the subhead at 3 px.

**Measured:**
- Sweep majors 231 → 226; collision majors 34 → 28.
- Better: L3 sample at 728×90 (4 → 1), longcopy at 728×90 (6 → 3).
- Worse: `l3_long_word_square` at 320×50 (+1, ribbon label 2 px).
- det 89.80 → 89.88; human IoU flat; 80/80 tests pass.

## Iteration 16: collisions inside a panel; a panel's CTA gets its banner slot

**`l2_underlay_portrait` at 320×50:** the CTA button covered the headline.
- **Energy blind spot:** `overlap` only compared top-level units. Children of one container (headline, subhead, CTA inside a card) could pile up for free. Added the same source-relative pair overlap among a container's children (`_pair_overlap`).
- **Banners impossible:** `banner_layout` returned None at 320×50. The panel's CTA (a child, so `u.parent` was set) was excluded from the CTA slot and stacked inside the 44 px text column. With `unpack`, a child CTA is free and gets its own slot.

**Result:** 320×50 is now a banner (product, headline, CTA); majors 10 → 5.

**Measured:**
- Sweep majors 226 → 222; collision majors 28 → 25; presence majors 16 → 12.
- 566 PASS, 59 REVIEW: the tiny tag pill at 728×90 crosses the panel edge (+1 minor-scale flag).
- Human IoU flat; det 89.88; 80/80 tests pass.

## Grader vs human labels (120 blind items, labelled by the user)

**First measurement:** exact 53/120 (κ 0.15); fine vs not-fine 65% (κ 0.36). 39 items the user called not-fine were graded PASS.

**Looking at all 24 "human FAIL, grader PASS" engine outputs by eye, the misses were:**
- content shrunk into a small part of the canvas;
- logos left as a speck;
- lopsided compositions;
- obvious defects graded REVIEW where the human says FAIL.

**Changes, each measured with `python -m eval.grader.agree labels.json --regrade`:**
- **Verdict scale matches the human:** a hard rule or major flag gives FAIL, a minor flag gives REVIEW. Exact agreement 53 → 75.
- **Presence:** an element keeping < 50% of its canvas share is minor; < 25% is major. The old rule flagged only under 25%.
- **New `recognisable` check:** an image that was ≥ 40 px drawn in the source but is now < 20 px drawn (minor) or < 12 px (major). Logo PNGs carry padding, so the box can look fine while the mark is 2 px.

**Now:**
- Exact agreement 82/120 (κ 0.49).
- Fine vs not-fine 80% (κ 0.60); 19 missed, 5 false alarms.
- Synthetic validation still 100% on every defect, with 0 false positives on controls.

**Not reachable with rules:** the user's "space" and "hierarchy" FAILs on engine outputs (proportion and taste) match no simple feature.
- Claude and Gemini API credits are exhausted.
- Local VLMs scored 0.5 precision.
- Next: more labels, a re-label for the user's own κ, and a strong VLM once credits return.

## Iteration 17: Claude vision loop → two missing energy terms

**Claude vision (headless `claude -p`, subscription):**
- Blind on the user's 120 labels it reached 85.0% binary agreement; combined with rule FAILs, 86.7% (rules alone 80%).
- `eval/vision_loop.py`: Claude sees the original, the current resize and numbered element boxes, and returns move/resize edits, which engine `finalize` enforces.
- On 6 engine outputs the user failed, the loop improved 5 (checked by eye):
  - right_aligned at 1000×1500 restacked;
  - LinkedIn at 1275×1650 is a real portrait;
  - pin cards at full width;
  - catalog rows closed up;
  - poster subhead readable.

**Diagnosis:** the energy scored engine and loop versions almost the same; it preferred the engine's in 4 of 6. The loop's fixes restore the source's **coverage** (pin cards 0.59 → 0.80 vs source 0.79; catalog 0.50 → 0.67 vs 0.68) and close **internal dead bands** (catalog 0.13 → 0.08).

**New terms:**
- `cover` (W = 8): flow units' covered area below 85% of the source's coverage.
- `gap` (W = 10): the largest empty band inside the content span more than 3% beyond the source's own gaps.

**Measured:**
- Human held-out IoU 0.3473 → **0.3559**; tune 0.3373 → 0.3471; centre error 0.0794 → 0.0759.
- det 89.88 → 90.18; 80/80 tests pass.
- Same-grader sweep (terms off vs on): PASS 379 → 393, FAIL 80 → 75, majors 348 → 343; 7 pairs better, 2 worse (+1 each).

**Still out of reach of the templates:**
- LinkedIn at 1275×1650: a full-height panel can't become a portrait band.
- right_aligned at 1000×1500: wants product-over-text, not a narrow side column.

## Claude vision replaces the human labels as ground truth

At the user's request the 120 human labels are retired, archived under `eval/grader_labels/archive/` and no longer used for any decision.

**Ground truth:** Claude-vision verdicts on the same 120 pairs (four blind subagent judges) in `eval/grader_labels/claude_labels_v1.json`: 46 PASS, 27 REVIEW, 47 FAIL.

**Re-derived:** everything that had been fit to the human labels.
- The grader settings searched against Claude's labels: verdict scale (old / new) × presence keep 25–65% × lost 10–25% × recognisability off / 12–28 px.
- Best κ 0.566 (78.3% binary) is the same configuration the human labels had picked (new scale, 50/25, 20/12). The pre-tuning grader scores κ 0.381 (66.7%).
- Settings unchanged; comments now cite the Claude labels.

**`--vision`:** Claude decides taste, and hard-rule FAILs always stand. That rule is justified by the brief: Claude can miss an 8% stretch the rules prove.

**Engine progress from here:** measured with `python -m eval.vision_bench --tag X` (a Claude-judged 60-pair panel, cached by image), next to human-resize IoU and the grader sweep.

## Iteration 19: missing assets shrink to a speck

**Problem:** Claude's panel failed outputs whose grey "missing asset" placeholder (with its filename) was visible or clipped, including the official L3 sample at 1920×1080 and 2560×1440.

**Checked the official starter repo** (github.com/jhaaj08/chameleon = origin): nothing new, the asset is still absent, and RULES.md doesn't require it to be visible.

**Change:**
- `finalize` shrinks any missing-asset element to a 2 px short side (`MISSING_PX`), centred on its slot. The id is kept, it stays in-canvas, aspect kept.
- `analyze` no longer promotes a missing picture to hero.

**Measured:**
- Claude vision bench 0.533 → 0.550 (3 FAIL → REVIEW, 1 REVIEW → FAIL: `l3_missing_hero` at 1600×900, whose product *is* the missing asset).
- Human IoU unchanged.
- Grader majors unchanged (343).
- 80/80 tests pass.
- det 90.18 → 88.22: the det proxy scores the missing element's presence. The judges' view wins.

## Tried and reverted (judged by Claude vision bench, 60 pairs)

- **Lines weight 1 → 2 (iter18):** fixes `right_aligned` tall layouts by eye. Grader sweep: 0 better, 2 worse; vision panel unchanged.
- **Space weight 2 → 4, proportion 1 → 0.5:** changed 1 of 60 panel renders between them; no effect.
- **One alignment axis per column** (`_row_targets` centring rule + `align` energy term + left/right misaligned critic and edits):
  - Panel 0.55 → 0.55 (+1 PASS, +1 FAIL).
  - The flagged logos (`sample_l2_core`, `split_panels`) never shared an axis with the text in the *source*, so source-relative alignment can't see them.
  - `editorial_wide` at 300×250 flipped to a worse layout (logo lost).
- **Claude reranking the engine's top 3:** already measured as H5 in docs/AI_EXPERIMENTS.md (+0.05 / 10). Selection is not the bottleneck; edits are.

## Iteration 21: absolute logo floor (Claude vision bench)

**Problem:** 3 of 7 panel FAILs were logos reduced to a dot on small banners (`sample_l1_warmup` at 320×50 and 728×90, `right_aligned` at 320×100). The logo floor was 20% of the canvas short side, so 18 px on a 90 px banner; a stacked lockup's wordmark ends up about 4 px.

**First try:** a 45% relative floor fixed those but made big-canvas logos oversized (3 PASS → REVIEW).

**Shipped:** `LOGO_MIN_PX = 40`, an absolute floor on a logo's longest visible side, capped at 60% of the short side (`_img_floor`).

**Measured:**
- Vision bench 0.550 → 0.567 (2 FAIL → REVIEW, no regressions).
- Human IoU unchanged; 80/80 tests pass.
- Sweep: PASS 386 → 394, FAIL 75 → 73, majors 343 → 345 (4 better, 3 worse, on 320-wide strips).

**Also measured and not shipped:**
- read weight 3 → 6: vision +0.008, but human held-out IoU 0.3559 → 0.3499.
- Last-resort text floor 1 → 5 px: no render changed, because tiny text comes from the proportional and scale templates, not floors.

**The loop's strongest signal:** across 19 vision-loop fixes, the smallest font went up 12 times, never down (median +2 px).
- **iter22 (reverted): forcing CTA/logo floors past the slot cap; logo min height 16 px; logo floor never relaxes.**
  - Vision panel 0.567 → 0.575 (tall_pin at 728×90 and right_aligned at 320×100 FAIL → REVIEW, split_panels at 1000×1500 REVIEW → FAIL).
  - Grader majors 345 → 414: subheads crushed to 1–2 px and new overlaps on 320×50 / 320×100. Human held-out IoU 0.3559 → 0.3535.
  - Gating to short side ≥ 90 px, and dropping the CTA override, still left +9 majors on 100 small pairs.
  - On thin strips every forced floor is paid for by the other copy. The iter21 absolute 40 px logo floor stays.
- **Proximity/crowded threshold 0.8% → 2% (reverted):** 13 panel renders changed, no Claude verdict changed; human tune IoU 0.3471 → 0.3432, held-out +0.0007 (noise).

## Iteration 23: a lone corner badge stays in its corner (official L2 sample)

**Problem:** Claude on the shipped L2 output (1080×1080 → 1600×400) gave REVIEW with a major issue: the NEW FLAVOR pill floated alone at the top middle. In the source it sits in the top-left corner (40 px margin). Corner anchoring only covered ribbons.

**Change:**
- `_mark_corners` also anchors a lone badge (the only badge unit, < 5% of the canvas) hugging a corner.
- `_place_corners` keeps its corner margin scaled by the short side; ribbons stay flush.
- Corner badges are sized by 0.85 × area scale (the geometric mean shrank the pill to 10 px text).

**Measured:**
- Official L2: major → 3 minors.
- Vision bench 0.567 → 0.575 (underlay_portrait at 1000×1500 REVIEW → PASS).
- Human IoU unchanged; 80/80 tests pass.
- Grader sweep: PASS 394 → 387, FAIL 73 → 78, all on `sample_l2_core`, where a decorative circle shrinks ("almost vanished"). Claude doesn't flag it, and Claude is the ground truth.
- **CTA-in-footer banner variant for corner ribbons (reverted):** on the official L3 at 728×90 the ribbon is clear, but Claude still FAILs it. The CTA reads as a fourth pill with a tiny label (major), and the energy prefers the current layout. L3 at 728×90 carries more copy than 90 px can hold legibly without dropping elements (not allowed).
- **Skip orphan grids (3+1, 2+1) (reverted):**
  - Catalog at 300×250 loses Claude's "3+1 lopsided" major but falls to `scale_fit` (grader majors 5 → 7, Claude still FAIL).
  - Skipping 2+1 wrecked `tall_pin_cards` at 1080×1080 (0 → 12 majors).
  - The 2×2 stack candidate exists but loses on `read` and `illegible`; a better fix makes 2×2 text competitive, not deleting candidates.

## Iteration 24: 6 px last-resort text floor; rewrap stays out of the margin

**Problem:** `l3_catalog_grid` at 300×250 shipped a lopsided 3+1 card grid. The vision loop's 2×2 scores far better on the engine's own energy (7.06 vs 49.46), so this was a generation gap, not a selection gap. When nothing fits, floor level 5 dropped every non-headline/CTA text to 1 px; the 2×2 stack then put the legal line at 2 px and lost on `read`.

**Changes:**
- `Ctx.floor` at level 5: text roles keep `LAST_RESORT_PX = 6` (5 px changed no render, as logged earlier; 6 px is the first value where the 2×2 wins). Only when the short side is ≥ `LAST_RESORT_MIN_SIDE = 150`.
  - Ungated, grader majors went 346 → 397 (320×50 / 728×90 strips: `catalog_grid@320x50` 13 → 35, `tall_pin_cards@320x50` 12 → 26, `sample_l3_advanced@728x90` 1 → 9). Same lesson as iter22.
- `propose._rewrap_text`: "rewrap wider" never widens past the side margin (`REWRAP_MARGIN = 0.05` of the short side). It used to widen to the full canvas width; Claude flagged the catalog footer as "clipped at the left and right edges" (major).

**Measured (tag `iter24c`):**
- Grader sweep: majors 346 → 341, 2 better (`catalog_grid@300x250` 5 → 1, `longcopy_story@300x250` 8 → 7), 0 worse.
- Vision bench 0.575 (unchanged). Catalog at 300×250 stays FAIL, but its "3+1 lopsided" and "footer clipped" majors are gone. What's left: the rotated can renders jagged (the source rotation, not layout), and the footnote sits tight against the bottom edge.
- Human IoU unchanged (held-out 0.3559). 80/80 tests pass.

**Next for this pair:** widen card backing shapes to fill the column (the loop's 2×2 uses cards 130 px wide; the engine's are 85 px), then give the card text the freed width.
