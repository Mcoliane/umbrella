#!/usr/bin/env python3
from __future__ import annotations


def validate_identifier(value: str, field_name: str, *, allow_empty: bool = False, max_length: int = 200) -> str:
    text = str(value or "").strip()
    if not text:
        if allow_empty:
            return ""
        raise ValueError(f"{field_name} is required")
    if len(text) > max_length:
        raise ValueError(f"{field_name} exceeds max length {max_length}")
    if ".." in text or "/" in text or "\\" in text:
        raise ValueError(f"{field_name} contains invalid path characters")
    if any(ord(ch) < 32 for ch in text):
        raise ValueError(f"{field_name} contains control characters")
    return text
