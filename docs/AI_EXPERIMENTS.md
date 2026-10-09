# AI in the Chameleon engine: what was tried, what shipped, and why

**Bottom line:** the submitted engine is deterministic. Every attempt to let a model decide geometry, labels or the
final pick at runtime either lost to the rules, landed within grader noise, or cost minutes per design under the
scoring machine's amd64 emulation. AI earned its place at development time: research, test-design authoring, grading,
and turning model insights into rules.

## How results were measured

- **Graders.** Vision models looked at the source and output renders (and, later, both design JSONs). They scored the
  brief's rubric rows and listed issues.
- **Grader panels.**
  - **1–5 scale, three graders:** a local Jev-style JSON grader, a Gemini image grader, and a Claude vision grader.
  - **1–10 scale, two graders:** Gemini 3.8 Flash and Claude Sonnet 5.5. An unchanged source design scores 7 here,
    so 7 is the practical ceiling.
- **Comparisons.**
  - **Paired:** the deterministic pick and the model-assisted pick are graded on the same posters in the same run.
  - **Held out:** results are confirmed on unseen posters.
- **Grader noise.**
  - **Between runs:** about ±0.06 on the mean. Only paired, within-run differences are trusted.
  - **Between graders:** they agree with each other only 28–37% of the time when two renders score within 0.25 of
    each other, and 91–94% of the time when the gap is 0.5 or more.

## Runtime experiments

| Experiment | What the model did | Result | Verdict |
|---|---|---|---|
| LLM semantic pass (Qwen3-1.7B, choice mode) | Answered typed questions per element: role, importance, intentional overlap | Off 3.77 vs on 3.67 vs roles-only 3.69 (1–5); losses came from wrong strategy switches. About 4 s per element; 50–85 s per task in the container | Off by default |
| LLM role labeller (Qwen3-4B / 1.7B, JSON schema) | Labelled each unit's role | 4B 89.6% / 90.7% on key roles vs the then-current rules 85.5% / 85.2%; 1.7B 61%. Half the gain came from descriptive ids. +2.5 GB image, 0.5–3 min per design under emulation | Rejected; its wins were ported into rules, which now score 90.8% / 97.6% |
| Jev geometry (1.7B text model + Qwen3-VL-4B judge) | Picked a grid region and size class per element; a solver turned the letters into pixels | Deterministic 3.85 vs Jev champion 2.35, worse on 29/30. The 1.7B model answered "middle-center" for everything, and was at chance as a pairwise judge. 70–200 s per task | Rejected |
| Clef-Flash geometry (open-weight Jev-compatible decider) | Same region/size decisions, with the source image | Deterministic 3.85 vs Clef champion 2.46, worse on 27/30. Same failure modes: text landing on the headline, call to action shrunk to a speck, logo over the product | Rejected: the decider was not the bottleneck; a discrete region menu without seeing the result cannot compete |
| Clef as top-2 tie-breaker | Chose between the two best deterministic layouts | 3.85 → 3.84; 2 better / 2 worse | Neutral, not shipped |
| Clef steer / search / constrained / rank | Steered strategy knobs; judged code proposals pairwise; drove a collision-free region solver; ranked knob variants | Shipped 3.79 / 3.72 / 3.53 / 3.80 vs 3.77. Rank gained +0.05 on 30 tuning posters, which did not replicate held out (3.91 → 3.90) | Rejected; zero-shot VLM pairwise preference between coherent layouts is about 0.5 |
| Clef repair menu | Numeric defects in, one repair move chosen from a fixed menu | 3.69 vs 3.77; 1 up / 1 down | Rejected |
| Clef relations + retrieved exemplars → solver | Relation-level answers as soft constraints | Shipped 3.64; the scorer approved switches the graders rejected | Rejected |
| Learned selector (pairwise logistic on scorer features) | Learned to pick among candidates | 3.90 vs deterministic 3.91 held out | Rejected; the scorer is already at that frontier |
| Learned judge (frozen CLIP + render features, Bradley-Terry head on synthetic perturbation pairs) | Scored renders locally in about 1 ms | Picked the grader-preferred render on 82% of reliable grader pairs; on its own it recovered the best layout no better than the deterministic pick | Kept only as a cheap extra vote; not a better judge of near-ties |
| Vision-model rerank of the top 3 (Claude as an upper bound) | Picked the best of the engine's three best distinct layouts | 5.65 → 5.70 (1–10, 40 pairs); the best of the three in hindsight is 5.875 | Rejected: even a frontier model adds +0.05; a local 4B VLM would add less at minutes per design |
| Local vision judge (Qwen3-VL-4B via Ollama) | Grade renders | Never returned an answer: it ignored the "don't think" switch and ran past the 8,192-token cap | Unusable on this hardware |
| Archetype fan-out + abstaining judge panel (scorer + learned judge + Claude "designer") | Generated structurally different layouts; switched only when 2 or more panel members agreed by a margin, and nobody vetoed | 3.85 → 3.91 tuning, 3.91 → 3.93 held out: +0.04 over 60 posters, 5 up / 1 down | **The one positive configuration.** It needs a hosted model and about $0.05 per pair, so it is reserved for the Express add-on finale. The scored container stays deterministic |
| LLM layout generation (H7, LayoutPrompter-style): Claude, Gemini, local Qwen3-4B, zero- and few-shot | Planned a box per unit for the target; a deterministic realizer fitted text and pictures exactly into the boxes | Strict judge (1–10), paired with the engine (5.33 on 30 pairs): Gemini few-shot 4.20 (3 wins / 16 losses); Qwen local 2.83 (1 / 25); Claude zero-shot 3.80 and few-shot 3.44 on the 9–10 pairs completed before credits ran out (1 / 7, 0 / 7). As an extra candidate scored by the engine, it was picked on 1 of 9 pairs: a tie | Rejected: the model's compositions read well at a glance, but collide, crowd the call to action, or drop relations the engine keeps |
| Learned layout models (LayoutDM, FlexDM, PosterLlama, LayoutNUWA) | Generate layouts with a trained model | Researched only | No open model retargets an existing design to a new size, and none fits the offline CPU budget |

## Rules that came from model critiques, and how they fared

- **Kept.** Grader issue lists turned into deterministic fixes that improved scores. Examples:
  - pinning shadows and backdrop discs to their product
  - keeping canvas-centred logos centred
  - anchoring corner ribbons
  - stickers that travel with their card
  - grid reflow for card families
  - a backdrop-colour check, so text never loses its panel
  - letter-spacing-aware text fitting
  - priority-ordered readability floors
  - spreading short blocks on tall canvases
  - tiny-canvas hierarchy budgets
- **Rejected.** Bolder rules applied as global defaults lost on paired A/Bs:
  - growing the call to action (−0.47)
  - forcing alignment (−0.34)
  - hero growth (−0.49)
  - decoration clearance (−0.36)
  - the full "bold" rule set (3.36 vs 3.77)

  Each local move created a new crowding or proximity problem the graders caught.

## Why the pattern repeats

1. **Geometry is the hard part, and small models are bad at it.** Coordinates, text fit and non-overlap are exact
   computations. Models allowed in the container produce plausible-looking but colliding or crammed layouts.
2. **Zero-shot model judges can't separate good near-ties.** Pairwise preference is about 0.5. Judges are reliable
   only on obvious differences, which the deterministic scorer already catches.
3. **Gains shrink to noise when re-measured.** Several positive tuning results vanished held out. Only paired,
   held-out wins count.
4. **The runtime budget is hostile.** amd64 emulation on Apple silicon turns a 4B model into 30 s – 3 min per design,
   against about 1 s for the rules.
5. **The scorer is a tie-breaker, not a judge of structure.** It reliably ranks small knob variants. It can't rank
   structurally different layouts, which is why model proposals need a judge, the scorer and a margin all agreeing.

## Where AI stays useful

- **Development time:**
  - research synthesis
  - test-corpus authoring: Claude-authored briefs passing code gates
  - human-like graders that receive the renders plus both design JSONs
  - ground-truth labelling
  - mining grader complaints into rules
- **Finale (Express add-on):**
  - an archetype fan-out with an abstaining judge panel
  - a critique-then-typed-edit touch-up pass, accepted only when the scorer agrees
  - user steering

  In the finale, latency and network access allow a strong hosted model.
