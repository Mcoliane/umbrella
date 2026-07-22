#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/tests/contract/helpers/runtime-root.sh"
TEST_TMP="$(contract_make_tmpdir "$ROOT" session-converse)"
RUNTIME_ROOT="$(contract_make_runtime_root "$ROOT" session-converse-runtime)"
export UMBRELLA_RUNTIME_ROOT="$RUNTIME_ROOT"

free_port() {
  python3 - <<'PY'
import socket
s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()
PY
}

CONFIG_PATH="$RUNTIME_ROOT/control-plane/runtime/model-provider.json"
SECRETS_PATH="$RUNTIME_ROOT/control-plane/runtime/model-provider.secrets.json"
BROKER_CONFIG_PATH="$RUNTIME_ROOT/control-plane/runtime/model-broker.json"
BROKER_SECRETS_PATH="$RUNTIME_ROOT/control-plane/runtime/model-broker.secrets.json"

cat >"$CONFIG_PATH" <<'JSON'
{
  "version": "umbrella.model-provider.v1",
  "enabled": false,
  "provider": {
    "id": "default",
    "type": "openai-compatible",
    "baseUrl": "",
    "defaultModel": "",
    "timeoutSec": 20
  },
  "agentDefaults": {
    "umbrella.mayor.v1": {
      "model": ""
    },
    "umbrella.originator.v1": {
      "model": ""
    },
    "umbrella.programming-agent.v1": {
      "model": ""
    }
  }
}
JSON
rm -f "$SECRETS_PATH"
cat >"$BROKER_CONFIG_PATH" <<'JSON'
{
  "version": "umbrella.model-broker.v1",
  "enabled": false,
  "broker": {
    "url": "",
    "defaultConnectionId": "default",
    "allowFallback": true
  },
  "providers": {
    "openai-compatible": {
      "id": "openai-compatible",
      "type": "openai-compatible",
      "supportsApiKey": true,
      "supportsOAuth": false
    }
  },
  "connections": {
    "default": {
      "id": "default",
      "providerId": "openai-compatible",
      "authMode": "api_key",
      "label": "Disabled for fallback test",
      "enabled": false,
      "baseUrl": "",
      "defaultModel": "",
      "timeoutSec": 20
    }
  },
  "routing": {
    "defaultConnectionId": "default",
    "allowFallback": true,
    "packageDefaults": {}
  }
}
JSON
rm -f "$BROKER_SECRETS_PATH"

POLICY_PORT="${POLICY_PORT:-$(free_port)}"
CATALOG_PORT="${CATALOG_PORT:-$(free_port)}"
PLUGIN_HOST_PORT="${PLUGIN_HOST_PORT:-$(free_port)}"
EXEC_PORT="${EXEC_PORT:-$(free_port)}"
SESSION_PORT="${SESSION_PORT:-$(free_port)}"

POLICY_URL="http://127.0.0.1:$POLICY_PORT"
CATALOG_URL="http://127.0.0.1:$CATALOG_PORT"
PLUGIN_HOST_URL="http://127.0.0.1:$PLUGIN_HOST_PORT"
EXEC_URL="http://127.0.0.1:$EXEC_PORT"
SESSION_URL="http://127.0.0.1:$SESSION_PORT"

python3 "$ROOT/services/catalog/app.py" --host 127.0.0.1 --port "$CATALOG_PORT" --umbrella-root "$ROOT" --registry "$TEST_TMP/session-converse-catalog.json" >"$TEST_TMP/umbrella04-conv-catalog.out" 2>"$TEST_TMP/umbrella04-conv-catalog.err" &
P2=$!
python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$TEST_TMP/umbrella04-conv-policy.out" 2>"$TEST_TMP/umbrella04-conv-policy.err" &
P1=$!
python3 "$ROOT/services/plugin_host/app.py" --host 127.0.0.1 --port "$PLUGIN_HOST_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$TEST_TMP/umbrella04-conv-plugin-host.out" 2>"$TEST_TMP/umbrella04-conv-plugin-host.err" &
P3=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" --policy-url "$POLICY_URL" --catalog-url "$CATALOG_URL" --plugin-host-url "$PLUGIN_HOST_URL" >"$TEST_TMP/umbrella04-conv-execution.out" 2>"$TEST_TMP/umbrella04-conv-execution.err" &
P4=$!
python3 "$ROOT/services/session/app.py" --host 127.0.0.1 --port "$SESSION_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" --execution-url "$EXEC_URL" >"$TEST_TMP/umbrella04-conv-session.out" 2>"$TEST_TMP/umbrella04-conv-session.err" &
P5=$!

cleanup() {
  contract_kill_pids "$P1" "$P2" "$P3" "$P4" "$P5"
  rm -rf "$RUNTIME_ROOT" "$TEST_TMP"
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
wait_health "$CATALOG_URL/v1/catalog/health"
wait_health "$PLUGIN_HOST_URL/v1/plugin-host/health"
wait_health "$EXEC_URL/v1/execution/health"
wait_health "$SESSION_URL/v1/session/health"

python3 - "$SESSION_URL" <<'PY'
import json, sys, urllib.request

session_url = sys.argv[1]

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

created = post(session_url + '/v1/sessions', {'agentId': 'mayor', 'title': 'Town Hall', 'metadata': {'workerAgentPackageIds': []}})
session = created.get('session') or {}
session_id = session.get('sessionId')
assert session_id, created

mayor_direct = post(session_url + f'/v1/sessions/{session_id}/converse', {'target': 'mayor', 'content': 'can you respond with "hello"?'})
assert mayor_direct.get('ok') is True, mayor_direct
assert mayor_direct.get('modeResolved') == 'direct', mayor_direct
assert mayor_direct.get('reply') == 'hello', mayor_direct

originator = post(session_url + f'/v1/sessions/{session_id}/converse', {'target': 'originator', 'content': 'what do you do here?'})
assert originator.get('ok') is True, originator
assert 'create workers' in originator.get('reply', '').lower() or 'staff' in originator.get('reply', '').lower(), originator

originated = post(
    session_url + f'/v1/sessions/{session_id}/originations',
    {
        'originatorAgentId': 'originator',
        'agentId': 'programming-agent-1',
        'shopId': 'development-shop',
        'agentPackageId': 'umbrella.programming-agent.v1',
    },
)
assert originated.get('agent', {}).get('agentId') == 'programming-agent-1', originated

worker = post(session_url + f'/v1/sessions/{session_id}/converse', {'target': 'programming-agent-1', 'content': 'fact:worker'})
assert worker.get('ok') is True, worker
assert 'fact:worker' in worker.get('reply', ''), worker

mayor_delegate = post(session_url + f'/v1/sessions/{session_id}/converse', {'target': 'mayor', 'content': 'fact:delegated', 'waitForResult': True})
assert mayor_delegate.get('ok') is True, mayor_delegate
assert mayor_delegate.get('modeResolved') == 'delegate', mayor_delegate
assert mayor_delegate.get('turnId'), mayor_delegate
assert len(mayor_delegate.get('delegations') or []) == 1, mayor_delegate
assert 'Mayor summary for "fact:delegated"' in mayor_delegate.get('reply', ''), mayor_delegate

fetched = get(session_url + f'/v1/sessions/{session_id}')
session_payload = fetched.get('session') if isinstance(fetched.get('session'), dict) else fetched
messages = session_payload.get('messages') or []
contents = [str(message.get('content', '')) for message in messages]
assert 'hello' in contents, messages
assert any('fact:worker' in content for content in contents), messages
assert any('Mayor summary for "fact:delegated"' in content for content in contents), messages
print('session converse PASS')
PY

echo "umbrella0.4 session converse contract PASS"
