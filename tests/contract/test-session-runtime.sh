#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$ROOT/tmp"

free_port() {
  python3 - <<'PY'
import socket
s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()
PY
}

POLICY_PORT="$(free_port)"
CATALOG_PORT="$(free_port)"
PLUGIN_HOST_PORT="$(free_port)"
EXEC_PORT="$(free_port)"
SESSION_PORT="$(free_port)"
POLICY_URL="http://127.0.0.1:$POLICY_PORT"
CATALOG_URL="http://127.0.0.1:$CATALOG_PORT"
PLUGIN_HOST_URL="http://127.0.0.1:$PLUGIN_HOST_PORT"
EXEC_URL="http://127.0.0.1:$EXEC_PORT"
SESSION_URL="http://127.0.0.1:$SESSION_PORT"
REGISTRY_PATH="$ROOT/tmp/session-catalog-registry.json"
AGENT_ID="session-agent-$(date +%s)"

rm -f "$REGISTRY_PATH"

python3 "$ROOT/services/catalog/app.py" --host 127.0.0.1 --port "$CATALOG_PORT" --umbrella-root "$ROOT" --registry "$REGISTRY_PATH" >"$ROOT/tmp/umbrella04-sr-catalog.out" 2>"$ROOT/tmp/umbrella04-sr-catalog.err" &
P1=$!
python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-sr-policy.out" 2>"$ROOT/tmp/umbrella04-sr-policy.err" &
P2=$!
python3 "$ROOT/services/plugin_host/app.py" --host 127.0.0.1 --port "$PLUGIN_HOST_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-sr-host.out" 2>"$ROOT/tmp/umbrella04-sr-host.err" &
P3=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" --policy-url "$POLICY_URL" --catalog-url "$CATALOG_URL" --plugin-host-url "$PLUGIN_HOST_URL" >"$ROOT/tmp/umbrella04-sr-exec.out" 2>"$ROOT/tmp/umbrella04-sr-exec.err" &
P4=$!
python3 "$ROOT/services/session/app.py" --host 127.0.0.1 --port "$SESSION_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" --execution-url "$EXEC_URL" >"$ROOT/tmp/umbrella04-sr-session.out" 2>"$ROOT/tmp/umbrella04-sr-session.err" &
P5=$!

cleanup() {
  kill "$P1" "$P2" "$P3" "$P4" "$P5" >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_health() {
  local url="$1"
  local attempts=30
  local i=1
  while [[ "$i" -le "$attempts" ]]; do
    if python3 - "$url" <<'PY'
import json, sys, urllib.error, urllib.request
url=sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=1.5) as r:
        data=json.loads(r.read().decode('utf-8'))
    raise SystemExit(0 if data.get('status') == 'ok' else 1)
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

wait_health "$CATALOG_URL/v1/catalog/health"
wait_health "$POLICY_URL/v1/policy/health"
wait_health "$PLUGIN_HOST_URL/v1/plugin-host/health"
wait_health "$EXEC_URL/v1/execution/health"
wait_health "$SESSION_URL/v1/session/health"

python3 - "$POLICY_URL" "$AGENT_ID" <<'PY'
import json, sys, urllib.request
policy_url, agent_id = sys.argv[1], sys.argv[2]
for current_agent in [agent_id, 'originator', 'programming-agent', 'research-agent']:
    payload = {'agentId': current_agent, 'source': 'external', 'capabilities': ['knowledge.read']}
    req = urllib.request.Request(policy_url + '/v1/policy/agents/register', method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        out = json.loads(resp.read().decode('utf-8'))
    assert out.get('ok') is True, out
PY

python3 - "$SESSION_URL" "$ROOT" "$AGENT_ID" <<'PY'
import json, sys, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

session_url, root, agent_id = sys.argv[1], Path(sys.argv[2]), sys.argv[3]

profile_req = urllib.request.Request(
    session_url + '/v1/shop-profiles',
    method='POST',
    data=json.dumps({
        'profileId': 'development-shop-profile',
        'name': 'Development Shop Profile',
        'shopType': 'business',
        'defaultTitle': 'Programming Agent',
        'defaultShopName': 'Development Shop',
        'enabledActionIds': ['skill.memory.summarize'],
        'metadata': {'business': 'development-shop'},
    }).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(profile_req, timeout=20) as resp:
    profile_out = json.loads(resp.read().decode('utf-8'))
assert profile_out.get('profile', {}).get('profileId') == 'development-shop-profile', profile_out

research_profile_req = urllib.request.Request(
    session_url + '/v1/shop-profiles',
    method='POST',
    data=json.dumps({
        'profileId': 'research-office-profile',
        'name': 'Research Office Profile',
        'shopType': 'business',
        'defaultTitle': 'Research Agent',
        'defaultShopName': 'Research Office',
        'enabledActionIds': [],
        'metadata': {'business': 'research-office'},
    }).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(research_profile_req, timeout=20) as resp:
    research_profile_out = json.loads(resp.read().decode('utf-8'))
assert research_profile_out.get('profile', {}).get('profileId') == 'research-office-profile', research_profile_out

list_profiles_req = urllib.request.Request(session_url + '/v1/shop-profiles', method='GET')
with urllib.request.urlopen(list_profiles_req, timeout=20) as resp:
    listed_profiles = json.loads(resp.read().decode('utf-8'))
assert len(listed_profiles.get('profiles', [])) >= 2, listed_profiles

create_req = urllib.request.Request(session_url + '/v1/sessions', method='POST', data=json.dumps({'agentId': agent_id, 'title': 'Town runtime test', 'metadata': {'townHallName': 'Town Hall', 'heartbeatTtlSec': 300}}).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(create_req, timeout=20) as resp:
    created = json.loads(resp.read().decode('utf-8'))
session = created.get('session') or {}
session_id = session.get('sessionId')
assert session_id, created
assert session.get('mayorAgentId') == agent_id, session
assert session.get('heartbeatStatus') == 'healthy', session
assert session.get('heartbeatTtlSec') == 300, session
assert 'town-hall' in (session.get('shops') or {}), session
assert 'originator-studio' in (session.get('shops') or {}), session
assert any(agent.get('role') == 'originator' for agent in session.get('agents', [])), session
assert any(a.get('id') == 'skill.memory.summarize' for a in session.get('availableActions', [])), session
assert session.get('shops', {}).get('town-hall', {}).get('heartbeatStatus') == 'healthy', session

session_path = root / 'control-plane' / 'observability' / 'sessions' / session_id / 'session.json'
assert session_path.exists(), session_path
session_data = json.loads(session_path.read_text(encoding='utf-8'))
stale_time = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
session_data['lastHeartbeatAt'] = stale_time
session_path.write_text(json.dumps(session_data, indent=2) + '\n', encoding='utf-8')
stale_req = urllib.request.Request(session_url + f'/v1/sessions/{session_id}', method='GET')
with urllib.request.urlopen(stale_req, timeout=20) as resp:
    stale_fetched = json.loads(resp.read().decode('utf-8'))
assert stale_fetched.get('heartbeatStatus') == 'stale', stale_fetched

session_data = json.loads(session_path.read_text(encoding='utf-8'))
expired_time = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
session_data['lastHeartbeatAt'] = expired_time
session_path.write_text(json.dumps(session_data, indent=2) + '\n', encoding='utf-8')
expired_req = urllib.request.Request(session_url + f'/v1/sessions/{session_id}', method='GET')
with urllib.request.urlopen(expired_req, timeout=20) as resp:
    expired_fetched = json.loads(resp.read().decode('utf-8'))
assert expired_fetched.get('heartbeatStatus') == 'expired', expired_fetched

expired_turn_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/turns',
    method='POST',
    data=json.dumps({'objective': 'Should fail while heartbeat expired', 'requestedBy': 'user'}).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
try:
    with urllib.request.urlopen(expired_turn_req, timeout=20) as resp:
        expired_turn_out = json.loads(resp.read().decode('utf-8'))
    raise AssertionError(expired_turn_out)
except urllib.error.HTTPError as exc:
    expired_turn_out = json.loads(exc.read().decode('utf-8'))
assert 'heartbeat expired' in (((expired_turn_out.get('error') or {}).get('message')) or ''), expired_turn_out

heartbeat_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/heartbeat',
    method='POST',
    data=json.dumps({'seenBy': 'mayor'}).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(heartbeat_req, timeout=20) as resp:
    heartbeat_out = json.loads(resp.read().decode('utf-8'))
assert heartbeat_out.get('ok') is True, heartbeat_out
assert heartbeat_out.get('session', {}).get('heartbeatStatus') == 'healthy', heartbeat_out
assert heartbeat_out.get('session', {}).get('lastSeenBy') == 'mayor', heartbeat_out

worker_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/originations',
    method='POST',
    data=json.dumps({
        'originatorAgentId': 'originator',
        'agentId': 'programming-agent',
        'role': 'programmer',
        'shopId': 'development-shop',
        'shopProfileId': 'development-shop-profile',
    }).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(worker_req, timeout=20) as resp:
    worker_out = json.loads(resp.read().decode('utf-8'))
assert worker_out.get('shop', {}).get('shopId') == 'development-shop', worker_out
assert worker_out.get('agent', {}).get('createdByAgentId') == 'originator', worker_out
assert worker_out.get('agent', {}).get('shopProfileId') == 'development-shop-profile', worker_out
assert worker_out.get('session', {}).get('shops', {}).get('development-shop', {}).get('heartbeatStatus') == 'healthy', worker_out
assert worker_out.get('shop', {}).get('shopProfileId') == 'development-shop-profile', worker_out
assert worker_out.get('shop', {}).get('name') == 'Development Shop', worker_out

research_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/originations',
    method='POST',
    data=json.dumps({
        'originatorAgentId': 'originator',
        'agentId': 'research-agent',
        'role': 'researcher',
        'shopId': 'research-office',
        'shopProfileId': 'research-office-profile',
    }).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(research_req, timeout=20) as resp:
    research_out = json.loads(resp.read().decode('utf-8'))
assert research_out.get('shop', {}).get('shopId') == 'research-office', research_out
assert research_out.get('shop', {}).get('enabledActionIds') == [], research_out
assert research_out.get('shop', {}).get('shopProfileId') == 'research-office-profile', research_out

agent_heartbeat_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/agents/research-agent/heartbeat',
    method='POST',
    data=json.dumps({'seenBy': 'worker'}).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(agent_heartbeat_req, timeout=20) as resp:
    agent_heartbeat_out = json.loads(resp.read().decode('utf-8'))
assert agent_heartbeat_out.get('ok') is True, agent_heartbeat_out
assert agent_heartbeat_out.get('agent', {}).get('heartbeatStatus') == 'healthy', agent_heartbeat_out
assert agent_heartbeat_out.get('agent', {}).get('lastSeenBy') == 'worker', agent_heartbeat_out
assert agent_heartbeat_out.get('shop', {}).get('shopId') == 'research-office', agent_heartbeat_out
assert agent_heartbeat_out.get('shop', {}).get('heartbeatStatus') == 'healthy', agent_heartbeat_out

sub_agent_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/sub-agents',
    method='POST',
    data=json.dumps({
        'createdByAgentId': 'originator',
        'subAgentId': 'research-sub-agent',
        'shopProfileId': 'research-office-profile',
        'assignmentPolicy': {'maxConcurrentAssignments': 1},
        'metadata': {'desk': 'research'},
    }).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(sub_agent_req, timeout=20) as resp:
    sub_agent_out = json.loads(resp.read().decode('utf-8'))
assert sub_agent_out.get('subAgent', {}).get('subAgentId') == 'research-sub-agent', sub_agent_out
assert sub_agent_out.get('subAgent', {}).get('agentId') == 'research-agent', sub_agent_out
assert sub_agent_out.get('subAgent', {}).get('shopId') == 'research-office', sub_agent_out
assert sub_agent_out.get('subAgent', {}).get('state') == 'idle', sub_agent_out
assert sub_agent_out.get('subAgent', {}).get('assignmentPolicy', {}).get('maxConcurrentAssignments') == 1, sub_agent_out

list_sub_agents_req = urllib.request.Request(session_url + f'/v1/sessions/{session_id}/sub-agents', method='GET')
with urllib.request.urlopen(list_sub_agents_req, timeout=20) as resp:
    list_sub_agents_out = json.loads(resp.read().decode('utf-8'))
assert len(list_sub_agents_out.get('subAgents', [])) == 1, list_sub_agents_out

sub_agent_heartbeat_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/sub-agents/research-sub-agent/heartbeat',
    method='POST',
    data=json.dumps({'seenBy': 'worker'}).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(sub_agent_heartbeat_req, timeout=20) as resp:
    sub_agent_heartbeat_out = json.loads(resp.read().decode('utf-8'))
assert sub_agent_heartbeat_out.get('subAgent', {}).get('heartbeatStatus') == 'healthy', sub_agent_heartbeat_out
assert sub_agent_heartbeat_out.get('subAgent', {}).get('lastSeenBy') == 'worker', sub_agent_heartbeat_out

govern_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/shops/research-office/actions/enable',
    method='POST',
    data=json.dumps({
        'managedByAgentId': 'originator',
        'actionId': 'skill.memory.summarize',
        'installed': True,
        'metadata': {'source': 'originator-governance'},
    }).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(govern_req, timeout=20) as resp:
    govern_out = json.loads(resp.read().decode('utf-8'))
assert govern_out.get('actionState', {}).get('enabled') is True, govern_out
assert govern_out.get('actionState', {}).get('managedByAgentId') == 'originator', govern_out
assert 'skill.memory.summarize' in (govern_out.get('shop', {}).get('effectiveEnabledActionIds') or []), govern_out

assignment_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/assignments',
    method='POST',
    data=json.dumps({
        'createdByAgentId': 'originator',
        'subAgentId': 'research-sub-agent',
        'objective': 'Research fact 999',
        'actionId': 'skill.memory.summarize',
        'inputs': {'nodeId': 'fact:999'},
        'metadata': {'assignmentKind': 'research-pass'},
    }).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(assignment_req, timeout=20) as resp:
    assignment_out = json.loads(resp.read().decode('utf-8'))
assert assignment_out.get('ok') is True, assignment_out
assignment = assignment_out.get('assignment') or {}
assert assignment.get('status') == 'completed', assignment_out
assert assignment.get('subAgentId') == 'research-sub-agent', assignment_out
assert assignment.get('resultRefs', {}).get('turnId'), assignment_out
assert assignment.get('resultRefs', {}).get('delegationId'), assignment_out
assert assignment.get('resultRefs', {}).get('invocationId'), assignment_out
assert assignment_out.get('subAgent', {}).get('state') == 'completed', assignment_out
assert assignment_out.get('delegation', {}).get('shopId') == 'research-office', assignment_out
assert assignment_out.get('invocation', {}).get('inputs', {}).get('nodeId') == 'fact:999', assignment_out

assignment_get_req = urllib.request.Request(
    session_url + f"/v1/sessions/{session_id}/assignments/{assignment.get('assignmentId')}",
    method='GET',
)
with urllib.request.urlopen(assignment_get_req, timeout=20) as resp:
    assignment_get_out = json.loads(resp.read().decode('utf-8'))
assert assignment_get_out.get('assignment', {}).get('assignmentId') == assignment.get('assignmentId'), assignment_get_out

turn_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/turns',
    method='POST',
    data=json.dumps({'objective': 'Have research feed development for fact 123', 'requestedBy': 'user'}).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(turn_req, timeout=20) as resp:
    turn_out = json.loads(resp.read().decode('utf-8'))
turn = turn_out.get('turn') or {}
turn_id = turn.get('turnId')
assert turn_id, turn_out

message_req = urllib.request.Request(session_url + f'/v1/sessions/{session_id}/messages', method='POST', data=json.dumps({'role': 'user', 'content': 'Summarize fact 123'}).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(message_req, timeout=20) as resp:
    message_out = json.loads(resp.read().decode('utf-8'))
assert message_out.get('message', {}).get('role') == 'user', message_out

orchestrate_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/orchestrate-turn',
    method='POST',
    data=json.dumps({
        'turnId': turn_id,
        'plan': [
            {'planStepId': 'research-pass', 'role': 'researcher', 'actionId': 'skill.memory.summarize', 'inputs': {'nodeId': 'fact:123'}},
            {
                'planStepId': 'development-pass',
                'dependsOn': ['research-pass'],
                'shopProfileId': 'development-shop-profile',
                'actionId': 'skill.memory.summarize',
                'inputBindings': [
                    {'inputKey': 'nodeId', 'fromStepId': 'research-pass', 'resultPath': 'result.result.pluginResult.summary'}
                ],
            },
        ],
    }).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(orchestrate_req, timeout=20) as resp:
    orchestrate_out = json.loads(resp.read().decode('utf-8'))
assert orchestrate_out.get('ok') is True, orchestrate_out
delegations = orchestrate_out.get('delegations') or []
assert len(delegations) == 2, orchestrate_out
assert {item.get('shopId') for item in delegations} == {'development-shop', 'research-office'}, orchestrate_out
assert {item.get('state') for item in delegations} == {'COMPLETED'}, orchestrate_out
assert {item.get('requestedShopProfileId') for item in delegations if item.get('requestedShopProfileId')} == {'development-shop-profile'}, orchestrate_out
assert {item.get('requestedRole') for item in delegations if item.get('requestedRole')} == {'researcher'}, orchestrate_out
assert {item.get('planStepId') for item in delegations} == {'research-pass', 'development-pass'}, orchestrate_out
invocations = orchestrate_out.get('invocations') or []
assert len(invocations) == 2, orchestrate_out
invocation_by_step = {((item.get('result') or {}).get('stepSpec') or {}).get('metadata', {}).get('planStepId'): item for item in invocations}
research_invocation = next(item for item in invocations if item.get('delegationId') and item.get('inputs', {}).get('nodeId') == 'fact:123')
research_result = ((research_invocation.get('result') or {}).get('result') or {}).get('pluginResult') or {}
assert research_result.get('summary') == 'example summary for fact:123', orchestrate_out
development_invocation = next(item for item in invocations if item.get('inputs', {}).get('nodeId') == 'example summary for fact:123')
development_result = ((development_invocation.get('result') or {}).get('result') or {}).get('pluginResult') or {}
assert development_result.get('summary') == 'example summary for example summary for fact:123', orchestrate_out
reconciliation = orchestrate_out.get('reconciliation') or {}
assert reconciliation.get('completedDelegations') == 2, orchestrate_out
assert reconciliation.get('skippedDelegations') == 0, orchestrate_out
assert 'Mayor summary for "Have research feed development for fact 123"' in reconciliation.get('summary', ''), orchestrate_out

failure_turn_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/turns',
    method='POST',
    data=json.dumps({'objective': 'Handle upstream failure with skip policy', 'requestedBy': 'user'}).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(failure_turn_req, timeout=20) as resp:
    failure_turn_out = json.loads(resp.read().decode('utf-8'))
failure_turn = failure_turn_out.get('turn') or {}
failure_turn_id = failure_turn.get('turnId')
assert failure_turn_id, failure_turn_out

failure_orchestrate_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/orchestrate-turn',
    method='POST',
    data=json.dumps({
        'turnId': failure_turn_id,
        'plan': [
            {'planStepId': 'research-fail', 'role': 'researcher', 'actionId': 'skill.memory.summarize', 'inputs': {'nodeId': 'fail:123'}},
            {
                'planStepId': 'development-skipped',
                'dependsOn': ['research-fail'],
                'shopProfileId': 'development-shop-profile',
                'actionId': 'skill.memory.summarize',
                'onDependencyFailure': 'skip',
                'inputBindings': [
                    {'inputKey': 'nodeId', 'fromStepId': 'research-fail', 'resultPath': 'result.result.pluginResult.summary'}
                ],
            },
        ],
    }).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(failure_orchestrate_req, timeout=20) as resp:
    failure_orchestrate_out = json.loads(resp.read().decode('utf-8'))
assert failure_orchestrate_out.get('ok') is False, failure_orchestrate_out
failure_delegations = failure_orchestrate_out.get('delegations') or []
assert len(failure_delegations) == 2, failure_orchestrate_out
failed_step = next(item for item in failure_delegations if item.get('planStepId') == 'research-fail')
skipped_step = next(item for item in failure_delegations if item.get('planStepId') == 'development-skipped')
assert failed_step.get('state') == 'FAILED', failure_orchestrate_out
assert skipped_step.get('state') == 'SKIPPED', failure_orchestrate_out
assert skipped_step.get('failurePolicy') == 'skip', failure_orchestrate_out
assert 'blocked by dependency state' in skipped_step.get('skipReason', ''), failure_orchestrate_out
failure_reconciliation = failure_orchestrate_out.get('reconciliation') or {}
assert failure_reconciliation.get('failedDelegations') == 1, failure_orchestrate_out
assert failure_reconciliation.get('skippedDelegations') == 1, failure_orchestrate_out

retry_turn_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/turns',
    method='POST',
    data=json.dumps({'objective': 'Retry flaky research before development', 'requestedBy': 'user'}).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(retry_turn_req, timeout=20) as resp:
    retry_turn_out = json.loads(resp.read().decode('utf-8'))
retry_turn = retry_turn_out.get('turn') or {}
retry_turn_id = retry_turn.get('turnId')
assert retry_turn_id, retry_turn_out

retry_orchestrate_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/orchestrate-turn',
    method='POST',
    data=json.dumps({
        'turnId': retry_turn_id,
        'metadata': {'retryBudget': 1, 'onDependencyFailure': 'skip'},
        'plan': [
            {'planStepId': 'flaky-research', 'role': 'researcher', 'actionId': 'skill.memory.summarize', 'inputs': {'nodeId': 'flaky:123'}},
            {
                'planStepId': 'development-after-retry',
                'dependsOn': ['flaky-research'],
                'shopProfileId': 'development-shop-profile',
                'actionId': 'skill.memory.summarize',
                'inputBindings': [
                    {'inputKey': 'nodeId', 'fromStepId': 'flaky-research', 'resultPath': 'result.result.pluginResult.summary'}
                ],
            },
        ],
    }).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(retry_orchestrate_req, timeout=20) as resp:
    retry_orchestrate_out = json.loads(resp.read().decode('utf-8'))
assert retry_orchestrate_out.get('ok') is False, retry_orchestrate_out
retry_delegations = retry_orchestrate_out.get('delegations') or []
assert len(retry_delegations) == 3, retry_orchestrate_out
flaky_attempts = [item for item in retry_delegations if item.get('planStepId') == 'flaky-research']
assert len(flaky_attempts) == 2, retry_orchestrate_out
assert {item.get('state') for item in flaky_attempts} == {'FAILED', 'COMPLETED'}, retry_orchestrate_out
assert {item.get('metadata', {}).get('attemptNumber') for item in flaky_attempts} == {1, 2}, retry_orchestrate_out
retry_reconciliation = retry_orchestrate_out.get('reconciliation') or {}
assert retry_reconciliation.get('completedDelegations') == 2, retry_orchestrate_out
assert retry_reconciliation.get('failedDelegations') == 1, retry_orchestrate_out
assert retry_orchestrate_out.get('turn', {}).get('state') == 'PARTIAL', retry_orchestrate_out
retry_invocations = retry_orchestrate_out.get('invocations') or []
assert len(retry_invocations) == 3, retry_orchestrate_out
successful_flaky = next(item for item in retry_invocations if item.get('inputs', {}).get('nodeId') == 'flaky:123' and ((item.get('result') or {}).get('ok') is True))
assert ((successful_flaky.get('result') or {}).get('result') or {}).get('pluginResult', {}).get('summary') == 'example summary for flaky:123', retry_orchestrate_out
downstream_retry = next(item for item in retry_invocations if item.get('inputs', {}).get('nodeId') == 'example summary for flaky:123')
assert ((downstream_retry.get('result') or {}).get('result') or {}).get('pluginResult', {}).get('summary') == 'example summary for example summary for flaky:123', retry_orchestrate_out

compact_req = urllib.request.Request(
    session_url + f'/v1/sessions/{session_id}/compact',
    method='POST',
    data=json.dumps({'keepLastMessages': 2}).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
with urllib.request.urlopen(compact_req, timeout=20) as resp:
    compact_out = json.loads(resp.read().decode('utf-8'))
assert compact_out.get('compaction', {}).get('keptMessages') == 2, compact_out

get_req = urllib.request.Request(session_url + f'/v1/sessions/{session_id}', method='GET')
with urllib.request.urlopen(get_req, timeout=20) as resp:
    fetched = json.loads(resp.read().decode('utf-8'))
assert len(fetched.get('messages', [])) <= 2, fetched
assert len(fetched.get('invocations', [])) == 7, fetched
assert len(fetched.get('turns', [])) == 4, fetched
assert len(fetched.get('delegations', [])) == 8, fetched
assert len(fetched.get('compactions', [])) == 1, fetched
assert len(fetched.get('subAgents', [])) == 1, fetched
assert len(fetched.get('assignments', [])) == 1, fetched
assert fetched.get('assignments', [])[0].get('status') == 'completed', fetched
assert fetched.get('assignments', [])[0].get('resultRefs', {}).get('delegationId'), fetched
assert fetched.get('turns', [])[0].get('state') == 'COMPLETED', fetched
assert fetched.get('turns', [])[0].get('orchestrationMode') == 'manual', fetched
assert fetched.get('turns', [])[1].get('state') == 'COMPLETED', fetched
assert fetched.get('turns', [])[1].get('orchestrationMode') == 'dependency-graph', fetched
assert fetched.get('turns', [])[1].get('planStepOrder') == ['research-pass', 'development-pass'], fetched
assert fetched.get('turns', [])[1].get('reconciliation', {}).get('completedDelegations') == 2, fetched
assert fetched.get('turns', [])[2].get('state') == 'FAILED', fetched
assert fetched.get('turns', [])[2].get('reconciliation', {}).get('skippedDelegations') == 1, fetched
assert fetched.get('turns', [])[3].get('state') == 'PARTIAL', fetched
assert fetched.get('turns', [])[3].get('orchestrationMetadata', {}).get('retryBudget') == 1, fetched
assert 'development-shop' in (fetched.get('shopActions') or {}), fetched
assert 'research-office' in (fetched.get('shopActions') or {}), fetched
assert fetched.get('shops', {}).get('research-office', {}).get('actionGovernance', {}).get('skill.memory.summarize', {}).get('managedByAgentId') == 'originator', fetched
assert fetched.get('shops', {}).get('development-shop', {}).get('shopProfileId') == 'development-shop-profile', fetched
assert fetched.get('subAgents', [])[0].get('shopId') == 'research-office', fetched

assert session_path.exists(), session_path
profile_path = root / 'control-plane' / 'observability' / 'session-profiles' / 'profiles.json'
assert profile_path.exists(), profile_path
print('session runtime PASS')
PY

echo "umbrella0.4 session runtime contract PASS"
