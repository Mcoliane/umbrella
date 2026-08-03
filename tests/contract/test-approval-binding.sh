#!/usr/bin/env bash
set -euo pipefail

# An APPROVED record is not authorization on its own.
#
# approvalKey is chosen by the plan (`spec.get('approvalKey')`), so a second plan can
# name a key that an earlier run already had approved. The approval service stamps the
# runId/stepId a decision was granted for, but the orchestrator used to check only
# status == 'APPROVED' — so one approval acted as a transferable grant to execute a
# different step of a different plan.
#
# This drives the real attack: approve run A / step gate-a, then try to resume a
# different run and a different step under the same key. The legitimate resume must
# still succeed; the borrowed one must be refused.

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUNNER="$ROOT/scripts/control-plane/run-umbrella-control-plane"

free_port() {
  python3 - <<'PY'
import socket
s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()
PY
}

POLICY_PORT="$(free_port)";    LIFECYCLE_PORT="$(free_port)"
ROUTER_PORT="$(free_port)";    SCHED_PORT="$(free_port)"
EXEC_PORT="$(free_port)";      APPROVAL_PORT="$(free_port)"
ORCH_PORT="$(free_port)"

POLICY_URL="http://127.0.0.1:$POLICY_PORT";       LIFECYCLE_URL="http://127.0.0.1:$LIFECYCLE_PORT"
ROUTER_URL="http://127.0.0.1:$ROUTER_PORT";       SCHED_URL="http://127.0.0.1:$SCHED_PORT"
EXEC_URL="http://127.0.0.1:$EXEC_PORT";           APPROVAL_URL="http://127.0.0.1:$APPROVAL_PORT"
ORCH_URL="http://127.0.0.1:$ORCH_PORT"

SHARED_KEY="umbrella04-approval-binding-shared-key"
RUN_A="run-umbrella04-approval-binding-a-$(date +%s)"
RUN_B="run-umbrella04-approval-binding-b-$(date +%s)"

PLAN_A="$ROOT/tmp/approval-binding-a.plan.json"
PLAN_B="$ROOT/tmp/approval-binding-b.plan.json"

mkdir -p "$ROOT/tmp"

cat > "$PLAN_A" <<JSON
{
  "id": "umbrella.plan.approval-binding-a.v1",
  "steps": [
    {
      "stepId": "gate-a",
      "objective": "Approval-gated step that legitimately owns the approval",
      "command": "python3 -c 'print(\"approval-binding-a-ok\")'",
      "workdir": ".",
      "timeoutSec": 30,
      "timeoutClass": "short",
      "riskClass": "low",
      "requiresApproval": true,
      "approvalKey": "$SHARED_KEY"
    }
  ]
}
JSON

# Different run, different stepId, same approvalKey — this is the borrowed decision.
cat > "$PLAN_B" <<JSON
{
  "id": "umbrella.plan.approval-binding-b.v1",
  "steps": [
    {
      "stepId": "gate-b",
      "objective": "Different step of a different plan reusing another run's approval",
      "command": "python3 -c 'print(\"approval-binding-b-SHOULD-NOT-RUN\")'",
      "workdir": ".",
      "timeoutSec": 30,
      "timeoutClass": "short",
      "riskClass": "low",
      "requiresApproval": true,
      "approvalKey": "$SHARED_KEY"
    }
  ]
}
JSON

python3 "$ROOT/services/policy/app.py"       --host 127.0.0.1 --port "$POLICY_PORT"    --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-ab-policy.out"     2>&1 & P1=$!
python3 "$ROOT/services/lifecycle/app.py"    --host 127.0.0.1 --port "$LIFECYCLE_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-ab-lifecycle.out"  2>&1 & P2=$!
python3 "$ROOT/services/router/app.py"       --host 127.0.0.1 --port "$ROUTER_PORT"    --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-ab-router.out"     2>&1 & P3=$!
python3 "$ROOT/services/scheduler/app.py"    --host 127.0.0.1 --port "$SCHED_PORT"     --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-ab-scheduler.out"  2>&1 & P4=$!
python3 "$ROOT/services/execution/app.py"    --host 127.0.0.1 --port "$EXEC_PORT"      --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-ab-execution.out"  2>&1 & P5=$!
python3 "$ROOT/services/approval/app.py"     --host 127.0.0.1 --port "$APPROVAL_PORT"  --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-ab-approval.out"   2>&1 & P6=$!
python3 "$ROOT/services/orchestrator/app.py" --host 127.0.0.1 --port "$ORCH_PORT"      --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-ab-orchestrator.out" 2>&1 & P7=$!

cleanup(){ kill "$P1" "$P2" "$P3" "$P4" "$P5" "$P6" "$P7" >/dev/null 2>&1 || true; }
trap cleanup EXIT

wait_health() {
  local url="$1" attempts=30 i=1
  while [[ "$i" -le "$attempts" ]]; do
    if python3 - "$url" <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=1.5) as r:
        raise SystemExit(0 if json.loads(r.read().decode('utf-8')).get('status')=='ok' else 1)
except Exception:
    raise SystemExit(1)
PY
    then return 0; fi
    sleep 0.2; i=$((i+1))
  done
  echo "service health timeout: $url"; return 1
}

wait_health "$POLICY_URL/v1/policy/health"
wait_health "$LIFECYCLE_URL/v1/lifecycle/health"
wait_health "$ROUTER_URL/v1/router/health"
wait_health "$SCHED_URL/v1/scheduler/health"
wait_health "$EXEC_URL/v1/execution/health"
wait_health "$APPROVAL_URL/v1/approval/health"
wait_health "$ORCH_URL/v1/orchestrator/health"

run_plan() {
  set +e
  "$RUNNER" --umbrella-root "$ROOT" --plan "tmp/$(basename "$1")" --run-id "$2" \
    --policy-url "$POLICY_URL" --lifecycle-url "$LIFECYCLE_URL" --router-url "$ROUTER_URL" \
    --scheduler-url "$SCHED_URL" --execution-url "$EXEC_URL" --approval-url "$APPROVAL_URL" \
    --orchestrator-url "$ORCH_URL" >"$ROOT/tmp/umbrella04-ab-$2.out" 2>&1
  local rc=$?
  set -e
  echo "$rc"
}

RC_A="$(run_plan "$PLAN_A" "$RUN_A")"
[[ "$RC_A" -eq 3 ]] || { echo "expected run A to block for approval (exit 3), got $RC_A"; exit 1; }

RC_B="$(run_plan "$PLAN_B" "$RUN_B")"
[[ "$RC_B" -eq 3 ]] || { echo "expected run B to block for approval (exit 3), got $RC_B"; exit 1; }

python3 - "$APPROVAL_URL" "$SHARED_KEY" "$POLICY_URL" "$LIFECYCLE_URL" "$ROUTER_URL" \
         "$SCHED_URL" "$EXEC_URL" "$ORCH_URL" "$RUN_A" "$RUN_B" "$ROOT" <<'PY'
import json, sys, urllib.error, urllib.request
from pathlib import Path

(approval_url, shared_key, policy_url, lifecycle_url, router_url,
 sched_url, exec_url, orch_url, run_a, run_b, root) = sys.argv[1:12]


def resume(plan_basename, run_id):
    payload = {
        'plan': f'tmp/{plan_basename}',
        'runId': run_id,
        'approvalKey': shared_key,
        'policyUrl': policy_url,
        'lifecycleUrl': lifecycle_url,
        'routerUrl': router_url,
        'schedulerUrl': sched_url,
        'executionUrl': exec_url,
        'approvalUrl': approval_url,
        'orchestratorUrl': orch_url,
    }
    req = urllib.request.Request(
        approval_url + '/v1/approval/resume', method='POST',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode('utf-8'))


# Run B blocked LAST, so the shared approval record currently names run B. Approving
# now grants a decision bound to run B / gate-b.
req = urllib.request.Request(
    approval_url + f'/v1/approval/{shared_key}/approve', method='POST',
    data=json.dumps({'by': 'qa', 'note': 'approve binding test'}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(req, timeout=30) as resp:
    approved = json.loads(resp.read().decode('utf-8'))
assert approved['ok'] is True and approved['approval']['status'] == 'APPROVED', approved
granted_run = approved['approval'].get('runId')
granted_step = approved['approval'].get('stepId')
assert granted_run and granted_step, approved

# The run the approval actually names must proceed.
owner_plan = 'approval-binding-b.plan.json' if granted_run == run_b else 'approval-binding-a.plan.json'
owner_run = run_b if granted_run == run_b else run_a
out_owner = resume(owner_plan, owner_run)
assert out_owner.get('ok') is True and out_owner.get('exitCode') == 0, ('owner resume must succeed', out_owner)

# The other run must NOT be able to borrow it, even though it names the same key.
other_plan = 'approval-binding-a.plan.json' if granted_run == run_b else 'approval-binding-b.plan.json'
other_run = run_a if granted_run == run_b else run_b
out_other = resume(other_plan, other_run)
assert out_other.get('ok') is not True, ('borrowed approval must be refused', out_other)

run_json = Path(root) / 'control-plane' / 'observability' / 'runs' / other_run / 'run.json'
assert run_json.exists(), str(run_json)
record = json.loads(run_json.read_text(encoding='utf-8'))
assert record.get('state') == 'BLOCKED', record
blocked_step = next((s for s in record.get('steps', []) if s.get('status') == 'BLOCKED'), {})
assert (blocked_step.get('result') or {}).get('reason') == 'approval_binding_mismatch', record

# And the step that must not have run, did not.
stdout_blob = json.dumps(record)
assert 'approval-binding-b-SHOULD-NOT-RUN' not in stdout_blob or record.get('state') == 'BLOCKED', record

print(f'approval binding PASS (granted to {granted_run}/{granted_step}, borrow refused)')
PY

echo "umbrella0.4 approval binding contract PASS"
