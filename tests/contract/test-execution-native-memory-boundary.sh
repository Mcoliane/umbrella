#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$ROOT/tmp"

free_port() {
  python3 - <<'PY'
import socket
s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()
PY
}

MEM_PORT="$(free_port)"
MEM_CORE_PORT="$(free_port)"
POLICY_PORT="$(free_port)"
EXEC_PORT="$(free_port)"
MEM_URL="http://127.0.0.1:$MEM_PORT"
MEM_CORE_URL="http://127.0.0.1:$MEM_CORE_PORT"
POLICY_URL="http://127.0.0.1:$POLICY_PORT"
EXEC_URL="http://127.0.0.1:$EXEC_PORT"
AGENT_ID="native-boundary-agent-$(date +%s)"

python3 "$ROOT/services/memory/app.py" --host 127.0.0.1 --port "$MEM_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-enmb-memory.out" 2>"$ROOT/tmp/umbrella04-enmb-memory.err" &
P1=$!
python3 "$ROOT/services/memory-core/app.py" --host 127.0.0.1 --port "$MEM_CORE_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-enmb-memorycore.out" 2>"$ROOT/tmp/umbrella04-enmb-memorycore.err" &
P2=$!
python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-enmb-policy.out" 2>"$ROOT/tmp/umbrella04-enmb-policy.err" &
P3=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" --memory-core-url "$MEM_CORE_URL" --memory-url "$MEM_URL" --policy-url "$POLICY_URL" >"$ROOT/tmp/umbrella04-enmb-exec.out" 2>"$ROOT/tmp/umbrella04-enmb-exec.err" &
P4=$!

cleanup() {
  kill "$P1" "$P2" "$P3" "$P4" >/dev/null 2>&1 || true
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
  echo "service health timeout: $url"
  return 1
}

wait_health "$MEM_URL/v1/memory/health"
wait_health "$MEM_CORE_URL/v1/memory-core/health"
wait_health "$POLICY_URL/v1/policy/health"
wait_health "$EXEC_URL/v1/execution/health"

python3 - "$MEM_CORE_URL" "$POLICY_URL" "$EXEC_URL" "$MEM_URL" "$AGENT_ID" <<'PY'
import json, sys, urllib.request

mem_core_url, policy_url, exec_url, mem_url, agent_id = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

def post(url, payload):
    req = urllib.request.Request(url, method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

registered = post(policy_url + '/v1/policy/agents/register', {
    'agentId': agent_id,
    'source': 'external',
    'capabilities': ['knowledge.promote', 'knowledge.backfill', 'memorycore.read', 'memorycore.write'],
})
assert registered.get('ok') is True, registered

seed = post(mem_core_url + '/v1/memory-core/put', {
    'namespace': 'team',
    'key': 'handoff:incident:42',
    'value': {'status': 'triaged', 'owner': 'agent-a'},
    'metadata': {'runId': 'run-native-boundary'},
})
assert seed.get('ok') is True, seed

promote = post(exec_url + '/v1/execution/submit-step-spec', {
    'runId': 'native-promote-run',
    'stepId': 'native-promote-step',
    'stepSpec': {
        'stepId': 'native-promote-step',
        'action': 'memory.promote',
        'inputs': {
            'namespace': 'team',
            'key': 'handoff:incident:42',
            'targetNamespace': 'knowledge-team',
            'nodeId': 'fact:incident:42',
            'title': 'Incident 42 Handoff',
        },
        'metadata': {
            'agentId': agent_id,
            'async': True,
            'boundaryContext': {'phase': 'active-run'},
        },
    },
})
assert promote.get('ok') is True, promote
assert promote.get('runtimeResolved') == 'native', promote
assert promote.get('executorRuntime') == 'native', promote
assert promote.get('result', {}).get('kind') == 'memory.promote', promote

node_req = urllib.request.Request(mem_url + '/v1/nodes/fact:incident:42', method='GET')
with urllib.request.urlopen(node_req, timeout=20) as resp:
    node = json.loads(resp.read().decode('utf-8'))
assert ((node.get('content') or {}).get('value')) == {'status': 'triaged', 'owner': 'agent-a'}, node

hydrate = post(exec_url + '/v1/execution/submit-step-spec', {
    'runId': 'native-hydrate-run',
    'stepId': 'native-hydrate-step',
    'stepSpec': {
        'stepId': 'native-hydrate-step',
        'action': 'memory.hydrate',
        'inputs': {
            'nodeId': 'fact:incident:42',
            'phase': 'bootstrap',
            'targetNamespace': 'team',
            'targetKey': 'bootstrap:incident:42',
        },
        'metadata': {
            'agentId': agent_id,
            'boundaryContext': {'phase': 'bootstrap'},
        },
    },
})
assert hydrate.get('ok') is True, hydrate
assert hydrate.get('runtimeResolved') == 'native', hydrate
assert hydrate.get('executorRuntime') == 'native', hydrate
assert hydrate.get('result', {}).get('kind') == 'memory.hydrate', hydrate

hydrated = post(mem_core_url + '/v1/memory-core/get', {'namespace': 'team', 'key': 'bootstrap:incident:42'})
assert hydrated.get('exists') is True, hydrated
assert ((hydrated.get('memory') or {}).get('value')) == {'status': 'triaged', 'owner': 'agent-a'}, hydrated

print('execution native memory boundary PASS')
PY

echo "umbrella0.4 execution native memory boundary contract PASS"
