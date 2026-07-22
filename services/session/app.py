#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import threading
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.append(str(Path(__file__).resolve().parents[2]))
from services.environment import survey_environment
from services.id_utils import validate_identifier
from services.memory.auth import check_auth
from services.persistence import atomic_write_json, file_lock, update_json
from services.runtime_model import call_model_broker, discover_broker_url, load_model_broker, load_model_provider, mask_secret, provider_enabled, save_model_provider

LEGACY_ACTION_ALIASES = {
    'memory.get': 'skill.memory.get',
    'memory.search': 'skill.memory.search',
    'memory.link': 'skill.memory.link',
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso8601(value: str) -> datetime | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except Exception:
        return None


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def compact_text(value: str, limit: int = 280) -> str:
    text = str(value or '').strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + '...'


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


def get_path(data, path: str):
    current = data
    for part in [segment for segment in str(path or '').split('.') if segment]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise ValueError(f'path not found: {path}')
    return current


class SessionStore:
    def __init__(self, root: Path):
        self.root = root / 'control-plane' / 'observability' / 'sessions'
        self.root.mkdir(parents=True, exist_ok=True)
        # Per-session in-process locks, combined with the cross-process file
        # lock in services.persistence (which is not reentrant — never nest on
        # the same session within one thread).
        self._locks_guard = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}

    def _path(self, session_id: str) -> Path:
        return self.root / session_id / 'session.json'

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._session_locks.setdefault(session_id, threading.Lock())

    def create(self, *, session_id: str, agent_id: str, title: str, metadata: dict, heartbeat_ttl_sec: int) -> dict:
        town_hall_shop_id = validate_identifier(str(metadata.get('townHallShopId', 'town-hall')).strip() or 'town-hall', 'shopId')
        created_at = now_iso()
        session = {
            'sessionId': session_id,
            'agentId': agent_id,
            'mayorAgentId': agent_id,
            'title': title,
            'state': 'ACTIVE',
            'heartbeatTtlSec': heartbeat_ttl_sec,
            'lastHeartbeatAt': created_at,
            'lastSeenBy': 'system',
            'messages': [],
            'invocations': [],
            'turns': [],
            'delegations': [],
            'compactions': [],
            'subAgents': [],
            'assignments': [],
            'agents': [
                {
                    'agentId': agent_id,
                    'role': 'mayor',
                    'title': str(metadata.get('mayorTitle', 'Mayor')).strip() or 'Mayor',
                    'shopId': town_hall_shop_id,
                    'createdAt': created_at,
                    'lastHeartbeatAt': created_at,
                    'lastSeenBy': 'system',
                }
            ],
            'shops': {
                town_hall_shop_id: {
                    'shopId': town_hall_shop_id,
                    'name': str(metadata.get('townHallName', 'Town Hall')).strip() or 'Town Hall',
                    'ownerAgentId': agent_id,
                    'shopType': 'town-hall',
                    'enabledActionIds': list(metadata.get('enabledActionIds') or []),
                    'metadata': metadata.get('shopMetadata') if isinstance(metadata.get('shopMetadata'), dict) else {},
                    'createdAt': created_at,
                    'lastHeartbeatAt': created_at,
                    'lastSeenBy': 'system',
                }
            },
            'metadata': metadata,
            'createdAt': created_at,
            'updatedAt': created_at,
        }
        with self._session_lock(session_id), file_lock(self._path(session_id)):
            atomic_write_json(self._path(session_id), session)
        return session

    def get(self, session_id: str) -> dict | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        data = load_json(path, None)
        return data if isinstance(data, dict) else None

    def save(self, session: dict):
        """Full-snapshot write. Only safe while the session is not yet visible
        to concurrent writers (bootstrap); everywhere else use update()."""
        session['updatedAt'] = now_iso()
        session_id = str(session.get('sessionId', ''))
        with self._session_lock(session_id), file_lock(self._path(session_id)):
            atomic_write_json(self._path(session_id), session)

    def update(self, session_id: str, mutator) -> dict:
        """Locked read-modify-write: apply ``mutator`` to the freshly read
        session under both the in-process and cross-process locks so
        concurrent writers (e.g. a heartbeat during orchestrate_turn) can no
        longer lose writes. The mutator may raise to abort without writing."""
        path = self._path(session_id)
        if not path.exists():
            raise ValueError('session not found')

        def _apply(current):
            if not isinstance(current, dict):
                raise ValueError('session not found')
            updated = mutator(current)
            updated['updatedAt'] = now_iso()
            return updated

        with self._session_lock(session_id):
            return update_json(path, _apply, default=None)


class ShopProfileStore:
    def __init__(self, root: Path):
        self.path = root / 'control-plane' / 'observability' / 'session-profiles' / 'profiles.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            with self._lock, file_lock(self.path):
                if not self.path.exists():
                    atomic_write_json(self.path, {'profiles': {}, 'updatedAt': now_iso()})

    def load(self) -> dict:
        data = load_json(self.path, {'profiles': {}, 'updatedAt': now_iso()})
        if not isinstance(data, dict):
            data = {'profiles': {}, 'updatedAt': now_iso()}
        if not isinstance(data.get('profiles'), dict):
            data['profiles'] = {}
        return data

    def update(self, mutator) -> dict:
        """Locked read-modify-write of the profiles payload."""

        def _apply(current):
            if not isinstance(current, dict):
                current = {'profiles': {}, 'updatedAt': now_iso()}
            if not isinstance(current.get('profiles'), dict):
                current['profiles'] = {}
            updated = mutator(current)
            updated['updatedAt'] = now_iso()
            return updated

        with self._lock:
            return update_json(self.path, _apply, default=None)


class AgentPackageStore:
    def __init__(self, root: Path):
        self.path = root / 'control-plane' / 'runtime' / 'agent-packages.json'

    def load(self) -> dict:
        data = load_json(self.path, {'packages': {}, 'updatedAt': now_iso()})
        if not isinstance(data, dict):
            data = {'packages': {}, 'updatedAt': now_iso()}
        if not isinstance(data.get('packages'), dict):
            data['packages'] = {}
        return data


class SessionEngine:
    def __init__(self, umbrella_root: Path, catalog_url: str, execution_url: str, mesh_token: str, heartbeat_ttl_sec: int):
        self.root = umbrella_root
        self.catalog_url = catalog_url.rstrip('/')
        self.execution_url = execution_url.rstrip('/')
        self.mesh_token = mesh_token.strip()
        self.heartbeat_ttl_sec = max(1, int(heartbeat_ttl_sec))
        self.store = SessionStore(umbrella_root)
        self.profile_store = ShopProfileStore(umbrella_root)
        self.package_store = AgentPackageStore(umbrella_root)

    def _headers(self) -> dict:
        headers = {'Content-Type': 'application/json'}
        if self.mesh_token:
            headers['Authorization'] = f'Bearer {self.mesh_token}'
        return headers

    def _get_json(self, base_url: str, path: str, timeout: int = 15) -> dict:
        req = urllib.request.Request(f'{base_url.rstrip("/")}{path}', method='GET', headers=self._headers())
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

    def _catalog_actions(self) -> list[dict]:
        if not self.catalog_url:
            return []
        payload = self._get_json(self.catalog_url, '/v1/catalog/actions')
        actions = payload.get('actions') if isinstance(payload.get('actions'), list) else []
        return [a for a in actions if isinstance(a, dict) and bool(a.get('enabled', False))]

    def _catalog_action_map(self) -> dict[str, dict]:
        return {str(a.get('id', '')).strip(): a for a in self._catalog_actions() if str(a.get('id', '')).strip()}

    def _catalog_item(self, item_id: str) -> dict | None:
        item_id = str(item_id or '').strip()
        if not item_id or not self.catalog_url:
            return None
        payload = self._get_json(self.catalog_url, f'/v1/catalog/items/{urllib.parse.quote(item_id, safe="")}')
        return payload if isinstance(payload, dict) else None

    def _requested_runtime(self, metadata: dict | None = None) -> str:
        requested = str((metadata or {}).get('runtimeRequested') or '').strip()
        if not requested:
            requested = str((metadata or {}).get('runtime') or '').strip()
        return requested

    def _resolved_runtime(self, action_id: str, metadata: dict | None = None) -> dict:
        requested = self._requested_runtime(metadata)
        if str(action_id or '').strip().startswith('skill.'):
            return {
                'runtimeRequested': requested,
                'runtimeResolved': 'umbrella-agent-runtime',
                'runtimeClass': 'umbrella-agent-runtime',
                'runtimeReason': 'shop_skill_action',
                'executorRuntime': 'plugin-host',
            }
        return {
            'runtimeRequested': requested,
            'runtimeResolved': 'umbrella-agent-runtime',
            'runtimeClass': 'umbrella-agent-runtime',
            'runtimeReason': 'session_action',
            'executorRuntime': 'plugin-host',
        }

    def _resolve_action_alias(self, action_id: str) -> tuple[str, str]:
        original = str(action_id or '').strip()
        return original, LEGACY_ACTION_ALIASES.get(original, original)

    def _resolved_timeout_sec(self, action_id: str, metadata: dict | None = None) -> int:
        raw_timeout = (metadata or {}).get('timeoutSec', None)
        if raw_timeout is not None:
            try:
                return max(1, int(raw_timeout))
            except Exception as ex:
                raise ValueError('timeoutSec must be an integer') from ex

        timeout_sec = 30
        action = self._catalog_action_map().get(str(action_id or '').strip())
        plugin_id = str((action or {}).get('pluginId', '')).strip()
        if plugin_id:
            item = self._catalog_item(plugin_id)
            execution_policy = item.get('executionPolicy') if isinstance((item or {}).get('executionPolicy'), dict) else {}
            try:
                max_runtime_sec = int(execution_policy.get('maxRuntimeSec', timeout_sec) or timeout_sec)
            except Exception:
                max_runtime_sec = timeout_sec
            # Default to the skill's own declared budget: this never exceeds the
            # plugin's maxRuntimeSec (short skills stay short) yet gives long
            # actions like the code agent their full budget when a delegation
            # carries no explicit timeout.
            timeout_sec = max(1, max_runtime_sec)
        return timeout_sec

    def _build_action_governance(
        self,
        action_ids: list[str] | None,
        managed_by_agent_id: str,
        *,
        installed: bool = True,
        enabled: bool = True,
        source: str = 'session-bootstrap',
    ) -> dict[str, dict]:
        governance: dict[str, dict] = {}
        for raw_action_id in action_ids or []:
            action_id = str(raw_action_id).strip()
            if not action_id:
                continue
            governance[action_id] = {
                'actionId': action_id,
                'installed': installed,
                'enabled': enabled,
                'managedByAgentId': managed_by_agent_id,
                'managedAt': now_iso(),
                'source': source,
            }
        return governance

    def _profile_payload(self, profile: dict) -> dict:
        return {
            'profileId': str(profile.get('profileId', '')).strip(),
            'name': str(profile.get('name', '')).strip(),
            'shopType': str(profile.get('shopType', 'business')).strip() or 'business',
            'defaultTitle': str(profile.get('defaultTitle', 'Worker')).strip() or 'Worker',
            'defaultShopName': str(profile.get('defaultShopName', '')).strip(),
            'enabledActionIds': [str(x).strip() for x in (profile.get('enabledActionIds') or []) if str(x).strip()],
            'metadata': profile.get('metadata') if isinstance(profile.get('metadata'), dict) else {},
            'createdAt': str(profile.get('createdAt', '')).strip(),
            'updatedAt': str(profile.get('updatedAt', '')).strip(),
        }

    def _package_payload(self, package: dict) -> dict:
        return {
            'packageId': str(package.get('packageId', '')).strip(),
            'name': str(package.get('name', '')).strip(),
            'runtimeId': str(package.get('runtimeId', '')).strip(),
            'role': str(package.get('role', 'worker')).strip() or 'worker',
            'defaultTitle': str(package.get('defaultTitle', 'Worker')).strip() or 'Worker',
            'defaultShopName': str(package.get('defaultShopName', '')).strip(),
            'shopType': str(package.get('shopType', 'business')).strip() or 'business',
            'shopProfileId': str(package.get('shopProfileId', '')).strip(),
            'enabledActionIds': [str(x).strip() for x in (package.get('enabledActionIds') or []) if str(x).strip()],
            'capabilityFamilies': [str(x).strip() for x in (package.get('capabilityFamilies') or []) if str(x).strip()],
            'metadata': package.get('metadata') if isinstance(package.get('metadata'), dict) else {},
        }

    def list_agent_packages(self) -> dict:
        payload = self.package_store.load()
        packages = payload.get('packages') if isinstance(payload.get('packages'), dict) else {}
        return {'packages': [self._package_payload(packages[key]) for key in sorted(packages.keys()) if isinstance(packages.get(key), dict)]}

    def get_agent_package(self, package_id: str) -> dict | None:
        package_id = validate_identifier(package_id, 'packageId')
        payload = self.package_store.load()
        packages = payload.get('packages') if isinstance(payload.get('packages'), dict) else {}
        package = packages.get(package_id)
        if not isinstance(package, dict):
            return None
        return self._package_payload(package)

    def model_provider_status(self) -> dict:
        broker = load_model_broker(self.root)
        provider = load_model_provider(self.root)
        provider_meta = provider.get('provider') if isinstance(provider.get('provider'), dict) else {}
        connection_id = str(((broker.get('routing') or {}).get('defaultConnectionId', '')) or ((broker.get('broker') or {}).get('defaultConnectionId', ''))).strip() or str(provider_meta.get('id', '')).strip()
        connection = (broker.get('connections') or {}).get(connection_id, {}) if isinstance((broker.get('connections') or {}).get(connection_id, {}), dict) else {}
        return {
            'enabled': bool(provider.get('enabled', False)),
            'configured': bool(str(provider_meta.get('baseUrl', '')).strip() and str(provider_meta.get('defaultModel', '')).strip()),
            'broker': {
                'enabled': bool(broker.get('enabled', False)),
                'url': discover_broker_url(self.root, broker),
                'defaultConnectionId': connection_id,
                'allowFallback': bool(((broker.get('broker') or {}).get('allowFallback', True))),
            },
            'connection': {
                'id': connection_id,
                'label': str(connection.get('label', '')).strip(),
                'authMode': str(connection.get('authMode', '')).strip(),
            },
            'provider': {
                'id': str(provider_meta.get('id', '')).strip(),
                'type': str(provider_meta.get('type', '')).strip(),
                'baseUrl': str(provider_meta.get('baseUrl', '')).strip(),
                'defaultModel': str(provider_meta.get('defaultModel', '')).strip(),
                'timeoutSec': int(provider_meta.get('timeoutSec', 20) or 20),
            },
            'agentDefaults': provider.get('agentDefaults') if isinstance(provider.get('agentDefaults'), dict) else {},
            'secrets': {
                'apiKeyPresent': bool(str(((provider.get('secrets') or {}).get('apiKey', ''))).strip()),
                'apiKeyMasked': mask_secret(str(((provider.get('secrets') or {}).get('apiKey', ''))).strip()),
            },
            'paths': provider.get('paths') if isinstance(provider.get('paths'), dict) else {},
        }

    def save_model_provider(self, *, enabled: bool | None = None, provider_payload: dict | None = None, agent_defaults: dict | None = None, api_key: str | None = None) -> dict:
        current = load_model_provider(self.root)
        config = {
            'version': current.get('version', 'umbrella.model-provider.v1'),
            'enabled': bool(current.get('enabled', False)),
            'provider': dict(current.get('provider') if isinstance(current.get('provider'), dict) else {}),
            'agentDefaults': dict(current.get('agentDefaults') if isinstance(current.get('agentDefaults'), dict) else {}),
        }
        secrets = {
            'apiKey': str(((current.get('secrets') or {}).get('apiKey', ''))).strip(),
        }
        if enabled is not None:
            config['enabled'] = bool(enabled)
        if isinstance(provider_payload, dict):
            provider_row = config['provider']
            for key in ('id', 'type', 'baseUrl', 'defaultModel', 'timeoutSec'):
                if key in provider_payload:
                    provider_row[key] = provider_payload[key]
        if isinstance(agent_defaults, dict):
            config['agentDefaults'] = agent_defaults
        if api_key is not None:
            secrets['apiKey'] = str(api_key).strip()
        provider = save_model_provider(self.root, config=config, secrets=secrets)
        return self.model_provider_status() | {'saved': True}

    def test_model_provider(self) -> dict:
        # Route the test through the model broker so a passing test proves the
        # same path conversation uses, not a direct provider call.
        status = self.model_provider_status()
        provider = load_model_provider(self.root)
        if not provider_enabled(provider):
            return status | {'test': {'ok': False, 'configured': False, 'message': 'model provider is not configured'}}
        broker = load_model_broker(self.root)
        broker_url = discover_broker_url(self.root, broker)
        connection_id = str(((status.get('broker') or {}).get('defaultConnectionId', ''))).strip()
        provider_meta = provider.get('provider') if isinstance(provider.get('provider'), dict) else {}
        timeout_sec = float(provider_meta.get('timeoutSec', 120) or 120) + 10.0
        try:
            payload = call_model_broker(
                self.root,
                '/v1/connections/test',
                {'connectionId': connection_id},
                timeout_sec=timeout_sec,
                token_override=self.mesh_token,
            )
        except urllib.error.HTTPError as exc:
            return status | {'test': {
                'ok': False,
                'configured': True,
                'brokerUrl': broker_url,
                'message': f'model broker request failed at {broker_url}: HTTP {exc.code}',
            }}
        except Exception as exc:
            return status | {'test': {
                'ok': False,
                'configured': True,
                'brokerUrl': broker_url,
                'message': f'model broker unreachable at {broker_url}: {exc}',
            }}
        result = payload.get('test') if isinstance(payload.get('test'), dict) else {}
        if not result:
            result = {'ok': False, 'configured': True, 'message': 'model broker returned an invalid test response'}
        result.setdefault('brokerUrl', broker_url)
        return status | {'test': result}

    def model_broker_status(self) -> dict:
        return self.model_provider_status()

    def test_model_broker(self) -> dict:
        return self.test_model_provider()

    def _resolve_agent_package(self, package_id: str) -> dict | None:
        if not str(package_id or '').strip():
            return None
        package = self.get_agent_package(package_id)
        if not package:
            raise ValueError('agent package not found')
        return package

    def _agent_package_defaults(
        self,
        package: dict | None,
        *,
        fallback_role: str,
        fallback_title: str,
        fallback_shop_name: str,
        fallback_shop_type: str,
    ) -> dict:
        package = package or {}
        metadata = package.get('metadata') if isinstance(package.get('metadata'), dict) else {}
        return {
            'packageId': str(package.get('packageId', '')).strip(),
            'role': str(package.get('role', fallback_role)).strip() or fallback_role,
            'title': str(package.get('defaultTitle', fallback_title)).strip() or fallback_title,
            'shopName': str(package.get('defaultShopName', fallback_shop_name)).strip() or fallback_shop_name,
            'shopType': str(package.get('shopType', fallback_shop_type)).strip() or fallback_shop_type,
            'enabledActionIds': [str(x).strip() for x in (package.get('enabledActionIds') or []) if str(x).strip()],
            'runtimeId': str(package.get('runtimeId', '')).strip(),
            'capabilityFamilies': [str(x).strip() for x in (package.get('capabilityFamilies') or []) if str(x).strip()],
            'metadata': metadata,
        }

    def _stamp_worker_shop(self, session: dict, shops: dict, *, package_id: str, created_at: str) -> None:
        """Stamp a worker agent + shop into a fresh town from its agent package,
        so a new town comes up with usable workers instead of only the two civic
        shops. Missing packages and id collisions are skipped, never fatal."""
        package_id = str(package_id or '').strip()
        if not package_id:
            return
        package = self.get_agent_package(package_id)
        if not package:
            return
        defaults = self._agent_package_defaults(
            package,
            fallback_role='worker',
            fallback_title='Worker',
            fallback_shop_name='Worker Shop',
            fallback_shop_type='worker-shop',
        )
        role = defaults['role'] or 'worker'
        try:
            agent_id = validate_identifier(role, 'agentId')
            shop_id = validate_identifier(defaults['shopType'] or f'{role}-shop', 'shopId')
        except Exception:
            return
        existing_agent_ids = {str(a.get('agentId', '')).strip() for a in session.get('agents', []) if isinstance(a, dict)}
        if agent_id in existing_agent_ids or shop_id in shops:
            return
        enabled = defaults['enabledActionIds']
        session['agents'].append(
            {
                'agentId': agent_id,
                'role': role,
                'title': defaults['title'],
                'shopId': shop_id,
                'agentPackageId': defaults['packageId'],
                'createdAt': created_at,
                'lastHeartbeatAt': created_at,
                'lastSeenBy': 'system',
            }
        )
        shops[shop_id] = {
            'shopId': shop_id,
            'name': defaults['shopName'],
            'ownerAgentId': agent_id,
            'shopType': defaults['shopType'],
            'agentPackageId': defaults['packageId'],
            'enabledActionIds': enabled,
            'actionGovernance': self._build_action_governance(enabled, agent_id, source='worker-bootstrap'),
            'metadata': {
                **defaults['metadata'],
                'runtimeId': defaults['runtimeId'],
                'capabilityFamilies': defaults['capabilityFamilies'],
            },
            'createdAt': created_at,
            'lastHeartbeatAt': created_at,
            'lastSeenBy': 'system',
        }

    def list_shop_profiles(self) -> dict:
        payload = self.profile_store.load()
        profiles = payload.get('profiles') if isinstance(payload.get('profiles'), dict) else {}
        return {'profiles': [self._profile_payload(profiles[key]) for key in sorted(profiles.keys()) if isinstance(profiles.get(key), dict)]}

    def get_shop_profile(self, profile_id: str) -> dict | None:
        profile_id = validate_identifier(profile_id, 'profileId')
        payload = self.profile_store.load()
        profiles = payload.get('profiles') if isinstance(payload.get('profiles'), dict) else {}
        profile = profiles.get(profile_id)
        if not isinstance(profile, dict):
            return None
        return self._profile_payload(profile)

    def save_shop_profile(
        self,
        profile_id: str,
        name: str,
        *,
        shop_type: str = 'business',
        default_title: str = 'Worker',
        default_shop_name: str = '',
        enabled_action_ids: list[str] | None = None,
        metadata: dict | None = None,
    ) -> dict:
        profile_id = validate_identifier(profile_id, 'profileId')
        saved: dict = {}

        def _apply(payload: dict) -> dict:
            profiles = payload.get('profiles') if isinstance(payload.get('profiles'), dict) else {}
            previous = profiles.get(profile_id) if isinstance(profiles.get(profile_id), dict) else {}
            profile = {
                'profileId': profile_id,
                'name': str(name or '').strip() or profile_id,
                'shopType': str(shop_type or 'business').strip() or 'business',
                'defaultTitle': str(default_title or 'Worker').strip() or 'Worker',
                'defaultShopName': str(default_shop_name or '').strip() or str(name or profile_id).strip() or profile_id,
                'enabledActionIds': [str(x).strip() for x in (enabled_action_ids or []) if str(x).strip()],
                'metadata': metadata if isinstance(metadata, dict) else {},
                'createdAt': str(previous.get('createdAt', '')).strip() or now_iso(),
                'updatedAt': now_iso(),
            }
            profiles[profile_id] = profile
            payload['profiles'] = profiles
            saved['profile'] = profile
            return payload

        self.profile_store.update(_apply)
        return {'ok': True, 'profile': self._profile_payload(saved['profile'])}

    def _resolve_shop_profile(self, profile_id: str) -> dict | None:
        if not str(profile_id or '').strip():
            return None
        profile = self.get_shop_profile(profile_id)
        if not profile:
            raise ValueError('shop profile not found')
        return profile

    def _town_hall_shop_id(self, session: dict) -> str:
        for agent in session.get('agents', []):
            if isinstance(agent, dict) and str(agent.get('role', '')).strip() == 'mayor':
                shop_id = str(agent.get('shopId', '')).strip()
                if shop_id:
                    return shop_id
        shops = session.get('shops') if isinstance(session.get('shops'), dict) else {}
        return next(iter(shops.keys()), 'town-hall')

    def _originator_shop_id(self, session: dict) -> str:
        for agent in session.get('agents', []):
            if isinstance(agent, dict) and str(agent.get('role', '')).strip() == 'originator':
                shop_id = str(agent.get('shopId', '')).strip()
                if shop_id:
                    return shop_id
        return 'originator-studio'

    def _heartbeat_ttl(self, session: dict) -> int:
        try:
            ttl = int(session.get('heartbeatTtlSec', self.heartbeat_ttl_sec))
        except Exception:
            ttl = self.heartbeat_ttl_sec
        return max(1, ttl)

    def _heartbeat_status(self, last_heartbeat_at: str, ttl_sec: int) -> str:
        seen_at = parse_iso8601(last_heartbeat_at)
        if not seen_at:
            return 'expired'
        age_sec = max(0.0, (datetime.now(timezone.utc) - seen_at).total_seconds())
        if age_sec > ttl_sec:
            return 'expired'
        if age_sec > (ttl_sec / 2.0):
            return 'stale'
        return 'healthy'

    def _apply_liveness(self, session: dict) -> dict:
        ttl_sec = self._heartbeat_ttl(session)
        if not str(session.get('lastHeartbeatAt', '')).strip():
            session['lastHeartbeatAt'] = str(session.get('updatedAt', '')).strip() or str(session.get('createdAt', '')).strip()
        if not str(session.get('lastSeenBy', '')).strip():
            session['lastSeenBy'] = 'system'
        session['heartbeatTtlSec'] = ttl_sec
        session['heartbeatStatus'] = self._heartbeat_status(str(session.get('lastHeartbeatAt', '')), ttl_sec)
        agents = session.get('agents') if isinstance(session.get('agents'), list) else []
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            if not str(agent.get('lastHeartbeatAt', '')).strip():
                agent['lastHeartbeatAt'] = str(agent.get('createdAt', '')).strip() or str(session.get('lastHeartbeatAt', '')).strip()
            if not str(agent.get('lastSeenBy', '')).strip():
                agent['lastSeenBy'] = 'system'
            agent['heartbeatStatus'] = self._heartbeat_status(str(agent.get('lastHeartbeatAt', '')), ttl_sec)
        shops = session.get('shops') if isinstance(session.get('shops'), dict) else {}
        for shop in shops.values():
            if not isinstance(shop, dict):
                continue
            if not str(shop.get('lastHeartbeatAt', '')).strip():
                shop['lastHeartbeatAt'] = str(shop.get('createdAt', '')).strip() or str(session.get('lastHeartbeatAt', '')).strip()
            if not str(shop.get('lastSeenBy', '')).strip():
                shop['lastSeenBy'] = 'system'
            shop['heartbeatStatus'] = self._heartbeat_status(str(shop.get('lastHeartbeatAt', '')), ttl_sec)
        return session

    def _touch_session(self, session: dict, *, seen_by: str, touched_agent_id: str = '') -> dict:
        timestamp = now_iso()
        session['lastHeartbeatAt'] = timestamp
        session['lastSeenBy'] = str(seen_by or 'system').strip() or 'system'
        if touched_agent_id:
            agent_map = self._agent_map(session)
            agent = agent_map.get(touched_agent_id)
            if not agent:
                raise ValueError('agent not registered in session')
            agent['lastHeartbeatAt'] = timestamp
            agent['lastSeenBy'] = session['lastSeenBy']
            shop_id = str(agent.get('shopId', '')).strip()
            shops = session.get('shops') if isinstance(session.get('shops'), dict) else {}
            shop = shops.get(shop_id) if isinstance(shops.get(shop_id), dict) else None
            if shop:
                shop['lastHeartbeatAt'] = timestamp
                shop['lastSeenBy'] = session['lastSeenBy']
        return session

    def _sub_agent_map(self, session: dict) -> dict[str, dict]:
        sub_agents = session.get('subAgents') if isinstance(session.get('subAgents'), list) else []
        return {
            str(sub_agent.get('subAgentId', '')).strip(): sub_agent
            for sub_agent in sub_agents
            if isinstance(sub_agent, dict) and str(sub_agent.get('subAgentId', '')).strip()
        }

    def _assert_session_available(self, session: dict, *, allow_expired: bool = False):
        self._apply_liveness(session)
        if session.get('heartbeatStatus') == 'expired' and not allow_expired:
            raise ValueError('session heartbeat expired; refresh via /v1/sessions/{id}/heartbeat before starting new work')

    def _agent_map(self, session: dict) -> dict[str, dict]:
        agents = session.get('agents') if isinstance(session.get('agents'), list) else []
        return {
            str(agent.get('agentId', '')).strip(): agent
            for agent in agents
            if isinstance(agent, dict) and str(agent.get('agentId', '')).strip()
        }

    def _creator_role(self, session: dict, agent_id: str) -> str:
        creator = self._agent_map(session).get(agent_id)
        return str((creator or {}).get('role', '')).strip() or 'system'

    def _ensure_authorized_creator(self, session: dict, created_by_agent_id: str) -> str:
        creator_id = validate_identifier(created_by_agent_id, 'createdByAgentId')
        agents = self._agent_map(session)
        creator = agents.get(creator_id)
        if not creator:
            raise ValueError('creator agent is not registered in session')
        if str(creator.get('role', '')).strip() not in {'mayor', 'originator'}:
            raise ValueError('creator agent must be the mayor or the originator')
        return creator_id

    def _reconciliation_summary(self, turn: dict, delegations: list[dict], invocations: list[dict]) -> str:
        objective = str(turn.get('objective', '')).strip() or 'turn objective'
        completed = [d for d in delegations if str(d.get('state', '')).strip() == 'COMPLETED']
        failed = [d for d in delegations if str(d.get('state', '')).strip() == 'FAILED']
        skipped = [d for d in delegations if str(d.get('state', '')).strip() == 'SKIPPED']
        snippets: list[str] = []
        invocation_by_id = {
            str(inv.get('delegationId', '')).strip(): inv
            for inv in invocations
            if isinstance(inv, dict) and str(inv.get('delegationId', '')).strip()
        }
        for delegation in completed:
            delegation_id = str(delegation.get('delegationId', '')).strip()
            shop_id = str(delegation.get('shopId', '')).strip() or 'shop'
            invocation = invocation_by_id.get(delegation_id, {})
            plugin_result = ((((invocation.get('result') or {}).get('result') or {}).get('pluginResult')) or {})
            summary = str(plugin_result.get('summary', '')).strip() or str(plugin_result.get('reply', '')).strip()
            if summary:
                snippets.append(f'{shop_id}: {summary}')
        # Surface the ACTUAL failure reason for each failed delegation so the mayor
        # reports what really happened (e.g. a 900s timeout) instead of confabulating.
        for delegation in failed:
            delegation_id = str(delegation.get('delegationId', '')).strip()
            shop_id = str(delegation.get('shopId', '')).strip() or 'shop'
            inv_result = (invocation_by_id.get(delegation_id, {}) or {}).get('result') or {}
            inner = inv_result.get('result') if isinstance(inv_result.get('result'), dict) else {}
            reason = str(inv_result.get('failureReason', '')).strip()
            detail = (str(inner.get('error', '')).strip()
                      or str(inv_result.get('failureMessage', '')).strip()
                      or str(inner.get('summary', '')).strip())
            note = ' — '.join(p for p in (reason, detail) if p) or 'no failure detail recorded'
            snippets.append(f'{shop_id} FAILED: {note[:300]}')
        status = f'{len(completed)} completed'
        if failed:
            status += f', {len(failed)} failed'
        if skipped:
            status += f', {len(skipped)} skipped'
        if snippets:
            return f'Mayor summary for "{objective}": {status}. ' + ' | '.join(snippets)
        return f'Mayor summary for "{objective}": {status}.'

    def _resolve_orchestration_inputs(self, item: dict, completed_steps: dict[str, dict]) -> dict:
        inputs = dict(item.get('inputs') or {}) if isinstance(item.get('inputs'), dict) else {}
        bindings = item.get('inputBindings') if isinstance(item.get('inputBindings'), list) else []
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            input_key = str(binding.get('inputKey', '')).strip()
            from_step_id = str(binding.get('fromStepId', '')).strip()
            result_path = str(binding.get('resultPath', 'result.result.pluginResult.summary')).strip()
            if not input_key or not from_step_id:
                raise ValueError('inputBindings require inputKey and fromStepId')
            upstream = completed_steps.get(from_step_id)
            if not isinstance(upstream, dict):
                raise ValueError(f'upstream step not found for binding: {from_step_id}')
            inputs[input_key] = get_path(upstream, result_path)
        return inputs

    def _dependency_failure_policy(self, item: dict, orchestration_metadata: dict | None = None) -> str:
        policy = str(item.get('onDependencyFailure', '')).strip() or str((orchestration_metadata or {}).get('onDependencyFailure', '')).strip() or 'skip'
        if policy not in {'skip', 'fail-fast', 'continue'}:
            raise ValueError('onDependencyFailure must be one of skip, fail-fast, continue')
        return policy

    def _retry_budget(self, item: dict, orchestration_metadata: dict | None = None) -> int:
        raw = item.get('retryBudget', (orchestration_metadata or {}).get('retryBudget', 0))
        try:
            budget = int(raw)
        except Exception as ex:
            raise ValueError('retryBudget must be an integer') from ex
        if budget < 0:
            raise ValueError('retryBudget must be non-negative')
        return budget

    def _record_skipped_delegation(
        self,
        session_id: str,
        turn_id: str,
        *,
        requested_shop_id: str,
        requested_shop_profile_id: str,
        requested_role: str,
        action_id: str,
        plan_step_id: str,
        depends_on: list[str],
        failure_policy: str,
        reason: str,
        metadata: dict | None = None,
    ) -> dict:
        recorded: dict = {}

        def _skip(session: dict) -> dict:
            turns = session.get('turns') if isinstance(session.get('turns'), list) else []
            turn = next((t for t in turns if isinstance(t, dict) and t.get('turnId') == turn_id), None)
            if not turn:
                raise ValueError('turn not found')
            delegation_id = validate_identifier(f'delegation-{uuid.uuid4().hex[:12]}', 'delegationId')
            delegation = {
                'delegationId': delegation_id,
                'turnId': turn_id,
                'delegatedByAgentId': str(session.get('mayorAgentId', '')).strip(),
                'shopId': '',
                'requestedShopId': str(requested_shop_id or '').strip(),
                'requestedShopProfileId': str(requested_shop_profile_id or '').strip(),
                'requestedRole': str(requested_role or '').strip(),
                'resolvedShopProfileId': '',
                'resolvedOwnerAgentId': '',
                'planStepId': plan_step_id,
                'dependsOn': depends_on,
                'actionId': str(action_id or '').strip(),
                'state': 'SKIPPED',
                'skipReason': reason,
                'failurePolicy': failure_policy,
                'createdAt': now_iso(),
                'completedAt': now_iso(),
                'metadata': metadata or {},
            }
            delegations = session.get('delegations') if isinstance(session.get('delegations'), list) else []
            delegations.append(delegation)
            session['delegations'] = delegations
            delegation_ids = turn.get('delegationIds') if isinstance(turn.get('delegationIds'), list) else []
            delegation_ids.append(delegation_id)
            turn['delegationIds'] = delegation_ids
            session['turns'] = turns
            recorded['delegation'] = delegation
            return session

        self.store.update(session_id, _skip)
        return recorded['delegation']

    def _effective_shop_action_ids(self, shop: dict, catalog_actions: dict[str, dict]) -> list[str]:
        action_governance = shop.get('actionGovernance') if isinstance(shop.get('actionGovernance'), dict) else {}
        enabled_ids = [
            action_id
            for action_id, row in action_governance.items()
            if isinstance(row, dict)
            and str(action_id).strip() in catalog_actions
            and bool(row.get('installed', False))
            and bool(row.get('enabled', False))
        ]
        if enabled_ids:
            return sorted(set(enabled_ids))
        legacy_enabled = [str(x).strip() for x in (shop.get('enabledActionIds') or []) if str(x).strip()]
        if legacy_enabled:
            return [action_id for action_id in legacy_enabled if action_id in catalog_actions]
        if str(shop.get('shopType', '')).strip() == 'town-hall':
            return sorted(catalog_actions.keys())
        return []

    def _shop_manager_ids(self, session: dict, shop: dict) -> set[str]:
        manager_ids = {str(session.get('mayorAgentId', '')).strip()}
        owner_agent_id = str(shop.get('ownerAgentId', '')).strip()
        if owner_agent_id:
            manager_ids.add(owner_agent_id)
        for agent in session.get('agents', []):
            if isinstance(agent, dict) and str(agent.get('role', '')).strip() == 'originator':
                manager_id = str(agent.get('agentId', '')).strip()
                if manager_id:
                    manager_ids.add(manager_id)
        return {agent_id for agent_id in manager_ids if agent_id}

    def _assert_shop_manager(self, session: dict, shop: dict, managed_by_agent_id: str) -> str:
        manager_id = validate_identifier(managed_by_agent_id, 'managedByAgentId')
        if manager_id not in self._shop_manager_ids(session, shop):
            raise ValueError('agent is not authorized to govern this shop')
        return manager_id

    def _resolve_turn_target(self, session: dict, *, shop_id: str = '', shop_profile_id: str = '', role: str = '') -> tuple[str, dict]:
        shops = session.get('shops') if isinstance(session.get('shops'), dict) else {}
        if str(shop_id or '').strip():
            resolved_shop_id = validate_identifier(shop_id, 'shopId')
            shop = shops.get(resolved_shop_id) if isinstance(shops.get(resolved_shop_id), dict) else None
            if not shop:
                raise ValueError('shop not found')
            return resolved_shop_id, shop

        if str(shop_profile_id or '').strip():
            requested_profile_id = validate_identifier(shop_profile_id, 'shopProfileId')
            matches = [
                (candidate_shop_id, shop)
                for candidate_shop_id, shop in shops.items()
                if isinstance(shop, dict) and str(shop.get('shopProfileId', '')).strip() == requested_profile_id
            ]
            if not matches:
                raise ValueError('no shop found for shop profile')
            resolved_shop_id, shop = sorted(matches, key=lambda item: item[0])[0]
            return resolved_shop_id, shop

        if str(role or '').strip():
            requested_role = str(role or '').strip()
            agents = session.get('agents') if isinstance(session.get('agents'), list) else []
            matches: list[tuple[str, dict]] = []
            for agent in agents:
                if not isinstance(agent, dict) or str(agent.get('role', '')).strip() != requested_role:
                    continue
                candidate_shop_id = str(agent.get('shopId', '')).strip()
                shop = shops.get(candidate_shop_id) if isinstance(shops.get(candidate_shop_id), dict) else None
                if shop:
                    matches.append((candidate_shop_id, shop))
            if not matches:
                raise ValueError('no shop found for role')
            resolved_shop_id, shop = sorted(matches, key=lambda item: item[0])[0]
            return resolved_shop_id, shop

        raise ValueError('one of shopId, shopProfileId, or role is required')

    def _resolve_sub_agent_target(
        self,
        session: dict,
        *,
        agent_id: str = '',
        shop_id: str = '',
        shop_profile_id: str = '',
        role: str = '',
    ) -> tuple[str, dict, dict]:
        agent_map = self._agent_map(session)
        if str(agent_id or '').strip():
            resolved_agent_id = validate_identifier(agent_id, 'agentId')
            agent = agent_map.get(resolved_agent_id)
            if not agent:
                raise ValueError('agent not found in session')
            resolved_shop_id = str(agent.get('shopId', '')).strip()
            if not resolved_shop_id:
                raise ValueError('agent does not own a shop')
            shops = session.get('shops') if isinstance(session.get('shops'), dict) else {}
            shop = shops.get(resolved_shop_id) if isinstance(shops.get(resolved_shop_id), dict) else None
            if not shop:
                raise ValueError('shop not found')
            return resolved_agent_id, shop, agent
        resolved_shop_id, shop = self._resolve_turn_target(session, shop_id=shop_id, shop_profile_id=shop_profile_id, role=role)
        owner_agent_id = str(shop.get('ownerAgentId', '')).strip()
        agent = agent_map.get(owner_agent_id)
        if not agent:
            raise ValueError('shop owner agent not found in session')
        return resolved_shop_id, shop, agent

    def _shop_actions(self, session: dict) -> dict[str, list[dict]]:
        catalog_actions = self._catalog_action_map()
        out: dict[str, list[dict]] = {}
        shops = session.get('shops') if isinstance(session.get('shops'), dict) else {}
        for shop_id, shop in shops.items():
            if not isinstance(shop, dict):
                continue
            enabled_ids = self._effective_shop_action_ids(shop, catalog_actions)
            out[shop_id] = [catalog_actions[action_id] for action_id in enabled_ids if action_id in catalog_actions]
        return out

    def _append_message_row(self, session: dict, role: str, content: str, metadata: dict | None = None) -> dict:
        message = {
            'messageId': f'msg-{uuid.uuid4().hex[:12]}',
            'role': role,
            'content': str(content or ''),
            'metadata': metadata or {},
            'createdAt': now_iso(),
        }
        session['messages'].append(message)
        return message

    def _prompt_text(self, path_hint: str) -> str:
        rel = str(path_hint or '').strip()
        if not rel:
            return ''
        path = (self.root / rel).resolve()
        if not path.exists():
            return ''
        try:
            return path.read_text(encoding='utf-8').strip()
        except Exception:
            return ''

    def _conversation_history(self, session: dict, limit: int = 12) -> list[dict]:
        messages = session.get('messages') if isinstance(session.get('messages'), list) else []
        rows: list[dict] = []
        for message in messages[-limit:]:
            if not isinstance(message, dict):
                continue
            rows.append(
                {
                    'role': str(message.get('role', '')).strip(),
                    'content': str(message.get('content', '')).strip(),
                    'metadata': message.get('metadata') if isinstance(message.get('metadata'), dict) else {},
                }
            )
        return rows

    def _available_worker_shops(self, session: dict) -> list[dict]:
        shops = session.get('shops') if isinstance(session.get('shops'), dict) else {}
        out: list[dict] = []
        for shop_id, shop in shops.items():
            if not isinstance(shop, dict):
                continue
            shop_type = str(shop.get('shopType', '')).strip()
            if shop_type in {'town-hall', 'originator-studio'}:
                continue
            actions = self._effective_shop_action_ids(shop, self._catalog_action_map())
            preferred = 'skill.chat.respond' if 'skill.chat.respond' in actions else ('skill.memory.summarize' if 'skill.memory.summarize' in actions else (actions[0] if actions else ''))
            out.append(
                {
                    'shopId': str(shop_id).strip(),
                    'name': str(shop.get('name', '')).strip() or str(shop_id).strip(),
                    'shopType': shop_type,
                    'ownerAgentId': str(shop.get('ownerAgentId', '')).strip(),
                    'preferredConversationAction': preferred,
                    'enabledActionIds': actions,
                }
            )
        return out

    def _resolve_conversation_target(self, session: dict, target: str) -> tuple[str, str, dict, dict]:
        target = str(target or '').strip() or str(session.get('mayorAgentId', 'mayor')).strip() or 'mayor'
        agents = self._agent_map(session)
        shops = session.get('shops') if isinstance(session.get('shops'), dict) else {}

        def resolve_shop_for_agent(resolved_agent_id: str, *, fallback_role: str = '') -> tuple[str, dict | None]:
            resolved_agent = agents.get(resolved_agent_id) if resolved_agent_id else None
            candidate_shop_id = str((resolved_agent or {}).get('shopId', '')).strip()
            if candidate_shop_id and isinstance(shops.get(candidate_shop_id), dict):
                return candidate_shop_id, shops[candidate_shop_id]
            for current_shop_id, current_shop in shops.items():
                if not isinstance(current_shop, dict):
                    continue
                if str(current_shop.get('ownerAgentId', '')).strip() == resolved_agent_id:
                    return str(current_shop_id).strip(), current_shop
            if fallback_role:
                for current_agent_id, current_agent in agents.items():
                    if str(current_agent.get('role', '')).strip() != fallback_role:
                        continue
                    return resolve_shop_for_agent(current_agent_id)
            return '', None

        shop = None
        shop_id = ''
        agent = None
        agent_id = ''
        if target in shops and isinstance(shops.get(target), dict):
            shop_id = target
            shop = shops[target]
            agent_id = str(shop.get('ownerAgentId', '')).strip()
            agent = agents.get(agent_id) if agent_id else None
        elif target in agents:
            agent_id = target
            agent = agents[agent_id]
            shop_id, shop = resolve_shop_for_agent(agent_id)
        elif target in {'mayor', 'originator'}:
            fallback_role = 'mayor' if target == 'mayor' else 'originator'
            for current_agent_id, current_agent in agents.items():
                if str(current_agent.get('role', '')).strip() == fallback_role:
                    agent_id = current_agent_id
                    agent = current_agent
                    break
            if not agent_id and target == 'mayor':
                agent_id = str(session.get('mayorAgentId', '')).strip()
                agent = agents.get(agent_id) if agent_id else None
            shop_id, shop = resolve_shop_for_agent(agent_id, fallback_role=fallback_role)
        else:
            for current_agent_id, current_agent in agents.items():
                if str(current_agent.get('role', '')).strip() == target:
                    agent_id = current_agent_id
                    agent = current_agent
                    shop_id, shop = resolve_shop_for_agent(agent_id, fallback_role=target)
                    break
        if not shop_id or not isinstance(shop, dict) or not isinstance(agent, dict):
            raise ValueError(f'target not found: {target}')
        return agent_id, shop_id, agent, shop

    def _conversation_action_id(self, session: dict, shop_id: str) -> str:
        shop = (session.get('shops') or {}).get(shop_id)
        if not isinstance(shop, dict):
            raise ValueError('shop not found')
        actions = self._effective_shop_action_ids(shop, self._catalog_action_map())
        if 'skill.chat.respond' in actions:
            return 'skill.chat.respond'
        if 'skill.memory.summarize' in actions:
            return 'skill.memory.summarize'
        raise ValueError('no conversational action is enabled in target shop')

    def _environment_summary(self) -> str:
        """Machine toolchain + coherence survey, computed once per process."""
        cached = getattr(self, '_env_summary_cache', None)
        if cached is None:
            try:
                cached = str(survey_environment().get('summary', '')).strip()
            except Exception:  # noqa: BLE001 — survey is best-effort
                cached = ''
            self._env_summary_cache = cached
        return cached

    def _converse_direct(
        self,
        session_id: str,
        *,
        session: dict,
        target: str,
        agent_id: str,
        shop_id: str,
        shop: dict,
        runtime_mode: str,
        content: str,
        system_prompt: str,
        instructions: str,
        extra_inputs: dict | None = None,
        invocation_metadata: dict | None = None,
    ) -> dict:
        action_id = self._conversation_action_id(session, shop_id)
        agent_row = self._agent_map(session).get(agent_id) if isinstance(self._agent_map(session), dict) else None
        agent_package_id = str((shop.get('agentPackageId', '') or ((agent_row or {}).get('agentPackageId', '')))).strip()
        agent_package = self.get_agent_package(agent_package_id) if agent_package_id else None
        package_metadata = agent_package.get('metadata') if isinstance((agent_package or {}).get('metadata'), dict) else {}
        # Coordinators (mayor/originator) get the machine's coherence-aware toolchain
        # survey so they can suggest a coherent runtime and delegate any needed
        # install. Worker agents don't need it (the code agent surveys on its own).
        agent_role = str((agent_row or {}).get('role', '')).strip()
        environment_summary = self._environment_summary() if agent_role in {'mayor', 'originator'} else ''
        inputs = {
            'sessionId': session_id,
            'agentId': agent_id,
            'shopId': shop_id,
            'agentPackageId': agent_package_id,
            'agentPackageMetadata': package_metadata,
            'message': content,
            'conversationHistory': self._conversation_history(session),
            'townContext': {
                'sessionId': session_id,
                'title': str(session.get('title', '')).strip(),
                'mayorAgentId': str(session.get('mayorAgentId', '')).strip(),
                'workerShopCount': len(self._available_worker_shops(session)),
            },
            'availableShops': self._available_worker_shops(session),
            'subAgents': session.get('subAgents') if isinstance(session.get('subAgents'), list) else [],
            'runtimeMode': runtime_mode,
            'systemPrompt': system_prompt,
            'instructions': instructions,
            'environmentSummary': environment_summary,
        }
        if isinstance(extra_inputs, dict):
            inputs.update(extra_inputs)
        result = self.invoke_action(
            session_id=session_id,
            action_id=action_id,
            inputs=inputs,
            metadata={
                'timeoutSec': 120,
                'role': str((self._agent_map(session).get(agent_id) or {}).get('role', '')).strip(),
                **(invocation_metadata if isinstance(invocation_metadata, dict) else {}),
            },
            shop_id=shop_id,
        )
        invocation = result.get('invocation') if isinstance(result.get('invocation'), dict) else {}
        plugin_result = ((((invocation.get('result') or {}).get('result') or {}).get('pluginResult')) or {})
        reply = str(plugin_result.get('reply', '')).strip() or str(plugin_result.get('summary', '')).strip()
        mode = str(plugin_result.get('mode', 'direct')).strip() or 'direct'
        return {
            'ok': bool(result.get('ok', False)),
            'actionId': action_id,
            'reply': reply,
            'mode': mode,
            'providerUsed': bool(plugin_result.get('providerUsed', False)),
            'providerType': str(plugin_result.get('providerType', '')).strip(),
            'connectionUsed': str(plugin_result.get('connectionUsed', '')).strip(),
            'modelUsed': str(plugin_result.get('modelUsed', '')).strip(),
            'fallbackUsed': bool(plugin_result.get('fallbackUsed', False)),
            'latencyMs': int(plugin_result.get('latencyMs', 0) or 0),
            'delegationPlan': plugin_result.get('delegationPlan') if isinstance(plugin_result.get('delegationPlan'), list) else [],
            'questions': plugin_result.get('questions') if isinstance(plugin_result.get('questions'), list) else [],
            'invocation': invocation,
            'pluginResult': plugin_result,
        }

    def _conversation_reply_or_error(self, direct: dict, *, fallback_error: str) -> tuple[str, str | None]:
        reply = str(direct.get('reply', '')).strip()
        if reply:
            return reply, None
        plugin_result = direct.get('pluginResult') if isinstance(direct.get('pluginResult'), dict) else {}
        error_bits: list[str] = []
        provider_type = str(direct.get('providerType', '')).strip()
        model_used = str(direct.get('modelUsed', '')).strip()
        if provider_type:
            error_bits.append(provider_type)
        if model_used:
            error_bits.append(model_used)
        invocation = direct.get('invocation') if isinstance(direct.get('invocation'), dict) else {}
        result_payload = invocation.get('result') if isinstance(invocation.get('result'), dict) else {}
        result_error = result_payload.get('error') if isinstance(result_payload.get('error'), dict) else {}
        plugin_error = plugin_result.get('error') if isinstance(plugin_result.get('error'), dict) else {}
        message = (
            str(plugin_error.get('message', '')).strip()
            or str(result_error.get('message', '')).strip()
            or fallback_error
        )
        if error_bits:
            message = f"{message} ({' / '.join(error_bits)})"
        return '', message

    def create_session(self, agent_id: str, title: str, metadata: dict | None = None) -> dict:
        agent_id = validate_identifier(agent_id, 'agentId')
        session_id = validate_identifier(f'session-{uuid.uuid4().hex[:12]}', 'sessionId')
        metadata = metadata or {}
        try:
            heartbeat_ttl_sec = int(metadata.get('heartbeatTtlSec', self.heartbeat_ttl_sec))
        except Exception as ex:
            raise ValueError('heartbeatTtlSec must be an integer') from ex
        if heartbeat_ttl_sec < 1:
            raise ValueError('heartbeatTtlSec must be positive')
        mayor_package = self._resolve_agent_package(str(metadata.get('mayorAgentPackageId', 'umbrella.mayor.v1')).strip() or 'umbrella.mayor.v1')
        originator_package = self._resolve_agent_package(str(metadata.get('originatorAgentPackageId', 'umbrella.originator.v1')).strip() or 'umbrella.originator.v1')
        mayor_defaults = self._agent_package_defaults(
            mayor_package,
            fallback_role='mayor',
            fallback_title='Mayor',
            fallback_shop_name='Town Hall',
            fallback_shop_type='town-hall',
        )
        originator_defaults = self._agent_package_defaults(
            originator_package,
            fallback_role='originator',
            fallback_title='Originator',
            fallback_shop_name='Originator Studio',
            fallback_shop_type='originator-studio',
        )
        originator_agent_id = validate_identifier(str(metadata.get('originatorAgentId', 'originator')).strip() or 'originator', 'agentId')
        originator_shop_id = validate_identifier(str(metadata.get('originatorShopId', 'originator-studio')).strip() or 'originator-studio', 'shopId')
        session = self.store.create(
            session_id=session_id,
            agent_id=agent_id,
            title=str(title or '').strip(),
            metadata=metadata,
            heartbeat_ttl_sec=heartbeat_ttl_sec,
        )
        created_at = now_iso()
        session['agents'][0].update(
            {
                'role': mayor_defaults['role'],
                'title': str(metadata.get('mayorTitle', mayor_defaults['title'])).strip() or mayor_defaults['title'],
                'agentPackageId': mayor_defaults['packageId'],
            }
        )
        session['agents'].append(
            {
                'agentId': originator_agent_id,
                'role': originator_defaults['role'],
                'title': str(metadata.get('originatorTitle', originator_defaults['title'])).strip() or originator_defaults['title'],
                'shopId': originator_shop_id,
                'agentPackageId': originator_defaults['packageId'],
                'createdAt': created_at,
                'lastHeartbeatAt': created_at,
                'lastSeenBy': 'system',
            }
        )
        shops = session.get('shops') if isinstance(session.get('shops'), dict) else {}
        town_hall_shop_id = self._town_hall_shop_id(session)
        town_hall_shop = shops.get(town_hall_shop_id) if isinstance(shops.get(town_hall_shop_id), dict) else None
        if town_hall_shop is not None:
            mayor_enabled_action_ids = [
                str(x).strip()
                for x in (
                    metadata.get('enabledActionIds') if isinstance(metadata.get('enabledActionIds'), list) else mayor_defaults['enabledActionIds']
                )
                if str(x).strip()
            ]
            mayor_shop_metadata = metadata.get('shopMetadata') if isinstance(metadata.get('shopMetadata'), dict) else {}
            town_hall_shop.update(
                {
                    'name': str(metadata.get('townHallName', mayor_defaults['shopName'])).strip() or mayor_defaults['shopName'],
                    'shopType': mayor_defaults['shopType'],
                    'agentPackageId': mayor_defaults['packageId'],
                    'enabledActionIds': mayor_enabled_action_ids,
                    'metadata': {
                        **mayor_defaults['metadata'],
                        **mayor_shop_metadata,
                        'runtimeId': mayor_defaults['runtimeId'],
                        'capabilityFamilies': mayor_defaults['capabilityFamilies'],
                    },
                }
            )
            town_hall_shop['actionGovernance'] = self._build_action_governance(
                mayor_enabled_action_ids,
                agent_id,
                source='town-bootstrap',
            )
        shops[originator_shop_id] = {
            'shopId': originator_shop_id,
            'name': str(metadata.get('originatorShopName', originator_defaults['shopName'])).strip() or originator_defaults['shopName'],
            'ownerAgentId': originator_agent_id,
            'shopType': originator_defaults['shopType'],
            'agentPackageId': originator_defaults['packageId'],
            'enabledActionIds': [
                str(x).strip()
                for x in (
                    metadata.get('originatorEnabledActionIds')
                    if isinstance(metadata.get('originatorEnabledActionIds'), list)
                    else originator_defaults['enabledActionIds']
                )
                if str(x).strip()
            ],
            'actionGovernance': self._build_action_governance(
                metadata.get('originatorEnabledActionIds')
                if isinstance(metadata.get('originatorEnabledActionIds'), list)
                else originator_defaults['enabledActionIds'],
                originator_agent_id,
                source='originator-bootstrap',
            ),
            'metadata': {
                **originator_defaults['metadata'],
                **(metadata.get('originatorShopMetadata') if isinstance(metadata.get('originatorShopMetadata'), dict) else {}),
                'runtimeId': originator_defaults['runtimeId'],
                'capabilityFamilies': originator_defaults['capabilityFamilies'],
            },
            'createdAt': created_at,
            'lastHeartbeatAt': created_at,
            'lastSeenBy': 'system',
        }
        # Bootstrap the default worker shops so a new town can actually build,
        # search, and research without hand-wiring shops every time. Overridable
        # per town via metadata.workerAgentPackageIds (pass [] for a bare town).
        worker_package_ids = metadata.get('workerAgentPackageIds')
        if not isinstance(worker_package_ids, list):
            worker_package_ids = [
                'umbrella.programming-agent.v1',
                'umbrella.web-agent.v1',
                'umbrella.research-agent.v1',
                'umbrella.security-agent.v1',
            ]
        for worker_package_id in worker_package_ids:
            self._stamp_worker_shop(session, shops, package_id=str(worker_package_id).strip(), created_at=created_at)
        session['shops'] = shops
        self.store.save(session)
        return self._session_payload(session)

    def _session_payload(self, session: dict) -> dict:
        payload = dict(session)
        payload = self._apply_liveness(payload)
        payload['subAgents'] = payload.get('subAgents') if isinstance(payload.get('subAgents'), list) else []
        payload['assignments'] = payload.get('assignments') if isinstance(payload.get('assignments'), list) else []
        for sub_agent in payload['subAgents']:
            if not isinstance(sub_agent, dict):
                continue
            if not str(sub_agent.get('lastHeartbeatAt', '')).strip():
                sub_agent['lastHeartbeatAt'] = str(sub_agent.get('createdAt', '')).strip() or str(payload.get('lastHeartbeatAt', '')).strip()
            if not str(sub_agent.get('lastSeenBy', '')).strip():
                sub_agent['lastSeenBy'] = 'system'
            sub_agent['heartbeatStatus'] = self._heartbeat_status(
                str(sub_agent.get('lastHeartbeatAt', '')),
                self._heartbeat_ttl(payload),
            )
        shops = payload.get('shops') if isinstance(payload.get('shops'), dict) else {}
        catalog_actions = self._catalog_action_map()
        normalized_shops: dict[str, dict] = {}
        for shop_id, shop in shops.items():
            if not isinstance(shop, dict):
                continue
            shop_payload = dict(shop)
            action_governance = shop_payload.get('actionGovernance') if isinstance(shop_payload.get('actionGovernance'), dict) else {}
            shop_payload['actionGovernance'] = action_governance
            shop_payload['effectiveEnabledActionIds'] = self._effective_shop_action_ids(shop_payload, catalog_actions)
            normalized_shops[shop_id] = shop_payload
        payload['shops'] = normalized_shops
        shop_actions = self._shop_actions(session)
        town_hall_shop_id = self._town_hall_shop_id(session)
        payload['shopActions'] = shop_actions
        payload['availableActions'] = shop_actions.get(town_hall_shop_id, [])
        return payload

    def get_session(self, session_id: str) -> dict | None:
        session_id = validate_identifier(session_id, 'sessionId')
        session = self.store.get(session_id)
        if not session:
            return None
        return self._session_payload(session)

    def add_message(self, session_id: str, role: str, content: str, metadata: dict | None = None) -> dict:
        session_id = validate_identifier(session_id, 'sessionId')
        if not self.store.get(session_id):
            raise ValueError('session not found')
        role = str(role or '').strip()
        if role not in {'system', 'user', 'assistant', 'tool'}:
            raise ValueError('role must be one of system, user, assistant, tool')
        appended: dict = {}

        def _append(session: dict) -> dict:
            appended['message'] = self._append_message_row(session, role, content, metadata)
            return session

        session = self.store.update(session_id, _append)
        return {'ok': True, 'session': self._session_payload(session), 'message': appended['message']}

    def converse(
        self,
        session_id: str,
        *,
        target: str,
        content: str,
        mode: str = '',
        allow_delegation: bool = True,
        conversation_mode_hint: str = '',
        wait_for_result: bool = False,
    ) -> dict:
        session_id = validate_identifier(session_id, 'sessionId')
        session = self.store.get(session_id)
        if not session:
            raise ValueError('session not found')
        self._assert_session_available(session)
        content = str(content or '').strip()
        if not content:
            raise ValueError('content is required')
        agent_id, shop_id, agent, shop = self._resolve_conversation_target(session, target)
        role = str(agent.get('role', '')).strip() or 'worker'
        prompt_text = self._prompt_text(str((shop.get('metadata') or {}).get('promptTemplate', '')))

        def _append_user(fresh: dict) -> dict:
            self._apply_liveness(fresh)
            self._append_message_row(
                fresh,
                'user',
                content,
                {
                    'target': str(target or '').strip() or agent_id,
                    'targetAgentId': agent_id,
                    'targetShopId': shop_id,
                    'conversationModeHint': str(conversation_mode_hint or '').strip(),
                },
            )
            return fresh

        session = self.store.update(session_id, _append_user)

        if role == 'mayor':
            direct = self._converse_direct(
                session_id,
                session=session,
                target=target,
                agent_id=agent_id,
                shop_id=shop_id,
                shop=shop,
                runtime_mode=mode or 'direct-first',
                content=content,
                system_prompt=prompt_text,
                instructions='Answer directly when possible. Delegate only when specialized worker work is needed.',
            )
            if not direct.get('ok', False):
                return {'ok': False, 'target': target, 'agentId': agent_id, 'shopId': shop_id, 'error': {'message': 'mayor conversation action failed'}, 'invocation': direct.get('invocation')}
            mode_resolved = str(direct.get('mode', 'direct')).strip() or 'direct'
            delegation_plan = direct.get('delegationPlan') if isinstance(direct.get('delegationPlan'), list) else []
            worker_shops = self._available_worker_shops(session)
            if mode_resolved == 'delegate' and allow_delegation and worker_shops and delegation_plan:
                turn_out = self.create_turn(session_id=session_id, objective=content, requested_by='user', metadata={'source': 'session-converse', 'target': 'mayor'})
                turn = turn_out.get('turn') if isinstance(turn_out.get('turn'), dict) else {}
                turn_id = str(turn.get('turnId', '')).strip()
                plan: list[dict] = []
                for idx, row in enumerate(delegation_plan, start=1):
                    if not isinstance(row, dict):
                        continue
                    target_shop_id = str(row.get('shopId', '')).strip() or worker_shops[0]['shopId']
                    plan.append(
                        {
                            'planStepId': validate_identifier(str(row.get('planStepId', '')).strip() or f'conversation-step-{idx}', 'planStepId'),
                            'shopId': target_shop_id,
                            'actionId': str(row.get('actionId', '')).strip() or worker_shops[0]['preferredConversationAction'],
                            'inputs': row.get('inputs') if isinstance(row.get('inputs'), dict) else {'message': content},
                            'metadata': {'source': 'session-converse-mayor'},
                        }
                    )
                provider_meta = {
                    'providerUsed': bool(direct.get('providerUsed', False)),
                    'providerType': str(direct.get('providerType', '')).strip(),
                    'connectionUsed': str(direct.get('connectionUsed', '')).strip(),
                    'modelUsed': str(direct.get('modelUsed', '')).strip(),
                    'fallbackUsed': bool(direct.get('fallbackUsed', False)),
                    'latencyMs': int(direct.get('latencyMs', 0) or 0),
                }

                if wait_for_result:
                    # Synchronous path: block until the delegated work completes.
                    orchestrated = self.orchestrate_turn(session_id=session_id, turn_id=turn_id, plan=plan, metadata={'source': 'session-converse', 'allowDelegation': True})
                    reconciliation = orchestrated.get('reconciliation') if isinstance(orchestrated.get('reconciliation'), dict) else {}
                    reply = str(reconciliation.get('summary', '')).strip() or compact_text(str(direct.get('reply', '')).strip() or 'The delegated work is complete.')
                    appended: dict = {}

                    def _append_summary(fresh: dict) -> dict:
                        appended['message'] = self._append_message_row(
                            fresh, 'assistant', reply,
                            {'targetAgentId': agent_id, 'targetShopId': shop_id, 'modeResolved': 'delegate',
                             'delegationStatus': 'completed', 'turnId': turn_id,
                             'delegationIds': [str(row.get('delegationId', '')).strip() for row in (orchestrated.get('delegations') or []) if isinstance(row, dict)],
                             **provider_meta},
                        )
                        return fresh

                    session = self.store.update(session_id, _append_summary)
                    return {
                        'ok': True, 'target': agent_id, 'agentId': agent_id, 'shopId': shop_id,
                        'modeResolved': 'delegate', 'delegationStatus': 'completed', 'async': False,
                        'reply': reply, 'turnId': turn_id,
                        'delegations': orchestrated.get('delegations') if isinstance(orchestrated.get('delegations'), list) else [],
                        'reconciliation': reconciliation, 'message': appended['message'],
                        'runtimeResolved': 'umbrella-agent-runtime', **provider_meta,
                    }

                # Async path (default): acknowledge now, run the delegation in the
                # background, and post the result to the session when it completes.
                ack = compact_text(str(direct.get('reply', '')).strip()) or f"On it — delegating to {plan[0]['shopId']}. I'll post the result here when it's done."

                def _run_delegation() -> None:
                    try:
                        orchestrated = self.orchestrate_turn(session_id=session_id, turn_id=turn_id, plan=plan, metadata={'source': 'session-converse', 'allowDelegation': True})
                        reconciliation = orchestrated.get('reconciliation') if isinstance(orchestrated.get('reconciliation'), dict) else {}
                        summary = str(reconciliation.get('summary', '')).strip() or 'The delegated work is complete.'
                        delegation_ids = [str(r.get('delegationId', '')).strip() for r in (orchestrated.get('delegations') or []) if isinstance(r, dict)]
                        status = 'completed'
                    except Exception as ex:  # noqa: BLE001 — record the failure as a message
                        summary, delegation_ids, status = f'The delegated work failed: {ex}', [], 'failed'

                    def _finish(fresh: dict) -> dict:
                        self._append_message_row(
                            fresh, 'assistant', summary,
                            {'targetAgentId': agent_id, 'targetShopId': shop_id, 'modeResolved': 'delegate',
                             'delegationStatus': status, 'turnId': turn_id, 'delegationIds': delegation_ids,
                             'async': True, **provider_meta},
                        )
                        return fresh

                    try:
                        self.store.update(session_id, _finish)
                    except Exception:  # noqa: BLE001 — best-effort result posting
                        pass

                appended = {}

                def _append_ack(fresh: dict) -> dict:
                    appended['message'] = self._append_message_row(
                        fresh, 'assistant', ack,
                        {'targetAgentId': agent_id, 'targetShopId': shop_id, 'modeResolved': 'delegate',
                         'delegationStatus': 'running', 'turnId': turn_id, 'async': True, **provider_meta},
                    )
                    return fresh

                session = self.store.update(session_id, _append_ack)
                threading.Thread(target=_run_delegation, name=f'delegation-{turn_id}', daemon=True).start()
                return {
                    'ok': True, 'target': agent_id, 'agentId': agent_id, 'shopId': shop_id,
                    'modeResolved': 'delegate', 'delegationStatus': 'running', 'async': True,
                    'reply': ack, 'turnId': turn_id, 'delegations': [],
                    'message': appended['message'], 'runtimeResolved': 'umbrella-agent-runtime', **provider_meta,
                }
            reply, reply_error = self._conversation_reply_or_error(
                direct,
                fallback_error='The mayor conversation returned an empty reply',
            )
            if reply_error:
                return {
                    'ok': False,
                    'target': target,
                    'agentId': agent_id,
                    'shopId': shop_id,
                    'error': {'message': reply_error},
                    'invocation': direct.get('invocation'),
                }
            appended_direct: dict = {}

            def _append_direct(fresh: dict) -> dict:
                appended_direct['message'] = self._append_message_row(
                    fresh,
                    'assistant',
                    reply,
                    {
                        'targetAgentId': agent_id,
                        'targetShopId': shop_id,
                        'modeResolved': 'direct',
                        'actionId': direct.get('actionId', ''),
                        'providerUsed': bool(direct.get('providerUsed', False)),
                        'providerType': str(direct.get('providerType', '')).strip(),
                        'connectionUsed': str(direct.get('connectionUsed', '')).strip(),
                        'modelUsed': str(direct.get('modelUsed', '')).strip(),
                        'fallbackUsed': bool(direct.get('fallbackUsed', False)),
                        'latencyMs': int(direct.get('latencyMs', 0) or 0),
                    },
                )
                return fresh

            session = self.store.update(session_id, _append_direct)
            message = appended_direct['message']
            return {
                'ok': True,
                'target': agent_id,
                'agentId': agent_id,
                'shopId': shop_id,
                'modeResolved': 'direct',
                'reply': reply,
                'questions': direct.get('questions') if isinstance(direct.get('questions'), list) else [],
                'invocation': direct.get('invocation'),
                'message': message,
                'runtimeResolved': 'umbrella-agent-runtime',
                'providerUsed': bool(direct.get('providerUsed', False)),
                'providerType': str(direct.get('providerType', '')).strip(),
                'connectionUsed': str(direct.get('connectionUsed', '')).strip(),
                'modelUsed': str(direct.get('modelUsed', '')).strip(),
                'fallbackUsed': bool(direct.get('fallbackUsed', False)),
                'latencyMs': int(direct.get('latencyMs', 0) or 0),
            }

        direct = self._converse_direct(
            session_id,
            session=session,
            target=target,
            agent_id=agent_id,
            shop_id=shop_id,
            shop=shop,
            runtime_mode=mode or 'direct',
            content=content,
            system_prompt=prompt_text,
            instructions='Respond directly and concisely.',
        )
        if not direct.get('ok', False):
            return {'ok': False, 'target': target, 'agentId': agent_id, 'shopId': shop_id, 'error': {'message': 'conversation action failed'}, 'invocation': direct.get('invocation')}
        reply, reply_error = self._conversation_reply_or_error(
            direct,
            fallback_error='The conversation action returned an empty reply',
        )
        if reply_error:
            return {
                'ok': False,
                'target': target,
                'agentId': agent_id,
                'shopId': shop_id,
                'error': {'message': reply_error},
                'invocation': direct.get('invocation'),
            }
        appended_worker: dict = {}

        def _append_worker(fresh: dict) -> dict:
            appended_worker['message'] = self._append_message_row(
                fresh,
                'assistant',
                reply,
                {
                    'targetAgentId': agent_id,
                    'targetShopId': shop_id,
                    'modeResolved': str(direct.get('mode', 'direct')).strip() or 'direct',
                    'actionId': direct.get('actionId', ''),
                    'providerUsed': bool(direct.get('providerUsed', False)),
                    'providerType': str(direct.get('providerType', '')).strip(),
                    'connectionUsed': str(direct.get('connectionUsed', '')).strip(),
                    'modelUsed': str(direct.get('modelUsed', '')).strip(),
                    'fallbackUsed': bool(direct.get('fallbackUsed', False)),
                    'latencyMs': int(direct.get('latencyMs', 0) or 0),
                },
            )
            return fresh

        session = self.store.update(session_id, _append_worker)
        message = appended_worker['message']
        return {
            'ok': True,
            'target': agent_id,
            'agentId': agent_id,
            'shopId': shop_id,
            'modeResolved': str(direct.get('mode', 'direct')).strip() or 'direct',
            'reply': reply,
            'invocation': direct.get('invocation'),
            'message': message,
            'runtimeResolved': 'umbrella-agent-runtime',
            'providerUsed': bool(direct.get('providerUsed', False)),
            'providerType': str(direct.get('providerType', '')).strip(),
            'connectionUsed': str(direct.get('connectionUsed', '')).strip(),
            'modelUsed': str(direct.get('modelUsed', '')).strip(),
            'fallbackUsed': bool(direct.get('fallbackUsed', False)),
            'latencyMs': int(direct.get('latencyMs', 0) or 0),
        }

    def heartbeat_session(self, session_id: str, *, seen_by: str = 'system') -> dict:
        session_id = validate_identifier(session_id, 'sessionId')
        seen_by = str(seen_by or '').strip() or 'system'
        if seen_by not in {'mayor', 'originator', 'worker', 'system'}:
            raise ValueError('seenBy must be one of mayor, originator, worker, system')
        session = self.store.update(session_id, lambda fresh: self._touch_session(fresh, seen_by=seen_by))
        return {'ok': True, 'session': self._session_payload(session)}

    def heartbeat_agent(self, session_id: str, agent_id: str, *, seen_by: str) -> dict:
        session_id = validate_identifier(session_id, 'sessionId')
        agent_id = validate_identifier(agent_id, 'agentId')
        seen_by = str(seen_by or '').strip() or 'worker'
        if seen_by not in {'mayor', 'originator', 'worker', 'system'}:
            raise ValueError('seenBy must be one of mayor, originator, worker, system')
        session = self.store.update(
            session_id,
            lambda fresh: self._touch_session(fresh, seen_by=seen_by, touched_agent_id=agent_id),
        )
        payload = self._session_payload(session)
        agents = payload.get('agents') if isinstance(payload.get('agents'), list) else []
        shops = payload.get('shops') if isinstance(payload.get('shops'), dict) else {}
        agent = next((row for row in agents if isinstance(row, dict) and row.get('agentId') == agent_id), None)
        shop = shops.get(str(agent.get('shopId', '')).strip()) if isinstance(agent, dict) else None
        return {'ok': True, 'session': payload, 'agent': agent, 'shop': shop}

    def create_sub_agent(
        self,
        session_id: str,
        *,
        created_by_agent_id: str,
        sub_agent_id: str,
        agent_id: str = '',
        shop_id: str = '',
        shop_profile_id: str = '',
        role: str = '',
        assignment_policy: dict | None = None,
        metadata: dict | None = None,
    ) -> dict:
        session_id = validate_identifier(session_id, 'sessionId')
        created: dict = {}

        def _create(session: dict) -> dict:
            self._assert_session_available(session)
            creator_id = self._ensure_authorized_creator(session, created_by_agent_id)
            resolved_sub_agent_id = validate_identifier(sub_agent_id or f'sub-agent-{uuid.uuid4().hex[:12]}', 'subAgentId')
            if resolved_sub_agent_id in self._sub_agent_map(session):
                raise ValueError('sub-agent already exists in session')
            resolved_shop_id, shop, agent = self._resolve_sub_agent_target(
                session,
                agent_id=agent_id,
                shop_id=shop_id,
                shop_profile_id=shop_profile_id,
                role=role,
            )
            created_at = now_iso()
            sub_agent = {
                'subAgentId': resolved_sub_agent_id,
                'agentId': str(agent.get('agentId', '')).strip(),
                'role': str(agent.get('role', '')).strip(),
                'shopId': resolved_shop_id,
                'shopProfileId': str(shop.get('shopProfileId', '')).strip(),
                'state': 'idle',
                'parentSessionId': session_id,
                'createdByAgentId': creator_id,
                'assignmentPolicy': assignment_policy if isinstance(assignment_policy, dict) else {},
                'subAgentMemory': {},
                'metadata': metadata if isinstance(metadata, dict) else {},
                'createdAt': created_at,
                'lastHeartbeatAt': created_at,
                'lastSeenBy': self._creator_role(session, creator_id),
            }
            sub_agents = session.get('subAgents') if isinstance(session.get('subAgents'), list) else []
            sub_agents.append(sub_agent)
            session['subAgents'] = sub_agents
            created['subAgent'] = sub_agent
            return session

        session = self.store.update(session_id, _create)
        return {'ok': True, 'session': self._session_payload(session), 'subAgent': created['subAgent']}

    def list_sub_agents(self, session_id: str) -> dict:
        session = self.get_session(session_id)
        if not session:
            raise ValueError('session not found')
        return {'subAgents': session.get('subAgents', [])}

    def heartbeat_sub_agent(self, session_id: str, sub_agent_id: str, *, seen_by: str) -> dict:
        session_id = validate_identifier(session_id, 'sessionId')
        sub_agent_id = validate_identifier(sub_agent_id, 'subAgentId')
        seen_by = str(seen_by or '').strip() or 'worker'
        if seen_by not in {'mayor', 'originator', 'worker', 'system'}:
            raise ValueError('seenBy must be one of mayor, originator, worker, system')
        def _touch(session: dict) -> dict:
            sub_agents = session.get('subAgents') if isinstance(session.get('subAgents'), list) else []
            sub_agent = next((row for row in sub_agents if isinstance(row, dict) and row.get('subAgentId') == sub_agent_id), None)
            if not sub_agent:
                raise ValueError('sub-agent not found')
            self._touch_session(session, seen_by=seen_by, touched_agent_id=str(sub_agent.get('agentId', '')).strip())
            sub_agent['lastHeartbeatAt'] = now_iso()
            sub_agent['lastSeenBy'] = seen_by
            return session

        session = self.store.update(session_id, _touch)
        payload = self._session_payload(session)
        sub_agents_payload = payload.get('subAgents') if isinstance(payload.get('subAgents'), list) else []
        refreshed_sub_agent = next((row for row in sub_agents_payload if isinstance(row, dict) and row.get('subAgentId') == sub_agent_id), None)
        return {'ok': True, 'session': payload, 'subAgent': refreshed_sub_agent}

    def create_assignment(
        self,
        session_id: str,
        *,
        created_by_agent_id: str,
        sub_agent_id: str,
        objective: str,
        action_id: str,
        inputs: dict | None = None,
        turn_id: str = '',
        depends_on: list[str] | None = None,
        metadata: dict | None = None,
    ) -> dict:
        session_id = validate_identifier(session_id, 'sessionId')
        session = self.store.get(session_id)
        if not session:
            raise ValueError('session not found')
        self._assert_session_available(session)
        creator_id = self._ensure_authorized_creator(session, created_by_agent_id)
        sub_agent_id = validate_identifier(sub_agent_id, 'subAgentId')
        action_id = str(action_id or '').strip()
        if not action_id:
            raise ValueError('actionId is required')
        sub_agents = session.get('subAgents') if isinstance(session.get('subAgents'), list) else []
        sub_agent = next((row for row in sub_agents if isinstance(row, dict) and row.get('subAgentId') == sub_agent_id), None)
        if not sub_agent:
            raise ValueError('sub-agent not found')
        if turn_id:
            turn_id = validate_identifier(turn_id, 'turnId')
        else:
            turn_out = self.create_turn(session_id=session_id, objective=objective, requested_by=creator_id, metadata={'source': 'sub-agent-assignment'})
            turn = turn_out.get('turn') if isinstance(turn_out.get('turn'), dict) else {}
            turn_id = str(turn.get('turnId', '')).strip()
        assignment_id = validate_identifier(f'assignment-{uuid.uuid4().hex[:12]}', 'assignmentId')
        assigned: dict = {}

        def _assign(fresh: dict) -> dict:
            fresh_sub_agents = fresh.get('subAgents') if isinstance(fresh.get('subAgents'), list) else []
            target = next((row for row in fresh_sub_agents if isinstance(row, dict) and row.get('subAgentId') == sub_agent_id), sub_agent)
            assignment = {
                'assignmentId': assignment_id,
                'turnId': turn_id,
                'subAgentId': sub_agent_id,
                'agentId': str(target.get('agentId', '')).strip(),
                'shopId': str(target.get('shopId', '')).strip(),
                'objective': str(objective or '').strip(),
                'status': 'assigned',
                'dependsOn': [validate_identifier(str(x).strip(), 'assignmentId') for x in (depends_on or []) if str(x).strip()],
                'resultRefs': {},
                'createdByAgentId': creator_id,
                'createdAt': now_iso(),
                'metadata': metadata if isinstance(metadata, dict) else {},
            }
            fresh_assignments = fresh.get('assignments') if isinstance(fresh.get('assignments'), list) else []
            fresh_assignments.append(assignment)
            target['state'] = 'running'
            target['lastAssignmentId'] = assignment_id
            fresh['assignments'] = fresh_assignments
            fresh['subAgents'] = fresh_sub_agents
            assigned['assignment'] = assignment
            assigned['subAgent'] = target
            return fresh

        self.store.update(session_id, _assign)
        result = self.delegate_turn(
            session_id=session_id,
            turn_id=turn_id,
            shop_id=str(assigned['subAgent'].get('shopId', '')).strip(),
            shop_profile_id='',
            role='',
            action_id=action_id,
            inputs=inputs if isinstance(inputs, dict) else {},
            metadata={
                **(metadata if isinstance(metadata, dict) else {}),
                'subAgentId': sub_agent_id,
                'assignmentId': assignment_id,
            },
        )
        invocation = result.get('invocation') if isinstance(result.get('invocation'), dict) else {}
        delegation = result.get('delegation') if isinstance(result.get('delegation'), dict) else {}
        finalized: dict = {}

        def _finalize(fresh: dict) -> dict:
            fresh_sub_agents = fresh.get('subAgents') if isinstance(fresh.get('subAgents'), list) else []
            fresh_assignments = fresh.get('assignments') if isinstance(fresh.get('assignments'), list) else []
            target = next((row for row in fresh_sub_agents if isinstance(row, dict) and row.get('subAgentId') == sub_agent_id), assigned['subAgent'])
            assignment = next((row for row in fresh_assignments if isinstance(row, dict) and row.get('assignmentId') == assignment_id), assigned['assignment'])
            assignment['status'] = 'completed' if bool(result.get('ok', False)) else 'failed'
            assignment['completedAt'] = now_iso()
            assignment['resultRefs'] = {
                'turnId': turn_id,
                'delegationId': str(delegation.get('delegationId', '')).strip(),
                'invocationId': str(invocation.get('invocationId', '')).strip(),
            }
            target['state'] = 'completed' if bool(result.get('ok', False)) else 'failed'
            target['lastResultRefs'] = assignment['resultRefs']
            fresh['subAgents'] = fresh_sub_agents
            fresh['assignments'] = fresh_assignments
            finalized['assignment'] = assignment
            finalized['subAgent'] = target
            return fresh

        session = self.store.update(session_id, _finalize)
        return {
            'ok': bool(result.get('ok', False)),
            'session': self._session_payload(session),
            'subAgent': finalized['subAgent'],
            'assignment': finalized['assignment'],
            'delegation': delegation,
            'invocation': invocation,
        }

    def get_assignment(self, session_id: str, assignment_id: str) -> dict | None:
        session_id = validate_identifier(session_id, 'sessionId')
        assignment_id = validate_identifier(assignment_id, 'assignmentId')
        session = self.get_session(session_id)
        if not session:
            return None
        assignments = session.get('assignments') if isinstance(session.get('assignments'), list) else []
        return next((row for row in assignments if isinstance(row, dict) and row.get('assignmentId') == assignment_id), None)

    def register_worker(
        self,
        session_id: str,
        created_by_agent_id: str,
        agent_id: str,
        role: str,
        title: str,
        shop_id: str,
        shop_name: str,
        agent_package_id: str = '',
        shop_profile_id: str = '',
        enabled_action_ids: list[str] | None = None,
        metadata: dict | None = None,
    ) -> dict:
        session_id = validate_identifier(session_id, 'sessionId')
        registered: dict = {}

        def _register(session: dict) -> dict:
            self._assert_session_available(session)
            creator_id = str(created_by_agent_id or '').strip() or str(session.get('mayorAgentId', '')).strip()
            creator_id = self._ensure_authorized_creator(session, creator_id)
            worker_agent_id = validate_identifier(agent_id, 'agentId')
            worker_shop_id = validate_identifier(shop_id, 'shopId')
            package = self._resolve_agent_package(agent_package_id)
            package_profile_id = str(package.get('shopProfileId', '')).strip() if package else ''
            resolved_profile_id = str(shop_profile_id or '').strip() or package_profile_id
            profile = self._resolve_shop_profile(resolved_profile_id)
            agents = session.get('agents') if isinstance(session.get('agents'), list) else []
            if any(isinstance(agent, dict) and agent.get('agentId') == worker_agent_id for agent in agents):
                raise ValueError('agent already registered in session')
            shops = session.get('shops') if isinstance(session.get('shops'), dict) else {}
            if worker_shop_id in shops:
                raise ValueError('shop already exists in session')
            package_role = str(package.get('role', '')).strip() if package else ''
            package_title = str(package.get('defaultTitle', '')).strip() if package else ''
            package_shop_name = str(package.get('defaultShopName', '')).strip() if package else ''
            package_enabled_action_ids = package.get('enabledActionIds') if isinstance(package, dict) and isinstance(package.get('enabledActionIds'), list) else []
            package_metadata = package.get('metadata') if isinstance(package, dict) and isinstance(package.get('metadata'), dict) else {}
            resolved_role = str(role or '').strip() or package_role or 'worker'
            resolved_title = str(title or '').strip() or package_title or (str(profile.get('defaultTitle', '')).strip() if profile else '') or 'Worker'
            resolved_shop_name = str(shop_name or '').strip() or package_shop_name or (str(profile.get('defaultShopName', '')).strip() if profile else '') or worker_shop_id
            profile_enabled_action_ids = profile.get('enabledActionIds') if isinstance(profile, dict) and isinstance(profile.get('enabledActionIds'), list) else []
            resolved_enabled_action_ids = [
                str(x).strip()
                for x in (enabled_action_ids if enabled_action_ids is not None else (package_enabled_action_ids or profile_enabled_action_ids)) or []
                if str(x).strip()
            ]
            profile_metadata = profile.get('metadata') if isinstance(profile, dict) and isinstance(profile.get('metadata'), dict) else {}
            resolved_metadata = {**profile_metadata, **package_metadata, **(metadata or {})}
            resolved_shop_type = (str(profile.get('shopType', '')).strip() if profile else '') or (str(package.get('shopType', '')).strip() if package else '') or 'business'
            worker = {
                'agentId': worker_agent_id,
                'role': resolved_role,
                'title': resolved_title,
                'shopId': worker_shop_id,
                'agentPackageId': str(package.get('packageId', '')).strip() if package else '',
                'shopProfileId': str(profile.get('profileId', '')).strip() if profile else '',
                'createdByAgentId': creator_id,
                'createdAt': now_iso(),
                'lastHeartbeatAt': now_iso(),
                'lastSeenBy': creator_id,
            }
            shop = {
                'shopId': worker_shop_id,
                'name': resolved_shop_name,
                'ownerAgentId': worker_agent_id,
                'shopType': resolved_shop_type,
                'agentPackageId': str(package.get('packageId', '')).strip() if package else '',
                'shopProfileId': str(profile.get('profileId', '')).strip() if profile else '',
                'enabledActionIds': resolved_enabled_action_ids,
                'actionGovernance': self._build_action_governance(
                    resolved_enabled_action_ids,
                    creator_id,
                    source='shop-profile-origination' if profile else 'shop-origination',
                ),
                'metadata': {
                    **resolved_metadata,
                    'createdByAgentId': creator_id,
                    'runtimeId': str(package.get('runtimeId', '')).strip() if package else '',
                    'capabilityFamilies': package.get('capabilityFamilies') if isinstance(package, dict) and isinstance(package.get('capabilityFamilies'), list) else [],
                },
                'createdAt': now_iso(),
                'lastHeartbeatAt': now_iso(),
                'lastSeenBy': creator_id,
            }
            agents.append(worker)
            shops[worker_shop_id] = shop
            session['agents'] = agents
            session['shops'] = shops
            registered['agent'] = worker
            registered['shop'] = shop
            return session

        session = self.store.update(session_id, _register)
        return {'ok': True, 'session': self._session_payload(session), 'agent': registered['agent'], 'shop': registered['shop']}

    def originate_worker(
        self,
        session_id: str,
        originator_agent_id: str,
        agent_id: str,
        role: str,
        title: str,
        shop_id: str,
        shop_name: str,
        agent_package_id: str = '',
        shop_profile_id: str = '',
        enabled_action_ids: list[str] | None = None,
        metadata: dict | None = None,
    ) -> dict:
        return self.register_worker(
            session_id=session_id,
            created_by_agent_id=originator_agent_id,
            agent_id=agent_id,
            role=role,
            title=title,
            shop_id=shop_id,
            shop_name=shop_name,
            agent_package_id=agent_package_id,
            shop_profile_id=shop_profile_id,
            enabled_action_ids=enabled_action_ids,
            metadata=metadata,
        )

    def set_shop_action_state(
        self,
        session_id: str,
        shop_id: str,
        action_id: str,
        *,
        managed_by_agent_id: str,
        enabled: bool,
        installed: bool | None = None,
        metadata: dict | None = None,
    ) -> dict:
        session_id = validate_identifier(session_id, 'sessionId')
        session = self.store.get(session_id)
        if not session:
            raise ValueError('session not found')
        self._assert_session_available(session)
        shop_id = validate_identifier(shop_id, 'shopId')
        action_id = str(action_id or '').strip()
        if not action_id:
            raise ValueError('actionId is required')
        catalog_actions = self._catalog_action_map()
        if action_id not in catalog_actions:
            raise ValueError('action is not present in the catalog')
        governed: dict = {}

        def _govern(fresh: dict) -> dict:
            shops = fresh.get('shops') if isinstance(fresh.get('shops'), dict) else {}
            shop = shops.get(shop_id) if isinstance(shops.get(shop_id), dict) else None
            if not shop:
                raise ValueError('shop not found')
            manager_id = self._assert_shop_manager(fresh, shop, managed_by_agent_id)
            action_governance = shop.get('actionGovernance') if isinstance(shop.get('actionGovernance'), dict) else {}
            previous = action_governance.get(action_id) if isinstance(action_governance.get(action_id), dict) else {}
            action_governance[action_id] = {
                'actionId': action_id,
                'installed': bool(previous.get('installed', False)) if installed is None else bool(installed),
                'enabled': bool(enabled),
                'managedByAgentId': manager_id,
                'managedAt': now_iso(),
                'source': str((metadata or {}).get('source', 'shop-governance')).strip() or 'shop-governance',
                'metadata': metadata or {},
            }
            if enabled and not action_governance[action_id]['installed']:
                action_governance[action_id]['installed'] = True
            shop['actionGovernance'] = action_governance
            shop['enabledActionIds'] = sorted(
                {
                    action_key
                    for action_key, row in action_governance.items()
                    if isinstance(row, dict) and bool(row.get('installed', False)) and bool(row.get('enabled', False))
                }
            )
            shops[shop_id] = shop
            fresh['shops'] = shops
            governed['shop'] = shop
            governed['actionState'] = action_governance[action_id]
            return fresh

        session = self.store.update(session_id, _govern)
        payload = self._session_payload(session)
        return {
            'ok': True,
            'session': payload,
            'shop': (payload.get('shops') or {}).get(shop_id, governed['shop']),
            'actionState': governed['actionState'],
        }

    def create_turn(self, session_id: str, objective: str, requested_by: str, metadata: dict | None = None) -> dict:
        session_id = validate_identifier(session_id, 'sessionId')
        created: dict = {}

        def _create(session: dict) -> dict:
            self._assert_session_available(session)
            turn = {
                'turnId': validate_identifier(f'turn-{uuid.uuid4().hex[:12]}', 'turnId'),
                'objective': str(objective or '').strip(),
                'requestedBy': str(requested_by or '').strip() or 'user',
                'state': 'OPEN',
                'orchestrationMode': 'manual',
                'delegationIds': [],
                'reconciliation': None,
                'metadata': metadata or {},
                'createdAt': now_iso(),
            }
            turns = session.get('turns') if isinstance(session.get('turns'), list) else []
            turns.append(turn)
            session['turns'] = turns
            created['turn'] = turn
            return session

        session = self.store.update(session_id, _create)
        return {'ok': True, 'session': self._session_payload(session), 'turn': created['turn']}

    def invoke_action(
        self,
        session_id: str,
        action_id: str,
        inputs: dict | None = None,
        metadata: dict | None = None,
        *,
        shop_id: str = '',
        turn_id: str = '',
        delegation_id: str = '',
    ) -> dict:
        session_id = validate_identifier(session_id, 'sessionId')
        session = self.store.get(session_id)
        if not session:
            raise ValueError('session not found')
        allow_expired = bool((metadata or {}).get('allowExpiredSession', False))
        self._assert_session_available(session, allow_expired=allow_expired)
        action_id = str(action_id or '').strip()
        if not action_id:
            raise ValueError('actionId is required')
        original_action_id, resolved_action_id = self._resolve_action_alias(action_id)
        shops = session.get('shops') if isinstance(session.get('shops'), dict) else {}
        target_shop_id = validate_identifier(shop_id or self._town_hall_shop_id(session), 'shopId')
        shop = shops.get(target_shop_id) if isinstance(shops.get(target_shop_id), dict) else None
        if not shop:
            raise ValueError('shop not found')
        shop_actions = self._shop_actions(session).get(target_shop_id, [])
        if resolved_action_id not in {str(a.get('id', '')).strip() for a in shop_actions}:
            raise ValueError('action is not enabled in target shop')
        executing_agent_id = str(shop.get('ownerAgentId', '')).strip() or str(session.get('mayorAgentId', '')).strip()
        invocation_id = f'invoke-{uuid.uuid4().hex[:12]}'
        run_id = validate_identifier(f'{session_id}-{invocation_id}', 'runId')
        step_id = validate_identifier(invocation_id, 'stepId')
        step_spec = {
            'stepId': step_id,
            'action': original_action_id,
            'runtime': 'umbrella-agent-runtime',
            'inputs': inputs if isinstance(inputs, dict) else {},
            'timeoutSec': self._resolved_timeout_sec(resolved_action_id, metadata),
            'metadata': {
                **(metadata if isinstance(metadata, dict) else {}),
                'runtimeRequested': self._requested_runtime(metadata) or 'umbrella-agent-runtime',
                'originalActionId': original_action_id,
                'resolvedActionId': resolved_action_id,
                'agentId': executing_agent_id,
                'sessionId': session_id,
                'shopId': target_shop_id,
                'delegatedByAgentId': str(session.get('mayorAgentId', '')).strip(),
                'turnId': turn_id,
                'delegationId': delegation_id,
            },
        }
        try:
            execution = self._post_json(
                self.execution_url,
                '/v1/execution/submit-step-spec',
                {'runId': run_id, 'stepId': step_id, 'stepSpec': step_spec},
                timeout=int(step_spec['timeoutSec']) + 5,
            )
        except urllib.error.URLError as ex:
            raise RuntimeError(str(ex)) from ex
        except urllib.error.HTTPError as ex:
            body = ''
            try:
                body = ex.read().decode('utf-8')
            except Exception:
                body = ''
            raise RuntimeError(f'HTTP {ex.code}: {body or ex.reason}') from ex

        runtime = self._resolved_runtime(resolved_action_id, metadata)
        invocation = {
            'invocationId': invocation_id,
            'actionId': original_action_id,
            'resolvedActionId': resolved_action_id,
            'shopId': target_shop_id,
            'executingAgentId': executing_agent_id,
            'turnId': turn_id,
            'delegationId': delegation_id,
            'inputs': inputs if isinstance(inputs, dict) else {},
            **runtime,
            'result': execution,
            'createdAt': now_iso(),
        }
        def _record(fresh: dict) -> dict:
            self._apply_liveness(fresh)
            invocations = fresh.get('invocations') if isinstance(fresh.get('invocations'), list) else []
            invocations.append(invocation)
            fresh['invocations'] = invocations
            if bool(execution.get('ok', False)):
                plugin_result = (((execution.get('result') or {}).get('pluginResult')) or {})
                content = json.dumps(plugin_result, ensure_ascii=False)
                messages = fresh.get('messages') if isinstance(fresh.get('messages'), list) else []
                messages.append(
                    {
                        'messageId': f'msg-{uuid.uuid4().hex[:12]}',
                        'role': 'tool',
                        'content': content,
                        'metadata': {'actionId': original_action_id, 'resolvedActionId': resolved_action_id, 'invocationId': invocation_id, 'shopId': target_shop_id, 'executingAgentId': executing_agent_id, **runtime},
                        'createdAt': now_iso(),
                    }
                )
                fresh['messages'] = messages
            return fresh

        session = self.store.update(session_id, _record)
        return {'ok': bool(execution.get('ok', False)), 'session': self._session_payload(session), 'invocation': invocation}

    def delegate_turn(
        self,
        session_id: str,
        turn_id: str,
        shop_id: str,
        shop_profile_id: str,
        role: str,
        action_id: str,
        inputs: dict | None = None,
        metadata: dict | None = None,
    ) -> dict:
        session_id = validate_identifier(session_id, 'sessionId')
        session = self.store.get(session_id)
        if not session:
            raise ValueError('session not found')
        allow_expired = bool((metadata or {}).get('allowExpiredSession', False))
        self._assert_session_available(session, allow_expired=allow_expired)
        turn_id = validate_identifier(turn_id, 'turnId')
        started: dict = {}

        def _start(fresh: dict) -> dict:
            self._apply_liveness(fresh)
            turns = fresh.get('turns') if isinstance(fresh.get('turns'), list) else []
            turn = next((t for t in turns if isinstance(t, dict) and t.get('turnId') == turn_id), None)
            if not turn:
                raise ValueError('turn not found')
            resolved_shop_id, resolved_shop = self._resolve_turn_target(
                fresh,
                shop_id=shop_id,
                shop_profile_id=shop_profile_id,
                role=role,
            )
            delegation_id = validate_identifier(f'delegation-{uuid.uuid4().hex[:12]}', 'delegationId')
            delegation = {
                'delegationId': delegation_id,
                'turnId': turn_id,
                'delegatedByAgentId': str(fresh.get('mayorAgentId', '')).strip(),
                'shopId': resolved_shop_id,
                'requestedShopId': str(shop_id or '').strip(),
                'requestedShopProfileId': str(shop_profile_id or '').strip(),
                'requestedRole': str(role or '').strip(),
                'resolvedShopProfileId': str(resolved_shop.get('shopProfileId', '')).strip(),
                'resolvedOwnerAgentId': str(resolved_shop.get('ownerAgentId', '')).strip(),
                'planStepId': str((metadata or {}).get('planStepId', '')).strip(),
                'dependsOn': [str(x).strip() for x in ((metadata or {}).get('dependsOn') or []) if str(x).strip()],
                'actionId': str(action_id or '').strip(),
                'resolvedActionId': self._resolve_action_alias(str(action_id or '').strip())[1],
                'runtimeRequested': self._requested_runtime(metadata),
                'runtimeResolved': 'umbrella-agent-runtime',
                'runtimeClass': 'umbrella-agent-runtime',
                'runtimeReason': 'delegated_shop_action',
                'state': 'IN_PROGRESS',
                'createdAt': now_iso(),
                'metadata': metadata or {},
            }
            delegations = fresh.get('delegations') if isinstance(fresh.get('delegations'), list) else []
            delegations.append(delegation)
            fresh['delegations'] = delegations
            turn['state'] = 'IN_PROGRESS'
            delegation_ids = turn.get('delegationIds') if isinstance(turn.get('delegationIds'), list) else []
            delegation_ids.append(delegation_id)
            turn['delegationIds'] = delegation_ids
            started['turn'] = turn
            started['delegation'] = delegation
            started['resolvedShopId'] = resolved_shop_id
            return fresh

        self.store.update(session_id, _start)
        delegation_id = str(started['delegation'].get('delegationId', ''))
        resolved_shop_id = started['resolvedShopId']
        result = self.invoke_action(
            session_id=session_id,
            action_id=action_id,
            inputs=inputs,
            metadata=metadata,
            shop_id=resolved_shop_id,
            turn_id=turn_id,
            delegation_id=delegation_id,
        )
        finalized: dict = {}

        def _finalize(fresh: dict) -> dict:
            turns = fresh.get('turns') if isinstance(fresh.get('turns'), list) else []
            delegations = fresh.get('delegations') if isinstance(fresh.get('delegations'), list) else []
            turn = next((t for t in turns if isinstance(t, dict) and t.get('turnId') == turn_id), started['turn'])
            delegation = next((d for d in delegations if isinstance(d, dict) and d.get('delegationId') == delegation_id), started['delegation'])
            delegation['state'] = 'COMPLETED' if bool(result.get('ok', False)) else 'FAILED'
            delegation['completedAt'] = now_iso()
            turn['state'] = 'COMPLETED' if bool(result.get('ok', False)) else 'FAILED'
            fresh['turns'] = turns
            fresh['delegations'] = delegations
            finalized['delegation'] = delegation
            return fresh

        session = self.store.update(session_id, _finalize)
        return {'ok': bool(result.get('ok', False)), 'session': self._session_payload(session), 'delegation': finalized['delegation'], 'invocation': result.get('invocation')}

    def orchestrate_turn(
        self,
        session_id: str,
        turn_id: str,
        plan: list[dict],
        metadata: dict | None = None,
    ) -> dict:
        session_id = validate_identifier(session_id, 'sessionId')
        session = self.store.get(session_id)
        if not session:
            raise ValueError('session not found')
        allow_expired = bool((metadata or {}).get('allowExpiredSession', False))
        self._assert_session_available(session, allow_expired=allow_expired)
        turn_id = validate_identifier(turn_id, 'turnId')
        orchestration_id = validate_identifier(f'orchestration-{uuid.uuid4().hex[:12]}', 'orchestrationId')
        started: dict = {}

        def _start(fresh: dict) -> dict:
            self._apply_liveness(fresh)
            turns = fresh.get('turns') if isinstance(fresh.get('turns'), list) else []
            turn = next((t for t in turns if isinstance(t, dict) and t.get('turnId') == turn_id), None)
            if not turn:
                raise ValueError('turn not found')
            if not isinstance(plan, list) or not plan:
                raise ValueError('plan must include at least one delegation')
            turn['state'] = 'IN_PROGRESS'
            turn['orchestrationMode'] = 'fanout'
            turn['orchestrationId'] = orchestration_id
            turn['orchestrationMetadata'] = metadata or {}
            started['turn'] = turn
            return fresh

        self.store.update(session_id, _start)

        delegation_results: list[dict] = []
        invocation_results: list[dict] = []
        completed_steps: dict[str, dict] = {}
        step_status: dict[str, str] = {}
        step_order: list[str] = []
        plan_contains_dependencies = False
        for index, item in enumerate(plan, start=1):
            if not isinstance(item, dict):
                continue
            plan_step_id = validate_identifier(str(item.get('planStepId', '')).strip() or f'plan-step-{index}', 'planStepId')
            if plan_step_id in completed_steps or plan_step_id in step_order:
                raise ValueError(f'duplicate planStepId: {plan_step_id}')
            depends_on = [validate_identifier(str(x).strip(), 'planStepId') for x in (item.get('dependsOn') or []) if str(x).strip()]
            if depends_on:
                plan_contains_dependencies = True
            missing = [dependency for dependency in depends_on if dependency not in step_status]
            if missing:
                raise ValueError(f'plan step dependencies must reference previously completed steps: {", ".join(missing)}')
            dependency_policy = self._dependency_failure_policy(item, metadata)
            retry_budget = self._retry_budget(item, metadata)
            blocked_dependencies = [dependency for dependency in depends_on if step_status.get(dependency) in {'FAILED', 'SKIPPED'}]
            if blocked_dependencies and dependency_policy != 'continue':
                skipped = self._record_skipped_delegation(
                    session_id=session_id,
                    turn_id=turn_id,
                    requested_shop_id=str(item.get('shopId', '')),
                    requested_shop_profile_id=str(item.get('shopProfileId', '')),
                    requested_role=str(item.get('role', '')),
                    action_id=str(item.get('actionId', '')),
                    plan_step_id=plan_step_id,
                    depends_on=depends_on,
                    failure_policy=dependency_policy,
                    reason=f'blocked by dependency state: {", ".join(blocked_dependencies)}',
                    metadata=item.get('metadata') if isinstance(item.get('metadata'), dict) else {},
                )
                delegation_results.append(skipped)
                step_status[plan_step_id] = 'SKIPPED'
                step_order.append(plan_step_id)
                if dependency_policy == 'fail-fast':
                    break
                continue
            resolved_inputs = self._resolve_orchestration_inputs(item, completed_steps)
            result = None
            for attempt_number in range(1, retry_budget + 2):
                result = self.delegate_turn(
                    session_id=session_id,
                    turn_id=turn_id,
                    shop_id=str(item.get('shopId', '')),
                    shop_profile_id=str(item.get('shopProfileId', '')),
                    role=str(item.get('role', '')),
                    action_id=str(item.get('actionId', '')),
                    inputs=resolved_inputs,
                    metadata={
                        **(item.get('metadata') if isinstance(item.get('metadata'), dict) else {}),
                        'planStepId': plan_step_id,
                        'dependsOn': depends_on,
                        'onDependencyFailure': dependency_policy,
                        'retryBudget': retry_budget,
                        'attemptNumber': attempt_number,
                    },
                )
                delegation = result.get('delegation')
                invocation = result.get('invocation')
                if isinstance(delegation, dict):
                    delegation_results.append(delegation)
                    step_status[plan_step_id] = str(delegation.get('state', '')).strip() or ('COMPLETED' if result.get('ok') else 'FAILED')
                if isinstance(invocation, dict):
                    invocation_results.append(invocation)
                    if bool(result.get('ok', False)):
                        completed_steps[plan_step_id] = invocation
                if bool(result.get('ok', False)):
                    break
            step_order.append(plan_step_id)

        completed = [d for d in delegation_results if str(d.get('state', '')).strip() == 'COMPLETED']
        failed = [d for d in delegation_results if str(d.get('state', '')).strip() == 'FAILED']
        skipped = [d for d in delegation_results if str(d.get('state', '')).strip() == 'SKIPPED']
        finished: dict = {}

        def _finish(fresh: dict) -> dict:
            turns = fresh.get('turns') if isinstance(fresh.get('turns'), list) else []
            turn = next((t for t in turns if isinstance(t, dict) and t.get('turnId') == turn_id), started['turn'])
            turn['orchestrationMode'] = 'dependency-graph' if plan_contains_dependencies else 'fanout'
            turn['planStepOrder'] = step_order
            mayor_summary = self._reconciliation_summary(turn, delegation_results, invocation_results)
            reconciliation = {
                'reconciliationId': validate_identifier(f'reconcile-{uuid.uuid4().hex[:12]}', 'reconciliationId'),
                'orchestrationId': orchestration_id,
                'completedDelegations': len(completed),
                'failedDelegations': len(failed),
                'skippedDelegations': len(skipped),
                'summary': mayor_summary,
                'createdAt': now_iso(),
            }
            turn['state'] = 'COMPLETED' if not failed and not skipped else ('PARTIAL' if completed else 'FAILED')
            turn['reconciliation'] = reconciliation
            fresh['turns'] = turns
            messages = fresh.get('messages') if isinstance(fresh.get('messages'), list) else []
            messages.append(
                {
                    'messageId': f'msg-{uuid.uuid4().hex[:12]}',
                    'role': 'assistant',
                    'content': mayor_summary,
                    'metadata': {'turnId': turn_id, 'orchestrationId': orchestration_id, 'kind': 'mayor-summary'},
                    'createdAt': now_iso(),
                }
            )
            fresh['messages'] = messages
            finished['turn'] = turn
            finished['reconciliation'] = reconciliation
            return fresh

        session = self.store.update(session_id, _finish)
        return {
            'ok': not failed,
            'session': self._session_payload(session),
            'turn': finished['turn'],
            'delegations': delegation_results,
            'invocations': invocation_results,
            'reconciliation': finished['reconciliation'],
        }

    def compact_session(self, session_id: str, keep_last_messages: int = 10) -> dict:
        session_id = validate_identifier(session_id, 'sessionId')
        keep_last = max(1, int(keep_last_messages))
        result: dict = {}

        def _compact(session: dict) -> dict:
            messages = session.get('messages') if isinstance(session.get('messages'), list) else []
            if len(messages) <= keep_last:
                compacted = {
                    'compactionId': validate_identifier(f'compact-{uuid.uuid4().hex[:12]}', 'compactionId'),
                    'keptMessages': len(messages),
                    'droppedMessages': 0,
                    'summary': 'no compaction needed',
                    'createdAt': now_iso(),
                }
            else:
                dropped = messages[:-keep_last]
                kept = messages[-keep_last:]
                compacted = {
                    'compactionId': validate_identifier(f'compact-{uuid.uuid4().hex[:12]}', 'compactionId'),
                    'keptMessages': len(kept),
                    'droppedMessages': len(dropped),
                    'summary': f'compacted {len(dropped)} earlier messages into a retained summary record',
                    'createdAt': now_iso(),
                }
                session['messages'] = kept
            compactions = session.get('compactions') if isinstance(session.get('compactions'), list) else []
            compactions.append(compacted)
            session['compactions'] = compactions
            result['compaction'] = compacted
            return session

        session = self.store.update(session_id, _compact)
        return {'ok': True, 'session': self._session_payload(session), 'compaction': result['compaction']}


def handler_factory(engine: SessionEngine, token: str):
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
            if path == '/v1/session/health':
                return json_response(self, 200, {'status': 'ok', 'service': 'session', 'checkedAt': now_iso()})
            if path == '/v1/runtime/model-broker':
                return json_response(self, 200, engine.model_broker_status())
            if path == '/v1/runtime/model-provider':
                return json_response(self, 200, engine.model_provider_status())
            if path == '/v1/shop-profiles':
                return json_response(self, 200, engine.list_shop_profiles())
            if path == '/v1/agent-packages':
                return json_response(self, 200, engine.list_agent_packages())
            if path.startswith('/v1/agent-packages/'):
                package_id = unquote(path[len('/v1/agent-packages/') :].strip('/'))
                try:
                    package = engine.get_agent_package(package_id)
                except ValueError as ex:
                    return json_response(self, 400, err('VALIDATION_ERROR', str(ex), req_id))
                if not package:
                    return json_response(self, 404, err('NOT_FOUND', 'agent package not found', req_id))
                return json_response(self, 200, package)
            if path.startswith('/v1/shop-profiles/'):
                profile_id = unquote(path[len('/v1/shop-profiles/') :].strip('/'))
                try:
                    profile = engine.get_shop_profile(profile_id)
                except ValueError as ex:
                    return json_response(self, 400, err('VALIDATION_ERROR', str(ex), req_id))
                if not profile:
                    return json_response(self, 404, err('NOT_FOUND', 'shop profile not found', req_id))
                return json_response(self, 200, profile)
            prefix = '/v1/sessions/'
            if path.endswith('/sub-agents') and path.startswith(prefix):
                session_id = unquote(path[len(prefix) : -len('/sub-agents')].strip('/'))
                try:
                    out = engine.list_sub_agents(session_id)
                except ValueError as ex:
                    return json_response(self, 400, err('VALIDATION_ERROR', str(ex), req_id))
                return json_response(self, 200, out)
            if '/assignments/' in path and path.startswith(prefix):
                remainder = path[len(prefix):]
                session_id, assignment_suffix = remainder.split('/assignments/', 1)
                try:
                    assignment = engine.get_assignment(session_id.strip('/'), unquote(assignment_suffix.strip('/')))
                except ValueError as ex:
                    return json_response(self, 400, err('VALIDATION_ERROR', str(ex), req_id))
                if not assignment:
                    return json_response(self, 404, err('NOT_FOUND', 'assignment not found', req_id))
                return json_response(self, 200, {'assignment': assignment})
            if path.startswith(prefix):
                session_id = unquote(path[len(prefix):].strip('/'))
                try:
                    session_id = validate_identifier(session_id, 'sessionId')
                except ValueError as ex:
                    return json_response(self, 400, err('VALIDATION_ERROR', str(ex), req_id))
                session = engine.get_session(session_id)
                if not session:
                    return json_response(self, 404, err('NOT_FOUND', 'session not found', req_id))
                return json_response(self, 200, session)
            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def do_POST(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            body = parse_json(self)
            try:
                if path in {'/v1/runtime/model-provider', '/v1/runtime/model-broker'}:
                    out = engine.save_model_provider(
                        enabled=body.get('enabled') if isinstance(body.get('enabled'), bool) else None,
                        provider_payload=body.get('provider') if isinstance(body.get('provider'), dict) else None,
                        agent_defaults=body.get('agentDefaults') if isinstance(body.get('agentDefaults'), dict) else None,
                        api_key=body.get('apiKey') if 'apiKey' in body else None,
                    )
                    return json_response(self, 200, out)
                if path == '/v1/runtime/model-broker/test':
                    return json_response(self, 200, engine.test_model_broker())
                if path == '/v1/runtime/model-provider/test':
                    return json_response(self, 200, engine.test_model_provider())
                if path == '/v1/shop-profiles':
                    out = engine.save_shop_profile(
                        profile_id=str(body.get('profileId', '')),
                        name=str(body.get('name', '')),
                        shop_type=str(body.get('shopType', 'business')),
                        default_title=str(body.get('defaultTitle', 'Worker')),
                        default_shop_name=str(body.get('defaultShopName', '')),
                        enabled_action_ids=body.get('enabledActionIds') if isinstance(body.get('enabledActionIds'), list) else [],
                        metadata=body.get('metadata') if isinstance(body.get('metadata'), dict) else {},
                    )
                    return json_response(self, 200, out)
                if path == '/v1/sessions':
                    out = engine.create_session(
                        agent_id=str(body.get('agentId', '')),
                        title=str(body.get('title', '')),
                        metadata=body.get('metadata') if isinstance(body.get('metadata'), dict) else {},
                    )
                    return json_response(self, 200, {'ok': True, 'session': out})
                if path.endswith('/sub-agents') and path.startswith('/v1/sessions/'):
                    session_id = path[len('/v1/sessions/') : -len('/sub-agents')].strip('/')
                    out = engine.create_sub_agent(
                        session_id=session_id,
                        created_by_agent_id=str(body.get('createdByAgentId', body.get('originatorAgentId', body.get('agentId', '')))),
                        sub_agent_id=str(body.get('subAgentId', '')),
                        agent_id=str(body.get('agentId', '')),
                        shop_id=str(body.get('shopId', '')),
                        shop_profile_id=str(body.get('shopProfileId', '')),
                        role=str(body.get('role', '')),
                        assignment_policy=body.get('assignmentPolicy') if isinstance(body.get('assignmentPolicy'), dict) else {},
                        metadata=body.get('metadata') if isinstance(body.get('metadata'), dict) else {},
                    )
                    return json_response(self, 200, out)
                if path.endswith('/heartbeat') and '/sub-agents/' in path and path.startswith('/v1/sessions/'):
                    prefix = '/v1/sessions/'
                    remainder = path[len(prefix):]
                    session_id, sub_agent_suffix = remainder.split('/sub-agents/', 1)
                    sub_agent_id = sub_agent_suffix[: -len('/heartbeat')].strip('/')
                    out = engine.heartbeat_sub_agent(
                        session_id=session_id.strip('/'),
                        sub_agent_id=sub_agent_id,
                        seen_by=str(body.get('seenBy', 'worker')),
                    )
                    return json_response(self, 200, out)
                if path.endswith('/assignments') and path.startswith('/v1/sessions/'):
                    session_id = path[len('/v1/sessions/') : -len('/assignments')].strip('/')
                    out = engine.create_assignment(
                        session_id=session_id,
                        created_by_agent_id=str(body.get('createdByAgentId', body.get('originatorAgentId', body.get('agentId', '')))),
                        sub_agent_id=str(body.get('subAgentId', '')),
                        objective=str(body.get('objective', '')),
                        action_id=str(body.get('actionId', '')),
                        inputs=body.get('inputs') if isinstance(body.get('inputs'), dict) else {},
                        turn_id=str(body.get('turnId', '')),
                        depends_on=body.get('dependsOn') if isinstance(body.get('dependsOn'), list) else [],
                        metadata=body.get('metadata') if isinstance(body.get('metadata'), dict) else {},
                    )
                    return json_response(self, 200, out)
                if path.endswith('/heartbeat') and '/agents/' in path and path.startswith('/v1/sessions/'):
                    prefix = '/v1/sessions/'
                    remainder = path[len(prefix):]
                    session_id, agent_suffix = remainder.split('/agents/', 1)
                    agent_id = agent_suffix[: -len('/heartbeat')].strip('/')
                    out = engine.heartbeat_agent(
                        session_id=session_id.strip('/'),
                        agent_id=agent_id,
                        seen_by=str(body.get('seenBy', 'worker')),
                    )
                    return json_response(self, 200, out)
                if path.endswith('/heartbeat') and path.startswith('/v1/sessions/'):
                    session_id = path[len('/v1/sessions/') : -len('/heartbeat')].strip('/')
                    out = engine.heartbeat_session(
                        session_id=session_id,
                        seen_by=str(body.get('seenBy', 'system')),
                    )
                    return json_response(self, 200, out)
                if path.endswith('/workers') and path.startswith('/v1/sessions/'):
                    session_id = path[len('/v1/sessions/') : -len('/workers')].strip('/')
                    out = engine.register_worker(
                        session_id=session_id,
                        created_by_agent_id=str(body.get('createdByAgentId', body.get('originatorAgentId', ''))),
                        agent_id=str(body.get('agentId', '')),
                        role=str(body.get('role', '')),
                        title=str(body.get('title', '')),
                        shop_id=str(body.get('shopId', '')),
                        shop_name=str(body.get('shopName', '')),
                        agent_package_id=str(body.get('agentPackageId', '')),
                        shop_profile_id=str(body.get('shopProfileId', '')),
                        enabled_action_ids=body.get('enabledActionIds') if ('enabledActionIds' in body and isinstance(body.get('enabledActionIds'), list)) else None,
                        metadata=body.get('metadata') if isinstance(body.get('metadata'), dict) else {},
                    )
                    return json_response(self, 200, out)
                if path.endswith('/originations') and path.startswith('/v1/sessions/'):
                    session_id = path[len('/v1/sessions/') : -len('/originations')].strip('/')
                    out = engine.originate_worker(
                        session_id=session_id,
                        originator_agent_id=str(body.get('originatorAgentId', 'originator')),
                        agent_id=str(body.get('agentId', '')),
                        role=str(body.get('role', '')),
                        title=str(body.get('title', '')),
                        shop_id=str(body.get('shopId', '')),
                        shop_name=str(body.get('shopName', '')),
                        agent_package_id=str(body.get('agentPackageId', '')),
                        shop_profile_id=str(body.get('shopProfileId', '')),
                        enabled_action_ids=body.get('enabledActionIds') if ('enabledActionIds' in body and isinstance(body.get('enabledActionIds'), list)) else None,
                        metadata=body.get('metadata') if isinstance(body.get('metadata'), dict) else {},
                    )
                    return json_response(self, 200, out)
                if path.endswith('/actions/enable') and '/shops/' in path and path.startswith('/v1/sessions/'):
                    prefix = '/v1/sessions/'
                    remainder = path[len(prefix):]
                    session_id, shop_suffix = remainder.split('/shops/', 1)
                    shop_id = shop_suffix[: -len('/actions/enable')].strip('/')
                    out = engine.set_shop_action_state(
                        session_id=session_id.strip('/'),
                        shop_id=shop_id,
                        action_id=str(body.get('actionId', '')),
                        managed_by_agent_id=str(body.get('managedByAgentId', body.get('originatorAgentId', body.get('agentId', '')))),
                        enabled=True,
                        installed=body.get('installed') if isinstance(body.get('installed'), bool) else None,
                        metadata=body.get('metadata') if isinstance(body.get('metadata'), dict) else {},
                    )
                    return json_response(self, 200, out)
                if path.endswith('/actions/disable') and '/shops/' in path and path.startswith('/v1/sessions/'):
                    prefix = '/v1/sessions/'
                    remainder = path[len(prefix):]
                    session_id, shop_suffix = remainder.split('/shops/', 1)
                    shop_id = shop_suffix[: -len('/actions/disable')].strip('/')
                    out = engine.set_shop_action_state(
                        session_id=session_id.strip('/'),
                        shop_id=shop_id,
                        action_id=str(body.get('actionId', '')),
                        managed_by_agent_id=str(body.get('managedByAgentId', body.get('originatorAgentId', body.get('agentId', '')))),
                        enabled=False,
                        installed=body.get('installed') if isinstance(body.get('installed'), bool) else None,
                        metadata=body.get('metadata') if isinstance(body.get('metadata'), dict) else {},
                    )
                    return json_response(self, 200, out)
                if path.endswith('/turns') and path.startswith('/v1/sessions/'):
                    session_id = path[len('/v1/sessions/') : -len('/turns')].strip('/')
                    out = engine.create_turn(
                        session_id=session_id,
                        objective=str(body.get('objective', '')),
                        requested_by=str(body.get('requestedBy', 'user')),
                        metadata=body.get('metadata') if isinstance(body.get('metadata'), dict) else {},
                    )
                    return json_response(self, 200, out)
                if path.endswith('/orchestrate-turn') and path.startswith('/v1/sessions/'):
                    session_id = path[len('/v1/sessions/') : -len('/orchestrate-turn')].strip('/')
                    out = engine.orchestrate_turn(
                        session_id=session_id,
                        turn_id=str(body.get('turnId', '')),
                        plan=body.get('plan') if isinstance(body.get('plan'), list) else [],
                        metadata=body.get('metadata') if isinstance(body.get('metadata'), dict) else {},
                    )
                    return json_response(self, 200, out)
                if path.endswith('/delegations') and path.startswith('/v1/sessions/'):
                    session_id = path[len('/v1/sessions/') : -len('/delegations')].strip('/')
                    out = engine.delegate_turn(
                        session_id=session_id,
                        turn_id=str(body.get('turnId', '')),
                        shop_id=str(body.get('shopId', '')),
                        shop_profile_id=str(body.get('shopProfileId', '')),
                        role=str(body.get('role', '')),
                        action_id=str(body.get('actionId', '')),
                        inputs=body.get('inputs') if isinstance(body.get('inputs'), dict) else {},
                        metadata=body.get('metadata') if isinstance(body.get('metadata'), dict) else {},
                    )
                    return json_response(self, 200, out)
                if path.endswith('/compact') and path.startswith('/v1/sessions/'):
                    session_id = path[len('/v1/sessions/') : -len('/compact')].strip('/')
                    out = engine.compact_session(
                        session_id=session_id,
                        keep_last_messages=int(body.get('keepLastMessages', 10)),
                    )
                    return json_response(self, 200, out)
                if path.endswith('/messages') and path.startswith('/v1/sessions/'):
                    session_id = path[len('/v1/sessions/') : -len('/messages')].strip('/')
                    out = engine.add_message(
                        session_id=session_id,
                        role=str(body.get('role', '')),
                        content=str(body.get('content', '')),
                        metadata=body.get('metadata') if isinstance(body.get('metadata'), dict) else {},
                    )
                    return json_response(self, 200, out)
                if path.endswith('/converse') and path.startswith('/v1/sessions/'):
                    session_id = path[len('/v1/sessions/') : -len('/converse')].strip('/')
                    out = engine.converse(
                        session_id=session_id,
                        target=str(body.get('target', '')),
                        content=str(body.get('content', '')),
                        mode=str(body.get('mode', '')),
                        allow_delegation=bool(body.get('allowDelegation', True)),
                        conversation_mode_hint=str(body.get('conversationModeHint', '')),
                        wait_for_result=bool(body.get('waitForResult', False)),
                    )
                    return json_response(self, 200, out)
                if path.endswith('/invoke-action') and path.startswith('/v1/sessions/'):
                    session_id = path[len('/v1/sessions/') : -len('/invoke-action')].strip('/')
                    out = engine.invoke_action(
                        session_id=session_id,
                        action_id=str(body.get('actionId', '')),
                        inputs=body.get('inputs') if isinstance(body.get('inputs'), dict) else {},
                        metadata=body.get('metadata') if isinstance(body.get('metadata'), dict) else {},
                        shop_id=str(body.get('shopId', '')),
                    )
                    return json_response(self, 200, out)
                return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
            except ValueError as ex:
                return json_response(self, 400, err('VALIDATION_ERROR', str(ex), req_id))
            except RuntimeError as ex:
                return json_response(self, 502, err('DEPENDENCY_REQUEST_FAILED', str(ex), req_id))
            except Exception as ex:
                return json_response(self, 500, err('INTERNAL', str(ex), req_id))

        def log_message(self, fmt: str, *args):
            return

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description='Umbrella Session Service')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8784)
    ap.add_argument('--umbrella-root', default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument('--catalog-url', default='')
    ap.add_argument('--execution-url', default='http://127.0.0.1:8794')
    ap.add_argument('--heartbeat-ttl-sec', type=int, default=300)
    ap.add_argument('--mesh-token', default='')
    ap.add_argument('--token', default='')
    args = ap.parse_args()

    root = Path(args.umbrella_root).resolve()
    engine = SessionEngine(
        umbrella_root=root,
        catalog_url=args.catalog_url,
        execution_url=args.execution_url,
        mesh_token=args.mesh_token,
        heartbeat_ttl_sec=args.heartbeat_ttl_sec,
    )
    handler = handler_factory(engine=engine, token=args.token.strip())
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({'status': 'listening', 'service': 'session', 'host': args.host, 'port': args.port}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
