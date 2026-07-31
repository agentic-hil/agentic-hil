"""Shared helpers that must live outside any single test module."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Captured before any test monkeypatches HOME.
REAL_HOME = Path(os.path.expanduser("~"))
REAL_LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA") or REAL_HOME / "AppData" / "Local")
# Windows rejects trust-boundary paths below the replaceable per-user Temp ACL,
# so use protected Local AppData there, exactly as the sandbox fixture does.
LAUNCHER_ROOT = (REAL_LOCAL_APPDATA if os.name == "nt" else REAL_HOME) / f"agentic-hil-pytest-launcher-{os.getpid()}"


def trusted_launcher() -> Path:
    """A launcher the product's trust rule accepts, created once per session.

    The rule rejects an executable whose ancestors group or other may write, and
    a CI runner's Python lives under exactly such a prefix, so `sys.executable`
    cannot stand in for an installed launcher: it is untrusted there and trusted
    here, which is why this only ever failed away from a developer machine.
    `tempfile.gettempdir()` is a forbidden root for an MCP command, so `tmp_path`
    cannot hold it either.
    """
    launcher = LAUNCHER_ROOT / "bin" / ("agentic-hil.exe" if os.name == "nt" else "agentic-hil")
    if not launcher.is_file():
        launcher.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o700)
    return launcher


def remove_trusted_launcher() -> None:
    shutil.rmtree(LAUNCHER_ROOT, ignore_errors=True)
