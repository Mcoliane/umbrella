#!/usr/bin/env bash
set -euo pipefail

# Automatic promotion: knowledge that recurs across DIFFERENT towns is
# town-independent by demonstration, and the sweep promotes it to shared
# long-term memory without any model call or explicit promote request.
#
# Asserted here, against a memory service running solo:
#   A. A fact captured in two different towns is promoted by one sweep call:
#      it lands in shared:longterm, searchable, linked and tagged.
#   B. A one-town-only note is NOT promoted — recurrence within a single town
#      proves nothing about town-independence.
#   C. The sweep is idempotent: a second call promotes nothing new.
#   D. Content already in shared:longterm is deduped, not written twice.
#   E. Session triggers the sweep opportunistically after capture (seam check),
#      so promotion needs no cron and no operator action.

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/tests/contract/helpers/runtime-root.sh"

TEST_TMP="$(contract_make_tmpdir "$ROOT" promotion-sweep)"

free_port() {
  python3 - <<'PY'
import socket
s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()
PY
}

MPORT="$(free_port)"

P1=""
cleanup() {
  contract_kill_pids "$P1"
  rm -rf "$TEST_TMP"
}
trap cleanup EXIT

python3 "$ROOT/services/memory/app.py" --host 127.0.0.1 --port "$MPORT" \
  --db-path "$TEST_TMP/mem.db" --boundary-root "$TEST_TMP/boundary" --umbrella-root "$ROOT" \
  >"$ROOT/tmp/umbrella04-ps-mem.out" 2>"$ROOT/tmp/umbrella04-ps-mem.err" &
P1=$!

wait_health() {
  local url="$1"
  local i=1
  while [[ "$i" -le 30 ]]; do
    if python3 - "$url" <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=1.5) as r:
        raise SystemExit(0 if json.loads(r.read()).get('status') == 'ok' else 1)
except SystemExit:
    raise
except Exception:
    raise SystemExit(1)
PY
    then
      return 0
    fi
    sleep 0.5
    i=$((i + 1))
  done
  echo "service at $url never became healthy" >&2
  return 1
}

wait_health "http://127.0.0.1:$MPORT/v1/memory/health"

python3 - "$MPORT" <<'PY'
import json, sys, urllib.request

port = sys.argv[1]
M = f'http://127.0.0.1:{port}'

def call(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(M + path, data=data, method='POST' if data else 'GET',
                                 headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

for ns in ('town:alpha', 'town:beta', 'town:gamma'):
    call('/v1/namespaces', {'id': ns, 'owner_type': 'town', 'owner_id': ns, 'visibility': 'private'})

fact = ('The deploy pipeline publishes the umbrella app with rsync into the local prefix '
        'directory and preserves operator configuration')
for i, ns in enumerate(('town:alpha', 'town:beta')):
    call('/v1/nodes', {'node_id': f'n-fact-{i}', 'namespace': ns, 'kind': 'delegation-outcome',
                       'title': 'deploy question', 'content': fact, 'tags': ['deploy']})
call('/v1/nodes', {'node_id': 'n-junk', 'namespace': 'town:gamma', 'kind': 'delegation-outcome',
                   'title': 'scratch', 'content': 'temporary one-off note about cloudy weather this afternoon only',
                   'tags': []})

# A. one sweep promotes the cross-town fact
out = call('/v1/promotions/sweep', {'minTowns': 2})
assert out.get('ok') is True, out
assert out.get('promotedCount') == 1, out
assert sorted(out['promoted'][0]['towns']) == ['town:alpha', 'town:beta'], out

hits = call('/v1/nodes/search', {'namespace': 'shared:longterm', 'query': 'deploy pipeline rsync prefix', 'k': 5})['results']
assert hits and 'rsync' in str(hits[0]['node']['content']), hits
promoted_node = hits[0]['node']
assert 'auto-promoted' in promoted_node['tags'] and 'longterm' in promoted_node['tags'], promoted_node['tags']
assert promoted_node['source'] == 'auto-promotion-sweep', promoted_node['source']

# B. the single-town note stayed out
listing = call('/v1/nodes/search', {'namespace': 'shared:longterm', 'query': '', 'k': 50})['results']
assert all('weather' not in str(r['node']['content']) for r in listing), listing

# sources are tagged so the next sweep skips them
alpha = call('/v1/nodes/search', {'namespace': 'town:alpha', 'query': '', 'k': 10})['results']
assert any('swept-promoted' in r['node']['tags'] for r in alpha), alpha

# C. idempotent
again = call('/v1/promotions/sweep', {'minTowns': 2})
assert again.get('promotedCount') == 0, again

# D. content already durable is deduped, not duplicated: same fact appears in two
# NEW towns, but shared:longterm already holds it from step A.
for ns in ('town:delta', 'town:epsilon'):
    call('/v1/namespaces', {'id': ns, 'owner_type': 'town', 'owner_id': ns, 'visibility': 'private'})
    call('/v1/nodes', {'node_id': f'n-dup-{ns}', 'namespace': ns, 'kind': 'delegation-outcome',
                       'title': 'deploy question', 'content': fact, 'tags': []})
dedup = call('/v1/promotions/sweep', {'minTowns': 2})
assert dedup.get('promotedCount') == 0 and dedup.get('dedupedClusters', 0) >= 1, dedup
listing = call('/v1/nodes/search', {'namespace': 'shared:longterm', 'query': '', 'k': 50})['results']
assert sum(1 for r in listing if 'rsync' in str(r['node']['content'])) == 1, 'fact written twice'
print('promotion sweep PASS')
PY

# E. seam check: session pokes the sweep after capture, so no cron is needed.
python3 - "$ROOT" <<'PY'
import sys
from pathlib import Path

src = (Path(sys.argv[1]) / 'services' / 'session' / 'app.py').read_text(encoding='utf-8')
assert 'self._maybe_sweep(base)' in src, 'session no longer triggers the promotion sweep after capture'
assert '/v1/promotions/sweep' in src, 'session sweep trigger no longer calls the sweep endpoint'
print('sweep trigger seam PASS')
PY

echo "PASS test-memory-promotion-sweep"
