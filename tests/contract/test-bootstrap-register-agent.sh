#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/tests/contract/helpers/runtime-root.sh"
TEST_TMP="$(contract_make_tmpdir "$ROOT" bootstrap-register-agent)"
RUNTIME_ROOT="$(contract_make_runtime_root "$ROOT" bootstrap-register-agent-runtime)"
export UMBRELLA_RUNTIME_ROOT="$RUNTIME_ROOT"
MGR="$ROOT/scripts/control-plane/manage-service-mesh"
BOOTSTRAP="$ROOT/scripts/bootstrap/register-agent"
MANIFEST="$TEST_TMP/bootstrap-service-manifest.json"
OUT="$TEST_TMP/bootstrap-agent-config.json"
AGENT_ID="bootstrap-agent-$(date +%s)-$$"

rm -f "$MANIFEST" "$OUT"

cleanup() {
  "$MGR" shutdown --umbrella-root "$ROOT" --manifest "$MANIFEST" >/dev/null 2>&1 || true
  rm -rf "$RUNTIME_ROOT" "$TEST_TMP"
}
trap cleanup EXIT

"$MGR" bringup --umbrella-root "$ROOT" --manifest "$MANIFEST" >"$TEST_TMP/umbrella04-bootstrap-bringup.out"

"$BOOTSTRAP" \
  --umbrella-root "$ROOT" \
  --manifest "$MANIFEST" \
  --agent-id "$AGENT_ID" \
  --capability memory.write \
  --capability memory.read \
  --out "$OUT" \
  >"$TEST_TMP/umbrella04-bootstrap-register.out"

python3 - "$OUT" "$AGENT_ID" <<'PY'
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
agent_id=sys.argv[2]
obj=json.loads(p.read_text())
assert obj.get('ok') is True, obj
sig=obj.get('signature') or {}
assert sig.get('alg') == 'HMAC-SHA256', sig
assert isinstance(sig.get('value'), str) and len(sig['value']) == 64, sig
cfg=obj.get('config') or {}
assert cfg.get('version') == 'umbrella.agent-config.v1', cfg
agent=cfg.get('agent') or {}
assert agent.get('agentId') == agent_id, agent
caps=set(agent.get('capabilities') or [])
assert {'memory.write','memory.read'}.issubset(caps), caps
sv=cfg.get('services') or {}
for k in ['policyUrl','executionUrl','memoryCoreUrl','orchestratorUrl','approvalUrl']:
    assert str(sv.get(k,'')).startswith('http://127.0.0.1:'), (k,sv)
print('bootstrap config blob PASS')
PY

python3 - "$OUT" <<'PY'
import json, sys, urllib.request
from pathlib import Path
obj=json.loads(Path(sys.argv[1]).read_text())
cfg=obj['config']
policy=cfg['services']['policyUrl'].rstrip('/')
agent_id=cfg['agent']['agentId']
token=str((cfg.get('auth') or {}).get('bearerToken',''))
step={'action':'memoryWrite','metadata':{'agentId':agent_id}}
headers={'Content-Type':'application/json'}
if token:
    headers['Authorization']=f'Bearer {token}'
req=urllib.request.Request(policy+'/v1/policy/authorize-step', method='POST', data=json.dumps({'stepSpec':step}).encode('utf-8'), headers=headers)
with urllib.request.urlopen(req, timeout=30) as resp:
    out=json.loads(resp.read().decode('utf-8'))
assert out.get('allowed') is True and out.get('ok') is True, out
print('bootstrap policy authorization PASS')
PY

"$MGR" shutdown --umbrella-root "$ROOT" --manifest "$MANIFEST" >"$TEST_TMP/umbrella04-bootstrap-shutdown.out"

echo "umbrella0.4 bootstrap register-agent contract PASS"
