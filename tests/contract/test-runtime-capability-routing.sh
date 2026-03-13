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
REGISTRY_PATH="$ROOT/tmp/runtime-capability-routing-registry.json"

rm -f "$REGISTRY_PATH"

python3 "$ROOT/services/catalog/app.py" --host 127.0.0.1 --port "$CATALOG_PORT" --umbrella-root "$ROOT" --registry "$REGISTRY_PATH" >"$ROOT/tmp/umbrella04-rcc-catalog.out" 2>"$ROOT/tmp/umbrella04-rcc-catalog.err" &
P1=$!
python3 "$ROOT/services/router/app.py" --host 127.0.0.1 --port "$ROUTER_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-rcc-router.out" 2>"$ROOT/tmp/umbrella04-rcc-router.err" &
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
import json, sys, urllib.parse, urllib.request

router_url = sys.argv[1]

with urllib.request.urlopen(router_url + '/v1/router/runtime-capabilities', timeout=20) as resp:
    caps = json.loads(resp.read().decode('utf-8'))
contract = caps.get('contract', {})
assert contract.get('id') == 'umbrella.runtime-capabilities.v1', caps
assert 'umbrella-agent-runtime' in (contract.get('runtimes') or {}), caps

def route(step):
    req = urllib.request.Request(
        router_url + '/v1/router/route-step',
        method='POST',
        data=json.dumps({'step': step}).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

skill_out = route({'stepId': 'skill-step', 'action': 'skill.memory.get'})
assert skill_out.get('runtimeResolved') == 'umbrella-agent-runtime', skill_out
assert skill_out.get('actionFamily') == 'skill.*', skill_out
assert skill_out.get('runtimeCapability') == 'catalog.skill.invoke', skill_out
assert skill_out.get('runtimeSupported') is True, skill_out

legacy_out = route({'stepId': 'legacy-step', 'action': 'memory.get'})
assert legacy_out.get('runtimeResolved') == 'umbrella-agent-runtime', legacy_out
assert legacy_out.get('resolvedActionId') == 'skill.memory.get', legacy_out
assert legacy_out.get('deprecatedActionId') == 'memory.get', legacy_out
assert legacy_out.get('runtimeCapability') == 'catalog.skill.invoke', legacy_out

native_out = route({'stepId': 'promote-step', 'action': 'memory.promote'})
assert native_out.get('runtimeResolved') == 'native', native_out
assert native_out.get('actionFamily') == 'memory.promote', native_out
assert native_out.get('runtimeCapability') == 'memory.boundary', native_out

removed_out = route({'stepId': 'bootstrap-step', 'action': 'bootstrap.prepare'})
assert removed_out.get('runtimeResolved') == 'removed', removed_out
assert removed_out.get('actionFamily') == 'bootstrap.*', removed_out
assert removed_out.get('runtimeCapability') == 'removed.compatibility', removed_out

rerouted_out = route({'stepId': 'bootstrap-step', 'action': 'bootstrap.prepare', 'runtime': 'umbrella-agent-runtime'})
assert rerouted_out.get('runtimeRequested') == 'umbrella-agent-runtime', rerouted_out
assert rerouted_out.get('runtimeResolved') == 'removed', rerouted_out
assert rerouted_out.get('runtimeSupported') is False, rerouted_out
assert rerouted_out.get('runtimeReason') == 'capability_reroute:umbrella-agent-runtime->removed', rerouted_out
assert rerouted_out.get('supportedRuntimes') == ['removed'], rerouted_out

print('runtime capability routing PASS')
PY

echo "umbrella0.4 runtime capability routing contract PASS"
