from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import (
    FAKE_OPENOCD,
    FAKE_STLINK,
    FAKE_STLINK_UNCONFIRMED,
    write_authoritative_config,
    write_config,
)
from support import trusted_launcher

from agentic_hil import __version__
from agentic_hil.artifacts import ArtifactManager
from agentic_hil.backends.pyocd import parse_pyocd_probes
from agentic_hil.backends.stlink import stlink_empty_result, stlink_probe_ids
from agentic_hil.can import CanFrame, ProcessCanAdapterSession, open_python_can_adapter
from agentic_hil.cli import (
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
    register_agent_mcp,
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
    project_config_path,
    tighten_owned_writable_ancestors,
    trusted_persistent_executable,
    user_file_lock_path,
    user_state_root,
)
from agentic_hil.mcp import MCP_PROTOCOL_VERSION, MCP_TOOL_NAMES, MCP_TOOLS, handle_mcp_message
from agentic_hil.process import process_group_kwargs, register_process_group, terminate_process_tree
from agentic_hil.tools import AgenticHILToolService, UnprovisionedToolService


def mcp_tool_call(service: AgenticHILToolService, name: str, arguments: dict | None = None) -> dict:
    response = handle_mcp_message(
        {"jsonrpc": "2.0", "id": name, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}},
        service,
    )
    assert isinstance(response, dict)
    return response["result"]["structuredContent"]


def test_init_config_writes_deterministic_deny_by_default_external_config(
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
    # permission line a fresh config carries is about writing.
    assert "version: 2" in config_text
    assert "allow_probe: " not in config_text
    assert "allow_flash: false" in config_text
    assert "allow_reset: false" in config_text
    assert "allow_upload: false" in config_text
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
    monkeypatch.setattr("agentic_hil.cli.shutil.which", lambda name: None if name == "claude" else real_which(name))

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


def test_upgrade_uses_running_python_and_refreshes_skill_from_updated_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str | None]] = []

    def run(command: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "9.9.9\n", "")
        if "skill-install" in command:
            return subprocess.CompletedProcess(command, 0, '{"ok": true}\n', "")
        return subprocess.CompletedProcess(command, 0, "installed\n", "")

    monkeypatch.setattr("agentic_hil.cli._upgrade_command", lambda: ("pip", [sys.executable, "-m", "pip", "install", "--upgrade", "agentic-hil"]))
    monkeypatch.setattr("agentic_hil.cli._run_upgrade_process", run)

    result = upgrade_installation(["opencode"])

    assert result["ok"] is True
    assert result["version"] == "9.9.9"
    assert result["restart_required"] is True
    assert calls[0][0][:3] == [sys.executable, "-m", "pip"]
    assert calls[1] == ([sys.executable, "-m", "agentic_hil", "--version"], None)
    assert calls[2][0] == [sys.executable, "-m", "agentic_hil", "skill-install", "--agent", "opencode"]
    assert calls[2][1] is not None


def test_upgrade_stops_before_skill_refresh_when_package_manager_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = [sys.executable, "-m", "pip", "install", "--upgrade", "agentic-hil"]
    monkeypatch.setattr("agentic_hil.cli._upgrade_command", lambda: ("pip", command))
    monkeypatch.setattr(
        "agentic_hil.cli._run_upgrade_process",
        lambda invoked, **_kwargs: subprocess.CompletedProcess(invoked, 1, "", "network failed"),
    )

    result = upgrade_installation(["opencode"])

    assert result["ok"] is False
    assert result["error_type"] == "upgrade_failed"
    assert result["install"]["stderr"] == "network failed"


def test_upgrade_rejects_unknown_agent_before_mutating_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentic_hil.cli._run_upgrade_process", lambda *_args, **_kwargs: pytest.fail("must not run"))

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
    from agentic_hil.cli import _upgrade_command

    monkeypatch.setattr(sys, "prefix", prefix)
    monkeypatch.setattr(sys, "executable", "PYTHON")
    monkeypatch.setattr("agentic_hil.cli._distribution_installer", lambda: installer)
    monkeypatch.setattr("agentic_hil.cli.shutil.which", lambda name: f"{name}.exe")

    selected_manager, command = _upgrade_command()

    assert selected_manager == manager
    assert command == expected


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
    """Stand in for the profile of hardci-hq#64.

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


def test_agent_install_completes_where_the_config_location_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hardci-hq#64: a refused config location is not the skill's problem.

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
    monkeypatch.setattr("agentic_hil.cli.shutil.which", lambda name: None)
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
    # Reporting it is not the same as failing: an install may simply not have
    # happened yet, and doctor's verdict is about the hardware configuration.
    assert result["ok"] is True


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


def test_skill_routes_firmware_work_to_agentic_hil() -> None:
    text = _packaged_skill_text()

    for tool in ("flash_firmware", "reset_target", "probe_target", "com_read", "can_send", "debug_set_breakpoint"):
        assert tool in text, tool
    # The skill is worthless if it does not name what it replaces.
    for raw in ("openocd", "gdb", "minicom", "candump"):
        assert raw in text, raw


def test_skill_frontmatter_survives_a_windows_checkout() -> None:
    # git converts the packaged skill to CRLF on Windows unless .gitattributes
    # pins it; refusing to recognise it there would report this project's own
    # skill as a foreign one and stop setup.
    crlf = _packaged_skill_text().replace("\n", "\r\n")

    assert is_agentic_hil_setup_skill(crlf)
    # Against the package version, not against the same function's own output:
    # comparing two computed values passes when both are None.
    assert skill_version(crlf) == __version__


def test_plugin_skill_carries_the_packaged_guidance() -> None:
    # A plugin install is an alternative to setup, not a lesser one: it has to
    # deliver the same routing rules, so the bodies may not drift apart.
    packaged = _packaged_skill_text()
    plugin_path = Path(__file__).resolve().parents[1] / "plugins" / "agentic-hil" / "skills" / "agentic-hil" / "SKILL.md"
    plugin = plugin_path.read_text(encoding="utf-8")
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
    config_directory = Path(result["steps"]["config"]["path"]).parent.as_posix()
    assert f"Edit({config_directory}/**)" in deny
    assert f"Write({config_directory}/**)" in deny
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


def test_skill_only_names_tools_the_server_exposes() -> None:
    contract = Path(__file__).resolve().parents[1] / "evals" / "install" / "tools.list.expected"
    exposed = set(contract.read_text(encoding="utf-8").split())
    # Underscored identifiers in backticks are tool names; these are the import
    # name and two error types, not tools.
    not_a_tool = {"agentic_hil", "permission_denied", "config_file_not_found"}

    referenced = {
        token
        for token in re.findall(r"`([a-z][a-z0-9_]*)`", _packaged_skill_text())
        if "_" in token
    } - not_a_tool

    assert referenced, "the skill names no tools at all"
    assert referenced <= exposed, sorted(referenced - exposed)


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


def test_default_mcp_path_is_rejected_when_home_is_inside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("HOME", str(workspace))
    monkeypatch.setenv("USERPROFILE", str(workspace))

    with pytest.raises(ConfigError) as excinfo:
        register_agent_mcp("claude-code", command=str(trusted_launcher()))

    assert excinfo.value.error_type == "unsafe_configured_path"
    assert not (workspace / ".claude.json").exists()


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_register_agent_mcp_rejects_group_writable_user_config_without_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_workspace(tmp_path, monkeypatch)
    home = _isolated_home(tmp_path, monkeypatch)
    path = home / ".claude.json"
    existing = '{"mcpServers": {"custom": {}}}'
    path.write_text(existing, encoding="utf-8")
    path.chmod(0o660)

    with pytest.raises(ConfigError) as excinfo:
        register_agent_mcp("claude-code", command=str(trusted_launcher()))

    assert excinfo.value.error_type == "unsafe_configured_path"
    assert path.read_text(encoding="utf-8") == existing


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
def test_setup_smooths_group_writable_home_and_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_config_environment: Path,
) -> None:
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
    assert all(directory.stat().st_mode & 0o020 for directory in writable)

    result = setup_project(agent="codex")

    assert result["ok"] is True, result
    assert result["steps"]["mcp_config"]["ok"] is True
    assert result["permission_changes"], "setup should report the tightened directories"
    for directory in writable:
        assert not (directory.stat().st_mode & 0o022), f"{directory} still group/other-writable"
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
    assert len(MCP_TOOL_NAMES) == 33
    assert len(set(MCP_TOOL_NAMES)) == 33
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
    child = register_process_group(subprocess.Popen([sys.executable, "-c", code, str(pid_file)], **process_group_kwargs()))
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
    child = register_process_group(subprocess.Popen([sys.executable, "-c", code, str(pid_file)], **process_group_kwargs()))
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
    child = register_process_group(subprocess.Popen([sys.executable, "-c", code, str(pid_file)], **process_group_kwargs()))
    child.wait(timeout=5)
    descendant_pid = int(pid_file.read_text(encoding="utf-8"))

    terminate_process_tree(child, 1.0)

    assert not pid_alive(descendant_pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended-start regression")
def test_windows_process_does_not_run_before_job_registration(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    child = subprocess.Popen([sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started')", str(marker)], **process_group_kwargs())
    try:
        time.sleep(0.1)
        assert not marker.exists()
        register_process_group(child)
        child.wait(timeout=5)
        assert marker.read_text(encoding="utf-8") == "started"
        terminate_process_tree(child, 1.0)
    finally:
        if child.poll() is None:
            child.kill()


@pytest.mark.skipif(os.name != "nt", reason="Windows fail-closed registration regression")
def test_windows_job_setup_failure_terminates_suspended_child(monkeypatch: pytest.MonkeyPatch) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **process_group_kwargs())
    monkeypatch.setattr("agentic_hil.process._create_windows_kill_job", lambda process: (_ for _ in ()).throw(OSError("job setup failed")))

    with pytest.raises(OSError, match="job setup failed"):
        register_process_group(child)

    assert child.poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows missing Job Object regression")
def test_windows_missing_job_handle_terminates_suspended_child(monkeypatch: pytest.MonkeyPatch) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **process_group_kwargs())
    monkeypatch.setattr("agentic_hil.process._create_windows_kill_job", lambda process: None)

    with pytest.raises(OSError, match="no containment handle"):
        register_process_group(child)

    assert child.poll() is not None


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
