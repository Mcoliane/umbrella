#!/usr/bin/env bash
set -euo pipefail

# The security posture of a fresh clone, asserted in code instead of prose.
#
# Two audits found the same class of defect: the launcher and the documentation
# disagreed about what a default install actually enforces, and nothing checked. The
# approval gate and catalog trust verification were both described as primary controls
# while shipping switched off. That is not a bug in any one line — it is the absence of
# a test, and the project already learned this lesson once in verify-patterns.
#
# This asserts three things:
#
#   A. Mesh auth is closed on every HTTP handler. This is the one control that IS on by
#      default, and the check exists so a new endpoint cannot quietly ship unauthenticated.
#   B. Catalog trust verification is OFF by default, and the docs say so.
#   C. The policy approval gate does NOT fire by default (autonomy=auto), and the docs say so.
#
# B and C deliberately assert the *current, deliberate, pre-launch* posture rather than
# the desired one. If someone arms either control, this test fails and forces the
# documentation to be updated in the same commit — which is the entire point. Do not
# "fix" a failure here by loosening the assertion; update it together with the docs.

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import ast
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
failures = []

# ---- A. every HTTP handler authenticates before doing anything --------------------
AUTH_CALLS = {'_auth_ok', 'check_auth'}
MAX_STMT_INDEX = 1  # statement 0 is the request-id assignment; auth is statement 1

handlers = 0
for path in sorted(root.glob('services/*/app.py')):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith('do_')):
            continue
        if node.name in {'do_HEAD', 'do_OPTIONS'}:
            continue
        handlers += 1
        found_at = None
        for i, stmt in enumerate(node.body):
            if any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr in AUTH_CALLS
                for c in ast.walk(stmt)
            ):
                found_at = i
                break
        rel = path.relative_to(root)
        if found_at is None:
            failures.append(f'A: {rel}:{node.name} never calls _auth_ok/check_auth — unauthenticated endpoint')
        elif found_at > MAX_STMT_INDEX:
            failures.append(
                f'A: {rel}:{node.name} authenticates at statement {found_at}; work happens before the '
                f'auth check (expected <= {MAX_STMT_INDEX})'
            )

if handlers < 20:
    failures.append(f'A: only found {handlers} do_* handlers — the sweep is probably broken, not the code')

# ---- B. catalog trust verification is off by default, and documented as off -------
catalog_src = (root / 'services' / 'catalog' / 'app.py').read_text(encoding='utf-8')
m = re.search(r"add_argument\('--signature-mode',\s*default='([^']*)'", catalog_src)
if not m:
    failures.append('B: could not find the catalog --signature-mode default')
elif m.group(1) != 'permissive':
    failures.append(
        f"B: catalog --signature-mode now defaults to '{m.group(1)}', not 'permissive'. Trust "
        'verification has been armed — update docs/KNOWN_LIMITATIONS.md and this test together.'
    )

launcher = (root / 'scripts' / 'control-plane' / 'manage-platform-stack').read_text(encoding='utf-8')
catalog_spawn = [ln for ln in launcher.splitlines() if 'catalog/app.py' in ln]
if not catalog_spawn:
    failures.append('B: could not find the catalog spawn line in manage-platform-stack')
elif any('--signature-mode' in ln for ln in catalog_spawn):
    failures.append(
        'B: the launcher now passes --signature-mode. Trust verification has been armed — note that '
        'scan-root skills carry no checksum manifest and become untrusted, so confirm the shipped '
        'skills are signed, then update docs/KNOWN_LIMITATIONS.md and this test together.'
    )

# ---- C. the approval gate does not fire by default, and is documented as such -----
policy_src = (root / 'services' / 'policy' / 'app.py').read_text(encoding='utf-8')
if not re.search(r"\.get\('mode',\s*'auto'\)", policy_src):
    failures.append(
        "C: policy no longer defaults autonomy to 'auto'. The approval gate has been armed — "
        'confirm session/TUI can actually prompt (they had no approve/deny surface), then update '
        'docs/KNOWN_LIMITATIONS.md and this test together.'
    )
if "self.get_autonomy_mode() != 'auto'" not in policy_src:
    failures.append(
        "C: the approval short-circuit on autonomy=='auto' changed shape. Re-verify whether the gate "
        'now fires by default and update docs/KNOWN_LIMITATIONS.md and this test together.'
    )

# ---- B+C. the documentation must state both are off ------------------------------
known = (root / 'docs' / 'KNOWN_LIMITATIONS.md').read_text(encoding='utf-8')
for needle, label in (
    ('does not fire in the default configuration', 'the approval gate being off'),
    ('permissive', 'catalog trust verification being off'),
):
    if needle not in known:
        failures.append(
            f'B/C: docs/KNOWN_LIMITATIONS.md no longer documents {label} (missing: "{needle}"). '
            'The code posture and the documented posture must be changed together.'
        )

if failures:
    print('DEFAULT POSTURE DRIFT:\n')
    for f in failures:
        print('  - ' + f)
    raise SystemExit(1)

print(f'default posture PASS ({handlers} handlers authenticate; trust off and gate off, both documented)')
PY

echo "umbrella0.4 default posture contract PASS"
