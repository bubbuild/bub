#!/bin/sh
set -eu

python - <<'PY'
import json
from pathlib import Path

plan = json.loads(Path("/workspace/plan.json").read_text())
expected = {
    "project": "Bub",
    "deliverable": "release-checklist.md",
    "checks": ["format", "types", "behavior"],
    "status": "prepared",
}
if plan != expected:
    raise SystemExit(f"unexpected plan: {plan!r}")
if Path("/workspace/release-checklist.md").exists():
    raise SystemExit("final deliverable was created before the delivery step")
PY

echo 1 > /logs/verifier/reward.txt
