# Pipeline v2: relations, insight, layout, score, propose

Six steps. Each one writes its own section to the HTML trace (`python -m engine.trace`). Steps marked
**LLM** are optional plug-ins. Each has a deterministic fallback, so the scored container works with
them switched off.

```
1 Analyze      roles · units · reading order · relation graph (pairwise + page)        deterministic
2 Insight      model reviews the analysis + render, returns structured corrections      LLM (optional)
3 Layout       templates + LLM-planned layouts (if H7 wins) → snap relations exactly    deterministic (+LLM plans)
4 Score        rubric energy incl. relation fidelity                                    deterministic
5 Propose      critic finds defects → typed edit menu → re-score → keep only if better  deterministic critic (+LLM critic)
6 Finalize     contract-safe output                                                     deterministic
```

## 1. Analyze: add an explicit relation graph

The graph covers every unit and every decoration, and it is recorded from the source.

- **Pairwise relations:**
  - **Concentric:** a product on its halo, or a label on its disc.
  - **Centre-x aligned or centre-y aligned:** a column of centred items, or a shadow under a product.
  - **Edge-aligned:** shared left, right, top or bottom edges.
  - **Near:** a bubble or dot next to a product, kept at the same relative offset scaled by the host.
  - **Below:** a drop shadow.
- **Page relations:**
  - **Centred on the page:** horizontally or vertically.
  - **Anchored to a page edge or corner:** with its margin.
  - **Spans the page:** for example a band, or a panel that fills the width or height.
  - **Fills the page:** the whole content block filled the canvas. Fill is reproduced *only* when the source filled the page; a compact centred block stays compact.

## 2. Insight (LLM, optional)

- **Input:**
  - the analysis as text: units, roles, relations
  - the source render
  - the target size
- **Output:** strict JSON corrections, validated before use:
  - wrong roles
  - relations that are intentional and must hold, or accidental and may be dropped
  - what must fill the page
  - priority order of what to shrink first
  - pairs that must stay together
- **Safety and fallbacks:**
  - Results are cached by design hash.
  - On any failure or timeout, the step falls back to the deterministic analysis.
- **Backends:** local Qwen3-4B (container-feasible but slow) or a hosted model (dev time and finale).

## 3. Layout and snapping

- **Candidate sources:** the templates as today, plus the H7 LLM-planned layouts as extra candidates, if the H7
  experiment shows they win.
- **New snap pass on every candidate:**
  - It enforces each source relation exactly where it can do so without new collisions.
  - **Concentric pairs:** these become exactly concentric.
  - **Shadows:** a shadow's centre-x snaps to its product.
  - **Near decorations:** these keep their scaled offset from the host.
  - **Page-centred items:** these snap to the page centre.
  - **Spanning bands and panels:** these span the target.
  - **Edge-anchored items:** these keep their edge margin.

## 4. Score

The energy includes relation fidelity: concentric, focal, proportion, dead zones, edge clearance and line count.
Snapped relations score zero; broken ones cost.

## 5. Propose: touch-up after scoring

1. **Critique:** a deterministic critic lists concrete defects in the best layout:
   - collisions
   - proximity
   - edge contact
   - dead zones
   - broken relations
   - tiny text
   - proportion drift

   An optional VLM critic adds defects it sees in the render.
2. **Typed edits:** each defect maps to a fixed menu of edits:
   - nudge apart
   - re-centre
   - re-align to axis
   - grow or shrink one unit
   - swap the order of two rows
   - try the runner-up template
3. **Accept:** every edit is applied deterministically and re-scored. It is kept only if the energy improves.
   Several rounds run.
4. **Model choice:** the model, when used, only picks edits from the menu. It never writes coordinates.

## Evaluation

- **Graders:** the strict judge (images plus both JSONs), Gemini and Claude.
- **Method:** every step is A/B'd paired on the same pairs.
- **Shipping rule:** a step ships only if it is not worse held-out and fits the runtime.
