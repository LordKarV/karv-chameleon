#!/usr/bin/env bash
# Engine tests, then the engine on input/, then the brief-compliance grader (docs/GRADER_PLAN.md).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
"$PY" -m pytest -q tests
"$PY" -m engine.main
"$PY" -m eval.grader --batch output --tasks input --report output/grader_report "$@"
