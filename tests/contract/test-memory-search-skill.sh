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
CATALOG_PORT="$(free_port)"
POLICY_PORT="$(free_port)"
PLUGIN_HOST_PORT="$(free_port)"
EXEC_PORT="$(free_port)"
SESSION_PORT="$(free_port)"
MEM_URL="http://127.0.0.1:$MEM_PORT"
CATALOG_URL="http://127.0.0.1:$CATALOG_PORT"
POLICY_URL="http://127.0.0.1:$POLICY_PORT"
PLUGIN_HOST_URL="http://127.0.0.1:$PLUGIN_HOST_PORT"
EXEC_URL="http://127.0.0.1:$EXEC_PORT"
SESSION_URL="http://127.0.0.1:$SESSION_PORT"
REGISTRY_PATH="$ROOT/tmp/memory-search-skill-catalog-registry.json"
MEM_DB_PATH="$ROOT/tmp/memory-search-skill.db"
MEM_BOUNDARY_ROOT="$ROOT/tmp/memory-search-skill-boundary"
AGENT_ID="memory-search-agent-$(date +%s)"

rm -f "$REGISTRY_PATH"
rm -f "$MEM_DB_PATH" "$MEM_DB_PATH-shm" "$MEM_DB_PATH-wal"
rm -rf "$MEM_BOUNDARY_ROOT"

python3 "$ROOT/services/memory/app.py" --host 127.0.0.1 --port "$MEM_PORT" --umbrella-root "$ROOT" --db-path "$MEM_DB_PATH" --boundary-root "$MEM_BOUNDARY_ROOT" >"$ROOT/tmp/umbrella04-mss-memory.out" 2>"$ROOT/tmp/umbrella04-mss-memory.err" &
P1=$!
python3 "$ROOT/services/catalog/app.py" --host 127.0.0.1 --port "$CATALOG_PORT" --umbrella-root "$ROOT" --registry "$REGISTRY_PATH" >"$ROOT/tmp/umbrella04-mss-catalog.out" 2>"$ROOT/tmp/umbrella04-mss-catalog.err" &
P2=$!
python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-mss-policy.out" 2>"$ROOT/tmp/umbrella04-mss-policy.err" &
P3=$!
python3 "$ROOT/services/plugin_host/app.py" --host 127.0.0.1 --port "$PLUGIN_HOST_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-mss-host.out" 2>"$ROOT/tmp/umbrella04-mss-host.err" &
P4=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" --policy-url "$POLICY_URL" --catalog-url "$CATALOG_URL" --plugin-host-url "$PLUGIN_HOST_URL" >"$ROOT/tmp/umbrella04-mss-exec.out" 2>"$ROOT/tmp/umbrella04-mss-exec.err" &
P5=$!
python3 "$ROOT/services/session/app.py" --host 127.0.0.1 --port "$SESSION_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" --execution-url "$EXEC_URL" >"$ROOT/tmp/umbrella04-mss-session.out" 2>"$ROOT/tmp/umbrella04-mss-session.err" &
P6=$!

cleanup() {
  kill "$P1" "$P2" "$P3" "$P4" "$P5" "$P6" >/dev/null 2>&1 || true
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
wait_health "$CATALOG_URL/v1/catalog/health"
wait_health "$POLICY_URL/v1/policy/health"
wait_health "$PLUGIN_HOST_URL/v1/plugin-host/health"
wait_health "$EXEC_URL/v1/execution/health"
wait_health "$SESSION_URL/v1/session/health"

python3 - "$MEM_URL" "$POLICY_URL" "$CATALOG_URL" "$SESSION_URL" "$AGENT_ID" <<'PY'
import json, sys, urllib.request

mem_url, policy_url, catalog_url, session_url, agent_id = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

def post(url, payload):
    req = urllib.request.Request(url, method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

ns = post(mem_url + '/v1/namespaces', {'id': 'research', 'owner_type': 'system', 'owner_id': 'test'})
assert ns.get('id') == 'research', ns
node_a = post(mem_url + '/v1/nodes', {
    'node_id': 'fact:research:alpha',
    'namespace': 'research',
    'kind': 'fact',
    'title': 'Alpha Result',
    'content': {'value': {'summary': 'alpha result'}},
})
node_b = post(mem_url + '/v1/nodes', {
    'node_id': 'fact:research:beta',
    'namespace': 'research',
    'kind': 'fact',
    'title': 'Beta Result',
    'content': {'value': {'summary': 'beta result'}},
})
assert node_a.get('node_id') == 'fact:research:alpha', node_a
assert node_b.get('node_id') == 'fact:research:beta', node_b

catalog_items_req = urllib.request.Request(catalog_url + '/v1/catalog/items', method='GET')
with urllib.request.urlopen(catalog_items_req, timeout=20) as resp:
    catalog_items = json.loads(resp.read().decode('utf-8'))
entries = {item['id']: item for item in catalog_items.get('items', [])}
memory_search_item = entries.get('memory.search.skill')
assert memory_search_item is not None, catalog_items
assert any(action.get('id') == 'skill.memory.search' for action in memory_search_item.get('actions', [])), memory_search_item

registered = post(policy_url + '/v1/policy/agents/register', {'agentId': agent_id, 'source': 'external', 'capabilities': ['knowledge.read']})
assert registered.get('ok') is True, registered
registered_worker = post(policy_url + '/v1/policy/agents/register', {'agentId': 'research-agent', 'source': 'external', 'capabilities': ['knowledge.read']})
assert registered_worker.get('ok') is True, registered_worker

profile = post(session_url + '/v1/shop-profiles', {
    'profileId': 'memory-search-research-profile',
    'name': 'Memory Search Research Profile',
    'shopType': 'business',
    'defaultTitle': 'Research Agent',
    'defaultShopName': 'Research Office',
    'enabledActionIds': ['skill.memory.search'],
    'metadata': {'business': 'research-office'},
})
assert profile.get('profile', {}).get('profileId') == 'memory-search-research-profile', profile

session_create = post(session_url + '/v1/sessions', {'agentId': agent_id, 'title': 'Memory Search Session'})
session = session_create.get('session') or {}
session_id = session.get('sessionId')
assert session_id, session_create
assert any(a.get('id') == 'skill.memory.search' for a in session.get('availableActions', [])), session

worker = post(session_url + f'/v1/sessions/{session_id}/originations', {
    'originatorAgentId': 'originator',
    'agentId': 'research-agent',
    'role': 'researcher',
    'shopId': 'research-office',
    'shopProfileId': 'memory-search-research-profile',
})
assert worker.get('shop', {}).get('enabledActionIds') == ['skill.memory.search'], worker

invoked = post(session_url + f'/v1/sessions/{session_id}/invoke-action', {
    'shopId': 'research-office',
    'actionId': 'memory.search',
    'inputs': {'namespace': 'research', 'query': 'result', 'k': 10, 'baseUrl': mem_url},
    'metadata': {'timeoutSec': 5},
})
assert invoked.get('ok') is True, invoked
assert invoked.get('invocation', {}).get('actionId') == 'memory.search', invoked
assert invoked.get('invocation', {}).get('resolvedActionId') == 'skill.memory.search', invoked
assert invoked.get('invocation', {}).get('result', {}).get('resolvedActionId') == 'skill.memory.search', invoked
plugin_result = ((((invoked.get('invocation') or {}).get('result') or {}).get('result') or {}).get('pluginResult') or {})
results = plugin_result.get('results') if isinstance(plugin_result.get('results'), list) else []
assert len(results) >= 2, invoked
assert plugin_result.get('namespace') == 'research', invoked
assert plugin_result.get('summary') == 'memory.search returned 2 result(s) from research', invoked

print('memory search skill PASS')
PY

echo "umbrella0.4 memory search skill contract PASS"
