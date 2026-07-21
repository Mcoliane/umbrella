#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUNNER="$ROOT/scripts/control-plane/run-umbrella-control-plane"
PLAN="$ROOT/tmp/failure-reporting.plan.json"
RUN_ID="run-failure-reporting-$(date +%s)"

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
MEM_CORE_PORT="$(free_port)"
EXEC_PORT="$(free_port)"
APPROVAL_PORT="$(free_port)"
ORCH_PORT="$(free_port)"

POLICY_URL="http://127.0.0.1:$POLICY_PORT"
LIFECYCLE_URL="http://127.0.0.1:$LIFECYCLE_PORT"
ROUTER_URL="http://127.0.0.1:$ROUTER_PORT"
SCHED_URL="http://127.0.0.1:$SCHED_PORT"
MEM_CORE_URL="http://127.0.0.1:$MEM_CORE_PORT"
EXEC_URL="http://127.0.0.1:$EXEC_PORT"
APPROVAL_URL="http://127.0.0.1:$APPROVAL_PORT"
ORCH_URL="http://127.0.0.1:$ORCH_PORT"

mkdir -p "$ROOT/tmp"
cat >"$PLAN" <<'JSON'
{
  "id": "umbrella.plan.failure-reporting.v1",
  "steps": [
    {
      "stepId": "invalid-step",
      "action": "not.real.action"
    }
  ]
}
JSON

python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-fr-policy.out" 2>"$ROOT/tmp/umbrella04-fr-policy.err" &
P1=$!
python3 "$ROOT/services/lifecycle/app.py" --host 127.0.0.1 --port "$LIFECYCLE_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-fr-lifecycle.out" 2>"$ROOT/tmp/umbrella04-fr-lifecycle.err" &
P2=$!
python3 "$ROOT/services/router/app.py" --host 127.0.0.1 --port "$ROUTER_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-fr-router.out" 2>"$ROOT/tmp/umbrella04-fr-router.err" &
P3=$!
python3 "$ROOT/services/scheduler/app.py" --host 127.0.0.1 --port "$SCHED_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-fr-scheduler.out" 2>"$ROOT/tmp/umbrella04-fr-scheduler.err" &
P4=$!
python3 "$ROOT/services/memory-core/app.py" --host 127.0.0.1 --port "$MEM_CORE_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-fr-memorycore.out" 2>"$ROOT/tmp/umbrella04-fr-memorycore.err" &
P5=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" --memory-core-url "$MEM_CORE_URL" --policy-url "$POLICY_URL" >"$ROOT/tmp/umbrella04-fr-execution.out" 2>"$ROOT/tmp/umbrella04-fr-execution.err" &
P6=$!
python3 "$ROOT/services/approval/app.py" --host 127.0.0.1 --port "$APPROVAL_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-fr-approval.out" 2>"$ROOT/tmp/umbrella04-fr-approval.err" &
P7=$!
python3 "$ROOT/services/orchestrator/app.py" --host 127.0.0.1 --port "$ORCH_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-fr-orchestrator.out" 2>"$ROOT/tmp/umbrella04-fr-orchestrator.err" &
P8=$!

cleanup() {
  kill "$P1" "$P2" "$P3" "$P4" "$P5" "$P6" "$P7" "$P8" >/dev/null 2>&1 || true
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
    raise SystemExit(0 if data.get('status') == 'ok' else 1)
except Exception:
    raise SystemExit(1)
PY
    then
      return 0
    fi
    sleep 0.2
    i=$((i+1))
  done
  return 1
}

wait_health "$POLICY_URL/v1/policy/health"
wait_health "$LIFECYCLE_URL/v1/lifecycle/health"
wait_health "$ROUTER_URL/v1/router/health"
wait_health "$SCHED_URL/v1/scheduler/health"
wait_health "$MEM_CORE_URL/v1/memory-core/health"
wait_health "$EXEC_URL/v1/execution/health"
wait_health "$APPROVAL_URL/v1/approval/health"
wait_health "$ORCH_URL/v1/orchestrator/health"

set +e
"$RUNNER" \
  --umbrella-root "$ROOT" \
  --plan "$PLAN" \
  --run-id "$RUN_ID" \
  --policy-url "$POLICY_URL" \
  --lifecycle-url "$LIFECYCLE_URL" \
  --router-url "$ROUTER_URL" \
  --scheduler-url "$SCHED_URL" \
  --execution-url "$EXEC_URL" \
  --approval-url "$APPROVAL_URL" \
  --orchestrator-url "$ORCH_URL" \
  >"$ROOT/tmp/umbrella04-fr-runner.out"
RC=$?
set -e

if [[ "$RC" -eq 0 ]]; then
  echo "expected failure-reporting runner to fail"
  exit 1
fi

python3 - "$EXEC_URL" "$ROOT" "$RUN_ID" <<'PY'
import json, sys, urllib.request
from pathlib import Path

exec_url, root, run_id = sys.argv[1:]

req = urllib.request.Request(
    exec_url + '/v1/execution/submit-step-spec',
    method='POST',
    data=json.dumps({'runId': 'exec-failure-check', 'stepId': 'invalid-direct', 'stepSpec': {'stepId': 'invalid-direct', 'action': 'not.real.action'}}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(req, timeout=5) as resp:
    payload = json.loads(resp.read().decode('utf-8'))

assert payload['failureCategory'] == 'validation', payload
assert payload['failureReason'] == 'runtime_capability_unsupported', payload
assert payload['failureSource'] == 'execution', payload
assert payload['result']['kind'] == 'runtimeCapability', payload

run_dir = Path(root) / 'control-plane' / 'observability' / 'runs' / run_id
summary = json.loads((run_dir / 'summary.json').read_text())
run = json.loads((run_dir / 'run.json').read_text())
step = run['steps'][0]

assert summary['state'] == 'FAILED', summary
assert summary['terminalReason'] == 'runtime_capability_unsupported', summary
assert summary['failureCategory'] == 'validation', summary
assert summary['failureSource'] == 'execution', summary
assert summary['failedStepId'] == 'invalid-step', summary
assert step['status'] == 'FAILED', step

print('failure reporting PASS')
PY

echo "umbrella0.4 failure reporting contract PASS"
