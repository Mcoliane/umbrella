#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Umbrella0.4 installer

Usage:
  ./install.sh [--prefix PATH] [--skip-health-check] [--no-backup] [--shell-profile PATH]

Defaults:
  --prefix ~/.local/umbrella0.4
  --shell-profile auto-detected (~/.zshrc, ~/.bashrc, or ~/.profile)

What this installer does:
  1) Verifies python3 satisfies runtime/runtime.lock.json (>= python.minimum)
  2) On upgrade: snapshots durable state to PREFIX/backups/ (skip: --no-backup)
  3) Copies the Umbrella0.4 app into PREFIX/app, preserving all durable state
     (knowledge graph, sessions, shop profiles, catalog registry, approvals)
  4) Applies pending memory-service schema migrations
  5) Installs wrapper CLIs in PREFIX/bin
  6) Writes ~/.umbrella/config.json
  7) Performs bringup/status/shutdown health verification (unless skipped)

Umbrella0.4 is stdlib-only: no virtual environment or pip packages are
installed.
EOF
}

ROOT="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${HOME}/.local/umbrella0.4"
SKIP_HEALTH="0"
SKIP_BACKUP="0"
SHELL_PROFILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)
      PREFIX="$2"
      shift 2
      ;;
    --skip-health-check)
      SKIP_HEALTH="1"
      shift 1
      ;;
    --no-backup)
      SKIP_BACKUP="1"
      shift 1
      ;;
    --shell-profile)
      SHELL_PROFILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required but not found in PATH" >&2
  exit 1
fi
if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required but not found in PATH" >&2
  exit 1
fi

# Enforce the supported interpreter range declared in runtime/runtime.lock.json.
python3 - "$ROOT/runtime/runtime.lock.json" <<'PY'
import json, sys
from pathlib import Path

lock_path = Path(sys.argv[1])
minimum = "3.9"
if lock_path.is_file():
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    minimum = str((lock.get("python") or {}).get("minimum", minimum))
need = tuple(int(part) for part in minimum.split("."))
have = sys.version_info[: len(need)]
if have < need:
    print(
        f"python3 >= {minimum} is required; found {sys.version.split()[0]} "
        f"at {sys.executable}",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY

PREFIX="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$PREFIX")"
APP_DIR="$PREFIX/app"
BIN_DIR="$PREFIX/bin"
RUNTIME_DIR="$PREFIX/runtime"
BACKUP_DIR="$PREFIX/backups"
MANIFEST_PATH="$RUNTIME_DIR/service-manifest.json"
CONFIG_DIR="$HOME/.umbrella"
CONFIG_PATH="$CONFIG_DIR/config.json"

mkdir -p "$PREFIX" "$BIN_DIR" "$RUNTIME_DIR" "$CONFIG_DIR"

# Upgrade safety: snapshot durable state before touching PREFIX/app. The
# rsync below never deletes state paths (they are all excluded), but a backup
# makes even an unexpected failure recoverable.
if [[ "$SKIP_BACKUP" != "1" && -d "$APP_DIR/control-plane" ]]; then
  HAS_STATE="0"
  for state_path in \
    "$APP_DIR/control-plane/observability/memory-service" \
    "$APP_DIR/control-plane/observability/sessions" \
    "$APP_DIR/control-plane/observability/session-profiles" \
    "$APP_DIR/control-plane/observability/catalog" \
    "$APP_DIR/control-plane/memory-core/store.json" \
    "$APP_DIR/control-plane/approvals"; do
    if [[ -e "$state_path" ]]; then
      HAS_STATE="1"
      break
    fi
  done
  if [[ "$HAS_STATE" == "1" ]]; then
    mkdir -p "$BACKUP_DIR"
    BACKUP_FILE="$BACKUP_DIR/pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
    echo "[install] backing up durable state to $BACKUP_FILE"
    "$ROOT/scripts/tools/umbrella-backup" create --app-dir "$APP_DIR" --output "$BACKUP_FILE"
  fi
fi

echo "[install] copying app to $APP_DIR"
mkdir -p "$APP_DIR"
# Exclusions serve two purposes:
#   1) an install from a used checkout must not propagate live secrets or
#      runtime-generated state into PREFIX/app (mirrors .gitignore);
#   2) rsync --delete never removes receiver-side files matched by an
#      exclude, so every durable-state path listed here SURVIVES upgrades:
#      the knowledge graph (memory-service), sessions, shop profiles,
#      catalog registry, approvals, memory-core store, extensions, and run
#      history are all preserved in place. Do not remove entries from this
#      list without relocating the state first.
rsync -a --delete \
  --exclude '.git' \
  --exclude '.DS_Store' \
  --exclude '* 2' \
  --exclude '* 2.*' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'dist/releases' \
  --exclude 'dist/releases-test' \
  --exclude 'tmp' \
  --exclude '.venv' \
  --exclude 'memory-boundary' \
  --exclude 'control-plane/observability/runs' \
  --exclude 'control-plane/observability/memory-boundary' \
  --exclude 'control-plane/observability/memory-service' \
  --exclude 'control-plane/observability/policy' \
  --exclude 'control-plane/observability/catalog' \
  --exclude 'control-plane/observability/plugin-host' \
  --exclude 'control-plane/observability/session-profiles' \
  --exclude 'control-plane/observability/sessions' \
  --exclude 'control-plane/extensions' \
  --exclude 'control-plane/approvals/*.json' \
  --exclude 'control-plane/approvals/resume-journal' \
  --exclude 'control-plane/runtime/bootstrap-secret.txt' \
  --exclude 'control-plane/runtime/platform-manifest.json' \
  --exclude 'control-plane/runtime/platform-token.txt' \
  --exclude 'control-plane/runtime/service-manifest.json' \
  --exclude 'control-plane/runtime/service-token.txt' \
  --exclude 'control-plane/runtime/logs' \
  --exclude 'control-plane/memory-core/store.json' \
  --exclude '*.secrets.json' \
  "$ROOT/" "$APP_DIR/"

# Purge stale mesh credentials propagated into APP_DIR by earlier installers;
# they are regenerated at bringup. Operator-configured provider secrets
# (*.secrets.json) are durable state and are deliberately PRESERVED: deleting
# them broke model conversations after every upgrade. The rsync exclusion
# above already prevents new secrets leaking in from the source checkout.
rm -f \
  "$APP_DIR/control-plane/runtime/bootstrap-secret.txt" \
  "$APP_DIR/control-plane/runtime/platform-token.txt" \
  "$APP_DIR/control-plane/runtime/service-token.txt"

# Bring the durable memory database up to the current schema version. This is
# a no-op on a fresh install (the service stamps the baseline at first boot)
# and on an already-current database.
echo "[install] applying memory-service schema migrations"
"$APP_DIR/scripts/tools/memory-migrate" --umbrella-root "$APP_DIR"

echo "[install] writing CLI wrappers"
cat > "$BIN_DIR/umbrellactl" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$APP_DIR/scripts/umbrellactl" --umbrella-root "$APP_DIR" "\$@"
EOF

cat > "$BIN_DIR/umbrella-manage" <<EOF
#!/usr/bin/env bash
set -euo pipefail
if [[ "\$#" -eq 0 ]]; then
  exec "$APP_DIR/scripts/control-plane/manage-service-mesh" --help
fi
cmd="\$1"
shift
exec "$APP_DIR/scripts/control-plane/manage-service-mesh" "\$cmd" --umbrella-root "$APP_DIR" "\$@"
EOF

cat > "$BIN_DIR/umbrella-runner" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$APP_DIR/scripts/control-plane/run-umbrella-control-plane" --umbrella-root "$APP_DIR" "\$@"
EOF

cat > "$BIN_DIR/umbrella-tui" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$APP_DIR/scripts/umbrella-tui" --umbrella-root "$APP_DIR" "\$@"
EOF

chmod +x "$BIN_DIR/umbrellactl" "$BIN_DIR/umbrella-manage" "$BIN_DIR/umbrella-runner" "$BIN_DIR/umbrella-tui"

cat > "$PREFIX/env.sh" <<EOF
#!/usr/bin/env bash
export PATH="$BIN_DIR:\$PATH"
export UMBRELLA_HOME="$APP_DIR"
EOF
chmod +x "$PREFIX/env.sh"

VERSION="unknown"
if [[ -f "$APP_DIR/VERSION" ]]; then
  VERSION="$(cat "$APP_DIR/VERSION" | tr -d '\r\n')"
fi

python3 - "$CONFIG_PATH" "$PREFIX" "$APP_DIR" "$BIN_DIR" "$VERSION" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path

config_path = Path(sys.argv[1])
prefix = Path(sys.argv[2])
app_dir = Path(sys.argv[3])
bin_dir = Path(sys.argv[4])
version = sys.argv[5]

payload = {
    "schema": "umbrella.user-config.v1",
    "updatedAt": datetime.now(timezone.utc).isoformat(),
    "install": {
        "version": version,
        "prefix": str(prefix),
        "appDir": str(app_dir),
        "binDir": str(bin_dir),
    },
}
config_path.parent.mkdir(parents=True, exist_ok=True)
config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
print(str(config_path))
PY

if [[ -z "$SHELL_PROFILE" ]]; then
  if [[ -f "$HOME/.zshrc" ]]; then
    SHELL_PROFILE="$HOME/.zshrc"
  elif [[ -f "$HOME/.bashrc" ]]; then
    SHELL_PROFILE="$HOME/.bashrc"
  else
    SHELL_PROFILE="$HOME/.profile"
  fi
fi

PROFILE_LINE="source \"$PREFIX/env.sh\""
if [[ ! -f "$SHELL_PROFILE" ]] || ! grep -Fq "$PROFILE_LINE" "$SHELL_PROFILE"; then
  echo "$PROFILE_LINE" >> "$SHELL_PROFILE"
fi

if [[ "$SKIP_HEALTH" != "1" ]]; then
  echo "[install] running health verification (bringup/status/shutdown)"
  mkdir -p "$APP_DIR/tmp"
  "$APP_DIR/scripts/control-plane/manage-service-mesh" bringup --umbrella-root "$APP_DIR" --manifest "$MANIFEST_PATH" >"$APP_DIR/tmp/umbrella04-install-bringup.out"
  "$APP_DIR/scripts/control-plane/manage-service-mesh" status --umbrella-root "$APP_DIR" --manifest "$MANIFEST_PATH" >"$APP_DIR/tmp/umbrella04-install-status.out"
  "$APP_DIR/scripts/control-plane/manage-service-mesh" shutdown --umbrella-root "$APP_DIR" --manifest "$MANIFEST_PATH" >"$APP_DIR/tmp/umbrella04-install-shutdown.out"
fi

echo "[install] complete"
echo ""
echo "Installed version: $VERSION"
echo "Install prefix: $PREFIX"
echo "User config: $CONFIG_PATH"
echo ""
echo "Open a new shell or run:"
echo "  source \"$PREFIX/env.sh\""
echo ""
echo "Then try:"
echo "  umbrellactl --help"
echo "  umbrella-manage bringup"
echo "  umbrella-tui"
