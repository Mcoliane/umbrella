from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def model_provider_path(root: Path) -> Path:
    return root / "control-plane" / "runtime" / "model-provider.json"


def model_provider_secrets_path(root: Path) -> Path:
    return root / "control-plane" / "runtime" / "model-provider.secrets.json"


def model_broker_path(root: Path) -> Path:
    return root / "control-plane" / "runtime" / "model-broker.json"


def model_broker_secrets_path(root: Path) -> Path:
    return root / "control-plane" / "runtime" / "model-broker.secrets.json"


def platform_manifest_path(root: Path) -> Path:
    return root / "control-plane" / "runtime" / "platform-manifest.json"


def platform_token_path(root: Path) -> Path:
    return root / "control-plane" / "runtime" / "platform-token.txt"


def platform_mesh_token(root: Path) -> str:
    path = platform_token_path(Path(root).resolve())
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def mask_secret(secret: str) -> str:
    value = str(secret or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + ("*" * max(4, len(value) - 8)) + value[-4:]


def _normalize_legacy_provider(config: dict, secrets: dict, root: Path) -> dict:
    if not isinstance(config, dict):
        config = {}
    if not isinstance(secrets, dict):
        secrets = {}
    provider = config.get("provider") if isinstance(config.get("provider"), dict) else {}
    agent_defaults = config.get("agentDefaults") if isinstance(config.get("agentDefaults"), dict) else {}
    return {
        "version": str(config.get("version", "umbrella.model-provider.v1")).strip() or "umbrella.model-provider.v1",
        "enabled": bool(config.get("enabled", False)),
        "provider": {
            "id": str(provider.get("id", "")).strip() or "default",
            "type": str(provider.get("type", "")).strip() or "zai",
            "baseUrl": str(provider.get("baseUrl", "")).strip(),
            "defaultModel": str(provider.get("defaultModel", "")).strip(),
            "timeoutSec": int(provider.get("timeoutSec", 20) or 20),
        },
        "agentDefaults": agent_defaults,
        "secrets": {
            "apiKey": str(secrets.get("apiKey", "")).strip(),
        },
        "paths": {
            "config": str(model_provider_path(root)),
            "secrets": str(model_provider_secrets_path(root)),
        },
    }


def _default_broker_config() -> dict:
    return {
        "version": "umbrella.model-broker.v1",
        "enabled": False,
        "broker": {
            "url": "",
            "defaultConnectionId": "default",
            "allowFallback": True,
        },
        "providers": {
            "zai": {
                "id": "zai",
                "type": "zai",
                "supportsApiKey": True,
                "supportsOAuth": False,
            },
            "openai-compatible": {
                "id": "openai-compatible",
                "type": "openai-compatible",
                "supportsApiKey": True,
                "supportsOAuth": False,
            }
        },
        "connections": {
            "default": {
                "id": "default",
                "providerId": "zai",
                "authMode": "api_key",
                "label": "Default Z.ai",
                "enabled": False,
                "baseUrl": "",
                "defaultModel": "",
                "timeoutSec": 20,
            }
        },
        "routing": {
            "defaultConnectionId": "default",
            "allowFallback": True,
            "packageDefaults": {},
        },
    }


def _normalize_broker(config: dict, secrets: dict, root: Path) -> dict:
    base = _default_broker_config()
    if not isinstance(config, dict):
        config = {}
    if not isinstance(secrets, dict):
        secrets = {}

    broker_meta = config.get("broker") if isinstance(config.get("broker"), dict) else {}
    providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}
    connections = config.get("connections") if isinstance(config.get("connections"), dict) else {}
    routing = config.get("routing") if isinstance(config.get("routing"), dict) else {}
    secret_connections = secrets.get("connections") if isinstance(secrets.get("connections"), dict) else {}

    normalized_providers = dict(base["providers"])
    for provider_id, row in providers.items():
        if not isinstance(row, dict):
            continue
        pid = str(provider_id or row.get("id", "")).strip()
        if not pid:
            continue
        normalized_providers[pid] = {
            "id": pid,
            "type": str(row.get("type", "")).strip() or pid,
            "supportsApiKey": bool(row.get("supportsApiKey", True)),
            "supportsOAuth": bool(row.get("supportsOAuth", False)),
        }

    normalized_connections = {}
    for connection_id, row in connections.items():
        if not isinstance(row, dict):
            continue
        cid = str(connection_id or row.get("id", "")).strip()
        if not cid:
            continue
        normalized_connections[cid] = {
            "id": cid,
            "providerId": str(row.get("providerId", "")).strip() or "zai",
            "authMode": str(row.get("authMode", "api_key")).strip() or "api_key",
            "label": str(row.get("label", "")).strip() or cid,
            "enabled": bool(row.get("enabled", False)),
            "baseUrl": str(row.get("baseUrl", "")).strip(),
            "defaultModel": str(row.get("defaultModel", "")).strip(),
            "timeoutSec": int(row.get("timeoutSec", 20) or 20),
        }
    if not normalized_connections:
        normalized_connections = dict(base["connections"])

    package_defaults = routing.get("packageDefaults") if isinstance(routing.get("packageDefaults"), dict) else {}
    normalized_secrets = {"connections": {}}
    for cid in normalized_connections:
        row = secret_connections.get(cid) if isinstance(secret_connections.get(cid), dict) else {}
        normalized_secrets["connections"][cid] = {
            "apiKey": str(row.get("apiKey", "")).strip(),
            "oauthAccessToken": str(row.get("oauthAccessToken", "")).strip(),
            "oauthRefreshToken": str(row.get("oauthRefreshToken", "")).strip(),
        }

    return {
        "version": str(config.get("version", base["version"])).strip() or base["version"],
        "enabled": bool(config.get("enabled", False)),
        "broker": {
            "url": str(broker_meta.get("url", "")).strip(),
            "defaultConnectionId": str(
                broker_meta.get("defaultConnectionId", routing.get("defaultConnectionId", base["broker"]["defaultConnectionId"]))
            ).strip()
            or base["broker"]["defaultConnectionId"],
            "allowFallback": bool(broker_meta.get("allowFallback", routing.get("allowFallback", True))),
        },
        "providers": normalized_providers,
        "connections": normalized_connections,
        "routing": {
            "defaultConnectionId": str(
                routing.get("defaultConnectionId", broker_meta.get("defaultConnectionId", base["routing"]["defaultConnectionId"]))
            ).strip()
            or base["routing"]["defaultConnectionId"],
            "allowFallback": bool(routing.get("allowFallback", broker_meta.get("allowFallback", True))),
            "packageDefaults": package_defaults,
        },
        "secrets": normalized_secrets,
        "paths": {
            "config": str(model_broker_path(root)),
            "secrets": str(model_broker_secrets_path(root)),
        },
    }


def _legacy_to_broker(provider: dict) -> dict:
    normalized = _default_broker_config()
    provider_meta = provider.get("provider") if isinstance(provider.get("provider"), dict) else {}
    connection_id = str(provider_meta.get("id", "default")).strip() or "default"
    normalized["enabled"] = bool(provider.get("enabled", False))
    normalized["broker"]["defaultConnectionId"] = connection_id
    normalized["routing"]["defaultConnectionId"] = connection_id
    normalized["connections"] = {
        connection_id: {
            "id": connection_id,
            "providerId": str(provider_meta.get("type", "zai")).strip() or "zai",
            "authMode": "api_key",
            "label": "Migrated Model Provider",
            "enabled": bool(provider.get("enabled", False)),
            "baseUrl": str(provider_meta.get("baseUrl", "")).strip(),
            "defaultModel": str(provider_meta.get("defaultModel", "")).strip(),
            "timeoutSec": int(provider_meta.get("timeoutSec", 20) or 20),
        }
    }
    normalized["routing"]["packageDefaults"] = dict(provider.get("agentDefaults") if isinstance(provider.get("agentDefaults"), dict) else {})
    normalized["secrets"] = {
        "connections": {
            connection_id: {
                "apiKey": str(((provider.get("secrets") or {}).get("apiKey", ""))).strip(),
                "oauthAccessToken": "",
                "oauthRefreshToken": "",
            }
        }
    }
    return normalized


def load_model_broker(root: Path) -> dict:
    root = Path(root).resolve()
    config = _load_json(model_broker_path(root), {})
    secrets = _load_json(model_broker_secrets_path(root), {})
    if isinstance(config, dict) and config:
        return _normalize_broker(config, secrets, root)
    legacy = _normalize_legacy_provider(
        _load_json(model_provider_path(root), {}),
        _load_json(model_provider_secrets_path(root), {}),
        root,
    )
    return _normalize_broker(_legacy_to_broker(legacy), _legacy_to_broker(legacy).get("secrets", {}), root)


def save_model_broker(root: Path, *, config: dict | None = None, secrets: dict | None = None, mirror_legacy: bool = False) -> dict:
    root = Path(root).resolve()
    current = load_model_broker(root)
    if config is None:
        config = {
            "version": current.get("version", "umbrella.model-broker.v1"),
            "enabled": bool(current.get("enabled", False)),
            "broker": dict(current.get("broker") if isinstance(current.get("broker"), dict) else {}),
            "providers": dict(current.get("providers") if isinstance(current.get("providers"), dict) else {}),
            "connections": dict(current.get("connections") if isinstance(current.get("connections"), dict) else {}),
            "routing": dict(current.get("routing") if isinstance(current.get("routing"), dict) else {}),
        }
    if secrets is None:
        secrets = {
            "connections": dict(current.get("secrets", {}).get("connections", {})),
        }
    _write_json(model_broker_path(root), config)
    _write_json(model_broker_secrets_path(root), secrets)
    return load_model_broker(root)


def _default_connection_id(broker: dict) -> str:
    routing = broker.get("routing") if isinstance(broker.get("routing"), dict) else {}
    broker_meta = broker.get("broker") if isinstance(broker.get("broker"), dict) else {}
    connection_id = str(routing.get("defaultConnectionId", "")).strip() or str(broker_meta.get("defaultConnectionId", "")).strip()
    if connection_id:
        return connection_id
    connections = broker.get("connections") if isinstance(broker.get("connections"), dict) else {}
    return next(iter(connections.keys()), "default")


def default_connection(broker: dict) -> dict:
    connections = broker.get("connections") if isinstance(broker.get("connections"), dict) else {}
    return dict(connections.get(_default_connection_id(broker)) if isinstance(connections.get(_default_connection_id(broker)), dict) else {})


def load_model_provider(root: Path) -> dict:
    root = Path(root).resolve()
    broker = load_model_broker(root)
    connection = default_connection(broker)
    provider_id = str(connection.get("providerId", "zai")).strip() or "zai"
    providers = broker.get("providers") if isinstance(broker.get("providers"), dict) else {}
    provider_meta = providers.get(provider_id) if isinstance(providers.get(provider_id), dict) else {}
    connection_id = str(connection.get("id", _default_connection_id(broker))).strip() or "default"
    secrets = broker.get("secrets", {}).get("connections", {}).get(connection_id, {}) if isinstance(broker.get("secrets", {}).get("connections", {}), dict) else {}
    return {
        "version": "umbrella.model-provider.v1",
        "enabled": bool(broker.get("enabled", False) and connection.get("enabled", False)),
        "provider": {
            "id": connection_id,
            "type": str(provider_meta.get("type", provider_id)).strip() or provider_id,
            "baseUrl": str(connection.get("baseUrl", "")).strip(),
            "defaultModel": str(connection.get("defaultModel", "")).strip(),
            "timeoutSec": int(connection.get("timeoutSec", 20) or 20),
        },
        "agentDefaults": dict((broker.get("routing") or {}).get("packageDefaults", {})),
        "secrets": {
            "apiKey": str((secrets or {}).get("apiKey", "")).strip(),
        },
        "paths": {
            "config": str(model_provider_path(root)),
            "secrets": str(model_provider_secrets_path(root)),
            "brokerConfig": str(model_broker_path(root)),
            "brokerSecrets": str(model_broker_secrets_path(root)),
        },
    }


def save_model_provider(root: Path, *, config: dict | None = None, secrets: dict | None = None) -> dict:
    root = Path(root).resolve()
    current = load_model_broker(root)
    normalized_provider = _normalize_legacy_provider(config or {}, secrets or {}, root)
    connection_id = str((normalized_provider.get("provider") or {}).get("id", "")).strip() or _default_connection_id(current)
    current["enabled"] = bool(normalized_provider.get("enabled", False))
    current["broker"]["defaultConnectionId"] = connection_id
    current["routing"]["defaultConnectionId"] = connection_id
    current["routing"]["packageDefaults"] = dict(normalized_provider.get("agentDefaults") if isinstance(normalized_provider.get("agentDefaults"), dict) else {})
    provider_meta = normalized_provider.get("provider") if isinstance(normalized_provider.get("provider"), dict) else {}
    provider_type = str(provider_meta.get("type", "zai")).strip() or "zai"
    providers = current.get("providers") if isinstance(current.get("providers"), dict) else {}
    if provider_type not in providers:
        providers[provider_type] = {
            "id": provider_type,
            "type": provider_type,
            "supportsApiKey": True,
            "supportsOAuth": False,
        }
    current["providers"] = providers
    connections = current.get("connections") if isinstance(current.get("connections"), dict) else {}
    connections[connection_id] = {
        "id": connection_id,
        "providerId": provider_type,
        "authMode": "api_key",
        "label": str(connections.get(connection_id, {}).get("label", "")).strip() or "Primary Connection",
        "enabled": bool(normalized_provider.get("enabled", False)),
        "baseUrl": str(provider_meta.get("baseUrl", "")).strip(),
        "defaultModel": str(provider_meta.get("defaultModel", "")).strip(),
        "timeoutSec": int(provider_meta.get("timeoutSec", 20) or 20),
    }
    current["connections"] = connections
    secret_connections = current.get("secrets", {}).get("connections", {}) if isinstance(current.get("secrets", {}).get("connections", {}), dict) else {}
    secret_connections[connection_id] = {
        "apiKey": str(((normalized_provider.get("secrets") or {}).get("apiKey", ""))).strip(),
        "oauthAccessToken": str((secret_connections.get(connection_id, {}) or {}).get("oauthAccessToken", "")).strip(),
        "oauthRefreshToken": str((secret_connections.get(connection_id, {}) or {}).get("oauthRefreshToken", "")).strip(),
    }
    current["secrets"] = {"connections": secret_connections}
    save_model_broker(
        root,
        config={
            "version": current.get("version", "umbrella.model-broker.v1"),
            "enabled": bool(current.get("enabled", False)),
            "broker": dict(current.get("broker") if isinstance(current.get("broker"), dict) else {}),
            "providers": dict(current.get("providers") if isinstance(current.get("providers"), dict) else {}),
            "connections": dict(current.get("connections") if isinstance(current.get("connections"), dict) else {}),
            "routing": dict(current.get("routing") if isinstance(current.get("routing"), dict) else {}),
        },
        secrets={"connections": secret_connections},
        mirror_legacy=False,
    )
    return load_model_provider(root)


def resolve_model_for_agent(package_id: str, metadata: dict | None, provider: dict, *, override: dict | None = None) -> dict:
    override = override if isinstance(override, dict) else {}
    provider_meta = provider.get("provider") if isinstance(provider.get("provider"), dict) else {}
    agent_defaults = provider.get("agentDefaults") if isinstance(provider.get("agentDefaults"), dict) else {}
    package_defaults = agent_defaults.get(str(package_id or "").strip()) if isinstance(agent_defaults.get(str(package_id or "").strip()), dict) else {}
    meta = metadata if isinstance(metadata, dict) else {}
    return {
        "model": str(override.get("model") or package_defaults.get("model") or meta.get("modelPreference") or provider_meta.get("defaultModel") or "").strip(),
        "temperature": float(override.get("temperature") if override.get("temperature") is not None else (meta.get("temperatureDefault", 0.2) or 0.2)),
        "maxTokens": int(override.get("maxTokens") if override.get("maxTokens") is not None else (meta.get("maxTokensDefault", 300) or 300)),
    }


def provider_enabled(provider: dict) -> bool:
    if not isinstance(provider, dict):
        return False
    provider_meta = provider.get("provider") if isinstance(provider.get("provider"), dict) else {}
    return bool(provider.get("enabled")) and bool(str(provider_meta.get("baseUrl", "")).strip()) and bool(
        str(provider_meta.get("defaultModel", "")).strip()
    )


def provider_chat_url(provider: dict) -> str:
    provider_meta = provider.get("provider") if isinstance(provider.get("provider"), dict) else {}
    return str(provider_meta.get("baseUrl", "")).rstrip("/") + "/chat/completions"


def provider_headers(provider: dict, api_key_override: str = "") -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = str(api_key_override or ((provider.get("secrets") or {}).get("apiKey", ""))).strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def test_model_provider(provider: dict, *, timeout_sec: int | None = None) -> dict:
    started = time.time()
    if not provider_enabled(provider):
        return {"ok": False, "configured": False, "message": "model provider is not configured"}
    provider_meta = provider.get("provider") if isinstance(provider.get("provider"), dict) else {}
    timeout = int(timeout_sec or provider_meta.get("timeoutSec", 20) or 20)
    body = {
        "model": str(provider_meta.get("defaultModel", "")).strip(),
        "messages": [
            {"role": "system", "content": "Reply in JSON with keys reply and mode."},
            {"role": "user", "content": "ping"},
        ],
        "temperature": 0,
        "max_tokens": 32,
    }
    req = urllib.request.Request(
        provider_chat_url(provider),
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers=provider_headers(provider),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        elapsed_ms = int((time.time() - started) * 1000)
        choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
        ok = bool(choices)
        return {
            "ok": ok,
            "configured": True,
            "providerType": str(provider_meta.get("type", "")).strip(),
            "baseUrl": str(provider_meta.get("baseUrl", "")).strip(),
            "model": str(provider_meta.get("defaultModel", "")).strip(),
            "latencyMs": elapsed_ms,
            "message": "provider reachable" if ok else "provider returned no choices",
        }
    except urllib.error.HTTPError as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "ok": False,
            "configured": True,
            "model": str(provider_meta.get("defaultModel", "")).strip(),
            "latencyMs": elapsed_ms,
            "message": f"HTTP {exc.code}",
        }
    except Exception as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "ok": False,
            "configured": True,
            "model": str(provider_meta.get("defaultModel", "")).strip(),
            "latencyMs": elapsed_ms,
            "message": str(exc),
        }


def discover_broker_url(root: Path, broker: dict | None = None) -> str:
    root = Path(root).resolve()
    env_url = os.environ.get("UMBRELLA_MODEL_BROKER_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    payload = broker if isinstance(broker, dict) else load_model_broker(root)
    broker_meta = payload.get("broker") if isinstance(payload.get("broker"), dict) else {}
    configured = str(broker_meta.get("url", "")).strip()
    manifest = _load_json(platform_manifest_path(root), {})
    services = manifest.get("services") if isinstance(manifest.get("services"), dict) else {}
    row = services.get("model-broker")
    manifest_url = str(row.get("url", "")).strip() if isinstance(row, dict) else ""

    def healthy(base_url: str) -> bool:
        candidate = str(base_url or "").strip().rstrip("/")
        if not candidate:
            return False
        req = urllib.request.Request(
            candidate + "/v1/model-broker/health",
            headers=broker_headers(root),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("status") == "ok"
        except Exception:
            return False

    if configured and healthy(configured):
        return configured.rstrip("/")
    if manifest_url and healthy(manifest_url):
        return manifest_url.rstrip("/")
    if configured:
        return configured.rstrip("/")
    if manifest_url:
        return manifest_url.rstrip("/")
    return "http://127.0.0.1:8796"


def broker_headers(root: Path, token_override: str = "") -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = str(token_override or os.environ.get("UMBRELLA_MODEL_BROKER_TOKEN", "") or platform_mesh_token(root)).strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def broker_enabled(broker: dict) -> bool:
    if not isinstance(broker, dict):
        return False
    if not bool(broker.get("enabled", False)):
        return False
    connection = default_connection(broker)
    return bool(connection.get("enabled", False)) and bool(str(connection.get("baseUrl", "")).strip()) and bool(str(connection.get("defaultModel", "")).strip())


def call_model_broker(root: Path, path: str, payload: dict, *, timeout_sec: float = 20.0, token_override: str = "") -> dict:
    base_url = discover_broker_url(root).rstrip("/")
    req = urllib.request.Request(
        f"{base_url}{path}",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers=broker_headers(root, token_override=token_override),
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def env_provider_fallback() -> dict:
    base_url = os.environ.get("UMBRELLA_CHAT_BASE_URL", "").strip()
    model = os.environ.get("UMBRELLA_CHAT_MODEL", "").strip()
    return {
        "version": "umbrella.model-provider.v1",
        "enabled": bool(base_url and model),
        "provider": {
            "id": "env",
            "type": os.environ.get("UMBRELLA_CHAT_PROVIDER", "openai-compatible").strip() or "openai-compatible",
            "baseUrl": base_url,
            "defaultModel": model,
            "timeoutSec": int((os.environ.get("UMBRELLA_CHAT_TIMEOUT_SEC", "20").strip() or "20")),
        },
        "agentDefaults": {},
        "secrets": {"apiKey": os.environ.get("UMBRELLA_CHAT_API_KEY", "").strip()},
        "paths": {"config": "", "secrets": ""},
    }
