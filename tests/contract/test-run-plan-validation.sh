#!/usr/bin/env bash
set -euo pipefail

# A run that executed nothing must never report SUCCEEDED.
#
# Three inputs used to produce `state: SUCCEEDED, exitCode 0` while doing no work:
# a plan path that does not exist, a plan file that is not valid JSON, and a plan
# whose steps share a stepId (the duplicate left an unreachable READY row that the
# terminal-state decision could not see). All three must now be rejected before any
# run state is written, and rejected as a caller error (400) rather than a 500.

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

free_port() {
  python3 - <<'PY'
import socket
s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()
PY
}

ORCH_PORT="$(free_port)"
ORCH_URL="http://127.0.0.1:$ORCH_PORT"

mkdir -p "$ROOT/tmp"

CORRUPT_PLAN="$ROOT/tmp/plan-validation-corrupt.plan.json"
DUPLICATE_PLAN="$ROOT/tmp/plan-validation-duplicate.plan.json"
EMPTY_PLAN="$ROOT/tmp/plan-validation-empty.plan.json"
MISSING_PLAN="$ROOT/tmp/plan-validation-does-not-exist.plan.json"
rm -f "$MISSING_PLAN"

printf 'this is { not json\n' > "$CORRUPT_PLAN"

cat > "$EMPTY_PLAN" <<'JSON'
{
  "id": "umbrella.plan.plan-validation-empty.v1",
  "steps": []
}
JSON

# Both steps carry the same stepId; the second would previously be dropped while
# the run still reported success, so the failing command never ran.
cat > "$DUPLICATE_PLAN" <<'JSON'
{
  "id": "umbrella.plan.plan-validation-duplicate.v1",
  "steps": [
    {
      "stepId": "collide",
      "objective": "First step with the colliding id",
      "command": "python3 -c 'print(\"A\")'",
      "workdir": ".",
      "timeoutSec": 30,
      "timeoutClass": "short",
      "riskClass": "low"
    },
    {
      "stepId": "collide",
      "objective": "Second step with the colliding id — must not be silently dropped",
      "command": "python3 -c 'raise SystemExit(5)'",
      "workdir": ".",
      "timeoutSec": 30,
      "timeoutClass": "short",
      "riskClass": "low"
    }
  ]
}
JSON

python3 "$ROOT/services/orchestrator/app.py" --host 127.0.0.1 --port "$ORCH_PORT" --umbrella-root "$ROOT" \
  >"$ROOT/tmp/umbrella04-planval-orchestrator.out" 2>"$ROOT/tmp/umbrella04-planval-orchestrator.err" &
P1=$!

cleanup(){
  kill "$P1" >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_health() {
  local url="$1"
  local attempts=30
  local i=1
  while [[ "$i" -le "$attempts" ]]; do
    if python3 - "$url" <<'PY'
import json, sys, urllib.request
url=sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=1.5) as r:
        data=json.loads(r.read().decode('utf-8'))
    raise SystemExit(0 if data.get('status')=='ok' else 1)
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

wait_health "$ORCH_URL/v1/orchestrator/health"

python3 - "$ORCH_URL" "$MISSING_PLAN" "$CORRUPT_PLAN" "$EMPTY_PLAN" "$DUPLICATE_PLAN" "$ROOT" <<'PY'
import json, sys, urllib.error, urllib.request
from pathlib import Path

orch_url, missing, corrupt, empty, duplicate, root = sys.argv[1:7]


def start_run(plan_path, run_id):
    req = urllib.request.Request(
        orch_url + '/v1/orchestrator/runs/start',
        method='POST',
        data=json.dumps({'plan': plan_path, 'runId': run_id}).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode('utf-8'))


def assert_rejected(plan_path, run_id, needle):
    status, out = start_run(plan_path, run_id)
    assert status == 400, (plan_path, status, out)
    message = ((out.get('error') or {}).get('message')) or ''
    assert (out.get('error') or {}).get('code') == 'VALIDATION_ERROR', out
    assert needle in message, (needle, out)
    # The run must not have been recorded as anything, least of all as succeeded.
    run_json = Path(root) / 'control-plane' / 'observability' / 'runs' / run_id / 'run.json'
    if run_json.exists():
        state = json.loads(run_json.read_text(encoding='utf-8')).get('state')
        assert state != 'SUCCEEDED', (plan_path, state)
    return out


assert_rejected(missing, 'run-planval-missing', 'plan not found')
assert_rejected(corrupt, 'run-planval-corrupt', 'plan is not valid JSON')
assert_rejected(empty, 'run-planval-empty', 'non-empty steps list')
assert_rejected(duplicate, 'run-planval-duplicate', 'duplicate stepId')

print('run plan validation PASS')
PY

echo "umbrella0.4 run plan validation contract PASS"
