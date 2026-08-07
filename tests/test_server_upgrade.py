"""Upgrading over MCP: what it may do, what it may not, and what it may claim.

`server_upgrade` reverses the CLI-only rule. Upgrading was CLI-only —
the command was named and never run — and on a host that is only an MCP client
there is then no way at all for the main surface to perform the basic maintenance
of its own server. The tool exists now, and it keeps three teeth:

*It only goes forward.* There is no argument that names a version, so the
permission ratchet cannot be walked around through the code: an agent that could
install an older release could install one that reads `permissions:` under weaker
rules.

*It is honest about the restart.* Replacing files on disk does not change what an
already-running interpreter executes, so a success reports `running_version`
beside `version` and asks for the restart rather than claiming to be the new code.

*It refuses rather than half-acting.* While the bench is held, because those holds
were taken under the release being replaced; on a host that locks the files of a
running process, because there the swap cannot be completed at all; and on a
pinned installation, where it names the reinstall command and never runs it.

Nothing here runs a real package operation. The manager, the subprocess runner
and the installed-extras reading are all replaced, which is also what lets the
same tests exercise both sides of the platform branch on either platform.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import write_authoritative_config, write_config

from agentic_hil import __version__
from agentic_hil.config import load_config
from agentic_hil.contracts import MCP_TOOL_NAMES, MCP_TOOLS, TOOL_ANNOTATIONS, validate_tool_arguments
from agentic_hil.knowledge import ERROR_CATALOGUE
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import AgenticHILConfig
from agentic_hil.upgrade import SERVER_UPGRADE

SOCKETCAN_BUS = """can_buses:
  dut_can:
    adapter: "socketcan"
    channel: "can0"
    bitrate: 500000
"""

# The line `uv tool upgrade` writes on stderr beside exit code 0 when the
# installation it was asked to move is recorded with an exact pin — measured on
# the reporter's box, and the reason the pin is read out of the
# text rather than out of the return code.
UV_EXACT_PIN_HINT = (
    "hint: `agentic-hil` is pinned to `0.8.1` (installed with an exact version pin); "
    "reinstall with `uv tool install agentic-hil@latest` to upgrade to a new version."
)


def upgradable_config(
    tmp_path: Path,
    *,
    allow_upgrade: bool = True,
    can_buses_yaml: str = "can_buses: {}\n",
) -> AgenticHILConfig:
    """A version 2 configuration that states `permissions.allow_upgrade` outright.

    Stated rather than left out, in both directions: an absent block means the
    dataclass default, which is `False`, and a test that wants the refusal should
    ask for a file that denies the permission rather than for one that forgot to
    mention it."""
    path = write_config(tmp_path, config_version=2, can_buses_yaml=can_buses_yaml)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["permissions"] = {"allow_upgrade": allow_upgrade}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return load_config(str(path))


def fake_manager(
    monkeypatch: pytest.MonkeyPatch,
    *,
    installed: subprocess.CompletedProcess[str],
    version_after: str,
    extras: tuple[str, ...] = (),
) -> list[list[str]]:
    """One faked `uv tool upgrade`, and the list of commands it was asked to run.

    `_host_locks_running_files` is replaced along with it, because otherwise
    every one of these tests would exercise the Windows refusal on Windows and
    the manager path on Linux — two different suites wearing one name. The
    platform branch has a test of its own, on both sides."""
    calls: list[list[str]] = []

    def run(invoked: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(invoked)
        if invoked[-1] == "--version":
            return subprocess.CompletedProcess(invoked, 0, f"{version_after}\n", "")
        return installed

    monkeypatch.setattr("agentic_hil.upgrade._host_locks_running_files", lambda: False)
    monkeypatch.setattr("agentic_hil.upgrade._upgrade_command", lambda: ("uv", ["uv.exe", "tool", "upgrade", "agentic-hil"]))
    monkeypatch.setattr("agentic_hil.upgrade._processes_holding_installation", list)
    monkeypatch.setattr("agentic_hil.upgrade._installed_extras", lambda: extras)
    monkeypatch.setattr("agentic_hil.upgrade._run_upgrade_process", run)
    return calls


def never_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate that refuses must not have reached a package manager first."""
    monkeypatch.setattr("agentic_hil.upgrade._run_upgrade_process", lambda *_args, **_kwargs: pytest.fail("a refusal reached the package manager"))


# ---------------------------------------------------------------------------
# The refusals.


def test_the_configuration_can_close_this_tool_and_the_refusal_names_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`allow_upgrade: false` is the whole answer, and nothing is attempted.

    The point of a new permission is that an operator can say no to this one
    thing without giving up the rest of the surface, so the refusal has to name
    the key it came from — a `permission_denied` that does not is a search."""
    never_runs(monkeypatch)
    tools = AgenticHILToolService(upgradable_config(tmp_path, allow_upgrade=False))
    try:
        refused = tools.call(SERVER_UPGRADE)
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "permission_denied"
    assert refused["permission"] == "allow_upgrade"
    assert refused["running_version"] == __version__
    assert refused["side_effect_status"] == "not_started"
    assert refused["hardware_state"] == "unchanged"
    # The way out is a person's, and the result says which one: the command line
    # does the same job and answers to nobody's permission block.
    assert refused["remediation"] == list(ERROR_CATALOGUE["permission_denied:allow_upgrade"].remediation)
    assert any("agentic-hil upgrade" in step for step in refused["remediation"])
    assert any("uv tool upgrade" in step for step in refused["do_not"])


def test_the_upgrade_is_refused_while_a_run_holds_the_bench(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The open-run refusal, applied to the code instead of to the permissions.

    A declared run took its locks under the release that is running. Replacing
    that release mid-run moves the rules during the run they govern, which is the
    same objection as changing a permission underneath it — so the same answer,
    with an error type of its own because what is refused is not a write."""
    calls = fake_manager(monkeypatch, installed=subprocess.CompletedProcess([], 0, "installed\n", ""), version_after="9.9.9")
    tools = AgenticHILToolService(upgradable_config(tmp_path))
    try:
        assert tools.call("bench_run_start", {"devices": [{"kind": "debugger", "id": "dut"}], "label": "upgrade"})["ok"] is True

        refused = tools.call(SERVER_UPGRADE)

        assert refused["ok"] is False
        assert refused["error_type"] == "upgrade_in_open_run"
        assert refused["held_devices"], refused
        assert refused["running_version"] == __version__
        assert refused["restart_required"] is False
        assert refused["retry_safe"] is True
        assert refused["remediation"] == list(ERROR_CATALOGUE["upgrade_in_open_run"].remediation)
        # Refused before the manager, not after it: a refusal that had already
        # replaced the package would be a report of the thing it says it stopped.
        assert calls == []
        # And it is available again the moment the run is closed, which is what
        # `retry_safe` above promises.
        assert tools.call("bench_run_stop")["ok"] is True
        assert tools.call(SERVER_UPGRADE)["upgraded_on_disk"] is True
        assert calls
    finally:
        tools.close()


def test_a_host_that_locks_running_files_is_told_the_upgrade_is_the_command_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows answer, and it is a refusal rather than a deferred swap.

    A helper that replaced the files after this server exits would outlive the
    result that announced it: a failure would have nobody left to report to, and
    would leave the half-replaced environment the whole guard exists to prevent.
    So the tool says which command does it and does not pretend to have started
    anything — with the extras named, because the reader is about to reinstall.
    """
    never_runs(monkeypatch)
    monkeypatch.setattr("agentic_hil.upgrade._host_locks_running_files", lambda: True)
    monkeypatch.setattr("agentic_hil.upgrade._installed_extras", lambda: ("can",))
    tools = AgenticHILToolService(upgradable_config(tmp_path))
    try:
        refused = tools.call(SERVER_UPGRADE)
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "upgrade_cli_only_on_host"
    assert refused["upgrade_command"] == "agentic-hil upgrade"
    assert refused["installed_extras"] == ["can"]
    assert refused["running_version"] == __version__
    assert refused["restart_required"] is False
    assert refused["side_effect_status"] == "not_started"
    # Retrying is pointless here and the result says so: this is about the host,
    # not about what the bench happens to be doing.
    assert refused["retry_safe"] is False
    assert any("agentic-hil upgrade" in step for step in refused["remediation"])
    assert any("--force" in step for step in refused["do_not"])


def test_a_pinned_installation_is_refused_with_a_command_that_keeps_the_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI's pin case, reached through the MCP tool this time.

    `uv tool upgrade` exits 0 for a pin it cannot move and writes the reason on
    stderr, so success is decided by the version and not by the return code. The
    command offered has to carry the extras: `uv`'s own hint names the bare
    distribution, and following it takes `[can]` off a bench that has CAN buses.
    """
    fake_manager(
        monkeypatch,
        installed=subprocess.CompletedProcess([], 0, "", UV_EXACT_PIN_HINT),
        version_after=__version__,
        extras=("can",),
    )
    tools = AgenticHILToolService(upgradable_config(tmp_path))
    try:
        refused = tools.call(SERVER_UPGRADE)
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "upgrade_blocked_by_pin"
    assert refused["tool"] == SERVER_UPGRADE
    assert refused["pinned_version"] == "0.8.1"
    assert refused["installed_extras"] == ["can"]
    assert refused["reinstall_command"] == 'uv tool install "agentic-hil[can]@latest"'
    # Nothing moved, so nothing is to be restarted, and both version fields say
    # the same number — which is what makes this a refusal and not an upgrade.
    assert refused["restart_required"] is False
    assert refused["previous_version"] == refused["version"] == __version__
    assert refused["running_version"] == __version__


# ---------------------------------------------------------------------------
# The success, and what it may claim.


def test_a_swap_on_disk_names_both_versions_and_does_not_claim_to_be_running_the_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honesty tooth of this tool.

    Files were replaced; this interpreter has already imported the old ones and
    goes on executing them. A result that reported the new number as this
    server's would be a claim a host cannot check, so `running_version` is beside
    `version` and the restart is asked for rather than assumed."""
    calls = fake_manager(
        monkeypatch,
        installed=subprocess.CompletedProcess([], 0, "installed\n", ""),
        version_after="9.9.9",
    )
    tools = AgenticHILToolService(upgradable_config(tmp_path))
    try:
        result = tools.call(SERVER_UPGRADE)
    finally:
        tools.close()

    assert result["ok"] is True
    assert result["upgraded_on_disk"] is True
    assert result["previous_version"] == __version__
    assert result["version"] == "9.9.9"
    assert result["running_version"] == __version__
    assert result["restart_required"] is True
    assert result["manager"] == "uv"
    assert calls[0] == ["uv.exe", "tool", "upgrade", "agentic-hil"]
    # The summary is read by a person, so it has to carry the same fact the
    # fields do rather than leaving it to whoever notices the extra key.
    assert "9.9.9" in result["summary"] and f"still running {__version__}" in result["summary"]
    assert any("restart the MCP server" in step for step in result["next_steps"])
    # And the trap the wording exists to avoid: the command line is already on
    # the new code, so checking there proves nothing about this server.
    assert any("command line reads the new code" in step for step in result["next_steps"])


def test_an_installation_that_is_already_current_is_not_reported_as_an_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`uv tool upgrade` exits 0 with nothing to do, and this is that outcome.

    A success, because nothing is wrong — and not an upgrade, because nothing
    moved. No restart is asked for, which is the whole of what the CLI fix was
    about at the other end."""
    fake_manager(
        monkeypatch,
        installed=subprocess.CompletedProcess([], 0, "Nothing to upgrade\n", ""),
        version_after=__version__,
    )
    tools = AgenticHILToolService(upgradable_config(tmp_path))
    try:
        result = tools.call(SERVER_UPGRADE)
    finally:
        tools.close()

    assert result["ok"] is True
    assert result["already_current"] is True
    assert result["restart_required"] is False
    assert "upgraded_on_disk" not in result
    assert result["running_version"] == result["version"] == __version__


# ---------------------------------------------------------------------------
# Forward only, as a property of the schema rather than of the implementation.


def test_the_tool_offers_no_argument_that_could_choose_a_version(tmp_path: Path) -> None:
    """The ratchet's other half, pinned where a host can see it.

    A version argument would make every permission on this bench reachable
    through the code: an agent that can install an older release installs one
    that reads `permissions:` under the rules of its day. So the guarantee is
    the *absence* of a parameter, which is a property of the published schema
    and is checked as one — no properties at all, and additionalProperties
    false, so a caller that sends one anyway is refused rather than ignored."""
    schema = next(tool["inputSchema"] for tool in MCP_TOOLS if tool["name"] == SERVER_UPGRADE)

    assert schema["properties"] == {}
    assert schema["additionalProperties"] is False
    assert "required" not in schema
    for argument in ({"version": "0.7.1"}, {"target_version": "0.7.1"}, {"ref": "0.7.1"}, {"downgrade": True}):
        refused = validate_tool_arguments(SERVER_UPGRADE, argument)
        assert refused is not None, argument
        assert refused["error_type"] == "invalid_argument", argument
    # And the description does not offer one either, because the first place a
    # caller looks for a parameter is the sentence describing the tool.
    description = next(str(tool["description"]) for tool in MCP_TOOLS if tool["name"] == SERVER_UPGRADE)
    assert "no arguments" in description


def test_the_upgrade_is_the_one_tool_that_reaches_the_open_world(tmp_path: Path) -> None:
    """`openWorldHint` was false on every tool, and now it is true on exactly one.

    Not a relaxation of the rule but the first case that meets it: what this
    installs comes off a package index over the network, from an entity outside
    this machine and outside the configuration. Every other tool is still bounded
    by the bench, and a second `true` appearing here should be argued for the
    same way."""
    open_world = sorted(name for name in MCP_TOOL_NAMES if TOOL_ANNOTATIONS[name].get("openWorldHint") is True)

    assert open_world == [SERVER_UPGRADE]
    assert TOOL_ANNOTATIONS[SERVER_UPGRADE]["readOnlyHint"] is False
    # It replaces a package and destroys nothing: the bench, the configuration
    # and the release it replaced all survive it.
    assert TOOL_ANNOTATIONS[SERVER_UPGRADE]["destructiveHint"] is False
    assert TOOL_ANNOTATIONS[SERVER_UPGRADE]["idempotentHint"] is True


# ---------------------------------------------------------------------------
# Extras the configuration needs and the installation does not have.


def test_the_result_warns_when_the_installation_lost_an_extra_the_configuration_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bench behind it: `can_buses` configured, python-can gone.

    `uv tool install --upgrade` replaces the recorded requirement rather than
    raising it, so an installation created as `agentic-hil[can]` comes back
    without the extra and nothing said so until the first CAN call. The warning
    carries the command that repairs it, and that command names the union of what
    is installed and what is missing — one that named only the missing extra
    would take the others away."""
    fake_manager(
        monkeypatch,
        installed=subprocess.CompletedProcess([], 0, "installed\n", ""),
        version_after="9.9.9",
        extras=("pyocd",),
    )
    monkeypatch.setattr("agentic_hil.upgrade._distribution_installer", lambda: "pip")
    tools = AgenticHILToolService(upgradable_config(tmp_path, can_buses_yaml=SOCKETCAN_BUS))
    try:
        result = tools.call(SERVER_UPGRADE)
    finally:
        tools.close()

    assert result["upgraded_on_disk"] is True
    warning = result["extras_warning"]
    assert warning["missing_extras"] == ["can"]
    assert warning["installed_extras"] == ["pyocd"]
    assert warning["configured_entries"] == {"can": ["dut_can"]}
    assert "agentic-hil[can,pyocd]" in warning["reinstall_command"]
    assert "dut_can" in warning["summary"]


def test_no_extras_warning_when_the_configuration_needs_nothing_the_installation_lacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bench with no direct CAN adapter needs no `[can]`, and is not nagged.

    The `process` adapter drives a bridge subprocess and never imports
    python-can, so reading every `can_buses` entry as a claim on the extra would
    warn a working bench about a package it has no use for."""
    fake_manager(
        monkeypatch,
        installed=subprocess.CompletedProcess([], 0, "installed\n", ""),
        version_after="9.9.9",
    )
    bridge = 'can_buses:\n  dut_can:\n    adapter: "process"\n    channel: "bridge"\n    bitrate: 500000\n    executable: "python"\n'
    tools = AgenticHILToolService(upgradable_config(tmp_path, can_buses_yaml=bridge))
    try:
        result = tools.call(SERVER_UPGRADE)
    finally:
        tools.close()

    assert result["upgraded_on_disk"] is True
    assert "extras_warning" not in result


def test_doctor_names_the_extra_the_configuration_needs_and_the_installation_lacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first hole: until now the first CAN call found this.

    `doctor` is where an operator looks before a bench misbehaves, so the
    contradiction belongs there — with the exact reinstall line, because a
    warning that does not carry the command is the start of a search. It is a
    warning and not a failure: the configuration is valid and every other part of
    the bench works."""
    from agentic_hil.cli import doctor

    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, config_version=2, can_buses_yaml=SOCKETCAN_BUS)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("agentic_hil.upgrade._installed_extras", lambda: ())
    monkeypatch.setattr("agentic_hil.upgrade._distribution_installer", lambda: "pip")

    checked = doctor()

    assert checked["missing_extras"]["missing_extras"] == ["can"]
    assert checked["missing_extras"]["configured_entries"] == {"can": ["dut_can"]}
    assert any("dut_can" in warning and "agentic-hil[can]" in warning for warning in checked["warnings"]), checked["warnings"]
    # A warning and not a refusal: nothing about the configuration is invalid,
    # and refusing here would also roll `agentic-hil setup` back over a package
    # that is reinstalled in one line.
    assert "error_type" not in checked, checked
