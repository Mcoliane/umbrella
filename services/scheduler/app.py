#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import sys

sys.path.append(str(Path(__file__).resolve().parents[2]))
from services.memory.auth import check_auth


TERMINAL_STEP_STATES = {'SUCCESS', 'FAILED', 'BLOCKED', 'CANCELLED'}


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


class SchedulerEngine:
    def __init__(self, scheduler_path: Path):
        self.scheduler_path = scheduler_path
        self.config = load_json(scheduler_path, {'id': 'umbrella.scheduler.default.v1', 'strategy': 'serial', 'maxParallel': 1})

    def config_payload(self) -> dict:
        return {
            'loadedAt': now_iso(),
            'path': str(self.scheduler_path),
            'config': self.config,
        }

    def compute_ready(self, steps: list[dict], step_states: dict[str, str]) -> dict:
        ready = []
        blocked_by = {}

        for step in steps:
            step_id = str(step.get('stepId') or step.get('id') or '')
            if not step_id:
                continue
            current = str(step_states.get(step_id, 'READY')).upper()
            if current in TERMINAL_STEP_STATES or current in {'DISPATCHED', 'IN_PROGRESS', 'RUNNING', 'RETRY_SCHEDULED'}:
                continue

            deps = step.get('dependsOn') or []
            unmet = []
            for d in deps:
                ds = str(step_states.get(str(d), 'READY')).upper()
                if ds != 'SUCCESS':
                    unmet.append({'dependency': str(d), 'status': ds})

            if unmet:
                blocked_by[step_id] = unmet
                continue
            ready.append(step_id)

        return {
            'readyStepIds': ready,
            'blockedByDependencies': blocked_by,
            'readyCount': len(ready),
        }

    def next_batch(self, steps: list[dict], step_states: dict[str, str], max_parallel_override: int | None = None) -> dict:
        ready = self.compute_ready(steps, step_states)
        limit = int(max_parallel_override or self.config.get('maxParallel', 1) or 1)
        limit = max(1, limit)

        strategy = str(self.config.get('strategy', 'serial')).lower()
        ready_ids = ready['readyStepIds']
        if strategy == 'serial':
            batch = ready_ids[:1]
        else:
            batch = ready_ids[:limit]

        return {
            'strategy': strategy,
            'limit': limit,
            'dispatchStepIds': batch,
            'readyStepIds': ready_ids,
            'blockedByDependencies': ready['blockedByDependencies'],
            'dispatchCount': len(batch),
        }


def handler_factory(engine: SchedulerEngine, token: str):
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
            if path == '/v1/scheduler/health':
                return json_response(self, 200, {'status': 'ok', 'service': 'scheduler', 'checkedAt': now_iso()})
            if path == '/v1/scheduler/config':
                return json_response(self, 200, engine.config_payload())
            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def do_POST(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            body = parse_json(self)
            steps = body.get('steps') or []
            step_states = body.get('stepStates') or {}

            if path == '/v1/scheduler/compute-ready':
                out = engine.compute_ready(steps=steps, step_states=step_states)
                return json_response(self, 200, out)

            if path == '/v1/scheduler/next-batch':
                mpo = body.get('maxParallel')
                out = engine.next_batch(steps=steps, step_states=step_states, max_parallel_override=mpo)
                return json_response(self, 200, out)

            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def log_message(self, fmt: str, *args):
            return

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description='Umbrella Scheduler Service')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8796)
    ap.add_argument('--umbrella-root', default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument('--scheduler', default='control-plane/scheduler/default-scheduler.json')
    ap.add_argument('--token', default='')
    args = ap.parse_args()

    root = Path(args.umbrella_root).resolve()
    engine = SchedulerEngine(scheduler_path=(root / args.scheduler))
    handler = handler_factory(engine=engine, token=args.token.strip())
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({'status': 'listening', 'service': 'scheduler', 'host': args.host, 'port': args.port}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
