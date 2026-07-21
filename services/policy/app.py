#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request
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


DEFAULT_MULTI_AGENT_POLICY = {
    'id': 'umbrella.policy.multi-agent.v1',
    'requiredRegistrationForPrivilegedActions': True,
    'privilegedActions': [
        'memoryWrite',
        'memoryDelete',
        'memoryList',
        'memory.put',
        'memory.link',
        'memory.import',
        'memory.promote',
        'memory.hydrate',
    ],
    'toolCapabilityClaims': {
        # Operational memory plane (short-term, hot path).
        'memoryWrite': 'memorycore.write',
        'memoryRead': 'memorycore.read',
        'memoryDelete': 'memorycore.delete',
        'memoryList': 'memorycore.read',
        # Knowledge memory plane (durable node/edge graph).
        'memory.get': 'knowledge.read',
        'memory.put': 'knowledge.write',
        'memory.search': 'knowledge.read',
        'memory.link': 'knowledge.write',
        'memory.import': 'knowledge.write',
        # Cross-layer operations must be explicit and privileged.
        'memory.promote': 'knowledge.promote',
        'memory.hydrate': 'knowledge.backfill',
    },
    # Backward compatibility for existing agents/tests that still use v0 claims.
    'toolCapabilityClaimAlternates': {
        'memoryWrite': ['memory.write'],
        'memoryRead': ['memory.read'],
        'memoryDelete': ['memory.delete'],
        'memoryList': ['memory.read'],
        'memory.put': ['memory.write'],
        'memory.get': ['memory.read'],
        'memory.search': ['memory.read'],
        'memory.link': ['memory.write'],
    },
    'actionPolicyDefaults': {
        'builtin': {
            'riskClass': 'moderate',
            'approvalMode': 'conditional',
            'memoryAccess': 'none',
            'networkAccess': 'none',
            'fsAccess': 'none',
            'processAccess': 'restricted-subprocess',
            'identityScope': {'agentIds': [], 'roles': [], 'shopIds': []},
            'delegationAllowed': True,
            'subAgentAllowed': True,
        },
        'skill': {
            'riskClass': 'low',
            'approvalMode': 'none',
            'memoryAccess': 'session',
            'networkAccess': 'none',
            'fsAccess': 'scratch-only',
            'processAccess': 'restricted-subprocess',
            'identityScope': {'agentIds': [], 'roles': [], 'shopIds': []},
            'delegationAllowed': True,
            'subAgentAllowed': True,
        },
        'plugin': {
            'riskClass': 'moderate',
            'approvalMode': 'conditional',
            'memoryAccess': 'none',
            'networkAccess': 'none',
            'fsAccess': 'scratch-only',
            'processAccess': 'restricted-subprocess',
            'identityScope': {'agentIds': [], 'roles': [], 'shopIds': []},
            'delegationAllowed': True,
            'subAgentAllowed': False,
        },
    },
    'actionPolicyOverrides': {
        'skill.memory.link': {
            'riskClass': 'high',
            'approvalMode': 'required',
            'memoryAccess': 'durable-memory',
            'identityScope': {'shopIds': ['town-hall']},
            'delegationAllowed': False,
            'subAgentAllowed': False,
        },
        'memory.promote': {
            'riskClass': 'high',
            'approvalMode': 'conditional',
            'memoryAccess': 'durable-memory',
        },
        'memory.hydrate': {
            'riskClass': 'high',
            'approvalMode': 'conditional',
            'memoryAccess': 'durable-memory',
        },
    },
    'agents': {},
}


class PolicyEngine:
    def __init__(
        self,
        umbrella_root: Path,
        parity_reconcile_cmd: str,
        multi_agent_policy_path: str,
        agent_registry_path: str,
        catalog_url: str = '',
        mesh_token: str = '',
    ):
        self.root = umbrella_root
        self.parity_reconcile_cmd = parity_reconcile_cmd.strip() or str(self.root / 'scripts' / 'tools' / 'memory-core-reconcile')
        self.multi_agent_policy_path = (self.root / multi_agent_policy_path).resolve()
        self.agent_registry_path = (self.root / agent_registry_path).resolve()
        self.catalog_url = catalog_url.rstrip('/')
        self.mesh_token = mesh_token.strip()
        self.drift_lint = self.root / 'scripts' / 'control-plane' / 'drift-lint'
        self.parity_gate = self.root / 'scripts' / 'control-plane' / 'capability-parity-gate'
        self.multi_agent_policy_path.parent.mkdir(parents=True, exist_ok=True)
        self.agent_registry_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.multi_agent_policy_path.exists():
            write_json(self.multi_agent_policy_path, DEFAULT_MULTI_AGENT_POLICY)
        if not self.agent_registry_path.exists():
            write_json(self.agent_registry_path, {'agents': {}})

    def _catalog_headers(self) -> dict:
        if self.mesh_token:
            return {'Authorization': f'Bearer {self.mesh_token}'}
        return {}

    def _catalog_action(self, action_id: str, timeout: int = 15) -> dict | None:
        if not self.catalog_url:
            return None
        req = urllib.request.Request(
            f'{self.catalog_url}/v1/catalog/actions/{urllib.parse.quote(action_id, safe="")}',
            method='GET',
            headers=self._catalog_headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                return None
            raise

    def _catalog_item(self, item_id: str, timeout: int = 15) -> dict | None:
        if not self.catalog_url:
            return None
        req = urllib.request.Request(
            f'{self.catalog_url}/v1/catalog/items/{urllib.parse.quote(item_id, safe="")}',
            method='GET',
            headers=self._catalog_headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                return None
            raise

    def load_multi_agent_policy_seed(self) -> dict:
        pol = load_json(self.multi_agent_policy_path, DEFAULT_MULTI_AGENT_POLICY)
        if not isinstance(pol, dict):
            pol = dict(DEFAULT_MULTI_AGENT_POLICY)
        # Best-effort migration for older policy files.
        if not isinstance(pol.get('toolCapabilityClaims'), dict):
            pol['toolCapabilityClaims'] = dict(DEFAULT_MULTI_AGENT_POLICY['toolCapabilityClaims'])
        if not isinstance(pol.get('toolCapabilityClaimAlternates'), dict):
            pol['toolCapabilityClaimAlternates'] = dict(DEFAULT_MULTI_AGENT_POLICY['toolCapabilityClaimAlternates'])
        if not isinstance(pol.get('privilegedActions'), list):
            pol['privilegedActions'] = list(DEFAULT_MULTI_AGENT_POLICY['privilegedActions'])
        if not isinstance(pol.get('actionPolicyDefaults'), dict):
            pol['actionPolicyDefaults'] = dict(DEFAULT_MULTI_AGENT_POLICY['actionPolicyDefaults'])
        if not isinstance(pol.get('actionPolicyOverrides'), dict):
            pol['actionPolicyOverrides'] = dict(DEFAULT_MULTI_AGENT_POLICY['actionPolicyOverrides'])
        return pol

    def load_agent_registry(self) -> dict:
        reg = load_json(self.agent_registry_path, {'agents': {}})
        if not isinstance(reg, dict):
            reg = {'agents': {}}
        if not isinstance(reg.get('agents'), dict):
            reg['agents'] = {}
        return reg

    def load_multi_agent_policy(self) -> dict:
        pol = self.load_multi_agent_policy_seed()
        seed_agents = pol.get('agents') if isinstance(pol.get('agents'), dict) else {}
        reg = self.load_agent_registry()
        agents = dict(seed_agents)
        agents.update(reg.get('agents') or {})
        pol['agents'] = agents
        return pol

    def register_agent(self, agent_id: str, capabilities: list[str], source: str = 'external') -> dict:
        pol = self.load_multi_agent_policy_seed()
        reg = self.load_agent_registry()
        agents = reg.get('agents') if isinstance(reg.get('agents'), dict) else {}
        agents[agent_id] = {
            'agentId': agent_id,
            'registered': True,
            'source': source or 'external',
            'capabilities': sorted({str(c).strip() for c in capabilities if str(c).strip()}),
            'updatedAt': now_iso(),
        }
        reg['agents'] = agents
        write_json(self.agent_registry_path, reg)
        return {'ok': True, 'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'), 'agent': agents[agent_id]}

    def authorize_step(self, step_spec: dict) -> dict:
        pol = self.load_multi_agent_policy()
        action = str(step_spec.get('action', '')).strip()
        metadata = step_spec.get('metadata') if isinstance(step_spec.get('metadata'), dict) else {}
        boundary_context = metadata.get('boundaryContext') if isinstance(metadata.get('boundaryContext'), dict) else {}
        phase = str(boundary_context.get('phase') or metadata.get('phase') or step_spec.get('phase') or '').strip().lower()
        agent_id = str(step_spec.get('agentId') or metadata.get('agentId', '')).strip()
        privileged = set(str(x).strip() for x in (pol.get('privilegedActions') or []))
        claims = pol.get('toolCapabilityClaims') if isinstance(pol.get('toolCapabilityClaims'), dict) else {}
        alternates = pol.get('toolCapabilityClaimAlternates') if isinstance(pol.get('toolCapabilityClaimAlternates'), dict) else {}
        agents = pol.get('agents') if isinstance(pol.get('agents'), dict) else {}
        action_policy_defaults = pol.get('actionPolicyDefaults') if isinstance(pol.get('actionPolicyDefaults'), dict) else {}
        action_policy_overrides = pol.get('actionPolicyOverrides') if isinstance(pol.get('actionPolicyOverrides'), dict) else {}
        required_registration = bool(pol.get('requiredRegistrationForPrivilegedActions', True))
        required_capability = str(claims.get(action, '')).strip()
        acceptable_capabilities: set[str] = {required_capability} if required_capability else set()
        catalog_action = self._catalog_action(action)
        catalog_item = None
        if isinstance(catalog_action, dict):
            plugin_id = str(catalog_action.get('pluginId', '')).strip()
            if plugin_id:
                catalog_item = self._catalog_item(plugin_id)
        if isinstance(catalog_action, dict):
            acceptable_capabilities.update(
                str(c).strip() for c in (catalog_action.get('requiredCapabilities') or []) if str(c).strip()
            )
            if not required_capability and acceptable_capabilities:
                required_capability = sorted(acceptable_capabilities)[0]
        operational_actions = {'memoryWrite', 'memoryRead', 'memoryDelete', 'memoryList'}
        knowledge_actions = {'memory.get', 'memory.put', 'memory.search', 'memory.link', 'memory.import'}
        cross_layer_actions = {'memory.promote', 'memory.hydrate'}
        active_phases = {'active-run', 'running', 'runtime'}

        row = agents.get(agent_id) if isinstance(agents.get(agent_id), dict) else None
        action_class = 'builtin'
        if isinstance(catalog_item, dict):
            item_kind = str(catalog_item.get('kind', '')).strip()
            action_class = item_kind if item_kind in {'skill', 'plugin'} else 'plugin'
        action_policy = dict(action_policy_defaults.get(action_class) if isinstance(action_policy_defaults.get(action_class), dict) else {})
        policy_hints = catalog_action.get('policyHints') if isinstance(catalog_action, dict) and isinstance(catalog_action.get('policyHints'), dict) else {}
        if 'requiresApproval' in policy_hints and 'approvalMode' not in policy_hints:
            policy_hints = dict(policy_hints)
            policy_hints['approvalMode'] = 'required' if bool(policy_hints.get('requiresApproval')) else 'none'
        override_policy = action_policy_overrides.get(action) if isinstance(action_policy_overrides.get(action), dict) else {}
        action_policy.update({k: v for k, v in policy_hints.items() if v not in (None, '')})
        action_policy.update({k: v for k, v in override_policy.items() if v not in (None, '')})
        identity_scope = action_policy.get('identityScope') if isinstance(action_policy.get('identityScope'), dict) else {}
        effective_action_policy = {
            'actionClass': action_class,
            'riskClass': str(action_policy.get('riskClass', 'moderate')).strip() or 'moderate',
            'approvalMode': str(action_policy.get('approvalMode', 'conditional')).strip() or 'conditional',
            'memoryAccess': str(action_policy.get('memoryAccess', 'none')).strip() or 'none',
            'networkAccess': str(action_policy.get('networkAccess', 'none')).strip() or 'none',
            'fsAccess': str(action_policy.get('fsAccess', 'none')).strip() or 'none',
            'processAccess': str(action_policy.get('processAccess', 'restricted-subprocess')).strip() or 'restricted-subprocess',
            'identityScope': {
                'agentIds': [str(x).strip() for x in (identity_scope.get('agentIds') or []) if str(x).strip()],
                'roles': [str(x).strip() for x in (identity_scope.get('roles') or []) if str(x).strip()],
                'shopIds': [str(x).strip() for x in (identity_scope.get('shopIds') or []) if str(x).strip()],
            },
            'delegationAllowed': bool(action_policy.get('delegationAllowed', True)),
            'subAgentAllowed': bool(action_policy.get('subAgentAllowed', True)),
        }
        approval_context = metadata.get('approvalContext') if isinstance(metadata.get('approvalContext'), dict) else {}
        policy_context = metadata.get('policyContext') if isinstance(metadata.get('policyContext'), dict) else {}
        role = str(metadata.get('role', '')).strip()
        shop_id = str(metadata.get('shopId', '')).strip()
        delegated_by = str(metadata.get('delegatedByAgentId', '')).strip()
        sub_agent_id = str(metadata.get('subAgentId', '')).strip()

        # Hard-fail boundary protections: active run hot path must not invoke direct knowledge actions.
        if action in knowledge_actions and phase in active_phases:
            return {
                'ok': False,
                'allowed': False,
                'reason': 'boundary_hot_path_forbidden',
                'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                'action': action,
                'agentId': agent_id,
                'boundary': {
                    'phase': phase,
                    'allowedOperationalActions': sorted(operational_actions),
                    'allowedCrossLayer': ['memory.promote (async only)', 'memory.hydrate (bootstrap/resume only)'],
                },
                'effectiveActionPolicy': effective_action_policy,
            }

        if action == 'memory.hydrate' and phase not in {'bootstrap', 'resume'}:
            return {
                'ok': False,
                'allowed': False,
                'reason': 'hydration_phase_forbidden',
                'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                'action': action,
                'agentId': agent_id,
                'requiredPhase': ['bootstrap', 'resume'],
                'phase': phase or 'unknown',
                'effectiveActionPolicy': effective_action_policy,
            }

        if action == 'memory.promote' and phase in active_phases and not bool(metadata.get('async', False)):
            return {
                'ok': False,
                'allowed': False,
                'reason': 'cross_layer_hot_path_forbidden',
                'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                'action': action,
                'agentId': agent_id,
                'requirement': 'metadata.async=true for active-run promotion',
                'effectiveActionPolicy': effective_action_policy,
            }

        if action in privileged and required_registration:
            if not agent_id:
                return {
                    'ok': False,
                    'allowed': False,
                    'reason': 'external_agent_registration_required',
                    'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                    'action': action,
                    'agentId': '',
                    'effectiveActionPolicy': effective_action_policy,
                }
            if not row or not bool(row.get('registered', False)):
                return {
                    'ok': False,
                    'allowed': False,
                    'reason': 'external_agent_registration_required',
                    'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                    'action': action,
                    'agentId': agent_id,
                    'effectiveActionPolicy': effective_action_policy,
                }

        if required_capability or acceptable_capabilities:
            if not agent_id:
                return {
                    'ok': False,
                    'allowed': False,
                    'reason': 'agent_identity_required',
                    'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                    'action': action,
                    'agentId': '',
                    'requiredCapability': required_capability,
                    'effectiveActionPolicy': effective_action_policy,
                }
            if not row:
                return {
                    'ok': False,
                    'allowed': False,
                    'reason': 'external_agent_registration_required',
                    'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                    'action': action,
                    'agentId': agent_id,
                    'effectiveActionPolicy': effective_action_policy,
                }
            caps = set(str(c).strip() for c in (row.get('capabilities') or []))
            candidate_caps = set(acceptable_capabilities)
            if required_capability:
                candidate_caps.add(required_capability)
            alt_caps = alternates.get(action)
            if isinstance(alt_caps, list):
                candidate_caps.update(str(c).strip() for c in alt_caps if str(c).strip())
            if not candidate_caps.intersection(caps):
                return {
                    'ok': False,
                    'allowed': False,
                    'reason': 'tool_capability_claim_missing',
                    'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                    'action': action,
                    'agentId': agent_id,
                    'requiredCapability': required_capability,
                    'acceptableCapabilities': sorted(candidate_caps),
                    'effectiveActionPolicy': effective_action_policy,
                }

        allowed_agent_ids = set(effective_action_policy['identityScope']['agentIds'])
        allowed_roles = set(effective_action_policy['identityScope']['roles'])
        allowed_shop_ids = set(effective_action_policy['identityScope']['shopIds'])
        if allowed_agent_ids and agent_id not in allowed_agent_ids:
            return {
                'ok': False,
                'allowed': False,
                'reason': 'action_identity_scope_denied',
                'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                'action': action,
                'agentId': agent_id,
                'effectiveActionPolicy': effective_action_policy,
            }
        if allowed_roles and role not in allowed_roles:
            return {
                'ok': False,
                'allowed': False,
                'reason': 'action_identity_scope_denied',
                'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                'action': action,
                'agentId': agent_id,
                'effectiveActionPolicy': effective_action_policy,
            }
        if allowed_shop_ids and shop_id not in allowed_shop_ids:
            return {
                'ok': False,
                'allowed': False,
                'reason': 'action_identity_scope_denied',
                'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                'action': action,
                'agentId': agent_id,
                'effectiveActionPolicy': effective_action_policy,
            }
        if delegated_by and delegated_by != agent_id and not effective_action_policy['delegationAllowed']:
            return {
                'ok': False,
                'allowed': False,
                'reason': 'action_delegation_forbidden',
                'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                'action': action,
                'agentId': agent_id,
                'effectiveActionPolicy': effective_action_policy,
            }
        if sub_agent_id and not effective_action_policy['subAgentAllowed']:
            return {
                'ok': False,
                'allowed': False,
                'reason': 'action_subagent_forbidden',
                'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                'action': action,
                'agentId': agent_id,
                'effectiveActionPolicy': effective_action_policy,
            }
        if effective_action_policy['approvalMode'] == 'required' and not bool(approval_context.get('approved', False)):
            return {
                'ok': False,
                'allowed': False,
                'reason': 'action_approval_required',
                'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                'action': action,
                'agentId': agent_id,
                'effectiveActionPolicy': effective_action_policy,
            }
        if effective_action_policy['fsAccess'] == 'workspace' and not bool(policy_context.get('workspaceFsAllowed', False)):
            return {
                'ok': False,
                'allowed': False,
                'reason': 'action_fs_scope_denied',
                'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                'action': action,
                'agentId': agent_id,
                'effectiveActionPolicy': effective_action_policy,
            }
        if effective_action_policy['networkAccess'] == 'open' and not bool(policy_context.get('networkAccessAllowed', False)):
            return {
                'ok': False,
                'allowed': False,
                'reason': 'action_network_scope_denied',
                'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
                'action': action,
                'agentId': agent_id,
                'effectiveActionPolicy': effective_action_policy,
            }

        return {
            'ok': True,
            'allowed': True,
            'reason': 'policy_allow',
            'policyId': pol.get('id', 'umbrella.policy.multi-agent.v1'),
            'action': action,
            'agentId': agent_id,
            'requiredCapability': required_capability,
            'catalogAction': catalog_action or {},
            'effectiveActionPolicy': effective_action_policy,
        }

    def preflight_drift(self, skip: bool = False) -> dict:
        if skip:
            return {
                'check': 'drift_lint',
                'ok': True,
                'skipped': True,
                'result': {'status': 'SKIPPED', 'reason': 'skip_requested'},
            }
        cmd = [str(self.drift_lint), '--umbrella-root', str(self.root)]
        rc, payload, out, err = run_cmd(cmd, self.root)
        result = payload if isinstance(payload, dict) else {'stdout': out[-4000:], 'stderr': err[-4000:]}
        return {
            'check': 'drift_lint',
            'ok': rc == 0,
            'exitCode': rc,
            'result': result,
        }

    def preflight_parity(self, reconcile_cmd: str = '', skip: bool = False) -> dict:
        if skip:
            return {
                'check': 'capability_parity',
                'ok': True,
                'skipped': True,
                'result': {'status': 'SKIPPED', 'reason': 'skip_requested'},
            }
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

    def preflight_all(self, reconcile_cmd: str = '', skip_drift_lint: bool = False, skip_capability_parity: bool = False) -> dict:
        checks = []
        drift = self.preflight_drift(skip=skip_drift_lint)
        checks.append(drift)
        parity = self.preflight_parity(reconcile_cmd=reconcile_cmd, skip=skip_capability_parity)
        checks.append(parity)

        ok = all(bool(c.get('ok')) for c in checks)
        skipped = [str(c.get('check')) for c in checks if bool(c.get('skipped'))]
        out = {
            'checkedAt': now_iso(),
            'ok': ok,
            'status': 'PASS' if ok else 'BLOCKED',
            'checks': checks,
        }
        if skipped:
            out['skippedChecks'] = skipped
        return out


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
                if path == '/v1/policy/preflight/drift-lint':
                    out = engine.preflight_drift(skip=bool(body.get('skipDriftLint', False)))
                    return json_response(self, 200, out)
                if path == '/v1/policy/preflight/capability-parity':
                    out = engine.preflight_parity(
                        reconcile_cmd=str(body.get('reconcileCmd', '')),
                        skip=bool(body.get('skipCapabilityParity', False)),
                    )
                    return json_response(self, 200, out)
                if path == '/v1/policy/preflight/all':
                    out = engine.preflight_all(
                        reconcile_cmd=str(body.get('reconcileCmd', '')),
                        skip_drift_lint=bool(body.get('skipDriftLint', False)),
                        skip_capability_parity=bool(body.get('skipCapabilityParity', False)),
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
    ap.add_argument('--parity-reconcile-cmd', default='')
    ap.add_argument('--multi-agent-policy', default='control-plane/policy/multi-agent-policy.json')
    ap.add_argument('--agent-registry', default='control-plane/observability/policy/agent-registry.json')
    ap.add_argument('--catalog-url', default='')
    ap.add_argument('--token', default='')
    args = ap.parse_args()

    root = Path(args.umbrella_root).resolve()
    token = args.token.strip()
    engine = PolicyEngine(
        umbrella_root=root,
        parity_reconcile_cmd=args.parity_reconcile_cmd,
        multi_agent_policy_path=args.multi_agent_policy,
        agent_registry_path=args.agent_registry,
        catalog_url=args.catalog_url,
        mesh_token=token,
    )
    handler = handler_factory(engine, token)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({'status': 'listening', 'service': 'policy', 'host': args.host, 'port': args.port}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
