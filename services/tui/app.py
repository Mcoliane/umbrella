from __future__ import annotations

import argparse
import curses
import json
import threading
import textwrap
import time
from pathlib import Path

from services.tui.client import TuiClient
from services.tui.state import PlatformState

SPINNER_FRAMES = ("|", "/", "-", "\\")


def _clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    return value[:width]


class UmbrellaTui:
    def __init__(self, *, root: Path, manifest: Path | None = None, session_id: str = ""):
        self.root = root
        self.client = TuiClient(root=root, manifest_path=manifest)
        self.state = PlatformState(selected_session_id=session_id)
        self.initial_session_id = session_id.strip()
        self.session_cursor = 0
        self._pending_lock = threading.Lock()
        self._pending_result: dict | None = None
        self.has_color = False
        self._inflight: dict[str, dict] = {}  # turnId -> {shop, target, started} for live activity
        self._pending_questions: list = []  # structured clarify questions awaiting the picker

    def add_local_event(self, role: str, content: str):
        self.state.local_transcript.append({"role": role, "content": content})
        self.state.local_transcript = self.state.local_transcript[-30:]

    @staticmethod
    def _provenance_tag(row: dict) -> str:
        if not isinstance(row, dict):
            return ""
        if bool(row.get("fallbackUsed", False)):
            return "[fallback — no model]"
        if bool(row.get("providerUsed", False)):
            provider = str(row.get("providerType", "")).strip()
            model = str(row.get("modelUsed", "")).strip()
            detail = "/".join(bit for bit in (provider, model) if bit)
            return f"[{detail}]" if detail else "[model]"
        return ""

    def _active_model_label(self) -> str:
        """Short label for the model currently answering, for the header bar."""
        if self._model_warning():
            return "off (fallback)"
        home = self.state.home if isinstance(self.state.home, dict) else {}
        provider = home.get("modelProvider") if isinstance(home.get("modelProvider"), dict) else {}
        meta = provider.get("provider") if isinstance(provider.get("provider"), dict) else {}
        return str(meta.get("defaultModel", "")).strip() or "unset"

    def _model_warning(self) -> str:
        home = self.state.home if isinstance(self.state.home, dict) else {}
        provider = home.get("modelProvider") if isinstance(home.get("modelProvider"), dict) else {}
        if not provider:
            return ""
        broker_meta = provider.get("broker") if isinstance(provider.get("broker"), dict) else {}
        secrets = provider.get("secrets") if isinstance(provider.get("secrets"), dict) else {}
        if not bool(provider.get("enabled", False)) or not bool(broker_meta.get("enabled", False)):
            return "MODEL OFF: broker disabled — replies come from the keyword fallback (/model setup)"
        if not bool(provider.get("configured", False)):
            return "MODEL OFF: broker unconfigured — replies come from the keyword fallback (/model setup)"
        if not bool(secrets.get("apiKeyPresent", False)):
            return "MODEL OFF: no API key — replies come from the keyword fallback (/model setup)"
        for row in home.get("services") or []:
            if isinstance(row, dict) and row.get("service") == "model-broker" and not row.get("ok"):
                return "MODEL OFF: broker unreachable — replies come from the keyword fallback (/start full)"
        return ""

    def _pending_elapsed_sec(self) -> int:
        if not self.state.pending_request or self.state.pending_started_at <= 0:
            return 0
        return max(0, int(time.time() - self.state.pending_started_at))

    def _pending_spinner(self) -> str:
        return SPINNER_FRAMES[self.state.pending_spinner_index % len(SPINNER_FRAMES)]

    def _begin_pending(self, *, target: str, content: str) -> None:
        self.state.pending_request = True
        self.state.pending_target = target
        self.state.pending_content = content
        self.state.pending_started_at = time.time()
        self.state.pending_spinner_index = 0
        self.state.status = f"{target} is thinking..."

    def _clear_pending(self) -> None:
        self.state.pending_request = False
        self.state.pending_target = ""
        self.state.pending_content = ""
        self.state.pending_started_at = 0.0
        self.state.pending_spinner_index = 0

    def _conversation_worker(self, *, session_id: str, target: str, content: str) -> None:
        try:
            heartbeat = self.client.heartbeat_session(session_id=session_id, seen_by="system")
            if isinstance(heartbeat, dict) and heartbeat.get("error"):
                error = heartbeat.get("error") if isinstance(heartbeat.get("error"), dict) else {}
                message = str(error.get("message", "")).strip() or json.dumps(heartbeat, ensure_ascii=False)[:180]
                outcome = {"ok": False, "stage": "heartbeat", "message": message}
            else:
                out = self.client.converse(session_id=session_id, target=target, content=content.strip())
                outcome = {"ok": bool(out.get("ok")), "stage": "converse", "payload": out}
        except Exception as exc:
            outcome = {"ok": False, "stage": "exception", "message": str(exc)}
        with self._pending_lock:
            self._pending_result = outcome

    def _drain_pending_result(self) -> None:
        if not self.state.pending_request:
            return
        with self._pending_lock:
            outcome = self._pending_result
            if outcome is None:
                self.state.pending_spinner_index += 1
                return
            self._pending_result = None
        target = self.state.pending_target or self.state.active_target or "agent"
        elapsed = self._pending_elapsed_sec()
        self._clear_pending()
        if outcome.get("ok"):
            out = outcome.get("payload") if isinstance(outcome.get("payload"), dict) else {}
            self.state.active_target = str(out.get("target", target)).strip() or target
            self.refresh_session()
            tag = self._provenance_tag(out)
            suffix = f" {tag}" if tag else ""
            reply = str(out.get("reply", "")).strip()
            questions = out.get("questions") if isinstance(out.get("questions"), list) else []
            if questions:
                # The mayor is scoping the task. The intro reply is already in the
                # transcript (appended server-side); queue the picker for the loop.
                self._pending_questions = questions
                self.state.status = f"{self.state.active_target} needs a few answers — pick below"
                return
            if str(out.get("delegationStatus", "")).strip() == "running":
                shop = str(out.get("shopId", "a shop")).strip() or "a shop"
                turn_id = str(out.get("turnId", "")).strip()
                if turn_id:
                    self._inflight[turn_id] = {"shop": shop, "target": self.state.active_target, "started": time.time()}
                self.state.status = f"Delegated to {shop} — running in background"
                return
            self.state.status = f"Talked to {self.state.active_target} in {elapsed}s{suffix}"
            if reply and not any(str(msg.get("content", "")).strip() == reply for msg in self.state.local_transcript[-2:]):
                self.add_local_event("system", f"{self.state.active_target} replied in {elapsed}s{suffix}.")
            return
        if outcome.get("stage") == "heartbeat":
            message = str(outcome.get("message", "")).strip() or "heartbeat failed"
            self.add_local_event("error", f"Heartbeat failed: {message}")
            self.state.status = f"Heartbeat failed: {message[:70]}"
            return
        if outcome.get("stage") == "converse":
            out = outcome.get("payload") if isinstance(outcome.get("payload"), dict) else {}
            error = out.get("error") if isinstance(out.get("error"), dict) else {}
            message = str(error.get("message", "")).strip() or json.dumps(out, ensure_ascii=False)[:180]
            self.add_local_event("error", f"Conversation failed: {message}")
            self.state.status = f"Conversation failed: {message[:70]}"
            return
        message = str(outcome.get("message", "")).strip() or "conversation failed"
        self.add_local_event("error", f"Conversation failed: {message}")
        self.state.status = f"Conversation failed: {message[:70]}"

    def refresh_home(self):
        self.state.home = self.client.home_snapshot()
        try:
            self.state.home["autonomyMode"] = self.client.get_autonomy()
        except Exception:
            self.state.home["autonomyMode"] = "unknown"
        sessions = self.state.home.get("sessions") or []
        if sessions:
            session_ids = [str(row.get("sessionId", "")) for row in sessions]
            if self.state.selected_session_id not in session_ids:
                if self.initial_session_id and self.initial_session_id in session_ids:
                    self.session_cursor = max(0, session_ids.index(self.initial_session_id))
                    self.state.selected_session_id = self.initial_session_id
                else:
                    self.session_cursor = 0
                    self.state.selected_session_id = ""
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
        # Clear in-flight delegations whose result has now landed.
        if self._inflight:
            for msg in (session.get("messages") or [])[-30:]:
                md = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
                if str(md.get("delegationStatus", "")).strip() in {"completed", "failed"}:
                    self._inflight.pop(str(md.get("turnId", "")).strip(), None)
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
        return {
            "type": normalized or str(provider_meta.get("type", "openai-compatible")).strip() or "openai-compatible",
            "baseUrl": str(provider_meta.get("baseUrl", "")).strip(),
            "defaultModel": str(provider_meta.get("defaultModel", "")).strip(),
            "timeoutSec": int(provider_meta.get("timeoutSec", 120) or 120),
        }

    def setup_model_provider(self, screen):
        current = self.state.home.get("modelProvider") if isinstance(self.state.home.get("modelProvider"), dict) else {}
        provider_meta = current.get("provider") if isinstance(current.get("provider"), dict) else {}
        provider_type = self.prompt(screen, "Provider type", default=str(provider_meta.get("type", "openai-compatible")).strip() or "openai-compatible")
        if provider_type is None:
            return
        provider_type = str(provider_type).strip().lower() or "openai-compatible"
        # "zai" is a retired alias: silently fold a typed value into the one
        # supported type so no new Z.ai-typed connection is ever created here.
        if provider_type == "zai":
            provider_type = "openai-compatible"
        if provider_type != "openai-compatible":
            self.add_local_event("error", f"Unsupported provider type: {provider_type}")
            self.state.status = "Model setup failed"
            return
        recommended = self._recommended_provider_defaults(provider_type, current)
        base_url = self.prompt(screen, "Base URL", default=str(recommended.get("baseUrl", "")).strip())
        if base_url is None:
            return
        model = self.prompt(screen, "Default model", default=str(recommended.get("defaultModel", "")).strip())
        if model is None:
            return
        timeout_raw = self.prompt(screen, "Timeout seconds", default=str(recommended.get("timeoutSec", 20) or 20))
        if timeout_raw is None:
            return
        api_key = self.prompt(screen, "API key (blank keeps existing)", default="", secret=True)
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

    def prompt(self, screen, label: str, default: str = "", secret: bool = False) -> str | None:
        previous_cursor = None
        if not secret:
            curses.echo()
        else:
            curses.noecho()
        try:
            try:
                previous_cursor = curses.curs_set(1)
            except curses.error:
                previous_cursor = None
            screen.timeout(-1)
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
            screen.timeout(100)
            if previous_cursor is not None:
                try:
                    curses.curs_set(previous_cursor)
                except curses.error:
                    pass
            curses.noecho()

    def run_question_flow(self, screen, questions: list) -> None:
        """Claude-CLI-style clarify flow: present each structured question as a
        selectable picker, collect the answers, and send them back to the mayor
        as one concise message so it can delegate a concrete spec."""
        answers: list[tuple[str, list]] = []
        for idx, q in enumerate(questions, start=1):
            if not isinstance(q, dict):
                continue
            qtext = str(q.get("question", "")).strip() or f"Question {idx}"
            options = [str(o) for o in (q.get("options") or []) if str(o).strip()]
            multi = bool(q.get("multiSelect"))
            title = f"({idx}/{len(questions)})  {qtext}"
            if options:
                chosen = self._select(screen, title, options, multi=multi)
            else:
                typed = self.prompt(screen, qtext, default="")
                chosen = [typed] if typed else []
            if chosen is None:  # ESC anywhere cancels the whole flow
                self.add_local_event("system", "Questions cancelled — you can answer in your own words instead.")
                self.state.status = "Clarify cancelled"
                return
            answers.append((qtext, chosen))
        lines = [f"{i}. {qtext}: {', '.join(ans) if ans else '(no preference)'}" for i, (qtext, ans) in enumerate(answers, start=1)]
        assembled = "Here are my answers:\n" + "\n".join(lines)
        self.talk(screen, content_override=assembled)

    def _select(self, screen, title: str, options: list, *, multi: bool = False) -> list | None:
        """Interactive single/multi-select picker. Returns the chosen answer
        strings (free text resolved for the 'Other' entry), or None on ESC."""
        display = list(options) + ["Other (type your own)"]
        other_idx = len(display) - 1
        selected: set[int] = set()
        cursor = 0
        try:
            prev_cursor = curses.curs_set(0)
        except curses.error:
            prev_cursor = None
        screen.timeout(-1)
        try:
            while True:
                h, w = screen.getmaxyx()
                screen.erase()
                self._put(screen, 0, 1, _clip(title, w - 2), self._c(7, curses.A_BOLD))
                hint = ("↑↓ move · SPACE toggle · ⏎ confirm · ESC cancel" if multi
                        else "↑↓ move · number or ⏎ to pick · ESC cancel")
                self._put(screen, 1, 1, _clip(hint, w - 2), self._c(3))
                for i, label in enumerate(display):
                    box = ("[x] " if i in selected else "[ ] ") if multi else ("(•) " if i == cursor else "( ) ")
                    arrow = "› " if i == cursor else "  "
                    num = f"{i + 1}. " if i < 9 else "   "
                    attr = self._c(2, curses.A_BOLD) if i == cursor else self._c(0)
                    self._put(screen, 3 + i, 2, _clip(arrow + box + num + label, w - 4), attr)
                screen.refresh()
                k = screen.getch()
                if k == 27:  # ESC
                    return None
                if k in (curses.KEY_UP, ord("k")):
                    cursor = (cursor - 1) % len(display)
                elif k in (curses.KEY_DOWN, ord("j")):
                    cursor = (cursor + 1) % len(display)
                elif ord("1") <= k <= ord("9") and (k - ord("1")) < len(display):
                    picked = k - ord("1")
                    if multi:
                        selected.symmetric_difference_update({picked})
                        cursor = picked
                    else:
                        return self._resolve_select(screen, display, [picked], other_idx)
                elif multi and k == ord(" "):
                    selected.symmetric_difference_update({cursor})
                elif k in (10, 13):
                    idxs = sorted(selected) if (multi and selected) else [cursor]
                    return self._resolve_select(screen, display, idxs, other_idx)
        finally:
            screen.timeout(100)
            if prev_cursor is not None:
                try:
                    curses.curs_set(prev_cursor)
                except curses.error:
                    pass

    def _resolve_select(self, screen, display: list, idxs: list, other_idx: int) -> list:
        answers: list[str] = []
        for i in idxs:
            if i == other_idx:
                typed = self.prompt(screen, "Type your answer", default="")
                if typed:
                    answers.append(typed)
            elif 0 <= i < len(display):
                answers.append(display[i])
        return answers

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
        if self.state.pending_request:
            self.state.status = f"{self.state.pending_target or 'Agent'} is still working..."
            return
        if not self.state.selected_session_id:
            self.state.status = "Open a town first"
            return
        target = target_override.strip() or self.state.active_target or "mayor"
        content = content_override
        if not content:
            content = self.prompt(screen, f"Message to {target}", default="") or ""
        if not content.strip():
            return
        self._begin_pending(target=target, content=content.strip())
        worker = threading.Thread(
            target=self._conversation_worker,
            kwargs={"session_id": self.state.selected_session_id, "target": target, "content": content.strip()},
            daemon=True,
        )
        worker.start()

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
                "Commands: /help /status /autonomy [auto|ask] /new [title] /sessions /session <id|n> /agent <id> /shops /workers /model /model setup /model test /model use <model> /model disable /refresh /start [full|core] /stop /quit",
            )
            self.state.status = "Help"
            return
        if name in {"autonomy", "approvals"}:
            if args and args[0].strip().lower() in {"auto", "ask", "on", "off"}:
                choice = args[0].strip().lower()
                mode = "auto" if choice in {"auto", "off"} else "ask"
                out = self.client.set_autonomy(mode)
                now = str(out.get("mode", mode)) if isinstance(out, dict) else mode
                self.add_local_event(
                    "system",
                    f"Approval gating: {now.upper()} — "
                    + ("actions run without asking (autonomous)." if now == "auto" else "approval-required actions must be approved."),
                )
                self.state.status = f"approvals={now}"
            else:
                current = self.client.get_autonomy()
                self.add_local_event(
                    "system",
                    f"Approval gating is {current.upper()}. Use /autonomy auto (run without asking) or /autonomy ask (require approval).",
                )
                self.state.status = f"approvals={current}"
            self.refresh_home()
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
                        "type": str(provider_meta.get("type", "openai-compatible")).strip() or "openai-compatible",
                        "baseUrl": str(provider_meta.get("baseUrl", "")).strip(),
                        "defaultModel": str(args[1]).strip(),
                        "timeoutSec": int(provider_meta.get("timeoutSec", 120) or 120),
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

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    def _init_colors(self) -> None:
        self.has_color = False
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)     # you
            curses.init_pair(2, curses.COLOR_GREEN, -1)    # agent / ok
            curses.init_pair(3, curses.COLOR_YELLOW, -1)   # system / warn
            curses.init_pair(4, curses.COLOR_RED, -1)      # error
            curses.init_pair(5, curses.COLOR_MAGENTA, -1)  # delegation / activity
            curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_BLUE)  # header bar
            curses.init_pair(7, curses.COLOR_BLUE, -1)     # accent / headings
            self.has_color = True
        except curses.error:
            self.has_color = False

    def _c(self, pair: int, extra: int = 0) -> int:
        return (curses.color_pair(pair) if self.has_color else 0) | extra

    def _put(self, screen, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = screen.getmaxyx()
        if not (0 <= y < h) or x >= w - 1:
            return
        try:
            screen.addstr(y, x, _clip(text, w - x - 1), attr)
        except curses.error:
            pass

    @staticmethod
    def _seconds_ago(ts: str) -> int:
        from datetime import datetime, timezone
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return max(0, int((datetime.now(timezone.utc) - t).total_seconds()))
        except Exception:
            return 0

    def _sidebar_w(self, width: int) -> int:
        return max(30, min(46, width // 3))

    def draw_header(self, screen):
        h, w = screen.getmaxyx()
        session = self.session_payload
        title = str(session.get("title", "Town Hall")).strip() or "Town Hall"
        approvals = str((self.state.home or {}).get("autonomyMode", "")).strip() or "?"
        model_off = bool(self._model_warning())
        model = "off" if model_off else self._active_model_label()
        bar = self._c(6) if self.has_color else curses.A_REVERSE
        self._put(screen, 0, 0, " " * (w - 1), bar)
        self._put(screen, 0, 1, f"◆ Umbrella · {title} · @{self.state.active_target}", bar | curses.A_BOLD)
        dot = "●" if not model_off else "○"
        right = f"{dot} model:{model}  approvals:{approvals} "
        self._put(screen, 0, max(1, w - 1 - len(right)), right, bar)

    def draw_footer(self, screen):
        h, w = screen.getmaxyx()
        warning = self._model_warning()
        keys = "type + ⏎ send · / cmd · ⇥ target · n new · s open · S full · x stop · q quit"
        status = self.state.status
        if self.state.pending_request:
            status = f'{self._pending_spinner()} {self.state.pending_target or "agent"} thinking {self._pending_elapsed_sec()}s'
        # A model-off warning row sits just above the key row, in red.
        if warning:
            self._put(screen, h - 2, 0, " " * (w - 1), self._c(4, curses.A_REVERSE))
            self._put(screen, h - 2, 1, "⚠ " + warning, self._c(4, curses.A_REVERSE | curses.A_BOLD))
        bar = self._c(6) if self.has_color else curses.A_REVERSE
        self._put(screen, h - 1, 0, " " * (w - 1), bar)
        self._put(screen, h - 1, 1, _clip(status, max(10, w // 2)), bar | curses.A_BOLD)
        self._put(screen, h - 1, max(1, w - 1 - len(keys)), keys, bar)

    def _conversation_lines(self, width: int) -> list[tuple[int, str]]:
        """A single ordered, spaced, color-differentiated stream:
        speaker header on its own line, wrapped body indented, blank spacer."""
        lines: list[tuple[int, str]] = []
        wrap = max(10, width - 2)

        def bubble(header: str, header_attr: int, body: str, body_attr: int) -> None:
            lines.append((header_attr, header))
            for wl in (textwrap.wrap(body, wrap) or [""]):
                lines.append((body_attr, "  " + wl))
            lines.append((0, ""))

        session = self.session_payload
        for message in (session.get("messages") or [])[-60:]:
            role = str(message.get("role", "")).lower()
            if role == "tool":
                continue
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            md = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            if role == "user":
                bubble("You", self._c(1, curses.A_BOLD), content, self._c(1))
            elif role == "assistant":
                dstatus = str(md.get("delegationStatus", "")).strip()
                shop = str(md.get("targetShopId", "")).strip()
                if dstatus:
                    icon = {"running": "⟳", "completed": "✓", "failed": "✗"}.get(dstatus, "•")
                    bubble(f"{icon} delegation → {shop or 'shop'} · {dstatus}", self._c(5, curses.A_BOLD), content, self._c(5))
                else:
                    target = str(md.get("targetAgentId", "")).strip() or str(md.get("target", "")).strip() or "agent"
                    tag = self._provenance_tag(md)
                    header = target + (f"   {tag}" if tag else "")
                    bubble(header, self._c(2, curses.A_BOLD), content, self._c(2))
            else:
                bubble(role, self._c(3), content, self._c(3))

        # Errors from local events, shown at the end (most recent), in red.
        for ev in self.state.local_transcript[-4:]:
            if str(ev.get("role", "")).lower() == "error":
                bubble("⚠ error", self._c(4, curses.A_BOLD), str(ev.get("content", "")), self._c(4))

        # Optimistic echo of the just-sent message + a live thinking line.
        if self.state.pending_request:
            optimistic = str(self.state.pending_content or "").strip()
            if optimistic:
                bubble("You", self._c(1, curses.A_BOLD), optimistic, self._c(1))
            lines.append((self._c(3, curses.A_DIM),
                          f"  {self._pending_spinner()} {self.state.pending_target or 'agent'} is thinking… {self._pending_elapsed_sec()}s"))
        return lines

    def draw_transcript(self, screen):
        h, w = screen.getmaxyx()
        tw = max(24, w - self._sidebar_w(w) - 4)
        self._put(screen, 1, 1, "TRANSCRIPT", self._c(7, curses.A_BOLD))
        lines = self._conversation_lines(tw)
        top, bottom = 2, h - 2
        avail = max(1, bottom - top)
        visible = lines[-avail:]
        y = top
        for attr, text in visible:
            self._put(screen, y, 1, text, attr)
            y += 1
        if not visible:
            self._put(screen, 3, 2, "No conversation yet.", self._c(3, curses.A_BOLD))
            self._put(screen, 4, 2, "Press n to create a town, then just type and hit enter.", self._c(3))

    def _activity_lines(self) -> list[tuple[int, str]]:
        """What the agents are doing: live in-flight delegations + recent ones."""
        out: list[tuple[int, str]] = []
        now = time.time()
        for info in self._inflight.values():
            el = int(now - info.get("started", now))
            out.append((self._c(5, curses.A_BOLD), f"{self._pending_spinner()} {info.get('shop','shop')} · working {el}s"))
        session = self.session_payload
        delegs = session.get("delegations") if isinstance(session.get("delegations"), list) else []
        for d in list(delegs)[-6:][::-1]:
            if not isinstance(d, dict):
                continue
            state = str(d.get("state", "")).upper()
            shop = str(d.get("shopId", "")).strip() or "shop"
            action = str(d.get("resolvedActionId", "") or d.get("actionId", "")).replace("skill.", "")
            if state in {"COMPLETED", "SUCCEEDED"}:
                dur = ""
                if d.get("createdAt") and d.get("completedAt"):
                    dur = f" {max(0, self._seconds_ago(d['createdAt']) - self._seconds_ago(d['completedAt']))}s"
                out.append((self._c(2), f"✓ {shop} · {action}{dur}"))
            elif state == "FAILED":
                out.append((self._c(4), f"✗ {shop} · {action}"))
            elif state:
                out.append((self._c(5), f"⟳ {shop} · {action}"))
        return out[:8]

    def draw_sidebar(self, screen):
        h, w = screen.getmaxyx()
        sw = self._sidebar_w(w)
        x = w - sw
        session = self.session_payload
        platform = self.state.home.get("platformStack") or {}
        services = self.state.home.get("services") or []
        # vertical divider
        for yy in range(1, h - 1):
            self._put(screen, yy, x - 1, "│", self._c(7))
        y = 1

        def heading(label: str):
            nonlocal y
            self._put(screen, y, x + 1, label, self._c(7, curses.A_BOLD))
            y += 1

        heading("TOWN")
        if session:
            self._put(screen, y, x + 1, f"mayor  {session.get('mayorAgentId','')}"[:sw - 2]); y += 1
            hb = str(session.get("heartbeatStatus", "")).strip()
            self._put(screen, y, x + 1, f"beat   {hb}", self._c(2 if hb == 'healthy' else 3)); y += 1
        else:
            self._put(screen, y, x + 1, "no town selected", self._c(3)); y += 1
        up = bool(platform.get("ok"))
        self._put(screen, y, x + 1, f"stack  {'UP' if up else 'DOWN'} {platform.get('profile','')}", self._c(2 if up else 4)); y += 2

        heading("ACTIVITY")
        activity = self._activity_lines()
        if activity:
            for attr, text in activity:
                self._put(screen, y, x + 1, text[:sw - 2], attr); y += 1
        else:
            self._put(screen, y, x + 1, "idle — no delegations", self._c(3)); y += 1
        y += 1

        heading("AGENTS")
        for agent in (session.get("agents") or [])[: max(1, h // 5)]:
            aid = str(agent.get("agentId", "")).strip()
            role = str(agent.get("role", "")).strip()
            sel = aid == self.state.active_target
            self._put(screen, y, x + 1, f"{'▸' if sel else ' '} {aid} · {role}",
                      self._c(1, curses.A_BOLD) if sel else 0); y += 1
        y += 1

        heading("SHOPS")
        shops = session.get("shops") if isinstance(session.get("shops"), dict) else {}
        for shop_id, row in list(shops.items())[: max(1, h // 6)]:
            self._put(screen, y, x + 1, f"{shop_id}", self._c(2)); y += 1
        y += 1

        heading("SERVICES")
        for row in services[: max(0, h - y - 1)]:
            ok = bool(row.get("ok"))
            self._put(screen, y, x + 1, f"{'●' if ok else '○'} {row.get('service','')}",
                      self._c(2 if ok else 4)); y += 1

    def draw_town(self, screen):
        self.draw_header(screen)
        self.draw_transcript(screen)
        self.draw_sidebar(screen)
        self.draw_footer(screen)

    def run(self, screen):
        curses.curs_set(0)
        self._init_colors()
        screen.keypad(True)
        screen.timeout(100)
        self.refresh_home()
        if self.state.selected_session_id:
            self.refresh_session()
        else:
            self.add_local_event("system", "No town selected. Press n to create a town or s to open one.")
        last_auto = time.time()
        while True:
            self._drain_pending_result()
            if self._pending_questions and not self.state.pending_request:
                questions = self._pending_questions
                self._pending_questions = []
                self.run_question_flow(screen, questions)
                continue
            screen.erase()
            self.draw_town(screen)
            screen.refresh()
            key = screen.getch()
            if key == -1:
                # Idle tick: poll the session so background (async) delegation
                # results appear on their own without a manual refresh.
                now = time.time()
                if self.state.selected_session_id and not self.state.pending_request and (now - last_auto) > 4.0:
                    last_auto = now
                    try:
                        self.refresh_session()
                    except Exception:
                        pass
                continue
            if key in (ord("q"), ord("Q")):
                break
            if self.state.pending_request:
                self.state.status = f'{self.state.pending_target or "agent"} is still working...'
                continue
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
                raw = self.prompt(screen, "Command", default="")
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
