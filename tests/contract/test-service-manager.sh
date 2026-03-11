#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MGR="$ROOT/scripts/control-plane/manage-service-mesh"
RUNNER="$ROOT/scripts/control-plane/run-umbrella-control-plane"
PLAN="control-plane/planner/plans/service-mesh-smoke.json"
MANIFEST="$ROOT/tmp/service-manager.manifest.json"
RUN_ID="run-service-manager-smoke-$(date +%s)"

rm -f "$MANIFEST"

cleanup() {
  "$MGR" shutdown --umbrella-root "$ROOT" --manifest "$MANIFEST" >/dev/null 2>&1 || true
}
trap cleanup EXIT

"$MGR" bringup --umbrella-root "$ROOT" --manifest "$MANIFEST" >/tmp/umbrella04-smgr-bringup.out

python3 - "$MANIFEST" <<'PY'
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
obj=json.loads(p.read_text())
assert obj.get('version') == 'umbrella.service-manifest.v1', obj
services=obj.get('services') or {}
expected={'policy','lifecycle','router','scheduler','memory-core','execution','approval','orchestrator'}
assert set(services.keys()) == expected, services.keys()
for name,row in services.items():
    assert isinstance(row.get('pid'), int) and row['pid'] > 0, (name,row)
    assert str(row.get('url','')).startswith('http://127.0.0.1:'), (name,row)
    assert row.get('logOut') and row.get('logErr'), (name,row)
print('service manager manifest PASS')
PY

"$MGR" status --umbrella-root "$ROOT" --manifest "$MANIFEST" >/tmp/umbrella04-smgr-status1.out

ARGS="$(python3 - "$MANIFEST" <<'PY'
import json, sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text())
svc=m['services']
print(' '.join([
  '--policy-url', svc['policy']['url'],
  '--lifecycle-url', svc['lifecycle']['url'],
  '--router-url', svc['router']['url'],
  '--scheduler-url', svc['scheduler']['url'],
  '--execution-url', svc['execution']['url'],
  '--approval-url', svc['approval']['url'],
  '--orchestrator-url', svc['orchestrator']['url'],
  '--mesh-token', Path((m.get('auth') or {}).get('tokenPath','')).read_text().strip(),
]))
PY
)"

# shellcheck disable=SC2086
"$RUNNER" --umbrella-root "$ROOT" --plan "$PLAN" --run-id "$RUN_ID" $ARGS >/tmp/umbrella04-smgr-runner.out

ROOT="$ROOT" RUN_ID="$RUN_ID" python3 - <<'PY'
import json, os
from pathlib import Path
root=Path(os.environ['ROOT'])
run_id=os.environ['RUN_ID']
summary=json.loads((root/'control-plane'/'observability'/'runs'/run_id/'summary.json').read_text())
assert summary['state']=='SUCCEEDED', summary
print('service manager runner integration PASS')
PY

"$MGR" shutdown --umbrella-root "$ROOT" --manifest "$MANIFEST" >/tmp/umbrella04-smgr-shutdown.out

set +e
"$MGR" status --umbrella-root "$ROOT" --manifest "$MANIFEST" >/tmp/umbrella04-smgr-status2.out
RC=$?
set -e
if [[ "$RC" -eq 0 ]]; then
  echo "expected status to fail after shutdown"
  exit 1
fi

echo "umbrella0.4 service manager contract PASS"
