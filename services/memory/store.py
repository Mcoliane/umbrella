#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
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
    MAX_REPLAY_ATTEMPTS = 5

    def __init__(self, db_path: Path, boundary_root: Path | None = None):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self.boundary_root = boundary_root if boundary_root is not None else self.db_path.parent.parent / 'memory-boundary'
        self.promotion_queue_dir = self.boundary_root / 'promotion-queue'
        self.promotion_dlq_dir = self.boundary_root / 'promotion-dlq'
        self.promotion_processed_dir = self.boundary_root / 'promotion-processed'
        self.promotion_parked_dir = self.boundary_root / 'promotion-parked'
        self.promotion_queue_dir.mkdir(parents=True, exist_ok=True)
        self.promotion_dlq_dir.mkdir(parents=True, exist_ok=True)
        self.promotion_processed_dir.mkdir(parents=True, exist_ok=True)
        self.promotion_parked_dir.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=5000')
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def init_db(self, migration_sql: str):
        with self._lock:
            self.conn.executescript(migration_sql)
            self.conn.commit()

    def upsert_namespace(self, data: dict) -> dict:
        with self._lock:
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
        with self._lock:
            cur = self.conn.execute('SELECT * FROM namespaces WHERE id=?', (ns_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def create_node(self, data: dict, actor: str, request_id: str = '') -> dict:
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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

    def restore_node(self, node_id: str, patch: dict, actor: str, request_id: str = '') -> tuple[dict | None, str | None]:
        with self._lock:
            cur_obj = self.get_node(node_id, include_deleted=True)
            if not cur_obj:
                return None, 'NOT_FOUND'
            if cur_obj.get('deleted_at') is None:
                return None, 'NOT_DELETED'

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
                SET kind=?, title=?, content=?, tags=?, source=?, version=?, etag=?, updated_at=?, deleted_at=NULL
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
            self.log_event(namespace=cur_obj['namespace'], op='restore', node_id=node_id, actor=actor, request_id=request_id, payload=patch)
            self.conn.commit()
            return self.get_node(node_id, include_deleted=True), None

    def delete_node(self, node_id: str, actor: str, request_id: str = '') -> bool:
        with self._lock:
            cur_obj = self.get_node(node_id, include_deleted=False)
            if not cur_obj:
                return False
            self.conn.execute('UPDATE nodes SET deleted_at=?, updated_at=? WHERE node_id=?', (now_iso(), now_iso(), node_id))
            self.log_event(namespace=cur_obj['namespace'], op='delete', node_id=node_id, actor=actor, request_id=request_id, payload={})
            self.conn.commit()
            return True

    def search_nodes(self, req: dict) -> dict:
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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

    def promote_from_memory_core(self, req: dict, actor: str, request_id: str = '') -> dict:
        with self._lock:
            source = req.get('source') if isinstance(req.get('source'), dict) else {}
            target = req.get('target') if isinstance(req.get('target'), dict) else {}
            provenance = req.get('provenance') if isinstance(req.get('provenance'), dict) else {}

            source_namespace = str(source.get('namespace', '')).strip()
            source_key = str(source.get('key', '')).strip()
            if not source_namespace or not source_key:
                raise ValueError('source.namespace and source.key are required')

            namespace = str(target.get('namespace', '')).strip() or source_namespace
            node_id = str(target.get('node_id', '')).strip() or f'fact:{source_namespace}:{source_key}'
            title = str(target.get('title', '')).strip() or f'{source_namespace}:{source_key}'
            kind = str(target.get('kind', 'fact')).strip() or 'fact'
            tags = target.get('tags')
            if not isinstance(tags, list):
                tags = ['memory-core', 'promoted']

            if not self.get_namespace(namespace):
                self.upsert_namespace(
                    {
                        'id': namespace,
                        'owner_type': 'system',
                        'owner_id': 'umbrella',
                        'visibility': 'shared',
                        'retention_days': None,
                    }
                )

            payload = {
                'node_id': node_id,
                'namespace': namespace,
                'kind': kind,
                'title': title,
                'content': {
                    'value': source.get('value'),
                    'metadata': source.get('metadata') if isinstance(source.get('metadata'), dict) else {},
                    'sourceNamespace': source_namespace,
                    'sourceKey': source_key,
                    'promotedAt': now_iso(),
                    'provenance': provenance,
                },
                'tags': tags,
                'source': str(target.get('source', 'memory-core-promotion')),
            }

            mode = 'created'
            existing = self.get_node(node_id, include_deleted=True)
            if existing:
                if existing.get('deleted_at') is not None:
                    mode = 'restored'
                    out, problem = self.restore_node(node_id=node_id, patch=payload, actor=actor, request_id=request_id)
                else:
                    mode = 'updated'
                    out, problem = self.update_node(node_id=node_id, patch=payload, if_match=existing['etag'], actor=actor, request_id=request_id)
                if problem:
                    raise ValueError(f'promotion update failed: {problem}')
                node = out
            else:
                node = self.create_node(payload, actor=actor, request_id=request_id)

            result = {
                'ok': True,
                'mode': mode,
                'source': {'namespace': source_namespace, 'key': source_key},
                'node': node,
                'provenance': provenance,
            }
            self.log_event(namespace=namespace, op='promote', node_id=node_id, actor=actor, request_id=request_id, payload=result)
            self.conn.commit()
            return result

    def _promotion_token(self) -> str:
        return f'{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")}-{uuid.uuid4().hex[:10]}'

    def _queue_path(self, token: str) -> Path:
        return self.promotion_queue_dir / f'{token}.json'

    def _dlq_path(self, token: str) -> Path:
        return self.promotion_dlq_dir / f'{token}.json'

    def _processed_path(self, token: str) -> Path:
        return self.promotion_processed_dir / f'{token}.json'

    def _parked_path(self, token: str) -> Path:
        return self.promotion_parked_dir / f'{token}.json'

    def _metrics_path(self) -> Path:
        return self.boundary_root / 'metrics.json'

    @staticmethod
    def _write_json_atomic(path: Path, obj: dict):
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(obj, indent=2) + '\n', encoding='utf-8')
        os.replace(tmp, path)

    @staticmethod
    def validate_promotion_payload(req: dict):
        source = req.get('source') if isinstance(req.get('source'), dict) else {}
        source_namespace = str(source.get('namespace', '')).strip()
        source_key = str(source.get('key', '')).strip()
        if not source_namespace or not source_key:
            raise ValueError('source.namespace and source.key are required')

    def _park_dlq_entry(self, p: Path, entry: dict, reason: str) -> bool:
        parked = dict(entry)
        parked['status'] = 'PARKED'
        parked['parkedAt'] = now_iso()
        parked['parkReason'] = reason
        try:
            self._write_json_atomic(self._parked_path(p.stem), parked)
        except Exception:
            return False
        try:
            p.unlink()
        except Exception:
            pass
        return True

    def _load_metrics(self) -> dict:
        with self._lock:
            p = self._metrics_path()
            if not p.exists():
                return {
                    'queuedTotal': 0,
                    'processedTotal': 0,
                    'succeededTotal': 0,
                    'failedTotal': 0,
                    'replayedTotal': 0,
                    'replaySucceededTotal': 0,
                    'replayFailedTotal': 0,
                    'parkedTotal': 0,
                    'updatedAt': now_iso(),
                }
            try:
                row = json.loads(p.read_text(encoding='utf-8'))
            except Exception:
                row = {}
            out = {
                'queuedTotal': int(row.get('queuedTotal', 0)),
                'processedTotal': int(row.get('processedTotal', 0)),
                'succeededTotal': int(row.get('succeededTotal', 0)),
                'failedTotal': int(row.get('failedTotal', 0)),
                'replayedTotal': int(row.get('replayedTotal', 0)),
                'replaySucceededTotal': int(row.get('replaySucceededTotal', 0)),
                'replayFailedTotal': int(row.get('replayFailedTotal', 0)),
                'parkedTotal': int(row.get('parkedTotal', 0)),
                'updatedAt': str(row.get('updatedAt', now_iso())),
            }
            return out

    def _save_metrics(self, metrics: dict):
        with self._lock:
            metrics['updatedAt'] = now_iso()
            self._write_json_atomic(self._metrics_path(), metrics)

    def _bump_metrics(self, **counts: int):
        m = self._load_metrics()
        for k, v in counts.items():
            if k in m:
                m[k] = int(m.get(k, 0)) + int(v)
        self._save_metrics(m)

    def _oldest_age_seconds(self, folder: Path) -> float:
        files = sorted(folder.glob('*.json'))
        if not files:
            return 0.0
        oldest = min(f.stat().st_mtime for f in files)
        now_ts = datetime.now(timezone.utc).timestamp()
        age = now_ts - float(oldest)
        return age if age > 0 else 0.0

    def enqueue_promotion(self, req: dict, actor: str, request_id: str = '', drain: bool = True) -> dict:
        with self._lock:
            self.validate_promotion_payload(req)
            token = self._promotion_token()
            entry = {
                'token': token,
                'queuedAt': now_iso(),
                'attempts': 0,
                'actor': actor,
                'requestId': request_id,
                'payload': req,
            }
            self._write_json_atomic(self._queue_path(token), entry)
            self._bump_metrics(queuedTotal=1)
            out = {'ok': True, 'queued': True, 'token': token, 'queuePath': str(self._queue_path(token))}
            if drain:
                try:
                    out['drain'] = self.process_promotion_queue(actor=actor, request_id=request_id)
                except Exception as ex:
                    out['drain'] = {'ok': False, 'error': str(ex)}
            return out

    def process_promotion_queue(self, actor: str, request_id: str = '', max_items: int = 50) -> dict:
        with self._lock:
            processed = 0
            succeeded = 0
            failed = 0
            dlq_tokens: list[str] = []
            for p in sorted(self.promotion_queue_dir.glob('*.json')):
                if processed >= max_items:
                    break
                processed += 1
                token = p.stem
                try:
                    entry = json.loads(p.read_text(encoding='utf-8'))
                except Exception:
                    entry = {'payload': {}, 'attempts': 0}
                attempts = int(entry.get('attempts', 0)) + 1
                payload = entry.get('payload') if isinstance(entry.get('payload'), dict) else {}
                try:
                    out = self.promote_from_memory_core(payload, actor=actor, request_id=request_id)
                except Exception as ex:
                    failed += 1
                    failed_entry = {
                        'token': token,
                        'status': 'FAILED',
                        'failedAt': now_iso(),
                        'attempts': attempts,
                        'lastError': str(ex),
                        'payload': payload,
                        'actor': actor,
                        'requestId': request_id,
                    }
                    try:
                        self._write_json_atomic(self._dlq_path(token), failed_entry)
                    except Exception:
                        # DLQ write failed: keep the queue entry so nothing is lost.
                        continue
                    dlq_tokens.append(token)
                    try:
                        p.unlink()
                    except Exception:
                        pass
                    continue
                done = {
                    'token': token,
                    'status': 'SUCCESS',
                    'processedAt': now_iso(),
                    'attempts': attempts,
                    'result': out,
                }
                try:
                    self._write_json_atomic(self._processed_path(token), done)
                except Exception:
                    pass
                succeeded += 1
                try:
                    p.unlink()
                except Exception:
                    pass

            self._bump_metrics(processedTotal=processed, succeededTotal=succeeded, failedTotal=failed)

            return {
                'ok': True,
                'processed': processed,
                'succeeded': succeeded,
                'failed': failed,
                'dlqTokens': dlq_tokens,
                'queueDepth': len(list(self.promotion_queue_dir.glob('*.json'))),
                'dlqDepth': len(list(self.promotion_dlq_dir.glob('*.json'))),
            }

    def list_promotion_dlq(self, limit: int = 100) -> dict:
        with self._lock:
            rows = []
            for p in sorted(self.promotion_dlq_dir.glob('*.json'))[: max(1, limit)]:
                try:
                    rows.append(json.loads(p.read_text(encoding='utf-8')))
                except Exception:
                    rows.append({'token': p.stem, 'status': 'FAILED', 'parseError': True})
            return {'ok': True, 'count': len(rows), 'entries': rows}

    def replay_promotion_dlq(self, actor: str, request_id: str = '', max_items: int = 20, max_attempts: int = MAX_REPLAY_ATTEMPTS) -> dict:
        with self._lock:
            replayed = 0
            succeeded = 0
            failed = 0
            parked = 0
            for p in sorted(self.promotion_dlq_dir.glob('*.json')):
                if replayed + parked >= max_items:
                    break
                token = p.stem
                try:
                    entry = json.loads(p.read_text(encoding='utf-8'))
                except Exception:
                    # Preserve the original bytes in the parked record — the
                    # unreadable file is the only copy of the payload.
                    try:
                        raw = p.read_text(encoding='utf-8', errors='replace')
                    except OSError:
                        failed += 1
                        continue
                    if self._park_dlq_entry(p, {'token': token, 'parseError': True, 'raw': raw}, 'dlq_entry_unreadable'):
                        parked += 1
                    else:
                        failed += 1
                    continue
                payload = entry.get('payload') if isinstance(entry.get('payload'), dict) else {}
                prior_attempts = int(entry.get('attempts', 0))
                park_reason = ''
                try:
                    self.validate_promotion_payload(payload)
                except ValueError as ex:
                    park_reason = f'payload_validation_failed: {ex}'
                if not park_reason and prior_attempts >= max_attempts:
                    park_reason = f'max_attempts_exceeded: {prior_attempts} >= {max_attempts}'
                if park_reason:
                    if self._park_dlq_entry(p, entry, park_reason):
                        parked += 1
                    else:
                        failed += 1
                    continue
                replayed += 1
                attempts = prior_attempts + 1
                try:
                    out = self.promote_from_memory_core(payload, actor=actor, request_id=request_id)
                    done = {
                        'token': token,
                        'status': 'SUCCESS',
                        'replayedAt': now_iso(),
                        'attempts': attempts,
                        'result': out,
                    }
                    self._write_json_atomic(self._processed_path(token), done)
                    try:
                        p.unlink()
                    except Exception:
                        pass
                    succeeded += 1
                except Exception as ex:
                    failed += 1
                    entry['attempts'] = attempts
                    entry['lastError'] = str(ex)
                    entry['failedAt'] = now_iso()
                    self._write_json_atomic(p, entry)
            self._bump_metrics(replayedTotal=replayed, replaySucceededTotal=succeeded, replayFailedTotal=failed, parkedTotal=parked)
            return {
                'ok': True,
                'replayed': replayed,
                'succeeded': succeeded,
                'failed': failed,
                'parked': parked,
                'dlqDepth': len(list(self.promotion_dlq_dir.glob('*.json'))),
                'parkedDepth': len(list(self.promotion_parked_dir.glob('*.json'))),
            }

    def boundary_stats(self) -> dict:
        with self._lock:
            m = self._load_metrics()
            processed_total = int(m.get('processedTotal', 0))
            replayed_total = int(m.get('replayedTotal', 0))
            failure_rate = float(m.get('failedTotal', 0)) / float(processed_total) if processed_total > 0 else 0.0
            replay_success_rate = float(m.get('replaySucceededTotal', 0)) / float(replayed_total) if replayed_total > 0 else 0.0
            return {
                'ok': True,
                'promotionQueueDepth': len(list(self.promotion_queue_dir.glob('*.json'))),
                'promotionDlqDepth': len(list(self.promotion_dlq_dir.glob('*.json'))),
                'promotionProcessedCount': len(list(self.promotion_processed_dir.glob('*.json'))),
                'promotionParkedCount': len(list(self.promotion_parked_dir.glob('*.json'))),
                'promotionQueueOldestAgeSec': round(self._oldest_age_seconds(self.promotion_queue_dir), 3),
                'promotionDlqOldestAgeSec': round(self._oldest_age_seconds(self.promotion_dlq_dir), 3),
                'counters': m,
                'slo': {
                    'promotionFailureRate': round(failure_rate, 6),
                    'promotionReplaySuccessRate': round(replay_success_rate, 6),
                },
                'checkedAt': now_iso(),
            }

    def boundary_metrics_text(self) -> str:
        stats = self.boundary_stats()
        counters = stats.get('counters') if isinstance(stats.get('counters'), dict) else {}
        lines = [
            '# HELP umbrella_memory_promotion_queue_depth Current promotion queue depth',
            '# TYPE umbrella_memory_promotion_queue_depth gauge',
            f"umbrella_memory_promotion_queue_depth {int(stats.get('promotionQueueDepth', 0))}",
            '# HELP umbrella_memory_promotion_dlq_depth Current promotion DLQ depth',
            '# TYPE umbrella_memory_promotion_dlq_depth gauge',
            f"umbrella_memory_promotion_dlq_depth {int(stats.get('promotionDlqDepth', 0))}",
            '# HELP umbrella_memory_promotion_parked_depth Current parked promotion count',
            '# TYPE umbrella_memory_promotion_parked_depth gauge',
            f"umbrella_memory_promotion_parked_depth {int(stats.get('promotionParkedCount', 0))}",
            '# HELP umbrella_memory_promotion_processed_total Total processed promotions',
            '# TYPE umbrella_memory_promotion_processed_total counter',
            f"umbrella_memory_promotion_processed_total {int(counters.get('processedTotal', 0))}",
            '# HELP umbrella_memory_promotion_failed_total Total failed promotions',
            '# TYPE umbrella_memory_promotion_failed_total counter',
            f"umbrella_memory_promotion_failed_total {int(counters.get('failedTotal', 0))}",
            '# HELP umbrella_memory_promotion_failure_rate Promotion failure rate',
            '# TYPE umbrella_memory_promotion_failure_rate gauge',
            f"umbrella_memory_promotion_failure_rate {float((stats.get('slo') or {}).get('promotionFailureRate', 0.0))}",
            '# HELP umbrella_memory_promotion_replay_success_rate DLQ replay success rate',
            '# TYPE umbrella_memory_promotion_replay_success_rate gauge',
            f"umbrella_memory_promotion_replay_success_rate {float((stats.get('slo') or {}).get('promotionReplaySuccessRate', 0.0))}",
            '# HELP umbrella_memory_promotion_queue_oldest_age_seconds Age of oldest queue item in seconds',
            '# TYPE umbrella_memory_promotion_queue_oldest_age_seconds gauge',
            f"umbrella_memory_promotion_queue_oldest_age_seconds {float(stats.get('promotionQueueOldestAgeSec', 0.0))}",
            '# HELP umbrella_memory_promotion_dlq_oldest_age_seconds Age of oldest DLQ item in seconds',
            '# TYPE umbrella_memory_promotion_dlq_oldest_age_seconds gauge',
            f"umbrella_memory_promotion_dlq_oldest_age_seconds {float(stats.get('promotionDlqOldestAgeSec', 0.0))}",
        ]
        return '\n'.join(lines) + '\n'

    def hydration_payload_for_memory_core(self, req: dict, actor: str, request_id: str = '') -> dict:
        with self._lock:
            node_id = str(req.get('node_id', '')).strip()
            target = req.get('target') if isinstance(req.get('target'), dict) else {}
            context = req.get('context') if isinstance(req.get('context'), dict) else {}
            phase = str(context.get('phase', '')).strip().lower()
            if not node_id:
                raise ValueError('node_id is required')
            if phase not in {'bootstrap', 'resume'}:
                raise ValueError('context.phase must be bootstrap or resume')

            node = self.get_node(node_id, include_deleted=False)
            if not node:
                raise ValueError('node not found')

            target_namespace = str(target.get('namespace', '')).strip() or str(node.get('namespace', ''))
            target_key = str(target.get('key', '')).strip() or f'hydrate:{node_id}'
            content = node.get('content')
            value = content.get('value') if isinstance(content, dict) and 'value' in content else content
            metadata = {
                'hydrateFromNodeId': node_id,
                'hydrateFromEtag': str(node.get('etag', '')),
                'hydrateFromVersion': int(node.get('version', 0)),
                'hydratedAt': now_iso(),
                'actor': actor,
            }

            out = {
                'ok': True,
                'target': {'namespace': target_namespace, 'key': target_key},
                'memoryCore': {'namespace': target_namespace, 'key': target_key, 'value': value, 'metadata': metadata},
                'node': {'node_id': node_id, 'namespace': node.get('namespace', ''), 'kind': node.get('kind', ''), 'title': node.get('title', '')},
                'context': {'phase': phase},
            }
            self.log_event(
                namespace=str(node.get('namespace', '')),
                op='hydrate',
                node_id=node_id,
                actor=actor,
                request_id=request_id,
                payload={'target': out['target'], 'fromNode': out['node']},
            )
            self.conn.commit()
            return out

    def log_event(self, namespace: str, op: str, node_id: str | None, actor: str, request_id: str, payload: dict):
        with self._lock:
            self.conn.execute(
                '''
                INSERT INTO events(namespace, op, node_id, actor, request_id, ts, payload)
                VALUES(?,?,?,?,?,?,?)
                ''',
                (namespace, op, node_id, actor or 'unknown', request_id or None, now_iso(), event_payload(payload)),
            )
