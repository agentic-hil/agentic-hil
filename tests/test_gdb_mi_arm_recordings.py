"""Offline checks for the Nucleo-F446RE Arm GDB stop recordings."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from conftest import FAKE_GDB, write_config
from fixtures import fake_gdb

from agentic_hil.backends.gdbdebug import GdbDebugSession
from agentic_hil.backends.openocd import OpenOCDBackend
from agentic_hil.config import load_config
from agentic_hil.gdbmi import stop_result_from_line
from agentic_hil.test_reactor import load_test_config
from tests.bench.test_bench_faults import sanitize_gdb_mi_source_paths

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
ARM = FIXTURES / "gdb_mi_stop_recordings_arm_none_eabi.json"
DEBIAN = FIXTURES / "gdb_mi_stop_recordings.json"
FIXTURE_SHA256 = "c7c47ed143c76fb155469f658f5830b3fe94c999a7edf1bce95c8b124e57b859"
FIRMWARE = "tests/bench/firmware/undefined_instruction.c"
IMAGE = "build/hil/f446re_undefined_instruction.elf"
ATTACH_JOB = "job-07ee9aecf1ff95e7"
RESET_JOB = "job-c2a4b07d17c0e862"
REDACTION = "absolute source path replaced with <path redacted>; relative source names retained"

SOURCE_NAME = r"(?:<path redacted>|[A-Za-z_][A-Za-z0-9_-]*\.(?:c|cc|cpp|cxx|h|hh|hpp|hxx|[sS]))"
DIGEST = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", re.I)
DIGEST_PATHS = {
    ("provenance", "bench_config_sha256"),
    ("provenance", "firmware_elf_sha256"),
    ("provenance", "gdb_executable_sha256"),
    ("provenance", "evidence_artifacts", "attach_hardfault", "evidence.zip_sha256"),
    ("provenance", "evidence_artifacts", "attach_hardfault", "gdb_session_log_sha256"),
    ("provenance", "evidence_artifacts", "reset_halt_to_hardfault", "evidence.zip_sha256"),
    ("provenance", "evidence_artifacts", "reset_halt_to_hardfault", "gdb_session_log_sha256"),
}
IDENTIFYING_TEXT = re.compile(
    r"(?<![\w.])(?:[A-Z]:[\\/]|[\\/]{2}|/)"
    r"|(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"
    r"|(?<![\w.-])(?:[a-z0-9-]+\.)+[a-z]{2,}(?![\w.-])"
    r"|\b[0-9a-f]{24}\b|@|\b(?:user(?:name)?|host(?:name)?|(?:probe[ _-])?serial)\s*[:=]",
    re.I,
)


def _stop_pattern(prefix: str, function: str, line: int) -> re.Pattern[str]:
    # The capture has three frames. Keep their function names, line numbers and
    # fields closed while allowing a firmware address and redacted source names.
    return re.compile(
        rf'\*stopped,{prefix}frame=\{{addr="0x[0-9a-f]{{8}}",func="{function}",args=\[\],'
        rf'file="{SOURCE_NAME}",fullname="{SOURCE_NAME}",line="{line}",arch="armv7e-m"\}},'
        r'thread-id="1",stopped-threads="all"'
    )


STOP_FORMS = (
    _stop_pattern("", "HardFault_Handler", 87),
    _stop_pattern("", "Reset_Handler", 56),
    _stop_pattern('reason="breakpoint-hit",disp="keep",bkptno="1",', "main", 93),
    _stop_pattern('reason="signal-received",signal-name="SIGINT",signal-meaning="Interrupt",', "HardFault_Handler", 87),
)


def _redact_stop(record: str) -> str:
    sanitized = sanitize_gdb_mi_source_paths(record)
    if not any(form.fullmatch(sanitized) for form in STOP_FORMS):
        raise ValueError("Unrecognized F446RE GDB/MI stop record")
    return sanitized


def _read_recordings(path: Path) -> dict:
    return json.loads(path.read_bytes())


def _fixture_strings(value: object, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield (*path, "<key>"), key
            yield from _fixture_strings(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _fixture_strings(item, (*path, str(index)))


def test_recording_bytes_and_closed_provenance_match_source() -> None:
    raw = ARM.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA256
    assert b"\r" not in raw and not raw.endswith(b"\n")
    fixture = json.loads(raw)
    assert set(fixture) == {"schema", "provenance", "recordings"}
    assert fixture["schema"] == "agentic-hil.gdb-mi-stop-recording/v1"
    assert fixture["provenance"] == {
        "source_commit": "9f2769017ebc3525317dee008b4944834e307331",
        "recorded_on": "2026-11-25",
        "run_id": f"{ATTACH_JOB}, {RESET_JOB}",
        "workflow_url": None,
        "workflow_url_note": "no workflow ran; the two serve job ids identify the runs, one per scenario",
        "recorded_by": (
            "two test plans run as agentic-hil serve jobs, not the bench stage of "
            "tests/bench/test_bench_faults.py; its path sanitizer is the redaction"
        ),
        "agentic_hil_version": "0.28.0",
        "gdb_version": "GNU gdb (xPack GNU Arm Embedded GCC x86_64) 14.2.90.20240526-git",
        "gdb_executable": "arm-none-eabi-gdb",
        "gdb_executable_sha256": "98dcb123acc98fa50917f661e28cdef00094508b809081abfed81bf36a26d700",
        "gdb_identity": (
            "the session logs name no GDB: each job report carries the bench config digest below, "
            "that config names this arm-none-eabi-gdb as debug.gdb_executable, and gdb_version is "
            "the first line of its --version on the bench on the recording day"
        ),
        "bench_config_sha256": "199f03151137349b04077e47cb1b13592004b85480fd1c777b2280e80c427bdf",
        "backend": "openocd",
        "backend_version": "Open On-Chip Debugger 0.12.0",
        "board": "ST Nucleo-F446RE (STM32F446RE)",
        "firmware_image": "undefined_instruction",
        "firmware_source": FIRMWARE,
        "firmware_elf_sha256": "90c8d27a1a1fa363b09894c079bd622d1fae7b3d25e4dbb721036249a134133b",
        "firmware_toolchain": "arm-none-eabi-gcc (GNU Tools for STM32 14.3.rel1.20251027-0700) 14.3.1 20250623",
        "evidence_artifacts": {
            "attach_hardfault": {
                "evidence.zip_sha256": "cfc368b023ca06d4e85529e5a8409cad3325ba76f0afa5a85076be7e9e600d92",
                "gdb_session_log_sha256": "bfb414b03b069e4f7fb6f35cc5c5b9ebbcef79d087cf1f471d964c5bbc2f2f8d",
            },
            "reset_halt_to_hardfault": {
                "evidence.zip_sha256": "42460f10db4fb129e56621e2ebce682f49b4b79d73787fcca7328d6d32fd1f5b",
                "gdb_session_log_sha256": "c42e5c847ced8dc783ac89e4ee7418e50cd06f59baaf0faa56ede41528831387",
            },
        },
        "redacted_fields": {"frame.file": REDACTION, "frame.fullname": REDACTION},
    }
    assert [(case["scenario"], case["job_id"], len(case["records"])) for case in fixture["recordings"]] == [
        ("attach_hardfault", ATTACH_JOB, 1),
        ("reset_halt_to_hardfault", RESET_JOB, 3),
    ]
    assert all(set(case) == {"scenario", "job_id", "records", "expected_stop_reasons"} for case in fixture["recordings"])


def test_redaction_preserves_recordings_and_redacts_absolute_source_paths() -> None:
    cases = _read_recordings(ARM)["recordings"]
    for case in cases:
        for record in case["records"]:
            assert _redact_stop(record) == record

    attach = cases[0]["records"][0]
    absolute = attach.replace('file="<path redacted>"', 'file="/fixture/build/main.c"').replace(
        'fullname="<path redacted>"', r'fullname="C:\\fixture\\build\\main.c"'
    )
    assert _redact_stop(absolute) == attach
    relative = attach.replace("<path redacted>", "main.c")
    assert _redact_stop(relative) == relative


def test_redaction_rejects_disallowed_fields_values_order_escapes_and_trailing_text() -> None:
    attach = _read_recordings(ARM)["recordings"][0]["records"][0]
    invalid = {
        "probe serial": attach.replace("args=[]", 'args=[{value="0123456789ABCDEF01234567"}]'),
        "hostname": attach.replace('func="HardFault_Handler"', 'func="bench.example"'),
        "IP address": attach.replace('addr="0x08000416"', 'addr="192.0.2.15"'),
        "username": attach.replace('arch="armv7e-m"', 'arch="fixture-user"'),
        "absolute path outside source fields": attach.replace('func="HardFault_Handler"', 'func="/fixture/main.c"'),
        "relative path with directory": attach.replace('file="<path redacted>"', 'file="Src/main.c"'),
        "unknown field": attach.replace('thread-id="1"', 'thread-id="1",core="0"'),
        "duplicate field": attach.replace('thread-id="1"', 'thread-id="1",thread-id="1"'),
        "reordered fields": attach.replace('thread-id="1",stopped-threads="all"', 'stopped-threads="all",thread-id="1"'),
        "escape sequence": attach.replace('file="<path redacted>"', r'file="ma\151n.c"'),
        "trailing text": attach + " trailing",
        "trailing newline": attach + "\n",
    }
    for label, record in invalid.items():
        assert record != attach, label
        with pytest.raises(ValueError, match="Unrecognized F446RE GDB/MI stop record"):
            _redact_stop(record)


def test_fixture_strings_are_redacted_and_digests_are_scoped() -> None:
    digest_paths = set()
    for path, value in _fixture_strings(_read_recordings(ARM)):
        # These two field names look like hostnames to the scanner.
        if path != ("provenance", "redacted_fields", "<key>") or value not in {"frame.file", "frame.fullname"}:
            assert IDENTIFYING_TEXT.search(value) is None, ".".join(path)
        if path in DIGEST_PATHS:
            assert DIGEST.fullmatch(value), ".".join(path)
            digest_paths.add(path)
        else:
            assert DIGEST.search(value) is None, ".".join(path)
    assert digest_paths == DIGEST_PATHS


def test_both_scenarios_replay_with_recorded_stop_reasons(tmp_path: Path) -> None:
    backend = OpenOCDBackend(load_config(str(write_config(tmp_path, gdb_executable=FAKE_GDB))))
    captured = _read_recordings(ARM)["recordings"]

    attach = GdbDebugSession("attach", {}, "attach", 0, SimpleNamespace(poll=lambda: None), [], "attach.json")
    [line] = captured[0]["records"]
    attach_stop = backend._debug._stop_reason_from_gdb(attach, stop_result_from_line(line))
    assert attach_stop["backend_stop_reason"] == "unknown"
    assert [f'{attach_stop["stop_reason"]}:{attach_stop["exception_type"]}'] == captured[0]["expected_stop_reasons"] == [
        "exception:hardfault",
    ]

    reset = GdbDebugSession("reset", {}, "reset_halt", 0, SimpleNamespace(poll=lambda: None), [], "reset.json")
    reset.breakpoints = [{"backend_id": "1", "id": "bp-main"}]
    reset_stop, main_stop, fault_stop = [
        backend._debug._stop_reason_from_gdb(reset, stop_result_from_line(line))
        for line in captured[1]["records"]
    ]
    assert main_stop["breakpoint_expected"] is True
    assert fault_stop["backend_stop_reason"] == "signal-received"
    assert fault_stop["signal"] == {"name": "SIGINT", "meaning": "Interrupt"}
    assert [
        reset_stop["stop_reason"],
        f'{main_stop["stop_reason"]}:expected',
        f'{fault_stop["stop_reason"]}:{fault_stop["exception_type"]}',
    ] == captured[1]["expected_stop_reasons"] == ["reset", "breakpoint_hit:expected", "exception:hardfault"]


def test_arm_and_debian_stops_match_except_for_frame_addr() -> None:
    arm_cases = _read_recordings(ARM)["recordings"]
    debian_cases = _read_recordings(DEBIAN)["recordings"]
    address = re.compile(r'(?<=frame=\{addr=")0x[0-9a-f]{8}(?=")')
    for arm, debian in zip(arm_cases, debian_cases, strict=True):
        assert arm["scenario"] == debian["scenario"]
        for arm_line, debian_line in zip(arm["records"], debian["records"], strict=True):
            arm_masked, arm_count = address.subn("<address>", arm_line)
            debian_masked, debian_count = address.subn("<address>", debian_line)
            assert arm_count == debian_count == 1
            assert arm_line != debian_line
            assert arm_masked == debian_masked


def test_typed_plans_match_required_steps_firmware_boot_text_and_headers() -> None:
    firmware = (ROOT / FIRMWARE).read_text(encoding="utf-8")
    boot_text = "undefined instruction image booted"
    assert rf'usart2_send("{boot_text}\n");' in firmware

    debugger = {"debugger": "f446re"}
    uart = {"port_id": "f446re_vcp"}
    flash = {"action": "flash", **debugger, "image_path": IMAGE, "reset_after_flash": False}
    stop = {"action": "debug_stop", **debugger}
    boot = [
        {"action": "uart_open", **uart, "clear_buffer": True},
        {"action": "reset", **debugger, "mode": "run"},
    ]
    close_uart = {"action": "uart_close", **uart}
    reset_halt = {"action": "debug_start", **debugger, "image_path": IMAGE, "mode": "reset_halt"}
    wait_at_main = {"action": "run_until_breakpoint", **debugger, "location": "main", "timeout_s": 5}

    plans = {
        "f446re_attach_hardfault_recording.yaml": [
            flash, *boot,
            {"action": "uart_expect", **uart, "text": boot_text, "timeout_s": 5},
            close_uart,
            {"action": "debug_start", **debugger, "image_path": IMAGE, "mode": "attach"},
            stop,
        ],
        "f446re_reset_halt_to_hardfault_recording.yaml": [
            flash, reset_halt, wait_at_main,
            {"action": "run_until_breakpoint", **debugger, "location": "SystemInit", "timeout_s": 5},
            stop,
        ],
        "f446re_fault_stop_companion.yaml": [
            flash, *boot,
            {"action": "uart_expect", **uart, "text": boot_text + "\n", "timeout_s": 5},
            close_uart, reset_halt, wait_at_main,
            {"action": "run_until_breakpoint", **debugger, "location": "HardFault_Handler", "timeout_s": 5},
            stop,
        ],
    }
    recording_notes = {
        "f446re_attach_hardfault_recording.yaml": (
            "red by design", "step 6", "target_exception", "evidence bundle", "reasonless", "HardFault_Handler",
        ),
        "f446re_reset_halt_to_hardfault_recording.yaml": (
            "red by design", "step 4", "timeout", "SystemInit", "evidence bundle", "Reset_Handler",
            "main", "SIGINT", "HardFault_Handler",
        ),
    }
    for plan_name, expected_steps in plans.items():
        path = ROOT / "tests/hil" / plan_name
        text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
        assert set(raw) == {"version", "name", "steps"}
        assert type(raw["version"]) is int and raw["version"] == 5

        plan = load_test_config(str(path), str(ROOT))
        assert [
            {"action": step.action, **{key: getattr(step, key) for key in step.route_keys}, **step.arguments}
            for step in plan.steps
        ] == expected_steps, plan_name

        header = " ".join(line.lstrip("# ").strip() for line in text.split("version:", 1)[0].splitlines())
        assert re.search(r"\bcopy\b.*examples/nucleo-f446re_demo\b.*\bwithout\b.*\bbuild/", header, re.I)
        assert re.search(r"\breplace\b.*Src/main\.c\b.*" + re.escape(FIRMWARE), header, re.I)
        assert re.search(r"cmake --preset Debug --fresh\b.*cmake --build --preset Debug\b", header)
        assert re.search(r"\bcopy\b.*build/Debug/nucleo-f446re_demo\.elf\b.*" + re.escape(IMAGE), header, re.I)
        for phrase in recording_notes.get(plan_name, ()):
            assert phrase in header, (plan_name, phrase)


def test_fake_documents_arm_recordings_and_uses_debian_recordings() -> None:
    source = (FIXTURES / "fake_gdb.py").read_text(encoding="utf-8")
    provenance = source.split("# The two HardFault stops are recordings", 1)[1].split("\nGDB_MI_STOP_RECORDING", 1)[0]
    assert "No fault recorded" not in provenance
    assert ARM.name in provenance and "arm-none-eabi-gdb" in provenance
    assert "This fake answers from the Debian recording" in provenance.replace("\n# ", " ")
    assert fake_gdb.GDB_MI_STOP_RECORDING == DEBIAN


def test_changelog_entry_documents_arm_recordings() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    releases = re.findall(
        r"^##[ \t]+(?:\[Unreleased\]|\[\d+\.\d+\.\d+\] - \d{4}-\d{2}-\d{2})[ \t]*\n(.*?)(?=^##[ \t]|\Z)",
        changelog, re.M | re.S,
    )
    entries = (
        " ".join(entry.replace("`", "").split())
        for release in releases
        for section in re.findall(r"^###[ \t]+[^\n]+\n(.*?)(?=^###[ \t]|\Z)", release, re.M | re.S)
        for entry in re.findall(r"(?m)^[-*+][ \t]+[^\n]*(?:\n[ \t]+[^\n]*)*", section)
        if ATTACH_JOB in entry and RESET_JOB in entry
    )
    requirements = (
        r"\bHardFault\b", r"\bNucleo-F446RE\b", r"\bshared bench\b", r"\barm-none-eabi-gdb\b",
        r"\bserve\b[^.!?]*\bplans?\b[^.!?]*\bbench stage\b|\bbench stage\b[^.!?]*\bserve\b[^.!?]*\bplans?\b",
        r"\b(?:in place of|instead of|replac\w*|no)\b[^.!?]{0,55}\bworkflow URL\b",
        r"tests/test_gdb_mi_arm_recordings\.py", r"tests/test_gdb_mi_recordings\.py",
        r"\b(?:another|a separate|a concurrent) change\b",
        r"\bconfig(?:uration)?(?:'s)?\b.{0,35}\b(?:digest|SHA-256)\b",
        r"--version", r"\b(?:recording day|recording-day|day of (?:the )?recording)\b",
        r"\b(?:session logs?|sessions?)\b.{0,55}\b(?:omit\w*|do not|don't|name no|carry no|lack\w*)\b",
        r"\b(?:red by design|fail\w* intentionally|intentional(?:ly)? fail\w*|designed to fail)\b",
        r"\bcompanion\b", r"\bplan check\b|\bcheck\w* (?:both|the two) recording plans\b",
        r"\b(?:without|does not|doesn't|no)\b[^.]{0,60}\b(?:re-?record\w*|recaptur\w*)\b",
    )
    if not any(all(re.search(pattern, entry, re.I) for pattern in requirements) for entry in entries):
        raise AssertionError(
            "CHANGELOG.md needs one entry in Unreleased or a dated release that names both job IDs "
            "and describes the Arm HardFault recordings, capture changes and evidence limits."
        )


def test_manifest_excludes_checkout_only_arm_recording_tests() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    if f"exclude tests/{Path(__file__).name}" not in {line.strip() for line in manifest.splitlines()}:
        raise AssertionError("MANIFEST.in must exclude tests/test_gdb_mi_arm_recordings.py.")
