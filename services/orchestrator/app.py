#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
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
from services.persistence import atomic_write_json, read_json

NON_TERMINAL_RUN_STATES = {'PENDING', 'RUNNING', 'RETRYING'}
DEFAULT_RUN_BUDGET_SEC = 1800


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


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


def _is_timeout_error(ex: Exception) -> bool:
    if isinstance(ex, (TimeoutError, socket.timeout)):
        return True
    if isinstance(getattr(ex, 'reason', None), (TimeoutError, socket.timeout)):
        return True
    return 'timed out' in str(ex).lower()


def parse_service_error(ex: Exception) -> dict:
    message = str(ex)
    if _is_timeout_error(ex):
        return {
            'failureCategory': 'runtime',
            'failureReason': 'timeout',
            'failureMessage': message,
        }
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


def extract_runtime_metadata(execution_result: dict) -> dict:
    if not isinstance(execution_result, dict):
        return {}
    result = execution_result.get('result') if isinstance(execution_result.get('result'), dict) else {}
    runtime_requested = str(execution_result.get('runtimeRequested') or result.get('runtimeRequested') or '').strip()
    runtime_resolved = str(execution_result.get('runtimeResolved') or result.get('runtimeResolved') or '').strip()
    runtime_class = str(execution_result.get('runtimeClass') or result.get('runtimeClass') or '').strip()
    runtime_reason = str(execution_result.get('runtimeReason') or result.get('runtimeReason') or '').strip()
    executor_runtime = str(execution_result.get('executorRuntime') or result.get('executorRuntime') or '').strip()
    action_family = str(execution_result.get('actionFamily') or result.get('actionFamily') or '').strip()
    runtime_capability = str(execution_result.get('runtimeCapability') or result.get('runtimeCapability') or '').strip()
    resolved_action_id = str(execution_result.get('resolvedActionId') or result.get('resolvedActionId') or '').strip()
    deprecated_action_id = str(execution_result.get('deprecatedActionId') or result.get('deprecatedActionId') or '').strip()
    supported_runtimes = execution_result.get('supportedRuntimes')
    if not isinstance(supported_runtimes, list):
        supported_runtimes = result.get('supportedRuntimes') if isinstance(result.get('supportedRuntimes'), list) else []
    runtime_supported = execution_result.get('runtimeSupported')
    if not isinstance(runtime_supported, bool):
        runtime_supported = result.get('runtimeSupported') if isinstance(result.get('runtimeSupported'), bool) else None
    out = {}
    if runtime_requested:
        out['runtimeRequested'] = runtime_requested
    if runtime_resolved:
        out['runtimeResolved'] = runtime_resolved
    if runtime_class:
        out['runtimeClass'] = runtime_class
    if runtime_reason:
        out['runtimeReason'] = runtime_reason
    if executor_runtime:
        out['executorRuntime'] = executor_runtime
    if action_family:
        out['actionFamily'] = action_family
    if runtime_capability:
        out['runtimeCapability'] = runtime_capability
    if resolved_action_id:
        out['resolvedActionId'] = resolved_action_id
    if deprecated_action_id:
        out['deprecatedActionId'] = deprecated_action_id
    if supported_runtimes:
        out['supportedRuntimes'] = supported_runtimes
    if runtime_supported is not None:
        out['runtimeSupported'] = runtime_supported
    return out


class OrchestratorEngine:
    def __init__(self, umbrella_root: Path):
        self.root = umbrella_root
        # Reconciliation is NOT done here: constructing the engine must be
        # side-effect free so tests/tooling that instantiate it, or an
        # accidental second process, cannot fail a live run. main() calls
        # reconcile_orphaned_runs() only after the port bind succeeds, which
        # makes the bind itself the single-instance guard.
        self.reconciled_runs: list[str] = []

    def runs_root(self) -> Path:
        return self.root / 'control-plane' / 'observability' / 'runs'

    def reconcile_orphaned_runs(self) -> list[str]:
        """Mark runs stranded in a non-terminal state by a dead orchestrator process as FAILED.

        run.json is flushed on every step/state transition, so a run found
        PENDING/RUNNING/RETRYING at startup can only belong to a process that
        died mid-run. A run must never be silently stuck. Call this ONLY after
        the orchestrator has bound its port (so no live sibling owns the runs).
        """
        runs_root = self.runs_root()
        if not runs_root.is_dir():
            return []
        reconciled = []
        for run_path in sorted(runs_root.glob('*/run.json')):
            try:
                run = read_json(run_path, None)
                if not isinstance(run, dict):
                    continue
                state = str(run.get('state', '')).upper()
                if state not in NON_TERMINAL_RUN_STATES:
                    continue
                run_dir = run_path.parent
                run.setdefault('runId', run_dir.name)
                run.setdefault('createdAt', '')
                run.setdefault('steps', [])
                interrupted_step_id = ''
                for step in run['steps']:
                    if str(step.get('status', '')).upper() == 'RUNNING':
                        step['status'] = 'FAILED'
                        step['endedAt'] = step.get('endedAt') or now_iso()
                        step['result'] = {
                            'error': {
                                'failureReason': 'orchestrator_crash',
                                'failureMessage': 'step was RUNNING when the orchestrator restarted',
                            }
                        }
                        interrupted_step_id = interrupted_step_id or str(step.get('stepId', ''))
                run['state'] = 'FAILED'
                run['terminalReason'] = 'orchestrator_crash'
                run['failureCategory'] = 'runtime'
                run['failureSource'] = 'orchestrator'
                run['failureMessage'] = (
                    f'run was {state} at orchestrator startup; a previous orchestrator process died mid-run'
                )
                if interrupted_step_id:
                    run['failedStepId'] = interrupted_step_id
                run['finishedAt'] = now_iso()
                self._write_run_state(run_dir, run)
                self._write_summary(run_dir, run, extra={'reconciledAt': now_iso()})
                reconciled.append(str(run.get('runId')))
            except Exception:
                continue
        self.reconciled_runs = reconciled
        return reconciled

    def _write_run_state(self, run_dir: Path, run: dict):
        run['updatedAt'] = now_iso()
        atomic_write_json(run_dir / 'run.json', run)

    def _write_summary(self, run_dir: Path, run: dict, extra: dict | None = None) -> dict:
        completed = sum(1 for s in run['steps'] if s.get('status') in {'SUCCESS', 'FAILED', 'BLOCKED'})
        runtime_breakdown = {}
        for step in run['steps']:
            step_result = step.get('result') if isinstance(step.get('result'), dict) else {}
            runtime = str(step_result.get('runtimeResolved', '')).strip()
            if runtime:
                runtime_breakdown[runtime] = int(runtime_breakdown.get(runtime, 0)) + 1
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
        if runtime_breakdown:
            summary['runtimeBreakdown'] = runtime_breakdown
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
        for key in (
            'runtimeRequested',
            'runtimeResolved',
            'runtimeClass',
            'runtimeReason',
            'executorRuntime',
            'actionFamily',
            'runtimeCapability',
            'resolvedActionId',
            'deprecatedActionId',
            'supportedRuntimes',
            'runtimeSupported',
        ):
            if key in run:
                summary[key] = run.get(key)
        if extra:
            summary.update(extra)
        atomic_write_json(run_dir / 'summary.json', summary)
        return summary

    @staticmethod
    def _retryable(failure: dict, retry_on: set) -> bool:
        return bool(retry_on) and (
            str(failure.get('failureReason', '')) in retry_on
            or str(failure.get('failureCategory', '')) in retry_on
        )

    @staticmethod
    def _schedule_retry(run: dict, row: dict, step_states: dict, sid: str, failure: dict):
        row['status'] = 'READY'
        row['endedAt'] = ''
        row['lastFailure'] = {**failure, 'attemptCount': row.get('attemptCount', 0), 'at': now_iso()}
        step_states[sid] = 'READY'
        run['state'] = 'RETRYING'

    @staticmethod
    def _fail_step(run: dict, row: dict, step_states: dict, sid: str, failure: dict, *, source: str = ''):
        run['state'] = 'FAILED'
        run['terminalReason'] = failure['failureReason']
        run['failedStepId'] = sid
        run['failureSource'] = source or failure.get('failureSource', '')
        run['failureCategory'] = failure['failureCategory']
        run['failureMessage'] = failure['failureMessage']
        row['status'] = 'FAILED'
        row['endedAt'] = now_iso()
        row['result'] = {'error': failure}
        step_states[sid] = 'FAILED'

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
        skip_drift_lint: bool = False,
        skip_capability_parity: bool = False,
        mesh_token: str = '',
    ) -> dict:
        run_id = validate_identifier(run_id, 'runId')
        reconcile_cmd = reconcile_cmd.strip() or str(self.root / 'scripts' / 'tools' / 'memory-core-reconcile')
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

        run_dir = self.runs_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        # Real resume: an approved resume of a blocked run must not repeat side
        # effects. Steps that already reached SUCCESS in the prior run record
        # are preserved verbatim and never re-dispatched.
        prior_run = read_json(run_dir / 'run.json', None) if resume_blocked else None
        if not isinstance(prior_run, dict):
            prior_run = None
        prior_success_steps = {}
        if prior_run:
            for prior_step in prior_run.get('steps') or []:
                if str(prior_step.get('status', '')).upper() == 'SUCCESS':
                    prior_success_steps[str(prior_step.get('stepId', ''))] = prior_step

        run = {
            'runId': run_id,
            'planId': parsed_plan.get('id', f'umbrella.plan.unknown.{run_id}'),
            'state': 'PENDING',
            'createdAt': (prior_run.get('createdAt') if prior_run else '') or now_iso(),
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
        if prior_run:
            run['resumedAt'] = now_iso()
            run['resumedFromState'] = str(prior_run.get('state', ''))

        step_rows = {}
        step_states = {}
        for i, s in enumerate(steps, start=1):
            sid = validate_identifier(step_id(s, i), f'stepId[{i}]')
            prior_step = prior_success_steps.get(sid)
            if prior_step is not None:
                row = dict(prior_step)
                row['status'] = 'SUCCESS'
                row['preservedOnResume'] = True
                initial_state = 'SUCCESS'
            else:
                row = {
                    'stepId': sid,
                    'status': 'READY',
                    'attemptCount': 0,
                    'startedAt': '',
                    'endedAt': '',
                    'result': {},
                }
                initial_state = 'READY'
            run['steps'].append(row)
            step_rows[sid] = row
            step_states[sid] = initial_state

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
                {
                    'reconcileCmd': reconcile_cmd,
                    'skipDriftLint': skip_drift_lint,
                    'skipCapabilityParity': skip_capability_parity,
                },
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

        # Retry policy and run budget come from the scheduler's declared config
        # (default-scheduler.json); the scheduler is already a hard dependency.
        scheduler_cfg = {}
        try:
            cfg_out = get_json(scheduler_url, '/v1/scheduler/config', mesh_token=mesh_token)
            scheduler_cfg = cfg_out.get('config') if isinstance(cfg_out.get('config'), dict) else {}
        except Exception:
            scheduler_cfg = {}
        if not scheduler_cfg:
            local_cfg = load_json(self.root / 'control-plane' / 'scheduler' / 'default-scheduler.json', {})
            scheduler_cfg = local_cfg if isinstance(local_cfg, dict) else {}
        retry_policy = scheduler_cfg.get('retryPolicy') if isinstance(scheduler_cfg.get('retryPolicy'), dict) else {}
        try:
            retry_max_attempts = max(1, int(retry_policy.get('maxAttempts', 1) or 1))
        except (TypeError, ValueError):
            retry_max_attempts = 1
        retry_on = {str(r).strip() for r in (retry_policy.get('retryOn') or []) if str(r).strip()}
        try:
            run_budget_sec = int(parsed_plan.get('runBudgetSec') or scheduler_cfg.get('runBudgetSec') or DEFAULT_RUN_BUDGET_SEC)
        except (TypeError, ValueError):
            run_budget_sec = DEFAULT_RUN_BUDGET_SEC
        run_budget_sec = max(1, run_budget_sec)
        run['retryPolicy'] = {'maxAttempts': retry_max_attempts, 'retryOn': sorted(retry_on)}
        run['runBudgetSec'] = run_budget_sec

        run['state'] = 'RUNNING'
        self._write_run_state(run_dir, run)
        deadline = time.monotonic() + run_budget_sec

        def budget_failure():
            run['state'] = 'FAILED'
            run['terminalReason'] = 'timeout'
            run['failureCategory'] = 'runtime'
            run['failureSource'] = 'orchestrator'
            run['failureMessage'] = f'run budget of {run_budget_sec}s exceeded'

        try:
            while True:
                if time.monotonic() >= deadline:
                    budget_failure()
                    break
                if run['state'] == 'RETRYING':
                    run['state'] = 'RUNNING'
                    self._write_run_state(run_dir, run)
                try:
                    sched = post_json(
                        scheduler_url,
                        '/v1/scheduler/next-batch',
                        {'steps': steps, 'stepStates': step_states},
                        mesh_token=mesh_token,
                    )
                except Exception as ex:
                    failure = parse_service_error(ex)
                    run['state'] = 'FAILED'
                    run['terminalReason'] = failure['failureReason']
                    run['failureSource'] = 'scheduler'
                    run['failureCategory'] = failure['failureCategory']
                    run['failureMessage'] = failure['failureMessage']
                    break
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
                    if time.monotonic() >= deadline:
                        budget_failure()
                        break
                    idx = next((i for i, s in enumerate(steps, start=1) if step_id(s, i) == sid), None)
                    if idx is None:
                        continue
                    spec = steps[idx - 1]
                    row = step_rows[sid]
                    row['status'] = 'RUNNING'
                    row['attemptCount'] += 1
                    row['startedAt'] = row['startedAt'] or now_iso()
                    step_states[sid] = 'RUNNING'
                    self._write_run_state(run_dir, run)

                    if bool(spec.get('requiresApproval')):
                        approval_key = str(spec.get('approvalKey') or f'{run_id}:{sid}')
                        row['approvalKey'] = approval_key
                        if not resume_blocked:
                            try:
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
                            except Exception as ex:
                                self._fail_step(run, row, step_states, sid, parse_service_error(ex), source='approval')
                                self._write_run_state(run_dir, run)
                                break
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
                            self._write_run_state(run_dir, run)
                            break
                        try:
                            ap_get = get_json(approval_url, f'/v1/approval/{approval_key}', mesh_token=mesh_token)
                        except Exception as ex:
                            self._fail_step(run, row, step_states, sid, parse_service_error(ex), source='approval')
                            self._write_run_state(run_dir, run)
                            break
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
                            self._write_run_state(run_dir, run)
                            break

                    # Runtime resolution is owned by the execution service
                    # (single resolver); the orchestrator no longer consults
                    # the router per step. The router remains an advisory /
                    # introspection service.
                    try:
                        step_timeout = max(1, int(spec.get('timeoutSec', 300) or 300))
                    except (TypeError, ValueError):
                        step_timeout = 300
                    remaining = deadline - time.monotonic()
                    call_timeout = int(max(5, min(step_timeout + 30, remaining + 5)))
                    max_attempts = retry_max_attempts
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
                                    'timeoutSec': step_timeout,
                                },
                                timeout=call_timeout,
                                mesh_token=mesh_token,
                            )
                        else:
                            ex = post_json(
                                execution_url,
                                '/v1/execution/submit-step-spec',
                                {'runId': run_id, 'stepId': sid, 'stepSpec': spec},
                                timeout=call_timeout,
                                mesh_token=mesh_token,
                            )
                    except Exception as exn:
                        failure = parse_service_error(exn)
                        failure['failureSource'] = 'execution'
                        retryable = self._retryable(failure, retry_on) and max_attempts > 1
                        if retryable and row['attemptCount'] < max_attempts:
                            self._schedule_retry(run, row, step_states, sid, failure)
                            self._write_run_state(run_dir, run)
                            break
                        self._fail_step(run, row, step_states, sid, failure, source='execution')
                        if retryable:
                            run['terminalReason'] = 'retry_exhausted'
                        self._write_run_state(run_dir, run)
                        break

                    payload = ex.get('result') if isinstance(ex.get('result'), dict) else {}
                    status = str(payload.get('status', 'FAILED')).upper()
                    row['endedAt'] = now_iso()
                    row['result'] = ex
                    runtime_meta = extract_runtime_metadata(ex)
                    if runtime_meta:
                        row.update(runtime_meta)

                    if ex.get('ok') and status == 'SUCCESS':
                        row['status'] = 'SUCCESS'
                        step_states[sid] = 'SUCCESS'
                        self._write_run_state(run_dir, run)
                        continue

                    failure = classify_execution_failure(ex)
                    retryable = self._retryable(failure, retry_on) and max_attempts > 1
                    if retryable and row['attemptCount'] < max_attempts:
                        self._schedule_retry(run, row, step_states, sid, failure)
                        self._write_run_state(run_dir, run)
                        break
                    row['status'] = 'FAILED'
                    step_states[sid] = 'FAILED'
                    run['state'] = 'FAILED'
                    run['terminalReason'] = 'retry_exhausted' if retryable else failure['failureReason']
                    run['failedStepId'] = sid
                    run['failureSource'] = failure['failureSource']
                    run['failureCategory'] = failure['failureCategory']
                    run['failureMessage'] = failure['failureMessage']
                    run.update(runtime_meta)
                    self._write_run_state(run_dir, run)
                    break

                if run['state'] in {'FAILED', 'BLOCKED'}:
                    break
        except Exception as ex:
            # Defensive terminal path: a run must never be silently stuck in
            # RUNNING because of an unexpected orchestrator error.
            run['state'] = 'FAILED'
            run['terminalReason'] = 'orchestrator_error'
            run['failureCategory'] = 'runtime'
            run['failureSource'] = 'orchestrator'
            run['failureMessage'] = f'unexpected orchestrator error: {ex}'
            for row in run['steps']:
                if str(row.get('status', '')).upper() == 'RUNNING':
                    row['status'] = 'FAILED'
                    row['endedAt'] = row.get('endedAt') or now_iso()

        lifecycle_warning = None
        try:
            reason_check = post_json(
                lifecycle_url,
                '/v1/lifecycle/validate-terminal-reason',
                {'reason': run['terminalReason']},
                mesh_token=mesh_token,
            )
            if reason_check.get('valid') is False:
                lifecycle_warning = (
                    f"terminal reason {run['terminalReason']!r} is not in the lifecycle taxonomy"
                )
        except Exception as ex:
            lifecycle_warning = str(ex)

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
                        reconcile_cmd=str(body.get('reconcileCmd', '')).strip(),
                        resume_blocked=bool(body.get('resumeBlocked', False)),
                        caller=str(body.get('caller', 'runner')).strip() or 'runner',
                        skip_drift_lint=bool(body.get('skipDriftLint', False)),
                        skip_capability_parity=bool(body.get('skipCapabilityParity', False)),
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
    # Bind the port BEFORE reconciling: a second orchestrator started while the
    # first is mid-run fails here with "Address already in use" and never
    # touches the live runs. Only the process that owns the port reconciles.
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    reconciled = engine.reconcile_orphaned_runs()
    print(json.dumps({'status': 'listening', 'service': 'orchestrator', 'host': args.host, 'port': args.port, 'reconciledRuns': reconciled}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
