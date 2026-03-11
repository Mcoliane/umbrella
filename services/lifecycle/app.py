#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.append(str(Path(__file__).resolve().parents[2]))
from services.memory.auth import check_auth
sys.path.append(str(Path(__file__).resolve().parents[2] / 'scripts'))
from control_plane_lifecycle import LifecycleError, LifecycleModel


def now_iso() -> str:
    from datetime import datetime, timezone

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


class LifecycleEngine:
    def __init__(self, lifecycle_path: Path):
        self.lifecycle_path = lifecycle_path
        self.model = LifecycleModel.load(lifecycle_path)

    def model_payload(self) -> dict:
        return {
            'id': self.model.id,
            'initialState': self.model.initial_state,
            'states': sorted(self.model.states),
            'terminalStates': sorted(self.model.terminal_states),
            'transitions': sorted([{'from': f, 'to': t} for (f, t) in self.model.transitions], key=lambda x: (x['from'], x['to'])),
            'terminalReasonTaxonomy': sorted(self.model.terminal_reasons),
            'loadedAt': now_iso(),
            'path': str(self.lifecycle_path),
        }

    def validate_transition(self, from_state: str, to_state: str) -> dict:
        valid = self.model.can_transition(from_state, to_state)
        return {
            'lifecycleId': self.model.id,
            'from': from_state,
            'to': to_state,
            'valid': valid,
        }

    def validate_terminal_reason(self, reason: str) -> dict:
        try:
            self.model.assert_terminal_reason(reason)
            return {'lifecycleId': self.model.id, 'reason': reason, 'valid': True}
        except LifecycleError:
            return {
                'lifecycleId': self.model.id,
                'reason': reason,
                'valid': False,
                'allowed': sorted(self.model.terminal_reasons),
            }


def handler_factory(engine: LifecycleEngine, token: str):
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
            if path == '/v1/lifecycle/health':
                return json_response(self, 200, {'status': 'ok', 'service': 'lifecycle', 'checkedAt': now_iso()})
            if path == '/v1/lifecycle/model':
                return json_response(self, 200, engine.model_payload())
            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def do_POST(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            body = parse_json(self)
            try:
                if path == '/v1/lifecycle/validate-transition':
                    from_state = str(body.get('fromState', ''))
                    to_state = str(body.get('toState', ''))
                    out = engine.validate_transition(from_state, to_state)
                    return json_response(self, 200, out)
                if path == '/v1/lifecycle/validate-terminal-reason':
                    reason = str(body.get('reason', ''))
                    out = engine.validate_terminal_reason(reason)
                    return json_response(self, 200, out)
                return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
            except LifecycleError as ex:
                return json_response(self, 400, err('VALIDATION_ERROR', str(ex), req_id))
            except Exception as ex:
                return json_response(self, 500, err('INTERNAL', str(ex), req_id))

        def log_message(self, fmt: str, *args):
            return

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description='Umbrella Lifecycle Service')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8793)
    ap.add_argument('--umbrella-root', default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument('--lifecycle', default='control-plane/state-machine/run-lifecycle.json')
    ap.add_argument('--token', default='')
    args = ap.parse_args()

    root = Path(args.umbrella_root).resolve()
    engine = LifecycleEngine(lifecycle_path=(root / args.lifecycle))
    handler = handler_factory(engine=engine, token=args.token.strip())
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({'status': 'listening', 'service': 'lifecycle', 'host': args.host, 'port': args.port}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
