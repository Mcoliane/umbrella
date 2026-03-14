#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MANIFEST="$ROOT/tmp/platform-stack-manifest.json"

cleanup() {
  python3 "$ROOT/scripts/control-plane/manage-platform-stack" shutdown --umbrella-root "$ROOT" --manifest "$MANIFEST" >/dev/null 2>&1 || true
  rm -f "$MANIFEST"
}
trap cleanup EXIT

"$ROOT/scripts/control-plane/manage-platform-stack" bringup --umbrella-root "$ROOT" --manifest "$MANIFEST" --profile full >/tmp/umbrella-platform-stack.out
"$ROOT/scripts/control-plane/manage-platform-stack" status --umbrella-root "$ROOT" --manifest "$MANIFEST" >/tmp/umbrella-platform-stack-status.out

python3 - "$MANIFEST" /tmp/umbrella-platform-stack-status.out <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
status = json.loads(Path(sys.argv[2]).read_text())

assert manifest.get('profile') == 'full', manifest
services = manifest.get('services') or {}
for required in ['policy', 'catalog', 'plugin-host', 'model-broker', 'execution', 'session']:
    assert required in services, manifest
assert status.get('ok') is True, status
rows = {row.get('service'): row for row in status.get('services', [])}
for required in ['policy', 'catalog', 'plugin-host', 'model-broker', 'execution', 'session']:
    assert rows.get(required, {}).get('healthOk') is True, status
print('platform stack launcher PASS')
PY

echo "umbrella0.4 platform stack launcher PASS"
