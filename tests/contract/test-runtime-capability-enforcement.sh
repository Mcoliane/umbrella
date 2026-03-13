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

POLICY_PORT="$(free_port)"
MEMORY_CORE_PORT="$(free_port)"
MEMORY_PORT="$(free_port)"
CATALOG_PORT="$(free_port)"
PLUGIN_HOST_PORT="$(free_port)"
EXECUTION_PORT="$(free_port)"

POLICY_URL="http://127.0.0.1:$POLICY_PORT"
MEMORY_CORE_URL="http://127.0.0.1:$MEMORY_CORE_PORT"
MEMORY_URL="http://127.0.0.1:$MEMORY_PORT"
CATALOG_URL="http://127.0.0.1:$CATALOG_PORT"
PLUGIN_HOST_URL="http://127.0.0.1:$PLUGIN_HOST_PORT"
EXECUTION_URL="http://127.0.0.1:$EXECUTION_PORT"

CATALOG_REGISTRY="$ROOT/tmp/runtime-capability-enforcement-catalog.json"
MEMORY_DB="$ROOT/tmp/runtime-capability-enforcement-memory.db"

rm -f "$CATALOG_REGISTRY" "$MEMORY_DB"

python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-rce-policy.out" 2>"$ROOT/tmp/umbrella04-rce-policy.err" &
P1=$!
python3 "$ROOT/services/memory-core/app.py" --host 127.0.0.1 --port "$MEMORY_CORE_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-rce-memory-core.out" 2>"$ROOT/tmp/umbrella04-rce-memory-core.err" &
P2=$!
python3 "$ROOT/services/memory/app.py" --host 127.0.0.1 --port "$MEMORY_PORT" --db-path "$MEMORY_DB" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-rce-memory.out" 2>"$ROOT/tmp/umbrella04-rce-memory.err" &
P3=$!
python3 "$ROOT/services/catalog/app.py" --host 127.0.0.1 --port "$CATALOG_PORT" --umbrella-root "$ROOT" --registry "$CATALOG_REGISTRY" >"$ROOT/tmp/umbrella04-rce-catalog.out" 2>"$ROOT/tmp/umbrella04-rce-catalog.err" &
P4=$!
python3 "$ROOT/services/plugin_host/app.py" --host 127.0.0.1 --port "$PLUGIN_HOST_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-rce-plugin-host.out" 2>"$ROOT/tmp/umbrella04-rce-plugin-host.err" &
P5=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXECUTION_PORT" --umbrella-root "$ROOT" --policy-url "$POLICY_URL" --memory-core-url "$MEMORY_CORE_URL" --memory-url "$MEMORY_URL" --catalog-url "$CATALOG_URL" --plugin-host-url "$PLUGIN_HOST_URL" >"$ROOT/tmp/umbrella04-rce-execution.out" 2>"$ROOT/tmp/umbrella04-rce-execution.err" &
P6=$!

cleanup() {
  kill "$P1" "$P2" "$P3" "$P4" "$P5" "$P6" >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_health() {
  local url="$1"
  local attempts=40
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

wait_health "$POLICY_URL/v1/policy/health"
wait_health "$MEMORY_CORE_URL/v1/memory-core/health"
wait_health "$MEMORY_URL/v1/memory/health"
wait_health "$CATALOG_URL/v1/catalog/health"
wait_health "$PLUGIN_HOST_URL/v1/plugin-host/health"
wait_health "$EXECUTION_URL/v1/execution/health"

python3 - "$EXECUTION_URL" "$MEMORY_CORE_URL" "$POLICY_URL" "$MEMORY_URL" <<'PY'
import json, sys, urllib.request

execution_url = sys.argv[1]
memory_core_url = sys.argv[2]
policy_url = sys.argv[3]
memory_url = sys.argv[4]

def post(url, payload):
    req = urllib.request.Request(
        url,
        method='POST',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

with urllib.request.urlopen(execution_url + '/v1/execution/runtime-support?actionId=bootstrap.prepare&runtime=umbrella-agent-runtime', timeout=20) as resp:
    support = json.loads(resp.read().decode('utf-8'))
assert support.get('runtimeResolved') == 'removed', support
assert support.get('runtimeSupported') is False, support
assert support.get('supportedRuntimes') == ['removed'], support

registered = post(policy_url + '/v1/policy/agents/register', {
    'agentId': 'runtime-capability-agent',
    'source': 'external',
    'capabilities': ['knowledge.promote', 'knowledge.backfill', 'memorycore.read', 'memorycore.write'],
})
assert registered.get('ok') is True, registered

put = post(memory_core_url + '/v1/memory-core/put', {'namespace': 'team', 'key': 'alpha', 'value': {'title': 'Alpha'}, 'metadata': {'source': 'contract'}})
assert put.get('ok') is True, put

native = post(execution_url + '/v1/execution/submit-step-spec', {
    'runId': 'runtime-cap-run',
    'stepId': 'promote-step',
    'stepSpec': {
        'action': 'memory.promote',
        'runtime': 'native',
        'inputs': {
            'namespace': 'team',
            'key': 'alpha',
            'nodeId': 'node-alpha',
            'targetNamespace': 'durable',
            'kind': 'fact',
            'tags': ['contract']
        },
        'metadata': {
            'actor': 'contract:runtime-capability',
            'agentId': 'runtime-capability-agent',
            'async': True,
            'boundaryContext': {'phase': 'active-run'}
        }
    }
})
assert native.get('runtimeResolved') == 'native', native
assert native.get('ok') is True, native

skill = post(execution_url + '/v1/execution/submit-step-spec', {
    'runId': 'runtime-cap-run',
    'stepId': 'search-step',
    'stepSpec': {
        'action': 'skill.memory.search',
        'runtime': 'umbrella-agent-runtime',
        'timeoutSec': 10,
        'inputs': {'namespace': 'team', 'query': 'alpha', 'k': 5, 'baseUrl': memory_url}
    }
})
assert skill.get('runtimeResolved') == 'umbrella-agent-runtime', skill
assert skill.get('ok') is True, skill

unsupported = post(execution_url + '/v1/execution/submit-step-spec', {
    'runId': 'runtime-cap-run',
    'stepId': 'bootstrap-unsupported',
    'stepSpec': {
        'action': 'bootstrap.prepare',
        'runtime': 'umbrella-agent-runtime',
        'metadata': {'allowCapabilityReroute': False}
    }
})
assert unsupported.get('failureReason') == 'runtime_capability_unsupported', unsupported
assert unsupported.get('failureCategory') == 'validation', unsupported
assert unsupported.get('runtimeRequested') == 'umbrella-agent-runtime', unsupported
assert unsupported.get('runtimeSupported') is False, unsupported
assert unsupported.get('supportedRuntimes') == ['removed'], unsupported

rerouted = post(execution_url + '/v1/execution/submit-step-spec', {
    'runId': 'runtime-cap-run',
    'stepId': 'bootstrap-rerouted',
    'stepSpec': {
        'action': 'bootstrap.prepare',
        'runtime': 'umbrella-agent-runtime'
    }
})
assert rerouted.get('runtimeResolved') == 'removed', rerouted
assert rerouted.get('runtimeReason') == 'capability_reroute:umbrella-agent-runtime->removed', rerouted
assert rerouted.get('executorRuntime') == 'removed-adapter', rerouted

print('runtime capability enforcement PASS')
PY

echo "umbrella0.4 runtime capability enforcement contract PASS"
