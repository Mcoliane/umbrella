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

MEM_PORT="$(free_port)"
MEM_CORE_PORT="$(free_port)"
MEM_URL="http://127.0.0.1:$MEM_PORT"
MEM_CORE_URL="http://127.0.0.1:$MEM_CORE_PORT"
MEM_DB_PATH="$ROOT/tmp/umbrella04-mb-memory.db"
MEM_BOUNDARY_ROOT="$ROOT/tmp/umbrella04-mb-memory-boundary"

rm -f "$MEM_DB_PATH" "$MEM_DB_PATH-shm" "$MEM_DB_PATH-wal"
rm -rf "$MEM_BOUNDARY_ROOT"

python3 "$ROOT/services/memory/app.py" --host 127.0.0.1 --port "$MEM_PORT" --umbrella-root "$ROOT" --db-path "$MEM_DB_PATH" --boundary-root "$MEM_BOUNDARY_ROOT" >"$ROOT/tmp/umbrella04-mb-memory.out" 2>"$ROOT/tmp/umbrella04-mb-memory.err" &
P1=$!
python3 "$ROOT/services/memory-core/app.py" --host 127.0.0.1 --port "$MEM_CORE_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-mb-memorycore.out" 2>"$ROOT/tmp/umbrella04-mb-memorycore.err" &
P2=$!

cleanup() {
  kill "$P1" "$P2" >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_health() {
  local url="$1"
  local attempts=100
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

wait_health "$MEM_URL/v1/memory/health"
wait_health "$MEM_CORE_URL/v1/memory-core/health"

python3 - "$MEM_CORE_URL" <<'PY'
import json, sys, urllib.request
base=sys.argv[1]
payload={'namespace':'team','key':'handoff:incident:42','value':{'status':'triaged','owner':'agent-a'},'metadata':{'runId':'run-mb-1'}}
req=urllib.request.Request(base+'/v1/memory-core/put', method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=20) as resp:
    out=json.loads(resp.read().decode('utf-8'))
assert out.get('ok') is True, out
PY

python3 "$ROOT/scripts/tools/memory-promote" \
  --memory-core-url "$MEM_CORE_URL" \
  --memory-url "$MEM_URL" \
  --namespace team \
  --key "handoff:incident:42" \
  --target-namespace knowledge-team \
  --node-id "fact:incident:42" \
  --title "Incident 42 Handoff" \
  >"$ROOT/tmp/umbrella04-mb-promote.out"

python3 - "$MEM_URL" <<'PY'
import json, sys, urllib.request
base=sys.argv[1]
with urllib.request.urlopen(base+'/v1/nodes/fact:incident:42', timeout=20) as resp:
    node=json.loads(resp.read().decode('utf-8'))
value=((node.get('content') or {}).get('value') if isinstance(node.get('content'), dict) else None)
assert value == {'status':'triaged','owner':'agent-a'}, node
print('memory promote PASS')
PY

python3 "$ROOT/scripts/tools/memory-hydrate" \
  --memory-core-url "$MEM_CORE_URL" \
  --memory-url "$MEM_URL" \
  --node-id "fact:incident:42" \
  --phase bootstrap \
  --target-namespace team \
  --target-key "bootstrap:incident:42" \
  >"$ROOT/tmp/umbrella04-mb-hydrate.out"

python3 - "$MEM_CORE_URL" <<'PY'
import json, sys, urllib.request
base=sys.argv[1]
req=urllib.request.Request(base+'/v1/memory-core/get', method='POST', data=json.dumps({'namespace':'team','key':'bootstrap:incident:42'}).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=20) as resp:
    out=json.loads(resp.read().decode('utf-8'))
assert out.get('exists') is True, out
value=((out.get('memory') or {}).get('value'))
assert value == {'status':'triaged','owner':'agent-a'}, out
print('memory hydrate PASS')
PY

echo "umbrella0.4 memory boundary promote/hydrate contract PASS"
