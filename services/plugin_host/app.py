#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

sys.path.append(str(Path(__file__).resolve().parents[2]))
from services.memory.auth import check_auth


DEFAULT_INPUT_BYTES = 32768
DEFAULT_OUTPUT_BYTES = 4096
DEFAULT_TIMEOUT_SEC = 30
ALLOWED_FS_POLICIES = {'scratch-only', 'install-root'}
ALLOWED_NETWORK_POLICIES = {'none', 'http-outbound'}
ALLOWED_ISOLATION_PROFILES = {'process-default', 'shell-restricted', 'python-restricted', 'http-outbound', 'container-default', 'container-restricted'}


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


def parse_payload(raw: str):
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    for line in reversed([ln.strip() for ln in raw.splitlines() if ln.strip()]):
        try:
            return json.loads(line)
        except Exception:
            continue
    return None


def truncate_text(text: str, limit: int) -> str:
    if limit < 0:
        limit = 0
    raw = text or ''
    encoded = raw.encode('utf-8')
    if len(encoded) <= limit:
        return raw
    if limit == 0:
        return ''
    return encoded[:limit].decode('utf-8', errors='ignore')


class PluginHostEngine:
    def __init__(self, umbrella_root: Path, catalog_url: str, mesh_token: str, container_runtime: str):
        self.root = umbrella_root
        self.catalog_url = catalog_url.rstrip('/')
        self.mesh_token = mesh_token.strip()
        self.container_runtime_preference = str(container_runtime or 'auto').strip() or 'auto'
        self.scratch_root = self.root / 'control-plane' / 'observability' / 'plugin-host' / 'scratch'
        self.scratch_root.mkdir(parents=True, exist_ok=True)

    def resolve_container_runtime(self) -> str:
        if self.container_runtime_preference == 'none':
            return ''
        if self.container_runtime_preference in {'docker', 'podman'}:
            return self.container_runtime_preference if shutil.which(self.container_runtime_preference) else ''
        for candidate in ('docker', 'podman'):
            if shutil.which(candidate):
                return candidate
        return ''

    def _headers(self) -> dict:
        headers = {'Content-Type': 'application/json'}
        if self.mesh_token:
            headers['Authorization'] = f'Bearer {self.mesh_token}'
        return headers

    def _get_json(self, path: str, timeout: int = 15) -> dict:
        req = urllib.request.Request(f'{self.catalog_url}{path}', method='GET', headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))

    def resolve_action(self, action_id: str) -> tuple[dict, dict]:
        action = self._get_json(f'/v1/catalog/actions/{quote(action_id, safe="")}')
        plugin_id = str(action.get('pluginId', '')).strip()
        if not plugin_id:
            raise ValueError('catalog action missing pluginId')
        item = self._get_json(f'/v1/catalog/items/{quote(plugin_id, safe="")}')
        return action, item

    def _effective_execution_policy(self, item: dict, invocation: dict) -> dict:
        manifest_policy = item.get('executionPolicy') if isinstance(item.get('executionPolicy'), dict) else {}
        timeouts = invocation.get('timeouts') if isinstance(invocation.get('timeouts'), dict) else {}
        requested_timeout = int(timeouts.get('timeoutSec', DEFAULT_TIMEOUT_SEC) or DEFAULT_TIMEOUT_SEC)
        max_runtime_sec = int(manifest_policy.get('maxRuntimeSec', requested_timeout) or requested_timeout)
        max_output_bytes = int(manifest_policy.get('maxOutputBytes', DEFAULT_OUTPUT_BYTES) or DEFAULT_OUTPUT_BYTES)
        max_input_bytes = int(manifest_policy.get('maxInputBytes', DEFAULT_INPUT_BYTES) or DEFAULT_INPUT_BYTES)
        env_allowlist = [str(x).strip() for x in (manifest_policy.get('envAllowlist') or []) if str(x).strip()]
        fs_policy = str(manifest_policy.get('fs', 'scratch-only')).strip() or 'scratch-only'
        network_policy = str(manifest_policy.get('network', 'none')).strip() or 'none'
        isolation_profile = str(manifest_policy.get('isolationProfile', 'process-default')).strip() or 'process-default'
        if fs_policy not in ALLOWED_FS_POLICIES:
            raise ValueError(f'unsupported fs execution policy: {fs_policy}')
        if network_policy not in ALLOWED_NETWORK_POLICIES:
            raise ValueError(f'unsupported network execution policy: {network_policy}')
        if isolation_profile not in ALLOWED_ISOLATION_PROFILES:
            raise ValueError(f'unsupported isolation profile: {isolation_profile}')
        if requested_timeout > max_runtime_sec:
            raise ValueError('requested timeout exceeds plugin executionPolicy.maxRuntimeSec')
        return {
            'maxRuntimeSec': max(1, max_runtime_sec),
            'maxOutputBytes': max(0, max_output_bytes),
            'maxInputBytes': max(1, max_input_bytes),
            'envAllowlist': env_allowlist,
            'fs': fs_policy,
            'network': network_policy,
            'isolationProfile': isolation_profile,
        }

    def _assert_invocation_allowed(self, action_id: str, item: dict):
        compatible = item.get('compatible') if isinstance(item.get('compatible'), dict) else {}
        if not bool(compatible.get('ok', False)):
            raise ValueError('catalog item is incompatible with this umbrella version')
        if not bool(item.get('enabled', False)):
            raise ValueError('catalog item is disabled')
        install = item.get('install') if isinstance(item.get('install'), dict) else {}
        lifecycle_state = str(install.get('lifecycleState', '')).strip()
        if lifecycle_state in {'failed', 'incompatible'}:
            raise ValueError(f'catalog item lifecycle state does not permit invocation: {lifecycle_state}')
        actions = item.get('actions') if isinstance(item.get('actions'), list) else []
        if action_id not in {str(row.get('id', '')).strip() for row in actions if isinstance(row, dict)}:
            raise ValueError('catalog item does not declare requested action')
        runtime_support = ((compatible.get('pluginHostRuntimes') or {}).get('ok'))
        if runtime_support is False:
            raise ValueError('catalog item runtime is not supported by plugin-host')

    def _scratch_dir(self, invocation: dict) -> Path:
        run_id = str(invocation.get('runId', '')).strip() or f'run-{uuid.uuid4().hex[:12]}'
        step_id = str(invocation.get('stepId', '')).strip() or f'step-{uuid.uuid4().hex[:12]}'
        scratch_dir = self.scratch_root / run_id / step_id
        scratch_dir.mkdir(parents=True, exist_ok=True)
        return scratch_dir

    def _build_env(self, policy: dict, invocation: dict, scratch_dir: Path) -> dict:
        parent_env = os.environ
        env = {
            'PATH': parent_env.get('PATH', '/usr/bin:/bin'),
            'TMPDIR': str(scratch_dir),
            'HOME': str(scratch_dir),
            'PWD': str(scratch_dir),
            'UMBRELLA_PLUGIN_SCRATCH_DIR': str(scratch_dir),
            'UMBRELLA_PLUGIN_RUN_ID': str(invocation.get('runId', '')).strip(),
            'UMBRELLA_PLUGIN_STEP_ID': str(invocation.get('stepId', '')).strip(),
            'UMBRELLA_PLUGIN_AGENT_ID': str(invocation.get('agentId', '')).strip(),
        }
        for key in policy.get('envAllowlist', []):
            if key in parent_env:
                env[key] = parent_env[key]
        return env

    def invoke(self, action_id: str, invocation: dict) -> dict:
        action, item = self.resolve_action(action_id)
        self._assert_invocation_allowed(action_id, item)
        policy = self._effective_execution_policy(item, invocation)

        runtime = str(item.get('runtime', '')).strip()
        entrypoint = Path(str(item.get('entrypoint', '')).strip())
        if not entrypoint.exists():
            raise ValueError('plugin entrypoint not found')
        install_root = Path(str((item.get('install') or {}).get('installPath', entrypoint.parent.parent))).resolve()
        if not install_root.exists():
            raise ValueError('plugin install path not found')
        scratch_dir = self._scratch_dir(invocation)
        env = self._build_env(policy, invocation, scratch_dir)
        payload = {
            'invokedAt': now_iso(),
            'action': action,
            'plugin': {
                'id': item.get('id'),
                'name': item.get('name'),
                'version': item.get('version'),
                'runtime': runtime,
            },
            'executionPolicy': policy,
            'invocation': invocation,
        }
        payload_json = json.dumps(payload)
        if len(payload_json.encode('utf-8')) > policy['maxInputBytes']:
            raise ValueError('invocation payload exceeds plugin executionPolicy.maxInputBytes')

        if runtime == 'shell':
            cmd = [str(entrypoint)]
        elif runtime == 'python':
            cmd = ['python3', str(entrypoint)]
        elif runtime == 'container':
            container = item.get('container') if isinstance(item.get('container'), dict) else {}
            image = str(container.get('image', '')).strip()
            if not image:
                raise ValueError('container runtime requires plugin.container.image')
            runner = self.resolve_container_runtime()
            if not runner:
                raise ValueError('container runtime not available; configure docker or podman to enable runtime=container plugins')
            mount_mode = 'rw' if policy['fs'] == 'install-root' else 'ro'
            cmd = [
                runner,
                'run',
                '--rm',
                '-i',
                '--network',
                'none',
                '-v',
                f'{install_root}:/plugin:{mount_mode}',
                '-v',
                f'{scratch_dir}:/scratch:rw',
                '-w',
                '/plugin',
            ]
            if policy['fs'] == 'scratch-only' or policy['isolationProfile'] == 'container-restricted':
                cmd.append('--read-only')
            for key, value in env.items():
                cmd.extend(['-e', f'{key}={value}'])
            container_command = container.get('command')
            if isinstance(container_command, list) and container_command:
                container_args = [str(part) for part in container_command if str(part)]
            else:
                container_args = [f'/plugin/{entrypoint.as_posix()}']
            cmd.extend([image, *container_args])
        else:
            raise ValueError(f'unsupported plugin runtime: {runtime}')

        try:
            proc = subprocess.run(
                cmd,
                input=payload_json,
                capture_output=True,
                text=True,
                cwd=str(install_root),
                env=env,
                timeout=policy['maxRuntimeSec'],
            )
        except subprocess.TimeoutExpired as ex:
            return {
                'ok': False,
                'exitCode': 124,
                'failureCategory': 'runtime',
                'failureSource': 'plugin-host',
                'failureReason': 'timeout',
                'result': {'status': 'FAILED', 'kind': 'plugin', 'timedOut': True, 'error': str(ex)},
                'stderr': truncate_text(str(ex), policy['maxOutputBytes']),
                'command': cmd,
                'scratchDir': str(scratch_dir),
                'executionPolicy': policy,
            }

        stdout = truncate_text(proc.stdout or '', policy['maxOutputBytes'])
        stderr = truncate_text(proc.stderr or '', policy['maxOutputBytes'])
        parsed = parse_payload(stdout)
        status = 'SUCCESS' if proc.returncode == 0 else 'FAILED'
        result = parsed if isinstance(parsed, dict) else {'stdout': stdout}
        out = {
            'ok': proc.returncode == 0,
            'exitCode': proc.returncode,
            'result': {
                'status': status,
                'kind': 'plugin',
                'pluginId': item.get('id'),
                'pluginActionId': action_id,
                'pluginResult': result,
            },
            'stderr': stderr,
            'command': cmd,
            'scratchDir': str(scratch_dir),
            'executionPolicy': policy,
        }
        if not out['ok']:
            out['failureCategory'] = 'runtime'
            out['failureSource'] = 'plugin-host'
            out['failureReason'] = 'execution_runtime_failed'
        return out


def handler_factory(engine: PluginHostEngine, token: str):
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
            if path == '/v1/plugin-host/health':
                return json_response(
                    self,
                    200,
                    {
                        'status': 'ok',
                        'service': 'plugin-host',
                        'checkedAt': now_iso(),
                        'containerRuntime': engine.resolve_container_runtime() or 'unavailable',
                        'containerRuntimePreference': engine.container_runtime_preference,
                    },
                )
            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def do_POST(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            body = parse_json(self)
            try:
                if path == '/v1/plugin-host/invoke':
                    action_id = str(body.get('actionId', '')).strip()
                    if not action_id:
                        return json_response(self, 400, err('VALIDATION_ERROR', 'actionId is required', req_id))
                    out = engine.invoke(action_id=action_id, invocation=body.get('invocation') or {})
                    return json_response(self, 200, out)
                return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
            except urllib.error.URLError as ex:
                return json_response(self, 502, err('DEPENDENCY_UNAVAILABLE', str(ex), req_id))
            except urllib.error.HTTPError as ex:
                body_text = ''
                try:
                    body_text = ex.read().decode('utf-8')
                except Exception:
                    body_text = ''
                return json_response(self, 502, err('DEPENDENCY_REQUEST_FAILED', f'HTTP {ex.code}: {body_text or ex.reason}', req_id))
            except ValueError as ex:
                return json_response(self, 400, err('VALIDATION_ERROR', str(ex), req_id))
            except Exception as ex:
                return json_response(self, 500, err('INTERNAL', str(ex), req_id))

        def log_message(self, fmt: str, *args):
            return

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description='Umbrella Plugin Host Service')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8785)
    ap.add_argument('--umbrella-root', default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument('--catalog-url', default='http://127.0.0.1:8786')
    ap.add_argument('--container-runtime', default='auto')
    ap.add_argument('--mesh-token', default='')
    ap.add_argument('--token', default='')
    args = ap.parse_args()

    root = Path(args.umbrella_root).resolve()
    engine = PluginHostEngine(
        umbrella_root=root,
        catalog_url=args.catalog_url,
        mesh_token=args.mesh_token,
        container_runtime=args.container_runtime,
    )
    handler = handler_factory(engine=engine, token=args.token.strip())
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({'status': 'listening', 'service': 'plugin-host', 'host': args.host, 'port': args.port}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
