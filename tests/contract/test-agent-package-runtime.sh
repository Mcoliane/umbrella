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
CATALOG_PORT="$(free_port)"
PLUGIN_HOST_PORT="$(free_port)"
EXEC_PORT="$(free_port)"
SESSION_PORT="$(free_port)"

POLICY_URL="http://127.0.0.1:$POLICY_PORT"
CATALOG_URL="http://127.0.0.1:$CATALOG_PORT"
PLUGIN_HOST_URL="http://127.0.0.1:$PLUGIN_HOST_PORT"
EXEC_URL="http://127.0.0.1:$EXEC_PORT"
SESSION_URL="http://127.0.0.1:$SESSION_PORT"

python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-apr-policy.out" 2>"$ROOT/tmp/umbrella04-apr-policy.err" &
P1=$!
python3 "$ROOT/services/catalog/app.py" --host 127.0.0.1 --port "$CATALOG_PORT" --umbrella-root "$ROOT" --registry "$ROOT/tmp/agent-package-runtime-catalog.json" >"$ROOT/tmp/umbrella04-apr-catalog.out" 2>"$ROOT/tmp/umbrella04-apr-catalog.err" &
P2=$!
python3 "$ROOT/services/plugin_host/app.py" --host 127.0.0.1 --port "$PLUGIN_HOST_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-apr-plugin-host.out" 2>"$ROOT/tmp/umbrella04-apr-plugin-host.err" &
P3=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" --policy-url "$POLICY_URL" --catalog-url "$CATALOG_URL" --plugin-host-url "$PLUGIN_HOST_URL" >"$ROOT/tmp/umbrella04-apr-execution.out" 2>"$ROOT/tmp/umbrella04-apr-execution.err" &
P4=$!
python3 "$ROOT/services/session/app.py" --host 127.0.0.1 --port "$SESSION_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" --execution-url "$EXEC_URL" >"$ROOT/tmp/umbrella04-apr-session.out" 2>"$ROOT/tmp/umbrella04-apr-session.err" &
P5=$!

cleanup() {
  kill "$P1" "$P2" "$P3" "$P4" "$P5" >/dev/null 2>&1 || true
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

wait_health "$POLICY_URL/v1/policy/health"
wait_health "$CATALOG_URL/v1/catalog/health"
wait_health "$PLUGIN_HOST_URL/v1/plugin-host/health"
wait_health "$EXEC_URL/v1/execution/health"
wait_health "$SESSION_URL/v1/session/health"

python3 - "$SESSION_URL" "$POLICY_URL" <<'PY'
import json, sys, urllib.request

session_url = sys.argv[1]
policy_url = sys.argv[2]

def get(url):
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

def post(url, payload):
    req = urllib.request.Request(
        url,
        method='POST',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

packages = get(session_url + '/v1/agent-packages')
assert next((p for p in packages.get('packages', []) if p.get('packageId') == 'umbrella.mayor.v1'), None), packages
assert next((p for p in packages.get('packages', []) if p.get('packageId') == 'umbrella.originator.v1'), None), packages
package = next((p for p in packages.get('packages', []) if p.get('packageId') == 'umbrella.programming-agent.v1'), None)
assert package is not None, packages
assert package.get('runtimeId') == 'umbrella-agent-runtime', package
assert 'skill.chat.respond' in (package.get('enabledActionIds') or []), package
assert 'skill.memory.summarize' in (package.get('enabledActionIds') or []), package

registered = post(policy_url + '/v1/policy/agents/register', {
    'agentId': 'programming-agent-1',
    'source': 'external',
    'capabilities': ['knowledge.read'],
})
assert registered.get('ok') is True, registered

# A default town ships with the worker shops (code/web/research) so a fresh town
# can build/search/research without hand-wiring shops.
default_town = post(session_url + '/v1/sessions', {'agentId': 'mayor', 'title': 'Default Town'})
default_shops = (default_town.get('session') or {}).get('shops', {})
for wshop in ('development-shop', 'web-shop', 'research-shop'):
    assert wshop in default_shops, default_town

# The origination test below builds its own development-shop, so it uses a bare
# town (workerAgentPackageIds: []) to avoid colliding with the auto-provisioned one.
created = post(session_url + '/v1/sessions', {'agentId': 'mayor', 'title': 'Mayor', 'metadata': {'workerAgentPackageIds': []}})
session = created.get('session') or {}
session_id = session.get('sessionId')
assert session_id, created
assert any(agent.get('agentPackageId') == 'umbrella.mayor.v1' for agent in session.get('agents', [])), session
assert any(agent.get('agentPackageId') == 'umbrella.originator.v1' for agent in session.get('agents', [])), session
assert session.get('shops', {}).get('town-hall', {}).get('agentPackageId') == 'umbrella.mayor.v1', session
assert session.get('shops', {}).get('originator-studio', {}).get('agentPackageId') == 'umbrella.originator.v1', session

originated = post(
    session_url + f'/v1/sessions/{session_id}/originations',
    {
        'originatorAgentId': 'originator',
        'agentId': 'programming-agent-1',
        'shopId': 'development-shop',
        'agentPackageId': 'umbrella.programming-agent.v1',
    },
)
agent = originated.get('agent') or {}
shop = originated.get('shop') or {}
assert agent.get('agentPackageId') == 'umbrella.programming-agent.v1', originated
assert agent.get('role') == 'programmer', originated
assert agent.get('title') == 'Programming Agent', originated
assert shop.get('name') == 'Development Shop', originated
assert shop.get('metadata', {}).get('runtimeId') == 'umbrella-agent-runtime', originated
assert shop.get('metadata', {}).get('business') == 'development-shop', originated
assert 'catalog.skill.invoke' in (shop.get('metadata', {}).get('capabilityFamilies') or []), originated
assert 'skill.chat.respond' in (shop.get('enabledActionIds') or []), originated
assert 'skill.memory.summarize' in (shop.get('enabledActionIds') or []), originated

invoked = post(
    session_url + f'/v1/sessions/{session_id}/invoke-action',
    {
        'shopId': 'development-shop',
        'actionId': 'skill.memory.summarize',
        'inputs': {'nodeId': 'fact:package-test'},
        'metadata': {'timeoutSec': 10},
    },
)
invocation = invoked.get('invocation') or {}
assert invocation.get('runtimeResolved') == 'umbrella-agent-runtime', invoked
assert invocation.get('result', {}).get('runtimeResolved') == 'umbrella-agent-runtime', invoked
assert invocation.get('result', {}).get('result', {}).get('pluginResult', {}).get('summary') == 'example summary for fact:package-test', invoked

print('agent package runtime PASS')
PY

echo "umbrella0.4 agent package runtime contract PASS"
