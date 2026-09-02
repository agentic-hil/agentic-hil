"""Reading needs no permission from version 2 on; writing still does.

The migration half matters as much as the model half: a file written before
this release keeps the meaning it was written with, because the alternative is
that an update quietly widens what an existing bench allows.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, write_authoritative_config, write_config

from agentic_hil.can import CanBusService
from agentic_hil.cli import doctor, init_config
from agentic_hil.comports import ComPortService
from agentic_hil.config import ConfigError, load_config
from agentic_hil.humanize import render_result
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import CURRENT_CONFIG_VERSION, LEGACY_CONFIG_VERSION, SUPPORTED_CONFIG_VERSIONS

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
# `identity_source: device` is the deliberate opt-out version 3 requires of an
# entry that names no hardware. Written here so these tests keep being about the
# read model at the current version and nothing else: without it the current
# version refuses the file, and with a `serial_number` instead the open would
# start comparing this entry against whatever is attached to the machine running
# the suite.
COM_PORTS = 'com_ports:\n  dut_uart:\n    device: "/dev/ttyNONEXISTENT"\n    identity_source: "device"\n'
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
        # The device does not exist, so the open fails, but on the hardware, not
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
    assert excinfo.value.details["supported_versions"] == list(SUPPORTED_CONFIG_VERSIONS)


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
    it, and the line state has to be decided before the port is open, not
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
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n    identity_source: "device"\n    assert_dtr: false\n    assert_rts: false\n',
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


# --- one file, and every command that describes it says the same thing -------
#
# A permission that decides nothing is not "closed". `permission_summary` is
# what `init` reports and what a reload compares, and it leaves the read grants
# out of a read-free file rather than reporting them false, because `false`
# beside a free read is a lie in the other direction. `doctor` built its
# permission blocks out of the dataclass instead, so it listed `allow_probe` and
# `allow_read` as closed on the very file `init` had just described as open for
# everything but the flash interlock (#388).


def test_doctor_does_not_call_a_read_free_permission_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file `init` just generated, read back by `doctor`.

    Through the real generation and the real report rather than a fixture: the
    disagreement was between two commands over one file, so a document a test
    wrote would not have had it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    written = init_config()
    assert written["ok"] is True, written

    report = doctor()
    rendered = " ".join(render_result(report, "doctor").split())

    # Nothing withholds probing on a version 2 file, so no surface names a grant
    # for it at all: neither as closed, nor as granted.
    assert "allow_probe" not in rendered
    assert "allow_probe" not in report["debuggers"]["dut"]["permissions"]
    # And the whole line, which is `init`'s sentence about this file in the
    # rendering's own words: everything granted but the two that are false so
    # that flashing works. The pair that really is withheld is still named as
    # withheld, or the fix would be "stop saying closed".
    assert "granted: allow_debug_execution, allow_flash, allow_reset; closed: allow_mass_erase, allow_raw_debugger_commands" in rendered


def test_doctor_reports_the_permissions_init_reported_for_the_same_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The agreement itself, key for key, on the document both commands answer with."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    written = init_config()
    assert written["ok"] is True, written

    report = doctor()

    assert report["debuggers"]["dut"]["permissions"] == written["permissions"]["debuggers"]["dut"]


def test_doctor_does_not_call_a_read_free_port_or_bus_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the same rendering, on the sections a bare `init` leaves empty."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        config_version=CURRENT_CONFIG_VERSION,
        com_ports_yaml=COM_PORTS,
        can_buses_yaml=CAN_BUSES,
    )
    monkeypatch.chdir(workspace)

    report = doctor()
    rendered = " ".join(render_result(report, "doctor").split())

    assert "allow_read" not in rendered
    assert "allow_read" not in report["com_ports"]["dut_uart"]["permissions"]
    assert "allow_read" not in report["can_buses"]["bench"]["permissions"]
    assert "granted: allow_write" in rendered


def test_doctor_still_calls_a_version_one_read_permission_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbouring case this must not quieten.

    On a version 1 file the two flags still decide whether this bench may probe
    and may read a port, and a bench that granted neither is a bench where
    `doctor` has to say so. Everything else is granted here so that each closed
    list is exactly the read grant plus the interlocked pair: a file that
    withheld everything would have hidden the one name under test in a crowd."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        permissions={**DEFAULT_TEST_PERMISSIONS, "allow_probe": False, "allow_com_read": False, "allow_can_read": False},
        com_ports_yaml=COM_PORTS,
        can_buses_yaml=CAN_BUSES,
    )
    monkeypatch.chdir(workspace)

    report = doctor()
    rendered = " ".join(render_result(report, "doctor").split())

    assert report["debuggers"]["dut"]["permissions"]["allow_probe"] is False
    assert report["com_ports"]["dut_uart"]["permissions"]["allow_read"] is False
    assert "closed: allow_mass_erase, allow_probe, allow_raw_debugger_commands" in rendered
    assert "granted: allow_write; closed: allow_read" in rendered


def test_the_shipped_template_is_born_on_the_new_model(tmp_path: Path) -> None:
    from agentic_hil.cli import DEFAULT_CONFIG_TEMPLATE

    assert f"version: {CURRENT_CONFIG_VERSION}\n" in DEFAULT_CONFIG_TEMPLATE
    assert "allow_probe: " not in DEFAULT_CONFIG_TEMPLATE
    # Reading needs no grant, and since 0.8.0 the writing grants are not
    # withheld either: the skeleton states each of them, granted. The two
    # exceptions are the pair that refuses flashing while it is true, which the
    # skeleton states false, see
    # test_generated_configurations_can_flash.py for the rule that binds them.
    assert "allow_flash: true" in DEFAULT_CONFIG_TEMPLATE
    assert "allow_mass_erase: false" in DEFAULT_CONFIG_TEMPLATE
