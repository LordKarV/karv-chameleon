# Claude as a vision judge (via Claude Code, no API credits)

Driver: `eval/grader/claude_judge.py`. This is a dev-time tool only. It is not part of the offline submission.

## What works

- **Headless `claude -p`** with `--input-format stream-json --output-format stream-json --verbose`.
  - You send one user message on stdin. Its content holds base64 `image` blocks plus text.
  - There is no Read-tool round trip, because tools are disabled with `--tools ""`.
  - `--input-format stream-json` requires `--output-format stream-json`. The CLI rejects other combinations.
  - The CLI docs don't describe image blocks in stream-json input, but it works on v2.1.296 (tested here). The Agent SDK streaming docs do describe them.
- **Structured output:** `--json-schema '<schema>'` puts the parsed object in `structured_output` on the `result` event ([headless docs](https://code.claude.com/docs/en/headless)). The driver falls back to parsing JSON out of `result` if that field is missing.
- **Isolation:**
  - `--system-prompt` replaces the Claude Code prompt with the judge rubric.
  - `--safe-mode` turns off hooks, plugins, MCP and CLAUDE.md, but keeps the OAuth login.
  - `--no-session-persistence` stops sessions being saved.
  - The driver runs in an empty temp cwd.
  - Don't use `--bare`. It forces `ANTHROPIC_API_KEY` auth.
- **Agent SDK (`claude-agent-sdk`): not installed.**
  - It wraps the same CLI binary, and its docs describe API-key auth.
  - The docs also say: "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK" ([agent-sdk overview](https://code.claude.com/docs/en/agent-sdk/python)).
  - The plain CLI is the simpler driver for personal dev use.

## Billing and auth (checked on this machine)

- `claude auth status` reports `authMethod: claude.ai`, `subscriptionType: max`, and no `ANTHROPIC_API_KEY` in the environment.
- The `system` init event reports `apiKeySource: none`.
- Each run emits a `rate_limit_event` with `five_hour` and `seven_day` utilization. Usage counts against the **Max plan's 5-hour and weekly limits**. Those limits are shared with interactive Claude and Claude Code use ([support article](https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan)).
- Overage is disabled for this org, so nothing is charged.
- `total_cost_usd` (about $0.02–0.03 per judgement on Opus) is a list-price estimate only. The docs say it is a client-side estimate ([cost tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking)).
- If `ANTHROPIC_API_KEY` is set, `-p` always uses it ([authentication](https://code.claude.com/docs/en/authentication)). That would hit the exhausted API credits, so the driver strips it (and `ANTHROPIC_AUTH_TOKEN`) from the child environment.
- When the smoke test ran, weekly utilization was already 52%. Budget large sweeps with that in mind. Use `--model sonnet` for bulk runs if quota gets tight.

## Image limits ([vision docs](https://platform.claude.com/docs/en/build-with-claude/vision))

- **Standard tier:** long edge 1568 px, at most 1568 visual tokens.
- **High-res tier (Claude 4.7+, which includes the default `claude-opus-5-5`):** long edge 2576 px, at most 4784 visual tokens.
  - Cost is ⌈w/28⌉·⌈h/28⌉ tokens, one per 28×28 patch.
  - The driver pre-resizes to 2576 px or 4784 patches, so Claude never resamples the image itself.
  - It also upscales renders with a long side under 1000 px, so small text gets more patches.
- **Hard limits:** 8000×8000 px, or 2000 px per side when a request has more than 20 images. 10 MB base64. Up to 100 or 600 images per API request.
- **Image placement:**
  - "Claude works best when images come before text."
  - With several images, label each one ("Image 1:", "Image 2:").
  - "Avoid cropping out key visual context solely to enlarge the text."
- **Two images vs one composite:**
  - Sending ORIGINAL and RESIZED as two labelled images (`judge_pair(orig, resized, …)`) gives each its own resolution budget. That is better for small text.
  - The driver also accepts the existing left/right composites (`original_png=None`).
- **Crop/zoom:** not implemented. Add a third, cropped image only if small text turns out to be a measured failure mode.

## Prompt and protocol (see `SYSTEM` and `SCHEMA` in the driver)

- **Rubric:** inventory → lost/shrunk → overlap → readability → grouping → hierarchy → composition.
  - Each verdict level is defined in designer terms: "would not ship", "touch-up", "ship as is".
  - The prompt says "do not invent problems", to counter over-flagging.
  - Detailed rubrics and reasoning before the score are both advised in [develop-tests](https://platform.claude.com/docs/en/test-and-evaluate/develop-tests).
- **Schema order:** `observations` → `issues[{element_hint, problem, fix, severity}]` → `verdict`. The model writes its reasoning before it commits to a verdict.
- **Determinism:** neither the CLI nor the Agent SDK has a temperature flag.
  - Use `--effort` (the default is medium) and the on-disk cache. The cache key is a sha256 of the images, prompt, target, model and effort.
  - For borderline cases, run k=3 and take the majority. Not built yet.
- **Position bias:**
  - This is a single-candidate judgement, not an A/B comparison, so the main risk is the model confusing which image is which.
  - Fix: use explicit "ORIGINAL (reference)" and "RESIZED (judge this one)" labels, and send two separate images rather than relying on left/right.
  - The docs have no guidance on position bias.
- **Pass the correct target size.** With a wrong `--target`, item 0960afe9 flipped from PASS to REVIEW.

## Throughput (measured)

- A single call with a short prompt and one composite took 4.4 s wall time.
- The full rubric with JSON output took 12–15 s per judgement (Opus, effort medium, about 1000 output tokens).
- 3 judgements in parallel with `-j 3` took 14.5 s in total.
- No headless concurrency limit is documented. The binding constraint is the plan's 5-hour and weekly quota. A concurrency of 4–8 is a reasonable start.
- Watch `meta.quota` in the results.

## Smoke test (3 composites from the labelled set)

| id | human | claude |
|---|---|---|
| 889351cd | FAIL | FAIL (jar covers the headline; 5 issues with concrete moves) |
| 0960afe9 | FAIL | PASS |
| 0d464138 | PASS | PASS |

Three items say nothing about agreement. The next step is to run all 120 against `eval/grader_labels/grader_labels_v2_5.json`.

## Usage

```bash
.venv/bin/python -m eval.grader.claude_judge vj/*.png --target-json targets.json -j 6 -o out.json
.venv/bin/python -m eval.grader.claude_judge --pairs src.png:out.png --target 300x250
```

```python
from eval.grader.claude_judge import judge_pair
r = judge_pair("src.png", "out.png", "300x250 (medium rectangle)")   # or judge_pair(None, "composite.png", ...)
r["verdict"], r["issues"], r["observations"], r["meta"]
```
