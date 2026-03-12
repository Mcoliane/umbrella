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
POLICY_URL="http://127.0.0.1:$POLICY_PORT"
AGENT_ID="boundary-agent-$(date +%s)"

python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-mbp-policy.out" 2>"$ROOT/tmp/umbrella04-mbp-policy.err" &
P1=$!

cleanup() {
  kill "$P1" >/dev/null 2>&1 || true
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

python3 - "$POLICY_URL" "$AGENT_ID" <<'PY'
import json, sys, urllib.request
policy_url=sys.argv[1]
agent_id=sys.argv[2]
payload={
    'agentId': agent_id,
    'source': 'external',
    'capabilities': [
        'knowledge.read',
        'knowledge.write',
        'knowledge.promote',
        'knowledge.backfill',
        'memorycore.read',
        'memorycore.write',
    ],
}
req=urllib.request.Request(policy_url+'/v1/policy/agents/register', method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=20) as resp:
    out=json.loads(resp.read().decode('utf-8'))
assert out.get('ok') is True, out
print('register boundary agent PASS')
PY

python3 - "$POLICY_URL" "$AGENT_ID" <<'PY'
import json, sys, urllib.request
policy_url=sys.argv[1]
agent_id=sys.argv[2]

def authorize(step):
    req=urllib.request.Request(policy_url+'/v1/policy/authorize-step', method='POST', data=json.dumps({'stepSpec': step}).encode('utf-8'), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

# 1) knowledge action in active-run must fail hard
deny1 = authorize({
    'action': 'memory.put',
    'metadata': {'agentId': agent_id, 'boundaryContext': {'phase': 'active-run'}},
})
assert deny1.get('allowed') is False and deny1.get('reason') == 'boundary_hot_path_forbidden', deny1

# 2) hydrate outside bootstrap/resume must fail hard
deny2 = authorize({
    'action': 'memory.hydrate',
    'metadata': {'agentId': agent_id, 'boundaryContext': {'phase': 'active-run'}},
})
assert deny2.get('allowed') is False and deny2.get('reason') == 'hydration_phase_forbidden', deny2

# 3) promote in active-run must require async
deny3 = authorize({
    'action': 'memory.promote',
    'metadata': {'agentId': agent_id, 'boundaryContext': {'phase': 'active-run'}},
})
assert deny3.get('allowed') is False and deny3.get('reason') == 'cross_layer_hot_path_forbidden', deny3

# 4) promote allowed when explicit async in active-run
allow = authorize({
    'action': 'memory.promote',
    'metadata': {'agentId': agent_id, 'boundaryContext': {'phase': 'active-run'}, 'async': True},
})
assert allow.get('allowed') is True and allow.get('ok') is True, allow
print('memory boundary policy hot-path guards PASS')
PY

echo "umbrella0.4 memory boundary policy hotpath contract PASS"
