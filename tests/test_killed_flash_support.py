"""Offline contracts for what the killed-flash bench test reports when it fails.

Two questions the test in tests/bench/test_bench_faults.py has to answer for
itself, because the board does not answer them twice: why the recovery the
aborted flash ran stopped (#621), and whether the in-circuit debugger's serial
port was removed and created again under the run (#620).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tests.bench.killed_flash_support import (
    debugger_logs_since,
    reaches_its_tty,
    recovery_report,
    reenumerated_fields,
    wait_for_the_serial_port,
)

DECISIVE = "Error: libusb_open() failed with LIBUSB_ERROR_NO_DEVICE"


def a_failed_recovery() -> dict:
    """The block #621 saw, with the fields pytest's own rendering cut off."""
    return {
        "attempted": True,
        "actions": ["reap_processes", "reset_halt"],
        "outcome": "failed",
        "devices": ["probe:[withheld]"],
        "auto_recover_policy": "reset_halt",
        "failed_action": "reset_halt",
        "failed_check": "ok",
        "summary": "The target did not confirm a reset into halt, so the bench stays as the failed run left it. " * 3,
    }


def write_log(directory: Path, name: str, *, stderr: str, returncode: int = 1, when: float | None = None) -> Path:
    path = directory / name
    path.write_text(
        json.dumps({"command": ["openocd", "-c", "reset halt"], "returncode": returncode, "timed_out": False, "stdout": "", "stderr": stderr}),
        encoding="utf-8",
    )
    if when is not None:
        os.utime(path, (when, when))
    return path


# -- #621: the whole recovery, and the reset's own line ------------------------


def test_the_report_carries_the_whole_recovery_block_untruncated() -> None:
    recovery = a_failed_recovery()

    report = recovery_report(recovery, [])

    assert json.dumps(recovery, indent=1, sort_keys=True) in report
    assert recovery["summary"] in report
    assert '"failed_action": "reset_halt"' in report


def test_the_report_names_the_resets_own_result_and_its_decisive_line(tmp_path: Path) -> None:
    started = time.time()
    write_log(tmp_path, "openocd-1-flash_firmware.log", stderr="Info : erasing\n", returncode=-9, when=started + 1)
    write_log(tmp_path, "openocd-2-reset_target.log", stderr=f"Open On-Chip Debugger 0.11.0\n{DECISIVE}\nInfo : shutdown\n", when=started + 2)

    report = recovery_report(a_failed_recovery(), debugger_logs_since(tmp_path, started))

    assert "openocd-2-reset_target.log" in report
    assert "returncode 1" in report
    assert DECISIVE in report
    # The flash's own log is there too, after the kill, and comes first.
    assert report.index("openocd-1-flash_firmware.log") < report.index("openocd-2-reset_target.log")


def test_a_log_from_before_the_flash_is_not_this_recoverys(tmp_path: Path) -> None:
    started = time.time()
    write_log(tmp_path, "openocd-0-reset_target.log", stderr="Error: from an earlier test\n", when=started - 60)

    assert debugger_logs_since(tmp_path, started) == []


def test_an_unreadable_log_is_named_rather_than_dropped(tmp_path: Path) -> None:
    started = time.time()
    path = tmp_path / "openocd-3-reset_target.log"
    path.write_text("not json", encoding="utf-8")
    os.utime(path, (started + 1, started + 1))

    report = recovery_report(a_failed_recovery(), debugger_logs_since(tmp_path, started))

    assert "openocd-3-reset_target.log" in report
    assert "unreadable" in report


def test_no_logs_directory_is_said_rather_than_raised(tmp_path: Path) -> None:
    assert debugger_logs_since(tmp_path / "absent", time.time()) == []
    assert "no debugger log" in recovery_report(a_failed_recovery(), [])


# -- #620: the serial port removed and created again --------------------------


def snapshot(name: str = "ttyACM0", *, sysfs_inode: int = 11, node_inode: int = 21, minor: int = 0, gone: bool = False) -> dict:
    taken: dict = {
        "name": name,
        "sysfs_inode": None if gone else sysfs_inode,
        "sysfs_link_inode": None if gone else sysfs_inode + 100,
        "node_inode": node_inode,
        "node_mode": 0o20660,
        "node_rdev": minor,
        "major": 166,
        "minor": minor,
    }
    if gone:
        taken["sysfs_stat_error"] = {"error_type": "FileNotFoundError", "errno": 2}
    return taken


def tty_class(root: Path, **devices: str) -> Path:
    for name, number in devices.items():
        (root / name).mkdir(parents=True)
        (root / name / "dev").write_text(number + "\n", encoding="ascii")
    return root


def test_the_same_port_is_not_reenumerated() -> None:
    assert reenumerated_fields(snapshot(), snapshot()) == []


def test_a_port_whose_sysfs_entry_was_created_again_is_reenumerated() -> None:
    assert "sysfs_inode" in reenumerated_fields(snapshot(), snapshot(sysfs_inode=12))


def test_a_port_whose_sysfs_entry_is_gone_is_reenumerated() -> None:
    assert reenumerated_fields(snapshot(), snapshot(gone=True))


def test_a_node_reaches_its_tty_only_through_the_number_sysfs_gives_it(tmp_path: Path) -> None:
    classes = tty_class(tmp_path, ttyACM0="166:0", ttyACM1="166:1")

    assert reaches_its_tty(snapshot(), classes) is True
    assert reaches_its_tty(snapshot(minor=1), classes) is False
    assert reaches_its_tty(snapshot("ttyACM1", minor=1), classes) is True
    assert reaches_its_tty(snapshot(gone=True), classes) is False


def clock_and_sleep() -> tuple:
    now = [0.0]
    return (lambda: now[0]), (lambda seconds: now.__setitem__(0, now[0] + seconds))


def test_a_port_that_was_never_reenumerated_needs_no_wait(tmp_path: Path) -> None:
    clock, sleep = clock_and_sleep()
    taken = []

    def take() -> dict:
        taken.append(1)
        return snapshot()

    said = wait_for_the_serial_port(snapshot(), take, tty_class(tmp_path, ttyACM0="166:0"), clock=clock, sleep=sleep)

    assert said is None
    assert len(taken) == 1


def test_a_reenumerated_port_this_run_still_reaches_is_followed(tmp_path: Path) -> None:
    """Under a live device tree the stable name leads to the new node, and the test goes on."""
    clock, sleep = clock_and_sleep()
    answers = iter([snapshot(gone=True), snapshot("ttyACM1", sysfs_inode=13, node_inode=23, minor=1)])

    said = wait_for_the_serial_port(snapshot(), lambda: next(answers), tty_class(tmp_path, ttyACM1="166:1"), clock=clock, sleep=sleep)

    assert said is None


def test_a_reenumerated_port_this_run_cannot_reach_is_named_for_what_it_is(tmp_path: Path) -> None:
    """A fixed --device binding keeps the node the run started with: the failure names the re-enumeration, not the product."""
    clock, sleep = clock_and_sleep()

    said = wait_for_the_serial_port(snapshot(), lambda: snapshot(gone=True), tty_class(tmp_path, ttyACM1="166:1"), timeout_s=5.0, clock=clock, sleep=sleep)

    assert said is not None
    assert "re-enumerated" in said
    assert "ttyACM0" in said
    assert "--live-device-tree" in said
    assert clock() >= 5.0


def test_the_wait_bounds_must_be_positive() -> None:
    with pytest.raises(ValueError):
        wait_for_the_serial_port(snapshot(), snapshot, Path("."), timeout_s=0)
