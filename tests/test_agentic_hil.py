from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest
import yaml
from conftest import (
    FAKE_OPENOCD,
    FAKE_OPENOCD_NO_TARGET,
    FAKE_STLINK,
    FAKE_STLINK_HALT_UNCONFIRMED,
    FAKE_STLINK_UNCONFIRMED,
    write_authoritative_config,
    write_config,
)
from fixtures import fake_openocd
from support import trusted_launcher

from agentic_hil import __version__
from agentic_hil import process as process_module
from agentic_hil.artifacts import ArtifactManager
from agentic_hil.backends import openocd as openocd_backend
from agentic_hil.backends.common import CompletedCommand, command_for_log
from agentic_hil.backends.pyocd import parse_pyocd_probes
from agentic_hil.backends.stlink import stlink_empty_result, stlink_probe_ids
from agentic_hil.can import CanFrame, ProcessCanAdapterSession, open_python_can_adapter
from agentic_hil.cli import (
    _posix_filesystem_path,
    _smooth_user_permissions,
    build_parser,
    debugger_probes,
    doctor,
    entrypoint,
    init_config,
    init_project,
    initialized_config_path,
    install_agent,
    install_skill,
    is_agentic_hil_setup_skill,
    mcp_config,
    mcp_server_command,
    register_agent_mcp,
    resolve_skill_agent,
    restrict_agent_write_access,
    schema,
    setup_project,
    skill_version,
    test_schema,
    upgrade_installation,
)
from agentic_hil.comports import ComPortService
from agentic_hil.config import (
    ConfigError,
    load_config,
    project_config_directory,
    project_config_path,
    tighten_owned_writable_ancestors,
    trusted_persistent_executable,
    untrusted_launcher_directory,
    user_file_lock_path,
    user_state_root,
)
from agentic_hil.mcp import MCP_PROTOCOL_VERSION, MCP_TOOL_NAMES, MCP_TOOLS, handle_mcp_message
from agentic_hil.process import ProcessImage, spawn_managed_process, terminate_process_tree
from agentic_hil.report import logs_directory
from agentic_hil.tools import AgenticHILToolService, UnprovisionedToolService
from agentic_hil.types import CURRENT_CONFIG_VERSION


def mcp_tool_call(service: AgenticHILToolService, name: str, arguments: dict | None = None) -> dict:
    response = handle_mcp_message(
        {"jsonrpc": "2.0", "id": name, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}},
        service,
    )
    assert isinstance(response, dict)
    return response["result"]["structuredContent"]


def test_init_config_writes_a_deterministic_external_config_that_grants_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    config_path = initialized_config_path(workspace)

    result = init_config()

    assert result["ok"] is True, json.dumps(result)
    assert result["path"] == str(config_path)
    config_text = config_path.read_text(encoding="utf-8")
    assert f"workspace_root: {json.dumps(str(workspace.resolve()))}" in config_text
    # Born on the read-free model: reading has no key to set, so every
    # permission line a fresh config carries is about writing — and since
    # 0.8.0 every one of them that grants a capability is granted, so the
    # bench this writes is workable without anybody opening it.
    assert f"version: {CURRENT_CONFIG_VERSION}\n" in config_text
    assert "allow_probe: " not in config_text
    assert "allow_flash: true" in config_text
    assert "allow_reset: true" in config_text
    assert "allow_upload: true" in config_text
    assert "allow_config_permissions_write: true" in config_text
    # The two exceptions, and the reason the bench above is workable: either one
    # true refuses flash_firmware on that probe, and neither has a tool behind it
    # to trade for that.
    assert "allow_raw_debugger_commands: false" in config_text
    assert "allow_mass_erase: false" in config_text
    # And nothing else in the file it wrote is off, whatever the key is called: a
    # generation decides the permission state completely rather than half.
    document = yaml.safe_load(config_text)

    def denied(node: object, prefix: str = "") -> list[str]:
        if not isinstance(node, dict):
            return []
        found: list[str] = []
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key).startswith("allow_") and value is not True:
                found.append(path)
            found += denied(value, path)
        return found

    assert denied(document) == [
        "debuggers.dut.permissions.allow_raw_debugger_commands",
        "debuggers.dut.permissions.allow_mass_erase",
    ]
    assert initialized_config_path(workspace) == config_path


def test_setup_runs_all_steps_in_one_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    home = Path.home()
    command = _trusted_test_mcp_command(monkeypatch)
    config_path = project_config_path(workspace)
    assert not config_path.exists(), "setup must bootstrap before any project config exists"

    real_which = shutil.which
    monkeypatch.setattr("agentic_hil.upgrade.shutil.which", lambda name: None if name == "claude" else real_which(name))

    result = setup_project(agent="claude-code")

    assert result["ok"] is True, result
    assert result["steps"]["config"]["ok"] is True
    assert result["steps"]["skill_install"]["ok"] is True
    assert result["steps"]["mcp_config"]["ok"] is True
    assert result["steps"]["doctor"]["ok"] is True
    # MCP registration is USER-level, never written into the (untrusted) repo.
    assert not (workspace / ".mcp.json").exists()
    claude_json = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert "agentic-hil" in claude_json["mcpServers"]
    assert claude_json["mcpServers"]["agentic-hil"]["command"] == command
    assert (home / ".claude" / "skills" / "agentic-hil" / "SKILL.md").is_file()
    assert config_path.is_file()
    # This run wrote a registration, so the session that ran it is looking at
    # tools that cannot appear until it restarts, and the result says so.
    assert result["restart_required"] is True
    assert "Claude Code" in result["restart_notice"]


def test_setup_force_preserves_existing_authoritative_config_byte_for_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _trusted_test_mcp_command(monkeypatch)
    assert init_config()["ok"] is True
    config_path = initialized_config_path(workspace)
    before = config_path.read_bytes()

    result = setup_project(agent="claude-code", force=True)

    assert result["ok"] is True, result
    assert result["steps"]["config"]["skipped"] is True
    assert config_path.read_bytes() == before


def test_setup_rolls_back_new_config_skill_registration_and_mcp_config_on_late_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentic_hil import cli as cli_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _trusted_test_mcp_command(monkeypatch)
    real_register = cli_module.register_agent_mcp

    def write_then_fail(*args: object, **kwargs: object) -> dict:
        written = real_register(*args, **kwargs)
        assert written["ok"] is True
        return {"ok": False, "error_type": "injected_failure", "summary": "late MCP failure"}

    monkeypatch.setattr("agentic_hil.cli.register_agent_mcp", write_then_fail)
    config_path = initialized_config_path(workspace)
    skill_path = Path.home() / ".codex" / "skills" / "agentic-hil" / "SKILL.md"
    agents_path = Path.home() / ".codex" / "AGENTS.md"
    mcp_path = Path.home() / ".codex" / "config.toml"

    result = setup_project(agent="codex")

    assert result["ok"] is False
    assert result["rollback"]["ok"] is True
    assert not config_path.exists()
    assert not skill_path.exists()
    assert not agents_path.exists()
    assert not mcp_path.exists()


def test_setup_keeps_a_skill_it_did_not_install_and_says_so(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback undoes setup's own writes, not a command that already succeeded.

    Measured: a model ran the documented `skill-install` first and `setup`
    second, setup refused the operator's config exactly as it should, and the
    skill from the earlier command survived. Removing it would mean setup
    undoing work it never did; saying nothing would leave a skill pointing
    hardware work at tools no longer registered.
    """
    from agentic_hil import cli as cli_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _trusted_test_mcp_command(monkeypatch)
    assert install_skill("codex")["ok"] is True
    skill_path = Path.home() / ".codex" / "skills" / "agentic-hil" / "SKILL.md"
    before = skill_path.read_bytes()
    real_register = cli_module.register_agent_mcp

    def write_then_fail(*args: object, **kwargs: object) -> dict:
        real_register(*args, **kwargs)
        return {"ok": False, "error_type": "injected_failure", "summary": "late MCP failure"}

    monkeypatch.setattr("agentic_hil.cli.register_agent_mcp", write_then_fail)

    result = setup_project(agent="codex")

    assert result["ok"] is False
    assert result["rollback"]["ok"] is True
    assert skill_path.read_bytes() == before
    assert result["left_behind"]["skill"] == str(skill_path)
    assert str(skill_path) in result["left_behind"]["next_step"]
    assert not initialized_config_path(workspace).exists()


def test_doctor_reports_an_editable_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An editable install points the gate at a tree that can change under it.

    Measured: two models fell back to `uv tool install --editable` after pip
    refused, and nothing in the product said a word about it.
    """
    from agentic_hil.cli import _doctor_installation_report

    class Editable:
        def read_text(self, name: str) -> str:
            assert name == "direct_url.json"
            return json.dumps({"url": "file:///workspace/source", "dir_info": {"editable": True}})

    monkeypatch.setattr("importlib.metadata.distribution", lambda name: Editable())

    report = _doctor_installation_report()

    assert report["editable"] is True
    assert "reinstall it" in report["summary"]
    assert report["package_path"].endswith("agentic_hil")


def test_doctor_calls_a_copied_installation_what_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.cli import _doctor_installation_report

    class Copied:
        def read_text(self, name: str) -> str:
            return json.dumps({"url": "file:///tmp/agentic_hil-0.4.0.tar.gz"})

    monkeypatch.setattr("importlib.metadata.distribution", lambda name: Copied())

    report = _doctor_installation_report()

    assert report["editable"] is False
    assert "summary" not in report


# The four subprocesses one `agentic-hil upgrade` can run, and canned answers
# for each. The resolution query is first and is the one that decides whether
# any of the others happen at all: pip's own `--dry-run --report -`, whose
# `install` list is empty exactly when pip would install nothing.
PIP_UPGRADE_COMMAND = [sys.executable, "-m", "pip", "install", "--upgrade", "agentic-hil"]
UV_PIP_UPGRADE_COMMAND = ["uv.exe", "pip", "install", "--python", sys.executable, "--upgrade", "agentic-hil"]
PIP_WOULD_INSTALL_NOTHING = subprocess.CompletedProcess[str]([], 0, json.dumps({"version": "1", "install": []}), "")
PIP_WOULD_INSTALL_A_RELEASE = subprocess.CompletedProcess[str]([], 0, json.dumps({"version": "1", "install": [{"metadata": {"name": "agentic-hil", "version": "9.9.9"}}]}), "")
UV_PIP_WOULD_CHANGE_NOTHING = subprocess.CompletedProcess[str]([], 0, "Resolved 8 packages in 12ms\nChecked 8 packages in 1ms\nWould make no changes\n", "")
MANAGER_INSTALLED = subprocess.CompletedProcess[str]([], 0, "installed\n", "")
AGENT_INSTALL_DONE = subprocess.CompletedProcess[str]([], 0, json.dumps({"ok": True, "steps": {"skill_install": {"ok": True}, "mcp_config": {"ok": True}}}), "")


def _version_answer(version: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess[str]([], 0, f"{version}\n", "")


def _recording_manager(
    monkeypatch: pytest.MonkeyPatch,
    *,
    answers: dict[str, subprocess.CompletedProcess[str]],
    manager: str = "pip",
    command: list[str] | None = None,
) -> list[tuple[list[str], str | None]]:
    """Every subprocess this upgrade runs, recorded, with one canned answer per kind.

    The kinds are `resolution` (the non-mutating `--dry-run` query),
    `install` (the one command that can replace this installation),
    `version` (the import check) and `agent-install` (the refresh). A kind with
    no answer fails the test where it is run rather than defaulting to
    something, which is what lets a test assert that a mutating command was
    never reached by simply not offering an answer for it.
    """
    calls: list[tuple[list[str], str | None]] = []

    def kind_of(invoked: list[str]) -> str:
        if "--dry-run" in invoked:
            return "resolution"
        if invoked[-1] == "--version":
            return "version"
        if "agent-install" in invoked:
            return "agent-install"
        return "install"

    def run(invoked: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
        calls.append((invoked, cwd))
        kind = kind_of(invoked)
        if kind not in answers:
            pytest.fail(f"the upgrade ran a {kind} subprocess it was not supposed to reach: {invoked}")
        return answers[kind]

    monkeypatch.setattr("agentic_hil.upgrade._upgrade_command", lambda: (manager, command or PIP_UPGRADE_COMMAND))
    monkeypatch.setattr("agentic_hil.upgrade._processes_holding_installation", list)
    monkeypatch.setattr("agentic_hil.upgrade._run_upgrade_process", run)
    return calls


def _place_agent_skill(agent_id: str) -> Path:
    """The skill file a previous `agentic-hil agent-install` left for this agent."""
    from agentic_hil.cli import _agent_skill_target

    agent = resolve_skill_agent(agent_id)
    assert agent is not None
    target = _agent_skill_target(agent)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# installed by an earlier release\n", encoding="utf-8")
    return target


def test_upgrade_uses_running_python_and_refreshes_the_agent_it_had_set_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _place_agent_skill("opencode")
    calls = _recording_manager(
        monkeypatch,
        answers={
            "resolution": PIP_WOULD_INSTALL_A_RELEASE,
            "install": MANAGER_INSTALLED,
            "version": _version_answer("9.9.9"),
            "agent-install": AGENT_INSTALL_DONE,
        },
    )

    result = upgrade_installation(["opencode"])

    assert result["ok"] is True
    assert result["version"] == "9.9.9"
    assert result["restart_required"] is True
    # The one outcome that may claim an upgrade, and it says which two numbers
    # it moved between rather than asserting movement in the abstract.
    assert result["summary"].startswith(f"Agentic HIL upgraded from {__version__} to 9.9.9; restart agent hosts to load the new MCP server.")
    assert "error_type" not in result
    # The resolution query comes first and is not a command that could replace
    # anything; only then the manager, the import check, and the refresh.
    assert calls[0][0][:3] == [sys.executable, "-m", "pip"] and "--dry-run" in calls[0][0]
    assert calls[1][0] == PIP_UPGRADE_COMMAND
    assert calls[2] == ([sys.executable, "-m", "agentic_hil", "--version"], None)
    assert calls[3][0] == [sys.executable, "-m", "agentic_hil", "agent-install", "--agent", "opencode", "--force"]
    assert calls[3][1] is not None


def test_upgrade_stops_before_the_agent_refresh_when_the_package_manager_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manager that failed on the network, over an installation that survived it.

    The refresh is not reached, because there is no new package to refresh out
    of, and the result says the installation is intact rather than leaving a
    reader to assume it either way."""
    _place_agent_skill("opencode")
    _recording_manager(
        monkeypatch,
        answers={
            "resolution": PIP_WOULD_INSTALL_A_RELEASE,
            "install": subprocess.CompletedProcess([], 1, "", "network failed"),
            "version": _version_answer(__version__),
        },
    )

    result = upgrade_installation(["opencode"])

    assert result["ok"] is False
    assert result["error_type"] == "upgrade_failed"
    assert result["install"]["stderr"] == "network failed"
    assert result["installation_intact"] is True
    assert result["version"] == __version__
    assert result["restart_required"] is False
    assert "refreshed" not in result


def test_a_manager_failure_that_changed_the_version_on_disk_is_reported_as_half_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed run that replaced files before it stopped is neither of the two it used to be.

    `_failed_upgrade` measures what the installation loads after the manager
    stops. The reported defect was reading any loadable version as intact and
    `was not replaced`: with the manager failed and the on-disk package now a
    different release, that states the opposite of what the probe found. Here the
    post-failure import answers a version other than the running one, so the
    result names both numbers, refuses the restart, and points at the same
    known-good reinstall a broken installation gets.
    """
    from agentic_hil.knowledge import ERROR_CATALOGUE

    monkeypatch.setattr("agentic_hil.upgrade._installed_extras", lambda: ("can",))
    monkeypatch.setattr("agentic_hil.upgrade._distribution_installer", lambda: "pip")
    _recording_manager(
        monkeypatch,
        answers={
            "resolution": PIP_WOULD_INSTALL_A_RELEASE,
            "install": subprocess.CompletedProcess([], 1, "", "post-install step failed"),
            "version": _version_answer("9.9.9"),
        },
    )

    result = upgrade_installation(["opencode"])

    assert result["ok"] is False
    assert result["error_type"] == "installation_changed_after_failed_upgrade"
    assert result["installation_intact"] is False
    assert result["changed_on_disk"] is True
    assert result["previous_version"] == __version__
    assert result["version"] == "9.9.9"
    assert result["previous_version"] != result["version"]
    assert result["restart_required"] is False
    assert result["installed_extras"] == ["can"]
    assert "agentic-hil[can]" in result["reinstall_command"]
    assert result["reinstall_command"] in result["summary"]
    assert "9.9.9" in result["summary"] and __version__ in result["summary"]
    assert "was not replaced" not in result["summary"]
    assert result["remediation"] == list(ERROR_CATALOGUE["installation_changed_after_failed_upgrade"].remediation)
    # The refresh is never reached: there is no trustworthy new package to
    # refresh out of, only a half-changed tree the reinstall has to replace.
    assert "refreshed" not in result


# The line `uv tool upgrade` wrote on the reporter's Linux box, beside the exit
# code 0 that was read as success while the installation stayed on 0.7.1.
_UV_EXACT_PIN_HINT = (
    "hint: `agentic-hil` is pinned to `0.7.1` (installed with an exact version pin); "
    "reinstall with `uv tool install agentic-hil@latest` to upgrade to a new version."
)


def _upgrade_reporting(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manager: str,
    command: list[str],
    installed: subprocess.CompletedProcess[str],
    version_after: str,
) -> list[list[str]]:
    """Drive one upgrade with a fixed manager outcome and a fixed resulting version."""
    calls: list[list[str]] = []

    def run(invoked: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(invoked)
        if invoked[-1] == "--version":
            return subprocess.CompletedProcess(invoked, 0, f"{version_after}\n", "")
        if "skill-install" in invoked:
            return subprocess.CompletedProcess(invoked, 0, '{"ok": true}\n', "")
        return installed

    monkeypatch.setattr("agentic_hil.upgrade._upgrade_command", lambda: (manager, command))
    monkeypatch.setattr("agentic_hil.upgrade._processes_holding_installation", list)
    monkeypatch.setattr("agentic_hil.upgrade._run_upgrade_process", run)
    return calls


def test_upgrade_blocked_by_an_exact_pin_names_the_pin_and_keeps_the_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`uv tool upgrade` cannot move an exact pin and exits 0 saying so on stderr.

    The reported defect in one result: `previous_version` and `version` both
    0.7.1, `ok: true`, `restart_required: true`, and uv's plain-text reason
    passed through unread. The operator restarted onto the same release and
    carried on believing they were on 0.8.0.

    The suggested command has to carry the extras, because uv's own hint does
    not: `agentic-hil@latest` re-resolves the bare distribution and uninstalls
    what `[can]` brought in, which takes CAN support off a bench that has it.
    """
    monkeypatch.setattr("agentic_hil.upgrade._installed_extras", lambda: ("can",))
    calls = _upgrade_reporting(
        monkeypatch,
        manager="uv",
        command=["uv.exe", "tool", "upgrade", "agentic-hil"],
        installed=subprocess.CompletedProcess([], 0, "", _UV_EXACT_PIN_HINT),
        version_after=__version__,
    )

    result = upgrade_installation(["opencode"])

    assert result["ok"] is False
    assert result["error_type"] == "upgrade_blocked_by_pin"
    assert result["restart_required"] is False
    assert result["previous_version"] == __version__
    assert result["version"] == __version__
    assert result["pinned_version"] == "0.7.1"
    assert result["installed_extras"] == ["can"]
    assert result["reinstall_command"] == 'uv tool install "agentic-hil[can]@latest"'
    assert result["install"]["stderr"] == _UV_EXACT_PIN_HINT
    # Nothing was replaced, so refreshing the skills out of it would be work
    # with no effect reported under a result whose content is that nothing moved.
    assert not any("skill-install" in call for call in calls)
    assert any("reinstall_command" in step for step in result["remediation"])
    assert any("agentic-hil@latest" in step for step in result["do_not"])


def test_upgrade_that_finds_nothing_newer_succeeds_without_asking_for_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Already current is a success: nothing to do, and nothing wrong.

    Told apart from the pinned case by what the manager said, not by the exit
    code, which is 0 for both. The two answers differ in what they cost the
    operator: this one costs nothing, while a pin means the release they wanted
    is not the one they are running. Refusing both would make
    `agentic-hil upgrade` exit non-zero on every up-to-date machine and break
    the provisioning scripts that run it unconditionally -- the defect was
    about claiming an upgrade that never happened, not about reporting that
    there was none to make.
    """
    calls = _upgrade_reporting(
        monkeypatch,
        manager="uv",
        command=["uv.exe", "tool", "upgrade", "agentic-hil"],
        installed=subprocess.CompletedProcess([], 0, "Nothing to upgrade\n", ""),
        version_after=__version__,
    )

    result = upgrade_installation(["opencode"])

    assert result["ok"] is True
    assert result["already_current"] is True
    assert "error_type" not in result
    # The success that matters: it does not claim an upgrade and does not send
    # anyone to a restart that would reload the same release.
    assert result["restart_required"] is False
    assert "upgraded" not in result["summary"]
    assert result["previous_version"] == __version__
    assert result["version"] == __version__
    assert "reinstall_command" not in result
    assert "pinned_version" not in result
    assert not any("skill-install" in call for call in calls)


# ---------------------------------------------------------------------------
# Already current, decided before anything can be replaced.
#
# The reported defect, measured on a pip `--user` installation that was already
# at the newest release: `agentic-hil upgrade` ran the manager anyway, the
# manager failed, and the failure took the installation with it. The manager
# ran because nothing had asked whether it needed to. `uv tool upgrade` and
# `pipx upgrade` answer that question inside themselves and exit without
# touching anything; the two pip-shaped routes answer it while they are already
# replacing files, so it has to be put to them first, as the same install with
# `--dry-run`.


def test_an_already_current_pip_installation_runs_no_command_that_could_replace_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tooth of the idempotence half: no answer is offered for `install`.

    `_recording_manager` fails the test on any subprocess kind it was not given
    an answer for, so the assertion that the mutating command never ran is made
    by its absence from the answers rather than by counting calls afterwards.
    The refresh is absent for the same reason: nothing moved, so there is
    nothing to rewrite anybody's skill file out of."""
    _place_agent_skill("opencode")
    calls = _recording_manager(monkeypatch, answers={"resolution": PIP_WOULD_INSTALL_NOTHING})

    result = upgrade_installation(["opencode"])

    assert result["ok"] is True
    assert result["already_current"] is True
    assert result["install_skipped"] is True
    assert result["restart_required"] is False
    assert result["previous_version"] == result["version"] == __version__
    assert "upgraded_on_disk" not in result
    assert "refreshed" not in result
    assert "error_type" not in result
    # One subprocess, and it is a question rather than an install.
    assert len(calls) == 1
    assert calls[0][0] == [*PIP_UPGRADE_COMMAND, "--dry-run", "--quiet", "--report", "-"]
    # The summary is what an operator reads, so it carries the fact the fields
    # carry: nothing was installed, and nothing needs restarting.
    assert "no command that could replace it was run" in result["summary"]


def test_the_uv_pip_route_is_short_circuited_by_its_own_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`uv pip install --upgrade` shares the shape and therefore the defect.

    It is the other route that hands a bare requirement to a resolver, so it is
    asked the same way. uv publishes no machine-readable report for `uv pip`, so
    the answer is read out of the line it prints, exactly as the exact-pin hint
    is. `uv tool upgrade` and `pipx upgrade` are not asked: both read the
    requirement they recorded and exit without touching the environment, which
    is the check itself, one level down."""
    calls = _recording_manager(
        monkeypatch,
        answers={"resolution": UV_PIP_WOULD_CHANGE_NOTHING},
        manager="uv",
        command=UV_PIP_UPGRADE_COMMAND,
    )

    result = upgrade_installation([])

    assert result["already_current"] is True
    assert result["install_skipped"] is True
    assert calls[0][0] == [*UV_PIP_UPGRADE_COMMAND, "--dry-run"]


@pytest.mark.parametrize(
    "unreadable",
    [
        subprocess.CompletedProcess[str]([], 2, "", "no such option: --report"),
        subprocess.CompletedProcess[str]([], 0, "not json at all", ""),
        subprocess.CompletedProcess[str]([], 0, json.dumps({"version": "1"}), ""),
    ],
)
def test_a_resolution_this_program_cannot_read_upgrades_exactly_as_before(
    unreadable: subprocess.CompletedProcess[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old pip, a changed report, a query that failed: none of them is an answer.

    The check exists to stop an upgrade that has nothing to do, never to invent
    one. Anything it cannot read falls through to the upgrade this command
    always ran, so a machine with a pip too old for `--report` is slower to
    answer and never wrong."""
    _recording_manager(
        monkeypatch,
        answers={"resolution": unreadable, "install": MANAGER_INSTALLED, "version": _version_answer("9.9.9")},
    )

    result = upgrade_installation([])

    assert result["ok"] is True
    assert result["upgraded_on_disk"] is True
    assert result["version"] == "9.9.9"


# ---------------------------------------------------------------------------
# A failed upgrade, and whether it left an installation behind.


def test_an_upgrade_that_destroyed_the_installation_names_the_command_that_repairs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second half of the report: the shim survived and the package did not.

    An `upgrade_failed` on its own told the operator that a package manager
    returned non-zero. It did not tell them that `agentic-hil` would now die
    with a ModuleNotFoundError on the next call, and it named no way back. The
    difference is measured rather than assumed: the same import the console
    script performs, run after the manager stopped.

    The extras are read before the manager runs, which is what the fading fake
    below pins. By the time this result is written the distribution metadata
    that names them is part of what is missing, so a repair command built then
    would name the bare distribution and take `[can]` off a bench that had it.
    """
    from agentic_hil.knowledge import ERROR_CATALOGUE

    seen: list[int] = []

    def fading_extras() -> tuple[str, ...]:
        seen.append(1)
        return ("can",) if len(seen) == 1 else ()

    monkeypatch.setattr("agentic_hil.upgrade._installed_extras", fading_extras)
    monkeypatch.setattr("agentic_hil.upgrade._distribution_installer", lambda: "pip")
    _recording_manager(
        monkeypatch,
        answers={
            "resolution": PIP_WOULD_INSTALL_A_RELEASE,
            "install": subprocess.CompletedProcess([], 1, "", "could not install"),
            "version": subprocess.CompletedProcess([], 1, "", "ModuleNotFoundError: No module named 'agentic_hil'"),
        },
    )

    result = upgrade_installation([])

    assert result["ok"] is False
    assert result["error_type"] == "installation_broken"
    assert result["installation_broken"] is True
    assert result["installed_extras"] == ["can"]
    assert "agentic-hil[can]" in result["reinstall_command"]
    assert result["reinstall_command"] in result["summary"]
    assert "installation is now broken" in result["summary"]
    assert result["restart_required"] is False
    assert "installation_intact" not in result
    assert result["remediation"] == list(ERROR_CATALOGUE["installation_broken"].remediation)
    assert result["verification"]["stderr"].startswith("ModuleNotFoundError")


def test_a_manager_that_exited_zero_and_left_nothing_loadable_is_the_same_broken_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other door to the same end state, and it used to have its own name.

    `upgrade_verification_failed` said something could not be loaded and
    stopped there. The fact is the installation is gone and the fix is one
    command, so it is reported as the one thing it is."""
    monkeypatch.setattr("agentic_hil.upgrade._installed_extras", tuple)
    monkeypatch.setattr("agentic_hil.upgrade._distribution_installer", lambda: "pip")
    _recording_manager(
        monkeypatch,
        answers={
            "resolution": PIP_WOULD_INSTALL_A_RELEASE,
            "install": MANAGER_INSTALLED,
            "version": subprocess.CompletedProcess([], 1, "", "ModuleNotFoundError: No module named 'agentic_hil.cli'"),
        },
    )

    result = upgrade_installation([])

    assert result["error_type"] == "installation_broken"
    assert result["installation_broken"] is True
    assert result["install"]["returncode"] == 0
    assert result["reinstall_command"]
    assert "-m pip install --upgrade" in result["reinstall_command"]


def test_a_per_user_pip_installation_is_upgraded_inside_its_own_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--user` is what keeps pip replacing in place instead of moving the package.

    Without it, pip uninstalls the distribution from the per-user site and
    installs the replacement into the interpreter's default site: two
    directories, so a failure between them leaves the console script in the
    per-user Scripts directory with no package under it. That is the reported
    end state, reached without this program asking for a reinstall or an
    uninstall of its own, and the flag is the whole of the difference."""
    from agentic_hil.upgrade import _upgrade_command, reinstall_command_with_extras

    monkeypatch.setattr(sys, "prefix", "C:/Python313")
    monkeypatch.setattr(sys, "executable", "PYTHON")
    monkeypatch.setattr("agentic_hil.upgrade._distribution_installer", lambda: "pip")
    monkeypatch.setattr("agentic_hil.upgrade._installed_extras", tuple)
    monkeypatch.setattr("agentic_hil.upgrade._user_site_installation", lambda: True)

    assert _upgrade_command() == ("pip", ["PYTHON", "-m", "pip", "install", "--upgrade", "--user", "agentic-hil"])
    # And the line this program prints for a person to run says the same thing,
    # because a repair that puts the package back somewhere else is not one.
    assert reinstall_command_with_extras(("can",)) == '"PYTHON" -m pip install --upgrade --user "agentic-hil[can]"'


def _installation_located_at(monkeypatch: pytest.MonkeyPatch, *, distribution_at: Path, user_site: Path) -> None:
    """One per-user site, and one place the distribution sits, both stated.

    Stated rather than read off whichever interpreter the suite happens to be
    running under. The first version of this asked the running one and asserted
    that it was a virtual environment, which is true under `.venv` and false in
    the Linux CI container, where the suite runs against the system Python at
    `/usr/local`: an assertion about the test host rather than about the code,
    and the container is where it was caught.
    """
    monkeypatch.setattr("sysconfig.get_path", lambda name, scheme=None, *_args, **_kwargs: str(user_site) if name == "purelib" else "")
    monkeypatch.setattr("importlib.metadata.distribution", lambda _name: SimpleNamespace(locate_file=lambda _relative: distribution_at))


def test_a_distribution_in_the_per_user_site_is_the_one_that_keeps_its_scheme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--user` is earned by the location, and by nothing else.

    Adding it to a system-wide installation would scatter the package into a
    per-user directory nothing on PATH points at, which is the same class of
    damage from the other side, so the flag follows where the distribution
    actually is."""
    from agentic_hil.upgrade import _user_site_installation

    user_site = tmp_path / "user-site"
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "python"))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "python"))

    _installation_located_at(monkeypatch, distribution_at=user_site, user_site=user_site)
    assert _user_site_installation() is True

    _installation_located_at(monkeypatch, distribution_at=tmp_path / "python" / "site-packages", user_site=user_site)
    assert _user_site_installation() is False


def test_a_virtual_environment_is_never_called_a_per_user_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pip refuses `--user` inside a virtual environment, so the check does too.

    Decided by the two prefixes differing and nothing else: a distribution
    sitting exactly where the per-user site is still answers False here,
    because the command the flag would be added to fails outright there."""
    from agentic_hil.upgrade import _user_site_installation

    user_site = tmp_path / "user-site"
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "python"))
    _installation_located_at(monkeypatch, distribution_at=user_site, user_site=user_site)

    assert _user_site_installation() is False


# ---------------------------------------------------------------------------
# What an upgrade leaves behind outside the package: the agent skills and the
# MCP registrations this installation wrote. Neither is inside the package, so
# replacing the package leaves both describing a release that is gone, and the
# registration names a launcher an upgrade is entitled to move.


def _upgrade_with_refresh(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_install: subprocess.CompletedProcess[str] = AGENT_INSTALL_DONE,
) -> list[tuple[list[str], str | None]]:
    return _recording_manager(
        monkeypatch,
        answers={
            "resolution": PIP_WOULD_INSTALL_A_RELEASE,
            "install": MANAGER_INSTALLED,
            "version": _version_answer("9.9.9"),
            "agent-install": agent_install,
        },
    )


def test_a_successful_upgrade_refreshes_only_the_agents_this_installation_had_set_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule that keeps a maintenance command from registering MCP servers.

    An agent with a skill or a registration already there had this installation
    set up for it, and both files describe a release that has just been
    replaced. An agent with neither was never set up, and an upgrade that
    installed for it would be adding a server to somebody's machine on the
    strength of `agentic-hil upgrade`."""
    _place_agent_skill("opencode")
    calls = _upgrade_with_refresh(monkeypatch)

    result = upgrade_installation([])

    assert result["ok"] is True
    refreshed = {entry["agent"]: entry for entry in result["refreshed"]}
    assert set(refreshed) == {"opencode", "claude-code", "codex"}
    assert refreshed["opencode"]["skill"] is True
    assert refreshed["opencode"]["registration"] is True
    for untouched in ("claude-code", "codex"):
        assert refreshed[untouched]["skill"] is False
        assert refreshed[untouched]["registration"] is False
        assert "Nothing to refresh" in refreshed[untouched]["summary"]
    # Exactly one refresh, run out of the new package rather than in this
    # process, which still holds the release that was just replaced.
    invocations = [command for command, _cwd in calls if "agent-install" in command]
    assert invocations == [[sys.executable, "-m", "agentic_hil", "agent-install", "--agent", "opencode", "--force"]]


def test_a_registration_that_exists_without_a_skill_is_refreshed_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Either half being present is what marks an agent as one of ours.

    A registration alone is the state an operator reaches by removing a skill
    file, and it is the half that names the launcher, so it is the one an
    upgrade most has to rewrite."""
    registration = Path.home() / ".claude.json"
    registration.write_text(json.dumps({"mcpServers": {"agentic-hil": {"type": "stdio", "command": "agentic-hil", "args": ["mcp-stdio"]}}}), encoding="utf-8")
    calls = _upgrade_with_refresh(monkeypatch)

    result = upgrade_installation([])

    invocations = [command for command, _cwd in calls if "agent-install" in command]
    assert invocations == [[sys.executable, "-m", "agentic_hil", "agent-install", "--agent", "claude-code", "--force"]]
    assert result["summary"].endswith("The MCP registration was rewritten for Claude Code, so restart Claude Code to load it.")


def test_naming_an_agent_narrows_the_refresh_and_never_widens_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--agent` selects among the agents that are set up, and adds none.

    Naming an agent that has neither half still installs nothing for it and
    says so, which is the same rule the unnamed case follows: an upgrade does
    not decide that somebody wants a new agent integration."""
    _place_agent_skill("opencode")
    calls = _upgrade_with_refresh(monkeypatch)

    result = upgrade_installation(["codex"])

    assert [entry["agent"] for entry in result["refreshed"]] == ["codex"]
    assert result["refreshed"][0]["registration"] is False
    assert not [command for command, _cwd in calls if "agent-install" in command]


def test_a_refresh_that_failed_is_reported_and_does_not_fail_the_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The package moved, which is what this command promises.

    What did not is a file maintained for somebody else's program, so it is
    reported per agent with the exact line that finishes it, and the upgrade
    that did succeed is not turned into a failure by it."""
    _place_agent_skill("opencode")
    _upgrade_with_refresh(monkeypatch, agent_install=subprocess.CompletedProcess([], 1, "", "could not write the skill"))

    result = upgrade_installation([])

    assert result["ok"] is True
    assert result["upgraded_on_disk"] is True
    entry = result["refreshed"][0]
    assert entry["agent"] == "opencode"
    assert entry["ok"] is False
    assert entry["skill"] is False
    assert entry["registration"] is False
    assert entry["command"] == [sys.executable, "-m", "agentic_hil", "agent-install", "--agent", "opencode", "--force"]
    assert "could not be refreshed for opencode" in result["summary"]


def test_a_registration_that_was_already_current_asks_for_no_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a rewrite is something an agent host has to be restarted to pick up.

    `agent-install` reports an entry that already named this launcher as
    skipped. It is in place and current, so the refresh succeeded; nothing
    changed in the file, so naming the agent in a restart sentence would send
    an operator to close a host for no reason."""
    _place_agent_skill("opencode")
    already = subprocess.CompletedProcess[str]([], 0, json.dumps({"ok": True, "steps": {"skill_install": {"ok": True}, "mcp_config": {"ok": True, "skipped": True}}}), "")
    _upgrade_with_refresh(monkeypatch, agent_install=already)

    result = upgrade_installation([])

    entry = result["refreshed"][0]
    assert entry["ok"] is True
    assert entry["registration"] is True
    assert entry["registration_rewritten"] is False
    assert "restart opencode" not in result["summary"]


@pytest.mark.parametrize(
    ("manager", "command", "expected"),
    [
        ("uv", ["uv.exe", "tool", "upgrade", "agentic-hil"], 'uv tool install "agentic-hil[can]@latest"'),
        ("pipx", ["pipx.exe", "upgrade", "agentic-hil"], 'pipx install --force "agentic-hil[can]"'),
        ("uv", ["uv.exe", "pip", "install", "--python", "PYTHON", "--upgrade", "agentic-hil[can]"], None),
        ("pip", ["PYTHON", "-m", "pip", "install", "--upgrade", "agentic-hil[can]"], None),
    ],
)
def test_the_command_that_clears_a_pin_exists_only_where_a_pin_can_be_recorded(
    manager: str,
    command: list[str],
    expected: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only `uv tool` and `pipx` reinstall from a requirement of their own.

    The `uv pip` and `pip` paths take the requirement from the command line this
    program builds, which never pins, so there is no pin for a command to clear
    and none is invented. A pin nobody can name a fix for is reported as the
    plain unchanged outcome, never as advice that does not exist.
    """
    from agentic_hil.upgrade import _unpinned_reinstall_command

    monkeypatch.setattr("agentic_hil.upgrade._installed_extras", lambda: ("can",))

    assert _unpinned_reinstall_command(manager, command) == expected


def test_upgrade_rejects_unknown_agent_before_mutating_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentic_hil.upgrade._run_upgrade_process", lambda *_args, **_kwargs: pytest.fail("must not run"))

    result = upgrade_installation(["unknown"])

    assert result["ok"] is False
    assert result["error_type"] == "unsupported_agent"


@pytest.mark.parametrize(
    ("prefix", "installer", "manager", "expected"),
    [
        ("C:/Users/op/AppData/Roaming/uv/tools/agentic-hil", "uv", "uv", ["uv.exe", "tool", "upgrade", "agentic-hil"]),
        ("C:/Users/op/.local/pipx/venvs/agentic-hil", "pip", "pipx", ["pipx.exe", "upgrade", "agentic-hil"]),
        ("C:/Users/op/venv", "uv", "uv", ["uv.exe", "pip", "install", "--python", "PYTHON", "--upgrade", "agentic-hil"]),
        ("C:/Python313", "pip", "pip", ["PYTHON", "-m", "pip", "install", "--upgrade", "agentic-hil"]),
    ],
)
def test_upgrade_selects_manager_owning_running_installation(
    prefix: str,
    installer: str,
    manager: str,
    expected: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentic_hil.upgrade import _upgrade_command

    monkeypatch.setattr(sys, "prefix", prefix)
    monkeypatch.setattr(sys, "executable", "PYTHON")
    monkeypatch.setattr("agentic_hil.upgrade._distribution_installer", lambda: installer)
    monkeypatch.setattr("agentic_hil.upgrade._installed_extras", tuple)
    monkeypatch.setattr("agentic_hil.upgrade.shutil.which", lambda name: f"{name}.exe")

    selected_manager, command = _upgrade_command()

    assert selected_manager == manager
    assert command == expected


@pytest.mark.parametrize(
    ("prefix", "installer", "expected"),
    [
        ("C:/Users/op/venv", "uv", ["uv.exe", "pip", "install", "--python", "PYTHON", "--upgrade", "agentic-hil[can,pyocd]"]),
        ("C:/Python313", "pip", ["PYTHON", "-m", "pip", "install", "--upgrade", "agentic-hil[can,pyocd]"]),
    ],
)
def test_upgrade_asks_for_the_extras_that_are_installed(
    prefix: str,
    installer: str,
    expected: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pip paths take the requirement from here, so it has to name the extras.

    Measured against a real installation: an upgrade requesting the bare
    distribution re-resolves without `can` or `pyocd`, so whatever the extra
    installed is never moved with the release that now depends on it. The uv
    tool and pipx paths need nothing here because both reinstall from a
    requirement their own receipt already records with its extras.
    """
    from agentic_hil.upgrade import _upgrade_command

    monkeypatch.setattr(sys, "prefix", prefix)
    monkeypatch.setattr(sys, "executable", "PYTHON")
    monkeypatch.setattr("agentic_hil.upgrade._distribution_installer", lambda: installer)
    monkeypatch.setattr("agentic_hil.upgrade._installed_extras", lambda: ("can", "pyocd"))
    monkeypatch.setattr("agentic_hil.upgrade.shutil.which", lambda name: f"{name}.exe")

    _manager, command = _upgrade_command()

    assert command == expected


def test_installed_extras_names_only_the_extras_whose_requirements_are_present() -> None:
    """Read against the real distribution metadata, not a fixture of it.

    `can` is a requirement of this suite, so it must be reported. `pyocd` is what
    tells the two apart on the canonical development environment, which does not
    install it — but an environment that does is legitimate, and is the only one
    where the pyOCD phrase tests run at all, so pinning its absence outright
    would make installing the extra fail the suite. What is pinned instead is
    that the answer tracks what is installed, asked here through the import
    system rather than through the metadata the function itself reads.
    """
    from importlib.util import find_spec

    from agentic_hil.upgrade import _installed_extras

    extras = _installed_extras()

    assert "can" in extras
    assert ("pyocd" in extras) is (find_spec("pyocd") is not None)


def _fake_process(pid: int, parent_pid: int, image: str, created_ns: int = 100) -> ProcessImage:
    return ProcessImage(pid=pid, parent_pid=parent_pid, image=image, created_ns=created_ns)


_TOOL_ENV = "C:/Users/op/AppData/Roaming/uv/tools/agentic-hil"
# The measured Windows shape of one `agentic-hil upgrade` run: the user-level
# shim starts a launcher inside the tool environment, and that launcher starts
# the interpreter whose image is the base Python outside it. The code runs in
# the last one, so the process holding `Scripts/python.exe` open is its parent.
_UPGRADE_ITSELF = (
    _fake_process(100, 1, "C:/Users/op/.local/bin/agentic-hil.exe", created_ns=10),
    _fake_process(200, 100, f"{_TOOL_ENV}/Scripts/python.exe", created_ns=20),
    _fake_process(300, 200, "C:/Users/op/AppData/Roaming/uv/python/cpython-3.13/python.exe", created_ns=30),
)


def _watch_installation(
    monkeypatch: pytest.MonkeyPatch,
    processes: tuple[ProcessImage, ...],
    *,
    prefix: str = _TOOL_ENV,
    executable: str | None = None,
) -> None:
    monkeypatch.setattr(sys, "prefix", prefix)
    monkeypatch.setattr(sys, "executable", executable or f"{prefix}/Scripts/python.exe")
    monkeypatch.setattr("agentic_hil.upgrade.os.getpid", lambda: 300)
    monkeypatch.setattr("agentic_hil.upgrade.snapshot_process_images", lambda: processes)


def test_the_upgrading_process_does_not_report_itself_as_holding_the_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the guard refuses every upgrade there is, including valid ones.

    `agentic-hil upgrade` runs out of the installation it replaces, and on
    Windows it reaches its own code through a launcher inside that environment.
    Excluding only the current pid would leave that launcher looking exactly
    like a running MCP server.
    """
    from agentic_hil.upgrade import _processes_holding_installation

    _watch_installation(monkeypatch, _UPGRADE_ITSELF)

    assert _processes_holding_installation() == []


def test_a_second_process_in_the_installation_is_reported_with_pid_and_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP server the agent host started is a sibling, never an ancestor."""
    from agentic_hil.upgrade import _processes_holding_installation

    server = _fake_process(400, 999, f"{_TOOL_ENV}/Scripts/python.exe", created_ns=5)
    _watch_installation(monkeypatch, (*_UPGRADE_ITSELF, server))

    holders = _processes_holding_installation()

    assert holders == [{"pid": 400, "image": f"{_TOOL_ENV}/Scripts/python.exe"}]


def test_a_reused_parent_pid_does_not_hide_a_process_holding_the_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pid is reused once its process exits, and the walk must not follow it.

    Here pid 200 is younger than the process that claims it as a parent, so it
    cannot be the launcher this run came through — it is a different Agentic HIL
    process that inherited the number, and hiding it is what would destroy the
    installation.
    """
    from agentic_hil.upgrade import _processes_holding_installation

    recycled = (
        _fake_process(100, 1, "C:/Users/op/.local/bin/agentic-hil.exe", created_ns=10),
        _fake_process(200, 100, f"{_TOOL_ENV}/Scripts/python.exe", created_ns=90),
        _fake_process(300, 200, "C:/Users/op/AppData/Roaming/uv/python/cpython-3.13/python.exe", created_ns=30),
    )
    _watch_installation(monkeypatch, recycled)

    assert _processes_holding_installation() == [{"pid": 200, "image": f"{_TOOL_ENV}/Scripts/python.exe"}]


def test_a_shared_prefix_counts_only_this_distributions_own_console_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `pip --user` or system prefix runs every other Python program too.

    Reading each of those as a holder would refuse an upgrade because something
    unrelated happens to be running, which is worse than the bug this guards.
    """
    from agentic_hil.upgrade import _processes_holding_installation

    # A shared prefix has a real shape per platform, and the console script only
    # sits where that platform puts it: `<prefix>/Scripts/agentic-hil.exe` beside
    # a Windows interpreter at the prefix root, `<prefix>/bin/agentic-hil` beside
    # a POSIX one already inside `bin`.
    shared, interpreter, script = (
        ("C:/Python313", "C:/Python313/python.exe", "C:/Python313/Scripts/agentic-hil.exe")
        if os.name == "nt"
        else ("/opt/python313", "/opt/python313/bin/python3", "/opt/python313/bin/agentic-hil")
    )
    unrelated = _fake_process(400, 999, interpreter, created_ns=5)
    ours = _fake_process(401, 999, script, created_ns=5)
    _watch_installation(monkeypatch, (*_UPGRADE_ITSELF, unrelated, ours), prefix=shared, executable=interpreter)

    assert _processes_holding_installation() == [{"pid": 401, "image": script}]


def test_nothing_is_reported_where_the_platform_replaces_a_running_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX unlinks the old file and lets the running process keep reading it.

    The upgrade completes and nothing is lost, so a refusal there would block a
    valid upgrade over a failure that platform does not have.
    """
    from agentic_hil.upgrade import _processes_holding_installation

    monkeypatch.setattr(sys, "prefix", _TOOL_ENV)
    monkeypatch.setattr("agentic_hil.upgrade.snapshot_process_images", lambda: None)

    assert _processes_holding_installation() == []


def test_upgrade_refuses_before_removing_anything_when_the_installation_is_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: no package manager runs, so both installations survive.

    Windows refuses to delete a file mapped as a running image, and uv removes
    the tool environment before it rebuilds it. Failing there leaves neither the
    old installation nor the new one, so the only safe move is to stop first.
    """
    holder = {"pid": 4242, "image": f"{_TOOL_ENV}/Scripts/python.exe"}
    monkeypatch.setattr("agentic_hil.upgrade._processes_holding_installation", lambda: [holder])
    monkeypatch.setattr("agentic_hil.upgrade._upgrade_command", lambda: pytest.fail("must not select a manager"))
    monkeypatch.setattr("agentic_hil.upgrade._run_upgrade_process", lambda *_args, **_kwargs: pytest.fail("must not run"))

    result = upgrade_installation(["opencode"])

    assert result["ok"] is False
    assert result["error_type"] == "installation_in_use"
    assert result["held_by"] == [holder]
    assert result["held_by_count"] == 1
    assert "Nothing was changed." in result["summary"]
    # The refusal has to say what to do, or it reads as an obstacle to clear —
    # and the wrong way through is the command that destroys the installation.
    assert any("Close the agent host" in step for step in result["remediation"])
    assert any("uv tool install --upgrade" in step for step in result["do_not"])


def test_rewriting_the_registration_block_survives_a_backslash_in_the_path() -> None:
    """The block names the skill's absolute path, which on Windows has backslashes.

    re.sub reads escapes in a replacement string, so rewriting a block that
    named C:\\Users\\... died on "bad escape \\U". Only the second write takes
    that branch, which is why skill-install followed by setup hit it.
    """
    from agentic_hil.cli import (
        AGENTIC_HIL_REGISTRATION_END,
        AGENTIC_HIL_REGISTRATION_START,
        upsert_marked_block,
    )

    target = Path.home() / ".codex" / "AGENTS.md"
    old = f"{AGENTIC_HIL_REGISTRATION_START}\nold\n{AGENTIC_HIL_REGISTRATION_END}"
    new = f"{AGENTIC_HIL_REGISTRATION_START}\nSee C:\\Users\\op\\.codex\\skills\\agentic-hil\n{AGENTIC_HIL_REGISTRATION_END}"
    assert upsert_marked_block(target, old)["updated"] is True

    assert upsert_marked_block(target, new)["updated"] is True

    assert "C:\\Users\\op\\.codex\\skills\\agentic-hil" in target.read_text(encoding="utf-8")
    assert "old" not in target.read_text(encoding="utf-8")


def test_setup_says_nothing_about_a_skill_when_it_left_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentic_hil import cli as cli_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _trusted_test_mcp_command(monkeypatch)
    real_register = cli_module.register_agent_mcp

    def write_then_fail(*args: object, **kwargs: object) -> dict:
        real_register(*args, **kwargs)
        return {"ok": False, "error_type": "injected_failure", "summary": "late MCP failure"}

    monkeypatch.setattr("agentic_hil.cli.register_agent_mcp", write_then_fail)

    result = setup_project(agent="codex")

    assert result["ok"] is False
    # Setup installed this skill itself, so rollback removed it and there is
    # nothing for the operator to clean up.
    assert not (Path.home() / ".codex" / "skills" / "agentic-hil" / "SKILL.md").exists()
    assert "left_behind" not in result


def test_setup_preserves_absolute_config_override_as_only_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _trusted_test_mcp_command(monkeypatch)
    override = Path.home() / "operator-policy" / "config.yaml"
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(override))
    assert init_config()["ok"] is True
    before = override.read_bytes()
    if os.name != "nt":
        override.chmod(0o400)

    result = setup_project(agent="opencode", force=True)

    assert result["ok"] is True, result
    assert result["steps"]["config"]["path"] == str(override)
    assert result["steps"]["config"]["skipped"] is True
    assert override.read_bytes() == before
    assert not project_config_path(workspace).exists()


def _claude_skill_path() -> Path:
    return Path.home() / ".claude" / "skills" / "agentic-hil" / "SKILL.md"


def _registered_claude_command() -> str | None:
    entry = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))["mcpServers"].get("agentic-hil")
    return None if entry is None else entry["command"]


def _default_state_root() -> Path:
    """The state root without creating it; user_state_root() would."""
    return Path(os.environ["LOCALAPPDATA" if os.name == "nt" else "XDG_STATE_HOME"]) / "agentic-hil"


def _refuse_the_config_location(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the profile that refuses the config location.

    There, `%APPDATA%` carries an app-capability ACE and the authoritative
    config's own location fails the ancestor trust check before anything is
    written. The ACL is not reproducible in a test on either platform; a
    location the config rules refuse outright is, and it refuses at the same
    point for the same reason — the location, not the content.
    """
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(workspace / "in-the-repo.yaml"))


def test_agent_install_needs_no_workspace_and_writes_nothing_project_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user-wide half is user state and nothing else.

    It has to run before any project exists, so it may not read a config, and it
    may not create one — nor the state root a config would have to name.
    """
    elsewhere = tmp_path / "not-a-project"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    command = _trusted_test_mcp_command(monkeypatch)

    result = install_agent(agent="claude-code")

    assert result["ok"] is True, result
    assert result["scope"] == "user"
    assert result["steps"]["skill_install"]["ok"] is True
    assert result["steps"]["mcp_config"]["ok"] is True
    assert _claude_skill_path().is_file()
    assert _registered_claude_command() == command
    assert not project_config_path(elsewhere).exists()
    assert not _default_state_root().exists()


@pytest.mark.parametrize("where", ["home", "filesystem-root"])
def test_agent_install_runs_where_the_install_line_is_typed(
    where: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half that writes only user-level files has to run from home.

    Its entire output lives under the home directory, and the check that keeps
    those files out of a repository read the working directory as the
    repository: from home, and from the filesystem root above it, every one of
    its own targets was "inside the workspace" and the command refused with
    `unsafe_configured_path` in the two places an install one-liner is actually
    typed. The one-liner's container proof had to `mkdir` a project directory to
    get past it. (#235)
    """
    home = Path.home()
    monkeypatch.chdir(home if where == "home" else Path(home.anchor))
    command = _trusted_test_mcp_command(monkeypatch)

    result = install_agent(agent="claude-code")

    assert result["ok"] is True, result
    assert result["scope"] == "user"
    assert result["steps"]["skill_install"]["ok"] is True
    assert result["steps"]["mcp_config"]["ok"] is True
    assert _claude_skill_path().is_file()
    assert _registered_claude_command() == command


def test_agent_install_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = tmp_path / "not-a-project"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    _trusted_test_mcp_command(monkeypatch)
    assert install_agent(agent="claude-code")["ok"] is True
    skill_before = _claude_skill_path().read_bytes()
    registration_before = (Path.home() / ".claude.json").read_bytes()

    again = install_agent(agent="claude-code")

    assert again["ok"] is True, again
    assert again["steps"]["skill_install"]["installed"] is False
    assert _claude_skill_path().read_bytes() == skill_before
    assert (Path.home() / ".claude.json").read_bytes() == registration_before


def test_agent_install_asks_for_a_restart_only_when_it_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The result is the one place the session that ran the command will look.

    A host reads its MCP registrations when the session starts, so the session
    that just wrote one waits on tools that cannot appear however long it waits.
    That has to arrive with `ok: true`, naming the agent, and it has to be
    absent when nothing was written: a second run that found its own entry
    already there gives nobody a reason to restart anything.
    """
    elsewhere = tmp_path / "not-a-project"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    _trusted_test_mcp_command(monkeypatch)

    first = install_agent(agent="claude-code")

    assert first["ok"] is True, first
    assert first["restart_required"] is True
    assert "Claude Code" in first["restart_notice"]
    # The CLI prints one JSON document and that is its human output, so the
    # sentence has to reach the line a person reads first.
    assert first["restart_notice"] in first["summary"]

    again = install_agent(agent="claude-code")

    assert again["ok"] is True, again
    assert again["steps"]["mcp_config"]["skipped"] is True
    assert again["restart_required"] is False
    assert "restart_notice" not in again
    assert "restart" not in again["summary"]


def test_the_user_half_hands_back_the_project_line_that_carries_the_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is where a split first run hands back to the operator.

    A bare `agentic-hil init` finishes the project and writes no rule, so the
    line this half offers has to name the agent it just installed.
    """
    elsewhere = tmp_path / "not-a-project"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    _trusted_test_mcp_command(monkeypatch)

    result = install_agent(agent="claude")

    assert result["ok"] is True, result
    assert "agentic-hil init --agent claude-code" in result["next_step"]


def test_init_project_is_idempotent_and_keeps_the_config_it_finds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _isolated_workspace(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    first = init_project(agent="claude-code")

    assert first["ok"] is True, first
    assert first["steps"]["config"]["ok"] is True
    assert first["steps"]["doctor"]["ok"] is True
    config_path = initialized_config_path(workspace)
    before = config_path.read_bytes()

    second = init_project(agent="claude-code")

    assert second["ok"] is True, second
    assert second["steps"]["config"]["skipped"] is True
    assert second["steps"]["agent_write_restriction"]["added"] == []
    assert config_path.read_bytes() == before


def _claude_deny_rules(home: Path) -> list[str]:
    """The deny list the host would evaluate, `[]` where there is no file yet.

    `_deny_rules` reads a settings file a test wrote itself. These tests start
    before anything has written one, so a missing file is an answer here rather
    than an error.
    """
    settings = home / ".claude" / "settings.json"
    if not settings.is_file():
        return []
    document = json.loads(settings.read_text(encoding="utf-8"))
    return document.get("permissions", {}).get("deny", [])


def _protected_trees(config_path: Path) -> list[Path]:
    return [config_path.parent, Path(load_config(str(config_path)).state_root)]


def _expected_deny_rules(config_path: Path) -> list[str]:
    return [f"Edit(/{_posix_filesystem_path(tree)}/**)" for tree in _protected_trees(config_path)]


def test_init_with_an_agent_hardens_a_config_an_earlier_plain_init_wrote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The completion path for a split first run.

    Where the host blocks the agent's own `setup`, what is left is a plain
    `init` by the agent and an operator-run `agent-install`, and from then on
    the config exists. Naming the agent afterwards has to add that agent's
    refusal of its own write tools without touching the config, or the hardening
    has no route left on this bench, nor on any bench whose config predates the
    rules.
    """
    workspace = _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    assert init_project()["ok"] is True
    config_path = initialized_config_path(workspace)
    before = config_path.read_bytes()
    assert _claude_deny_rules(home) == []

    result = init_project(agent="claude-code")

    assert result["ok"] is True, result
    # The config is not this run's to write, before or after.
    assert config_path.read_bytes() == before
    assert result["steps"]["config"]["ok"] is True
    assert result["steps"]["config"]["existing"] is True
    assert result["steps"]["config"]["skipped"] is True
    assert result["steps"]["doctor"]["ok"] is True
    restriction = result["steps"]["agent_write_restriction"]
    assert restriction["ok"] is True
    assert restriction["added"] == _expected_deny_rules(config_path)
    assert _claude_deny_rules(home) == _expected_deny_rules(config_path)


def test_a_second_init_with_an_agent_on_an_existing_config_changes_no_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    assert init_project()["ok"] is True
    assert init_project(agent="claude-code")["ok"] is True
    config_path = initialized_config_path(workspace)
    config_before = config_path.read_bytes()
    settings_before = (home / ".claude" / "settings.json").read_bytes()

    again = init_project(agent="claude-code")

    assert again["ok"] is True, again
    restriction = again["steps"]["agent_write_restriction"]
    assert (restriction["added"], restriction["removed"]) == ([], [])
    assert config_path.read_bytes() == config_before
    assert (home / ".claude" / "settings.json").read_bytes() == settings_before


def test_init_with_an_agent_on_an_existing_config_takes_back_the_inert_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bench whose config predates the rules is the second case #201 names.

    Its settings carry the forms releases up to 0.7.0 wrote for these same two
    trees: a `Write(...)` no host consults and an `Edit(...)` anchored under
    `~/.claude` instead of at the filesystem root. The repair route has to drop
    them and write the form the host evaluates, with the config still untouched.
    """
    workspace = _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    assert init_project()["ok"] is True
    config_path = initialized_config_path(workspace)
    config_directory, state_root = _protected_trees(config_path)
    superseded = _superseded_claude_rules(config_path, state_root)
    _claude_settings(home, ["Bash(curl *)", *superseded])
    before = config_path.read_bytes()

    result = init_project(agent="claude-code")

    assert result["ok"] is True, result
    assert config_path.read_bytes() == before
    restriction = result["steps"]["agent_write_restriction"]
    assert sorted(restriction["removed"]) == sorted(superseded)
    assert restriction["added"] == _expected_deny_rules(config_path)
    assert _claude_deny_rules(home) == ["Bash(curl *)", *_expected_deny_rules(config_path)]
    assert config_directory.is_dir()


def test_init_with_an_agent_takes_back_the_rule_for_a_state_root_the_operator_moved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rules follow the config, and the config is still not rewritten.

    Moving `state_root` is an operator's edit to their own file. The next
    `init --agent` reads it back, denies the tree the config names now, and takes
    back the rule for the tree it left.

    That second half is #206. Until then a rule could only be recognised as this
    tool's by the text of the current configuration, and #204 pinned the old
    state root's rule as one this command must not touch; the default state root
    it names sits inside a root this tool creates for itself, which identifies it
    as ours whatever the configuration says today.
    """
    workspace = _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    assert init_project(agent="claude-code")["ok"] is True
    config_path = initialized_config_path(workspace)
    first_state_root = Path(load_config(str(config_path)).state_root)
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    document["state_root"] = str(tmp_path / "moved-state")
    config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    before = config_path.read_bytes()

    result = init_project(agent="claude-code")

    assert result["ok"] is True, result
    assert config_path.read_bytes() == before
    moved = f"Edit(/{_posix_filesystem_path(tmp_path / 'moved-state')}/**)"
    left = f"Edit(/{_posix_filesystem_path(first_state_root)}/**)"
    restriction = result["steps"]["agent_write_restriction"]
    assert restriction["added"] == [moved]
    assert restriction["removed"] == [left]
    assert _claude_deny_rules(home) == _expected_deny_rules(config_path)
    # The tree the bench left keeps no denial of its own...
    assert left not in _claude_deny_rules(home)
    # ...and the configuration directory, which did not move, keeps its rule.
    assert f"Edit(/{_posix_filesystem_path(config_path.parent)}/**)" in _claude_deny_rules(home)


def test_a_second_projects_setup_leaves_the_first_projects_rules_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every project on this machine has a rule for its own config directory.

    They share one deny list and they are all this tool's own by the namespace
    argument, so a cleanup that read "ours and not this run's pair" as abandoned
    would quietly unprotect every other project the operator has set up. What
    decides is whether a configuration still names the tree, not which run is
    writing.
    """
    first = _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    assert init_project(agent="claude-code")["ok"] is True
    first_rules = _expected_deny_rules(initialized_config_path(first))
    second = tmp_path / "second-workspace"
    second.mkdir()
    monkeypatch.chdir(second)

    result = init_project(agent="claude-code")

    assert result["ok"] is True, result
    assert result["steps"]["agent_write_restriction"]["removed"] == []
    rules = _claude_deny_rules(home)
    assert all(rule in rules for rule in first_rules), rules
    assert all(rule in rules for rule in _expected_deny_rules(initialized_config_path(second))), rules


def test_init_without_an_agent_on_an_existing_config_writes_no_rule_and_names_the_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an agent there is no permission half to complete.

    The config is kept either way, so what is left to report is that nothing was
    written and which command does write the rules.
    """
    workspace = _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    assert init_project()["ok"] is True
    config_path = initialized_config_path(workspace)
    before = config_path.read_bytes()

    result = init_project()

    assert result["ok"] is True, result
    assert result["agent"] is None
    assert config_path.read_bytes() == before
    assert result["steps"]["config"]["existing"] is True
    restriction = result["steps"]["agent_write_restriction"]
    assert restriction["skipped"] is True
    assert "--agent" in restriction["summary"]
    assert "agentic-hil init --agent" in restriction["summary"]
    assert _claude_deny_rules(home) == []


def test_setup_completes_the_permission_half_on_a_config_it_did_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator's `setup` after a split first run is a complete path too.

    Its project half is `init`, so an existing config leaves it the same work,
    and the whole command has to report `ok` rather than a refusal for a file it
    was never going to replace.
    """
    workspace = _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    assert init_project()["ok"] is True
    config_path = initialized_config_path(workspace)
    before = config_path.read_bytes()

    result = setup_project(agent="claude-code")

    assert result["ok"] is True, result
    assert result["scopes"]["project"]["ok"] is True
    assert result["steps"]["config"]["existing"] is True
    assert config_path.read_bytes() == before
    assert result["steps"]["agent_write_restriction"]["added"] == _expected_deny_rules(config_path)
    assert _claude_deny_rules(home) == _expected_deny_rules(config_path)


def test_init_with_codex_on_an_existing_config_reports_the_agents_own_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _isolated_workspace(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    assert init_project()["ok"] is True
    config_path = initialized_config_path(workspace)
    before = config_path.read_bytes()

    result = init_project(agent="codex")

    assert result["ok"] is True, result
    assert config_path.read_bytes() == before
    assert result["steps"]["config"]["existing"] is True
    assert result["steps"]["agent_write_restriction"]["mode"] == "sandboxed-by-the-agent"


def test_init_with_opencode_on_an_existing_config_keeps_saying_it_writes_no_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    assert init_project()["ok"] is True
    config_path = initialized_config_path(workspace)
    before = config_path.read_bytes()

    result = init_project(agent="opencode")

    assert result["ok"] is True, result
    assert config_path.read_bytes() == before
    assert result["steps"]["config"]["existing"] is True
    restriction = result["steps"]["agent_write_restriction"]
    assert restriction["mode"] == "operator-managed"
    assert (restriction["added"], restriction["removed"]) == ([], [])
    assert "writes no write restriction for opencode" in restriction["summary"]
    assert not (home / ".config" / "opencode" / "opencode.json").exists()


def test_a_failed_permission_half_on_an_existing_config_leaves_that_config_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rollback set is the permission file and nothing else.

    The config was not written by this run, so a refusal in the half that
    follows can neither restore nor remove it, and the refusal the operator has
    to act on is reported unchanged.
    """
    workspace = _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    assert init_project()["ok"] is True
    config_path = initialized_config_path(workspace)
    before = config_path.read_bytes()
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_bytes(b"not json at all\n")

    result = init_project(agent="claude-code")

    assert result["ok"] is False
    assert result["steps"]["config"]["existing"] is True
    assert result["steps"]["agent_write_restriction"]["error_type"] == "agent_permissions_unreadable"
    assert result["rollback"]["ok"] is True
    assert config_path.read_bytes() == before
    assert settings.read_bytes() == b"not json at all\n"


def test_agent_install_completes_where_the_config_location_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused config location is not the skill's problem.

    Before the split both halves shared one transaction, so this refusal took
    the skill installation and the MCP registration with it and the operator was
    left with nothing. The user-wide half never reads or writes a config, so
    it has to finish here.
    """
    workspace = _isolated_workspace(tmp_path, monkeypatch)
    command = _trusted_test_mcp_command(monkeypatch)
    _refuse_the_config_location(workspace, monkeypatch)
    with pytest.raises(ConfigError) as refusal:
        init_project()
    assert refusal.value.error_type == "config_invalid"

    result = install_agent(agent="claude-code")

    assert result["ok"] is True, result
    assert _claude_skill_path().is_file()
    assert _registered_claude_command() == command


def test_setup_keeps_the_installed_agent_when_the_config_location_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composed command must not lose the half that succeeded.

    Reporting only the configuration refusal would tell the operator nothing
    about the agent that is now installed, which is exactly what they get to
    keep on such a profile.
    """
    workspace = _isolated_workspace(tmp_path, monkeypatch)
    command = _trusted_test_mcp_command(monkeypatch)
    _refuse_the_config_location(workspace, monkeypatch)

    result = setup_project(agent="claude-code")

    assert result["ok"] is False
    assert result["scopes"]["user"]["ok"] is True
    assert result["scopes"]["project"]["ok"] is False
    assert result["steps"]["skill_install"]["ok"] is True
    assert result["steps"]["mcp_config"]["ok"] is True
    assert result["steps"]["config"]["error_type"] == "config_invalid"
    assert result["rollback"]["ok"] is True
    assert "agentic-hil init" in result["next_step"]
    # The point of the split: the agent works even though the project does not.
    assert _claude_skill_path().is_file()
    assert _registered_claude_command() == command


def test_a_failed_project_half_leaves_the_user_wide_installation_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback stays inside its own half.

    opencode is the case that can go wrong: its MCP entry and its permission
    rules live in one file, so a project half that restored that file from a
    snapshot taken before the user half ran would delete the registration.
    Each half snapshots when it starts, so the project baseline already holds
    the user half's write.
    """
    from agentic_hil import cli as cli_module

    workspace = _isolated_workspace(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    assert install_agent(agent="opencode")["ok"] is True
    opencode_json = Path.home() / ".config" / "opencode" / "opencode.json"
    skill_path = Path.home() / ".config" / "opencode" / "skills" / "agentic-hil" / "SKILL.md"
    registered = json.loads(opencode_json.read_text(encoding="utf-8"))["mcp"]["agentic-hil"]
    real_restrict = cli_module.restrict_agent_write_access

    def write_then_fail(*args: object, **kwargs: object) -> dict:
        written = real_restrict(*args, **kwargs)
        assert written["ok"] is True
        return {"ok": False, "error_type": "injected_failure", "summary": "late restriction failure"}

    monkeypatch.setattr("agentic_hil.cli.restrict_agent_write_access", write_then_fail)

    result = init_project(agent="opencode")

    assert result["ok"] is False
    assert result["rollback"]["ok"] is True
    # The project half took back its own two writes...
    assert not initialized_config_path(workspace).exists()
    assert "permission" not in json.loads(opencode_json.read_text(encoding="utf-8"))
    # ...and neither of the user half's.
    assert json.loads(opencode_json.read_text(encoding="utf-8"))["mcp"]["agentic-hil"] == registered
    assert skill_path.is_file()


def test_a_failed_user_half_leaves_an_existing_project_config_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And the same in the other direction."""
    from agentic_hil import cli as cli_module

    workspace = _isolated_workspace(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    assert init_project()["ok"] is True
    config_path = initialized_config_path(workspace)
    before = config_path.read_bytes()
    real_register = cli_module.register_agent_mcp

    def write_then_fail(*args: object, **kwargs: object) -> dict:
        real_register(*args, **kwargs)
        return {"ok": False, "error_type": "injected_failure", "summary": "late MCP failure"}

    monkeypatch.setattr("agentic_hil.cli.register_agent_mcp", write_then_fail)

    result = install_agent(agent="claude-code")

    assert result["ok"] is False
    assert result["rollback"]["ok"] is True
    assert not _claude_skill_path().exists()
    assert config_path.read_bytes() == before


def test_one_agent_install_serves_every_project_of_this_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install once per user, bind once per project.

    The whole reason for the split: the second firmware repository must not
    reinstall the skill or re-register the MCP server.
    """
    outside = tmp_path / "not-a-project"
    outside.mkdir()
    monkeypatch.chdir(outside)
    _trusted_test_mcp_command(monkeypatch)
    assert install_agent(agent="claude-code")["ok"] is True
    user_files = {path: path.read_bytes() for path in (_claude_skill_path(), Path.home() / ".claude.json")}

    configs = []
    for name in ("firmware-a", "firmware-b"):
        workspace = tmp_path / name
        workspace.mkdir()
        monkeypatch.chdir(workspace)
        result = init_project()
        assert result["ok"] is True, result
        config_path = initialized_config_path(workspace)
        assert f"workspace_root: {json.dumps(str(workspace.resolve()))}" in config_path.read_text(encoding="utf-8")
        configs.append(config_path)

    assert configs[0] != configs[1]
    for path, content in user_files.items():
        assert path.read_bytes() == content, f"{path} was rewritten by a project step"


def test_setup_is_the_two_halves_and_says_which_is_which(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)

    result = setup_project(agent="claude-code")

    assert result["ok"] is True, result
    assert result["scopes"]["user"]["ok"] is True
    assert result["scopes"]["user"]["scope"] == "user"
    assert result["scopes"]["project"]["scope"] == "project"
    assert set(result["scopes"]["user"]["steps"]) == {"skill_install", "mcp_config"}
    assert set(result["scopes"]["project"]["steps"]) == {"config", "doctor", "agent_write_restriction"}
    # Each half rolls back only itself, so neither may hold the other's paths.
    assert result["rollback"]["attempted"] is False


def test_both_halves_are_reachable_from_the_command_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outside = tmp_path / "not-a-project"
    outside.mkdir()
    monkeypatch.chdir(outside)
    _trusted_test_mcp_command(monkeypatch)

    assert entrypoint(["agent-install", "--agent", "claude-code"]) == 0
    user_scope = json.loads(capsys.readouterr().out)
    assert user_scope["scope"] == "user"

    workspace = tmp_path / "firmware"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    assert entrypoint(["init", "--agent", "claude-code"]) == 0
    project = json.loads(capsys.readouterr().out)
    assert project["scope"] == "project"
    assert project["steps"]["doctor"]["ok"] is True
    assert initialized_config_path(workspace).is_file()


def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    del tmp_path, monkeypatch
    return Path.home()


def _isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Select a workspace that never contains the test interpreter.

    register_agent_mcp rejects an MCP command inside the current workspace. A
    repository-local ``.venv`` would otherwise fail these tests for that reason
    alone whenever pytest starts in the repository root.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    monkeypatch.chdir(workspace)
    return workspace


def _trusted_test_mcp_command(monkeypatch: pytest.MonkeyPatch) -> str:
    """A launcher the trust rule accepts, standing in for an installed one.

    Not `sys.executable`: on a CI runner the interpreter sits under a
    world-writable tool cache, which the rule rejects on purpose. Borrowing it
    made these tests pass on a developer machine and fail everywhere else.
    """
    command = str(trusted_launcher())
    monkeypatch.setattr("agentic_hil.cli.mcp_server_command", lambda: command)
    # Keep setup's permission smoothing from touching the real test venv launcher.
    monkeypatch.setattr("agentic_hil.cli._mcp_command_candidates", list)
    return command


def test_register_agent_mcp_codex_writes_user_config_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    command = _trusted_test_mcp_command(monkeypatch)
    result = register_agent_mcp("codex")
    assert result["ok"] is True
    toml = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.agentic-hil]" in toml
    assert f"command = {json.dumps(command)}" in toml
    assert 'args = ["mcp-stdio"]' in toml
    assert "enabled = true" in toml
    # idempotent
    assert register_agent_mcp("codex").get("skipped") is True


def test_register_agent_mcp_codex_preserves_existing_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    cfg = home / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[mcp_servers.other]\ncommand = "other"\n', encoding="utf-8")
    register_agent_mcp("codex")
    text = cfg.read_text(encoding="utf-8")
    assert "[mcp_servers.other]" in text
    assert "[mcp_servers.agentic-hil]" in text


def test_register_agent_mcp_opencode_writes_and_merges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    command = _trusted_test_mcp_command(monkeypatch)
    path = home / ".config" / "opencode" / "opencode.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcp": {"other": {"type": "local", "command": ["x"]}}}), encoding="utf-8")
    result = register_agent_mcp("opencode")
    assert result["ok"] is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "other" in data["mcp"]
    entry = data["mcp"]["agentic-hil"]
    assert entry["type"] == "local"
    assert entry["command"][0] == command
    assert entry["command"][-1] == "mcp-stdio"


def test_register_agent_mcp_claude_writes_user_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    command = _trusted_test_mcp_command(monkeypatch)
    monkeypatch.setattr("agentic_hil.upgrade.shutil.which", lambda name: None)
    result = register_agent_mcp("claude-code")
    assert result["ok"] is True
    assert result["method"] == "file"
    data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["agentic-hil"]["command"] == command
    assert data["mcpServers"]["agentic-hil"]["args"] == ["mcp-stdio"]
    assert not (tmp_path / ".mcp.json").exists()


def test_schema_exports_bundled_config_schema(tmp_path: Path) -> None:
    schema_path = tmp_path / "config.schema.json"
    result = schema(str(schema_path))
    assert result["ok"] is True
    assert "Agentic HIL configuration" in schema_path.read_text(encoding="utf-8")


def test_test_schema_exports_bundled_testconfig_schema(tmp_path: Path) -> None:
    schema_path = tmp_path / "testconfig.schema.json"
    result = test_schema(str(schema_path))
    assert result["ok"] is True
    assert "test reactor configuration" in schema_path.read_text(encoding="utf-8")

    denied = test_schema(str(schema_path))
    assert denied["ok"] is False
    assert denied["error_type"] == "schema_exists"

    assert test_schema(str(schema_path), force=True)["ok"] is True


def test_doctor_reports_the_command_registration_would_accept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Printing the bare name would name the one form registration refuses, so an
    # operator who copies what doctor prints would be sent to a dead end.
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, permissions={})
    monkeypatch.chdir(workspace)
    launcher = str(trusted_launcher())
    monkeypatch.setattr("agentic_hil.cli.mcp_server_command", lambda: launcher)

    report = doctor()["mcp"]

    assert report["command"] == launcher
    assert report["persistent"] is True
    assert report["args"] == ["mcp-stdio"]


def test_doctor_says_when_there_is_nothing_trustworthy_to_register(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, permissions={})
    monkeypatch.chdir(workspace)

    def no_trusted_executable() -> str:
        raise ConfigError("mcp_command_untrusted", "No stable trusted Agentic HIL executable was found.")

    monkeypatch.setattr("agentic_hil.cli.mcp_server_command", no_trusted_executable)

    result = doctor()

    assert result["mcp"]["command"] is None
    assert result["mcp"]["persistent"] is False
    assert "No stable trusted" in result["mcp"]["summary"]
    # A refusal that examined nothing lists nothing: the key is absent rather
    # than an empty list nobody can act on.
    assert "rejected_candidates" not in result["mcp"]
    # Reporting it is not the same as failing: an install may simply not have
    # happened yet, and doctor's verdict is about the hardware configuration.
    assert result["ok"] is True


def test_the_mcp_command_refusal_says_what_each_candidate_was_rejected_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """One path and one sentence per candidate was not enough to act on.

    The refusal that ends the search recommends a persistent installation, so
    the operator it reaches is the one who already has one and cannot use it.
    What the underlying refusal knew — which component stopped the walk, its
    mode and its owner — now travels with the candidate it condemned.
    """
    directory_refusal = ConfigError(
        "mcp_command_untrusted",
        "The MCP server executable has a parent directory that is owned by another user.",
        {"path": "/opt/shared/bin/agentic-hil", "directory": "/opt/shared/bin", "mode": "0755", "uid": 4242},
    )
    refusals: dict[str, Exception] = {
        "/opt/shared/bin/agentic-hil": directory_refusal,
        "/home/dev/venv/bin/agentic-hil": OSError("No such file or directory"),
    }

    def refuse(candidate: str) -> str:
        raise refusals[candidate]

    monkeypatch.setattr("agentic_hil.cli._mcp_command_candidates", lambda: list(refusals))
    monkeypatch.setattr("agentic_hil.cli._trusted_mcp_command", refuse)

    with pytest.raises(ConfigError) as refused:
        mcp_server_command()

    rejected = refused.value.details["rejected_candidates"]
    assert [entry["path"] for entry in rejected] == list(refusals)
    assert rejected[0]["directory"] == "/opt/shared/bin"
    assert rejected[0]["mode"] == "0755"
    assert rejected[0]["uid"] == 4242
    assert rejected[0]["error_type"] == "mcp_command_untrusted"
    assert "owned by another user" in rejected[0]["reason"]
    # An OSError carries no details and is still named, with its own message.
    assert "No such file" in rejected[1]["reason"]
    assert "mode" not in rejected[1]


def test_doctor_carries_the_rejected_candidates_and_not_only_the_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handing on a refusal without its reasons is what this fixes.

    `doctor` is where an operator looks when a registration will not happen, and
    it reported "no trusted persistent executable to register yet" over a
    refusal that knew the path, the mode and the owner of every candidate it had
    turned down.
    """
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, permissions={})
    monkeypatch.chdir(workspace)
    refusal = ConfigError(
        "mcp_command_untrusted",
        "The MCP server executable must be trusted, executable, and not writable by other users.",
        {"path": "/home/dev/.local/bin/agentic-hil", "mode": "0777", "uid": 1000},
    )

    def refuse(candidate: str) -> str:
        raise refusal

    monkeypatch.setattr("agentic_hil.cli._mcp_command_candidates", lambda: ["/home/dev/.local/bin/agentic-hil"])
    monkeypatch.setattr("agentic_hil.cli._trusted_mcp_command", refuse)

    report = doctor()["mcp"]

    assert report["command"] is None
    assert report["persistent"] is False
    assert report["rejected_candidates"] == [
        {
            "path": "/home/dev/.local/bin/agentic-hil",
            "reason": refusal.summary,
            "error_type": "mcp_command_untrusted",
            "mode": "0777",
            "uid": 1000,
        }
    ]
    assert json.dumps(report)


def test_doctor_reports_every_named_debugger_and_its_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_name="dut_a",
        debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n",
        permissions={},
    )
    monkeypatch.chdir(workspace)

    result = doctor()

    assert result["ok"] is True
    assert sorted(result["debuggers"]) == ["dut_a", "probe_b"]
    assert result["debuggers"]["probe_b"]["probe_id"] is not None
    # Two probes, so nothing is bound and every grant is still denied.
    assert all(entry["bound"] is False for entry in result["debuggers"].values())
    assert result["debuggers"]["dut_a"]["permissions"]["allow_flash"] is False


def test_mcp_config_writes_project_mcp_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    command = _trusted_test_mcp_command(monkeypatch)
    monkeypatch.chdir(tmp_path)
    output_path = tmp_path / ".mcp.json"
    result = mcp_config(str(output_path))
    assert result["ok"] is True
    content = json.loads(output_path.read_text(encoding="utf-8"))
    assert content["mcpServers"]["agentic-hil"]["command"] == command
    assert "mcp-stdio" in content["mcpServers"]["agentic-hil"]["args"]


def test_mcp_config_refuses_overwrite_without_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _trusted_test_mcp_command(monkeypatch)
    monkeypatch.chdir(tmp_path)
    output_path = tmp_path / ".mcp.json"
    output_path.write_text("{}", encoding="utf-8")
    result = mcp_config(str(output_path))
    assert result["ok"] is False
    assert result["error_type"] == "mcp_config_exists"
    result_forced = mcp_config(str(output_path), force=True)
    assert result_forced["ok"] is True


@pytest.mark.parametrize("alias_kind", ["hardlink", "symlink"])
def test_mcp_config_force_rejects_alias_without_changing_victim(
    alias_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_test_mcp_command(monkeypatch)
    monkeypatch.chdir(tmp_path)
    victim = tmp_path / "victim.json"
    output_path = tmp_path / ".mcp.json"
    existing = '{"keep": true}\n'
    victim.write_text(existing, encoding="utf-8")
    try:
        if alias_kind == "hardlink":
            os.link(victim, output_path)
        else:
            output_path.symlink_to(victim)
    except OSError as error:
        pytest.skip(f"{alias_kind} unavailable: {error}")

    with pytest.raises(ConfigError) as excinfo:
        mcp_config(str(output_path), force=True)

    assert excinfo.value.error_type == "unsafe_configured_path"
    assert victim.read_text(encoding="utf-8") == existing


@pytest.mark.parametrize(
    "existing",
    [
        '[mcp_servers."agentic-hil"]\ncommand = "custom"\n',
        '[mcp_servers]\n"agentic-hil" = { command = "custom" }\n',
        'mcp_servers = { "agentic-hil" = { command = "custom" } }\n',
    ],
    ids=["quoted-table", "parent-table", "top-level-inline-table"],
)
def test_register_agent_mcp_codex_reports_semantic_unmanaged_entry_conflict(
    existing: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(existing, encoding="utf-8")

    result = register_agent_mcp("codex")

    assert result["ok"] is False
    assert result["error_type"] == "mcp_config_conflict"
    assert path.read_text(encoding="utf-8") == existing


@pytest.mark.parametrize("agent", ["codex", "claude-code", "opencode"])
def test_a_foreign_mcp_entry_refusal_says_the_refusal_is_the_answer(
    agent: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal that only named the obstacle was read as a task to clear it.

    Measured: ten runs hand-edited the operator's entry to the path setup had
    reported, reran setup with --force, and reported success.
    """
    from evals.install.fixtures import agent_config_path, fixture_content

    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    path = agent_config_path(agent, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    seeded = fixture_content(agent, "unsafe-existing-config")
    assert seeded is not None
    path.write_text(seeded, encoding="utf-8")

    result = register_agent_mcp(agent, force=True)

    assert result["error_type"] == "mcp_config_conflict"
    assert path.read_text(encoding="utf-8") == seeded
    step = result["next_step"]
    assert "belongs to the operator" in step
    assert "--force does not apply" in step
    assert "Report it" in step
    # The wording that once taught a model to rewrite the file must not return.
    assert "ask the operator to change" not in step.lower()


def test_register_agent_mcp_codex_rejects_invalid_toml_without_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    existing = '[mcp_servers."agentic-hil"\ncommand = "custom"\n'
    path.write_text(existing, encoding="utf-8")

    result = register_agent_mcp("codex", force=True)

    assert result["ok"] is False
    assert result["error_type"] == "config_invalid"
    assert path.read_text(encoding="utf-8") == existing


def test_register_agent_mcp_codex_migrates_managed_workspace_command_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    command = _trusted_test_mcp_command(monkeypatch)
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    stale = str(workspace / "old-venv" / "agentic-hil")
    path.write_text(
        "# >>> agentic-hil mcp (managed) >>>\n"
        "[mcp_servers.agentic-hil]\n"
        f"command = {json.dumps(stale)}\n"
        'args = ["mcp-stdio"]\n'
        "enabled = true\n"
        "# <<< agentic-hil mcp (managed) <<<\n",
        encoding="utf-8",
    )

    result = register_agent_mcp("codex")

    assert result["ok"] is True, json.dumps(result)
    assert result["migrated"] is True
    assert f"command = {json.dumps(command)}" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("agent", "relative_path", "existing"),
    [
        ("claude-code", Path(".claude.json"), '{"mcpServers": {}, "mcpServers": {}}'),
        ("opencode", Path(".config/opencode/opencode.json"), '{"mcp": {"other": 1, "other": 2}}'),
    ],
)
def test_register_agent_mcp_rejects_duplicate_json_keys_without_changes(
    agent: str,
    relative_path: Path,
    existing: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    path = home / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing, encoding="utf-8")

    result = register_agent_mcp(agent, force=True)

    assert result["ok"] is False
    assert result["error_type"] == "config_invalid"
    assert path.read_text(encoding="utf-8") == existing


@pytest.mark.parametrize(
    ("agent", "relative_path", "existing"),
    [
        (
            "claude-code",
            Path(".claude.json"),
            '{"mcpServers": {"agentic-hil": {"command": "custom", "args": ["serve"], "env": {"TOKEN": "keep"}}}}',
        ),
        (
            "opencode",
            Path(".config/opencode/opencode.json"),
            '{"mcp": {"agentic-hil": {"type": "remote", "url": "https://example.invalid/mcp"}}}',
        ),
    ],
)
def test_register_agent_mcp_force_preserves_unmanaged_json_entry_and_reports_conflict(
    agent: str,
    relative_path: Path,
    existing: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    path = home / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing, encoding="utf-8")

    result = register_agent_mcp(agent, force=True)

    assert result["ok"] is False
    assert result["error_type"] == "mcp_config_conflict"
    assert path.read_text(encoding="utf-8") == existing


def _packaged_skill_text() -> str:
    skill = Path(__file__).resolve().parents[1] / "src" / "agentic_hil" / "skills"
    return (skill / "agentic-hil" / "SKILL.md").read_text(encoding="utf-8")


def _plugin_skill_text() -> str:
    plugin = Path(__file__).resolve().parents[1] / "plugins" / "agentic-hil" / "skills"
    return (plugin / "agentic-hil" / "SKILL.md").read_text(encoding="utf-8")


def _skill_description(text: str) -> str:
    """The frontmatter line a host decides to load this skill on."""
    frontmatter = text.split("---\n", 2)[1]
    return next(line for line in frontmatter.splitlines() if line.startswith("description:"))


def test_skill_routes_firmware_work_to_agentic_hil() -> None:
    text = _packaged_skill_text()

    for tool in ("flash_firmware", "reset_target", "probe_target", "com_read", "can_send", "debug_set_breakpoint"):
        assert tool in text, tool
    # The skill is worthless if it does not name what it replaces.
    for raw in ("openocd", "gdb", "minicom", "candump"):
        assert raw in text, raw


def test_the_skill_description_scopes_to_operating_the_bench() -> None:
    """The description is the routing decision, so it has to state its own limits.

    "Use for any embedded firmware or hardware request" was taken at its word:
    PCB layout, schematic capture and mechanical design are hardware requests,
    so hosts loaded the skill on turns where no tool here can contribute
    anything. The line therefore names what the bench does *and* says the
    negative out loud, and both installed copies say it, because an agent reads
    whichever one its host put there.
    """
    for name, text in (("packaged", _packaged_skill_text()), ("plugin", _plugin_skill_text())):
        description = _skill_description(text)
        for scope in ("connected target board", "flashing", "resetting", "probing", "debugging", "UART", "CAN", "test reports"):
            assert scope in description, f"{name} description does not name {scope}"
        for outside in ("PCB layout", "schematic capture", "EDA", "mechanical design", "never touches a board"):
            assert outside in description, f"{name} description does not exclude {outside}"
        # Rescoping must not drop what the skill exists to displace.
        assert "instead of invoking a debugger, serial device, or CAN adapter directly" in description, name
        # And the body repeats the boundary for a session routed here anyway.
        assert "The gate is the whole of the scope." in text, name



def test_the_codex_registration_block_carries_the_same_boundary() -> None:
    """The block written into a host's AGENTS.md routes like the skill does.

    The over-broad "any firmware or hardware request" wording sent
    hardware-design sessions here; the registration block states the same
    positive scope and the same negative as the skill description."""
    from agentic_hil.cli import codex_registration_block

    block = codex_registration_block("skills/agentic-hil/SKILL.md", "0.0.0", "codex")
    assert "operates the connected target board through the configured bench" in block
    assert "Not for designing hardware" in block
    assert "PCB layout" in block
    assert "never touches a board" in block
    assert "any firmware or hardware request" not in block

def test_skill_frontmatter_survives_a_windows_checkout() -> None:
    # git converts the packaged skill to CRLF on Windows unless .gitattributes
    # pins it; refusing to recognise it there would report this project's own
    # skill as a foreign one and stop setup.
    crlf = _packaged_skill_text().replace("\n", "\r\n")

    assert is_agentic_hil_setup_skill(crlf)
    # Not against `__version__`: that is the distribution version, which runs
    # ahead of the release for a whole cycle, while a skill states the release
    # it belongs to. Pinned as not-None first, because comparing two computed
    # values passes when both are None.
    stated = skill_version(_packaged_skill_text())
    assert stated is not None
    assert skill_version(crlf) == stated


def test_plugin_skill_carries_the_packaged_guidance() -> None:
    # A plugin install is an alternative to setup, not a lesser one: it has to
    # deliver the same routing rules, so the bodies may not drift apart.
    packaged = _packaged_skill_text()
    plugin = _plugin_skill_text()
    body = packaged.split("---\n", 2)[2].strip()

    assert body in plugin
    # What only the plugin can say: it registers no server command of its own.
    assert 'uv tool install --upgrade "agentic-hil==' in plugin
    assert "agentic-hil setup --agent claude" in plugin


def test_setup_asks_the_agent_to_refuse_writing_the_policy_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installation and use need opposite things.

    setup has to create the authoritative config; afterwards nothing but the
    operator may change it. A small model rewrote it with a yaml script to grant
    itself allow_flash, so the restriction is written last — and it constrains
    the agent's own tools, not a path, because a path rule would also stop
    `agentic-hil mcp-stdio` from reading the file it protects.
    """
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)

    result = setup_project(agent="claude-code")

    assert result["ok"] is True
    restriction = result["steps"]["agent_write_restriction"]
    assert restriction["ok"] is True
    deny = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]["deny"]
    config_directory = Path(result["steps"]["config"]["path"]).parent
    assert f"Edit(/{_posix_filesystem_path(config_directory)}/**)" in deny
    # Running the CLI is untouched: setup, doctor and a rerun must keep working.
    assert not any(rule.startswith("Bash(") for rule in deny)


def test_the_write_restriction_keeps_what_the_operator_wrote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_bytes(b'{"permissions": {"deny": ["Bash(curl *)"], "allow": ["Read(~/notes)"]}}\n')

    restriction = restrict_agent_write_access("claude-code", tmp_path / "cfg" / "config.yaml", tmp_path / "state")

    assert restriction["ok"] is True
    document = json.loads(settings.read_text(encoding="utf-8"))
    assert "Bash(curl *)" in document["permissions"]["deny"]
    assert document["permissions"]["allow"] == ["Read(~/notes)"]
    # Idempotent: a second setup must not append the same rules again.
    before = settings.read_text(encoding="utf-8")
    assert restrict_agent_write_access("claude-code", tmp_path / "cfg" / "config.yaml", tmp_path / "state")["added"] == []
    assert settings.read_text(encoding="utf-8") == before


def test_the_write_restriction_is_left_to_the_sandbox_for_codex(tmp_path: Path) -> None:
    # Codex sandboxes model-generated shell commands and leaves MCP servers
    # outside it, so its own default already refuses this write.
    restriction = restrict_agent_write_access("codex", tmp_path / "cfg" / "config.yaml", tmp_path / "state")

    assert restriction["ok"] is True
    assert restriction["mode"] == "sandboxed-by-the-agent"


def _claude_settings(home: Path, deny: list[str]) -> Path:
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"permissions": {"deny": deny}}) + "\n", encoding="utf-8")
    return settings


def _deny_rules(settings: Path) -> list[str]:
    return json.loads(settings.read_text(encoding="utf-8"))["permissions"]["deny"]


def _superseded_claude_rules(config_path: Path, state_root: Path) -> list[str]:
    """Exactly what releases up to 0.7.0 wrote into ~/.claude/settings.json."""
    return [f"{form}({directory.as_posix()}/**)" for directory in (config_path.parent, state_root) for form in ("Edit", "Write")]


def test_the_claude_deny_rules_use_only_the_form_that_host_evaluates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the shape, because nothing else does.

    https://code.claude.com/docs/en/permissions: Claude Code "checks file
    permissions against `Edit(path)` and `Read(path)` rules only", a `Write(...)`
    path rule is "accepted but never consulted" and warned about at every start,
    and a single leading slash "anchors at the settings source, not the
    filesystem root" — which in user settings is `~/.claude`, not `/`.

    Both halves were written from expectation once already, so
    the absence of the `Write` form is asserted explicitly rather than implied.
    """
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    config_path, state_root = tmp_path / "cfg" / "config.yaml", tmp_path / "state"
    settings = _claude_settings(home, [])

    assert restrict_agent_write_access("claude-code", config_path, state_root)["ok"] is True

    deny = _deny_rules(settings)
    assert deny == [f"Edit(/{_posix_filesystem_path(directory)}/**)" for directory in (config_path.parent, state_root)]
    # Anchored at the filesystem root, so it means the path it names...
    assert all(rule.startswith("Edit(//") for rule in deny), deny
    # ...and no inert twin comes along with it, in any spelling.
    assert not any(rule.startswith("Write(") for rule in deny), deny


def test_a_deny_pattern_names_a_filesystem_path_on_either_platform() -> None:
    """Both branches, on whichever runner this lands on.

    The defect was reported on Debian and the fix is written on Windows, so the
    platform-dependent half of this would otherwise only ever run on one of them.
    `C:\\Users\\alice` matches as `/c/Users/alice`, per
    https://code.claude.com/docs/en/permissions.
    """
    assert _posix_filesystem_path(PureWindowsPath(r"C:\Users\alice\.agentic-hil")) == "/c/Users/alice/.agentic-hil"
    assert _posix_filesystem_path(PurePosixPath("/home/hpauli/.config/agentic-hil")) == "/home/hpauli/.config/agentic-hil"
    # A colon deeper in a POSIX path is not a drive letter.
    assert _posix_filesystem_path(PurePosixPath("/home/h/a:b/state")) == "/home/h/a:b/state"


def test_a_repeat_setup_takes_back_the_inert_rules_an_earlier_release_wrote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.7.0 left a warning behind that no later start clears by itself.

    Anyone who ran that setup carries the rules in ~/.claude/settings.json, so
    removal has to happen here or the yellow warning is permanent.
    """
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    config_path, state_root = tmp_path / "cfg" / "config.yaml", tmp_path / "state"
    superseded = _superseded_claude_rules(config_path, state_root)
    settings = _claude_settings(home, ["Bash(curl *)", *superseded])

    restriction = restrict_agent_write_access("claude-code", config_path, state_root)

    assert restriction["ok"] is True
    assert sorted(restriction["removed"]) == sorted(superseded)
    deny = _deny_rules(settings)
    assert not any(rule in superseded for rule in deny), deny
    assert deny == ["Bash(curl *)", *[f"Edit(/{_posix_filesystem_path(directory)}/**)" for directory in (config_path.parent, state_root)]]


def test_the_deny_rule_cleanup_leaves_an_operators_own_write_rule_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ours is identified by its exact text, never by its shape.

    A `Write(...)` rule of the operator's is inert for the same reason ours was,
    but it is not this tool's to delete — including one that covers a tree of
    ours from further up.
    """
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    config_path, state_root = tmp_path / "cfg" / "config.yaml", tmp_path / "state"
    theirs = [
        "Write(/etc/**)",
        f"Write({tmp_path.as_posix()}/**)",
        f"Write({config_path.parent.as_posix()}/*.yaml)",
        f"Edit({state_root.as_posix()})",
    ]
    settings = _claude_settings(home, theirs)

    restriction = restrict_agent_write_access("claude-code", config_path, state_root)

    assert restriction["ok"] is True
    assert restriction["removed"] == []
    assert _deny_rules(settings)[: len(theirs)] == theirs


def _tool_state_namespace() -> Path:
    """The state root this tool creates for itself on this profile.

    Read off the environment the suite's own isolation sets, which is where
    `user_state_root` reads it from as well; asking that function instead would
    create the directory, and these tests are about trees that are gone.
    """
    return Path(os.environ["LOCALAPPDATA" if os.name == "nt" else "XDG_STATE_HOME"]) / "agentic-hil"


def test_a_refresh_takes_back_the_rule_for_a_tree_no_configuration_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#206: an abandoned tree keeps no denial, in either spelling.

    Both state roots here are inside the root this tool creates for itself under
    `%LOCALAPPDATA%`/`$XDG_STATE_HOME`, and that is the whole of the provenance:
    nothing but Agentic HIL puts a directory there, so a rule about one is this
    tool's own however long ago it was written and whatever the configuration
    named at the time. The reported case was an uninstall that left two rules
    denying writes on directories that no longer existed, removable only by hand.
    """
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    abandoned = _tool_state_namespace() / "state-of-a-bench-that-is-gone"
    config_path, state_root = tmp_path / "cfg" / "config.yaml", _tool_state_namespace() / "state"
    # The `//`-anchored rule this release writes, and the plain one 0.7.0 wrote.
    left_behind = [f"Edit(/{_posix_filesystem_path(abandoned)}/**)", f"Write({abandoned.as_posix()}/**)"]
    settings = _claude_settings(home, ["Bash(curl *)", *left_behind])

    restriction = restrict_agent_write_access("claude-code", config_path, state_root)

    assert restriction["ok"] is True
    assert restriction["removed"] == left_behind
    assert _deny_rules(settings) == ["Bash(curl *)", *[f"Edit(/{_posix_filesystem_path(tree)}/**)" for tree in (config_path.parent, state_root)]]
    assert "no longer needs were dropped" in restriction["summary"]


def test_the_deny_rule_cleanup_claims_nothing_outside_this_tools_own_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The namespace is the claim, and it stops at its own edge.

    A checkout that happens to be called `agentic-hil`, a directory whose name
    merely starts with the root's, and a rule about one of the tool's own trees
    in a shape the tool has never written are the operator's, and come back
    exactly as they were written.
    """
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    namespace = _tool_state_namespace()
    config_path, state_root = tmp_path / "cfg" / "config.yaml", namespace / "state"
    theirs = [
        "Bash(curl *)",
        f"Edit(/{_posix_filesystem_path(Path.home() / 'work' / 'agentic-hil')}/**)",
        f"Edit(/{_posix_filesystem_path(namespace.parent / 'agentic-hil-backup')}/**)",
        f"Edit(/{_posix_filesystem_path(namespace / 'reports')})",
        f"Write(/{_posix_filesystem_path(namespace)}/reports/*.json)",
    ]
    settings = _claude_settings(home, theirs)

    restriction = restrict_agent_write_access("claude-code", config_path, state_root)

    assert restriction["ok"] is True
    assert restriction["removed"] == []
    assert _deny_rules(settings)[: len(theirs)] == theirs


def test_the_deny_rule_cleanup_leaves_an_entry_that_is_not_a_rule_where_it_found_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`~/.claude/settings.json` belongs to another program.

    A deny entry that is not a string at all, and a pattern that navigates back
    out of the tree it starts in, are both things this tool has never written.
    Reading the first as one of ours would be a crash on somebody's hand-edited
    file, and the second a claim on a tree outside the namespace that justifies
    the claim.
    """
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    namespace = _tool_state_namespace()
    config_path, state_root = tmp_path / "cfg" / "config.yaml", namespace / "state"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    theirs = [{"tool": "Edit"}, f"Edit(/{_posix_filesystem_path(namespace)}/../elsewhere/**)"]
    settings.write_text(json.dumps({"permissions": {"deny": theirs}}) + "\n", encoding="utf-8")

    restriction = restrict_agent_write_access("claude-code", config_path, state_root)

    assert restriction["ok"] is True
    assert restriction["removed"] == []
    assert _deny_rules(settings)[: len(theirs)] == theirs


def test_a_project_configuration_that_will_not_read_costs_no_other_project_its_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown is not abandoned.

    Which trees are still wanted is read out of the configurations this user has,
    so one that cannot be read leaves the answer incomplete, and an incomplete
    answer takes nothing back rather than guessing.
    """
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    namespace = _tool_state_namespace()
    unreadable = project_config_directory() / "other-project-0123456789"
    unreadable.mkdir(parents=True)
    (unreadable / "config.yaml").write_text("state_root: [not a path\n", encoding="utf-8")
    config_path, state_root = tmp_path / "cfg" / "config.yaml", namespace / "state"
    abandoned = f"Edit(/{_posix_filesystem_path(namespace / 'state-of-a-bench-that-is-gone')}/**)"
    settings = _claude_settings(home, [abandoned])

    restriction = restrict_agent_write_access("claude-code", config_path, state_root)

    assert restriction["ok"] is True
    assert restriction["removed"] == []
    assert abandoned in _deny_rules(settings)


def test_the_deny_rule_cleanup_settles_after_one_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    config_path, state_root = tmp_path / "cfg" / "config.yaml", tmp_path / "state"
    settings = _claude_settings(home, _superseded_claude_rules(config_path, state_root))

    assert restrict_agent_write_access("claude-code", config_path, state_root)["ok"] is True
    migrated = settings.read_text(encoding="utf-8")
    second = restrict_agent_write_access("claude-code", config_path, state_root)

    assert (second["added"], second["removed"]) == ([], [])
    assert settings.read_text(encoding="utf-8") == migrated


def _opencode_wildcard_match(subject: str, pattern: str, *, windows: bool = False) -> bool:
    """`Wildcard.match` from opencode's `packages/core/src/util/wildcard.ts`.

    Ported rather than paraphrased, so what these tests pin is the matcher the
    host runs. `si` on win32, `s` elsewhere, and `*` -> `.*` crosses `/` because
    `.` takes the separator like any other character.
    """
    escaped = re.sub(r"[.+^${}()|\[\]\\]", r"\\\g<0>", pattern.replace("\\", "/"))
    escaped = escaped.replace("*", ".*").replace("?", ".")
    if escaped.endswith(" .*"):
        escaped = escaped[:-3] + "( .*)?"
    flags = re.DOTALL | (re.IGNORECASE if windows else 0)
    return re.fullmatch(escaped, subject.replace("\\", "/"), flags) is not None


def _opencode_ruleset(document: dict) -> list[tuple[str, str, str]]:
    """`fromConfig` from `packages/opencode/src/permission/index.ts`.

    Its `~/` and `$HOME/` expansion is left out: it only fires on a pattern that
    starts with either, and none written here does.
    """
    ruleset: list[tuple[str, str, str]] = []
    for key, value in document.get("permission", {}).items():
        if isinstance(value, str):
            ruleset.append((key, "*", value))
            continue
        ruleset.extend((key, pattern, action) for pattern, action in value.items())
    return ruleset


def _opencode_evaluate(permission: str, subject: str, ruleset: list[tuple[str, str, str]], *, windows: bool = False) -> str:
    """`evaluate` from the same file: `findLast`, hence the reverse walk, and
    `ask` when nothing matches."""
    for rule_permission, rule_pattern, action in reversed(ruleset):
        if _opencode_wildcard_match(permission, rule_permission, windows=windows) and _opencode_wildcard_match(subject, rule_pattern, windows=windows):
            return action
    return "ask"


def _opencode_edit_subject(worktree: str, filepath: str) -> str:
    """What `write.ts`, `edit.ts` and `apply_patch.ts` ask with:
    `path.relative(instance.worktree, filepath)`.

    Held to POSIX semantics whatever the runner is, so the Debian layout the
    defect was reported on is exercised on both. The one Windows departure is
    modelled where it matters, in the cross-drive case below: Node hands the
    target back unchanged when there is no relative route to it.
    """
    return posixpath.relpath(filepath, worktree)


def _opencode_permission_file(home: Path) -> Path:
    path = home / ".config" / "opencode" / "opencode.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


_OPENCODE_HOME = PurePosixPath("/home/hpauli")
_OPENCODE_CONFIG_DIR = _OPENCODE_HOME / ".config" / "agentic-hil" / "projects" / "stepper_module_s_3"


def test_setup_writes_no_opencode_restriction_and_says_so(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator's call, and the report has to read like one.

    On opencode a single "always" answer adds a session `allow` for pattern `*`
    that outranks anything written here, so a rule from setup would be a lock one
    click removes. Agentic HIL writes none, leaves the file untouched, and names
    the file the operator sets this in — a silent no-op would read as protection.
    """
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    config_path, state_root = tmp_path / "cfg" / "config.yaml", tmp_path / "state"
    opencode_json = _opencode_permission_file(home)
    opencode_json.write_text(json.dumps({"permission": {"edit": {"src/**": "allow"}}}) + "\n", encoding="utf-8")
    before = opencode_json.read_text(encoding="utf-8")

    restriction = restrict_agent_write_access("opencode", config_path, state_root)

    assert restriction["ok"] is True
    assert restriction["mode"] == "operator-managed"
    assert (restriction["added"], restriction["removed"]) == ([], [])
    assert "opencode.json" in restriction["summary"]
    assert opencode_json.read_text(encoding="utf-8") == before


def test_a_repeat_setup_takes_back_the_inert_opencode_patterns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What earlier releases wrote has to go, now that nothing replaces it.

    Left alone those patterns read as protection and are none — the silent half
    of the same defect on a second host. An operator's own pattern over one of these
    trees is not ours to drop, and neither is one of ours they have since set to
    something other than `deny`.
    """
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    config_path, state_root = tmp_path / "cfg" / "config.yaml", tmp_path / "state"
    ours = f"{config_path.parent.as_posix()}/**"
    initial = {
        ours: "deny",  # ours, inert, and the only thing this may touch
        f"{state_root.as_posix()}/**": "ask",  # ours once; theirs now, at their action
        f"{config_path.parent.as_posix()}/*.yaml": "deny",  # theirs, over a tree of ours
        "src/**": "allow",  # theirs
    }
    opencode_json = _opencode_permission_file(home)
    opencode_json.write_text(json.dumps({"permission": {"edit": initial}}) + "\n", encoding="utf-8")

    restriction = restrict_agent_write_access("opencode", config_path, state_root)

    assert restriction["ok"] is True
    assert restriction["added"] == []
    assert restriction["removed"] == [ours]
    rules = json.loads(opencode_json.read_text(encoding="utf-8"))["permission"]["edit"]
    # Everything else survives, at its action and in the operator's own order,
    # and nothing of ours takes the place of what went.
    assert list(rules.items()) == [(pattern, action) for pattern, action in initial.items() if pattern != ours]


def test_the_opencode_cleanup_settles_after_one_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    config_path, state_root = tmp_path / "cfg" / "config.yaml", tmp_path / "state"
    opencode_json = _opencode_permission_file(home)
    opencode_json.write_text(json.dumps({"permission": {"edit": {f"{state_root.as_posix()}/**": "deny"}}}) + "\n", encoding="utf-8")

    assert restrict_agent_write_access("opencode", config_path, state_root)["ok"] is True
    migrated = opencode_json.read_text(encoding="utf-8")
    second = restrict_agent_write_access("opencode", config_path, state_root)

    assert (second["added"], second["removed"]) == ([], [])
    assert opencode_json.read_text(encoding="utf-8") == migrated


def test_the_absolute_opencode_pattern_matched_nothing() -> None:
    """Why those patterns are removed rather than corrected.

    opencode matches `permission.edit` against the worktree-relative path, so the
    absolute form earlier releases wrote could not match anything. Its own
    matcher and rule evaluation are ported above and fed the subject the host
    really produces: the answer is `ask`, from a rule written as `deny`.
    """
    stale = [("edit", f"{_OPENCODE_CONFIG_DIR}/**", "deny")]
    subject = _opencode_edit_subject(str(_OPENCODE_HOME / "work" / "firmware"), str(_OPENCODE_CONFIG_DIR / "config.yaml"))

    assert subject.startswith("../"), subject
    assert _opencode_evaluate("edit", subject, stale) == "ask"
    # And it is the relativisation that does it, not how the pattern is spelled.
    assert _opencode_evaluate("edit", str(_OPENCODE_CONFIG_DIR / "config.yaml"), stale) == "deny"


def test_initialize_carries_the_one_thing_said_before_the_agent_decides() -> None:
    """A refusal and a skill both arrive after the first decision.

    Measured: a small model ran st-flash before calling a single tool here, so
    nothing this server says at call time could have reached it. initialize is
    the only moment that precedes the decision.
    """
    response = handle_mcp_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": MCP_PROTOCOL_VERSION}},
        AgenticHILToolService(load_config(str(write_config(Path.cwd())))),
    )
    instructions = response["result"]["instructions"]

    for raw in ("openocd", "pyocd", "st-flash", "candump", "Makefile", "/dev/tty*"):
        assert raw in instructions, raw
    assert "permission_denied" in instructions
    assert "before reaching for a shell" in instructions
    # The observed worst case: a model that granted itself the permission.
    assert "Never edit the authoritative configuration" in instructions


def test_a_refusal_says_what_to_do_and_what_not_to_do() -> None:
    """A refusal that only states a fact reads like a door to walk around.

    A weak model called flash_firmware first, was refused, diagnosed correctly
    through the other tools, and then flashed the board with st-flash. The
    instruction has to travel with the refusal, where the caller is looking.
    """
    from agentic_hil.tools import tool_error

    denied = tool_error("flash_firmware", "permission_denied", "Flashing is disabled by the authoritative config.")

    assert "only the operator may edit it" in denied["next_step"]
    assert "must not carry out the action another way" in denied["next_step"]
    # No verb phrase a caller can act on: an earlier wording said "ask the
    # operator to change the authoritative config" and a small model rewrote the
    # config itself to grant allow_flash.
    assert "change the authoritative config" not in denied["next_step"]
    # Only refusals carry it; an ordinary error must not grow advice it cannot honour.
    assert "next_step" not in tool_error("flash_firmware", "artifact_validation_failed", "bad image")


def test_gateway_tool_descriptions_name_what_they_replace() -> None:
    # A tool description is the only routing hint every session sees; a skill
    # has to be discovered and loaded first.
    replaced = {
        "debugger_info": "openocd",
        "debugger_probes_list": "st-info",
        "probe_target": "openocd",
        "flash_firmware": "st-flash",
        "reset_target": "st-util",
        "com_ports_list": "picocom",
        "com_read": "minicom",
        "can_buses_list": "candump",
        "can_send": "cansend",
        "can_read": "candump",
    }
    descriptions = {str(tool["name"]): str(tool["description"]) for tool in MCP_TOOLS}

    for name, raw_command in replaced.items():
        assert raw_command in descriptions[name], name


SKILL_TOOL_COLUMN = "The tools"


def _table_column(text: str, header: str) -> list[str]:
    """Every filled cell under a named column, in document order."""
    cells_under: list[str] = []
    column: int | None = None
    for line in text.splitlines():
        row = line.strip()
        if not row.startswith("|"):
            column = None  # A table ends where its rows do; the next one names its own column.
            continue
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if column is None:
            if header in cells:
                column = cells.index(header)
            continue
        if column < len(cells) and set(cells[column]) - set("-: "):
            cells_under.append(cells[column])
    return cells_under


def _skill_routing_table(text: str) -> list[str]:
    """The tools the skill declares, read out of the routing table's tool column.

    The skill names tools in one defined place — that column — and names error
    types, result fields, permissions and configuration sections in prose, in
    the same backticks, because that is how all of them are written. A scan of
    the whole document cannot tell the two apart, so it took every underscored
    identifier for a tool and every documented field had to be excluded by
    hand before the suite went green. That list grew with the documentation,
    and on one merge two branches extended it independently.
    Reading the column reads the document's own structure instead of guessing
    at its prose.
    """
    return [name for cell in _table_column(text, SKILL_TOOL_COLUMN) for name in re.findall(r"`([a-z][a-z0-9_]*)`", cell)]


def test_skill_only_names_tools_the_server_exposes() -> None:
    """The routing table is the skill's tool inventory, and it has to hold the contract.

    Checked in both directions on purpose. Prose is out of scope by
    construction, and that costs no coverage only while the table names every
    tool there is — so a tool this server gains without a row here fails as
    loudly as a row naming one it does not serve.
    """
    contract = Path(__file__).resolve().parents[1] / "evals" / "install" / "tools.list.expected"
    exposed = set(contract.read_text(encoding="utf-8").split())
    declared = _skill_routing_table(_packaged_skill_text())

    assert declared, f"the skill has no `{SKILL_TOOL_COLUMN}` column to read"
    assert len(set(declared)) == len(declared), "the routing table names a tool twice"
    assert set(declared) == exposed, (
        f"routed but not exposed: {sorted(set(declared) - exposed)}; "
        f"exposed but not routed: {sorted(exposed - set(declared))}"
    )


def test_the_skill_tool_check_reads_the_table_and_not_the_prose() -> None:
    """Both directions of the routing-table contract, against the shipped document.

    Prose is out of scope whatever an identifier there is called — a result
    field, and equally a name shaped like a tool, which is the deliberate half
    of the trade the test above pays for by holding the whole table. A name in
    the tool column is caught even when it reads like one of ours.
    """
    contract = Path(__file__).resolve().parents[1] / "evals" / "install" / "tools.list.expected"
    exposed = set(contract.read_text(encoding="utf-8").split())
    text = _packaged_skill_text()
    declared = _skill_routing_table(text)

    documented = text + "\nA refusal carries `some_new_result_field`, and `reset_target_hard` is prose.\n"

    assert declared
    assert _skill_routing_table(documented) == declared

    lines = text.splitlines()
    lines.insert(
        max(index for index, line in enumerate(lines) if line.startswith("|")) + 1,
        "| A request no tool serves | `flash_firmware_v2` |",
    )

    assert set(_skill_routing_table("\n".join(lines))) - exposed == {"flash_firmware_v2"}


# The tool inventory used to live in README.md. It moved to docs/mcp-tools.md
# when README became the human entry point; the table and this pin moved
# together, because a table nothing reads back against the served surface is how
# the inventory drifts.
TOOL_INVENTORY_DOCUMENT = "docs/mcp-tools.md"
INVENTORY_TOOL_COLUMN = "Tools"
AGENTS_CONFIG_COLUMN = "Call"
AGENTS_CONFIG_FAMILY = "project_config_"
# The one configuration tool that table leaves out on purpose: it creates a
# configuration rather than changing an existing one, and both documents make
# that split — the inventory gives it a "Project setup" row of its own, apart
# from the "Project config" group. Excluded from the table, not from the
# document: the check below still requires AGENTS.md to name it somewhere.
AGENTS_CONFIG_TABLE_OMITS = frozenset({"project_config_create"})


def _documented_tools(text: str, header: str) -> list[str]:
    """Every backticked token in a document's tool column, verbatim.

    Verbatim because a token that is not a tool name has to be visible to the
    caller. Matching the shape of a name here would drop `debug_*` on the floor
    and report a table that names 26 of 42 tools as complete.
    """
    return [token for cell in _table_column(text, header) for token in re.findall(r"`([^`]+)`", cell)]


def _not_tool_names(names: list[str]) -> list[str]:
    return [name for name in names if not re.fullmatch(r"[a-z][a-z0-9_]*", name)]


def _repository_tool_contract() -> set[str]:
    contract = Path(__file__).resolve().parents[1] / "evals" / "install" / "tools.list.expected"
    return set(contract.read_text(encoding="utf-8").split())


def _repository_document(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")


def test_tool_inventory_table_is_the_whole_tool_inventory() -> None:
    """The inventory's `Tools` column holds the contract both ways.

    The same shape as the skill's routing table and for the same reason: the column is read, the prose is not. The notes column
    alone writes `set`, `describe`, `adopt_hardware` and `reload_description` in
    backticks, none of which is a tool name, and a scan of the document would
    have to exclude them by hand for as long as anybody keeps writing notes.
    """
    exposed = _repository_tool_contract()
    declared = _documented_tools(_repository_document(TOOL_INVENTORY_DOCUMENT), INVENTORY_TOOL_COLUMN)

    assert declared, f"{TOOL_INVENTORY_DOCUMENT} has no `{INVENTORY_TOOL_COLUMN}` column to read"
    assert len(set(declared)) == len(declared), "the tool table names a tool twice"
    assert set(declared) == exposed, (
        f"documented but not exposed: {sorted(set(declared) - exposed)}; "
        f"exposed but not documented: {sorted(exposed - set(declared))}"
    )


def test_tool_inventory_table_spells_out_every_tool_rather_than_a_prefix() -> None:
    """No wildcards in that column: `debug_*` stood for eleven tools and checked none.

    Expanding a prefix against the contract would have passed today and left the
    completeness direction blind exactly where the wildcard sits — a twelfth
    session tool would be absorbed by `debug_*` and the inventory would never
    have to mention it, which is the drift this check exists to catch. Spelling
    the names out costs one long cell and holds all 38.
    """
    declared = _documented_tools(_repository_document(TOOL_INVENTORY_DOCUMENT), INVENTORY_TOOL_COLUMN)

    assert not _not_tool_names(declared), (
        f"the `{INVENTORY_TOOL_COLUMN}` column must name each tool in full: {_not_tool_names(declared)}"
    )
    assert "debug_start_session" in declared and "debug_dump_symbol_ihex" in declared


def test_agents_configuration_table_holds_the_configuration_family() -> None:
    """AGENTS.md's table is a partial inventory, and partial does not mean unchecked.

    It claims the configuration tools, not all 38, so it is held to exactly
    that: every `project_config_*` tool this server serves has a row, and every
    row names one. Demanding the whole contract here would be a false claim
    about a table that never made it; demanding nothing would let a new
    configuration tool arrive unmentioned, which is the drift this check is
    about.
    """
    exposed = _repository_tool_contract()
    agents = _repository_document("AGENTS.md")
    declared = _documented_tools(agents, AGENTS_CONFIG_COLUMN)
    family = {name for name in exposed if name.startswith(AGENTS_CONFIG_FAMILY)} - AGENTS_CONFIG_TABLE_OMITS

    assert declared, f"AGENTS.md has no `{AGENTS_CONFIG_COLUMN}` column to read"
    assert not _not_tool_names(declared), _not_tool_names(declared)
    assert set(declared) == family, (
        f"documented but not a configuration tool this server serves: {sorted(set(declared) - family)}; "
        f"served but not documented: {sorted(family - set(declared))}"
    )
    # The omission is a placement, not an exemption: a tool kept out of the
    # table has to be covered by the document that keeps it out.
    for omitted in AGENTS_CONFIG_TABLE_OMITS:
        assert omitted in exposed and f"`{omitted}`" in agents, omitted


def test_the_document_tool_tables_fail_in_both_directions() -> None:
    """The proof the inventory contract asks for, run against the shipped documents.

    A tool added to the contract fails until the documents name it, and a name
    in a document the server does not serve fails too — for the whole inventory
    in docs/mcp-tools.md and for the configuration family in AGENTS.md.
    """
    exposed = _repository_tool_contract()
    inventory = _repository_document(TOOL_INVENTORY_DOCUMENT)
    agents = _repository_document("AGENTS.md")

    # A tool this server gains has no row anywhere until somebody writes one.
    assert (exposed | {"flash_firmware_v2"}) - set(_documented_tools(inventory, INVENTORY_TOOL_COLUMN)) == {
        "flash_firmware_v2"
    }
    gained_config = {
        name for name in exposed | {"project_config_freeze"} if name.startswith(AGENTS_CONFIG_FAMILY)
    } - AGENTS_CONFIG_TABLE_OMITS
    assert gained_config - set(_documented_tools(agents, AGENTS_CONFIG_COLUMN)) == {"project_config_freeze"}

    # A name a document invents is caught in the other direction.
    invented = _with_extra_row(inventory, INVENTORY_TOOL_COLUMN, "| Invented | `flash_firmware_v2` | no such tool |")
    assert set(_documented_tools(invented, INVENTORY_TOOL_COLUMN)) - exposed == {"flash_firmware_v2"}
    invented_agents = _with_extra_row(agents, AGENTS_CONFIG_COLUMN, "| `project_config_freeze` | nothing |")
    assert set(_documented_tools(invented_agents, AGENTS_CONFIG_COLUMN)) - exposed == {"project_config_freeze"}

    # A served tool in the wrong table is caught as well: AGENTS.md's is the
    # configuration family, so a real tool from outside it is still a lie.
    misfiled = _with_extra_row(agents, AGENTS_CONFIG_COLUMN, "| `flash_firmware` | not a configuration tool |")
    assert "flash_firmware" in set(_documented_tools(misfiled, AGENTS_CONFIG_COLUMN)) - {
        name for name in exposed if name.startswith(AGENTS_CONFIG_FAMILY)
    }

    # And the wildcard this check ruled out is caught rather than skipped: the
    # reader hands it back as written, so it fails the shape it does not have.
    wildcarded = _with_extra_row(inventory, INVENTORY_TOOL_COLUMN, "| Debug sessions | `debug_*` | a prefix |")
    assert _not_tool_names(_documented_tools(wildcarded, INVENTORY_TOOL_COLUMN)) == ["debug_*"]


def _with_extra_row(text: str, header: str, row: str) -> str:
    """The same table the check reads, one row longer.

    By header rather than by the document's last table row: AGENTS.md carries
    tables after the one under test, and appending to the wrong one would prove
    nothing.
    """
    lines = text.splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.strip().startswith("|") and header in [cell.strip() for cell in line.strip().strip("|").split("|")]
    )
    end = start
    while end + 1 < len(lines) and lines[end + 1].startswith("|"):
        end += 1
    lines.insert(end + 1, row)
    return "\n".join(lines)


@pytest.mark.parametrize("agent", ["claude-code", "opencode"])
def test_register_agent_mcp_never_repoints_an_operator_owned_absolute_command(
    agent: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    _trusted_test_mcp_command(monkeypatch)
    operator_command = tmp_path / "operator" / "agentic-hil"
    relative_path = Path(".claude.json") if agent == "claude-code" else Path(".config/opencode/opencode.json")
    entry = (
        {"type": "stdio", "command": str(operator_command), "args": ["mcp-stdio"]}
        if agent == "claude-code"
        else {"type": "local", "command": [str(operator_command), "mcp-stdio"], "enabled": True}
    )
    container = "mcpServers" if agent == "claude-code" else "mcp"
    path = home / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = json.dumps({container: {"agentic-hil": entry}})
    path.write_text(existing, encoding="utf-8")

    result = register_agent_mcp(agent)

    # A JSON config carries no managed marker, so an absolute command outside the
    # workspace that is not a trusted launcher belongs to the operator.
    assert result["ok"] is False
    assert result["error_type"] == "mcp_config_conflict"
    assert path.read_text(encoding="utf-8") == existing


@pytest.mark.parametrize(
    ("agent", "relative_path", "document", "entry_path"),
    [
        (
            "claude-code",
            Path(".claude.json"),
            {"keep": 1, "mcpServers": {"agentic-hil": {"type": "stdio", "command": "agentic-hil", "args": ["mcp-stdio"]}}},
            ("mcpServers", "agentic-hil", "command"),
        ),
        (
            "opencode",
            Path(".config/opencode/opencode.json"),
            {"keep": 1, "mcp": {"agentic-hil": {"type": "local", "command": ["uvx", "--from", "agentic-hil", "agentic-hil", "mcp-stdio"], "enabled": True}}},
            ("mcp", "agentic-hil", "command"),
        ),
    ],
)
def test_register_agent_mcp_migrates_recognized_legacy_json_entry(
    agent: str,
    relative_path: Path,
    document: dict,
    entry_path: tuple[str, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    command = _trusted_test_mcp_command(monkeypatch)
    path = home / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")

    result = register_agent_mcp(agent)

    assert result["ok"] is True
    assert result["migrated"] is True
    migrated = json.loads(path.read_text(encoding="utf-8"))
    value = migrated[entry_path[0]][entry_path[1]][entry_path[2]]
    assert value == command or value == [command, "mcp-stdio"]
    assert migrated["keep"] == 1


@pytest.mark.parametrize(
    ("agent", "relative_path", "container"),
    [
        ("claude-code", Path(".claude.json"), "mcpServers"),
        ("opencode", Path(".config/opencode/opencode.json"), "mcp"),
    ],
)
def test_register_agent_mcp_migrates_managed_workspace_command_without_force(
    agent: str,
    relative_path: Path,
    container: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    command = _trusted_test_mcp_command(monkeypatch)
    stale = str(workspace / "old-venv" / "agentic-hil")
    entry = (
        {"type": "stdio", "command": stale, "args": ["mcp-stdio"]}
        if agent == "claude-code"
        else {"type": "local", "command": [stale, "mcp-stdio"], "enabled": True}
    )
    path = home / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({container: {"agentic-hil": entry}}), encoding="utf-8")

    result = register_agent_mcp(agent)

    assert result["ok"] is True
    assert result["migrated"] is True
    migrated = json.loads(path.read_text(encoding="utf-8"))[container]["agentic-hil"]["command"]
    assert migrated == command or migrated == [command, "mcp-stdio"]


@pytest.mark.parametrize("command", ["agentic-hil", "bin/agentic-hil", "uvx"])
def test_register_agent_mcp_rejects_non_absolute_injected_command(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path.home() / ".claude.json"

    with pytest.raises(ConfigError) as excinfo:
        register_agent_mcp("claude-code", command=command)

    assert excinfo.value.error_type == "mcp_command_untrusted"
    assert not path.exists()


def test_register_agent_mcp_rejects_workspace_injected_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    command = workspace / "agentic-hil"

    with pytest.raises(ConfigError) as excinfo:
        register_agent_mcp("claude-code", command=str(command))

    assert excinfo.value.error_type == "mcp_command_untrusted"
    assert not (Path.home() / ".claude.json").exists()


def test_register_agent_mcp_rejects_temp_and_uv_cache_injected_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_command = tmp_path / "agentic-hil"
    uv_cache = Path.home() / "uv-cache"
    monkeypatch.setenv("UV_CACHE_DIR", str(uv_cache))

    for command in (temp_command, uv_cache / "archive" / "agentic-hil"):
        with pytest.raises(ConfigError) as excinfo:
            register_agent_mcp("claude-code", command=str(command))
        assert excinfo.value.error_type == "mcp_command_untrusted"

    assert not (Path.home() / ".claude.json").exists()


def test_default_mcp_path_is_written_when_the_working_directory_is_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registration's own home is not a workspace it has to stay out of.

    The check that keeps user-level files out of a repository read the working
    directory as the repository, so standing in the home directory made
    `~/.claude.json` "inside the workspace" and refused the one place that file
    is supposed to be. (#235)
    """
    monkeypatch.chdir(Path.home())

    result = register_agent_mcp("claude-code", command=str(trusted_launcher()))

    assert result["ok"] is True, result
    assert (Path.home() / ".claude.json").is_file()


def test_default_skill_path_is_rejected_from_a_workspace_below_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Home and the directories above it are the whole of the exception.

    The boundary collapses only where it would otherwise swallow the home
    directory whole. A working directory below home is a project like any other,
    and a user-level target inside its tree is still refused.
    """
    inside = Path.home() / ".claude"
    inside.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(inside)

    with pytest.raises(ConfigError) as excinfo:
        install_skill("claude-code")

    assert excinfo.value.error_type == "unsafe_configured_path"
    assert not _claude_skill_path().exists()


def test_the_pinned_launcher_may_live_in_the_directory_the_command_runs_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uv and pipx install the launcher under home, and home is where it is typed.

    The launcher trust check draws its boundary at the working directory too, so
    from home the copy `install.sh` had just written to `~/.local/bin` was
    refused for coming "from the workspace" and the registration had nothing
    left to register. A launcher inside a real project workspace stays refused,
    which `test_register_agent_mcp_rejects_workspace_injected_command` pins. (#235)
    """
    launcher = trusted_launcher()
    home = launcher.parent.parent
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(home)
    monkeypatch.setattr("agentic_hil.cli._mcp_command_candidates", lambda: [str(launcher)])

    assert mcp_server_command() == str(launcher)


def test_register_agent_mcp_rejects_hardlinked_user_config_without_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    path = home / ".claude.json"
    victim = home / "victim.json"
    existing = '{"mcpServers": {"custom": {}}}'
    victim.write_text(existing, encoding="utf-8")
    try:
        os.link(victim, path)
    except OSError as error:
        pytest.skip(f"hardlinks unavailable: {error}")

    with pytest.raises(ConfigError) as excinfo:
        register_agent_mcp("claude-code", command=str(trusted_launcher()))

    assert excinfo.value.error_type == "unsafe_configured_path"
    assert victim.read_text(encoding="utf-8") == existing


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher symlink contract")
def test_trusted_persistent_executable_accepts_safe_pipx_style_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    launcher_dir = tmp_path / "home" / ".local" / "bin"
    target_dir = tmp_path / "home" / ".local" / "share" / "pipx" / "venvs" / "agentic-hil" / "bin"
    workspace.mkdir()
    launcher_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    target = target_dir / "agentic-hil"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o700)
    launcher = launcher_dir / "agentic-hil"
    launcher.symlink_to(target)

    assert trusted_persistent_executable(launcher, workspace=workspace) == str(launcher)


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher symlink contract")
@pytest.mark.parametrize("forbidden_kind", ["workspace", "cache"])
def test_trusted_persistent_executable_rejects_symlink_target_in_forbidden_root(
    forbidden_kind: str,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    cache = tmp_path / "uv-cache"
    launcher_dir = tmp_path / "home" / ".local" / "bin"
    workspace.mkdir()
    cache.mkdir()
    launcher_dir.mkdir(parents=True)
    forbidden_root = workspace if forbidden_kind == "workspace" else cache
    target = forbidden_root / "agentic-hil"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o700)
    launcher = launcher_dir / "agentic-hil"
    launcher.symlink_to(target)

    with pytest.raises(ConfigError) as excinfo:
        trusted_persistent_executable(launcher, workspace=workspace, disallowed_roots=[cache])

    assert excinfo.value.error_type == "mcp_command_untrusted"


def _directory_stat(mode: int, uid: int) -> SimpleNamespace:
    """A stat of an opened directory, as the POSIX ancestor walk sees one.

    The walk itself cannot run on Windows — it needs `os.geteuid`, `O_DIRECTORY`
    and `dir_fd` — so the decision it makes per component is a named function
    and this is what that function is asked about, on every host.
    """
    return SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=uid)


@pytest.mark.parametrize("mode", [0o775, 0o777, 0o1777, 0o755])
@pytest.mark.parametrize("final", [True, False])
def test_a_launcher_ancestor_is_no_longer_untrusted_for_who_else_may_write_it(mode: int, final: bool) -> None:
    """The refusal that made an ordinary user-local installation unregisterable.

    `~` or `~/.local` at `0775` is what a distribution combining `umask 002`
    with per-user groups produces, and the ancestor walk refused every one of
    them: on such a host `mcp-config` rejected every candidate and then
    recommended installing with the user-level tool installer that had written
    the layout it was refusing. Group and other write are no longer read, sticky
    or not, at any depth — the same detect-rather-than-prevent line Windows has
    held since its ACL walk was removed in 0.8.0.
    """
    assert untrusted_launcher_directory(_directory_stat(mode, 1000), final=final, trusted_uids=frozenset({0, 1000})) is None


def test_the_launchers_own_parent_must_still_belong_to_root_or_this_user() -> None:
    """What survives the removal: identity, not permissions.

    A directory a third account owns can have the validated file renamed out of
    it between the check and the registration, and no mode expresses that. Only
    the launcher's own parent is asked — an ancestor further up owned by another
    user is how every `/home/<user>` chain looks.
    """
    foreign = _directory_stat(0o755, 4242)
    trusted = frozenset({0, 1000})

    assert untrusted_launcher_directory(foreign, final=True, trusted_uids=trusted) == "owned by another user"
    assert untrusted_launcher_directory(foreign, final=False, trusted_uids=trusted) is None
    assert untrusted_launcher_directory(_directory_stat(0o755, 0), final=True, trusted_uids=trusted) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes; the walk this exercises is POSIX-only")
def test_a_umask_002_home_registers_its_launcher(tmp_path: Path) -> None:
    """The reported case, end to end, through the walk itself.

    Reported twice from a normally installed, non-editable installation: every
    ancestor group-writable, the launcher itself as an installer writes it.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launcher = tmp_path / "home" / ".local" / "bin" / "agentic-hil"
    launcher.parent.mkdir(parents=True)
    for directory in (tmp_path / "home", tmp_path / "home" / ".local", launcher.parent):
        directory.chmod(0o775)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)

    assert trusted_persistent_executable(launcher, workspace=workspace) == str(launcher)
    # Untouched: this validates, it does not repair. Smoothing is a separate
    # call an operator's `setup` makes, and it is the only thing that chmods.
    assert stat.S_IMODE((tmp_path / "home").stat().st_mode) == 0o775


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes; the walk this exercises is POSIX-only")
def test_the_launcher_file_itself_may_still_not_be_writable_by_others(tmp_path: Path) -> None:
    """The ancestors stopped being read; the executable did not.

    It is the object that gets run under the hardware gate, so who may rewrite
    it is the one permission question still worth refusing on. The refusal
    carries the mode and the owner it read, because a caller who is told only
    that the file is untrusted has to go and stat it to learn why.

    Runs as root too: what refuses here is the mode this code reads, not the
    mode the kernel would enforce against the caller.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launcher = tmp_path / "home" / ".local" / "bin" / "agentic-hil"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o775)

    with pytest.raises(ConfigError) as refused:
        trusted_persistent_executable(launcher, workspace=workspace)

    assert refused.value.error_type == "mcp_command_untrusted"
    assert refused.value.details["mode"] == "0775"
    assert refused.value.details["uid"] == os.geteuid()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission smoothing")
def test_tighten_owned_writable_ancestors_tightens_dirs_and_launcher_file(tmp_path: Path) -> None:
    state_dir = tmp_path / "home" / ".local" / "state" / "agentic-hil"
    state_dir.mkdir(parents=True)
    for directory in (tmp_path / "home", tmp_path / "home" / ".local", tmp_path / "home" / ".local" / "state"):
        directory.chmod(0o775)
    launcher = tmp_path / "home" / ".local" / "bin" / "agentic-hil"
    launcher.parent.mkdir(parents=True)
    launcher.parent.chmod(0o775)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o775)

    actions = tighten_owned_writable_ancestors(launcher)

    assert actions, "expected the group-writable components to be reported"
    launcher_mode = launcher.stat().st_mode
    assert not (launcher_mode & 0o022), "launcher must lose group/other write"
    assert launcher_mode & 0o100, "launcher must stay owner-executable"
    for directory in (launcher.parent, tmp_path / "home" / ".local", tmp_path / "home"):
        assert not (directory.stat().st_mode & 0o022), f"{directory} still group/other-writable"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission smoothing")
def test_smoothing_covers_the_launcher_chain_and_nothing_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One validator walks modes, so one chain is smoothed.

    `trusted_persistent_executable` refuses an MCP launcher that group or other
    may write, and it is the last check that reads a POSIX mode at all.
    Smoothing exists to keep a launcher an installer wrote under a umask-002 /
    private-group home from failing it; anything beyond that chain is a chmod
    with no refusal behind it."""
    launcher = tmp_path / "profile" / ".local" / "bin" / "agentic-hil"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o775)
    for directory in (tmp_path / "profile", tmp_path / "profile" / ".local", launcher.parent):
        directory.chmod(0o775)
    elsewhere = tmp_path / "profile" / ".config" / "agentic-hil"
    elsewhere.mkdir(parents=True)
    elsewhere.chmod(0o775)
    monkeypatch.setattr("agentic_hil.cli._mcp_command_candidates", lambda: [str(launcher)])

    actions = _smooth_user_permissions()

    assert actions, "the launcher chain is still smoothed"
    assert not (launcher.stat().st_mode & 0o022)
    assert not (launcher.parent.stat().st_mode & 0o022)
    # A sibling of the chain, of the shape the removed configured-path check
    # used to reach for: off the walk, so untouched.
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o775


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_setup_leaves_configured_path_modes_exactly_as_it_found_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_config_environment: Path,
) -> None:
    """An operator's group-writable tree survives `setup`.

    `init`/`setup` used to chmod group/other write off every user-owned ancestor
    of `state_root`, the authoritative config and the agent's policy file,
    because the configured-path trust check refused those directories for their
    mode. That check was removed whole and the mutation outlived
    it — invisible on Windows, where the tightening is a no-op, and a real loss
    of access on POSIX for a validator that no longer exists. Setup succeeds on
    the same tree it used to rewrite, and rewrites none of it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _trusted_test_mcp_command(monkeypatch)

    home = Path.home()
    state_parent = user_state_root().parent
    state_parent.mkdir(parents=True, exist_ok=True)
    config_home = isolated_config_environment
    config_home.mkdir(parents=True, exist_ok=True)
    writable = [home, config_home, state_parent]
    for directory in writable:
        directory.chmod(0o775)
    before = {directory: stat.S_IMODE(directory.stat().st_mode) for directory in writable}
    assert all(mode & 0o020 for mode in before.values())

    result = setup_project(agent="codex")

    assert result["ok"] is True, result
    assert result["steps"]["mcp_config"]["ok"] is True
    assert result["permission_changes"] == []
    for directory, mode in before.items():
        assert stat.S_IMODE(directory.stat().st_mode) == mode, f"{directory} was chmod-ed by setup"
    # The registration landed in the user-level config, never the repo.
    assert (home / ".codex" / "config.toml").is_file()
    assert not (workspace / ".mcp.json").exists()


def test_mcp_stdio_serves_a_workspace_that_has_no_config_yet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing configuration used to end the server before it started.

    That left an agent holding config_file_not_found with no route that did not
    involve a person at a terminal. The server now starts bound to the workspace,
    every hardware tool answers with the same error type, and the answer names
    the one tool that can do something about it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    served: dict = {}

    def fake_server(config, *args, **kwargs):
        served["config"] = config
        served["tools"] = kwargs.get("tools")
        return 0

    monkeypatch.setattr("agentic_hil.cli.run_stdio_server", fake_server)

    assert entrypoint(["mcp-stdio"]) == 0
    assert served["config"] is None
    service = served["tools"]
    assert isinstance(service, UnprovisionedToolService)
    assert service.workspace == workspace.resolve()

    refused = service.call("probe_target", {})

    assert refused["error_type"] == "config_file_not_found"
    assert "project_config_create" in refused["next_step"]


def test_mcp_stdio_passes_authoritative_config_to_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(
        workspace, monkeypatch, permissions={"allow_probe": False}
    )
    monkeypatch.delenv("AGENTIC_HIL_CONFIG")
    received: dict = {}

    def fake_server(config):
        received["config"] = config
        return 0

    monkeypatch.chdir(workspace)
    monkeypatch.setattr("agentic_hil.cli.run_stdio_server", fake_server)

    exit_code = entrypoint(["mcp-stdio"])

    assert exit_code == 0
    assert received["config"].config_path == str(config_path.resolve())
    assert received["config"].debuggers["dut"].permissions.allow_probe is False


def test_doctor_succeeds_when_debugger_check_is_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(workspace, monkeypatch, permissions={})
    monkeypatch.chdir(workspace)

    result = doctor()

    assert result["ok"] is True
    assert result["debugger"]["skipped"] is True
    assert result["config_path"] == str(config_path.resolve())


def test_doctor_closes_tool_service_after_debugger_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch)
    lifecycle: list[str] = []

    class DoctorService:
        def __init__(self, config, frontend: str) -> None:
            assert frontend == "doctor"
            lifecycle.append("created")

        def call(self, name: str) -> dict:
            assert name == "debugger_info"
            lifecycle.append("called")
            return {"ok": True}

        def close(self) -> None:
            lifecycle.append("closed")

    monkeypatch.chdir(workspace)
    monkeypatch.setattr("agentic_hil.cli.AgenticHILToolService", DoctorService)

    result = doctor()

    assert result["ok"] is True
    assert lifecycle == ["created", "called", "closed"]


def test_com_stdio_uses_authoritative_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(
        workspace,
        monkeypatch,
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n',
    )
    received: dict = {}

    def fake_com_stdio(config, port_id, **kwargs):
        received["config"] = config
        received["port_id"] = port_id
        return 0

    monkeypatch.chdir(workspace)
    monkeypatch.setattr("agentic_hil.cli.run_com_stdio", fake_com_stdio)

    exit_code = entrypoint(["com-stdio", "--port", "dut_uart"])

    assert exit_code == 0
    assert received["config"].config_path == str(config_path.resolve())
    assert received["port_id"] == "dut_uart"


def test_config_loads_defaults(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    assert config.target.name == "example-target"
    assert config.debugger.probe_id is None
    assert config.artifacts.allowed_extensions == [".elf", ".hex", ".bin"]
    assert config.can_buses == {}
    assert config.debugger_id == "dut"
    assert config.debuggers["dut"].permissions.allow_flash is True


def test_tool_service_keeps_startup_config_when_file_becomes_invalid(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        config_path.write_text("debuggers: [invalid\n", encoding="utf-8")
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is True


def test_mcp_lists_configured_socketcan_buses_without_opening_hardware(tmp_path: Path) -> None:
    config = load_config(
        str(
            write_config(
                tmp_path,
                can_buses_yaml='''can_buses:
  dut_can:
    adapter: "socketcan"
    channel: "can0"
    bitrate: 500000
''',
            )
        ),
    )
    service = AgenticHILToolService(config)
    try:
        listed = mcp_tool_call(service, "can_buses_list")
    finally:
        service.close()
    assert listed["ok"] is True
    assert listed["buses"]["dut_can"]["adapter"] == "socketcan"
    assert "socketcan" in listed["supported_adapters"]


def test_openocd_passes_configured_probe_id(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, probe_id="STLINK123")))
    service = AgenticHILToolService(config)
    try:
        probe = mcp_tool_call(service, "probe_target")
    finally:
        service.close()
    assert probe["ok"] is True
    log_text = (tmp_path / probe["log_path"]).read_text(encoding="utf-8")
    assert "adapter serial STLINK123" in log_text


def test_openocd_probe_listing_reports_not_supported(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        result = mcp_tool_call(service, "debugger_probes_list")
    finally:
        service.close()
    assert result["ok"] is False
    assert result["error_type"] == "not_supported"


def test_stlink_lists_all_connected_probe_ids(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, debugger_type="stlink")))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "debugger_probes_list")
    finally:
        service.close()
    assert result["ok"] is True, result
    assert result["probes"] == [{"probe_id": "STLINK123"}, {"probe_id": "STLINK456"}]


def test_pyocd_lists_all_connected_probe_ids(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd")))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "debugger_probes_list")
    finally:
        service.close()
    assert result["ok"] is True, result
    assert result["probes"] == [{"probe_id": "PYOCD123"}, {"probe_id": "PYOCD456"}]


def test_probe_listing_requires_probe_permission(tmp_path: Path) -> None:
    config = load_config(
        str(write_config(tmp_path, debugger_type="stlink", permissions={"allow_probe": False})),
    )
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "debugger_probes_list")
    finally:
        service.close()
    assert result["ok"] is False
    assert result["error_type"] == "permission_denied"


def test_probe_listing_parsers_fail_closed() -> None:
    assert stlink_probe_ids("ST-LINK SN  : 001\nSTLink SN: 002\n") == ["001", "002"]
    assert stlink_empty_result("No ST-LINK detected") is True
    assert parse_pyocd_probes("not-json")["error_type"] == "probe_discovery_failed"
    assert parse_pyocd_probes('{"status": 1, "boards": []}')["error_type"] == "probe_discovery_failed"


def test_debugger_probes_cli_uses_authoritative_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_authoritative_config(tmp_path, monkeypatch, debugger_type="stlink")
    monkeypatch.chdir(tmp_path)

    exit_code = entrypoint(["debugger-probes"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["probes"] == [{"probe_id": "STLINK123"}, {"probe_id": "STLINK456"}]
    # A configured bench answers through its own backend and says so by not
    # claiming otherwise: the bootstrap fallback labels every answer it gives,
    # so this label appearing here would mean the configured path had been
    # rerouted through discovery that knows nothing about this file.
    assert "source" not in result


def test_debugger_probes_answers_through_bootstrap_without_a_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The listing is wanted exactly where the configuration is not there yet.

    Before the first `setup` an operator wants to know whether the board is
    visible and whether there is one of it, and `setup`'s own bootstrap already
    enumerates without a configuration. So this answers, out of that same fixed
    read-only command, and labels where the answer came from rather than
    letting it pass for the configured bench.
    """
    workspace = tmp_path / "unconfigured"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    commands: list[list[str]] = []

    def fake_spawn(command: list[str], cwd: str, timeout_s: float) -> CompletedCommand:
        commands.append(command)
        return CompletedCommand("ST-LINK SN : STLINK123\n", "", 0, False, False)

    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: str(Path("C:/ST/STM32_Programmer_CLI.exe")))
    monkeypatch.setattr("agentic_hil.bootstrap.spawn_command", fake_spawn)

    exit_code = entrypoint(["debugger-probes"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0, result
    assert result["ok"] is True
    assert result["probes"] == [{"probe_id": "STLINK123"}]
    assert result["source"] == "bootstrap"
    assert result["backend"] == "stlink"
    # One command, the read-only enumeration and nothing else: no HOTPLUG
    # connect follows it here, because nothing is being adopted.
    assert [command[-3:] for command in commands] == [["-q", "-l", "st-link-only"]]


def test_openocd_flash_defaults_to_no_reset_and_can_reset_explicitly(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    config = load_config(str(write_config(tmp_path)))
    service = AgenticHILToolService(config)
    try:
        no_reset = mcp_tool_call(service, "flash_firmware", {"image_path": "build/firmware.elf"})
        with_reset = mcp_tool_call(service, "flash_firmware", {"image_path": "build/firmware.elf", "reset_after_flash": True})
    finally:
        service.close()
    assert no_reset["ok"] is True
    assert no_reset["reset_after_flash"] is False
    no_reset_log = (tmp_path / no_reset["log_path"]).read_text(encoding="utf-8")
    assert "program" in no_reset_log
    assert "verify reset" not in no_reset_log
    assert with_reset["ok"] is True
    assert with_reset["reset_after_flash"] is True
    reset_log = (tmp_path / with_reset["log_path"]).read_text(encoding="utf-8")
    assert "verify reset" in reset_log


OPENOCD_RESET_COMMANDS = {
    "run": 'init; echo "AGENTIC_HIL_STAGE:init:ok"; reset run; echo "AGENTIC_HIL_RESULT:reset_target:ok"; shutdown',
    "halt": 'init; echo "AGENTIC_HIL_STAGE:init:ok"; reset halt; echo "AGENTIC_HIL_RESULT:reset_target:ok"; shutdown',
    "init": 'init; echo "AGENTIC_HIL_STAGE:init:ok"; reset init; echo "AGENTIC_HIL_RESULT:reset_target:ok"; shutdown',
}


@pytest.mark.parametrize("mode", ["run", "halt", "init"])
def test_openocd_reset_sends_init_before_reset_in_every_mode(tmp_path: Path, mode: str) -> None:
    """The command line itself, not just the result.

    0.7.0 sent `reset <mode>` with no `init`, which OpenOCD refuses with
    `invalid command name "reset"` before it opens the probe, and no test looked
    at what was sent. `init; reset init` is two different things in one line: the
    server leaving its configuration stage, then OpenOCD's reset-init mode - the
    same pair OpenOCD's own `program` proc issues."""
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        result = mcp_tool_call(service, "reset_target", {"mode": mode})
    finally:
        service.close()

    assert openocd_backend.openocd_reset_command(mode, "AGENTIC_HIL_RESULT:reset_target:ok") == OPENOCD_RESET_COMMANDS[mode]
    assert result["ok"] is True, result
    assert result["success_confirmed"] is True
    logged = json.loads((tmp_path / result["log_path"]).read_text(encoding="utf-8"))["command"]
    assert logged.endswith(command_for_log(["-c", OPENOCD_RESET_COMMANDS[mode]]))


def test_openocd_probe_sends_init_before_the_target_query(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        result = mcp_tool_call(service, "probe_target")
    finally:
        service.close()

    assert result["ok"] is True, result
    logged = json.loads((tmp_path / result["log_path"]).read_text(encoding="utf-8"))["command"]
    assert logged.endswith(
        command_for_log(["-c", 'init; echo "AGENTIC_HIL_STAGE:init:ok"; targets; echo "AGENTIC_HIL_RESULT:probe_target:ok"; shutdown'])
    )


def test_a_command_openocd_refuses_before_init_does_not_take_the_bench_out_of_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 0.7.0 command line again, and what it may cost.

    OpenOCD stopped in its interpreter, before it opened the adapter, so the
    board is untouched. Reporting that as an unconfirmed target state is what
    turned a mistyped command line into a work stoppage: the quarantine also
    blocked the reset that would have cleared it."""
    monkeypatch.setattr(
        openocd_backend,
        "openocd_reset_command",
        lambda mode, marker: f'reset {mode}; echo "{marker}"; shutdown',
    )
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        refused = mcp_tool_call(service, "reset_target", {"mode": "halt"})
        still_usable = mcp_tool_call(service, "probe_target")
    finally:
        service.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "debugger_command_rejected"
    assert refused["backend_error_type"] == "command_rejected_before_init"
    assert refused["rejected_commands"] == ["reset"]
    assert refused["target_contacted"] is False
    assert refused["side_effect_committed"] is False
    assert refused["side_effect_status"] == "not_started"
    assert refused["retry_safe"] is True
    assert refused.get("quarantined") is not True, refused
    assert refused.get("cleanup_required") is not True, refused
    # The operator is told not to go and look at the board for this one.
    assert any("rejected_commands" in step for step in refused["remediation"])
    assert still_usable["ok"] is True, still_usable


def test_a_reset_that_may_have_reached_the_target_still_quarantines(tmp_path: Path) -> None:
    """The other direction, which matters more.

    This OpenOCD opened the adapter and then failed, so nothing on the host knows
    what state the target is in. Only an abort that provably never got that far
    may be waved through."""
    config = load_config(str(write_config(tmp_path, debugger_executable=FAKE_OPENOCD_NO_TARGET)))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "reset_target", {"mode": "halt"})
    finally:
        service.close()

    assert result["ok"] is False
    assert result.get("rejected_commands") is None
    assert result["side_effect_status"] == "unknown"
    # The reason is unchanged and it is what the caller reads. The bench is
    # not held for it: an unconfirmed target is what the next reset and probe
    # speak for, so the incident ended when this call did.
    assert result["quarantined"] is False
    assert result["incident_stood_down"]["reasons"] == ["debugger_result_unconfirmed"]
    assert result["cleanup_reasons"] == ["debugger_result_unconfirmed"]


def test_an_openocd_that_is_not_installed_is_a_call_that_never_started(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, debugger_executable=tmp_path / "missing-openocd.exe")))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "reset_target", {"mode": "run"})
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "debugger_not_found"
    assert result["target_contacted"] is False
    assert result["side_effect_status"] == "not_started"
    assert result.get("quarantined") is not True, result


def test_the_openocd_fake_refuses_a_run_stage_command_the_way_openocd_does() -> None:
    """The fake is the thing under test here.

    Every OpenOCD test in this suite is only worth what this evaluator is worth,
    and the one it accepted for three releases could not have run."""
    refused, initialized, exit_code = fake_openocd.evaluate('reset halt; echo "done"; shutdown', initialized=False)
    accepted, _, accepted_exit = fake_openocd.evaluate('init; reset halt; echo "done"; shutdown', initialized=False)

    assert refused == ['Error: invalid command name "reset"']
    assert initialized is False
    assert exit_code == 1
    assert accepted == ["done"]
    assert accepted_exit == 0


def test_stlink_backend_probes_and_flashes_with_probe_id(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    config = load_config(str(write_config(tmp_path, debugger_type="stlink", probe_id="STLINK123")))
    service = AgenticHILToolService(config)
    try:
        info = mcp_tool_call(service, "debugger_info")
        probe = mcp_tool_call(service, "probe_target")
        flash = mcp_tool_call(service, "flash_firmware", {"image_path": "build/firmware.elf"})
    finally:
        service.close()
    assert info["ok"] is True
    assert probe["ok"] is True
    probe_log = (tmp_path / probe["log_path"]).read_text(encoding="utf-8")
    assert "mode=HOTPLUG" in probe_log
    assert flash["ok"] is True
    assert flash["operation_result"]["confirmed"] is True
    assert flash["reset_after_flash"] is False
    log_text = (tmp_path / flash["log_path"]).read_text(encoding="utf-8")
    assert "port=SWD" in log_text
    assert "mode=HOTPLUG" in log_text
    assert "sn=STLINK123" in log_text
    assert "-w" in log_text
    assert "-v" in log_text
    assert "-rst" not in log_text


def test_pyocd_backend_probes_flashes_and_resets_with_probe_and_target(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    config = load_config(
        str(write_config(tmp_path, debugger_type="pyocd", probe_id="PYOCD123", target_type="stm32f446re")),
    )
    service = AgenticHILToolService(config)
    try:
        info = mcp_tool_call(service, "debugger_info")
        probe = mcp_tool_call(service, "probe_target")
        flash = mcp_tool_call(service, "flash_firmware", {"image_path": "build/firmware.elf", "reset_after_flash": True})
        reset = mcp_tool_call(service, "reset_target", {"mode": "halt"})
    finally:
        service.close()
    assert info["ok"] is True
    assert "0.36.0" in info["version"]
    assert probe["ok"] is True
    assert probe["target_detected"] is True
    probe_log = (tmp_path / probe["log_path"]).read_text(encoding="utf-8")
    assert "--uid PYOCD123" in probe_log
    assert "--target stm32f446re" in probe_log
    assert flash["ok"] is True
    assert flash["reset_after_flash"] is True
    flash_log = (tmp_path / flash["log_path"]).read_text(encoding="utf-8")
    assert "flash" in flash_log
    assert "--no-reset" in flash_log
    assert "firmware.elf" in flash_log
    assert reset["ok"] is True
    reset_log = (tmp_path / reset["log_path"]).read_text(encoding="utf-8")
    assert "reset halt" in reset_log


def test_pyocd_requires_flash_address_for_bin_artifacts(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.bin"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x01\x02\x03\x04")
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd")))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "flash_firmware", {"image_path": "build/firmware.bin"})
    finally:
        service.close()
    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    assert "debuggers.<name>.flash_address" in result["summary"]


def test_stlink_rejects_unconfirmed_successful_exit(tmp_path: Path) -> None:
    config = load_config(
        str(write_config(tmp_path, debugger_type="stlink", debugger_executable=FAKE_STLINK_UNCONFIRMED)),
    )
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "reset_target", {"mode": "run"})
    finally:
        service.close()
    assert result["ok"] is False
    assert result["error_type"] == "reset_failed"
    assert result["backend_error_type"] == "reset_unconfirmed"


# What STM32CubeProgrammer prints when each reset mode's own command did what it
# says. Two commands, two success lines: `-rst` announces the reset it drove,
# `-halt` announces the core it stopped. Measured on v2.23.0 against a
# NUCLEO-F446RE and modelled in fake_stlink.py.
STLINK_RESET_SUCCESS_TEXT = {
    "run": ["MCU Reset", "reset is performed"],
    "halt": ["Core halted"],
}


@pytest.mark.parametrize("mode", sorted(STLINK_RESET_SUCCESS_TEXT))
def test_stlink_confirms_each_reset_mode_on_the_lines_its_own_command_prints(tmp_path: Path, mode: str) -> None:
    """Mode `halt` can report success at all, and mode `run` is untouched by that.

    `halt` sends `-halt`, which prints neither line `-rst` prints, and the
    backend asked for the `-rst` pair whichever mode was requested — so a halt
    that did exactly what was asked came back `reset_failed` /
    `reset_unconfirmed`, quarantining the bench over a correct call (#142).
    Confirmation is still all-or-nothing; what changed is which set is
    selected. The `run` row is the old set unchanged and is here to keep it
    that way."""
    config = load_config(str(write_config(tmp_path, debugger_type="stlink", probe_id="STLINK123")))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "reset_target", {"mode": mode})
    finally:
        service.close()

    assert result["ok"] is True, result
    assert result["mode"] == mode
    assert result["success_confirmed"] is True
    assert result["operation_result"] == {"confirmed": True, "matched_success_text": STLINK_RESET_SUCCESS_TEXT[mode]}
    assert result["summary"] == f"Target reset with mode '{mode}'."
    assert result["side_effect_status"] == "committed"
    assert result.get("quarantined") is not True, result
    logged = json.loads((tmp_path / result["log_path"]).read_text(encoding="utf-8"))["command"]
    assert BACKEND_RESET_COMMANDS[("stlink", mode)] in logged


def test_a_halt_that_never_reported_the_core_stays_unconfirmed_however_much_the_connect_printed(tmp_path: Path) -> None:
    """The connect banner is not a marker, and this is why.

    This CLI connects, prints the whole banner including `Reset mode  :
    Software reset` — the line that says mode `halt` resets before it halts —
    and then never reports stopping the core. Connect runs before the halt is
    attempted, so every line here is one a failed halt prints too; reading any
    of them as confirmation would have confirmed exactly the run nobody can
    account for. `Core halted` is the operation's own success line, it did not
    arrive, and the result says so and quarantines."""
    config = load_config(
        str(write_config(tmp_path, debugger_type="stlink", debugger_executable=FAKE_STLINK_HALT_UNCONFIRMED)),
    )
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "reset_target", {"mode": "halt"})
    finally:
        service.close()

    assert result["ok"] is False, result
    assert result["error_type"] == "reset_failed"
    assert result["backend_error_type"] == "reset_unconfirmed"
    assert result["operation_result"] == {"confirmed": False, "expected_success_text": ["Core halted"], "matched_success_text": []}
    # Nothing on the host knows whether the core is halted or running, so this
    # is the branch that stops the bench rather than one that refuses.
    assert result["side_effect_status"] == "unknown"
    # The reason is unchanged and it is what the caller reads. The bench is
    # not held for it: an unconfirmed target is what the next reset and probe
    # speak for, so the incident ended when this call did.
    assert result["quarantined"] is False
    assert result["incident_stood_down"]["reasons"] == ["debugger_result_unconfirmed"]
    # The banner did print, and it confirmed nothing.
    assert "Reset mode  : Software reset" in json.loads((tmp_path / result["log_path"]).read_text(encoding="utf-8"))["stdout"]


def test_stlink_requires_flash_address_for_bin_artifacts(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.bin"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x01\x02\x03\x04")
    config = load_config(str(write_config(tmp_path, debugger_type="stlink")))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "flash_firmware", {"image_path": "build/firmware.bin"})
    finally:
        service.close()
    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    assert "debuggers.<name>.flash_address" in result["summary"]


# What each backend puts on the wire for each mode it supports, which is the
# only place the three can be compared. `run` and `halt` are two different
# commands on both; `init` is OpenOCD's alone, and OPENOCD_RESET_COMMANDS above
# holds the third line neither of these two can write.
BACKEND_RESET_COMMANDS = {
    ("stlink", "run"): "-rst",
    ("stlink", "halt"): "-halt",
    ("pyocd", "run"): "--command reset",
    ("pyocd", "halt"): '--command "reset halt"',
}


def reset_probe_id(backend: str) -> str:
    return "STLINK123" if backend == "stlink" else "PYOCD123"


@pytest.mark.parametrize(("backend", "mode"), sorted(BACKEND_RESET_COMMANDS))
def test_each_supported_reset_mode_sends_its_own_command_on_every_backend(tmp_path: Path, backend: str, mode: str) -> None:
    """One command line per backend per mode, which is the only comparable thing.

    The mode defect survived three releases because nothing here compared the
    backends: the OpenOCD parametrization above pinned all three of its command
    lines, stlink and pyocd had no mode coverage at all, and `init` quietly
    borrowing the halt argument was invisible from inside either one. Two modes
    that share a command line within one backend now fail here, whichever pair
    it is.

    This asserts what went on the wire rather than `ok`, deliberately: what a
    mode reports is a separate question from which command it sends, and the two
    failed separately - stlink asked for the `-rst` confirmation lines whichever
    mode ran, so `halt` sent the right command and could still never report
    success (#142). That one is answered by
    test_stlink_confirms_each_reset_mode_on_the_lines_its_own_command_prints;
    what this test has to hold is that the argument reaching the CLI is the one
    the mode names."""
    config = load_config(str(write_config(tmp_path, debugger_type=backend, probe_id=reset_probe_id(backend))))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "reset_target", {"mode": mode})
    finally:
        service.close()

    assert result["mode"] == mode
    logged = json.loads((tmp_path / result["log_path"]).read_text(encoding="utf-8"))["command"]
    assert BACKEND_RESET_COMMANDS[(backend, mode)] in logged
    other = "halt" if mode == "run" else "run"
    assert BACKEND_RESET_COMMANDS[(backend, other)] not in logged


@pytest.mark.parametrize("backend", ["stlink", "pyocd"])
def test_reset_init_is_refused_where_it_cannot_run_instead_of_halting_quietly(tmp_path: Path, backend: str) -> None:
    """The effect that changed, not the string that changed.

    Through 0.8.0 this call spawned the backend's CLI with the same plain halt
    argument mode `halt` sends, so the reset-init script a caller asked for -
    clock tree, wait states, watchdog - never ran on either of these two, and on
    pyocd the result said `Target reset with mode 'init'.` over it. The
    assertion that matters is therefore not the error_type but the empty log
    directory: no process ran at all, and the board is where the last call that
    did reach it left it. The `halt` that follows still reaches the CLI, which
    is what separates a refusal of one mode from a backend that stopped
    working."""
    config = load_config(str(write_config(tmp_path, debugger_type=backend, probe_id=reset_probe_id(backend))))
    service = AgenticHILToolService(config)
    try:
        refused = mcp_tool_call(service, "reset_target", {"mode": "init"})
        logs_after_refusal = sorted(Path(logs_directory(config)).glob("*"))
        still_reaches_the_cli = mcp_tool_call(service, "reset_target", {"mode": "halt"})
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "not_supported"
    assert refused["mode"] == "init"
    assert refused["supported_modes"] == ["run", "halt"]
    assert "OpenOCD" in refused["summary"]
    # Nothing was sent: the refusal precedes the spawn, so it has no log to name.
    assert "log_path" not in refused, refused
    assert logs_after_refusal == []
    # A refusal the caller may act on, not a bench somebody has to go and look at.
    assert refused["target_contacted"] is False
    assert refused["side_effect_committed"] is False
    assert refused["side_effect_status"] == "not_started"
    assert refused["hardware_state"] == "unchanged"
    assert refused.get("quarantined") is not True, refused
    assert refused.get("cleanup_required") is not True, refused
    logged = json.loads((tmp_path / still_reaches_the_cli["log_path"]).read_text(encoding="utf-8"))["command"]
    assert BACKEND_RESET_COMMANDS[(backend, "halt")] in logged


def test_the_reset_mode_schema_tells_a_caller_init_is_openocd_only(tmp_path: Path) -> None:
    """The schema is where an agent reads the enum, so it is where the caveat goes.

    A backend that refuses `init` and a schema that offers all three values
    without comment is the same defect one layer up: the caller still has to
    call to find out."""
    schema = next(tool for tool in MCP_TOOLS if tool["name"] == "reset_target")["inputSchema"]
    described = schema["properties"]["mode"]["description"]

    assert schema["properties"]["mode"]["enum"] == ["run", "halt", "init"]
    assert "OpenOCD-only" in described
    assert "not_supported" in described
    assert "reset-init" in described


def test_artifact_validation_computes_sha256(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    data = b"\x7fELFfake"
    firmware.write_bytes(data)
    result = ArtifactManager(config).validate_local_path("build/firmware.elf")
    assert result["ok"] is True
    assert result["artifact"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["validation"]["sha256_computed"] is True


def test_artifact_validation_blocks_outside_root(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    firmware = tmp_path / "other" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELF")
    result = ArtifactManager(config).validate_local_path("other/firmware.elf")
    assert result["ok"] is False
    assert result["error_type"] == "artifact_validation_failed"


def test_skill_install_supports_agent_aliases() -> None:
    target = Path.home() / "skills" / "agentic-hil" / "SKILL.md"
    result = install_skill("open-code", str(target))
    assert result["ok"] is True
    assert result["agent"] == "opencode"
    assert "agentic_hil_version" in target.read_text(encoding="utf-8")


def _legacy_skill(name: str, managed: bool) -> Path:
    path = Path.home() / ".claude" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    origin = "  origin: Agentic HIL\n" if managed else "  origin: someone else\n"
    # Bytes, not write_text: the installer never translates newlines, so a file
    # it wrote has LF endings on every platform.
    path.write_bytes(f"---\nname: {name}\nmetadata:\n{origin}---\n\nOlder copy.\n".encode())
    return path


def test_skill_install_removes_the_name_it_superseded() -> None:
    # Two installed skills for one job compete: the agent picks by name.
    legacy = _legacy_skill("agentic-hil-config-setup", managed=True)

    result = install_skill("claude-code")

    assert result["ok"] is True
    assert not legacy.exists()
    assert not legacy.parent.exists()
    assert result["legacy_skills_removed"] == [str(legacy)]
    assert (Path.home() / ".claude" / "skills" / "agentic-hil" / "SKILL.md").is_file()


def test_skill_install_removes_the_lock_an_earlier_release_left_behind() -> None:
    # The previous release locked its skill, and the sidecar outlives the
    # transaction, so without removing it the directory can never be empty.
    legacy = _legacy_skill("agentic-hil-config-setup", managed=True)
    sidecar = user_file_lock_path(legacy)
    sidecar.write_bytes(b"")

    install_skill("claude-code")

    assert not sidecar.exists()
    assert not legacy.parent.exists()


def test_skill_install_does_not_plant_the_directory_it_removes() -> None:
    # Probing through the validated read would create the directory chain, so a
    # machine that never had the old release would end up with it anyway.
    install_skill("claude-code")

    assert not (Path.home() / ".claude" / "skills" / "agentic-hil-config-setup").exists()


def test_skill_install_ignores_a_foreign_directory_at_the_old_name() -> None:
    # Whatever occupies the superseded name must not become a precondition of
    # installing under the current one.
    stranger = Path.home() / ".claude" / "skills" / "agentic-hil-config-setup"
    stranger.mkdir(parents=True)
    (stranger / "notes.txt").write_bytes(b"someone else's file\n")

    result = install_skill("claude-code")

    assert result["ok"] is True
    assert (stranger / "notes.txt").read_bytes() == b"someone else's file\n"


def test_skill_install_keeps_a_foreign_skill_that_uses_an_old_name() -> None:
    # Only a file this project wrote may be removed; the name alone is no proof.
    foreign = _legacy_skill("agentic-hil-config-setup", managed=False)
    existing = foreign.read_text(encoding="utf-8")

    result = install_skill("claude-code")

    assert result["ok"] is True
    assert "legacy_skills_removed" not in result
    assert foreign.read_text(encoding="utf-8") == existing


def test_skill_install_force_preserves_unmanaged_skill_and_reports_conflict() -> None:
    target = Path.home() / ".claude" / "skills" / "agentic-hil" / "SKILL.md"
    target.parent.mkdir(parents=True)
    existing = "---\nname: unrelated-user-skill\n---\nKeep this content.\n"
    target.write_text(existing, encoding="utf-8")

    result = install_skill("claude-code", force=True)

    assert result["ok"] is False
    assert result["error_type"] == "skill_conflict"
    assert target.read_text(encoding="utf-8") == existing


def test_codex_skill_update_rolls_back_skill_and_registration_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert install_skill("codex")["ok"] is True
    skill_path = Path.home() / ".codex" / "skills" / "agentic-hil" / "SKILL.md"
    agents_path = Path.home() / ".codex" / "AGENTS.md"
    current_skill = skill_path.read_text(encoding="utf-8")
    old_skill = re.sub(r'agentic_hil_version: "[^"]+"', 'agentic_hil_version: "0.0.0"', current_skill)
    assert old_skill != current_skill
    skill_path.write_bytes(old_skill.encode("utf-8"))
    current_agents = agents_path.read_text(encoding="utf-8")
    old_agents = re.sub(r"Agentic HIL version: `[^`]+`", "Agentic HIL version: `0.0.0`", current_agents)
    assert old_agents != current_agents
    agents_path.write_bytes(old_agents.encode("utf-8"))

    from agentic_hil import cli as cli_module

    def fail_registration_write(*_args: object, **_kwargs: object) -> dict:
        assert skill_path.read_text(encoding="utf-8") == current_skill
        raise ConfigError("injected_failure", "registration write failed")

    monkeypatch.setattr(cli_module, "upsert_marked_block", fail_registration_write)

    with pytest.raises(ConfigError, match="registration write failed"):
        install_skill("codex")

    assert skill_path.read_text(encoding="utf-8") == old_skill
    assert agents_path.read_text(encoding="utf-8") == old_agents


def test_load_config_reports_unreadable_path_as_config_error(tmp_path: Path) -> None:
    config_path = tmp_path / ".agentic-hil" / "config.yaml"
    config_path.mkdir(parents=True)  # a directory passes exists() but cannot be read
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(config_path))
    assert excinfo.value.error_type == "config_unreadable"


def test_load_config_reports_non_utf8_file_as_config_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(b"\xff\xfe\x00 broken")
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(config_path))
    assert excinfo.value.error_type == "config_invalid"


def test_mcp_tool_registry_is_consistent(tmp_path: Path) -> None:
    assert [tool["name"] for tool in MCP_TOOLS] == MCP_TOOL_NAMES
    assert len(MCP_TOOL_NAMES) == 42
    assert len(set(MCP_TOOL_NAMES)) == 42
    assert all(not name.startswith("agentic_hil_") for name in MCP_TOOL_NAMES)
    # The install eval asserts the live tools/list against this snapshot, so a
    # tool added or removed here has to reach it or every eval run fails on a
    # contract mismatch that has nothing to do with the change under test.
    snapshot = (Path(__file__).resolve().parents[1] / "evals" / "install" / "tools.list.expected").read_text(encoding="utf-8").split()
    assert snapshot == sorted(MCP_TOOL_NAMES)
    config = load_config(str(write_config(tmp_path)))
    service = AgenticHILToolService(config)
    try:
        for name in MCP_TOOL_NAMES:
            result = service.call(name, {})
            assert result.get("error_type") != "unknown_tool", f"{name} is advertised but not dispatched"
    finally:
        service.close()


def test_setup_is_cli_only_and_cannot_be_called_over_mcp(tmp_path: Path) -> None:
    assert "setup" not in MCP_TOOL_NAMES
    config = load_config(str(write_config(tmp_path)))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "setup")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "unknown_tool"


def test_mcp_initialize_rejects_unsupported_protocol_version(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    service = AgenticHILToolService(config)
    try:
        response = handle_mcp_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
            service,
        )
    finally:
        service.close()
    assert isinstance(response, dict)
    assert response["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION


def test_com_write_rejects_unencodable_text(tmp_path: Path) -> None:
    config = load_config(
        str(
            write_config(
                tmp_path,
                com_ports_yaml='''com_ports:
  dut_uart:
    device: "/dev/ttyNONEXISTENT"
    encoding: "ascii"
''',
            )
        ),
    )
    service = ComPortService(config)
    try:
        result = service.write("dut_uart", {"text": "Temperatur: 25 °C"})
    finally:
        service.close()
    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"


def spawn_ignoring_bridge_child() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        output = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout
        return str(pid).encode("ascii") in output
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup regression")
def test_process_cleanup_kills_sigterm_ignoring_descendant(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    code = """
import signal, subprocess, sys, time
descendant = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])
open(sys.argv[1], 'w', encoding='utf-8').write(str(descendant.pid))
signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
time.sleep(30)
"""
    child = spawn_managed_process([sys.executable, "-c", code, str(pid_file)])
    try:
        for _ in range(100):
            if pid_file.exists():
                break
            time.sleep(0.01)
        descendant_pid = int(pid_file.read_text(encoding="utf-8"))

        terminate_process_tree(child, 1.0)

        assert child.poll() is not None
        assert not pid_alive(descendant_pid)
    finally:
        if child.poll() is None:
            child.kill()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup regression")
def test_process_cleanup_handles_exited_group_leader(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    code = """
import signal, subprocess, sys
descendant = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])
open(sys.argv[1], 'w', encoding='utf-8').write(str(descendant.pid))
"""
    child = spawn_managed_process([sys.executable, "-c", code, str(pid_file)])
    try:
        child.wait(timeout=5)
        descendant_pid = int(pid_file.read_text(encoding="utf-8"))

        terminate_process_tree(child, 1.0)

        assert not pid_alive(descendant_pid)
    finally:
        if child.poll() is None:
            child.kill()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
def test_registered_windows_process_cleanup_kills_descendant_after_leader_exit(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    code = """
import subprocess, sys
descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
open(sys.argv[1], 'w', encoding='utf-8').write(str(descendant.pid))
"""
    child = spawn_managed_process([sys.executable, "-c", code, str(pid_file)])
    child.wait(timeout=5)
    descendant_pid = int(pid_file.read_text(encoding="utf-8"))

    terminate_process_tree(child, 1.0)

    assert not pid_alive(descendant_pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended-start regression")
def test_windows_managed_spawn_holds_child_until_contained_then_runs_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The suspended start is still there, and it still ends in a running child.

    Both halves in one call means neither is observable from outside any more,
    so the freeze is caught where it happens: at Job Object setup the child must
    not have run a line yet, and by the time the call returns it must have.
    """
    marker = tmp_path / "started"
    original = process_module._create_windows_kill_job
    frozen_at_containment: list[bool] = []

    def watched(process: subprocess.Popen) -> int:
        frozen_at_containment.append(not marker.exists())
        return original(process)

    monkeypatch.setattr("agentic_hil.process._create_windows_kill_job", watched)
    child = spawn_managed_process([sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started')", str(marker)])
    try:
        child.wait(timeout=5)
        assert frozen_at_containment == [True]
        assert marker.read_text(encoding="utf-8") == "started"
        terminate_process_tree(child, 1.0)
    finally:
        if child.poll() is None:
            child.kill()


@pytest.mark.skipif(os.name != "nt", reason="Windows fail-closed registration regression")
def test_windows_job_setup_failure_terminates_suspended_child(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[subprocess.Popen] = []

    def fail(process: subprocess.Popen) -> int:
        spawned.append(process)
        raise OSError("job setup failed")

    monkeypatch.setattr("agentic_hil.process._create_windows_kill_job", fail)

    with pytest.raises(OSError, match="job setup failed"):
        spawn_managed_process([sys.executable, "-c", "import time; time.sleep(30)"])

    assert len(spawned) == 1
    assert spawned[0].poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows missing Job Object regression")
def test_windows_missing_job_handle_terminates_suspended_child(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[subprocess.Popen] = []

    def no_handle(process: subprocess.Popen) -> None:
        spawned.append(process)
        return None

    monkeypatch.setattr("agentic_hil.process._create_windows_kill_job", no_handle)

    with pytest.raises(OSError, match="no containment handle"):
        spawn_managed_process([sys.executable, "-c", "import time; time.sleep(30)"])

    assert len(spawned) == 1
    assert spawned[0].poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows console inheritance decision")
def test_a_detached_child_keeps_a_hidden_console_for_its_own_children() -> None:
    # DETACHED_PROCESS would give the child no console at all, and Windows then
    # allocates every console-subsystem grandchild a fresh visible window: a
    # detached test run popped one console per spawned backend over the
    # operator's desktop. A hidden console is inherited silently instead.
    from agentic_hil.process import _detached_process_kwargs

    flags = _detached_process_kwargs()["creationflags"]
    assert flags & subprocess.CREATE_NO_WINDOW
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert not flags & subprocess.DETACHED_PROCESS


SPAWNER_SOURCE = """
import subprocess, sys
from agentic_hil.process import {function}
child = {function}(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
open(sys.argv[1], "w", encoding="utf-8").write(str(child.pid))
"""


def grandchild_pid_after_spawner_exit(function: str, pid_file: Path) -> int:
    """Start a child through ``function`` from a process that then exits, and report its pid.

    The spawner is a plain ``subprocess.Popen`` on purpose: it must belong to no
    Job Object of this test's making, so the only containment in the picture is
    the one ``function`` itself creates.

    Two details are the difference between a proof and a hang. The grandchild's
    standard streams go to ``DEVNULL``, because on POSIX a child inherits its
    parent's fds 0-2 and would hold the write end of any pipe open for its whole
    life — leaving this reader blocked on an EOF that only arrives once the
    process it is asking about has died. It is the same reason
    :func:`agentic_hil.canbroker._spawn_broker` hands the broker a log file
    rather than a pipe. And the pid travels through a file rather than that
    stream, so nothing here depends on a descriptor the grandchild shares.
    """
    spawner = subprocess.Popen([sys.executable, "-c", SPAWNER_SOURCE.format(function=function), str(pid_file)])
    assert spawner.wait(timeout=60) == 0
    return int(pid_file.read_text(encoding="utf-8"))


def pid_alive_after_settling(pid: int, *, awaiting: bool, timeout_s: float = 5.0) -> bool:
    """Poll until the pid reaches ``awaiting`` or the deadline passes, then answer.

    Waiting for the answer the caller is *not* asserting is the point: a child
    that was going to die has the full window in which to do it, so "still
    alive" is a proof rather than a race won.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        alive = pid_alive(pid)
        if alive == awaiting or time.monotonic() >= deadline:
            return alive
        time.sleep(0.05)


def kill_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return
    with suppress(ProcessLookupError):
        os.kill(pid, 9)


def test_managed_spawn_hands_back_a_child_that_is_already_running(tmp_path: Path) -> None:
    """The regression this surface exists for: the caller owes no second call.

    On Windows the child is born suspended, so a spawn that forgot to resume it
    would sit here until the wait timed out — alive, silent, nothing to read.
    """
    marker = tmp_path / "ran"
    child = spawn_managed_process([sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')", str(marker)])
    assert child.wait(timeout=10) == 0
    assert marker.read_text(encoding="utf-8") == "ran"
    terminate_process_tree(child, 5.0)


def test_detached_spawn_child_outlives_the_process_that_started_it(tmp_path: Path) -> None:
    """What the CAN broker needs: a bus that does not end with the first run to attach."""
    pid = grandchild_pid_after_spawner_exit("spawn_detached_process", tmp_path / "detached.pid")
    try:
        assert pid_alive_after_settling(pid, awaiting=False) is True
    finally:
        kill_pid(pid)


@pytest.mark.skipif(os.name != "nt", reason="Job Object containment is what the detached variant opts out of")
def test_managed_spawn_child_dies_with_the_process_that_started_it(tmp_path: Path) -> None:
    """The contrast that gives the detached variant its meaning: same shape, opposite lifetime."""
    pid = grandchild_pid_after_spawner_exit("spawn_managed_process", tmp_path / "managed.pid")
    try:
        assert pid_alive_after_settling(pid, awaiting=False) is False
    finally:
        kill_pid(pid)


def test_the_spawn_contract_has_no_half_a_caller_can_hold() -> None:
    """Neither half is reachable by the name it used to have."""
    assert not hasattr(process_module, "process_group_kwargs")
    assert not hasattr(process_module, "register_process_group")


def test_registration_refuses_a_child_it_did_not_spawn() -> None:
    """Reaching past the underscore does not get the old two-step back either."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(RuntimeError, match="not a standalone step"):
            process_module._register_process_group(child)
    finally:
        child.kill()
        child.wait(timeout=5)


@pytest.mark.parametrize("function", ["spawn_managed_process", "spawn_detached_process"])
@pytest.mark.parametrize(("keyword", "value"), [("creationflags", 0), ("start_new_session", True)])
def test_spawn_refuses_caller_supplied_creation_flags(function: str, keyword: str, value: object) -> None:
    """The flags are the contract, so a caller cannot re-open the hole from outside."""
    with pytest.raises(TypeError, match="spawn contract"):
        getattr(process_module, function)([sys.executable, "-c", "pass"], **{keyword: value})


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object verification regression")
def test_windows_job_cleanup_reports_remaining_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    child = SimpleNamespace(_agentic_hil_job_handle=123, wait=lambda timeout: 0, poll=lambda: 0)
    closed: list[int] = []
    monkeypatch.setattr("agentic_hil.process._terminate_windows_job", lambda handle: None)
    monkeypatch.setattr("agentic_hil.process._wait_for_windows_job", lambda handle, timeout: False)
    monkeypatch.setattr("agentic_hil.process._close_windows_handle", lambda handle: closed.append(handle))

    with pytest.raises(RuntimeError, match="retained active processes"):
        terminate_process_tree(child, 0.1)  # type: ignore[arg-type]

    assert closed == []
    assert child._agentic_hil_job_handle == 123

    monkeypatch.setattr("agentic_hil.process._wait_for_windows_job", lambda handle, timeout: True)
    terminate_process_tree(child, 0.1)  # type: ignore[arg-type]

    assert closed == [123]
    assert child._agentic_hil_job_handle is None
    assert child._agentic_hil_tree_reaped is True


def test_process_can_adapter_close_reaps_child() -> None:
    child = spawn_ignoring_bridge_child()
    session = ProcessCanAdapterSession(child)
    from agentic_hil.bridge import BridgeCleanupError

    with pytest.raises(BridgeCleanupError) as excinfo:
        session.close()
    assert excinfo.value.result["safe_state_confirmed"] is False
    assert child.poll() is not None


def test_process_can_adapter_request_after_exit_returns_error() -> None:
    child = spawn_ignoring_bridge_child()
    session = ProcessCanAdapterSession(child)
    from agentic_hil.bridge import BridgeCleanupError

    with pytest.raises(BridgeCleanupError):
        session.close()
    result = session.send(CanFrame(id=1, extended=False, rtr=False, data=b""))
    assert result["ok"] is False


def test_process_can_adapter_read_allows_bridge_response_overhead(monkeypatch: pytest.MonkeyPatch) -> None:
    session = object.__new__(ProcessCanAdapterSession)
    session.timeout_s = 5.0
    captured: dict[str, object] = {}

    def fake_request(method: str, params: dict, timeout_s: float) -> dict:
        captured.update({"method": method, "params": params, "timeout_s": timeout_s})
        return {"ok": True, "frames": []}

    monkeypatch.setattr(session, "request", fake_request)

    result = session.read(max_frames=8, wait_timeout_s=5.0)

    assert result["ok"] is True
    assert captured["timeout_s"] == 6.0


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only PEAK channel validation")
def test_peak_adapter_on_posix_requires_socketcan_channel(tmp_path: Path) -> None:
    config = load_config(
        str(
            write_config(
                tmp_path,
                can_buses_yaml='''can_buses:
  dut_can:
    adapter: "peak"
    channel: "USBBUS1"
''',
            )
        ),
    )
    result = open_python_can_adapter(config, "dut_can", config.can_buses["dut_can"], True)
    assert result["ok"] is False
    assert result["error_type"] == "config_invalid"


@pytest.mark.parametrize("command", ["init", "doctor", "mcp-stdio", "com-stdio"])
def test_legacy_cli_config_option_is_parsed(command: str) -> None:
    arguments = [command, "--config", "legacy.yaml"]
    if command == "com-stdio":
        arguments.extend(["--port", "dut"])

    parsed = build_parser().parse_args(arguments)

    assert parsed.config == "legacy.yaml"


def test_doctor_checks_every_probe_that_grants_allow_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A multi-board bench is exactly where a green "skipped" doctor would hide
    # an unplugged board or a wrong probe_id, so every granted probe is checked.
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_name="board_a",
        probe_id="PROBE-A",
        debuggers_yaml=(
            'debuggers:\n  board_b:\n    type: openocd\n    probe_id: "PROBE-B"\n'
            f'    executable: "{FAKE_OPENOCD.as_posix()}"\n'
        ),
    )
    monkeypatch.chdir(workspace)

    result = doctor()

    assert sorted(result["debuggers"]) == ["board_a", "board_b"]
    assert result["debuggers"]["board_a"]["check"]["tool"] == "debugger_info"
    assert result["debuggers"]["board_b"]["check"]["tool"] == "debugger_info"
    assert "skipped" not in result["summary"]


def test_doctor_skips_only_when_no_probe_grants_allow_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, permissions={})
    monkeypatch.chdir(workspace)

    result = doctor()

    assert result["ok"] is True
    assert "skipped" in result["summary"]
    assert "check" not in result["debuggers"]["dut"]


def test_debugger_probes_enumerates_every_granted_probe_when_none_is_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Probe discovery is how an operator finds the serials a multi-board config
    # demands, so it has to work in exactly the config that has no bound probe.
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_type="stlink",
        debugger_name="board_a",
        probe_id="PROBE-A",
        debuggers_yaml=(
            'debuggers:\n  board_b:\n    type: stlink\n    probe_id: "PROBE-B"\n'
            f'    executable: "{FAKE_STLINK.as_posix()}"\n'
        ),
    )
    monkeypatch.chdir(workspace)

    result = debugger_probes()

    assert result["error_type"] if result.get("ok") is False else True
    assert sorted(result["debuggers"]) == ["board_a", "board_b"]
    assert result["debuggers"]["board_a"]["tool"] == "debugger_probes_list"
    assert result["debuggers"]["board_a"].get("error_type") != "not_supported"
