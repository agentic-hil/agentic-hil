"""`debuggers.<name>.gdb_server_executable`: the GDB server typed debug sessions run on under `type: stlink` (#624).

STM32_Programmer_CLI has no GDB server of its own. STM32CubeCLT ships one
beside it, ST-LINK_gdbserver, and a session on this backend runs that. So the
entry names a second program, or has it found: beside the configured CLI in
the same STM32CubeCLT tree first, because the server is started with that CLI's
directory as `-cp` and the two come from one bundle; then on this host's PATH
and in its STM32CubeCLT installations. OpenOCD and pyOCD run their sessions on
their own executable and never read the key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FAKE_STLINK, write_authoritative_config, write_config

from agentic_hil.backends import common
from agentic_hil.backends.common import cube_clt_gdb_server_paths, find_st_link_gdb_server, st_link_gdb_server_beside
from agentic_hil.config import ConfigError, config_schema, load_authoritative_config, load_config
from agentic_hil.knowledge import DEBUGGER_FIELD_MATRIX
from agentic_hil.tools import configured_opens_debug_sessions

FIELD = "debuggers.dut.gdb_server_executable"


def cube_clt(root: Path, *, suffix: str = "") -> tuple[Path, Path]:
    """An STM32CubeCLT tree as its installer lays it out: the CLI and the GDB server, each in its own `bin`."""
    cli = root / "STM32CubeProgrammer" / "bin" / f"STM32_Programmer_CLI{suffix}"
    server = root / "STLink-gdb-server" / "bin" / f"ST-LINK_gdbserver{suffix}"
    for path in (cli, server):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    return cli, server


# ---------------------------------------------------------------------------
# Discovery.


@pytest.mark.parametrize("suffix", ["", ".exe"])
def test_the_gdb_server_is_found_beside_the_cli_of_the_same_cubeclt_tree(tmp_path: Path, suffix: str) -> None:
    cli, server = cube_clt(tmp_path / "STM32CubeCLT_1.22.0", suffix=suffix)

    assert st_link_gdb_server_beside(str(cli)) == str(server)


def test_a_cli_outside_a_cubeclt_tree_has_no_gdb_server_beside_it(tmp_path: Path) -> None:
    """A standalone STM32CubeProgrammer installs the CLI and no GDB server."""
    cli = tmp_path / "STM32CubeProgrammer" / "bin" / "STM32_Programmer_CLI"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"")

    assert st_link_gdb_server_beside(str(cli)) is None
    assert st_link_gdb_server_beside(None) is None


def test_the_newest_cubeclt_installation_is_listed_first(tmp_path: Path) -> None:
    _, older = cube_clt(tmp_path / "STM32CubeCLT_1.21.0", suffix=".exe")
    _, newer = cube_clt(tmp_path / "STM32CubeCLT_1.22.0", suffix=".exe")
    (tmp_path / "STM32CubeIDE_2.0.0").mkdir()

    assert cube_clt_gdb_server_paths(tmp_path) == [str(newer), str(older)]


def test_the_server_beside_the_cli_wins_over_one_elsewhere_on_the_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The server is started with the CLI's directory as `-cp`, so the one from the same tree is preferred."""
    cli, server = cube_clt(tmp_path / "clt")
    monkeypatch.setattr(common, "host_st_link_gdb_server", lambda: str(tmp_path / "elsewhere" / "ST-LINK_gdbserver"))

    assert find_st_link_gdb_server(str(cli)) == str(server)
    assert find_st_link_gdb_server(str(tmp_path / "standalone" / "STM32_Programmer_CLI")) == str(tmp_path / "elsewhere" / "ST-LINK_gdbserver")


# ---------------------------------------------------------------------------
# Loading.


def test_an_stlink_entry_that_names_no_gdb_server_pins_the_one_beside_its_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli, server = cube_clt(tmp_path / "clt")
    write_authoritative_config(tmp_path / "workspace", monkeypatch, debugger_type="stlink", debugger_executable=cli, probe_id="STLINK123")

    config = load_authoritative_config(tmp_path / "workspace")

    assert config.debuggers["dut"].gdb_server_executable == str(server.resolve())
    assert configured_opens_debug_sessions(config) is True


def test_an_stlink_entry_with_no_gdb_server_anywhere_loads_and_opens_no_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a load failure: flashing, probing and reading need no GDB server, so the entry stays usable for them."""
    write_authoritative_config(tmp_path / "workspace", monkeypatch, debugger_type="stlink", debugger_executable=FAKE_STLINK, probe_id="STLINK123")

    config = load_authoritative_config(tmp_path / "workspace")

    assert config.debuggers["dut"].gdb_server_executable is None
    assert configured_opens_debug_sessions(config) is False


def test_a_configured_gdb_server_is_pinned_to_its_resolved_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, server = cube_clt(tmp_path / "clt")
    write_authoritative_config(
        tmp_path / "workspace", monkeypatch, debugger_type="stlink", debugger_executable=FAKE_STLINK, probe_id="STLINK123", gdb_server_executable=server
    )

    config = load_authoritative_config(tmp_path / "workspace")

    assert config.debuggers["dut"].gdb_server_executable == str(server.resolve())
    assert configured_opens_debug_sessions(config) is True


def test_a_configured_gdb_server_that_does_not_exist_is_refused_at_load_naming_the_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same rule `executable` is held to: a program somebody named and nobody can start is a wrong file, said at load."""
    write_authoritative_config(
        tmp_path / "workspace",
        monkeypatch,
        debugger_type="stlink",
        debugger_executable=FAKE_STLINK,
        probe_id="STLINK123",
        gdb_server_executable=tmp_path / "nowhere" / "ST-LINK_gdbserver",
    )

    with pytest.raises(ConfigError) as refused:
        load_authoritative_config(tmp_path / "workspace")

    assert refused.value.error_type == "config_invalid"
    assert refused.value.details["field"] == FIELD


def test_a_configured_gdb_server_inside_the_workspace_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A program the workspace can rewrite is refused as every configured executable is."""
    workspace = tmp_path / "workspace"
    inside = workspace / "tools" / "ST-LINK_gdbserver"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"")
    write_authoritative_config(workspace, monkeypatch, debugger_type="stlink", debugger_executable=FAKE_STLINK, probe_id="STLINK123", gdb_server_executable=inside)

    with pytest.raises(ConfigError) as refused:
        load_authoritative_config(workspace)

    assert refused.value.error_type == "config_invalid"
    assert refused.value.details["field"] == FIELD
    assert "outside the workspace" in refused.value.summary


@pytest.mark.parametrize("debugger_type", ["openocd", "pyocd"])
def test_the_backends_that_run_their_own_gdb_server_never_read_the_key(tmp_path: Path, debugger_type: str) -> None:
    """Ignored, like target_type on OpenOCD: a value here changes nothing, so it is not checked either."""
    config = load_config(
        str(write_config(tmp_path, debugger_type=debugger_type, target_type="stm32f446retx", gdb_server_executable=tmp_path / "nowhere" / "ST-LINK_gdbserver"))
    )

    assert configured_opens_debug_sessions(config) is True


def test_the_key_is_in_the_schema_as_an_optional_path() -> None:
    entry = config_schema()["properties"]["debuggers"]["additionalProperties"]["properties"]["gdb_server_executable"]

    assert entry["type"] == ["string", "null"]
    assert entry["default"] is None
    assert "ST-LINK_gdbserver" in entry["description"]


def test_the_field_matrix_says_which_backend_reads_the_key() -> None:
    assert DEBUGGER_FIELD_MATRIX["stlink"]["gdb_server_executable"]["status"] == "discovered"
    assert DEBUGGER_FIELD_MATRIX["openocd"]["gdb_server_executable"]["status"] == "ignored"
    assert DEBUGGER_FIELD_MATRIX["pyocd"]["gdb_server_executable"]["status"] == "ignored"
