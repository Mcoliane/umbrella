#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.append(str(Path(__file__).resolve().parents[2]))

from services.memory.auth import check_auth
from services.memory.config import load_config
from services.memory.store import MemoryStore


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def parse_json(handler: BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get('Content-Length', '0'))
    raw = handler.rfile.read(n) if n > 0 else b'{}'
    try:
        return json.loads(raw.decode('utf-8') or '{}')
    except Exception:
        return {}


def err(code: str, message: str, request_id: str) -> dict:
    return {'error': {'code': code, 'message': message, 'request_id': request_id}}


def handler_factory(store: MemoryStore, token: str, root: Path):
    class Handler(BaseHTTPRequestHandler):
        def _request_id(self) -> str:
            return self.headers.get('X-Request-Id', '').strip() or str(uuid.uuid4())

        def _actor(self) -> str:
            return self.headers.get('X-Actor', '').strip() or 'api'

        def _auth_ok(self, req_id: str) -> bool:
            if check_auth(self.headers.get('Authorization', ''), token):
                return True
            json_response(self, 401, err('UNAUTHORIZED', 'missing or invalid bearer token', req_id))
            return False

        def do_GET(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return

            u = urlparse(self.path)
            path = u.path
            if path == '/v1/memory/health':
                return json_response(self, 200, {'status': 'ok'})

            if path.startswith('/v1/namespaces/'):
                ns = path.split('/v1/namespaces/', 1)[1]
                obj = store.get_namespace(ns)
                if not obj:
                    return json_response(self, 404, err('NOT_FOUND', 'namespace not found', req_id))
                return json_response(self, 200, obj)

            if path.startswith('/v1/nodes/'):
                node_id = path.split('/v1/nodes/', 1)[1]
                obj = store.get_node(node_id)
                if not obj:
                    return json_response(self, 404, err('NOT_FOUND', 'node not found', req_id))
                return json_response(self, 200, obj)

            if path == '/v1/events':
                q = parse_qs(u.query)
                namespace = (q.get('namespace') or [''])[0]
                if not namespace:
                    return json_response(self, 400, err('VALIDATION_ERROR', 'namespace query param is required', req_id))
                cursor = int((q.get('cursor') or ['0'])[0])
                out = store.list_events(namespace=namespace, cursor=cursor)
                return json_response(self, 200, out)

            if path == '/v1/promotions/dlq':
                q = parse_qs(u.query)
                limit = int((q.get('limit') or ['100'])[0])
                out = store.list_promotion_dlq(limit=limit)
                return json_response(self, 200, out)

            if path == '/v1/memory/boundary/stats':
                out = store.boundary_stats()
                return json_response(self, 200, out)

            if path == '/v1/memory/boundary/metrics':
                body = store.boundary_metrics_text().encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def do_POST(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return

            path = urlparse(self.path).path
            body = parse_json(self)
            actor = self._actor()
            try:
                if path == '/v1/namespaces':
                    out = store.upsert_namespace(body)
                    return json_response(self, 200, out)
                if path == '/v1/nodes':
                    out = store.create_node(body, actor=actor, request_id=req_id)
                    return json_response(self, 200, out)
                if path == '/v1/nodes/search':
                    out = store.search_nodes(body)
                    return json_response(self, 200, out)
                if path == '/v1/edges/upsert':
                    out = store.upsert_edge(body, actor=actor, request_id=req_id)
                    return json_response(self, 200, out)
                if path == '/v1/promotions':
                    out = store.promote_from_memory_core(body, actor=actor, request_id=req_id)
                    return json_response(self, 200, out)
                if path == '/v1/promotions/queue':
                    out = store.enqueue_promotion(body, actor=actor, request_id=req_id)
                    return json_response(self, 200, out)
                if path == '/v1/promotions/process-queue':
                    max_items = int(body.get('maxItems', 50))
                    out = store.process_promotion_queue(actor=actor, request_id=req_id, max_items=max_items)
                    return json_response(self, 200, out)
                if path == '/v1/promotions/replay-dlq':
                    max_items = int(body.get('maxItems', 20))
                    out = store.replay_promotion_dlq(actor=actor, request_id=req_id, max_items=max_items)
                    return json_response(self, 200, out)
                if path == '/v1/hydrations/payload':
                    out = store.hydration_payload_for_memory_core(body, actor=actor, request_id=req_id)
                    return json_response(self, 200, out)
                if path == '/v1/import/removed':
                    namespace = str(body.get('namespace', '')).strip()
                    canonical_rel = str(body.get('canonical_path', 'umbrella/memory-core/canonical/removed-setup-elements.json'))
                    canonical_path = (root / canonical_rel).resolve()
                    if not canonical_path.exists():
                        return json_response(self, 404, err('NOT_FOUND', 'canonical file not found', req_id))
                    out = store.import_removed(namespace=namespace, canonical_path=canonical_path, actor=actor, request_id=req_id)
                    return json_response(self, 200, out)
                return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
            except ValueError as ex:
                return json_response(self, 400, err('VALIDATION_ERROR', str(ex), req_id))
            except Exception as ex:
                return json_response(self, 500, err('INTERNAL', str(ex), req_id))

        def do_PATCH(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            if not path.startswith('/v1/nodes/'):
                return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
            node_id = path.split('/v1/nodes/', 1)[1]
            body = parse_json(self)
            actor = self._actor()
            if_match = self.headers.get('If-Match', '').strip()
            try:
                out, problem = store.update_node(node_id=node_id, patch=body, if_match=if_match, actor=actor, request_id=req_id)
                if problem == 'NOT_FOUND':
                    return json_response(self, 404, err('NOT_FOUND', 'node not found', req_id))
                if problem == 'CONFLICT_ETAG':
                    return json_response(self, 409, err('CONFLICT_ETAG', 'etag mismatch', req_id))
                return json_response(self, 200, out)
            except ValueError as ex:
                return json_response(self, 400, err('VALIDATION_ERROR', str(ex), req_id))
            except Exception as ex:
                return json_response(self, 500, err('INTERNAL', str(ex), req_id))

        def do_DELETE(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            if not path.startswith('/v1/nodes/'):
                return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
            node_id = path.split('/v1/nodes/', 1)[1]
            actor = self._actor()
            ok = store.delete_node(node_id=node_id, actor=actor, request_id=req_id)
            if not ok:
                return json_response(self, 404, err('NOT_FOUND', 'node not found', req_id))
            return json_response(self, 200, {'ok': True, 'node_id': node_id})

        def log_message(self, fmt: str, *args):
            return

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description='Umbrella Memory Service v1')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8787)
    ap.add_argument('--db-path', default='')
    ap.add_argument('--umbrella-root', default=str(Path(__file__).resolve().parents[2]))
    args = ap.parse_args()

    cfg = load_config(host=args.host, port=args.port, db_path=args.db_path)
    root = Path(args.umbrella_root).resolve()

    store = MemoryStore(cfg.db_path)
    migration = (root / 'services' / 'memory' / 'db' / 'migrations' / '001_init.sql').read_text(encoding='utf-8')
    store.init_db(migration)

    Handler = handler_factory(store=store, token=cfg.token, root=root)
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    print(json.dumps({'status': 'listening', 'host': cfg.host, 'port': cfg.port, 'dbPath': str(cfg.db_path)}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
