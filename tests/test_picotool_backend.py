"""Acceptance tests for the serial-pinned picotool backend."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import get_args, get_type_hints

import pytest
import yaml
from conftest import DEFAULT_TEST_PERMISSIONS, write_config
from httpserver_fakes import short_settings
from support import process_is_running, scaled_time_bound
from test_config_write import bench, changes, service

from agentic_hil import bench as bench_module
from agentic_hil import cli, windows_device_lock
from agentic_hil import config as configuration
from agentic_hil.backends import common
from agentic_hil.backends.common import CONTACT_UNPROVEN, NOT_CONTACTED
from agentic_hil.benchview import BenchView
from agentic_hil.config import ConfigError, load_config
from agentic_hil.knowledge import BACKENDS, catalogue_entry, debugger_backends_document
from agentic_hil.runevidence import write_run_evidence
from agentic_hil.test_reactor import TestReactor, load_test_config
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import DebuggerConfig

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests/fixtures/fake_picotool.py"
PREFIX = "AGENTIC_HIL_FAKE_PICOTOOL_"
SERIAL = "PicoTestAbCd1234"
OTHER_SERIAL = "PicoTestEf012345"
START = "The device was rebooted to start the application.\n"
RETURN = "The device was asked to reboot back into application mode.\n"
FORCE_ENTRY = "The device was asked to reboot into BOOTSEL mode so the command can be executed."
ACCESSIBLE = (
    "The device has been left accessible, but without the drive mounted; use 'picotool reboot' "
    "to reboot into regular BOOTSEL mode or application mode.\n"
)
WRITE_START = "Loading into Flash:   [                              ]  0%\r"
WRITE_FRAME = WRITE_START + "\n"
WRITE_COMPLETE = WRITE_START + "Loading into Flash:   [==============================]  100%\r\n"
VERIFY_BAR = (
    "Verifying Flash:      [                              ]  0%\r"
    "Verifying Flash:      [==============================]  100%\r\n"
)
VERIFY_COMPLETE = VERIFY_BAR + "  OK\n"
VERIFY_MISMATCH = "  FAILED\nERROR: The device contents did not match the file\n"
OVERSIZE = "ERROR: File size 0x200001 is too big to fit in flash size 0x200000"
NO_DEVICE = f"No accessible RP-series devices in BOOTSEL mode were found with serial number {SERIAL}."
REAL_HOST_FIND = configuration.host_debugger_executable


class Pico:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.log = root / "picotool.jsonl"
        self.states = root / "pico-states.json"
        self.monkeypatch = monkeypatch
        self.set(LOG=self.log, STATE_FILE=self.states)

    def set(self, **settings):
        for name, value in settings.items():
            self.monkeypatch.setenv(PREFIX + name, str(value))

    def raw(self, command, *, stdout="", stderr="", status=0):
        self.set(RAW_COMMAND=command, STDOUT=stdout, STDERR=stderr, EXIT=status)

    def runs(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def devices(self, serial=SERIAL, *, start=0):
        records = [entry for entry in self.runs()[start:] if entry["command"] != "version"]
        for entry in records:
            args = entry["argv"]
            assert args.count("--ser") == 1, entry
            assert args[args.index("--ser") + 1] == (serial or entry["serial"]), entry
            assert not {arg.split("=", 1)[0] for arg in args} & {"--bus", "--address", "--vid", "--pid"}, entry
            assert entry["command"] in {"info", "load", "reboot"}, entry
        return records


@pytest.fixture
def pico(tmp_path, monkeypatch):
    monkeypatch.setattr(bench_module, "machine_device_lock_root", lambda: None)
    return Pico(tmp_path, monkeypatch)


def fake_call(*argv, text=True):
    return subprocess.run(
        [sys.executable, str(FAKE), *argv], capture_output=True, text=text, encoding="utf-8" if text else None,
        timeout=scaled_time_bound(5), check=False,
    )


def native_status(code):
    return code % (2**32 if os.name == "nt" else 256)


def test_fake_contract(pico):
    assert fake_call("version").stdout == "picotool v2.3.1 (Windows, GNU-14.2.0, Release)\n"
    assert fake_call("version", "-s").stdout == "2.3.1\n"
    pico.set(VERSION="3.0.0")
    assert fake_call("version", "-s").stdout == "3.0.0\n"
    pin = ("--ser", SERIAL)
    for chip, expected in [
        ("RP2040", (
            "Device Information\n"
            " type:        RP2040\n"
            " flash size:  2048K\n"
            " flash id:    0xE6614103E7A1B2C3\n"
        )),
        ("RP2350", (
            "Device Information\n"
            " type:                 RP2350\n"
            " package:              QFN60\n"
            " chipid:               0x8c2d5e1f4a3b6971\n"
            " flash devinfo:        0x0c00\n"
            " current cpu:          ARM\n"
            " available cpus:       ARM, RISC-V\n"
            " default cpu:          ARM\n"
            " secure boot:          0\n"
            " debug enable:         1\n"
            " secure debug enable:  1\n"
            " flash size:           4096K\n"
        )),
    ]:
        pico.set(STATE="bootsel", CHIP=chip)
        info = fake_call("info", "-d", *pin)
        assert (info.returncode, info.stdout, info.stderr) == (0, expected, "")
    verified = fake_call("load", "app.uf2", "-v", *pin, text=False)
    assert (verified.returncode, verified.stdout, verified.stderr) == (0, (WRITE_COMPLETE + VERIFY_COMPLETE).encode("utf-8"), b"")

    pico.set(STATE="application", CHIP="RP2040")
    hint = fake_call("info", "-d", *pin)
    assert hint.returncode == native_status(-7)
    hint_prefix = f"{NO_DEVICE}\n\nbut:\n\nRP2040 device at bus 1, address 2 appears to have a USB serial connection, so consider "
    assert hint.stdout == hint_prefix + "-f (or -F) to force reboot in order to run the command.\n"
    assert hint.stderr == ""
    hint = fake_call("reboot", "-u", *pin)
    assert (hint.returncode, hint.stdout, hint.stderr) == (native_status(-7), hint_prefix + "-f to force the reboot.\n", "")
    for state in ("no-reset-interface", "absent"):
        pico.set(STATE=state)
        absent = fake_call("info", "-d", *pin)
        assert (absent.returncode, absent.stdout, absent.stderr) == (native_status(-7), NO_DEVICE + "\n", "")

    pico.set(STATE="application", CHIP="RP2350", WAIT_RETRIES=2, ABSOLUTE_FAMILY="RP2350_ARM_S")
    held = fake_call("load", "app.uf2", "-v", "-F", *pin, text=False)
    expected_hold = (
        FORCE_ENTRY + "\nWaiting for device to reboot...\n\n"
        "Family ID RP2350_ARM_S can be downloaded in absolute space:\n  00000000->00400000\n"
        + WRITE_COMPLETE + VERIFY_COMPLETE + "\n" + ACCESSIBLE
    )
    assert (held.returncode, held.stdout, held.stderr) == (0, expected_hold.encode("utf-8"), b"")
    assert json.loads(pico.states.read_text(encoding="utf-8")) == {SERIAL: "bootsel"}

    assert fake_call("reboot", "-a", "-f", *pin).stdout == "The device was rebooted into application mode.\n"
    assert fake_call("reboot", "-u", "-f", *pin).stdout == "The device was asked to reboot into BOOTSEL mode.\n"
    assert fake_call("reboot", "-a", *pin).stdout == "The device was rebooted into application mode.\n"
    assert json.loads(pico.states.read_text(encoding="utf-8")) == {SERIAL: "application"}

    pico.raw("load", stdout=OVERSIZE + "\n", status=-8)
    raw = fake_call("load", "-v", *pin, text=False)
    assert (raw.returncode, raw.stdout, raw.stderr) == (native_status(-8), (OVERSIZE + "\n").encode("utf-8"), b"")
    mismatch = WRITE_COMPLETE + VERIFY_BAR + VERIFY_MISMATCH
    pico.raw("load", stdout=mismatch, status=-11)
    raw = fake_call("load", "-v", *pin, text=False)
    assert (raw.returncode, raw.stdout, raw.stderr) == (native_status(-11), mismatch.encode("utf-8"), b"")
    prohibited = [
        ("info", "-d"),
        *(("info", "-d", option, "1", *pin) for option in ("--bus", "--address", "--vid", "--pid")),
        *((action, *pin) for action in ("erase", "otp", "config", "partition", "seal", "encrypt")),
        *(("load", option, *pin) for option in ("--ignore-partitions", "-n", "-N", "-u")),
        *(("reboot", option, *pin) for option in ("-c", "-g")),
        *(("info", "-d", option, *pin) for option in ("-f", "-F")),
    ]
    start = len(pico.runs())
    for argv in prohibited:
        refused = fake_call(*argv)
        assert refused.returncode == 97 and refused.stderr.startswith("fake picotool:")
    assert [entry["argv"] for entry in pico.runs()[start:]] == [list(argv) for argv in prohibited]


def yaml_config(workspace, **fields):
    path = write_config(workspace, debugger_executable=FAKE, probe_id=SERIAL, timeout_s=17)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entry = raw["debuggers"]["dut"]
    entry["type"] = "picotool"
    entry.pop("target_type", None)
    for key, value in fields.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


@pytest.mark.parametrize("family", ["rp2040", "rp2350", None], ids=["rp2040", "rp2350", "unspecified-family"])
def test_load_configuration(tmp_path, family):
    config = load_config(str(yaml_config(tmp_path / "workspace", target_type=family)))
    debugger = config.debuggers["dut"]
    assert (debugger.type, debugger.probe_id, debugger.target_type) == ("picotool", SERIAL, family)
    assert debugger.timeout_s == 17 and debugger.permissions.allow_flash and debugger.permissions.allow_reset
    assert debugger.executable == str(FAKE)
    assert "picotool" in get_args(get_type_hints(DebuggerConfig)["type"])
    assert "picotool" in configuration.DEBUGGER_TYPES
    assert "picotool" in BACKENDS


@pytest.mark.parametrize(("key", "value"), [
    ("probe_id", None), ("probe_id", ""), ("probe_id", "-option"), ("probe_id", "@args"),
    ("target_type", "rp9999"), ("target_type", "-option"), ("target_type", "@args"),
    ("com_port", "port"), ("connect_mode", "under_reset"),
], ids=[
    "missing-serial", "empty-serial", "dash-serial", "at-serial", "unknown-family", "dash-family",
    "at-family", "com-port", "under-reset",
])
def test_invalid_configuration(tmp_path, key, value):
    path = yaml_config(tmp_path / "workspace", **{key: value})
    with pytest.raises(ConfigError) as caught:
        load_config(str(path))
    refusal = caught.value.to_dict()
    if (refusal.get("error_type"), refusal.get("field")) != ("config_invalid", f"debuggers.dut.{key}"):
        raise AssertionError("wrong picotool configuration field")


def test_backend_matrix_and_switch_require_probe_id(tmp_path, monkeypatch):
    matrix = debugger_backends_document()["backends"]
    if "picotool" not in matrix:
        raise AssertionError("picotool is missing from the debugger field matrix")
    assert matrix["picotool"]["probe_id"]["status"] == "required"
    workspace, path = bench(tmp_path, monkeypatch, debugger_type="stlink", allow_config_description_write=True)
    before = path.read_bytes()
    with closing_service(service(workspace)) as tools:
        refused = tools.call("project_config_set", changes(
            ("debuggers.dut.type", "picotool"), ("debuggers.dut.executable", FAKE.as_posix()),
        ))
        assert refused["ok"] is False and "probe_id" in str(refused)
        assert path.read_bytes() == before
        accepted = tools.call("project_config_set", changes(
            ("debuggers.dut.type", "picotool"), ("debuggers.dut.executable", FAKE.as_posix()),
            ("debuggers.dut.probe_id", SERIAL),
        ))
        assert accepted["ok"] is True, accepted


def test_native_executable_resolves_from_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda program: str(FAKE) if program in {"picotool", "picotool.exe"} else None)
    assert REAL_HOST_FIND("picotool") == str(FAKE)


def runtime_config(workspace, *, serial=SERIAL, executable=FAKE, family=None, alias=None, **kwargs):
    path = write_config(
        workspace, debugger_executable=FAKE, probe_id=serial, auto_recover="off",
        state_root=workspace.parent / f"{workspace.name}-state", **kwargs,
    )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["artifacts"]["allowed_extensions"].append(".uf2")
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    base = load_config(str(path))
    debugger = replace(
        base.debuggers["dut"], type="picotool", executable=str(executable) if executable else None,
        target_type=family, resource_id=alias,
    )
    return replace(base, debuggers={"dut": debugger}, debugger=debugger)


@contextmanager
def closing_service(tools):
    try:
        yield tools
    finally:
        tools.close()


def call(config, tool, arguments=None):
    with closing_service(AgenticHILToolService(config)) as tools:
        return tools.call(tool, arguments or {})


def firmware(workspace, extension="bin"):
    build = workspace / "build"
    build.mkdir(parents=True, exist_ok=True)
    path = build / f"app.{extension}"
    data = bytes(range(32))
    if extension == "uf2":
        data = (struct.pack("<8I", 0x0A324655, 0x9E5D5157, 0, 0x10000000, 256, 0, 1, 0)
                + data.ljust(476, b"\0") + struct.pack("<I", 0x0AB16F30))
    elif extension == "elf":
        ident = b"\x7fELF\x01\x01\x01" + bytes(9)
        header = struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0x10000001, 52, 0, 0, 52, 32, 1, 0, 0, 0)
        segment = struct.pack("<8I", 1, 84, 0x10000000, 0x10000000, len(data), len(data), 5, 4)
        data = ident + header + segment + data
    elif extension == "hex":
        data = b":020000041000EA\n:0400000001020304F2\n:00000001FF\n"
    path.write_bytes(data)
    return {"image_path": path.relative_to(workspace).as_posix()}


def assert_contact(result, expected):
    for name, value in expected.items():
        assert result.get(name) == value, (name, result)
    if expected is CONTACT_UNPROVEN:
        assert result.get("target_contacted") is not False, result


def state_is(result, state):
    # State claims come from structured fields or their caller-facing summary.
    facts = {key: val for key, val in result.items() if any(
        term in key for term in ("state", "status", "held", "present", "detected", "chip", "family", "summary")
    )}
    words = re.sub(r"[_-]", " ", json.dumps(facts).lower())
    if state == "bootsel":
        return ("bootsel" in words or result.get("held_in_rom_bootloader") is True) and "not detected" not in words
    if state == "application":
        return ("application" in words or "running" in words) and "not running" not in words
    return any(mark in words for mark in ('"present": false', '"detected": false', "not detected", "absent"))


def assert_info(records, serial=SERIAL):
    assert [entry["serial"] for entry in records] == [serial]
    assert set(records[0]["argv"]) == {"info", "-d", "--ser", serial}, records


@pytest.mark.parametrize(("state", "chip"), [
    ("bootsel", "RP2040"), ("bootsel", "RP2350"), ("application", "RP2350"),
    ("no-reset-interface", "RP2350"), ("absent", "RP2350"),
], ids=["bootsel-rp2040", "bootsel-rp2350", "reset-interface", "no-reset-interface", "absent"])
def test_probe_without_reset_permission(pico, state, chip):
    pico.set(STATE=state, CHIP=chip)
    config = runtime_config(pico.root / "workspace", permissions={**DEFAULT_TEST_PERMISSIONS, "allow_reset": False})
    result = call(config, "probe_target")
    if state in {"bootsel", "application"}:
        assert result["ok"] is True and state_is(result, state), result
        assert chip.casefold() in json.dumps(result).casefold()
    else:
        assert result["ok"] is False and result["error_type"] == "target_not_detected", result
        assert_contact(result, NOT_CONTACTED)
    assert_info(pico.devices())
    assert not pico.states.exists()


def listing_config(pico):
    config = runtime_config(pico.root / "workspace")
    boards = {
        "dut": (SERIAL, "bootsel", "RP2040"),
        "running": (OTHER_SERIAL, "application", "RP2350"),
        "invisible": ("PicoInvisible1234", "no-reset-interface", "RP2040"),
        "absent": ("PicoAbsent1234", "absent", "RP2350"),
    }
    pico.set(BOARDS=json.dumps({
        **{serial: {"state": state, "chip": chip} for serial, state, chip in boards.values()},
        "PicoUnconfigured1234": {"state": "bootsel", "chip": "RP2350"},
    }))
    entries = {name: replace(config.debugger, probe_id=serial) for name, (serial, _, _) in boards.items()}
    return replace(config, debuggers=entries), boards


def listing_rows(result):
    rows = result.get("probes", result.get("debuggers", {}))
    if isinstance(rows, list):
        return {row.get("debugger_id", row.get("id", row.get("name", row.get("probe_id")))): row for row in rows}
    return rows


def matches_listing(result, boards):
    rows = listing_rows(result)
    if len(rows) != len(boards):
        return False
    for name, (serial, state, chip) in boards.items():
        row = rows.get(name, rows.get(serial, {}))
        expected = state if state in {"bootsel", "application"} else "absent"
        candidates = listing_rows(row) or {"row": row}
        if not any(
            state_is(candidate, expected)
            and (chip.lower() in json.dumps(candidate).lower() if expected != "absent" else not re.search(r"\brp20(?:40)\b|\brp2350\b", json.dumps(candidate).lower()))
            for candidate in candidates.values()
        ):
            return False
    return True


def check_listing(pico, result, boards):
    assert matches_listing(result, boards), result
    notice = json.dumps(result).lower()
    assert "com_ports_list" in notice and ("unconfigured" in notice or "configured" in notice), result
    assert "PicoUnconfigured1234" not in json.dumps(result)
    records = pico.devices(None)
    assert sorted(entry["serial"] for entry in records) == sorted(value[0] for value in boards.values())
    for entry in records:
        assert_info([entry], entry["serial"])


@pytest.mark.parametrize("surface", ["service", "cli"], ids=["service", "cli"])
def test_listing_reads_each_granted_configured_serial_once(pico, surface):
    config, boards = listing_config(pico)
    if surface == "service":
        result = call(config, "debugger_probes_list")
    else:
        pico.monkeypatch.setattr(cli, "load_authoritative_config", lambda *_: replace(config, debugger_id=None, debugger=None))
        result = cli.debugger_probes()
    check_listing(pico, result, boards)


def test_bench_view_reports_configured_picotool_states(pico):
    config, boards = listing_config(pico)
    with BenchView(config, short_settings(), activity=lambda: {}) as view:
        result = view.probes()
    if not matches_listing(result, boards):
        raise AssertionError("bench inventory did not report configured picotool states")
    check_listing(pico, result, boards)


def assert_load(pico, payload, *, state, reset, start=0, address=None):
    info, load = pico.devices(start=start)
    assert_info([info])
    assert load["command"] == "load", load
    args = load["argv"]
    assert "-v" in args
    assert not set(args) & {"-n", "-N", "-u", "--ignore-partitions"}
    assert ("-F" in args) is (state == "application" and not reset)
    assert ("-f" in args) is (state == "application" and reset)
    if state == "bootsel" or not reset:
        assert ("-x" in args) is reset
    if address is None:
        assert "-o" not in args
    else:
        assert int(args[args.index("-o") + 1], 0) == int(address, 0)
    original = pico.root / "workspace" / payload["image_path"]
    assert Path(load["artifact"]).is_absolute() and Path(load["artifact"]) != original
    assert load["sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()
    if "-t" in args:
        assert args[args.index("-t") + 1] == original.suffix[1:]


@pytest.mark.parametrize(("state", "reset", "extension", "upload"), [
    ("bootsel", True, "uf2", False), ("bootsel", False, "elf", True),
    ("application", True, "bin", False), ("application", False, "uf2", False),
], ids=["bootsel-start-uf2", "bootsel-hold-uploaded-elf", "application-start-bin", "application-hold-uf2-flash-grant-only"])
def test_verified_flash(pico, state, reset, extension, upload):
    pico.set(STATE=state)
    if extension == "uf2":
        pico.set(ABSOLUTE_FAMILY="RP2350_ARM_S")
    if state == "application" and not reset:
        pico.set(WAIT_RETRIES=2)
    workspace = pico.root / "workspace"
    address = "0x10000000" if extension == "bin" else "0x1"
    payload = firmware(workspace, extension)
    config = runtime_config(workspace, flash_address=address, permissions={**DEFAULT_TEST_PERMISSIONS, "allow_reset": reset})
    with closing_service(AgenticHILToolService(config)) as tools:
        request = payload
        if upload:
            uploaded = tools.call("artifact_upload", payload)
            assert uploaded["ok"] is True, uploaded
            request = {"artifact_id": uploaded["artifact_id"]}
        result = tools.call("flash_firmware", {**request, "reset_after_flash": reset})
    assert result["ok"] is True, result
    assert state_is(result, "application" if reset else "bootsel"), result
    assert_load(pico, payload, state=state, reset=reset, address=address if extension == "bin" else None)
    expected = "application" if reset else "bootsel"
    states = json.loads(pico.states.read_text(encoding="utf-8")) if pico.states.exists() else {}
    assert states.get(SERIAL, state) == expected


def test_bin_window_endpoints_for_both_families(pico):
    workspace = pico.root / "workspace"
    payload = firmware(workspace)
    for chip in ("RP2040", "RP2350"):
        for address in ("0x10000000", "0x10fff000"):
            pico.set(CHIP=chip)
            config = runtime_config(workspace, family=chip.lower(), flash_address=address)
            start = len(pico.runs())
            result = call(config, "flash_firmware", payload)
            assert result["ok"] is True, (chip, address, result)
            assert_load(pico, payload, state="bootsel", reset=False, address=address, start=start)


@pytest.mark.parametrize(("address", "extension"), [
    (None, "bin"), ("0x10000001", "bin"), ("0x0ffff000", "bin"), ("0x11000000", "bin"), ("0x10000000", "hex"),
], ids=["missing-address", "unaligned", "below-base", "exclusive-end", "hex"])
def test_invalid_image_never_starts_picotool(pico, address, extension):
    config = runtime_config(pico.root / "workspace", flash_address=address)
    result = call(config, "flash_firmware", firmware(Path(config.work_dir), extension))
    assert result["ok"] is False and result["error_type"] == "invalid_argument", result
    assert_contact(result, NOT_CONTACTED)
    assert pico.runs() == []


@pytest.mark.parametrize(("state", "family", "error"), [
    ("bootsel", "rp2040", "target_type_invalid"),
    ("no-reset-interface", None, "target_not_detected"), ("absent", None, "target_not_detected"),
], ids=["wrong-family", "no-reset-interface", "absent"])
def test_flash_preflight_prevents_load(pico, state, family, error):
    pico.set(STATE=state, CHIP="RP2350")
    config = runtime_config(pico.root / "workspace", family=family, flash_address="0x10000000")
    result = call(config, "flash_firmware", firmware(Path(config.work_dir)))
    assert result["ok"] is False, result
    if family:
        assert error == result["error_type"] or "chip" in str(result.get("summary", "")).lower(), result
    else:
        assert result["error_type"] == error, result
    assert_info(pico.devices())


def flash_transcript(*, state, write=True, verify=True, finish=None):
    entry = FORCE_ENTRY + "\n\n" if state == "application" else ""
    loading = WRITE_COMPLETE if write else WRITE_FRAME
    verification = VERIFY_COMPLETE if verify else VERIFY_BAR
    return entry + loading + verification + ("\n" + finish if finish else "")


@pytest.mark.parametrize(("state", "reset", "write", "verify", "finish"), [
    ("bootsel", False, False, True, None), ("bootsel", False, True, False, None),
    ("bootsel", True, True, True, None), ("application", True, True, True, None),
    ("application", False, True, True, START),
], ids=[
    "write-incomplete", "verify-missing-ok", "bootsel-start-missing-reboot",
    "application-start-missing-return", "application-hold-missing-left-accessible",
])
def test_exit_zero_needs_flash_confirmation(pico, state, reset, write, verify, finish):
    pico.set(STATE=state)
    workspace = pico.root / "workspace"
    config = runtime_config(workspace, flash_address="0x10000000")
    with closing_service(AgenticHILToolService(config)) as tools:
        pico.raw("load", stdout=flash_transcript(state=state, write=write, verify=verify, finish=finish))
        payload = firmware(workspace)
        result = tools.call("flash_firmware", {**payload, "reset_after_flash": reset})
    assert result["ok"] is False, result
    assert_contact(result, CONTACT_UNPROVEN)
    assert_load(pico, payload, state=state, reset=reset, address="0x10000000")


def test_forced_start_accepts_each_documented_confirmation(pico):
    pico.set(STATE="application")
    workspace = pico.root / "workspace"
    config = runtime_config(workspace, flash_address="0x10000000")
    payload = firmware(workspace)
    with closing_service(AgenticHILToolService(config)) as tools:
        for marker in (START, RETURN):
            pico.raw("load", stdout=flash_transcript(state="application", finish=marker))
            start = len(pico.runs())
            result = tools.call("flash_firmware", {**payload, "reset_after_flash": True})
            assert result["ok"] is True and state_is(result, "application"), result
            assert_load(pico, payload, state="application", reset=True, address="0x10000000", start=start)


@pytest.mark.parametrize(("state", "mode"), [
    ("bootsel", "run"), ("bootsel", "halt"), ("application", "run"), ("application", "halt"),
], ids=["bootsel-run", "bootsel-halt", "application-run", "application-halt"])
def test_reset_requires_its_own_confirmation(pico, state, mode):
    pico.set(STATE=state)
    workspace = pico.root / "workspace"
    config = runtime_config(workspace)
    result = call(config, "reset_target", {"mode": mode})
    destination = "application" if mode == "run" else "bootsel"
    assert result["ok"] is True and state_is(result, destination), result
    assert json.loads(pico.states.read_text(encoding="utf-8"))[SERIAL] == destination
    info, reset = pico.devices()
    assert_info([info])
    expected = {"reboot", "-a" if mode == "run" else "-u", "--ser", SERIAL}
    if state == "application":
        expected.add("-f")
    assert set(reset["argv"]) == expected

    pico.states.unlink()
    opposite = "BOOTSEL" if mode == "run" else "application"
    verb = "was asked to reboot" if state == "application" else "was rebooted"
    pico.raw("reboot", stdout=f"The device {verb} into {opposite} mode.\n")
    refused = call(config, "reset_target", {"mode": mode})
    assert refused["ok"] is False, refused
    assert_contact(refused, CONTACT_UNPROVEN)


@pytest.mark.parametrize("case", ["init", "undetected"], ids=["init", "undetected"])
def test_reset_refuses_before_reboot(pico, case):
    pico.set(STATE="absent")
    config = runtime_config(pico.root / "workspace")
    result = call(config, "reset_target", {"mode": "init" if case == "init" else "run"})
    assert result["ok"] is False
    if case == "init":
        assert result["error_type"] == "not_supported" and pico.runs() == [], result
    else:
        assert result["error_type"] == "target_not_detected", result
        assert_info(pico.devices())
    assert_contact(result, NOT_CONTACTED)


@pytest.mark.parametrize(("tool", "permission", "value", "start"), [
    ("flash_firmware", "allow_flash", False, False),
    ("reset_target", "allow_reset", False, False),
    ("flash_firmware", "allow_reset", False, True),
    ("flash_firmware", "allow_raw_debugger_commands", True, False),
    ("flash_firmware", "allow_mass_erase", True, False),
    ("probe_target", "allow_probe", False, False),
    ("debugger_probes_list", "allow_probe", False, False),
], ids=["flash", "reset", "post-flash-start", "raw-debugger-commands", "mass-erase", "legacy-probe-read", "legacy-list-read"])
def test_permission_refusal_before_spawn(pico, tool, permission, value, start):
    workspace = pico.root / "workspace"
    config = runtime_config(workspace, flash_address="0x10000000", permissions={**DEFAULT_TEST_PERMISSIONS, permission: value})
    args = {**firmware(workspace), "reset_after_flash": start} if tool == "flash_firmware" else {}
    result = call(config, tool, args)
    assert result["ok"] is False and result["error_type"] == "permission_denied", result
    assert result["permission"] == f"debuggers.dut.permissions.{permission}", result
    assert pico.runs() == []


@pytest.mark.parametrize("case", ["missing", "unsupported-major", "unreadable-version"], ids=["missing", "unsupported-major", "unreadable-version"])
def test_precontact_executable_refusal(pico, case):
    executable = FAKE
    if case == "missing":
        executable = pico.root / "missing-picotool.exe"
    elif case == "unsupported-major":
        pico.set(VERSION="3.0.0")
    else:
        pico.raw("version")
    config = runtime_config(pico.root / "workspace", executable=executable)
    with closing_service(AgenticHILToolService(config)) as tools:
        result = tools.call("probe_target")
        assert result["ok"] is False and result["error_type"] == "debugger_not_found", result
        expected = "picotool_not_found" if case == "missing" else "picotool_version_unsupported"
        assert result["backend_error_type"] == expected, result
        assert_contact(result, NOT_CONTACTED)
        assert pico.devices() == []
        assert bool(pico.runs()) is (case != "missing")
        assert_classification(tools, result)


DEBUG_CALLS = {
    "debug_start_session": {"image_path": "build/app.elf"},
    "debug_stop_session": {},
    "debug_get_session_status": {},
    "debug_set_breakpoint": {"location": "main"},
    "debug_list_breakpoints": {},
    "debug_clear_breakpoints": {},
    "debug_continue": {},
    "debug_halt": {},
    "debug_get_stop_reason": {},
    "debug_symbol_info": {"symbol": "main"},
    "debug_symbol_value": {"symbol": "main"},
    "debug_dump_symbol_ihex": {"symbol": "main", "output_path": "build/symbol.hex"},
}


@pytest.mark.parametrize("tool", list(DEBUG_CALLS), ids=list(DEBUG_CALLS))
def test_no_typed_debug(pico, tool):
    workspace = pico.root / "workspace"
    config = runtime_config(workspace)
    image = firmware(workspace, "elf")
    with closing_service(AgenticHILToolService(config)) as tools:
        if tool == "debug_symbol_info":
            flashed = tools.call("flash_firmware", image)
            assert flashed["ok"] is True, flashed
        start = len(pico.runs())
        result = tools.call(tool, DEBUG_CALLS[tool])
        assert result["ok"] is False and result["error_type"] == "not_supported", result
        assert len(pico.runs()) == start


def test_capabilities_and_plan_refusal_before_first_flash(pico):
    workspace = pico.root / "workspace"
    config = runtime_config(workspace, flash_address="0x10000000")
    firmware(workspace)
    plan = workspace / "plan.yaml"
    plan.write_text(
        "version: 6\nsteps:\n"
        "  - {device: dut, action: flash, image_path: build/app.bin}\n"
        "  - {device: dut, action: read_symbol, symbol: counter, size_bytes: 4, comparator: {equals: 1}}\n",
        encoding="utf-8",
    )
    with closing_service(AgenticHILToolService(config)) as tools:
        assert tools.backend.opens_debug_sessions() is False
        assert tools.backend.sessionless_debug_tools() == frozenset()
        result = TestReactor(config, tools).run(load_test_config(str(plan), config.work_dir))
    assert result["ok"] is False
    rejected = result["validation_error"]
    assert (rejected["step"], rejected["action"], rejected["debugger_type"]) == (2, "read_symbol", "picotool"), rejected
    assert pico.runs() == []


def start_run(tools):
    started = tools.call("bench_run_start", {"devices": [{"kind": "debugger", "id": "dut"}]})
    assert started["ok"] is True, started


def test_board_lease_follows_serial_across_aliases(pico):
    a = runtime_config(pico.root / "board-a", alias="first-local-alias")
    b = runtime_config(pico.root / "board-b", alias="second-local-alias")
    with closing_service(AgenticHILToolService(a)) as holder, closing_service(AgenticHILToolService(b)) as contender:
        start_run(holder)
        before = pico.runs()
        response = contender.call("bench_run_start", {"devices": [{"kind": "debugger", "id": "dut"}]})
        assert response["ok"] is False and response["error_type"] == "device_busy", response
        assert pico.runs() == before


def worker_probe(config, ready, result, lock_root):
    with pytest.MonkeyPatch.context() as patch:
        isolate_lock_namespace(patch, lock_root, peer=True)
        with closing_service(AgenticHILToolService(config, frontend="bench-server")) as tools:
            ready.set()
            result.send(tools.call("probe_target"))
    result.close()


def isolate_lock_namespace(monkeypatch, root, *, peer=False):
    monkeypatch.setattr(bench_module, "machine_device_lock_root", lambda: None)
    if os.name == "nt":
        # Model different Windows profiles with independent lock files and the
        # same named-pipe namespace. Use the unique test directory as its salt.
        files = root / ("peer" if peer else "holder")
        files.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(bench_module, "device_lock_root", lambda: files)
        pipe_name = windows_device_lock.pipe_name
        monkeypatch.setattr(windows_device_lock, "pipe_name", lambda digest, _root: pipe_name(digest, root))


def wait_for_record(pico, predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for record in pico.runs():
            if predicate(record):
                return record
        time.sleep(0.02)
    raise AssertionError(f"picotool invocation did not appear: {pico.runs()}")


def test_process_lock_spans_frontends_until_reaped_and_releases_between_calls(pico):
    alias = pico.root / "alternate-tool" / "picotool.py"
    alias.parent.mkdir()
    shutil.copyfile(FAKE, alias)
    release = pico.root / "release-first-command"
    pico.set(BOARDS=json.dumps({
        SERIAL: {"state": "application", "chip": "RP2040"},
        OTHER_SERIAL: {"state": "application", "chip": "RP2350"},
    }), HANG_COMMAND="info", HANG_SERIAL=SERIAL, HANG_RELEASE=release)
    a = runtime_config(pico.root / "board-a", timeout_s=scaled_time_bound(8))
    b = runtime_config(pico.root / "board-b", serial=OTHER_SERIAL, executable=alias, timeout_s=scaled_time_bound(3))
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    receive, send = context.Pipe(duplex=False)
    lock_root = bench_module.device_lock_root() / "picotool-test"
    isolate_lock_namespace(pico.monkeypatch, lock_root)
    with closing_service(AgenticHILToolService(a, frontend="mcp")) as holder:
        start_run(holder)
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(holder.call, "probe_target")
            child = context.Process(target=worker_probe, args=(b, ready, send, lock_root))
            try:
                running = wait_for_record(pico, lambda record: record["serial"] == SERIAL, scaled_time_bound(4))
                assert process_is_running(running["pid"])
                pico.set(WATCH_SERIAL=OTHER_SERIAL, WATCH_PIDS=json.dumps([running["pid"], running["child_pid"]]))
                paths = [str(ROOT / "tests"), os.environ.get("PYTHONPATH", "")]
                pico.monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, paths)))
                child.start()
                send.close()
                assert ready.wait(scaled_time_bound(4))
                # Keep the first command alive until the contender has either
                # refused or had time to reach its bounded acquisition.
                busy = receive.recv() if receive.poll(scaled_time_bound(1)) else None
                assert process_is_running(running["pid"])
                assert all(record["serial"] != OTHER_SERIAL for record in pico.runs())
                if busy is not None:
                    assert busy["error_type"] == "device_busy", busy
                    assert_contact(busy, NOT_CONTACTED)
                release.touch()
                assert first.result(timeout=scaled_time_bound(5))["ok"] is True
                assert not process_is_running(running["pid"])
                assert not process_is_running(running["child_pid"])
                if busy is None:
                    assert receive.poll(scaled_time_bound(5))
                    busy = receive.recv()
                    if busy["ok"] is False:
                        assert busy["error_type"] == "device_busy", busy
                        assert_contact(busy, NOT_CONTACTED)
                child.join(scaled_time_bound(5))
                assert child.exitcode == 0
                if busy["ok"] is False:
                    assert call(b, "probe_target")["ok"] is True
                assert holder.call("bench_run_status")["run_active"] is True
                first_record, second_record = pico.devices(None)
                assert first_record["serial"] == SERIAL and second_record["serial"] == OTHER_SERIAL
                assert second_record["live_predecessors"] == []
            finally:
                release.touch()
                if child.pid is not None and child.is_alive():
                    child.terminate()
                    child.join(scaled_time_bound(5))
                receive.close()
                send.close()


@pytest.mark.parametrize(("no_device", "too_big"), [(249, 248), (4294967289, 4294967288)], ids=["posix", "windows"])
def test_signed_status_and_words(pico, monkeypatch, no_device, too_big):
    workspace = pico.root / "workspace"
    config = runtime_config(workspace, flash_address="0x10000000")
    original = common.CompletedCommand
    with closing_service(AgenticHILToolService(config)) as tools:
        # Change just the OS representation delivered by the subprocess helper.
        # The transcript and execution still come from the real child.
        class EncodedCommand(original):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                code = {native_status(-7): no_device, native_status(-8): too_big}.get(self.returncode)
                if code is not None:
                    object.__setattr__(self, "returncode", code)

        monkeypatch.setattr(common, "CompletedCommand", EncodedCommand)
        pico.set(STATE="absent")
        result = tools.call("probe_target")
        assert result["error_type"] == "target_not_detected", result
        assert_contact(result, NOT_CONTACTED)
        pico.set(STATE="application")
        pico.raw("load", stdout=FORCE_ENTRY + "\n\n" + OVERSIZE + "\n", status=-8)
        result = tools.call("flash_firmware", firmware(workspace))
        assert result["error_type"] == "flash_failed", result
        assert_contact(result, CONTACT_UNPROVEN)


def test_oversized_image_after_forced_open_is_flash_failed_contact_unproven(pico):
    pico.set(STATE="application")
    pico.raw("load", stdout=FORCE_ENTRY + "\n\n" + OVERSIZE + "\n", status=-8)
    workspace = pico.root / "workspace"
    config = runtime_config(workspace, flash_address="0x10000000")
    with closing_service(AgenticHILToolService(config)) as tools:
        result = tools.call("flash_firmware", firmware(workspace))
        assert result["ok"] is False and result["error_type"] == "flash_failed", result
        assert_contact(result, CONTACT_UNPROVEN)
        assert OVERSIZE in json.dumps(result)
        assert_classification(tools, result)
    info, load = pico.devices()
    assert_info([info])
    assert load["command"] == "load" and "-F" in load["argv"]


@pytest.mark.parametrize(("status", "error"), [
    (-9, "flash_failed"), (-5, "flash_failed"), (-11, "verify_failed"),
], ids=["forced-reboot", "write", "verify"])
def test_postcontact_failure_preserves_uncertainty(pico, status, error):
    pico.set(STATE="application")
    transcript = FORCE_ENTRY + "\n\n"
    if status == -5:
        transcript += WRITE_FRAME
    elif status == -11:
        transcript += WRITE_COMPLETE + VERIFY_BAR + VERIFY_MISMATCH
    pico.raw("load", stdout=transcript, status=status)
    workspace = pico.root / "workspace"
    config = runtime_config(workspace, flash_address="0x10000000")
    with closing_service(AgenticHILToolService(config)) as tools:
        result = tools.call("flash_firmware", firmware(workspace))
        assert result["ok"] is False, result
        if status != -9:
            assert result["error_type"] == error, result
        assert_contact(result, CONTACT_UNPROVEN)
        assert_classification(tools, result)


def wait_for_exit(record):
    deadline = time.monotonic() + scaled_time_bound(3)
    pids = (record["pid"], record["child_pid"])
    assert all(isinstance(pid, int) for pid in pids), record
    while any(process_is_running(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not any(process_is_running(pid) for pid in pids), record


def test_timeout_reaps_fake_process_tree(pico):
    pico.set(HANG_COMMAND="info")
    config = runtime_config(pico.root / "workspace", timeout_s=scaled_time_bound(1))
    with closing_service(AgenticHILToolService(config)) as tools:
        start = time.monotonic()
        result = tools.call("probe_target")
        assert time.monotonic() - start < scaled_time_bound(12), result
        assert result["ok"] is False and result["error_type"] == "timeout", result
        assert_contact(result, CONTACT_UNPROVEN)
        assert_classification(tools, result)
    [record] = pico.devices()
    wait_for_exit(record)


def assert_classification(tools, failure):
    error = failure["error_type"]
    entry = catalogue_entry(f"{error}:picotool")
    assert entry is not None, error
    assert failure.get("remediation") == entry["remediation"], failure
    classified = tools.call("classify_last_error")
    assert classified["ok"] is True and classified["error_type"] == error, classified
    assert classified.get("remediation") == entry["remediation"], classified


def test_classification_uses_scoped_remedy_from_failed_operation(pico):
    pico.set(STATE="absent")
    config = runtime_config(pico.root / "workspace")
    with closing_service(AgenticHILToolService(config)) as tools:
        result = tools.call("probe_target")
        assert result["error_type"] == "target_not_detected", result
        assert_classification(tools, result)


@pytest.mark.parametrize("error", ["target_not_detected", "debugger_not_found"], ids=["target_not_detected", "debugger_not_found"])
def test_remedy_identifies_host_and_board_causes(error):
    remedy = catalogue_entry(f"{error}:picotool")
    if remedy is None:
        raise AssertionError("picotool scoped remedy is missing")
    content = json.dumps(remedy).lower()
    if error == "target_not_detected":
        for term in ("probe_id", "pico_stdio_usb", "bootsel", "winusb", "zadig", "udev", "rp2040"):
            assert term in content, remedy
        assert "ide" in content and "picotool" in content, remedy
    else:
        assert "install" in content and "executable" in content, remedy


def test_log_report_and_export_redact_tracking_serial_and_host_paths(pico):
    pico.set(STATE="application")
    workspace = pico.root / "workspace"
    tool_path = pico.root / "fixture-tools" / "picotool.py"
    tool_path.parent.mkdir()
    shutil.copyfile(FAKE, tool_path)
    config = runtime_config(workspace, executable=tool_path, flash_address="0x10000000")
    payload = firmware(workspace)
    # Preserve the programmer's diagnostic while exercising both streams.
    pico.raw("load", stdout=f"Tracking device serial number {SERIAL} for reboot\n{OVERSIZE}\n",
             stderr=f"{workspace}\n{tool_path}\n{{artifact}}\n", status=-8)
    result = call(config, "flash_firmware", payload)
    assert result["error_type"] == "flash_failed", result
    log_path = workspace / result["log_path"]
    report_path = workspace / result["report_path"]
    log_text = log_path.read_text(encoding="utf-8")
    raw_report = report_path.read_text(encoding="utf-8")
    assert OVERSIZE in log_text and OVERSIZE in raw_report
    destination = workspace / "evidence"
    write_run_evidence(str(report_path), str(destination), workspace=workspace, environ={})
    exported = [path.read_text(encoding="utf-8") for path in destination.rglob("*") if path.is_file()]
    assert exported
    [_, load] = pico.devices()
    for content in [log_text, raw_report, *exported]:
        assert SERIAL not in content, content

    report = json.loads(raw_report)
    report.pop("config_in_force", None)
    report.pop("canonical_report_path", None)
    host_paths = (str(tool_path), str(workspace), load["artifact"])
    for content in [log_text, json.dumps(report), *exported]:
        normal = content.replace("\\\\", "\\").replace("\\", "/")
        assert all(path.replace("\\", "/") not in normal for path in host_paths), content


def test_published_picotool_configuration_and_support_contract():
    paths = (
        "src/agentic_hil/schemas/config.schema.json",
        "docs/configuration.md", "docs/installation.md", "docs/support.md", "docs/mcp-tools.md",
        "TROUBLESHOOTING.md", "src/agentic_hil/skills/agentic-hil/SKILL.md",
        "plugins/agentic-hil/skills/agentic-hil/SKILL.md", "CHANGELOG.md",
    )
    fetched = [(ROOT / path).read_text(encoding="utf-8") for path in paths]
    schema = json.loads(fetched[0])
    config, install, support, mcp, troubleshooting, skill, plugin_skill, changelog = [text.lower() for text in fetched[1:]]
    types = schema["properties"]["debuggers"]["additionalProperties"]["properties"]["type"]["enum"]
    section = re.search(r"(?m)^#+ an rp2040 or rp2350 board through picotool\s*$([\s\S]*?)(?=^#+\s|\Z)", config)
    support_rows = [line for line in support.splitlines() if line.startswith("|") and "picotool 2" in line]
    unreleased = re.search(r"(?m)^## \[unreleased\]\s*$([\s\S]*?)(?=^##\s|\Z)", changelog)
    added = re.search(r"(?m)^### added\s*$([\s\S]*?)(?=^###\s|\Z)", unreleased[1]) if unreleased else None
    if not (
        "picotool" in types
        and section
        and all(word in section[1] for word in ("type: picotool", "probe_id", "com_ports_list", "bootsel", "pico_stdio_usb", "winusb", "udev", ".uf2", "allowed_extensions"))
        and "picotool" in install
        and re.search(r"(?m)^.*picotool.*separat.*$|^.*separat.*picotool.*$", install)
        and support_rows
        and all(word in support_rows[0] for word in ("uf2", "elf", "bin", "reboot", "prob", "usb", "debug"))
        and re.search(r"no (?:cpu |typed )?debug|debug[^|]*(?:unsupported|unavailable)", support_rows[0])
        and re.search(r"unproven|not yet proven|no hardware proof", support_rows[0])
        and not re.search(r"(?m)^all five backends\b", support)
        and all("picotool" in document for document in (mcp, troubleshooting, skill, plugin_skill))
        and added and "picotool" in added[1]
    ):
        raise AssertionError("published picotool configuration and support contract is incomplete")
