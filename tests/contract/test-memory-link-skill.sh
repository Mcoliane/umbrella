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
REGISTRY_PATH="$ROOT/tmp/memory-link-skill-catalog-registry.json"
MEM_DB_PATH="$ROOT/tmp/memory-link-skill.db"
MEM_BOUNDARY_ROOT="$ROOT/tmp/memory-link-skill-boundary"
MAYOR_AGENT_ID="memory-link-mayor-$(date +%s)"

rm -f "$REGISTRY_PATH"
rm -f "$MEM_DB_PATH" "$MEM_DB_PATH-shm" "$MEM_DB_PATH-wal"
rm -rf "$MEM_BOUNDARY_ROOT"

python3 "$ROOT/services/memory/app.py" --host 127.0.0.1 --port "$MEM_PORT" --umbrella-root "$ROOT" --db-path "$MEM_DB_PATH" --boundary-root "$MEM_BOUNDARY_ROOT" >"$ROOT/tmp/umbrella04-mls-memory.out" 2>"$ROOT/tmp/umbrella04-mls-memory.err" &
P1=$!
python3 "$ROOT/services/catalog/app.py" --host 127.0.0.1 --port "$CATALOG_PORT" --umbrella-root "$ROOT" --registry "$REGISTRY_PATH" >"$ROOT/tmp/umbrella04-mls-catalog.out" 2>"$ROOT/tmp/umbrella04-mls-catalog.err" &
P2=$!
python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-mls-policy.out" 2>"$ROOT/tmp/umbrella04-mls-policy.err" &
P3=$!
python3 "$ROOT/services/plugin_host/app.py" --host 127.0.0.1 --port "$PLUGIN_HOST_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-mls-host.out" 2>"$ROOT/tmp/umbrella04-mls-host.err" &
P4=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" --policy-url "$POLICY_URL" --catalog-url "$CATALOG_URL" --plugin-host-url "$PLUGIN_HOST_URL" >"$ROOT/tmp/umbrella04-mls-exec.out" 2>"$ROOT/tmp/umbrella04-mls-exec.err" &
P5=$!
python3 "$ROOT/services/session/app.py" --host 127.0.0.1 --port "$SESSION_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" --execution-url "$EXEC_URL" >"$ROOT/tmp/umbrella04-mls-session.out" 2>"$ROOT/tmp/umbrella04-mls-session.err" &
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
import json, sys, urllib.error, urllib.request
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

python3 - "$MEM_URL" "$POLICY_URL" "$CATALOG_URL" "$SESSION_URL" "$MAYOR_AGENT_ID" <<'PY'
import json, sys, urllib.request

mem_url, policy_url, catalog_url, session_url, mayor_agent_id = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

def post(url, payload):
    req = urllib.request.Request(url, method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as ex:
        body = ex.read().decode('utf-8')
        raise AssertionError(f'HTTP {ex.code} POST {url}: {body}') from ex

def get(url):
    req = urllib.request.Request(url, method='GET')
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

def authorize(step_spec):
    return post(policy_url + '/v1/policy/authorize-step', {'stepSpec': step_spec})

post(mem_url + '/v1/namespaces', {'id': 'graph', 'owner_type': 'system', 'owner_id': 'test'})
post(mem_url + '/v1/nodes', {
    'node_id': 'fact:graph:source',
    'namespace': 'graph',
    'kind': 'fact',
    'title': 'Source Fact',
    'content': {'value': {'summary': 'source node'}},
})
post(mem_url + '/v1/nodes', {
    'node_id': 'fact:graph:target',
    'namespace': 'graph',
    'kind': 'fact',
    'title': 'Target Fact',
    'content': {'value': {'summary': 'target node'}},
})

catalog_items = get(catalog_url + '/v1/catalog/items')
entries = {item['id']: item for item in catalog_items.get('items', [])}
memory_link_item = entries.get('memory.link.skill')
assert memory_link_item is not None, catalog_items
assert any(action.get('id') == 'skill.memory.link' for action in memory_link_item.get('actions', [])), memory_link_item
enabled_item = post(catalog_url + '/v1/catalog/items/enable', {'id': 'memory.link.skill'})
assert enabled_item.get('item', {}).get('enabled') is True, enabled_item

deny_missing_cap = authorize({'action': 'skill.memory.link', 'metadata': {'agentId': mayor_agent_id, 'shopId': 'town-hall'}})
assert deny_missing_cap.get('allowed') is False and deny_missing_cap.get('reason') == 'external_agent_registration_required', deny_missing_cap

post(policy_url + '/v1/policy/agents/register', {'agentId': mayor_agent_id, 'source': 'external', 'capabilities': ['knowledge.write']})
post(policy_url + '/v1/policy/agents/register', {'agentId': 'graph-worker', 'source': 'external', 'capabilities': ['knowledge.write']})

deny_no_approval = authorize({'action': 'skill.memory.link', 'metadata': {'agentId': mayor_agent_id, 'shopId': 'town-hall'}})
assert deny_no_approval.get('allowed') is False and deny_no_approval.get('reason') == 'action_approval_required', deny_no_approval

deny_wrong_shop = authorize({
    'action': 'skill.memory.link',
    'metadata': {
        'agentId': 'graph-worker',
        'shopId': 'knowledge-curation-shop',
        'approvalContext': {'approved': True},
    },
})
assert deny_wrong_shop.get('allowed') is False and deny_wrong_shop.get('reason') == 'action_identity_scope_denied', deny_wrong_shop

deny_delegated = authorize({
    'action': 'skill.memory.link',
    'metadata': {
        'agentId': mayor_agent_id,
        'shopId': 'town-hall',
        'delegatedByAgentId': 'originator',
        'approvalContext': {'approved': True},
    },
})
assert deny_delegated.get('allowed') is False and deny_delegated.get('reason') == 'action_delegation_forbidden', deny_delegated

deny_subagent = authorize({
    'action': 'skill.memory.link',
    'metadata': {
        'agentId': mayor_agent_id,
        'shopId': 'town-hall',
        'subAgentId': 'graph-sub-agent',
        'approvalContext': {'approved': True},
    },
})
assert deny_subagent.get('allowed') is False and deny_subagent.get('reason') == 'action_subagent_forbidden', deny_subagent

session_create = post(session_url + '/v1/sessions', {'agentId': mayor_agent_id, 'title': 'Memory Link Session'})
session = session_create.get('session') or {}
session_id = session.get('sessionId')
assert session_id, session_create

worker = post(session_url + f'/v1/sessions/{session_id}/originations', {
    'originatorAgentId': 'originator',
    'agentId': 'graph-worker',
    'role': 'knowledge-curator',
    'shopId': 'knowledge-curation-shop',
    'shopName': 'Knowledge Curation Shop',
    'enabledActionIds': ['skill.memory.link'],
    'metadata': {'business': 'knowledge-curation'},
})
assert worker.get('shop', {}).get('enabledActionIds') == ['skill.memory.link'], worker

denied_worker_invoke = post(session_url + f'/v1/sessions/{session_id}/invoke-action', {
    'shopId': 'knowledge-curation-shop',
    'actionId': 'skill.memory.link',
    'inputs': {
        'fromNodeId': 'fact:graph:source',
        'toNodeId': 'fact:graph:target',
        'relation': 'supports',
        'baseUrl': mem_url,
    },
    'metadata': {'approvalContext': {'approved': True}, 'timeoutSec': 5},
})
assert denied_worker_invoke.get('ok') is False, denied_worker_invoke
worker_result = ((denied_worker_invoke.get('invocation') or {}).get('result') or {})
assert worker_result.get('failureReason') == 'execution_policy_denied', denied_worker_invoke
worker_policy_reason = ((((worker_result.get('result') or {}).get('policyDecision') or {}).get('reason')))
assert worker_policy_reason == 'action_identity_scope_denied', denied_worker_invoke

enabled = post(session_url + f'/v1/sessions/{session_id}/shops/town-hall/actions/enable', {
    'managedByAgentId': mayor_agent_id,
    'actionId': 'skill.memory.link',
    'metadata': {'source': 'memory-link-contract'},
})
town_hall_enabled = enabled.get('shop', {}).get('enabledActionIds', [])
assert 'skill.memory.link' in town_hall_enabled, enabled

invoked = post(session_url + f'/v1/sessions/{session_id}/invoke-action', {
    'shopId': 'town-hall',
    'actionId': 'memory.link',
    'inputs': {
        'fromNodeId': 'fact:graph:source',
        'toNodeId': 'fact:graph:target',
        'relation': 'supports',
        'weight': 0.75,
        'baseUrl': mem_url,
    },
    'metadata': {'approvalContext': {'approved': True}, 'timeoutSec': 5},
})
assert invoked.get('ok') is True, invoked
assert invoked.get('invocation', {}).get('actionId') == 'memory.link', invoked
assert invoked.get('invocation', {}).get('resolvedActionId') == 'skill.memory.link', invoked
assert invoked.get('invocation', {}).get('result', {}).get('resolvedActionId') == 'skill.memory.link', invoked
plugin_result = ((((invoked.get('invocation') or {}).get('result') or {}).get('result') or {}).get('pluginResult') or {})
assert plugin_result.get('relation') == 'supports', invoked
assert plugin_result.get('fromNodeId') == 'fact:graph:source', invoked
assert plugin_result.get('toNodeId') == 'fact:graph:target', invoked
assert abs(float(plugin_result.get('weight', 0.0)) - 0.75) < 0.0001, invoked

events = get(mem_url + '/v1/events?namespace=graph&cursor=0')
edge_events = [event for event in events.get('events', []) if event.get('op') == 'edge_upsert']
assert edge_events, events
payload = edge_events[-1].get('payload') or {}
assert payload.get('from_node_id') == 'fact:graph:source', edge_events[-1]
assert payload.get('to_node_id') == 'fact:graph:target', edge_events[-1]
assert payload.get('relation') == 'supports', edge_events[-1]

print('memory link skill PASS')
PY

echo "umbrella0.4 memory link skill contract PASS"
