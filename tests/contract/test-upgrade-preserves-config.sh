#!/usr/bin/env bash
set -euo pipefail

# PROPERTY: ./install.sh over an existing install must not destroy operator configuration,
# and umbrella-backup must capture everything install.sh could destroy.
#
# docs/UPGRADE.md:3 states "Upgrades are non-destructive: ./install.sh never deletes durable
# state." The rsync at install.sh:143 uses --delete with an exclude list that mirrors
# .gitignore for most runtime state but not all of it, and it also overwrites *tracked*
# runtime files with the repo's shipped template. Two distinct loss paths result:
#
#   deleted   (untracked + unexcluded): model-broker.json, autonomy.json
#   reverted  (tracked + unexcluded):   model-provider.json
#
# Those two compound. services/runtime_model.py:281-284 loads model-broker.json first and
# falls back to model-provider.json only when the broker config is absent or empty — so an
# upgrade destroys the primary source of truth AND its fallback in one pass, while
# *.secrets.json (excluded at install.sh:173) survives, leaving an API key orphaned from any
# connection. The failure presents as a model outage, not as an install problem.
#
# Everything here runs against a scratch prefix and a scratch shell profile; the operator's
# real install at ~/.local/umbrella0.4 and their real ~/.zshrc are never touched.

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRATCH="$ROOT/tmp/upgrade-preserve-$$"
PREFIX="$SCRATCH/prefix"
PROFILE="$SCRATCH/shell-profile"
APP="$PREFIX/app"
RUNTIME="$APP/control-plane/runtime"

mkdir -p "$SCRATCH"
: > "$PROFILE"

cleanup(){ rm -rf "$SCRATCH"; }
trap cleanup EXIT

install_once() {
  "$ROOT/install.sh" \
    --prefix "$PREFIX" \
    --skip-health-check \
    --no-backup \
    --shell-profile "$PROFILE" \
    >"$SCRATCH/install-$1.out" 2>"$SCRATCH/install-$1.err" || {
      echo "install.sh ($1) failed:"; tail -20 "$SCRATCH/install-$1.err"; exit 1; }
}

install_once first

# The operator configures a model. These are the exact shapes the runtime writes.
cat > "$RUNTIME/model-broker.json" <<'JSON'
{
  "version": "umbrella.model-broker.v1",
  "enabled": true,
  "defaultConnectionId": "contract-conn",
  "connections": [
    {
      "id": "contract-conn",
      "enabled": true,
      "providerId": "openai-compatible",
      "baseUrl": "https://contract.example.invalid/v1",
      "defaultModel": "contract-model"
    }
  ]
}
JSON

cat > "$RUNTIME/model-provider.json" <<'JSON'
{
  "version": "umbrella.model-provider.v1",
  "enabled": true,
  "provider": {
    "id": "contract-legacy",
    "providerId": "openai-compatible",
    "baseUrl": "https://contract-legacy.example.invalid/v1",
    "defaultModel": "contract-legacy-model"
  }
}
JSON

cat > "$RUNTIME/autonomy.json" <<'JSON'
{ "mode": "ask", "updatedAt": "2026-08-03T00:00:00+00:00" }
JSON

chmod 600 "$RUNTIME/model-broker.json" "$RUNTIME/model-provider.json" "$RUNTIME/autonomy.json"

# The upgrade.
install_once second

python3 - "$RUNTIME" <<'PY'
import json, sys
from pathlib import Path

runtime = Path(sys.argv[1])
failures = []


def load(name):
    p = runtime / name
    if not p.exists():
        failures.append(f'{name}: DELETED by the upgrade')
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception as ex:
        failures.append(f'{name}: unreadable after upgrade ({ex})')
        return None


broker = load('model-broker.json')
if broker is not None:
    if broker.get('defaultConnectionId') != 'contract-conn':
        failures.append(f"model-broker.json: operator config replaced (defaultConnectionId={broker.get('defaultConnectionId')!r})")
    if not broker.get('enabled'):
        failures.append('model-broker.json: operator had enabled=true, upgrade left it disabled')

provider = load('model-provider.json')
if provider is not None:
    got = (provider.get('provider') or {}).get('id')
    if got != 'contract-legacy':
        failures.append(f"model-provider.json: REVERTED to the shipped template (provider.id={got!r}, expected 'contract-legacy')")
    if not provider.get('enabled'):
        failures.append('model-provider.json: operator had enabled=true, upgrade left it disabled')

autonomy = load('autonomy.json')
if autonomy is not None and autonomy.get('mode') != 'ask':
    failures.append(f"autonomy.json: mode reset to {autonomy.get('mode')!r}, operator had 'ask'")

if failures:
    print('UPGRADE DESTROYED OPERATOR CONFIG:\n')
    for f in failures:
        print('  - ' + f)
    raise SystemExit(1)

print('upgrade preserves operator config PASS')
PY

# umbrella-backup must be able to restore whatever install.sh could destroy, or the
# "take a backup first" instruction in docs/UPGRADE.md is not actually a safety net.
BACKUP_OUT="$SCRATCH/backup.json"
bash "$ROOT/scripts/tools/umbrella-backup" create --app-dir "$APP" --output "$SCRATCH/backup.tgz" \
  >"$BACKUP_OUT" 2>"$SCRATCH/backup.err" || {
    echo "umbrella-backup create failed:"; tail -20 "$SCRATCH/backup.err"; exit 1; }

python3 - "$BACKUP_OUT" <<'PY'
import json, sys
from pathlib import Path

# umbrella-backup prints a JSON object followed by a "[umbrella-backup] wrote ..." line,
# so decode just the leading object and ignore the trailing log output.
raw = Path(sys.argv[1]).read_text(encoding='utf-8').strip()
start = raw.find('{')
out, _ = json.JSONDecoder().raw_decode(raw[start:]) if start >= 0 else ({}, 0)
captured = set(out.get('captured') or [])

required = [
    'control-plane/runtime/model-broker.json',
    'control-plane/runtime/model-provider.json',
    'control-plane/runtime/autonomy.json',
]
missing = [r for r in required if r not in captured]
if missing:
    print('BACKUP CANNOT RESTORE WHAT THE UPGRADE DESTROYS:\n')
    for m in missing:
        print('  - not captured: ' + m)
    print('\ncaptured was: ' + ', '.join(sorted(captured)) or '(nothing)')
    raise SystemExit(1)

print('backup captures upgrade-destroyable config PASS')
PY

echo "umbrella0.4 upgrade preserves config contract PASS"
