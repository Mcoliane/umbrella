from __future__ import annotations

import argparse
import curses
import json
import textwrap
from pathlib import Path

from services.tui.client import TuiClient
from services.tui.state import PlatformState


SERVICE_LABELS = (
    "policy",
    "catalog",
    "plugin-host",
    "execution",
    "session",
    "router",
    "orchestrator",
)


class UmbrellaTui:
    def __init__(self, *, root: Path, manifest: Path | None = None, session_id: str = ""):
        self.root = root
        self.client = TuiClient(root=root, manifest_path=manifest)
        self.state = PlatformState(selected_session_id=session_id)
        self.home_index = 0

    def refresh_home(self):
        self.state.home = self.client.home_snapshot()
        sessions = self.state.home.get("sessions") or []
        if sessions and not self.state.selected_session_id:
            self.state.selected_session_id = str(sessions[0].get("sessionId", ""))
        self.state.status = "Home refreshed"

    def refresh_session(self):
        if not self.state.selected_session_id:
            self.state.session = {}
            self.state.status = "No town selected"
            return
        self.state.session = self.client.get_session(self.state.selected_session_id)
        self.state.status = f'Town loaded: {self.state.selected_session_id}'

    def create_session(self, screen):
        agent_id = self.prompt(screen, "Mayor agent id", default="mayor")
        if agent_id is None:
            return
        title = self.prompt(screen, "Town title", default="Town Hall")
        if title is None:
            return
        created = self.client.create_session(agent_id=agent_id, title=title)
        session = created.get("session") if isinstance(created, dict) else {}
        session_id = str(session.get("sessionId", "")).strip()
        if session_id:
            self.state.selected_session_id = session_id
            self.state.view = "town"
            self.refresh_home()
            self.refresh_session()
            self.state.status = f"Created town session {session_id}"
            return
        self.state.status = f'Create failed: {json.dumps(created)[:140]}'

    def post_message(self, screen):
        if not self.state.selected_session_id:
            self.state.status = "Open a town first"
            return
        content = self.prompt(screen, "Message to mayor", default="")
        if content is None or not content.strip():
            return
        out = self.client.append_message(session_id=self.state.selected_session_id, role="user", content=content.strip())
        if out.get("message"):
            self.refresh_session()
            self.state.status = "Message posted to mayor"
            return
        self.state.status = f'Message failed: {json.dumps(out)[:140]}'

    def prompt(self, screen, label: str, default: str = "") -> str | None:
        curses.echo()
        try:
            height, width = screen.getmaxyx()
            prompt = f"{label}"
            if default:
                prompt += f" [{default}]"
            prompt += ": "
            screen.attron(curses.A_REVERSE)
            screen.addstr(height - 1, 0, " " * max(1, width - 1))
            screen.addstr(height - 1, 0, prompt[: max(1, width - 1)])
            screen.attroff(curses.A_REVERSE)
            screen.refresh()
            raw = screen.getstr(height - 1, min(len(prompt), max(0, width - 2)), max(1, width - len(prompt) - 1))
            value = raw.decode("utf-8", errors="ignore").strip()
            return value or default
        except KeyboardInterrupt:
            return None
        finally:
            curses.noecho()

    def draw_header(self, screen, title: str):
        height, width = screen.getmaxyx()
        header = f" Umbrella Platform TUI | {title} "
        screen.attron(curses.A_REVERSE)
        screen.addstr(0, 0, " " * max(1, width - 1))
        screen.addstr(0, 0, header[: max(1, width - 1)])
        screen.attroff(curses.A_REVERSE)

    def draw_footer(self, screen, text: str):
        height, width = screen.getmaxyx()
        screen.attron(curses.A_REVERSE)
        screen.addstr(height - 1, 0, " " * max(1, width - 1))
        screen.addstr(height - 1, 0, text[: max(1, width - 1)])
        screen.attroff(curses.A_REVERSE)

    def draw_home(self, screen):
        self.draw_header(screen, "Home")
        height, width = screen.getmaxyx()
        services = self.state.home.get("services") or []
        sessions = self.state.home.get("sessions") or []
        pkgs = self.state.home.get("agentPackages") or []
        left_w = max(28, width // 3)
        y = 2
        screen.addstr(y, 2, "Services", curses.A_BOLD)
        y += 1
        for row in services:
            status = "UP" if row.get("ok") else "DOWN"
            screen.addstr(y, 2, f"{row.get('service',''):14} {status:4} {row.get('url','')[: left_w - 22]}")
            y += 1
        y += 1
        screen.addstr(y, 2, "Sessions", curses.A_BOLD)
        y += 1
        if not sessions:
            screen.addstr(y, 2, "No town sessions yet. Press n to create one.")
        else:
            for idx, row in enumerate(sessions[: max(1, height - y - 4)]):
                marker = ">" if idx == self.home_index else " "
                sid = str(row.get("sessionId", ""))[:18]
                title = str(row.get("title", ""))[: max(1, left_w - 28)]
                hb = str(row.get("heartbeatStatus", ""))[:8]
                screen.addstr(y + idx, 2, f"{marker} {sid:18} {hb:8} {title}")
        right_x = left_w + 3
        line = 2
        screen.addstr(line, right_x, "Runtime Classes", curses.A_BOLD)
        line += 1
        contract = self.state.home.get("runtimeCapabilities") or {}
        runtimes = (contract.get("runtimes") if isinstance(contract, dict) else {}) or {}
        for name, row in runtimes.items():
            screen.addstr(line, right_x, f"{name}")
            line += 1
            for cap in (row.get("capabilities") if isinstance(row, dict) else []) or []:
                for wrapped in textwrap.wrap(f"- {cap}", max(16, width - right_x - 2)):
                    screen.addstr(line, right_x, wrapped)
                    line += 1
        line += 1
        screen.addstr(line, right_x, "Agent Packages", curses.A_BOLD)
        line += 1
        for pkg in pkgs[:6]:
            name = str(pkg.get("packageId", ""))
            screen.addstr(line, right_x, name[: max(10, width - right_x - 2)])
            line += 1
        self.draw_footer(screen, "q quit | r refresh | n new town | enter open selected town | t town view")

    def draw_town(self, screen):
        self.draw_header(screen, f"Town Hall: {self.state.selected_session_id or 'none'}")
        height, width = screen.getmaxyx()
        session = self.state.session.get("session") if isinstance(self.state.session, dict) and "session" in self.state.session else self.state.session
        if not isinstance(session, dict) or not session:
            screen.addstr(2, 2, "No town loaded. Press h for Home.")
            self.draw_footer(screen, "h home | r refresh | n new town")
            return
        left_w = max(28, width // 3)
        center_w = max(30, width // 3)
        right_x = left_w + center_w + 4
        screen.addstr(2, 2, "Town", curses.A_BOLD)
        screen.addstr(3, 2, f"Title: {session.get('title','')}")
        screen.addstr(4, 2, f"Mayor: {session.get('mayorAgentId','')}")
        screen.addstr(5, 2, f"Heartbeat: {session.get('heartbeatStatus','')}")
        screen.addstr(6, 2, f"Last seen by: {session.get('lastSeenBy','')}")
        screen.addstr(8, 2, "Agents", curses.A_BOLD)
        line = 9
        for agent in (session.get("agents") or [])[: max(1, height - 12)]:
            role = str(agent.get("role", ""))
            aid = str(agent.get("agentId", ""))
            pkg = str(agent.get("agentPackageId", ""))
            screen.addstr(line, 2, f"{aid:18} {role:12} {pkg[: left_w - 34]}")
            line += 1

        center_x = left_w + 2
        screen.addstr(2, center_x, "Messages", curses.A_BOLD)
        msg_line = 3
        messages = session.get("messages") or []
        visible = messages[-max(5, height - 8):]
        for message in visible:
            role = str(message.get("role", ""))
            content = str(message.get("content", ""))
            for wrapped in textwrap.wrap(f"{role}: {content}", max(16, center_w - 2))[:3]:
                if msg_line >= height - 2:
                    break
                screen.addstr(msg_line, center_x, wrapped[: center_w - 1])
                msg_line += 1

        screen.addstr(2, right_x, "Shops", curses.A_BOLD)
        line = 3
        shops = session.get("shops") if isinstance(session.get("shops"), dict) else {}
        for shop_id, shop in list(shops.items())[: max(1, height - 6)]:
            name = str(shop.get("name", ""))
            hb = str(shop.get("heartbeatStatus", ""))
            screen.addstr(line, right_x, f"{shop_id}: {name}"[: max(10, width - right_x - 2)])
            line += 1
            screen.addstr(line, right_x, f"  {hb} | owner {shop.get('ownerAgentId','')}"[: max(10, width - right_x - 2)])
            line += 1
        self.draw_footer(screen, "h home | r refresh | m message mayor | n new town")

    def run(self, screen):
        curses.curs_set(0)
        screen.keypad(True)
        self.refresh_home()
        if self.state.selected_session_id:
            self.refresh_session()
            self.state.view = "town"
        while True:
            screen.erase()
            if self.state.view == "town":
                self.draw_town(screen)
            else:
                self.draw_home(screen)
            self.draw_footer(screen, f"{self.state.status} | h home | t town | r refresh | n new town | q quit")
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), 27):
                if key == 27 and self.state.view != "home":
                    self.state.view = "home"
                    self.state.status = "Back to home"
                    continue
                if key == ord("q"):
                    break
            if key == ord("h"):
                self.state.view = "home"
                self.refresh_home()
                continue
            if key == ord("t"):
                if self.state.selected_session_id:
                    self.state.view = "town"
                    self.refresh_session()
                else:
                    self.state.status = "No town selected"
                continue
            if key == ord("r"):
                if self.state.view == "town":
                    self.refresh_session()
                else:
                    self.refresh_home()
                continue
            if key == ord("n"):
                self.create_session(screen)
                continue
            if self.state.view == "home":
                sessions = self.state.home.get("sessions") or []
                if key == curses.KEY_DOWN and sessions:
                    self.home_index = min(self.home_index + 1, len(sessions) - 1)
                    self.state.selected_session_id = str(sessions[self.home_index].get("sessionId", ""))
                    continue
                if key == curses.KEY_UP and sessions:
                    self.home_index = max(self.home_index - 1, 0)
                    self.state.selected_session_id = str(sessions[self.home_index].get("sessionId", ""))
                    continue
                if key in (10, 13) and sessions:
                    self.state.selected_session_id = str(sessions[self.home_index].get("sessionId", ""))
                    self.state.view = "town"
                    self.refresh_session()
                    continue
            if self.state.view == "town" and key == ord("m"):
                self.post_message(screen)


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
    manifest = Path(args.manifest).resolve() if str(args.manifest).strip() else None
    app = UmbrellaTui(root=root, manifest=manifest, session_id=args.session_id.strip())
    if args.dump_home:
        print(json.dumps(app.client.home_snapshot(), indent=2))
        return 0
    curses.wrapper(app.run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
