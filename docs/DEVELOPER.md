# Chameleon engine: developer guide (the short README is at the repo root)

Give it a finished design and a new canvas size. It returns the same elements, rearranged for the new
shape: nothing stretched, nothing colliding, text readable, the brand intact. It runs fully offline
and deterministically, with no model in the loop. It takes about 1 s per design in the amd64
container on Apple silicon.

> **Starting a new session?** Read the top-level [`README.md`](../README.md) first, then
> [`eval/arena/README.md`](../eval/arena/README.md) for the grading tools.


## Run

```bash
pip install -r requirements.txt
python -m engine.main                       # input/*.json -> output/*_output.{json,png}

docker build --platform linux/amd64 -t chameleon-engine .
docker run --rm --network none -v $(pwd)/input:/app/input -v $(pwd)/output:/app/output chameleon-engine
```

The starter kit's original instructions are in `../STARTER_README.md`. The research, the hypotheses
and the architecture rationale are in `../HYPOTHESES.md`.

## See every step

```bash
python -m engine.trace input/*.json --out trace                       # one page per task + trace/index.html
python -m engine.trace eval/corpus/designs/l2_trio_products.json --target 1280x720 --target 728x90
```

Each page has a sidebar and seven sections:

1. **Input:** the source design and its elements.
2. **Understanding:** a role-coloured overlay plus the units, reading order, decoration anchors and background glow.
3. **Insight:** the optional model review.
4. **Candidates:** every candidate with its score breakdown, and a thumbnail of the best from each template.
5. **Touch-up:** defects found, edits tried, and the one accepted.
6. **Decision:** the chosen layout.
7. **Output:** the old-to-new geometry table.

The current plan and decision record is `PLAN_V3.md`. Every AI
experiment and its verdict is in `AI_EXPERIMENTS.md`.

## Grader: does an output meet the brief?

```bash
scripts/check.sh            # engine tests -> engine on input/ -> grader report in output/grader_report/
python -m eval.grader --input task.json --output out.json [--output-png out.png]
python -m eval.grader.validate --n 50     # prove its accuracy on labelled defects
```

- **How it measures:** the grader renders every element on its own, and the design without each element, with
  the official renderer. That gives exact pixel masks for ink, occlusion and off-canvas clipping.
- **Verdicts:**
  - **FAIL** means a hard rule broke: ids, schema, canvas, changed content/fonts/colours/assets, off-canvas,
    image aspect, or a PNG that is not the official render.
  - **REVIEW** means a major flag relative to the source: collision, covered text, text below a legible size,
    an element vanished, the headline no longer the largest text, or a label leaving its pill.
- **Accuracy and design:** on 50 pairs × 19 defect types it catches 100% and raises no false alarms on
  unchanged or harmless outputs. It never imports the engine. Details are in `GRADER_PLAN.md`.

## Approach

The pipeline has four stages: understand the design, propose layouts, score them, and finalize.

```
source + target
  └─ analyze   roles, units, reading order          (engine/analyze.py)
  └─ layout    ~40-120 candidate layouts            (engine/layout.py)
  └─ energy    rubric-mirroring score, pick lowest  (engine/energy.py)
  └─ finalize  contract-safe output                 (engine/resize.py)
```

### 1. Understand the design, without labels

Element ids may be opaque, so everything is inferred from geometry, type, z-order and style.

- **Background and decoration.**
  - Full-bleed images or shapes at the bottom of the stack are background.
  - Faint or edge-bleeding shapes are decoration.
- **Units that move together.**
  - Elements sharing a `group_id` move rigidly.
  - A shape holding content becomes a container: an underlay, a button or a panel.
  - Pictures that overlap or sit side by side as a set become one cluster.
  - Small badges painted on a card or product are pinned to it as stickers.
  - Shadows and halos behind a product are pinned to the product.
  - Ribbons that hug a canvas corner stay anchored to that corner.
- **Roles.**
  - Headline is the largest type.
  - Hero is the largest picture.
  - Logo is a small or wide picture near an edge that isn't a product.
  - Call to action is the button that stands out from its siblings.
  - Badges, legal text, body copy and missing assets are recognized too.
- **Reading order** comes from recursive XY-cut, the classic document-layout algorithm. Bands are read
  top to bottom and columns left to right, so wide banners are read correctly.

### 2. Propose many layouts

Each template is a different topology. Each one is solved exactly, measuring text with the
renderer's own skia font code at the exact integer size it will be drawn.

| Template | When it helps |
|---|---|
| Scale-fit | Nearby ratios: uniform scale, background stays full-bleed |
| Stack | Tall and square targets: one column. Variants try the hero at its source position, top, after the headline, or bottom, and unpacked containers |
| Grid reflow | Families of 3 or more look-alike cards or badges re-flowed to 1–6 columns, e.g. a 2×2 catalog becomes 4×1 at 16:9 |
| Split | Wide targets: a picture column beside a text column, at several ratios and sides |
| Banner | Extreme strips such as 728×90: one row of logo, pictures, text, badges and call to action, all at one shared type scale maximized to fit |

Shared mechanics:

- **One type scale per layout,** found by binary search.
- **Per-role readability floors.**
  - These are absolute pixels plus a fraction of the target's short side.
  - When space runs out, they relax in priority order: legal and badges first; headline and call to action last.
- **Images never distort.** The hero grows into spare space.
- **Text boxes are balanced to their real ink width,** which avoids orphan words.
- **Look-alike siblings share one scale.**
- **Panels hug their content** and extend to the canvas edges they touched in the source.
- **Decorations slide** to the nearest spot clear of text.

### 3. Score and pick

The energy mirrors the rubric. It counts:

- new collisions beyond what the source already had
- readability against the floors
- boldness (no timid heroes or logos)
- hierarchy (the headline stays the largest text)
- use of the canvas relative to the source
- balance
- bare canvas
- out-of-bounds boxes

A small fidelity prior means departing from the source's structure has to earn it.

### 4. Finalize

Every source element is copied and only `x`, `y`, `width`, `height` and `font_size` change: the fields the
brief lets the engine touch. `letter_spacing` (px) and `corner_radius` stay as authored (the renderer clamps a
radius to half the shape, so a shrunk pill is still a pill). Boxes are clamped in-canvas and images keep their aspect.
Any failure falls back to scale-fit, which satisfies the contract by construction.

## Results

The harness is in `eval/`: a 25-design corpus across L1–L3 (half with opaque ids) and 25 real platform
formats. It produces deterministic rubric metrics and AI-judge scores from Gemini and Claude on the
rendered PNGs. The judge gives an unchanged source design 7/10, so 7 is the practical ceiling.

| Engine (150 sampled pairs, seed 7) | Deterministic score (0–100) | Gemini judge (1–10) | Ultrawide det. |
|---|---|---|---|
| H1: uniform scale-to-fit | 78.9 | 4.65 | 58.2 |
| This engine (v1 → v5) | 88.5 → **90.4** | 5.17 → **5.73** | 77.9 → 78.9 |

An independent cross-check with the Claude judge (Sonnet 5.5) on 60 different pairs (seed 21) gave
**5.33 vs 3.95** for this engine against scale-to-fit. Deterministic scores were 88.7 vs 72.6. The
improvement holds across judges rather than reflecting one judge's taste.

**Contract:** `tests/test_contract.py` checks every corpus design × all 25 targets, plus degenerate inputs.
It checks for identical ids, unchanged non-geometry fields, in-canvas boxes, ±5% image aspect,
determinism and no input mutation. All pass.

## What we tried and didn't ship

- **H4, a small local LLM labelling roles** (Qwen3-4B / 1.7B through llama.cpp with a JSON schema):
  - Measured against hand-labelled ground truth for 173 units (`eval/roles_gt.json`).
  - **Results:**

    | Labeller | All units | Headline, hero, logo, call to action |
    |---|---|---|
    | Qwen3-4B | 89.6% | 90.7% |
    | Qwen3-1.7B | 61% | 66% |

  - **Costs:** it adds about 2.5 GB to the image and an estimated 0.5–3 min per design under amd64 emulation.
  - **What we did instead:** we ported its wins into rules (action-verb calls to action, solid-over-translucent buttons,
    round stamps as badges, the line hugging the headline as subhead).
  - **Rules afterwards:** **90.8% / 97.6%** with no model. The rules use only text and geometry, never ids, but they were
    written against this corpus.
  - The code was removed on the v3 branch (git history keeps it).
- **H5, a VLM reranking the top candidates:** this is an upper-bound test.
  - **Method:** Claude Sonnet 5.5 picked the best of the engine's top-3 distinct candidates; Gemini judged the result.
  - **Results:** the energy's own pick scored 5.65 and the Claude-reranked pick 5.70 (40 pairs). Picking the best of the
    three in hindsight would score 5.875.
  - **Conclusion:** a frontier reranker adds +0.05. A 4B local VLM inside an emulated container would add less, at
    minutes per design (`eval/h5_rerank.py`).
- **H6, learned layout models:** these include LayoutDM, FlexDM and PosterLlama. None does conditional retargeting off
  the shelf at a size that runs on CPU, so they were researched and not built.

## Known limits

- Tiny strips such as 320×50 and 728×90 with long copy can't keep every element legible. Low-priority text
  goes small first.
- Minor missing assets (e.g. the L3 sample's `holiday_watermark`) have no pixels to show, and the renderer's
  grey "missing asset" box reads as a broken image to any judge. The element stays in the output with its id,
  inside the canvas and with its aspect, but shrunk to a 2 px speck (`engine/resize.py` `MISSING_PX`), and
  layouts no longer reserve space for it. A missing MAIN picture (>= 8% of the source canvas,
  `analyze.MISSING_MAJOR`) keeps its place as the hero instead: the judge sees the placeholder box in the source
  render too and calls a vanished one "a key element dropped" (PLAN_V3 v4e: four FAILs -> PASS/REVIEW). The
  official starter repo was checked on 2026-10-11: the asset is still absent, and RULES.md doesn't require it
  to be visible.
