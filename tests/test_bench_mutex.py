"""The machine-wide device mutex that replaced the read permission.

Every test here is about the property the permission used to provide: while one
run holds a board, nothing else on the machine reaches it, no matter which
configuration, state_root, or process the other side came from.
"""
from __future__ import annotations

import ast
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from conftest import write_config
from support import (
    PUBLISH_ATOMICALLY_SOURCE,
    process_is_running,
    publish_atomically,
    published,
    read_when_published,
    scaled_time_bound,
)
from test_test_reactor import RecordingService

from agentic_hil import bench as bench_module
from agentic_hil.bench import BenchMutex, DeviceBusyError, device_lock_root, is_physical_resource, resource_digest
from agentic_hil.config import ConfigError, load_config
from agentic_hil.coordination import DEBUGGER_DISCOVERY_RESOURCE, CoordinationError, HardwareCoordinator
from agentic_hil.test_reactor import TestReactor, load_test_config

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
    """Wait until the child has published `path`, or until it dies trying.

    The wait is for the value rather than for the name: this file carries the
    child's pid, and a name that is there is not yet a pid that is there unless
    the writer renamed it into place (issue #395)."""
    deadline = time.monotonic() + timeout_s
    while not published(path) and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    return published(path)


def holder_pid(ready: Path) -> int:
    """The pid that actually holds the lock.

    On Windows a venv's ``python.exe`` re-launches the real interpreter, so
    Popen's pid is a redirector and not the owner; the child reports its own."""
    return int(read_when_published(ready))


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


def test_a_refused_acquire_gives_back_a_device_its_owner_already_held() -> None:
    """All or nothing covers a device the owner held before the call.

    An acquire naming a device its owner already holds counts that hold once
    more, and the matching release counts it down again. Refused on a later
    device, the acquire has no release to come, so the count it added goes
    back with the refusal; kept, it outlives every release the owner makes."""
    other = "physical:bench-board-two"
    first = BenchMutex(frontend="first")
    second = BenchMutex(frontend="second")
    first.acquire([other])
    second.acquire([BOARD])
    try:
        with pytest.raises(DeviceBusyError):
            second.acquire([BOARD, other])
        second.release([BOARD])
        assert second.held_resources() == frozenset()
    finally:
        second.release_all()
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
    script = (
        PUBLISH_ATOMICALLY_SOURCE
        + """
import os, sys, time
from pathlib import Path
from agentic_hil.config import load_config
from agentic_hil.coordination import HardwareCoordinator
config = load_config(sys.argv[1])
coordinator = HardwareCoordinator(config, 'child')
coordinator.begin_run(['physical:bench-board'], label='child-plan')
publish_atomically(sys.argv[2], str(os.getpid()))
while not Path(sys.argv[3]).exists():
    time.sleep(0.02)
coordinator.end_run()
coordinator.close()
"""
    )
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
        publish_atomically(str(stop), "stop")
        child.wait(timeout=scaled_time_bound(20))


def test_a_killed_run_frees_its_devices_without_operator_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crashed run must not block the bench.

    Quarantine plus `recover --confirm-safe-state` did exactly that on
    2026-08-02. Ownership is the operating system's lock, so the kernel hands the
    board back the moment the owner dies."""
    config_path = write_config(tmp_path)
    ready = tmp_path / "child-ready"
    script = (
        PUBLISH_ATOMICALLY_SOURCE
        + """
import os, sys, time
from agentic_hil.config import load_config
from agentic_hil.coordination import HardwareCoordinator
config = load_config(sys.argv[1])
coordinator = HardwareCoordinator(config, 'child')
coordinator.begin_run(['physical:bench-board'], label='doomed-plan')
publish_atomically(sys.argv[2], str(os.getpid()))
time.sleep(600)
"""
    )
    environment = child_environment(tmp_path / "child-state")
    child = subprocess.Popen([sys.executable, "-c", script, str(config_path), str(ready)], env=environment)
    try:
        assert wait_for_file(ready, child), "child did not open its run"
        # Killed outright: no finally, no release, no chance to write anything.
        os.kill(holder_pid(ready), getattr(signal, "SIGKILL", signal.SIGTERM))
        child.wait(timeout=scaled_time_bound(20))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=scaled_time_bound(20))

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


PROBE_ONLY_PLAN = """version: 2
name: probe-plan
steps:
  - debugger: dut
    action: flash
    image_path: build/app.bin
"""


def write_plan(workspace: Path, text: str = PROBE_ONLY_PLAN) -> Path:
    path = workspace / ".agentic-hil" / "testconfig.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_the_plan_is_what_the_run_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.test_reactor import declared_devices, load_test_config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = load_config(str(write_config(workspace, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    monkeypatch.chdir(workspace)
    plan = load_test_config(
        str(
            write_plan(
                workspace,
                """version: 2
name: two-devices
steps:
  - port_id: dut_uart
    action: uart_open
  - debugger: dut
    action: flash
    image_path: build/app.bin
  - port_id: dut_uart
    action: uart_close
""",
            )
        ),
        config.work_dir,
    )

    devices = declared_devices(config, plan)

    # One entry per physical device, not per step: the mutex locks boards. The
    # UART named by two steps is a single lock, and the executable-identified
    # debugger is two names rather than two devices: its `probe-exe:` key and the
    # legacy `probe:` twin it also holds so an unupgraded process cannot take it
    # out from under this run. Two devices, three lock names.
    assert sum(1 for item in devices if item.startswith("com:")) == 1
    assert len(devices) == 3
    assert any(item.startswith("com:") or item.startswith("physical:") for item in devices)


def test_a_reactor_run_is_refused_while_a_stranger_holds_a_declared_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal an operator gets when the bench is already in use.

    Nothing runs, and the result names who has the board."""
    from conftest import write_authoritative_config

    from agentic_hil.cli import run_test_reactor
    from agentic_hil.config import load_authoritative_config
    from agentic_hil.test_reactor import declared_devices, load_test_config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_authoritative_config(workspace, monkeypatch)
    monkeypatch.chdir(workspace)
    (workspace / "build").mkdir(exist_ok=True)
    (workspace / "build" / "app.bin").write_bytes(b"\x00" * 16)
    plan_path = write_plan(workspace)
    config = load_authoritative_config(workspace)
    devices = declared_devices(config, load_test_config(str(plan_path), config.work_dir))
    assert devices

    stranger = BenchMutex(frontend="stranger", label="other-bench-session")
    stranger.acquire(devices)
    try:
        result = run_test_reactor(str(plan_path))
    finally:
        stranger.release_all()

    assert result["ok"] is False
    assert result["error_type"] == "device_busy"
    assert result["holder"]["label"] == "other-bench-session"
    assert result["steps"] == []
    assert result["side_effect_committed"] is False


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


@pytest.mark.parametrize("read_busy_result", [False, True])
def test_a_holder_read_retries_a_heartbeat_replacement(monkeypatch: pytest.MonkeyPatch, read_busy_result: bool) -> None:
    owner = BenchMutex(frontend="owner", label="refreshing-run")
    contender = BenchMutex(frontend="contender")
    owner.acquire([BOARD])
    read_text = bench_module.safe_read_text
    attempts = 0

    def replaced_once(path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            owner.heartbeat()
            raise ConfigError("unsafe_configured_path", "Configured file changed while it was being opened.")
        return read_text(path)

    monkeypatch.setattr(bench_module, "safe_read_text", replaced_once)
    try:
        result = contender.busy_result(BOARD) if read_busy_result else contender.holder(BOARD)
        assert result is not None
        assert result["holder" if read_busy_result else "owner"]["label"] == "refreshing-run"
        assert attempts == 2
    finally:
        owner.release_all()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits replacing a file while its old inode is open")
def test_an_atomic_heartbeat_between_open_and_stat_keeps_the_holder(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil import config as config_module

    owner = BenchMutex(frontend="owner", label="refreshing-run")
    contender = BenchMutex(frontend="contender")
    owner.acquire([BOARD])
    validate = config_module._validate_open_file
    replaced = False

    def replace_after_validation(descriptor, path):
        nonlocal replaced
        opened = validate(descriptor, path)
        if path == owner._holder_path(BOARD) and not replaced:
            replaced = True
            owner.heartbeat()
        return opened

    monkeypatch.setattr(config_module, "_validate_open_file", replace_after_validation)
    try:
        result = contender.busy_result(BOARD)
        assert replaced
        assert result["holder"]["label"] == "refreshing-run", result
        assert result["heartbeat_at"] is not None
    finally:
        owner.release_all()


def test_retrying_a_holder_read_still_refuses_a_hard_link(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = BenchMutex(frontend="owner", label="unsafe-record")
    contender = BenchMutex(frontend="contender")
    monkeypatch.setattr(owner, "_ensure_heartbeat_pump", lambda: None)
    owner.acquire([BOARD])
    # Beside the record, not in tmp_path: a hard link cannot leave its
    # filesystem, and where /tmp is a tmpfs (Ubuntu 26.04) the sandbox home
    # under it and the basetemp are two devices. Nothing in the lock directory
    # is found by listing it, so a stray name there is never read.
    link = owner._holder_path(BOARD).with_name("holder-link.json")
    read_text = bench_module.safe_read_text
    attempts = 0

    def counted_read(path):
        nonlocal attempts
        attempts += 1
        assert attempts <= 3, "a permanently unsafe holder record must not loop"
        return read_text(path)

    try:
        os.link(owner._holder_path(BOARD), link)
        monkeypatch.setattr(bench_module, "safe_read_text", counted_read)
        assert "holder" not in contender.busy_result(BOARD)
        assert attempts > 0
    finally:
        link.unlink(missing_ok=True)
        owner.release_all()


# ---------------------------------------------------------------------------
# The heartbeat of a live run (#489).
#
# A holder record's `heartbeat_at` was written at `begin_run` and at each lease
# acquire and never again. A `delay`, a long `uart_expect` or a `repeat` block
# takes no lease, so after the stale window every `device_busy` refusal against
# a perfectly live run carried `holder_heartbeat_stale: true`, and the error
# catalogue told the operator that holder is hung and to stop that process. The
# run has to keep beating for as long as its process is alive, on a schedule of
# its own rather than on the accident of the next lease.


# How old a live holder's heartbeat may read to a contender. The step here
# takes two seconds and beats every interval, so a record written during it
# is a few tenths of a second old; a record last written at `begin_run` is
# the whole step old and fails this. The base is the claim and does not
# move. What a loaded host adds on top of it comes from
# `AGENTIC_HIL_TEST_TIME_SCALE` through `scaled_time_bound`, because a run
# that read 1.324 s here had a perfectly live heartbeat and a busy machine
# under it (#515).
FRESH_HEARTBEAT_AGE_S = 1.0


def _heartbeat_time(record: dict) -> datetime:
    return datetime.fromisoformat(str(record["heartbeat_at"]).replace("Z", "+00:00"))


def _contender_refusal(resource: str) -> dict:
    stranger = BenchMutex(frontend="stranger")
    with pytest.raises(DeviceBusyError) as excinfo:
        stranger.acquire([resource])
    return excinfo.value.result


def _contender_run_refusal(tmp_path: Path) -> dict:
    """What an agent's `bench_run_start` reads: a second owner, on its own
    configuration of the same machine, declaring the same board through the
    coordinator. The refusal is the mutex's busy result as the coordinator
    hands it on, heartbeat fields included."""
    contender = HardwareCoordinator(config_for(tmp_path / "contender"), "contender")
    try:
        with pytest.raises(CoordinationError) as excinfo:
            contender.begin_run([BOARD], label="contender")
    finally:
        contender.close()
    return excinfo.value.result


QUIET_CAN_BUS_YAML = 'can_buses:\n  dut_can:\n    adapter: "socketcan"\n    channel: "can0"\n    bitrate: 500000\n'


def _run_plan_inside_a_declared_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plan_text: str, **config_kwargs) -> tuple[dict, dict, dict, dict]:
    """Run one plan while a real run holds BOARD, and return the holder record
    before, the holder record after, a stranger's refusal off the mutex after,
    and a contender's refusal through the coordinator after.

    The reactor is driven through RecordingService, which never touches the
    coordinator, so no accidental lease acquire can refresh the record: what
    moves it has to be the run's own beat."""
    monkeypatch.setattr(bench_module, "HEARTBEAT_INTERVAL_S", 0.2)
    config = config_for(tmp_path, **config_kwargs)
    plan_path = tmp_path / ".agentic-hil" / "testconfig.yaml"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(plan_text, encoding="utf-8")
    coordinator = HardwareCoordinator(config, "owner")
    service = RecordingService()
    coordinator.begin_run([BOARD], label="long-step")
    try:
        before = coordinator.bench.holder(BOARD)
        assert before is not None and before["state"] == "held"
        TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]
        after = coordinator.bench.holder(BOARD)
        assert after is not None
        refusal = _contender_refusal(BOARD)
        run_refusal = _contender_run_refusal(tmp_path)
    finally:
        coordinator.end_run()
        coordinator.close()
    return before, after, refusal, run_refusal


def _assert_the_holder_read_as_live(before: dict, after: dict, refusal: dict) -> None:
    # The step took no lease, and the record moved anyway.
    assert _heartbeat_time(after) > _heartbeat_time(before), (before["heartbeat_at"], after["heartbeat_at"])
    # What a contender reads: a holder that is busy, not hung. The age is the
    # tooth: a record last written at begin_run is two seconds old here, one
    # written during the step is at most a few intervals old. The stale flag
    # cannot trip inside two seconds (its window is never under a minute), so
    # it is pinned in the hung-holder test, not here.
    assert refusal["error_type"] == "device_busy"
    assert refusal["holder"]["label"] == "long-step"
    assert refusal["heartbeat_age_s"] < scaled_time_bound(FRESH_HEARTBEAT_AGE_S), refusal
    assert refusal.get("holder_heartbeat_stale") is not True, refusal


def test_a_live_run_in_a_long_delay_keeps_its_heartbeat_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before, after, refusal, run_refusal = _run_plan_inside_a_declared_run(
        tmp_path,
        monkeypatch,
        "version: 3\nname: long-delay\nsteps:\n  - {device: dut, action: delay, duration_ms: 2000}\n",
    )

    _assert_the_holder_read_as_live(before, after, refusal)
    # And through the coordinator, which is what `bench_run_start` answers with:
    # the same record, the same age, and no hung verdict.
    assert run_refusal["error_type"] == "device_busy"
    assert run_refusal["holder"]["label"] == "long-step"
    assert _heartbeat_time(run_refusal) >= _heartbeat_time(after)
    assert run_refusal["heartbeat_age_s"] < scaled_time_bound(FRESH_HEARTBEAT_AGE_S), run_refusal
    assert run_refusal.get("holder_heartbeat_stale") is not True, run_refusal


def test_a_live_run_in_a_long_uart_expect_keeps_its_heartbeat_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A silent port: the expect waits out its whole timeout, and the run is
    # alive for every millisecond of it.
    before, after, refusal, _ = _run_plan_inside_a_declared_run(
        tmp_path,
        monkeypatch,
        "version: 3\nname: long-expect\nsteps:\n"
        "  - {device: dut_uart, action: uart_open}\n"
        '  - {device: dut_uart, action: uart_expect, text: "never printed", timeout_s: 2}\n'
        "  - {device: dut_uart, action: uart_close}\n",
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n',
    )

    _assert_the_holder_read_as_live(before, after, refusal)


def test_a_live_run_waiting_on_a_quiet_can_bus_keeps_its_heartbeat_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A quiet bus: the comparator polls `can_read` until its deadline, and the
    # frame it waits for never comes. The loop is the reactor's own idle poll,
    # so a beat that hung off one step kind would miss this one.
    before, after, refusal, _ = _run_plan_inside_a_declared_run(
        tmp_path,
        monkeypatch,
        "version: 3\nname: quiet-bus\nsteps:\n"
        "  - {device: dut_can, action: can_open}\n"
        '  - {device: dut_can, action: can_read, comparator: {id: 1, equals: "01"}, timeout_s: 2}\n'
        "  - {device: dut_can, action: can_close}\n",
        can_buses_yaml=QUIET_CAN_BUS_YAML,
    )

    _assert_the_holder_read_as_live(before, after, refusal)


def test_a_live_run_inside_a_repeat_block_keeps_its_heartbeat_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The step the issue names: a bounded loop of short waits, none of which
    # takes a lease, adding up to a long stretch the run is alive for.
    before, after, refusal, _ = _run_plan_inside_a_declared_run(
        tmp_path,
        monkeypatch,
        "version: 4\nname: long-repeat\nsteps:\n  - {action: repeat, count: 4, steps: [{device: dut, action: delay, duration_ms: 500}]}\n",
    )

    _assert_the_holder_read_as_live(before, after, refusal)


def test_a_hold_survives_a_heartbeat_pump_that_cannot_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refresh is best effort like the record it writes: a host that cannot
    start one more thread (`Thread.start` raises RuntimeError) still holds the
    device exclusively, still names itself to a contender, and still releases.
    Only the freshness of the record is lost."""
    import threading

    def cannot_start(thread: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    mutex = BenchMutex(frontend="cramped")
    mutex.acquire([BOARD])
    try:
        assert mutex.holds(BOARD)
        record = mutex.holder(BOARD)
        assert record is not None and record["state"] == "held"
        assert _contender_refusal(BOARD)["holder"]["frontend"] == "cramped"
    finally:
        mutex.release([BOARD])
    released = mutex.holder(BOARD)
    assert released is not None and released["state"] == "released"


def test_a_holder_that_stopped_heartbeating_still_reads_as_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction, pinned so the fix cannot be a contender that stopped
    looking: a holder whose refreshes no longer reach the disk is exactly the
    hung process the catalogue describes, and it must still read as stale once
    its last written heartbeat is older than the window."""
    monkeypatch.setattr(bench_module, "HEARTBEAT_INTERVAL_S", 0.2)
    coordinator = HardwareCoordinator(config_for(tmp_path), "owner")
    coordinator.begin_run([BOARD], label="hung-plan")
    hung = True
    original_write = coordinator.bench._write_holder

    def write_holder(resource: str, state: str, *, released: bool = False) -> None:
        if hung:
            raise OSError("the holder is hung and its refresh never lands")
        original_write(resource, state, released=released)

    try:
        monkeypatch.setattr(coordinator.bench, "_write_holder", write_holder)
        # Any refresh already in flight lands before the record is backdated.
        time.sleep(0.3)
        record = coordinator.bench.holder(BOARD)
        assert record is not None
        stale_at = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        coordinator.bench._holder_path(BOARD).write_text(json.dumps({**record, "heartbeat_at": stale_at}), encoding="utf-8")
        # Three intervals in which a live holder would have written.
        time.sleep(0.6)

        refusal = _contender_refusal(BOARD)

        assert refusal["holder"]["label"] == "hung-plan"
        assert refusal["heartbeat_age_s"] >= 299.0, refusal
        assert refusal["holder_heartbeat_stale"] is True, refusal
    finally:
        hung = False
        coordinator.end_run()
        coordinator.close()


# A pending set must not monopolize its free members or stop an existing hold's
# heartbeat (#880). The child signals from the real poll sleep, not from startup.
MUTEX_WAIT_CHILD_SOURCE = PUBLISH_ATOMICALLY_SOURCE + """
import json
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

directory = Path(sys.argv[1])
mode = sys.argv[2]
wait_s = float(sys.argv[3])
handshake_s = float(sys.argv[4])
publish_atomically(str(directory / 'actual-pid'), str(os.getpid()))

from agentic_hil import bench

owner = bench.BenchMutex(root=directory / 'locks', frontend='child', label=mode)
if mode == 'crashed':
    owner.acquire(['com:a'])
    # No release or record rewrite: the kernel alone frees the lock.
    os._exit(0)
if mode == 'availability':
    # Diagnostic metadata is deterministic; actual-pid above is the real process.
    owner.owner = replace(owner.owner, pid=880, host='issue-880-test',
                          frontend='waiter', label='waiting-set')
else:
    bench.HEARTBEAT_INTERVAL_S = 0.1
    owner.acquire(['com:a'])

real_sleep = time.sleep
waiting_thread = threading.get_ident()
announced = False
in_wait = False
deadline = time.monotonic() + wait_s

def announce_wait(seconds):
    global announced
    if in_wait and threading.get_ident() == waiting_thread and not announced:
        announced = True
        publish_atomically(str(directory / 'waiting'), json.dumps({'deadline': deadline}))
    real_sleep(seconds)

try:
    time.sleep = announce_wait
    in_wait = True
    try:
        if mode == 'named':
            taken = owner.acquire_named('com:b', wait_s=wait_s)
        else:
            taken = owner.acquire(['com:a', 'com:b'], wait_s=wait_s)
    finally:
        in_wait = False
        time.sleep = real_sleep
    publish_atomically(str(directory / 'acquired'),
                       json.dumps({'taken': taken, 'held': sorted(owner.held_resources())}))
    release_deadline = time.monotonic() + handshake_s
    while not (directory / 'release').exists():
        if time.monotonic() >= release_deadline:
            raise AssertionError('parent did not acknowledge the acquired mutex set')
        real_sleep(0.02)
    owner.release(['com:b'] if mode == 'named' else ['com:a', 'com:b'])
    after_request = sorted(owner.held_resources())
    if mode != 'availability':
        owner.release(['com:a'])
    publish_atomically(str(directory / 'released'),
                       json.dumps({'after_request': after_request,
                                   'final': sorted(owner.held_resources())}))
finally:
    owner.release_all()
"""


@contextmanager
def _mutex_wait_child(directory: Path, mode: str) -> Iterator[subprocess.Popen]:
    """Own the real child even when a Windows venv launches a redirector."""
    actual_pid: int | None = None
    with (directory / "child.log").open("w", encoding="utf-8") as log:
        child = subprocess.Popen(
            [sys.executable, "-c", MUTEX_WAIT_CHILD_SOURCE, str(directory), mode,
             str(min(scaled_time_bound(60), 900.0)), str(scaled_time_bound(20))],
            env=child_environment(directory / "child-state"),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _mutex_child_publication(directory, child, "actual-pid")
            actual_pid = holder_pid(directory / "actual-pid")
            yield child
        finally:
            if child.poll() is None:
                # Never use the synthetic holder pid for lifecycle handling.
                if actual_pid is None and published(directory / "actual-pid"):
                    actual_pid = holder_pid(directory / "actual-pid")
                if actual_pid is not None:
                    with suppress(ProcessLookupError):
                        os.kill(actual_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                else:
                    child.kill()
            child.wait(timeout=scaled_time_bound(20))
            if os.name == "nt" and actual_pid is not None:
                assert not process_is_running(actual_pid), "mutex helper survived its redirector"


def _mutex_child_publication(directory: Path, child: subprocess.Popen, name: str) -> str:
    path = directory / name
    if not wait_for_file(path, child, timeout_s=scaled_time_bound(20)):
        output = (directory / "child.log").read_text(encoding="utf-8")
        raise AssertionError(f"mutex helper did not publish {name}: exit={child.poll()}\n{output}")
    return read_when_published(path)


def _assert_mutex_child_pending(directory: Path, child: subprocess.Popen, deadline: float) -> None:
    assert child.poll() is None, "mutex helper exited before the blocking device was released"
    assert not published(directory / "acquired"), "mutex helper acquired a device held by the blocker"
    assert time.monotonic() < deadline, "mutex helper exhausted its wait before the observation"


def _release_mutex_child(directory: Path, child: subprocess.Popen) -> dict:
    publish_atomically(str(directory / "release"), "release")
    result = json.loads(_mutex_child_publication(directory, child, "released"))
    assert child.wait(timeout=scaled_time_bound(20)) == 0
    return result


def test_waiting_set_releases_partial_locks_and_then_acquires_all(tmp_path: Path) -> None:
    root = tmp_path / "locks"
    root.mkdir()
    blocker = BenchMutex(root=root, frontend="blocker")
    contender = BenchMutex(root=root, frontend="contender")
    try:
        blocker.acquire(["com:b"])
        with _mutex_wait_child(tmp_path, "availability") as child:
            marker = json.loads(_mutex_child_publication(tmp_path, child, "waiting"))
            deadline = marker["deadline"]
            _assert_mutex_child_pending(tmp_path, child, deadline)
            assert time.monotonic() + scaled_time_bound(3) < deadline
            # Unhandled on the old product: this is its real busy refusal.
            assert contender.acquire(["com:a"], wait_s=scaled_time_bound(3)) == ["com:a"]
            assert contender.held_resources() == frozenset({"com:a"})
            assert blocker.holds("com:b")
            _assert_mutex_child_pending(tmp_path, child, deadline)
            contender.release(["com:a"])
            blocker.release(["com:b"])
            acquired = json.loads(_mutex_child_publication(tmp_path, child, "acquired"))
            assert acquired == {"taken": ["com:a", "com:b"], "held": ["com:a", "com:b"]}
            for resource in ("com:a", "com:b"):
                with pytest.raises(DeviceBusyError):
                    contender.acquire([resource])
            assert _release_mutex_child(tmp_path, child) == {"after_request": [], "final": []}
        assert contender.acquire(["com:a", "com:b"]) == ["com:a", "com:b"]
    finally:
        contender.release_all()
        blocker.release_all()


def test_failed_set_attempt_preserves_holder_record_and_reclamation(tmp_path: Path) -> None:
    root = tmp_path / "locks"
    root.mkdir()
    with _mutex_wait_child(tmp_path, "crashed") as child:
        assert child.wait(timeout=scaled_time_bound(20)) == 0
    requester = BenchMutex(root=root, frontend="requester")
    blocker = BenchMutex(root=root, frontend="blocker")
    observer = BenchMutex(root=root, frontend="observer")
    try:
        original = requester.holder("com:a")
        assert original is not None and original["state"] == "held"
        holder_path = root / f"{resource_digest('com:a')}.holder.json"
        original_bytes = holder_path.read_bytes()
        blocker.acquire(["com:b"])
        with pytest.raises(DeviceBusyError) as refusal:
            requester.acquire(["com:a", "com:b"], wait_s=scaled_time_bound(0.3))
        assert refusal.value.result["resource"] == "com:b"
        assert refusal.value.result["holder"] == blocker.owner.as_json()
        assert requester.held_resources() == frozenset()
        after = requester.holder("com:a")
        if after != original or holder_path.read_bytes() != original_bytes:
            raise AssertionError("failed acquisition changed the earlier holder record")
        blocker.release(["com:b"])
        assert requester.acquire(["com:a", "com:b"]) == ["com:a", "com:b"]
        reclaimed = requester.reclaimed("com:a")
        assert reclaimed is not None
        assert reclaimed["owner"] == original["owner"]
        assert reclaimed["reason"] == "owner_process_exited_without_release"
        requester.release(["com:a", "com:b"])
        assert observer.acquire(["com:a", "com:b"]) == ["com:a", "com:b"]
    finally:
        observer.release_all()
        requester.release_all()
        blocker.release_all()


@pytest.mark.parametrize("mode", ["set", "named"], ids=["set", "named"])
def test_existing_hold_heartbeats_while_owner_waits(tmp_path: Path, mode: str) -> None:
    root = tmp_path / "locks"
    root.mkdir()
    blocker = BenchMutex(root=root, frontend="blocker")
    observer = BenchMutex(root=root, frontend="observer")
    try:
        blocker.acquire(["com:b"])
        with _mutex_wait_child(tmp_path, mode) as child:
            marker = json.loads(_mutex_child_publication(tmp_path, child, "waiting"))
            deadline = marker["deadline"]
            _assert_mutex_child_pending(tmp_path, child, deadline)
            baseline = observer.holder("com:a")
            assert baseline is not None and baseline["state"] == "held"
            last_heartbeat = _heartbeat_time(baseline)
            advances = 0
            observation_deadline = time.monotonic() + scaled_time_bound(5)
            assert observation_deadline < deadline
            while advances < 2 and time.monotonic() < observation_deadline:
                _assert_mutex_child_pending(tmp_path, child, deadline)
                assert blocker.holds("com:b")
                record = observer.holder("com:a")
                if record is not None:
                    heartbeat = _heartbeat_time(record)
                    if heartbeat > last_heartbeat:
                        advances += 1
                        last_heartbeat = heartbeat
                if advances < 2:
                    time.sleep(0.02)
            _assert_mutex_child_pending(tmp_path, child, deadline)
            if advances < 2:
                raise AssertionError("held device heartbeat did not advance twice during acquisition wait")
            blocker.release(["com:b"])
            acquired = json.loads(_mutex_child_publication(tmp_path, child, "acquired"))
            assert acquired["held"] == ["com:a", "com:b"]
            if mode == "set":
                assert acquired["taken"] == ["com:b"]
            else:
                assert acquired["taken"] is True
            assert _release_mutex_child(tmp_path, child) == {"after_request": ["com:a"], "final": []}
        assert observer.acquire(["com:a", "com:b"]) == ["com:a", "com:b"]
    finally:
        observer.release_all()
        blocker.release_all()


def test_bench_run_start_docstring_describes_rollback_before_waiting() -> None:
    source_path = Path(__file__).resolve().parents[1] / "src" / "agentic_hil" / "tools.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    service = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgenticHILToolService")
    method = next(node for node in service.body if isinstance(node, ast.FunctionDef) and node.name == "bench_run_start")
    text = " ".join((ast.get_docstring(method) or "").lower().split())
    sentences = text.split(".")
    rollback_before_wait = any(
        re.search(r"\b(partial|incomplete|tentative)\b", sentence)
        and re.search(r"\b(releas\w*|drop\w*|given back|gives? back)\b", sentence)
        and re.search(r"\b(before|prior to)\b.*\b(wait\w*|retry\w*|retries)\b", sentence)
        for sentence in sentences
    )
    complete_before_run = any(
        re.search(r"\b(whole|complete|entire|every|all)\b", sentence)
        and re.search(r"\b(set|devices|locks)\b", sentence)
        and re.search(r"\b(only after|only when|once|until|after)\b", sentence)
        and re.search(r"\b(acquir\w*|secur\w*|taken|obtain\w*|succeed\w*)\b", sentence)
        and re.search(r"\b(run|start\w*|begin\w*|retain\w*|hold\w*)\b", sentence)
        for sentence in sentences
    )
    never_held = re.search(r"\bnever held (its |the )?first two\b", text)
    if never_held or not rollback_before_wait or not complete_before_run:
        raise AssertionError("bench_run_start docstring does not describe rollback before waiting")
