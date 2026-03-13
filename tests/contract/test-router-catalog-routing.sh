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
ROUTER_PORT="$(free_port)"
CATALOG_URL="http://127.0.0.1:$CATALOG_PORT"
ROUTER_URL="http://127.0.0.1:$ROUTER_PORT"
REGISTRY_PATH="$ROOT/tmp/router-catalog-registry.json"

rm -f "$REGISTRY_PATH"

python3 "$ROOT/services/catalog/app.py" --host 127.0.0.1 --port "$CATALOG_PORT" --umbrella-root "$ROOT" --registry "$REGISTRY_PATH" >"$ROOT/tmp/umbrella04-rcr-catalog.out" 2>"$ROOT/tmp/umbrella04-rcr-catalog.err" &
P1=$!
python3 "$ROOT/services/router/app.py" --host 127.0.0.1 --port "$ROUTER_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-rcr-router.out" 2>"$ROOT/tmp/umbrella04-rcr-router.err" &
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
wait_health "$ROUTER_URL/v1/router/health"

python3 - "$ROUTER_URL" <<'PY'
import json, sys, urllib.request

router_url = sys.argv[1]
req = urllib.request.Request(router_url + '/v1/router/route-step', method='POST', data=json.dumps({'step': {'stepId': 'skill-step', 'action': 'skill.memory.summarize'}}).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=20) as resp:
    out = json.loads(resp.read().decode('utf-8'))
assert out.get('runtime') == 'umbrella-agent-runtime', out
assert out.get('runtimeResolved') == 'umbrella-agent-runtime', out
assert out.get('runtimeClass') == 'umbrella-agent-runtime', out
assert out.get('executorRuntime') == 'plugin-host', out
assert out.get('runtimeReason') == 'catalog_action', out
assert out.get('reason') == 'catalog_action', out
assert out.get('catalogAction', {}).get('pluginId') == 'example.memory.skill', out

legacy_req = urllib.request.Request(router_url + '/v1/router/route-step', method='POST', data=json.dumps({'step': {'stepId': 'legacy-memory-get', 'action': 'memory.get'}}).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(legacy_req, timeout=20) as resp:
    legacy_out = json.loads(resp.read().decode('utf-8'))
assert legacy_out.get('runtime') == 'umbrella-agent-runtime', legacy_out
assert legacy_out.get('resolvedActionId') == 'skill.memory.get', legacy_out
assert legacy_out.get('deprecatedActionId') == 'memory.get', legacy_out

promote_req = urllib.request.Request(router_url + '/v1/router/route-step', method='POST', data=json.dumps({'step': {'stepId': 'promote-step', 'action': 'memory.promote'}}).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(promote_req, timeout=20) as resp:
    promote_out = json.loads(resp.read().decode('utf-8'))
assert promote_out.get('runtime') == 'native', promote_out
assert promote_out.get('runtimeReason') == 'matched_action:memory.promote', promote_out

hydrate_req = urllib.request.Request(router_url + '/v1/router/route-step', method='POST', data=json.dumps({'step': {'stepId': 'hydrate-step', 'action': 'memory.hydrate'}}).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(hydrate_req, timeout=20) as resp:
    hydrate_out = json.loads(resp.read().decode('utf-8'))
assert hydrate_out.get('runtime') == 'native', hydrate_out
assert hydrate_out.get('runtimeReason') == 'matched_action:memory.hydrate', hydrate_out
print('router catalog routing PASS')
PY

echo "umbrella0.4 router catalog routing contract PASS"
