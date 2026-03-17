#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/tests/contract/helpers/runtime-root.sh"
TEST_TMP="$(contract_make_tmpdir "$ROOT" umbrellactl-smoke)"
RUNTIME_ROOT="$(contract_make_runtime_root "$ROOT" umbrellactl-smoke-runtime)"
export UMBRELLA_RUNTIME_ROOT="$RUNTIME_ROOT"
MGR="$ROOT/scripts/control-plane/manage-service-mesh"
CTL="$ROOT/scripts/umbrellactl"
MANIFEST="$TEST_TMP/umbrellactl-smoke.manifest.json"
PLAN="$TEST_TMP/umbrellactl-approval.plan.json"
RUN_ID="run-umbrellactl-smoke-$(date +%s)-$$"
APPROVAL_KEY="umbrellactl-approval-key-$(date +%s)-$$"

rm -f "$MANIFEST" "$PLAN"

cleanup() {
  "$MGR" shutdown --umbrella-root "$ROOT" --manifest "$MANIFEST" >/dev/null 2>&1 || true
  rm -rf "$RUNTIME_ROOT" "$TEST_TMP"
}
trap cleanup EXIT

"$MGR" bringup --umbrella-root "$ROOT" --manifest "$MANIFEST" >"$TEST_TMP/umbrella04-ctl-bringup.out"

# memory put/get via CLI
"$CTL" --umbrella-root "$ROOT" --manifest "$MANIFEST" memory put --namespace team --key ctl-smoke --value '{"v":"ok"}' >"$TEST_TMP/umbrella04-ctl-mem-put.out"
"$CTL" --umbrella-root "$ROOT" --manifest "$MANIFEST" memory get --namespace team --key ctl-smoke >"$TEST_TMP/umbrella04-ctl-mem-get.out"
python3 - "$TEST_TMP" <<'PY'
import json
import sys
from pathlib import Path
tmp=Path(sys.argv[1])
obj=json.loads((tmp/'umbrella04-ctl-mem-get.out').read_text())
assert obj.get('ok') is True and obj.get('exists') is True, obj
assert (obj.get('memory') or {}).get('value') == {'v':'ok'}, obj
print('umbrellactl memory get/put PASS')
PY

cat > "$PLAN" <<JSON
{
  "id": "umbrella.plan.umbrellactl.approval.v1",
  "steps": [
    {
      "stepId": "approval-step",
      "objective": "approval flow via umbrellactl",
      "command": "python3 -c 'print(\"umbrellactl-approval-ok\")'",
      "workdir": ".",
      "timeoutSec": 30,
      "timeoutClass": "short",
      "riskClass": "low",
      "requiresApproval": true,
      "approvalKey": "$APPROVAL_KEY"
    }
  ]
}
JSON

set +e
"$CTL" --umbrella-root "$ROOT" --manifest "$MANIFEST" run --plan "$(python3 - "$PLAN" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).resolve().as_posix())
PY
)" --run-id "$RUN_ID" >"$TEST_TMP/umbrella04-ctl-run1.out"
RC1=$?
set -e
if [[ "$RC1" -ne 3 ]]; then
  echo "expected umbrellactl run to block with exit 3, got $RC1"
  exit 1
fi

"$CTL" --umbrella-root "$ROOT" --manifest "$MANIFEST" run-status --approval-key "$APPROVAL_KEY" >"$TEST_TMP/umbrella04-ctl-status1.out"
python3 - "$TEST_TMP" <<'PY'
import json
import sys
from pathlib import Path
tmp=Path(sys.argv[1])
obj=json.loads((tmp/'umbrella04-ctl-status1.out').read_text())
state=((obj.get('status') or {}).get('state'))
assert state == 'BLOCKED', obj
print('umbrellactl run-status BLOCKED PASS')
PY

"$CTL" --umbrella-root "$ROOT" --manifest "$MANIFEST" approve --approval-key "$APPROVAL_KEY" --by qa --note 'approved via umbrellactl' >"$TEST_TMP/umbrella04-ctl-approve.out"

"$CTL" --umbrella-root "$ROOT" --manifest "$MANIFEST" resume --plan "$(python3 - "$PLAN" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).resolve().as_posix())
PY
)" --run-id "$RUN_ID" --approval-key "$APPROVAL_KEY" >"$TEST_TMP/umbrella04-ctl-resume.out"

"$CTL" --umbrella-root "$ROOT" --manifest "$MANIFEST" run-status --approval-key "$APPROVAL_KEY" >"$TEST_TMP/umbrella04-ctl-status2.out"
python3 - "$TEST_TMP" <<'PY'
import json
import sys
from pathlib import Path
tmp=Path(sys.argv[1])
obj=json.loads((tmp/'umbrella04-ctl-status2.out').read_text())
state=((obj.get('status') or {}).get('state'))
assert state == 'SUCCEEDED', obj
print('umbrellactl approval flow PASS')
PY

echo "umbrella0.4 umbrellactl smoke contract PASS"
