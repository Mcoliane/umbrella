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
    boundary_root: Path
    token: str


def load_config(host: str, port: int, db_path: str, umbrella_root: str = '', token: str = '', boundary_root: str = '') -> MemoryConfig:
    tok = (token or '').strip() or os.environ.get('UMBRELLA_MEMORY_TOKEN', '').strip()
    root = Path(umbrella_root).resolve() if umbrella_root else Path.cwd()
    default_db = root / 'control-plane' / 'observability' / 'memory-service' / 'memory.db'
    p = Path(db_path).resolve() if db_path else default_db
    default_boundary = root / 'control-plane' / 'observability' / 'memory-boundary'
    b = Path(boundary_root).resolve() if boundary_root else default_boundary
    return MemoryConfig(host=host, port=port, db_path=p, boundary_root=b, token=tok)
