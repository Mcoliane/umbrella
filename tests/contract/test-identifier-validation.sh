#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

mkdir -p "$ROOT/tmp"

python3 - "$ROOT" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])

from services.orchestrator.app import OrchestratorEngine
from services.approval.app import ApprovalStore

orch = OrchestratorEngine(root)
try:
    orch.get_summary("../escape")
except ValueError as ex:
    assert "runId contains invalid path characters" in str(ex), ex
else:
    raise AssertionError("expected orchestrator runId validation failure")

approval = ApprovalStore(root)
try:
    approval.list_resume_journal("../escape")
except ValueError as ex:
    assert "runId contains invalid path characters" in str(ex), ex
else:
    raise AssertionError("expected approval runId validation failure")

print("identifier validation PASS")
PY

echo "umbrella0.4 identifier validation contract PASS"
