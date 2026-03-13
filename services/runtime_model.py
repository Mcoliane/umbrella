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


def model_provider_path(root: Path) -> Path:
    return root / "control-plane" / "runtime" / "model-provider.json"


def model_provider_secrets_path(root: Path) -> Path:
    return root / "control-plane" / "runtime" / "model-provider.secrets.json"


def load_model_provider(root: Path) -> dict:
    root = Path(root).resolve()
    config = _load_json(model_provider_path(root), {})
    secrets = _load_json(model_provider_secrets_path(root), {})
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
            "type": str(provider.get("type", "")).strip() or "openai-compatible",
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


def save_model_provider(root: Path, *, config: dict | None = None, secrets: dict | None = None) -> dict:
    root = Path(root).resolve()
    config_path = model_provider_path(root)
    secrets_path = model_provider_secrets_path(root)
    if config is not None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    if secrets is not None:
        secrets_path.parent.mkdir(parents=True, exist_ok=True)
        secrets_path.write_text(json.dumps(secrets, indent=2) + "\n", encoding="utf-8")
    return load_model_provider(root)


def mask_secret(secret: str) -> str:
    value = str(secret or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + ("*" * max(4, len(value) - 8)) + value[-4:]


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
        return {"ok": False, "configured": True, "model": str(provider_meta.get("defaultModel", "")).strip(), "latencyMs": elapsed_ms, "message": f"HTTP {exc.code}"}
    except Exception as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        return {"ok": False, "configured": True, "model": str(provider_meta.get("defaultModel", "")).strip(), "latencyMs": elapsed_ms, "message": str(exc)}


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
