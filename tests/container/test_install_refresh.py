"""install.sh's refresh against a tool the real uv installed with an interpreter.

`uv tool install --python <interpreter> agentic-hil` records the interpreter in
its receipt, and the refresh in install.sh reads that receipt to decide the line
it rebuilds the installation from. Whether the record survives that line is
uv's to decide, and what uv writes into the receipt is uv's to decide too, so
the suite's own fixtures can only ever assert what somebody believed about
both. #476 was measured on a real uv: the receipt reader saw no `[tool.options]`
table, accepted the receipt, and the refresh ran `uv tool install --upgrade
--reinstall` with no `--python`, after which the `python` key was gone from the
receipt. The interpreter of the environment did not move on that run, because
uv reuses a valid environment when no `--python` is given; what was lost was the
record, and with it every later rebuild.

So this test installs with the real uv, runs the real installer against it with
a uv on PATH that only records what it is asked before handing over, and reads
the real receipt afterwards. The next time uv moves the key, this is what
notices.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import CONTAINER_ONLY, INSTALL_TIMEOUT_S, UvTool

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SHELL_SCRIPT = REPOSITORY_ROOT / "install.sh"


def receipt_document(uv_tool: UvTool) -> dict:
    """uv's receipt, parsed, so a test can ask where a key sits and not only whether it is there."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - only a 3.10 collection reaches this
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib.loads(uv_tool.receipt.read_text(encoding="utf-8"))


def recording_uv(into: Path, real_uv: str, log: Path) -> Path:
    """A `uv` that writes each argument list it is given to `log`, then runs the real one."""
    into.mkdir(parents=True)
    wrapper = into / "uv"
    wrapper.write_text(f'#!/bin/sh\nprintf \'%s\n\' "$*" >> "{log}"\nexec "{real_uv}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def test_a_refresh_keeps_the_interpreter_the_real_uv_recorded(uv_tool: UvTool, tmp_path: Path) -> None:
    """One install with `--python`, one `install.sh` refresh over it, one receipt read back.

    Three things are asserted, and each is a fact only the real tools can give:
    that uv recorded the interpreter where the reader has to look for it, that
    the refresh's own reinstall line replayed it as `--python`, and that the
    receipt uv wrote after that line still names it. `--no-agent-install`
    because the machine half is not what is under test, and `--no-can` because
    the extra is not either.

    The package comes from the index, unpinned and with no source option, and
    that is not a shortcut: what is under test is this checkout's `install.sh`,
    and a receipt that records a pin, a path or a `--find-links` is one the
    reader refuses on purpose (uv 0.12.9 writes a `--find-links` given on the
    line or through `UV_FIND_LINKS` under `[tool.options]`, measured in this
    image on 2026-09-06), which would send the refresh to the fallback whatever
    it made of the interpreter. The receipt this leaves is the one #476 quotes.
    """
    interpreter = os.path.realpath(sys.executable)
    uv_tool.install("--python", interpreter, "agentic-hil")
    before = receipt_document(uv_tool)
    assert before["tool"]["python"] == interpreter, before

    log = tmp_path / "uv-invocations"
    wrappers = tmp_path / "wrappers"
    recording_uv(wrappers, uv_tool.uv, log)
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    environment = uv_tool.environment(
        HOME=str(home),
        PATH=f"{wrappers}{os.pathsep}{uv_tool.bin_directory}{os.pathsep}{os.environ.get('PATH', '')}",
    )

    refreshed = subprocess.run(
        ["sh", str(SHELL_SCRIPT), "--no-agent-install", "--no-can"],
        cwd=str(project),
        capture_output=True,
        text=True,
        env=environment,
        timeout=INSTALL_TIMEOUT_S,
        check=False,
    )

    transcript = f"{refreshed.stdout}{refreshed.stderr}"
    assert refreshed.returncode == 0, transcript
    invocations = log.read_text(encoding="utf-8") if log.is_file() else ""
    reinstalls = [line for line in invocations.splitlines() if line.startswith("tool install --upgrade --reinstall agentic-hil")]
    assert len(reinstalls) == 1, invocations
    assert f"--python {interpreter}" in reinstalls[0], invocations
    assert not any(line.startswith("tool upgrade") for line in invocations.splitlines()), invocations
    after = receipt_document(uv_tool)
    assert after["tool"]["python"] == interpreter, after
    assert "python" not in after["tool"].get("options", {}), after
