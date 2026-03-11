#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .events import event_payload
from .search import contains_query


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def etag_for(version: int, node_id: str) -> str:
    raw = f'{node_id}:{version}'.encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


class MemoryStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def init_db(self, migration_sql: str):
        self.conn.executescript(migration_sql)
        self.conn.commit()

    def upsert_namespace(self, data: dict) -> dict:
        ns_id = str(data.get('id', '')).strip()
        if not ns_id:
            raise ValueError('id is required')
        owner_type = str(data.get('owner_type', 'user'))
        owner_id = str(data.get('owner_id', 'default'))
        visibility = str(data.get('visibility', 'private'))
        retention_days = data.get('retention_days')
        ts = now_iso()

        cur = self.conn.execute('SELECT id, created_at FROM namespaces WHERE id=?', (ns_id,))
        row = cur.fetchone()
        created_at = row['created_at'] if row else ts

        self.conn.execute(
            '''
            INSERT INTO namespaces(id, owner_type, owner_id, visibility, retention_days, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              owner_type=excluded.owner_type,
              owner_id=excluded.owner_id,
              visibility=excluded.visibility,
              retention_days=excluded.retention_days,
              updated_at=excluded.updated_at
            ''',
            (ns_id, owner_type, owner_id, visibility, retention_days, created_at, ts),
        )
        self.conn.commit()
        return self.get_namespace(ns_id)

    def get_namespace(self, ns_id: str) -> dict | None:
        cur = self.conn.execute('SELECT * FROM namespaces WHERE id=?', (ns_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def create_node(self, data: dict, actor: str, request_id: str = '') -> dict:
        node_id = str(data.get('node_id', '')).strip()
        namespace = str(data.get('namespace', '')).strip()
        kind = str(data.get('kind', '')).strip()
        title = str(data.get('title', '')).strip()
        if not node_id or not namespace or not kind or not title:
            raise ValueError('node_id, namespace, kind, title are required')
        if not self.get_namespace(namespace):
            raise ValueError('namespace does not exist')

        content = data.get('content', '')
        tags = data.get('tags') or []
        source = str(data.get('source', 'umbrella'))
        ts = now_iso()
        version = 1
        etag = etag_for(version, node_id)

        self.conn.execute(
            '''
            INSERT INTO nodes(node_id, namespace, kind, title, content, tags, source, version, etag, created_at, updated_at, deleted_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)
            ''',
            (
                node_id,
                namespace,
                kind,
                title,
                json.dumps(content, ensure_ascii=False),
                json.dumps(tags, ensure_ascii=False),
                source,
                version,
                etag,
                ts,
                ts,
            ),
        )
        self.log_event(namespace=namespace, op='create', node_id=node_id, actor=actor, request_id=request_id, payload=data)
        self.conn.commit()
        return self.get_node(node_id, include_deleted=True)

    def get_node(self, node_id: str, include_deleted: bool = False) -> dict | None:
        if include_deleted:
            cur = self.conn.execute('SELECT * FROM nodes WHERE node_id=?', (node_id,))
        else:
            cur = self.conn.execute('SELECT * FROM nodes WHERE node_id=? AND deleted_at IS NULL', (node_id,))
        row = cur.fetchone()
        if not row:
            return None
        obj = dict(row)
        obj['content'] = json.loads(obj['content'])
        obj['tags'] = json.loads(obj['tags'])
        return obj

    def update_node(self, node_id: str, patch: dict, if_match: str, actor: str, request_id: str = '') -> tuple[dict | None, str | None]:
        cur_obj = self.get_node(node_id, include_deleted=False)
        if not cur_obj:
            return None, 'NOT_FOUND'
        if if_match and if_match != cur_obj['etag']:
            return None, 'CONFLICT_ETAG'

        title = str(patch.get('title', cur_obj['title']))
        kind = str(patch.get('kind', cur_obj['kind']))
        source = str(patch.get('source', cur_obj['source']))
        content = patch.get('content', cur_obj['content'])
        tags = patch.get('tags', cur_obj['tags'])

        version = int(cur_obj['version']) + 1
        etag = etag_for(version, node_id)
        ts = now_iso()

        self.conn.execute(
            '''
            UPDATE nodes
            SET kind=?, title=?, content=?, tags=?, source=?, version=?, etag=?, updated_at=?
            WHERE node_id=?
            ''',
            (
                kind,
                title,
                json.dumps(content, ensure_ascii=False),
                json.dumps(tags, ensure_ascii=False),
                source,
                version,
                etag,
                ts,
                node_id,
            ),
        )
        self.log_event(namespace=cur_obj['namespace'], op='update', node_id=node_id, actor=actor, request_id=request_id, payload=patch)
        self.conn.commit()
        return self.get_node(node_id, include_deleted=True), None

    def delete_node(self, node_id: str, actor: str, request_id: str = '') -> bool:
        cur_obj = self.get_node(node_id, include_deleted=False)
        if not cur_obj:
            return False
        self.conn.execute('UPDATE nodes SET deleted_at=?, updated_at=? WHERE node_id=?', (now_iso(), now_iso(), node_id))
        self.log_event(namespace=cur_obj['namespace'], op='delete', node_id=node_id, actor=actor, request_id=request_id, payload={})
        self.conn.commit()
        return True

    def search_nodes(self, req: dict) -> dict:
        namespace = str(req.get('namespace', '')).strip()
        query = str(req.get('query', ''))
        k = int(req.get('k', 20))
        kind_filter = set(req.get('kind_filter') or [])
        tag_filter = set(req.get('tag_filter') or [])
        include_deleted = bool(req.get('include_deleted', False))

        if not namespace:
            raise ValueError('namespace is required')

        if include_deleted:
            cur = self.conn.execute('SELECT * FROM nodes WHERE namespace=? ORDER BY updated_at DESC', (namespace,))
        else:
            cur = self.conn.execute('SELECT * FROM nodes WHERE namespace=? AND deleted_at IS NULL ORDER BY updated_at DESC', (namespace,))

        rows = []
        for row in cur.fetchall():
            obj = dict(row)
            obj['content'] = json.loads(obj['content'])
            obj['tags'] = json.loads(obj['tags'])
            if kind_filter and obj['kind'] not in kind_filter:
                continue
            if tag_filter and not tag_filter.intersection(set(obj['tags'])):
                continue
            content_text = obj['content'] if isinstance(obj['content'], str) else json.dumps(obj['content'], ensure_ascii=False)
            if not contains_query(obj['title'], content_text, query):
                continue
            score = 1.0
            if query:
                q = query.lower()
                if q in obj['title'].lower():
                    score = 2.0
            rows.append({'score': score, 'node': obj})
            if len(rows) >= k:
                break

        return {'results': rows, 'next_cursor': None}

    def upsert_edge(self, req: dict, actor: str, request_id: str = '') -> dict:
        from_node_id = str(req.get('from_node_id', '')).strip()
        to_node_id = str(req.get('to_node_id', '')).strip()
        relation = str(req.get('relation', '')).strip()
        weight = float(req.get('weight', 1.0))
        if not from_node_id or not to_node_id or not relation:
            raise ValueError('from_node_id, to_node_id, relation are required')

        f = self.get_node(from_node_id, include_deleted=False)
        t = self.get_node(to_node_id, include_deleted=False)
        if not f or not t:
            raise ValueError('both nodes must exist')

        ts = now_iso()
        self.conn.execute(
            '''
            INSERT INTO edges(from_node_id, to_node_id, relation, weight, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(from_node_id, to_node_id, relation) DO UPDATE SET
              weight=excluded.weight,
              updated_at=excluded.updated_at
            ''',
            (from_node_id, to_node_id, relation, weight, ts),
        )
        self.log_event(namespace=f['namespace'], op='edge_upsert', node_id=from_node_id, actor=actor, request_id=request_id, payload=req)
        self.conn.commit()
        return {
            'from_node_id': from_node_id,
            'to_node_id': to_node_id,
            'relation': relation,
            'weight': weight,
            'updated_at': ts,
        }

    def list_events(self, namespace: str, cursor: int, limit: int = 100) -> dict:
        cur = self.conn.execute(
            '''
            SELECT event_id, namespace, op, node_id, actor, request_id, ts, payload
            FROM events
            WHERE namespace=? AND event_id>?
            ORDER BY event_id ASC
            LIMIT ?
            ''',
            (namespace, cursor, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r['payload'] = json.loads(r['payload'])
        next_cursor = rows[-1]['event_id'] if rows else cursor
        return {'events': rows, 'next_cursor': next_cursor}

    def import_removed(self, namespace: str, canonical_path: Path, actor: str, request_id: str = '') -> dict:
        if not self.get_namespace(namespace):
            raise ValueError('namespace does not exist')
        data = json.loads(canonical_path.read_text(encoding='utf-8'))
        imported = 0
        updated = 0
        for e in data.get('elements', []):
            node_id = f"setup:{e.get('applyRelPath','')}"
            payload = {
                'node_id': node_id,
                'namespace': namespace,
                'kind': 'artifact_ref',
                'title': str(e.get('applyRelPath', 'setup-element')),
                'content': {
                    'lane': e.get('lane'),
                    'class': e.get('class'),
                    'mode': e.get('mode'),
                    'sha256': e.get('sha256'),
                    'sourceRel': e.get('sourceRel'),
                },
                'tags': ['removed', 'setup'],
                'source': 'import',
            }
            existing = self.get_node(node_id, include_deleted=True)
            if not existing:
                self.create_node(payload, actor=actor, request_id=request_id)
                imported += 1
            else:
                self.update_node(node_id, payload, if_match=existing['etag'], actor=actor, request_id=request_id)
                updated += 1
        self.log_event(namespace=namespace, op='import', node_id=None, actor=actor, request_id=request_id, payload={'canonicalPath': str(canonical_path), 'imported': imported, 'updated': updated})
        self.conn.commit()
        return {'namespace': namespace, 'imported': imported, 'updated': updated}

    def log_event(self, namespace: str, op: str, node_id: str | None, actor: str, request_id: str, payload: dict):
        self.conn.execute(
            '''
            INSERT INTO events(namespace, op, node_id, actor, request_id, ts, payload)
            VALUES(?,?,?,?,?,?,?)
            ''',
            (namespace, op, node_id, actor or 'unknown', request_id or None, now_iso(), event_payload(payload)),
        )
