#!/usr/bin/env python3
from __future__ import annotations


def contains_query(title: str, content_text: str, query: str) -> bool:
    q = (query or '').strip().lower()
    if not q:
        return True
    return q in (title or '').lower() or q in (content_text or '').lower()
