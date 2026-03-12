#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import urllib.request

sys.path.append(str(Path(__file__).resolve().parents[2]))
from services.memory.auth import check_auth


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def parse_payload(raw: str):
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    for ln in reversed(lines):
        try:
            return json.loads(ln)
        except Exception:
            continue
    return None


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


class ExecutionEngine:
    def __init__(self, umbrella_root: Path, memory_core_url: str, policy_url: str, mesh_token: str):
        self.root = umbrella_root
        self.adapter = self.root / 'scripts' / 'adapters' / 'removed-runtime-adapter'
        self.memory_core_url = memory_core_url.rstrip('/')
        self.policy_url = policy_url.rstrip('/')
        self.mesh_token = mesh_token.strip()

    def _headers(self) -> dict:
        h = {'Content-Type': 'application/json'}
        if self.mesh_token:
            h['Authorization'] = f'Bearer {self.mesh_token}'
        return h

    def _post_memory(self, path: str, payload: dict, timeout: int = 30) -> dict:
        req = urllib.request.Request(
            f'{self.memory_core_url}{path}',
            method='POST',
            data=json.dumps(payload).encode('utf-8'),
            headers=self._headers(),
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))

    def _authorize_step(self, step_spec: dict, timeout: int = 15) -> dict:
        req = urllib.request.Request(
            f'{self.policy_url}/v1/policy/authorize-step',
            method='POST',
            data=json.dumps({'stepSpec': step_spec}).encode('utf-8'),
            headers=self._headers(),
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))

    def _run(self, args: list[str]) -> dict:
        cmd = [str(self.adapter), '--umbrella-root', str(self.root)] + args
        proc = subprocess.run(cmd, cwd=str(self.root), capture_output=True, text=True)
        payload = parse_payload(proc.stdout)
        return {
            'ok': proc.returncode == 0,
            'exitCode': proc.returncode,
            'result': payload if isinstance(payload, dict) else {'stdout': (proc.stdout or '')[-4000:]},
            'stderr': (proc.stderr or '')[-4000:],
            'command': cmd,
        }

    def submit_step_spec(self, run_id: str, step_id: str, step_spec: dict) -> dict:
        action = str(step_spec.get('action', '')).strip()
        policy_step = dict(step_spec) if isinstance(step_spec, dict) else {}
        metadata = policy_step.get('metadata') if isinstance(policy_step.get('metadata'), dict) else {}
        boundary_context = metadata.get('boundaryContext') if isinstance(metadata.get('boundaryContext'), dict) else {}
        if not boundary_context:
            boundary_context = {'phase': 'active-run'}
        if not str(boundary_context.get('phase', '')).strip():
            boundary_context['phase'] = 'active-run'
        boundary_context['runId'] = run_id
        if step_id:
            boundary_context['stepId'] = step_id
        metadata['boundaryContext'] = boundary_context
        policy_step['metadata'] = metadata
        if action:
            auth = self._authorize_step(step_spec=policy_step)
            if not bool(auth.get('allowed', False)):
                return {
                    'ok': False,
                    'exitCode': 1,
                    'result': {'status': 'FAILED', 'kind': 'policy', 'policyDecision': auth},
                    'stderr': str(auth.get('reason', 'policy_denied')),
                    'command': ['policy', 'authorize-step'],
                }

        if action == 'memoryWrite':
            namespace = str(step_spec.get('namespace', '')).strip()
            key = str(step_spec.get('key', '')).strip()
            out = self._post_memory(
                '/v1/memory-core/put',
                {
                    'namespace': namespace,
                    'key': key,
                    'value': step_spec.get('value'),
                    'metadata': step_spec.get('metadata') if isinstance(step_spec.get('metadata'), dict) else {},
                },
            )
            return {
                'ok': bool(out.get('ok', False)),
                'exitCode': 0 if bool(out.get('ok', False)) else 1,
                'result': {
                    'status': 'SUCCESS' if bool(out.get('ok', False)) else 'FAILED',
                    'kind': 'memoryWrite',
                    'memory': out.get('memory', {}),
                },
                'stderr': '',
                'command': ['memory-core', 'put'],
            }

        if action == 'memoryRead':
            namespace = str(step_spec.get('namespace', '')).strip()
            key = str(step_spec.get('key', '')).strip()
            out = self._post_memory('/v1/memory-core/get', {'namespace': namespace, 'key': key})
            exists = bool(out.get('exists', False))
            expected = step_spec.get('expectValue', None)
            status = 'SUCCESS'
            if expected is not None and out.get('memory', {}).get('value') != expected:
                status = 'FAILED'
            return {
                'ok': status == 'SUCCESS',
                'exitCode': 0 if status == 'SUCCESS' else 1,
                'result': {
                    'status': status,
                    'kind': 'memoryRead',
                    'exists': exists,
                    'memory': out.get('memory', {}),
                },
                'stderr': '',
                'command': ['memory-core', 'get'],
            }

        if action == 'memoryDelete':
            namespace = str(step_spec.get('namespace', '')).strip()
            key = str(step_spec.get('key', '')).strip()
            out = self._post_memory('/v1/memory-core/delete', {'namespace': namespace, 'key': key})
            ok = bool(out.get('ok', False))
            return {
                'ok': ok,
                'exitCode': 0 if ok else 1,
                'result': {'status': 'SUCCESS' if ok else 'FAILED', 'kind': 'memoryDelete', 'deleted': bool(out.get('deleted', False))},
                'stderr': '',
                'command': ['memory-core', 'delete'],
            }

        if action == 'memoryList':
            namespace = str(step_spec.get('namespace', '')).strip()
            out = self._post_memory('/v1/memory-core/list', {'namespace': namespace})
            ok = bool(out.get('ok', False))
            return {
                'ok': ok,
                'exitCode': 0 if ok else 1,
                'result': {
                    'status': 'SUCCESS' if ok else 'FAILED',
                    'kind': 'memoryList',
                    'namespace': namespace,
                    'count': int(out.get('count', 0)),
                    'entries': out.get('entries', []),
                },
                'stderr': '',
                'command': ['memory-core', 'list'],
            }

        args = [
            'submit_step_spec',
            '--run-id',
            run_id,
            '--step-spec-json',
            json.dumps(step_spec),
        ]
        if step_id:
            args.extend(['--step-id', step_id])
        return self._run(args)

    def submit_command(self, run_id: str, step_id: str, command: str, workdir: str, timeout_sec: int) -> dict:
        return self._run(
            [
                'submit_step',
                '--run-id',
                run_id,
                '--step-id',
                step_id,
                '--command',
                command,
                '--workdir',
                workdir,
                '--timeout-sec',
                str(timeout_sec),
            ]
        )

    def heartbeat(self, run_id: str, step_id: str) -> dict:
        return self._run(['heartbeat', '--run-id', run_id, '--step-id', step_id])

    def result(self, run_id: str, step_id: str) -> dict:
        return self._run(['result', '--run-id', run_id, '--step-id', step_id])

    def cancel(self, run_id: str, step_id: str) -> dict:
        return self._run(['cancel', '--run-id', run_id, '--step-id', step_id])

    def compensate(self, run_id: str, step_id: str, note: str) -> dict:
        return self._run(['compensate', '--run-id', run_id, '--step-id', step_id, '--note', note])


def handler_factory(engine: ExecutionEngine, token: str):
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
            if path == '/v1/execution/health':
                return json_response(self, 200, {'status': 'ok', 'service': 'execution', 'checkedAt': now_iso()})
            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def do_POST(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            body = parse_json(self)

            try:
                if path == '/v1/execution/submit-step-spec':
                    out = engine.submit_step_spec(
                        run_id=str(body.get('runId', '')),
                        step_id=str(body.get('stepId', '')),
                        step_spec=body.get('stepSpec') or {},
                    )
                    return json_response(self, 200, out)
                if path == '/v1/execution/submit-command':
                    out = engine.submit_command(
                        run_id=str(body.get('runId', '')),
                        step_id=str(body.get('stepId', '')),
                        command=str(body.get('command', '')),
                        workdir=str(body.get('workdir', '.')),
                        timeout_sec=int(body.get('timeoutSec', 300)),
                    )
                    return json_response(self, 200, out)
                if path == '/v1/execution/heartbeat':
                    out = engine.heartbeat(run_id=str(body.get('runId', '')), step_id=str(body.get('stepId', '')))
                    return json_response(self, 200, out)
                if path == '/v1/execution/result':
                    out = engine.result(run_id=str(body.get('runId', '')), step_id=str(body.get('stepId', '')))
                    return json_response(self, 200, out)
                if path == '/v1/execution/cancel':
                    out = engine.cancel(run_id=str(body.get('runId', '')), step_id=str(body.get('stepId', '')))
                    return json_response(self, 200, out)
                if path == '/v1/execution/compensate':
                    out = engine.compensate(
                        run_id=str(body.get('runId', '')),
                        step_id=str(body.get('stepId', '')),
                        note=str(body.get('note', '')),
                    )
                    return json_response(self, 200, out)
                return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
            except Exception as ex:
                return json_response(self, 500, err('INTERNAL', str(ex), req_id))

        def log_message(self, fmt: str, *args):
            return

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description='Umbrella Execution Service')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8794)
    ap.add_argument('--umbrella-root', default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument('--memory-core-url', default='http://127.0.0.1:8798')
    ap.add_argument('--policy-url', default='http://127.0.0.1:8791')
    ap.add_argument('--mesh-token', default='')
    ap.add_argument('--token', default='')
    args = ap.parse_args()

    root = Path(args.umbrella_root).resolve()
    engine = ExecutionEngine(
        umbrella_root=root,
        memory_core_url=args.memory_core_url,
        policy_url=args.policy_url,
        mesh_token=args.mesh_token,
    )
    handler = handler_factory(engine=engine, token=args.token.strip())
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({'status': 'listening', 'service': 'execution', 'host': args.host, 'port': args.port}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
