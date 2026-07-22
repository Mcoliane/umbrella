#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import uuid
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.append(str(Path(__file__).resolve().parents[2]))
from services.memory.auth import check_auth
from services.model_broker.providers import openai_compatible
from services.runtime_model import load_model_broker, mask_secret, save_model_broker

# A single completion can come back as a valid HTTP 200 with empty or unusable
# content (flaky providers do this). The adapter retries transient 5xx/network
# errors underneath each call; these govern how many times chat_respond re-asks
# when a 200 body is unusable before giving up.
_COMPLETION_ATTEMPTS = 3
_COMPLETION_BACKOFF_SEC = 0.5

# Canonical provider type. Every backend Umbrella talks to is an OpenAI-compatible
# /chat/completions endpoint; there is one adapter.
DEFAULT_PROVIDER_TYPE = "openai-compatible"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def parse_json(handler: BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(n) if n > 0 else b"{}"
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {}


def err(code: str, message: str, request_id: str) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def http_error_detail(exc: urllib.error.HTTPError, limit: int = 500) -> str:
    # Keep the provider's own explanation (truncated) instead of reducing every
    # failure to an opaque status code.
    try:
        body = exc.read().decode("utf-8", "replace").strip()
    except Exception:
        body = ""
    if not body:
        body = str(getattr(exc, "reason", "")).strip()
    if body:
        return f"HTTP {exc.code}: {body[:limit]}"
    return f"HTTP {exc.code}"


def parse_json_content_block(content: str) -> dict | None:
    raw = str(content or "").strip()
    if not raw:
        return None
    candidates = [raw]
    if raw.startswith("```"):
        stripped = raw.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
        candidates.append(stripped)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return None


def summarize_history(rows: list[dict], limit: int = 8) -> str:
    lines: list[str] = []
    for row in rows[-limit:]:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role", "")).strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = str(row.get("content", "")).strip()
        if not content:
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


# What each built-in action does and the inputs to fill when delegating to it.
# Lets the mayor form a correct delegationPlan instead of guessing.
ACTION_GUIDE = {
    "skill.code.agent": "autonomously write/modify/verify code (plans, edits files, runs tests until they pass) — inputs: task (required: the full description of what to build), workingDir (optional: absolute path, e.g. ~/Desktop/my-app)",
    "skill.code.run": "run a single Python or Bash snippet and return its output — inputs: code (required), language ('python' or 'bash')",
    "skill.web.search": "search the web and return ranked results — inputs: query (required)",
    "skill.web.fetch": "fetch a URL and return its readable text — inputs: url (required)",
    "skill.memory.search": "search durable knowledge memory — inputs: query (required)",
    "skill.memory.get": "read a durable knowledge node — inputs: nodeId (required)",
    "skill.memory.link": "create a knowledge edge — inputs: fromNodeId, toNodeId, relation",
    "skill.memory.summarize": "summarize a memory node — inputs: nodeId",
    "skill.shop.originate": "create a new worker shop in this session — inputs: shopId, shopName, role, enabledActionIds (array)",
    "skill.chat.respond": "hand a conversational sub-request to this shop's agent — inputs: message",
}


def summarize_shops(rows: list[dict], limit: int = 8) -> str:
    parts: list[str] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        shop_id = str(row.get("shopId", "")).strip()
        name = str(row.get("name", "")).strip() or shop_id
        shop_type = str(row.get("shopType", "")).strip() or "shop"
        actions = [str(x).strip() for x in (row.get("enabledActionIds") or []) if str(x).strip()]
        parts.append(f"- shop id={shop_id} ({name}, {shop_type}):")
        if not actions:
            parts.append("    (no actions enabled)")
        for action in actions[:8]:
            guide = ACTION_GUIDE.get(action, "custom action")
            parts.append(f"    - {action}: {guide}")
    return "\n".join(parts)


class BrokerEngine:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def _provider_adapter(self, provider_type: str):
        normalized = str(provider_type or "").strip().lower()
        # "openai-compatible" is the only adapter. "zai" is a legacy alias that
        # resolves to it — Z.ai spoke the same OpenAI-compatible API, so old
        # configs keep working without a migration step. Empty type also resolves
        # to it, since there is nothing else to be.
        if normalized in {"", "openai-compatible", "zai"}:
            return openai_compatible
        return None

    def _load(self) -> dict:
        return load_model_broker(self.root)

    def _save(self, payload: dict) -> dict:
        return save_model_broker(
            self.root,
            config={
                "version": payload.get("version", "umbrella.model-broker.v1"),
                "enabled": bool(payload.get("enabled", False)),
                "broker": dict(payload.get("broker") if isinstance(payload.get("broker"), dict) else {}),
                "providers": dict(payload.get("providers") if isinstance(payload.get("providers"), dict) else {}),
                "connections": dict(payload.get("connections") if isinstance(payload.get("connections"), dict) else {}),
                "routing": dict(payload.get("routing") if isinstance(payload.get("routing"), dict) else {}),
            },
            secrets={"connections": dict((payload.get("secrets") or {}).get("connections", {}))},
        )

    def _default_connection_id(self, broker: dict) -> str:
        routing = broker.get("routing") if isinstance(broker.get("routing"), dict) else {}
        broker_meta = broker.get("broker") if isinstance(broker.get("broker"), dict) else {}
        value = str(routing.get("defaultConnectionId", "")).strip() or str(broker_meta.get("defaultConnectionId", "")).strip()
        if value:
            return value
        connections = broker.get("connections") if isinstance(broker.get("connections"), dict) else {}
        return next(iter(connections.keys()), "default")

    def _connection_payload(self, broker: dict, connection_id: str, connection: dict) -> dict:
        providers = broker.get("providers") if isinstance(broker.get("providers"), dict) else {}
        provider_id = str(connection.get("providerId", "")).strip() or "openai-compatible"
        provider = providers.get(provider_id) if isinstance(providers.get(provider_id), dict) else {}
        secret = (
            ((broker.get("secrets") or {}).get("connections") or {}).get(connection_id, {})
            if isinstance(((broker.get("secrets") or {}).get("connections") or {}).get(connection_id, {}), dict)
            else {}
        )
        return {
            "id": connection_id,
            "providerId": provider_id,
            "providerType": str(provider.get("type", provider_id)).strip() or provider_id,
            "authMode": str(connection.get("authMode", "api_key")).strip() or "api_key",
            "label": str(connection.get("label", "")).strip() or connection_id,
            "enabled": bool(connection.get("enabled", False)),
            "baseUrl": str(connection.get("baseUrl", "")).strip(),
            "defaultModel": str(connection.get("defaultModel", "")).strip(),
            "timeoutSec": int(connection.get("timeoutSec", 120) or 120),
            "secrets": {
                "apiKeyPresent": bool(str(secret.get("apiKey", "")).strip()),
                "apiKeyMasked": mask_secret(str(secret.get("apiKey", "")).strip()),
                "oauthConfigured": bool(str(secret.get("oauthRefreshToken", "")).strip() or str(secret.get("oauthAccessToken", "")).strip()),
            },
        }

    def status(self) -> dict:
        broker = self._load()
        connection_id = self._default_connection_id(broker)
        connections = broker.get("connections") if isinstance(broker.get("connections"), dict) else {}
        current = connections.get(connection_id) if isinstance(connections.get(connection_id), dict) else {}
        return {
            "enabled": bool(broker.get("enabled", False)),
            "configured": bool(str(current.get("baseUrl", "")).strip() and str(current.get("defaultModel", "")).strip()),
            "broker": broker.get("broker") if isinstance(broker.get("broker"), dict) else {},
            "defaultConnectionId": connection_id,
            "connection": self._connection_payload(broker, connection_id, current) if current else {},
            "routing": broker.get("routing") if isinstance(broker.get("routing"), dict) else {},
            "paths": broker.get("paths") if isinstance(broker.get("paths"), dict) else {},
        }

    def list_providers(self) -> dict:
        broker = self._load()
        providers = broker.get("providers") if isinstance(broker.get("providers"), dict) else {}
        return {
            "providers": [
                {
                    "id": str(provider_id).strip(),
                    "type": str(row.get("type", provider_id)).strip() or provider_id,
                    "supportsApiKey": bool(row.get("supportsApiKey", True)),
                    "supportsOAuth": bool(row.get("supportsOAuth", False)),
                }
                for provider_id, row in sorted(providers.items())
                if isinstance(row, dict)
            ]
        }

    def list_connections(self) -> dict:
        broker = self._load()
        connections = broker.get("connections") if isinstance(broker.get("connections"), dict) else {}
        return {
            "defaultConnectionId": self._default_connection_id(broker),
            "connections": [
                self._connection_payload(broker, cid, row)
                for cid, row in sorted(connections.items())
                if isinstance(row, dict)
            ],
        }

    def save_connection(
        self,
        *,
        connection_id: str,
        provider_id: str,
        auth_mode: str = "api_key",
        label: str = "",
        enabled: bool | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
        timeout_sec: int | None = None,
        api_key: str | None = None,
        make_default: bool = False,
        package_defaults: dict | None = None,
    ) -> dict:
        broker = self._load()
        connection_id = str(connection_id or "").strip() or self._default_connection_id(broker)
        provider_id = str(provider_id or "").strip() or "openai-compatible"
        providers = broker.get("providers") if isinstance(broker.get("providers"), dict) else {}
        if provider_id not in providers:
            providers[provider_id] = {
                "id": provider_id,
                "type": provider_id,
                "supportsApiKey": True,
                "supportsOAuth": False,
            }
        broker["providers"] = providers
        connections = broker.get("connections") if isinstance(broker.get("connections"), dict) else {}
        current = connections.get(connection_id) if isinstance(connections.get(connection_id), dict) else {}
        connections[connection_id] = {
            "id": connection_id,
            "providerId": provider_id,
            "authMode": str(auth_mode or current.get("authMode", "api_key")).strip() or "api_key",
            "label": str(label or current.get("label", connection_id)).strip() or connection_id,
            "enabled": bool(current.get("enabled", False) if enabled is None else enabled),
            "baseUrl": str(current.get("baseUrl", "") if base_url is None else base_url).strip(),
            "defaultModel": str(current.get("defaultModel", "") if default_model is None else default_model).strip(),
            "timeoutSec": int(current.get("timeoutSec", 120) if timeout_sec is None else timeout_sec or 120),
        }
        broker["connections"] = connections
        routing = broker.get("routing") if isinstance(broker.get("routing"), dict) else {}
        if make_default:
            routing["defaultConnectionId"] = connection_id
            broker_meta = broker.get("broker") if isinstance(broker.get("broker"), dict) else {}
            broker_meta["defaultConnectionId"] = connection_id
            broker["broker"] = broker_meta
        if isinstance(package_defaults, dict):
            routing["packageDefaults"] = package_defaults
        broker["routing"] = routing
        secret_connections = broker.get("secrets", {}).get("connections", {}) if isinstance(broker.get("secrets", {}).get("connections", {}), dict) else {}
        current_secret = secret_connections.get(connection_id) if isinstance(secret_connections.get(connection_id), dict) else {}
        if api_key is not None:
            current_secret["apiKey"] = str(api_key).strip()
        secret_connections[connection_id] = current_secret
        broker["secrets"] = {"connections": secret_connections}
        broker["enabled"] = any(bool(row.get("enabled", False)) for row in connections.values() if isinstance(row, dict))
        saved = self._save(broker)
        status = self.status()
        status["saved"] = True
        status["connection"] = self._connection_payload(saved, connection_id, saved.get("connections", {}).get(connection_id, {}))
        return status

    def _resolve_connection(self, broker: dict, request: dict) -> tuple[str, dict, dict, dict]:
        connection_id = str(request.get("connectionId", "")).strip() or self._default_connection_id(broker)
        connections = broker.get("connections") if isinstance(broker.get("connections"), dict) else {}
        connection = connections.get(connection_id) if isinstance(connections.get(connection_id), dict) else {}
        provider_id = str(connection.get("providerId", "")).strip() or "openai-compatible"
        providers = broker.get("providers") if isinstance(broker.get("providers"), dict) else {}
        provider = providers.get(provider_id) if isinstance(providers.get(provider_id), dict) else {}
        secret = (
            ((broker.get("secrets") or {}).get("connections") or {}).get(connection_id, {})
            if isinstance(((broker.get("secrets") or {}).get("connections") or {}).get(connection_id, {}), dict)
            else {}
        )
        return connection_id, connection, provider, secret

    def test_connection(self, *, connection_id: str = "") -> dict:
        started = time.time()
        broker = self._load()
        cid, connection, provider, secret = self._resolve_connection(broker, {"connectionId": connection_id})
        if not connection:
            return {"ok": False, "configured": False, "message": "connection not found"}
        if not bool(connection.get("enabled", False)):
            return {"ok": False, "configured": False, "connectionId": cid, "message": "connection disabled"}
        base_url = str(connection.get("baseUrl", "")).strip()
        model = str(connection.get("defaultModel", "")).strip()
        if not base_url or not model:
            return {"ok": False, "configured": False, "connectionId": cid, "message": "connection not configured"}
        timeout_sec = float(connection.get("timeoutSec", 120) or 120)
        try:
            provider_type = str(provider.get("type", "")).strip() or str(connection.get("providerId", "")).strip() or "openai-compatible"
            adapter = self._provider_adapter(provider_type)
            if adapter is None:
                return {"ok": False, "configured": True, "connectionId": cid, "message": "unsupported provider type"}
            adapter.test_connection(base_url=base_url, model=model, api_key=str(secret.get("apiKey", "")).strip(), timeout_sec=timeout_sec)
            return {
                "ok": True,
                "configured": True,
                "connectionId": cid,
                "providerType": provider_type,
                "model": model,
                "latencyMs": int((time.time() - started) * 1000),
                "message": "connection reachable",
            }
        except urllib.error.HTTPError as exc:
            return {
                "ok": False,
                "configured": True,
                "connectionId": cid,
                "providerType": str(provider.get("type", "")).strip() or str(connection.get("providerId", "")).strip() or "openai-compatible",
                "model": model,
                "latencyMs": int((time.time() - started) * 1000),
                "message": http_error_detail(exc),
            }
        except Exception as exc:
            return {
                "ok": False,
                "configured": True,
                "connectionId": cid,
                "providerType": str(provider.get("type", "")).strip() or str(connection.get("providerId", "")).strip() or "openai-compatible",
                "model": model,
                "latencyMs": int((time.time() - started) * 1000),
                "message": str(exc),
            }

    def list_models(self, *, connection_id: str = "") -> dict:
        broker = self._load()
        cid, connection, provider, secret = self._resolve_connection(broker, {"connectionId": connection_id})
        models: list[str] = []
        default_model = str(connection.get("defaultModel", "")).strip()
        if default_model:
            models.append(default_model)
        if not connection or not bool(connection.get("enabled", False)):
            return {"connectionId": cid, "models": models}
        error_message = ""
        try:
            provider_type = str(provider.get("type", "")).strip() or str(connection.get("providerId", "")).strip() or "openai-compatible"
            adapter = self._provider_adapter(provider_type)
            if adapter is None:
                error_message = f"unsupported provider type: {provider_type}"
            else:
                payload = adapter.list_models(
                    base_url=str(connection.get("baseUrl", "")).strip(),
                    api_key=str(secret.get("apiKey", "")).strip(),
                    timeout_sec=float(connection.get("timeoutSec", 120) or 120),
                )
                rows = payload.get("data") if isinstance(payload.get("data"), list) else []
                models.extend(str(row.get("id", "")).strip() for row in rows if isinstance(row, dict) and str(row.get("id", "")).strip())
        except urllib.error.HTTPError as exc:
            error_message = http_error_detail(exc)
        except Exception as exc:
            error_message = str(exc)
        deduped = []
        for row in models:
            if row and row not in deduped:
                deduped.append(row)
        out = {"connectionId": cid, "models": deduped}
        if error_message:
            out["error"] = {"message": error_message}
        return out

    def chat_respond(self, payload: dict) -> dict:
        broker = self._load()
        cid, connection, provider, secret = self._resolve_connection(broker, payload)
        if not connection:
            return {"ok": False, "error": {"message": "connection not found"}}
        if not bool(broker.get("enabled", False)) or not bool(connection.get("enabled", False)):
            return {"ok": False, "error": {"message": "model broker is not configured"}}
        provider_type = str(provider.get("type", "")).strip() or str(connection.get("providerId", "")).strip() or "openai-compatible"
        adapter = self._provider_adapter(provider_type)
        if adapter is None:
            return {"ok": False, "error": {"message": f"unsupported provider type: {provider_type}"}}

        request = payload if isinstance(payload, dict) else {}
        system_prompt = str(request.get("systemPrompt", "")).strip() or "You are a town agent inside Umbrella. Reply in JSON with keys reply and mode."
        instructions = str(request.get("instructions", "")).strip() or "Mode must be direct or delegate."
        message = str(request.get("message", "")).strip()
        town_context = request.get("townContext") if isinstance(request.get("townContext"), dict) else {}
        available_shops = request.get("availableShops") if isinstance(request.get("availableShops"), list) else []
        conversation_history = request.get("conversationHistory") if isinstance(request.get("conversationHistory"), list) else []
        agent_package_metadata = request.get("agentPackageMetadata") if isinstance(request.get("agentPackageMetadata"), dict) else {}
        package_name = str(request.get("agentPackageId", "")).strip() or str(request.get("agentId", "agent")).strip() or "agent"
        agent_id = str(request.get("agentId", "")).strip() or "agent"
        model_hint = str(request.get("model", "")).strip()
        package_defaults = (broker.get("routing") or {}).get("packageDefaults", {})
        package_default = package_defaults.get(package_name) if isinstance(package_defaults.get(package_name), dict) else {}
        model = model_hint or str(package_default.get("model", "")).strip() or str(connection.get("defaultModel", "")).strip()
        if not model:
            return {"ok": False, "error": {"message": "default model is not configured"}}
        temperature = float(request.get("temperature") if request.get("temperature") is not None else request.get("temperatureDefault", 0.2) or 0.2)
        max_tokens = int(request.get("maxTokens") if request.get("maxTokens") is not None else request.get("maxTokensDefault", 1200) or 1200)
        history_text = summarize_history(conversation_history)
        shops_text = summarize_shops(available_shops)
        context_lines = [
            f"Agent package: {package_name}",
            f"Agent id: {agent_id}",
            f"Town title: {str(town_context.get('title', '')).strip() or 'Town Hall'}",
            f"Session id: {str(town_context.get('sessionId', '')).strip()}",
            f"Mayor agent id: {str(town_context.get('mayorAgentId', '')).strip()}",
            f"Worker shop count: {len(available_shops)}",
            f"Conversation style: {str(agent_package_metadata.get('conversationStyle', '')).strip() or 'default'}",
            f"Default mode: {str(agent_package_metadata.get('defaultMode', '')).strip() or 'direct'}",
            f"Delegation policy: {str(agent_package_metadata.get('delegationPolicy', '')).strip() or 'delegate only when needed'}",
        ]
        provider_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "system", "content": instructions},
                {
                    "role": "system",
                    "content": (
                        "\n".join(context_lines)
                        + "\nAlways respond with JSON containing reply and mode."
                        + "\nDo not answer with generic filler if the user asked a concrete question."
                    ),
                },
                {
                    "role": "system",
                    "content": "Available worker shops and the actions you can delegate to them:\n" + (shops_text or "none"),
                },
                {
                    "role": "system",
                    "content": (
                        "DELEGATION CONTRACT. You reply with a single JSON object.\n"
                        "- To answer yourself OR to ask the user clarifying questions: {\"mode\":\"direct\",\"reply\":\"<answer or questions>\"}.\n"
                        "- To hand real work to a shop: {\"mode\":\"delegate\",\"reply\":\"<one short line telling the user what you're doing>\","
                        "\"delegationPlan\":[{\"shopId\":\"<id from the list above>\",\"actionId\":\"<an action that shop has>\",\"inputs\":{...}}]}.\n"
                        "Rules:\n"
                        "1. CLARIFY BEFORE BUILDING. For a substantial or open-ended build — an app, tool, service, or multi-file "
                        "project — whose key decisions are unspecified (the FORM: web app / desktop GUI / command-line / API; the "
                        "must-have FEATURES; the target platform; important constraints), do NOT delegate yet. Reply mode:clarify with a "
                        "one-line intro and a STRUCTURED question list the interface renders as SELECTABLE OPTIONS: "
                        "{\"mode\":\"clarify\",\"reply\":\"<one-line intro>\",\"questions\":[{\"question\":\"<q>\",\"options\":[\"opt1\",\"opt2\",\"opt3\"],\"multiSelect\":false}]}. "
                        "Give 2-4 questions, each with 2-5 concrete selectable options; set multiSelect:true for 'pick any that apply' "
                        "questions like feature lists. Delegate only once they are answered. Do NOT interrogate for small, clear, or "
                        "already-specified tasks — build those right away. "
                        "Example: {\"mode\":\"clarify\",\"reply\":\"Happy to build that — a few quick things:\",\"questions\":["
                        "{\"question\":\"What form should it take?\",\"options\":[\"Web app\",\"Desktop app\",\"Command-line\"],\"multiSelect\":false},"
                        "{\"question\":\"Which features matter most?\",\"options\":[\"Crop\",\"Filters\",\"Layers\",\"Text overlay\",\"Adjustments\"],\"multiSelect\":true},"
                        "{\"question\":\"Where should output go?\",\"options\":[\"Local files\",\"Web-hosted\"],\"multiSelect\":false}]}\n"
                        "2. Once the request is concrete enough, DELEGATE real work a shop can do — writing or running code, building a "
                        "well-specified app, searching or scraping the web, or looking things up in memory. Do NOT try to do these yourself in prose.\n"
                        "3. Pick the shopId and actionId from the list above, and fill inputs exactly as that action describes. "
                        "For skill.code.agent put the user's full request in inputs.task, and if they name a location set inputs.workingDir "
                        "(e.g. a folder 'prism-code' on the desktop -> \"~/Desktop/prism-code\").\n"
                        "4. Only use shopIds and actionIds that appear above. If no shop fits, answer directly and say what worker would be needed.\n"
                        "5. Answer directly (mode:direct) for questions, chat, explanations, status, and for asking clarifying questions before a big build.\n"
                        "Return ONLY the JSON object, no prose outside it."
                    ),
                },
                {
                    "role": "system",
                    "content": "Recent conversation:\n" + (history_text or "none"),
                },
                {"role": "user", "content": message},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        started = time.time()
        base_url = str(connection.get("baseUrl", "")).strip()
        api_key = str(secret.get("apiKey", "")).strip()
        timeout = float(connection.get("timeoutSec", 120) or 120)
        # Ask up to _COMPLETION_ATTEMPTS times. The adapter already retries
        # transient 5xx/network errors inside each call, so an HTTPError that
        # reaches here is terminal (bad key, 4xx, exhausted 5xx) and we stop.
        # These attempts exist for the other failure mode: a valid 200 whose
        # body is empty or has no usable reply — which flaky models return and
        # which no HTTP-level retry can catch.
        last_error = "provider returned no usable content"
        for attempt in range(_COMPLETION_ATTEMPTS):
            try:
                raw = adapter.chat_respond(
                    base_url=base_url,
                    api_key=api_key,
                    payload=provider_payload,
                    timeout_sec=timeout,
                )
            except urllib.error.HTTPError as exc:
                # 4xx, or a 5xx the adapter already exhausted its retries on: terminal.
                return {"ok": False, "error": {"message": http_error_detail(exc)}}
            except json.JSONDecodeError:
                # A 200 whose body is not valid JSON (e.g. a proxy's HTML error
                # page served with status 200). Treat it as an unusable response
                # and re-ask rather than failing the turn.
                raw = None
            except Exception as exc:
                # Transport error the adapter already retried and re-raised: terminal.
                return {"ok": False, "error": {"message": str(exc)}}

            # A syntactically valid but non-object body (null -> None, [] -> list,
            # bare scalar) is as unusable as an empty one — normalize so it is
            # retried/degraded below instead of raising AttributeError.
            if not isinstance(raw, dict):
                raw = {}
            choices = raw.get("choices") if isinstance(raw.get("choices"), list) else []
            first = choices[0] if choices and isinstance(choices[0], dict) else {}
            msg = first.get("message") if isinstance(first.get("message"), dict) else {}
            content = str((msg or {}).get("content") or "").strip()

            parsed = parse_json_content_block(content)
            if not isinstance(parsed, dict):
                # Non-JSON (or no JSON object): treat the raw text as a direct answer.
                parsed = {"reply": content, "mode": "direct"} if content else {}
            reply = str(parsed.get("reply", "")).strip()
            mode = str(parsed.get("mode", "direct")).strip().lower() or "direct"
            if mode not in {"direct", "delegate"}:
                # Any other label a model invents is answered directly.
                mode = "direct"
            delegation_plan = parsed.get("delegationPlan")
            has_plan = isinstance(delegation_plan, list) and len(delegation_plan) > 0

            # Graceful degradation: a model that asks to delegate but gives no
            # usable plan is answered as a direct reply, not failed outright.
            if mode == "delegate" and not has_plan:
                mode = "direct"
                parsed.pop("delegationPlan", None)
            # A JSON object that parsed but carried no reply falls back to its
            # own raw text so the turn still says something.
            if mode != "delegate" and not reply and content:
                reply = content
            # A valid delegation with no status line still gets one, so a delegate
            # turn never renders as a blank reply.
            if mode == "delegate" and has_plan and not reply:
                reply = "Working on it."

            if reply or (mode == "delegate" and has_plan):
                out = dict(parsed)
                out["ok"] = True
                out["reply"] = reply
                out["mode"] = mode
                out["providerUsed"] = True
                out["fallbackUsed"] = False
                out["providerType"] = provider_type
                out["connectionUsed"] = cid
                out["modelUsed"] = model
                out["latencyMs"] = int((time.time() - started) * 1000)
                out["attempts"] = attempt + 1
                # Structured clarifying questions (mode "clarify" was normalized to
                # "direct" above): sanitize into {question, options, multiSelect} so
                # the client can render them as a selectable picker.
                clean_questions = []
                raw_questions = parsed.get("questions")
                if isinstance(raw_questions, list):
                    for q in raw_questions[:5]:
                        if not isinstance(q, dict):
                            continue
                        qtext = str(q.get("question", "")).strip()
                        if not qtext:
                            continue
                        options = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()][:6]
                        clean_questions.append({"question": qtext, "options": options, "multiSelect": bool(q.get("multiSelect"))})
                out["questions"] = clean_questions
                return out

            last_error = "provider returned empty content" if not content else "provider returned no usable reply"
            if attempt < _COMPLETION_ATTEMPTS - 1:
                time.sleep(_COMPLETION_BACKOFF_SEC * (attempt + 1) + random.uniform(0, 0.25))

        return {"ok": False, "error": {"message": last_error}}


def handler_factory(engine: BrokerEngine, token: str):
    class Handler(BaseHTTPRequestHandler):
        def _request_id(self) -> str:
            return self.headers.get("X-Request-Id", "").strip() or str(uuid.uuid4())

        def _auth_ok(self, req_id: str) -> bool:
            if check_auth(self.headers.get("Authorization", ""), token):
                return True
            json_response(self, 401, err("UNAUTHORIZED", "missing or invalid bearer token", req_id))
            return False

        def do_GET(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            if path in {"/healthz", "/v1/model-broker/health"}:
                return json_response(self, 200, {"status": "ok", "service": "model-broker", "checkedAt": now_iso()})
            if path == "/v1/providers":
                return json_response(self, 200, engine.list_providers())
            if path == "/v1/connections":
                return json_response(self, 200, engine.list_connections())
            if path == "/v1/models":
                return json_response(self, 200, engine.list_models())
            return json_response(self, 404, err("NOT_FOUND", "route not found", req_id))

        def do_POST(self):
            req_id = self._request_id()
            if not self._auth_ok(req_id):
                return
            path = urlparse(self.path).path
            body = parse_json(self)
            try:
                if path == "/v1/connections":
                    out = engine.save_connection(
                        connection_id=str(body.get("connectionId", "")).strip(),
                        provider_id=str(body.get("providerId", body.get("providerType", "openai-compatible"))).strip(),
                        auth_mode=str(body.get("authMode", "api_key")).strip(),
                        label=str(body.get("label", "")).strip(),
                        enabled=body.get("enabled") if isinstance(body.get("enabled"), bool) else None,
                        base_url=str(body.get("baseUrl", "")).strip() if "baseUrl" in body else None,
                        default_model=str(body.get("defaultModel", "")).strip() if "defaultModel" in body else None,
                        timeout_sec=int(body.get("timeoutSec", 120) or 120) if "timeoutSec" in body else None,
                        api_key=body.get("apiKey") if "apiKey" in body else None,
                        make_default=bool(body.get("makeDefault", False)),
                        package_defaults=body.get("packageDefaults") if isinstance(body.get("packageDefaults"), dict) else None,
                    )
                    return json_response(self, 200, out)
                if path == "/v1/connections/test":
                    return json_response(self, 200, {"test": engine.test_connection(connection_id=str(body.get("connectionId", "")).strip())})
                if path == "/v1/chat/respond":
                    out = engine.chat_respond(body)
                    status = 200 if out.get("ok") else 502
                    return json_response(self, status, out)
            except ValueError as ex:
                return json_response(self, 400, err("VALIDATION_ERROR", str(ex), req_id))
            except Exception as ex:
                return json_response(self, 500, err("INTERNAL_ERROR", str(ex), req_id))
            return json_response(self, 404, err("NOT_FOUND", "route not found", req_id))

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8782)
    ap.add_argument("--umbrella-root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--token", default="")
    args = ap.parse_args(argv)

    engine = BrokerEngine(Path(args.umbrella_root))
    handler = handler_factory(engine, args.token)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({"status": "ok", "service": "model-broker", "bind": f"http://{args.host}:{args.port}"}), flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
