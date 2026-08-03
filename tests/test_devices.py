"""Devices as a base class, and the run boundary that closes over MCP.

The tests here are about the promise in decision 0018 — every device a run names
is locked for the run's duration — reaching the agent path, and about the four
properties that make locking a *set* of devices safe rather than merely
plausible: hardware identity, up-front and complete acquisition, a fixed order,
and nothing left behind on a partial acquisition.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from conftest import write_config

from agentic_hil.bench import BenchMutex, DeviceBusyError
from agentic_hil.config import load_config
from agentic_hil.coordination import CoordinationError, HardwareCoordinator, can_resource, com_resource
from agentic_hil.devices import (
    CanDevice,
    DebuggerDevice,
    DeviceError,
    DeviceSet,
    UartDevice,
    can_device,
    config_devices,
    debugger_device,
    is_device_lock_key,
    resolve_devices,
    uart_device,
)
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

FOUR_PORTS = """com_ports:
  port_a:
    device: "COM_A"
    resource_id: "board-a"
  port_b:
    device: "COM_B"
    resource_id: "board-b"
  port_c:
    device: "COM_C"
    resource_id: "board-c"
  port_d:
    device: "COM_D"
    resource_id: "board-d"
"""

TWO_NAMES_ONE_PORT = """com_ports:
  dut_uart:
    device: "COM_TEST"
  bootloader_uart:
    device: "COM_TEST"
"""

# The real ST-Link shape: one USB unit that is both a debug probe and a virtual
# COM port. Only resource_id can say that they are one thing.
PROBE_AND_ITS_VCP_DEBUGGER = """debuggers:
  second_probe:
    type: stlink
    probe_id: "0669FF-VCP"
    resource_id: "usb-stlink-0669"
"""
PROBE_AND_ITS_VCP_PORT = """com_ports:
  probe_vcp:
    device: "COM_VCP"
    resource_id: "usb-stlink-0669"
"""


def config_for(workspace: Path, **kwargs):
    return load_config(str(write_config(workspace, **kwargs)))


def four_device_config(workspace: Path):
    return config_for(workspace, com_ports_yaml=FOUR_PORTS)


def four_devices(config) -> list:
    return [uart_device(config, name) for name in ("port_a", "port_b", "port_c", "port_d")]


# --- the class, and what each kind contributes ---------------------------


def test_the_dut_is_not_a_device(tmp_path: Path) -> None:
    """The class describes what drives the target, not the target."""
    config = config_for(tmp_path, com_ports_yaml=TWO_NAMES_ONE_PORT)

    kinds = {device.kind for device in config_devices(config)}

    assert kinds <= {"debugger", "uart", "can"}
    # The DUT is configured and named, and contributes no device and no lock.
    assert config.target.name == "example-target"
    assert not any(config.target.name in device.lock_key for device in config_devices(config))
    with pytest.raises(DeviceError) as excinfo:
        resolve_devices(config, [{"kind": "dut", "id": config.target.name}])
    assert excinfo.value.result["error_type"] == "invalid_argument"
    assert "not a device" in excinfo.value.result["summary"]


def test_every_device_kind_locks_a_physical_unit(tmp_path: Path) -> None:
    """A lock key that is not physical would serialize a bench, not protect a board."""
    config = config_for(
        tmp_path,
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n',
        can_buses_yaml='can_buses:\n  main:\n    adapter: peak\n    channel: "PCAN_USBBUS1"\n',
    )

    devices = config_devices(config)

    assert {device.kind for device in devices} == {"debugger", "uart", "can"}
    for device in devices:
        assert is_device_lock_key(device.lock_key), device.lock_key


def test_identity_comes_from_the_hardware_and_not_from_the_config_entry_name(tmp_path: Path) -> None:
    config = config_for(tmp_path, com_ports_yaml=TWO_NAMES_ONE_PORT)

    dut = uart_device(config, "dut_uart")
    bootloader = uart_device(config, "bootloader_uart")

    assert dut.config_id != bootloader.config_id
    assert dut.lock_key == bootloader.lock_key == "com:com_test"
    assert dut.identity_source == "device"
    # And the coordination layer derives the same key it always did.
    assert com_resource(config, "dut_uart") == dut.lock_key


def test_a_debugger_that_names_no_hardware_says_so(tmp_path: Path) -> None:
    """The one identity that cannot be derived from hardware.

    OpenOCD cannot enumerate probes, so an entry with neither resource_id nor
    probe_id has nothing but its toolchain to be named after. That is a real
    limitation and it is reported rather than dressed up as an identity."""
    unnamed = debugger_device(config_for(tmp_path / "unnamed"))
    named = debugger_device(config_for(tmp_path / "named", probe_id="0669FF"))

    assert unnamed.identity_source == "executable"
    assert unnamed.identity_warning is not None
    assert "probe_id" in unnamed.identity_warning
    assert named.identity_source == "probe_id"
    assert named.identity_warning is None
    assert named.lock_key == "probe:0669ff"


def test_the_execution_shape_is_the_same_for_debugger_uart_and_can(tmp_path: Path) -> None:
    """One call for every kind, with whatever arguments that kind takes."""
    config = config_for(
        tmp_path,
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n',
        can_buses_yaml='can_buses:\n  main:\n    adapter: peak\n    channel: "PCAN_USBBUS1"\n',
    )
    seen: list[tuple[str, dict]] = []

    class Recorder:
        def __init__(self) -> None:
            self.config = config

        def call(self, name: str, arguments: dict | None = None) -> dict:
            seen.append((name, dict(arguments or {})))
            return {"ok": True, "tool": name}

    service = Recorder()
    debugger_device(config).execute(service, "flash", {"image_path": "build/app.bin", "reset_after_flash": True})
    uart_device(config, "dut_uart").execute(service, "write", {"text": "ping\n"})
    can_device(config, "main").execute(service, "send", {"frame_id": 291, "data_hex": "01"})

    assert seen == [
        ("flash_firmware", {"image_path": "build/app.bin", "reset_after_flash": True}),
        ("com_write", {"text": "ping\n", "port_id": "dut_uart"}),
        ("can_send", {"frame_id": 291, "data_hex": "01", "bus_id": "main"}),
    ]
    # The device supplies its own name, so an action cannot be redirected at
    # another entry by passing a different id.
    redirected = uart_device(config, "dut_uart").execute(service, "write", {"port_id": "somewhere_else", "text": "x"})
    assert redirected["error_type"] == "invalid_argument"
    unknown = can_device(config, "main").execute(service, "flash", {})
    assert unknown["error_type"] == "invalid_argument"
    assert "flash" in unknown["summary"]


def test_a_debugger_action_is_refused_by_a_service_bound_to_another_probe(tmp_path: Path) -> None:
    """No debugger tool schema carries a probe name, so a wrong binding would
    silently drive the wrong board."""
    config = config_for(tmp_path, debuggers_yaml=PROBE_AND_ITS_VCP_DEBUGGER)

    class Recorder:
        def __init__(self) -> None:
            self.config = config

        def call(self, name: str, arguments: dict | None = None) -> dict:  # pragma: no cover - must not run
            raise AssertionError("a call reached a service bound to another probe")

    result = debugger_device(config, "second_probe").execute(Recorder(), "reset", {"mode": "halt"})

    assert result["ok"] is False
    assert result["error_type"] == "not_supported"
    assert result["device"]["id"] == "second_probe"


# --- identity: two config entries, one physical unit ---------------------


def test_two_config_entries_on_one_physical_unit_collapse_to_one_lock(tmp_path: Path) -> None:
    """A board reachable under two names is still one board.

    Locking per config entry would take the same unit twice, and — worse — would
    let a second run believe a board is free because it asked under the other
    name."""
    config = config_for(tmp_path, com_ports_yaml=TWO_NAMES_ONE_PORT)

    declared = DeviceSet.of([uart_device(config, "dut_uart"), uart_device(config, "bootloader_uart")])

    assert len(declared) == 1
    assert declared.lock_keys == ["com:com_test"]

    coordinator = HardwareCoordinator(config, "owner")
    started = coordinator.begin_run(declared, label="two-names")
    try:
        assert started["declared_devices"] == ["com:com_test"]
        # Both names are inside the declaration, so neither is undeclared.
        coordinator.acquire(com_resource(config, "dut_uart")).release()
        coordinator.acquire(com_resource(config, "bootloader_uart")).release()
        stranger = BenchMutex(frontend="stranger")
        with pytest.raises(DeviceBusyError):
            stranger.acquire(["com:com_test"])
    finally:
        coordinator.end_run()
        coordinator.close()


def test_a_probe_and_its_virtual_com_port_are_one_lock(tmp_path: Path) -> None:
    """Across kinds, too: only resource_id can say two entries are one unit."""
    config = config_for(tmp_path, debuggers_yaml=PROBE_AND_ITS_VCP_DEBUGGER, com_ports_yaml=PROBE_AND_ITS_VCP_PORT)

    probe = debugger_device(config, "second_probe")
    vcp = uart_device(config, "probe_vcp")

    assert probe.lock_key == vcp.lock_key == "physical:usb-stlink-0669"
    assert DeviceSet.of([probe, vcp]).lock_keys == ["physical:usb-stlink-0669"]


def test_a_declaration_keeps_only_devices_and_the_order_is_the_key_order(tmp_path: Path) -> None:
    config = four_device_config(tmp_path)
    devices = four_devices(config)

    forward = DeviceSet.of(devices)
    reversed_declaration = DeviceSet.of(list(reversed(devices)))

    assert forward.lock_keys == reversed_declaration.lock_keys == sorted(forward.lock_keys)


# --- acquisition: up front, complete, ordered, unwound -------------------


def test_a_held_device_fails_the_run_immediately_and_names_the_holder(tmp_path: Path) -> None:
    """No partial acquisition and no silent wait for the rest."""
    config = four_device_config(tmp_path)
    stranger = BenchMutex(frontend="stranger", label="other-bench-session")
    stranger.acquire(["physical:board-b"])
    coordinator = HardwareCoordinator(config, "owner")
    try:
        started = time.monotonic()
        with pytest.raises(CoordinationError) as excinfo:
            coordinator.begin_run(DeviceSet.of(four_devices(config)), label="mine")
        waited = time.monotonic() - started

        result = excinfo.value.result
        assert result["error_type"] == "device_busy"
        assert result["resource"] == "physical:board-b"
        assert result["holder"]["label"] == "other-bench-session"
        assert result["holder"]["frontend"] == "stranger"
        assert result["side_effect_committed"] is False
        assert result["declared_devices"] == ["physical:board-a", "physical:board-b", "physical:board-c", "physical:board-d"]
        # Immediately: a wait happens only when the caller asked for one.
        assert waited < 2.0
        assert coordinator.run_active is False
        assert coordinator.bench.held_resources() == frozenset()
    finally:
        coordinator.close()
        stranger.release_all()


def test_a_failure_on_the_third_of_four_leaves_none_held(tmp_path: Path) -> None:
    """The first two are given back before the error returns.

    A run that keeps what it managed to take is how two runs end up each holding
    half of what the other needs."""
    config = four_device_config(tmp_path)
    stranger = BenchMutex(frontend="stranger", label="holds-the-third")
    stranger.acquire(["physical:board-c"])
    coordinator = HardwareCoordinator(config, "owner")
    try:
        with pytest.raises(CoordinationError) as excinfo:
            coordinator.begin_run(DeviceSet.of(four_devices(config)), label="four-devices")

        assert excinfo.value.result["resource"] == "physical:board-c"
        assert coordinator.bench.held_resources() == frozenset()
        # And they are genuinely free, not merely forgotten by this owner.
        successor = BenchMutex(frontend="successor")
        successor.acquire(["physical:board-a", "physical:board-b", "physical:board-d"])
        successor.release_all()
    finally:
        coordinator.close()
        stranger.release_all()


def test_two_runs_with_overlapping_devices_and_reversed_order_do_not_deadlock(tmp_path: Path) -> None:
    """Declaration order must not survive into acquisition order.

    Without a fixed order the first run takes board-a and waits for board-b while
    the second holds board-b and waits for board-a, and neither ever moves. Both
    runs below declare the same two devices in opposite order."""
    config = four_device_config(tmp_path)
    first = HardwareCoordinator(config_for(tmp_path / "first"), "first")
    second = HardwareCoordinator(config_for(tmp_path / "second"), "second")
    forward = [uart_device(config, "port_a"), uart_device(config, "port_b")]
    start = threading.Barrier(2, timeout=30)
    outcomes: dict[str, str] = {}
    errors: dict[str, str] = {}

    def run(name: str, coordinator: HardwareCoordinator, declaration: list) -> None:
        try:
            start.wait()
            coordinator.begin_run(DeviceSet.of(declaration), label=name, wait_s=20)
            try:
                assert coordinator.bench.held_resources() == frozenset({"physical:board-a", "physical:board-b"})
                time.sleep(0.05)
            finally:
                coordinator.end_run()
            outcomes[name] = "held both, then released"
        except BaseException as error:  # pragma: no cover - only on a real deadlock
            errors[name] = f"{type(error).__name__}: {error}"

    threads = [
        threading.Thread(target=run, args=("forward", first, forward)),
        threading.Thread(target=run, args=("reversed", second, list(reversed(forward)))),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not any(thread.is_alive() for thread in threads), "a run never finished; the two deadlocked"
    finally:
        for coordinator in (first, second):
            coordinator.close()

    assert errors == {}
    assert set(outcomes) == {"forward", "reversed"}


def test_a_device_whose_key_the_mutex_would_not_lock_refuses_the_run(tmp_path: Path) -> None:
    """Silently not locking a declared board is worse than refusing the run.

    BenchMutex only locks physical resource names and ignores the rest, which is
    right for the host-wide probe enumeration and would be catastrophic for a
    device: the run would believe it held a board it never took."""
    config = four_device_config(tmp_path)

    class Ungrounded(UartDevice):
        @property
        def lock_key(self) -> str:
            return "project:not-a-device"

    port = uart_device(config, "port_a")
    declared = DeviceSet.of([Ungrounded(config_id=port.config_id, port=port.port)])
    coordinator = HardwareCoordinator(config, "owner")
    try:
        with pytest.raises(CoordinationError) as excinfo:
            coordinator.begin_run(declared)

        assert excinfo.value.result["error_type"] == "coordination_state_invalid"
        assert excinfo.value.result["unlockable_lock_keys"] == ["project:not-a-device"]
        assert coordinator.run_active is False
        assert coordinator.bench.held_resources() == frozenset()
    finally:
        coordinator.close()


def test_a_run_declared_with_bare_resource_names_still_works(tmp_path: Path) -> None:
    """The reactor and the existing suite declare names; devices are additive."""
    coordinator = HardwareCoordinator(four_device_config(tmp_path), "owner")
    try:
        started = coordinator.begin_run(["physical:board-b", "physical:board-a"], label="names-only")

        assert started["declared_devices"] == ["physical:board-a", "physical:board-b"]
        assert "devices" not in started
    finally:
        coordinator.end_run()
        coordinator.close()


def test_a_run_is_refused_before_it_holds_anything_when_a_device_is_unknown(tmp_path: Path) -> None:
    """Resolution finishes before acquisition starts."""
    config = four_device_config(tmp_path)
    service = AgenticHILToolService(config, frontend="mcp")
    try:
        result = service.call(
            "bench_run_start",
            {"devices": [{"kind": "uart", "id": "port_a"}, {"kind": "uart", "id": "port_b"}, {"kind": "uart", "id": "nonexistent"}]},
        )

        assert result["ok"] is False
        assert result["error_type"] == "unknown_device"
        assert result["id"] == "nonexistent"
        assert service.coordinator.bench.held_resources() == frozenset()
        assert service.coordinator.run_active is False
    finally:
        service.close()


# --- the run boundary over MCP -------------------------------------------


def mcp_call(service: AgenticHILToolService, name: str, arguments: dict | None = None) -> dict:
    response = handle_mcp_message(
        {"jsonrpc": "2.0", "id": name, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}},
        service,
    )
    assert isinstance(response, dict)
    return response["result"]["structuredContent"]


def test_an_agent_driven_run_holds_its_devices_across_several_mcp_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gap this issue closes.

    A call took its device per call, so flash, reset and read were three runs and
    the board was free between them. Declared once, the devices stay held from
    before the first call to after the last."""
    config = config_for(tmp_path, config_version=2)
    service = AgenticHILToolService(config, frontend="mcp")
    monkeypatch.setattr(service.backend, "probe_target", lambda: {"ok": True, "tool": "probe_target", "target_detected": True})
    probe_key = debugger_device(config).lock_key
    stranger = BenchMutex(frontend="stranger")
    try:
        started = mcp_call(service, "bench_run_start", {"devices": [{"kind": "debugger"}], "label": "boot-smoke"})
        assert started["ok"] is True
        assert started["declared_devices"] == [probe_key]
        assert started["run_label"] == "boot-smoke"

        for _ in range(3):
            assert mcp_call(service, "probe_target")["ok"] is True
            # Between two calls of the run the board is still this run's.
            with pytest.raises(DeviceBusyError) as excinfo:
                stranger.acquire([probe_key])
            assert excinfo.value.result["holder"]["label"] == "boot-smoke"

        status = mcp_call(service, "bench_run_status")
        assert status["run_active"] is True
        assert status["declared_devices"] == [probe_key]
        assert status["devices"][0]["kind"] == "debugger"

        stopped = mcp_call(service, "bench_run_stop")
        assert stopped["released_devices"] == [probe_key]
        assert mcp_call(service, "bench_run_status")["run_active"] is False
        # Only now is the board free again.
        stranger.acquire([probe_key])
        stranger.release_all()
    finally:
        service.close()


def test_a_run_over_mcp_may_touch_only_what_it_declared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path, config_version=2, com_ports_yaml=TWO_NAMES_ONE_PORT)
    service = AgenticHILToolService(config, frontend="mcp")
    monkeypatch.setattr(service.backend, "probe_target", lambda: {"ok": True, "tool": "probe_target", "target_detected": True})
    try:
        assert mcp_call(service, "bench_run_start", {"devices": [{"kind": "uart", "id": "dut_uart"}]})["ok"] is True

        refused = mcp_call(service, "probe_target")

        assert refused["ok"] is False
        assert refused["error_type"] == "undeclared_device"
        assert refused["declared_devices"] == ["com:com_test"]
    finally:
        mcp_call(service, "bench_run_stop")
        service.close()


def test_a_second_run_over_mcp_names_the_one_that_is_open(tmp_path: Path) -> None:
    config = four_device_config(tmp_path)
    service = AgenticHILToolService(config, frontend="mcp")
    try:
        mcp_call(service, "bench_run_start", {"devices": [{"kind": "uart", "id": "port_a"}], "label": "first-run"})

        second = mcp_call(service, "bench_run_start", {"devices": [{"kind": "uart", "id": "port_b"}], "label": "second-run"})

        assert second["error_type"] == "run_already_active"
        assert second["run_label"] == "first-run"
        assert second["declared_devices"] == ["physical:board-a"]
        # The refused declaration took nothing.
        assert service.coordinator.bench.held_resources() == frozenset({"physical:board-a"})
    finally:
        mcp_call(service, "bench_run_stop")
        service.close()


def test_stopping_a_run_that_is_not_open_answers_instead_of_refusing(tmp_path: Path) -> None:
    """An agent that lost track of its own state needs a way back, not a refusal."""
    service = AgenticHILToolService(four_device_config(tmp_path), frontend="mcp")
    try:
        result = mcp_call(service, "bench_run_stop")

        assert result["ok"] is True
        assert result["run_was_active"] is False
        assert result["released_devices"] == []
    finally:
        service.close()


def test_a_lost_client_releases_the_run_when_the_service_closes(tmp_path: Path) -> None:
    """stdin reaching EOF is what a disconnected client looks like here.

    The stdio server closes its service on EOF, and closing releases an open run,
    so a client that walks away does not hold the bench until the process is
    reaped. If the process itself dies, the operating system drops the lock —
    that is the layer below this one."""
    config = four_device_config(tmp_path)
    service = AgenticHILToolService(config, frontend="mcp")
    mcp_call(service, "bench_run_start", {"devices": [{"kind": "uart", "id": "port_a"}], "label": "abandoned"})
    assert service.coordinator.bench.held_resources() == frozenset({"physical:board-a"})

    service.close()

    assert service.coordinator.run_active is False
    successor = BenchMutex(frontend="successor")
    successor.acquire(["physical:board-a"])
    successor.release_all()


def test_a_run_declaration_reports_a_device_whose_identity_is_not_hardware(tmp_path: Path) -> None:
    config = config_for(tmp_path, config_version=2)
    service = AgenticHILToolService(config, frontend="mcp")
    try:
        started = mcp_call(service, "bench_run_start", {"devices": [{"kind": "debugger"}]})

        assert started["ok"] is True
        assert started["devices"][0]["identity_source"] == "executable"
        assert any("probe_id" in warning for warning in started["warnings"])
    finally:
        mcp_call(service, "bench_run_stop")
        service.close()


def test_ending_a_run_says_when_a_session_still_holds_a_device(tmp_path: Path) -> None:
    """A live COM or CAN session keeps its own hold; the run's end must not
    read as a release that did not happen."""
    config = four_device_config(tmp_path)
    coordinator = HardwareCoordinator(config, "owner")
    try:
        coordinator.begin_run(DeviceSet.of([uart_device(config, "port_a")]), label="session-run")
        lease = coordinator.acquire("physical:board-a")

        ended = coordinator.end_run()

        assert ended["open_leases"] == [lease.lease_id]
        assert ended["still_held_devices"] == ["physical:board-a"]
        stranger = BenchMutex(frontend="stranger")
        with pytest.raises(DeviceBusyError):
            stranger.acquire(["physical:board-a"])
        lease.release()
    finally:
        coordinator.close()


# --- the coordination layer derives no identity of its own ---------------


def test_the_coordination_helpers_and_the_devices_agree(tmp_path: Path) -> None:
    config = config_for(
        tmp_path,
        probe_id="0669FF",
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n',
        can_buses_yaml='can_buses:\n  main:\n    adapter: peak\n    channel: "PCAN_USBBUS1"\n',
    )

    assert com_resource(config, "dut_uart") == uart_device(config, "dut_uart").lock_key
    assert can_resource(config, "main") == can_device(config, "main").lock_key
    assert isinstance(debugger_device(config), DebuggerDevice)
    assert isinstance(uart_device(config, "dut_uart"), UartDevice)
    assert isinstance(can_device(config, "main"), CanDevice)


def test_config_validation_and_the_device_layer_derive_the_same_probe_identity(tmp_path: Path) -> None:
    """Config load refuses two debuggers that resolve to one lock, using its own
    copy of the derivation. The two must not drift."""
    from agentic_hil.config import debugger_resource_identity

    for kwargs in ({}, {"probe_id": "0669FF"}):
        config = config_for(tmp_path / f"case{len(kwargs)}", **kwargs)
        assert debugger_resource_identity(config.debugger) == debugger_device(config).lock_key
