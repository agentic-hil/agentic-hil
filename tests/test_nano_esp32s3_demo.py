"""Offline acceptance of the Nano ESP32-S3 demo and its bench evidence."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from copy import deepcopy
from functools import reduce
from operator import xor
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

import pytest
import yaml
from conftest import FAKE_ESPTOOL
from test_esptool_backend import port_runs
from test_implicit_single_action_run import install_fake_serial
from test_serial_port_identity import fake_host, host_port, inventory

from agentic_hil.config import load_config
from agentic_hil.test_reactor import TestStep as PlanStep
from agentic_hil.test_reactor import load_test_config
from agentic_hil.tools import AgenticHILToolService

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_esp32s3_demo_images as build  # noqa: E402
import esp32s3_demo_results as results  # noqa: E402

DEMO_PATH = "examples/nano-esp32s3_demo"
DEMO = ROOT / DEMO_PATH
HIL = ROOT / "tests" / "hil"
QUALIFICATION = HIL / "nano_esp32s3_shared_bench.yaml"
REFUSALS = {action: HIL / f"nano_esp32s3_console_refuses_{action}.yaml" for action in ("flash", "reset")}
IMAGES = {"A": "banner_a.bin", "B": "banner_b.bin"}
SOURCES = ("Src/banner.c", "esp32s3_ram.ld")
DEVICE = "esp32s3"
ACTIONS = ("flash", "reset", "uart_open", "uart_read", "uart_read", "uart_close")
UART_OPEN = PlanStep("uart_open", {"clear_buffer": True}, port_id=DEVICE)
UART_CLOSE = PlanStep("uart_close", {}, port_id=DEVICE)
RESET = PlanStep("reset", {"mode": "run"}, debugger=DEVICE)
PLACEHOLDER = re.compile(r"<[^<>]+>|\{[^{}]+\}|\b(?:example|placeholder|replace|your|path/to)\b", re.I)


def banner(letter: str, tick: int, ending: str = "\n") -> str:
    return f"agentic-hil esp32s3 image {letter} tick {tick}{ending}"


def flash(letter: str) -> PlanStep:
    return PlanStep("flash", {"image_path": f"{DEMO_PATH}/{IMAGES[letter]}", "reset_after_flash": False}, debugger=DEVICE)


def comparator(step: PlanStep, port: str) -> re.Pattern[str]:
    assert step.action == "uart_read" and step.port_id == port
    assert 0 < step.arguments["timeout_s"] < float("inf")
    return re.compile(step.arguments["comparator"]["pattern"])


def assert_banner_match(pattern: re.Pattern[str], expected: str) -> None:
    match = pattern.search(expected)
    assert match is not None and match.span() == (0, len(expected))


def check_image_patterns(first: re.Pattern[str], advancing: re.Pattern[str], letter: str) -> None:
    other = "B" if letter == "A" else "A"
    for ending in ("\n", "\r\n"):
        for tick in (0, 37, 4294967295):
            assert_banner_match(first, banner(letter, tick, ending))
        tick50 = banner(letter, 50, ending)
        assert_banner_match(advancing, tick50 + banner(letter, 51, ending))
        assert first.search(banner(other, 37, ending)) is None
        assert advancing.search(banner(other, 50, ending) + banner(other, 51, ending)) is None
        assert advancing.search(tick50 + banner(other, 51, ending)) is None
        assert advancing.search(tick50 + banner(letter, 52, ending)) is None
        assert advancing.search(tick50 + banner(letter, 51, "")) is None
    assert first.search(banner(letter, 37, "")) is None


def load_bench_plan(path: Path):
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw.keys() == {"version", "name", "steps"} and raw["version"] == 4
    return load_test_config(str(path), str(ROOT))


def test_shared_plans() -> None:
    (repeat,) = load_bench_plan(QUALIFICATION).steps
    assert repeat.action == "repeat" and not repeat.route_keys
    assert repeat.arguments == {"count": 20}
    assert len(repeat.steps) == 12
    for half, letter in enumerate(IMAGES):
        flash_step, reset, opened, first, advancing, closed = repeat.steps[half * 6 : (half + 1) * 6]
        assert (flash_step, reset, opened, closed) == (flash(letter), RESET, UART_OPEN, UART_CLOSE)
        assert first.route_keys == advancing.route_keys == ["port_id"]
        check_image_patterns(comparator(first, DEVICE), comparator(advancing, DEVICE), letter)

    for action, path in REFUSALS.items():
        opened, read, refused = load_bench_plan(path).steps
        assert opened == UART_OPEN
        assert refused == {"flash": flash("A"), "reset": RESET}[action]
        assert read.route_keys == ["port_id"]
        pattern = comparator(read, DEVICE)
        for letter in IMAGES:
            for ending in ("\n", "\r\n"):
                assert_banner_match(pattern, banner(letter, 9, ending))
            assert pattern.search(banner(letter, 9, "")) is None
        assert pattern.search(banner("C", 9)) is None


def test_pinned_image_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = [*IMAGES.values(), "manifest.json"]
    before = {name: (DEMO / name).read_bytes() for name in outputs if (DEMO / name).is_file()}
    compiler = tmp_path / "xtensa-esp32s3-elf-gcc.exe"
    compiler.touch()
    calls = []

    def find_compiler(name: str, **kwargs) -> str:
        assert name in {"xtensa-esp32s3-elf-gcc", compiler.name}
        assert Path(kwargs["path"]) == tmp_path
        return str(compiler)

    def compiler_version(argv, **kwargs):
        calls.append(argv)
        assert [str(arg) for arg in argv] == [str(compiler), "--version"]
        banner_text = "xtensa-esp32s3-elf-gcc (crosstool-NG esp-16.1.0_20260608) 16.1.0\n"
        return subprocess.CompletedProcess(argv, 0, stdout=banner_text, stderr="")

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "which", find_compiler)
        patch.setattr(subprocess, "run", compiler_version)
        with pytest.raises(SystemExit) as rejected:
            build.main(["--toolchain", str(tmp_path)])
    assert rejected.value.code not in (None, 0)
    assert calls, "The toolchain release must be read from the supplied compiler."
    assert {name: (DEMO / name).read_bytes() for name in outputs if (DEMO / name).is_file()} == before

    toolchain = os.environ.get("NANO_ESP32S3_TEST_TOOLCHAIN")
    if toolchain:
        checkout = tmp_path / "checkout"
        script = checkout / "tools" / "build_esp32s3_demo_images.py"
        script.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "tools" / script.name, script)
        for source in SOURCES:
            target = checkout / DEMO_PATH / source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(DEMO / source, target)
        subprocess.run([sys.executable, str(script), "--toolchain", toolchain], cwd=checkout, check=True)
        for name in outputs:
            assert (checkout / DEMO_PATH / name).read_bytes() == before[name], name


def rom_payload(path: Path, regions: dict[str, tuple[int, int]]) -> tuple[bytes, bytes]:
    data = path.read_bytes()
    magic, segment_count, flash_mode, _, entry = struct.unpack_from("<BBBBI", data)
    extended = struct.unpack_from("<BBBBHBHH4sB", data, 8)
    assert (magic, flash_mode, extended[4], extended[9]) == (0xE9, 2, 9, 1)
    iram, iram_size = regions["iram_seg"]
    assert iram <= entry < iram + iram_size and segment_count > 0
    offset = 24
    segments = []
    for _ in range(segment_count):
        address, length = struct.unpack_from("<II", data, offset)
        offset += 8
        assert length > 0 and offset + length <= len(data)
        assert any(start <= address and address + length <= start + size for start, size in regions.values())
        segments.append(data[offset : offset + length])
        offset += length
    checksum = offset + 15 - offset % 16
    assert data[checksum] == reduce(xor, b"".join(segments), 0xEF)
    assert data[checksum + 1 :] == hashlib.sha256(data[: checksum + 1]).digest()
    return data[:checksum], b"".join(segments)


def leaves(steps):
    for step in steps:
        if step.action == "repeat":
            yield from leaves(step.steps)
        else:
            yield step


def test_demo_plan_and_rom_images() -> None:
    demo_plan = load_test_config(str(DEMO / "testconfig.yaml"), str(DEMO))
    bench_plan = load_bench_plan(QUALIFICATION)
    manifest = json.loads((DEMO / "manifest.json").read_text(encoding="utf-8"))
    assert (manifest["chip"], manifest["flash_address"]) == ("esp32s3", "0x0")
    toolchain = manifest["toolchain"]
    assert toolchain["release"] == "esp-16.1.0_20260609"
    assert re.fullmatch(r"\d+\.\d+\.\d+", toolchain["gcc"])
    assert toolchain["cflags"] and all(isinstance(flag, str) for flag in toolchain["cflags"])
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[a-z0-9.]*)", manifest["esptool"])
    assert not any(re.search(r"[A-Za-z]:[/\\]|(?:^|[=,])/(?:[^/]+/)+", flag) for flag in toolchain["cflags"])
    source_hashes = {PurePosixPath(name).name: digest for name, digest in manifest["sources"].items()}
    assert source_hashes == {Path(source).name: sha256(DEMO / source) for source in SOURCES}
    for name in manifest["sources"]:
        assert name in (*SOURCES, "banner.c")

    script = (DEMO / "esp32s3_ram.ld").read_text(encoding="utf-8")
    regions = {
        name.lower(): (int(origin, 16), int(length, 16))
        for name, origin, length in re.findall(
            r"(?im)^\s*(\w+)\s*\([a-z]+\)\s*:\s*org(?:in)?\s*=\s*(0x[0-9a-f]+),\s*len(?:gth)?\s*=\s*(0x[0-9a-f]+)", script
        )
    }
    assert regions == {"dram_seg": (0x3FCE2700, 0x5000), "iram_seg": (0x403C8700, 0x3000)}
    code = (DEMO / "Src" / "banner.c").read_text(encoding="utf-8")
    (literal,) = re.findall(r'"agentic-hil esp32s3 image %[sc] tick %l?u\\n"', code)
    format_string = ast.literal_eval(literal)
    assert set(manifest["images"]) == set(IMAGES.values())
    rom = {}
    for letter, name in IMAGES.items():
        path = DEMO / name
        record = manifest["images"][name]
        assert (record["image"], record["sha256"], record["size"]) == (letter, sha256(path), path.stat().st_size)
        rom[letter], payload = rom_payload(path, regions)
        assert (format_string.encode() + b"\0") in payload
        for tick in (0, 50, 51):
            assert format_string % (letter, tick) == banner(letter, tick)
    assert len(rom["A"]) == len(rom["B"])
    assert [(a, b) for a, b in zip(rom["A"], rom["B"], strict=True) if a != b] == [(ord("A"), ord("B"))]

    for plan, base in ((demo_plan, DEMO), (bench_plan, ROOT)):
        selected = []
        letter = None
        for step in leaves(plan.steps):
            if step.action == "flash":
                image = (base / step.arguments["image_path"]).resolve().relative_to(DEMO).as_posix()
                selected.append(image)
                letter = manifest["images"][image]["image"]
            elif step.action == "uart_read":
                assert letter is not None
                pattern = re.compile(step.arguments["comparator"]["pattern"])
                first_line = format_string % (letter, 50)
                assert pattern.search(first_line + format_string % (letter, 51)) is not None
        assert selected == list(IMAGES.values())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_placeholder(value: object) -> bool:
    return isinstance(value, str) and bool(PLACEHOLDER.search(value))


def load_example(document: dict, case: Path):
    work = case / "work"
    work.mkdir(parents=True)
    path = case / "config.yaml"
    path.write_text(yaml.safe_dump({**document, "workspace_root": str(work), "state_root": str(case / "state")}), encoding="utf-8")
    return load_config(str(path), str(work)), work


def test_native_usb_port_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    template = yaml.safe_load((DEMO / "config.example.yaml").read_text(encoding="utf-8"))
    config, _ = load_example(template, tmp_path / "template")
    matches = [(name, entry) for name, entry in config.debuggers.items() if entry.type == "esptool" and entry.target_type == DEVICE]
    if config.config_version != 3 or len(matches) != 1 or matches[0][1].com_port not in config.com_ports:
        raise AssertionError("The example must bind the ESP32-S3 debugger to its pinned native USB console.")
    debugger_id, debugger = matches[0]
    port_id = debugger.com_port
    port = config.com_ports[port_id]
    assert debugger.flash_address == "0x0"
    assert debugger.permissions.allow_flash and debugger.permissions.allow_reset
    assert port.baudrate == 115200 and not port.permissions.allow_write
    assert all(is_placeholder(value) for value in (port.device, port.serial_number, template["workspace_root"], template["state_root"]))
    assert ".bin" in config.artifacts.allowed_extensions

    serial = "02:42:ac:10:12:01"
    by_id = f"/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_{serial}-if00"
    document = deepcopy(template)
    document["debuggers"][debugger_id]["executable"] = str(FAKE_ESPTOOL)
    document["com_ports"][port_id].update(device=by_id, serial_number=serial)
    port_values = {"stable": by_id, "vid": 0x303A, "pid": 0x1001}
    present = host_port("/dev/ttyACM0", serial, **port_values)
    duplicate = host_port("/dev/ttyACM1", serial, **port_values)
    foreign = host_port("/dev/ttyACM0", "02:42:ac:10:12:02", **port_values)
    cases = (
        ("missing", inventory(), "com_port_identity_unverified"),
        ("ambiguous", inventory(present, duplicate), "com_port_identity_unverified"),
        ("other-board", inventory(foreign), "com_port_identity_mismatch"),
        ("console-open", inventory(present), "device_busy"),
    )
    install_fake_serial(monkeypatch)
    monkeypatch.setenv("AGENTIC_HIL_FAKE_ESPTOOL_CHIP", "ESP32-S3")
    for case, ports, error_type in cases:
        loaded, work = load_example(document, tmp_path / case)
        for name in IMAGES.values():
            shutil.copyfile(DEMO / name, work / name)
        calls = tmp_path / f"{case}-esptool.jsonl"
        monkeypatch.setenv("AGENTIC_HIL_FAKE_ESPTOOL_LOG", str(calls))
        fake_host(monkeypatch, ports)
        service = AgenticHILToolService(loaded)
        try:
            if case == "console-open":
                opened = service.call("com_session_start", {"port_id": port_id})
                assert opened["ok"], opened
            refusal = service.call("flash_firmware", {"image_path": IMAGES["A"]})
            assert not refusal["ok"] and refusal["error_type"] == error_type, refusal
            if case == "console-open":
                assert refusal["held_by_com_session"] == port_id
            assert not port_runs(calls)
        finally:
            service.close()


JOB_ID = "job-0123456789abc123"
CONFIG_DIGEST = "sha256:" + "3c" * 32
AGENTIC_HIL_VERSION = "1.2.3"
CHIP = "ESP32-S3 (QFN56) (revision v0.2)"
USB_MODE = "USB-Serial/JTAG"
RESET_LINES = ("Hard resetting with a watchdog...", "Hard resetting via RTS pin...")
PRIVATE_MAC = "02:42:ac:10:12:03"
PRIVATE_DEVICE = f"/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_{PRIVATE_MAC}-if00"
PRIVATE_TTY = "/dev/ttyACM17"
PRIVATE_HOST = "bench-42.internal.example"
PRIVATE_USER = "nano-operator"
PRIVATE_ADDRESS = "192.168.17.29"
PRIVATE_ROOT = f"/home/{PRIVATE_USER}/runs/{JOB_ID}/workspace"
UNKNOWN_ADDRESS = "10.12.14.16"
REPORT_PATH = ".agentic-hil/reports/last-report.json"
CANONICAL_REPORT = f"/home/{PRIVATE_USER}/bench-state/runs/run-0123456789abc123/report.json"
COM_RESOURCE = f"com:serial:{PRIVATE_MAC.lower()}"
STARTED = "2026-11-25T16:00:00.000Z"
FINISHED = "2026-11-25T16:00:02.000Z"


def step(index: int, action: str, duration_ms: int, **result: object) -> dict:
    return {"index": index, "action": action, "route": DEVICE, "elapsed_ms": duration_ms, "result": {"ok": True, **result}}


def observed(text: str) -> dict:
    return {"text": text, "hex": text.encode().hex(), "encoding": "utf-8"}


def esptool_log(logs: dict[str, dict | str], filename: str, action: str, reset_line: str, *, image: str = IMAGES["A"]) -> str:
    command = [f"//{PRIVATE_ADDRESS}/opt/esptool", "--chip=esp32s3", f"--port={PRIVATE_DEVICE}"]
    if action == "flash":
        command.extend(["--after=no-reset", "write-flash", "0x0", f"{PRIVATE_ROOT}/runner-firmware/{image}"])
    else:
        after = "hard-reset" if reset_line.startswith("Hard resetting") else "no-reset"
        command.extend([f"--after={after}", "flash-id"])
    stdout = f"Chip type: {CHIP}\nUSB mode: {USB_MODE}\nMAC: <redacted-mac>\n"
    if action == "flash":
        stdout += "Hash of data verified.\n"
    logs[filename] = {"command": " ".join(command), "stdout": stdout + reset_line + "\n", "stderr": "", "returncode": 0, "timed_out": False}
    return f".agentic-hil/logs/{filename}"


def action_receipt(report: dict, tool: str, summary: str, **details: object) -> dict:
    resources = [COM_RESOURCE]
    if tool in {"flash_firmware", "reset_target"}:
        resources.append("debugger-discovery:all")
    return {
        **{key: report[key] for key in ("job_id", "submitted_by", "submitted_by_source", "config_in_force", "audit_ok", "report_path")},
        "lease_id": "lease-0123456789abc123",
        "lease_state": "active",
        "resources": resources,
        "cleanup_required": False,
        "cleanup_reasons": [],
        "quarantined": False,
        "quarantine_id": None,
        "safe_state_confirmed": False,
        "processes_reaped": False,
        "tool": tool,
        "summary": summary,
        "port_id": DEVICE,
        **details,
    }


def port_identity() -> dict:
    return {
        "device": PRIVATE_TTY,
        "stable_device": PRIVATE_DEVICE,
        "expected_from": f"com_ports.{DEVICE}.serial_number",
        "expected_serial_number": PRIVATE_MAC,
        "found_serial_number": PRIVATE_MAC,
        "expected_vid": 0x303A,
        "expected_pid": 0x1001,
        "found_vid": 0x303A,
        "found_pid": 0x1001,
        "status": "confirmed",
        "summary": f"Serial number {PRIVATE_MAC} matches the adapter at {PRIVATE_DEVICE}.",
    }


def debugger_receipt(report: dict, tool: str, elapsed_ms: int, log_path: str, *markers: str, **details: object) -> dict:
    return action_receipt(
        report, tool, "esptool completed on the selected ESP32-S3.",
        started_at=STARTED, finished_at=FINISHED, elapsed_ms=elapsed_ms, backend="esptool",
        side_effect_committed=True, side_effect_status="committed", retry_safe=False, success_confirmed=True,
        operation_result={"confirmed": True, "matched_success_text": list(markers)},
        port_identity=port_identity(), log_path=log_path, **details,
    )


def uart_receipts(report: dict, logs: dict[str, dict | str], stamp: str) -> tuple[dict, dict]:
    filename = f"com-{stamp}-{DEVICE}.jsonl"
    records = (
        {"event": "start", "port_id": DEVICE, "device": PRIVATE_DEVICE},
        {"event": "stop", "reason": "session_stop"},
    )
    logs[filename] = "".join(json.dumps({**record, "job_id": JOB_ID, "submitted_by": PRIVATE_USER}) + "\n" for record in records)
    session = {"started_at": STARTED, "rx_buffer_bytes": 0, "overflow_bytes": 0, "log_path": f".agentic-hil/logs/{filename}"}
    opened = action_receipt(
        report, "com_session_start", "COM port session started.", already_active=False,
        first_contact_at=STARTED, first_contact_by="com_session_start", identity=port_identity(),
        session={**session, "session_active": True},
    )
    closed = action_receipt(
        report, "com_session_stop", "COM port session stopped.", ok=True, was_active=True,
        session={**session, "session_active": False},
    )
    return opened, closed


def uart_read_step(index: int, elapsed_ms: int, plan_step: PlanStep, text: str) -> dict:
    return step(
        index, "uart_read", elapsed_ms, tool="test_reactor_run", summary="UART text matched the comparator.",
        port_id=DEVICE, bytes_received=len(text.encode()), reads=len(text.splitlines()),
        timeout_s=plan_step.arguments["timeout_s"], comparator=plan_step.arguments["comparator"],
        matched_text=observed(text), matched_text_truncated=False,
    )


def evidence_header(path: Path, ok: bool) -> tuple[dict, dict]:
    name = load_bench_plan(path).name
    # The client submits a rewritten plan with its upload paths substituted.
    plan = {"name": name, "sha256": hashlib.sha256(f"transferred {name}".encode()).hexdigest(), "path": "testconfig.yaml"}
    assert plan["sha256"] != sha256(path)
    config = {
        "path": f"/home/{PRIVATE_USER}/bench.yaml",
        "digest_algorithm": "sha256",
        "digest": CONFIG_DIGEST,
        "description_source": "startup",
        "file_state": "unchanged",
        "file_digest": CONFIG_DIGEST,
        "diverged_from_file": False,
        "checked_at": FINISHED,
    }
    outcome = "completed" if ok else "failed"
    report = {
        "ok": ok,
        "tool": "test_reactor_run",
        "run": "run-0123456789abc123",
        "job_id": JOB_ID,
        "name": plan["name"],
        "test_config_sha256": plan["sha256"],
        "test_config_path": f"{PRIVATE_ROOT}/{plan['path']}",
        "submitted_by": PRIVATE_USER,
        "submitted_by_source": "client",
        "config_in_force": config,
        "declared_devices": [COM_RESOURCE],
        "audit_ok": True,
        "cleanup": [],
        "cleanup_errors": [],
        "cleanup_ok": True,
        "report_path": REPORT_PATH,
        "canonical_report_path": CANONICAL_REPORT,
        "summary": f"Test reactor sequence {outcome}. Report: {CANONICAL_REPORT}",
    }
    summary = {
        "outcome": "success" if ok else "failure",
        "plan": plan,
        "tools": {"agentic_hil": AGENTIC_HIL_VERSION, "python": "3.13.1"},
        "bench": {
            "config_digest": CONFIG_DIGEST,
            "digest_algorithm": config["digest_algorithm"],
            "diverged_from_file": config["diverged_from_file"],
            "devices": {"debuggers": [DEVICE], "com_ports": [DEVICE]},
            "runner": {"name": PRIVATE_HOST},
        },
        "run": {"cleanup_ok": True, "audit_ok": True},
    }
    return report, summary


def results_argv(path: Path, report: dict, summary: dict, logs: dict[str, dict | str], images: dict, *, refusal: bool = False) -> list[str]:
    path.mkdir(exist_ok=True)
    report_path = path / "report.json"
    manifest_path = path / "manifest.json"
    evidence_path = path / f"{JOB_ID}-evidence.zip"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    manifest_path.write_text(json.dumps({"chip": "esp32s3", "flash_address": "0x0", "images": images}), encoding="utf-8")
    with ZipFile(evidence_path, "w") as archive:
        archive.writestr("job-summary.md", f"# Agentic HIL run\n\nOutcome: {summary['outcome']}\n")
        archive.writestr("run-summary.json", json.dumps(summary))
        for name, record in logs.items():
            archive.writestr(f"logs/{name}", record if isinstance(record, str) else json.dumps(record))
    args = ["--report", str(report_path), "--evidence", str(evidence_path), "--manifest", str(manifest_path)]
    return [*args, "--refusal"] if refusal else args


def run_results(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[object, str]:
    try:
        exit_code = results.main(args)
    except SystemExit as stopped:
        exit_code = stopped.code
        assert all(marker not in str(exit_code) for marker in private_markers())
    captured = capsys.readouterr()
    assert all(marker not in captured.out + captured.err for marker in private_markers())
    return exit_code, captured.out


def private_markers() -> tuple[str, ...]:
    return PRIVATE_MAC, PRIVATE_DEVICE, PRIVATE_TTY, PRIVATE_HOST, PRIVATE_USER, PRIVATE_ADDRESS, PRIVATE_ROOT, CANONICAL_REPORT, UNKNOWN_ADDRESS


def check_public_header(public: dict, summary: dict) -> None:
    assert public["job_id"] == JOB_ID
    assert public["plan"]["name"] == summary["plan"]["name"]
    assert public["plan"]["sha256"] == summary["plan"]["sha256"]
    assert public["agentic_hil"] == AGENTIC_HIL_VERSION
    assert public["config_digest"] == CONFIG_DIGEST


def test_qualification_results(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report, summary = evidence_header(QUALIFICATION, True)
    (repeat_plan,) = load_bench_plan(QUALIFICATION).steps
    images = {name: {"image": letter, "sha256": letter.lower() * 64, "size": 240 + index} for index, (letter, name) in enumerate(IMAGES.items())}
    logs: dict[str, dict | str] = {}
    iterations = []
    expected_cycles = []
    for iteration in range(1, 21):
        nested = []
        for half, (letter, image) in enumerate(IMAGES.items()):
            index = half * len(ACTIONS) + 1
            durations = [iteration * 1000 + index * 10 + i for i in range(len(ACTIONS))]
            first = banner(letter, iteration)
            pair = banner(letter, 50) + banner(letter, 51)
            image_ref = {"path": f"runner-firmware/{image}", "source": "path", "sha256": images[image]["sha256"]}
            reset_line = RESET_LINES[(iteration + half) % 2]
            stamp = f"202611251600{iteration:02d}{half:03d}Z"
            flash_log = esptool_log(logs, f"esptool-{stamp}-flash_firmware.log", "flash", "Staying in bootloader.", image=image)
            reset_log = esptool_log(logs, f"esptool-{stamp}-reset_target.log", "reset", reset_line)
            flashed = debugger_receipt(
                report, "flash_firmware", durations[0], flash_log, "Hash of data verified.", "Staying in bootloader.",
                verify=True, reset_after_flash=False, artifact=image_ref, held_in_rom_bootloader=True,
            )
            reset = debugger_receipt(
                report, "reset_target", durations[1], reset_log, "Hard resetting", mode="run", held_in_rom_bootloader=False,
            )
            opened, closed = uart_receipts(report, logs, stamp)
            nested.extend([
                step(index, "flash", durations[0], **flashed),
                step(index + 1, "reset", durations[1], **reset),
                step(index + 2, "uart_open", durations[2], **opened),
                uart_read_step(index + 3, durations[3], repeat_plan.steps[index + 2], first),
                uart_read_step(index + 4, durations[4], repeat_plan.steps[index + 3], pair),
                step(index + 5, "uart_close", durations[5], **closed),
            ])
            expected_cycles.append({
                "iteration": iteration, "image": letter, "artifact_sha256": image_ref["sha256"], "matches_manifest": True,
                "verify": True, "console_lines": [first.rstrip("\r\n"), *pair.splitlines()], "durations_ms": durations,
                "chip": CHIP, "usb_mode": USB_MODE, "reset_line": reset_line,
            })
        iterations.append({"iteration": iteration, "steps": nested})
    repeat = step(1, "repeat", 228000, count=20, iterations_run=20, exit_reason="count")
    repeat.update(route="-", iterations=iterations)
    report["steps"] = [repeat]

    status, output = run_results(results_argv(tmp_path, report, summary, logs, images), capsys)
    assert status == 0
    public = json.loads(output)
    check_public_header(public, summary)
    assert public["images"] == images
    for actual, expected in zip(public["cycles"], expected_cycles, strict=True):
        assert {key: actual[key] for key in expected} == expected
        assert actual["reset"]["ok"] is True and actual["reset"]["mode"] == "run"

    failed = deepcopy(report)
    # A successful run or bundle summary cannot replace the individual flash verdict.
    failed["steps"][0]["iterations"][0]["steps"][0]["result"].update(ok=False, error_type="verify_failed")
    mismatched = deepcopy(report)
    mismatched["steps"][0]["iterations"][0]["steps"][0]["result"]["artifact"]["sha256"] = "c" * 64
    incomplete = deepcopy(report)
    incomplete["steps"][0]["iterations"].pop()
    unknown_identity = {**report, "unexpected_metadata": {"value": UNKNOWN_ADDRESS}}
    for case in (failed, mismatched, incomplete):
        status, _ = run_results(results_argv(tmp_path, case, summary, logs, images), capsys)
        assert status not in (None, 0)
    status, output = run_results(results_argv(tmp_path, unknown_identity, summary, logs, images), capsys)
    assert status not in (None, 0) and not output


def test_refusal_results(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    images = {name: {"image": letter, "sha256": letter.lower() * 64, "size": 240} for letter, name in IMAGES.items()}
    for action, plan in REFUSALS.items():
        report, summary = evidence_header(plan, False)
        logs: dict[str, dict | str] = {}
        opened, closed = uart_receipts(report, logs, "20261125161435800Z")
        recovery_log = esptool_log(logs, "esptool-20261125161436110Z-reset_target.log", "reset", "Staying in bootloader.")
        recovery = {"attempted": True, "outcome": "recovered", "auto_recover_policy": "reset_halt"}
        tool = "flash_firmware" if action == "flash" else "reset_target"
        refused = {
            "ok": False,
            "tool": tool,
            "error_type": "device_busy",
            "summary": f"Physical device {COM_RESOURCE} is held by pid 4242 on {PRIVATE_HOST} (reactor) running {report['name']}.",
            "resource": COM_RESOURCE,
            "resources": [COM_RESOURCE, "debugger-discovery:all"],
            "holder": {"owner_id": "abcdef0123456789", "pid": 4242, "host": PRIVATE_HOST, "frontend": "reactor", "label": report["name"]},
            "holder_is_this_process": True,
            "held_since": STARTED,
            "heartbeat_at": FINISHED,
            "heartbeat_age_s": 0.1,
            "side_effect_committed": False,
            "retry_safe": True,
            "remediation": [f"Close the console session for {PRIVATE_DEVICE} before trying again."],
            "do_not": ["Do not flash or reset the target while its console is open."],
            "held_by_com_session": DEVICE,
            "next_step": f"Stop the COM session on {DEVICE} with com_session_stop, call {tool} again, then start the session again to read what the board prints.",
        }
        report.update(
            failed_step=3, error_type="device_busy", step_error_type="device_busy",
            remediation=refused["remediation"], do_not=refused["do_not"],
            recovery={**recovery, "reset": {"ok": True, "mode": "halt", "log_path": recovery_log}},
            cleanup=[{"port_id": DEVICE, "action": "uart_close", "result": closed}],
            steps=[
                step(1, "uart_open", 100, **opened),
                uart_read_step(2, 94, load_bench_plan(plan).steps[1], banner("A", 9)),
                step(3, action, 7, **refused),
            ],
        )
        summary["run"].update(failed_step=3, error_type="device_busy", recovery=recovery)
        status, output = run_results(results_argv(tmp_path, report, summary, logs, images, refusal=True), capsys)
        assert status == 0
        public = json.loads(output)
        check_public_header(public, summary)
        assert public["failed_step"] == 3 and public["action"] == action
        assert public["error_type"] == "device_busy" and public["held_by_com_session"] == DEVICE
        assert public["esptool_log_present"] is False

        other_error = deepcopy(report)
        other_error.update(error_type="permission_denied", step_error_type="permission_denied")
        other_error["steps"][2]["result"]["error_type"] = "permission_denied"
        other_holder = deepcopy(report)
        other_holder["steps"][2]["result"]["held_by_com_session"] = "other_console"
        earlier = deepcopy(report)
        earlier.update(failed_step=2, error_type="comparator_unmet", step_error_type="comparator_unmet")
        earlier["steps"][1:] = [step(2, "uart_read", 5000, ok=False, error_type="comparator_unmet")]
        contacted = deepcopy(report)
        contacted_logs = deepcopy(logs)
        contacted["steps"][2]["result"]["log_path"] = esptool_log(
            contacted_logs, f"esptool-20261125161436050Z-{tool}.log", action,
            "Staying in bootloader." if action == "flash" else RESET_LINES[0],
        )
        for case in (other_error, other_holder, earlier, contacted):
            case_summary = {**summary, "run": {**summary["run"], "failed_step": case["failed_step"], "error_type": case["error_type"]}}
            evidence_logs = contacted_logs if case is contacted else logs
            status, _ = run_results(results_argv(tmp_path, case, case_summary, evidence_logs, images, refusal=True), capsys)
            assert status not in (None, 0)
        unknown = {**report, "unexpected_metadata": {"value": UNKNOWN_ADDRESS}}
        status, output = run_results(results_argv(tmp_path, unknown, summary, logs, images, refusal=True), capsys)
        assert status not in (None, 0) and not output


def test_checkout_delivery() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    directives = {" ".join(line.split()) for line in manifest.splitlines()}
    expected = {f"prune {DEMO_PATH}", "exclude tests/test_nano_esp32s3_demo.py"}
    if not expected <= directives:
        raise AssertionError("MANIFEST.in must prune the Nano example and exclude its checkout-only test.")

    readme = " ".join((DEMO / "README.md").read_text(encoding="utf-8").lower().split())
    configuration = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8").lower()
    support = " ".join((ROOT / "docs" / "support.md").read_text(encoding="utf-8").lower().split())
    nano_sections = [
        " ".join(section.split()) for section in re.split(r"(?m)^#{1,3}\s+", configuration)
        if "nano esp32" in section.partition("\n")[0]
    ]
    assert any(
        all(term in section for term in ("native usb", "esp32s3", "by-id", "mac", "serial", "lock", "command", "sanitiz"))
        for section in nano_sections
    )
    assert all(term in readme for term in ("0x0", "bootloader", "native usb", "config.example.yaml", "testconfig.yaml", "esp-16.1.0_20260609"))
    assert re.search(r"\b(?:replac|overwrit)\w*\b", readme)
    assert re.search(r"\b(?:restor\w*|writ\w* back|reflash\w*)\b", readme)
    assert "nano esp32" in support
    limits = f"{readme} {support}"
    assert all(term in limits for term in ("probe", "offline", "device_busy", "held_by_com_session"))
    assert re.search(r"physical(?:ly)? disconnect|unplug", limits)
    assert re.search(r"\b(?:pull request|pr)\b", limits)
    assert re.search(r"(?:two|multiple|competing|concurrent)\s+(?:port\s+)?(?:owners|clients|sessions)", limits)
    for plan in REFUSALS.values():
        assert plan.name in limits

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    releases = re.findall(r"(?ms)^## \[(Unreleased|\d+\.\d+\.\d+)\][^\n]*\n(.*?)(?=^## |\Z)", changelog)
    assert any(
        re.search(r"Nano ESP32|nano-esp32s3_demo", notes, re.I)
        for release, notes in releases
        if release == "Unreleased" or tuple(map(int, release.split("."))) > (0, 29, 0)
    )

