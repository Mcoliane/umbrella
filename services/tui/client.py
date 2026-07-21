from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class TuiClient:
    def __init__(self, *, root: Path, manifest_path: Path | None = None, timeout: float = 5.0):
        self.root = root
        self.timeout = timeout
        self.platform_manifest_path = root / "control-plane" / "runtime" / "platform-manifest.json"
        self.manifest_path = manifest_path or (root / "control-plane" / "runtime" / "service-manifest.json")
        self.manifest = {}
        self.platform_manifest = {}
        self.services = {}
        self.service_overrides = {}
        # Last-resort fallbacks used only when the manifest lacks an entry;
        # each port mirrors the argparse default in that service's app.py.
        self.default_urls = {
            "policy": "http://127.0.0.1:8791",
            "catalog": "http://127.0.0.1:8786",
            "plugin-host": "http://127.0.0.1:8785",
            "execution": "http://127.0.0.1:8794",
            "session": "http://127.0.0.1:8784",
            "model-broker": "http://127.0.0.1:8782",
            "router": "http://127.0.0.1:8795",
            "orchestrator": "http://127.0.0.1:8797",
            "approval": "http://127.0.0.1:8792",
            "memory-core": "http://127.0.0.1:8798",
            "memory": "http://127.0.0.1:8787",
        }
        self.platform_script = root / "scripts" / "control-plane" / "manage-platform-stack"
        self.reload_manifests()

    def _load_json(self, path: Path, default):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def reload_manifests(self):
        manifest = self._load_json(self.manifest_path, {})
        platform_manifest = self._load_json(self.platform_manifest_path, {})
        platform_services = platform_manifest.get("services") if isinstance(platform_manifest, dict) else {}
        services = {}
        if isinstance(platform_services, dict):
            services.update(platform_services)
        file_services = manifest.get("services") if isinstance(manifest, dict) else {}
        if isinstance(file_services, dict):
            services.update(file_services)
        self.manifest = manifest if isinstance(manifest, dict) else {}
        self.platform_manifest = platform_manifest if isinstance(platform_manifest, dict) else {}
        self.services = services if isinstance(services, dict) else {}

    def service_url(self, name: str) -> str:
        self.reload_manifests()
        override = self.service_overrides.get(name)
        if str(override or "").strip():
            return str(override).rstrip("/")
        row = self.services.get(name)
        if isinstance(row, dict) and str(row.get("url", "")).strip():
            return str(row.get("url", "")).rstrip("/")
        return self.default_urls.get(name, "").rstrip("/")

    def set_service_url(self, name: str, url: str) -> None:
        self.service_overrides[str(name).strip()] = str(url).rstrip("/")

    def _mesh_token(self) -> str:
        token_path = self.root / "control-plane" / "runtime" / "platform-token.txt"
        if not token_path.exists():
            return ""
        try:
            return token_path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def _auth_headers(self) -> dict:
        token = self._mesh_token()
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    def _request(
        self,
        method: str,
        url: str,
        payload: dict | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
    ):
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(self._auth_headers())
        if isinstance(headers, dict):
            request_headers.update(headers)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, method=method, headers=request_headers, data=data)
        try:
            with urllib.request.urlopen(req, timeout=(timeout if timeout is not None else self.timeout)) as resp:
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
        out = self._request("GET", f"{base}/v1/sessions/{urllib.parse.quote(session_id)}", timeout=15.0)
        return out["json"]

    def create_session(self, *, agent_id: str, title: str) -> dict:
        base = self.service_url("session")
        return self._request(
            "POST",
            f"{base}/v1/sessions",
            {"agentId": agent_id, "title": title},
            timeout=20.0,
        )["json"]

    def heartbeat_session(self, *, session_id: str, seen_by: str = "tui") -> dict:
        base = self.service_url("session")
        return self._request(
            "POST",
            f"{base}/v1/sessions/{urllib.parse.quote(session_id)}/heartbeat",
            {"seenBy": seen_by},
            timeout=10.0,
        )["json"]

    def append_message(self, *, session_id: str, role: str, content: str) -> dict:
        base = self.service_url("session")
        return self._request(
            "POST",
            f"{base}/v1/sessions/{urllib.parse.quote(session_id)}/messages",
            {"role": role, "content": content},
        )["json"]

    def register_agent(self, *, agent_id: str, capabilities: list[str]) -> dict:
        base = self.service_url("policy")
        return self._request(
            "POST",
            f"{base}/v1/policy/agents/register",
            {"agentId": agent_id, "source": "tui", "capabilities": capabilities},
        )["json"]

    def invoke_action(self, *, session_id: str, shop_id: str, action_id: str, inputs: dict, metadata: dict | None = None) -> dict:
        base = self.service_url("session")
        return self._request(
            "POST",
            f"{base}/v1/sessions/{urllib.parse.quote(session_id)}/invoke-action",
            {
                "shopId": shop_id,
                "actionId": action_id,
                "inputs": inputs,
                "metadata": metadata or {},
            },
        )["json"]

    def create_turn(self, *, session_id: str, objective: str, requested_by: str = "user", metadata: dict | None = None) -> dict:
        base = self.service_url("session")
        return self._request(
            "POST",
            f"{base}/v1/sessions/{urllib.parse.quote(session_id)}/turns",
            {"objective": objective, "requestedBy": requested_by, "metadata": metadata or {}},
        )["json"]

    def orchestrate_turn(self, *, session_id: str, turn_id: str, plan: list[dict], metadata: dict | None = None) -> dict:
        base = self.service_url("session")
        return self._request(
            "POST",
            f"{base}/v1/sessions/{urllib.parse.quote(session_id)}/orchestrate-turn",
            {"turnId": turn_id, "plan": plan, "metadata": metadata or {}},
        )["json"]

    def converse(self, *, session_id: str, target: str, content: str) -> dict:
        base = self.service_url("session")
        return self._request(
            "POST",
            f"{base}/v1/sessions/{urllib.parse.quote(session_id)}/converse",
            {
                "target": target,
                "content": content,
            },
            timeout=180.0,
        )["json"]

    def list_agent_packages(self) -> dict:
        base = self.service_url("session")
        return self._request("GET", f"{base}/v1/agent-packages")["json"]

    def model_provider_status(self) -> dict:
        base = self.service_url("session")
        return self._request("GET", f"{base}/v1/runtime/model-provider", timeout=15.0)["json"]

    def test_model_provider(self) -> dict:
        base = self.service_url("session")
        return self._request("POST", f"{base}/v1/runtime/model-provider/test", {}, timeout=90.0)["json"]

    def save_model_provider(self, *, enabled: bool | None = None, provider: dict | None = None, api_key: str | None = None) -> dict:
        base = self.service_url("session")
        payload: dict = {}
        if enabled is not None:
            payload["enabled"] = bool(enabled)
        if isinstance(provider, dict):
            payload["provider"] = provider
        if api_key is not None:
            payload["apiKey"] = api_key
        return self._request("POST", f"{base}/v1/runtime/model-provider", payload, timeout=20.0)["json"]

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
            self.health("model-broker", "/v1/model-broker/health"),
            self.health("router", "/v1/router/health"),
            self.health("orchestrator", "/v1/orchestrator/health"),
        ]
        sessions = self.list_sessions()
        packages = self.list_agent_packages()
        model_provider = self.model_provider_status()
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
            "modelProvider": model_provider if isinstance(model_provider, dict) else {},
            "runtimeCapabilities": self.runtime_capabilities(),
            "platformStack": self.platform_status(),
        }

    def platform_status(self) -> dict:
        self.reload_manifests()
        if not self.platform_script.exists():
            return {"ok": False, "reason": "script_missing", "manifest": str(self.platform_manifest_path)}
        proc = subprocess.run(
            [str(self.platform_script), "status", "--umbrella-root", str(self.root), "--manifest", str(self.platform_manifest_path)],
            cwd=str(self.root),
            capture_output=True,
            text=True,
        )
        body = (proc.stdout or "").strip()
        if not body:
            return {"ok": False, "reason": "no_output", "stderr": (proc.stderr or "").strip()}
        try:
            return json.loads(body)
        except Exception:
            return {"ok": False, "reason": "invalid_json", "stdout": body, "stderr": (proc.stderr or "").strip()}

    def platform_action(self, action: str, *, profile: str = "full") -> dict:
        if not self.platform_script.exists():
            return {"ok": False, "reason": "script_missing", "manifest": str(self.platform_manifest_path)}
        cmd = [str(self.platform_script), action, "--umbrella-root", str(self.root), "--manifest", str(self.platform_manifest_path)]
        if action == "bringup":
            cmd.extend(["--profile", profile])
        proc = subprocess.run(cmd, cwd=str(self.root), capture_output=True, text=True)
        body = (proc.stdout or "").strip()
        if not body:
            return {"ok": False, "reason": "no_output", "stderr": (proc.stderr or "").strip(), "exitCode": proc.returncode}
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body}
        if proc.returncode != 0:
            parsed["ok"] = False
            parsed["exitCode"] = proc.returncode
            if proc.stderr:
                parsed["stderr"] = proc.stderr.strip()
        self.reload_manifests()
        return parsed
