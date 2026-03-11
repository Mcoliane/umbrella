#!/usr/bin/env python3
from __future__ import annotations


def check_auth(auth_header: str, expected_token: str) -> bool:
    if not expected_token:
        return True
    if not auth_header:
        return False
    prefix = 'Bearer '
    if not auth_header.startswith(prefix):
        return False
    return auth_header[len(prefix):].strip() == expected_token
