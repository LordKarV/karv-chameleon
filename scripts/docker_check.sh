#!/usr/bin/env bash
# The submission path, end to end: build the amd64 image, run the three official tasks offline inside it,
# then grade the container's own outputs with the brief-compliance grader (hard rules must all pass).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
OUT=${1:-output_docker}
docker build --platform linux/amd64 -t chameleon-engine . >/dev/null
rm -rf "$OUT" && mkdir -p "$OUT"
docker run --rm --platform linux/amd64 --network none \
  -v "$(pwd)/input:/app/input:ro" -v "$(pwd)/$OUT:/app/output" chameleon-engine 2>&1 | grep -v Fontconfig
"$PY" -m eval.grader --batch "$OUT" --tasks input --report "$OUT/grader_report" "$@"
