#!/usr/bin/env python3
"""Forward-only schema migrations for the durable memory service.

Mechanism:
  - migrations live in services/memory/db/migrations/ as NNN_name.sql,
    applied in ascending numeric order;
  - a schema_version table records every applied migration;
  - applying migrations to an already-current database is a no-op.

001_init.sql is the baseline. The memory service itself executes 001 at boot
(every statement in it is idempotent), which also stamps schema_version=1, so
databases created by a running service are versioned without any extra step.
A database created before schema versioning existed (has the core tables but
no schema_version rows) is adopted at version 1 without re-executing 001.

Migrations after 001 should be written to be safe to re-run where practical:
a migration script that fails partway is not recorded and will be re-executed
on the next run.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / 'migrations'
BASELINE_VERSION = 1

_NAME_RE = re.compile(r'^(\d{3})_[A-Za-z0-9][A-Za-z0-9_\-]*\.sql$')


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def discover_migrations(migrations_dir: Path | None = None) -> list[tuple[int, Path]]:
    """Return [(version, path), ...] sorted ascending; reject malformed or duplicate versions."""
    folder = migrations_dir if migrations_dir is not None else MIGRATIONS_DIR
    out: list[tuple[int, Path]] = []
    seen: dict[int, Path] = {}
    for p in sorted(folder.glob('*.sql')):
        m = _NAME_RE.match(p.name)
        if not m:
            raise ValueError(f'malformed migration filename (want NNN_name.sql): {p.name}')
        version = int(m.group(1))
        if version in seen:
            raise ValueError(f'duplicate migration version {version:03d}: {seen[version].name} and {p.name}')
        seen[version] = p
        out.append((version, p))
    return out


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS schema_version (
          version INTEGER PRIMARY KEY,
          filename TEXT NOT NULL,
          applied_at TEXT NOT NULL
        )
        '''
    )
    conn.commit()


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    cur = conn.execute('SELECT version FROM schema_version')
    return {int(row[0]) for row in cur.fetchall()}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


def _record(conn: sqlite3.Connection, version: int, filename: str) -> None:
    conn.execute(
        'INSERT OR IGNORE INTO schema_version(version, filename, applied_at) VALUES(?,?,?)',
        (version, filename, now_iso()),
    )
    conn.commit()


def apply_migrations(db_path: Path | str, migrations_dir: Path | None = None) -> dict:
    """Bring db_path to the latest schema version. Idempotent; safe on a fresh path."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    migrations = discover_migrations(migrations_dir)
    if not migrations:
        raise ValueError('no migrations found')

    conn = sqlite3.connect(str(path))
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=5000')
        _ensure_schema_version_table(conn)

        applied = _applied_versions(conn)
        adopted_baseline = False
        if not applied and _has_table(conn, 'namespaces') and _has_table(conn, 'nodes'):
            # Pre-versioning database: the baseline schema is already present.
            # Adopt it at version 1 instead of re-executing 001.
            _record(conn, BASELINE_VERSION, migrations[0][1].name)
            applied = _applied_versions(conn)
            adopted_baseline = True

        applied_now: list[str] = []
        for version, sql_path in migrations:
            if version in applied:
                continue
            sql = sql_path.read_text(encoding='utf-8')
            conn.executescript(sql)
            _record(conn, version, sql_path.name)
            applied_now.append(sql_path.name)

        current = max(_applied_versions(conn))
        return {
            'ok': True,
            'dbPath': str(path),
            'currentVersion': current,
            'adoptedBaseline': adopted_baseline,
            'appliedNow': applied_now,
            'availableVersions': [v for v, _ in migrations],
            'checkedAt': now_iso(),
        }
    finally:
        conn.close()


if __name__ == '__main__':
    import json
    import sys

    if len(sys.argv) != 2:
        print('usage: migrate.py DB_PATH', file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(apply_migrations(sys.argv[1]), indent=2))
