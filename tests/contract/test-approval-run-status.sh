#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUNNER="$ROOT/scripts/control-plane/run-umbrella-control-plane"

free_port() {
  python3 - <<'PY'
import socket
s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()
PY
}

POLICY_PORT="$(free_port)"
LIFECYCLE_PORT="$(free_port)"
ROUTER_PORT="$(free_port)"
SCHED_PORT="$(free_port)"
EXEC_PORT="$(free_port)"
APPROVAL_PORT="$(free_port)"
ORCH_PORT="$(free_port)"

POLICY_URL="http://127.0.0.1:$POLICY_PORT"
LIFECYCLE_URL="http://127.0.0.1:$LIFECYCLE_PORT"
ROUTER_URL="http://127.0.0.1:$ROUTER_PORT"
SCHED_URL="http://127.0.0.1:$SCHED_PORT"
EXEC_URL="http://127.0.0.1:$EXEC_PORT"
APPROVAL_URL="http://127.0.0.1:$APPROVAL_PORT"
ORCH_URL="http://127.0.0.1:$ORCH_PORT"

PLAN="$ROOT/tmp/approval-run-status-smoke.plan.json"
APPROVAL_KEY="umbrella04-approval-run-status-key"
RUN_ID="run-umbrella04-approval-run-status-$(date +%s)"

mkdir -p "$ROOT/tmp"
cat > "$PLAN" <<JSON
{
  "id": "umbrella.plan.approval-run-status-smoke.v1",
  "steps": [
    {
      "stepId": "approval-gated-step",
      "objective": "Validate run-status state transitions",
      "command": "python3 -c 'print(\"approval-run-status-ok\")'",
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

python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-rs-policy.out" 2>"$ROOT/tmp/umbrella04-rs-policy.err" &
P1=$!
python3 "$ROOT/services/lifecycle/app.py" --host 127.0.0.1 --port "$LIFECYCLE_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-rs-lifecycle.out" 2>"$ROOT/tmp/umbrella04-rs-lifecycle.err" &
P2=$!
python3 "$ROOT/services/router/app.py" --host 127.0.0.1 --port "$ROUTER_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-rs-router.out" 2>"$ROOT/tmp/umbrella04-rs-router.err" &
P3=$!
python3 "$ROOT/services/scheduler/app.py" --host 127.0.0.1 --port "$SCHED_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-rs-scheduler.out" 2>"$ROOT/tmp/umbrella04-rs-scheduler.err" &
P4=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-rs-execution.out" 2>"$ROOT/tmp/umbrella04-rs-execution.err" &
P5=$!
python3 "$ROOT/services/approval/app.py" --host 127.0.0.1 --port "$APPROVAL_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-rs-approval.out" 2>"$ROOT/tmp/umbrella04-rs-approval.err" &
P6=$!
python3 "$ROOT/services/orchestrator/app.py" --host 127.0.0.1 --port "$ORCH_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-rs-orchestrator.out" 2>"$ROOT/tmp/umbrella04-rs-orchestrator.err" &
P7=$!

cleanup(){
  kill "$P1" "$P2" "$P3" "$P4" "$P5" "$P6" "$P7" >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_health() {
  local url="$1"
  local attempts=30
  local i=1
  while [[ "$i" -le "$attempts" ]]; do
    if python3 - "$url" <<'PY'
import json, sys, urllib.request
url=sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=1.5) as r:
        data=json.loads(r.read().decode('utf-8'))
    ok=(data.get('status')=='ok')
    raise SystemExit(0 if ok else 1)
except Exception:
    raise SystemExit(1)
PY
    then
      return 0
    fi
    sleep 0.2
    i=$((i+1))
  done
  echo "service health timeout: $url"
  return 1
}

wait_health "$POLICY_URL/v1/policy/health"
wait_health "$LIFECYCLE_URL/v1/lifecycle/health"
wait_health "$ROUTER_URL/v1/router/health"
wait_health "$SCHED_URL/v1/scheduler/health"
wait_health "$EXEC_URL/v1/execution/health"
wait_health "$APPROVAL_URL/v1/approval/health"
wait_health "$ORCH_URL/v1/orchestrator/health"

set +e
"$RUNNER" \
  --umbrella-root "$ROOT" \
  --plan "tmp/$(basename "$PLAN")" \
  --run-id "$RUN_ID" \
  --policy-url "$POLICY_URL" \
  --lifecycle-url "$LIFECYCLE_URL" \
  --router-url "$ROUTER_URL" \
  --scheduler-url "$SCHED_URL" \
  --execution-url "$EXEC_URL" \
  --approval-url "$APPROVAL_URL" \
  --orchestrator-url "$ORCH_URL" \
  >"$ROOT/tmp/umbrella04-rs-run1.out"
RC1=$?
set -e
if [[ "$RC1" -ne 3 ]]; then
  echo "expected first run to block for approval (exit 3), got $RC1"
  exit 1
fi

python3 - "$APPROVAL_URL" "$APPROVAL_KEY" <<'PY'
import json, sys, urllib.request
approval_url, approval_key = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(approval_url + f'/v1/approval/{approval_key}/run-status', timeout=30) as resp:
    out = json.loads(resp.read().decode('utf-8'))
status = (out.get('status') or {}).get('state')
assert status == 'BLOCKED', out
print('approval run-status BLOCKED PASS')
PY

python3 - "$APPROVAL_URL" "$APPROVAL_KEY" <<'PY'
import json, sys, urllib.request
approval_url, approval_key = sys.argv[1], sys.argv[2]
payload={'by':'qa','note':'approve for run-status test'}
req=urllib.request.Request(approval_url+f'/v1/approval/{approval_key}/approve', method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=30) as resp:
    out=json.loads(resp.read().decode('utf-8'))
assert out['ok'] is True and out['approval']['status']=='APPROVED', out
with urllib.request.urlopen(approval_url + f'/v1/approval/{approval_key}/run-status', timeout=30) as resp:
    rs = json.loads(resp.read().decode('utf-8'))
assert (rs.get('status') or {}).get('state') == 'BLOCKED', rs
print('approval run-status post-approve BLOCKED PASS')
PY

python3 - "$APPROVAL_URL" "$POLICY_URL" "$LIFECYCLE_URL" "$ROUTER_URL" "$SCHED_URL" "$EXEC_URL" "$ORCH_URL" "$RUN_ID" "$APPROVAL_KEY" <<'PY'
import json, sys, urllib.request
approval_url, policy_url, lifecycle_url, router_url, sched_url, exec_url, orch_url, run_id, approval_key = sys.argv[1:]
payload = {
  'plan': 'tmp/approval-run-status-smoke.plan.json',
  'runId': run_id,
  'approvalKey': approval_key,
  'policyUrl': policy_url,
  'lifecycleUrl': lifecycle_url,
  'routerUrl': router_url,
  'schedulerUrl': sched_url,
  'executionUrl': exec_url,
  'approvalUrl': approval_url,
  'orchestratorUrl': orch_url,
}
req=urllib.request.Request(approval_url+'/v1/approval/resume', method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=60) as resp:
    out=json.loads(resp.read().decode('utf-8'))
assert out['ok'] is True and out['exitCode']==0, out
with urllib.request.urlopen(approval_url + f'/v1/approval/{approval_key}/run-status', timeout=30) as resp:
    rs = json.loads(resp.read().decode('utf-8'))
assert (rs.get('status') or {}).get('state') == 'SUCCEEDED', rs
print('approval run-status SUCCEEDED PASS')
PY

echo "umbrella0.4 approval run-status contract PASS"
