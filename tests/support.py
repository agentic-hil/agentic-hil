"""Shared helpers that must live outside any single test module."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Captured before any test monkeypatches HOME.
REAL_HOME = Path(os.path.expanduser("~"))
# The profile root on every platform. It used to divert to Local AppData on
# Windows because the trust check refused the per-user Temp ACL; it no longer
# does, and Local AppData was the wrong place anyway — a packaged host process
# has its writes there redirected into its own LocalCache, so the launcher never
# landed where the path said it did.
LAUNCHER_ROOT = REAL_HOME / f"agentic-hil-pytest-launcher-{os.getpid()}"


def trusted_launcher() -> Path:
    """A launcher the product's trust rule accepts, created once per session.

    The rule rejects an executable that group or other may write, and a CI
    runner's Python lives under a tool cache that hands out exactly such modes,
    so `sys.executable` cannot stand in for an installed launcher: it is
    untrusted there and trusted here, which is why this only ever failed away
    from a developer machine. `tempfile.gettempdir()` is a forbidden root for an
    MCP command, so `tmp_path` cannot hold it either.
    """
    launcher = LAUNCHER_ROOT / "bin" / ("agentic-hil.exe" if os.name == "nt" else "agentic-hil")
    if not launcher.is_file():
        launcher.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o700)
    return launcher


def remove_trusted_launcher() -> None:
    shutil.rmtree(LAUNCHER_ROOT, ignore_errors=True)
