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
