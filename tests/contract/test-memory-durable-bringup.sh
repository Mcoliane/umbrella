#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/tests/contract/helpers/runtime-root.sh"
TEST_TMP="$(contract_make_tmpdir "$ROOT" memory-durable-bringup)"
RUNTIME_ROOT="$(contract_make_runtime_root "$ROOT" memory-durable-bringup-runtime)"
export UMBRELLA_RUNTIME_ROOT="$RUNTIME_ROOT"
MGR="$ROOT/scripts/control-plane/manage-service-mesh"
MANIFEST="$TEST_TMP/memory-durable.manifest.json"
AGENT_ID="durable-memory-agent-$(date +%s)-$$"
INCIDENT="durable-$(date +%s)-$$"

rm -f "$MANIFEST"

cleanup() {
  "$MGR" shutdown --umbrella-root "$ROOT" --manifest "$MANIFEST" >/dev/null 2>&1 || true
  rm -rf "$RUNTIME_ROOT" "$TEST_TMP"
}
trap cleanup EXIT

"$MGR" bringup --umbrella-root "$ROOT" --manifest "$MANIFEST" >"$TEST_TMP/umbrella04-mdb-bringup.out"

python3 - "$MANIFEST" <<'PY'
import json, sys, urllib.error, urllib.request
from pathlib import Path

m = json.loads(Path(sys.argv[1]).read_text())
services = m.get('services') or {}
assert 'memory' in services, sorted(services.keys())
memory = services['memory']
assert memory.get('health') == '/v1/memory/health', memory
url = str(memory.get('url', ''))
assert url.startswith('http://127.0.0.1:'), memory
token = Path((m.get('auth') or {}).get('tokenPath', '')).read_text().strip()
assert token, m.get('auth')

# health WITH the mesh token
req = urllib.request.Request(url + '/v1/memory/health', headers={'Authorization': f'Bearer {token}'})
with urllib.request.urlopen(req, timeout=5) as resp:
    out = json.loads(resp.read().decode('utf-8'))
assert out.get('status') == 'ok', out

# health WITHOUT the token must 401
try:
    urllib.request.urlopen(url + '/v1/memory/health', timeout=5)
    raise SystemExit('expected 401 without mesh token')
except urllib.error.HTTPError as ex:
    assert ex.code == 401, ex.code
print('durable memory manifest/auth PASS')
PY

python3 - "$MANIFEST" "$AGENT_ID" "$INCIDENT" <<'PY'
import json, sys, urllib.request
from pathlib import Path

m = json.loads(Path(sys.argv[1]).read_text())
agent_id = sys.argv[2]
incident = sys.argv[3]
svc = m['services']
token = Path((m.get('auth') or {}).get('tokenPath', '')).read_text().strip()

def call(url, payload=None):
    headers = {'Authorization': f'Bearer {token}'}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, method='POST' if payload is not None else 'GET', data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

policy_url = svc['policy']['url']
mem_core_url = svc['memory-core']['url']
exec_url = svc['execution']['url']
mem_url = svc['memory']['url']

registered = call(policy_url + '/v1/policy/agents/register', {
    'agentId': agent_id,
    'source': 'external',
    'capabilities': ['knowledge.promote', 'knowledge.backfill', 'memorycore.read', 'memorycore.write'],
})
assert registered.get('ok') is True, registered

seed = call(mem_core_url + '/v1/memory-core/put', {
    'namespace': 'team',
    'key': f'handoff:incident:{incident}',
    'value': {'status': 'triaged', 'owner': 'agent-durable'},
    'metadata': {'runId': f'run-{incident}'},
})
assert seed.get('ok') is True, seed

promote = call(exec_url + '/v1/execution/submit-step-spec', {
    'runId': f'durable-promote-{incident}',
    'stepId': 'durable-promote-step',
    'stepSpec': {
        'stepId': 'durable-promote-step',
        'action': 'memory.promote',
        'inputs': {
            'namespace': 'team',
            'key': f'handoff:incident:{incident}',
            'targetNamespace': 'knowledge-team',
            'nodeId': f'fact:incident:{incident}',
            'title': f'Incident {incident} Handoff',
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

node = call(mem_url + f'/v1/nodes/fact:incident:{incident}')
assert ((node.get('content') or {}).get('value')) == {'status': 'triaged', 'owner': 'agent-durable'}, node

hydrate = call(exec_url + '/v1/execution/submit-step-spec', {
    'runId': f'durable-hydrate-{incident}',
    'stepId': 'durable-hydrate-step',
    'stepSpec': {
        'stepId': 'durable-hydrate-step',
        'action': 'memory.hydrate',
        'inputs': {
            'nodeId': f'fact:incident:{incident}',
            'phase': 'bootstrap',
            'targetNamespace': 'team',
            'targetKey': f'bootstrap:incident:{incident}',
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

hydrated = call(mem_core_url + '/v1/memory-core/get', {'namespace': 'team', 'key': f'bootstrap:incident:{incident}'})
assert hydrated.get('exists') is True, hydrated
assert ((hydrated.get('memory') or {}).get('value')) == {'status': 'triaged', 'owner': 'agent-durable'}, hydrated

print('durable memory promote/hydrate PASS')
PY

"$MGR" shutdown --umbrella-root "$ROOT" --manifest "$MANIFEST" >"$TEST_TMP/umbrella04-mdb-shutdown.out"

echo "umbrella0.4 memory durable bringup contract PASS"
