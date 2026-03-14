from __future__ import annotations

import argparse
import curses
import json
import textwrap
from pathlib import Path

from services.tui.client import TuiClient
from services.tui.state import PlatformState

ZAI_CODING_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
ZAI_GLM47_MODEL = "glm-4.7"


def _clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    return value[:width]


class UmbrellaTui:
    def __init__(self, *, root: Path, manifest: Path | None = None, session_id: str = ""):
        self.root = root
        self.client = TuiClient(root=root, manifest_path=manifest)
        self.state = PlatformState(selected_session_id=session_id)
        self.session_cursor = 0

    def add_local_event(self, role: str, content: str):
        self.state.local_transcript.append({"role": role, "content": content})
        self.state.local_transcript = self.state.local_transcript[-30:]

    def refresh_home(self):
        self.state.home = self.client.home_snapshot()
        sessions = self.state.home.get("sessions") or []
        if sessions:
            session_ids = [str(row.get("sessionId", "")) for row in sessions]
            if self.state.selected_session_id not in session_ids:
                self.session_cursor = 0
                self.state.selected_session_id = session_ids[0]
            else:
                self.session_cursor = max(0, session_ids.index(self.state.selected_session_id))
        else:
            self.session_cursor = 0
            self.state.selected_session_id = ""
        self.state.status = "Refreshed"

    def refresh_session(self):
        if not self.state.selected_session_id:
            self.state.session = {}
            return
        self.state.session = self.client.get_session(self.state.selected_session_id)
        if not self.session_payload:
            error = self.state.session.get("error") if isinstance(self.state.session, dict) else {}
            message = str((error or {}).get("message", "")).strip() or "session unavailable"
            self.state.status = f"Town load failed: {message}"
            return
        session = self.session_payload
        agents = session.get("agents") or []
        valid_targets = [str(agent.get("agentId", "")).strip() for agent in agents if str(agent.get("agentId", "")).strip()]
        if self.state.active_target not in valid_targets and valid_targets:
            self.state.active_target = valid_targets[0]
        self.state.status = f"Town loaded: {self.state.selected_session_id}"

    @property
    def session_payload(self) -> dict:
        if isinstance(self.state.session, dict) and isinstance(self.state.session.get("session"), dict):
            return self.state.session["session"]
        return self.state.session if isinstance(self.state.session, dict) else {}

    def start_platform(self, profile: str):
        out = self.client.platform_action("bringup", profile=profile)
        self.client.manifest_path = self.client.platform_manifest_path
        self.client.reload_manifests()
        self.refresh_home()
        if out.get("ok", True):
            self.add_local_event("system", f"Started platform stack ({profile}).")
            self.state.status = f"Started platform stack ({profile})"
            return
        self.add_local_event("error", f"Platform start failed: {json.dumps(out, ensure_ascii=False)[:180]}")
        self.state.status = "Platform start failed"

    def stop_platform(self):
        out = self.client.platform_action("shutdown")
        self.client.manifest_path = self.client.platform_manifest_path
        self.client.reload_manifests()
        self.refresh_home()
        if out.get("ok", True):
            self.add_local_event("system", "Stopped platform stack.")
            self.state.status = "Stopped platform stack"
            return
        self.add_local_event("error", f"Platform stop failed: {json.dumps(out, ensure_ascii=False)[:180]}")
        self.state.status = "Platform stop failed"

    def create_session(self, screen, title_override: str = ""):
        agent_id = self.prompt(screen, "Mayor agent id", default="mayor")
        if agent_id is None:
            return
        title_default = title_override or "Town Hall"
        title = self.prompt(screen, "Town title", default=title_default)
        if title is None:
            return
        created = self.client.create_session(agent_id=agent_id, title=title)
        session = created.get("session") if isinstance(created, dict) and isinstance(created.get("session"), dict) else {}
        session_id = str(session.get("sessionId", "")).strip()
        if session_id:
            self.refresh_home()
            self.state.selected_session_id = session_id
            self.state.active_target = str(session.get("mayorAgentId", "mayor")).strip() or "mayor"
            self.refresh_session()
            self.add_local_event("system", f'Created town "{title}" ({session_id}).')
            self.state.status = f"Created town session {session_id}"
            return
        error = created.get("error") if isinstance(created, dict) else {}
        message = str((error or {}).get("message", "")).strip()
        if not message:
            message = json.dumps(created, ensure_ascii=False)[:180]
        self.add_local_event("error", f"Create town failed: {message}")
        self.state.status = f"Create town failed: {message[:90]}"

    def show_model_provider(self):
        provider = self.state.home.get("modelProvider") if isinstance(self.state.home.get("modelProvider"), dict) else {}
        broker_meta = provider.get("broker") if isinstance(provider.get("broker"), dict) else {}
        connection_meta = provider.get("connection") if isinstance(provider.get("connection"), dict) else {}
        provider_meta = provider.get("provider") if isinstance(provider.get("provider"), dict) else {}
        configured = bool(provider.get("configured", False))
        enabled = bool(provider.get("enabled", False))
        model = str(provider_meta.get("defaultModel", "")).strip() or "unset"
        base_url = str(provider_meta.get("baseUrl", "")).strip() or "unset"
        key_masked = str(((provider.get("secrets") or {}).get("apiKeyMasked", ""))).strip() or "missing"
        self.add_local_event(
            "system",
            (
                f"Model broker: {'enabled' if enabled else 'disabled'} configured={configured} "
                f"connection={connection_meta.get('id','')} provider={provider_meta.get('type','')} model={model} "
                f"base={base_url} brokerUrl={broker_meta.get('url','')} key={key_masked}"
            ),
        )
        self.state.status = "Model broker"

    def _recommended_provider_defaults(self, provider_type: str, current: dict) -> dict:
        normalized = str(provider_type or "").strip().lower()
        provider_meta = current.get("provider") if isinstance(current.get("provider"), dict) else {}
        if normalized == "zai":
            return {
                "type": "zai",
                "baseUrl": str(provider_meta.get("baseUrl", "")).strip() or ZAI_CODING_BASE_URL,
                "defaultModel": str(provider_meta.get("defaultModel", "")).strip() or ZAI_GLM47_MODEL,
                "timeoutSec": int(provider_meta.get("timeoutSec", 20) or 20),
            }
        return {
            "type": normalized or str(provider_meta.get("type", "openai-compatible")).strip() or "openai-compatible",
            "baseUrl": str(provider_meta.get("baseUrl", "")).strip(),
            "defaultModel": str(provider_meta.get("defaultModel", "")).strip(),
            "timeoutSec": int(provider_meta.get("timeoutSec", 20) or 20),
        }

    def save_glm47_preset(self, screen):
        current = self.state.home.get("modelProvider") if isinstance(self.state.home.get("modelProvider"), dict) else {}
        provider = self._recommended_provider_defaults("zai", current)
        api_key = self.prompt(screen, "Z.ai API key (blank keeps existing)", default="")
        if api_key is None:
            return
        out = self.client.save_model_provider(
            enabled=True,
            provider=provider,
            api_key=None if not str(api_key).strip() else str(api_key).strip(),
        )
        self.refresh_home()
        if out.get("saved"):
            self.add_local_event(
                "system",
                f'Saved Z.ai preset {provider["defaultModel"]} at {provider["baseUrl"]}. Run /model test next.',
            )
            self.state.status = "glm-4.7 preset saved"
            return
        self.add_local_event("error", f"GLM-4.7 preset failed: {json.dumps(out, ensure_ascii=False)[:180]}")
        self.state.status = "glm-4.7 preset failed"

    def setup_model_provider(self, screen):
        current = self.state.home.get("modelProvider") if isinstance(self.state.home.get("modelProvider"), dict) else {}
        provider_meta = current.get("provider") if isinstance(current.get("provider"), dict) else {}
        provider_type = self.prompt(screen, "Provider type", default=str(provider_meta.get("type", "zai")).strip() or "zai")
        if provider_type is None:
            return
        provider_type = str(provider_type).strip().lower() or "zai"
        if provider_type not in {"zai", "openai-compatible"}:
            self.add_local_event("error", f"Unsupported provider type: {provider_type}")
            self.state.status = "Model setup failed"
            return
        recommended = self._recommended_provider_defaults(provider_type, current)
        if provider_type == "zai":
            self.add_local_event(
                "system",
                f'Recommended Z.ai preset: base={recommended["baseUrl"]} model={recommended["defaultModel"]}',
            )
        base_url = self.prompt(screen, "Base URL", default=str(recommended.get("baseUrl", "")).strip())
        if base_url is None:
            return
        model = self.prompt(screen, "Default model", default=str(recommended.get("defaultModel", "")).strip())
        if model is None:
            return
        timeout_raw = self.prompt(screen, "Timeout seconds", default=str(recommended.get("timeoutSec", 20) or 20))
        if timeout_raw is None:
            return
        api_key = self.prompt(screen, "API key (blank keeps existing)", default="")
        if api_key is None:
            return
        try:
            timeout_sec = int(str(timeout_raw).strip() or "20")
        except Exception:
            self.add_local_event("error", "Invalid timeout seconds")
            self.state.status = "Model setup failed"
            return
        out = self.client.save_model_provider(
            enabled=True,
            provider={
                "type": provider_type,
                "baseUrl": str(base_url).strip(),
                "defaultModel": str(model).strip(),
                "timeoutSec": timeout_sec,
            },
            api_key=None if not str(api_key).strip() else str(api_key).strip(),
        )
        self.refresh_home()
        if out.get("saved"):
            self.add_local_event("system", f"Saved {provider_type} model broker connection.")
            self.state.status = f"{provider_type} broker saved"
            return
        self.add_local_event("error", f"Model setup failed: {json.dumps(out, ensure_ascii=False)[:180]}")
        self.state.status = "Model broker setup failed"

    def test_model_provider(self):
        out = self.client.test_model_provider()
        result = out.get("test") if isinstance(out.get("test"), dict) else {}
        if result.get("ok"):
            self.add_local_event("system", f'Model test ok model={result.get("model","")} latencyMs={result.get("latencyMs","")}')
            self.state.status = "Model test ok"
            return
        self.add_local_event("error", f'Model test failed: {result.get("message","not configured")}')
        self.state.status = "Model test failed"

    def prompt(self, screen, label: str, default: str = "") -> str | None:
        curses.echo()
        try:
            height, width = screen.getmaxyx()
            prompt = label
            if default:
                prompt += f" [{default}]"
            prompt += ": "
            screen.attron(curses.A_REVERSE)
            screen.addstr(height - 1, 0, " " * max(1, width - 1))
            screen.addstr(height - 1, 0, _clip(prompt, width - 1))
            screen.attroff(curses.A_REVERSE)
            screen.refresh()
            raw = screen.getstr(height - 1, min(len(prompt), max(0, width - 2)), max(1, width - len(prompt) - 1))
            value = raw.decode("utf-8", errors="ignore").strip()
            return value or default
        except KeyboardInterrupt:
            return None
        finally:
            curses.noecho()

    def choose_session(self, screen):
        sessions = self.state.home.get("sessions") or []
        if not sessions:
            self.state.status = "No town sessions yet"
            self.add_local_event("system", "No town sessions available. Use /new or n to create one.")
            return
        choices = []
        for idx, row in enumerate(sessions, start=1):
            sid = str(row.get("sessionId", "")).strip()
            title = str(row.get("title", "")).strip()
            choices.append(f"{idx}:{sid} {title}".strip())
        self.add_local_event("system", "Sessions: " + " | ".join(choices[:6]))
        choice = self.prompt(screen, "Session number or id", default=str(self.session_cursor + 1))
        if choice is None:
            return
        selected = None
        if choice.isdigit():
            index = int(choice) - 1
            if 0 <= index < len(sessions):
                selected = sessions[index]
                self.session_cursor = index
        else:
            for idx, row in enumerate(sessions):
                if str(row.get("sessionId", "")).strip() == choice.strip():
                    selected = row
                    self.session_cursor = idx
                    break
        if not selected:
            self.state.status = "Unknown session"
            return
        self.state.selected_session_id = str(selected.get("sessionId", "")).strip()
        self.refresh_session()
        self.add_local_event("system", f"Opened town {self.state.selected_session_id}.")

    def cycle_target(self):
        session = self.session_payload
        agents = [str(agent.get("agentId", "")).strip() for agent in (session.get("agents") or []) if str(agent.get("agentId", "")).strip()]
        if not agents:
            self.state.status = "No agents in town"
            return
        if self.state.active_target not in agents:
            self.state.active_target = agents[0]
        else:
            index = agents.index(self.state.active_target)
            self.state.active_target = agents[(index + 1) % len(agents)]
        self.state.status = f"Target: {self.state.active_target}"

    def talk(self, screen, content_override: str = "", target_override: str = ""):
        if not self.state.selected_session_id:
            self.state.status = "Open a town first"
            return
        target = target_override.strip() or self.state.active_target or "mayor"
        content = content_override
        if not content:
            content = self.prompt(screen, f"Message to {target}", default="") or ""
        if not content.strip():
            return
        heartbeat = self.client.heartbeat_session(session_id=self.state.selected_session_id, seen_by="system")
        if isinstance(heartbeat, dict) and heartbeat.get("error"):
            error = heartbeat.get("error") if isinstance(heartbeat.get("error"), dict) else {}
            message = str(error.get("message", "")).strip() or json.dumps(heartbeat, ensure_ascii=False)[:180]
            self.add_local_event("error", f"Heartbeat failed: {message}")
            self.state.status = "Heartbeat failed"
            return
        out = self.client.converse(session_id=self.state.selected_session_id, target=target, content=content.strip())
        if out.get("ok"):
            self.state.active_target = str(out.get("target", target)).strip() or target
            self.refresh_session()
            reply = str(out.get("reply", "")).strip()
            self.state.status = f"Talked to {self.state.active_target}"
            if reply and not any(str(msg.get("content", "")).strip() == reply for msg in self.state.local_transcript[-2:]):
                self.add_local_event("system", f"{self.state.active_target} replied.")
            return
        error = out.get("error") if isinstance(out, dict) and isinstance(out.get("error"), dict) else {}
        message = str(error.get("message", "")).strip()
        if not message:
            message = json.dumps(out, ensure_ascii=False)[:180]
        self.add_local_event("error", f"Conversation failed: {message}")
        self.state.status = f"Conversation failed: {message[:80]}"

    def handle_command(self, screen, raw: str):
        command = str(raw or "").strip()
        if not command:
            return
        if not command.startswith("/"):
            self.talk(screen, content_override=command)
            return
        parts = command[1:].split()
        name = parts[0].lower() if parts else "help"
        args = parts[1:]
        if name in {"help", "h", "?"}:
            self.add_local_event(
                "system",
                "Commands: /help /status /new [title] /sessions /session <id|n> /agent <id> /shops /workers /model /model setup /model glm47 /model test /model use <model> /model disable /refresh /start [full|core] /stop /quit",
            )
            self.state.status = "Help"
            return
        if name == "status":
            platform = self.state.home.get("platformStack") or {}
            sessions = self.state.home.get("sessions") or []
            self.add_local_event(
                "system",
                f'Platform {"UP" if platform.get("ok") else "DOWN"} profile={platform.get("profile","")} sessions={len(sessions)} target={self.state.active_target}',
            )
            self.state.status = "Status"
            return
        if name == "new":
            self.create_session(screen, title_override=" ".join(args).strip())
            return
        if name == "sessions":
            self.choose_session(screen)
            return
        if name == "session":
            if not args:
                self.choose_session(screen)
                return
            self.state.selected_session_id = args[0].strip()
            self.refresh_session()
            self.add_local_event("system", f"Opened town {self.state.selected_session_id}.")
            return
        if name == "agent":
            if not args:
                self.add_local_event("system", f"Current target: {self.state.active_target}")
                return
            self.state.active_target = args[0].strip()
            self.state.status = f"Target: {self.state.active_target}"
            self.add_local_event("system", f"Conversation target set to {self.state.active_target}.")
            return
        if name == "shops":
            session = self.session_payload
            shops = session.get("shops") if isinstance(session.get("shops"), dict) else {}
            if not shops:
                self.add_local_event("system", "No shops in town.")
                return
            summary = " | ".join(f"{shop_id}:{row.get('ownerAgentId','')}" for shop_id, row in list(shops.items())[:8])
            self.add_local_event("system", f"Shops: {summary}")
            return
        if name == "workers":
            session = self.session_payload
            workers = [
                str(agent.get("agentId", "")).strip()
                for agent in (session.get("agents") or [])
                if str(agent.get("role", "")).strip() not in {"mayor", "originator"} and str(agent.get("agentId", "")).strip()
            ]
            self.add_local_event("system", f'Workers: {", ".join(workers) if workers else "none"}')
            return
        if name == "model":
            if not args:
                self.show_model_provider()
                return
            sub = args[0].lower()
            if sub == "setup":
                self.setup_model_provider(screen)
                return
            if sub in {"glm47", "glm-4.7"}:
                self.save_glm47_preset(screen)
                return
            if sub == "test":
                self.test_model_provider()
                return
            if sub == "disable":
                out = self.client.save_model_provider(enabled=False)
                self.refresh_home()
                if out.get("saved"):
                    self.add_local_event("system", "Model provider disabled.")
                    self.state.status = "Model provider disabled"
                    return
                self.add_local_event("error", f"Model disable failed: {json.dumps(out, ensure_ascii=False)[:180]}")
                self.state.status = "Model disable failed"
                return
            if sub == "use" and len(args) > 1:
                current = self.state.home.get("modelProvider") if isinstance(self.state.home.get("modelProvider"), dict) else {}
                provider_meta = current.get("provider") if isinstance(current.get("provider"), dict) else {}
                out = self.client.save_model_provider(
                    provider={
                        "type": str(provider_meta.get("type", "zai")).strip() or "zai",
                        "baseUrl": str(provider_meta.get("baseUrl", "")).strip(),
                        "defaultModel": str(args[1]).strip(),
                        "timeoutSec": int(provider_meta.get("timeoutSec", 20) or 20),
                    }
                )
                self.refresh_home()
                if out.get("saved"):
                    self.add_local_event("system", f"Default model set to {args[1].strip()}.")
                    self.state.status = "Model updated"
                    return
                self.add_local_event("error", f"Model update failed: {json.dumps(out, ensure_ascii=False)[:180]}")
                self.state.status = "Model update failed"
                return
            self.add_local_event("error", f"Unknown model command: {' '.join(args)}")
            self.state.status = "Unknown model command"
            return
        if name == "refresh":
            self.refresh_home()
            self.refresh_session()
            self.state.status = "Refreshed"
            return
        if name == "start":
            profile = args[0].strip() if args else "full"
            if profile not in {"full", "core"}:
                self.add_local_event("error", f"Unknown profile: {profile}")
                return
            self.start_platform(profile)
            return
        if name == "stop":
            self.stop_platform()
            return
        if name in {"quit", "exit"}:
            raise SystemExit(0)
        self.add_local_event("error", f"Unknown command: {command}")

    def draw_header(self, screen):
        height, width = screen.getmaxyx()
        session = self.session_payload
        title = str(session.get("title", "Town Hall")).strip() or "Town Hall"
        session_id = self.state.selected_session_id or "no-town"
        header = f" Umbrella Town Hall | {title} | {session_id} | target={self.state.active_target} "
        screen.attron(curses.A_REVERSE)
        screen.addstr(0, 0, " " * max(1, width - 1))
        screen.addstr(0, 0, _clip(header, width - 1))
        screen.attroff(curses.A_REVERSE)

    def draw_footer(self, screen):
        height, width = screen.getmaxyx()
        footer = "enter message | / command | tab target | s sessions | n new town | S/F5 full | c/F6 core | x/F7 stop | q quit"
        line = f"{self.state.status} | {footer}"
        screen.attron(curses.A_REVERSE)
        screen.addstr(height - 1, 0, " " * max(1, width - 1))
        screen.addstr(height - 1, 0, _clip(line, width - 1))
        screen.attroff(curses.A_REVERSE)

    def _transcript_rows(self, width: int) -> list[tuple[int, str]]:
        rows: list[tuple[int, str]] = []
        session = self.session_payload
        for event in self.state.local_transcript[-10:]:
            prefix = str(event.get("role", "system")).upper()
            content = str(event.get("content", ""))
            for line in textwrap.wrap(f"{prefix}: {content}", max(12, width)):
                rows.append((curses.A_DIM, line))
        for message in (session.get("messages") or [])[-80:]:
            role = str(message.get("role", "message")).lower()
            if role == "tool":
                continue
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            target = str(metadata.get("target", "")).strip()
            label = role
            if role == "assistant" and target:
                label = f"{target}"
            if role == "user":
                label = "you"
            content = str(message.get("content", ""))
            attr = curses.A_NORMAL
            if role == "assistant":
                attr = curses.A_BOLD
            elif role not in {"user", "assistant"}:
                attr = curses.A_DIM
            for line in textwrap.wrap(f"{label}: {content}", max(12, width)):
                rows.append((attr, line))
        return rows[-200:]

    def draw_transcript(self, screen):
        height, width = screen.getmaxyx()
        sidebar_w = max(28, min(42, width // 3))
        transcript_w = max(24, width - sidebar_w - 5)
        screen.addstr(2, 2, "Transcript", curses.A_BOLD)
        rows = self._transcript_rows(transcript_w - 2)
        start_y = 3
        available_h = max(3, height - 5)
        visible = rows[-available_h:]
        y = start_y
        for attr, line in visible:
            if y >= height - 1:
                break
            screen.addstr(y, 2, _clip(line, transcript_w - 1), attr)
            y += 1
        if not visible:
            screen.addstr(4, 2, "No transcript yet. Create or open a town and press Enter to speak.")

    def draw_sidebar(self, screen):
        height, width = screen.getmaxyx()
        sidebar_w = max(28, min(42, width // 3))
        x = width - sidebar_w - 2
        session = self.session_payload
        platform = self.state.home.get("platformStack") or {}
        services = self.state.home.get("services") or []
        model_provider = self.state.home.get("modelProvider") if isinstance(self.state.home.get("modelProvider"), dict) else {}
        line = 2
        screen.addstr(line, x, "Town State", curses.A_BOLD)
        line += 1
        if session:
            screen.addstr(line, x, _clip(f'Mayor: {session.get("mayorAgentId","")}', sidebar_w))
            line += 1
            screen.addstr(line, x, _clip(f'Heartbeat: {session.get("heartbeatStatus","")}', sidebar_w))
            line += 1
        else:
            screen.addstr(line, x, "No town selected")
            line += 1
        screen.addstr(line, x, _clip(f'Platform: {"UP" if platform.get("ok") else "DOWN"} {platform.get("profile","")}', sidebar_w))
        line += 2
        provider_meta = model_provider.get("provider") if isinstance(model_provider.get("provider"), dict) else {}
        configured = "configured" if model_provider.get("configured") else "missing"
        model_name = str(provider_meta.get("defaultModel", "")).strip() or "unset"
        provider_name = str(provider_meta.get("type", "")).strip() or "unset"
        screen.addstr(line, x, _clip(f"Model: {provider_name} / {model_name} ({configured})", sidebar_w))
        line += 2
        screen.addstr(line, x, "Agents", curses.A_BOLD)
        line += 1
        for agent in (session.get("agents") or [])[: max(1, (height // 3) - 3)]:
            aid = str(agent.get("agentId", "")).strip()
            role = str(agent.get("role", "")).strip()
            marker = ">" if aid == self.state.active_target else " "
            screen.addstr(line, x, _clip(f"{marker} {aid} [{role}]", sidebar_w))
            line += 1
        line += 1
        shops = session.get("shops") if isinstance(session.get("shops"), dict) else {}
        screen.addstr(line, x, "Shops", curses.A_BOLD)
        line += 1
        for shop_id, row in list(shops.items())[: max(1, (height // 4) - 2)]:
            name = str(row.get("name", "")).strip()
            screen.addstr(line, x, _clip(f"{shop_id}: {name}", sidebar_w))
            line += 1
        line += 1
        screen.addstr(line, x, "Services", curses.A_BOLD)
        line += 1
        for row in services[: max(1, height - line - 2)]:
            status = "UP" if row.get("ok") else "DN"
            screen.addstr(line, x, _clip(f'{status} {row.get("service","")}', sidebar_w))
            line += 1

    def draw_town(self, screen):
        self.draw_header(screen)
        self.draw_transcript(screen)
        self.draw_sidebar(screen)
        self.draw_footer(screen)

    def run(self, screen):
        curses.curs_set(0)
        screen.keypad(True)
        self.refresh_home()
        if self.state.selected_session_id:
            self.refresh_session()
        else:
            self.add_local_event("system", "Welcome to Town Hall. Start the platform with /start full, then create a town with /new.")
        while True:
            screen.erase()
            self.draw_town(screen)
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                break
            if key in (ord("S"), curses.KEY_F5):
                self.start_platform("full")
                continue
            if key in (ord("C"), ord("c"), curses.KEY_F6):
                self.start_platform("core")
                continue
            if key in (ord("X"), ord("x"), curses.KEY_F7):
                self.stop_platform()
                continue
            if key in (ord("n"), ord("N")):
                self.create_session(screen)
                continue
            if key in (ord("r"), ord("R")):
                self.refresh_home()
                self.refresh_session()
                continue
            if key in (ord("s"),):
                self.choose_session(screen)
                continue
            if key == 9:
                self.cycle_target()
                continue
            if key in (10, 13):
                self.talk(screen)
                continue
            if key == ord("/"):
                raw = self.prompt(screen, "Command", default="/help")
                if raw is None:
                    continue
                try:
                    self.handle_command(screen, raw if raw.startswith("/") else f"/{raw}")
                except SystemExit:
                    break
                continue


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Umbrella platform TUI")
    ap.add_argument("--umbrella-root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--manifest", default="")
    ap.add_argument("--session-id", default="")
    ap.add_argument("--dump-home", action="store_true", help="Print the home snapshot as JSON and exit")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    root = Path(args.umbrella_root).resolve()
    if str(args.manifest).strip():
        manifest = Path(args.manifest).resolve()
    else:
        platform_manifest = root / "control-plane" / "runtime" / "platform-manifest.json"
        service_manifest = root / "control-plane" / "runtime" / "service-manifest.json"
        manifest = platform_manifest if platform_manifest.exists() else service_manifest
    app = UmbrellaTui(root=root, manifest=manifest, session_id=args.session_id.strip())
    if args.dump_home:
        print(json.dumps(app.client.home_snapshot(), indent=2))
        return 0
    curses.wrapper(app.run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
