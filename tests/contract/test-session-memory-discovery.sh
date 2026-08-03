#!/usr/bin/env bash
set -euo pipefail

# Durable-memory discovery must be observable and recoverable.
#
# The failure mode this guards against: session resolved the memory URL once,
# cached the empty string on failure ('' is not None), and from then on silently
# disabled capture and recall for the process lifetime — no log, no counter, no
# health signal, and no recovery without a restart. It also only ever read
# platform-manifest.json, so a mesh brought up by manage-service-mesh (which
# writes service-manifest.json) never had durable memory at all.
#
# Asserted here, against a session service running solo:
#   A. /v1/session/health reports a durableMemory block, and with no manifest it
#      says so: configured=false, source=unresolved.
#   B. Writing a platform manifest AFTER the first failed resolution is enough —
#      the next health poll reports configured=true. A permanent negative cache
#      fails exactly this assertion.
#   C. A runtime root with only service-manifest.json (the mesh launcher's file)
#      also resolves: configured=true from the fallback manifest.
#   D. UMBRELLA_MEMORY_URL overrides discovery and reports source=env.

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/tests/contract/helpers/runtime-root.sh"

R1="$(contract_make_runtime_root "$ROOT" session-memory-discovery-r1)"
R2="$(contract_make_runtime_root "$ROOT" session-memory-discovery-r2)"
R3="$(contract_make_runtime_root "$ROOT" session-memory-discovery-r3)"

free_port() {
  python3 - <<'PY'
import socket
s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()
PY
}

PA="$(free_port)"
PB="$(free_port)"
PC="$(free_port)"

P1=""; P2=""; P3=""
cleanup() {
  contract_kill_pids "$P1" "$P2" "$P3"
  rm -rf "$R1" "$R2" "$R3"
}
trap cleanup EXIT

wait_health() {
  local url="$1"
  local i=1
  while [[ "$i" -le 30 ]]; do
    if python3 - "$url" <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=1.5) as r:
        data = json.loads(r.read().decode('utf-8'))
    raise SystemExit(0 if data.get('status') == 'ok' else 1)
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

# assert_memory <health-url> <configured> <source> <baseUrl>
assert_memory() {
  python3 - "$@" <<'PY'
import json, sys, urllib.request
url, want_configured, want_source, want_base = sys.argv[1:5]
with urllib.request.urlopen(url, timeout=5) as r:
    out = json.loads(r.read().decode('utf-8'))
dm = out.get('durableMemory')
assert isinstance(dm, dict), f'health has no durableMemory block: {out}'
assert dm.get('configured') is (want_configured == 'true'), dm
assert dm.get('source') == want_source, dm
assert dm.get('baseUrl') == want_base, dm
assert isinstance(dm.get('failures'), dict), dm
print(f'durableMemory ok: source={want_source} configured={want_configured}')
PY
}

# --- A + B: no manifest at spawn, manifest written afterwards -----------------
UMBRELLA_RUNTIME_ROOT="$R1" python3 "$ROOT/services/session/app.py" \
  --host 127.0.0.1 --port "$PA" --umbrella-root "$ROOT" \
  >"$ROOT/tmp/umbrella04-smd-a.out" 2>"$ROOT/tmp/umbrella04-smd-a.err" &
P1=$!
wait_health "http://127.0.0.1:$PA/v1/session/health"

assert_memory "http://127.0.0.1:$PA/v1/session/health" false unresolved ""

cat >"$R1/control-plane/runtime/platform-manifest.json" <<'JSON'
{"services": {"memory": {"url": "http://127.0.0.1:59991"}}}
JSON

# The manifest arrived after a failed resolution. The old implementation had
# already cached '' at this point and could never recover without a restart.
assert_memory "http://127.0.0.1:$PA/v1/session/health" true manifest "http://127.0.0.1:59991"

# --- C: mesh launcher writes service-manifest.json, not platform-manifest -----
cat >"$R2/control-plane/runtime/service-manifest.json" <<'JSON'
{"services": {"memory": {"url": "http://127.0.0.1:59992/"}}}
JSON

UMBRELLA_RUNTIME_ROOT="$R2" python3 "$ROOT/services/session/app.py" \
  --host 127.0.0.1 --port "$PB" --umbrella-root "$ROOT" \
  >"$ROOT/tmp/umbrella04-smd-b.out" 2>"$ROOT/tmp/umbrella04-smd-b.err" &
P2=$!
wait_health "http://127.0.0.1:$PB/v1/session/health"

# Trailing slash in the manifest must be normalized away.
assert_memory "http://127.0.0.1:$PB/v1/session/health" true manifest "http://127.0.0.1:59992"

# --- D: explicit env override wins and is labelled as such --------------------
UMBRELLA_RUNTIME_ROOT="$R3" UMBRELLA_MEMORY_URL="http://127.0.0.1:59993" \
  python3 "$ROOT/services/session/app.py" \
  --host 127.0.0.1 --port "$PC" --umbrella-root "$ROOT" \
  >"$ROOT/tmp/umbrella04-smd-c.out" 2>"$ROOT/tmp/umbrella04-smd-c.err" &
P3=$!
wait_health "http://127.0.0.1:$PC/v1/session/health"

assert_memory "http://127.0.0.1:$PC/v1/session/health" true env "http://127.0.0.1:59993"

echo "PASS test-session-memory-discovery"
