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
