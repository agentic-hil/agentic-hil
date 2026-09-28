"""Opt-in recording of a healthy pyOCD run against the configured Nucleo.

This module is selected explicitly by the pyOCD bench gate. The ordinary bench
uses OpenOCD, so this module owns a private config variant with the pyOCD backend
and the F446RE CMSIS-pack target selected before its one MCP server starts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from agentic_hil.report import overall_success
from tests.support import scaled_time_bound

from . import usb_reset_support as usb
from .conftest import BENCH_ONLY, DEMO_IMAGE, Bench, built_where_it_stands
from .test_bench_faults import Server
from .usb_reset_reenumeration import USB_SYSFS, USBFS_ROOT, VISIBILITY_TIMEOUT_S

pytestmark = [pytest.mark.bench, BENCH_ONLY]

BOOT_BANNER = "Hello World"
PACK_ID = "Keil.STM32F4xx_DFP"
TARGET_TYPE = "stm32f446retx"
RECORDING_SCHEMA = "agentic-hil.pyocd-recording/v1"


def pyocd_bench_environment(bench: Bench, **overrides: str) -> dict[str, str]:
    """Keep pyOCD's pack cache visible while retaining the bench's config/state isolation."""
    environment = bench.environment(**overrides)
    environment["XDG_DATA_HOME"] = str(Path(environment["HOME"]) / ".local" / "share")
    return environment


class PyOcdBench:
    """Use the image's CMSIS pack data root only for this opt-in test's commands."""

    def __init__(self, bench: Bench) -> None:
        self._bench = bench

    def __getattr__(self, name: str):
        return getattr(self._bench, name)

    def environment(self, **overrides: str) -> dict[str, str]:
        return pyocd_bench_environment(self._bench, **overrides)


def pyocd_provenance(environment: dict[str, str]) -> tuple[str, str, str]:
    """Return installed CLI version and the actual F446 target/pack metadata."""
    executable = shutil.which("pyocd")
    assert executable, "the explicit pyOCD bench stage requires the installed pyocd executable"
    targets_run = subprocess.run(
        [executable, "json", "--targets", "--no-config"],
        capture_output=True,
        text=True,
        timeout=scaled_time_bound(60),
        check=False,
        env=environment,
    )
    packs_run = subprocess.run(
        [executable, "pack", "show"],
        capture_output=True,
        text=True,
        timeout=scaled_time_bound(60),
        check=False,
        env=environment,
    )
    assert targets_run.returncode == 0, (targets_run.stdout, targets_run.stderr)
    assert packs_run.returncode == 0, (packs_run.stdout, packs_run.stderr)
    targets_document = json.loads(targets_run.stdout)
    assert targets_document.get("pyocd_version") == "0.45.1", targets_document
    targets = {
        item.get("name"): item
        for item in targets_document.get("targets", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    target = targets.get(TARGET_TYPE)
    assert isinstance(target, dict), f"pyOCD does not list {TARGET_TYPE}: {targets_run.stdout}"
    assert target.get("part_number") == "STM32F446RETx", target
    assert target.get("source") == "pack", target
    versions = [
        fields[1]
        for line in packs_run.stdout.splitlines()
        if len(fields := line.split()) == 2 and fields[0] == PACK_ID
    ]
    assert len(versions) == 1, packs_run.stdout
    assert versions[0] == "3.1.1", packs_run.stdout
    return executable, targets_document["pyocd_version"], versions[0]


def redact(value: str, private_values: tuple[str, ...]) -> str:
    for private in sorted((item for item in private_values if item), key=len, reverse=True):
        value = re.sub(re.escape(private), "[redacted]", value, flags=re.IGNORECASE)
    return value


def redact_values(value: object, private_values: tuple[str, ...]) -> object:
    if isinstance(value, str):
        return redact(value, private_values)
    if isinstance(value, list):
        return [redact_values(item, private_values) for item in value]
    if isinstance(value, dict):
        return {key: redact_values(item, private_values) for key, item in value.items()}
    return value


def transcript(bench: Bench, result: dict, private_values: tuple[str, ...]) -> dict:
    """Read the product's actual action log, requiring it to remain in owned roots."""
    log_path_value = result.get("log_path")
    action_log = None
    if isinstance(log_path_value, str) and log_path_value:
        log_path = Path(log_path_value)
        if not log_path.is_absolute():
            log_path = bench.project / log_path
        resolved = log_path.resolve(strict=True)
        try:
            resolved.relative_to(bench.project.resolve())
        except ValueError:
            resolved.relative_to(bench.state_root.resolve())
        loaded = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("the product action log must contain a JSON object")
        action_log = redact_values(loaded, private_values)
    output = result.get("programmer_output")
    if isinstance(output, dict):
        output = {key: redact(value, private_values) if isinstance(value, str) else value for key, value in output.items()}
    else:
        output = None
    return {"action_log": action_log, "programmer_output": output}


def product_diagnostics(result: dict) -> dict:
    return {
        key: result.get(key)
        for key in (
            "ok",
            "error_type",
            "backend_error_type",
            "target_ok",
            "target_contacted",
            "retry_safe",
            "audit_ok",
            "cleanup_ok",
            "cleanup_required",
            "quarantined",
            "lease_state",
            "side_effect_status",
            "hardware_state",
            "verify",
            "reset_after_flash",
        )
    }


LEASE_STATUS_EVIDENCE_FIELDS = (
    "ok",
    "error",
    "error_type",
    "summary",
    "audit_ok",
    "cleanup_ok",
    "cleanup_required",
    "quarantined",
    "lease_state",
    "blocked",
    "incident_stands",
    "standing_incidents",
    "cleanup_reasons",
    "quarantine_guidance",
    "auto_recoverable",
    "auto_recover_policy",
    "quarantine_id",
)
RUN_STOP_EVIDENCE_FIELDS = (
    "ok",
    "error",
    "error_type",
    "summary",
    "target_ok",
    "audit_ok",
    "cleanup_ok",
    "cleanup_required",
    "quarantined",
    "lease_state",
    "side_effect_status",
    "hardware_state",
    "released_devices",
)
RUN_STOP_RECOVERY_EVIDENCE_FIELDS = (
    "ok",
    "failed_check",
    "target_ok",
    "audit_ok",
    "cleanup_ok",
    "cleanup_required",
    "quarantined",
    "lease_state",
    "side_effect_status",
    "hardware_state",
    "attempted",
    "actions",
    "outcome",
    "auto_recover_policy",
    "auto_recover_policy_source",
    "failed_action",
    "incident_resolved",
    "incident_open",
    "incident_summary",
    "resolved_reason",
    "resolved_quarantine_id",
    "quarantine_id",
    "safe_state_predicate",
    "reason_not_attempted",
    "cleanup_reasons",
    "summary",
)


def lease_status_evidence(result: object, private_values: tuple[str, ...]) -> dict:
    """Keep a small, redacted lease-status snapshot; failed reads stay unavailable."""
    if not isinstance(result, dict):
        return {"available": False, "unavailable_reason": "status_result_missing"}
    evidence = {key: result[key] for key in LEASE_STATUS_EVIDENCE_FIELDS if key in result}
    available = result.get("ok") is True
    evidence["available"] = available
    if not available:
        evidence["unavailable_reason"] = "status_call_failed"
    return redact_values(evidence, private_values)


def run_stop_evidence(result: object, private_values: tuple[str, ...]) -> dict:
    """Preserve the stop result and its recovery verdict without implying success."""
    if isinstance(result, Exception):
        return {
            "available": False,
            "unavailable_reason": "run_stop_call_failed",
            "error": redact(f"{type(result).__name__}: {result}", private_values),
            "recovery": None,
        }
    if not isinstance(result, dict):
        return {"available": False, "unavailable_reason": "run_stop_result_missing", "recovery": None}
    evidence = {key: result[key] for key in RUN_STOP_EVIDENCE_FIELDS if key in result}
    recovery = result.get("recovery")
    if isinstance(recovery, dict):
        evidence["recovery"] = {
            key: recovery[key] for key in RUN_STOP_RECOVERY_EVIDENCE_FIELDS if key in recovery
        }
    else:
        evidence["recovery"] = None
    evidence["available"] = True
    return redact_values(evidence, private_values)


def run_stop_succeeded(result: object) -> bool:
    """A closed run with an unresolved recovery block is not a clean teardown."""
    if not isinstance(result, dict) or not overall_success(result):
        return False
    recovery = result.get("recovery")
    if recovery is None:
        return True
    return isinstance(recovery, dict) and recovery.get("outcome") == "recovered" and recovery.get("incident_open") is False


def capture_run_stop_with_lease_evidence(server: Server, private_values: tuple[str, ...]) -> dict:
    """Read status on either side of the single normal run-stop call."""
    try:
        before_result = server.try_call("hardware_lease_status")
    except Exception:
        before_result = None
    before = lease_status_evidence(before_result, private_values)
    stop_result = None
    try:
        stop_result = server.try_call("bench_run_stop")
    except Exception as error:
        stop_result = error
    finally:
        try:
            after_result = server.try_call("hardware_lease_status")
        except Exception:
            after_result = None
    return {
        "lease_status_before_stop": before,
        "run_stop": run_stop_evidence(stop_result, private_values),
        "lease_status_after_stop": lease_status_evidence(after_result, private_values),
    }


def safe_initial_usb_timeout(result: dict) -> bool:
    """Accept only a complete, retry-safe pre-target ST-Link timeout result."""
    if result.get("target_ok") is False or result.get("cleanup_ok") is False:
        return False
    if any(
        result.get(key) != expected
        for key, expected in (
            ("ok", False),
            ("tool", "debugger_probes_list"),
            ("backend", "pyocd"),
            ("error_type", "probe_discovery_failed"),
            ("target_contacted", False),
            ("retry_safe", True),
            ("audit_ok", True),
            ("cleanup_required", False),
            ("quarantined", False),
            ("side_effect_status", "not_started"),
            ("hardware_state", "unchanged"),
        )
    ):
        return False
    if result.get("lease_state") not in {"active", "released"}:
        return False
    output = result.get("programmer_output")
    stdout = output.get("stdout") if isinstance(output, dict) else None
    returncode = output.get("returncode") if isinstance(output, dict) else None
    if not isinstance(returncode, int) or isinstance(returncode, bool) or returncode == 0:
        return False
    if not isinstance(stdout, str) or not stdout.strip():
        return False
    try:
        document = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(document, dict):
        return False
    error = document.get("error")
    return (
        isinstance(document.get("status"), int)
        and not isinstance(document.get("status"), bool)
        and document["status"] != 0
        and isinstance(error, str)
        and "USBTimeoutError" in error
        and "Errno 110" in error
        and "Operation timed out" in error
    )


def pyocd_discovery_after_usb_reset(
    server: Server,
    bench: Bench,
    *,
    expected_serial: str,
    usb_identity: usb.USBDeviceIdentity,
    private_values: tuple[str, ...],
    record_property,
) -> dict:
    """Record same-process pyOCD discovery on both sides of one verified USB reset."""
    initial_pid = server.pid
    _, initial_result = server.call("debugger_probes_list")
    initial_evidence = pyocd_result_evidence(bench, initial_result, private_values)
    record_property(
        "pyocd_discovery_before_usb_reset_v1",
        json.dumps(initial_evidence, sort_keys=True, separators=(",", ":")),
    )
    if initial_result.get("ok") is not True and not safe_initial_usb_timeout(initial_result):
        require_success(initial_result, "initial debugger_probes_list", initial_evidence)
        pytest.fail("initial pyOCD discovery failed without a complete safe USB timeout result", pytrace=False)
    if initial_result.get("ok") is True:
        require_success(initial_result, "initial debugger_probes_list", initial_evidence)

    if server.pid != initial_pid or server.process.poll() is not None:
        pytest.fail("the pyOCD MCP process exited before the USB reset diagnostic", pytrace=False)
    usb.reset_usb_device(usb_identity)
    usb.wait_for_usb_device(
        sysfs_root=USB_SYSFS,
        device_root=USBFS_ROOT,
        expected_serial=expected_serial,
        expected_vid=usb_identity.vid,
        expected_pid=usb_identity.pid,
        timeout_s=VISIBILITY_TIMEOUT_S,
    )
    if server.pid != initial_pid or server.process.poll() is not None:
        pytest.fail("the same pyOCD MCP process did not remain live after the USB reset", pytrace=False)

    _, after_result = server.call("debugger_probes_list")
    after_evidence = pyocd_result_evidence(bench, after_result, private_values)
    record_property(
        "pyocd_discovery_after_usb_reset_v1",
        json.dumps(after_evidence, sort_keys=True, separators=(",", ":")),
    )
    require_success(after_result, "debugger_probes_list after USB reset", after_evidence)
    after_ids = {
        str(item.get("probe_id") or "").casefold()
        for item in after_result.get("probes", [])
        if isinstance(item, dict)
    }
    if expected_serial.casefold() not in after_ids:
        pytest.fail("pyOCD did not rediscover the configured probe after USB reset", pytrace=False)
    return {
        "initial": initial_evidence,
        "after": after_evidence,
        "mcp_process_same": True,
        "usb_identity_reappeared": True,
    }


def pyocd_result_evidence(bench: Bench, result: dict, private_values: tuple[str, ...]) -> dict:
    """Preserve a pyOCD result and any real subprocess transcript safely."""
    diagnostics = product_diagnostics(result)
    diagnostics["summary"] = redact(str(result.get("summary") or ""), private_values)
    evidence = {"result": diagnostics}
    try:
        evidence.update(transcript(bench, result, private_values))
    except (OSError, ValueError, TypeError) as exc:
        # A missing or malformed action log must not hide the backend result.
        evidence["transcript_error"] = redact(f"{type(exc).__name__}: {exc}", private_values)
        output = result.get("programmer_output")
        evidence["programmer_output"] = redact_values(output, private_values)
        evidence["action_log"] = None
    return evidence


def require_success(result: dict, action: str, evidence: dict | None = None) -> None:
    if not overall_success(result):
        diagnostics = evidence if evidence is not None else product_diagnostics(result)
        pytest.fail(f"Agentic HIL {action} failed its continue predicate: {diagnostics}", pytrace=False)


def read_boot_banner(server: Server, port_id: str, timeout_s: float = 15.0) -> str:
    deadline = time.monotonic() + timeout_s
    fragments: list[str] = []
    collected = ""
    while BOOT_BANNER not in collected:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail("the demo UART did not produce the complete boot banner in the bounded wait", pytrace=False)
        _, result = server.call("com_read", {"port_id": port_id, "wait_timeout_s": min(0.5, remaining)})
        require_success(result, "com_read after reset")
        data = result.get("data")
        if not isinstance(data, dict):
            pytest.fail("Agentic HIL returned no UART data document", pytrace=False)
        if isinstance(data.get("hex"), str):
            try:
                fragment = bytes.fromhex(data["hex"]).decode("utf-8", errors="replace")
            except ValueError:
                pytest.fail("Agentic HIL returned malformed UART bytes", pytrace=False)
        else:
            fragment = str(data.get("text") or "")
        fragments.append(fragment)
        collected = "".join(fragments)
    return collected


def test_pyocd_discovery_recovers_after_verified_usb_reset(bench: Bench, tmp_path: Path, record_property) -> None:
    """Prove same-server pyOCD listing after a targeted usbfs reset, without target actions."""
    build_error = built_where_it_stands(bench.project)
    if build_error is not None:
        pytest.fail("the bench image build prerequisite failed", pytrace=False)
    executable, pyocd_version, pack_version = pyocd_provenance(pyocd_bench_environment(bench))
    source_commit = os.environ.get("AGENTIC_HIL_BENCH_COMMIT")
    run_id = os.environ.get("AGENTIC_HIL_BENCH_RUN_ID")
    if source_commit is not None:
        assert re.fullmatch(r"[0-9a-f]{40}", source_commit), source_commit
    if run_id is not None:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+", run_id), run_id
    record_property(
        "pyocd_discovery_reset_recording_v1",
        json.dumps(
            {
                "schema": RECORDING_SCHEMA,
                "source_commit": source_commit,
                "run_id": run_id,
                "backend": "pyocd",
                "scenario": "probe-discovery-after-verified-usb-reset",
                "executable": executable,
                "pyocd_version": pyocd_version,
                "cmsis_pack": {"id": PACK_ID, "version": pack_version, "target_type": TARGET_TYPE},
                "target_action_requested": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    debugger_id = bench.debugger_name()
    ports = sorted(bench.configuration().get("com_ports") or {})
    if not ports:
        pytest.fail("the pyOCD USB reset diagnostic needs the probe's configured UART entry", pytrace=False)
    port_id = ports[0]
    variant = bench.config_root / "bench-pyocd-discovery-reset.yaml"
    if variant.exists():
        pytest.fail("refusing to overwrite an existing fixture config", pytrace=False)
    variant.resolve().relative_to(bench.config_root.resolve())
    original_config = bench.config.read_bytes()
    document = bench.configuration()
    debugger = document["debuggers"][debugger_id]
    debugger["type"] = "pyocd"
    debugger["executable"] = executable
    debugger["target_type"] = TARGET_TYPE
    debugger.pop("interface_cfg", None)
    debugger.pop("target_cfg", None)
    variant.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    variant_bench = PyOcdBench(replace(bench, config=variant))
    server = Server(variant_bench, tmp_path / "pyocd-discovery-reset-mcp.stderr")
    run_open = False
    private_values = (
        str(debugger.get("probe_id") or ""),
        str(document["com_ports"][port_id].get("device") or ""),
        str(bench.project),
        bench.project.as_posix(),
        str(bench.config_root),
        bench.config_root.as_posix(),
        str(bench.state_root),
        bench.state_root.as_posix(),
        str(Path.home()),
        Path.home().as_posix(),
    )
    try:
        server.greet()
        _, info = server.call("debugger_info")
        info_evidence = pyocd_result_evidence(bench, info, private_values)
        record_property("pyocd_discovery_debugger_info_v1", json.dumps(info_evidence, sort_keys=True, separators=(",", ":")))
        require_success(info, "debugger_info before discovery reset", info_evidence)
        assert info.get("backend") == "pyocd", info_evidence
        assert info.get("target_type") == TARGET_TYPE, info_evidence
        assert isinstance(info.get("version"), str) and pyocd_version in info["version"], info_evidence

        _, opened = server.call(
            "bench_run_start",
            {
                "devices": [
                    {"kind": "debugger", "id": debugger_id},
                    {"kind": "uart", "id": port_id},
                ],
                "label": "pyocd-discovery-usb-reset-diagnostic",
            },
        )
        require_success(opened, "bench_run_start for pyOCD discovery reset")
        run_open = True

        _, uart_listing = server.call("com_ports_list")
        require_success(uart_listing, "com_ports_list before pyOCD discovery reset")
        serial = str(debugger.get("probe_id") or "")
        available = uart_listing.get("available_com_ports")
        available_ports = available.get("ports") if isinstance(available, dict) else None
        matches = [
            item
            for item in (available_ports if isinstance(available_ports, list) else [])
            if isinstance(item, dict)
            and str(item.get("serial_number") or "").casefold() == serial.casefold()
            and isinstance(item.get("vid"), int)
            and isinstance(item.get("pid"), int)
        ]
        if len(matches) != 1 or not serial:
            pytest.fail("the UART inventory did not uniquely identify the configured probe", pytrace=False)
        port_identity = matches[0]
        usb_identity = usb.find_usb_device(
            sysfs_root=USB_SYSFS,
            device_root=USBFS_ROOT,
            expected_serial=serial,
            expected_vid=port_identity["vid"],
            expected_pid=port_identity["pid"],
        )
        pyocd_discovery_after_usb_reset(
            server,
            bench,
            expected_serial=serial,
            usb_identity=usb_identity,
            private_values=private_values,
            record_property=record_property,
        )
    finally:
        try:
            if run_open and server.process.poll() is None:
                stopped = server.try_call("bench_run_stop")
                if not isinstance(stopped, dict) or stopped.get("ok") is not True:
                    pytest.fail("Agentic HIL could not close the pyOCD discovery run", pytrace=False)
        finally:
            try:
                server.close()
            finally:
                if bench.config.read_bytes() != original_config:
                    pytest.fail("the original bench fixture config was modified", pytrace=False)
                variant.unlink(missing_ok=True)


def test_pyocd_f446re_probe_flash_reset_and_uart_recording(bench: Bench, tmp_path: Path, record_property) -> None:
    """Capture one genuine healthy pyOCD connect/flash/reset/boot through MCP."""
    build_error = built_where_it_stands(bench.project)
    assert build_error is None, f"the demo ELF needed for the pyOCD baseline did not build:\n{build_error}"
    image = bench.project / DEMO_IMAGE
    assert image.is_file(), f"the demo build left no ELF at {image}"
    executable, pyocd_version, pack_version = pyocd_provenance(pyocd_bench_environment(bench))

    debugger_id = bench.debugger_name()
    ports = sorted(bench.configuration().get("com_ports") or {})
    assert ports, "the pyOCD baseline needs the probe's configured UART entry"
    port_id = ports[0]

    # This is an owned throwaway configuration under the bench fixture root.
    # Keep every identity and permission from init; only select pyOCD and its
    # known CMSIS-pack target. Never write bench.config or an operator config.
    variant = bench.config_root / "bench-pyocd-recordings.yaml"
    assert not variant.exists(), f"refusing to overwrite an existing fixture config: {variant}"
    variant.resolve().relative_to(bench.config_root.resolve())
    original_config = bench.config.read_bytes()
    document = bench.configuration()
    debugger = document["debuggers"][debugger_id]
    debugger["type"] = "pyocd"
    debugger["executable"] = executable
    debugger["target_type"] = TARGET_TYPE
    debugger.pop("interface_cfg", None)
    debugger.pop("target_cfg", None)
    variant.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    variant_bench = PyOcdBench(replace(bench, config=variant))
    server = Server(variant_bench, tmp_path / "pyocd-recording-mcp.stderr")
    run_open = False
    run_stopped = False
    flash_result: dict | None = None
    info: dict = {}
    serial = str(debugger.get("probe_id") or "")
    port_device = str(document.get("com_ports", {}).get(port_id, {}).get("device") or "")
    private_values = (
        serial,
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
    probe_result: dict | None = None
    reset_result: dict | None = None
    boot_confirmed = False
    uart_session_open = False
    run_closure: dict | None = None
    try:
        server.greet()
        _, info = server.call("debugger_info")
        info_evidence = pyocd_result_evidence(bench, info, private_values)
        record_property("pyocd_debugger_info_v1", json.dumps(info_evidence, sort_keys=True, separators=(",", ":")))
        require_success(info, "debugger_info", info_evidence)
        assert info.get("backend") == "pyocd", info_evidence
        assert info.get("target_type") == TARGET_TYPE, info_evidence
        assert isinstance(info.get("version"), str) and pyocd_version in info["version"], info_evidence
        assert info.get("executable") == executable, info_evidence

        _, opened = server.call(
            "bench_run_start",
            {
                "devices": [
                    {"kind": "debugger", "id": debugger_id},
                    {"kind": "uart", "id": port_id},
                ],
                "label": "pyocd-recording",
            },
        )
        require_success(opened, "bench_run_start")
        run_open = True

        _, probe_result = server.call("probe_target")
        probe_evidence = pyocd_result_evidence(bench, probe_result, private_values)
        record_property("pyocd_probe_result_v1", json.dumps(probe_evidence, sort_keys=True, separators=(",", ":")))
        require_success(probe_result, "probe_target", probe_evidence)
        assert probe_result.get("backend") == "pyocd", probe_evidence
        assert probe_result.get("target_detected") is True, probe_evidence

        _, flash_result = server.call(
            "flash_firmware",
            {
                "image_path": image.relative_to(bench.project).as_posix(),
                "reset_after_flash": True,
                "capture": {"port_id": port_id, "until": BOOT_BANNER, "wait_timeout_s": 15.0},
            },
        )
        flash_result["pyocd_version"] = pyocd_version
        flash_result["cmsis_pack"] = {"id": PACK_ID, "version": pack_version, "target_type": TARGET_TYPE}
        source_commit = os.environ.get("AGENTIC_HIL_BENCH_COMMIT", "")
        run_id = os.environ.get("AGENTIC_HIL_BENCH_RUN_ID", "")
        if source_commit:
            assert re.fullmatch(r"[0-9a-f]{40}", source_commit), source_commit
        if run_id:
            assert re.fullmatch(r"[A-Za-z0-9_.-]+", run_id), run_id
        recorded_transcript = transcript(bench, flash_result, private_values)
        record_property(
            "pyocd_recording_v1",
            json.dumps(
                {
                    "schema": RECORDING_SCHEMA,
                    "source_commit": source_commit or None,
                    "run_id": run_id or None,
                    "backend": "pyocd",
                    "scenario": "healthy-demo-flash-reset-uart-boot",
                    "outcome": "success" if overall_success(flash_result) else "failure",
                    "executable": executable,
                    "pyocd_version": pyocd_version,
                    "cmsis_pack": {"id": PACK_ID, "version": pack_version, "target_type": TARGET_TYPE},
                    "probe": {
                        "backend": probe_result.get("backend"),
                        "target_detected": probe_result.get("target_detected"),
                        "ok": probe_result.get("ok"),
                    },
                    "flash_result": product_diagnostics(flash_result),
                    "flash_summary": redact(str(flash_result.get("summary") or ""), private_values),
                    # pyOCD's current product backend does not independently
                    # verify readback. Preserve that fact; the UART banner is
                    # the independent proof that this demo image booted.
                    "product_verify_claim": flash_result.get("verify"),
                    "capture": redact_values(flash_result.get("capture"), private_values),
                    **recorded_transcript,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        require_success(flash_result, "demo flash, reset and capture")
        assert flash_result.get("backend") == "pyocd", flash_result
        capture = flash_result.get("capture")
        assert isinstance(capture, dict) and capture.get("until_matched") is True, capture
        assert capture.get("matched") == BOOT_BANNER, capture
        assert flash_result.get("verify") is False, flash_result

        _, uart_opened = server.call("com_session_start", {"port_id": port_id, "clear_buffer": True})
        require_success(uart_opened, "com_session_start before reset")
        uart_session_open = True

        _, reset_result = server.call("reset_target", {"mode": "run"})
        record_property(
            "pyocd_reset_recording_v1",
            json.dumps(
                {
                    "schema": RECORDING_SCHEMA,
                    "outcome": "success" if overall_success(reset_result) else "failure",
                    "result": product_diagnostics(reset_result),
                    **transcript(bench, reset_result, private_values),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        require_success(reset_result, "reset_target")
        text = read_boot_banner(server, port_id)
        assert BOOT_BANNER in text, "the demo did not print its expected UART boot banner after reset"
        boot_confirmed = True
    finally:
        try:
            try:
                if uart_session_open and server.process.poll() is None:
                    stopped_uart = server.try_call("com_session_stop", {"port_id": port_id})
                    assert isinstance(stopped_uart, dict) and stopped_uart.get("ok") is True, stopped_uart
                    uart_session_open = False
            finally:
                if run_open and server.process.poll() is None:
                    run_closure = capture_run_stop_with_lease_evidence(server, private_values)
                    stopped = run_closure["run_stop"]
                    run_stopped = run_stop_succeeded(stopped)
                    if not run_stopped and sys.exc_info()[0] is None:
                        pytest.fail(f"Agentic HIL run stop failed its continue predicate: {stopped}", pytrace=False)
        finally:
            try:
                server.close()
            finally:
                assert bench.config.read_bytes() == original_config, "the original bench fixture config was modified"
                variant.unlink(missing_ok=True)
                record_property(
                    "pyocd_run_closure_v1",
                    json.dumps(
                        {
                            "schema": RECORDING_SCHEMA,
                            "run_stopped": run_stopped,
                            "reset_result": product_diagnostics(reset_result) if reset_result is not None else None,
                            "uart_boot_confirmed_after_reset": boot_confirmed,
                            "lease_status_before_stop": run_closure["lease_status_before_stop"] if run_closure else {"available": False, "unavailable_reason": "run_stop_not_attempted"},
                            "run_stop": run_closure["run_stop"] if run_closure else {"available": False, "unavailable_reason": "run_stop_not_attempted"},
                            "lease_status_after_stop": run_closure["lease_status_after_stop"] if run_closure else {"available": False, "unavailable_reason": "run_stop_not_attempted"},
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
