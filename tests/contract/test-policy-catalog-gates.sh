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

python3 - "$POLICY_URL" "$CATALOG_URL" "$ROOT" "$AGENT_ID" <<'PY'
import json, sys, urllib.request
from pathlib import Path

policy_url, catalog_url, root, agent_id = sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4]

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
assert allow.get('effectiveActionPolicy', {}).get('actionClass') == 'skill', allow
assert allow.get('effectiveActionPolicy', {}).get('approvalMode') == 'none', allow

plugin_dir = root / 'tmp' / 'policy-action-plugin'
plugin_dir.mkdir(parents=True, exist_ok=True)
(plugin_dir / 'bin').mkdir(exist_ok=True)
(plugin_dir / 'bin' / 'policy-action').write_text('#!/usr/bin/env bash\nset -euo pipefail\necho "{\\"ok\\":true}"\n', encoding='utf-8')
(plugin_dir / 'manifest.json').write_text(json.dumps({
    'id': 'policy.action.plugin',
    'name': 'Policy Action Plugin',
    'version': '0.1.0',
    'apiVersion': 'umbrella.catalog.manifest.v1',
    'kind': 'plugin',
    'runtime': 'shell',
    'entrypoint': 'bin/policy-action',
    'defaultEnabled': True,
    'compatibility': {
        'umbrella': {'minVersion': '0.4.0'},
        'pluginHostRuntimes': ['shell'],
        'apiVersions': ['umbrella.catalog.manifest.v1'],
        'actionSchemaVersions': ['umbrella.catalog.action.v1'],
    },
    'actions': [{
        'id': 'plugin.policy.enforced',
        'title': 'Policy Enforced',
        'requiredCapabilities': ['knowledge.read'],
        'policyHints': {
            'riskClass': 'high',
            'approvalMode': 'required',
            'fsAccess': 'workspace',
            'identityScope': {'shopIds': ['research-office']},
            'delegationAllowed': False,
            'subAgentAllowed': False
        }
    }],
}, indent=2) + '\n', encoding='utf-8')
install_req = urllib.request.Request(
    catalog_url + '/v1/catalog/install-local',
    method='POST',
    data=json.dumps({'manifestPath': str(plugin_dir / 'manifest.json')}).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(install_req, timeout=20) as resp:
    installed = json.loads(resp.read().decode('utf-8'))
assert installed.get('item', {}).get('id') == 'policy.action.plugin', installed

approval_denied = authorize({'action': 'plugin.policy.enforced', 'metadata': {'agentId': agent_id, 'shopId': 'research-office'}})
assert approval_denied.get('allowed') is False and approval_denied.get('reason') == 'action_approval_required', approval_denied
assert approval_denied.get('effectiveActionPolicy', {}).get('riskClass') == 'high', approval_denied

scope_denied = authorize({
    'action': 'plugin.policy.enforced',
    'metadata': {
        'agentId': agent_id,
        'shopId': 'development-shop',
        'approvalContext': {'approved': True},
        'policyContext': {'workspaceFsAllowed': True},
    },
})
assert scope_denied.get('allowed') is False and scope_denied.get('reason') == 'action_identity_scope_denied', scope_denied

fs_denied = authorize({
    'action': 'plugin.policy.enforced',
    'metadata': {
        'agentId': agent_id,
        'shopId': 'research-office',
        'approvalContext': {'approved': True},
    },
})
assert fs_denied.get('allowed') is False and fs_denied.get('reason') == 'action_fs_scope_denied', fs_denied

delegation_denied = authorize({
    'action': 'plugin.policy.enforced',
    'metadata': {
        'agentId': agent_id,
        'shopId': 'research-office',
        'delegatedByAgentId': 'originator',
        'approvalContext': {'approved': True},
        'policyContext': {'workspaceFsAllowed': True},
    },
})
assert delegation_denied.get('allowed') is False and delegation_denied.get('reason') == 'action_delegation_forbidden', delegation_denied

subagent_denied = authorize({
    'action': 'plugin.policy.enforced',
    'metadata': {
        'agentId': agent_id,
        'shopId': 'research-office',
        'subAgentId': 'research-sub-agent',
        'approvalContext': {'approved': True},
        'policyContext': {'workspaceFsAllowed': True},
    },
})
assert subagent_denied.get('allowed') is False and subagent_denied.get('reason') == 'action_subagent_forbidden', subagent_denied

allowed = authorize({
    'action': 'plugin.policy.enforced',
    'metadata': {
        'agentId': agent_id,
        'shopId': 'research-office',
        'approvalContext': {'approved': True},
        'policyContext': {'workspaceFsAllowed': True},
    },
})
assert allowed.get('allowed') is True, allowed
assert allowed.get('effectiveActionPolicy', {}).get('approvalMode') == 'required', allowed
assert allowed.get('effectiveActionPolicy', {}).get('fsAccess') == 'workspace', allowed

print('policy catalog gates PASS')
PY

echo "umbrella0.4 policy catalog gates contract PASS"
