#!/usr/bin/env python3
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MemoryConfig:
    host: str
    port: int
    db_path: Path
    token: str


def load_config(host: str, port: int, db_path: str) -> MemoryConfig:
    token = os.environ.get('UMBRELLA_MEMORY_TOKEN', '').strip()
    default_db = Path('control-plane/observability/memory-service/memory.db')
    p = Path(db_path).resolve() if db_path else default_db.resolve()
    return MemoryConfig(host=host, port=port, db_path=p, token=token)
