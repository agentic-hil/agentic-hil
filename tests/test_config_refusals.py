"""Two refusals the configuration path owes and does not yet make.

#474: a directory at the discovered configuration path is not "no
configuration". `load_config` already classifies that filesystem state as
`config_unreadable`; the authoritative loader reads it as absent, so
`mcp-stdio` starts the unprovisioned server over it and exits 0, and
`check-plan --strict` passes it as the board-free case. Only a path that is
genuinely absent takes the unprovisioned route.

#482: `debuggers.<name>.executable` is accepted under the system temporary
directory and under a package-manager cache, while the OpenOCD script fields of
the same entry are refused there. The same trust rule applies to the program
the debugger starts, with the refusal naming the field and the root that
disqualifies it. `debug.gdb_executable` and `can_buses.<name>.executable` share
the validation and the rule.

Written from the issue text before the implementation; the neighbours that must
not move are pinned beside the defects.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FAKE_OPENOCD, write_authoritative_config

from agentic_hil.cli import entrypoint
from agentic_hil.config import ConfigError, load_authoritative_config, project_config_path
from agentic_hil.tools import UnprovisionedToolService

REPOSITORY = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# #474: a directory at the configuration path is unreadable, not absent
# ---------------------------------------------------------------------------


def _discovered_configuration_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point discovery at a scratch root and leave the override unset.

    That is the shape of the reproduction in the issue: no `AGENTIC_HIL_CONFIG`,
    the path the product discovers for the workspace, and whatever sits there."""
    config_root = tmp_path / "user-config"
    monkeypatch.setenv("APPDATA", str(config_root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.delenv("AGENTIC_HIL_CONFIG", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def test_a_directory_at_the_discovered_configuration_path_is_unreadable_not_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same filesystem state `load_config` already calls `config_unreadable`.

    `Path.is_file()` is false for a directory as much as for nothing at all, and
    the authoritative loader read the two alike. A bind mount whose source did
    not exist, or a directory created by hand at that path, is a configuration
    location that could not be read, and the refusal says so and names it."""
    workspace = _discovered_configuration_root(tmp_path, monkeypatch)
    occupied = project_config_path(workspace)
    occupied.mkdir(parents=True)

    with pytest.raises(ConfigError) as refused:
        load_authoritative_config(workspace)

    assert refused.value.error_type == "config_unreadable"
    assert refused.value.details["path"] == str(occupied.resolve())


def test_a_directory_at_the_overriding_configuration_path_is_unreadable_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`AGENTIC_HIL_CONFIG` naming a directory is the same misread from the other entry."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    occupied = tmp_path / "override" / "config.yaml"
    occupied.mkdir(parents=True)
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(occupied))

    with pytest.raises(ConfigError) as refused:
        load_authoritative_config(workspace)

    assert refused.value.error_type == "config_unreadable"
    assert refused.value.details["path"] == str(occupied.resolve())


def test_an_absent_configuration_path_is_still_config_file_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour: genuinely nothing at the path is still the no-configuration answer."""
    workspace = _discovered_configuration_root(tmp_path, monkeypatch)
    expected = project_config_path(workspace)
    assert not expected.exists()

    with pytest.raises(ConfigError) as refused:
        load_authoritative_config(workspace)

    assert refused.value.error_type == "config_file_not_found"
    assert refused.value.details["path"] == str(expected.resolve())


def _serve_mcp_stdio(monkeypatch: pytest.MonkeyPatch) -> dict:
    served: dict = {}

    def fake_server(config, *args, **kwargs):
        served["config"] = config
        served["tools"] = kwargs.get("tools")
        return 0

    monkeypatch.setattr("agentic_hil.cli.run_stdio_server", fake_server)
    return served


def test_mcp_stdio_refuses_a_directory_at_the_configuration_path_instead_of_serving_unprovisioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The server must refuse the way the loader does.

    Over a directory at the discovered path `mcp-stdio` started the
    unprovisioned service: the client saw a healthy server, every hardware tool
    answered `config_file_not_found`, and the agent was pointed at
    `project_config_create`, which cannot succeed while a directory occupies the
    target. Nobody was told that the one location policy is read from could not
    be read. A malformed file at that path is a hard stop with a rendered
    refusal and exit 1; the directory is the same stop."""
    workspace = _discovered_configuration_root(tmp_path, monkeypatch)
    occupied = project_config_path(workspace)
    occupied.mkdir(parents=True)
    monkeypatch.chdir(workspace)
    served = _serve_mcp_stdio(monkeypatch)

    exit_code = entrypoint(["mcp-stdio"])

    assert exit_code == 1
    assert served == {}, "the server was started over an unreadable configuration"
    rendered = json.loads(capsys.readouterr().err)
    assert rendered["error_type"] == "config_unreadable"
    assert rendered["path"] == str(occupied.resolve())


def test_mcp_stdio_still_refuses_a_malformed_configuration_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The neighbour the rule was written for: a file that will not parse is a hard stop."""
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(workspace, monkeypatch)
    config_path.write_text("workspace_root: [unterminated\n", encoding="utf-8")
    monkeypatch.delenv("AGENTIC_HIL_CONFIG")
    monkeypatch.chdir(workspace)
    served = _serve_mcp_stdio(monkeypatch)

    exit_code = entrypoint(["mcp-stdio"])

    assert exit_code == 1
    assert served == {}
    assert json.loads(capsys.readouterr().err)["error_type"] == "config_invalid"


def test_mcp_stdio_still_serves_unprovisioned_when_nothing_is_at_the_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other neighbour: an absent file is the one state that starts the unprovisioned server."""
    workspace = _discovered_configuration_root(tmp_path, monkeypatch)
    monkeypatch.chdir(workspace)
    served = _serve_mcp_stdio(monkeypatch)

    assert entrypoint(["mcp-stdio"]) == 0
    assert served["config"] is None
    assert isinstance(served["tools"], UnprovisionedToolService)


def test_check_plan_strict_fails_on_a_directory_at_the_configuration_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`check-plan` leans on the same gate.

    `config_file_not_found` is the one loading failure it tolerates as the
    board-free case, so a workspace with a directory at its configuration path
    passed `--strict` as if it simply had no bench. It is a bench whose
    configuration could not be read, and strict plan checking fails on it the
    way it already fails on a malformed file."""
    workspace = _discovered_configuration_root(tmp_path, monkeypatch)
    project_config_path(workspace).mkdir(parents=True)
    monkeypatch.chdir(workspace)
    plan = workspace / "good.testconfig.yaml"
    plan.write_text("version: 3\nname: good\nsteps:\n  - {device: dut_uart, action: uart_open}\n", encoding="utf-8")

    exit_code = entrypoint(["check-plan", str(plan), "--strict", "--json"])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert result["ok"] is False
    assert result["configuration"]["ok"] is False
    assert result["configuration"]["error_type"] == "config_unreadable"
    assert "could not be loaded" in result["summary"]


# ---------------------------------------------------------------------------
# #482: a configured executable is held to the trust rule of its scripts
# ---------------------------------------------------------------------------


def _injected_temporary_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary root of the test's own.

    Injected because the suite's `tmp_path` is under the real one, which would
    make every configuration in the suite exempt for living there;
    `test_temporary_roots_name_this_platforms_temporary_storage` holds the real
    set."""
    temporary_root = tmp_path / "system-temp"
    temporary_root.mkdir()
    monkeypatch.setattr("agentic_hil.config.temporary_roots", lambda: (temporary_root,))
    return temporary_root


def _program(directory: Path, name: str) -> Path:
    """An executable-shaped file where the rule is about location, not content.

    Pinning asks that the path be an existing single-link regular file; what it
    would do when started is not this test's question, and the refusal under
    test is made before anything is started."""
    directory.mkdir(parents=True, exist_ok=True)
    program = directory / name
    program.write_text("", encoding="utf-8")
    return program


def test_a_debugger_executable_under_temporary_storage_is_refused_like_its_scripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sentence the script refusal uses is true of the executable, and more so.

    It is the program the debugger starts. A configuration pointing at a
    toolchain in temporary storage is valid the afternoon it is written and
    describes nothing after the next reboot; on POSIX the swept name in a
    sticky world-writable directory is then free for any local account to
    create, and the next run starts that file as the operator with every grant
    the entry carries. The refusal is the script refusal: same message, same
    details, the field and the root named."""
    temporary_root = _injected_temporary_root(tmp_path, monkeypatch)
    executable = _program(temporary_root / "openocd-bin", "openocd")
    write_authoritative_config(
        tmp_path / "workspace",
        monkeypatch,
        debugger_executable=executable,
        permissions={"allow_probe": True},
    )

    with pytest.raises(ConfigError) as refused:
        load_authoritative_config(tmp_path / "workspace")

    assert refused.value.error_type == "config_invalid"
    assert refused.value.details["field"] == "debuggers.dut.executable"
    assert refused.value.details["path"] == str(executable)
    assert refused.value.details["temporary_root"] == str(temporary_root)
    assert "temporary storage" in refused.value.summary
    assert "single-link regular file" not in refused.value.summary

    # The same refusal the script field already makes, asked in the same run
    # against the same root, so the two cannot drift apart in wording or shape.
    script = _program(temporary_root / "openocd-scripts" / "interface", "stlink.cfg")
    write_authoritative_config(
        tmp_path / "workspace-script",
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_probe": True},
        interface_cfg=script.as_posix(),
    )
    with pytest.raises(ConfigError) as script_refused:
        load_authoritative_config(tmp_path / "workspace-script")

    assert script_refused.value.details["field"] == "debuggers.dut.interface_cfg"
    assert script_refused.value.summary == refused.value.summary
    assert set(script_refused.value.details) == set(refused.value.details)


def test_the_gdb_executable_is_refused_under_temporary_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`debug.gdb_executable` shares the validation and so shares the rule."""
    temporary_root = _injected_temporary_root(tmp_path, monkeypatch)
    gdb = _program(temporary_root / "toolchain", "arm-none-eabi-gdb")
    write_authoritative_config(
        tmp_path / "workspace",
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        gdb_executable=gdb,
        permissions={"allow_probe": True},
    )

    with pytest.raises(ConfigError) as refused:
        load_authoritative_config(tmp_path / "workspace")

    assert refused.value.error_type == "config_invalid"
    assert refused.value.details["field"] == "debug.gdb_executable"
    assert refused.value.details["temporary_root"] == str(temporary_root)
    assert "temporary storage" in refused.value.summary


def test_a_can_bridge_executable_is_refused_under_temporary_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`can_buses.<name>.executable` is the third field behind the one validator."""
    temporary_root = _injected_temporary_root(tmp_path, monkeypatch)
    bridge = _program(temporary_root / "bridges", "can-bridge")
    write_authoritative_config(
        tmp_path / "workspace",
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_probe": True},
        can_buses_yaml=(
            "can_buses:\n"
            "  bench:\n"
            '    adapter: "process"\n'
            '    channel: "vcan0"\n'
            f'    executable: "{bridge.as_posix()}"\n'
        ),
    )

    with pytest.raises(ConfigError) as refused:
        load_authoritative_config(tmp_path / "workspace")

    assert refused.value.error_type == "config_invalid"
    assert refused.value.details["field"] == "can_buses.bench.executable"
    assert refused.value.details["temporary_root"] == str(temporary_root)
    assert "temporary storage" in refused.value.summary


@pytest.mark.parametrize(
    "cache",
    ["uv_cache_dir", "xdg_cache_home", "home_dot_cache"],
)
def test_a_debugger_executable_under_a_package_manager_cache_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache: str) -> None:
    """A cache is not an installation boundary, for the toolchain either.

    The MCP launcher is refused from these roots and `docs/mcp-hosts.md` says
    why. The debugger toolchain, which is the thing that reaches the board, is
    measured against the same list: `UV_CACHE_DIR`, `XDG_CACHE_HOME` and the
    user's `~/.cache` here, each named by the refusal as the root the path was
    found under. The temporary root is not injected: these roots are their own,
    and the configuration under the suite's `tmp_path` sits in none of them."""
    if cache == "uv_cache_dir":
        cache_root = tmp_path / "uv-cache"
        monkeypatch.setenv("UV_CACHE_DIR", str(cache_root))
    elif cache == "xdg_cache_home":
        cache_root = tmp_path / "xdg-cache"
        monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    else:
        cache_root = Path.home() / ".cache"
    executable = _program(cache_root / "archive-v0" / "openocd" / "bin", "openocd")
    write_authoritative_config(
        tmp_path / "workspace",
        monkeypatch,
        debugger_executable=executable,
        permissions={"allow_probe": True},
    )

    with pytest.raises(ConfigError) as refused:
        load_authoritative_config(tmp_path / "workspace")

    assert refused.value.error_type == "config_invalid"
    assert refused.value.details["field"] == "debuggers.dut.executable"
    assert refused.value.details["path"] == str(executable)
    assert str(cache_root) in refused.value.details.values(), refused.value.details
    assert "cache" in refused.value.summary.lower()
    assert "single-link regular file" not in refused.value.summary


def test_a_configuration_in_temporary_storage_is_not_refused_for_its_own_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The existing exemption stays, for the executable as for the scripts.

    A configuration that lives in temporary storage itself is already as
    ephemeral as the field could make it, so the field is not what is wrong with
    it. It is also the shape every test in this suite has, since `tmp_path` is
    under the platform's temporary root."""
    temporary_root = _injected_temporary_root(tmp_path, monkeypatch)
    executable = _program(temporary_root / "openocd-bin", "openocd")
    config_path = write_authoritative_config(
        tmp_path / "workspace",
        monkeypatch,
        config_root=temporary_root / "user-config",
        debugger_executable=executable,
        permissions={"allow_probe": True},
    )
    assert config_path.is_relative_to(temporary_root)

    config = load_authoritative_config(tmp_path / "workspace")

    assert config.debuggers["dut"].executable == str(executable.resolve())


def test_an_executable_outside_temporary_and_cache_storage_still_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour: a toolchain that lives somewhere durable is pinned as before."""
    _injected_temporary_root(tmp_path, monkeypatch)
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    write_authoritative_config(
        tmp_path / "workspace",
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_probe": True},
    )

    config = load_authoritative_config(tmp_path / "workspace")

    assert config.debuggers["dut"].executable == str(FAKE_OPENOCD.resolve())


def test_the_configuration_reference_states_the_rule_for_executables() -> None:
    """`docs/configuration.md` scoped the temporary-storage sentence to OpenOCD scripts.

    The rule now covers configured executables and refuses caches for them, and
    the reference is where an operator reads what a refusal will say before it
    says it. The sentence that names the system temporary directory has to name
    executables with it, and a sentence in the same paragraph has to name the
    cache half."""
    text = (REPOSITORY / "docs" / "configuration.md").read_text(encoding="utf-8")
    paragraphs = [paragraph for paragraph in text.split("\n\n") if "system temporary directory" in paragraph]
    assert paragraphs, "the reference no longer names the system temporary directory"
    sentences = [sentence for paragraph in paragraphs for sentence in paragraph.split(". ")]
    temporary = [sentence for sentence in sentences if "temporary" in sentence]
    assert any("executable" in sentence for sentence in temporary), temporary
    assert any("cache" in sentence.lower() for sentence in sentences), "the cache half of the rule is not documented"
