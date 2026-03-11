#!/usr/bin/env python3
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class LifecycleError(RuntimeError):
    pass


@dataclass
class LifecycleModel:
    id: str
    initial_state: str
    states: set[str]
    terminal_states: set[str]
    transitions: set[tuple[str, str]]
    terminal_reasons: set[str]

    @classmethod
    def load(cls, lifecycle_path: Path) -> "LifecycleModel":
        data = json.loads(Path(lifecycle_path).read_text(encoding='utf-8'))
        states = set(data.get('states', []))
        transitions = {(t['from'], t['to']) for t in data.get('transitions', [])}
        terminal_states = set(data.get('terminalStates', []))
        initial_state = data.get('initialState')
        terminal_reasons = set(data.get('terminalReasonTaxonomy', []))

        if not initial_state:
            raise LifecycleError('lifecycle missing initialState')
        if initial_state not in states:
            raise LifecycleError(f'initialState {initial_state!r} is not in states')
        if not terminal_states.issubset(states):
            missing = sorted(terminal_states - states)
            raise LifecycleError(f'terminalStates not in states: {missing}')

        for frm, to in transitions:
            if frm not in states or to not in states:
                raise LifecycleError(f'invalid transition {frm}->{to}: state not declared')

        return cls(
            id=data.get('id', 'unknown'),
            initial_state=initial_state,
            states=states,
            terminal_states=terminal_states,
            transitions=transitions,
            terminal_reasons=terminal_reasons,
        )

    def assert_state(self, state: str):
        if state not in self.states:
            raise LifecycleError(f'invalid state {state!r}; allowed={sorted(self.states)}')

    def can_transition(self, from_state: str, to_state: str) -> bool:
        self.assert_state(from_state)
        self.assert_state(to_state)
        return (from_state, to_state) in self.transitions

    def require_transition(self, from_state: str, to_state: str):
        if not self.can_transition(from_state, to_state):
            raise LifecycleError(f'illegal transition {from_state!r} -> {to_state!r}')

    def assert_terminal_reason(self, reason: str):
        if self.terminal_reasons and reason not in self.terminal_reasons:
            raise LifecycleError(
                f'invalid terminal reason {reason!r}; allowed={sorted(self.terminal_reasons)}'
            )
