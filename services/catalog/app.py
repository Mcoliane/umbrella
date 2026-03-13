#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import shutil
import sys
import tarfile
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

sys.path.append(str(Path(__file__).resolve().parents[2]))
from services.memory.auth import check_auth


SUPPORTED_PLUGIN_HOST_RUNTIMES = {'shell', 'python', 'http', 'container'}
SUPPORTED_ACTION_SCHEMA_VERSIONS = {'umbrella.catalog.action.v1'}
SUPPORTED_SIGNATURE_MODES = {'permissive', 'require-checksum', 'require-signature'}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def write_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + '\n', encoding='utf-8')


def parse_json(handler: BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get('Content-Length', '0'))
    raw = handler.rfile.read(n) if n > 0 else b'{}'
    try:
        return json.loads(raw.decode('utf-8') or '{}')
    except Exception:
        return {}


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def err(code: str, message: str, request_id: str) -> dict:
    return {'error': {'code': code, 'message': message, 'request_id': request_id}}


def parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for raw in str(text or '').strip().split('.'):
        if not raw:
            continue
        digits = ''.join(ch for ch in raw if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def safe_relpath(relpath: str) -> str:
    candidate = Path(str(relpath or '').strip())
    if candidate.is_absolute():
        raise ValueError('archive paths must be relative')
    normalized = Path(*[part for part in candidate.parts if part not in ('', '.')])
    if any(part == '..' for part in normalized.parts):
        raise ValueError('archive paths must not escape install root')
    return normalized.as_posix()


class CatalogEngine:
    def __init__(
        self,
        umbrella_root: Path,
        registry_path: str,
        scan_roots: list[str],
        extensions_root: str,
        signature_mode: str = 'permissive',
        trusted_key_dir: str = '',
    ):
        self.root = umbrella_root
        self.registry_path = (self.root / registry_path).resolve()
        self.scan_roots = [(self.root / rel).resolve() for rel in scan_roots]
        self.extensions_root = (self.root / extensions_root).resolve()
        self.signature_mode = str(signature_mode or 'permissive').strip() or 'permissive'
        if self.signature_mode not in SUPPORTED_SIGNATURE_MODES:
            raise ValueError(f'signatureMode must be one of {", ".join(sorted(SUPPORTED_SIGNATURE_MODES))}')
        trusted_key_dir = str(trusted_key_dir or '').strip()
        if trusted_key_dir:
            key_path = Path(trusted_key_dir)
            self.trusted_key_dir = (self.root / key_path).resolve() if not key_path.is_absolute() else key_path.resolve()
            self.trusted_key_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.trusted_key_dir = None
        self.umbrella_version = (self.root / 'VERSION').read_text(encoding='utf-8').strip() if (self.root / 'VERSION').exists() else '0.0.0'
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.extensions_root.mkdir(parents=True, exist_ok=True)
        if not self.registry_path.exists():
            self.save_registry(self._empty_registry())

    def _empty_registry(self) -> dict:
        return {
            'version': 'umbrella.catalog.registry.v2',
            'managedManifests': {},
            'managedInstalls': {},
            'itemState': {},
            'updatedAt': now_iso(),
        }

    def load_registry(self) -> dict:
        reg = load_json(self.registry_path, self._empty_registry())
        if not isinstance(reg, dict):
            reg = self._empty_registry()
        if not isinstance(reg.get('managedManifests'), dict):
            reg['managedManifests'] = {}
        if not isinstance(reg.get('managedInstalls'), dict):
            reg['managedInstalls'] = {}
        if not isinstance(reg.get('itemState'), dict):
            reg['itemState'] = {}
        return reg

    def save_registry(self, reg: dict):
        reg['updatedAt'] = now_iso()
        write_json(self.registry_path, reg)

    def _normalize_manifest(self, manifest: dict) -> dict:
        manifest = dict(manifest)
        compat = manifest.get('compatibility') if isinstance(manifest.get('compatibility'), dict) else {}
        compat.setdefault('umbrella', {'minVersion': '0.0.0'})
        compat.setdefault('pluginHostRuntimes', [str(manifest.get('runtime', '')).strip()])
        compat.setdefault('apiVersions', [str(manifest.get('apiVersion', '')).strip()])
        compat.setdefault('actionSchemaVersions', ['umbrella.catalog.action.v1'])
        compat.setdefault('requiresFeatures', [])
        manifest['compatibility'] = compat
        return manifest

    def _compatibility(self, manifest: dict) -> dict:
        manifest = self._normalize_manifest(manifest)
        compat = manifest.get('compatibility') if isinstance(manifest.get('compatibility'), dict) else {}
        umbrella = compat.get('umbrella') if isinstance(compat.get('umbrella'), dict) else {}
        current = parse_version(self.umbrella_version)
        minimum = parse_version(str(umbrella.get('minVersion', '0.0.0')))
        maximum_raw = str(umbrella.get('maxVersion', '')).strip()
        maximum = parse_version(maximum_raw) if maximum_raw else None
        umbrella_ok = current >= minimum and (maximum is None or current <= maximum)
        plugin_host_runtimes = [str(x).strip() for x in (compat.get('pluginHostRuntimes') or []) if str(x).strip()]
        plugin_host_ok = bool(set(plugin_host_runtimes or [str(manifest.get('runtime', '')).strip()]) & SUPPORTED_PLUGIN_HOST_RUNTIMES)
        api_versions = [str(x).strip() for x in (compat.get('apiVersions') or []) if str(x).strip()]
        api_ok = str(manifest.get('apiVersion', '')).strip() in api_versions if api_versions else True
        action_schema_versions = [str(x).strip() for x in (compat.get('actionSchemaVersions') or []) if str(x).strip()]
        action_schema_ok = bool(set(action_schema_versions or ['umbrella.catalog.action.v1']) & SUPPORTED_ACTION_SCHEMA_VERSIONS)
        ok = umbrella_ok and plugin_host_ok and api_ok and action_schema_ok
        return {
            'ok': ok,
            'umbrellaVersion': self.umbrella_version,
            'umbrella': {
                'ok': umbrella_ok,
                'minVersion': str(umbrella.get('minVersion', '0.0.0')),
                'maxVersion': maximum_raw,
            },
            'pluginHostRuntimes': {
                'ok': plugin_host_ok,
                'supported': sorted(SUPPORTED_PLUGIN_HOST_RUNTIMES),
                'declared': plugin_host_runtimes,
            },
            'apiVersions': {
                'ok': api_ok,
                'declared': api_versions,
            },
            'actionSchemaVersions': {
                'ok': action_schema_ok,
                'declared': action_schema_versions,
                'supported': sorted(SUPPORTED_ACTION_SCHEMA_VERSIONS),
            },
            'requiresFeatures': [str(x).strip() for x in (compat.get('requiresFeatures') or []) if str(x).strip()],
        }

    def _validate_manifest(self, manifest: dict, manifest_path: Path) -> list[str]:
        errors: list[str] = []
        manifest = self._normalize_manifest(manifest)
        required_str = ('id', 'name', 'version', 'apiVersion', 'kind', 'runtime', 'entrypoint')
        for key in required_str:
            if not str(manifest.get(key, '')).strip():
                errors.append(f'missing {key}')
        if str(manifest.get('apiVersion', '')).strip() != 'umbrella.catalog.manifest.v1':
            errors.append('apiVersion must be umbrella.catalog.manifest.v1')
        if str(manifest.get('kind', '')).strip() not in {'skill', 'plugin'}:
            errors.append('kind must be skill or plugin')
        if str(manifest.get('runtime', '')).strip() not in {'shell', 'python', 'http', 'container'}:
            errors.append('runtime must be shell, python, http, or container')
        actions = manifest.get('actions')
        if not isinstance(actions, list) or not actions:
            errors.append('actions must be a non-empty list')
        else:
            seen: set[str] = set()
            for idx, action in enumerate(actions, start=1):
                if not isinstance(action, dict):
                    errors.append(f'actions[{idx}] must be an object')
                    continue
                action_id = str(action.get('id', '')).strip()
                if not action_id:
                    errors.append(f'actions[{idx}] missing id')
                elif action_id in seen:
                    errors.append(f'duplicate action id: {action_id}')
                seen.add(action_id)
                if not str(action.get('title', '')).strip():
                    errors.append(f'actions[{idx}] missing title')
                req_caps = action.get('requiredCapabilities', [])
                if req_caps is not None and not isinstance(req_caps, list):
                    errors.append(f'actions[{idx}] requiredCapabilities must be a list')
        entrypoint = str(manifest.get('entrypoint', '')).strip()
        if entrypoint and str(manifest.get('runtime', '')).strip() != 'http':
            resolved = (manifest_path.parent / entrypoint).resolve()
            if not resolved.exists():
                errors.append(f'entrypoint not found: {entrypoint}')
        return errors

    def _file_checksum(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _verify_checksums(self, install_root: Path) -> dict:
        checksum_path = install_root / 'CHECKSUMS.json'
        if not checksum_path.exists():
            raise ValueError('CHECKSUMS.json is required for bundle installs')
        payload = load_json(checksum_path, {})
        if not isinstance(payload, dict):
            raise ValueError('CHECKSUMS.json must be a JSON object')
        files = payload.get('files')
        if not isinstance(files, dict) or not files:
            raise ValueError('CHECKSUMS.json must contain a non-empty files map')
        verified: dict[str, str] = {}
        for relpath, expected in sorted(files.items()):
            safe_path = safe_relpath(relpath)
            if safe_path == 'CHECKSUMS.json':
                continue
            expected = str(expected or '').strip().lower()
            if len(expected) != 64 or any(ch not in '0123456789abcdef' for ch in expected):
                raise ValueError(f'invalid checksum for {safe_path}')
            file_path = (install_root / safe_path).resolve()
            try:
                file_path.relative_to(install_root.resolve())
            except ValueError as ex:
                raise ValueError(f'checksum path escapes install root: {safe_path}')
            if not file_path.exists():
                raise ValueError(f'checksummed file not found: {safe_path}')
            actual = self._file_checksum(file_path)
            if actual != expected:
                raise ValueError(f'checksum mismatch for {safe_path}')
            verified[safe_path] = actual
        return {
            'checksumVerified': True,
            'verifiedAt': now_iso(),
            'files': verified,
            'signatureVerified': False,
            'signatureStatus': 'not-present',
        }

    def _verify_signature(self, install_root: Path, checksums: dict) -> dict:
        result = dict(checksums if isinstance(checksums, dict) else {})
        signature_meta_path = install_root / 'SIGNATURE.json'
        signature_path = install_root / 'SIGNATURE'
        if not signature_meta_path.exists() or not signature_path.exists():
            if self.signature_mode == 'require-signature':
                raise ValueError('SIGNATURE.json and SIGNATURE are required for bundle installs in require-signature mode')
            result['signatureVerified'] = False
            result['signatureStatus'] = 'not-present'
            return result

        payload = load_json(signature_meta_path, {})
        if not isinstance(payload, dict):
            raise ValueError('SIGNATURE.json must be a JSON object')
        key_id = str(payload.get('keyId', '')).strip()
        if not key_id:
            raise ValueError('SIGNATURE.json must contain keyId')
        algorithm = str(payload.get('algorithm', 'sha256-rsa')).strip() or 'sha256-rsa'
        if algorithm != 'sha256-rsa':
            raise ValueError('SIGNATURE.json algorithm must be sha256-rsa')
        signed_file_rel = safe_relpath(str(payload.get('signedFile', 'CHECKSUMS.json')).strip() or 'CHECKSUMS.json')
        signed_file = (install_root / signed_file_rel).resolve()
        try:
            signed_file.relative_to(install_root.resolve())
        except ValueError as ex:
            raise ValueError('signed file escapes install root') from ex
        if not signed_file.exists():
            raise ValueError('signed file not found for signature verification')
        if self.trusted_key_dir is None:
            if self.signature_mode == 'require-signature':
                raise ValueError('trustedKeyDir is required for require-signature mode')
            result['signatureVerified'] = False
            result['signatureStatus'] = 'not-configured'
            result['signatureKeyId'] = key_id
            return result
        key_path = (self.trusted_key_dir / f'{key_id}.pem').resolve()
        if not key_path.exists():
            if self.signature_mode == 'require-signature':
                raise ValueError(f'trusted signing key not found: {key_id}')
            result['signatureVerified'] = False
            result['signatureStatus'] = 'untrusted-key'
            result['signatureKeyId'] = key_id
            return result
        proc = subprocess.run(
            ['openssl', 'dgst', '-sha256', '-verify', str(key_path), '-signature', str(signature_path), str(signed_file)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or '').strip() or 'signature verification failed'
            raise ValueError(message)
        result['signatureVerified'] = True
        result['signatureStatus'] = 'verified'
        result['signatureKeyId'] = key_id
        result['signatureAlgorithm'] = algorithm
        result['signedFile'] = signed_file_rel
        result['signatureVerifiedAt'] = now_iso()
        return result

    def _extract_bundle(self, bundle_path: Path, install_root: Path):
        suffixes = bundle_path.suffixes
        if bundle_path.suffix == '.zip':
            with zipfile.ZipFile(bundle_path, 'r') as archive:
                for member in archive.infolist():
                    safe_relpath(member.filename)
                archive.extractall(install_root)
            return
        if suffixes[-2:] == ['.tar', '.gz'] or bundle_path.suffix in {'.tgz', '.tar'}:
            mode = 'r:gz' if bundle_path.suffix in {'.tgz', '.gz'} or suffixes[-2:] == ['.tar', '.gz'] else 'r:'
            with tarfile.open(bundle_path, mode) as archive:
                for member in archive.getmembers():
                    safe_relpath(member.name)
                archive.extractall(install_root, filter='data')
            return
        raise ValueError('bundlePath must point to a .zip, .tar, or .tgz bundle')

    def _registry_install_row(self, item_id: str, version: str) -> dict | None:
        reg = self.load_registry()
        installs = reg.get('managedInstalls') if isinstance(reg.get('managedInstalls'), dict) else {}
        item_versions = installs.get(item_id) if isinstance(installs.get(item_id), dict) else {}
        row = item_versions.get(version)
        return row if isinstance(row, dict) else None

    def _entry_from_manifest(
        self,
        manifest: dict,
        manifest_path: Path,
        source: str,
        state_row: dict | None = None,
        install_row: dict | None = None,
    ) -> dict:
        manifest = self._normalize_manifest(manifest)
        state_row = state_row if isinstance(state_row, dict) else {}
        install_row = install_row if isinstance(install_row, dict) else {}
        compatible = self._compatibility(manifest)
        lifecycle_state = str(install_row.get('lifecycleState', 'discovered' if source == 'scan' else 'installed')).strip() or 'discovered'
        enabled = bool(state_row.get('enabled', manifest.get('defaultEnabled', True)))
        if not compatible['ok']:
            lifecycle_state = 'incompatible'
            enabled = False
        elif lifecycle_state in {'failed', 'disabled'}:
            enabled = False
        actions = []
        for action in manifest.get('actions') or []:
            if not isinstance(action, dict):
                continue
            actions.append(
                {
                    'id': str(action.get('id', '')).strip(),
                    'title': str(action.get('title', '')).strip(),
                    'description': str(action.get('description', '')).strip(),
                    'requiredCapabilities': [str(x).strip() for x in (action.get('requiredCapabilities') or []) if str(x).strip()],
                    'policyHints': action.get('policyHints') if isinstance(action.get('policyHints'), dict) else {},
                    'inputSchema': action.get('inputSchema') if isinstance(action.get('inputSchema'), dict) else {},
                    'outputSchema': action.get('outputSchema') if isinstance(action.get('outputSchema'), dict) else {},
                }
            )
        install_path = str(install_row.get('installPath', manifest_path.parent))
        status = 'enabled' if enabled else lifecycle_state
        return {
            'id': str(manifest.get('id', '')).strip(),
            'name': str(manifest.get('name', '')).strip(),
            'version': str(manifest.get('version', '')).strip(),
            'apiVersion': str(manifest.get('apiVersion', '')).strip(),
            'kind': str(manifest.get('kind', '')).strip(),
            'runtime': str(manifest.get('runtime', '')).strip(),
            'entrypoint': str((Path(install_path) / str(manifest.get('entrypoint', '')).strip()).resolve()) if str(manifest.get('runtime', '')).strip() != 'http' else str(manifest.get('entrypoint', '')).strip(),
            'manifestPath': str(manifest_path),
            'source': source,
            'status': status,
            'enabled': enabled,
            'compatible': compatible,
            'requiredCapabilities': sorted({cap for action in actions for cap in action.get('requiredCapabilities', [])}),
            'actions': actions,
            'sessionHooks': manifest.get('sessionHooks') if isinstance(manifest.get('sessionHooks'), dict) else {},
            'isolationMode': str(manifest.get('isolationMode', 'process')).strip() or 'process',
            'executionPolicy': manifest.get('executionPolicy') if isinstance(manifest.get('executionPolicy'), dict) else {},
            'container': manifest.get('container') if isinstance(manifest.get('container'), dict) else {},
            'install': {
                'lifecycleState': lifecycle_state,
                'managed': source == 'managed',
                'sourceType': str(install_row.get('sourceType', source)).strip() or source,
                'sourcePath': str(install_row.get('sourcePath', manifest_path)).strip(),
                'installPath': install_path,
                'installedAt': str(install_row.get('installedAt', '')).strip(),
                'updatedAt': str(install_row.get('updatedAt', state_row.get('updatedAt', ''))).strip(),
                'healthStatus': str(install_row.get('healthStatus', 'unknown')).strip() or 'unknown',
                'checksumVerified': bool(install_row.get('checksumVerified', False)),
                'checksums': install_row.get('checksums') if isinstance(install_row.get('checksums'), dict) else {},
                'signatureVerified': bool(install_row.get('signatureVerified', False)),
                'signatureStatus': str(install_row.get('signatureStatus', 'not-configured')).strip() or 'not-configured',
                'signatureKeyId': str(install_row.get('signatureKeyId', '')).strip(),
            },
        }

    def _versions_payload(self, item_id: str, versions: dict[str, dict]) -> list[dict]:
        rows = []
        for version, row in sorted(versions.items(), key=lambda item: parse_version(item[0]), reverse=True):
            if not isinstance(row, dict):
                continue
            rows.append(
                {
                    'version': version,
                    'manifestPath': str(row.get('manifestPath', '')).strip(),
                    'installPath': str(row.get('installPath', '')).strip(),
                    'sourceType': str(row.get('sourceType', '')).strip(),
                    'lifecycleState': str(row.get('lifecycleState', '')).strip(),
                    'installedAt': str(row.get('installedAt', '')).strip(),
                    'updatedAt': str(row.get('updatedAt', '')).strip(),
                    'healthStatus': str(row.get('healthStatus', '')).strip(),
                    'checksumVerified': bool(row.get('checksumVerified', False)),
                    'signatureVerified': bool(row.get('signatureVerified', False)),
                    'signatureKeyId': str(row.get('signatureKeyId', '')).strip(),
                }
            )
        return rows

    def discover_catalog(self) -> dict:
        reg = self.load_registry()
        item_state = reg.get('itemState') if isinstance(reg.get('itemState'), dict) else {}
        managed_installs = reg.get('managedInstalls') if isinstance(reg.get('managedInstalls'), dict) else {}
        legacy_managed = reg.get('managedManifests') if isinstance(reg.get('managedManifests'), dict) else {}
        entries: dict[str, dict] = {}
        invalid: list[dict] = []
        manifest_sources: list[tuple[Path, str, dict | None]] = []
        for root in self.scan_roots:
            if root.exists():
                for manifest_path in sorted(root.rglob('manifest.json')):
                    manifest_sources.append((manifest_path.resolve(), 'scan', None))
        for item_id, versions in sorted(managed_installs.items()):
            if not isinstance(versions, dict):
                continue
            preferred_version = str((item_state.get(item_id) or {}).get('selectedVersion', '')).strip()
            candidates = [preferred_version] if preferred_version else []
            candidates.extend([version for version in sorted(versions.keys(), key=parse_version, reverse=True) if version != preferred_version])
            for version in candidates:
                row = versions.get(version)
                if not isinstance(row, dict):
                    continue
                manifest_path = Path(str(row.get('manifestPath', '')).strip()).expanduser()
                if not manifest_path.is_absolute():
                    manifest_path = (self.root / manifest_path).resolve()
                manifest_sources.append((manifest_path, 'managed', row))
                break
        for raw_id, row in sorted(legacy_managed.items()):
            if not isinstance(row, dict):
                continue
            if raw_id in managed_installs:
                continue
            manifest_path = Path(str(row.get('manifestPath', '')).strip()).expanduser()
            if not manifest_path.is_absolute():
                manifest_path = (self.root / manifest_path).resolve()
            manifest_sources.append((manifest_path, 'managed', {'manifestPath': str(manifest_path), 'sourceType': 'local', 'lifecycleState': 'installed'}))

        seen_ids: set[str] = set()
        for manifest_path, source, install_row in manifest_sources:
            if not manifest_path.exists():
                invalid.append({'manifestPath': str(manifest_path), 'errors': ['manifest not found'], 'source': source})
                continue
            manifest = load_json(manifest_path, {})
            if not isinstance(manifest, dict):
                invalid.append({'manifestPath': str(manifest_path), 'errors': ['manifest is not a JSON object'], 'source': source})
                continue
            manifest = self._normalize_manifest(manifest)
            errors = self._validate_manifest(manifest, manifest_path)
            item_id = str(manifest.get('id', '')).strip() or manifest_path.parent.name
            if item_id in seen_ids and source == 'scan':
                continue
            if errors:
                invalid.append({'id': item_id, 'manifestPath': str(manifest_path), 'errors': errors, 'source': source})
                continue
            versions = managed_installs.get(item_id) if isinstance(managed_installs.get(item_id), dict) else {}
            selected_version = str((item_state.get(item_id) or {}).get('selectedVersion', '')).strip() or str(manifest.get('version', '')).strip()
            entry_install_row = install_row
            if not entry_install_row and selected_version and isinstance(versions.get(selected_version), dict):
                entry_install_row = versions.get(selected_version)
            entry = self._entry_from_manifest(manifest, manifest_path, source, item_state.get(item_id), entry_install_row)
            entry['versions'] = self._versions_payload(item_id, versions) if versions else [{'version': entry['version'], 'lifecycleState': entry['install']['lifecycleState']}]
            entries[item_id] = entry
            seen_ids.add(item_id)

        return {
            'catalogVersion': 'umbrella.catalog.registry.v2',
            'umbrellaVersion': self.umbrella_version,
            'loadedAt': now_iso(),
            'scanRoots': [str(x) for x in self.scan_roots],
            'registryPath': str(self.registry_path),
            'extensionsRoot': str(self.extensions_root),
            'items': [entries[key] for key in sorted(entries.keys())],
            'invalid': invalid,
        }

    def refresh(self) -> dict:
        reg = self.load_registry()
        item_state = reg.get('itemState') if isinstance(reg.get('itemState'), dict) else {}
        catalog = self.discover_catalog()
        items_by_id = {item['id']: item for item in catalog['items']}
        for item_id, row in list(item_state.items()):
            if not isinstance(row, dict):
                continue
            item = items_by_id.get(item_id)
            if not item:
                continue
            if not item.get('compatible', {}).get('ok', False):
                row['enabled'] = False
                row['updatedAt'] = now_iso()
                selected_version = str(row.get('selectedVersion', '')).strip()
                install_row = self._registry_install_row(item_id, selected_version) if selected_version else None
                if install_row is not None:
                    install_row['lifecycleState'] = 'incompatible'
                    install_row['updatedAt'] = now_iso()
        reg['itemState'] = item_state
        self.save_registry(reg)
        catalog = self.discover_catalog()
        return {
            'ok': True,
            'refreshedAt': now_iso(),
            'itemCount': len(catalog['items']),
            'invalidCount': len(catalog['invalid']),
            'catalog': catalog,
        }

    def list_items(self) -> dict:
        return self.discover_catalog()

    def list_actions(self) -> dict:
        catalog = self.discover_catalog()
        actions = []
        for item in catalog['items']:
            for action in item.get('actions', []):
                actions.append(
                    {
                        'pluginId': item['id'],
                        'pluginName': item['name'],
                        'pluginVersion': item['version'],
                        'enabled': item['enabled'],
                        'status': item['status'],
                        **action,
                    }
                )
        return {
            'loadedAt': now_iso(),
            'actionCount': len(actions),
            'actions': sorted(actions, key=lambda x: (x['pluginId'], x['id'])),
        }

    def get_action(self, action_id: str) -> dict | None:
        actions = self.list_actions()
        for action in actions.get('actions', []):
            if action.get('id') == action_id:
                return action
        return None

    def get_item(self, item_id: str) -> dict | None:
        catalog = self.discover_catalog()
        for item in catalog['items']:
            if item.get('id') == item_id:
                return item
        return None

    def list_versions(self, item_id: str) -> dict:
        reg = self.load_registry()
        installs = reg.get('managedInstalls') if isinstance(reg.get('managedInstalls'), dict) else {}
        item_versions = installs.get(item_id)
        if not isinstance(item_versions, dict) or not item_versions:
            item = self.get_item(item_id)
            if not item:
                raise ValueError('catalog item not found')
            return {'id': item_id, 'versions': item.get('versions', [])}
        return {'id': item_id, 'versions': self._versions_payload(item_id, item_versions)}

    def _set_enabled(self, item_id: str, enabled: bool) -> dict:
        item = self.get_item(item_id)
        if not item:
            raise ValueError('catalog item not found')
        if enabled and not item.get('compatible', {}).get('ok', False):
            raise ValueError('catalog item is not compatible with this Umbrella runtime')
        reg = self.load_registry()
        item_state = reg.get('itemState') if isinstance(reg.get('itemState'), dict) else {}
        state_row = item_state.get(item_id) if isinstance(item_state.get(item_id), dict) else {}
        state_row['enabled'] = enabled
        state_row['selectedVersion'] = str(item.get('version', '')).strip()
        state_row['updatedAt'] = now_iso()
        item_state[item_id] = state_row
        reg['itemState'] = item_state
        installs = reg.get('managedInstalls') if isinstance(reg.get('managedInstalls'), dict) else {}
        version_row = (installs.get(item_id) or {}).get(str(item.get('version', '')).strip())
        if isinstance(version_row, dict):
            version_row['lifecycleState'] = 'enabled' if enabled else 'disabled'
            version_row['updatedAt'] = now_iso()
        self.save_registry(reg)
        updated = self.get_item(item_id)
        return {'ok': True, 'item': updated}

    def enable_item(self, item_id: str) -> dict:
        return self._set_enabled(item_id, True)

    def disable_item(self, item_id: str) -> dict:
        return self._set_enabled(item_id, False)

    def _persist_install(self, manifest: dict, manifest_path: Path, *, source_type: str, source_path: str, install_path: Path, checksums: dict | None = None) -> dict:
        manifest = self._normalize_manifest(manifest)
        item_id = str(manifest.get('id', '')).strip()
        version = str(manifest.get('version', '')).strip()
        compatibility = self._compatibility(manifest)
        lifecycle_state = 'validated' if compatibility['ok'] else 'incompatible'
        reg = self.load_registry()
        installs = reg.get('managedInstalls') if isinstance(reg.get('managedInstalls'), dict) else {}
        item_versions = installs.get(item_id) if isinstance(installs.get(item_id), dict) else {}
        item_versions[version] = {
            'manifestPath': str(manifest_path),
            'installPath': str(install_path),
            'sourceType': source_type,
            'sourcePath': source_path,
            'installedAt': now_iso(),
            'updatedAt': now_iso(),
            'lifecycleState': lifecycle_state,
            'healthStatus': 'healthy' if compatibility['ok'] else 'incompatible',
            'checksumVerified': bool((checksums or {}).get('checksumVerified', False)),
            'checksums': checksums if isinstance(checksums, dict) else {},
            'signatureVerified': bool((checksums or {}).get('signatureVerified', False)),
            'signatureStatus': str((checksums or {}).get('signatureStatus', 'not-configured')).strip() or 'not-configured',
            'signatureKeyId': str((checksums or {}).get('signatureKeyId', '')).strip(),
        }
        installs[item_id] = item_versions
        reg['managedInstalls'] = installs
        managed = reg.get('managedManifests') if isinstance(reg.get('managedManifests'), dict) else {}
        managed[item_id] = {
            'manifestPath': str(manifest_path),
            'installedAt': now_iso(),
        }
        reg['managedManifests'] = managed
        item_state = reg.get('itemState') if isinstance(reg.get('itemState'), dict) else {}
        state_row = item_state.get(item_id) if isinstance(item_state.get(item_id), dict) else {}
        state_row.setdefault('enabled', False if lifecycle_state != 'validated' else bool(manifest.get('defaultEnabled', True)))
        state_row['selectedVersion'] = version
        state_row['updatedAt'] = now_iso()
        if lifecycle_state != 'validated':
            state_row['enabled'] = False
        item_state[item_id] = state_row
        reg['itemState'] = item_state
        self.save_registry(reg)
        item = self.get_item(item_id)
        return {'ok': True, 'item': item}

    def install_local(self, manifest_path_value: str) -> dict:
        manifest_path = Path(str(manifest_path_value).strip()).expanduser()
        if not manifest_path.is_absolute():
            manifest_path = (self.root / manifest_path).resolve()
        if not manifest_path.exists():
            raise ValueError('manifestPath does not exist')
        manifest = load_json(manifest_path, {})
        if not isinstance(manifest, dict):
            raise ValueError('manifestPath does not point to a valid JSON manifest')
        manifest = self._normalize_manifest(manifest)
        errors = self._validate_manifest(manifest, manifest_path)
        if errors:
            raise ValueError('; '.join(errors))
        return self._persist_install(
            manifest,
            manifest_path,
            source_type='local',
            source_path=str(manifest_path),
            install_path=manifest_path.parent.resolve(),
            checksums={'checksumVerified': False, 'signatureVerified': False, 'signatureStatus': 'not-configured', 'files': {}},
        )

    def install_bundle(self, bundle_path_value: str) -> dict:
        bundle_path = Path(str(bundle_path_value).strip()).expanduser()
        if not bundle_path.is_absolute():
            bundle_path = (self.root / bundle_path).resolve()
        if not bundle_path.exists():
            raise ValueError('bundlePath does not exist')
        with tempfile.TemporaryDirectory(prefix='umbrella-catalog-bundle-') as tmpdir:
            extract_root = Path(tmpdir) / 'extract'
            extract_root.mkdir(parents=True, exist_ok=True)
            self._extract_bundle(bundle_path, extract_root)
            manifest_path = extract_root / 'manifest.json'
            if not manifest_path.exists():
                raise ValueError('bundle must include manifest.json at archive root')
            manifest = load_json(manifest_path, {})
            if not isinstance(manifest, dict):
                raise ValueError('bundle manifest.json is invalid')
            manifest = self._normalize_manifest(manifest)
            errors = self._validate_manifest(manifest, manifest_path)
            if errors:
                raise ValueError('; '.join(errors))
            checksums = self._verify_checksums(extract_root)
            checksums = self._verify_signature(extract_root, checksums)
            item_id = str(manifest.get('id', '')).strip()
            version = str(manifest.get('version', '')).strip()
            install_root = self.extensions_root / item_id / version
            if install_root.exists():
                raise ValueError('that bundle version is already installed; use update instead')
            install_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(extract_root, install_root)
        installed_manifest_path = install_root / 'manifest.json'
        installed_manifest = load_json(installed_manifest_path, {})
        if not isinstance(installed_manifest, dict):
            raise ValueError('installed manifest is invalid after extraction')
        return self._persist_install(
            self._normalize_manifest(installed_manifest),
            installed_manifest_path,
            source_type='bundle',
            source_path=str(bundle_path),
            install_path=install_root,
            checksums=checksums,
        )

    def update_item(self, item_id: str, bundle_path_value: str) -> dict:
        installed = self.install_bundle(bundle_path_value)
        item = installed.get('item') if isinstance(installed.get('item'), dict) else {}
        if str(item.get('id', '')).strip() != str(item_id or '').strip():
            raise ValueError('bundle item id does not match requested id')
        return installed

    def uninstall_item(self, item_id: str, version: str = '') -> dict:
        item_id = str(item_id or '').strip()
        reg = self.load_registry()
        installs = reg.get('managedInstalls') if isinstance(reg.get('managedInstalls'), dict) else {}
        item_versions = installs.get(item_id) if isinstance(installs.get(item_id), dict) else {}
        if not item_versions:
            raise ValueError('catalog item not found')
        target_version = str(version or '').strip()
        if not target_version:
            target_version = sorted(item_versions.keys(), key=parse_version, reverse=True)[0]
        row = item_versions.get(target_version)
        if not isinstance(row, dict):
            raise ValueError('catalog item version not found')
        install_path = Path(str(row.get('installPath', '')).strip())
        if install_path.exists() and str(install_path).startswith(str(self.extensions_root)):
            shutil.rmtree(install_path)
        item_versions.pop(target_version, None)
        if item_versions:
            installs[item_id] = item_versions
        else:
            installs.pop(item_id, None)
        reg['managedInstalls'] = installs
        managed = reg.get('managedManifests') if isinstance(reg.get('managedManifests'), dict) else {}
        if item_versions:
            latest_version = sorted(item_versions.keys(), key=parse_version, reverse=True)[0]
            managed[item_id] = {'manifestPath': str(item_versions[latest_version].get('manifestPath', '')), 'installedAt': now_iso()}
        else:
            managed.pop(item_id, None)
        reg['managedManifests'] = managed
        item_state = reg.get('itemState') if isinstance(reg.get('itemState'), dict) else {}
        state_row = item_state.get(item_id) if isinstance(item_state.get(item_id), dict) else {}
        if state_row:
            if item_versions:
                latest_version = sorted(item_versions.keys(), key=parse_version, reverse=True)[0]
                state_row['selectedVersion'] = latest_version
                state_row['enabled'] = False
                state_row['updatedAt'] = now_iso()
                item_state[item_id] = state_row
            else:
                item_state.pop(item_id, None)
        reg['itemState'] = item_state
        self.save_registry(reg)
        return {'ok': True, 'id': item_id, 'version': target_version, 'removed': True}


def handler_factory(engine: CatalogEngine, token: str):
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
            if path == '/v1/catalog/health':
                return json_response(self, 200, {'status': 'ok', 'service': 'catalog', 'checkedAt': now_iso()})
            if path == '/v1/catalog/items':
                return json_response(self, 200, engine.list_items())
            if path == '/v1/catalog/actions':
                return json_response(self, 200, engine.list_actions())
            action_prefix = '/v1/catalog/actions/'
            if path.startswith(action_prefix):
                action_id = unquote(path[len(action_prefix):].strip('/'))
                if not action_id:
                    return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
                action = engine.get_action(action_id)
                if not action:
                    return json_response(self, 404, err('NOT_FOUND', 'catalog action not found', req_id))
                return json_response(self, 200, action)
            if path.startswith('/v1/catalog/items/') and path.endswith('/versions'):
                item_id = unquote(path[len('/v1/catalog/items/') : -len('/versions')].strip('/'))
                try:
                    return json_response(self, 200, engine.list_versions(item_id))
                except ValueError as ex:
                    return json_response(self, 404, err('NOT_FOUND', str(ex), req_id))
            prefix = '/v1/catalog/items/'
            if path.startswith(prefix):
                item_id = unquote(path[len(prefix):].strip('/'))
                if not item_id:
                    return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
                item = engine.get_item(item_id)
                if not item:
                    return json_response(self, 404, err('NOT_FOUND', 'catalog item not found', req_id))
                return json_response(self, 200, item)
            return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))

        def do_POST(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            body = parse_json(self)
            try:
                if path == '/v1/catalog/refresh':
                    return json_response(self, 200, engine.refresh())
                if path == '/v1/catalog/install-local':
                    out = engine.install_local(str(body.get('manifestPath', '')))
                    return json_response(self, 200, out)
                if path == '/v1/catalog/install-bundle':
                    out = engine.install_bundle(str(body.get('bundlePath', '')))
                    return json_response(self, 200, out)
                if path == '/v1/catalog/update':
                    out = engine.update_item(str(body.get('id', '')).strip(), str(body.get('bundlePath', '')))
                    return json_response(self, 200, out)
                if path == '/v1/catalog/uninstall':
                    out = engine.uninstall_item(str(body.get('id', '')).strip(), str(body.get('version', '')).strip())
                    return json_response(self, 200, out)
                if path == '/v1/catalog/items/enable':
                    out = engine.enable_item(str(body.get('id', '')).strip())
                    return json_response(self, 200, out)
                if path == '/v1/catalog/items/disable':
                    out = engine.disable_item(str(body.get('id', '')).strip())
                    return json_response(self, 200, out)
                return json_response(self, 404, err('NOT_FOUND', 'route not found', req_id))
            except ValueError as ex:
                return json_response(self, 400, err('VALIDATION_ERROR', str(ex), req_id))
            except Exception as ex:
                return json_response(self, 500, err('INTERNAL', str(ex), req_id))

        def log_message(self, fmt: str, *args):
            return

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description='Umbrella Catalog Service')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8786)
    ap.add_argument('--umbrella-root', default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument('--registry', default='control-plane/observability/catalog/registry.json')
    ap.add_argument('--extensions-root', default='control-plane/extensions')
    ap.add_argument('--scan-root', action='append', default=['skills', 'plugins'])
    ap.add_argument('--signature-mode', default='permissive')
    ap.add_argument('--trusted-key-dir', default='')
    ap.add_argument('--token', default='')
    args = ap.parse_args()

    root = Path(args.umbrella_root).resolve()
    engine = CatalogEngine(
        umbrella_root=root,
        registry_path=args.registry,
        scan_roots=args.scan_root,
        extensions_root=args.extensions_root,
        signature_mode=args.signature_mode,
        trusted_key_dir=args.trusted_key_dir,
    )
    handler = handler_factory(engine=engine, token=args.token.strip())
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({'status': 'listening', 'service': 'catalog', 'host': args.host, 'port': args.port}, indent=2))
    httpd.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
