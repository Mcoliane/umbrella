#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

OUT="$(python3 "$ROOT/scripts/umbrella-tui" --umbrella-root "$ROOT" --dump-home)"

python3 - "$OUT" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
assert isinstance(payload.get('services'), list), payload
assert isinstance(payload.get('sessions'), list), payload
assert isinstance(payload.get('agentPackages'), list), payload
assert isinstance(payload.get('runtimeCapabilities'), dict), payload
print('platform tui smoke PASS')
PY

echo "umbrella0.4 platform tui smoke PASS"
