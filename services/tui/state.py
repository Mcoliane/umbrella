from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PlatformState:
    view: str = "town"
    selected_session_id: str = ""
    active_target: str = "mayor"
    status: str = "Ready"
    home: dict = field(default_factory=dict)
    session: dict = field(default_factory=dict)
    local_transcript: list[dict] = field(default_factory=list)
    pending_request: bool = False
    pending_target: str = ""
    pending_content: str = ""
    pending_started_at: float = 0.0
    pending_spinner_index: int = 0
