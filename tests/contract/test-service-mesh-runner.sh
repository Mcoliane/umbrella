#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUNNER="$ROOT/scripts/control-plane/run-umbrella-control-plane"
PLAN="control-plane/planner/plans/service-mesh-smoke.json"
RUN_ID="run-service-mesh-smoke-$(date +%s)"

free_port() {
  python3 - <<'PY'
import socket
s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()
PY
}

POLICY_PORT="$(free_port)"
LIFECYCLE_PORT="$(free_port)"
ROUTER_PORT="$(free_port)"
SCHED_PORT="$(free_port)"
EXEC_PORT="$(free_port)"
APPROVAL_PORT="$(free_port)"
ORCH_PORT="$(free_port)"

POLICY_URL="http://127.0.0.1:$POLICY_PORT"
LIFECYCLE_URL="http://127.0.0.1:$LIFECYCLE_PORT"
ROUTER_URL="http://127.0.0.1:$ROUTER_PORT"
SCHED_URL="http://127.0.0.1:$SCHED_PORT"
EXEC_URL="http://127.0.0.1:$EXEC_PORT"
APPROVAL_URL="http://127.0.0.1:$APPROVAL_PORT"
ORCH_URL="http://127.0.0.1:$ORCH_PORT"

RECON_STUB="$ROOT/tmp/reconcile-ok.sh"
mkdir -p "$ROOT/tmp"
cat > "$RECON_STUB" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
echo '{"ok":true}'
exit 0
SH
chmod +x "$RECON_STUB"

python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" >/tmp/umbrella04-policy.out 2>/tmp/umbrella04-policy.err &
P1=$!
python3 "$ROOT/services/lifecycle/app.py" --host 127.0.0.1 --port "$LIFECYCLE_PORT" --umbrella-root "$ROOT" >/tmp/umbrella04-lifecycle.out 2>/tmp/umbrella04-lifecycle.err &
P2=$!
python3 "$ROOT/services/router/app.py" --host 127.0.0.1 --port "$ROUTER_PORT" --umbrella-root "$ROOT" >/tmp/umbrella04-router.out 2>/tmp/umbrella04-router.err &
P3=$!
python3 "$ROOT/services/scheduler/app.py" --host 127.0.0.1 --port "$SCHED_PORT" --umbrella-root "$ROOT" >/tmp/umbrella04-scheduler.out 2>/tmp/umbrella04-scheduler.err &
P4=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" >/tmp/umbrella04-execution.out 2>/tmp/umbrella04-execution.err &
P5=$!
python3 "$ROOT/services/approval/app.py" --host 127.0.0.1 --port "$APPROVAL_PORT" --umbrella-root "$ROOT" >/tmp/umbrella04-approval.out 2>/tmp/umbrella04-approval.err &
P6=$!
python3 "$ROOT/services/orchestrator/app.py" --host 127.0.0.1 --port "$ORCH_PORT" --umbrella-root "$ROOT" >/tmp/umbrella04-orchestrator.out 2>/tmp/umbrella04-orchestrator.err &
P7=$!

cleanup(){
  kill "$P1" "$P2" "$P3" "$P4" "$P5" "$P6" "$P7" >/dev/null 2>&1 || true
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
    ok=(data.get('status')=='ok')
    raise SystemExit(0 if ok else 1)
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

wait_health "$POLICY_URL/v1/policy/health"
wait_health "$LIFECYCLE_URL/v1/lifecycle/health"
wait_health "$ROUTER_URL/v1/router/health"
wait_health "$SCHED_URL/v1/scheduler/health"
wait_health "$EXEC_URL/v1/execution/health"
wait_health "$APPROVAL_URL/v1/approval/health"
wait_health "$ORCH_URL/v1/orchestrator/health"

"$RUNNER" \
  --umbrella-root "$ROOT" \
  --plan "$PLAN" \
  --run-id "$RUN_ID" \
  --policy-url "$POLICY_URL" \
  --lifecycle-url "$LIFECYCLE_URL" \
  --router-url "$ROUTER_URL" \
  --scheduler-url "$SCHED_URL" \
  --execution-url "$EXEC_URL" \
  --approval-url "$APPROVAL_URL" \
  --orchestrator-url "$ORCH_URL" \
  --reconcile-cmd "$RECON_STUB" \
  >/tmp/umbrella04-runner.out

ROOT="$ROOT" RUN_ID="$RUN_ID" python3 - <<'PY'
import json, os
from pathlib import Path
root=Path(os.environ['ROOT'])
run_id=os.environ['RUN_ID']
run_dir=root/'control-plane'/'observability'/'runs'/run_id
summary=json.loads((run_dir/'summary.json').read_text())
run=json.loads((run_dir/'run.json').read_text())
assert summary['state']=='SUCCEEDED', summary
assert summary['terminalReason']=='all_steps_succeeded', summary
assert summary['stepCount']==2 and summary['completedSteps']==2, summary
steps={s['stepId']:s for s in run.get('steps',[])}
assert steps['prepare-smoke-a']['status']=='SUCCESS', steps
assert steps['prepare-smoke-b']['status']=='SUCCESS', steps
out_a=(steps['prepare-smoke-a'].get('result',{}).get('result',{}).get('stdout',''))
out_b=(steps['prepare-smoke-b'].get('result',{}).get('result',{}).get('stdout',''))
assert 'service-mesh-a' in out_a, out_a
assert 'service-mesh-b' in out_b, out_b
print('umbrella0.4 service mesh runner PASS')
PY

echo "umbrella0.4 service mesh runner contract PASS"
