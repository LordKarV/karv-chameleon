# Chameleon engine

Resizes a layered design to a new canvas by **re-laying it out**: same elements, nothing stretched,
nothing colliding, text readable, brand intact. It is deterministic and fully offline, with no model at
run time.

## Run

```bash
docker build --platform linux/amd64 -t chameleon-engine .
docker run --rm --network none \
  -v "$(pwd)/input:/app/input" -v "$(pwd)/output:/app/output" chameleon-engine
```

Every `input/*.json` task becomes `output/<name>_output.json` plus its official render `<name>_output.png`.
Without Docker: `pip install -r requirements.txt && python -m engine.main`.

## Approach

```
analyze  ->  candidate layouts  ->  energy (pick lowest)  ->  propose (critic + edits)  ->  finalize
```

1. **Analyze** (`engine/analyze.py`). Ids may be opaque, so structure is inferred from geometry, type,
   z-order and style:
   - background and decoration;
   - units that move together: groups, panels and buttons, stickers pinned to a product, corner ribbons;
   - roles: headline, hero, logo, CTA, badges, legal;
   - reading order (XY-cut).
2. **Generate** (`engine/layout.py`). About 40–120 candidates from several topologies:
   - proportional placement, stack (one column), grid reflow of look-alike cards, split (picture beside text), and banner rows for strips such as 728×90 and 320×50;
   - each candidate is solved at one shared type scale, measured with the renderer's own font code;
   - per-role readability floors give way in priority order (legal first, headline last).
3. **Score** (`engine/energy.py`). An energy that mirrors the rubric picks the best candidate. It counts:
   - new collisions;
   - text below a legible size;
   - lost or vanished elements;
   - broken hierarchy and source relations;
   - dead space, balance and edges.
4. **Touch up** (`engine/propose.py`). A critic finds remaining defects and tries small typed edits, keeping only the ones that lower the energy.
5. **Finalize** (`engine/resize.py`). Every source element is copied and only geometry changes (`x`, `y`, `width`, `height`, `font_size`):
   - ids and content stay identical; fonts, letter spacing, corner radii, colours and assets untouched;
   - images keep their aspect, and everything stays inside the canvas;
   - any failure falls back to a uniform scale-to-fit, which satisfies the contract by construction;
   - no element that was 2 px or larger in the source ships under 2 px, no side ships at 0 (the renderer rejects it),
     and rotated elements are kept inside the canvas by their rotated bounds;
   - if the chosen layout is clearly worse than a plain scale-to-fit on role-blind checks (text on text, text below
     8 px), the scale-to-fit ships instead;
   - the search stops after a fixed amount of work, never a clock, so a slower machine gets the same output.

## How it was checked

- **Contract:** `tests/test_contract.py` runs 25 designs × 25 platform formats plus degenerate inputs and
  checks the output against the brief's rules.
- **Grader** (`scripts/check.sh`, `eval/grader/`): a rule-based checker on the official render. Hard rules
  must pass; soft flags cover collisions, readability, presence and hierarchy.
- **Vision judge:** a 120-pair panel judged by Claude, used during development only (`eval/vision_bench.py`).

Missing assets have no pixels to show, so they stay in the JSON (same id, in canvas, aspect kept) but shrink to an invisible
speck. The very small strips with long copy (320×50) are the hardest case: some secondary text goes small.

Developer guide, tooling and the full experiment log: `docs/DEVELOPER.md`, `docs/OVERNIGHT.md`.
