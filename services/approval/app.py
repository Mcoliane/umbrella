#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.append(str(Path(__file__).resolve().parents[2]))
from services.memory.auth import check_auth


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


def key_to_filename(key: str) -> str:
    safe = ''.join(ch if ch.isalnum() or ch in ('-', '_', '.') else '_' for ch in key)
    return safe[:220]


class ApprovalStore:
    def __init__(self, umbrella_root: Path):
        self.root = umbrella_root
        self.approvals_dir = self.root / 'control-plane' / 'approvals'
        self.approvals_dir.mkdir(parents=True, exist_ok=True)
        self.resume_journal_dir = self.approvals_dir / 'resume-journal'
        self.resume_journal_dir.mkdir(parents=True, exist_ok=True)
        self.runner = self.root / 'scripts' / 'control-plane' / 'run-umbrella-control-plane'

    def approval_path(self, key: str) -> Path:
        return self.approvals_dir / f'{key_to_filename(key)}.json'

    def get(self, key: str) -> dict | None:
        p = self.approval_path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return None

    def put(self, key: str, data: dict) -> dict:
        p = self.approval_path(key)
        p.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
        return data

    def resume_journal_path(self, run_id: str, approval_key: str, idempotency_key: str) -> Path:
        token = f'{key_to_filename(run_id)}__{key_to_filename(approval_key)}__{key_to_filename(idempotency_key)}'
        return self.resume_journal_dir / f'{token}.json'

    def get_resume_journal(self, run_id: str, approval_key: str, idempotency_key: str) -> dict | None:
        if not idempotency_key.strip():
            return None
        p = self.resume_journal_path(run_id, approval_key, idempotency_key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return None

    def put_resume_journal(self, run_id: str, approval_key: str, idempotency_key: str, response: dict) -> dict:
        entry = {
            'runId': run_id,
            'approvalKey': approval_key,
            'idempotencyKey': idempotency_key,
            'createdAt': now_iso(),
            'response': response,
            'journalPath': str(self.resume_journal_path(run_id, approval_key, idempotency_key)),
        }
        p = self.resume_journal_path(run_id, approval_key, idempotency_key)
        p.write_text(json.dumps(entry, indent=2) + '\n', encoding='utf-8')
        return entry

    def list_resume_journal(self, run_id: str, approval_key: str = '') -> list[dict]:
        prefix = f'{key_to_filename(run_id)}__'
        rows: list[dict] = []
        for p in sorted(self.resume_journal_dir.glob(f'{prefix}*.json')):
            try:
                row = json.loads(p.read_text(encoding='utf-8'))
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if approval_key.strip() and str(row.get('approvalKey', '')).strip() != approval_key.strip():
                continue
            if not row.get('journalPath'):
                row['journalPath'] = str(p)
            rows.append(row)
        return rows

    def request(self, key: str, run_id: str = '', step_id: str = '', note: str = '') -> dict:
        current = self.get(key) or {}
        data = {
            'approvalKey': key,
            'status': 'PENDING',
            'requestedAt': current.get('requestedAt') or now_iso(),
            'updatedAt': now_iso(),
            'runId': run_id or current.get('runId', ''),
            'stepId': step_id or current.get('stepId', ''),
        }
        if note.strip():
            data['note'] = note.strip()
        if current.get('decisionBy'):
            data['previousDecisionBy'] = current.get('decisionBy')
        return self.put(key, data)

    def decide(self, key: str, status: str, by: str, note: str = '') -> dict:
        current = self.get(key) or {'approvalKey': key, 'requestedAt': now_iso()}
        current['status'] = status
        current['updatedAt'] = now_iso()
        current['decisionBy'] = by or 'operator'
        if note.strip():
            current['decisionNote'] = note.strip()
        return self.put(key, current)

    def get_run_status(self, approval_key: str) -> dict | None:
        ap = self.get(approval_key)
        if not ap:
            return None

        run_id = str(ap.get('runId', '')).strip()
        if not run_id:
            return {
                'approvalKey': approval_key,
                'runId': '',
                'state': 'PENDING',
                'source': 'approval',
            }

        runs_root = self.root / 'control-plane' / 'observability' / 'runs' / run_id
        summary_path = runs_root / 'summary.json'
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding='utf-8'))
                state = str(summary.get('state', '')).upper()
                if state in {'PENDING', 'BLOCKED', 'RUNNING', 'SUCCEEDED', 'FAILED'}:
                    return {
                        'approvalKey': approval_key,
                        'runId': run_id,
                        'state': state,
                        'source': 'summary',
                        'terminalReason': str(summary.get('terminalReason', '')),
                    }
            except Exception:
                pass

        run_path = runs_root / 'run.json'
        if run_path.exists():
            try:
                run = json.loads(run_path.read_text(encoding='utf-8'))
                state = str(run.get('state', '')).upper()
                if state in {'PENDING', 'BLOCKED', 'RUNNING', 'SUCCEEDED', 'FAILED'}:
                    return {
                        'approvalKey': approval_key,
                        'runId': run_id,
                        'state': state,
                        'source': 'run',
                        'terminalReason': str(run.get('terminalReason', '')),
                    }
            except Exception:
                pass

        approval_status = str(ap.get('status', '')).upper()
        fallback_state = 'PENDING' if approval_status == 'PENDING' else 'BLOCKED'
        return {
            'approvalKey': approval_key,
            'runId': run_id,
            'state': fallback_state,
            'source': 'approval',
        }

    def resume_run(
        self,
        plan: str,
        run_id: str,
        approval_key: str,
        idempotency_key: str = '',
        skip_drift_lint: bool = False,
        skip_capability_parity: bool = False,
        policy_url: str = '',
        lifecycle_url: str = '',
        router_url: str = '',
        scheduler_url: str = '',
        execution_url: str = '',
        approval_url: str = '',
        orchestrator_url: str = '',
        mesh_token: str = '',
        reconcile_cmd: str = '',
    ) -> dict:
        ap = self.get(approval_key)
        if not ap:
            return {
                'ok': False,
                'exitCode': 3,
                'result': {'reason': 'approval_not_found', 'approvalKey': approval_key},
                'stderr': '',
            }
        status = str(ap.get('status', '')).upper()
        if status != 'APPROVED':
            return {
                'ok': False,
                'exitCode': 3,
                'result': {'reason': 'approval_not_granted', 'approvalKey': approval_key, 'status': status},
                'stderr': '',
            }

        idem = idempotency_key.strip()
        if idem:
            journal = self.get_resume_journal(run_id, approval_key, idem)
            if journal and isinstance(journal.get('response'), dict):
                replay = dict(journal['response'])
                replay['idempotency'] = {
                    'replayed': True,
                    'idempotencyKey': idem,
                    'journalPath': str(self.resume_journal_path(run_id, approval_key, idem)),
                }
                return replay

        cmd = [
            str(self.runner),
            '--umbrella-root',
            str(self.root),
            '--plan',
            plan,
            '--run-id',
            run_id,
            '--resume-blocked',
        ]
        if idempotency_key.strip():
            cmd.extend(['--idempotency-key', idempotency_key.strip()])
        if skip_drift_lint:
            cmd.append('--skip-drift-lint')
        if skip_capability_parity:
            cmd.append('--skip-capability-parity')
        if policy_url.strip():
            cmd.extend(['--policy-url', policy_url.strip()])
        if lifecycle_url.strip():
            cmd.extend(['--lifecycle-url', lifecycle_url.strip()])
        if router_url.strip():
            cmd.extend(['--router-url', router_url.strip()])
        if scheduler_url.strip():
            cmd.extend(['--scheduler-url', scheduler_url.strip()])
        if execution_url.strip():
            cmd.extend(['--execution-url', execution_url.strip()])
        if approval_url.strip():
            cmd.extend(['--approval-url', approval_url.strip()])
        if orchestrator_url.strip():
            cmd.extend(['--orchestrator-url', orchestrator_url.strip()])
        cmd.extend(['--orchestrator-caller', 'approval-service'])
        if mesh_token.strip():
            cmd.extend(['--mesh-token', mesh_token.strip()])
        if reconcile_cmd.strip():
            cmd.extend(['--reconcile-cmd', reconcile_cmd.strip()])

        proc = subprocess.run(cmd, cwd=str(self.root), capture_output=True, text=True)
        payload = None
        try:
            payload = json.loads((proc.stdout or '').strip() or '{}')
        except Exception:
            payload = {'stdout': (proc.stdout or '')[-4000:]}
        out = {
            'ok': proc.returncode == 0,
            'exitCode': proc.returncode,
            'result': payload,
            'stderr': (proc.stderr or '')[-4000:],
        }
        if idem:
            self.put_resume_journal(run_id, approval_key, idem, out)
            out['idempotency'] = {
                'replayed': False,
                'idempotencyKey': idem,
                'journalPath': str(self.resume_journal_path(run_id, approval_key, idem)),
            }
        return out


def handler_factory(store: ApprovalStore, token: str):
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

            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == '/v1/approval/health':
                return json_response(self, 200, {'status': 'ok', 'service': 'approval', 'checkedAt': now_iso()})

            run_status_prefix = '/v1/approval/'
            if path.startswith(run_status_prefix) and path.endswith('/run-status'):
                key = path[len(run_status_prefix):-len('/run-status')]
                if not key or '/' in key:
                    return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
                status = store.get_run_status(key)
                if not status:
                    return json_response(self, 404, err('NOT_FOUND', 'approval not found', req_id))
                return json_response(self, 200, {'exists': True, 'status': status})

            if path == '/v1/approval/resume-journal':
                run_id = str((query.get('runId') or [''])[0]).strip()
                approval_key = str((query.get('approvalKey') or [''])[0]).strip()
                if not run_id:
                    return json_response(self, 400, err('VALIDATION_ERROR', 'runId query parameter is required', req_id))
                entries = store.list_resume_journal(run_id=run_id, approval_key=approval_key)
                return json_response(self, 200, {'ok': True, 'count': len(entries), 'entries': entries})

            journal_prefix = '/v1/approval/resume-journal/'
            if path.startswith(journal_prefix):
                rest = path[len(journal_prefix):]
                parts = rest.split('/')
                if len(parts) != 3 or not all(parts):
                    return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
                run_id, approval_key, idempotency_key = parts
                entry = store.get_resume_journal(run_id, approval_key, idempotency_key)
                if not entry:
                    return json_response(self, 404, err('NOT_FOUND', 'resume journal not found', req_id))
                if not entry.get('journalPath'):
                    entry['journalPath'] = str(store.resume_journal_path(run_id, approval_key, idempotency_key))
                return json_response(self, 200, {'exists': True, 'entry': entry})

            prefix = '/v1/approval/'
            if path.startswith(prefix) and path.count('/') >= 3:
                key = path[len(prefix):]
                if '/' in key:
                    return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
                ap = store.get(key)
                if not ap:
                    return json_response(self, 404, err('NOT_FOUND', 'approval not found', req_id))
                return json_response(self, 200, {'exists': True, 'approval': ap})

            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def do_POST(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return

            path = urlparse(self.path).path
            body = parse_json(self)

            if path == '/v1/approval/resume':
                plan = str(body.get('plan', '')).strip()
                run_id = str(body.get('runId', '')).strip()
                approval_key = str(body.get('approvalKey', '')).strip()
                if not plan or not run_id or not approval_key:
                    return json_response(self, 400, err('VALIDATION_ERROR', 'plan, runId, and approvalKey are required', req_id))
                out = store.resume_run(
                    plan=plan,
                    run_id=run_id,
                    approval_key=approval_key,
                    idempotency_key=str(body.get('idempotencyKey', '')),
                    skip_drift_lint=bool(body.get('skipDriftLint', False)),
                    skip_capability_parity=bool(body.get('skipCapabilityParity', False)),
                    policy_url=str(body.get('policyUrl', '')),
                    lifecycle_url=str(body.get('lifecycleUrl', '')),
                    router_url=str(body.get('routerUrl', '')),
                    scheduler_url=str(body.get('schedulerUrl', '')),
                    execution_url=str(body.get('executionUrl', '')),
                    approval_url=str(body.get('approvalUrl', '')),
                    orchestrator_url=str(body.get('orchestratorUrl', '')),
                    mesh_token=str(body.get('meshToken', '')),
                    reconcile_cmd=str(body.get('reconcileCmd', '')),
                )
                return json_response(self, 200, out)

            prefix = '/v1/approval/'
            if not path.startswith(prefix):
                return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

            rest = path[len(prefix):]
            parts = rest.split('/')
            if len(parts) != 2:
                return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
            key, action = parts[0], parts[1]

            if action == 'request':
                out = store.request(
                    key,
                    run_id=str(body.get('runId', '')),
                    step_id=str(body.get('stepId', '')),
                    note=str(body.get('note', '')),
                )
                return json_response(self, 200, {'ok': True, 'approval': out})
            if action == 'approve':
                out = store.decide(key, status='APPROVED', by=str(body.get('by', 'operator')), note=str(body.get('note', '')))
                return json_response(self, 200, {'ok': True, 'approval': out})
            if action == 'deny':
                out = store.decide(key, status='DENIED', by=str(body.get('by', 'operator')), note=str(body.get('note', '')))
                return json_response(self, 200, {'ok': True, 'approval': out})

            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def log_message(self, fmt: str, *args):
            return

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description='Umbrella Approval Service')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8792)
    ap.add_argument('--umbrella-root', default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument('--token', default='')
    args = ap.parse_args()

    root = Path(args.umbrella_root).resolve()
    store = ApprovalStore(umbrella_root=root)
    handler = handler_factory(store=store, token=args.token.strip())
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({'status': 'listening', 'service': 'approval', 'host': args.host, 'port': args.port}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
