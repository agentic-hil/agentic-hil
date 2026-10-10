"""The esptool backend: an ESP32 flashed, reset and probed through its ROM bootloader.

Every call here runs `fixtures/fake_esptool.py`, which prints esptool 5.5.0's
own lines and opens no port, and refuses the command lines the backend must
never send (no `--port`, an erase, a forced write, no private configuration).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from conftest import DEFAULT_TEST_PERMISSIONS, ESPTOOL_COM_PORTS_YAML, ESPTOOL_TEST_PORT, FAKE_ESPTOOL, write_config
from support import scaled_time_bound
from test_implicit_single_action_run import install_fake_serial
from test_serial_port_identity import CH340_PID, CH340_VID, STLINK_PID, STLINK_VID, fake_host, host_port, inventory

from agentic_hil.backends.esptool import REDACTED_MAC, REDACTED_WORKDIR, EsptoolBackend
from agentic_hil.bench import BenchMutex, DeviceBusyError
from agentic_hil.config import ConfigError, load_config
from agentic_hil.coordination import DEBUGGER_READONLY_RESULT_REASON, debugger_effect_resources
from agentic_hil.devices import debugger_device
from agentic_hil.report import logs_directory, reports_directory
from agentic_hil.test_reactor import TestReactor, load_test_config
from agentic_hil.tools import AgenticHILToolService

APPLICATION = b"\xe9\x03\x02\x20" + bytes(range(256)) * 4
FLASHED_APPLICATION = {"image_path": "build/app.bin"}
# The MAC address the fake prints on every connect, for the redaction to take out.
FAKE_MAC = "24:0a:c4:12:34:56"
SCENARIO = "AGENTIC_HIL_FAKE_ESPTOOL_SCENARIO"


def esptool_config(workspace: Path, **kwargs):
    kwargs.setdefault("com_ports_yaml", ESPTOOL_COM_PORTS_YAML)
    kwargs.setdefault("com_port", "esp")
    kwargs.setdefault("flash_address", "0x10000")
    kwargs.setdefault("debugger_executable", FAKE_ESPTOOL)
    return load_config(str(write_config(workspace, debugger_type="esptool", **kwargs)))


def call(config, tool: str, arguments: dict | None = None) -> dict:
    service = AgenticHILToolService(config)
    try:
        return service.call(tool, arguments or {})
    finally:
        service.close()


@pytest.fixture
def esptool_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "esptool-calls.jsonl"
    monkeypatch.setenv("AGENTIC_HIL_FAKE_ESPTOOL_LOG", str(path))
    return path


def runs(log: Path) -> list[dict]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def port_runs(log: Path) -> list[list[str]]:
    """The command lines of the runs that reached for the port, in order."""
    return [entry["argv"] for entry in runs(log) if entry["argv"] and entry["argv"][-1] != "version"]


def write_application(workspace: Path, name: str = "app.bin", data: bytes = APPLICATION) -> Path:
    path = workspace / "build" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def hex_record(record_type: int, address: int, payload: bytes = b"") -> str:
    """One Intel HEX record, with its byte count and its checksum."""
    body = bytes([len(payload), address >> 8, address & 0xFF, record_type]) + payload
    return f":{(body + bytes([-sum(body) & 0xFF])).hex().upper()}\n"


def intel_hex(base: int, data: bytes, *, end: bool = True) -> str:
    """`data` at `base`, sixteen bytes a record, under one extended linear address."""
    records = [hex_record(4, 0, (base >> 16).to_bytes(2, "big"))]
    records += [hex_record(0, (base & 0xFFFF) + offset, data[offset : offset + 16]) for offset in range(0, len(data), 16)]
    return "".join(records) + (hex_record(1, 0) if end else "")


def test_probe_reads_the_chip_through_its_rom_bootloader_and_resets_it_back(tmp_path: Path, esptool_log: Path) -> None:
    config = esptool_config(tmp_path)

    result = call(config, "probe_target")

    assert result["ok"] is True, result
    assert result["chip_type"] == "ESP32-D0WD-V3 (revision v3.1)", result
    assert result["flash_size"] == "4MB", result
    assert result["held_in_rom_bootloader"] is False, result
    assert port_runs(esptool_log) == [["--chip=auto", f"--port={ESPTOOL_TEST_PORT}", "--before=default-reset", "--after=hard-reset", "--no-stub", "flash-id"]]


def test_a_flash_writes_the_image_at_its_offset_verifies_it_and_holds_the_chip(tmp_path: Path, esptool_log: Path) -> None:
    config = esptool_config(tmp_path)
    write_application(tmp_path)

    result = call(config, "flash_firmware", {"image_path": "build/app.bin"})

    assert result["ok"] is True, result
    assert result["held_in_rom_bootloader"] is True, result
    assert result["operation_result"]["matched_success_text"] == ["Hash of data verified.", "Staying in bootloader."], result
    [argv] = port_runs(esptool_log)
    assert argv[:-1] == ["--chip=auto", f"--port={ESPTOOL_TEST_PORT}", "--baud=460800", "--before=default-reset", "--after=no-reset", "write-flash", "--no-progress", "0x10000"], argv
    # The staged copy of the artifact, by absolute path, since esptool runs in a
    # directory of its own.
    assert Path(argv[-1]).is_absolute() and Path(argv[-1]).name == "app.bin", argv


def test_a_flash_with_its_reset_lets_the_application_run(tmp_path: Path, esptool_log: Path) -> None:
    write_application(tmp_path)

    result = call(esptool_config(tmp_path), "flash_firmware", {**FLASHED_APPLICATION, "reset_after_flash": True})

    assert result["ok"] is True, result
    assert result["held_in_rom_bootloader"] is False, result
    assert result["operation_result"]["matched_success_text"] == ["Hash of data verified.", "Hard resetting"], result
    [argv] = port_runs(esptool_log)
    assert "--after=hard-reset" in argv, argv


@pytest.mark.parametrize(("mode", "after", "held"), [("run", "hard-reset", False), ("halt", "no-reset", True)])
def test_reset_modes_differ_only_in_how_the_chip_is_let_go(tmp_path: Path, esptool_log: Path, mode: str, after: str, held: bool) -> None:
    config = esptool_config(tmp_path)

    result = call(config, "reset_target", {"mode": mode})

    assert result["ok"] is True, result
    assert result["held_in_rom_bootloader"] is held, result
    assert port_runs(esptool_log) == [["--chip=auto", f"--port={ESPTOOL_TEST_PORT}", "--before=default-reset", f"--after={after}", "--no-stub", "flash-id"]]


# ---------------------------------------------------------------------------
# What a failure proves about the chip. The port is the line between the two
# kinds: a failure before it opened moved nothing, and one after it may have
# reset the chip through the bridge's control lines.


def test_a_port_that_will_not_open_never_reached_the_chip(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SCENARIO, "port_busy")

    result = call(esptool_config(tmp_path), "probe_target")

    assert result["error_type"] == "com_port_open_failed", result
    assert result["backend_error_type"] == "port_open_failed", result
    assert result["target_contacted"] is False and result["retry_safe"] is True, result
    assert result.get("quarantined") is not True, result
    # Nothing to recover from, so nothing ran after it.
    assert len(port_runs(esptool_log)) == 1


def test_a_bootloader_that_never_answers_leaves_the_chip_unaccounted_for(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The port opened and the control lines moved, so the chip may have been
    reset on the way to a bootloader that never answered."""
    monkeypatch.setenv(SCENARIO, "no_serial_data")

    result = call(esptool_config(tmp_path, auto_recover="off"), "probe_target")

    assert result["error_type"] == "target_not_detected", result
    assert result.get("target_contacted") is not False, result
    assert result["retry_safe"] is False and result["side_effect_status"] == "unknown", result


def test_a_chip_of_another_family_is_refused_before_anything_is_written(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIC_HIL_FAKE_ESPTOOL_CHIP", "ESP32-S3")
    write_application(tmp_path)

    result = call(esptool_config(tmp_path, target_type="esp32", auto_recover="off"), "flash_firmware", FLASHED_APPLICATION)

    assert result["error_type"] == "target_type_invalid", result
    assert result["backend_error_type"] == "chip_mismatch", result
    [argv] = port_runs(esptool_log)
    assert "--chip=esp32" in argv, argv
    output = result["programmer_output"]["stdout"] + result["programmer_output"]["stderr"]
    assert "Wrong chip argument?" in output and "Wrote " not in output, output
    # The chip answered, so it was reset into its bootloader on the way.
    assert result["retry_safe"] is False, result


def test_a_chip_name_esptool_does_not_know_is_refused_before_the_port_opens(tmp_path: Path, esptool_log: Path) -> None:
    """`esp99` has the shape the loader checks and is still no chip esptool
    knows: click refuses the option before esptool opens anything."""
    result = call(esptool_config(tmp_path, target_type="esp99"), "probe_target")

    assert result["error_type"] == "target_type_invalid", result
    assert result["backend_error_type"] == "chip_argument_invalid", result
    assert result["target_contacted"] is False and result["retry_safe"] is True, result


def test_a_write_whose_read_back_differs_is_never_reported_as_flashed(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SCENARIO, "verify_mismatch")
    write_application(tmp_path)

    result = call(esptool_config(tmp_path, auto_recover="off"), "flash_firmware", FLASHED_APPLICATION)

    assert result["ok"] is False and result["error_type"] == "verify_failed", result
    assert "held_in_rom_bootloader" not in result, result
    assert result["retry_safe"] is False, result
    assert "MD5 of file does not match data in flash" in result["programmer_output"]["stderr"], result


@pytest.mark.parametrize(
    ("tool", "arguments", "error_type", "expected"),
    [
        pytest.param("probe_target", {}, "target_state_unconfirmed", ["Chip type:", "Detected flash size:", "Hard resetting"], id="probe"),
        pytest.param("flash_firmware", FLASHED_APPLICATION, "flash_failed", ["Hash of data verified.", "Staying in bootloader."], id="flash"),
        pytest.param("reset_target", {"mode": "run"}, "reset_failed", ["Hard resetting"], id="reset"),
    ],
)
def test_an_exit_status_of_zero_without_the_success_lines_is_no_success(
    tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict, error_type: str, expected: list[str]
) -> None:
    monkeypatch.setenv(SCENARIO, "silent")
    write_application(tmp_path)

    result = call(esptool_config(tmp_path, auto_recover="off"), tool, arguments)

    assert result["ok"] is False and result["error_type"] == error_type, result
    assert result["operation_result"] == {"confirmed": False, "expected_success_text": expected, "matched_success_text": []}, result
    assert result["retry_safe"] is False, result


def test_an_esptool_that_stops_answering_is_killed_at_its_deadline(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SCENARIO, "hang")

    result = call(esptool_config(tmp_path, timeout_s=1, auto_recover="off"), "probe_target")

    assert result["error_type"] == "timeout", result
    # Killed after it had started connecting, so where it stopped is unknown.
    assert result["retry_safe"] is False and result["side_effect_status"] == "unknown", result
    # The fake sleeps for a minute; the deadline is what ended it.
    assert result["elapsed_ms"] < scaled_time_bound(30) * 1000, result


@pytest.mark.parametrize("version", ["4.8.1", "6.0.0"])
def test_an_esptool_of_another_major_version_is_never_handed_the_port(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setenv("AGENTIC_HIL_FAKE_ESPTOOL_VERSION", version)

    result = call(esptool_config(tmp_path), "probe_target")

    assert result["error_type"] == "debugger_not_found", result
    assert result["backend_error_type"] == "esptool_version_unsupported", result
    assert result["version"] == version, result
    assert result["target_contacted"] is False, result
    assert [entry["argv"] for entry in runs(esptool_log)] == [["version"]]


def test_a_missing_esptool_is_named_and_nothing_runs(tmp_path: Path, esptool_log: Path) -> None:
    result = call(esptool_config(tmp_path, debugger_executable=tmp_path / "not-installed" / "esptool.exe"), "probe_target")

    assert result["error_type"] == "debugger_not_found", result
    assert result["backend_error_type"] == "esptool_not_found", result
    assert result["target_contacted"] is False and result["remediation"], result
    assert runs(esptool_log) == []


# ---------------------------------------------------------------------------
# Permissions: refused before esptool is started at all, version check included.


@pytest.mark.parametrize(
    ("tool", "arguments", "withheld"),
    [
        pytest.param("probe_target", {}, "allow_probe", id="probe-without-allow_probe"),
        # A probe resets the chip into its bootloader and out again.
        pytest.param("probe_target", {}, "allow_reset", id="probe-without-allow_reset"),
        pytest.param("flash_firmware", FLASHED_APPLICATION, "allow_flash", id="flash-without-allow_flash"),
        pytest.param("reset_target", {"mode": "run"}, "allow_reset", id="reset-without-allow_reset"),
    ],
)
def test_a_withheld_permission_refuses_before_esptool_runs(tmp_path: Path, esptool_log: Path, tool: str, arguments: dict, withheld: str) -> None:
    write_application(tmp_path)

    result = call(esptool_config(tmp_path, permissions={**DEFAULT_TEST_PERMISSIONS, withheld: False}), tool, arguments)

    assert result["error_type"] == "permission_denied", result
    assert result["permission"] == f"debuggers.dut.permissions.{withheld}", result
    assert runs(esptool_log) == []


@pytest.mark.parametrize("granted", ["allow_raw_debugger_commands", "allow_mass_erase"])
def test_a_flash_is_refused_while_a_grant_it_excludes_is_open(tmp_path: Path, esptool_log: Path, granted: str) -> None:
    write_application(tmp_path)

    result = call(esptool_config(tmp_path, permissions={**DEFAULT_TEST_PERMISSIONS, granted: True}), "flash_firmware", FLASHED_APPLICATION)

    assert result["error_type"] == "permission_denied", result
    assert result["permission"] == f"debuggers.dut.permissions.{granted}", result
    assert runs(esptool_log) == []


# ---------------------------------------------------------------------------
# One serial line, one holder: esptool's port is the COM session's port.


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        pytest.param("probe_target", {}, id="probe"),
        pytest.param("flash_firmware", FLASHED_APPLICATION, id="flash"),
        pytest.param("reset_target", {"mode": "run"}, id="reset"),
    ],
)
def test_this_servers_own_com_session_on_the_port_is_named_and_stopping_it_lets_the_call_through(
    tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict
) -> None:
    install_fake_serial(monkeypatch)
    write_application(tmp_path)
    service = AgenticHILToolService(esptool_config(tmp_path))
    try:
        assert service.call("com_session_start", {"port_id": "esp"})["ok"] is True

        refused = service.call(tool, arguments)

        assert refused["error_type"] == "device_busy", refused
        assert refused["held_by_com_session"] == "esp", refused
        assert "com_session_stop" in refused["next_step"], refused
        assert port_runs(esptool_log) == []

        assert service.call("com_session_stop", {"port_id": "esp"})["ok"] is True
        assert service.call(tool, arguments)["ok"] is True
    finally:
        service.close()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        pytest.param("probe_target", {}, id="probe"),
        pytest.param("flash_firmware", FLASHED_APPLICATION, id="flash"),
        pytest.param("reset_target", {"mode": "run"}, id="reset"),
    ],
)
def test_a_run_holding_the_debugger_and_the_port_still_keeps_esptool_off_its_own_session(
    tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict
) -> None:
    """The bench's sequence: one run declares both names of the one line, so the run's hold cannot be what refuses."""
    install_fake_serial(monkeypatch)
    write_application(tmp_path)
    service = AgenticHILToolService(esptool_config(tmp_path))
    try:
        started = service.call("bench_run_start", {"devices": [{"kind": "debugger", "id": "dut"}, {"kind": "uart", "id": "esp"}]})
        assert started["ok"] is True, started
        assert service.call(tool, arguments)["ok"] is True
        assert service.call("com_session_start", {"port_id": "esp"})["ok"] is True

        refused = service.call(tool, arguments)

        assert refused["error_type"] == "device_busy", refused
        assert refused["held_by_com_session"] == "esp", refused
        assert len(port_runs(esptool_log)) == 1

        assert service.call("com_session_stop", {"port_id": "esp"})["ok"] is True
        assert service.call(tool, arguments)["ok"] is True
        assert service.call("bench_run_stop")["ok"] is True
    finally:
        service.close()


def port_known_by_a_second_name(tmp_path: Path) -> str:
    """A device name the host also gives the port under another spelling.

    The bench stage names the board's bridge by its `/dev/serial/by-id` link,
    which the host resolves to the kernel node behind it; Windows has the same
    pair in a port's device namespace spelling."""
    if os.name == "nt":
        return "\\\\.\\" + ESPTOOL_TEST_PORT
    node = tmp_path / "dev" / "ttyUSB0"
    link = tmp_path / "dev" / "serial" / "by-id" / "usb-1a86_USB_Serial-if00-port0"
    link.parent.mkdir(parents=True)
    node.write_text("", encoding="utf-8")
    link.symlink_to(node)
    return str(link)


@pytest.mark.parametrize("in_a_run", [True, False], ids=["declared-run", "bare-call"])
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        pytest.param("probe_target", {}, id="probe"),
        pytest.param("flash_firmware", FLASHED_APPLICATION, id="flash"),
        pytest.param("reset_target", {"mode": "run"}, id="reset"),
    ],
)
def test_a_port_named_by_a_link_is_held_under_both_names_and_declared_under_its_own(
    tmp_path: Path, esptool_log: Path, tool: str, arguments: dict, in_a_run: bool
) -> None:
    """The bench's first run: inside a run that declared the line, the flash was
    refused as `undeclared_device` over the node the link leads to. That name is
    held with the line and never declared, so it is not what a call is checked
    against, and a run holding the line still keeps everybody else off it."""
    write_application(tmp_path)
    config = esptool_config(tmp_path, com_ports_yaml=f"com_ports:\n  esp:\n    device: {json.dumps(port_known_by_a_second_name(tmp_path))}\n    baudrate: 115200\n")
    held, declared = debugger_device(config).lock_keys, debugger_device(config).declared_keys
    assert len(held) == 2 and declared == held[:1], (held, declared)
    service = AgenticHILToolService(config)
    try:
        if in_a_run:
            started = service.call("bench_run_start", {"devices": [{"kind": "debugger", "id": "dut"}, {"kind": "uart", "id": "esp"}]})
            assert started["ok"] is True, started
            assert started["declared_devices"] == list(declared), started
            with pytest.raises(DeviceBusyError):
                BenchMutex(frontend="stranger").acquire([held[1]])

        result = service.call(tool, arguments)

        assert result["ok"] is True, result
        assert len(port_runs(esptool_log)) == 1
        if in_a_run:
            assert service.call("bench_run_stop")["ok"] is True
    finally:
        service.close()


def test_another_servers_com_session_on_the_port_refuses_without_naming_a_session_of_ours(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_serial(monkeypatch)
    other = AgenticHILToolService(load_config(str(write_config(tmp_path / "other", com_ports_yaml=ESPTOOL_COM_PORTS_YAML, state_root=tmp_path / "other-state"))))
    service = AgenticHILToolService(esptool_config(tmp_path / "esp"))
    try:
        assert other.call("com_session_start", {"port_id": "esp"})["ok"] is True

        refused = service.call("probe_target")

        assert refused["error_type"] == "device_busy", refused
        assert "held_by_com_session" not in refused, refused
        assert port_runs(esptool_log) == []
    finally:
        service.close()
        other.close()


def test_a_capture_on_the_port_esptool_flashes_through_is_refused_before_anything_runs(tmp_path: Path, esptool_log: Path) -> None:
    write_application(tmp_path)

    result = call(esptool_config(tmp_path), "flash_firmware", {**FLASHED_APPLICATION, "reset_after_flash": True, "capture": {"port_id": "esp"}})

    assert result["error_type"] == "invalid_argument", result
    assert result["field"] == "capture.port_id", result
    assert result["side_effect_status"] == "not_started", result
    assert runs(esptool_log) == []


def test_a_foreign_adapter_behind_the_port_name_is_refused_before_its_control_lines_move(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A CH340 publishes no serial number, so the entry names its type, and an
    adapter of another type that enumerated under the same name is refused."""
    ch340 = f'com_ports:\n  esp:\n    device: "{ESPTOOL_TEST_PORT}"\n    vid: "1a86"\n    pid: "7523"\n'
    write_application(tmp_path)
    config = esptool_config(tmp_path, com_ports_yaml=ch340)
    fake_host(monkeypatch, inventory(host_port(ESPTOOL_TEST_PORT, None, vid=STLINK_VID, pid=STLINK_PID)))

    refused = call(config, "flash_firmware", FLASHED_APPLICATION)

    assert refused["error_type"] == "com_port_identity_mismatch", refused
    assert refused["expected_vid"] == CH340_VID and refused["found_vid"] == STLINK_VID, refused
    assert refused["target_contacted"] is False and refused["retry_safe"] is True, refused
    assert port_runs(esptool_log) == []

    fake_host(monkeypatch, inventory(host_port(ESPTOOL_TEST_PORT, None, vid=CH340_VID, pid=CH340_PID)))
    assert call(config, "flash_firmware", FLASHED_APPLICATION)["ok"] is True


# ---------------------------------------------------------------------------
# What esptool reads besides its command line, and what comes back out of it.


def test_esptool_runs_with_none_of_the_operators_esptool_settings(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A port, chip or baud rate left in an operator's shell, or a configuration
    file in the workspace, must not decide what a flash does."""
    operator_file = tmp_path / "esptool.cfg"
    operator_file.write_text("[esptool]\nconnect_attempts = 0\n", encoding="utf-8")
    for name, value in {
        "ESPTOOL_PORT": "COM1",
        "ESPTOOL_CHIP": "esp32s3",
        "ESPTOOL_BAUD": "921600",
        "ESPTOOL_CFGFILE": str(operator_file),
        "IDF_PATH": str(tmp_path),
        "ESP_IDE_WS": "1",
        "FORCE_COLOR": "1",
    }.items():
        monkeypatch.setenv(name, value)

    assert call(esptool_config(tmp_path), "probe_target")["ok"] is True

    recorded = runs(esptool_log)
    assert [entry["argv"][-1] for entry in recorded] == ["version", "flash-id"], recorded
    for entry in recorded:
        assert set(entry["environment"]) == {"ESPTOOL_CFGFILE", "NO_COLOR", "COLUMNS", "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONUNBUFFERED"}, entry
        # A private, empty configuration in a private working directory, which
        # is gone once the run is.
        assert entry["config_text"] == "[esptool]\n", entry
        assert Path(entry["config_file"]).parent.name == Path(entry["cwd"]).name, entry
        assert Path(entry["cwd"]).name.startswith("agentic-hil-esptool-"), entry
        assert not Path(entry["cwd"]).exists(), entry


def test_no_mac_address_or_private_directory_reaches_a_result_a_log_or_a_report(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """esptool prints the chip's factory MAC on every connect and its private
    configuration file by absolute path; neither is kept anywhere."""
    monkeypatch.setenv(SCENARIO, "verify_mismatch")
    write_application(tmp_path)
    config = esptool_config(tmp_path, auto_recover="off")

    result = call(config, "flash_firmware", FLASHED_APPLICATION)

    assert result["error_type"] == "verify_failed", result
    written = [json.dumps(result)]
    for directory in (logs_directory(config), reports_directory(config)):
        written += [path.read_text(encoding="utf-8") for path in Path(directory).rglob("*") if path.is_file()]
    assert any(REDACTED_MAC in text for text in written), written
    assert any(REDACTED_WORKDIR in text for text in written), written
    for text in written:
        assert FAKE_MAC not in text.lower(), text
        assert "agentic-hil-esptool-" not in text, text


# ---------------------------------------------------------------------------
# The bootloader hold: a halt is not undone by the next probe.


def test_a_probe_leaves_a_chip_that_was_halted_or_flashed_in_its_bootloader(tmp_path: Path, esptool_log: Path) -> None:
    write_application(tmp_path)
    service = AgenticHILToolService(esptool_config(tmp_path))
    try:
        assert service.call("reset_target", {"mode": "halt"})["ok"] is True
        assert service.call("probe_target")["held_in_rom_bootloader"] is True
        assert service.call("reset_target", {"mode": "run"})["ok"] is True
        assert service.call("probe_target")["held_in_rom_bootloader"] is False
        assert service.call("flash_firmware", FLASHED_APPLICATION)["ok"] is True
        assert service.call("probe_target")["held_in_rom_bootloader"] is True
    finally:
        service.close()

    afters = [next(argument for argument in argv if argument.startswith("--after=")) for argv in port_runs(esptool_log)]
    assert afters == ["--after=no-reset", "--after=no-reset", "--after=hard-reset", "--after=hard-reset", "--after=no-reset", "--after=no-reset"], afters


def test_a_failure_that_never_reached_the_chip_keeps_the_hold_and_any_other_forgets_it(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = AgenticHILToolService(esptool_config(tmp_path, auto_recover="off"))
    try:
        assert service.call("reset_target", {"mode": "halt"})["ok"] is True

        monkeypatch.setenv(SCENARIO, "port_busy")
        assert service.call("probe_target")["target_contacted"] is False
        monkeypatch.delenv(SCENARIO)
        assert service.call("probe_target")["held_in_rom_bootloader"] is True

        monkeypatch.setenv(SCENARIO, "stopped_responding")
        assert service.call("reset_target", {"mode": "halt"})["retry_safe"] is False
        monkeypatch.delenv(SCENARIO)
        assert service.call("probe_target")["held_in_rom_bootloader"] is False
    finally:
        service.close()

    assert "--after=hard-reset" in port_runs(esptool_log)[-1]


def test_a_failed_flash_recovers_the_bench_by_holding_the_chip_in_its_bootloader(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bench's default recovery, a reset into halt read back by a probe, is
    a reset into the ROM bootloader here, and the probe leaves the chip there."""
    monkeypatch.setenv(SCENARIO, "verify_mismatch")
    write_application(tmp_path)

    result = call(esptool_config(tmp_path), "flash_firmware", FLASHED_APPLICATION)

    assert result["ok"] is False and result["error_type"] == "verify_failed", result
    recovery = result["recovery"]
    assert recovery["outcome"] == "recovered", recovery
    assert recovery["actions"] == ["reap_processes", "reset_halt", "probe_target"], recovery
    assert result["quarantined"] is False, result
    flashed, reset, probed = port_runs(esptool_log)
    assert "write-flash" in flashed, flashed
    assert reset[-3:] == ["--after=no-reset", "--no-stub", "flash-id"], reset
    assert probed[-3:] == ["--after=no-reset", "--no-stub", "flash-id"], probed


def test_a_readonly_recovery_never_re_reads_a_chip_its_probe_would_reset(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`readonly` drives nothing physical, and the re-read it allows is a reset
    of the chip here, so the recovery names why it did not run instead of
    running it."""
    monkeypatch.setenv(SCENARIO, "verify_mismatch")
    write_application(tmp_path)

    result = call(esptool_config(tmp_path, auto_recover="readonly"), "flash_firmware", FLASHED_APPLICATION)

    assert result["ok"] is False and result["error_type"] == "verify_failed", result
    recovery = result["recovery"]
    assert recovery["attempted"] is False, recovery
    assert recovery["reason_not_attempted"] == "probe_resets_target", recovery
    assert "readonly" in recovery["summary"], recovery
    assert result["quarantined"] is False, result
    (flashed,) = port_runs(esptool_log)
    assert "write-flash" in flashed, flashed


@pytest.mark.parametrize("auto_recover", ["readonly", "reset_halt"])
def test_an_incident_only_a_re_read_settles_never_resets_the_chip_to_settle_it(tmp_path: Path, esptool_log: Path, auto_recover: str) -> None:
    """A reason that names nothing unconfirmed about the target asks for a
    re-read and for no physical act, under either policy, and a re-read through
    esptool is a reset. The automatic recovery leaves it to stand down; the one
    reset that reaches the chip is the probe the caller asked for."""
    service = AgenticHILToolService(esptool_config(tmp_path, auto_recover=auto_recover))
    try:
        lease = service.coordinator.acquire(*debugger_effect_resources(service.config))
        lease.quarantine(DEBUGGER_READONLY_RESULT_REASON)
        service._quarantined_lease = lease

        assert service._attempt_machine_recovery() is None
        assert service._machine_recovery_ran is False
        assert port_runs(esptool_log) == []

        probed = service.call("probe_target")

        assert probed["ok"] is True, probed
        assert service.coordinator.blocked is False
        assert len(port_runs(esptool_log)) == 1, port_runs(esptool_log)
    finally:
        service.close()


# ---------------------------------------------------------------------------
# The image: what esptool would write where, settled before it runs.


@pytest.mark.parametrize(
    ("flash_address", "named"),
    [
        pytest.param(None, "requires debuggers.dut.flash_address", id="unset"),
        pytest.param("0x10001", "'0x10001'", id="inside-a-sector"),
        pytest.param("010000", "'010000'", id="leading-zero"),
    ],
)
def test_a_bin_needs_an_offset_that_starts_a_flash_sector(tmp_path: Path, esptool_log: Path, flash_address: str | None, named: str) -> None:
    write_application(tmp_path)

    result = call(esptool_config(tmp_path, flash_address=flash_address), "flash_firmware", FLASHED_APPLICATION)

    assert result["error_type"] == "invalid_argument", result
    assert result["field"] == "debuggers.dut.flash_address", result
    assert named in result["summary"], result
    assert runs(esptool_log) == []


def test_a_bin_that_starts_like_intel_hex_is_refused(tmp_path: Path, esptool_log: Path) -> None:
    """esptool reads a file that starts with ':' as Intel HEX whatever its name,
    and would write it at the records' addresses rather than at the offset."""
    write_application(tmp_path, data=intel_hex(0x10000, APPLICATION).encode("ascii"))

    result = call(esptool_config(tmp_path), "flash_firmware", FLASHED_APPLICATION)

    assert result["error_type"] == "invalid_argument", result
    assert "starts with ':'" in result["summary"], result
    assert runs(esptool_log) == []


def test_an_intel_hex_file_is_written_at_the_addresses_its_records_carry(tmp_path: Path, esptool_log: Path) -> None:
    write_application(tmp_path, "app.hex", intel_hex(0x10000, APPLICATION).encode("ascii"))

    # No flash_address: the records carry theirs.
    result = call(esptool_config(tmp_path, flash_address=None), "flash_firmware", {"image_path": "build/app.hex"})

    assert result["ok"] is True, result
    [argv] = port_runs(esptool_log)
    assert argv[-3:-1] == ["--no-progress", "0x0"], argv
    assert Path(argv[-1]).name == "app.hex", argv


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        pytest.param(intel_hex(0x10000, APPLICATION, end=False), "no end-of-file record", id="cut-short"),
        pytest.param(intel_hex(0x10000, APPLICATION) + hex_record(0, 0, bytes(16)), "follows the end-of-file record", id="after-the-end"),
        pytest.param(intel_hex(0x10010, APPLICATION), "starts inside a 4 KiB flash sector", id="inside-a-sector"),
        pytest.param(intel_hex(0x10000, APPLICATION, end=False) + hex_record(0, 0, bytes(16)) + hex_record(1, 0), "more than once", id="written-twice"),
    ],
)
def test_an_intel_hex_file_esptool_would_write_otherwise_than_it_reads_is_refused(tmp_path: Path, esptool_log: Path, text: str, problem: str) -> None:
    """esptool falls back to a raw write when the HEX library refuses a file,
    and drops what follows the end record, so these are refused here."""
    write_application(tmp_path, "app.hex", text.encode("ascii"))

    result = call(esptool_config(tmp_path), "flash_firmware", {"image_path": "build/app.hex"})

    assert result["error_type"] == "invalid_argument", result
    assert problem in result["summary"], result
    assert runs(esptool_log) == []


def test_an_elf_is_refused_with_the_commands_that_turn_it_into_an_image(tmp_path: Path, esptool_log: Path) -> None:
    write_application(tmp_path, "app.elf", b"\x7fELF" + bytes(12))

    result = call(esptool_config(tmp_path), "flash_firmware", {"image_path": "build/app.elf"})

    assert result["error_type"] == "invalid_argument", result
    assert "elf2image" in result["summary"] and "merge-bin" in result["summary"], result
    assert runs(esptool_log) == []


# ---------------------------------------------------------------------------
# The configuration: the chip name put to esptool, and the entries refused.


def test_the_chip_name_is_put_to_esptool_without_opening_a_port(tmp_path: Path, esptool_log: Path) -> None:
    supported = EsptoolBackend(esptool_config(tmp_path / "supported", target_type="esp32")).target_support()
    unsupported = EsptoolBackend(esptool_config(tmp_path / "unsupported", target_type="esp99")).target_support()
    unset = EsptoolBackend(esptool_config(tmp_path / "unset")).target_support()
    missing = EsptoolBackend(esptool_config(tmp_path / "missing", target_type="esp32", debugger_executable=tmp_path / "not-installed" / "esptool.exe")).target_support()

    assert supported["status"] == "supported", supported
    assert unsupported["status"] == "unsupported" and unsupported["error_type"] == "target_type_invalid", unsupported
    assert "esp32" in unsupported["close_matches"] and unsupported["supported_chips"][:3] == ["auto", "esp8266", "esp32"], unsupported
    assert unset["status"] == "not_configured", unset
    # A host that cannot answer says nothing about the configuration.
    assert missing["status"] == "undetermined" and missing["ok"] is True, missing
    assert not any(argument.startswith("--port") for entry in runs(esptool_log) for argument in entry["argv"]), runs(esptool_log)


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        pytest.param({"com_port": None}, "debuggers.dut.com_port", id="no-port"),
        pytest.param({"com_port": "elsewhere"}, "debuggers.dut.com_port", id="unknown-port"),
        pytest.param({"probe_id": "E66038B713849D31"}, "debuggers.dut.probe_id", id="probe-id"),
        pytest.param(
            {"debuggers_yaml": yaml.safe_dump({"debuggers": {"dut": {"type": "esptool", "executable": FAKE_ESPTOOL.as_posix(), "com_port": "esp", "resource_id": "esp32-cam"}}})},
            "debuggers.dut.resource_id",
            id="resource-id",
        ),
        # Spelt as esptool prints it rather than as `--chip` takes it.
        pytest.param({"target_type": "ESP32-S3"}, "debuggers.dut.target_type", id="chip-name-shape"),
    ],
)
def test_the_loader_refuses_an_esptool_entry_it_could_not_act_on_safely(tmp_path: Path, kwargs: dict, field: str) -> None:
    with pytest.raises(ConfigError) as refused:
        esptool_config(tmp_path, **kwargs)

    assert refused.value.error_type == "config_invalid", refused.value.summary
    assert refused.value.details["field"] == field, refused.value.details


def test_the_loader_refuses_a_port_named_on_a_probe_backend(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as refused:
        load_config(str(write_config(tmp_path, com_ports_yaml=ESPTOOL_COM_PORTS_YAML, com_port="esp")))

    assert refused.value.details["field"] == "debuggers.dut.com_port", refused.value.details


def test_the_loader_refuses_two_esptool_entries_on_one_serial_line(tmp_path: Path) -> None:
    second = yaml.safe_dump({"debuggers": {"other": {"type": "esptool", "executable": FAKE_ESPTOOL.as_posix(), "com_port": "esp"}}})

    with pytest.raises(ConfigError) as refused:
        esptool_config(tmp_path, debuggers_yaml=second)

    assert "com_ports entry of its own" in refused.value.summary, refused.value.summary


# ---------------------------------------------------------------------------
# A plan: the reactor makes the same calls, and the console takes its turn on
# the line esptool flashes through.


class BannerSerialHandle:
    """A bridge whose board has printed its banner by the time the port opens."""

    def __init__(self, *args, **kwargs) -> None:
        self.is_open = False
        self.in_waiting = 0
        self.exclusive = None
        self.pending = b""

    def open(self) -> None:
        self.is_open = True
        self.pending = b"PONG A\r\n"

    def read(self, size: int) -> bytes:
        chunk, self.pending = self.pending[:size], self.pending[size:]
        return chunk

    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        return None

    def cancel_read(self) -> None:
        return None

    def close(self) -> None:
        self.is_open = False


def write_plan(workspace: Path, text: str) -> Path:
    path = workspace / ".agentic-hil" / "testconfig.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_plan_flashes_reads_the_console_and_resets_on_one_serial_line(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=BannerSerialHandle))
    write_application(tmp_path)
    plan = write_plan(
        tmp_path,
        """version: 6
name: esp32-console
steps:
  - {device: dut, action: flash, image_path: build/app.bin, reset_after_flash: true}
  - {device: esp, action: uart_open}
  - {device: esp, action: uart_read, comparator: {pattern: "PONG A"}, timeout_s: 2}
  - {device: esp, action: uart_close}
  - {device: dut, action: reset}
""",
    )
    service = AgenticHILToolService(esptool_config(tmp_path))
    try:
        result = TestReactor(service.config, service).run(load_test_config(str(plan), str(tmp_path)))
    finally:
        service.close()

    assert result["ok"] is True, result
    flashed, reset = port_runs(esptool_log)
    assert "write-flash" in flashed and "--after=hard-reset" in flashed, flashed
    assert reset[-3:] == ["--after=hard-reset", "--no-stub", "flash-id"], reset


def test_a_plan_with_a_debug_step_is_refused_before_esptool_runs(tmp_path: Path, esptool_log: Path) -> None:
    write_application(tmp_path)
    plan = write_plan(
        tmp_path,
        """version: 6
name: esp32-debug
steps:
  - {device: dut, action: flash, image_path: build/app.bin}
  - {device: dut, action: read_symbol, symbol: counter, size_bytes: 4, comparator: {equals: 1}}
""",
    )
    service = AgenticHILToolService(esptool_config(tmp_path))
    try:
        result = TestReactor(service.config, service).run(load_test_config(str(plan), str(tmp_path)))
    finally:
        service.close()

    assert result["ok"] is False, result
    refused = result["validation_error"]
    assert (refused["step"], refused["action"], refused["debugger_type"]) == (2, "read_symbol", "esptool"), refused
    assert refused["sessionless_debug_reads"] == [] and "way_out" not in refused, refused
    # No configuration change gives this entry a debug route, so the way on is
    # the claim the board can still answer: what it prints.
    assert "uart_read" in refused["next_step"], refused
    assert runs(esptool_log) == [], runs(esptool_log)


def test_a_plan_that_flashes_while_its_console_is_open_names_the_session_in_the_way(tmp_path: Path, esptool_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=BannerSerialHandle))
    write_application(tmp_path)
    plan = write_plan(
        tmp_path,
        """version: 6
name: esp32-console-open
steps:
  - {device: esp, action: uart_open}
  - {device: dut, action: flash, image_path: build/app.bin}
""",
    )
    service = AgenticHILToolService(esptool_config(tmp_path))
    try:
        result = TestReactor(service.config, service).run(load_test_config(str(plan), str(tmp_path)))
    finally:
        service.close()

    assert result["ok"] is False, result
    refused = result["steps"][1]["result"]
    assert refused["error_type"] == "device_busy" and refused["held_by_com_session"] == "esp", refused
    assert "com_session_stop" in refused["next_step"], refused
    # The run's cleanup closes the console, and only then does the failed run's
    # recovery reach the chip: the default policy resets it into its ROM
    # bootloader and reads it back there. The flash itself never ran.
    assert [entry["action"] for entry in result["cleanup"]] == ["uart_close"], result["cleanup"]
    assert result["recovery"]["actions"] == ["reap_processes", "reset_halt", "probe_target"], result["recovery"]
    assert result["recovery"]["outcome"] == "recovered", result["recovery"]
    held = ["--before=default-reset", "--after=no-reset", "--no-stub", "flash-id"]
    assert [run[-4:] for run in port_runs(esptool_log)] == [held, held], port_runs(esptool_log)
