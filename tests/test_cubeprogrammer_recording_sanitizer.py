from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from tests.bench.cubeprogrammer_recordings import failure_continuation, programmer_recording, sanitize_transcript


def test_cubeprogrammer_transcript_sanitizer_removes_only_known_private_values() -> None:
    transcript = (
        "ST-LINK SN: 066EFF123456789012345678\n"
        "File path: /tmp/bench-identity/build/Debug/demo.elf\n"
        "Download verified successfully\n"
    )

    result = sanitize_transcript(
        transcript,
        (
            "066EFF123456789012345678",
            "/tmp/bench-identity",
            "",
        ),
    )

    assert "066EFF123456789012345678" not in result
    assert "/tmp/bench-identity" not in result
    assert "[redacted]/build/Debug/demo.elf" in result
    assert "Download verified successfully" in result


def test_cubeprogrammer_transcript_sanitizer_leaves_unmatched_capture_unchanged() -> None:
    transcript = "Download verified successfully\n"

    assert sanitize_transcript(transcript, ("not-present", "")) == transcript


def test_cubeprogrammer_recording_exports_a_sanitized_failed_product_log(tmp_path: Path) -> None:
    """Synthetic unit input checks export only; hardware evidence comes from MCP logs."""
    project = tmp_path / "workspace"
    project.mkdir()
    image = project / "build" / "Debug" / "demo.elf"
    image_for_cli = image.as_posix()
    log = project / "logs" / "stlink-flash.log"
    log.parent.mkdir()
    log.write_text(
        json.dumps(
            {
                "command": f'"/opt/st/cubeprogrammer-2.23.0/bin/STM32_Programmer_CLI" -w "{image_for_cli}" sn=SECRET-SERIAL',
                "returncode": 1,
                "timed_out": False,
                "stdout": f"failed to program SECRET-SERIAL at {image_for_cli}\n",
                "stderr": "",
            }
        ),
        encoding="utf-8",
    )
    configuration = {
        "debuggers": {"dut": {"probe_id": "SECRET-SERIAL"}},
        "com_ports": {"uart": {"device": "/dev/ttyACM0"}},
    }
    bench = SimpleNamespace(
        project=project,
        config_root=tmp_path / "config-private",
        state_root=tmp_path / "state-private",
        configuration=lambda: configuration,
    )

    recording = json.loads(
        programmer_recording(
            bench,
            "dut",
            "uart",
            Path("/opt/st/cubeprogrammer-2.23.0/bin/STM32_Programmer_CLI"),
            {
                "ok": False,
                "error_type": "flash_failed",
                "summary": "CubeProgrammer flash failed",
                "log_path": "logs/stlink-flash.log",
                "programmer_version": "STM32CubeProgrammer 2.23.0",
            },
        )
    )

    assert recording["result_ok"] is False
    assert recording["error_type"] == "flash_failed"
    assert recording["programmer_log_available"] is True
    assert "SECRET-SERIAL" not in json.dumps(recording)
    assert str(project) not in json.dumps(recording)
    assert "failed to program [redacted] at [redacted]/build/Debug/demo.elf" in recording["stdout"]


def test_cubeprogrammer_recording_keeps_a_product_refusal_without_a_log(tmp_path: Path) -> None:
    project = tmp_path / "workspace"
    project.mkdir()
    bench = SimpleNamespace(
        project=project,
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
        configuration=lambda: {"debuggers": {"dut": {}}, "com_ports": {"uart": {}}},
    )

    recording = json.loads(
        programmer_recording(
            bench,
            "dut",
            "uart",
            Path("/opt/st/cubeprogrammer-2.23.0/bin/STM32_Programmer_CLI"),
            {"ok": False, "error_type": "debugger_not_found", "summary": "CLI unavailable"},
        )
    )

    assert recording["result_ok"] is False
    assert recording["error_type"] == "debugger_not_found"
    assert recording["programmer_log_available"] is False
    assert recording["stdout"] is None
    assert recording["stderr"] is None


def test_failed_result_continues_only_for_unattempted_authorized_auto_recovery() -> None:
    good = {
        "ok": True,
        "target_ok": True,
        "audit_ok": True,
        "cleanup_ok": True,
        "cleanup_required": False,
        "quarantined": False,
        "lease_state": "active",
        "side_effect_status": "committed",
        "hardware_state": "changed",
    }
    assert failure_continuation(good, {}) == "continue"

    for field, value in (
        ("ok", False),
        ("target_ok", False),
        ("audit_ok", False),
        ("cleanup_ok", False),
        ("cleanup_required", True),
        ("quarantined", True),
        ("lease_state", "stale"),
        ("side_effect_status", "partial"),
        ("hardware_state", "unknown"),
    ):
        failed = {**good, field: value}
        assert failure_continuation(failed, {"auto_recoverable": False}) == "stop", (field, value)
        assert failure_continuation(failed, {"auto_recoverable": True}) == "retry_once", (field, value)

    already_recovered = {**good, "ok": False, "auto_recovery_attempted": True}
    assert failure_continuation(already_recovered, {"auto_recoverable": True}) == "stop"
