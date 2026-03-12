#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$ROOT/tmp"
MGR="$ROOT/scripts/control-plane/manage-service-mesh"
RUNNER="$ROOT/scripts/control-plane/run-umbrella-control-plane"
PLAN="control-plane/planner/plans/service-mesh-smoke.json"
MANIFEST="$ROOT/tmp/service-auth-mesh.manifest.json"
RUN_BAD="run-auth-mesh-bad-$(date +%s)"
RUN_GOOD="run-auth-mesh-good-$(date +%s)"

rm -f "$MANIFEST"

cleanup() {
  "$MGR" shutdown --umbrella-root "$ROOT" --manifest "$MANIFEST" >/dev/null 2>&1 || true
}
trap cleanup EXIT

"$MGR" bringup --umbrella-root "$ROOT" --manifest "$MANIFEST" >"$ROOT/tmp/umbrella04-auth-bringup.out"

python3 - "$MANIFEST" <<'PY'
import json, sys, urllib.request, urllib.error
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text())
policy=(m['services']['policy']['url']).rstrip('/')
token=Path((m.get('auth') or {}).get('tokenPath','')).read_text().strip()

# no auth -> 401
req=urllib.request.Request(policy+'/v1/policy/health', method='GET')
try:
    urllib.request.urlopen(req, timeout=10)
    raise SystemExit('expected 401 without auth')
except urllib.error.HTTPError as e:
    assert e.code == 401, e.code

# with auth -> 200
req2=urllib.request.Request(policy+'/v1/policy/health', method='GET', headers={'Authorization': f'Bearer {token}'})
with urllib.request.urlopen(req2, timeout=10) as resp:
    out=json.loads(resp.read().decode('utf-8'))
assert out.get('status') == 'ok', out
print('service mesh auth health PASS')
PY

ARGS="$(python3 - "$MANIFEST" <<'PY'
import json, sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text())
svc=m['services']
token=Path((m.get('auth') or {}).get('tokenPath','')).read_text().strip()
print(' '.join([
  '--policy-url', svc['policy']['url'],
  '--lifecycle-url', svc['lifecycle']['url'],
  '--router-url', svc['router']['url'],
  '--scheduler-url', svc['scheduler']['url'],
  '--execution-url', svc['execution']['url'],
  '--approval-url', svc['approval']['url'],
  '--orchestrator-url', svc['orchestrator']['url'],
  '--mesh-token', token,
]))
PY
)"

# runner without token should fail
set +e
"$RUNNER" --umbrella-root "$ROOT" --plan "$PLAN" --run-id "$RUN_BAD" \
  --policy-url "$(python3 -c "import json;print(json.load(open('$MANIFEST'))['services']['policy']['url'])")" \
  --lifecycle-url "$(python3 -c "import json;print(json.load(open('$MANIFEST'))['services']['lifecycle']['url'])")" \
  --router-url "$(python3 -c "import json;print(json.load(open('$MANIFEST'))['services']['router']['url'])")" \
  --scheduler-url "$(python3 -c "import json;print(json.load(open('$MANIFEST'))['services']['scheduler']['url'])")" \
  --execution-url "$(python3 -c "import json;print(json.load(open('$MANIFEST'))['services']['execution']['url'])")" \
  --approval-url "$(python3 -c "import json;print(json.load(open('$MANIFEST'))['services']['approval']['url'])")" \
  --orchestrator-url "$(python3 -c "import json;print(json.load(open('$MANIFEST'))['services']['orchestrator']['url'])")" \
  >"$ROOT/tmp/umbrella04-auth-runner-bad.out" 2>"$ROOT/tmp/umbrella04-auth-runner-bad.err"
RC_BAD=$?
set -e
if [[ "$RC_BAD" -eq 0 ]]; then
  echo "expected runner without mesh token to fail"
  exit 1
fi

# runner with token should succeed
# shellcheck disable=SC2086
"$RUNNER" --umbrella-root "$ROOT" --plan "$PLAN" --run-id "$RUN_GOOD" $ARGS >"$ROOT/tmp/umbrella04-auth-runner-good.out"

ROOT="$ROOT" RUN_ID="$RUN_GOOD" python3 - <<'PY'
import json, os
from pathlib import Path
root=Path(os.environ['ROOT'])
run_id=os.environ['RUN_ID']
summary=json.loads((root/'control-plane'/'observability'/'runs'/run_id/'summary.json').read_text())
assert summary['state']=='SUCCEEDED', summary
print('service mesh auth runner PASS')
PY

echo "umbrella0.4 service auth mesh contract PASS"
