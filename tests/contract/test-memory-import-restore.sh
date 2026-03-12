#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])

from services.memory.store import MemoryStore

root = Path(sys.argv[1])
tmp_root = root / "tmp" / "memory-import-restore"
if tmp_root.exists():
    shutil.rmtree(tmp_root)
tmp_root.mkdir(parents=True, exist_ok=True)

db_path = tmp_root / "observability" / "memory-service" / "memory.db"
store = MemoryStore(db_path)
migration = (root / "services" / "memory" / "db" / "migrations" / "001_init.sql").read_text(encoding="utf-8")
store.init_db(migration)
store.upsert_namespace({"id": "team", "owner_type": "system", "owner_id": "test"})

canonical_path = tmp_root / "canonical.json"
canonical_path.write_text(
    json.dumps(
        {
            "elements": [
                {
                    "applyRelPath": "bootstrap/setup-a.sh",
                    "lane": "bootstrap",
                    "class": "script",
                    "mode": "0755",
                    "sha256": "abc123",
                    "sourceRel": "scripts/bootstrap/setup-a.sh",
                }
            ]
        }
    ),
    encoding="utf-8",
)

first = store.import_removed("team", canonical_path, actor="contract", request_id="req-import-1")
assert first["imported"] == 1, first
assert first["updated"] == 0, first
assert first["restored"] == 0, first

node_id = "setup:bootstrap/setup-a.sh"
assert store.delete_node(node_id, actor="contract", request_id="req-delete") is True
deleted = store.get_node(node_id, include_deleted=True)
assert deleted is not None and deleted["deleted_at"] is not None, deleted

second = store.import_removed("team", canonical_path, actor="contract", request_id="req-import-2")
assert second["imported"] == 0, second
assert second["updated"] == 0, second
assert second["restored"] == 1, second
restored = store.get_node(node_id, include_deleted=True)
assert restored is not None and restored["deleted_at"] is None, restored

store.delete_node(node_id, actor="contract", request_id="req-delete-2")
promoted = store.promote_from_memory_core(
    {
        "source": {"namespace": "team", "key": "incident:42", "value": {"severity": "high"}, "metadata": {"source": "test"}},
        "target": {"namespace": "team", "node_id": node_id, "title": "Promoted Setup", "kind": "fact"},
        "provenance": {"reason": "contract"},
    },
    actor="contract",
    request_id="req-promote",
)
assert promoted["mode"] == "restored", promoted
restored_again = store.get_node(node_id, include_deleted=True)
assert restored_again is not None and restored_again["deleted_at"] is None, restored_again
assert restored_again["content"]["sourceKey"] == "incident:42", restored_again

events = store.list_events("team", 0, 50)["events"]
restore_ops = [event for event in events if event["op"] == "restore"]
assert len(restore_ops) >= 2, restore_ops

print("memory import/restore PASS")
PY

echo "umbrella0.4 memory import/restore contract PASS"
