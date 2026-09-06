"""`agentic-hil upgrade` when the manager that holds the installation is not on PATH.

A uv tool installation is upgraded through `uv tool upgrade`, and nothing but
uv can run that. An operator whose shell no longer resolves `uv` (a fresh
terminal before `~/.local/bin` is on PATH, a service unit with a stripped
environment, an agent host started from a launcher with a PATH of its own)
still has a working `agentic-hil`, because the launcher names its interpreter
by absolute path, and the one thing that launcher cannot do is find the
program that installed it.

The answer this code owes for that is a refusal that names the manager and the
interpreter, `upgrade_manager_not_found`, exit non-zero at the command line,
and the same document over MCP with `running_version` beside it and
`restart_required` false, because nothing was replaced and no server is behind
anything. Both lines existed and neither had ever run: the receipt that decides
the manager is uv's own, so the case is made here with the real `uv tool
install` and a PATH from which every directory holding a `uv` has been taken.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from agentic_hil.upgrade import SERVER_UPGRADE

from .conftest import ABOVE_EVERY_RELEASE, CONTAINER_ONLY, LiveServer, UvTool, Wheelhouse, fixture_configuration

pytestmark = [pytest.mark.container, CONTAINER_ONLY]


def a_path_without(name: str, path: str) -> str:
    """`path` with every directory that resolves `name` taken out, checked rather than assumed."""
    kept = [directory for directory in path.split(os.pathsep) if directory and shutil.which(name, path=directory) is None]
    stripped = os.pathsep.join(kept)
    assert shutil.which(name, path=stripped) is None, stripped
    assert stripped, "nothing was left on PATH, so the child could not start at all"
    return stripped


def test_upgrade_without_the_manager_on_path_is_a_named_refusal(uv_tool: UvTool, wheelhouse: Wheelhouse) -> None:
    """The command line, with uv gone from PATH and the receipt still saying uv.

    The refusal is read for three facts: its type, the manager it names, and
    the interpreter it names, which is the one the launcher started and not
    whatever `python` the stripped PATH might resolve. And the installation is
    read afterwards, because a refusal that had moved something would not be
    one.
    """
    uv_tool.install("--find-links", str(wheelhouse.every_version), f"agentic-hil=={ABOVE_EVERY_RELEASE}")
    without_uv = a_path_without("uv", os.environ.get("PATH", ""))

    status, result = uv_tool.upgrade(PATH=without_uv)

    assert status != 0, result
    assert result["ok"] is False, result
    assert result["error_type"] == "upgrade_manager_not_found", result
    assert result["manager"] == "uv", result
    named = Path(result["python"])
    assert named.parent == uv_tool.interpreter.parent, result
    assert "uv" in result["summary"] and "PATH" in result["summary"], result["summary"]
    # Nothing moved and nothing is claimed about a restart: there is no manager
    # to have had an outcome from.
    assert result.get("upgraded_on_disk") is not True, result
    assert "restart_required_by" not in result, result
    assert uv_tool.run("--version", PATH=without_uv).stdout.strip() == ABOVE_EVERY_RELEASE


def test_server_upgrade_without_the_manager_returns_the_refusal_rather_than_raising(uv_tool: UvTool, wheelhouse: Wheelhouse, tmp_path: Path) -> None:
    """The same machine, asked over MCP by a server that was started out of the installation.

    `replace_installation` raises for this one condition and the tool has to
    turn that into a result: a host that got an exception instead of a document
    would show the operator a transport error about a PATH. The document
    carries `running_version`, because every answer of this tool does, and
    `restart_required` false, because this run replaced nothing.
    """
    uv_tool.install("--find-links", str(wheelhouse.every_version), f"agentic-hil=={ABOVE_EVERY_RELEASE}")
    without_uv = a_path_without("uv", os.environ.get("PATH", ""))
    project = tmp_path / "project"
    project.mkdir()
    config = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state")
    # The fixture denies the upgrade permission, and this test is about what
    # happens once it is granted.
    config.write_text(config.read_text(encoding="utf-8").replace("allow_upgrade: false", "allow_upgrade: true"), encoding="utf-8")

    with LiveServer(config, project, command=[str(uv_tool.launcher), "mcp-stdio"], environment=uv_tool.environment(PATH=without_uv)) as server:
        server.initialize()
        result = server.call(SERVER_UPGRADE)

    assert result["ok"] is False, result
    assert result["error_type"] == "upgrade_manager_not_found", result
    assert result["manager"] == "uv", result
    assert Path(result["python"]).parent == uv_tool.interpreter.parent, result
    assert result["running_version"] == ABOVE_EVERY_RELEASE, result
    assert result["restart_required"] is False, result
    assert result["tool"] == SERVER_UPGRADE, result
