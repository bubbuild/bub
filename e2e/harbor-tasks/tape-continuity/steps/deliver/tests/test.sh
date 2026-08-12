#!/bin/sh
set -eu

python - <<'PY'
import json
from pathlib import Path

plan = json.loads(Path("/workspace/plan.json").read_text())
if plan.get("status") != "delivered":
    raise SystemExit(f"plan was not delivered: {plan!r}")

expected = """# Bub release checklist

- [ ] format
- [ ] types
- [ ] behavior
"""
actual = Path("/workspace/release-checklist.md").read_text()
if actual.rstrip("\n") != expected.rstrip("\n"):
    raise SystemExit(f"unexpected deliverable: {actual!r}")
PY

echo 1 > /logs/verifier/reward.txt
