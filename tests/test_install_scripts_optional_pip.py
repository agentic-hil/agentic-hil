"""Optional pip behavior for the Windows installer test launcher."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests import test_install_scripts as install_scripts

_MISSING_PIP_MESSAGE = "Windows launcher tests must skip with a stated reason when pip is unavailable"


def test_windows_launcher_skips_when_pip_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module_name in tuple(sys.modules):
        if module_name == "pip" or module_name.startswith("pip."):
            monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setitem(sys.modules, "pip", None)

    try:
        install_scripts._windows_launcher(Path("unused-agentic-hil.exe"), "1.0.0")
    except pytest.skip.Exception as skipped:
        reason = str(skipped).lower()
        if all(part in reason for part in ("pip", "distlib", "windows test launchers")):
            return
    except ModuleNotFoundError:
        pass

    raise AssertionError(_MISSING_PIP_MESSAGE)
