#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/tests/contract/helpers/runtime-root.sh"
TEST_TMP="$(contract_make_tmpdir "$ROOT" session-converse-provider)"
RUNTIME_ROOT="$(contract_make_runtime_root "$ROOT" session-converse-provider-runtime)"
export UMBRELLA_RUNTIME_ROOT="$RUNTIME_ROOT"

free_port() {
  python3 - <<'PY'
import socket
s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()
PY
}

FAKE_PORT="$(free_port)"
BROKER_PORT="$(free_port)"
POLICY_PORT="$(free_port)"
CATALOG_PORT="$(free_port)"
PLUGIN_HOST_PORT="$(free_port)"
EXEC_PORT="$(free_port)"
SESSION_PORT="$(free_port)"

FAKE_URL="http://127.0.0.1:$FAKE_PORT/v1"
BROKER_URL="http://127.0.0.1:$BROKER_PORT"
POLICY_URL="http://127.0.0.1:$POLICY_PORT"
CATALOG_URL="http://127.0.0.1:$CATALOG_PORT"
PLUGIN_HOST_URL="http://127.0.0.1:$PLUGIN_HOST_PORT"
EXEC_URL="http://127.0.0.1:$EXEC_PORT"
SESSION_URL="http://127.0.0.1:$SESSION_PORT"

CONFIG_PATH="$RUNTIME_ROOT/control-plane/runtime/model-provider.json"
SECRETS_PATH="$RUNTIME_ROOT/control-plane/runtime/model-provider.secrets.json"
BROKER_CONFIG_PATH="$RUNTIME_ROOT/control-plane/runtime/model-broker.json"
BROKER_SECRETS_PATH="$RUNTIME_ROOT/control-plane/runtime/model-broker.secrets.json"

cleanup() {
  contract_kill_pids "$P0" "$PB" "$P1" "$P2" "$P3" "$P4" "$P5"
  rm -rf "$RUNTIME_ROOT" "$TEST_TMP"
}
trap cleanup EXIT

cat >"$BROKER_CONFIG_PATH" <<JSON
{
  "version": "umbrella.model-broker.v1",
  "enabled": true,
  "broker": {
    "url": "$BROKER_URL",
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
      "label": "OpenAI-Compatible Test Broker Connection",
      "enabled": true,
      "baseUrl": "$FAKE_URL",
      "defaultModel": "test-model",
      "timeoutSec": 10
    }
  },
  "routing": {
    "defaultConnectionId": "default",
    "allowFallback": true,
    "packageDefaults": {
      "umbrella.mayor.v1": {"model": "test-model"},
      "umbrella.originator.v1": {"model": "test-model"},
      "umbrella.programming-agent.v1": {"model": "test-model"}
    }
  }
}
JSON
cat >"$BROKER_SECRETS_PATH" <<JSON
{
  "connections": {
    "default": {
      "apiKey": "sk-fake-provider"
    }
  }
}
JSON

# Legacy files stay mirrored for compatibility-oriented status endpoints.
cat >"$CONFIG_PATH" <<JSON
{
  "version": "umbrella.model-provider.v1",
  "enabled": true,
  "provider": {
    "id": "default",
    "type": "openai-compatible",
    "baseUrl": "$FAKE_URL",
    "defaultModel": "test-model",
    "timeoutSec": 10
  },
  "agentDefaults": {
    "umbrella.mayor.v1": {"model": "test-model"},
    "umbrella.originator.v1": {"model": "test-model"},
    "umbrella.programming-agent.v1": {"model": "test-model"}
  }
}
JSON
cat >"$SECRETS_PATH" <<JSON
{
  "apiKey": "sk-fake-provider"
}
JSON

python3 - "$FAKE_PORT" >"$TEST_TMP/umbrella04-fake-provider.out" 2>"$TEST_TMP/umbrella04-fake-provider.err" <<'PY' &
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, sys

port = int(sys.argv[1])

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != '/v1/chat/completions':
            self.send_response(404); self.end_headers(); return
        n = int(self.headers.get('Content-Length', '0') or '0')
        raw = self.rfile.read(n)
        payload = json.loads(raw.decode('utf-8') or '{}')
        model = payload.get('model', '')
        auth = self.headers.get('Authorization', '')
        assert auth == 'Bearer sk-fake-provider', auth
        body = {
            'choices': [
                {'message': {'content': json.dumps({'reply': f'stub-provider:{model}', 'mode': 'direct'})}}
            ]
        }
        out = json.dumps(body).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, fmt, *args):
        return

ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
PY
P0=$!

python3 "$ROOT/services/model_broker/app.py" --host 127.0.0.1 --port "$BROKER_PORT" --umbrella-root "$ROOT" >"$TEST_TMP/umbrella04-broker.out" 2>"$TEST_TMP/umbrella04-broker.err" &
PB=$!

python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$TEST_TMP/umbrella04-prov-policy.out" 2>"$TEST_TMP/umbrella04-prov-policy.err" &
P1=$!
python3 "$ROOT/services/catalog/app.py" --host 127.0.0.1 --port "$CATALOG_PORT" --umbrella-root "$ROOT" --registry "$TEST_TMP/session-provider-catalog.json" >"$TEST_TMP/umbrella04-prov-catalog.out" 2>"$TEST_TMP/umbrella04-prov-catalog.err" &
P2=$!
python3 "$ROOT/services/plugin_host/app.py" --host 127.0.0.1 --port "$PLUGIN_HOST_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$TEST_TMP/umbrella04-prov-plugin-host.out" 2>"$TEST_TMP/umbrella04-prov-plugin-host.err" &
P3=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" --policy-url "$POLICY_URL" --catalog-url "$CATALOG_URL" --plugin-host-url "$PLUGIN_HOST_URL" >"$TEST_TMP/umbrella04-prov-execution.out" 2>"$TEST_TMP/umbrella04-prov-execution.err" &
P4=$!
python3 "$ROOT/services/session/app.py" --host 127.0.0.1 --port "$SESSION_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" --execution-url "$EXEC_URL" >"$TEST_TMP/umbrella04-prov-session.out" 2>"$TEST_TMP/umbrella04-prov-session.err" &
P5=$!

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
    raise SystemExit(0 if (data.get('status') == 'ok' or 'enabled' in data) else 1)
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
wait_health "$PLUGIN_HOST_URL/v1/plugin-host/health"
wait_health "$BROKER_URL/v1/model-broker/health"
wait_health "$EXEC_URL/v1/execution/health"
wait_health "$SESSION_URL/v1/session/health"

python3 - "$SESSION_URL" "$BROKER_URL" <<'PY'
import json, sys, urllib.request

session_url, broker_url = sys.argv[1:]

def get(url):
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

def post(url, payload):
    req = urllib.request.Request(url, method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

provider = get(session_url + '/v1/runtime/model-provider')
assert provider.get('configured') is True, provider
assert provider.get('secrets', {}).get('apiKeyPresent') is True, provider
assert provider.get('broker', {}).get('url') == broker_url, provider

broker_models = get(broker_url + '/v1/models')
assert 'test-model' in (broker_models.get('models') or []), broker_models

created = post(session_url + '/v1/sessions', {'agentId': 'mayor', 'title': 'Mayor'})
session = created.get('session') or {}
session_id = session.get('sessionId')
assert session_id, created

reply = post(session_url + f'/v1/sessions/{session_id}/converse', {'target': 'mayor', 'content': 'hello'})
assert reply.get('ok') is True, reply
assert reply.get('reply') == 'stub-provider:test-model', reply
assert reply.get('providerUsed') is True, reply
assert reply.get('providerType') == 'openai-compatible', reply
assert reply.get('modelUsed') == 'test-model', reply
assert reply.get('fallbackUsed') is False, reply
assert reply.get('connectionUsed') == 'default', reply

fetched = get(session_url + f'/v1/sessions/{session_id}')
session_payload = fetched.get('session') if isinstance(fetched.get('session'), dict) else fetched
messages = session_payload.get('messages') or []
assistant = [m for m in messages if m.get('role') == 'assistant'][-1]
meta = assistant.get('metadata') if isinstance(assistant.get('metadata'), dict) else {}
assert meta.get('providerUsed') is True, assistant
assert meta.get('providerType') == 'openai-compatible', assistant
assert meta.get('modelUsed') == 'test-model', assistant
assert meta.get('fallbackUsed') is False, assistant
print('session converse provider configured PASS')
PY

echo "umbrella0.4 session converse provider configured PASS"
