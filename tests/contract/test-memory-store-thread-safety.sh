#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, sys.argv[1])

from services.memory.store import MemoryStore

root = Path(sys.argv[1])
tmp_root = root / "tmp" / "memory-store-thread-safety"
if tmp_root.exists():
    shutil.rmtree(tmp_root)
tmp_root.mkdir(parents=True, exist_ok=True)

db_path = tmp_root / "observability" / "memory-service" / "memory.db"
store = MemoryStore(db_path)
migration = (root / "services" / "memory" / "db" / "migrations" / "001_init.sql").read_text(encoding="utf-8")
store.init_db(migration)
store.upsert_namespace({"id": "team", "owner_type": "system", "owner_id": "test"})

def create_node(i: int):
    return store.create_node(
        {
            "node_id": f"node-{i}",
            "namespace": "team",
            "kind": "note",
            "title": f"Node {i}",
            "content": {"v": i},
            "tags": ["threaded"],
        },
        actor="contract",
        request_id=f"req-create-{i}",
    )

with ThreadPoolExecutor(max_workers=16) as ex:
    list(ex.map(create_node, range(60)))

def update_node(i: int):
    node = store.get_node(f"node-{i}")
    assert node is not None, i
    updated, problem = store.update_node(
        f"node-{i}",
        {"content": {"v": i, "updated": True}, "title": f"Node {i} updated"},
        if_match=node["etag"],
        actor="contract",
        request_id=f"req-update-{i}",
    )
    assert problem is None, problem
    return updated

with ThreadPoolExecutor(max_workers=16) as ex:
    list(ex.map(update_node, range(60)))

results = store.search_nodes({"namespace": "team", "query": "Node", "k": 100})
assert len(results["results"]) == 60, len(results["results"])
events = store.list_events("team", 0, 200)
assert len(events["events"]) >= 120, len(events["events"])
for i in range(60):
    node = store.get_node(f"node-{i}")
    assert node is not None, i
    assert node["content"]["updated"] is True, node

print("memory store thread safety PASS")
PY

echo "umbrella0.4 memory store thread safety contract PASS"
