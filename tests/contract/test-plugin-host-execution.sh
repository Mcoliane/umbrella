#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$ROOT/tmp"
mkdir -p "$ROOT/control-plane/extensions"

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
POLICY_URL="http://127.0.0.1:$POLICY_PORT"
CATALOG_URL="http://127.0.0.1:$CATALOG_PORT"
PLUGIN_HOST_URL="http://127.0.0.1:$PLUGIN_HOST_PORT"
EXEC_URL="http://127.0.0.1:$EXEC_PORT"
REGISTRY_PATH="$ROOT/tmp/plugin-host-catalog-registry.json"

rm -f "$REGISTRY_PATH"
rm -rf "$ROOT/control-plane/extensions/contract.incompatible.plugin"

python3 "$ROOT/services/policy/app.py" --host 127.0.0.1 --port "$POLICY_PORT" --umbrella-root "$ROOT" >"$ROOT/tmp/umbrella04-phe-policy.out" 2>"$ROOT/tmp/umbrella04-phe-policy.err" &
P1=$!
python3 "$ROOT/services/catalog/app.py" --host 127.0.0.1 --port "$CATALOG_PORT" --umbrella-root "$ROOT" --registry "$REGISTRY_PATH" >"$ROOT/tmp/umbrella04-phe-catalog.out" 2>"$ROOT/tmp/umbrella04-phe-catalog.err" &
P2=$!
python3 "$ROOT/services/plugin_host/app.py" --host 127.0.0.1 --port "$PLUGIN_HOST_PORT" --umbrella-root "$ROOT" --catalog-url "$CATALOG_URL" >"$ROOT/tmp/umbrella04-phe-host.out" 2>"$ROOT/tmp/umbrella04-phe-host.err" &
P3=$!
python3 "$ROOT/services/execution/app.py" --host 127.0.0.1 --port "$EXEC_PORT" --umbrella-root "$ROOT" --policy-url "$POLICY_URL" --catalog-url "$CATALOG_URL" --plugin-host-url "$PLUGIN_HOST_URL" >"$ROOT/tmp/umbrella04-phe-exec.out" 2>"$ROOT/tmp/umbrella04-phe-exec.err" &
P4=$!

cleanup() {
  kill "$P1" "$P2" "$P3" "$P4" >/dev/null 2>&1 || true
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

PLUGIN_HOST_SECRET_SHOULD_NOT_LEAK="top-secret" python3 - "$EXEC_URL" "$PLUGIN_HOST_URL" "$ROOT" "$CATALOG_URL" <<'PY'
import hashlib, json, os, shutil, sys, urllib.error, urllib.request, zipfile
from pathlib import Path

exec_url, plugin_host_url, root, catalog_url = sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4]
payload = {
    'runId': 'plugin-run',
    'stepId': 'plugin-step',
    'stepSpec': {
        'stepId': 'plugin-step',
        'action': 'skill.memory.summarize',
        'inputs': {'nodeId': 'fact:123'},
        'timeoutSec': 10,
    },
}
req = urllib.request.Request(exec_url + '/v1/execution/submit-step-spec', method='POST', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=20) as resp:
    out = json.loads(resp.read().decode('utf-8'))
assert out.get('ok') is True, out
result = out.get('result') if isinstance(out.get('result'), dict) else {}
assert result.get('status') == 'SUCCESS', out
assert result.get('kind') == 'plugin', out
plugin_result = result.get('pluginResult') if isinstance(result.get('pluginResult'), dict) else {}
assert plugin_result.get('summary') == 'example summary for fact:123', out

env_req = urllib.request.Request(
    plugin_host_url + '/v1/plugin-host/invoke',
    method='POST',
    data=json.dumps({
        'actionId': 'skill.memory.summarize',
        'invocation': {
            'runId': 'env-run',
            'stepId': 'env-step',
            'agentId': 'programming-agent',
            'action': 'skill.memory.summarize',
            'inputs': {'nodeId': 'env-check'},
            'timeouts': {'timeoutSec': 5},
        },
    }).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(env_req, timeout=20) as resp:
    env_out = json.loads(resp.read().decode('utf-8'))
assert env_out.get('ok') is True, env_out
assert env_out.get('executionPolicy', {}).get('fs') == 'scratch-only', env_out
plugin_result = ((env_out.get('result') or {}).get('pluginResult')) or {}
env_payload = plugin_result.get('env') if isinstance(plugin_result.get('env'), dict) else {}
assert env_payload.get('runId') == 'env-run', env_out
assert env_payload.get('stepId') == 'env-step', env_out
assert env_payload.get('secretPresent') is False, env_out
assert env_payload.get('scratchDir') == env_out.get('scratchDir'), env_out

loud_req = urllib.request.Request(
    plugin_host_url + '/v1/plugin-host/invoke',
    method='POST',
    data=json.dumps({
        'actionId': 'skill.memory.summarize',
        'invocation': {
            'runId': 'loud-run',
            'stepId': 'loud-step',
            'agentId': 'programming-agent',
            'action': 'skill.memory.summarize',
            'inputs': {'nodeId': 'loud-output'},
            'timeouts': {'timeoutSec': 5},
        },
    }).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(loud_req, timeout=20) as resp:
    loud_out = json.loads(resp.read().decode('utf-8'))
assert loud_out.get('ok') is True, loud_out
loud_summary = (((loud_out.get('result') or {}).get('pluginResult')) or {}).get('summary', '')
assert len(loud_summary.encode('utf-8')) <= 512, loud_out

big_input_req = urllib.request.Request(
    plugin_host_url + '/v1/plugin-host/invoke',
    method='POST',
    data=json.dumps({
        'actionId': 'skill.memory.summarize',
        'invocation': {
            'runId': 'big-run',
            'stepId': 'big-step',
            'agentId': 'programming-agent',
            'action': 'skill.memory.summarize',
            'inputs': {'nodeId': 'X' * 5000},
            'timeouts': {'timeoutSec': 5},
        },
    }).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
try:
    with urllib.request.urlopen(big_input_req, timeout=20) as resp:
        big_input_out = json.loads(resp.read().decode('utf-8'))
    raise AssertionError(big_input_out)
except urllib.error.HTTPError as exc:
    big_input_out = json.loads(exc.read().decode('utf-8'))
assert 'maxInputBytes' in (((big_input_out.get('error') or {}).get('message')) or ''), big_input_out

bundle_dir = root / 'tmp' / 'plugin-host-incompatible-bundle'
bundle_dir.mkdir(parents=True, exist_ok=True)
(bundle_dir / 'bin').mkdir(exist_ok=True)
bundle_script = bundle_dir / 'bin' / 'echo-incompatible'
bundle_script.write_text('#!/usr/bin/env bash\nset -euo pipefail\necho "{\\"ok\\":true}"\n', encoding='utf-8')
bundle_manifest = bundle_dir / 'manifest.json'
bundle_manifest.write_text(json.dumps({
    'id': 'contract.incompatible.plugin',
    'name': 'Incompatible Plugin',
    'version': '1.0.0',
    'apiVersion': 'umbrella.catalog.manifest.v1',
    'kind': 'plugin',
    'runtime': 'shell',
    'entrypoint': 'bin/echo-incompatible',
    'defaultEnabled': True,
    'compatibility': {
        'umbrella': {'minVersion': '99.0.0'},
        'pluginHostRuntimes': ['shell'],
        'apiVersions': ['umbrella.catalog.manifest.v1'],
        'actionSchemaVersions': ['umbrella.catalog.action.v1'],
    },
    'executionPolicy': {
        'envAllowlist': [],
        'network': 'none',
        'fs': 'scratch-only',
        'maxRuntimeSec': 5,
        'maxOutputBytes': 512,
        'maxInputBytes': 1024,
        'isolationProfile': 'shell-restricted'
    },
    'actions': [{'id': 'plugin.incompatible.echo', 'title': 'Incompatible Echo', 'requiredCapabilities': []}],
}, indent=2) + '\n', encoding='utf-8')
(bundle_dir / 'CHECKSUMS.json').write_text(json.dumps({
    'files': {
        'manifest.json': hashlib.sha256(bundle_manifest.read_bytes()).hexdigest(),
        'bin/echo-incompatible': hashlib.sha256(bundle_script.read_bytes()).hexdigest(),
    }
}, indent=2) + '\n', encoding='utf-8')
bundle_zip = root / 'tmp' / 'plugin-host-incompatible-bundle.zip'
with zipfile.ZipFile(bundle_zip, 'w') as archive:
    archive.write(bundle_manifest, 'manifest.json')
    archive.write(bundle_script, 'bin/echo-incompatible')
    archive.write(bundle_dir / 'CHECKSUMS.json', 'CHECKSUMS.json')
install_req = urllib.request.Request(
    catalog_url + '/v1/catalog/install-bundle',
    method='POST',
    data=json.dumps({'bundlePath': str(bundle_zip)}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(install_req, timeout=20) as resp:
    install_out = json.loads(resp.read().decode('utf-8'))
assert install_out.get('item', {}).get('compatible', {}).get('ok') is False, install_out

incompatible_req = urllib.request.Request(
    plugin_host_url + '/v1/plugin-host/invoke',
    method='POST',
    data=json.dumps({
        'actionId': 'plugin.incompatible.echo',
        'invocation': {
            'runId': 'bad-run',
            'stepId': 'bad-step',
            'agentId': 'programming-agent',
            'action': 'plugin.incompatible.echo',
            'inputs': {},
            'timeouts': {'timeoutSec': 5},
        },
    }).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
try:
    with urllib.request.urlopen(incompatible_req, timeout=20) as resp:
        incompatible_out = json.loads(resp.read().decode('utf-8'))
    raise AssertionError(incompatible_out)
except urllib.error.HTTPError as exc:
    incompatible_out = json.loads(exc.read().decode('utf-8'))
assert 'incompatible' in (((incompatible_out.get('error') or {}).get('message')) or ''), incompatible_out

container_plugin_dir = root / 'tmp' / 'plugin-host-container-plugin'
container_plugin_dir.mkdir(parents=True, exist_ok=True)
(container_plugin_dir / 'bin').mkdir(exist_ok=True)
container_script = container_plugin_dir / 'bin' / 'container-entry'
container_script.write_text('#!/bin/sh\ncat >/dev/null\necho "{\\"ok\\":true,\\"source\\":\\"container\\"}"\n', encoding='utf-8')
container_manifest = container_plugin_dir / 'manifest.json'
container_manifest.write_text(json.dumps({
    'id': 'contract.container.plugin',
    'name': 'Container Plugin',
    'version': '0.1.0',
    'apiVersion': 'umbrella.catalog.manifest.v1',
    'kind': 'plugin',
    'runtime': 'container',
    'entrypoint': 'bin/container-entry',
    'defaultEnabled': True,
    'compatibility': {
        'umbrella': {'minVersion': '0.4.0'},
        'pluginHostRuntimes': ['container'],
        'apiVersions': ['umbrella.catalog.manifest.v1'],
        'actionSchemaVersions': ['umbrella.catalog.action.v1'],
    },
    'container': {
        'image': 'busybox:latest',
        'command': ['sh', '/plugin/bin/container-entry'],
    },
    'executionPolicy': {
        'envAllowlist': [],
        'network': 'none',
        'fs': 'scratch-only',
        'maxRuntimeSec': 5,
        'maxOutputBytes': 512,
        'maxInputBytes': 1024,
        'isolationProfile': 'container-restricted'
    },
    'actions': [{'id': 'plugin.container.echo', 'title': 'Container Echo', 'requiredCapabilities': []}],
}, indent=2) + '\n', encoding='utf-8')
install_container_req = urllib.request.Request(
    catalog_url + '/v1/catalog/install-local',
    method='POST',
    data=json.dumps({'manifestPath': str(container_manifest)}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(install_container_req, timeout=20) as resp:
    install_container_out = json.loads(resp.read().decode('utf-8'))
assert install_container_out.get('item', {}).get('runtime') == 'container', install_container_out

with urllib.request.urlopen(plugin_host_url + '/v1/plugin-host/health', timeout=20) as resp:
    host_health = json.loads(resp.read().decode('utf-8'))
container_runtime = host_health.get('containerRuntime')

container_req = urllib.request.Request(
    plugin_host_url + '/v1/plugin-host/invoke',
    method='POST',
    data=json.dumps({
        'actionId': 'plugin.container.echo',
        'invocation': {
            'runId': 'container-run',
            'stepId': 'container-step',
            'agentId': 'programming-agent',
            'action': 'plugin.container.echo',
            'inputs': {},
            'timeouts': {'timeoutSec': 5},
        },
    }).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
if container_runtime == 'unavailable':
    try:
        with urllib.request.urlopen(container_req, timeout=20) as resp:
            container_out = json.loads(resp.read().decode('utf-8'))
        raise AssertionError(container_out)
    except urllib.error.HTTPError as exc:
        container_out = json.loads(exc.read().decode('utf-8'))
    assert 'container runtime not available' in (((container_out.get('error') or {}).get('message')) or ''), container_out
else:
    with urllib.request.urlopen(container_req, timeout=20) as resp:
        container_out = json.loads(resp.read().decode('utf-8'))
    if container_out.get('ok') is True:
        plugin_result = ((container_out.get('result') or {}).get('pluginResult')) or {}
        assert plugin_result.get('source') == 'container', container_out
        assert container_out.get('command', [None])[0] == container_runtime, container_out
    else:
        assert container_out.get('failureReason') == 'execution_runtime_failed', container_out
        err_text = ((container_out.get('stderr') or '') + ' ' + ' '.join(container_out.get('command', []))).lower()
        assert container_runtime in err_text, container_out
print('plugin host execution PASS')
PY

echo "umbrella0.4 plugin host execution contract PASS"
