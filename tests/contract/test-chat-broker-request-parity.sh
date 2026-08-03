#!/usr/bin/env bash
set -euo pipefail

# The mayor's conversation path crosses three layers:
#
#   services/session/app.py  ->  skills/chat-respond-skill  ->  services/model_broker
#
# The skill builds its outbound broker request as a CLOSED dict literal. Any key the
# broker reads that the skill forgets to forward is silently dropped — no error, no
# warning, just a feature that quietly does nothing. That is exactly how recalledMemory
# and retrievedArtifact were lost: session computed both, the broker was their only
# consumer, and the skill in between never forwarded them, so the entire durable-memory
# recall path wrote memory that was never read back into a prompt.
#
# This asserts the seam directly: every key the broker reads out of a chat request must
# be a key the skill actually sends. It is a static check on purpose — an end-to-end
# assertion would only cover whichever keys the fixture happens to exercise, while this
# covers the whole contract and fails the moment a new key is added on one side only.

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import ast
import sys
from pathlib import Path

root = Path(sys.argv[1])
broker_path = root / 'services' / 'model_broker' / 'app.py'
skill_path = root / 'skills' / 'chat-respond-skill' / 'bin' / 'chat-respond-skill.py'

# Keys the broker only consults as a fallback when the primary key is absent, so no
# caller is obliged to send them. See services/model_broker/app.py:542-543.
OPTIONAL_FALLBACKS = {'temperatureDefault', 'maxTokensDefault'}


def broker_request_reads() -> set:
    tree = ast.parse(broker_path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'chat_respond':
            keys = set()
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == 'get'
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == 'request'
                    and sub.args
                    and isinstance(sub.args[0], ast.Constant)
                    and isinstance(sub.args[0].value, str)
                ):
                    keys.add(sub.args[0].value)
            return keys
    raise AssertionError('chat_respond not found in services/model_broker/app.py')


def skill_request_writes() -> set:
    tree = ast.parse(skill_path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == 'broker_request' for t in node.targets
        ):
            if isinstance(node.value, ast.Dict):
                if any(k is None for k in node.value.keys):
                    raise AssertionError('broker_request uses **spread; this check assumes a closed literal')
                return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError('broker_request literal not found in the chat-respond skill')


reads = broker_request_reads()
writes = skill_request_writes()

assert reads, 'no request.get() keys found in chat_respond — the check would pass vacuously'
assert writes, 'no keys found in broker_request — the check would pass vacuously'

dropped = sorted((reads - writes) - OPTIONAL_FALLBACKS)
assert not dropped, (
    'the chat-respond skill drops keys the model broker reads, so these features '
    'silently do nothing: ' + ', '.join(dropped)
)

# Guard the regression that motivated this test specifically, so the intent survives
# even if someone loosens the general check above.
for required in ('recalledMemory', 'retrievedArtifact'):
    assert required in reads, f'broker no longer reads {required}; update this test deliberately'
    assert required in writes, f'chat-respond skill no longer forwards {required}'

print(f'chat broker request parity PASS ({len(reads)} broker keys, {len(writes)} skill keys)')
PY

echo "umbrella0.4 chat broker request parity contract PASS"
