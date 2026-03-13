#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUNNER="$ROOT/scripts/control-plane/run-umbrella-control-plane"
SUCCESS_PLAN="$ROOT/tmp/orchestrator-runtime-summary-success.plan.json"
FAIL_PLAN="$ROOT/tmp/orchestrator-runtime-summary-fail.plan.json"
SUCCESS_RUN_ID="run-orchestrator-runtime-summary-success-$(date +%s)"
FAIL_RUN_ID="run-orchestrator-runtime-summary-fail-$(date +%s)"
AGENT_ID="orchestrator-runtime-summary-agent"

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
CATALOG_PORT="$(free_port)"
PLUGIN_HOST_PORT="$(free_port)"
APPROVAL_PORT="$(free_port)"
ORCH_PORT="$(free_port)"

POLICY_URL="http://127.0.0.1:$POLICY_PORT"
LIFECYCLE_URL="http://127.0.0.1:$LIFECYCLE_PORT"
ROUTER_URL="http://127.0.0.1:$ROUTER_PORT"
SCHED_URL="http://127.0.0.1:$SCHED_PORT"
MEM_CORE_URL="http://127.0.0.1:$MEM_CORE_PORT"
EXEC_URL="http://127.0.0.1:$EXEC_PORT"
CATALOG_URL="http://127.0.0.1:$CATALOG_PORT"
PLUGIN_HOST_URL="http://127.0.0.1:$PLUGIN_HOST_PORT"
APPROVAL_URL="http://127.0.0.1:$APPROVAL_PORT"
ORCH_URL="http://127.0.0.1:$ORCH_PORT"

mkdir -p "$ROOT/tmp"
cat >"$SUCCESS_PLAN" <<'JSON'
{
  "id": "umbrella.plan.orchestrator.runtime-summary.success.v1",
  "steps": [
    {
      "stepId": "native-memory-write",
      "action": "memoryWrite",
      "namespace": "team",
      "key": "runtime-summary-key",
      "value": {"hello": "world"},
      "metadata": {"agentId": "orchestrator-runtime-summary-agent"}
    },
    {
      "stepId": "skill-memory-summarize",
      "action": "skill.memory.summarize",
      "timeoutSec": 10,
      "inputs": {"nodeId": "fact:runtime-summary"},
      "metadata": {"agentId": "orchestrator-runtime-summary-agent"}
    }
  ]
}
JSON

cat >"$FAIL_PLAN" <<'JSON'
{
  "id": "umbrella.plan.orchestrator.runtime-summary.fail.v1",
  "steps": [
    {
      "stepId": "unsupported-bootstrap",
      "action": "bootstrap.prepare",
      "runtime": "umbrella-agent-runtime",
      "metadata": {"allowCapabilityReroute": false}
    }
  ]
}
JSON

python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-ors-policy.out" 2>"$ROOT/tmp/umbrella04-ors-policy.err" &
P1=$!
python3 "$ROOT/services/lifecycle/app.py" --host 127.0.0.1 --port "$LIFECYCLE_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-ors-lifecycle.out" 2>"$ROOT/tmp/umbrella04-ors-lifecycle.err" &
P2=$!
python3 "$ROOT/services/router/app.py" --host 127.0.0.1 --port "$ROUTER_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-ors-router.out" 2>"$ROOT/tmp/umbrella04-ors-router.err" &
P3=$!
python3 "$ROOT/services/scheduler/app.py" --host 127.0.0.1 --port "$SCHED_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-ors-scheduler.out" 2>"$ROOT/tmp/umbrella04-ors-scheduler.err" &
P4=$!
python3 "$ROOT/services/memory-core/app.py" --host 127.0.0.1 --port "$MEM_CORE_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-ors-memorycore.out" 2>"$ROOT/tmp/umbrella04-ors-memorycore.err" &
P5=$!
python3 "$ROOT/services/catalog/app.py" --host 127.0.0.1 --port "$CATALOG_PORT" --umbrella-root "$ROOT" --registry "$ROOT/tmp/orchestrator-runtime-summary-catalog.json" >"$ROOT/tmp/umbrella04-ors-catalog.out" 2>"$ROOT/tmp/umbrella04-ors-catalog.err" &
P6=$!
python3 "$ROOT/services/plugin_host/app.py" --host 127.0.0.1 --port "$PLUGIN_HOST_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-ors-plugin-host.out" 2>"$ROOT/tmp/umbrella04-ors-plugin-host.err" &
P7=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" --memory-core-url "$MEM_CORE_URL" --policy-url "$POLICY_URL" --catalog-url "$CATALOG_URL" --plugin-host-url "$PLUGIN_HOST_URL" >"$ROOT/tmp/umbrella04-ors-execution.out" 2>"$ROOT/tmp/umbrella04-ors-execution.err" &
P8=$!
python3 "$ROOT/services/approval/app.py" --host 127.0.0.1 --port "$APPROVAL_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-ors-approval.out" 2>"$ROOT/tmp/umbrella04-ors-approval.err" &
P9=$!
python3 "$ROOT/services/orchestrator/app.py" --host 127.0.0.1 --port "$ORCH_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-ors-orchestrator.out" 2>"$ROOT/tmp/umbrella04-ors-orchestrator.err" &
P10=$!

cleanup() {
  kill "$P1" "$P2" "$P3" "$P4" "$P5" "$P6" "$P7" "$P8" "$P9" "$P10" >/dev/null 2>&1 || true
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
wait_health "$CATALOG_URL/v1/catalog/health"
wait_health "$PLUGIN_HOST_URL/v1/plugin-host/health"
wait_health "$EXEC_URL/v1/execution/health"
wait_health "$APPROVAL_URL/v1/approval/health"
wait_health "$ORCH_URL/v1/orchestrator/health"

python3 - "$POLICY_URL" "$AGENT_ID" <<'PY'
import json, sys, urllib.request

policy_url, agent_id = sys.argv[1], sys.argv[2]
req = urllib.request.Request(
    policy_url + '/v1/policy/agents/register',
    method='POST',
    data=json.dumps({
        'agentId': agent_id,
        'source': 'external',
        'capabilities': ['knowledge.read', 'memorycore.read', 'memorycore.write'],
    }).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(req, timeout=20) as resp:
    out = json.loads(resp.read().decode('utf-8'))
assert out.get('ok') is True, out
PY

"$RUNNER" \
  --umbrella-root "$ROOT" \
  --plan "$SUCCESS_PLAN" \
  --run-id "$SUCCESS_RUN_ID" \
  --policy-url "$POLICY_URL" \
  --lifecycle-url "$LIFECYCLE_URL" \
  --router-url "$ROUTER_URL" \
  --scheduler-url "$SCHED_URL" \
  --execution-url "$EXEC_URL" \
  --approval-url "$APPROVAL_URL" \
  --orchestrator-url "$ORCH_URL" \
  >"$ROOT/tmp/umbrella04-ors-success-runner.out"

set +e
"$RUNNER" \
  --umbrella-root "$ROOT" \
  --plan "$FAIL_PLAN" \
  --run-id "$FAIL_RUN_ID" \
  --policy-url "$POLICY_URL" \
  --lifecycle-url "$LIFECYCLE_URL" \
  --router-url "$ROUTER_URL" \
  --scheduler-url "$SCHED_URL" \
  --execution-url "$EXEC_URL" \
  --approval-url "$APPROVAL_URL" \
  --orchestrator-url "$ORCH_URL" \
  >"$ROOT/tmp/umbrella04-ors-fail-runner.out"
RC=$?
set -e

if [[ "$RC" -eq 0 ]]; then
  echo "expected runtime-summary fail plan to fail"
  exit 1
fi

python3 - "$ROOT" "$SUCCESS_RUN_ID" "$FAIL_RUN_ID" <<'PY'
import json, sys
from pathlib import Path

root = Path(sys.argv[1])
success_run_id = sys.argv[2]
fail_run_id = sys.argv[3]

def load_run(run_id):
    run_dir = root / 'control-plane' / 'observability' / 'runs' / run_id
    return (
        json.loads((run_dir / 'run.json').read_text()),
        json.loads((run_dir / 'summary.json').read_text()),
    )

success_run, success_summary = load_run(success_run_id)
assert success_summary['state'] == 'SUCCEEDED', success_summary
assert success_summary.get('runtimeBreakdown', {}).get('native') == 1, success_summary
assert success_summary.get('runtimeBreakdown', {}).get('umbrella-agent-runtime') == 1, success_summary

steps = {step['stepId']: step for step in success_run['steps']}
native_step = steps['native-memory-write']
skill_step = steps['skill-memory-summarize']
assert native_step.get('runtimeResolved') == 'native', native_step
assert native_step.get('executorRuntime') == 'native', native_step
assert skill_step.get('runtimeResolved') == 'umbrella-agent-runtime', skill_step
assert skill_step.get('executorRuntime') == 'plugin-host', skill_step
assert skill_step.get('actionFamily') == 'skill.*', skill_step
assert skill_step.get('runtimeCapability') == 'catalog.skill.invoke', skill_step

fail_run, fail_summary = load_run(fail_run_id)
assert fail_summary['state'] == 'FAILED', fail_summary
assert fail_summary['terminalReason'] == 'runtime_capability_unsupported', fail_summary
assert fail_summary['runtimeRequested'] == 'umbrella-agent-runtime', fail_summary
assert fail_summary['runtimeResolved'] == 'removed', fail_summary
assert fail_summary['runtimeReason'] == 'requested_runtime_unsupported', fail_summary
assert fail_summary['executorRuntime'] == 'removed-adapter', fail_summary
assert fail_summary['actionFamily'] == 'bootstrap.*', fail_summary
assert fail_summary['runtimeCapability'] == 'removed.compatibility', fail_summary
assert fail_summary.get('supportedRuntimes') == ['removed'], fail_summary
assert fail_summary.get('runtimeSupported') is False, fail_summary
assert fail_summary.get('runtimeBreakdown', {}).get('removed') == 1, fail_summary

print('orchestrator runtime summary PASS')
PY

echo "umbrella0.4 orchestrator runtime summary contract PASS"
