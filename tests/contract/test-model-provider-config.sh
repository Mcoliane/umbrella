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

SESSION_PORT="${SESSION_PORT:-$(free_port)}"
SESSION_URL="http://127.0.0.1:$SESSION_PORT"

CONFIG_PATH="$ROOT/control-plane/runtime/model-provider.json"
SECRETS_PATH="$ROOT/control-plane/runtime/model-provider.secrets.json"
CONFIG_BAK="$ROOT/tmp/model-provider.json.bak"
SECRETS_BAK="$ROOT/tmp/model-provider.secrets.json.bak"

[[ -f "$CONFIG_PATH" ]] && cp "$CONFIG_PATH" "$CONFIG_BAK" || true
[[ -f "$SECRETS_PATH" ]] && cp "$SECRETS_PATH" "$SECRETS_BAK" || true

cleanup() {
  kill "$P1" >/dev/null 2>&1 || true
  if [[ -f "$CONFIG_BAK" ]]; then cp "$CONFIG_BAK" "$CONFIG_PATH"; fi
  if [[ -f "$SECRETS_BAK" ]]; then cp "$SECRETS_BAK" "$SECRETS_PATH"; else rm -f "$SECRETS_PATH"; fi
}
trap cleanup EXIT

python3 "$ROOT/services/session/app.py" --host 127.0.0.1 --port "$SESSION_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-modelcfg-session.out" 2>"$ROOT/tmp/umbrella04-modelcfg-session.err" &
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

wait_health "$SESSION_URL/v1/session/health"

python3 - "$SESSION_URL" <<'PY'
import json, sys, urllib.request

session_url = sys.argv[1]

def get(url):
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

def post(url, payload):
    req = urllib.request.Request(url, method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

before = get(session_url + '/v1/runtime/model-provider')
assert before.get('provider', {}).get('type') in {'zai', 'openai-compatible'}, before

saved = post(session_url + '/v1/runtime/model-provider', {
    'enabled': True,
    'provider': {
        'type': 'zai',
        'baseUrl': 'http://127.0.0.1:19999/v1',
        'defaultModel': 'glm-test',
        'timeoutSec': 9,
    },
    'apiKey': 'sk-test-1234567890',
})
assert saved.get('saved') is True, saved
assert saved.get('enabled') is True, saved
assert saved.get('provider', {}).get('type') == 'zai', saved
assert saved.get('provider', {}).get('defaultModel') == 'glm-test', saved
assert saved.get('secrets', {}).get('apiKeyPresent') is True, saved
assert saved.get('secrets', {}).get('apiKeyMasked', '').startswith('sk-t'), saved
assert '1234567890' not in json.dumps(saved), saved

tested = post(session_url + '/v1/runtime/model-provider/test', {})
assert isinstance(tested.get('test'), dict), tested
assert tested.get('test', {}).get('configured') is True, tested
print('model provider config PASS')
PY

echo "umbrella0.4 model provider config contract PASS"
