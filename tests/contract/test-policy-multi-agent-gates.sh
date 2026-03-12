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
MEM_CORE_PORT="$(free_port)"

POLICY_URL="http://127.0.0.1:$POLICY_PORT"
LIFECYCLE_URL="http://127.0.0.1:$LIFECYCLE_PORT"
ROUTER_URL="http://127.0.0.1:$ROUTER_PORT"
SCHED_URL="http://127.0.0.1:$SCHED_PORT"
EXEC_URL="http://127.0.0.1:$EXEC_PORT"
APPROVAL_URL="http://127.0.0.1:$APPROVAL_PORT"
ORCH_URL="http://127.0.0.1:$ORCH_PORT"
MEM_CORE_URL="http://127.0.0.1:$MEM_CORE_PORT"

AGENT_ID="external-agent-$(date +%s)"
PLAN="$ROOT/tmp/policy-multi-agent-gates.plan.json"
RUN_A="run-policy-gates-a-$(date +%s)"
RUN_B="run-policy-gates-b-$(date +%s)"
RUN_C="run-policy-gates-c-$(date +%s)"

mkdir -p "$ROOT/tmp"
cat > "$PLAN" <<JSON
{
  "id": "umbrella.plan.policy-multi-agent-gates.v1",
  "steps": [
    {
      "stepId": "priv-memory-write",
      "objective": "privileged memory write for external agent",
      "action": "memoryWrite",
      "namespace": "team",
      "key": "policy-gates-shared",
      "value": {"ok": true},
      "metadata": {"agentId": "$AGENT_ID"},
      "timeoutSec": 30,
      "timeoutClass": "short",
      "riskClass": "low",
      "requiresApproval": false
    }
  ]
}
JSON

python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-pmg-policy.out" 2>"$ROOT/tmp/umbrella04-pmg-policy.err" &
P1=$!
python3 "$ROOT/services/lifecycle/app.py" --host 127.0.0.1 --port "$LIFECYCLE_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-pmg-lifecycle.out" 2>"$ROOT/tmp/umbrella04-pmg-lifecycle.err" &
P2=$!
python3 "$ROOT/services/router/app.py" --host 127.0.0.1 --port "$ROUTER_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-pmg-router.out" 2>"$ROOT/tmp/umbrella04-pmg-router.err" &
P3=$!
python3 "$ROOT/services/scheduler/app.py" --host 127.0.0.1 --port "$SCHED_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-pmg-scheduler.out" 2>"$ROOT/tmp/umbrella04-pmg-scheduler.err" &
P4=$!
python3 "$ROOT/services/memory-core/app.py" --host 127.0.0.1 --port "$MEM_CORE_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-pmg-memorycore.out" 2>"$ROOT/tmp/umbrella04-pmg-memorycore.err" &
P5=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" --memory-core-url "$MEM_CORE_URL" --policy-url "$POLICY_URL" >"$ROOT/tmp/umbrella04-pmg-execution.out" 2>"$ROOT/tmp/umbrella04-pmg-execution.err" &
P6=$!
python3 "$ROOT/services/approval/app.py" --host 127.0.0.1 --port "$APPROVAL_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-pmg-approval.out" 2>"$ROOT/tmp/umbrella04-pmg-approval.err" &
P7=$!
python3 "$ROOT/services/orchestrator/app.py" --host 127.0.0.1 --port "$ORCH_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-pmg-orchestrator.out" 2>"$ROOT/tmp/umbrella04-pmg-orchestrator.err" &
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

# A) Unregistered external agent -> denied
set +e
"$RUNNER" \
  --umbrella-root "$ROOT" \
  --plan "tmp/$(basename "$PLAN")" \
  --run-id "$RUN_A" \
  --policy-url "$POLICY_URL" \
  --lifecycle-url "$LIFECYCLE_URL" \
  --router-url "$ROUTER_URL" \
  --scheduler-url "$SCHED_URL" \
  --execution-url "$EXEC_URL" \
  --approval-url "$APPROVAL_URL" \
  --orchestrator-url "$ORCH_URL" \
  >"$ROOT/tmp/umbrella04-pmg-run-a.out"
RCA=$?
set -e
if [[ "$RCA" -ne 1 ]]; then
  echo "expected unregistered run to fail with exit 1, got $RCA"
  exit 1
fi

ROOT="$ROOT" RUN_ID="$RUN_A" python3 - <<'PY'
import json, os
from pathlib import Path
root=Path(os.environ['ROOT'])
run_id=os.environ['RUN_ID']
run=json.loads((root/'control-plane'/'observability'/'runs'/run_id/'run.json').read_text())
step=run['steps'][0]
reason=(((step.get('result') or {}).get('result') or {}).get('policyDecision') or {}).get('reason')
assert reason == 'external_agent_registration_required', run
print('policy gate deny (registration) PASS')
PY

# Register agent without capability claim
python3 - "$POLICY_URL" "$AGENT_ID" <<'PY'
import json, sys, urllib.request
policy_url, agent_id = sys.argv[1], sys.argv[2]
payload={'agentId':agent_id, 'source':'external', 'capabilities':[]}
req=urllib.request.Request(policy_url+'/v1/policy/agents/register', method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=30) as resp:
    out=json.loads(resp.read().decode('utf-8'))
assert out.get('ok') is True, out
PY

# B) Registered but missing capability -> denied
set +e
"$RUNNER" \
  --umbrella-root "$ROOT" \
  --plan "tmp/$(basename "$PLAN")" \
  --run-id "$RUN_B" \
  --policy-url "$POLICY_URL" \
  --lifecycle-url "$LIFECYCLE_URL" \
  --router-url "$ROUTER_URL" \
  --scheduler-url "$SCHED_URL" \
  --execution-url "$EXEC_URL" \
  --approval-url "$APPROVAL_URL" \
  --orchestrator-url "$ORCH_URL" \
  >"$ROOT/tmp/umbrella04-pmg-run-b.out"
RCB=$?
set -e
if [[ "$RCB" -ne 1 ]]; then
  echo "expected missing-claim run to fail with exit 1, got $RCB"
  exit 1
fi

ROOT="$ROOT" RUN_ID="$RUN_B" python3 - <<'PY'
import json, os
from pathlib import Path
root=Path(os.environ['ROOT'])
run_id=os.environ['RUN_ID']
run=json.loads((root/'control-plane'/'observability'/'runs'/run_id/'run.json').read_text())
step=run['steps'][0]
reason=(((step.get('result') or {}).get('result') or {}).get('policyDecision') or {}).get('reason')
assert reason == 'tool_capability_claim_missing', run
print('policy gate deny (capability claim) PASS')
PY

# Register capability claim and rerun
python3 - "$POLICY_URL" "$AGENT_ID" <<'PY'
import json, sys, urllib.request
policy_url, agent_id = sys.argv[1], sys.argv[2]
payload={'agentId':agent_id, 'source':'external', 'capabilities':['memory.write']}
req=urllib.request.Request(policy_url+'/v1/policy/agents/register', method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=30) as resp:
    out=json.loads(resp.read().decode('utf-8'))
assert out.get('ok') is True, out
PY

# C) Registered + claim -> allow
"$RUNNER" \
  --umbrella-root "$ROOT" \
  --plan "tmp/$(basename "$PLAN")" \
  --run-id "$RUN_C" \
  --policy-url "$POLICY_URL" \
  --lifecycle-url "$LIFECYCLE_URL" \
  --router-url "$ROUTER_URL" \
  --scheduler-url "$SCHED_URL" \
  --execution-url "$EXEC_URL" \
  --approval-url "$APPROVAL_URL" \
  --orchestrator-url "$ORCH_URL" \
  >"$ROOT/tmp/umbrella04-pmg-run-c.out"

ROOT="$ROOT" RUN_ID="$RUN_C" python3 - <<'PY'
import json, os
from pathlib import Path
root=Path(os.environ['ROOT'])
run_id=os.environ['RUN_ID']
summary=json.loads((root/'control-plane'/'observability'/'runs'/run_id/'summary.json').read_text())
assert summary['state']=='SUCCEEDED', summary
print('policy gate allow PASS')
PY

echo "umbrella0.4 policy multi-agent gates contract PASS"
