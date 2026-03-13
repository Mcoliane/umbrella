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
import urllib.error
import urllib.parse

sys.path.append(str(Path(__file__).resolve().parents[2]))
from services.memory.auth import check_auth
from services.runtime_contract import RuntimeContract


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


def dependency_failure(source: str, reason: str, message: str, *, status: str = 'FAILED', category: str = 'dependency', exit_code: int = 1) -> dict:
    return {
        'ok': False,
        'exitCode': exit_code,
        'failureCategory': category,
        'failureSource': source,
        'failureReason': reason,
        'result': {
            'status': status,
            'kind': source,
            'error': message,
        },
        'stderr': message,
        'command': [source],
    }


class ExecutionEngine:
    def __init__(
        self,
        umbrella_root: Path,
        routing_path: Path,
        capability_path: Path,
        memory_core_url: str,
        memory_url: str,
        policy_url: str,
        mesh_token: str,
        catalog_url: str = '',
        plugin_host_url: str = '',
    ):
        self.root = umbrella_root
        self.routing_path = routing_path
        self.routing_config = json.loads(routing_path.read_text(encoding='utf-8')) if routing_path.exists() else {}
        self.capability_path = capability_path
        self.runtime_contract = RuntimeContract(capability_path)
        self.adapter = self.root / 'scripts' / 'adapters' / 'removed-runtime-adapter'
        self.memory_core_url = memory_core_url.rstrip('/')
        self.memory_url = memory_url.rstrip('/')
        self.policy_url = policy_url.rstrip('/')
        self.mesh_token = mesh_token.strip()
        self.catalog_url = catalog_url.rstrip('/')
        self.plugin_host_url = plugin_host_url.rstrip('/')
        self.native_actions = {'memoryWrite', 'memoryRead', 'memoryDelete', 'memoryList', 'memory.promote', 'memory.hydrate'}

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

    def _memory_headers(self, actor: str = 'execution') -> dict:
        headers = self._headers()
        headers['X-Actor'] = actor
        return headers

    def _post_memory_service(self, path: str, payload: dict, timeout: int = 30, actor: str = 'execution') -> dict:
        req = urllib.request.Request(
            f'{self.memory_url}{path}',
            method='POST',
            data=json.dumps(payload).encode('utf-8'),
            headers=self._memory_headers(actor=actor),
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))

    def _post_json(self, base_url: str, path: str, payload: dict, timeout: int = 30) -> dict:
        req = urllib.request.Request(
            f'{base_url.rstrip("/")}{path}',
            method='POST',
            data=json.dumps(payload).encode('utf-8'),
            headers=self._headers(),
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))

    def _get_json(self, base_url: str, path: str, timeout: int = 15) -> dict:
        req = urllib.request.Request(f'{base_url.rstrip("/")}{path}', method='GET', headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))

    def _authorize_step(self, step_spec: dict, timeout: int = 15) -> dict:
        return self._post_json(self.policy_url, '/v1/policy/authorize-step', {'stepSpec': step_spec}, timeout=timeout)

    def _catalog_action(self, action_id: str, timeout: int = 15) -> dict | None:
        if not self.catalog_url:
            return None
        try:
            return self._get_json(self.catalog_url, f'/v1/catalog/actions/{urllib.parse.quote(action_id, safe="")}', timeout=timeout)
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                return None
            raise

    def _invoke_plugin_action(self, run_id: str, step_id: str, action_id: str, step_spec: dict) -> dict:
        if not self.plugin_host_url:
            return dependency_failure('plugin-host', 'dependency_unavailable', 'plugin-host-url is not configured')
        invocation = {
            'runId': run_id,
            'stepId': step_id,
            'agentId': str(step_spec.get('agentId') or ((step_spec.get('metadata') or {}).get('agentId', ''))),
            'action': action_id,
            'inputs': step_spec.get('inputs') if isinstance(step_spec.get('inputs'), dict) else {},
            'context': step_spec.get('metadata') if isinstance(step_spec.get('metadata'), dict) else {},
            'timeouts': {'timeoutSec': int(step_spec.get('timeoutSec', 30))},
        }
        try:
            return self._post_json(self.plugin_host_url, '/v1/plugin-host/invoke', {'actionId': action_id, 'invocation': invocation}, timeout=int(step_spec.get('timeoutSec', 30)) + 5)
        except urllib.error.URLError as ex:
            return dependency_failure('plugin-host', 'dependency_unavailable', str(ex))
        except urllib.error.HTTPError as ex:
            body = ''
            try:
                body = ex.read().decode('utf-8')
            except Exception:
                body = ''
            reason = 'execution_validation_failed' if ex.code == 400 else 'dependency_request_failed'
            category = 'validation' if ex.code == 400 else 'dependency'
            return dependency_failure('plugin-host', reason, f'HTTP {ex.code}: {body or ex.reason}', category=category)

    def _requested_runtime(self, step_spec: dict) -> str:
        requested = str(step_spec.get('runtime') or '').strip()
        metadata = step_spec.get('metadata') if isinstance(step_spec.get('metadata'), dict) else {}
        if not requested:
            requested = str(metadata.get('runtimeRequested') or '').strip()
        return requested

    def _fallback_runtimes(self, requested_runtime: str, step_spec: dict | None = None) -> list[str]:
        metadata = (step_spec or {}).get('metadata') if isinstance((step_spec or {}).get('metadata'), dict) else {}
        if metadata.get('allowCapabilityReroute') is False:
            return []
        reroute = self.routing_config.get('capabilityReroute') or {}
        if not bool(reroute.get('enabled', False)):
            return []
        return [str(x).strip() for x in ((reroute.get('fallbackByRuntime') or {}).get(requested_runtime, [])) if str(x).strip()]

    def _executor_runtime(self, runtime_resolved: str) -> str:
        if runtime_resolved == 'umbrella-agent-runtime':
            return 'plugin-host'
        if runtime_resolved == 'native':
            return 'native'
        return 'removed-adapter'

    def runtime_support_payload(self, action_id: str = '', requested_runtime: str = '') -> dict:
        info = self.runtime_contract.resolve_action(action_id)
        resolved_runtime, runtime_supported, runtime_reason = self.runtime_contract.resolve_compatible_runtime(
            info.get('resolvedActionId', ''),
            requested_runtime,
            self._fallback_runtimes(requested_runtime),
        )
        return {
            'checkedAt': now_iso(),
            'actionId': info.get('originalActionId', ''),
            'resolvedActionId': info.get('resolvedActionId', ''),
            'deprecatedActionId': info.get('deprecatedActionId', ''),
            'runtimeRequested': requested_runtime,
            'runtimeResolved': resolved_runtime or info.get('preferredRuntime', ''),
            'runtimeReason': runtime_reason if requested_runtime else '',
            'runtimeSupported': runtime_supported if requested_runtime else bool(info.get('supportedRuntimes')),
            'supportedRuntimes': info.get('supportedRuntimes', []),
            'actionFamily': info.get('actionFamily', ''),
            'runtimeCapability': info.get('runtimeCapability', ''),
        }

    def _resolve_runtime(self, action_info: dict, step_spec: dict, catalog_action: dict | None) -> dict:
        requested = self._requested_runtime(step_spec)
        resolved_action = str(action_info.get('resolvedActionId', '')).strip()
        supported_runtimes = list(action_info.get('supportedRuntimes') or [])
        preferred = str(action_info.get('preferredRuntime') or '').strip()
        action_family = str(action_info.get('actionFamily') or '').strip()
        runtime_capability = str(action_info.get('runtimeCapability') or '').strip()
        deprecated = str(action_info.get('deprecatedActionId') or '').strip()
        runtime_supported = True
        reason = ''

        if requested:
            runtime_resolved, runtime_supported, reason = self.runtime_contract.resolve_compatible_runtime(
                resolved_action,
                requested,
                self._fallback_runtimes(requested, step_spec),
            )
            if not runtime_resolved:
                runtime_resolved = requested
        else:
            runtime_resolved = preferred
            if isinstance(catalog_action, dict) and runtime_resolved == 'umbrella-agent-runtime':
                reason = 'catalog_action'
            elif runtime_resolved == 'native':
                reason = 'native_action'
            elif runtime_resolved == 'removed':
                reason = 'legacy_runtime_adapter'
            else:
                reason = 'unresolved_runtime'

        if not reason:
            if runtime_resolved == 'umbrella-agent-runtime' and isinstance(catalog_action, dict):
                reason = 'catalog_action'
            elif runtime_resolved == 'native':
                reason = 'native_action'
            elif runtime_resolved == 'removed':
                reason = 'legacy_runtime_adapter'
            else:
                reason = 'requested_runtime'

        return {
            'runtimeRequested': requested,
            'runtimeResolved': runtime_resolved,
            'runtimeClass': runtime_resolved,
            'runtimeReason': reason,
            'executorRuntime': self._executor_runtime(runtime_resolved),
            'runtimeSupported': runtime_supported,
            'supportedRuntimes': supported_runtimes,
            'actionFamily': action_family,
            'runtimeCapability': runtime_capability,
            'resolvedActionId': resolved_action,
            'deprecatedActionId': deprecated,
        }

    def _with_runtime_metadata(self, payload: dict, runtime: dict) -> dict:
        out = dict(payload)
        out.update(runtime)
        result = out.get('result')
        if isinstance(result, dict):
            merged = dict(result)
            merged.setdefault('runtimeRequested', runtime.get('runtimeRequested', ''))
            merged.setdefault('runtimeResolved', runtime.get('runtimeResolved', ''))
            merged.setdefault('runtimeClass', runtime.get('runtimeClass', ''))
            merged.setdefault('runtimeReason', runtime.get('runtimeReason', ''))
            merged.setdefault('executorRuntime', runtime.get('executorRuntime', ''))
            merged.setdefault('resolvedActionId', runtime.get('resolvedActionId', ''))
            merged.setdefault('runtimeSupported', runtime.get('runtimeSupported', True))
            merged.setdefault('supportedRuntimes', runtime.get('supportedRuntimes', []))
            merged.setdefault('actionFamily', runtime.get('actionFamily', ''))
            merged.setdefault('runtimeCapability', runtime.get('runtimeCapability', ''))
            if runtime.get('deprecatedActionId'):
                merged.setdefault('deprecatedActionId', runtime.get('deprecatedActionId', ''))
            out['result'] = merged
        return out

    def _submit_native_action(self, run_id: str, step_id: str, step_spec: dict, resolved_action: str) -> dict | None:
        action = str(resolved_action or '').strip()
        if action == 'memoryWrite':
            namespace = str(step_spec.get('namespace', '')).strip()
            key = str(step_spec.get('key', '')).strip()
            try:
                out = self._post_memory(
                    '/v1/memory-core/put',
                    {
                        'namespace': namespace,
                        'key': key,
                        'value': step_spec.get('value'),
                        'metadata': step_spec.get('metadata') if isinstance(step_spec.get('metadata'), dict) else {},
                    },
                )
            except urllib.error.URLError as ex:
                return dependency_failure('memory-core', 'dependency_unavailable', str(ex))
            except urllib.error.HTTPError as ex:
                body = ''
                try:
                    body = ex.read().decode('utf-8')
                except Exception:
                    body = ''
                reason = 'execution_validation_failed' if ex.code == 400 else 'dependency_request_failed'
                category = 'validation' if ex.code == 400 else 'dependency'
                return dependency_failure('memory-core', reason, f'HTTP {ex.code}: {body or ex.reason}', category=category)
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
            try:
                out = self._post_memory('/v1/memory-core/get', {'namespace': namespace, 'key': key})
            except urllib.error.URLError as ex:
                return dependency_failure('memory-core', 'dependency_unavailable', str(ex))
            except urllib.error.HTTPError as ex:
                body = ''
                try:
                    body = ex.read().decode('utf-8')
                except Exception:
                    body = ''
                reason = 'execution_validation_failed' if ex.code == 400 else 'dependency_request_failed'
                category = 'validation' if ex.code == 400 else 'dependency'
                return dependency_failure('memory-core', reason, f'HTTP {ex.code}: {body or ex.reason}', category=category)
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
            try:
                out = self._post_memory('/v1/memory-core/delete', {'namespace': namespace, 'key': key})
            except urllib.error.URLError as ex:
                return dependency_failure('memory-core', 'dependency_unavailable', str(ex))
            except urllib.error.HTTPError as ex:
                body = ''
                try:
                    body = ex.read().decode('utf-8')
                except Exception:
                    body = ''
                reason = 'execution_validation_failed' if ex.code == 400 else 'dependency_request_failed'
                category = 'validation' if ex.code == 400 else 'dependency'
                return dependency_failure('memory-core', reason, f'HTTP {ex.code}: {body or ex.reason}', category=category)
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
            try:
                out = self._post_memory('/v1/memory-core/list', {'namespace': namespace})
            except urllib.error.URLError as ex:
                return dependency_failure('memory-core', 'dependency_unavailable', str(ex))
            except urllib.error.HTTPError as ex:
                body = ''
                try:
                    body = ex.read().decode('utf-8')
                except Exception:
                    body = ''
                reason = 'execution_validation_failed' if ex.code == 400 else 'dependency_request_failed'
                category = 'validation' if ex.code == 400 else 'dependency'
                return dependency_failure('memory-core', reason, f'HTTP {ex.code}: {body or ex.reason}', category=category)
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

        if action == 'memory.promote':
            inputs = step_spec.get('inputs') if isinstance(step_spec.get('inputs'), dict) else {}
            namespace = str(inputs.get('namespace', '')).strip()
            key = str(inputs.get('key', '')).strip()
            if not namespace or not key:
                return dependency_failure('execution', 'execution_validation_failed', 'memory.promote requires inputs.namespace and inputs.key', category='validation')
            actor = str(((step_spec.get('metadata') or {}).get('actor')) or 'execution:memory.promote').strip() or 'execution:memory.promote'
            try:
                src = self._post_memory('/v1/memory-core/get', {'namespace': namespace, 'key': key})
            except urllib.error.URLError as ex:
                return dependency_failure('memory-core', 'dependency_unavailable', str(ex))
            except urllib.error.HTTPError as ex:
                body = ''
                try:
                    body = ex.read().decode('utf-8')
                except Exception:
                    body = ''
                reason = 'execution_validation_failed' if ex.code == 400 else 'dependency_request_failed'
                category = 'validation' if ex.code == 400 else 'dependency'
                return dependency_failure('memory-core', reason, f'HTTP {ex.code}: {body or ex.reason}', category=category)
            if not bool(src.get('exists', False)):
                return dependency_failure('memory-core', 'execution_validation_failed', f'memory-core key not found: {namespace}/{key}', category='validation')
            tags = [str(t).strip() for t in (inputs.get('tags') or []) if str(t).strip()]
            if not tags:
                tags = [t.strip() for t in str(inputs.get('tagsCsv', 'memory-core,promoted')).split(',') if t.strip()]
            payload = {
                'source': {
                    'namespace': namespace,
                    'key': key,
                    'value': (src.get('memory') or {}).get('value'),
                    'metadata': (src.get('memory') or {}).get('metadata') or {},
                },
                'target': {
                    'namespace': str(inputs.get('targetNamespace') or namespace).strip() or namespace,
                    'node_id': str(inputs.get('nodeId', '')).strip(),
                    'kind': str(inputs.get('kind', 'fact')).strip() or 'fact',
                    'title': str(inputs.get('title', '')).strip(),
                    'tags': tags,
                    'source': 'memory-core-promotion',
                },
                'provenance': {
                    'runId': run_id,
                    'stepId': step_id,
                    'idempotencyKey': str(inputs.get('idempotencyKey', '')).strip(),
                    'actor': actor,
                },
            }
            queue = bool(inputs.get('queue', False) or ((step_spec.get('metadata') or {}).get('async', False)))
            process_queue = bool(inputs.get('processQueue', False))
            try:
                if queue:
                    out = self._post_memory_service('/v1/promotions/queue', payload, actor=actor)
                    if process_queue:
                        processed = self._post_memory_service('/v1/promotions/process-queue', {'maxItems': int(inputs.get('maxItems', 1) or 1)}, actor=actor)
                        out = {'queued': out, 'processed': processed}
                else:
                    out = self._post_memory_service('/v1/promotions', payload, actor=actor)
            except urllib.error.URLError as ex:
                return dependency_failure('memory', 'dependency_unavailable', str(ex))
            except urllib.error.HTTPError as ex:
                body = ''
                try:
                    body = ex.read().decode('utf-8')
                except Exception:
                    body = ''
                reason = 'execution_validation_failed' if ex.code == 400 else 'dependency_request_failed'
                category = 'validation' if ex.code == 400 else 'dependency'
                return dependency_failure('memory', reason, f'HTTP {ex.code}: {body or ex.reason}', category=category)
            return {
                'ok': True,
                'exitCode': 0,
                'result': {
                    'status': 'SUCCESS',
                    'kind': 'memory.promote',
                    'promotion': out,
                },
                'stderr': '',
                'command': ['memory', 'promotions'],
            }

        if action == 'memory.hydrate':
            inputs = step_spec.get('inputs') if isinstance(step_spec.get('inputs'), dict) else {}
            node_id = str(inputs.get('nodeId', '')).strip()
            if not node_id:
                return dependency_failure('execution', 'execution_validation_failed', 'memory.hydrate requires inputs.nodeId', category='validation')
            actor = str(((step_spec.get('metadata') or {}).get('actor')) or 'execution:memory.hydrate').strip() or 'execution:memory.hydrate'
            payload = {
                'node_id': node_id,
                'target': {
                    'namespace': str(inputs.get('targetNamespace', '')).strip(),
                    'key': str(inputs.get('targetKey', '')).strip(),
                },
                'context': {
                    'phase': str(inputs.get('phase', 'bootstrap')).strip().lower() or 'bootstrap',
                },
            }
            try:
                hydration = self._post_memory_service('/v1/hydrations/payload', payload, actor=actor)
            except urllib.error.URLError as ex:
                return dependency_failure('memory', 'dependency_unavailable', str(ex))
            except urllib.error.HTTPError as ex:
                body = ''
                try:
                    body = ex.read().decode('utf-8')
                except Exception:
                    body = ''
                reason = 'execution_validation_failed' if ex.code == 400 else 'dependency_request_failed'
                category = 'validation' if ex.code == 400 else 'dependency'
                return dependency_failure('memory', reason, f'HTTP {ex.code}: {body or ex.reason}', category=category)
            mem = hydration.get('memoryCore') if isinstance(hydration.get('memoryCore'), dict) else {}
            if not mem:
                return dependency_failure('memory', 'execution_validation_failed', 'hydration payload missing memoryCore object', category='validation')
            try:
                put_out = self._post_memory(
                    '/v1/memory-core/put',
                    {
                        'namespace': mem.get('namespace'),
                        'key': mem.get('key'),
                        'value': mem.get('value'),
                        'metadata': mem.get('metadata') if isinstance(mem.get('metadata'), dict) else {},
                    },
                )
            except urllib.error.URLError as ex:
                return dependency_failure('memory-core', 'dependency_unavailable', str(ex))
            except urllib.error.HTTPError as ex:
                body = ''
                try:
                    body = ex.read().decode('utf-8')
                except Exception:
                    body = ''
                reason = 'execution_validation_failed' if ex.code == 400 else 'dependency_request_failed'
                category = 'validation' if ex.code == 400 else 'dependency'
                return dependency_failure('memory-core', reason, f'HTTP {ex.code}: {body or ex.reason}', category=category)
            ok = bool(put_out.get('ok', False))
            return {
                'ok': ok,
                'exitCode': 0 if ok else 1,
                'result': {
                    'status': 'SUCCESS' if ok else 'FAILED',
                    'kind': 'memory.hydrate',
                    'hydration': hydration,
                    'memoryCoreWrite': put_out,
                },
                'stderr': '',
                'command': ['memory', 'hydrations'],
            }
        return None

    def _submit_umbrella_agent_runtime_action(self, run_id: str, step_id: str, step_spec: dict) -> dict:
        action = str(step_spec.get('resolvedActionId') or step_spec.get('action', '')).strip()
        return self._invoke_plugin_action(run_id=run_id, step_id=step_id, action_id=action, step_spec=step_spec)

    def _submit_removed_action(self, run_id: str, step_id: str, step_spec: dict) -> dict:
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

    def _run(self, args: list[str]) -> dict:
        cmd = [str(self.adapter), '--umbrella-root', str(self.root)] + args
        proc = subprocess.run(cmd, cwd=str(self.root), capture_output=True, text=True)
        payload = parse_payload(proc.stdout)
        out = {
            'ok': proc.returncode == 0,
            'exitCode': proc.returncode,
            'result': payload if isinstance(payload, dict) else {'stdout': (proc.stdout or '')[-4000:]},
            'stderr': (proc.stderr or '')[-4000:],
            'command': cmd,
        }
        if not out['ok']:
            result = out['result'] if isinstance(out.get('result'), dict) else {}
            if bool(result.get('timedOut', False)) or int(out.get('exitCode', 1)) == 124:
                out['failureCategory'] = 'runtime'
                out['failureSource'] = 'adapter'
                out['failureReason'] = 'timeout'
            elif 'error' in result:
                out['failureCategory'] = 'validation'
                out['failureSource'] = 'adapter'
                out['failureReason'] = 'execution_validation_failed'
            else:
                out['failureCategory'] = 'runtime'
                out['failureSource'] = 'adapter'
                out['failureReason'] = 'execution_runtime_failed'
        return out

    def _runtime_capability_unsupported(self, runtime: dict, action_id: str) -> dict:
        requested = str(runtime.get('runtimeRequested', '')).strip()
        resolved = str(runtime.get('runtimeResolved', '')).strip()
        reason = str(runtime.get('runtimeReason', '')).strip() or 'runtime_capability_unsupported'
        supported = runtime.get('supportedRuntimes', [])
        message = f'action {action_id} is not supported by requested runtime {requested or resolved}'
        if supported:
            message = f'{message}; supported runtimes: {", ".join(supported)}'
        return {
            'ok': False,
            'exitCode': 1,
            'failureCategory': 'validation',
            'failureSource': 'execution',
            'failureReason': 'runtime_capability_unsupported',
            'result': {
                'status': 'FAILED',
                'kind': 'runtimeCapability',
                'error': message,
                'runtimeRequested': requested,
                'runtimeResolved': resolved,
                'runtimeReason': reason,
                'resolvedActionId': runtime.get('resolvedActionId', action_id),
                'supportedRuntimes': supported,
                'actionFamily': runtime.get('actionFamily', ''),
                'runtimeCapability': runtime.get('runtimeCapability', ''),
            },
            'stderr': message,
            'command': ['execution', 'submit-step-spec'],
        }

    def submit_step_spec(self, run_id: str, step_id: str, step_spec: dict) -> dict:
        action_info = self.runtime_contract.resolve_action(str(step_spec.get('action', '')).strip())
        original_action = str(action_info.get('originalActionId', '')).strip()
        resolved_action = str(action_info.get('resolvedActionId', '')).strip()
        action = original_action
        resolved_step_spec = dict(step_spec) if isinstance(step_spec, dict) else {}
        resolved_step_spec['action'] = resolved_action
        resolved_step_spec['resolvedActionId'] = resolved_action
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
        metadata['originalActionId'] = original_action
        metadata['resolvedActionId'] = resolved_action
        policy_step['metadata'] = metadata
        policy_step['action'] = resolved_action
        resolved_step_spec['metadata'] = metadata
        if action:
            try:
                auth = self._authorize_step(step_spec=policy_step)
            except urllib.error.URLError as ex:
                return dependency_failure('policy', 'dependency_unavailable', str(ex))
            except urllib.error.HTTPError as ex:
                body = ''
                try:
                    body = ex.read().decode('utf-8')
                except Exception:
                    body = ''
                return dependency_failure('policy', 'dependency_request_failed', f'HTTP {ex.code}: {body or ex.reason}')
            if not bool(auth.get('allowed', False)):
                return {
                    'ok': False,
                    'exitCode': 1,
                    'failureCategory': 'policy',
                    'failureSource': 'policy',
                    'failureReason': 'execution_policy_denied',
                    'result': {'status': 'FAILED', 'kind': 'policy', 'policyDecision': auth},
                    'stderr': str(auth.get('reason', 'policy_denied')),
                    'command': ['policy', 'authorize-step'],
                }

        catalog_action = None
        if resolved_action:
            try:
                catalog_action = self._catalog_action(resolved_action)
            except urllib.error.URLError as ex:
                return dependency_failure('catalog', 'dependency_unavailable', str(ex))
            except urllib.error.HTTPError as ex:
                body = ''
                try:
                    body = ex.read().decode('utf-8')
                except Exception:
                    body = ''
                return dependency_failure('catalog', 'dependency_request_failed', f'HTTP {ex.code}: {body or ex.reason}')
        runtime = self._resolve_runtime(action_info, step_spec, catalog_action)
        if runtime['runtimeRequested'] and not bool(runtime.get('runtimeSupported', True)) and not str(runtime.get('runtimeReason', '')).startswith('capability_reroute:'):
            return self._with_runtime_metadata(self._runtime_capability_unsupported(runtime, action), runtime)
        if runtime['runtimeResolved'] == 'native':
            out = self._submit_native_action(run_id=run_id, step_id=step_id, step_spec=resolved_step_spec, resolved_action=resolved_action)
            return self._with_runtime_metadata(out or dependency_failure('execution', 'execution_validation_failed', f'unsupported native action: {action}', category='validation'), runtime)
        if runtime['runtimeResolved'] == 'umbrella-agent-runtime':
            return self._with_runtime_metadata(self._submit_umbrella_agent_runtime_action(run_id=run_id, step_id=step_id, step_spec=resolved_step_spec), runtime)
        return self._with_runtime_metadata(self._submit_removed_action(run_id=run_id, step_id=step_id, step_spec=resolved_step_spec), runtime)

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
            parsed = urlparse(self.path)
            path = parsed.path
            if path == '/v1/execution/health':
                return json_response(self, 200, {'status': 'ok', 'service': 'execution', 'checkedAt': now_iso()})
            if path == '/v1/execution/runtime-support':
                params = urllib.parse.parse_qs(parsed.query or '')
                action_id = str((params.get('actionId') or [''])[0])
                requested_runtime = str((params.get('runtime') or [''])[0])
                return json_response(self, 200, engine.runtime_support_payload(action_id=action_id, requested_runtime=requested_runtime))
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
    ap.add_argument('--routing', default='control-plane/router/runtime-routing.json')
    ap.add_argument('--runtime-capabilities', default='control-plane/router/runtime-capabilities.json')
    ap.add_argument('--memory-core-url', default='http://127.0.0.1:8798')
    ap.add_argument('--memory-url', default='http://127.0.0.1:8787')
    ap.add_argument('--policy-url', default='http://127.0.0.1:8791')
    ap.add_argument('--catalog-url', default='')
    ap.add_argument('--plugin-host-url', default='')
    ap.add_argument('--mesh-token', default='')
    ap.add_argument('--token', default='')
    args = ap.parse_args()

    root = Path(args.umbrella_root).resolve()
    engine = ExecutionEngine(
        umbrella_root=root,
        routing_path=(root / args.routing),
        capability_path=(root / args.runtime_capabilities),
        memory_core_url=args.memory_core_url,
        memory_url=args.memory_url,
        policy_url=args.policy_url,
        mesh_token=args.mesh_token,
        catalog_url=args.catalog_url,
        plugin_host_url=args.plugin_host_url,
    )
    handler = handler_factory(engine=engine, token=args.token.strip())
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({'status': 'listening', 'service': 'execution', 'host': args.host, 'port': args.port}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
