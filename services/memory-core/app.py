#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.append(str(Path(__file__).resolve().parents[2]))
from services.memory.auth import check_auth


ALLOWED_NAMESPACES = {'agent', 'team', 'global'}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class MemoryStore:
    def __init__(self, umbrella_root: Path):
        self.root = umbrella_root
        self.store_path = self.root / 'control-plane' / 'memory-core' / 'store.json'
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.store_path.exists():
            self._write({'namespaces': {'agent': {}, 'team': {}, 'global': {}}, 'updatedAt': now_iso()})

    def _read(self) -> dict:
        try:
            return json.loads(self.store_path.read_text(encoding='utf-8'))
        except Exception:
            return {'namespaces': {'agent': {}, 'team': {}, 'global': {}}, 'updatedAt': now_iso()}

    def _write(self, data: dict):
        data['updatedAt'] = now_iso()
        self.store_path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')

    def _ensure_namespace(self, data: dict, namespace: str):
        if 'namespaces' not in data or not isinstance(data['namespaces'], dict):
            data['namespaces'] = {}
        if namespace not in data['namespaces'] or not isinstance(data['namespaces'][namespace], dict):
            data['namespaces'][namespace] = {}

    def put(self, namespace: str, key: str, value, metadata: dict | None = None) -> dict:
        data = self._read()
        self._ensure_namespace(data, namespace)
        row = {
            'namespace': namespace,
            'key': key,
            'value': value,
            'metadata': metadata or {},
            'updatedAt': now_iso(),
        }
        data['namespaces'][namespace][key] = row
        self._write(data)
        return row

    def get(self, namespace: str, key: str) -> dict | None:
        data = self._read()
        self._ensure_namespace(data, namespace)
        row = data['namespaces'][namespace].get(key)
        return row if isinstance(row, dict) else None

    def delete(self, namespace: str, key: str) -> bool:
        data = self._read()
        self._ensure_namespace(data, namespace)
        exists = key in data['namespaces'][namespace]
        if exists:
            del data['namespaces'][namespace][key]
            self._write(data)
        return exists

    def list(self, namespace: str) -> list[dict]:
        data = self._read()
        self._ensure_namespace(data, namespace)
        out = []
        for k in sorted(data['namespaces'][namespace].keys()):
            row = data['namespaces'][namespace][k]
            if isinstance(row, dict):
                out.append(row)
        return out


def handler_factory(store: MemoryStore, token: str):
    class Handler(BaseHTTPRequestHandler):
        def _request_id(self) -> str:
            return self.headers.get('X-Request-Id', '').strip() or str(uuid.uuid4())

        def _auth_ok(self, req_id: str) -> bool:
            if check_auth(self.headers.get('Authorization', ''), token):
                return True
            json_response(self, 401, err('UNAUTHORIZED', 'missing or invalid bearer token', req_id))
            return False

        def _validate_ns(self, namespace: str, req_id: str) -> bool:
            if namespace in ALLOWED_NAMESPACES:
                return True
            json_response(self, 400, err('VALIDATION_ERROR', 'namespace must be one of agent|team|global', req_id))
            return False

        def do_GET(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            if path == '/v1/memory-core/health':
                return json_response(self, 200, {'status': 'ok', 'service': 'memory-core', 'checkedAt': now_iso()})
            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def do_POST(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            body = parse_json(self)
            ns = str(body.get('namespace', '')).strip()

            if path == '/v1/memory-core/put':
                key = str(body.get('key', '')).strip()
                if not ns or not key:
                    return json_response(self, 400, err('VALIDATION_ERROR', 'namespace and key are required', req_id))
                if not self._validate_ns(ns, req_id):
                    return
                row = store.put(ns, key, body.get('value'), body.get('metadata') if isinstance(body.get('metadata'), dict) else {})
                return json_response(self, 200, {'ok': True, 'memory': row})

            if path == '/v1/memory-core/get':
                key = str(body.get('key', '')).strip()
                if not ns or not key:
                    return json_response(self, 400, err('VALIDATION_ERROR', 'namespace and key are required', req_id))
                if not self._validate_ns(ns, req_id):
                    return
                row = store.get(ns, key)
                return json_response(self, 200, {'ok': True, 'exists': row is not None, 'memory': row or {}})

            if path == '/v1/memory-core/delete':
                key = str(body.get('key', '')).strip()
                if not ns or not key:
                    return json_response(self, 400, err('VALIDATION_ERROR', 'namespace and key are required', req_id))
                if not self._validate_ns(ns, req_id):
                    return
                existed = store.delete(ns, key)
                return json_response(self, 200, {'ok': True, 'deleted': existed, 'namespace': ns, 'key': key})

            if path == '/v1/memory-core/list':
                if not ns:
                    return json_response(self, 400, err('VALIDATION_ERROR', 'namespace is required', req_id))
                if not self._validate_ns(ns, req_id):
                    return
                rows = store.list(ns)
                return json_response(self, 200, {'ok': True, 'namespace': ns, 'count': len(rows), 'entries': rows})

            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def log_message(self, fmt: str, *args):
            return

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description='Umbrella Memory Core Service')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8798)
    ap.add_argument('--umbrella-root', default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument('--token', default='')
    args = ap.parse_args()

    root = Path(args.umbrella_root).resolve()
    store = MemoryStore(umbrella_root=root)
    handler = handler_factory(store=store, token=args.token.strip())
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({'status': 'listening', 'service': 'memory-core', 'host': args.host, 'port': args.port}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
