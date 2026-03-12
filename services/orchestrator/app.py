#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.append(str(Path(__file__).resolve().parents[2]))
from services.memory.auth import check_auth
from services.id_utils import validate_identifier


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def write_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + '\n', encoding='utf-8')


def _headers(mesh_token: str) -> dict:
    h = {'Content-Type': 'application/json'}
    if mesh_token.strip():
        h['Authorization'] = f'Bearer {mesh_token.strip()}'
    return h


def post_json(base_url: str, path: str, payload: dict, timeout: int = 30, mesh_token: str = '') -> dict:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        method='POST',
        data=json.dumps(payload).encode('utf-8'),
        headers=_headers(mesh_token),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as ex:
        body = ''
        try:
            body = ex.read().decode('utf-8')
        except Exception:
            body = ''
        raise RuntimeError(f'HTTP {ex.code} from {base_url.rstrip("/")}{path}: {body}') from ex


def get_json(base_url: str, path: str, timeout: int = 15, mesh_token: str = '') -> dict:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        method='GET',
        headers=({'Authorization': f'Bearer {mesh_token.strip()}'} if mesh_token.strip() else {}),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as ex:
        body = ''
        try:
            body = ex.read().decode('utf-8')
        except Exception:
            body = ''
        raise RuntimeError(f'HTTP {ex.code} from {base_url.rstrip("/")}{path}: {body}') from ex


def step_id(step: dict, idx: int) -> str:
    return str(step.get('stepId') or step.get('id') or f'step-{idx}')


def terminal_status_code(state: str) -> int:
    if state == 'SUCCEEDED':
        return 0
    if state == 'BLOCKED':
        return 3
    return 1


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


def parse_service_error(ex: Exception) -> dict:
    message = str(ex)
    if 'HTTP ' in message:
        return {
            'failureCategory': 'dependency',
            'failureReason': 'dependency_request_failed',
            'failureMessage': message,
        }
    return {
        'failureCategory': 'dependency',
        'failureReason': 'dependency_unavailable',
        'failureMessage': message,
    }


def classify_execution_failure(execution_result: dict) -> dict:
    if not isinstance(execution_result, dict):
        return {
            'failureCategory': 'runtime',
            'failureSource': 'execution',
            'failureReason': 'execution_runtime_failed',
            'failureMessage': 'execution result unavailable',
        }
    category = str(execution_result.get('failureCategory', '')).strip()
    source = str(execution_result.get('failureSource', '')).strip() or 'execution'
    reason = str(execution_result.get('failureReason', '')).strip()
    if category and reason:
        return {
            'failureCategory': category,
            'failureSource': source,
            'failureReason': reason,
            'failureMessage': str(execution_result.get('stderr', '')).strip() or str((execution_result.get('result') or {}).get('error', '')).strip(),
        }
    result = execution_result.get('result') if isinstance(execution_result.get('result'), dict) else {}
    if bool(result.get('timedOut', False)) or int(execution_result.get('exitCode', 1)) == 124:
        return {
            'failureCategory': 'runtime',
            'failureSource': source,
            'failureReason': 'timeout',
            'failureMessage': str(execution_result.get('stderr', '')).strip(),
        }
    if 'policyDecision' in result:
        return {
            'failureCategory': 'policy',
            'failureSource': 'policy',
            'failureReason': 'execution_policy_denied',
            'failureMessage': str((result.get('policyDecision') or {}).get('reason', 'policy_denied')),
        }
    if 'error' in result:
        return {
            'failureCategory': 'validation',
            'failureSource': source,
            'failureReason': 'execution_validation_failed',
            'failureMessage': str(result.get('error', 'validation failed')),
        }
    return {
        'failureCategory': 'runtime',
        'failureSource': source,
        'failureReason': 'execution_runtime_failed',
        'failureMessage': str(execution_result.get('stderr', '')).strip() or 'execution failed',
    }


class OrchestratorEngine:
    def __init__(self, umbrella_root: Path):
        self.root = umbrella_root

    def _write_run_state(self, run_dir: Path, run: dict):
        write_json(run_dir / 'run.json', run)

    def _write_summary(self, run_dir: Path, run: dict, extra: dict | None = None) -> dict:
        completed = sum(1 for s in run['steps'] if s.get('status') in {'SUCCESS', 'FAILED', 'BLOCKED'})
        summary = {
            'runId': run['runId'],
            'state': run['state'],
            'terminalReason': run['terminalReason'],
            'stepCount': len(run['steps']),
            'completedSteps': completed,
            'runPath': str(run_dir),
            'createdAt': run['createdAt'],
            'finishedAt': run.get('finishedAt', ''),
        }
        if run.get('approvalKey'):
            summary['approvalKey'] = run.get('approvalKey')
        if run.get('blockedStepId'):
            summary['blockedStepId'] = run.get('blockedStepId')
        if run.get('failedStepId'):
            summary['failedStepId'] = run.get('failedStepId')
        if run.get('failureCategory'):
            summary['failureCategory'] = run.get('failureCategory')
        if run.get('failureSource'):
            summary['failureSource'] = run.get('failureSource')
        if run.get('failureMessage'):
            summary['failureMessage'] = run.get('failureMessage')
        if extra:
            summary.update(extra)
        write_json(run_dir / 'summary.json', summary)
        return summary

    def _finalize_run(self, run_dir: Path, run: dict, *, state: str, terminal_reason: str, extra: dict | None = None) -> dict:
        run['state'] = state
        run['terminalReason'] = terminal_reason
        run['updatedAt'] = now_iso()
        run['finishedAt'] = now_iso()
        if extra:
            run.update(extra)
        self._write_run_state(run_dir, run)
        return self._write_summary(run_dir, run)

    def run_summary_path(self, run_id: str) -> Path:
        return self.root / 'control-plane' / 'observability' / 'runs' / run_id / 'summary.json'

    def get_summary(self, run_id: str) -> dict | None:
        run_id = validate_identifier(run_id, 'runId')
        p = self.run_summary_path(run_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return None

    def start_run(
        self,
        plan: str,
        run_id: str,
        policy_url: str,
        lifecycle_url: str,
        router_url: str,
        scheduler_url: str,
        execution_url: str,
        approval_url: str,
        reconcile_cmd: str,
        resume_blocked: bool,
        caller: str,
        mesh_token: str = '',
    ) -> dict:
        run_id = validate_identifier(run_id, 'runId')
        if resume_blocked and caller != 'approval-service':
            return {
                'ok': False,
                'exitCode': 1,
                'error': {'code': 'RESUME_FORBIDDEN', 'message': 'resumeBlocked runs must be invoked by approval-service'},
            }

        plan_path = Path(plan)
        if not plan_path.is_absolute():
            plan_path = (self.root / plan_path).resolve()
        parsed_plan = load_json(plan_path, {})
        steps = parsed_plan.get('steps') or []

        run_dir = self.root / 'control-plane' / 'observability' / 'runs' / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        run = {
            'runId': run_id,
            'planId': parsed_plan.get('id', f'umbrella.plan.unknown.{run_id}'),
            'state': 'PENDING',
            'createdAt': now_iso(),
            'updatedAt': now_iso(),
            'terminalReason': '',
            'steps': [],
            'services': {
                'policy': policy_url,
                'lifecycle': lifecycle_url,
                'router': router_url,
                'scheduler': scheduler_url,
                'execution': execution_url,
                'approval': approval_url,
                'orchestrator': 'local',
            },
        }

        step_rows = {}
        step_states = {}
        for i, s in enumerate(steps, start=1):
            sid = validate_identifier(step_id(s, i), f'stepId[{i}]')
            row = {
                'stepId': sid,
                'status': 'READY',
                'attemptCount': 0,
                'startedAt': '',
                'endedAt': '',
                'result': {},
            }
            run['steps'].append(row)
            step_rows[sid] = row
            step_states[sid] = 'READY'

        self._write_run_state(run_dir, run)

        try:
            for service_name, url, path in [
                ('policy', policy_url, '/v1/policy/health'),
                ('lifecycle', lifecycle_url, '/v1/lifecycle/health'),
                ('router', router_url, '/v1/router/health'),
                ('scheduler', scheduler_url, '/v1/scheduler/health'),
                ('execution', execution_url, '/v1/execution/health'),
                ('approval', approval_url, '/v1/approval/health'),
            ]:
                _ = get_json(url, path, mesh_token=mesh_token)
        except Exception as ex:
            failure = parse_service_error(ex)
            failure['failureSource'] = service_name
            summary = self._finalize_run(
                run_dir,
                run,
                state='FAILED',
                terminal_reason=failure['failureReason'],
                extra=failure,
            )
            return {'ok': False, 'exitCode': terminal_status_code('FAILED'), 'summary': summary}

        try:
            preflight = post_json(
                policy_url,
                '/v1/policy/preflight/all',
                {'reconcileCmd': reconcile_cmd},
                mesh_token=mesh_token,
            )
        except Exception as ex:
            failure = parse_service_error(ex)
            failure['failureSource'] = 'policy'
            summary = self._finalize_run(
                run_dir,
                run,
                state='FAILED',
                terminal_reason=failure['failureReason'],
                extra={**failure, 'preflight': {'ok': False}},
            )
            return {'ok': False, 'exitCode': terminal_status_code('FAILED'), 'summary': summary}
        if not bool(preflight.get('ok', False)):
            summary = self._finalize_run(
                run_dir,
                run,
                state='BLOCKED',
                terminal_reason='policy_blocked',
                extra={'failureCategory': 'policy', 'failureSource': 'policy', 'preflight': preflight},
            )
            return {'ok': False, 'exitCode': terminal_status_code(run['state']), 'summary': summary}

        run['state'] = 'RUNNING'
        run['updatedAt'] = now_iso()
        self._write_run_state(run_dir, run)

        while True:
            sched = post_json(
                scheduler_url,
                '/v1/scheduler/next-batch',
                {'steps': steps, 'stepStates': step_states},
                mesh_token=mesh_token,
            )
            batch = sched.get('dispatchStepIds') or []
            if not batch:
                pending = [s for s in step_states.values() if s not in {'SUCCESS', 'FAILED', 'BLOCKED', 'CANCELLED'}]
                if pending:
                    run['state'] = 'BLOCKED'
                    run['terminalReason'] = 'scheduler_no_dispatchable_steps'
                else:
                    run['state'] = 'SUCCEEDED'
                    run['terminalReason'] = 'all_steps_succeeded'
                break

            for sid in batch:
                idx = next((i for i, s in enumerate(steps, start=1) if step_id(s, i) == sid), None)
                if idx is None:
                    continue
                spec = steps[idx - 1]
                row = step_rows[sid]
                row['status'] = 'RUNNING'
                row['attemptCount'] += 1
                row['startedAt'] = row['startedAt'] or now_iso()
                step_states[sid] = 'RUNNING'

                if bool(spec.get('requiresApproval')):
                    approval_key = str(spec.get('approvalKey') or f'{run_id}:{sid}')
                    row['approvalKey'] = approval_key
                    if not resume_blocked:
                        req_out = post_json(
                            approval_url,
                            f'/v1/approval/{approval_key}/request',
                            {
                                'runId': run_id,
                                'stepId': sid,
                                'note': 'step requires approval',
                            },
                            mesh_token=mesh_token,
                        )
                        row['status'] = 'BLOCKED'
                        row['endedAt'] = now_iso()
                        row['result'] = {'approvalRequest': req_out}
                        step_states[sid] = 'BLOCKED'
                        run['state'] = 'BLOCKED'
                        run['terminalReason'] = 'approval_required'
                        run['approvalKey'] = approval_key
                        run['blockedStepId'] = sid
                        run['failureCategory'] = 'approval'
                        run['failureSource'] = 'approval'
                        run['failureMessage'] = 'step requires approval'
                        break
                    ap_get = get_json(approval_url, f'/v1/approval/{approval_key}', mesh_token=mesh_token)
                    approval = ap_get.get('approval') if isinstance(ap_get, dict) else {}
                    status = str((approval or {}).get('status', '')).upper()
                    if status != 'APPROVED':
                        row['status'] = 'BLOCKED'
                        row['endedAt'] = now_iso()
                        row['result'] = {'approval': approval or {}, 'reason': 'approval_not_granted'}
                        step_states[sid] = 'BLOCKED'
                        run['state'] = 'BLOCKED'
                        run['terminalReason'] = 'approval_required'
                        run['approvalKey'] = approval_key
                        run['blockedStepId'] = sid
                        run['failureCategory'] = 'approval'
                        run['failureSource'] = 'approval'
                        run['failureMessage'] = 'approval not granted'
                        break

                try:
                    _ = post_json(router_url, '/v1/router/route-step', {'step': spec}, mesh_token=mesh_token)
                except Exception as ex:
                    failure = parse_service_error(ex)
                    run['state'] = 'FAILED'
                    run['terminalReason'] = failure['failureReason']
                    run['failedStepId'] = sid
                    run['failureSource'] = 'router'
                    run['failureCategory'] = failure['failureCategory']
                    run['failureMessage'] = failure['failureMessage']
                    row['status'] = 'FAILED'
                    row['endedAt'] = now_iso()
                    row['result'] = {'error': failure}
                    step_states[sid] = 'FAILED'
                    break

                try:
                    if spec.get('command'):
                        ex = post_json(
                            execution_url,
                            '/v1/execution/submit-command',
                            {
                                'runId': run_id,
                                'stepId': sid,
                                'command': str(spec.get('command')),
                                'workdir': str(spec.get('workdir', '.')),
                                'timeoutSec': int(spec.get('timeoutSec', 300)),
                            },
                            mesh_token=mesh_token,
                        )
                    else:
                        ex = post_json(
                            execution_url,
                            '/v1/execution/submit-step-spec',
                            {'runId': run_id, 'stepId': sid, 'stepSpec': spec},
                            mesh_token=mesh_token,
                        )
                except Exception as exn:
                    failure = parse_service_error(exn)
                    run['state'] = 'FAILED'
                    run['terminalReason'] = failure['failureReason']
                    run['failedStepId'] = sid
                    run['failureSource'] = 'execution'
                    run['failureCategory'] = failure['failureCategory']
                    run['failureMessage'] = failure['failureMessage']
                    row['status'] = 'FAILED'
                    row['endedAt'] = now_iso()
                    row['result'] = {'error': failure}
                    step_states[sid] = 'FAILED'
                    break

                payload = ex.get('result') if isinstance(ex.get('result'), dict) else {}
                status = str(payload.get('status', 'FAILED')).upper()
                if ex.get('ok') and status == 'SUCCESS':
                    row['status'] = 'SUCCESS'
                    step_states[sid] = 'SUCCESS'
                else:
                    row['status'] = 'FAILED'
                    step_states[sid] = 'FAILED'
                row['endedAt'] = now_iso()
                row['result'] = ex

                if row['status'] != 'SUCCESS':
                    run['state'] = 'FAILED'
                    failure = classify_execution_failure(ex)
                    run['terminalReason'] = failure['failureReason']
                    run['failedStepId'] = sid
                    run['failureSource'] = failure['failureSource']
                    run['failureCategory'] = failure['failureCategory']
                    run['failureMessage'] = failure['failureMessage']
                    break

            if run['state'] in {'FAILED', 'BLOCKED'}:
                break

        lifecycle_warning = None
        try:
            _ = post_json(
                lifecycle_url,
                '/v1/lifecycle/validate-terminal-reason',
                {'reason': run['terminalReason']},
                mesh_token=mesh_token,
            )
        except Exception as ex:
            lifecycle_warning = str(ex)

        run['updatedAt'] = now_iso()
        run['finishedAt'] = now_iso()
        self._write_run_state(run_dir, run)
        summary_extra = {}
        if lifecycle_warning:
            summary_extra['lifecycleValidationWarning'] = lifecycle_warning
        summary = self._write_summary(run_dir, run, extra=summary_extra or None)
        return {
            'ok': run['state'] == 'SUCCEEDED',
            'exitCode': terminal_status_code(run['state']),
            'summary': summary,
        }


def handler_factory(engine: OrchestratorEngine, token: str):
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
            if path == '/v1/orchestrator/health':
                return json_response(self, 200, {'status': 'ok', 'service': 'orchestrator', 'checkedAt': now_iso()})

            prefix = '/v1/orchestrator/runs/'
            if path.startswith(prefix) and path.endswith('/summary'):
                run_id = path[len(prefix):-len('/summary')].strip('/')
                try:
                    run_id = validate_identifier(run_id, 'runId')
                except ValueError as ex:
                    return json_response(self, 400, err('VALIDATION_ERROR', str(ex), req_id))
                summary = engine.get_summary(run_id)
                if not summary:
                    return json_response(self, 404, err('NOT_FOUND', 'run summary not found', req_id))
                return json_response(self, 200, {'exists': True, 'summary': summary})

            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def do_POST(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            body = parse_json(self)

            if path == '/v1/orchestrator/runs/start':
                plan = str(body.get('plan', '')).strip()
                run_id = str(body.get('runId', '')).strip()
                if not plan or not run_id:
                    return json_response(self, 400, err('VALIDATION_ERROR', 'plan and runId are required', req_id))
                try:
                    out = engine.start_run(
                        plan=plan,
                        run_id=run_id,
                        policy_url=str(body.get('policyUrl', 'http://127.0.0.1:8791')).strip(),
                        lifecycle_url=str(body.get('lifecycleUrl', 'http://127.0.0.1:8793')).strip(),
                        router_url=str(body.get('routerUrl', 'http://127.0.0.1:8795')).strip(),
                        scheduler_url=str(body.get('schedulerUrl', 'http://127.0.0.1:8796')).strip(),
                        execution_url=str(body.get('executionUrl', 'http://127.0.0.1:8794')).strip(),
                        approval_url=str(body.get('approvalUrl', 'http://127.0.0.1:8792')).strip(),
                        reconcile_cmd=str(body.get('reconcileCmd', 'memory-core-reconcile')).strip(),
                        resume_blocked=bool(body.get('resumeBlocked', False)),
                        caller=str(body.get('caller', 'runner')).strip() or 'runner',
                        mesh_token=str(body.get('meshToken', '')),
                    )
                    return json_response(self, 200, out)
                except Exception as ex:
                    return json_response(self, 500, err('INTERNAL', str(ex), req_id))

            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def log_message(self, fmt: str, *args):
            return

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description='Umbrella Orchestrator Service')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8797)
    ap.add_argument('--umbrella-root', default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument('--token', default='')
    args = ap.parse_args()

    root = Path(args.umbrella_root).resolve()
    engine = OrchestratorEngine(umbrella_root=root)
    handler = handler_factory(engine=engine, token=args.token.strip())
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({'status': 'listening', 'service': 'orchestrator', 'host': args.host, 'port': args.port}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
