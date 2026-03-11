#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VALIDATOR="$ROOT/scripts/control-plane/validate-run-transition"

VALID_OUT="$ROOT/tmp/transition-valid.json"
INVALID_OUT="$ROOT/tmp/transition-invalid.json"

mkdir -p "$ROOT/tmp"

"$VALIDATOR" --umbrella-root "$ROOT" --from-state RUNNING --to-state SUCCEEDED >"$VALID_OUT"

set +e
"$VALIDATOR" --umbrella-root "$ROOT" --from-state PENDING --to-state SUCCEEDED >"$INVALID_OUT"
RC_INVALID=$?
set -e

if [[ "$RC_INVALID" -ne 2 ]]; then
  echo "expected invalid transition to exit 2, got $RC_INVALID"
  exit 1
fi

python3 - "$VALID_OUT" "$INVALID_OUT" <<'PY'
import json, sys
from pathlib import Path

valid = json.loads(Path(sys.argv[1]).read_text())
invalid = json.loads(Path(sys.argv[2]).read_text())

assert valid.get('valid') is True, valid
assert valid.get('from') == 'RUNNING', valid
assert valid.get('to') == 'SUCCEEDED', valid

assert invalid.get('valid') is False, invalid
assert invalid.get('from') == 'PENDING', invalid
assert invalid.get('to') == 'SUCCEEDED', invalid

print('run transition guard PASS')
PY

echo "umbrella0.4 run transition guard contract PASS"
