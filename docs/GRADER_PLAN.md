# Grader plan: does an output meet the brief?

**Status: phase 1 is built** (`eval/grader/`, run via `scripts/check.sh`).

- **Built:** the deterministic tiers.
  - Hard rules H1–H7. H6b and H7c were folded into H4, H6 and H7, because the official renderer makes them redundant.
  - Forensics.
  - Soft rows: collisions, readability, presence, hierarchy, relations, dead zones.
  - The HTML report, `corrupt.py`, `validate.py` and `tests/test_grader.py`.
- **Not built yet:** the OCR hook in the grader, the local VLM tier (§3), the labelling page (§5.1) and the colour/brand rows C3d–f.
- **Validation on 50 design × target pairs** (`python -m eval.grader.validate --n 50`):
  - Identity: 50/50 PASS.
  - Harmless controls: 92/92 no new flags.
  - All 19 defect types caught at 100%, 33–50 cases each, Wilson lower bound ≥ 0.90.

**Goal.** Given these four inputs, return a verdict a human art director would sign:
- the input JSON and the input render
- the output JSON and the output render
- the target size

**What the verdict must give:**
- near-100% accuracy on everything the brief makes *checkable*;
- an honest, calibrated score for what is *subjective*.

**When it runs.** The grader runs after `pytest tests` in one command.

---

## 0. TL;DR and key decisions

1. **Push almost everything into deterministic checks that use the official renderer.**
   - The renderer is deterministic. I measured this: two renders are bit-identical, and a re-render matches the
     stored `eval/runs/*/outputs/*.png` exactly (max diff 0).
   - A render takes about 0.15 s at 1024 px.
   - So the grader can afford N+3 *private* renders per design and measure each element's real pixels. These are
     "layer forensics":
     - the element alone on two keying colours, which gives its exact ink mask;
     - a knockout render without the element, which gives exactly what is visible of it in the final image;
     - a padded render, which shows ink that would fall outside the canvas.

   Collisions, occlusion, text overflow, clipping, in-canvas ink, image stretch and "same pixels as the asset" all
   become pixel counts. They are not opinions.
2. **The hard rules come from JSON plus pixel proof, never from a model.** Each one is PASS or FAIL with evidence.
   These checks are designed for precision = recall = 1.0 on the synthetic test set. Section 6 covers the honest
   limits.
3. **The soft rubric rows each get a score from 0 to 1, with evidence crops.** These rows are:
   - collisions
   - readability
   - brand
   - intentional layout and hierarchy

   They are built from deterministic signals that the validation set shows separate good from bad. OCR at true
   pixel size is the readability witness.
4. **The "human eye" is a local VLM tier.** It is optional, it abstains, and it **can never override a
   deterministic verdict.** It runs two models:
   - `qwen2.5vl:7b`, the primary;
   - `gemma3:4b`, the second voter.

   How it is used:
   - Each question is binary, asked on a crop with set-of-mark labels, and has forced reasoning.
   - Each image gets a counting probe that measures how competent the model is on that image.
   - Pairwise questions are asked in both orders to cancel position bias.
   - It only raises a REVIEW flag for Gestalt defects that code can't define.
   - **The hosted judge (`eval/judge.py`, Claude/Gemini) is the gold tier when credits return.**
5. **Accuracy is proven, not claimed.**
   - `eval/grader/corrupt.py` generates labelled defects at three severities, about 20 defect types × 675
     (design, target) pairs.
   - The positives are:
     - clean engine outputs;
     - "GT-projected" human resizes: the human geometry applied to the source's fields;
     - and identity resizes.
   - We report per-check precision and recall with Wilson 95% confidence intervals.
   - About 150 pairs get human labels from the user on a blind labelling page. That is the only real ground truth
     for the subjective rows.
6. **The grader is independent of the engine.**
   - `eval/grader/*` may import `eval/layout.py`, `eval/metrics.py` and `renderer/*`.
   - It may **never** import `engine/*`.
   - A grader that shares the engine's role and relation code shares its blind spots, and the engine would
     Goodhart against it.

**Measured facts that shape the plan:**

| Fact | Consequence |
|---|---|
| Renderer is deterministic, 0.15 s per render, and re-render equals the PNG | Pixel forensics is exact and cheap |
| `qwen2.5vl:7b` returns valid JSON with `format`, about 19 s per query warm on the M2 | The VLM tier is minutes per pair, so it is off by default in batch |
| `qwen3-vl:4b` returns an **empty** reply with `format` + `think:false` (matches AI_EXPERIMENTS.md) | Excluded |
| On the 178 human GT pairs, **none** of the raw "expected" designs passes the hard rules (888 `asset_id`, 454 aspect, 190 content, 49 off-canvas, 4 font diffs) | Raw GT can't be a hard-rule positive. It must be *projected* (§5.1) |
| The machine is an Apple M2 with 16 GB RAM and 40 GB free disk; the main `.venv` has no torch, cv2, scipy or skimage; the ImageSorcery venv has torch 2.14, torchvision, cv2, skimage, EasyOCR and ultralytics | Heavy work runs in the ImageSorcery venv as a subprocess worker, which is the proven `eval/ocr/worker.py` pattern. No new venv in phase 1 |

---

## 1. What "meets the brief" means: requirements

**Sources, all read in full:**
- *Chameleon Challenge.pdf*, all 14 pages, including §2.5, §4, §7.2 and the FAQ
- The starter repo (`origin/main`): `README.md`, `RULES.md`, `FAQ.md`, the schema, `engine/main.py`
  (`self_check`), `renderer/converter/*`, `brand_kit/README.md`, and the three `input/` samples with their `notes`
- Our repo:
  - `README.md`, `HYPOTHESES.md`
  - `docs/*`
  - `tests/test_contract.py`
  - `eval/*`

  Only `requirements.txt` differs from the starter's plumbing.

### 1.0 The brief's own words, and what each implies for the grader

| Brief or starter text | Grader implication |
|---|---|
| "same element `id`s … nothing added or removed, required fields present, nothing off-canvas, images kept in proportion" (RULES §What gets compared) | H1–H6 |
| "A submission with plausible-looking JSON that doesn't actually render legibly doesn't get credit for the JSON, and a rendered image that happens to look fine despite a broken or dishonest JSON doesn't get credit for the image. Both are checked." | The verdict is **joint**: JSON rules *and* render-level checks; neither alone gives a PASS |
| "Do not regenerate, redraw, or replace the pixel content of any asset … disqualifying" | H7 (asset identity + PNG honesty + per-image forward model) |
| "treat content and font family as fixed: resize and reposition, don't rewrite"; "Changing font size … is expected" | H4 whitelist: geometry + `font_size` (+ `letter_spacing` scaled with it) |
| "rearrange the design, not simply shrink or stretch it"; "move the furniture" | C6 penalises letterboxing and uniform-scale dead bands relative to the source. A *nearby-ratio* target (L1) legitimately stays close to scale-fit, so this is compared with the source, never absolute |
| §2.5 "overlaps are fine when they're intentional (layered graphics, deliberate stacking) … text never sits on a busy image" and L2's `deco_circle_1._note`: "intentionally overlaps the product … not a collision to flag" | C1 is **source-relative**: only overlap in excess of the same pair's source overlap counts. "Text on a busy image" is its own check (C1c) |
| "Everything is readable — text is large enough and has enough contrast; nothing is cut off" | C2: size, OCR, contrast, overflow, canvas clip |
| "colours, fonts, and the logo survive the resize intact and in proportion" | C3 + H6. The logo is a **white wordmark** (`brand_kit/README.md`: "invisible against a white viewer"), so the grader also checks that every image's ink keeps contrast against what lies under it (C3f) |
| "things line up, spacing is even, the most important element still stands out, pictures keep their proportions" | C4, C5, C6 |
| "holds up everywhere — many different designs and shapes" | C7, batch-level only |
| "speed and reliability is a rubric row"; "Latency is not a disqualifying concern" | C8, reported only |
| L3 note: the missing asset should be handled "gracefully (e.g. skip it, placeholder it) rather than crash" | The missing-asset element is **exempt** from presence (C3b) and from collapse. It must still keep its id. A *prominent* grey placeholder is a defect (V1, and a C6 area flag) |
| L3: "a run-on headline, a variable-count set of badges, a rotated element" | Rotated ink (H5b), and look-alike siblings sharing one scale (C4 sibling-consistency flag: sibling badges' font sizes differ by > 10%) |
| `engine/main.py` renders with `render_design_to_png(...)`, whose `max_size` defaults to 2048. I verified that output PNGs are at **true size** (728×90 stays 728×90); only canvases > 2048 are downscaled | The grader renders at `min(2048, max(W, H))`, which is exactly the official PNG. Readability thresholds therefore apply to what the AI evaluator sees |
| Target sizes come from "a fixed catalogue of common real-world formats" | Validation uses `eval/targets.json` (27 catalogue formats) |
| Sample elements carry extra non-schema keys (`_note`, `_adversarial_note`); `element` has no `additionalProperties:false` | H1 must allow unknown element keys. H4 requires them to be copied through unchanged

### 1.1 Hard rules (PASS/FAIL, any FAIL means the verdict is FAIL)

| ID | Rule | Verifier | Method | Threshold |
|---|---|---|---|---|
| H1 | Output is schema-valid | JSON | `jsonschema` (already in .venv) against `chameleon_design_schema_v0.9.json`, plus finite numbers | exact |
| H2 | Same ids: none added, removed or duplicated | JSON | Reuse `eval.metrics.structural` | exact |
| H3 | Canvas equals target | JSON | `structural(target=...)` | exact |
| H4 | Non-geometry fields unchanged | JSON | Every source key except the whitelist `{x, y, width, height, font_size, letter_spacing}` must be equal. This covers `type`, `content`, `font_family`, `asset_id`, `color`, `fill_color`, `style_runs`, `group_id`, `fit`, `opacity`, `rotation` and `z_index`. Same rule as `tests/test_contract.py` | exact. `z_index` and `rotation` changes are configurable to WARN until the organisers rule on them |
| H5a | In canvas, by JSON | JSON | The rotated AABB of each box is inside `[0, W] × [0, H]`. Elements already off-canvas in the source give WARN (as `structural` does) | 0.5 px tolerance |
| H5b | In canvas, by ink | Pixel | **Padded render:** shift every element by P = 25% of max(W, H) onto a (W+2P)×(H+2P) canvas. Ink of element *e* outside the inner rectangle shows that it was cut off by the canvas. This also catches rotated elements and text overflowing past the canvas edge, which a JSON box check misses | ≤ 0.5% of the element's ink and ≤ 2 px deep |
| H6a | Image aspect within ±5% | JSON | Per the test contract: non-`cover`, non-full-bleed, `(w/h)/(w0/h0)` | ±5% |
| H6b | Image not distorted, by pixels | Pixel | Fit the element's ink mask (alone render) against the asset's alpha bbox, mapped through `assets._fit_image`. The measured visible-ink aspect divided by the asset ink aspect must be 1 | ±5%. Catches `fit:"fill"` stretch, which the JSON check exempts if `fit` were mis-set |
| H7a | No regenerated pixels: asset identity | JSON + file | `asset_id` unchanged (H4). `sha256` of each referenced asset file equals a manifest frozen from `brand_kit/assets` at grader build time | exact |
| H7b | No regenerated pixels: PNG honesty | Pixel | Re-render the output JSON with the official renderer. Compare it with the supplied output PNG: max abs diff ≤ 2/255 on ≥ 99.9% of pixels. Do the same for the input PNG against a render of the input JSON | as stated |
| H7c | No regenerated pixels: per-image forward model | Pixel | For every image element, the *expected* pixels are computed exactly: asset → `_fit_image(fit)` → rotate → paste. Compare them with the output render, but only on the element's knockout-visible mask *V_e* (see §2): SSIM ≥ 0.98 and mean ΔE00 ≤ 2. Missing assets must match the grey placeholder | as stated |
| H8 | Text content rendered as written | OCR (soft-hard) | Already guaranteed by H4 in the JSON. OCR on the alone render at 2× is only a renderer sanity check (font fallback, tofu). It gives WARN, never FAIL | normalised similarity ≥ 0.9 |
| H9 | Engine sanity | pytest | `tests/test_contract.py` passes. Determinism: two `resize()` calls give the same result | exact |

**Why H7b and H7c matter even though the official scorer renders our JSON itself.** A pipeline that post-processes
PNGs is itself a disqualification risk. The same forward model also *proves* "same picture, not stretched or blurred"
per element, which is the "crop-and-compare" the user asked for, with an analytic ground truth instead of a learned
metric.

### 1.2 Soft rubric rows (each scored 0–1, with evidence)

| ID | Rubric row (brief §2.5) | Main signals (deterministic unless marked) | FLAG threshold (shown in report) |
|---|---|---|---|
| C1 | **No unwanted collisions** | (a) Ink-on-ink overlap for pairs (a, b) not sharing `group_id`: ‖M_a ∩ M_b‖ / min(‖M_a‖, ‖M_b‖), in excess of the same pair's overlap in the source. This reuses the `eval.metrics.collisions` pair weights but uses *ink masks*, not boxes. (b) **Text occlusion:** the visible fraction ‖V_t‖/‖M_t‖ of each text, compared with the source. (c) **Text on busy image:** edge density and luminance std under the glyph pixels in the text-free render. Reuse `_glyph_contrasts`, then compare against the source | (a) excess > 2%. (b) visible fraction < 0.98 when it was ≥ 0.98 in the source. (c) edge density > 0.12 and > 1.5× the source |
| C2 | **Everything is readable** | (a) Font px at true output size: an absolute floor of 8 px (6 px for legal), plus x-height in px from skia font metrics. (b) **EasyOCR at true size** on each text's ink bbox (`eval/ocr/worker.py`). (c) WCAG contrast under the glyphs, capped at what the source achieved (existing `pixel_checks`). (d) **Overflow:** ink outside the JSON box by more than 2 px, or overlapping a neighbour. (e) Clipped by canvas (H5b) | OCR < 0.6 means unreadable; 0.6–0.85 is weak. x-height < 4 px means FLAG. Contrast below the cap |
| C3 | **Stays on brand** | (a) Colours, fonts, `asset_id` (H4). (b) **Every element is still present:** visible ink share ≥ 25% of its source share relative to the canvas, or an absolute 0.05% of the canvas. This catches "logo shrunk to a speck" and "covered up". (c) **Cropping of brand imagery:** for `cover` images, the fraction of the asset's alpha content cropped away. FLAG if > 15% of a non-background image (a logo cut in half). (d) Global Lab colour histogram, source vs output, with Earth Mover's Distance. This catches colour blocking lost when a panel is buried. (e) **Text stays on its colour:** the median colour under each text's glyphs, source vs output, ΔE00. (f) **Image visibility:** each image's ink must keep its contrast against what is under it, comparing the image's ink pixels in `full` with the same pixels in `without[e]` (median ΔE00 vs the source). This catches the white wordmark logo moved onto a light area | (b) below the floor means FLAG; the missing asset is exempt (the brief allows skipping it). (d) EMD above the calibrated p95 of positives. (e) ΔE00 > 20 means FLAG. (f) ΔE drops below 50% of the source's value |
| C4 | **Looks intentional: hierarchy** | (a) Spearman of font sizes and of visible ink areas, source vs output (reuse `metrics.hierarchy`, on ink). (b) **Top-k importance kept.** Importance = visible ink area × mean contrast with its local surround. The top-1 element in the source should stay top-2 in the output, and the source's largest text should stay largest | ρ < 0.6, or the top element is lost |
| C5 | **Looks intentional: relations kept** | **The containment and contact graph from source masks**, built independently of `engine/relations.py`. The pairs are: ink of *a* ≥ 90% inside ink of *b* (text on a button, product on a disc, sticker on a card); concentric (centre offset < 10% of the host size); and "under" (a shadow's centre-x within 10% and directly below). Each source relation must hold in the output with the same tolerance. This is the deterministic detector for "detached shadow", "label left its pill" and "sticker floats loose" | any broken containment means FLAG, with a crop pair |
| C6 | **Looks intentional: alignment, spacing, balance, space use** | (a) Existing `metrics.alignment` (shared anchors relative to the source). (b) Spacing evenness: the coefficient of variation of vertical/horizontal gaps between consecutive units in XY-cut reading order. (c) Balance: the drift of the ink centroid from the source, weighted by contrast. (d) **Dead zones:** the largest empty rectangle on a 64×64 ink-occupancy grid, as a fraction of the canvas, compared with the source. (e) Coverage and spread (existing `utilization`) | relative to the source. Thresholds are fitted on positives (p5) |
| C7 | **Holds up everywhere** (batch only) | Pass rate and score distribution per aspect bucket (tall, square, wide, ultrawide) and per level. Report the worst 10 with links | — |
| C8 | Speed and reliability | Per-design engine seconds (from the harness), crash or fallback count (from `eval/sweep.py`) | — |
| V1 | **"Something obviously wrong" (human eye)** | VLM tier, §3 | REVIEW only |

**Why ink masks instead of boxes.**
- Box overlap gives false positives on transparent PNG padding and on wrapped text with a short last line.
- Box overlap gives false negatives on rotated elements.
- `eval/layout.visible_rect` already approximates ink with an alpha bbox. Masks are exact.

---

## 2. Core primitive: layer forensics (`eval/grader/forensics.py`)

All the renders here are **private to the grader**: it may add helper shapes to its own copies, never to the output.
They go through `renderer.converter.convert.convert_chameleon_to_crello` and `renderer.render(...)` (the same path as
`eval/layout.render`). Every render is at the **official PNG size** (`max_size = min(2048, max(W, H))`, which is what `engine/main.py`
produces). That is true size for every catalogue format, so pixel thresholds mean real pixels.

For a design D with elements e₁…eₙ (z-sorted):

| Render | Construction | Gives |
|---|---|---|
| `full` | D as is | The image the judge sees. Compared with the supplied PNG (H7b) |
| `alone[e]` on key K1 / K2 | Only *e*, over a full-canvas helper rectangle at z = −∞, coloured K1 = (255, 0, 255) and then K2 = (0, 255, 0) | **Ink mask M_e**: pixels that differ from the key in *either* render (Δ > 8/255). The two keys make white-on-white and black-on-black elements measurable. Anti-aliased alpha comes from the K1/K2 difference |
| `without[e]` | D minus *e* | **Visible mask V_e** = pixels where `full` and `without[e]` differ (Δ > 8/255). Occlusion of *e* = 1 − ‖V_e‖/‖M_e‖ |
| `notext` | D without text (`render_text=False`, as metrics does now) | Background under glyphs: contrast, busyness, ΔE |
| `padded` | D shifted by P onto a bigger canvas | Ink outside the canvas (H5b) |
| Same set for the **source** | | Source baselines: intentional overlaps, relations, importance |

**Cost.** (2n + 3) renders × 0.15 s per design and side. With n = 17 that is about 5.5 s for output plus source, and
the source side is cached per source hash.

**Elements.** An element's masks only depend on (element, W, H), so they are cached by `sha256(json(element) + W + H)`.

**Gotchas to handle:**
- Opacity < 1. The mask uses alpha, not the binary difference.
- Elements completely hidden in the source too. Compare with the source, never with absolute values.
- Group members. Collisions inside a group are skipped, but occlusion is still measured.
- Missing-asset placeholders. They are real pixels: treat them as an image.
- Text with rotation. The masks are exact, so nothing special is needed.

---

## 3. Human-like vision tier (`eval/grader/vlm.py`)

### 3.1 What the research says, and why the tier is narrow

- **"Can GPTs Evaluate Graphic Design Based on Design Principles?"** ([arXiv 2410.08885](https://arxiv.org/html/2410.08885v1))
  - Method: perturbation-made defects (alignment, overlap, white space).
  - GPT-4o tracked humans better than O'Donovan heuristics *on large perturbations*.
  - It was **worse than heuristics on small ones**, because it misses subtle detail that vector values capture.
- **DesignSense-10k** ([arXiv 2602.23438](https://arxiv.org/abs/2602.23438)): frontier VLMs are "unreliable overall"
  on layout preference and "fail catastrophically" on the four-class good/bad pairwise task. A trained judge helps,
  but its weights and base model aren't stated as released.
- **AesEval-Bench (ICLR 2026)** ([arXiv 2603.01083](https://arxiv.org/html/2603.01083)) and **DesignProbe** report
  large gaps for open VLMs on design-aesthetic judgement and localisation.
- **AesthetiQ (CVPR 2025):** its VLM evaluator reached about 79% agreement with human preference.
- **Position and visual bias in VLM judges.**
  - [Fooling the LVLM Judges, 2505.15249](https://arxiv.org/html/2505.15249v2): judges are vulnerable to visual
    biases; chain-of-thought only partly helps.
  - Swap-consistency protocols ([GenArena 2602.06013](https://arxiv.org/pdf/2602.06013)) keep a verdict only if it
    survives swapping the order. This also discards near-ties.
- **In-repo evidence** (`docs/AI_EXPERIMENTS.md`):
  - The strict judge's re-judging noise is sd 0.93.
  - An unchanged source scores 7/10.
  - Pairwise preference on coherent near-ties is about 0.5.
  - Qwen3-VL-4B never returned an answer.
- **Visual prompting.**
  - Set-of-Mark overlays (Yang et al. 2023) and crop/zoom of the relevant region ("MLLMs know where to look", ICLR
    2025, the ViCrop line) raise small-detail accuracy.
  - We have the *exact* boxes and masks, so both are free and exact here.

**Conclusion.**
- A VLM is good at *large, Gestalt* defects and bad at subtle, near-tie judgements.
- So it never scores the layout on a 1–10 scale in this grader.
- It answers **targeted binary questions**, it **abstains** when its competence probe fails, and it can only
  **escalate** a pass to REVIEW.

### 3.2 Protocol (the user's vision-grading rules, made concrete)

**Models** (Ollama, `temperature 0`, `format` = JSON schema):
- `qwen2.5vl:7b` (primary; verified working, about 19 s per query warm);
- `gemma3:4b` (second voter; vision-capable);
- `qwen3-vl:4b` excluded.

When credits return, `eval/judge.py`'s Claude/Gemini backends plug into the same question bank as the gold tier.

**For each pair:**

1. **Images before text.** Every message is `[image(s)] → question`, so the image tokens come first.
2. **Set-of-mark overlay.** On a copy of the output render, draw a thin numbered outline per *unit* (group or
   element) from the JSON boxes. Number labels go in the margin, so they never sit on content. The legend in the
   prompt maps a number to its type and its first 20 characters of content. The model refers to numbers, not
   descriptions.
3. **Competence probe (counting and verification loop).**
   - Ask: "List every separate block of text you can read, verbatim, and count the pictures."
   - The grader knows the truth from the ink masks and the JSON.
   - If the text recall is below 0.8 or the picture count is off by more than 1, mark the model *incompetent on this
     image*. Its answers are discarded (ABSTAIN), and the report says so.
   - A second probe asks a question with a known answer, e.g. "Do boxes 3 and 5 overlap?", where the masks say
     no. A wrong answer means ABSTAIN.
4. **Crop/zoom per question.** Questions about one relation get a crop of the union bbox + 20% margin, upscaled so
   its short side is ≥ 448 px. Source and output crops are shown side by side; a full-frame thumbnail goes first for
   context.
5. **Forced step-by-step.** The JSON schema has the form `{"observations": str (describe what you see in the region
   first), "answer": "yes"|"no"|"unsure", "confidence": 1–3}`. The `observations` field comes before `answer` in the
   schema order.
6. **Question bank (binary, generated from deterministic findings or relations):**

   | Question | When it is asked | Purpose |
   |---|---|---|
   | Q-rel | For each source relation from C5 | "In A, item 4 sits on/under item 2. In B, does it still?" |
   | Q-detached | For shadows, halos and discs | "Does any shadow/halo/disc look detached from what it belonged to?" |
   | Q-wrong | Always, once, at full frame | "Would a designer say something is obviously broken here? Name the box numbers." Open search; only `yes` with confidence 3 **and** naming valid box numbers counts |
   | Q-text-bg | For each text with a C3e ΔE flag | "Is the text in box 7 on the same kind of background as in the original?" |
   | Q-placeholder | For a missing asset | "Is a grey 'missing asset' box given prominent space?" |

7. **Position-bias swap.** Every source-vs-output question is asked twice: [A = source, B = output] and the reverse.
   The answer counts only if both orders agree; otherwise it is UNSURE.
8. **Ensemble.** The two models × two orders give 4 votes. FLAG needs ≥ 3 agreeing `yes` and no competence ABSTAIN.
   A disagreement gives UNSURE, which goes to the report and never to the verdict.
9. **Authority.**
   - The VLM can escalate PASS to **REVIEW**. It never turns a deterministic FAIL into a PASS, or the reverse.
   - Deterministic checks always win on the things they measure.
10. **Calibration.**
    - Each question type gets a measured precision on the labelled set (§5).
    - A question type with precision below 0.9 is disabled automatically. That rule is written down; the switch is
      a flag in `vlm.py`.

**Runtime.** At most about 6 queries × 2 orders × 2 models × about 15–20 s ≈ 3–6 min per pair on the M2. So the tier
is:
- **off** by default in batch;
- `--vlm local` for single pairs or a sampled 20;
- always on for pairs the deterministic tier scores in the borderline band.

### 3.3 Cheap perceptual models (optional, phase 2+; YAGNI until a check needs them)

| Model | pip | Device | Use | Status |
|---|---|---|---|---|
| SSIM / MS-SSIM | `scikit-image` (in the ImageSorcery venv) | CPU | H7c forward-model match, crop compare | **Phase 1** |
| DreamSim (ensemble ViT, NeurIPS 2023, trained on human similarity) | `dreamsim` | MPS/CPU, about 1 GB weights | Global "still the same design" score, source vs output, letterboxed to the same aspect. A *feature*, not a rule | Only if C3 needs it after validation |
| LPIPS / DISTS | `lpips`, `piq` or `pyiqa` | MPS/CPU | Element crop vs expected crop. Redundant with the exact forward model (H7c) | Skip |
| UMSI saliency (Fosco et al., UIST 2020, graphic-design importance) | Keras/TF weights from predimportance.mit.edu | CPU | Swap in as the C4 importance map | Skip unless the C4 contrast-area proxy fails validation; the TF port cost is real |
| LAION aesthetic, Q-Align | — | — | Trained on photos, not layouts; poor fit | Skip |

---

## 4. Output format

### 4.1 `grade.json`, per pair

```json
{
  "grader_version": "1.0",
  "task": "...",
  "target": [1080, 1920],
  "verdict": "PASS | FAIL | REVIEW",
  "hard": [{"id": "H5b", "status": "FAIL", "element": "cta_text",
            "evidence": {"ink_outside_px": 312, "crop": "crops/H5b_cta_text.png"}}],
  "criteria": {
    "collisions":  {"score": 0.94, "flags": [{"pair": ["t1", "img2"], "excess": 0.07, "crop": "..."}]},
    "readability": {"score": 0.81, "flags": [{"id": "legal", "ocr": 0.42, "font_px": 7, "crop": "..."}]},
    "brand":       {"score": 1.0, "flags": []},
    "hierarchy":   {"score": 0.9, "flags": []},
    "relations":   {"score": 0.5, "flags": [{"rel": "contained", "a": "shadow", "b": "bottle", "crop_src": "...", "crop_out": "..."}]},
    "composition": {"score": 0.88, "flags": []}
  },
  "vlm": {"model_votes": "...", "competence": {"qwen2.5vl:7b": "ok"}, "flags": []},
  "overall": 82.4,
  "timing_s": {"t0": 0.01, "forensics": 4.8, "ocr": 2.1, "vlm": 0}
}
```

### 4.2 Verdict logic

- **FAIL:** any hard rule fails.
- **REVIEW:** otherwise, any criterion has a FLAG of severity "major", or there is a VLM FLAG.
  - Major flags are: text occluded or unreadable (OCR < 0.6 on a top-2 text by size), a broken containment
    relation, an element collapsed, or brand imagery cropped by more than 15%.
- **PASS:** otherwise.
- **Overall score:** 100 × Σ wᵢ·Cᵢ, using `eval.metrics.WEIGHTS`-style weights. It is reported, but **the verdict
  never depends on the weighted sum.** Flags are interpretable; sums are not.

### 4.3 HTML report (`eval/grader/report.py`)

- A self-contained file, styled like `eval/blind_ab.py` (base64 images, no network).
- **Header:** the verdict badge, then source | output side by side at the same height.
- **Hard-rule table:** each FAIL comes with an evidence crop: red mask overlay, ink outside the canvas in magenta.
- **Per criterion:** the score bar plus its flags. Each flag shows a **source crop next to the output crop** for the
  same element ids, scaled to the same height, with mask outlines.
- **Overlays**, toggled by tabs: ink masks coloured per element, collision pixels, dead-zone rectangles, the
  importance heat map, and the set-of-mark image the VLM saw, with its Q/A transcript.
- **Batch index:** a sortable table by verdict, score and bucket, plus the C7 per-bucket summary and the worst-10
  strip.

---

## 5. Validation: proving the accuracy

### 5.1 Labelled set (`eval/grader/corrupt.py` → `eval/runs/grader_val/`)

#### Base pairs

- 25 corpus designs × 27 targets = 675 engine outputs.
- Plus 178 adobedata GT pairs, which use `CHAMELEON_BRAND_KIT=../adobedata/data/pairs/brand_kit`.

#### Positives (expected: PASS, or at worst REVIEW)

- **P1, engine outputs.** Those passing `test_contract` and judged clean.
  - These are *noisy* positives: engine bugs exist.
  - They are only "presumed clean" until the human-labelled subset confirms them.
- **P2, identity.** The source resized to its own size by returning it unchanged. It must PASS, with every criterion
  ≥ 0.95. This is the strongest sanity check.
- **P3, GT-projected human resizes.**
  - Take the human-made geometry (x, y, w, h, font_size) for each matching id.
  - Keep **every other field from the source**.
  - Fix the image aspect by fitting inside the human box. Clamp in canvas.
  - Drop pairs where more than 20% of ids don't match.
  - These are designer-quality layouts that satisfy the hard rules by construction, and they test soft-row false
    positives.
- **P4, legitimate-variation controls.**
  - 1–2 px nudges.
  - Intentional source overlaps kept.
  - Text shrunk only to the floor + 2 px.
  - A full-bleed background with `cover`.

  These must **not** fire. They measure precision.

#### Negatives (one function per defect; each sets an exact label of which check must fire)

Each runs at severities S/M/L:

| Corruption | Expected check |
|---|---|
| Stretch an image's `width` by 1.03 / 1.08 / 1.5 (the 1.03 case is a *negative control*: must pass) | H6a / H6b |
| Same stretch, with `fit` switched to `"fill"` | H4 (fit changed) + H6b |
| Move text off-canvas by 2 / 20 / 50% | H5a / H5b |
| Set rotation so that ink crosses the edge while the box stays inside | H5b |
| Shrink text to 3 / 5 / 7 px | C2 (FAIL band), OCR |
| Overlap elements: move a text onto another text, an image or a busy background, at 5 / 20 / 60% overlap | C1 |
| Bury the text: raise an opaque shape's `z_index` (H4 FAILs too; also run a variant that *moves* the shape over the text with z unchanged) | C1b |
| Detach the shadow or halo: offset by 15 / 40 / 100% of the host size | C5 |
| Pull a label out of its pill or button | C5, C3e |
| Change content (one char, a word, the whole text) | H4 |
| Change `font_family`, `color`, `asset_id` | H4 |
| Drop an element, duplicate an id, add an element | H2 |
| Collapse an element to 1 px, or opacity 0 | C3b (and H4 for opacity) |
| Crop the logo with `cover` in a narrow box | C3c |
| Scramble hierarchy: swap the headline and legal font sizes | C4 |
| Cluster everything in one corner; letterbox | C6 |
| **PNG-level:** Gaussian blur σ = 0.5 / 1.5 / 4 over one image's region; JPEG q = 30; hue shift 10°; paste a different asset or noise into an image region ("regenerated"); inpaint-like median blur | H7b, H7c |
| Missing asset given 60% of the canvas | C6 area flag / V1 (shrinking or hiding the missing asset must **pass**: the brief allows skipping it) |
| Make one sibling badge's font 30% smaller than its siblings' | C4 sibling-consistency |
| Move the white logo onto a light region or panel | C3f |

That is about 20 types × 3 severities × a stratified sample of 120 base pairs ≈ 7,200 cases. The deterministic tier
runs at about 5 s per case on 8 processes, which is about 75 min. The VLM tier runs on a stratified 150.

#### Human labels (the only ground truth for "intentional")

- Extend `eval/blind_ab.py` into `eval/grader/label_page.py`.
- It shows 150 pairs: 50 engine outputs, 50 P3 GT-projected, 50 random corruptions at M severity.
- For each pair the user records:
  - PASS / REVIEW / FAIL;
  - tick-box defect tags matching C1–C6;
  - a 1–5 overall rating.
- Labels are saved to localStorage and exported as JSON.
- Re-label 30 of them a day later, which gives the user's own test-retest κ: the ceiling we can't exceed.
- The 1,862 cached Gemini/Claude judge results in `eval/.cache/` are a *weak* label source, used for correlation
  only, never as truth.

### 5.2 Metrics reported (`eval/grader/validate.py`)

- **Per check:** precision, recall and F1 with Wilson 95% confidence intervals, by severity.
- **The false-positive rate on P1–P4.** This is the number that matters most for a grader.
- **Verdict confusion matrix** against human labels; Cohen's κ (grader vs user) next to the user's test-retest κ.
- **Score correlation:** Spearman of the overall grade against the human 1–5 rating, and against weak Gemini/Claude
  overall scores.
- **VLM tier:**
  - per question type, precision and recall;
  - the abstain rate;
  - swap-consistency rate;
  - per-model competence-probe pass rate.

### 5.3 Targets (acceptance)

| Tier | Target |
|---|---|
| H1–H7 on synthetic | P = R = 1.000 at S/M/L, except the 1.03 stretch negative control, which must pass. Any miss is a bug, not a threshold |
| H5b, H6b, H7b, H7c pixel checks | R ≥ 0.99 at M/L. FPR ≤ 0.5% on P1–P4. S-severity blur (σ 0.5) may be missed; that is reported |
| C1 collisions, C2 readability (OCR) | Precision ≥ 0.95 and recall ≥ 0.9 at M/L |
| C3–C5 | Precision ≥ 0.9 and recall ≥ 0.8 at M/L |
| C6 composition | Reported only. Spearman with human 1–5 ≥ 0.5 |
| Overall verdict vs human labels | Agreement ≥ 0.9 on "clearly broken vs fine" (FAIL vs PASS). κ within 0.1 of the user's own test-retest κ on the full 3-way |
| VLM question types | Kept only if precision ≥ 0.9; the abstain rate is reported |

---

## 6. Honest limits: what will NOT reach near-100%

1. **"Looks intentional", balance and brand intent have no objective ground truth.**
   - Humans disagree with themselves on near-ties.
   - The in-repo graders agreed only 28–37% when scores were within 0.25 of each other.
   - The grader *reports* these with evidence. It does not certify them. Expect κ around 0.5–0.7 at best.
2. **Synthetic corruptions are easier than real engine failures.**
   - Recall on `corrupt.py` overstates real recall.
   - Mitigation: mine real failures from `eval/sweep.py` and the labelled set, and report them separately.
3. **The official AI evaluator is unknown.** A PASS here does not predict its score; it means "meets the stated
   brief". The hosted judge (`eval/judge.py`) remains the proxy for it.
4. **Thresholds for "busy background" and "dead zone" are heuristic.**
   - They are calibrated to the positives' p5/p95, not to perception.
   - They miss low-contrast but textured backgrounds.
5. **Local VLMs.**
   - 7B on an M2 runs at about 20 s per query.
   - They miss subtle defects (research and in-repo evidence).
   - `qwen3-vl:4b` is unusable.
   - This tier's realistic value is catching a few *gross* defects that the code lacks a definition for. Its
     precision is gated by the competence probe.
6. **Renderer drift.** All pixel proofs are relative to the vendored renderer (`cr-renderer@a17e1fb`). If the
   official renderer differs, H7b compares against the official PNG only when it is supplied.
7. **Rules still awaiting an organiser ruling:**
   - whether `z_index`, `rotation` or `opacity` changes are allowed (currently FAIL, per test_contract; configurable);
   - whether a `cover` crop counts as "in proportion";
   - elements that were already off-canvas in the source (currently WARN).
8. **Goodhart risk.** If the engine is tuned on grader scores, C-row signals become targets. Keep the human-labelled
   set and the VLM/hosted tiers as held-out acceptance, never as tuning signals.

---

## 7. Integration

### 7.1 CLI

```
python -m eval.grader --input task.json --output out.json [--output-png out.png] [--input-png src.png]
                      [--vlm off|local|hosted] [--report grader_report/] [--json grade.json]
python -m eval.grader --batch output/ --tasks input/ [--vlm off] [--report grader_report/] [-j 8]
python -m eval.grader.validate [--n 120 --vlm-sample 150]   # accuracy report (§5)
```

**Exit codes:** 0 if everything is PASS or REVIEW; 1 if any FAIL; 2 on a grader error.

### 7.2 One command after the engine tests (`scripts/check.sh`)

```bash
set -e
.venv/bin/python -m pytest -q tests                       # engine contract (H9)
.venv/bin/python -m engine.main                           # writes output/*_output.json + .png
.venv/bin/python -m eval.grader --batch output --tasks input --report output/grader_report "$@"
```

### 7.3 Runtimes, offline status and caching

| Tier | Offline / deterministic? | Where it runs | Time per pair (n ≈ 17, M2) |
|---|---|---|---|
| T0 JSON (H1–H4, H5a, H6a, H7a) | yes / yes | `.venv` | < 20 ms |
| T1 forensics + pixel checks (H5b–H7c, C1–C6) | yes / yes | `.venv` (skia, numpy) + SSIM in the worker | 3–6 s (source side cached) |
| T2 OCR | yes / yes (EasyOCR CPU, fixed weights) | ImageSorcery venv, `eval/ocr/worker.py` (reused and extended to take crops from masks) | 1–3 s |
| T3 local VLM | yes / near-deterministic (temperature 0, cached) | Ollama | 3–6 min, off in batch |
| T4 hosted judge | no / no | `eval/judge.py` | ~$0.01–0.05; when credits allow |

- **Batch default** is T0–T2. The 3 samples take about 30 s; 675 corpus pairs at `-j 8` take about 10 min.
- **Cache** lives in `eval/.cache/grader/`:
  - source forensics by `sha256(source json + W×H + grader_version)`;
  - per-element masks by element hash;
  - OCR by crop hash;
  - VLM answers by `sha256(images + prompt + model)`, the same scheme as `judge.py`.

  Bumping `GRADER_VERSION` invalidates the cache.
- **Keys** are read via `judge._load_env()`. They are never printed or logged.

---

## 8. Build order and files (small files; each step is TDD'd against its corruption first)

All the new code goes in `eval/grader/`. Each file is under 250 lines. Nothing in `engine/` changes.

| # | File | Contents | Reuses | Done when |
|---|---|---|---|---|
| 1 | `eval/grader/hard.py` | H1–H4, H5a, H6a, H7a (asset manifest) | `eval.metrics.structural`, `jsonschema`, the schema file | Its corruptions: P = R = 1 |
| 2 | `eval/grader/corrupt.py` | One pure function per defect `(src, out, severity) -> (out', png_fn or None, label)`, immutable copies | — | Built **with** step 1; grows each phase |
| 3 | `eval/grader/__main__.py` + `verdict.py` | CLI, batch, exit codes, grade.json | — | `scripts/check.sh` runs end to end |
| 4 | `eval/grader/forensics.py` | Keyed, knockout, padded and notext renders; masks; caching | `renderer.converter.convert`, `eval.layout` | Masks equal a hand-made fixture on `sample_l1` |
| 5 | `eval/grader/pixel.py` | H5b, H6b, H7b, H7c; C1 ink collisions and occlusion; C2d/e overflow and clip | `metrics._glyph_contrasts`, `assets._fit_image` | Pixel corruptions meet the §5.3 targets |
| 6 | `eval/grader/ocr.py` | Batch crops from masks → worker → scores | `eval/ocr/worker.py` (add mask-bbox jobs) | C2 targets met |
| 7 | `eval/grader/soft.py` | C3–C6: presence, cover-crop, Lab EMD, ΔE under text, hierarchy, containment graph, gaps, dead zones | `metrics.hierarchy/alignment/utilization` | C3–C5 targets met; C6 correlation reported |
| 8 | `eval/grader/report.py` | HTML report and evidence crops | `blind_ab.py` styling, `b64` | Visual check by the user |
| 9 | `eval/grader/validate.py` | Builds the set, runs the grader, P/R/CI tables → `eval/runs/grader_val/summary.json` + HTML | `eval/iterate.py` Pool pattern | Prints the §5.3 table |
| 10 | `eval/grader/vlm.py` | Ollama client, set-of-mark overlay, competence probe, question bank, swap and ensemble, cache; hosted backend via `judge.py` | `judge._png_bytes`, the cache layout, `OLLAMA_URL` | Per-question precision measured; weak questions auto-disabled |
| 11 | `eval/grader/label_page.py` | Human labelling page (150 pairs) and label import | `blind_ab.py` | The user labels; κ is reported |
| 12 | `tests/test_grader.py` | 5 assertions: identity → PASS; 1.5× stretch → H6 FAIL; off-canvas → H5 FAIL; PNG blur → H7c FAIL; detached shadow → C5 flag | — | Runs inside `pytest tests` in < 20 s (no OCR or VLM) |
| 13 | `scripts/check.sh` | The single command | — | — |

**Rough effort:**

| Steps | Time |
|---|---|
| 1–3 | half a day |
| 4–5 | 1 day |
| 6–7 | 1 day |
| 8–9 | half a day |
| 10 | 1 day |
| 11 + labelling | user time |

---

## Sources

- [Can GPTs Evaluate Graphic Design Based on Design Principles? (arXiv 2410.08885)](https://arxiv.org/html/2410.08885v1)
- [DesignSense-10k: layout-quality preference judge (arXiv 2602.23438)](https://arxiv.org/abs/2602.23438)
- [AesEval-Bench, VLMs judging graphic-design aesthetics (arXiv 2603.01083)](https://arxiv.org/html/2603.01083)
- [DesignProbe: graphic design benchmark for MLLMs (review)](https://www.themoonlight.io/en/review/designprobe-a-graphic-design-benchmark-for-multimodal-large-language-models)
- [AesthetiQ, CVPR 2025 supplemental](https://openaccess.thecvf.com/content/CVPR2025/supplemental/Patnaik_AesthetiQ_Enhancing_Graphic_CVPR_2025_supplemental.pdf)
- [Fooling the LVLM Judges: Visual Biases in LVLM-Based Evaluation](https://arxiv.org/html/2505.15249v2)
- [GenArena: bidirectional (swap) pairwise VLM judging](https://arxiv.org/pdf/2602.06013)
- [DreamSim, NeurIPS 2023](https://proceedings.neurips.cc/paper_files/paper/2023/hash/9f09f316a3eaf59d9ced5ffaefe97e0f-Abstract.html)
- [UMSI: Predicting Visual Importance Across Graphic Design Types](https://arxiv.org/pdf/2008.02912)
- [Ollama structured outputs](https://registry.ollama.ai/blog/structured-outputs)
