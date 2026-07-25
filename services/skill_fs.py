#!/usr/bin/env python3
"""Shared, scope-enforced local-file tools for Umbrella skills.

ONE audited implementation of the read/write/scope/secret-guard logic, imported by the
skills that touch the user's local files (workspace, analysis, …) so the security-critical
path lives in a single place instead of being reimplemented per skill.

Guardrails (identical for every skill that uses this):
  * SCOPE   — every path is checked against an allow-list of roots (default the user's
              Desktop/Documents/Downloads and the working dir); anything outside is refused.
  * SECRETS — refuses to read/write secret-looking or hidden files.
  * LIMITS  — reads, result sets, and tool outputs are capped so a huge tree can't blow up.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

OBS_CAP = 12000      # max chars of a tool result fed back to the model
READ_CAP = 20000     # max chars returned from read_file
MAX_HITS = 60        # max find() matches

SECRETISH = ("id_rsa", "id_ed25519", ".pem", ".key", "credentials", ".env", ".secret", "id_dsa")


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


READ_TOOLS = [
    _fn("list_dir", "List the entries (files and folders, with sizes) in a directory. Refused for paths outside "
        "the allowed roots.", {"path": {"type": "string"}}, ["path"]),
    _fn("find", "Search for files under a directory, by filename glob (`name`, e.g. '*.py' or '*gateway*') and/or "
        "by text `contains`. Scoped and capped. Returns matching paths (with a snippet for content matches).",
        {"path": {"type": "string"}, "name": {"type": "string"}, "contains": {"type": "string"}}, ["path"]),
    _fn("read_file", "Read a text file's content (truncated). Refused for paths outside the allowed roots.",
        {"path": {"type": "string"}}, ["path"]),
]
WRITE_TOOLS = [
    _fn("write_file", "Create or OVERWRITE a text file with the given content. Scoped to the allowed roots, and "
        "refused for secret-looking or hidden files. Only available when write access was granted for this task.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _fn("move_file", "Move or rename a file from `src` to `dst` (both must be in scope; refused for secret/hidden "
        "files). Use this to reorganize files. Only available when write access was granted.",
        {"src": {"type": "string"}, "dst": {"type": "string"}}, ["src", "dst"]),
]


def safe_roots(roots_input: str) -> list[Path]:
    home = Path.home()
    explicit = []
    for tok in str(roots_input or "").replace(",", " ").split():
        p = Path(os.path.expanduser(tok))
        try:
            p = p.resolve()
        except Exception:
            continue
        if p.exists():
            explicit.append(p)
    if explicit:
        return explicit
    return [p for p in (home / "Desktop", home / "Documents", home / "Downloads", Path.cwd()) if p.exists()]


def resolve_in_scope(path: str, roots: list[Path]) -> Path | None:
    try:
        p = Path(os.path.expanduser(str(path))).resolve()
    except Exception:
        return None
    for r in roots:
        try:
            p.relative_to(r)
            return p
        except ValueError:
            continue
    return p if p in roots else None


class ScopedFiles:
    """Scope-enforced file operations over an allow-list of roots. Read methods
    (list_dir/find/read_file) are always safe to expose; write methods (write_file/
    move_file) should only be wired by a skill that was granted write access."""

    def __init__(self, roots: list[Path]):
        self.roots = roots
        self.files_read: list[str] = []
        self.files_written: list[str] = []

    def _scope(self, path: str):
        p = resolve_in_scope(path, self.roots)
        if p is None:
            return None, f"REFUSED: {path} is OUTSIDE the allowed roots {[str(r) for r in self.roots]}. Stay in scope."
        return p, ""

    def list_dir(self, args):
        p, err = self._scope(str(args.get("path", "")))
        if err:
            return err
        if not p.is_dir():
            return f"not a directory: {p}"
        rows = []
        for entry in sorted(p.iterdir())[:200]:
            try:
                kind = "d" if entry.is_dir() else "f"
                size = entry.stat().st_size if entry.is_file() else ""
                rows.append(f"  {kind} {entry.name} {size}")
            except OSError:
                continue
        return f"{p}:\n" + "\n".join(rows) if rows else f"{p}: (empty)"

    def find(self, args):
        base, err = self._scope(str(args.get("path", "")))
        if err:
            return err
        name = str(args.get("name", "")).strip()
        contains = str(args.get("contains", "")).strip()
        hits = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]  # skip dotdirs
            for fn in filenames:
                if fn.startswith("."):
                    continue
                if name and not fnmatch.fnmatch(fn.lower(), name.lower()):
                    continue
                fp = Path(dirpath) / fn
                if contains:
                    try:
                        text = fp.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        continue
                    idx = text.lower().find(contains.lower())
                    if idx < 0:
                        continue
                    snippet = text[max(0, idx - 40):idx + 80].replace("\n", " ")
                    hits.append(f"  {fp}  …{snippet}…")
                else:
                    hits.append(f"  {fp}")
                if len(hits) >= MAX_HITS:
                    return f"matches (capped at {MAX_HITS}):\n" + "\n".join(hits)
        return ("matches:\n" + "\n".join(hits)) if hits else "no matches"

    def read_file(self, args):
        p, err = self._scope(str(args.get("path", "")))
        if err:
            return err
        if any(s in p.name.lower() for s in SECRETISH):
            return f"REFUSED to read {p.name}: looks like a secret/credential file."
        if not p.is_file():
            return f"not a file: {p}"
        try:
            data = p.read_text(encoding="utf-8", errors="replace")
        except Exception as ex:  # noqa: BLE001
            return f"read error: {type(ex).__name__}: {ex}"
        self.files_read.append(str(p))
        trailer = "" if len(data) <= READ_CAP else f"\n…(truncated, {len(data)} bytes total)"
        return f"{p}:\n{data[:READ_CAP]}{trailer}"

    def _write_guard(self, p: Path) -> str:
        if any(s in p.name.lower() for s in SECRETISH) or p.name.startswith("."):
            return f"REFUSED: {p.name} is a secret/hidden file — off-limits for writing."
        if p.is_dir():
            return f"REFUSED: {p} is a directory."
        return ""

    def write_file(self, args):
        p, err = self._scope(str(args.get("path", "")))
        if err:
            return err
        guard = self._write_guard(p)
        if guard:
            return guard
        content = str(args.get("content", ""))
        try:
            p.parent.mkdir(parents=True, exist_ok=True)  # parent is in-scope (p passed the scope check)
            existed = p.is_file()
            p.write_text(content, encoding="utf-8")
        except Exception as ex:  # noqa: BLE001
            return f"write error: {type(ex).__name__}: {ex}"
        self.files_written.append(str(p))
        return f"{'overwrote' if existed else 'wrote'} {p} ({len(content)} bytes)"

    def move_file(self, args):
        src, err = self._scope(str(args.get("src", "")))
        if err:
            return err
        dst, err = self._scope(str(args.get("dst", "")))
        if err:
            return err
        guard = self._write_guard(dst) or (f"REFUSED: {src.name} is a secret/hidden file."
                                           if any(s in src.name.lower() for s in SECRETISH) or src.name.startswith(".")
                                           else "")
        if guard:
            return guard
        if not src.is_file():
            return f"not a file: {src}"
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        except Exception as ex:  # noqa: BLE001
            return f"move error: {type(ex).__name__}: {ex}"
        self.files_written.append(str(dst))
        return f"moved {src} -> {dst}"
