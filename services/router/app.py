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
from services.runtime_contract import RuntimeContract


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
    def __init__(self, routing_path: Path, capability_path: Path, catalog_url: str = '', mesh_token: str = ''):
        self.routing_path = routing_path
        self.config = load_json(routing_path, {})
        self.capability_path = capability_path
        self.runtime_contract = RuntimeContract(capability_path)
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
            'capabilityPath': str(self.capability_path),
            'catalogUrl': self.catalog_url,
        }

    def runtime_capabilities_payload(self) -> dict:
        return {
            'loadedAt': now_iso(),
            'path': str(self.capability_path),
            **self.runtime_contract.payload(),
        }

    def _requested_runtime(self, step: dict) -> str:
        runtime_requested = str(step.get('runtime') or '').strip()
        metadata = step.get('metadata') if isinstance(step.get('metadata'), dict) else {}
        if not runtime_requested:
            runtime_requested = str(metadata.get('runtimeRequested') or '').strip()
        return runtime_requested

    def _route_payload(
        self,
        *,
        step_id: str,
        action: str,
        runtime: str,
        reason: str,
        runtime_requested: str = '',
        catalog_action: dict | None = None,
        resolved_action_id: str = '',
        deprecated_action_id: str = '',
        action_family: str = '',
        runtime_capability: str = '',
        runtime_supported: bool = True,
        supported_runtimes: list[str] | None = None,
    ) -> dict:
        payload = {
            'routed': True,
            'runtime': runtime,
            'runtimeClass': runtime,
            'runtimeResolved': runtime,
            'runtimeRequested': runtime_requested,
            'runtimeReason': reason,
            'reason': reason,
            'stepId': step_id,
            'action': action,
            'resolvedActionId': resolved_action_id or action,
            'actionFamily': action_family,
            'runtimeCapability': runtime_capability,
            'runtimeSupported': runtime_supported,
            'supportedRuntimes': supported_runtimes or [],
        }
        if deprecated_action_id:
            payload['deprecatedActionId'] = deprecated_action_id
        if isinstance(catalog_action, dict):
            payload['catalogAction'] = catalog_action
            payload['executorRuntime'] = 'plugin-host'
        return payload

    def _match_rule_runtime(self, step_id: str, action: str) -> tuple[str, str]:
        rules = self.config.get('rules') or []
        for r in rules:
            match_action = str(r.get('matchAction', '')).strip()
            if match_action and action == match_action:
                return str(r.get('runtime', self.config.get('defaultRuntime', ''))), f'matched_action:{match_action}'
            prefix = str(r.get('matchStepPrefix', '')).strip()
            if prefix and (step_id.startswith(prefix) or action.startswith(prefix)):
                return str(r.get('runtime', self.config.get('defaultRuntime', ''))), f'matched_prefix:{prefix}'
        return '', ''

    def _fallback_runtimes(self, requested_runtime: str) -> list[str]:
        reroute = self.config.get('capabilityReroute') or {}
        if not bool(reroute.get('enabled', False)):
            return []
        return [str(x).strip() for x in ((reroute.get('fallbackByRuntime') or {}).get(requested_runtime, [])) if str(x).strip()]

    def route_step(self, step: dict) -> dict:
        step_id = str(step.get('stepId') or step.get('id') or '').strip()
        action_info = self.runtime_contract.resolve_action(str(step.get('action', '')).strip())
        original_action = action_info['originalActionId']
        resolved_action = action_info['resolvedActionId']
        runtime_requested = self._requested_runtime(step)
        supported_runtimes = list(action_info.get('supportedRuntimes') or [])
        action_family = str(action_info.get('actionFamily') or '').strip()
        runtime_capability = str(action_info.get('runtimeCapability') or '').strip()

        catalog_action = self._catalog_action(resolved_action)
        candidate_runtime = ''
        candidate_reason = ''
        candidate_catalog_action = None

        if isinstance(catalog_action, dict):
            candidate_runtime = 'umbrella-agent-runtime'
            candidate_reason = 'catalog_action'
            candidate_catalog_action = catalog_action
            if 'umbrella-agent-runtime' not in supported_runtimes:
                supported_runtimes = ['umbrella-agent-runtime', *supported_runtimes]
            action_family = action_family or 'skill.*'
            runtime_capability = runtime_capability or 'catalog.skill.invoke'
        else:
            candidate_runtime, candidate_reason = self._match_rule_runtime(step_id=step_id, action=original_action)
            if candidate_runtime and supported_runtimes and candidate_runtime not in supported_runtimes:
                candidate_runtime = action_info.get('preferredRuntime') or supported_runtimes[0]
                candidate_reason = f'rule_runtime_unsupported:{candidate_reason}'
            if not candidate_runtime and supported_runtimes:
                candidate_runtime = action_info.get('preferredRuntime') or supported_runtimes[0]
                candidate_reason = f'capability_family:{action_family or "unknown"}'
            if not candidate_runtime:
                candidate_runtime = str(self.config.get('defaultRuntime', ''))
                candidate_reason = 'default_runtime'

        runtime_supported = True
        if runtime_requested:
            resolved_runtime, runtime_supported, requested_reason = self.runtime_contract.resolve_compatible_runtime(
                resolved_action,
                runtime_requested,
                self._fallback_runtimes(runtime_requested),
            )
            if resolved_runtime:
                candidate_runtime = resolved_runtime
                candidate_reason = requested_reason
                candidate_catalog_action = catalog_action if resolved_runtime == 'umbrella-agent-runtime' else None

        return self._route_payload(
            step_id=step_id,
            action=original_action,
            runtime=candidate_runtime,
            reason=candidate_reason,
            runtime_requested=runtime_requested,
            catalog_action=candidate_catalog_action,
            resolved_action_id=resolved_action,
            deprecated_action_id=action_info.get('deprecatedActionId', ''),
            action_family=action_family,
            runtime_capability=runtime_capability,
            runtime_supported=runtime_supported,
            supported_runtimes=supported_runtimes,
        )

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
            if path == '/v1/router/runtime-capabilities':
                return json_response(self, 200, engine.runtime_capabilities_payload())
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
                from_runtime = str(body.get('fromRuntime', ''))
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
    ap.add_argument('--runtime-capabilities', default='control-plane/router/runtime-capabilities.json')
    ap.add_argument('--catalog-url', default='')
    ap.add_argument('--mesh-token', default='')
    ap.add_argument('--token', default='')
    args = ap.parse_args()

    root = Path(args.umbrella_root).resolve()
    engine = RouterEngine(
        routing_path=(root / args.routing),
        capability_path=(root / args.runtime_capabilities),
        catalog_url=args.catalog_url,
        mesh_token=args.mesh_token,
    )
    handler = handler_factory(engine=engine, token=args.token.strip())
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({'status': 'listening', 'service': 'router', 'host': args.host, 'port': args.port}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
