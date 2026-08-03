#!/usr/bin/env bash
set -euo pipefail

# Automatic promotion is nominate-then-distill, honoring the two-tier design:
# the memory service NOMINATES knowledge that recurs across DIFFERENT towns
# (town-independence by demonstration), and session curates nominations through
# the same Layer 2 distiller as conversation facts before anything reaches
# shared long-term memory. The memory service never writes long-term nodes from
# a sweep — it has no model access by design, and nothing enters the shared
# store undistilled.
#
# Asserted here, against a memory service running solo:
#   A. A fact captured in two different towns is NOMINATED: returned with its
#      member ids and towns — and NOT written to shared:longterm by the sweep.
#   B. A one-town-only note is not nominated — recurrence within a single town
#      proves nothing about town-independence.
#   C. Nomination is side-effect-free: an unmarked cluster re-nominates on the
#      next sweep, so a failed distillation run loses nothing.
#   D. mark-swept retires the sources: they are tagged, and the next sweep
#      returns no nominations.
#   E. Session runs the curation leg (seam): provider-gated, distills through
#      _model_extract_facts, writes via _promote_facts_to_longterm, links
#      promoted-from edges, marks sources only after processing.

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

for ns in ('town:alpha', 'town:beta', 'town:gamma', 'shared:longterm'):
    owner = 'platform' if ns.startswith('shared') else 'town'
    call('/v1/namespaces', {'id': ns, 'owner_type': owner, 'owner_id': ns, 'visibility': 'private'})

fact = ('The deploy pipeline publishes the umbrella app with rsync into the local prefix '
        'directory and preserves operator configuration')
for i, ns in enumerate(('town:alpha', 'town:beta')):
    call('/v1/nodes', {'node_id': f'n-fact-{i}', 'namespace': ns, 'kind': 'delegation-outcome',
                       'title': 'deploy question', 'content': fact, 'tags': ['deploy']})
call('/v1/nodes', {'node_id': 'n-junk', 'namespace': 'town:gamma', 'kind': 'delegation-outcome',
                   'title': 'scratch', 'content': 'temporary one-off note about cloudy weather this afternoon only',
                   'tags': []})

# A. the cross-town fact is nominated, and the sweep writes NOTHING itself
out = call('/v1/promotions/sweep', {'minTowns': 2})
assert out.get('ok') is True, out
assert out.get('nominationCount') == 1, out
nom = out['nominations'][0]
assert sorted(nom['towns']) == ['town:alpha', 'town:beta'], nom
assert sorted(nom['memberIds']) == ['n-fact-0', 'n-fact-1'], nom
assert 'rsync' in nom['representative']['content'], nom
longterm = call('/v1/nodes/search', {'namespace': 'shared:longterm', 'query': '', 'k': 50})['results']
assert longterm == [], f'sweep must not write long-term nodes itself: {longterm}'

# B. the single-town note was not nominated
assert all('weather' not in str(n['representative']['content']) for n in out['nominations']), out

# C. nomination is side-effect-free: unmarked clusters re-nominate
again = call('/v1/promotions/sweep', {'minTowns': 2})
assert again.get('nominationCount') == 1, 'unmarked nomination must survive for retry'
assert sorted(again['nominations'][0]['memberIds']) == ['n-fact-0', 'n-fact-1'], again

# D. mark-swept retires the sources
marked = call('/v1/promotions/mark-swept', {'nodeIds': nom['memberIds']})
assert marked.get('ok') is True and marked.get('marked') == 2, marked
done = call('/v1/promotions/sweep', {'minTowns': 2})
assert done.get('nominationCount') == 0, done
alpha = call('/v1/nodes/search', {'namespace': 'town:alpha', 'query': '', 'k': 10})['results']
assert any('swept-promoted' in r['node']['tags'] for r in alpha), alpha
print('promotion nomination PASS')
PY

# E. seam check: session owns the curation leg of the pipeline.
python3 - "$ROOT" <<'PY'
import sys
from pathlib import Path

src = (Path(sys.argv[1]) / 'services' / 'session' / 'app.py').read_text(encoding='utf-8')
assert 'self._maybe_sweep(base)' in src, 'session no longer triggers the promotion pipeline after capture'
assert '/v1/promotions/sweep' in src, 'session no longer requests nominations'
assert "hint='recurred across towns: '" in src, \
    'nominations no longer pass through Layer 2 distillation (_model_extract_facts)'
assert "self._promote_facts_to_longterm(base, facts, ['auto-promoted'])" in src, \
    'distilled nominations no longer written through the Layer 1 gate'
assert '/v1/promotions/mark-swept' in src, 'session no longer marks processed sources'
assert "'relation': 'promoted-from'" in src, 'provenance edges no longer linked'
assert 'if not provider_enabled(load_model_provider(self.root))' in src, \
    'sweep no longer waits for a model provider (curation would be skipped)'
print('nominate-then-distill seam PASS')
PY

echo "PASS test-memory-promotion-sweep"
