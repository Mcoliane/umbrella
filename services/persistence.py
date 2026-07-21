#!/usr/bin/env python3
"""Shared atomic JSON persistence with cross-process file locking.

Every stateful Umbrella service keeps its state in flat JSON files and mutates
them with read-modify-write while running under a ThreadingHTTPServer. Without
coordination, two concurrent handlers (or a service plus a CLI tool) can
interleave and lose writes or observe a half-written file.

This module centralizes the safe pattern already used by memory-core:

  atomic_write_json(path, data)   tmp file in the same dir, flush+fsync, then
                                  os.replace — readers never see a partial file.
  file_lock(path)                 advisory exclusive flock held for the body of
                                  a `with` block, keyed on a sidecar
                                  ``<path>.lock``; excludes other processes and
                                  other threads (each acquisition is a distinct
                                  open file description).
  update_json(path, mutator, ...) locked read-modify-write: acquire the file
                                  lock, read (``default`` when missing/corrupt),
                                  apply ``mutator``, atomically write the result.

Locking is intentionally NOT reentrant. Combine it with the service's existing
``threading.Lock``/``RLock`` for intra-process ordering and never nest
``file_lock`` on the same path within one thread, e.g.::

    with self._lock, file_lock(self.path):
        ...

so a thread cannot deadlock against itself.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def atomic_write_json(path: Path, data: Any, *, mode: int | None = None, indent: int = 2) -> None:
    """Write ``data`` as JSON to ``path`` atomically.

    The payload is written to a temporary file in the same directory (so
    ``os.replace`` is atomic on the same filesystem), flushed and fsynced, then
    renamed over the destination. When ``mode`` is given the destination is
    chmod-ed to it (use 0o600 for secrets).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    open_mode = 0o600 if mode is None else mode
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, open_mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=indent) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        # If os.replace already ran, the tmp file is gone; ignore.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if mode is not None:
        os.chmod(path, mode)


@contextmanager
def file_lock(path: Path):
    """Hold an exclusive advisory lock for the duration of the ``with`` block.

    The lock is taken on a sidecar ``<path>.lock`` file so it never conflicts
    with atomic renames of ``path`` itself. Not reentrant — see module docs.
    """
    path = Path(path)
    lock_file = _lock_path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_file, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON from ``path``, returning ``default`` if missing or unparseable."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def update_json(
    path: Path,
    mutator: Callable[[Any], Any],
    *,
    default: Any = None,
    mode: int | None = None,
    indent: int = 2,
) -> Any:
    """Locked read-modify-write.

    Acquires the file lock, reads the current value (``default`` when missing or
    corrupt), passes it to ``mutator``, and atomically writes whatever the
    mutator returns. The mutator may mutate its argument in place and return it,
    or return a fresh object. Returns the written value.
    """
    path = Path(path)
    with file_lock(path):
        current = read_json(path, default)
        updated = mutator(current)
        atomic_write_json(path, updated, mode=mode, indent=indent)
        return updated
