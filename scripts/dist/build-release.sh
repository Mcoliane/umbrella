#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Build a versioned Umbrella0.4 release tarball.

Usage:
  ./scripts/dist/build-release.sh [--version X.Y.Z] [--out-dir PATH]

Defaults:
  version: read from ./VERSION
  out-dir: ./dist/releases
EOF
}

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT_DIR="$ROOT/dist/releases"
VERSION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      VERSION="$2"
      shift 2
      ;;
    --out-dir)
      OUT_DIR="$2"
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

if [[ -z "$VERSION" ]]; then
  if [[ -f "$ROOT/VERSION" ]]; then
    VERSION="$(cat "$ROOT/VERSION" | tr -d '\r\n')"
  else
    echo "VERSION file missing and --version not provided" >&2
    exit 1
  fi
fi

OUT_DIR="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$OUT_DIR")"
mkdir -p "$OUT_DIR"

RELEASE_NAME="umbrella0.4-${VERSION}"
STAGE_DIR="$OUT_DIR/$RELEASE_NAME"
ARCHIVE="$OUT_DIR/${RELEASE_NAME}.tar.gz"
CHECKSUM="$OUT_DIR/${RELEASE_NAME}.sha256"
MANIFEST="$OUT_DIR/${RELEASE_NAME}.release.json"

rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"

echo "[release] staging $RELEASE_NAME"
# Exclusions mirror .gitignore: a release built from a used checkout must not
# ship live secrets or runtime-generated state.
rsync -a \
  --exclude '.git' \
  --exclude '.DS_Store' \
  --exclude '* 2' \
  --exclude '* 2.*' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'dist' \
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
  "$ROOT/" "$STAGE_DIR/"

echo "[release] writing manifest"
python3 - "$MANIFEST" "$VERSION" "$RELEASE_NAME" "$ARCHIVE" "$ROOT/runtime/runtime.lock.json" <<'PY'
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

manifest_path = Path(sys.argv[1])
version = sys.argv[2]
release_name = sys.argv[3]
archive = Path(sys.argv[4])
runtime_lock = Path(sys.argv[5])

runtime = json.loads(runtime_lock.read_text(encoding="utf-8")) if runtime_lock.exists() else {}

payload = {
    "schema": "umbrella.release.v1",
    "name": release_name,
    "version": version,
    "builtAt": datetime.now(timezone.utc).isoformat(),
    "archive": str(archive),
    "runtimeLock": runtime,
}
manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
print(str(manifest_path))
PY

echo "[release] creating archive $ARCHIVE"
tar -C "$OUT_DIR" -czf "$ARCHIVE" "$RELEASE_NAME"

echo "[release] verifying archive is secret-free"
SECRET_NAME_RE='(bootstrap-secret\.txt|platform-token\.txt|service-token\.txt|model-broker\.secrets\.json|model-provider\.secrets\.json|\.secrets\.json)$'
# List the archive in a separate step so a tar failure aborts the build
# instead of being masked by grep's no-match exit status under pipefail.
LISTING="$(tar -tzf "$ARCHIVE")"
if LEAKED="$(grep -E "$SECRET_NAME_RE" <<<"$LISTING")"; then
  echo "[release] ERROR: archive contains secret files; refusing to publish:" >&2
  echo "$LEAKED" >&2
  rm -f "$ARCHIVE"
  exit 1
fi

shasum -a 256 "$ARCHIVE" > "$CHECKSUM"

echo "[release] done"
echo "archive:  $ARCHIVE"
echo "sha256:   $CHECKSUM"
echo "manifest: $MANIFEST"
