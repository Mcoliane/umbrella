#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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
from services.model_broker.providers import zai
from services.runtime_model import load_model_broker, mask_secret, save_model_broker


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


class BrokerEngine:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def _provider_adapter(self, provider_type: str):
        normalized = str(provider_type or "").strip() or "zai"
        if normalized == "zai":
            return zai
        if normalized == "openai-compatible":
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
        provider_id = str(connection.get("providerId", "")).strip() or "zai"
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
            "timeoutSec": int(connection.get("timeoutSec", 20) or 20),
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
        provider_id = str(provider_id or "").strip() or "zai"
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
            "timeoutSec": int(current.get("timeoutSec", 20) if timeout_sec is None else timeout_sec or 20),
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
        provider_id = str(connection.get("providerId", "")).strip() or "zai"
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
        timeout_sec = float(connection.get("timeoutSec", 20) or 20)
        try:
            provider_type = str(provider.get("type", "")).strip() or str(connection.get("providerId", "")).strip() or "zai"
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
                "providerType": str(provider.get("type", "")).strip() or str(connection.get("providerId", "")).strip() or "zai",
                "model": model,
                "latencyMs": int((time.time() - started) * 1000),
                "message": f"HTTP {exc.code}",
            }
        except Exception as exc:
            return {
                "ok": False,
                "configured": True,
                "connectionId": cid,
                "providerType": str(provider.get("type", "")).strip() or str(connection.get("providerId", "")).strip() or "zai",
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
        try:
            provider_type = str(provider.get("type", "")).strip() or str(connection.get("providerId", "")).strip() or "zai"
            adapter = self._provider_adapter(provider_type)
            if adapter is not None:
                payload = adapter.list_models(
                    base_url=str(connection.get("baseUrl", "")).strip(),
                    api_key=str(secret.get("apiKey", "")).strip(),
                    timeout_sec=float(connection.get("timeoutSec", 20) or 20),
                )
                rows = payload.get("data") if isinstance(payload.get("data"), list) else []
                models.extend(str(row.get("id", "")).strip() for row in rows if isinstance(row, dict) and str(row.get("id", "")).strip())
        except Exception:
            pass
        deduped = []
        for row in models:
            if row and row not in deduped:
                deduped.append(row)
        return {"connectionId": cid, "models": deduped}

    def chat_respond(self, payload: dict) -> dict:
        broker = self._load()
        cid, connection, provider, secret = self._resolve_connection(broker, payload)
        if not connection:
            return {"ok": False, "error": {"message": "connection not found"}}
        if not bool(broker.get("enabled", False)) or not bool(connection.get("enabled", False)):
            return {"ok": False, "error": {"message": "model broker is not configured"}}
        provider_type = str(provider.get("type", "")).strip() or str(connection.get("providerId", "")).strip() or "zai"
        adapter = self._provider_adapter(provider_type)
        if adapter is None:
            return {"ok": False, "error": {"message": f"unsupported provider type: {provider_type}"}}

        request = payload if isinstance(payload, dict) else {}
        system_prompt = str(request.get("systemPrompt", "")).strip() or "You are a town agent inside Umbrella. Reply in JSON with keys reply and mode."
        instructions = str(request.get("instructions", "")).strip() or "Mode must be direct or delegate."
        message = str(request.get("message", "")).strip()
        town_context = request.get("townContext") if isinstance(request.get("townContext"), dict) else {}
        available_shops = request.get("availableShops") if isinstance(request.get("availableShops"), list) else []
        package_name = str(request.get("agentPackageId", "")).strip() or str(request.get("agentId", "agent")).strip() or "agent"
        model_hint = str(request.get("model", "")).strip()
        package_defaults = (broker.get("routing") or {}).get("packageDefaults", {})
        package_default = package_defaults.get(package_name) if isinstance(package_defaults.get(package_name), dict) else {}
        model = model_hint or str(package_default.get("model", "")).strip() or str(connection.get("defaultModel", "")).strip()
        if not model:
            return {"ok": False, "error": {"message": "default model is not configured"}}
        temperature = float(request.get("temperature") if request.get("temperature") is not None else request.get("temperatureDefault", 0.2) or 0.2)
        max_tokens = int(request.get("maxTokens") if request.get("maxTokens") is not None else request.get("maxTokensDefault", 300) or 300)
        provider_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "system", "content": instructions},
                {
                    "role": "system",
                    "content": (
                        f"Agent package: {package_name}. "
                        f"Town title: {str(town_context.get('title', '')).strip() or 'Town Hall'}. "
                        f"Worker shops available: {len(available_shops)}. "
                        "Always respond with JSON containing reply and mode."
                    ),
                },
                {"role": "user", "content": message},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if provider_type == "zai":
            provider_payload["response_format"] = {"type": "json_object"}
            provider_payload["thinking"] = {"type": "disabled"}
        started = time.time()
        try:
            raw = adapter.chat_respond(
                base_url=str(connection.get("baseUrl", "")).strip(),
                api_key=str(secret.get("apiKey", "")).strip(),
                payload=provider_payload,
                timeout_sec=float(connection.get("timeoutSec", 20) or 20),
            )
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": {"message": f"HTTP {exc.code}"}}
        except Exception as exc:
            return {"ok": False, "error": {"message": str(exc)}}
        choices = raw.get("choices") if isinstance(raw.get("choices"), list) else []
        if not choices:
            return {"ok": False, "error": {"message": "provider returned no choices"}}
        message = (choices[0] or {}).get("message") if isinstance((choices[0] or {}).get("message"), dict) else {}
        content = str((message or {}).get("content") or "").strip()
        if not content:
            return {"ok": False, "error": {"message": "provider returned empty content"}}
        parsed = parse_json_content_block(content)
        if not isinstance(parsed, dict):
            parsed = {"reply": content, "mode": "direct"}
        parsed["ok"] = True
        parsed["reply"] = str(parsed.get("reply", "")).strip()
        parsed["mode"] = str(parsed.get("mode", "direct")).strip() or "direct"
        parsed["providerUsed"] = True
        parsed["fallbackUsed"] = False
        parsed["providerType"] = provider_type
        parsed["connectionUsed"] = cid
        parsed["modelUsed"] = model
        parsed["latencyMs"] = int((time.time() - started) * 1000)
        return parsed


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
                        provider_id=str(body.get("providerId", body.get("providerType", "zai"))).strip(),
                        auth_mode=str(body.get("authMode", "api_key")).strip(),
                        label=str(body.get("label", "")).strip(),
                        enabled=body.get("enabled") if isinstance(body.get("enabled"), bool) else None,
                        base_url=str(body.get("baseUrl", "")).strip() if "baseUrl" in body else None,
                        default_model=str(body.get("defaultModel", "")).strip() if "defaultModel" in body else None,
                        timeout_sec=int(body.get("timeoutSec", 20) or 20) if "timeoutSec" in body else None,
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
    ap.add_argument("--port", type=int, default=8796)
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
