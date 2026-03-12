#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUNNER="$ROOT/scripts/control-plane/run-umbrella-control-plane"
PLAN="control-plane/planner/plans/memory-core-shared-smoke.json"
RUN_ID="run-memory-core-shared-$(date +%s)"

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
MEM_CORE_PORT="$(free_port)"

POLICY_URL="http://127.0.0.1:$POLICY_PORT"
LIFECYCLE_URL="http://127.0.0.1:$LIFECYCLE_PORT"
ROUTER_URL="http://127.0.0.1:$ROUTER_PORT"
SCHED_URL="http://127.0.0.1:$SCHED_PORT"
EXEC_URL="http://127.0.0.1:$EXEC_PORT"
APPROVAL_URL="http://127.0.0.1:$APPROVAL_PORT"
ORCH_URL="http://127.0.0.1:$ORCH_PORT"
MEM_CORE_URL="http://127.0.0.1:$MEM_CORE_PORT"

python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" >/tmp/umbrella04-mc-policy.out 2>/tmp/umbrella04-mc-policy.err &
P1=$!
python3 "$ROOT/services/lifecycle/app.py" --host 127.0.0.1 --port "$LIFECYCLE_PORT" --umbrella-root "$ROOT" >/tmp/umbrella04-mc-lifecycle.out 2>/tmp/umbrella04-mc-lifecycle.err &
P2=$!
python3 "$ROOT/services/router/app.py" --host 127.0.0.1 --port "$ROUTER_PORT" --umbrella-root "$ROOT" >/tmp/umbrella04-mc-router.out 2>/tmp/umbrella04-mc-router.err &
P3=$!
python3 "$ROOT/services/scheduler/app.py" --host 127.0.0.1 --port "$SCHED_PORT" --umbrella-root "$ROOT" >/tmp/umbrella04-mc-scheduler.out 2>/tmp/umbrella04-mc-scheduler.err &
P4=$!
python3 "$ROOT/services/memory-core/app.py" --host 127.0.0.1 --port "$MEM_CORE_PORT" --umbrella-root "$ROOT" >/tmp/umbrella04-mc-memorycore.out 2>/tmp/umbrella04-mc-memorycore.err &
P5=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" --memory-core-url "$MEM_CORE_URL" --policy-url "$POLICY_URL" >/tmp/umbrella04-mc-execution.out 2>/tmp/umbrella04-mc-execution.err &
P6=$!
python3 "$ROOT/services/approval/app.py" --host 127.0.0.1 --port "$APPROVAL_PORT" --umbrella-root "$ROOT" >/tmp/umbrella04-mc-approval.out 2>/tmp/umbrella04-mc-approval.err &
P7=$!
python3 "$ROOT/services/orchestrator/app.py" --host 127.0.0.1 --port "$ORCH_PORT" --umbrella-root "$ROOT" >/tmp/umbrella04-mc-orchestrator.out 2>/tmp/umbrella04-mc-orchestrator.err &
P8=$!

cleanup(){
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
wait_health "$MEM_CORE_URL/v1/memory-core/health"
wait_health "$EXEC_URL/v1/execution/health"
wait_health "$APPROVAL_URL/v1/approval/health"
wait_health "$ORCH_URL/v1/orchestrator/health"

python3 - "$POLICY_URL" <<'PY'
import json, sys, urllib.request
policy_url = sys.argv[1]
payloads = [
    {'agentId': 'agent-a', 'source': 'external', 'capabilities': ['memorycore.write', 'memory.write']},
    {'agentId': 'agent-b', 'source': 'external', 'capabilities': ['memorycore.read', 'memory.read']},
]
for payload in payloads:
    req = urllib.request.Request(policy_url + '/v1/policy/agents/register', method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read().decode('utf-8'))
    assert out.get('ok') is True, out
PY

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
  >/tmp/umbrella04-mc-runner.out

ROOT="$ROOT" RUN_ID="$RUN_ID" python3 - <<'PY'
import json, os
from pathlib import Path
root=Path(os.environ['ROOT'])
run_id=os.environ['RUN_ID']
run_dir=root/'control-plane'/'observability'/'runs'/run_id
summary=json.loads((run_dir/'summary.json').read_text())
run=json.loads((run_dir/'run.json').read_text())
assert summary['state']=='SUCCEEDED', summary
assert summary['terminalReason']=='all_steps_succeeded', summary
steps={s['stepId']:s for s in run.get('steps',[])}
write_step=steps['agent-a-memory-write']
read_step=steps['agent-b-memory-read']
assert write_step['status']=='SUCCESS', write_step
assert read_step['status']=='SUCCESS', read_step
read_memory=((read_step.get('result') or {}).get('result') or {}).get('memory') or {}
value=read_memory.get('value')
assert value == {'text':'umbrella-shared-memory-ok','from':'agent-a'}, value
print('umbrella0.4 memory-core shared e2e PASS')
PY

echo "umbrella0.4 memory-core shared e2e contract PASS"
