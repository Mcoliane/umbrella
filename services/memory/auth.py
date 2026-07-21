#!/usr/bin/env python3
from __future__ import annotations

import hmac
import sys

_warned_auth_disabled = False


def check_auth(auth_header: str, expected_token: str) -> bool:
    global _warned_auth_disabled
    if not expected_token:
        if not _warned_auth_disabled:
            _warned_auth_disabled = True
            print(
                'umbrella: auth disabled — service accepting unauthenticated requests',
                file=sys.stderr,
            )
        return True
    if not auth_header:
        return False
    prefix = 'Bearer '
    if not auth_header.startswith(prefix):
        return False
    presented = auth_header[len(prefix):].strip().encode('utf-8', 'surrogateescape')
    return hmac.compare_digest(presented, expected_token.encode('utf-8'))
