"""The machine-wide device mutex that replaced the read permission.

Every test here is about the property the permission used to provide: while one
run holds a board, nothing else on the machine reaches it — no matter which
configuration, state_root, or process the other side came from.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from conftest import write_config

from agentic_hil.bench import BenchMutex, DeviceBusyError, device_lock_root, is_physical_resource, resource_digest
from agentic_hil.config import ConfigError, load_config
from agentic_hil.coordination import DEBUGGER_DISCOVERY_RESOURCE, CoordinationError, HardwareCoordinator

BOARD = "physical:bench-board"


def config_for(workspace: Path, **kwargs):
    return load_config(str(write_config(workspace, **kwargs)))


def child_environment(state_root: Path) -> dict[str, str]:
    """A child that shares this machine but not this configuration.

    Different state_root is the whole point: on 2026-08-02 that difference made
    two live sessions invisible to one another."""
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(state_root)
    environment["XDG_STATE_HOME"] = str(state_root)
    dependency_root = str(Path(yaml.__file__).resolve().parents[1])
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join([dependency_root, source_root, environment.get("PYTHONPATH", "")])
    return environment


def wait_for_file(path: Path, child: subprocess.Popen, timeout_s: float = 20) -> bool:
    deadline = time.monotonic() + timeout_s
    while not path.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    return path.exists()


def holder_pid(ready: Path) -> int:
    """The pid that actually holds the lock.

    On Windows a venv's ``python.exe`` re-launches the real interpreter, so
    Popen's pid is a redirector and not the owner; the child reports its own."""
    for _ in range(200):
        text = ready.read_text(encoding="utf-8").strip()
        if text:
            return int(text)
        time.sleep(0.02)
    raise AssertionError("child never reported its pid")


def test_device_lock_lives_outside_state_root_and_under_the_user_home(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    root = device_lock_root()

    assert root == Path(os.path.expanduser("~")) / ".agentic-hil" / "device-locks"
    # A lock kept per configuration is not a bench lock: state_root differs
    # between sessions, and that is exactly how two owners missed each other.
    assert Path(config.state_root) not in root.parents
    assert root != Path(config.state_root)


def test_only_physical_resources_are_locked_machine_wide() -> None:
    assert is_physical_resource(BOARD)
    assert is_physical_resource("probe:0669FF")
    assert is_physical_resource("com:COM7")
    assert is_physical_resource("can:peak:PCAN_USBBUS1")
    # Locking a configuration or a host-wide enumeration machine-wide would
    # serialize unrelated benches instead of protecting a board.
    assert not is_physical_resource("project:abc")
    assert not is_physical_resource(DEBUGGER_DISCOVERY_RESOURCE)


def test_second_owner_is_refused_and_the_refusal_names_the_holder() -> None:
    first = BenchMutex(frontend="first", label="smoke-plan")
    second = BenchMutex(frontend="second")
    first.acquire([BOARD])
    try:
        with pytest.raises(DeviceBusyError) as excinfo:
            second.acquire([BOARD])
        result = excinfo.value.result
        assert result["error_type"] == "device_busy"
        assert result["resource"] == BOARD
        assert result["holder"]["pid"] == os.getpid()
        assert result["holder"]["frontend"] == "first"
        assert result["holder"]["label"] == "smoke-plan"
        assert "smoke-plan" in result["summary"]
        # The holder's ownership token is not part of what a stranger is told.
        assert "owner_marker" not in result["holder"]
    finally:
        first.release_all()


def test_acquire_is_all_or_nothing_over_the_declared_set() -> None:
    other = "physical:bench-board-two"
    first = BenchMutex(frontend="first")
    second = BenchMutex(frontend="second")
    first.acquire([other])
    try:
        with pytest.raises(DeviceBusyError):
            second.acquire([BOARD, other])
        # The board it did get must not stay held by a refused acquisition.
        assert second.held_resources() == frozenset()
        third = BenchMutex(frontend="third")
        third.acquire([BOARD])
        third.release_all()
    finally:
        first.release_all()


def test_waiting_happens_only_when_it_was_asked_for_and_stays_bounded() -> None:
    first = BenchMutex(frontend="first")
    second = BenchMutex(frontend="second")
    first.acquire([BOARD])
    try:
        started = time.monotonic()
        with pytest.raises(DeviceBusyError) as excinfo:
            second.acquire([BOARD], wait_s=0.5)
        waited = time.monotonic() - started
        assert waited >= 0.4
        assert excinfo.value.result["waited_s"] >= 0.4
        with pytest.raises(ConfigError) as invalid:
            second.acquire([BOARD], wait_s=-1)
        assert invalid.value.error_type == "invalid_argument"
        with pytest.raises(ConfigError) as unbounded:
            second.acquire([BOARD], wait_s=10_000)
        assert unbounded.value.error_type == "invalid_argument"
    finally:
        first.release_all()


def test_a_released_device_is_free_again() -> None:
    first = BenchMutex(frontend="first")
    second = BenchMutex(frontend="second")
    first.acquire([BOARD])
    first.release([BOARD])
    second.acquire([BOARD])
    try:
        assert second.held_resources() == frozenset({BOARD})
        assert second.reclaimed(BOARD) is None
    finally:
        second.release_all()


def test_a_run_holds_its_devices_across_every_call_inside_it(tmp_path: Path) -> None:
    """The lease per call was never exclusivity: it ended with the call.

    Between two steps of a run there used to be no lock at all, which is the gap
    an outside observation slipped into."""
    owner = HardwareCoordinator(config_for(tmp_path / "owner"), "owner")
    stranger = BenchMutex(frontend="stranger")
    owner.begin_run([BOARD], label="plan-a")
    try:
        lease = owner.acquire(BOARD)
        lease.release()
        # The call is over. Under the per-call lease the board was free here.
        with pytest.raises(DeviceBusyError) as excinfo:
            stranger.acquire([BOARD])
        assert excinfo.value.result["holder"]["label"] == "plan-a"
    finally:
        owner.end_run()
        owner.close()
    stranger.acquire([BOARD])
    stranger.release_all()


def test_a_run_refuses_a_device_its_description_does_not_name(tmp_path: Path) -> None:
    owner = HardwareCoordinator(config_for(tmp_path / "owner"), "owner")
    owner.begin_run([BOARD], label="plan-a")
    try:
        with pytest.raises(CoordinationError) as excinfo:
            owner.acquire("physical:undeclared-board")
        result = excinfo.value.result
        assert result["error_type"] == "undeclared_device"
        assert result["undeclared_devices"] == ["physical:undeclared-board"]
        assert result["declared_devices"] == [BOARD]
        assert result["retry_safe"] is False
        # A declared device still works, and the pseudo-resources a run never
        # declares (host-wide probe enumeration) stay reachable.
        owner.acquire(BOARD).release()
        owner.acquire(DEBUGGER_DISCOVERY_RESOURCE).release()
    finally:
        owner.end_run()
        owner.close()


def test_two_state_roots_on_one_board_see_each_other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 2026-08-02 incident, as a test.

    Two live sessions held the same Nucleo and neither could see the other,
    because the lock was kept beside each session's own state."""
    config_path = write_config(tmp_path)
    ready = tmp_path / "child-ready"
    stop = tmp_path / "child-stop"
    script = """
import os, sys, time
from pathlib import Path
from agentic_hil.config import load_config
from agentic_hil.coordination import HardwareCoordinator
config = load_config(sys.argv[1])
coordinator = HardwareCoordinator(config, 'child')
coordinator.begin_run(['physical:bench-board'], label='child-plan')
Path(sys.argv[2]).write_text(str(os.getpid()), encoding='utf-8')
while not Path(sys.argv[3]).exists():
    time.sleep(0.02)
coordinator.end_run()
coordinator.close()
"""
    environment = child_environment(tmp_path / "child-state")
    child = subprocess.Popen([sys.executable, "-c", script, str(config_path), str(ready), str(stop)], env=environment)
    try:
        assert wait_for_file(ready, child), "child did not open its run"
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "parent-state"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "parent-state"))
        coordinator = HardwareCoordinator(load_config(str(config_path)), "parent")
        with pytest.raises(CoordinationError) as excinfo:
            coordinator.acquire(BOARD)
        result = excinfo.value.result
        assert result["error_type"] == "device_busy"
        assert result["holder"]["pid"] == holder_pid(ready)
        assert result["holder"]["label"] == "child-plan"
        assert result["side_effect_committed"] is False
    finally:
        stop.write_text("stop", encoding="utf-8")
        child.wait(timeout=20)


def test_a_killed_run_frees_its_devices_without_operator_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crashed run must not block the bench.

    Quarantine plus `recover --confirm-safe-state` did exactly that on
    2026-08-02. Ownership is the operating system's lock, so the kernel hands the
    board back the moment the owner dies."""
    config_path = write_config(tmp_path)
    ready = tmp_path / "child-ready"
    script = """
import os, sys, time
from pathlib import Path
from agentic_hil.config import load_config
from agentic_hil.coordination import HardwareCoordinator
config = load_config(sys.argv[1])
coordinator = HardwareCoordinator(config, 'child')
coordinator.begin_run(['physical:bench-board'], label='doomed-plan')
Path(sys.argv[2]).write_text(str(os.getpid()), encoding='utf-8')
time.sleep(600)
"""
    environment = child_environment(tmp_path / "child-state")
    child = subprocess.Popen([sys.executable, "-c", script, str(config_path), str(ready)], env=environment)
    try:
        assert wait_for_file(ready, child), "child did not open its run"
        # Killed outright: no finally, no release, no chance to write anything.
        os.kill(holder_pid(ready), getattr(signal, "SIGKILL", signal.SIGTERM))
        child.wait(timeout=20)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=20)

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "parent-state"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "parent-state"))
    coordinator = HardwareCoordinator(load_config(str(config_path)), "parent")
    started = coordinator.begin_run([BOARD], label="next-plan")
    try:
        assert started["ok"] is True
        # And it says so: a silently reused board hides that a run died on it.
        assert started["reclaimed"][0]["reason"] == "owner_process_exited_without_release"
        assert started["reclaimed"][0]["owner"]["label"] == "doomed-plan"
    finally:
        coordinator.end_run()
        coordinator.close()


def test_a_second_run_on_the_same_owner_is_refused(tmp_path: Path) -> None:
    coordinator = HardwareCoordinator(config_for(tmp_path), "owner")
    coordinator.begin_run([BOARD], label="plan-a")
    try:
        with pytest.raises(CoordinationError) as excinfo:
            coordinator.begin_run(["physical:other"], label="plan-b")
        assert excinfo.value.result["error_type"] == "run_already_active"
    finally:
        coordinator.end_run()
        coordinator.close()


def test_a_run_must_declare_something(tmp_path: Path) -> None:
    coordinator = HardwareCoordinator(config_for(tmp_path), "owner")
    try:
        with pytest.raises(CoordinationError) as excinfo:
            coordinator.begin_run([DEBUGGER_DISCOVERY_RESOURCE])
        assert excinfo.value.result["error_type"] == "invalid_argument"
    finally:
        coordinator.close()


def test_the_holder_record_names_the_device_it_belongs_to() -> None:
    mutex = BenchMutex(frontend="first", label="plan")
    mutex.acquire([BOARD])
    try:
        record = mutex.holder(BOARD)
        assert record is not None
        assert record["state"] == "held"
        assert record["resource"] == BOARD
        assert (device_lock_root() / f"{resource_digest(BOARD)}.lock").is_file()
    finally:
        mutex.release_all()
    released = mutex.holder(BOARD)
    assert released is not None
    assert released["state"] == "released"
