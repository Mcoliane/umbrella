#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

mkdir -p "$ROOT/tmp"

python3 - "$ROOT" <<'PY'
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])

def expect_invalid(cmd: list[str], expected: str):
    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
    assert proc.returncode != 0, proc.returncode
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert expected in combined, combined

expect_invalid(
    [
        "python3",
        str(root / "scripts" / "adapters" / "removed-runtime-adapter"),
        "--umbrella-root",
        str(root),
        "result",
        "--run-id",
        "../escape",
        "--step-id",
        "step-1",
    ],
    "runId contains invalid path characters",
)

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
