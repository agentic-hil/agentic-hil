"""Sessions, devices and coordination: payloads, refusals and fields no test read (#508).

Each test here was written from the issue text before any change, and each
names the code it pins. A hex stimulus and its refusal, a base64 upload and a
flash by the id it returned, every `hardware_recover` refusal and its
`agentic-hil recover` spelling, the pytest fixture's teardown, a corrupt audit
ledger sidecar, a corrupt report-state file, a serial device this user may
not open, an undeclared port or bus, the holder a `device_busy` refusal names
and the CLI's passthrough of it, the two optional extras' own refusals, and a
broker whose adapter cannot open. Everything runs against the shipped
fakes and files under a temporary state root; what needs a real terminal
device, a real CAN interface or a real absence of python-can is in
tests/container beside this.
"""

from __future__ import annotations

import base64
import errno
import json
import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FAKE_GDB, write_authoritative_config, write_config
from test_can_broker import broker_diagnostics, reaped_brokers  # noqa: F401  (fixture)
from test_contact_marker import FakeHandle, FakePort, install_fake_serial
from test_debugger_processes import authoritative_config_with_executable_spelling, path_without_a_toolchain
from test_pytest_plugin import PLUGIN_ARGS

from agentic_hil.artifacts import decode_base64_payload
from agentic_hil.bench import HEARTBEAT_INTERVAL_S, BenchMutex, DeviceBusyError, resource_digest
from agentic_hil.canbroker import (
    BROKER_EXIT_ADAPTER,
    ParticipantError,
    attach_participant,
    broker_log_path,
    bus_lock_key,
)
from agentic_hil.cli import _holds_from_collision, build_parser, dispatch
from agentic_hil.comports import likely_causes, payload_bytes
from agentic_hil.config import ConfigError, load_authoritative_config, load_config
from agentic_hil.coordination import HardwareCoordinator
from agentic_hil.report import (
    append_jsonl,
    canonical_audit_log_path,
    logs_directory,
    report_state_path,
)
from agentic_hil.tools import AgenticHILToolService

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# Device locks are machine-wide, so every port, device and bus here is named
# for this file alone and contends with nothing another checkout runs.
PORT_ID = "sdc_uart"
DEVICE = "/dev/ttySDC0"
COM_PORT_YAML = f'com_ports:\n  {PORT_ID}:\n    device: "{DEVICE}"\n'
BUS_ID = "sdc_bus"
PEAK_BUS_YAML = f'can_buses:\n  {BUS_ID}:\n    adapter: "peak"\n    channel: "can0"\n'
BOARD = "physical:sdc-board"

# One Intel HEX record with a correct checksum and the end-of-file record, the
# smallest image the artifact validator reads as parseable.
INTEL_HEX = b":020000040800F2\n:00000001FF\n"


def service_for(tmp_path: Path, **kwargs) -> AgenticHILToolService:
    return AgenticHILToolService(load_config(str(write_config(tmp_path, **kwargs))))


# ---------------------------------------------------------------------------
# com_write with a `hex` payload, and its refusal.


class RecordingHandle(FakeHandle):
    """The fake serial handle, remembering every byte written to it."""

    def __init__(self, port: FakePort) -> None:
        super().__init__(port)
        self.written: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)


class RecordingPort(FakePort):
    def __init__(self) -> None:
        super().__init__()
        self.handles: list[RecordingHandle] = []

    def handle(self) -> RecordingHandle:
        handle = RecordingHandle(self)
        self.handles.append(handle)
        return handle


def test_com_write_hex_payload_bytes_and_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`com_write {hex}` writes exactly those bytes, whitespace ignored; odd or non-hex text is refused."""
    config = load_config(str(write_config(tmp_path, com_ports_yaml=COM_PORT_YAML)))
    port_config = config.com_ports[PORT_ID]

    assert payload_bytes(port_config, {"hex": "48 65\n6c"}) == {"ok": True, "data": b"Hel"}
    for bad in ("abc", "zz"):
        refused = payload_bytes(port_config, {"hex": bad})
        assert refused["ok"] is False, refused
        assert refused["error_type"] == "invalid_argument", refused
        assert refused["summary"] == "hex must contain valid hexadecimal bytes.", refused
        assert refused["tool"] == "com_write", refused

    service = AgenticHILToolService(config)
    port = RecordingPort()
    install_fake_serial(monkeypatch, port)
    try:
        started = service.call("com_session_start", {"port_id": PORT_ID})
        assert started["ok"] is True, started
        written = service.call("com_write", {"port_id": PORT_ID, "hex": "48 65\n6c"})
        assert written["ok"] is True, written
        assert written["bytes_written"] == 3, written
        refused = service.call("com_write", {"port_id": PORT_ID, "hex": "abc"})
        assert refused["ok"] is False, refused
        assert refused["error_type"] == "invalid_argument", refused
        assert refused["summary"] == "hex must contain valid hexadecimal bytes.", refused
    finally:
        service.close()
    # Through the service the handle received exactly those bytes, and the
    # refused payload never reached it.
    assert b"".join(port.handles[-1].written) == b"Hel", port.handles[-1].written


# ---------------------------------------------------------------------------
# A peak bus on Linux whose channel is neither a PCAN handle nor a netdev
# name, and python-can missing.
#
# The channel rule reads the shape: a kernel netdev name (`can0`, `vcan0`,
# `slcan0`) opens through SocketCAN, a PCANBasic handle (`PCAN_USBBUS1`,
# `0x51`) opens through `libpcanbasic` on Linux too and is pinned as admitted
# by test_can_frame_and_routing's
# test_a_peak_bus_naming_a_pcanbasic_handle_still_opens_through_pcan. What
# the rule refuses is a channel of neither shape, before the library is asked.


class HostOs:
    """The `os` module as a driver module sees it on one host: `name` is fixed, the rest is the real module."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attribute: str):
        return getattr(os, attribute)


def test_can_session_start_without_python_can_and_with_a_peak_channel_of_neither_shape_on_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both refusals are structured, side_effect_committed false, and neither is an ImportError."""
    bus_yaml = (
        "can_buses:\n"
        f"  {BUS_ID}:\n"
        '    adapter: "peak"\n'
        '    channel: "can0"\n'
        f"  {BUS_ID}_neither_shape:\n"
        '    adapter: "peak"\n'
        '    channel: "usb1"\n'
    )
    service = service_for(tmp_path, can_buses_yaml=bus_yaml)
    try:
        # The rule is the POSIX one whichever host runs the suite: `os.name`
        # is what the code reads, and only the CAN module sees the POSIX name.
        monkeypatch.setattr("agentic_hil.can.os", HostOs("posix"))
        monkeypatch.setitem(sys.modules, "can", None)
        without_backend = service.call("can_session_start", {"bus_id": BUS_ID})
        assert without_backend["ok"] is False, without_backend
        assert without_backend["error_type"] == "can_backend_not_available", without_backend
        assert without_backend["summary"] == "python-can is not installed. Install agentic-hil[can] to use direct CAN adapters.", without_backend
        assert without_backend["side_effect_committed"] is False, without_backend

        # With python-can present, a channel of neither shape on a Linux host
        # is refused by the channel rule before the library is asked.
        monkeypatch.setitem(sys.modules, "can", SimpleNamespace(Bus=lambda **kwargs: (_ for _ in ()).throw(AssertionError("the channel rule refuses before python-can is asked"))))
        refused = service.call("can_session_start", {"bus_id": f"{BUS_ID}_neither_shape"})
        assert refused["ok"] is False, refused
        assert refused["error_type"] == "config_invalid", refused
        assert refused["field"] == f"can_buses.{BUS_ID}_neither_shape.channel", refused
        assert refused["summary"] == "PEAK adapter on Linux expects a SocketCAN-style interface name such as can0.", refused
        assert refused["side_effect_committed"] is False, refused
    finally:
        service.close()


# ---------------------------------------------------------------------------
# artifact_upload with an inline base64 payload.


def uploaded_files(service: AgenticHILToolService) -> list[Path]:
    upload_directory = Path(service.config.work_dir) / service.config.artifacts.upload_directory
    return sorted(upload_directory.iterdir()) if upload_directory.is_dir() else []


def test_artifact_upload_by_base64_stores_and_refuses(tmp_path: Path) -> None:
    """A valid upload lands as <sha256>.<ext> under upload_directory; every refusal writes nothing."""
    service = service_for(tmp_path)
    try:
        # Whitespace inside the base64 is tolerated, as the schema description promises.
        encoded = base64.b64encode(INTEL_HEX).decode("ascii")
        spaced = encoded[:8] + "\n" + encoded[8:]
        uploaded = service.call("artifact_upload", {"filename": "fw.hex", "data_base64": spaced})
        assert uploaded["ok"] is True, uploaded
        assert uploaded["artifact_id"].endswith(".hex"), uploaded
        assert re.fullmatch(r"[a-f0-9]{64}\.hex", uploaded["artifact_id"]), uploaded
        stored = Path(service.config.work_dir) / service.config.artifacts.upload_directory / uploaded["artifact_id"]
        assert stored.is_file(), stored
        assert stored.read_bytes() == INTEL_HEX
        assert uploaded["artifact"]["source"] == "upload", uploaded
        assert uploaded["artifact"]["original_filename"] == "fw.hex", uploaded
        before = uploaded_files(service)

        refusals = {
            ("a/b.hex", encoded): "filename must not contain path separators or traversal segments.",
            ("fw.hex", "abc"): "data_base64 must contain valid padded base64 data.",
        }
        for (filename, data), summary in refusals.items():
            refused = service.call("artifact_upload", {"filename": filename, "data_base64": data})
            assert refused["ok"] is False, refused
            assert refused["error_type"] == "invalid_argument", refused
            assert refused["summary"] == summary, refused
        # An empty payload, which is also what base64 of b"" is, is refused at
        # the tool boundary and by the decoder alike.
        empty = service.call("artifact_upload", {"filename": "fw.hex", "data_base64": ""})
        assert empty["ok"] is False, empty
        assert empty["error_type"] == "invalid_argument", empty
        assert decode_base64_payload("") == {"ok": False, "tool": "artifact_upload", "error_type": "invalid_argument", "summary": "data_base64 must be a non-empty base64 string."}
        assert decode_base64_payload(base64.b64encode(b"").decode("ascii"))["error_type"] == "invalid_argument"
        assert uploaded_files(service) == before
    finally:
        service.close()


# ---------------------------------------------------------------------------
# flash_firmware and debug_start_session by artifact_id.


def test_flash_by_artifact_id_round_trip_and_refusals(tmp_path: Path) -> None:
    """An uploaded artifact is flashable by id, and the id's refusals name the tool and the id."""
    service = service_for(tmp_path, gdb_executable=FAKE_GDB)
    try:
        uploaded = service.call("artifact_upload", {"filename": "fw.hex", "data_base64": base64.b64encode(INTEL_HEX).decode("ascii")})
        assert uploaded["ok"] is True, uploaded
        artifact_id = uploaded["artifact_id"]

        flashed = service.call("flash_firmware", {"artifact_id": artifact_id})
        assert flashed["ok"] is True, flashed
        assert flashed["artifact"]["source"] == "upload", flashed
        assert flashed["artifact"]["path"].endswith(artifact_id), flashed
        assert flashed["artifact"]["sha256"] == artifact_id.split(".")[0], flashed

        unknown = "0" * 64 + ".hex"
        not_found = service.call("flash_firmware", {"artifact_id": unknown})
        assert not_found["ok"] is False, not_found
        assert not_found["error_type"] == "artifact_not_found", not_found
        assert not_found["artifact_id"] == unknown, not_found
        assert not_found["tool"] == "flash_firmware", not_found

        unsafe = service.call("flash_firmware", {"artifact_id": "../fw.hex"})
        assert unsafe["ok"] is False, unsafe
        assert unsafe["error_type"] == "invalid_argument", unsafe
        assert unsafe["summary"] == "artifact_id must be a safe uploaded artifact id.", unsafe
        assert unsafe["artifact_id"] == "../fw.hex", unsafe

        # Both or neither of the two is refused as invalid_argument. Over the
        # tool boundary the schema's oneOf answers first, naming the document
        # root; the tool's own line is the one a direct caller meets.
        for tool, method in (("flash_firmware", service.flash_firmware), ("debug_start_session", service.debug_start_session)):
            for arguments in ({"image_path": "build/app.hex", "artifact_id": artifact_id}, {}):
                refused = service.call(tool, arguments)
                assert refused["ok"] is False, (tool, refused)
                assert refused["error_type"] == "invalid_argument", (tool, refused)
                assert refused["field"] == "$" and refused["validator"] == "oneOf", (tool, refused)
                service._dispatch_depth = 1
                try:
                    direct = method(arguments)
                finally:
                    service._dispatch_depth = 0
                assert direct["ok"] is False, (tool, direct)
                assert direct["error_type"] == "invalid_argument", (tool, direct)
                assert direct["summary"] == "Provide exactly one of image_path or artifact_id.", (tool, direct)

        # A debug session by id needs an ELF; the stored file decides.
        elf = service.call("artifact_upload", {"filename": "app.elf", "data_base64": base64.b64encode(b"\x7fELF" + b"\x00" * 12).decode("ascii")})
        assert elf["ok"] is True, elf
        started = service.call("debug_start_session", {"artifact_id": elf["artifact_id"], "mode": "load", "timeout_s": 10.0})
        assert started["ok"] is True, started
        assert started["artifact"]["source"] == "upload", started
        stopped = service.call("debug_stop_session")
        assert stopped["ok"] is True, stopped
        wrong_kind = service.call("debug_start_session", {"artifact_id": artifact_id})
        assert wrong_kind["ok"] is False, wrong_kind
        assert wrong_kind["error_type"] == "artifact_validation_failed", wrong_kind
        assert wrong_kind["tool"] == "debug_start_session", wrong_kind
    finally:
        service.close()

    # With uploads disabled the id is refused as permission_denied, still naming it.
    config_path = write_config(tmp_path / "disabled")
    config_path.write_text(config_path.read_text(encoding="utf-8").replace("allow_upload: true", "allow_upload: false"), encoding="utf-8")
    disabled = AgenticHILToolService(load_config(str(config_path)))
    try:
        assert disabled.config.artifacts.allow_upload is False
        for tool in ("flash_firmware", "debug_start_session"):
            refused = disabled.call(tool, {"artifact_id": "0" * 64 + ".hex"})
            assert refused["ok"] is False, refused
            assert refused["error_type"] == "permission_denied", (tool, refused)
            assert refused["artifact_id"] == "0" * 64 + ".hex", (tool, refused)
            assert refused["tool"] == tool, refused
    finally:
        disabled.close()


# ---------------------------------------------------------------------------
# Every hardware_recover refusal, and the incident standing afterwards.


def test_recover_refusals_leave_the_incident_standing(tmp_path: Path) -> None:
    # The owner that quarantined the bench still holds its lease: it may not
    # recover around its own hold.
    config = load_config(str(write_config(tmp_path)))
    owner = HardwareCoordinator(config, "owner")
    resource = "physical:incident"
    lease = owner.acquire(resource)
    lease.quarantine("test_audit_broken", audit_broken=True)
    quarantine_id = str(owner.status()["quarantine_id"])
    busy = owner.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)
    assert busy["ok"] is False, busy
    assert busy["error_type"] == "resource_busy", busy
    assert busy["summary"] == "Live owner still holds project resources.", busy
    owner.close()

    recovery = HardwareCoordinator(config, "recovery")
    try:
        unconfirmed = recovery.recover(safe_state_confirmed=False, quarantine_id=quarantine_id)
        assert unconfirmed["ok"] is False, unconfirmed
        assert unconfirmed["error_type"] == "operator_confirmation_required", unconfirmed
        assert unconfirmed["tool"] == "hardware_recover", unconfirmed

        without_id = recovery.recover(safe_state_confirmed=True, quarantine_id="")
        assert without_id["ok"] is False, without_id
        assert without_id["error_type"] == "quarantine_id_required", without_id
        assert without_id["summary"] == "Recovery requires the current quarantine_id from lease-status.", without_id

        # Duplicate resource markers in the project record are refused rather
        # than walked.
        record = recovery._read_record(recovery.project_key)
        assert record is not None
        recovery._write_record(recovery.project_key, {**record, "resources": [resource, resource]})
        inconsistent = recovery.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)
        assert inconsistent["ok"] is False, inconsistent
        assert inconsistent["error_type"] == "coordination_state_invalid", inconsistent
        assert inconsistent["summary"] == "Quarantine resource markers are inconsistent.", inconsistent
        recovery._write_record(recovery.project_key, record)

        status = recovery.status()
        assert status["blocked"] is True, status
        assert status["quarantine_id"] == quarantine_id, status
    finally:
        recovery.close()


def cli_recover(argv: list[str]) -> dict:
    """`agentic-hil recover ...` for the project in the working directory, as the document the command prints."""
    result = dispatch(build_parser().parse_args(["recover", *argv]))
    assert isinstance(result, dict), result
    return result


def test_the_recover_command_spells_the_same_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`agentic-hil recover` at a shell: the confirmation and the id are the parser's, the other two are the coordinator's.

    The two flags are required by the command line itself, so an operator who
    leaves one out is answered by the parser naming the flag and never reaches
    the bench; an id that is present and empty reaches the coordinator and is
    `quarantine_id_required` there. The live owner is `resource_busy` in the
    shape a shell sees it: the command's own coordinator is never the holder,
    so the project lock is held by another process and the summary says so,
    naming the project resource, where the holder recovering around its own
    lease reads "Live owner still holds project resources." above. The
    inconsistent markers are the coordinator's refusal printed as the command's
    document, and `lease-status` afterwards still reports the incident under
    the same id.
    """
    workspace = tmp_path / "project"
    write_authoritative_config(workspace, monkeypatch)
    monkeypatch.chdir(workspace)
    config = load_authoritative_config(workspace)
    owner = HardwareCoordinator(config, "owner")
    resource = "physical:incident"
    lease = owner.acquire(resource)
    lease.quarantine("test_audit_broken", audit_broken=True)
    quarantine_id = str(owner.status()["quarantine_id"])

    for argv, flag in (
        (["--quarantine-id", quarantine_id], "--confirm-safe-state"),
        (["--confirm-safe-state"], "--quarantine-id"),
    ):
        with pytest.raises(SystemExit) as usage:
            build_parser().parse_args(["recover", *argv])
        assert usage.value.code == 2, argv
        assert flag in capsys.readouterr().err, argv

    busy = cli_recover(["--confirm-safe-state", "--quarantine-id", quarantine_id])
    assert busy["ok"] is False, busy
    assert busy["error_type"] == "resource_busy", busy
    assert busy["summary"] == "Hardware resource is owned by another Agentic HIL process.", busy
    assert busy["resources"] == [owner.project_key], busy
    assert busy["retry_safe"] is True, busy
    owner.close()

    without_id = cli_recover(["--confirm-safe-state", "--quarantine-id", ""])
    assert without_id["ok"] is False, without_id
    assert without_id["error_type"] == "quarantine_id_required", without_id
    assert without_id["summary"] == "Recovery requires the current quarantine_id from lease-status.", without_id

    recovery = HardwareCoordinator(config, "recovery")
    try:
        record = recovery._read_record(recovery.project_key)
        assert record is not None
        recovery._write_record(recovery.project_key, {**record, "resources": [resource, resource]})
    finally:
        recovery.close()
    inconsistent = cli_recover(["--confirm-safe-state", "--quarantine-id", quarantine_id])
    assert inconsistent["ok"] is False, inconsistent
    assert inconsistent["error_type"] == "coordination_state_invalid", inconsistent
    assert inconsistent["summary"] == "Quarantine resource markers are inconsistent.", inconsistent

    status = dispatch(build_parser().parse_args(["lease-status"]))
    assert isinstance(status, dict), status
    assert status["blocked"] is True and status["incident_stands"] is True, status
    assert status["quarantine_id"] == quarantine_id, status


# ---------------------------------------------------------------------------
# The pytest plugin fixture's teardown.


def elf_for_the_plugin(workspace: Path) -> None:
    elf_path = workspace / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12)


def test_fixture_teardown_stops_a_leftover_debug_session_and_reports_close_failures(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """A session a test left open is stopped at teardown; a close that fails fails the test, naming it."""
    write_authoritative_config(pytester.path, monkeypatch, gdb_executable=FAKE_GDB)
    elf_for_the_plugin(pytester.path)
    pytester.makepyfile(
        test_leftover="""
def test_leaves_a_debug_session_open(agentic_hil):
    started = agentic_hil.call("debug_start_session", {"image_path": "build/app.elf", "mode": "load", "timeout_s": 10.0})
    assert started["ok"] is True, started
    assert agentic_hil.call("debug_get_session_status")["active"] is True


def test_the_next_test_finds_no_session(agentic_hil):
    status = agentic_hil.call("debug_get_session_status")
    assert status["active"] is False, status
    assert agentic_hil._debug_artifact is None
"""
    )
    result = pytester.runpytest(*PLUGIN_ARGS, "-p", "no:cacheprovider", "test_leftover.py")
    result.assert_outcomes(passed=2)

    pytester.makepyfile(
        test_close_fails="""
def test_whose_port_cleanup_fails(agentic_hil):
    def failing_close():
        raise RuntimeError("port would not close")
    agentic_hil.com_ports.close = failing_close
    assert agentic_hil.call("debugger_info")["ok"] is True
"""
    )
    failing = pytester.runpytest(*PLUGIN_ARGS, "-p", "no:cacheprovider", "test_close_fails.py")
    # The body passed; the teardown is what fails the test, as an error at
    # teardown naming the resource that would not close.
    outcomes = failing.parseoutcomes()
    assert outcomes.get("errors", 0) == 1 or outcomes.get("failed", 0) == 1, outcomes
    output = failing.stdout.str()
    assert "Agentic HIL fixture cleanup failed: COM: RuntimeError: port would not close" in output
    assert "ERROR at teardown of test_whose_port_cleanup_fails" in output or "test_whose_port_cleanup_fails FAILED" in output


def test_fixture_teardown_reports_a_debug_stop_that_is_refused_or_raises(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stop half of the same sentence: a `debug_stop_session` that answers not ok, or raises, fails the test as `debug: ...`.

    Each test leaves its session open and replaces what the fixture will call
    with something that answers once and then puts the real thing back, so the
    session-scoped service still closes the session it was left with. The
    refused stop is the tool's own not-ok document and the message carries its
    summary. The raising stop is the service's `call` raising: a tool body that
    raises never reaches the fixture as an exception, because the service turns
    it into a quarantining not-ok result (the first shape again), so what the
    fixture's own except branch reports is a failure of the call itself, by
    class and text.
    """
    write_authoritative_config(pytester.path, monkeypatch, gdb_executable=FAKE_GDB)
    elf_for_the_plugin(pytester.path)
    pytester.makepyfile(
        test_stop_fails="""
def open_session(agentic_hil):
    started = agentic_hil.call("debug_start_session", {"image_path": "build/app.elf", "mode": "load", "timeout_s": 10.0})
    assert started["ok"] is True, started
    assert agentic_hil.call("debug_get_session_status")["active"] is True


def test_whose_stop_is_refused(agentic_hil):
    open_session(agentic_hil)

    def refusing_stop(arguments=None):
        del agentic_hil.debug_stop_session
        return {"ok": False, "tool": "debug_stop_session", "error_type": "debugger_unresponsive", "summary": "the probe did not answer the stop"}

    agentic_hil.debug_stop_session = refusing_stop


def test_whose_stop_raises(agentic_hil):
    open_session(agentic_hil)
    real_call = agentic_hil.call

    def raising_call(name, arguments=None):
        del agentic_hil.call
        if name == "debug_stop_session":
            raise RuntimeError("gdb pipe broke")
        return real_call(name, arguments)

    agentic_hil.call = raising_call
"""
    )
    failing = pytester.runpytest(*PLUGIN_ARGS, "-p", "no:cacheprovider", "test_stop_fails.py")
    outcomes = failing.parseoutcomes()
    assert outcomes.get("errors", 0) == 2, outcomes
    assert outcomes.get("passed", 0) == 2, outcomes
    output = failing.stdout.str()
    assert "Agentic HIL fixture cleanup failed: debug: the probe did not answer the stop" in output
    assert "Agentic HIL fixture cleanup failed: debug: RuntimeError: gdb pipe broke" in output
    assert "ERROR at teardown of test_whose_stop_is_refused" in output
    assert "ERROR at teardown of test_whose_stop_raises" in output


# ---------------------------------------------------------------------------
# The canonical audit ledger's sidecar.


def test_a_corrupt_or_mismatched_audit_sidecar_fails_the_append_closed(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, com_ports_yaml=COM_PORT_YAML)))
    workspace_log = str(Path(logs_directory(config)) / "sdc-effect.jsonl")
    assert append_jsonl(workspace_log, {"direction": "tx", "hex": "01"}, config) is None
    canonical = canonical_audit_log_path(config, workspace_log)
    sidecar = canonical.with_name(canonical.name + ".digest")
    ledger_before = canonical.read_bytes()
    workspace_before = Path(workspace_log).read_bytes()
    sidecar_before = sidecar.read_bytes()

    sidecar.write_text("x", encoding="utf-8")
    corrupt = append_jsonl(workspace_log, {"direction": "tx", "hex": "02"}, config)
    assert isinstance(corrupt, ConfigError), corrupt
    assert corrupt.error_type == "coordination_state_invalid"
    assert corrupt.summary == "Canonical audit ledger is corrupted."
    assert corrupt.details["path"] == str(sidecar)
    assert canonical.read_bytes() == ledger_before
    assert Path(workspace_log).read_bytes() == workspace_before
    assert sidecar.read_bytes() == b"x"

    sidecar.write_bytes(sidecar_before)
    canonical.write_bytes(ledger_before[:-1])
    mismatched = append_jsonl(workspace_log, {"direction": "tx", "hex": "03"}, config)
    assert isinstance(mismatched, ConfigError), mismatched
    assert mismatched.error_type == "coordination_state_invalid"
    assert "size disagrees with its digest sidecar" in mismatched.summary
    assert mismatched.details["recorded_bytes"] == len(ledger_before)
    assert mismatched.details["actual_bytes"] == len(ledger_before) - 1
    assert canonical.read_bytes() == ledger_before[:-1]
    assert Path(workspace_log).read_bytes() == workspace_before
    assert sidecar.read_bytes() == sidecar_before


# ---------------------------------------------------------------------------
# A corrupt report-state file.


def test_a_corrupt_report_state_is_a_refusal_without_the_state_root_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = service_for(tmp_path)
    try:
        state_root = str(Path(service.config.state_root).resolve())
        state_file = Path(report_state_path(service.config))
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("{", encoding="utf-8")

        for tool in ("get_last_report", "classify_last_error"):
            result = service.call(tool)
            assert result["ok"] is False, (tool, result)
            assert result["error_type"] == "config_invalid", (tool, result)
            assert result["tool"] == tool, result
            assert result["summary"] == "Agentic HIL report state is not valid JSON.", result
            serialised = json.dumps(result)
            assert state_root not in serialised, serialised
            assert "path" not in result, result

        # A directory in place of the file is refused by the safe read before
        # any OS read happens; the refusal still carries no state-root path.
        state_file.unlink()
        state_file.mkdir()
        for tool in ("get_last_report", "classify_last_error"):
            result = service.call(tool)
            assert result["ok"] is False, (tool, result)
            assert result["error_type"] in {"report_unreadable", "unsafe_configured_path"}, (tool, result)
            serialised = json.dumps(result)
            assert state_root not in serialised, serialised
            assert str(state_file) not in serialised, serialised
        state_file.rmdir()

        # The OS refusing the read (EACCES on the file, as a root-owned state
        # root answers an unprivileged server) is `report_unreadable` with the
        # error class and number and, again, no path.
        state_file.write_text("{}", encoding="utf-8")

        def unreadable(path, *args, **kwargs):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))

        monkeypatch.setattr("agentic_hil.report.safe_read_text", unreadable)
        for tool in ("get_last_report", "classify_last_error"):
            result = service.call(tool)
            assert result["ok"] is False, (tool, result)
            assert result["error_type"] == "report_unreadable", (tool, result)
            assert result["tool"] == tool, result
            assert result["error_class"] == "PermissionError", result
            assert result["errno"] == errno.EACCES, result
            serialised = json.dumps(result)
            assert state_root not in serialised, serialised
            assert str(state_file) not in serialised, serialised
    finally:
        service.close()


# ---------------------------------------------------------------------------
# A serial device this user may not open.


class UnopenableHandle(FakeHandle):
    """pyserial's POSIX open on a device whose mode refuses this user: EACCES."""

    def __init__(self, port: FakePort, error: OSError) -> None:
        super().__init__(port)
        self.error = error

    def open(self) -> None:
        self.port_device.opens += 1
        raise self.error


class UnopenablePort(FakePort):
    def __init__(self, error: OSError) -> None:
        super().__init__()
        self.error = error

    def handle(self) -> UnopenableHandle:
        return UnopenableHandle(self, self.error)


def names_the_device_group(causes: list[str]) -> bool:
    return any(re.search(r"dialout|uucp|group", cause, re.IGNORECASE) for cause in causes)


def open_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, error: OSError) -> dict:
    """`com_session_start` on a device whose open raises `error`, as the driver module sees host `host`."""
    config = load_config(str(write_config(tmp_path, com_ports_yaml=COM_PORT_YAML)))
    service = AgenticHILToolService(config)
    install_fake_serial(monkeypatch, UnopenablePort(error))
    monkeypatch.setattr("agentic_hil.comports.os", HostOs(host))
    try:
        return service.call("com_session_start", {"port_id": PORT_ID})
    finally:
        service.close()


def eacces_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str) -> dict:
    return open_refusal(tmp_path, monkeypatch, host, PermissionError(errno.EACCES, "could not open port", DEVICE))


def test_a_serial_device_this_user_may_not_open_names_the_permission_not_a_second_holder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """EACCES on the device: an open failure whose causes name the group to join, not a holder.

    The expectation is keyed on two things and the test fixes both: the OS
    number, read off the exception the way `serial_port_busy` reads its own
    (`raised_errno`, never the message), and the POSIX host, given to the
    driver module the way the CAN channel rule is given its host above, so the
    suite decides the Linux advice wherever it runs. On Windows the same number
    means the opposite thing, and the neighbour below pins that.
    """
    refused = eacces_refusal(tmp_path, monkeypatch, "posix")

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "com_port_open_failed", refused
    assert "[Errno 13]" in refused["backend_error"], refused
    assert refused["side_effect_committed"] is False, refused
    assert refused["retry_safe"] is True, refused
    assert names_the_device_group(refused["likely_causes"]), refused["likely_causes"]
    assert not any("another program" in cause for cause in refused["likely_causes"]), refused["likely_causes"]


def test_access_denied_on_a_windows_com_port_keeps_the_second_holder_among_its_causes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour: `PermissionError(13, 'Access is denied.')` is what a held COM port answers on Windows.

    pyserial reports `CreateFile` on a port another program holds as errno 13
    there, so on that host the number is the second holder and not a group,
    and the causes stay the ones the open failure always had.
    """
    refused = eacces_refusal(tmp_path, monkeypatch, "nt")

    assert refused["error_type"] == "com_port_open_failed", refused
    assert refused["likely_causes"] == likely_causes("com_port_open_failed"), refused
    assert not names_the_device_group(refused["likely_causes"]), refused["likely_causes"]


@pytest.mark.parametrize("host", ["posix", "nt"])
def test_a_device_that_does_not_exist_keeps_the_causes_it_always_had(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    """The neighbour on either host: ENOENT is still the absent device, the second holder and the missing driver.

    The POSIX half is the one that carries weight. It is the host on which the
    permission advice exists at all, so advice keyed on the wrong number, or on
    the host alone rather than on the number, is caught here rather than left
    to a bench: a device that is simply not there must keep the three causes it
    always had.
    """
    refused = open_refusal(tmp_path, monkeypatch, host, FileNotFoundError(errno.ENOENT, "could not open port", DEVICE))

    assert refused["error_type"] == "com_port_open_failed", refused
    assert "[Errno 2]" in refused["backend_error"], refused
    assert refused["likely_causes"] == likely_causes("com_port_open_failed"), refused
    assert refused["likely_causes"] == [
        "configured COM port device does not exist",
        "COM port is already open in another program",
        "USB serial adapter is unplugged or driver is missing",
    ]


# ---------------------------------------------------------------------------
# An undeclared COM port or CAN bus.


def test_a_call_naming_an_undeclared_device_is_refused_with_the_documented_error_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The code's names, `com_port_not_configured` and `can_bus_not_configured`, and the documents naming the same."""
    config = load_config(str(write_config(tmp_path, com_ports_yaml=COM_PORT_YAML, can_buses_yaml=PEAK_BUS_YAML)))
    service = AgenticHILToolService(config)
    # No driver may be constructed on the way to the refusal.
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("a serial handle was constructed for an undeclared port"))))
    monkeypatch.setitem(sys.modules, "can", SimpleNamespace(Bus=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("a CAN bus was constructed for an undeclared bus"))))
    try:
        for tool, arguments in (
            ("com_session_start", {"port_id": "ghost"}),
            ("com_read", {"port_id": "ghost"}),
            ("com_write", {"port_id": "ghost", "text": "PING\r\n"}),
            ("com_session_stop", {"port_id": "ghost"}),
        ):
            refused = service.call(tool, arguments)
            assert refused["ok"] is False, (tool, refused)
            assert refused["error_type"] == "com_port_not_configured", (tool, refused)
            assert refused["summary"] == "COM port is not available in the authoritative config.", (tool, refused)
            assert refused["port_id"] == "ghost", (tool, refused)
            assert refused["configured_ports"] == [PORT_ID], (tool, refused)
            assert refused.get("side_effect_committed") is not True, (tool, refused)
        for tool, arguments in (
            ("can_session_start", {"bus_id": "ghost"}),
            ("can_read", {"bus_id": "ghost"}),
            ("can_send", {"bus_id": "ghost", "frame_id": 0x123, "data_hex": "01"}),
            ("can_session_stop", {"bus_id": "ghost"}),
        ):
            refused = service.call(tool, arguments)
            assert refused["ok"] is False, (tool, refused)
            assert refused["error_type"] == "can_bus_not_configured", (tool, refused)
            assert refused["summary"] == "CAN bus is not available in the authoritative config.", (tool, refused)
            assert refused["bus_id"] == "ghost", (tool, refused)
            assert refused["configured_buses"] == [BUS_ID], (tool, refused)
            assert refused.get("side_effect_committed") is not True, (tool, refused)
        # The third route, and the one the documents were right about: a run
        # declaring a device the configuration does not carry is refused by the
        # declaration itself, before any device is held.
        declared = service.call("bench_run_start", {"devices": [{"kind": "uart", "id": "ghost"}]})
        assert declared["ok"] is False, declared
        assert declared["error_type"] == "unknown_device", declared
        assert declared["side_effect_committed"] is False, declared
    finally:
        service.close()

    # README.md and docs/safety-model.md gave `unknown_device` as the refusal
    # for any call naming an undeclared device. There are three, and which one
    # answers depends on what did the naming: a run declaring a device it may
    # not have is `unknown_device` (`resolve_devices`, pinned by
    # test_permission_default_claim against a generated file), and a port tool
    # or a bus tool naming one is the two above, which TROUBLESHOOTING.md
    # already relies on. The pages carry all three and say which is which,
    # rather than one name for three routes.
    for document in ("README.md", "docs/safety-model.md"):
        text = (REPOSITORY_ROOT / document).read_text(encoding="utf-8")
        sentence = next((line for line in text.splitlines() if "does not exist on this bench until" in line), None)
        assert sentence is not None, f"{document} no longer carries the presence sentence"
        assert "`com_port_not_configured`" in sentence, (document, sentence)
        assert "`can_bus_not_configured`" in sentence, (document, sentence)
        assert "`unknown_device` where a run declares it" in sentence, (document, sentence)
        assert "refused with `unknown_device`" not in sentence, (document, sentence)
    troubleshooting = (REPOSITORY_ROOT / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    assert "turns a precise refusal back into `com_port_not_configured`" in troubleshooting
    # docs/security-design.md makes the same promise in its own words, in the
    # paragraph on where enforcement sits.
    security = (REPOSITORY_ROOT / "docs" / "security-design.md").read_text(encoding="utf-8")
    sentence = next((line for line in security.splitlines() if "does not declare does not exist here" in line), None)
    assert sentence is not None, "docs/security-design.md no longer carries the presence sentence"
    assert "`com_port_not_configured`" in sentence and "`can_bus_not_configured`" in sentence, sentence
    assert "`unknown_device` to a run that declares it" in sentence, sentence


# ---------------------------------------------------------------------------
# device_busy names the holder and flags a stale heartbeat.


def test_device_busy_names_the_holder_and_flags_a_stale_heartbeat(tmp_path: Path) -> None:
    root = tmp_path / "device-locks"
    root.mkdir()
    holder = BenchMutex(frontend="mcp", label="run-a", root=root)
    contender = BenchMutex(frontend="cli", root=root)
    try:
        assert holder.acquire([BOARD]) == [BOARD]
        record_path = root / f"{resource_digest(BOARD)}.holder.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))

        with pytest.raises(DeviceBusyError) as busy:
            contender.acquire([BOARD])
        result = busy.value.result
        assert result["ok"] is False, result
        assert result["error_type"] == "device_busy", result
        assert result["resource"] == BOARD, result
        assert result["holder"] == {"owner_id": holder.owner.owner_id, "pid": os.getpid(), "host": holder.owner.host, "frontend": "mcp", "label": "run-a"}, result
        assert result["held_since"] == record["acquired_at"], result
        assert result["heartbeat_at"] == record["heartbeat_at"], result
        assert result["heartbeat_age_s"] >= 0, result
        assert "holder_heartbeat_stale" not in result, result
        # The same process is the holder here, and the refusal says so.
        assert result["holder_is_this_process"] is True, result
        assert f"pid {os.getpid()}" in result["summary"] and "running run-a" in result["summary"], result

        # A record whose heartbeat stopped landing five intervals ago is a
        # holder that is hung rather than busy.
        stale_at = (datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_INTERVAL_S * 5)).isoformat().replace("+00:00", "Z")
        record_path.write_text(json.dumps({**record, "heartbeat_at": stale_at}), encoding="utf-8")
        with pytest.raises(DeviceBusyError) as hung:
            contender.acquire([BOARD])
        stale = hung.value.result
        assert stale["heartbeat_age_s"] > HEARTBEAT_INTERVAL_S * 4, stale
        assert stale["holder_heartbeat_stale"] is True, stale
        assert stale["holder"]["label"] == "run-a", stale

        # A record inside the window, even past one interval, is not stale.
        fresh_at = (datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_INTERVAL_S * 1.5)).isoformat().replace("+00:00", "Z")
        record_path.write_text(json.dumps({**record, "heartbeat_at": fresh_at}), encoding="utf-8")
        with pytest.raises(DeviceBusyError) as recent:
            contender.acquire([BOARD])
        assert recent.value.result["heartbeat_age_s"] > HEARTBEAT_INTERVAL_S, recent.value.result
        assert "holder_heartbeat_stale" not in recent.value.result, recent.value.result

        # The CLI's passthrough: a grant or revoke that raced this holder
        # carries the same refusal as `open_holds`, and the reader of that
        # document is told who holds the device, since when, how long ago the
        # holder last said so, and, for the hung holder, that it is hung.
        holds = _holds_from_collision(stale)
        assert holds["raced_a_run"] is True and holds["owner_active"] is False, holds
        assert holds["held_devices"] == [BOARD] and holds["busy_devices"] == [BOARD], holds
        assert holds["holder"] == stale["holder"], holds
        assert holds["held_since"] == stale["held_since"], holds
        assert holds["heartbeat_age_s"] == stale["heartbeat_age_s"], holds
        assert holds["holder_heartbeat_stale"] is True, holds
        fresh = _holds_from_collision(recent.value.result)
        assert fresh["heartbeat_age_s"] == recent.value.result["heartbeat_age_s"], fresh
        assert "holder_heartbeat_stale" not in fresh, fresh
    finally:
        holder.release_all()
        contender.release_all()


# ---------------------------------------------------------------------------
# The two optional extras refuse by their own names.


def test_missing_optional_extras_refuse_by_their_own_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """python-can missing is `can_backend_not_available`; pyocd missing is `debugger_not_found`; the docs say which is which."""
    service = service_for(tmp_path / "can", can_buses_yaml=PEAK_BUS_YAML)
    monkeypatch.setitem(sys.modules, "can", None)
    try:
        refused = service.call("can_session_start", {"bus_id": BUS_ID})
    finally:
        service.close()
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "can_backend_not_available", refused
    assert refused["summary"] == "python-can is not installed. Install agentic-hil[can] to use direct CAN adapters.", refused
    assert refused["side_effect_committed"] is False, refused
    assert refused["adapter"] == "peak", refused

    workspace = tmp_path / "pyocd" / "workspace"
    authoritative_config_with_executable_spelling(workspace, monkeypatch, "null", debugger_type="pyocd")
    path_without_a_toolchain(monkeypatch)
    probe = AgenticHILToolService(load_authoritative_config(workspace))
    try:
        result = probe.call("probe_target")
    finally:
        probe.close()
    assert result["ok"] is False, result
    assert result["error_type"] == "debugger_not_found", result
    assert result["backend_error_type"] == "pyocd_not_found", result
    assert "pyOCD is not installed (install agentic-hil[pyocd] or pip install pyocd)" in result["likely_causes"], result
    assert result["error_type"] != "can_backend_not_available"

    # docs/installation.md names each extra's own refusal rather than
    # attributing `can_backend_not_available` to the pyOCD extra.
    installation = (REPOSITORY_ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    paragraphs = [" ".join(block.split()) for block in re.split(r"\n\s*\n", installation)]
    extras = next((block for block in paragraphs if "agentic-hil[pyocd]" in block and "agentic-hil[can]" in block), None)
    assert extras is not None, "docs/installation.md no longer describes the two extras in one paragraph"
    assert "`can_backend_not_available`" in extras, extras
    assert "`debugger_not_found`" in extras, extras


# ---------------------------------------------------------------------------
# A broker whose adapter cannot open.

# A bridge that reads the broker's open and exits without answering it: the
# adapter is gone, and the exit status is the bridge's own, unrelated to the
# broker's. It reads the request first on purpose. The transport polls the child
# before it writes and answers `can_adapter_process_exited` for a child that is
# already gone, so a bridge that exited on startup would give the broker one of
# two documents depending on whether the child or the broker's write came first,
# and on a loaded host the child did. Blocking on the request pins the one
# outcome this test is about: the request was written, and nothing came back.
FAILING_BRIDGE = textwrap.dedent(
    """
    import sys
    sys.stdin.readline()
    sys.stderr.write("adapter gone\\n")
    sys.exit(3)
    """
)


def failing_bridge_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    bridge = tmp_path / "failing_bridge.py"
    bridge.write_text(FAILING_BRIDGE, encoding="utf-8")
    can_buses_yaml = (
        "can_buses:\n"
        "  sdcbroker:\n"
        '    adapter: "process"\n'
        '    channel: "vcan0"\n'
        f'    executable: "{bridge.as_posix()}"\n'
        # Short, so a broker whose bridge never answers its open exits well
        # inside the attach deadline and what the loop does next is observable.
        "    timeout_s: 1.0\n"
        "    shares:\n"
        "      alpha:\n"
        "        max_frames: 16\n"
        "        permissions:\n"
        "          allow_read: true\n"
        "          allow_write: true\n"
    )
    write_authoritative_config(workspace, monkeypatch, can_buses_yaml=can_buses_yaml)
    return load_authoritative_config(workspace)


def test_a_broker_whose_adapter_cannot_open_refuses_the_participant_with_the_adapter_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:  # noqa: F811
    """One broker is spawned, and the participant learns why the bus is not there.

    The broker's own open refusal is the bridge's: its open request went
    unanswered because the bridge exited, so the document the broker wrote to
    its log before exiting `BROKER_EXIT_ADAPTER` is `can_adapter_timeout` with
    the bridge's stderr in `stderr_tail`. The participant is refused with that
    document, not with the generic "could not be reached or started": the
    adapter's error type, its summary, the bridge's last words as
    `backend_error`, the broker's exit code and the path of the log that holds
    the whole of it, and `retry_safe` stated as a value.
    """
    config = failing_bridge_config(tmp_path, monkeypatch)
    bus_key = bus_lock_key(config, "sdcbroker")

    with pytest.raises(ParticipantError) as refused:
        attach_participant(config, "sdcbroker", "alpha", start_timeout_s=4.0)
    result = refused.value.result
    diagnostics = broker_diagnostics(config, bus_key)

    assert len(reaped_brokers) == 1, f"{len(reaped_brokers)} brokers were spawned for one adapter that cannot open\n{diagnostics}"
    assert reaped_brokers[0].wait(timeout=15) == BROKER_EXIT_ADAPTER, diagnostics
    assert result["ok"] is False, result
    assert result["bus_id"] == "sdcbroker", result
    assert result["participant"] == "alpha", result
    assert result["error_type"] == "can_adapter_timeout", (result, diagnostics)
    assert result["summary"].startswith("The CAN broker started for this bus could not open its adapter: "), result
    assert "adapter gone" in result["backend_error"], (result, diagnostics)
    assert "adapter gone" in result["stderr_tail"], (result, diagnostics)
    assert result["broker_exit_code"] == BROKER_EXIT_ADAPTER, result
    assert result["broker_log"] == str(broker_log_path(bus_key, BenchMutex().root)), result
    assert result["retry_safe"] is True, result
    # The log the result points at carries the same document, and the deadline
    # was not what ended the attach: one broker, one refusal, well inside it.
    assert "adapter gone" in diagnostics and "can_adapter_timeout" in diagnostics, diagnostics
    assert "can_broker_unavailable" not in json.dumps(result), result
