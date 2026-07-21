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

CATALOG_PORT="$(free_port)"
CATALOG_URL="http://127.0.0.1:$CATALOG_PORT"
REGISTRY_PATH="$ROOT/tmp/catalog-test-registry.json"
TRUSTED_KEY_DIR="$ROOT/tmp/catalog-trusted-keys"

rm -f "$REGISTRY_PATH"
rm -rf "$TRUSTED_KEY_DIR"
rm -rf "$ROOT/control-plane/extensions/contract.bundle.plugin"
rm -rf "$ROOT/tmp/catalog-local-plugin" "$ROOT/tmp/catalog-bundle-plugin" "$ROOT/tmp/catalog-bundle-plugin-v2" "$ROOT/tmp/catalog-bundle-plugin-bad"
rm -f "$ROOT/tmp/catalog-bundle-plugin.zip" "$ROOT/tmp/catalog-bundle-plugin-v2.zip" "$ROOT/tmp/catalog-bundle-plugin-bad.zip"

python3 "$ROOT/services/catalog/app.py" --host 127.0.0.1 --port "$CATALOG_PORT" --umbrella-root "$ROOT" --registry "$REGISTRY_PATH" --signature-mode require-signature --trusted-key-dir "$TRUSTED_KEY_DIR" >"$ROOT/tmp/umbrella04-catalog.out" 2>"$ROOT/tmp/umbrella04-catalog.err" &
P1=$!

cleanup() {
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

ROOT="$ROOT" CATALOG_URL="$CATALOG_URL" TRUSTED_KEY_DIR="$TRUSTED_KEY_DIR" python3 - <<'PY'
import hashlib, json, os, subprocess, urllib.error, urllib.request, zipfile
from pathlib import Path

root = Path(os.environ['ROOT'])
catalog_url = os.environ['CATALOG_URL']
trusted_key_dir = Path(os.environ['TRUSTED_KEY_DIR'])
trusted_key_dir.mkdir(parents=True, exist_ok=True)

private_key = trusted_key_dir / 'catalog-test-private.pem'
public_key = trusted_key_dir / 'catalog-test-signer.pem'
subprocess.run(
    ['openssl', 'genrsa', '-out', str(private_key), '2048'],
    check=True,
    capture_output=True,
    text=True,
)
subprocess.run(
    ['openssl', 'rsa', '-in', str(private_key), '-pubout', '-out', str(public_key)],
    check=True,
    capture_output=True,
    text=True,
)

def sign_bundle(bundle_dir: Path):
    signature_meta = {
        'keyId': 'catalog-test-signer',
        'algorithm': 'sha256-rsa',
        'signedFile': 'CHECKSUMS.json',
    }
    (bundle_dir / 'SIGNATURE.json').write_text(json.dumps(signature_meta, indent=2) + '\n', encoding='utf-8')
    subprocess.run(
        ['openssl', 'dgst', '-sha256', '-sign', str(private_key), '-out', str(bundle_dir / 'SIGNATURE'), str(bundle_dir / 'CHECKSUMS.json')],
        check=True,
        capture_output=True,
        text=True,
    )

with urllib.request.urlopen(catalog_url + '/v1/catalog/items', timeout=20) as resp:
    items = json.loads(resp.read().decode('utf-8'))

entries = {item['id']: item for item in items.get('items', [])}
example = entries.get('example.memory.skill')
assert example is not None, items
# Trusted-enable model: in require-signature mode a scan-discovered skill has
# no verified bundle signature, so it is registered but not enabled and the
# enable endpoint refuses it.
assert example['enabled'] is False, example
assert example['compatible']['ok'] is True, example
assert example.get('trust', {}).get('ok') is False, example

enable_untrusted_req = urllib.request.Request(
    catalog_url + '/v1/catalog/items/enable',
    method='POST',
    data=json.dumps({'id': 'example.memory.skill'}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
try:
    with urllib.request.urlopen(enable_untrusted_req, timeout=20) as resp:
        enable_untrusted_out = json.loads(resp.read().decode('utf-8'))
    raise AssertionError(enable_untrusted_out)
except urllib.error.HTTPError as exc:
    enable_untrusted_out = json.loads(exc.read().decode('utf-8'))
assert 'require-signature' in (((enable_untrusted_out.get('error') or {}).get('message')) or ''), enable_untrusted_out

with urllib.request.urlopen(catalog_url + '/v1/catalog/actions', timeout=20) as resp:
    actions = json.loads(resp.read().decode('utf-8'))
assert any(a.get('id') == 'skill.memory.summarize' for a in actions.get('actions', [])), actions

disable_req = urllib.request.Request(
    catalog_url + '/v1/catalog/items/disable',
    method='POST',
    data=json.dumps({'id': 'example.memory.skill'}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(disable_req, timeout=20) as resp:
    disabled = json.loads(resp.read().decode('utf-8'))
assert disabled.get('item', {}).get('enabled') is False, disabled

plugin_dir = root / 'tmp' / 'catalog-local-plugin'
plugin_dir.mkdir(parents=True, exist_ok=True)
(plugin_dir / 'bin').mkdir(exist_ok=True)
(plugin_dir / 'bin' / 'local-echo').write_text('#!/usr/bin/env bash\nset -euo pipefail\necho \"{\\\"ok\\\":true}\"\n', encoding='utf-8')
(plugin_dir / 'manifest.json').write_text(json.dumps({
    'id': 'contract.local.plugin',
    'name': 'Contract Local Plugin',
    'version': '0.1.0',
    'apiVersion': 'umbrella.catalog.manifest.v1',
    'kind': 'plugin',
    'runtime': 'shell',
    'entrypoint': 'bin/local-echo',
    'defaultEnabled': True,
    'compatibility': {'umbrella': {'minVersion': '0.4.0'}},
    'actions': [{'id': 'plugin.local.echo', 'title': 'Local Echo', 'requiredCapabilities': []}],
}, indent=2) + '\n', encoding='utf-8')

install_req = urllib.request.Request(
    catalog_url + '/v1/catalog/install-local',
    method='POST',
    data=json.dumps({'manifestPath': str(plugin_dir / 'manifest.json')}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(install_req, timeout=20) as resp:
    installed = json.loads(resp.read().decode('utf-8'))
assert installed.get('item', {}).get('id') == 'contract.local.plugin', installed
assert installed.get('item', {}).get('install', {}).get('lifecycleState') == 'validated', installed

bundle_dir = root / 'tmp' / 'catalog-bundle-plugin'
bundle_dir.mkdir(parents=True, exist_ok=True)
(bundle_dir / 'bin').mkdir(exist_ok=True)
bundle_script = bundle_dir / 'bin' / 'bundle-echo'
bundle_script.write_text('#!/usr/bin/env bash\nset -euo pipefail\necho "{\\"ok\\":true,\\"source\\":\\"bundle\\"}"\n', encoding='utf-8')
bundle_manifest = bundle_dir / 'manifest.json'
bundle_manifest.write_text(json.dumps({
    'id': 'contract.bundle.plugin',
    'name': 'Contract Bundle Plugin',
    'version': '1.0.0',
    'apiVersion': 'umbrella.catalog.manifest.v1',
    'kind': 'plugin',
    'runtime': 'shell',
    'entrypoint': 'bin/bundle-echo',
    'defaultEnabled': True,
    'compatibility': {
        'umbrella': {'minVersion': '0.4.0'},
        'pluginHostRuntimes': ['shell'],
        'apiVersions': ['umbrella.catalog.manifest.v1'],
        'actionSchemaVersions': ['umbrella.catalog.action.v1'],
    },
    'actions': [{'id': 'plugin.bundle.echo', 'title': 'Bundle Echo', 'requiredCapabilities': []}],
}, indent=2) + '\n', encoding='utf-8')
checksums = {
    'files': {
        'manifest.json': hashlib.sha256(bundle_manifest.read_bytes()).hexdigest(),
        'bin/bundle-echo': hashlib.sha256(bundle_script.read_bytes()).hexdigest(),
    }
}
(bundle_dir / 'CHECKSUMS.json').write_text(json.dumps(checksums, indent=2) + '\n', encoding='utf-8')
sign_bundle(bundle_dir)
bundle_zip = root / 'tmp' / 'catalog-bundle-plugin.zip'
with zipfile.ZipFile(bundle_zip, 'w') as archive:
    archive.write(bundle_manifest, 'manifest.json')
    archive.write(bundle_script, 'bin/bundle-echo')
    archive.write(bundle_dir / 'CHECKSUMS.json', 'CHECKSUMS.json')
    archive.write(bundle_dir / 'SIGNATURE.json', 'SIGNATURE.json')
    archive.write(bundle_dir / 'SIGNATURE', 'SIGNATURE')

bundle_install_req = urllib.request.Request(
    catalog_url + '/v1/catalog/install-bundle',
    method='POST',
    data=json.dumps({'bundlePath': str(bundle_zip)}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(bundle_install_req, timeout=20) as resp:
    bundle_installed = json.loads(resp.read().decode('utf-8'))
assert bundle_installed.get('item', {}).get('id') == 'contract.bundle.plugin', bundle_installed
assert bundle_installed.get('item', {}).get('install', {}).get('lifecycleState') == 'validated', bundle_installed
assert bundle_installed.get('item', {}).get('install', {}).get('checksumVerified') is True, bundle_installed
assert bundle_installed.get('item', {}).get('install', {}).get('signatureVerified') is True, bundle_installed
assert bundle_installed.get('item', {}).get('install', {}).get('signatureStatus') == 'verified', bundle_installed
assert bundle_installed.get('item', {}).get('install', {}).get('sourceType') == 'bundle', bundle_installed

versions_req = urllib.request.Request(catalog_url + '/v1/catalog/items/contract.bundle.plugin/versions', method='GET')
with urllib.request.urlopen(versions_req, timeout=20) as resp:
    versions_out = json.loads(resp.read().decode('utf-8'))
assert versions_out.get('versions', [])[0].get('version') == '1.0.0', versions_out

updated_bundle_dir = root / 'tmp' / 'catalog-bundle-plugin-v2'
updated_bundle_dir.mkdir(parents=True, exist_ok=True)
(updated_bundle_dir / 'bin').mkdir(exist_ok=True)
updated_script = updated_bundle_dir / 'bin' / 'bundle-echo'
updated_script.write_text('#!/usr/bin/env bash\nset -euo pipefail\necho "{\\"ok\\":true,\\"source\\":\\"bundle-v2\\"}"\n', encoding='utf-8')
updated_manifest = updated_bundle_dir / 'manifest.json'
updated_manifest.write_text(json.dumps({
    'id': 'contract.bundle.plugin',
    'name': 'Contract Bundle Plugin',
    'version': '1.1.0',
    'apiVersion': 'umbrella.catalog.manifest.v1',
    'kind': 'plugin',
    'runtime': 'shell',
    'entrypoint': 'bin/bundle-echo',
    'defaultEnabled': True,
    'compatibility': {
        'umbrella': {'minVersion': '0.4.0'},
        'pluginHostRuntimes': ['shell'],
        'apiVersions': ['umbrella.catalog.manifest.v1'],
        'actionSchemaVersions': ['umbrella.catalog.action.v1'],
    },
    'actions': [{'id': 'plugin.bundle.echo', 'title': 'Bundle Echo', 'requiredCapabilities': []}],
}, indent=2) + '\n', encoding='utf-8')
updated_checksums = {
    'files': {
        'manifest.json': hashlib.sha256(updated_manifest.read_bytes()).hexdigest(),
        'bin/bundle-echo': hashlib.sha256(updated_script.read_bytes()).hexdigest(),
    }
}
(updated_bundle_dir / 'CHECKSUMS.json').write_text(json.dumps(updated_checksums, indent=2) + '\n', encoding='utf-8')
sign_bundle(updated_bundle_dir)
updated_bundle_zip = root / 'tmp' / 'catalog-bundle-plugin-v2.zip'
with zipfile.ZipFile(updated_bundle_zip, 'w') as archive:
    archive.write(updated_manifest, 'manifest.json')
    archive.write(updated_script, 'bin/bundle-echo')
    archive.write(updated_bundle_dir / 'CHECKSUMS.json', 'CHECKSUMS.json')
    archive.write(updated_bundle_dir / 'SIGNATURE.json', 'SIGNATURE.json')
    archive.write(updated_bundle_dir / 'SIGNATURE', 'SIGNATURE')

update_req = urllib.request.Request(
    catalog_url + '/v1/catalog/update',
    method='POST',
    data=json.dumps({'id': 'contract.bundle.plugin', 'bundlePath': str(updated_bundle_zip)}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(update_req, timeout=20) as resp:
    updated = json.loads(resp.read().decode('utf-8'))
assert updated.get('item', {}).get('version') == '1.1.0', updated

tampered_dir = root / 'tmp' / 'catalog-bundle-plugin-bad'
tampered_dir.mkdir(parents=True, exist_ok=True)
(tampered_dir / 'bin').mkdir(exist_ok=True)
tampered_script = tampered_dir / 'bin' / 'bundle-echo'
tampered_script.write_text('#!/usr/bin/env bash\necho bad\n', encoding='utf-8')
tampered_manifest = tampered_dir / 'manifest.json'
tampered_manifest.write_text(updated_manifest.read_text(encoding='utf-8').replace('1.1.0', '1.2.0'), encoding='utf-8')
(tampered_dir / 'CHECKSUMS.json').write_text(json.dumps({
    'files': {
        'manifest.json': '0' * 64,
        'bin/bundle-echo': '1' * 64,
    }
}, indent=2) + '\n', encoding='utf-8')
tampered_zip = root / 'tmp' / 'catalog-bundle-plugin-bad.zip'
with zipfile.ZipFile(tampered_zip, 'w') as archive:
    archive.write(tampered_manifest, 'manifest.json')
    archive.write(tampered_script, 'bin/bundle-echo')
    archive.write(tampered_dir / 'CHECKSUMS.json', 'CHECKSUMS.json')
tampered_req = urllib.request.Request(
    catalog_url + '/v1/catalog/install-bundle',
    method='POST',
    data=json.dumps({'bundlePath': str(tampered_zip)}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
try:
    with urllib.request.urlopen(tampered_req, timeout=20) as resp:
        tampered_out = json.loads(resp.read().decode('utf-8'))
    raise AssertionError(tampered_out)
except urllib.error.HTTPError as exc:
    tampered_out = json.loads(exc.read().decode('utf-8'))
assert 'checksum mismatch' in (((tampered_out.get('error') or {}).get('message')) or ''), tampered_out

invalid_sig_dir = root / 'tmp' / 'catalog-bundle-plugin-invalid-signature'
invalid_sig_dir.mkdir(parents=True, exist_ok=True)
(invalid_sig_dir / 'bin').mkdir(exist_ok=True)
invalid_sig_script = invalid_sig_dir / 'bin' / 'bundle-echo'
invalid_sig_script.write_text('#!/usr/bin/env bash\nset -euo pipefail\necho "{\\"ok\\":true,\\"source\\":\\"invalid-signature\\"}"\n', encoding='utf-8')
invalid_sig_manifest = invalid_sig_dir / 'manifest.json'
invalid_sig_manifest.write_text(updated_manifest.read_text(encoding='utf-8').replace('1.1.0', '1.3.0'), encoding='utf-8')
invalid_sig_checksums = {
    'files': {
        'manifest.json': hashlib.sha256(invalid_sig_manifest.read_bytes()).hexdigest(),
        'bin/bundle-echo': hashlib.sha256(invalid_sig_script.read_bytes()).hexdigest(),
    }
}
(invalid_sig_dir / 'CHECKSUMS.json').write_text(json.dumps(invalid_sig_checksums, indent=2) + '\n', encoding='utf-8')
(invalid_sig_dir / 'SIGNATURE.json').write_text(json.dumps({
    'keyId': 'catalog-test-signer',
    'algorithm': 'sha256-rsa',
    'signedFile': 'CHECKSUMS.json',
}, indent=2) + '\n', encoding='utf-8')
(invalid_sig_dir / 'SIGNATURE').write_bytes(b'invalid-signature')
invalid_sig_zip = root / 'tmp' / 'catalog-bundle-plugin-invalid-signature.zip'
with zipfile.ZipFile(invalid_sig_zip, 'w') as archive:
    archive.write(invalid_sig_manifest, 'manifest.json')
    archive.write(invalid_sig_script, 'bin/bundle-echo')
    archive.write(invalid_sig_dir / 'CHECKSUMS.json', 'CHECKSUMS.json')
    archive.write(invalid_sig_dir / 'SIGNATURE.json', 'SIGNATURE.json')
    archive.write(invalid_sig_dir / 'SIGNATURE', 'SIGNATURE')
invalid_sig_req = urllib.request.Request(
    catalog_url + '/v1/catalog/install-bundle',
    method='POST',
    data=json.dumps({'bundlePath': str(invalid_sig_zip)}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
try:
    with urllib.request.urlopen(invalid_sig_req, timeout=20) as resp:
        invalid_sig_out = json.loads(resp.read().decode('utf-8'))
    raise AssertionError(invalid_sig_out)
except urllib.error.HTTPError as exc:
    invalid_sig_out = json.loads(exc.read().decode('utf-8'))
assert 'verification failure' in ((((invalid_sig_out.get('error') or {}).get('message')) or '').lower()), invalid_sig_out

uninstall_req = urllib.request.Request(
    catalog_url + '/v1/catalog/uninstall',
    method='POST',
    data=json.dumps({'id': 'contract.bundle.plugin', 'version': '1.1.0'}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(uninstall_req, timeout=20) as resp:
    uninstalled = json.loads(resp.read().decode('utf-8'))
assert uninstalled.get('removed') is True, uninstalled

refresh_req = urllib.request.Request(
    catalog_url + '/v1/catalog/refresh',
    method='POST',
    data=b'{}',
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(refresh_req, timeout=20) as resp:
    refreshed = json.loads(resp.read().decode('utf-8'))
assert refreshed.get('itemCount', 0) >= 2, refreshed

print('catalog service PASS')
PY

echo "umbrella0.4 catalog service contract PASS"
