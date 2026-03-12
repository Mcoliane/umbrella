#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.append(str(Path(__file__).resolve().parents[2]))
from services.memory.auth import check_auth


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


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


class RouterEngine:
    def __init__(self, routing_path: Path, catalog_url: str = '', mesh_token: str = ''):
        self.routing_path = routing_path
        self.config = load_json(routing_path, {})
        self.catalog_url = catalog_url.rstrip('/')
        self.mesh_token = mesh_token.strip()

    def _headers(self) -> dict:
        headers = {}
        if self.mesh_token:
            headers['Authorization'] = f'Bearer {self.mesh_token}'
        return headers

    def _catalog_action(self, action_id: str, timeout: int = 15) -> dict | None:
        if not self.catalog_url:
            return None
        req = urllib.request.Request(
            f'{self.catalog_url}/v1/catalog/actions/{urllib.parse.quote(action_id, safe="")}',
            method='GET',
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                return None
            raise

    def config_payload(self) -> dict:
        return {
            'loadedAt': now_iso(),
            'path': str(self.routing_path),
            'config': self.config,
            'catalogUrl': self.catalog_url,
        }

    def route_step(self, step: dict) -> dict:
        step_id = str(step.get('stepId') or step.get('id') or '').strip()
        action = str(step.get('action', '')).strip()

        catalog_action = self._catalog_action(action)
        if isinstance(catalog_action, dict):
            return {
                'routed': True,
                'runtime': 'plugin-host',
                'reason': 'catalog_action',
                'stepId': step_id,
                'action': action,
                'catalogAction': catalog_action,
            }

        rules = self.config.get('rules') or []
        for r in rules:
            prefix = str(r.get('matchStepPrefix', '')).strip()
            if not prefix:
                continue
            if step_id.startswith(prefix) or action.startswith(prefix):
                return {
                    'routed': True,
                    'runtime': r.get('runtime', self.config.get('defaultRuntime', 'removed')),
                    'reason': f'matched_prefix:{prefix}',
                    'stepId': step_id,
                    'action': action,
                }

        return {
            'routed': True,
            'runtime': self.config.get('defaultRuntime', 'removed'),
            'reason': 'default_runtime',
            'stepId': step_id,
            'action': action,
        }

    def reroute_step(self, from_runtime: str, step: dict) -> dict:
        reroute = (self.config.get('reroute') or {})
        enabled = bool(reroute.get('enabled', False))
        if not enabled:
            return {
                'rerouted': False,
                'reason': 'reroute_disabled',
                'fromRuntime': from_runtime,
                'toRuntime': None,
            }

        fb = (reroute.get('fallbackRuntimes') or {}).get(from_runtime, [])
        if not fb:
            return {
                'rerouted': False,
                'reason': 'no_fallback_runtime',
                'fromRuntime': from_runtime,
                'toRuntime': None,
            }

        to_runtime = fb[0]
        route = self.route_step(step)
        return {
            'rerouted': True,
            'reason': 'fallback_runtime',
            'fromRuntime': from_runtime,
            'toRuntime': to_runtime,
            'stepRoute': route,
        }


def handler_factory(engine: RouterEngine, token: str):
    class Handler(BaseHTTPRequestHandler):
        def _request_id(self) -> str:
            return self.headers.get('X-Request-Id', '').strip() or str(uuid.uuid4())

        def _auth_ok(self, req_id: str) -> bool:
            if check_auth(self.headers.get('Authorization', ''), token):
                return True
            json_response(self, 401, err('UNAUTHORIZED', 'missing or invalid bearer token', req_id))
            return False

        def do_GET(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            if path == '/v1/router/health':
                return json_response(self, 200, {'status': 'ok', 'service': 'router', 'checkedAt': now_iso()})
            if path == '/v1/router/config':
                return json_response(self, 200, engine.config_payload())
            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def do_POST(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            body = parse_json(self)

            if path == '/v1/router/route-step':
                step = body.get('step') or {}
                out = engine.route_step(step)
                return json_response(self, 200, out)

            if path == '/v1/router/reroute-step':
                step = body.get('step') or {}
                from_runtime = str(body.get('fromRuntime', 'removed'))
                out = engine.reroute_step(from_runtime=from_runtime, step=step)
                return json_response(self, 200, out)

            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def log_message(self, fmt: str, *args):
            return

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description='Umbrella Router Service')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8795)
    ap.add_argument('--umbrella-root', default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument('--routing', default='control-plane/router/runtime-routing.json')
    ap.add_argument('--catalog-url', default='')
    ap.add_argument('--mesh-token', default='')
    ap.add_argument('--token', default='')
    args = ap.parse_args()

    root = Path(args.umbrella_root).resolve()
    engine = RouterEngine(routing_path=(root / args.routing), catalog_url=args.catalog_url, mesh_token=args.mesh_token)
    handler = handler_factory(engine=engine, token=args.token.strip())
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({'status': 'listening', 'service': 'router', 'host': args.host, 'port': args.port}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
