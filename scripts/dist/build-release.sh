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
rsync -a \
  --exclude '.git' \
  --exclude '.DS_Store' \
  --exclude 'dist' \
  --exclude 'tmp' \
  --exclude 'control-plane/observability/runs' \
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
shasum -a 256 "$ARCHIVE" > "$CHECKSUM"

echo "[release] done"
echo "archive:  $ARCHIVE"
echo "sha256:   $CHECKSUM"
echo "manifest: $MANIFEST"
