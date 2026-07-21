#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/tests/contract/helpers/runtime-root.sh"
TEST_TMP="$(contract_make_tmpdir "$ROOT" service-auth-gating)"
RUNTIME_ROOT="$(contract_make_runtime_root "$ROOT" service-auth-gating-runtime)"
export UMBRELLA_RUNTIME_ROOT="$RUNTIME_ROOT"
MGR="$ROOT/scripts/control-plane/manage-platform-stack"
MANIFEST="$TEST_TMP/service-auth-gating.manifest.json"

rm -f "$MANIFEST"

cleanup() {
  "$MGR" shutdown --umbrella-root "$ROOT" --manifest "$MANIFEST" >/dev/null 2>&1 || true
  rm -rf "$RUNTIME_ROOT" "$TEST_TMP"
}
trap cleanup EXIT

"$MGR" bringup --umbrella-root "$ROOT" --manifest "$MANIFEST" --profile full >"$TEST_TMP/umbrella04-sag-bringup.out"

python3 - "$MANIFEST" <<'PY'
import json, sys, urllib.error, urllib.request
from pathlib import Path

m = json.loads(Path(sys.argv[1]).read_text())
svc = m.get('services') or {}
token = Path((m.get('auth') or {}).get('tokenPath', '')).read_text().strip()
assert token, m.get('auth')

checks = [
    ('session', svc['session']['url'] + '/v1/agent-packages'),
    ('catalog', svc['catalog']['url'] + '/v1/catalog/items'),
    ('plugin-host', svc['plugin-host']['url'] + '/v1/plugin-host/health'),
]
for name, url in checks:
    # without a token the request must be rejected
    try:
        urllib.request.urlopen(url, timeout=5)
        raise SystemExit(f'{name}: expected 401/403 without token: {url}')
    except urllib.error.HTTPError as ex:
        assert ex.code in (401, 403), (name, ex.code)
    # with the mesh token the same request must succeed
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200, (name, resp.status)
        json.loads(resp.read().decode('utf-8'))
    print(f'{name} auth gating PASS')
PY

"$MGR" shutdown --umbrella-root "$ROOT" --manifest "$MANIFEST" >"$TEST_TMP/umbrella04-sag-shutdown.out"

echo "umbrella0.4 service auth gating contract PASS"
