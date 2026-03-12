#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUNNER="$ROOT/scripts/control-plane/run-umbrella-control-plane"

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

RECON_STUB="$ROOT/tmp/reconcile-ok-approval.sh"
mkdir -p "$ROOT/tmp"
cat > "$RECON_STUB" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
echo '{"ok":true}'
exit 0
SH
chmod +x "$RECON_STUB"

PLAN="$ROOT/tmp/approval-authority-smoke.plan.json"
APPROVAL_KEY="umbrella04-approval-authority-key"
RUN_ID="run-umbrella04-approval-authority-$(date +%s)"
cat > "$PLAN" <<JSON
{
  "id": "umbrella.plan.approval-authority-smoke.v1",
  "steps": [
    {
      "stepId": "approval-gated-step",
      "objective": "Must be approved via approval-service before execution",
      "command": "python3 -c 'print(\"approval-authority-ok\")'",
      "workdir": ".",
      "timeoutSec": 30,
      "timeoutClass": "short",
      "riskClass": "low",
      "requiresApproval": true,
      "approvalKey": "$APPROVAL_KEY"
    }
  ]
}
JSON

python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-appr-policy.out" 2>"$ROOT/tmp/umbrella04-appr-policy.err" &
P1=$!
python3 "$ROOT/services/lifecycle/app.py" --host 127.0.0.1 --port "$LIFECYCLE_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-appr-lifecycle.out" 2>"$ROOT/tmp/umbrella04-appr-lifecycle.err" &
P2=$!
python3 "$ROOT/services/router/app.py" --host 127.0.0.1 --port "$ROUTER_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-appr-router.out" 2>"$ROOT/tmp/umbrella04-appr-router.err" &
P3=$!
python3 "$ROOT/services/scheduler/app.py" --host 127.0.0.1 --port "$SCHED_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-appr-scheduler.out" 2>"$ROOT/tmp/umbrella04-appr-scheduler.err" &
P4=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-appr-execution.out" 2>"$ROOT/tmp/umbrella04-appr-execution.err" &
P5=$!
python3 "$ROOT/services/approval/app.py" --host 127.0.0.1 --port "$APPROVAL_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-appr-approval.out" 2>"$ROOT/tmp/umbrella04-appr-approval.err" &
P6=$!
python3 "$ROOT/services/orchestrator/app.py" --host 127.0.0.1 --port "$ORCH_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-appr-orchestrator.out" 2>"$ROOT/tmp/umbrella04-appr-orchestrator.err" &
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

set +e
"$RUNNER" \
  --umbrella-root "$ROOT" \
  --plan "tmp/$(basename "$PLAN")" \
  --run-id "$RUN_ID" \
  --policy-url "$POLICY_URL" \
  --lifecycle-url "$LIFECYCLE_URL" \
  --router-url "$ROUTER_URL" \
  --scheduler-url "$SCHED_URL" \
  --execution-url "$EXEC_URL" \
  --approval-url "$APPROVAL_URL" \
  --orchestrator-url "$ORCH_URL" \
  --reconcile-cmd "$RECON_STUB" \
  >"$ROOT/tmp/umbrella04-approval-authority-run1.out"
RC1=$?
set -e
if [[ "$RC1" -ne 3 ]]; then
  echo "expected first run to block for approval (exit 3), got $RC1"
  exit 1
fi

python3 - "$APPROVAL_URL" "$RUN_ID" "$APPROVAL_KEY" <<'PY'
import json, sys, urllib.request
base, run_id, approval_key = sys.argv[1], sys.argv[2], sys.argv[3]

def req(method, path, payload=None):
    data=None
    headers={}
    if payload is not None:
        data=json.dumps(payload).encode('utf-8')
        headers['Content-Type']='application/json'
    r=urllib.request.Request(base+path, method=method, data=data, headers=headers)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))

ap=req('GET', f'/v1/approval/{approval_key}')
assert ap['exists'] is True, ap
assert ap['approval']['status']=='PENDING', ap

approve=req('POST', f'/v1/approval/{approval_key}/approve', {'by':'qa','note':'approved for resume'})
assert approve['ok'] is True and approve['approval']['status']=='APPROVED', approve
PY

# patch generated payload placeholders and execute actual resume call
python3 - "$APPROVAL_URL" "$POLICY_URL" "$LIFECYCLE_URL" "$ROUTER_URL" "$SCHED_URL" "$EXEC_URL" "$ORCH_URL" "$RECON_STUB" "$RUN_ID" "$APPROVAL_KEY" <<'PY'
import json, sys, urllib.request
approval_url, policy_url, lifecycle_url, router_url, sched_url, exec_url, orch_url, recon_stub, run_id, approval_key = sys.argv[1:]
payload = {
  'plan': 'tmp/approval-authority-smoke.plan.json',
  'runId': run_id,
  'approvalKey': approval_key,
  'policyUrl': policy_url,
  'lifecycleUrl': lifecycle_url,
  'routerUrl': router_url,
  'schedulerUrl': sched_url,
  'executionUrl': exec_url,
  'approvalUrl': approval_url,
  'orchestratorUrl': orch_url,
  'reconcileCmd': recon_stub,
}
req=urllib.request.Request(approval_url+'/v1/approval/resume', method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=60) as resp:
    out=json.loads(resp.read().decode('utf-8'))
assert out['ok'] is True and out['exitCode']==0, out
print('approval resume API PASS')
PY

ROOT="$ROOT" RUN_ID="$RUN_ID" python3 - <<'PY'
import json, os
from pathlib import Path
root=Path(os.environ['ROOT'])
run_id=os.environ['RUN_ID']
summary=json.loads((root/'control-plane'/'observability'/'runs'/run_id/'summary.json').read_text())
run=json.loads((root/'control-plane'/'observability'/'runs'/run_id/'run.json').read_text())
assert summary['state']=='SUCCEEDED', summary
assert run['state']=='SUCCEEDED', run
step=run['steps'][0]
assert step['status']=='SUCCESS', step
print('approval authority runner PASS')
PY

echo "umbrella0.4 approval authority contract PASS"
