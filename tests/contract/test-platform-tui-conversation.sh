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

python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-tuiconv-policy.out" 2>"$ROOT/tmp/umbrella04-tuiconv-policy.err" &
P1=$!
python3 "$ROOT/services/catalog/app.py" --host 127.0.0.1 --port "$CATALOG_PORT" --umbrella-root "$ROOT" --registry "$ROOT/tmp/platform-tui-conversation-catalog.json" >"$ROOT/tmp/umbrella04-tuiconv-catalog.out" 2>"$ROOT/tmp/umbrella04-tuiconv-catalog.err" &
P2=$!
python3 "$ROOT/services/plugin_host/app.py" --host 127.0.0.1 --port "$PLUGIN_HOST_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-tuiconv-plugin-host.out" 2>"$ROOT/tmp/umbrella04-tuiconv-plugin-host.err" &
P3=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" --policy-url "$POLICY_URL" --catalog-url "$CATALOG_URL" --plugin-host-url "$PLUGIN_HOST_URL" >"$ROOT/tmp/umbrella04-tuiconv-execution.out" 2>"$ROOT/tmp/umbrella04-tuiconv-execution.err" &
P4=$!
python3 "$ROOT/services/session/app.py" --host 127.0.0.1 --port "$SESSION_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" --execution-url "$EXEC_URL" >"$ROOT/tmp/umbrella04-tuiconv-session.out" 2>"$ROOT/tmp/umbrella04-tuiconv-session.err" &
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
import json, sys, urllib.request
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

wait_health "$POLICY_URL/v1/policy/health"
wait_health "$CATALOG_URL/v1/catalog/health"
wait_health "$PLUGIN_HOST_URL/v1/plugin-host/health"
wait_health "$EXEC_URL/v1/execution/health"
wait_health "$SESSION_URL/v1/session/health"

python3 - "$ROOT" "$POLICY_URL" "$CATALOG_URL" "$PLUGIN_HOST_URL" "$EXEC_URL" "$SESSION_URL" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
policy_url, catalog_url, plugin_host_url, exec_url, session_url = sys.argv[2:]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from services.tui.client import TuiClient

client = TuiClient(root=root)
client.default_urls['policy'] = policy_url
client.default_urls['catalog'] = catalog_url
client.default_urls['plugin-host'] = plugin_host_url
client.default_urls['execution'] = exec_url
client.default_urls['session'] = session_url

created = client.create_session(agent_id='mayor', title='TUI Conversation')
session = created.get('session') or {}
session_id = session.get('sessionId')
assert session_id, created

originated = client._request(
    'POST',
    f"{session_url}/v1/sessions/{session_id}/originations",
    {
        'originatorAgentId': 'originator',
        'agentId': 'programming-agent-1',
        'shopId': 'development-shop',
        'agentPackageId': 'umbrella.programming-agent.v1',
    },
)['json']
assert originated.get('agent', {}).get('agentPackageId') == 'umbrella.programming-agent.v1', originated

mayor = client.converse(session_id=session_id, target='mayor', content='fact:mayor')
assert mayor.get('ok') is True, mayor
assert 'Mayor summary for "fact:mayor"' in mayor.get('reply', ''), mayor
delegations = mayor.get('delegations') or []
assert len(delegations) == 1, mayor
assert delegations[0].get('shopId') == 'development-shop', mayor

worker = client.converse(session_id=session_id, target='programming-agent-1', content='fact:worker')
assert worker.get('ok') is True, worker
assert 'fact:worker' in worker.get('reply', ''), worker

fetched = client.get_session(session_id)
session_payload = fetched.get('session') if isinstance(fetched.get('session'), dict) else fetched
messages = session_payload.get('messages') or []
contents = [str(msg.get('content', '')) for msg in messages]
assert any('Mayor summary for "fact:mayor"' in content for content in contents), messages
assert any('fact:worker' in content for content in contents), messages
print('platform tui conversation PASS')
PY

echo "umbrella0.4 platform tui conversation PASS"
