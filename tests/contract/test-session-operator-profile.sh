#!/usr/bin/env bash
set -euo pipefail

# The operator profile is the always-on complement to query-dependent recall: a small
# block of durable operator facts injected into EVERY mayor turn, so the model knows
# its operator without depending on what this particular message happens to match.
#
# Asserted here:
#   A. GET /v1/session/profile is empty before anything is set.
#   B. POST sets it; GET returns it through the session -> memory round trip.
#   C. A second POST wins (newest node is the profile; history is retained as nodes).
#   D. Oversized profiles are capped at the documented budget.
#   E. The seam is actually wired: session injects operatorProfile into the mayor's
#      extra inputs, and the broker renders an OPERATOR PROFILE block. (The skill leg
#      of the seam is enforced by test-chat-broker-request-parity.sh: the broker
#      reading operatorProfile forces the skill to send it.)
#   F. Injection is optional: {"enabled": false} keeps the text visible to the
#      operator but stops it reaching the model; text-only saves preserve the off
#      state; {"enabled": true} resumes. The off switch also halts automatic
#      profile-note accumulation (source seam).

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/tests/contract/helpers/runtime-root.sh"

TEST_TMP="$(contract_make_tmpdir "$ROOT" operator-profile)"
RUNTIME_ROOT="$(contract_make_runtime_root "$ROOT" operator-profile-runtime)"

free_port() {
  python3 - <<'PY'
import socket
s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()
PY
}

MPORT="$(free_port)"
SPORT="$(free_port)"

P1=""; P2=""
cleanup() {
  contract_kill_pids "$P1" "$P2"
  rm -rf "$TEST_TMP" "$RUNTIME_ROOT"
}
trap cleanup EXIT

python3 "$ROOT/services/memory/app.py" --host 127.0.0.1 --port "$MPORT" \
  --db-path "$TEST_TMP/mem.db" --boundary-root "$TEST_TMP/boundary" --umbrella-root "$ROOT" \
  >"$ROOT/tmp/umbrella04-op-mem.out" 2>"$ROOT/tmp/umbrella04-op-mem.err" &
P1=$!
UMBRELLA_RUNTIME_ROOT="$RUNTIME_ROOT" UMBRELLA_MEMORY_URL="http://127.0.0.1:$MPORT" \
  python3 "$ROOT/services/session/app.py" --host 127.0.0.1 --port "$SPORT" --umbrella-root "$ROOT" \
  >"$ROOT/tmp/umbrella04-op-sess.out" 2>"$ROOT/tmp/umbrella04-op-sess.err" &
P2=$!

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
wait_health "http://127.0.0.1:$SPORT/v1/session/health"

python3 - "$SPORT" <<'PY'
import json, sys, urllib.request

port = sys.argv[1]
BASE = f'http://127.0.0.1:{port}/v1/session/profile'

def call(payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE, data=data, method='POST' if data else 'GET',
                                 headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

# A. empty before set
out = call()
assert out.get('ok') is True and out.get('profile') == '', out

# B. set -> get round trip
first = 'Prefers concise replies.\nRuns macOS with zsh.'
assert call({'profile': first}).get('ok') is True
assert call().get('profile') == first

# C. newest wins
second = first + '\nTimezone: US Eastern.'
assert call({'profile': second}).get('ok') is True
assert call().get('profile') == second

# D. budget cap
cap = int(call().get('maxChars', 0))
assert cap > 0
assert call({'profile': 'x' * (cap + 500)}).get('ok') is True
assert len(call().get('profile')) <= cap

# F. injection is optional — and switching off never hides or loses the text
assert call().get('enabled') is True, 'profile must default to enabled'
assert call({'enabled': False}).get('ok') is True
out = call()
assert out.get('enabled') is False and len(out.get('profile', '')) > 0, out
# a text-only save must NOT silently flip injection back on
assert call({'profile': 'just text while off'}).get('ok') is True
out = call()
assert out.get('profile') == 'just text while off' and out.get('enabled') is False, out
assert call({'enabled': True}).get('ok') is True
out = call()
assert out.get('enabled') is True and out.get('profile') == 'just text while off', out
print('operator profile CRUD PASS')
PY

# E. seam wiring: injection on the session side, rendering on the broker side.
python3 - "$ROOT" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
session_src = (root / 'services' / 'session' / 'app.py').read_text(encoding='utf-8')
broker_src = (root / 'services' / 'model_broker' / 'app.py').read_text(encoding='utf-8')

assert "extra['operatorProfile'] = profile" in session_src, \
    'session no longer injects operatorProfile into mayor extra inputs'
assert '"operatorProfile"' in broker_src and 'OPERATOR PROFILE' in broker_src, \
    'broker no longer reads/renders the operator profile block'
# The off switch gates BOTH consumers of the profile: injection reads _profile_text
# (which returns '' when the disabled tag is set) and note accumulation checks the
# enabled flag before growing the profile.
assert '_PROFILE_DISABLED_TAG' in session_src, 'profile off switch removed'
assert "if not payload['enabled']:" in session_src, \
    'profile-note accumulation no longer respects the off switch'
print('operator profile seam wiring PASS')
PY

echo "PASS test-session-operator-profile"
