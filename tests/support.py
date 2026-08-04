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


def owner_only_directory(directory: Path) -> Path:
    """`mkdir -p` that gives every component it creates mode 0700.

    Only missing components are touched, so a directory pytest or the operating
    system already owns keeps the mode it has. `Path.mkdir(parents=True,
    mode=...)` is not this: it applies the mode to the last component only and
    creates every parent under the ambient umask, which is 0775 on any machine
    that uses the common `umask 002`. The product treats these directories as
    trust boundaries and refuses the ones another user could write, so a mode
    inherited from a umask turns the whole suite red for a reason that has
    nothing to do with the code under test."""
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for component in reversed(missing):
        component.mkdir(mode=0o700)
    return directory


def owner_only_write(path: Path, text: str) -> Path:
    """Write a file the product will accept as trusted user configuration.

    The product writes its own files with an explicit 0600; a test writing one
    with `write_text` inherits the umask instead, which is group-writable under
    `umask 002`."""
    owner_only_directory(path.parent)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def trusted_program(source: Path) -> Path:
    """A copy of a fixture program in a directory the product will run from.

    A configured executable, and an OpenOCD script the configured executable
    reads, are refused when another local identity could replace them before
    they run. A repository checkout is exactly that on any machine using
    `umask 002`: the tree is group-writable and so are the files in it, so the
    fakes cannot be pointed at where they are committed. They are copied once
    per session next to the trusted launcher, which is already the one place
    these tests have that satisfies the rule."""
    destination = LAUNCHER_ROOT / "fixtures" / source.name
    if not destination.is_file():
        owner_only_directory(destination.parent)
        shutil.copyfile(source, destination)
        destination.chmod(0o700)
    return destination


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
        owner_only_directory(launcher.parent)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o700)
    return launcher


def remove_trusted_launcher() -> None:
    shutil.rmtree(LAUNCHER_ROOT, ignore_errors=True)
