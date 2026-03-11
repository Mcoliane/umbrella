#!/usr/bin/env python3
from __future__ import annotations

import json


def event_payload(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
