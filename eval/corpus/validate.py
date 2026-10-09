"""Validate eval/corpus/designs/*.json: schema, unique ids, wrapper shape.
Run: .venv/bin/python eval/corpus/validate.py   (exit 1 on any failure)"""
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
validator = Draft202012Validator(json.loads((ROOT / "chameleon_design_schema_v0.9.json").read_text()))
errors = []
files = sorted((Path(__file__).parent / "designs").glob("*.json"))
for f in files:
    doc = json.loads(f.read_text())
    if set(doc) != {"design_id", "level", "source"} or doc["level"] not in (1, 2, 3):
        errors.append(f"{f.name}: wrapper must be exactly design_id/level(1-3)/source")
    if doc.get("design_id") != f.stem:
        errors.append(f"{f.name}: design_id != filename")
    src = doc.get("source", {})
    errors += [f"{f.name}: {'/'.join(map(str, e.path))}: {e.message}" for e in validator.iter_errors(src)]
    ids = [e.get("id") for e in src.get("elements", [])]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        errors.append(f"{f.name}: duplicate ids {sorted(dupes)}")
    extra = [e["id"] for e in src.get("elements", []) if any(k.startswith("_") for k in e)]
    if extra:
        errors.append(f"{f.name}: underscore note keys on {extra}")
print("\n".join(errors) if errors else f"OK: {len(files)} designs valid")
sys.exit(1 if errors else 0)
