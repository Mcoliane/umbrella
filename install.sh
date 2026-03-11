#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Umbrella0.4 installer

Usage:
  ./install.sh [--prefix PATH] [--skip-health-check] [--shell-profile PATH]

Defaults:
  --prefix ~/.local/umbrella0.4
  --shell-profile auto-detected (~/.zshrc, ~/.bashrc, or ~/.profile)

What this installer does:
  1) Copies the Umbrella0.4 app into PREFIX/app
  2) Creates a local Python venv in PREFIX/runtime/venv
  3) Installs pinned Python tooling (pip/setuptools/wheel)
  4) Installs wrapper CLIs in PREFIX/bin
  5) Writes ~/.umbrella/config.json
  6) Performs bringup/status/shutdown health verification (unless skipped)
EOF
}

ROOT="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${HOME}/.local/umbrella0.4"
SKIP_HEALTH="0"
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

PREFIX="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$PREFIX")"
APP_DIR="$PREFIX/app"
BIN_DIR="$PREFIX/bin"
RUNTIME_DIR="$PREFIX/runtime"
VENV_DIR="$RUNTIME_DIR/venv"
MANIFEST_PATH="$RUNTIME_DIR/service-manifest.json"
CONFIG_DIR="$HOME/.umbrella"
CONFIG_PATH="$CONFIG_DIR/config.json"
LOCK_PATH="$APP_DIR/runtime/runtime.lock.json"

mkdir -p "$PREFIX" "$BIN_DIR" "$RUNTIME_DIR" "$CONFIG_DIR"

echo "[install] copying app to $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete \
  --exclude '.git' \
  --exclude '.DS_Store' \
  --exclude 'tmp' \
  --exclude 'control-plane/observability/runs' \
  "$ROOT/" "$APP_DIR/"

echo "[install] creating virtual environment at $VENV_DIR"
python3 -m venv "$VENV_DIR"

echo "[install] installing pinned runtime tooling"
set +e
"$VENV_DIR/bin/pip" install --disable-pip-version-check -r "$APP_DIR/runtime/requirements-tools.txt"
PIP_RC=$?
set -e
if [[ "$PIP_RC" -ne 0 ]]; then
  echo "[install] warning: pinned tooling install failed (likely offline environment); continuing with venv defaults"
fi

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

chmod +x "$BIN_DIR/umbrellactl" "$BIN_DIR/umbrella-manage" "$BIN_DIR/umbrella-runner"

cat > "$PREFIX/env.sh" <<EOF
#!/usr/bin/env bash
export PATH="$BIN_DIR:\$PATH"
export UMBRELLA_HOME="$APP_DIR"
export UMBRELLA_VENV="$VENV_DIR"
EOF
chmod +x "$PREFIX/env.sh"

VERSION="unknown"
if [[ -f "$APP_DIR/VERSION" ]]; then
  VERSION="$(cat "$APP_DIR/VERSION" | tr -d '\r\n')"
fi

python3 - "$CONFIG_PATH" "$PREFIX" "$APP_DIR" "$BIN_DIR" "$VENV_DIR" "$VERSION" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path

config_path = Path(sys.argv[1])
prefix = Path(sys.argv[2])
app_dir = Path(sys.argv[3])
bin_dir = Path(sys.argv[4])
venv_dir = Path(sys.argv[5])
version = sys.argv[6]

payload = {
    "schema": "umbrella.user-config.v1",
    "updatedAt": datetime.now(timezone.utc).isoformat(),
    "install": {
        "version": version,
        "prefix": str(prefix),
        "appDir": str(app_dir),
        "binDir": str(bin_dir),
        "venvDir": str(venv_dir),
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
  "$APP_DIR/scripts/control-plane/manage-service-mesh" bringup --umbrella-root "$APP_DIR" --manifest "$MANIFEST_PATH" >/tmp/umbrella04-install-bringup.out
  "$APP_DIR/scripts/control-plane/manage-service-mesh" status --umbrella-root "$APP_DIR" --manifest "$MANIFEST_PATH" >/tmp/umbrella04-install-status.out
  "$APP_DIR/scripts/control-plane/manage-service-mesh" shutdown --umbrella-root "$APP_DIR" --manifest "$MANIFEST_PATH" >/tmp/umbrella04-install-shutdown.out
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
