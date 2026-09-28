"""Offline contracts for the opt-in Linux USB reset probe helper."""

from __future__ import annotations

import importlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE = "tests.bench.usb_reset_support"
SERIAL = "PRIVATE-STLINK-SERIAL"
VID = "0483"
PID = "374B"


def usb_entry(
    root: Path,
    name: str,
    *,
    serial: str = SERIAL,
    vid: str = VID,
    pid: str = PID,
    device_class: str = "00",
    bus: str = "001",
    dev: str = "007",
) -> Path:
    entry = root / name
    entry.mkdir(parents=True)
    for key, value in {
        "serial": serial,
        "idVendor": vid,
        "idProduct": pid,
        "bDeviceClass": device_class,
        "busnum": str(int(bus)),
        "devnum": str(int(dev)),
    }.items():
        (entry / key).write_text(value, encoding="ascii")
    return entry


def identity_for(helper, entry: Path, dev_root: Path):
    return helper.find_usb_device(
        sysfs_root=entry.parent,
        device_root=dev_root,
        expected_serial=SERIAL,
        expected_vid=VID,
        expected_pid=PID,
        stat_fn=lambda path: SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=0),
    )


def test_module_import_does_not_require_posix_fcntl() -> None:
    """The bench module is imported during collection on Windows too."""
    helper = importlib.import_module(MODULE)
    assert helper.USBDEVFS_RESET_IOCTL == 0x5514


def test_discovery_derives_only_the_exact_matching_device_node(tmp_path: Path) -> None:
    helper = importlib.import_module(MODULE)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    dev_root = tmp_path / "dev" / "bus" / "usb"
    expected = usb_entry(sysfs, "2-1", bus="002", dev="009")
    usb_entry(sysfs, "2-2", serial="OTHER-SERIAL", bus="002", dev="010")
    node = dev_root / "002" / "009"
    node.parent.mkdir(parents=True)
    node.touch()

    found = helper.find_usb_device(
        sysfs_root=sysfs,
        device_root=dev_root,
        expected_serial=SERIAL,
        expected_vid=VID,
        expected_pid=PID,
        stat_fn=lambda path: SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=0),
    )

    assert found.sysfs_path == expected
    assert found.node_path == node
    assert found.serial_number == SERIAL
    assert found.vid == VID.casefold()
    assert found.pid == PID.casefold()
    assert (found.busnum, found.devnum) == (2, 9)


@pytest.mark.parametrize(
    ("field", "value"),
    (("serial", "OTHER-SERIAL"), ("idVendor", "1234"), ("idProduct", "0001")),
)
def test_discovery_rejects_a_node_that_does_not_match_every_expected_identity_field(
    tmp_path: Path, field: str, value: str
) -> None:
    helper = importlib.import_module(MODULE)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    entry = usb_entry(sysfs, "1-1")
    (entry / field).write_text(value, encoding="ascii")

    with pytest.raises(helper.USBDeviceNotFound):
        helper.find_usb_device(
            sysfs_root=sysfs,
            device_root=tmp_path / "dev",
            expected_serial=SERIAL,
            expected_vid=VID,
            expected_pid=PID,
        )


def test_discovery_rejects_duplicate_matching_units_and_hubs(tmp_path: Path) -> None:
    helper = importlib.import_module(MODULE)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    usb_entry(sysfs, "1-1", bus="001", dev="003")
    usb_entry(sysfs, "1-2", bus="001", dev="004")
    with pytest.raises(helper.AmbiguousUSBDevice):
        helper.find_usb_device(
            sysfs_root=sysfs,
            device_root=tmp_path / "dev",
            expected_serial=SERIAL,
            expected_vid=VID,
            expected_pid=PID,
            stat_fn=lambda path: SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=0),
        )

    only_hub = tmp_path / "hub-sysfs"
    only_hub.mkdir()
    usb_entry(only_hub, "1-1", device_class="09")
    with pytest.raises(helper.USBDeviceNotFound):
        helper.find_usb_device(
            sysfs_root=only_hub,
            device_root=tmp_path / "dev",
            expected_serial=SERIAL,
            expected_vid=VID,
            expected_pid=PID,
        )


def test_reset_opens_only_the_derived_node_read_write_checks_fstat_then_issues_reset_and_closes(
    tmp_path: Path,
) -> None:
    helper = importlib.import_module(MODULE)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    entry = usb_entry(sysfs, "1-1", bus="001", dev="007")
    dev_root = tmp_path / "dev" / "bus" / "usb"
    node = dev_root / "001" / "007"
    node.parent.mkdir(parents=True)
    node.touch()
    identity = identity_for(helper, entry, dev_root)
    rdev = 0
    events: list[tuple] = []

    def open_fn(path, flags):
        events.append(("open", path, flags))
        return 41

    def fstat_fn(fd):
        events.append(("fstat", fd))
        return SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=rdev)

    def ioctl_fn(fd, request, argument):
        events.append(("ioctl", fd, request, argument))

    def close_fn(fd):
        events.append(("close", fd))

    helper.reset_usb_device(
        identity,
        open_fn=open_fn,
        fstat_fn=fstat_fn,
        ioctl_fn=ioctl_fn,
        close_fn=close_fn,
        stat_fn=lambda path: SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=rdev),
    )

    assert events == [
        ("open", node, os.O_RDWR | getattr(os, "O_CLOEXEC", 0)),
        ("fstat", 41),
        ("ioctl", 41, helper.USBDEVFS_RESET_IOCTL, 0),
        ("close", 41),
    ]


@pytest.mark.parametrize(
    "fstat_result",
    (
        SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_rdev=0),
        SimpleNamespace(st_mode=stat.S_IFCHR | 0o600, st_rdev=123456),
    ),
)
def test_reset_refuses_a_non_character_or_replaced_device_node_before_ioctl(tmp_path: Path, fstat_result) -> None:
    helper = importlib.import_module(MODULE)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    entry = usb_entry(sysfs, "1-1")
    dev_root = tmp_path / "dev" / "bus" / "usb"
    node = dev_root / "001" / "007"
    node.parent.mkdir(parents=True)
    node.touch()
    identity = identity_for(helper, entry, dev_root)
    ioctl_calls: list[tuple] = []

    with pytest.raises(helper.USBDeviceNodeMismatch):
        helper.reset_usb_device(
            identity,
            open_fn=lambda path, flags: 42,
            fstat_fn=lambda fd: fstat_result,
            ioctl_fn=lambda *args: ioctl_calls.append(args),
            close_fn=lambda fd: None,
            stat_fn=lambda path: SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=0),
        )

    assert ioctl_calls == []


def test_reset_rechecks_sysfs_identity_after_open_before_ioctl(tmp_path: Path) -> None:
    """A same-node re-enumeration during open must not redirect the reset."""
    helper = importlib.import_module(MODULE)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    entry = usb_entry(sysfs, "1-1")
    dev_root = tmp_path / "dev" / "bus" / "usb"
    node = dev_root / "001" / "007"
    node.parent.mkdir(parents=True)
    node.touch()
    identity = identity_for(helper, entry, dev_root)
    rdev = 0
    events: list[tuple] = []

    def open_fn(path, flags):
        events.append(("open", path, flags))
        # Simulate another device taking over the same usbfs path after the
        # pre-open scan; the node's rdev is unchanged.
        (entry / "serial").write_text("FOREIGN-SERIAL", encoding="ascii")
        return 44

    def fstat_fn(fd):
        return SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=rdev)

    with pytest.raises(helper.USBDeviceNodeMismatch):
        helper.reset_usb_device(
            identity,
            open_fn=open_fn,
            fstat_fn=fstat_fn,
            ioctl_fn=lambda *args: events.append(("ioctl", *args)),
            close_fn=lambda fd: events.append(("close", fd)),
            stat_fn=lambda path: SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=rdev),
        )

    assert [event[0] for event in events] == ["open", "close"]


def test_reset_does_not_retry_access_denial_with_sudo_or_permission_changes(tmp_path: Path) -> None:
    helper = importlib.import_module(MODULE)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    entry = usb_entry(sysfs, "1-1")
    dev_root = tmp_path / "dev"
    node = dev_root / "001" / "007"
    node.parent.mkdir(parents=True)
    node.touch()
    identity = identity_for(helper, entry, dev_root)
    opened: list[tuple] = []
    denied = PermissionError("USB node is not writable")

    def open_fn(path, flags):
        opened.append((path, flags))
        raise denied

    with pytest.raises(PermissionError) as caught:
        helper.reset_usb_device(
            identity,
            open_fn=open_fn,
            stat_fn=lambda path: SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=0),
        )

    assert caught.value is denied
    assert opened == [(identity.node_path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))]


def test_reset_always_closes_the_fd_when_ioctl_fails(tmp_path: Path) -> None:
    helper = importlib.import_module(MODULE)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    entry = usb_entry(sysfs, "1-1")
    dev_root = tmp_path / "dev" / "bus" / "usb"
    node = dev_root / "001" / "007"
    node.parent.mkdir(parents=True)
    node.touch()
    identity = identity_for(helper, entry, dev_root)
    rdev = 0
    closed: list[int] = []
    ioctl_error = OSError("reset ioctl refused")

    with pytest.raises(OSError) as caught:
        helper.reset_usb_device(
            identity,
            open_fn=lambda path, flags: 43,
            fstat_fn=lambda fd: SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=rdev),
            ioctl_fn=lambda *args: (_ for _ in ()).throw(ioctl_error),
            close_fn=closed.append,
            stat_fn=lambda path: SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=rdev),
        )

    assert caught.value is ioctl_error
    assert closed == [43]


def test_recording_sanitizes_probe_serial_and_host_paths_but_keeps_identity_change_facts(tmp_path: Path) -> None:
    helper = importlib.import_module(MODULE)
    value = helper.usb_reset_recording(
        serial_number=SERIAL,
        host_paths=(str(tmp_path), tmp_path.as_posix(), "/dev/bus/usb/001/007"),
        before={"busnum": 1, "devnum": 7, "major": 189, "minor": 6},
        after={"busnum": 1, "devnum": 8, "major": 189, "minor": 7},
        reset_confirmed=True,
        uart_rebind_observed=False,
        source_commit="a" * 40,
        run_id="run-123",
    )

    assert value["schema"] == "agentic-hil.usb-reset-recording/v1"
    assert value["reset_confirmed"] is True
    assert value["uart_rebind_observed"] is False
    assert value["devnum_changed"] is True
    assert value["major_minor_changed"] is True
    assert SERIAL not in str(value)
    assert str(tmp_path) not in str(value)
    assert "/dev/bus/usb/001/007" not in str(value)


def test_boot_read_accumulates_fragments_until_the_demo_banner_is_complete() -> None:
    helper = importlib.import_module("tests.bench.usb_reset_reenumeration")
    complete = {
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

    class FakeMCP:
        def __init__(self):
            self.fragments = iter(("Hello ", "World"))
            self.calls = 0

        def call(self, name, arguments):
            assert name == "com_read"
            self.calls += 1
            return False, {**complete, "data": {"text": next(self.fragments)}}

    server = FakeMCP()

    assert helper.read_demo_boot(server, "uart", timeout_s=2.0) == "Hello World"
    assert server.calls == 2


def test_boot_read_stops_immediately_when_a_read_fails_the_continue_predicate() -> None:
    helper = importlib.import_module("tests.bench.usb_reset_reenumeration")

    class FakeMCP:
        calls = 0

        def call(self, name, arguments):
            self.calls += 1
            return True, {"ok": False, "error_type": "uart_read_failed", "cleanup_ok": False}

    server = FakeMCP()
    with pytest.raises(pytest.fail.Exception):
        helper.read_demo_boot(server, "uart", timeout_s=2.0)
    assert server.calls == 1
