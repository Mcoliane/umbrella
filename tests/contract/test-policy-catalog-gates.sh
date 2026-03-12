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

CATALOG_PORT="$(free_port)"
POLICY_PORT="$(free_port)"
CATALOG_URL="http://127.0.0.1:$CATALOG_PORT"
POLICY_URL="http://127.0.0.1:$POLICY_PORT"
REGISTRY_PATH="$ROOT/tmp/policy-catalog-registry.json"
AGENT_ID="catalog-agent-$(date +%s)"

rm -f "$REGISTRY_PATH"

python3 "$ROOT/services/catalog/app.py" --host 127.0.0.1 --port "$CATALOG_PORT" --umbrella-root "$ROOT" --registry "$REGISTRY_PATH" >"$ROOT/tmp/umbrella04-pcg-catalog.out" 2>"$ROOT/tmp/umbrella04-pcg-catalog.err" &
P1=$!
python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-pcg-policy.out" 2>"$ROOT/tmp/umbrella04-pcg-policy.err" &
P2=$!

cleanup() {
  kill "$P1" "$P2" >/dev/null 2>&1 || true
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

wait_health "$CATALOG_URL/v1/catalog/health"
wait_health "$POLICY_URL/v1/policy/health"

python3 - "$POLICY_URL" "$AGENT_ID" <<'PY'
import json, sys, urllib.request

policy_url, agent_id = sys.argv[1], sys.argv[2]

def authorize(step_spec):
    req = urllib.request.Request(policy_url + '/v1/policy/authorize-step', method='POST', data=json.dumps({'stepSpec': step_spec}).encode('utf-8'), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

deny_unregistered = authorize({'action': 'skill.memory.summarize', 'metadata': {'agentId': agent_id}})
assert deny_unregistered.get('allowed') is False and deny_unregistered.get('reason') == 'external_agent_registration_required', deny_unregistered

register_req = urllib.request.Request(policy_url + '/v1/policy/agents/register', method='POST', data=json.dumps({'agentId': agent_id, 'source': 'external', 'capabilities': []}).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(register_req, timeout=20) as resp:
    registered = json.loads(resp.read().decode('utf-8'))
assert registered.get('ok') is True, registered

deny_missing_cap = authorize({'action': 'skill.memory.summarize', 'metadata': {'agentId': agent_id}})
assert deny_missing_cap.get('allowed') is False and deny_missing_cap.get('reason') == 'tool_capability_claim_missing', deny_missing_cap
assert 'knowledge.read' in deny_missing_cap.get('acceptableCapabilities', []), deny_missing_cap

register_req = urllib.request.Request(policy_url + '/v1/policy/agents/register', method='POST', data=json.dumps({'agentId': agent_id, 'source': 'external', 'capabilities': ['knowledge.read']}).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(register_req, timeout=20) as resp:
    registered = json.loads(resp.read().decode('utf-8'))
assert registered.get('ok') is True, registered

allow = authorize({'action': 'skill.memory.summarize', 'metadata': {'agentId': agent_id}})
assert allow.get('allowed') is True, allow
assert allow.get('requiredCapability') == 'knowledge.read', allow
assert allow.get('catalogAction', {}).get('pluginId') == 'example.memory.skill', allow

print('policy catalog gates PASS')
PY

echo "umbrella0.4 policy catalog gates contract PASS"
