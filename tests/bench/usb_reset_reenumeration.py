"""Opt-in proof that the same MCP server finds its ST-Link and UART after usbfs reset.

Select this module explicitly on the Linux bench. It is intentionally not named
``test_*.py`` so the normal OpenOCD bench tier never triggers a USB reset.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

from agentic_hil.report import overall_success

from .conftest import BENCH_ONLY, DEMO_IMAGE, Bench, built_where_it_stands
from .test_bench_faults import Server

pytestmark = [pytest.mark.bench, BENCH_ONLY]

USB_SYSFS = Path("/sys/bus/usb/devices")
USBFS_ROOT = Path("/dev/bus/usb")
BOOT_BANNER = b"Hello World"
VISIBILITY_TIMEOUT_S = 12.0


def require_product_success(result: dict, action: str, private_values: tuple[str, ...] = ()) -> None:
    if not overall_success(result):
        diagnostics = {
            key: result.get(key)
            for key in (
                "error_type",
                "backend_error_type",
                "backend_error",
                "summary",
                "retry_safe",
                "ok",
                "target_ok",
                "audit_ok",
                "cleanup_ok",
                "cleanup_required",
                "quarantined",
                "lease_state",
                "side_effect_status",
                "hardware_state",
            )
        }
        for key, value in diagnostics.items():
            if isinstance(value, str):
                for private in sorted((item for item in private_values if item), key=len, reverse=True):
                    value = re.sub(re.escape(private), "[redacted]", value, flags=re.IGNORECASE)
                diagnostics[key] = value
        pytest.fail(f"Agentic HIL {action} failed its continue predicate: {diagnostics}", pytrace=False)


def read_demo_boot(
    server: Server, port_id: str, *, private_values: tuple[str, ...] = (), timeout_s: float = 15.0
) -> str:
    """Accumulate HIL UART reads until the banner arrives, checking every result."""
    if timeout_s <= 0:
        raise ValueError("UART boot wait must be positive")
    deadline = time.monotonic() + timeout_s
    fragments: list[str] = []
    collected = ""
    banner = BOOT_BANNER.decode("ascii")
    while banner not in collected:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(
                "the demo UART did not produce its complete boot banner before the bounded wait expired", pytrace=False
            )
        _, result = server.call("com_read", {"port_id": port_id, "wait_timeout_s": min(0.5, remaining)})
        require_product_success(result, "com_read after target reset", private_values)
        data = result.get("data")
        if not isinstance(data, dict):
            pytest.fail("Agentic HIL returned no UART data document", pytrace=False)
        hex_data = data.get("hex")
        if isinstance(hex_data, str):
            try:
                fragment = bytes.fromhex(hex_data).decode("utf-8", errors="replace")
            except ValueError:
                pytest.fail("Agentic HIL returned malformed UART bytes", pytrace=False)
        else:
            fragment = str(data.get("text") or "")
        fragments.append(fragment)
        collected = "".join(fragments)
    return collected


def stlink_identity(listing: dict, expected_serial: str) -> tuple[str, str]:
    """Take VID/PID only from the product's current ST-Link inventory record."""
    matching = [
        port
        for port in listing.get("stlink_ports", [])
        if isinstance(port, dict)
        and str(port.get("serial_number") or "").casefold() == expected_serial.casefold()
        and isinstance(port.get("vid"), int)
        and not isinstance(port.get("vid"), bool)
        and isinstance(port.get("pid"), int)
        and not isinstance(port.get("pid"), bool)
    ]
    identities = {(f"{port['vid']:04x}", f"{port['pid']:04x}") for port in matching}
    if len(identities) != 1:
        pytest.fail(
            "the MCP ST-Link inventory did not identify exactly one VID/PID for the configured probe", pytrace=False
        )
    return next(iter(identities))


def matching_available_port(listing: dict, serial: str, vid: str, pid: str) -> dict:
    found = available_port_matching(listing, serial, vid, pid)
    if found is None:
        pytest.fail(
            "Agentic HIL did not rediscover exactly one UART with the configured ST-Link identity", pytrace=False
        )
    return found


def wait_for_the_configured_uart(server, serial: str, vid: str, pid: str, private_values, timeout_s: float = VISIBILITY_TIMEOUT_S) -> dict:
    """Poll the product's own listing until the rediscovered UART is there and openable.

    Re-enumeration is exactly what this stage induces, and `cdc_acm` binds and
    creates `/dev/ttyACM*` after the usbfs node exists, with udev correcting the
    new node's group and ACL after that again. Asking once, the moment the usbfs
    wait returned, failed on timing rather than on behaviour: `matching_available_port`
    hard-fails when the UART is not already back, and `com_session_start` fails
    outright on a node that exists and is not yet group- or ACL-corrected, because
    comports.py keeps EACCES out of PORT_BUSY_ERRNOS deliberately and nothing
    retries it. The recording's own `uart_open_failure` field is where that
    outcome was being recorded instead of waited out.

    The listing is the product's, so this adds no device access of its own; the
    node's openability is asked of the kernel with `os.access`, which opens
    nothing. The bound is the same visibility bound the usbfs wait uses, and what
    it gave up on is reported by the single-shot calls that follow, unchanged.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        _, listing = server.call("com_ports_list")
        require_product_success(listing, "com_ports_list after reset", private_values)
        found = available_port_matching(listing, serial, vid, pid)
        if found is not None:
            device = str(found.get("device") or "")
            if device and os.access(device, os.R_OK | os.W_OK):
                return listing
        if time.monotonic() >= deadline:
            return listing
        time.sleep(0.25)


def available_port_matching(listing: dict, serial: str, vid: str, pid: str) -> dict | None:
    """The one rediscovered UART with this identity, or None where there is not exactly one.

    What `matching_available_port` reads, without the refusal: a poll asks this and
    waits, and the single-shot call after the wait is the one that fails.
    """
    available = listing.get("available_com_ports")
    ports = available.get("ports") if isinstance(available, dict) else None
    matches = [
        port
        for port in (ports if isinstance(ports, list) else [])
        if isinstance(port, dict)
        and str(port.get("serial_number") or "").casefold() == serial.casefold()
        and isinstance(port.get("vid"), int)
        and isinstance(port.get("pid"), int)
        and f"{port['vid']:04x}" == vid
        and f"{port['pid']:04x}" == pid
    ]
    return matches[0] if len(matches) == 1 else None


def test_usbdevfs_reset_is_recovered_by_the_same_mcp_server_and_demo_uart(
    bench: Bench, tmp_path: Path, record_property
) -> None:
    """Reset exactly the configured probe's usbfs node and prove same-server recovery."""
    from . import usb_reset_support as usb

    build_error = built_where_it_stands(bench.project)
    if build_error is not None:
        pytest.fail("the demo ELF needed for the USB reset check did not build", pytrace=False)
    image = bench.project / DEMO_IMAGE
    if not image.is_file():
        pytest.fail("the demo build left no ELF for the USB reset check", pytrace=False)

    configuration = bench.configuration()
    debugger_id = bench.debugger_name()
    debugger = configuration["debuggers"][debugger_id]
    serial = str(debugger.get("probe_id") or "")
    port_ids = sorted(configuration.get("com_ports") or {})
    if not serial or not port_ids:
        pytest.fail("the bench configuration lacks its ST-Link serial or UART entry", pytrace=False)
    port_id = port_ids[0]
    port_config = configuration["com_ports"][port_id]
    if str(port_config.get("serial_number") or "").casefold() != serial.casefold():
        pytest.fail("the configured UART does not identify the configured ST-Link serial", pytrace=False)

    server = Server(bench, tmp_path / "usb-reset-mcp.stderr")
    server_pid = server.pid
    run_open = False
    uart_open = False
    reset_confirmed = False
    probe_after_reset = False
    uart_rebind_observed = False
    before = None
    after = None
    tty_before = None
    tty_after = None
    uart_open_result = None
    host_paths = (
        str(USB_SYSFS),
        USB_SYSFS.as_posix(),
        str(USBFS_ROOT),
        USBFS_ROOT.as_posix(),
        str(tmp_path),
        str(Path.home()),
    )
    private_values = (
        serial,
        str(port_config.get("device") or ""),
        str(bench.project),
        bench.project.as_posix(),
        str(bench.config_root),
        bench.config_root.as_posix(),
        str(bench.state_root),
        bench.state_root.as_posix(),
        str(Path.home()),
        Path.home().as_posix(),
    )
    demo_boot_before_reset = False
    demo_boot_after_reset = False
    try:
        server.greet()
        _, run = server.call(
            "bench_run_start",
            {
                "devices": [
                    {"kind": "debugger", "id": debugger_id},
                    {"kind": "uart", "id": port_id},
                ],
                "label": "usbdevfs-reset-reenumeration",
            },
        )
        require_product_success(run, "bench_run_start", private_values)
        run_open = True

        _, listed = server.call("debugger_probes_list")
        require_product_success(listed, "debugger_probes_list", private_values)
        probe_ids = {
            str(item.get("probe_id") or "").casefold() for item in listed.get("probes", []) if isinstance(item, dict)
        }
        if serial.casefold() not in probe_ids:
            pytest.fail("the configured ST-Link serial was absent from the live HIL probe listing", pytrace=False)
        vid, pid = stlink_identity(listed, serial)

        _, uart_listing = server.call("com_ports_list")
        require_product_success(uart_listing, "com_ports_list before reset", private_values)
        before_port = matching_available_port(uart_listing, serial, vid, pid)
        before = usb.find_usb_device(
            sysfs_root=USB_SYSFS,
            device_root=USBFS_ROOT,
            expected_serial=serial,
            expected_vid=vid,
            expected_pid=pid,
        )
        tty_before = usb.tty_device_snapshot(
            str(before_port.get("device") or ""),
            sysfs_root=Path("/sys/class/tty"),
            device_root=Path("/dev"),
        )

        _, flashed = server.call(
            "flash_firmware",
            {
                "image_path": image.relative_to(bench.project).as_posix(),
                "reset_after_flash": True,
                "capture": {"port_id": port_id, "until": BOOT_BANNER.decode("ascii"), "wait_timeout_s": 15.0},
            },
        )
        require_product_success(flashed, "demo flash and boot capture before reset", private_values)
        capture = flashed.get("capture")
        if (
            not isinstance(capture, dict)
            or capture.get("until_matched") is not True
            or capture.get("matched") != BOOT_BANNER.decode("ascii")
        ):
            pytest.fail("the demo did not boot and print its expected UART banner before USB reset", pytrace=False)
        demo_boot_before_reset = True

        # The flash capture owns and closes its temporary serial handle. If the
        # product reports one still active, close it before resetting the USB device.
        _, port_status = server.call("com_ports_list")
        require_product_success(port_status, "com_ports_list before reset", private_values)
        configured_status = port_status.get("ports", {}).get(port_id, {})
        if configured_status.get("session_active") is True:
            _, stopped = server.call("com_session_stop", {"port_id": port_id})
            require_product_success(stopped, "com_session_stop before USB reset", private_values)

        try:
            usb.reset_usb_device(before)
        except OSError as error:
            pytest.fail(f"usbfs reset failed: {type(error).__name__}, errno={error.errno}", pytrace=False)
        reset_confirmed = True
        after = usb.wait_for_usb_device(
            sysfs_root=USB_SYSFS,
            device_root=USBFS_ROOT,
            expected_serial=serial,
            expected_vid=vid,
            expected_pid=pid,
            timeout_s=VISIBILITY_TIMEOUT_S,
        )

        if server.pid != server_pid or server.process.poll() is not None:
            pytest.fail("the MCP process did not remain the same live instance across USB reset", pytrace=False)
        _, rediscovered = server.call("debugger_probes_list")
        require_product_success(rediscovered, "debugger_probes_list after reset", private_values)
        discovered_ids = {
            str(item.get("probe_id") or "").casefold()
            for item in rediscovered.get("probes", [])
            if isinstance(item, dict)
        }
        if serial.casefold() not in discovered_ids:
            pytest.fail("the same MCP server did not rediscover the configured probe after USB reset", pytrace=False)
        _, probe = server.call("probe_target")
        require_product_success(probe, "probe_target after reset", private_values)
        if probe.get("target_detected") is not True:
            pytest.fail("the same MCP server did not confirm the target after USB reset", pytrace=False)
        probe_after_reset = True

        # The tty is created after the usbfs node and corrected after that again,
        # so the two single-shot calls below get a bounded wait of their own rather
        # than the instant the usbfs wait returned.
        after_listing = wait_for_the_configured_uart(server, serial, vid, pid, private_values)
        after_port = matching_available_port(after_listing, serial, vid, pid)
        before_device = str(before_port.get("device") or "")
        after_device = str(after_port.get("device") or "")
        tty_after = usb.tty_device_snapshot(
            after_device,
            sysfs_root=Path("/sys/class/tty"),
            device_root=Path("/dev"),
        )
        uart_rebind_observed = bool(before_device and after_device and before_device != after_device)
        if tty_before is not None:
            uart_rebind_observed = uart_rebind_observed or any(
                tty_before.get(field) is not None
                and tty_after.get(field) is not None
                and tty_before[field] != tty_after[field]
                for field in ("sysfs_inode", "sysfs_link_inode", "node_inode", "node_rdev")
            )

        _, opened = server.call("com_session_start", {"port_id": port_id, "clear_buffer": True})
        uart_open_result = opened
        require_product_success(opened, "com_session_start after reset", private_values)
        uart_open = True
        _, reset_target = server.call("reset_target", {"mode": "run"})
        require_product_success(reset_target, "reset_target after USB reset", private_values)
        text = read_demo_boot(server, port_id, private_values=private_values, timeout_s=15.0)
        if BOOT_BANNER.decode("ascii") not in text:
            pytest.fail("the demo did not boot and print its expected UART banner after USB reset", pytrace=False)
        demo_boot_after_reset = True
    finally:
        try:
            if uart_open and server.process.poll() is None:
                stop_result = server.try_call("com_session_stop", {"port_id": port_id})
                uart_open = False
                if isinstance(stop_result, dict) and not overall_success(stop_result):
                    pytest.fail("Agentic HIL could not close the UART session during cleanup", pytrace=False)
        finally:
            try:
                if run_open and server.process.poll() is None:
                    stop_run = server.try_call("bench_run_stop")
                    if not isinstance(stop_run, dict) or stop_run.get("ok") is not True:
                        pytest.fail("Agentic HIL could not release the declared run lease", pytrace=False)
            finally:
                try:
                    server.close()
                finally:
                    source_commit = os.environ.get("AGENTIC_HIL_BENCH_COMMIT", "")
                    run_id = os.environ.get("AGENTIC_HIL_BENCH_RUN_ID", "")
                    identity_before = (
                        None
                        if before is None
                        else {
                            "busnum": before.busnum,
                            "devnum": before.devnum,
                            "major": before.major,
                            "minor": before.minor,
                        }
                    )
                    identity_after = (
                        None
                        if after is None
                        else {
                            "busnum": after.busnum,
                            "devnum": after.devnum,
                            "major": after.major,
                            "minor": after.minor,
                        }
                    )
                    recording = usb.usb_reset_recording(
                        serial_number=serial,
                        host_paths=host_paths,
                        before=identity_before,
                        after=identity_after,
                        reset_confirmed=reset_confirmed,
                        uart_rebind_observed=uart_rebind_observed,
                        uart_open_result=uart_open_result,
                        tty_before=tty_before,
                        tty_after=tty_after,
                        private_values=private_values,
                        source_commit=source_commit,
                        run_id=run_id,
                    )
                    recording.update(
                        {
                            "mcp_process_same": server.pid == server_pid,
                            "probe_after_reset_confirmed": probe_after_reset,
                            "demo_boot_before_reset_confirmed": demo_boot_before_reset,
                            "demo_boot_after_reset_confirmed": demo_boot_after_reset,
                        }
                    )
                    record_property(
                        "usb_reset_recording_v1", json.dumps(recording, sort_keys=True, separators=(",", ":"))
                    )
