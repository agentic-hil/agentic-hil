"""Narrow usbfs reset support used only by the opt-in Linux bench test."""

from __future__ import annotations

import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

USBDEVFS_RESET_IOCTL = 0x5514  # _IO('U', 20), from linux/usbdevice_fs.h


class USBDeviceNotFound(RuntimeError):
    """No unique USB device matched the requested ST-Link identity."""


class AmbiguousUSBDevice(RuntimeError):
    """More than one non-hub USB device matched the requested identity."""


class USBDeviceNodeMismatch(RuntimeError):
    """The opened usbfs node did not retain the identity observed before open."""


@dataclass(frozen=True)
class USBDeviceIdentity:
    sysfs_path: Path
    device_root: Path
    node_path: Path
    serial_number: str
    vid: str
    pid: str
    busnum: int
    devnum: int
    rdev: int
    major: int
    minor: int


def _read_text(path: Path) -> str:
    return path.read_text(encoding="ascii").strip()


def _normalize_usb_id(value: str | int) -> str:
    if isinstance(value, bool):
        raise ValueError("USB IDs must be integers or hexadecimal strings")
    try:
        numeric = int(value, 16) if isinstance(value, str) else value
    except ValueError as error:
        raise ValueError("USB IDs must be integers or hexadecimal strings") from error
    if not isinstance(numeric, int) or not 0 <= numeric <= 0xFFFF:
        raise ValueError("USB IDs must fit in 16 bits")
    return f"{numeric:04x}"


def _device_number(rdev: int) -> tuple[int, int]:
    # os.major/minor are POSIX APIs and deliberately remain off the import path
    # on Windows, where unit tests still import this module.
    major = getattr(os, "major", None)
    minor = getattr(os, "minor", None)
    if major is None or minor is None:
        return 0, 0
    return int(major(rdev)), int(minor(rdev))


def _identity_at(
    sysfs_path: Path,
    device_root: Path,
    *,
    expected_serial: str,
    expected_vid: str,
    expected_pid: str,
    stat_fn: Callable[[Path], Any] = os.stat,
) -> USBDeviceIdentity:
    serial = _read_text(sysfs_path / "serial")
    vid = _normalize_usb_id(_read_text(sysfs_path / "idVendor"))
    pid = _normalize_usb_id(_read_text(sysfs_path / "idProduct"))
    device_class = _normalize_usb_id(_read_text(sysfs_path / "bDeviceClass"))
    if device_class == "0009":
        raise USBDeviceNotFound("a USB hub is not a resettable probe")
    if serial.casefold() != expected_serial.casefold() or vid != expected_vid or pid != expected_pid:
        raise USBDeviceNotFound("USB identity changed while it was being checked")
    busnum = int(_read_text(sysfs_path / "busnum"), 10)
    devnum = int(_read_text(sysfs_path / "devnum"), 10)
    node_path = device_root / f"{busnum:03d}" / f"{devnum:03d}"
    node_stat = stat_fn(node_path)
    rdev = int(node_stat.st_rdev)
    major, minor = _device_number(rdev)
    return USBDeviceIdentity(
        sysfs_path=sysfs_path,
        device_root=device_root,
        node_path=node_path,
        serial_number=serial,
        vid=vid,
        pid=pid,
        busnum=busnum,
        devnum=devnum,
        rdev=rdev,
        major=major,
        minor=minor,
    )


def find_usb_device(
    *,
    sysfs_root: Path,
    device_root: Path,
    expected_serial: str,
    expected_vid: str | int,
    expected_pid: str | int,
    stat_fn: Callable[[Path], Any] = os.stat,
) -> USBDeviceIdentity:
    """Find exactly one USB device node by serial plus both configured USB IDs."""
    if not expected_serial.strip():
        raise ValueError("a configured probe serial is required")
    wanted_vid = _normalize_usb_id(expected_vid)
    wanted_pid = _normalize_usb_id(expected_pid)
    candidates: list[USBDeviceIdentity] = []
    for sysfs_path in sysfs_root.iterdir():
        if not sysfs_path.is_dir():
            continue
        try:
            serial = _read_text(sysfs_path / "serial")
            vid = _normalize_usb_id(_read_text(sysfs_path / "idVendor"))
            pid = _normalize_usb_id(_read_text(sysfs_path / "idProduct"))
            device_class = _normalize_usb_id(_read_text(sysfs_path / "bDeviceClass"))
        except (OSError, ValueError):
            # USB interface entries and devices with incomplete descriptors do
            # not have the complete identity tuple required for selection.
            continue
        if (serial.casefold(), vid, pid) != (expected_serial.casefold(), wanted_vid, wanted_pid):
            continue
        if device_class == "0009":
            continue
        try:
            candidates.append(
                _identity_at(
                    sysfs_path,
                    device_root,
                    expected_serial=expected_serial,
                    expected_vid=wanted_vid,
                    expected_pid=wanted_pid,
                    stat_fn=stat_fn,
                )
            )
        except FileNotFoundError:
            continue
    if not candidates:
        raise USBDeviceNotFound("no attached USB device matched the configured probe identity")
    if len(candidates) != 1:
        raise AmbiguousUSBDevice("multiple attached USB devices matched the configured probe identity")
    return candidates[0]


def reset_usb_device(
    identity: USBDeviceIdentity,
    *,
    open_fn: Callable[[Path, int], int] | None = None,
    fstat_fn: Callable[[int], Any] | None = None,
    ioctl_fn: Callable[[int, int, int], Any] | None = None,
    close_fn: Callable[[int], Any] | None = None,
    stat_fn: Callable[[Path], Any] | None = None,
) -> None:
    """Issue one usbfs reset after revalidating the sysfs and opened-node identity.

    The Linux ``fcntl`` module is imported only at the actual ioctl boundary,
    so importing the opt-in test module on Windows remains safe. Permission
    errors propagate unchanged; no sudo or permission-changing fallback exists.
    """
    fresh = _identity_at(
        identity.sysfs_path,
        identity.device_root,
        expected_serial=identity.serial_number,
        expected_vid=identity.vid,
        expected_pid=identity.pid,
        stat_fn=stat_fn or os.stat,
    )
    if fresh.node_path != identity.node_path or fresh.rdev != identity.rdev:
        raise USBDeviceNodeMismatch("the USB node changed after probe identity discovery")

    opener = open_fn or os.open
    fstat = fstat_fn or os.fstat
    closer = close_fn or os.close
    node_stat = (stat_fn or os.stat)(fresh.node_path)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    fd = opener(fresh.node_path, flags)
    try:
        opened_stat = fstat(fd)
        if (
            not stat.S_ISCHR(opened_stat.st_mode)
            or int(opened_stat.st_rdev) != int(node_stat.st_rdev)
            or int(opened_stat.st_rdev) != identity.rdev
        ):
            raise USBDeviceNodeMismatch("the opened USB node is not the freshly identified character device")
        try:
            after_open = _identity_at(
                identity.sysfs_path,
                identity.device_root,
                expected_serial=identity.serial_number,
                expected_vid=identity.vid,
                expected_pid=identity.pid,
                stat_fn=stat_fn or os.stat,
            )
        except (OSError, ValueError, USBDeviceNotFound) as error:
            raise USBDeviceNodeMismatch("the USB identity changed while its node was being opened") from error
        if (
            after_open.node_path != fresh.node_path
            or after_open.rdev != int(opened_stat.st_rdev)
            or after_open.serial_number.casefold() != identity.serial_number.casefold()
            or after_open.vid != identity.vid
            or after_open.pid != identity.pid
        ):
            raise USBDeviceNodeMismatch("the opened USB node no longer belongs to the identified probe")
        if ioctl_fn is None:
            import fcntl

            ioctl_fn = fcntl.ioctl
        ioctl_fn(fd, USBDEVFS_RESET_IOCTL, 0)
    finally:
        closer(fd)


def wait_for_usb_device(
    *,
    sysfs_root: Path,
    device_root: Path,
    expected_serial: str,
    expected_vid: str | int,
    expected_pid: str | int,
    timeout_s: float = 12.0,
    poll_interval_s: float = 0.25,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> USBDeviceIdentity:
    """Wait a bounded interval for the same identity to become visible again."""
    if timeout_s <= 0 or poll_interval_s <= 0:
        raise ValueError("USB visibility wait bounds must be positive")
    deadline = clock() + timeout_s
    while True:
        try:
            return find_usb_device(
                sysfs_root=sysfs_root,
                device_root=device_root,
                expected_serial=expected_serial,
                expected_vid=expected_vid,
                expected_pid=expected_pid,
            )
        except USBDeviceNotFound:
            remaining = deadline - clock()
            if remaining <= 0:
                raise
            sleep(min(poll_interval_s, remaining))


def usb_reset_recording(
    *,
    serial_number: str,
    host_paths: tuple[str, ...],
    before: dict[str, int] | None,
    after: dict[str, int] | None,
    reset_confirmed: bool,
    uart_rebind_observed: bool,
    source_commit: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    """Build a JUnit-safe fact record without publishing the probe serial or paths."""
    if source_commit and not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("source commit must be a full lowercase Git SHA")
    if run_id and not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise ValueError("run id contains unsupported characters")
    safe_before = _numeric_snapshot(before)
    safe_after = _numeric_snapshot(after)
    return {
        "schema": "agentic-hil.usb-reset-recording/v1",
        "source_commit": source_commit or None,
        "run_id": run_id or None,
        "scenario": "usbdevfs-reset-reenumeration-check",
        "outcome": "reset-confirmed" if reset_confirmed else "reset-not-confirmed",
        "serial_number": "[redacted]" if serial_number else None,
        "host_paths": ["[redacted]" for _ in host_paths],
        "reset_confirmed": bool(reset_confirmed),
        "uart_rebind_observed": bool(uart_rebind_observed),
        "before": safe_before,
        "after": safe_after,
        "devnum_changed": _changed(safe_before, safe_after, "devnum"),
        "major_minor_changed": _pair_changed(safe_before, safe_after, "major", "minor"),
        "full_reenumeration_claimed": False,
    }


def _numeric_snapshot(snapshot: dict[str, int] | None) -> dict[str, int] | None:
    if snapshot is None:
        return None
    keys = ("busnum", "devnum", "major", "minor")
    if any(isinstance(snapshot.get(key), bool) or not isinstance(snapshot.get(key), int) for key in keys):
        raise ValueError("USB snapshots must contain numeric busnum/devnum/major/minor facts")
    return {key: int(snapshot[key]) for key in keys}


def _changed(before: dict[str, int] | None, after: dict[str, int] | None, key: str) -> bool | None:
    if before is None or after is None:
        return None
    return before[key] != after[key]


def _pair_changed(before: dict[str, int] | None, after: dict[str, int] | None, first: str, second: str) -> bool | None:
    if before is None or after is None:
        return None
    return (before[first], before[second]) != (after[first], after[second])
