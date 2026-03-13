from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class TuiClient:
    def __init__(self, *, root: Path, manifest_path: Path | None = None, timeout: float = 3.0):
        self.root = root
        self.timeout = timeout
        self.manifest_path = manifest_path or (root / "control-plane" / "runtime" / "service-manifest.json")
        self.manifest = self._load_json(self.manifest_path, {})
        services = self.manifest.get("services")
        self.services = services if isinstance(services, dict) else {}
        self.default_urls = {
            "policy": "http://127.0.0.1:8788",
            "catalog": "http://127.0.0.1:8786",
            "plugin-host": "http://127.0.0.1:8790",
            "execution": "http://127.0.0.1:8794",
            "session": "http://127.0.0.1:8784",
            "router": "http://127.0.0.1:8795",
            "orchestrator": "http://127.0.0.1:8787",
            "approval": "http://127.0.0.1:8793",
            "memory-core": "http://127.0.0.1:8792",
            "memory": "http://127.0.0.1:8791",
        }

    def _load_json(self, path: Path, default):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def service_url(self, name: str) -> str:
        row = self.services.get(name)
        if isinstance(row, dict) and str(row.get("url", "")).strip():
            return str(row.get("url", "")).rstrip("/")
        return self.default_urls.get(name, "").rstrip("/")

    def _request(self, method: str, url: str, payload: dict | None = None):
        headers = {"Content-Type": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, method=method, headers=headers, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return {"ok": True, "status": resp.status, "json": json.loads(body) if body else {}}
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8")
            except Exception:
                body = ""
            parsed = {}
            try:
                parsed = json.loads(body) if body else {}
            except Exception:
                parsed = {"raw": body}
            return {"ok": False, "status": exc.code, "json": parsed}
        except Exception as exc:
            return {"ok": False, "status": 0, "json": {"error": {"message": str(exc)}}}

    def health(self, service: str, path: str) -> dict:
        base = self.service_url(service)
        if not base:
            return {"service": service, "ok": False, "status": "missing-url"}
        out = self._request("GET", f"{base}{path}")
        payload = out["json"]
        ok = bool(out["ok"] and isinstance(payload, dict) and payload.get("status") == "ok")
        return {
            "service": service,
            "url": base,
            "ok": ok,
            "status": payload.get("status", "down") if isinstance(payload, dict) else "down",
            "payload": payload,
        }

    def list_sessions(self) -> list[dict]:
        root = self.root / "control-plane" / "observability" / "sessions"
        rows: list[dict] = []
        if not root.exists():
            return rows
        for session_file in sorted(root.glob("*/session.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            data = self._load_json(session_file, {})
            if isinstance(data, dict):
                rows.append(data)
        return rows

    def get_session(self, session_id: str) -> dict:
        base = self.service_url("session")
        out = self._request("GET", f"{base}/v1/sessions/{urllib.parse.quote(session_id)}")
        return out["json"]

    def create_session(self, *, agent_id: str, title: str) -> dict:
        base = self.service_url("session")
        return self._request(
            "POST",
            f"{base}/v1/sessions",
            {"agentId": agent_id, "title": title},
        )["json"]

    def append_message(self, *, session_id: str, role: str, content: str) -> dict:
        base = self.service_url("session")
        return self._request(
            "POST",
            f"{base}/v1/sessions/{urllib.parse.quote(session_id)}/messages",
            {"role": role, "content": content},
        )["json"]

    def list_agent_packages(self) -> dict:
        base = self.service_url("session")
        return self._request("GET", f"{base}/v1/agent-packages")["json"]

    def runtime_capabilities(self) -> dict:
        base = self.service_url("router")
        return self._request("GET", f"{base}/v1/router/runtime-capabilities")["json"]

    def home_snapshot(self) -> dict:
        health_checks = [
            self.health("policy", "/v1/policy/health"),
            self.health("catalog", "/v1/catalog/health"),
            self.health("plugin-host", "/v1/plugin-host/health"),
            self.health("execution", "/v1/execution/health"),
            self.health("session", "/v1/session/health"),
            self.health("router", "/v1/router/health"),
            self.health("orchestrator", "/v1/orchestrator/health"),
        ]
        sessions = self.list_sessions()
        packages = self.list_agent_packages()
        return {
            "services": health_checks,
            "sessions": [
                {
                    "sessionId": row.get("sessionId", ""),
                    "title": row.get("title", ""),
                    "mayorAgentId": row.get("mayorAgentId", ""),
                    "heartbeatStatus": row.get("heartbeatStatus", ""),
                    "updatedAt": row.get("updatedAt", ""),
                }
                for row in sessions
            ],
            "agentPackages": (packages.get("packages") if isinstance(packages, dict) else []) or [],
            "runtimeCapabilities": self.runtime_capabilities(),
        }
