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
MEM_DB_PATH="$ROOT/tmp/umbrella04-mbq-memory.db"
MEM_BOUNDARY_ROOT="$ROOT/tmp/umbrella04-mbq-memory-boundary"

rm -f "$MEM_DB_PATH" "$MEM_DB_PATH-shm" "$MEM_DB_PATH-wal"
rm -rf "$MEM_BOUNDARY_ROOT"

python3 "$ROOT/services/memory/app.py" --host 127.0.0.1 --port "$MEM_PORT" --umbrella-root "$ROOT" --db-path "$MEM_DB_PATH" --boundary-root "$MEM_BOUNDARY_ROOT" >"$ROOT/tmp/umbrella04-mbq-memory.out" 2>"$ROOT/tmp/umbrella04-mbq-memory.err" &
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

python3 - "$MEM_URL" "$MEM_BOUNDARY_ROOT" <<'PY'
import json, sys, urllib.error, urllib.request
from pathlib import Path
base=sys.argv[1]
boundary=Path(sys.argv[2])

def post(path, payload):
    req=urllib.request.Request(base+path, method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

def get(path):
    with urllib.request.urlopen(base+path, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

# queue one valid promotion; enqueue auto-drains the queue inline
queued_ok = post('/v1/promotions/queue', {
    'source': {'namespace': 'team', 'key': 'handoff:incident:77', 'value': {'status':'queued','owner':'agent-q'}, 'metadata': {'runId': 'run-mbq-1'}},
    'target': {'namespace': 'knowledge-team', 'node_id': 'fact:incident:77', 'kind': 'fact', 'title': 'Incident 77 Handoff'},
})
assert queued_ok.get('ok') is True and queued_ok.get('queued') is True, queued_ok
drain = queued_ok.get('drain') or {}
assert drain.get('succeeded', 0) >= 1, queued_ok

# an invalid promotion (missing source.key) is rejected at the door
try:
    post('/v1/promotions/queue', {
        'source': {'namespace': 'team'},
        'target': {'namespace': 'knowledge-team'},
    })
    raise SystemExit('expected enqueue validation failure')
except urllib.error.HTTPError as ex:
    assert ex.code == 400, ex.code
    detail = json.loads(ex.read().decode('utf-8'))
    assert (detail.get('error') or {}).get('code') == 'VALIDATION_ERROR', detail

# a poison entry already sitting in the queue (e.g. written by an older
# service version) is moved to the DLQ by process-queue
poison_token = '00000000000000000001-mbqpoison1'
(boundary / 'promotion-queue' / f'{poison_token}.json').write_text(json.dumps({
    'token': poison_token,
    'queuedAt': '2026-01-01T00:00:00+00:00',
    'attempts': 0,
    'actor': 'contract-test',
    'requestId': '',
    'payload': {'source': {'namespace': 'team'}, 'target': {'namespace': 'knowledge-team'}},
}, indent=2) + '\n', encoding='utf-8')

processed = post('/v1/promotions/process-queue', {'maxItems': 10})
assert processed.get('processed', 0) >= 1, processed
assert processed.get('failed', 0) >= 1, processed
assert poison_token in (processed.get('dlqTokens') or []), processed

dlq = get('/v1/promotions/dlq?limit=20')
assert dlq.get('count', 0) >= 1, dlq

# a replayable DLQ entry (valid payload, attempts under the cap) succeeds on
# replay while the poison entry is parked instead of replayed forever
replay_token = '00000000000000000002-mbqreplay1'
(boundary / 'promotion-dlq' / f'{replay_token}.json').write_text(json.dumps({
    'token': replay_token,
    'status': 'FAILED',
    'attempts': 1,
    'lastError': 'transient failure',
    'payload': {
        'source': {'namespace': 'team', 'key': 'handoff:incident:78', 'value': {'status':'replayed','owner':'agent-q'}},
        'target': {'namespace': 'knowledge-team', 'node_id': 'fact:incident:78', 'kind': 'fact', 'title': 'Incident 78 Handoff'},
    },
}, indent=2) + '\n', encoding='utf-8')

replay = post('/v1/promotions/replay-dlq', {'maxItems': 10})
assert replay.get('replayed', 0) >= 1, replay
assert replay.get('succeeded', 0) >= 1, replay
assert replay.get('parked', 0) >= 1, replay
assert replay.get('dlqDepth') == 0, replay
assert (boundary / 'promotion-parked' / f'{poison_token}.json').exists(), list(boundary.glob('promotion-parked/*'))
assert (boundary / 'promotion-processed' / f'{replay_token}.json').exists(), list(boundary.glob('promotion-processed/*'))

stats = get('/v1/memory/boundary/stats')
assert 'promotionQueueDepth' in stats and 'promotionDlqDepth' in stats and 'promotionProcessedCount' in stats, stats
assert stats.get('promotionQueueDepth') == 0, stats
assert stats.get('promotionDlqDepth') == 0, stats
assert stats.get('promotionParkedCount', 0) >= 1, stats

with urllib.request.urlopen(base + '/v1/memory/boundary/metrics', timeout=20) as resp:
    metrics = resp.read().decode('utf-8')
assert 'umbrella_memory_promotion_failure_rate' in metrics, metrics
assert 'umbrella_memory_promotion_queue_depth' in metrics, metrics
assert 'umbrella_memory_promotion_parked_depth' in metrics, metrics

with urllib.request.urlopen(base + '/v1/nodes/fact:incident:77', timeout=20) as resp:
    node=json.loads(resp.read().decode('utf-8'))
assert ((node.get('content') or {}).get('value')) == {'status':'queued','owner':'agent-q'}, node

with urllib.request.urlopen(base + '/v1/nodes/fact:incident:78', timeout=20) as resp:
    node=json.loads(resp.read().decode('utf-8'))
assert ((node.get('content') or {}).get('value')) == {'status':'replayed','owner':'agent-q'}, node
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
