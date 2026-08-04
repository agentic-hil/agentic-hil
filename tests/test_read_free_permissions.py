"""Reading needs no permission from version 2 on; writing still does.

The migration half matters as much as the model half: a file written before
this release keeps the meaning it was written with, because the alternative is
that an update quietly widens what an existing bench allows.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FAKE_GDB, FAKE_OPENOCD_STATE_ENV, write_config

from agentic_hil.can import CanBusService
from agentic_hil.comports import ComPortService
from agentic_hil.config import ConfigError, load_config
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import CURRENT_CONFIG_VERSION, LEGACY_CONFIG_VERSION

NO_PERMISSIONS = {
    "allow_probe": False,
    "allow_flash": False,
    "allow_reset": False,
    "allow_com_read": False,
    "allow_com_write": False,
    "allow_can_read": False,
    "allow_can_write": False,
    "allow_raw_debugger_commands": False,
    "allow_mass_erase": False,
}
COM_PORTS = 'com_ports:\n  dut_uart:\n    device: "/dev/ttyNONEXISTENT"\n'
CAN_BUSES = 'can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n'


def config_for(workspace: Path, **kwargs):
    return load_config(str(write_config(workspace, **kwargs)))


def bare_config(workspace: Path, version: int | None):
    """A bench that granted nothing at all, under one permission model."""
    return config_for(
        workspace,
        permissions=NO_PERMISSIONS,
        com_ports_yaml=COM_PORTS,
        can_buses_yaml=CAN_BUSES,
        config_version=version,
    )


def test_a_file_without_a_version_key_is_read_under_the_old_model(tmp_path: Path) -> None:
    config = bare_config(tmp_path, None)
    assert config.config_version == LEGACY_CONFIG_VERSION
    assert config.read_free is False


def test_an_existing_config_does_not_gain_read_access_on_update(tmp_path: Path) -> None:
    """The migration guarantee, stated as a test.

    Every read this bench refused before the release it still refuses, because
    nothing infers the new model from the absence of a key."""
    config = bare_config(tmp_path, None)
    service = AgenticHILToolService(config)
    try:
        assert service.call("debugger_info")["error_type"] == "permission_denied"
        assert service.call("debugger_probes_list")["error_type"] == "permission_denied"
        assert service.call("probe_target")["error_type"] == "permission_denied"
    finally:
        service.close()

    com = ComPortService(config)
    try:
        assert com.session_start("dut_uart")["error_type"] == "permission_denied"
        assert "no configured COM port has permissions.allow_read" in com.list_ports()["available_com_ports"]["summary"]
    finally:
        com.close()

    can = CanBusService(config)
    try:
        assert can.session_start("bench")["error_type"] == "permission_denied"
    finally:
        can.close()


def test_version_two_reads_without_any_grant(tmp_path: Path) -> None:
    config = bare_config(tmp_path, CURRENT_CONFIG_VERSION)
    assert config.read_free is True
    service = AgenticHILToolService(config)
    try:
        # The fake OpenOCD answers, so these get through to the backend rather
        # than stopping at a permission that no longer exists.
        assert service.call("debugger_info").get("error_type") != "permission_denied"
        assert service.call("debugger_probes_list").get("error_type") != "permission_denied"
        assert service.call("probe_target").get("error_type") != "permission_denied"
    finally:
        service.close()

    com = ComPortService(config)
    try:
        # The device does not exist, so the open fails — but on the hardware, not
        # on a permission.
        assert com.session_start("dut_uart").get("error_type") != "permission_denied"
        assert com.list_ports()["available_com_ports"].get("error_type") != "permission_denied"
    finally:
        com.close()

    can = CanBusService(config)
    try:
        assert can.session_start("bench").get("error_type") != "permission_denied"
    finally:
        can.close()


def test_version_two_still_refuses_every_write(tmp_path: Path) -> None:
    """The half of the model that did not change.

    Flash, reset, serial write and CAN send are state-changing, and a bench that
    granted none of them grants none of them."""
    config = bare_config(tmp_path, CURRENT_CONFIG_VERSION)
    service = AgenticHILToolService(config)
    try:
        assert service.call("flash_firmware", {"image_path": "build/app.bin"})["error_type"] == "permission_denied"
        assert service.call("reset_target", {"mode": "run"})["error_type"] == "permission_denied"
    finally:
        service.close()

    com = ComPortService(config)
    try:
        assert com.write("dut_uart", {"text": "x"})["error_type"] == "permission_denied"
    finally:
        com.close()

    can = CanBusService(config)
    try:
        assert can.send("bench", {"frame_id": 0x100})["error_type"] == "permission_denied"
    finally:
        can.close()


@pytest.mark.parametrize(
    ("section", "flag"),
    [("debuggers", "allow_probe"), ("com_ports", "allow_read"), ("can_buses", "allow_read")],
)
def test_a_version_two_file_that_still_carries_a_read_permission_is_refused(tmp_path: Path, section: str, flag: str) -> None:
    """A half-migrated file is refused by name, not quietly ignored.

    Ignoring the key would leave an operator believing a flag still gates
    something."""
    path = write_config(tmp_path, com_ports_yaml=COM_PORTS, can_buses_yaml=CAN_BUSES, config_version=CURRENT_CONFIG_VERSION)
    text = path.read_text(encoding="utf-8")
    marker = {"debuggers": "      allow_flash:", "com_ports": "      allow_write:", "can_buses": "      allow_write:"}[section]
    index = text.index(marker) if section == "debuggers" else text.rindex(marker) if section == "can_buses" else text.index(marker)
    path.write_text(text[:index] + f"      {flag}: true\n" + text[index:], encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(str(path))
    assert excinfo.value.error_type == "config_invalid"
    assert excinfo.value.details["field"].endswith(f"permissions.{flag}")
    assert excinfo.value.details["migration"]["remove"].endswith(flag)
    assert "no read permission" in excinfo.value.summary


def test_an_unsupported_version_is_refused_by_number(tmp_path: Path) -> None:
    path = write_config(tmp_path, config_version=7)
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(path))
    assert excinfo.value.error_type == "config_invalid"
    assert excinfo.value.details["field"] == "version"
    assert excinfo.value.details["supported_versions"] == [LEGACY_CONFIG_VERSION, CURRENT_CONFIG_VERSION]


class _RecordingHandle:
    """A pyserial stand-in that records the order of attribute writes and open()."""

    def __init__(self, inner, record) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_record", record)

    def __setattr__(self, name: str, value: object) -> None:
        self._record(name, value)
        setattr(self._inner, name, value)

    def __getattr__(self, name: str):
        if name == "open":
            def open_port() -> None:
                self._record("open", True)
                self._inner.is_open = True

            return open_port
        return getattr(self._inner, name)


def test_a_port_can_be_opened_without_touching_the_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The passive mode that is no longer an access precondition.

    Reading needs no permission now, so the way to prove an observation did not
    disturb the target is to open the port without raising the lines that reset
    it — and the line state has to be decided before the port is open, not
    after."""
    events: list[tuple[str, object]] = []
    inner = SimpleNamespace(is_open=False, in_waiting=0, close=lambda: None, read=lambda size: b"", reset_input_buffer=lambda: None, reset_output_buffer=lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "serial",
        SimpleNamespace(Serial=lambda *args, **kwargs: _RecordingHandle(inner, lambda name, value: events.append((name, value)))),
    )
    config = config_for(
        tmp_path,
        config_version=CURRENT_CONFIG_VERSION,
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n    assert_dtr: false\n    assert_rts: false\n',
    )
    assert config.com_ports["dut_uart"].assert_dtr is False
    assert config.com_ports["dut_uart"].assert_rts is False

    service = ComPortService(config)
    try:
        started = service.session_start("dut_uart", clear_buffer=False)
        assert started["ok"] is True, started
        # Reported, so a reader can tell a passive observation from one that
        # raised the line that resets the board.
        assert service.list_ports()["ports"]["dut_uart"]["assert_dtr"] is False
    finally:
        service.close()

    # Both lines are decided before the port exists, or the open itself has
    # already reset the board this was meant to observe.
    assert ("dtr", False) in events
    assert ("rts", False) in events
    assert events.index(("dtr", False)) < events.index(("open", True))
    assert events.index(("rts", False)) < events.index(("open", True))


def test_the_shipped_template_is_born_on_the_new_model(tmp_path: Path) -> None:
    from agentic_hil.cli import DEFAULT_CONFIG_TEMPLATE

    assert "version: 2" in DEFAULT_CONFIG_TEMPLATE
    assert "allow_probe: " not in DEFAULT_CONFIG_TEMPLATE
    assert "allow_flash: false" in DEFAULT_CONFIG_TEMPLATE
    assert "allow_mass_erase: false" in DEFAULT_CONFIG_TEMPLATE
    assert "allow_debug_execution: false" in DEFAULT_CONFIG_TEMPLATE


# ---------------------------------------------------------------------------
# Watching a target is a read. Letting it run is not.


def debug_service(workspace: Path, *, may_execute: bool):
    """A read-free bench with a fake GDB, granting execution or nothing."""
    permissions = {**NO_PERMISSIONS, "allow_debug_execution": may_execute}
    config_path = write_config(workspace, gdb_executable=FAKE_GDB, permissions=permissions, config_version=CURRENT_CONFIG_VERSION)
    elf_path = workspace / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12)
    return AgenticHILToolService(load_config(str(config_path)))


def gdb_commands(workspace: Path, started: dict) -> list[str]:
    """Every MI command the session log recorded, which is what reached the target."""
    log = json.loads((workspace / started["log_path"]).read_text(encoding="utf-8"))
    return [str(entry.get("command")) for entry in log["gdb_commands"]]


def test_a_read_free_bench_may_attach_to_a_target_but_not_run_it(tmp_path: Path) -> None:
    """Attaching halts the core; continuing executes firmware.

    Version 2 made every read free, and a debug session is a read — but
    `-exec-continue` runs the code on the board and actuates whatever it drives,
    so it stays a write and needs `allow_debug_execution`."""
    service = debug_service(tmp_path, may_execute=False)
    try:
        started = service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "attach", "timeout_s": 10.0})
        assert started["ok"] is True, started

        continued = service.call("debug_continue", {"timeout_s": 5})

        assert continued["ok"] is False
        assert continued["error_type"] == "permission_denied"
        assert "allow_debug_execution" in continued["summary"]
        assert "-exec-continue" not in gdb_commands(tmp_path, started)
        # Containment is never gated: a target this bench may not start must
        # still be one it can stop.
        assert service.call("debug_halt", {"timeout_s": 5}).get("error_type") != "permission_denied"
    finally:
        service.close()


def test_the_execution_grant_is_what_lets_the_target_run(tmp_path: Path) -> None:
    service = debug_service(tmp_path, may_execute=True)
    try:
        started = service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "attach", "timeout_s": 10.0})
        assert started["ok"] is True, started
        assert service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})["ok"] is True

        continued = service.call("debug_continue", {"timeout_s": 5})

        assert continued["ok"] is True, continued
        assert continued["stop_reason"] == "breakpoint_hit"
        assert "-exec-continue" in gdb_commands(tmp_path, started)
    finally:
        service.close()


def test_stopping_a_session_leaves_the_target_halted_rather_than_running_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ending a session must not be the resume the bench was refused.

    `debug_stop_session` is deliberately ungated — containment that needed a
    permission would leave a running target nobody may stop — and it detaches
    GDB. A debug server whose detach event resumes the core would therefore turn
    the one call every caller may make into the one call this configuration
    forbids."""
    state_path = tmp_path / "openocd-detach-state.json"
    monkeypatch.setenv(FAKE_OPENOCD_STATE_ENV, str(state_path))
    service = debug_service(tmp_path, may_execute=False)
    try:
        started = service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "attach", "timeout_s": 10.0})
        assert started["ok"] is True, started

        stopped = service.call("debug_stop_session", {"timeout_s": 5})

        assert stopped["ok"] is True, stopped
        # Confirmed while GDB was still attached, not assumed after it left.
        assert stopped["halt_confirmed"] is True
        assert stopped["target_state"] == "halted"
        assert "-exec-continue" not in gdb_commands(tmp_path, started)
    finally:
        service.close()

    # Read back from the server rather than from the argument list this bench
    # built: what it would leave on the target, and what a detach would do to it.
    left_on_the_target = json.loads(state_path.read_text(encoding="utf-8"))
    assert left_on_the_target["target_state"] == "halted"
    assert left_on_the_target["state_after_detach"] == "halted"
    assert "resume" not in json.dumps(left_on_the_target["events"])


def test_the_refusal_does_not_depend_on_a_session_being_open(tmp_path: Path) -> None:
    """The permission decides before the session state does.

    A refusal that answered `session_not_active` first would report the grant as
    missing only for callers who already hold a session, and would say nothing
    about the bench's policy to anyone else."""
    service = debug_service(tmp_path, may_execute=False)
    try:
        refused = service.call("debug_continue", {"timeout_s": 5})

        assert refused["error_type"] == "permission_denied"
        assert refused["ok"] is False
    finally:
        service.close()
