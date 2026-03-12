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
SEED_PATH="$ROOT/control-plane/policy/multi-agent-policy.json"
REGISTRY_PATH="$ROOT/control-plane/observability/policy/agent-registry.json"
AGENT_ID="registry-split-agent-$(date +%s)"

before_hash="$(python3 - "$SEED_PATH" <<'PY'
import hashlib, sys
from pathlib import Path
print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"

python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-prs-policy.out" 2>"$ROOT/tmp/umbrella04-prs-policy.err" &
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
payload={'agentId': agent_id, 'source': 'external', 'capabilities': ['memory.write']}
req=urllib.request.Request(policy_url+'/v1/policy/agents/register', method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=20) as resp:
    out=json.loads(resp.read().decode('utf-8'))
assert out.get('ok') is True, out
PY

after_hash="$(python3 - "$SEED_PATH" <<'PY'
import hashlib, sys
from pathlib import Path
print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"

if [[ "$before_hash" != "$after_hash" ]]; then
  echo "expected policy seed to remain unchanged after registration"
  exit 1
fi

python3 - "$REGISTRY_PATH" "$AGENT_ID" <<'PY'
import json, sys
from pathlib import Path
registry_path = Path(sys.argv[1])
agent_id = sys.argv[2]
data = json.loads(registry_path.read_text(encoding='utf-8'))
agents = data.get('agents') if isinstance(data.get('agents'), dict) else {}
agent = agents.get(agent_id) if isinstance(agents.get(agent_id), dict) else None
assert agent is not None, data
assert agent.get('agentId') == agent_id, agent
assert agent.get('capabilities') == ['memory.write'], agent
print('policy runtime registry split PASS')
PY

echo "umbrella0.4 policy runtime registry split contract PASS"
