#!/usr/bin/env bash
set -euo pipefail

# First rendering coverage for the TUI.
#
# services/tui/app.py is ~1360 lines and had no test that rendered anything:
# test-platform-tui-smoke.sh only calls --dump-home, which returns JSON and passes with
# the entire stack down. That is how a renderer shipped which filtered local events to
# role == "error", silently discarding every add_local_event("system", ...) — so /help,
# /status, /shops, /workers and /model all answered into the void.
#
# _conversation_lines(width) is pure: it returns (attr, text) tuples and touches no
# screen, and _c() short-circuits when has_color is False. So the render path is
# testable in-process without curses or a terminal — no pty, no snapshot harness.

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))

from services.tui.app import UmbrellaTui

app = UmbrellaTui(root=root, auto_resume=False)
app.has_color = False


def render(transcript, width=80):
    app.state.local_transcript = list(transcript)
    lines = app._conversation_lines(width)
    return lines, '\n'.join(text for _, text in lines)


# 1. Both local-event roles must reach the screen. `system` is how every client-side
#    command answers; filtering it out made those commands look like no-ops.
_, out = render([
    {'role': 'system', 'content': 'SYSTEM-MARKER-shops-analysis-code'},
    {'role': 'error', 'content': 'ERROR-MARKER-something-failed'},
])
assert 'SYSTEM-MARKER-shops-analysis-code' in out, out
assert 'ERROR-MARKER-something-failed' in out, out

# 2. An error must not be evicted by a couple of subsequent system events — that was the
#    other half of the bug, with a four-entry window.
_, out = render([
    {'role': 'error', 'content': 'ERROR-MARKER-early-failure'},
    {'role': 'system', 'content': 'noise one'},
    {'role': 'system', 'content': 'noise two'},
    {'role': 'system', 'content': 'noise three'},
])
assert 'ERROR-MARKER-early-failure' in out, out

# 3. The window is still bounded, so a chatty session cannot flood the transcript pane.
lines, out = render([{'role': 'system', 'content': f'event-{i}'} for i in range(20)])
shown = sum(1 for _, text in lines if text.startswith('· umbrella'))
assert shown <= 6, f'local-event window is unbounded: {shown} events rendered'
assert 'event-19' in out, 'most recent event must be visible'
assert 'event-0' not in out, 'oldest event should have scrolled out of the window'

# 4. Long content wraps to the requested width rather than overflowing the pane.
long_line = 'Commands: ' + ', '.join(f'/{c}' for c in (
    'help status refresh new open session agent shops workers model '
    'start stop autonomy abort resume quit'
).split())
for width in (60, 80, 120):
    lines, _ = render([{'role': 'system', 'content': long_line}], width=width)
    over = [text for _, text in lines if len(text) > width]
    assert not over, f'width={width}: {len(over)} line(s) exceed the pane: {over[:1]}'

# 5. An unknown role must not crash the renderer or leak in unlabelled.
_, out = render([{'role': 'debug', 'content': 'UNKNOWN-ROLE-MARKER'}])
assert 'UNKNOWN-ROLE-MARKER' not in out, 'unrecognised local-event roles should not render'

print('tui rendering PASS')
PY

echo "umbrella0.4 tui rendering contract PASS"
