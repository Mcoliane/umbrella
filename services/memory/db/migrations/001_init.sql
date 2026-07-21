CREATE TABLE IF NOT EXISTS namespaces (
  id TEXT PRIMARY KEY,
  owner_type TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  visibility TEXT NOT NULL,
  retention_days INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
  node_id TEXT PRIMARY KEY,
  namespace TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  tags TEXT NOT NULL,
  source TEXT NOT NULL,
  version INTEGER NOT NULL,
  etag TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT,
  FOREIGN KEY(namespace) REFERENCES namespaces(id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_namespace ON nodes(namespace);
CREATE INDEX IF NOT EXISTS idx_nodes_updated_at ON nodes(updated_at);

CREATE TABLE IF NOT EXISTS edges (
  from_node_id TEXT NOT NULL,
  to_node_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1.0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (from_node_id, to_node_id, relation)
);

CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  namespace TEXT NOT NULL,
  op TEXT NOT NULL,
  node_id TEXT,
  actor TEXT NOT NULL,
  request_id TEXT,
  ts TEXT NOT NULL,
  payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_namespace_id ON events(namespace, event_id);

-- Schema versioning: every statement in this baseline is idempotent
-- (IF NOT EXISTS / OR IGNORE), so re-applying 001 on an existing database is
-- a no-op. Forward migrations (002_*.sql, ...) are applied in order by
-- services/memory/db/migrate.py and recorded here.
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  filename TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_version(version, filename, applied_at)
VALUES (1, '001_init.sql', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
