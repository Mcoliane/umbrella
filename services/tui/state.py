from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PlatformState:
    view: str = "home"
    selected_session_id: str = ""
    status: str = "Ready"
    home: dict = field(default_factory=dict)
    session: dict = field(default_factory=dict)

