#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MGR="$ROOT/scripts/control-plane/manage-service-mesh"
CTL="$ROOT/scripts/umbrellactl"
MANIFEST="$ROOT/tmp/umbrellactl-smoke.manifest.json"
PLAN="$ROOT/tmp/umbrellactl-approval.plan.json"
RUN_ID="run-umbrellactl-smoke-$(date +%s)"
APPROVAL_KEY="umbrellactl-approval-key-$(date +%s)"

rm -f "$MANIFEST" "$PLAN"

cleanup() {
  "$MGR" shutdown --umbrella-root "$ROOT" --manifest "$MANIFEST" >/dev/null 2>&1 || true
}
trap cleanup EXIT

"$MGR" bringup --umbrella-root "$ROOT" --manifest "$MANIFEST" >/tmp/umbrella04-ctl-bringup.out

# memory put/get via CLI
"$CTL" --umbrella-root "$ROOT" --manifest "$MANIFEST" memory put --namespace team --key ctl-smoke --value '{"v":"ok"}' >/tmp/umbrella04-ctl-mem-put.out
"$CTL" --umbrella-root "$ROOT" --manifest "$MANIFEST" memory get --namespace team --key ctl-smoke >/tmp/umbrella04-ctl-mem-get.out
python3 - <<'PY'
import json
from pathlib import Path
obj=json.loads(Path('/tmp/umbrella04-ctl-mem-get.out').read_text())
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
"$CTL" --umbrella-root "$ROOT" --manifest "$MANIFEST" run --plan "tmp/$(basename "$PLAN")" --run-id "$RUN_ID" >/tmp/umbrella04-ctl-run1.out
RC1=$?
set -e
if [[ "$RC1" -ne 3 ]]; then
  echo "expected umbrellactl run to block with exit 3, got $RC1"
  exit 1
fi

"$CTL" --umbrella-root "$ROOT" --manifest "$MANIFEST" run-status --approval-key "$APPROVAL_KEY" >/tmp/umbrella04-ctl-status1.out
python3 - <<'PY'
import json
from pathlib import Path
obj=json.loads(Path('/tmp/umbrella04-ctl-status1.out').read_text())
state=((obj.get('status') or {}).get('state'))
assert state == 'BLOCKED', obj
print('umbrellactl run-status BLOCKED PASS')
PY

"$CTL" --umbrella-root "$ROOT" --manifest "$MANIFEST" approve --approval-key "$APPROVAL_KEY" --by qa --note 'approved via umbrellactl' >/tmp/umbrella04-ctl-approve.out

"$CTL" --umbrella-root "$ROOT" --manifest "$MANIFEST" resume --plan "tmp/$(basename "$PLAN")" --run-id "$RUN_ID" --approval-key "$APPROVAL_KEY" >/tmp/umbrella04-ctl-resume.out

"$CTL" --umbrella-root "$ROOT" --manifest "$MANIFEST" run-status --approval-key "$APPROVAL_KEY" >/tmp/umbrella04-ctl-status2.out
python3 - <<'PY'
import json
from pathlib import Path
obj=json.loads(Path('/tmp/umbrella04-ctl-status2.out').read_text())
state=((obj.get('status') or {}).get('state'))
assert state == 'SUCCEEDED', obj
print('umbrellactl approval flow PASS')
PY

echo "umbrella0.4 umbrellactl smoke contract PASS"
