#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import importlib.util
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("memory_core_app", root / "services" / "memory-core" / "app.py")
memory_core_app = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(memory_core_app)

tmp_root = root / "tmp" / "memory-core-concurrency"
if tmp_root.exists():
    shutil.rmtree(tmp_root)
tmp_root.mkdir(parents=True, exist_ok=True)

store = memory_core_app.MemoryStore(tmp_root)

def write_one(i: int):
    return store.put("team", f"key-{i}", {"value": i}, {"writer": i})

with ThreadPoolExecutor(max_workers=16) as ex:
    rows = list(ex.map(write_one, range(80)))

entries = store.list("team")
assert len(entries) == 80, len(entries)
keys = {row["key"] for row in entries}
assert len(keys) == 80, len(keys)
for i in range(80):
    row = store.get("team", f"key-{i}")
    assert row is not None, i
    assert row["value"] == {"value": i}, row
    assert row["metadata"]["writer"] == i, row

print("memory-core concurrency PASS")
PY

echo "umbrella0.4 memory-core concurrency contract PASS"
