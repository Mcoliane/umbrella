"""Shared machine-toolchain survey with tool-COHERENCE analysis.

The point of coherence: it is not enough to know a tool exists somewhere — the
runtime you actually build with must have ALL the pieces it needs together. On a
Mac, for example, one Python may have Pillow but an ancient Tk (can't open a
window), while another has a modern Tk but no Pillow — so neither can run a
Pillow+Tkinter GUI as-is. This module probes each interpreter individually and
reports which combinations are runnable, plus concrete install suggestions when
the architecture needs a dependency the chosen runtime lacks.

Used by both the code agent (to build with the right runtime) and the mayor (to
make coherence suggestions and delegate the necessary installs).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_CACHE: dict | None = None

# Runtimes + CLIs probed through the login shell so version-managed tools resolve
# the same way the build will see them. Python interpreters are probed separately
# (below) because coherence needs per-interpreter detail.
_TOOL_PROBE = r'''
ver(){ case "$1" in go) go version 2>&1;; *) "$1" --version 2>&1 || "$1" -version 2>&1;; esac | head -1; }
for t in node deno bun ruby php go rustc java gcc clang swift perl Rscript; do
  command -v "$t" >/dev/null 2>&1 && echo "RT|$t|$(ver "$t")"
done
for t in git make cmake docker podman sqlite3 ffmpeg magick convert jq pandoc dot curl wget pytest npm cargo pip3 brew; do
  command -v "$t" >/dev/null 2>&1 && echo "TOOL|$t"
done
'''

# Per-interpreter probe: version, the compiled Tk version (module constant — does
# NOT create a Tk interpreter, so it never triggers the macOS Tk version abort),
# and which notable libraries are importable (find_spec = no execution, fast).
_PY_PROBE = (
    "import sys, json, importlib.util\n"
    "info = {'version': sys.version.split()[0], 'tk': None, 'libs': []}\n"
    "try:\n"
    "    import _tkinter; info['tk'] = _tkinter.TK_VERSION\n"
    "except Exception: pass\n"
    "for name, imp in [('pillow','PIL'),('numpy','numpy'),('pandas','pandas'),('fastapi','fastapi'),"
    "('flask','flask'),('django','django'),('requests','requests'),('sqlalchemy','sqlalchemy'),"
    "('matplotlib','matplotlib'),('pyqt5','PyQt5'),('pyside6','PySide6'),('pygame','pygame'),('click','click'),('rich','rich')]:\n"
    "    try:\n"
    "        if importlib.util.find_spec(imp) is not None: info['libs'].append(name)\n"
    "    except Exception: pass\n"
    "print(json.dumps(info))\n"
)


def _widened_path() -> str:
    extra = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/bin", "/bin",
             "/usr/sbin", "/sbin", os.path.expanduser("~/.cargo/bin"), os.path.expanduser("~/go/bin"),
             os.path.expanduser("~/.local/bin")]
    return os.pathsep.join([p for p in ([os.environ.get("PATH", "")] + extra) if p])


def _python_candidates(path_env: str) -> list[str]:
    paths: list[str] = []
    for name in ("python3", "python3.14", "python3.13", "python3.12", "python3.11", "python3.10"):
        found = shutil.which(name, path=path_env)
        if found:
            paths.append(found)
    for explicit in ("/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3", sys.executable):
        if explicit and os.path.exists(explicit):
            paths.append(explicit)
    fw = Path("/Library/Frameworks/Python.framework/Versions")
    if fw.is_dir():
        for ver in fw.iterdir():
            cand = ver / "bin" / "python3"
            if cand.exists():
                paths.append(str(cand))
    seen: set[str] = set()
    unique: list[str] = []
    for p in paths:
        real = os.path.realpath(p)
        if real in seen:
            continue
        seen.add(real)
        unique.append(p)
    return unique


def _probe_python(py: str, path_env: str) -> dict | None:
    try:
        proc = subprocess.run([py, "-c", _PY_PROBE], capture_output=True, text=True, timeout=15,
                              env={**os.environ, "PATH": path_env})
        line = (proc.stdout or "").strip().splitlines()[-1]
        return json.loads(line)
    except Exception:  # noqa: BLE001
        return None


def _tk_ok(tk: str | None) -> bool:
    try:
        parts = tuple(int(x) for x in str(tk).split(".")[:2])
        return parts >= (8, 6)
    except Exception:  # noqa: BLE001
        return False


def survey_environment() -> dict:
    """Return {summary, interpreters, runtimes, tools, coherenceNotes}. Cached per
    process; only version/capability probes — no network, no installs."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    path_env = _widened_path()
    runtimes: list[str] = []
    tools: list[str] = []
    try:
        proc = subprocess.run(["bash", "-lc", _TOOL_PROBE], capture_output=True, text=True, timeout=30)
        for line in (proc.stdout or "").splitlines():
            parts = line.split("|")
            if parts[0] == "RT" and len(parts) >= 3:
                runtimes.append(f"{parts[1]} ({parts[2].strip()})")
            elif parts[0] == "TOOL" and len(parts) >= 2:
                tools.append(parts[1])
    except Exception:  # noqa: BLE001
        pass

    interpreters: list[dict] = []
    for py in _python_candidates(path_env):
        info = _probe_python(py, path_env)
        if not info:
            continue
        interpreters.append({
            "path": py,
            "version": info.get("version"),
            "tk": info.get("tk"),
            "tkOk": _tk_ok(info.get("tk")),
            "libs": info.get("libs") or [],
        })

    notes: list[str] = []
    gui_ready = [i for i in interpreters if i["tkOk"] and "pillow" in i["libs"]]
    tk_ok_list = [i for i in interpreters if i["tkOk"]]
    pillow_list = [i for i in interpreters if "pillow" in i["libs"]]
    if interpreters and not gui_ready:
        if tk_ok_list:
            best = tk_ok_list[0]
            missing = [lib for lib in ("pillow",) if lib not in best["libs"]]
            note = (f"Python desktop GUI (tkinter): use {best['path']} (Python {best['version']}, Tk {best['tk']}). ")
            if missing:
                note += f"It lacks {', '.join(missing)} — install into THAT interpreter: {best['path']} -m pip install {' '.join(missing)}. "
            stale = [i for i in pillow_list if not i["tkOk"]]
            if stale:
                note += (f"(Do NOT use {stale[0]['path']}: it has Pillow but Tk {stale[0]['tk']}, which will not open a window on this OS.)")
            notes.append(note.strip())
        else:
            notes.append("No installed Python has a modern Tk (>=8.6); a Python desktop GUI needs a Tk-capable Python "
                         "(e.g. `brew install python-tk`). Prefer a non-GUI form or a web UI unless one is installed.")

    lines = ["MACHINE TOOLCHAIN — already installed; build with these and keep the runtime COHERENT (the runtime you "
             "pick must contain every piece the architecture needs):"]
    if runtimes:
        lines.append("  Runtimes: " + ", ".join(runtimes))
    if tools:
        lines.append("  CLIs/build tools: " + ", ".join(sorted(set(tools))))
    if interpreters:
        lines.append("  Python interpreters (path — Tk — importable libraries):")
        for i in interpreters:
            libs = ", ".join(i["libs"]) or "no notable libs"
            lines.append(f"    - {i['path']} (Python {i['version']}, Tk {i['tk'] or 'none'}): {libs}")
    if notes:
        lines.append("  TOOL-COHERENCE NOTES:")
        for n in notes:
            lines.append("    * " + n)
    lines.append("  If the chosen architecture needs a dependency missing from the runtime you'll use, install it into "
                 "THAT runtime as an explicit step; otherwise do not download anything.")

    _CACHE = {
        "summary": "\n".join(lines),
        "interpreters": interpreters,
        "runtimes": runtimes,
        "tools": sorted(set(tools)),
        "coherenceNotes": notes,
    }
    return _CACHE


if __name__ == "__main__":
    print(survey_environment()["summary"])
