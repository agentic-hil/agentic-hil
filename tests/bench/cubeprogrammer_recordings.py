"""Capture a real STM32CubeProgrammer flash through the product's MCP gate.

This module is selected only by the optional CubeProgrammer bench stage. It is
deliberately outside pytest's default ``test_*.py`` collection: the ordinary
bench tier uses OpenOCD and must not change backend as a side effect.

The test compiles the project's demo without touching the board, asks the
temporary bench configuration over MCP to use the fixed CubeProgrammer install,
then probes and flashes the demo through one MCP server and one declared run.
The product's own audit log is preserved in JUnit as a sanitized recording.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path

import pytest

from agentic_hil.report import overall_success

from .conftest import BENCH_ONLY, DEMO_IMAGE, Bench, built_where_it_stands
from .test_bench_faults import Server

pytestmark = [pytest.mark.bench, BENCH_ONLY]

RECORDING_SCHEMA = "agentic-hil.cubeprogrammer-recording/v1"
CUBEPROGRAMMER = Path("/opt/st/cubeprogrammer-2.23.0/bin/STM32_Programmer_CLI")
BOOT_BANNER = "Hello World"


def require_cubeprogrammer() -> Path:
    """Fail the explicit gate when its optional CLI was not installed."""
    assert CUBEPROGRAMMER.is_file(), f"the CubeProgrammer bench stage requires {CUBEPROGRAMMER}"
    assert os.access(CUBEPROGRAMMER, os.X_OK), f"the CubeProgrammer CLI is not executable: {CUBEPROGRAMMER}"
    return CUBEPROGRAMMER


def sanitize_transcript(text: str, private_values: Iterable[str]) -> str:
    """Redact known bench identity and temporary paths, preserving all other bytes."""
    values = sorted({value for value in private_values if value}, key=len, reverse=True)
    for value in values:
        text = re.sub(re.escape(value), "[redacted]", text, flags=re.IGNORECASE)
    return text


def configure_cubeprogrammer(server: Server, debugger_id: str, executable: Path) -> None:
    """Select CubeProgrammer under reset in this run's temporary description."""
    errored, changed = server.call(
        "project_config_set",
        {
            "changes": [
                {"key": f"debuggers.{debugger_id}.type", "value": "stlink"},
                {"key": f"debuggers.{debugger_id}.executable", "value": str(executable)},
                {"key": f"debuggers.{debugger_id}.connect_mode", "value": "under_reset"},
            ]
        },
    )
    assert not errored and changed.get("ok") is True, changed
    errored, reloaded = server.call("project_config_reload_description")
    assert not errored and reloaded.get("ok") is True, reloaded


def programmer_recording(
    bench: Bench,
    debugger_id: str,
    port_id: str,
    executable: Path,
    result: dict,
) -> str:
    """Read an available product audit log and return the sanitized JUnit value.

    Failures before the CLI starts have no action log. Those still get a JUnit
    record of the product result, clearly marked without a transcript.
    """
    configuration = bench.configuration()
    probe_serial = str(configuration["debuggers"][debugger_id].get("probe_id") or "")
    port_device = str(configuration["com_ports"][port_id].get("device") or "")
    private_values = (
        probe_serial,
        port_device,
        str(bench.project),
        bench.project.as_posix(),
        str(bench.config_root),
        bench.config_root.as_posix(),
        str(bench.state_root),
        bench.state_root.as_posix(),
        str(Path.home()),
        Path.home().as_posix(),
    )
    action_log = None
    log_path_value = result.get("log_path")
    if isinstance(log_path_value, str) and log_path_value:
        log_path = Path(log_path_value)
        if not log_path.is_absolute():
            log_path = bench.project / log_path
        resolved_log = log_path.resolve(strict=True)
        resolved_log.relative_to(bench.project.resolve())
        loaded = json.loads(resolved_log.read_text(encoding="utf-8"))
        assert isinstance(loaded.get("stdout"), str), loaded
        assert isinstance(loaded.get("stderr"), str), loaded
        action_log = {
            key: sanitize_transcript(value, private_values) if isinstance(value, str) else value
            for key, value in loaded.items()
        }
        assert action_log["stdout"].strip() or action_log["stderr"].strip(), "CubeProgrammer produced no transcript"

    source_commit = os.environ.get("AGENTIC_HIL_BENCH_COMMIT", "")
    run_id = os.environ.get("AGENTIC_HIL_BENCH_RUN_ID", "")
    if source_commit:
        assert re.fullmatch(r"[0-9a-f]{40}", source_commit), source_commit
    if run_id:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+", run_id), run_id
    programmer_output = result.get("programmer_output")
    if isinstance(programmer_output, dict):
        programmer_output = {
            key: sanitize_transcript(value, private_values) if isinstance(value, str) else value
            for key, value in programmer_output.items()
        }
    else:
        programmer_output = None
    return json.dumps(
        {
            "schema": RECORDING_SCHEMA,
            "source_commit": source_commit or None,
            "run_id": run_id or None,
            "backend": "stlink",
            "scenario": "demo-flash",
            "connect_mode": configuration["debuggers"][debugger_id].get("connect_mode"),
            "outcome": "success" if overall_success(result) else "failure",
            "cubeprogrammer_version": str(result.get("programmer_version") or "reported-by-debugger-info"),
            "executable": str(executable),
            "result_ok": result.get("ok"),
            "error_type": sanitize_transcript(str(result.get("error_type") or ""), private_values) or None,
            "backend_error_type": sanitize_transcript(str(result.get("backend_error_type") or ""), private_values) or None,
            "summary": sanitize_transcript(str(result.get("summary") or ""), private_values),
            "target_ok": result.get("target_ok"),
            "audit_ok": result.get("audit_ok"),
            "cleanup_ok": result.get("cleanup_ok"),
            "cleanup_required": result.get("cleanup_required"),
            "quarantined": result.get("quarantined"),
            "lease_state": result.get("lease_state"),
            "side_effect_status": result.get("side_effect_status"),
            "hardware_state": result.get("hardware_state"),
            "programmer_output": programmer_output,
            "programmer_log_available": action_log is not None,
            "command": action_log.get("command") if action_log is not None else None,
            "returncode": action_log.get("returncode") if action_log is not None else None,
            "stdout": action_log.get("stdout") if action_log is not None else None,
            "stderr": action_log.get("stderr") if action_log is not None else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def failure_continuation(result: dict, lease_status: dict) -> str:
    """Whether a failed hardware call may be retried under the HIL lease rules."""
    if overall_success(result):
        return "continue"
    if lease_status.get("auto_recoverable") is True and result.get("auto_recovery_attempted") is not True:
        return "retry_once"
    return "stop"


def test_cubeprogrammer_flashes_and_resets_demo_through_one_mcp_run(bench: Bench, tmp_path: Path, record_property) -> None:
    """Capture Cube's real connect/program/verify/reset transcript through MCP."""
    executable = require_cubeprogrammer()
    build_error = built_where_it_stands(bench.project)
    assert build_error is None, f"the demo ELF needed for the CubeProgrammer recording did not build:\n{build_error}"
    image = bench.project / DEMO_IMAGE
    assert image.is_file(), f"the demo build left no ELF at {image}"

    server = Server(bench, tmp_path / "cubeprogrammer-mcp.stderr")
    run_open = False
    flash_succeeded = False
    flash_result: dict | None = None
    try:
        server.greet()
        debugger_id = bench.debugger_name()
        ports = sorted(bench.configuration().get("com_ports") or {})
        assert ports, "the CubeProgrammer recording needs the probe's configured UART entry"
        port_id = ports[0]

        # This is the bench fixture's throwaway config, under its temp root.
        # Change only through MCP and reload the description in this same server.
        configure_cubeprogrammer(server, debugger_id, executable)

        errored, info = server.call("debugger_info")
        assert not errored and overall_success(info), info
        assert info.get("backend") == "stlink", info
        assert info.get("version"), info
        assert info.get("executable") == str(executable), info

        errored, opened = server.call(
            "bench_run_start",
            {
                "devices": [
                    {"kind": "debugger", "id": debugger_id},
                    {"kind": "uart", "id": port_id},
                ],
                "label": "cubeprogrammer-recording",
            },
        )
        assert not errored and overall_success(opened), opened
        run_open = True

        errored, probe = server.call("probe_target")
        assert not errored and overall_success(probe), probe
        assert probe.get("backend") == "stlink", probe

        errored, flash_result = server.call(
            "flash_firmware",
            {
                "image_path": image.relative_to(bench.project).as_posix(),
                "reset_after_flash": True,
                "capture": {"port_id": port_id, "until": BOOT_BANNER, "wait_timeout_s": 15.0},
            },
        )
        flash_result["programmer_version"] = info["version"]
        # Record the genuine product result and its audit log before the first
        # success assertion, so a real refusal remains evidence in the report.
        record_property(
            "cubeprogrammer_recording_v1",
            programmer_recording(bench, debugger_id, port_id, executable, flash_result),
        )
        flash_succeeded = not errored and overall_success(flash_result)
        assert flash_succeeded, flash_result
        assert flash_result.get("backend") == "stlink", flash_result
        assert flash_result.get("reset_after_flash") is True, flash_result
        assert isinstance(flash_result.get("log_path"), str) and flash_result["log_path"], flash_result
        capture = flash_result.get("capture")
        assert isinstance(capture, dict) and capture.get("until_matched") is True, capture
        assert capture.get("matched") == BOOT_BANNER, capture
    finally:
        try:
            if run_open and flash_succeeded:
                reset = server.try_call("reset_target", {"mode": "run"})
                assert isinstance(reset, dict) and overall_success(reset), reset
            elif run_open and flash_result is not None:
                # A failed action is evidence first. The current lease state
                # decides whether the product authorizes its one automatic
                # recovery retry; otherwise do not issue another target call.
                lease_status = server.try_call("hardware_lease_status")
                decision = failure_continuation(flash_result, lease_status or {})
                if decision == "retry_once":
                    retried = server.try_call(
                        "flash_firmware",
                        {
                            "image_path": image.relative_to(bench.project).as_posix(),
                            "reset_after_flash": True,
                            "capture": {"port_id": port_id, "until": BOOT_BANNER, "wait_timeout_s": 15.0},
                        },
                    )
                    if isinstance(retried, dict):
                        retried["programmer_version"] = info.get("version")
                        record_property(
                            "cubeprogrammer_recovery_recording_v1",
                            programmer_recording(bench, debugger_id, port_id, executable, retried),
                        )
                    if isinstance(retried, dict) and overall_success(retried):
                        reset = server.try_call("reset_target", {"mode": "run"})
                        assert isinstance(reset, dict) and overall_success(reset), reset
        finally:
            try:
                if run_open:
                    stopped = server.try_call("bench_run_stop")
                    assert isinstance(stopped, dict) and stopped.get("ok") is True, stopped
            finally:
                server.close()
