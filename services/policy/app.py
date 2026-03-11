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

sys.path.append(str(Path(__file__).resolve().parents[2]))
from services.memory.auth import check_auth  # reuse optional bearer check


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


def write_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + '\n', encoding='utf-8')


def normalize_spaces(text: str) -> str:
    return ' '.join(str(text or '').split())


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


def run_cmd(cmd: list[str], cwd: Path) -> tuple[int, dict | None, str, str]:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    payload = parse_payload(proc.stdout)
    return proc.returncode, payload, proc.stdout, proc.stderr


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


class PolicyEngine:
    def __init__(self, umbrella_root: Path, parity_reconcile_cmd: str, multi_agent_policy_path: str):
        self.root = umbrella_root
        self.parity_reconcile_cmd = parity_reconcile_cmd
        self.policy_path = self.root / 'control-plane' / 'policy' / 'no-cutover-policy.json'
        self.multi_agent_policy_path = (self.root / multi_agent_policy_path).resolve()
        self.drift_lint = self.root / 'scripts' / 'control-plane' / 'drift-lint'
        self.parity_gate = self.root / 'scripts' / 'control-plane' / 'capability-parity-gate'
        self.multi_agent_policy_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.multi_agent_policy_path.exists():
            write_json(
                self.multi_agent_policy_path,
                {
                    'id': 'umbrella.policy.multi-agent.v1',
                    'requiredRegistrationForPrivilegedActions': True,
                    'privilegedActions': ['memoryWrite', 'memoryDelete', 'memoryList'],
                    'toolCapabilityClaims': {
                        'memoryWrite': 'memory.write',
                        'memoryRead': 'memory.read',
                        'memoryDelete': 'memory.delete',
                        'memoryList': 'memory.read',
                    },
                    'agents': {},
                },
            )

    def load_multi_agent_policy(self) -> dict:
        return load_json(
            self.multi_agent_policy_path,
            {
                'id': 'umbrella.policy.multi-agent.v1',
                'requiredRegistrationForPrivilegedActions': True,
                'privilegedActions': ['memoryWrite', 'memoryDelete', 'memoryList'],
                'toolCapabilityClaims': {},
                'agents': {},
            },
        )

    def register_agent(self, agent_id: str, capabilities: list[str], source: str = 'external') -> dict:
        pol = self.load_multi_agent_policy()
        agents = pol.get('agents') if isinstance(pol.get('agents'), dict) else {}
        agents[agent_id] = {
            'agentId': agent_id,
            'registered': True,
            'source': source or 'external',
            'capabilities': sorted({str(c).strip() for c in capabilities if str(c).strip()}),
            'updatedAt': now_iso(),
        }
        pol['agents'] = agents
        write_json(self.multi_agent_policy_path, pol)
        return {'ok': True, 'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'), 'agent': agents[agent_id]}

    def authorize_step(self, step_spec: dict) -> dict:
        pol = self.load_multi_agent_policy()
        action = str(step_spec.get('action', '')).strip()
        agent_id = str(step_spec.get('agentId') or (step_spec.get('metadata') or {}).get('agentId', '')).strip()
        privileged = set(str(x).strip() for x in (pol.get('privilegedActions') or []))
        claims = pol.get('toolCapabilityClaims') if isinstance(pol.get('toolCapabilityClaims'), dict) else {}
        agents = pol.get('agents') if isinstance(pol.get('agents'), dict) else {}
        required_registration = bool(pol.get('requiredRegistrationForPrivilegedActions', True))
        required_capability = str(claims.get(action, '')).strip()

        if action in privileged and required_registration:
            if not agent_id:
                return {
                    'ok': False,
                    'allowed': False,
                    'reason': 'external_agent_registration_required',
                    'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                    'action': action,
                    'agentId': '',
                }
            row = agents.get(agent_id) if isinstance(agents.get(agent_id), dict) else None
            if not row or not bool(row.get('registered', False)):
                return {
                    'ok': False,
                    'allowed': False,
                    'reason': 'external_agent_registration_required',
                    'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                    'action': action,
                    'agentId': agent_id,
                }
            if required_capability:
                caps = set(str(c).strip() for c in (row.get('capabilities') or []))
                if required_capability not in caps:
                    return {
                        'ok': False,
                        'allowed': False,
                        'reason': 'tool_capability_claim_missing',
                        'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                        'action': action,
                        'agentId': agent_id,
                        'requiredCapability': required_capability,
                    }

        return {
            'ok': True,
            'allowed': True,
            'reason': 'policy_allow',
            'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
            'action': action,
            'agentId': agent_id,
            'requiredCapability': required_capability,
        }

    def check_command(self, command: str) -> dict:
        policy = load_json(self.policy_path, {})
        forbidden = [normalize_spaces(x) for x in (policy.get('forbiddenCommandPatterns') or [])]
        cmd_norm = normalize_spaces(command)
        blocked_by = [p for p in forbidden if p and p in cmd_norm]
        blocked = len(blocked_by) > 0
        return {
            'checkedAt': now_iso(),
            'policyId': policy.get('id', 'umbrella.policy.no-cutover.v1'),
            'command': command,
            'blocked': blocked,
            'decision': 'BLOCKED' if blocked else 'ALLOW',
            'reason': 'policy_blocked' if blocked else 'policy_allow',
            'matchedPatterns': blocked_by,
        }

    def preflight_drift(self, exceptions_path: str = '') -> dict:
        cmd = [str(self.drift_lint), '--umbrella-root', str(self.root)]
        if exceptions_path.strip():
            cmd.extend(['--exceptions', exceptions_path.strip()])
        rc, payload, out, err = run_cmd(cmd, self.root)
        result = payload if isinstance(payload, dict) else {'stdout': out[-4000:], 'stderr': err[-4000:]}
        return {
            'check': 'drift_lint',
            'ok': rc == 0,
            'exitCode': rc,
            'result': result,
        }

    def preflight_parity(self, reconcile_cmd: str = '') -> dict:
        cmd = [
            str(self.parity_gate),
            '--umbrella-root',
            str(self.root),
            '--reconcile-cmd',
            reconcile_cmd.strip() or self.parity_reconcile_cmd,
        ]
        rc, payload, out, err = run_cmd(cmd, self.root)
        result = payload if isinstance(payload, dict) else {'stdout': out[-4000:], 'stderr': err[-4000:]}
        ok = rc == 0
        return {
            'check': 'capability_parity',
            'ok': ok,
            'exitCode': rc,
            'result': result,
        }

    def preflight_all(self, command: str = '', exceptions_path: str = '', reconcile_cmd: str = '') -> dict:
        checks = []
        if command.strip():
            cc = self.check_command(command)
            checks.append({'check': 'command_policy', 'ok': not cc['blocked'], 'result': cc})
        drift = self.preflight_drift(exceptions_path=exceptions_path)
        checks.append(drift)
        parity = self.preflight_parity(reconcile_cmd=reconcile_cmd)
        checks.append(parity)

        ok = all(bool(c.get('ok')) for c in checks)
        return {
            'checkedAt': now_iso(),
            'ok': ok,
            'status': 'PASS' if ok else 'BLOCKED',
            'checks': checks,
        }


def handler_factory(engine: PolicyEngine, token: str):
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
            if path == '/v1/policy/health':
                return json_response(self, 200, {'status': 'ok', 'service': 'policy', 'checkedAt': now_iso()})
            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def do_POST(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            body = parse_json(self)

            try:
                if path == '/v1/policy/check-command':
                    command = str(body.get('command', ''))
                    out = engine.check_command(command)
                    return json_response(self, 200, out)
                if path == '/v1/policy/preflight/drift-lint':
                    out = engine.preflight_drift(exceptions_path=str(body.get('exceptionsPath', '')))
                    return json_response(self, 200, out)
                if path == '/v1/policy/preflight/capability-parity':
                    out = engine.preflight_parity(reconcile_cmd=str(body.get('reconcileCmd', '')))
                    return json_response(self, 200, out)
                if path == '/v1/policy/preflight/all':
                    out = engine.preflight_all(
                        command=str(body.get('command', '')),
                        exceptions_path=str(body.get('exceptionsPath', '')),
                        reconcile_cmd=str(body.get('reconcileCmd', '')),
                    )
                    return json_response(self, 200, out)
                if path == '/v1/policy/agents/register':
                    agent_id = str(body.get('agentId', '')).strip()
                    if not agent_id:
                        return json_response(self, 400, err('VALIDATION_ERROR', 'agentId is required', req_id))
                    capabilities = body.get('capabilities') if isinstance(body.get('capabilities'), list) else []
                    out = engine.register_agent(
                        agent_id=agent_id,
                        capabilities=[str(c) for c in capabilities],
                        source=str(body.get('source', 'external')),
                    )
                    return json_response(self, 200, out)
                if path == '/v1/policy/authorize-step':
                    step_spec = body.get('stepSpec') if isinstance(body.get('stepSpec'), dict) else {}
                    out = engine.authorize_step(step_spec=step_spec)
                    return json_response(self, 200, out)
                return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
            except Exception as ex:
                return json_response(self, 500, err('INTERNAL', str(ex), req_id))

        def log_message(self, fmt: str, *args):
            return

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description='Umbrella Policy Service')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8791)
    ap.add_argument('--umbrella-root', default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument('--parity-reconcile-cmd', default='memory-core-reconcile')
    ap.add_argument('--multi-agent-policy', default='control-plane/policy/multi-agent-policy.json')
    ap.add_argument('--token', default='')
    args = ap.parse_args()

    root = Path(args.umbrella_root).resolve()
    token = args.token.strip()
    engine = PolicyEngine(
        umbrella_root=root,
        parity_reconcile_cmd=args.parity_reconcile_cmd,
        multi_agent_policy_path=args.multi_agent_policy,
    )
    handler = handler_factory(engine, token)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({'status': 'listening', 'service': 'policy', 'host': args.host, 'port': args.port}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
