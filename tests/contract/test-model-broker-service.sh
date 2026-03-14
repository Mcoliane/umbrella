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

FAKE_PORT="$(free_port)"
BROKER_PORT="$(free_port)"
BROKER_URL="http://127.0.0.1:$BROKER_PORT"
FAKE_URL="http://127.0.0.1:$FAKE_PORT/v1"
CONFIG_PATH="$ROOT/control-plane/runtime/model-broker.json"
SECRETS_PATH="$ROOT/control-plane/runtime/model-broker.secrets.json"
CONFIG_BAK="$ROOT/tmp/model-broker.json.bak"
SECRETS_BAK="$ROOT/tmp/model-broker.secrets.json.bak"

[[ -f "$CONFIG_PATH" ]] && cp "$CONFIG_PATH" "$CONFIG_BAK" || true
[[ -f "$SECRETS_PATH" ]] && cp "$SECRETS_PATH" "$SECRETS_BAK" || true

cleanup() {
  kill "$P0" "$P1" >/dev/null 2>&1 || true
  if [[ -f "$CONFIG_BAK" ]]; then cp "$CONFIG_BAK" "$CONFIG_PATH"; fi
  if [[ -f "$SECRETS_BAK" ]]; then cp "$SECRETS_BAK" "$SECRETS_PATH"; else rm -f "$SECRETS_PATH"; fi
}
trap cleanup EXIT

cat >"$CONFIG_PATH" <<JSON
{
  "version": "umbrella.model-broker.v1",
  "enabled": true,
  "broker": {
    "url": "$BROKER_URL",
    "defaultConnectionId": "default",
    "allowFallback": true
  },
  "providers": {
    "zai": {
      "id": "zai",
      "type": "zai",
      "supportsApiKey": true,
      "supportsOAuth": false
    }
  },
  "connections": {
    "default": {
      "id": "default",
      "providerId": "zai",
      "authMode": "api_key",
      "label": "Z.ai Broker Test",
      "enabled": true,
      "baseUrl": "$FAKE_URL",
      "defaultModel": "glm-broker",
      "timeoutSec": 10
    }
  },
  "routing": {
    "defaultConnectionId": "default",
    "allowFallback": true,
    "packageDefaults": {
      "umbrella.mayor.v1": {"model": "glm-broker"}
    }
  }
}
JSON

cat >"$SECRETS_PATH" <<JSON
{
  "connections": {
    "default": {
      "apiKey": "sk-broker-test"
    }
  }
}
JSON

python3 - "$FAKE_PORT" <<'PY' >"$ROOT/tmp/umbrella04-broker-fake.out" 2>"$ROOT/tmp/umbrella04-broker-fake.err" &
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, sys

port = int(sys.argv[1])

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/v1/models':
            self.send_response(404); self.end_headers(); return
        body = json.dumps({'data': [{'id': 'glm-broker'}]}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != '/v1/chat/completions':
            self.send_response(404); self.end_headers(); return
        n = int(self.headers.get('Content-Length', '0') or '0')
        payload = json.loads(self.rfile.read(n).decode('utf-8') or '{}')
        model = payload.get('model', '')
        auth = self.headers.get('Authorization', '')
        assert auth == 'Bearer sk-broker-test', auth
        body = json.dumps({'choices': [{'message': {'content': json.dumps({'reply': f'zai:{model}', 'mode': 'direct'})}}]}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return

ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
PY
P0=$!

python3 "$ROOT/services/model_broker/app.py" --host 127.0.0.1 --port "$BROKER_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-model-broker.out" 2>"$ROOT/tmp/umbrella04-model-broker.err" &
P1=$!

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

wait_health "$BROKER_URL/v1/model-broker/health"

python3 - "$BROKER_URL" <<'PY'
import json, sys, urllib.request

broker_url = sys.argv[1]

def get(url):
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

def post(url, payload):
    req = urllib.request.Request(url, method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

providers = get(broker_url + '/v1/providers')
assert any(row.get('id') == 'zai' for row in providers.get('providers', [])), providers

connections = get(broker_url + '/v1/connections')
assert connections.get('defaultConnectionId') == 'default', connections
assert any(row.get('id') == 'default' and row.get('providerType') == 'zai' and row.get('secrets', {}).get('apiKeyPresent') is True for row in connections.get('connections', [])), connections

models = get(broker_url + '/v1/models')
assert 'glm-broker' in (models.get('models') or []), models

tested = post(broker_url + '/v1/connections/test', {'connectionId': 'default'})
assert tested.get('test', {}).get('ok') is True, tested
assert tested.get('test', {}).get('providerType') == 'zai', tested

reply = post(broker_url + '/v1/chat/respond', {
    'agentPackageId': 'umbrella.mayor.v1',
    'agentId': 'mayor',
    'message': 'hello',
    'systemPrompt': 'Reply in JSON with keys reply and mode.',
    'instructions': 'Mode must be direct.',
    'townContext': {'title': 'Broker Town'},
    'availableShops': []
})
assert reply.get('ok') is True, reply
assert reply.get('reply') == 'zai:glm-broker', reply
assert reply.get('providerType') == 'zai', reply
assert reply.get('connectionUsed') == 'default', reply
print('model broker service PASS')
PY

echo "umbrella0.4 model broker service PASS"
