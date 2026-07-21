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

python3 "$ROOT/services/memory/app.py" --host 127.0.0.1 --port "$MEM_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-mbq-memory.out" 2>"$ROOT/tmp/umbrella04-mbq-memory.err" &
P1=$!
python3 "$ROOT/services/memory-core/app.py" --host 127.0.0.1 --port "$MEM_CORE_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-mbq-memorycore.out" 2>"$ROOT/tmp/umbrella04-mbq-memorycore.err" &
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
payload={'namespace':'team','key':'handoff:incident:77','value':{'status':'queued','owner':'agent-q'},'metadata':{'runId':'run-mbq-1'}}
req=urllib.request.Request(base+'/v1/memory-core/put', method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=20) as resp:
    out=json.loads(resp.read().decode('utf-8'))
assert out.get('ok') is True, out
PY

python3 - "$MEM_URL" <<'PY'
import json, sys, urllib.request
base=sys.argv[1]

def post(path, payload):
    req=urllib.request.Request(base+path, method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

def get(path):
    with urllib.request.urlopen(base+path, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

# queue one valid promotion
queued_ok = post('/v1/promotions/queue', {
    'source': {'namespace': 'team', 'key': 'handoff:incident:77', 'value': {'status':'queued','owner':'agent-q'}, 'metadata': {'runId': 'run-mbq-1'}},
    'target': {'namespace': 'knowledge-team', 'node_id': 'fact:incident:77', 'kind': 'fact', 'title': 'Incident 77 Handoff'},
})
assert queued_ok.get('ok') is True and queued_ok.get('queued') is True, queued_ok

# queue one invalid promotion (missing source.key) to force DLQ
queued_bad = post('/v1/promotions/queue', {
    'source': {'namespace': 'team'},
    'target': {'namespace': 'knowledge-team'},
})
assert queued_bad.get('ok') is True and queued_bad.get('queued') is True, queued_bad

processed = post('/v1/promotions/process-queue', {'maxItems': 10})
assert processed.get('succeeded', 0) >= 1, processed
assert processed.get('failed', 0) >= 1, processed

dlq = get('/v1/promotions/dlq?limit=20')
assert dlq.get('count', 0) >= 1, dlq

replay = post('/v1/promotions/replay-dlq', {'maxItems': 10})
assert replay.get('replayed', 0) >= 1, replay

stats = get('/v1/memory/boundary/stats')
assert 'promotionQueueDepth' in stats and 'promotionDlqDepth' in stats and 'promotionProcessedCount' in stats, stats

with urllib.request.urlopen(base + '/v1/memory/boundary/metrics', timeout=20) as resp:
    metrics = resp.read().decode('utf-8')
assert 'umbrella_memory_promotion_failure_rate' in metrics, metrics
assert 'umbrella_memory_promotion_queue_depth' in metrics, metrics

with urllib.request.urlopen(base + '/v1/nodes/fact:incident:77', timeout=20) as resp:
    node=json.loads(resp.read().decode('utf-8'))
assert ((node.get('content') or {}).get('value')) == {'status':'queued','owner':'agent-q'}, node
print('memory boundary queue/dlq PASS')
PY

python3 - "$MEM_URL" <<'PY'
import json, sys, urllib.error, urllib.request
base=sys.argv[1]

# hydrate guard: phase is required and must be bootstrap|resume
req = urllib.request.Request(
    base + '/v1/hydrations/payload',
    method='POST',
    data=json.dumps({'node_id':'fact:incident:77'}).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
try:
    urllib.request.urlopen(req, timeout=20)
    raise SystemExit('expected hydration guard failure')
except urllib.error.HTTPError as ex:
    assert ex.code == 400, ex.code
print('memory boundary hydration guard PASS')
PY

echo "umbrella0.4 memory boundary queue/dlq contract PASS"
