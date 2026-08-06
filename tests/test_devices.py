"""Devices as a base class, and the run boundary that closes over MCP.

The tests here are about the promise in decision 0018 — every device a run names
is locked for the run's duration — reaching the agent path, and about the four
properties that make locking a *set* of devices safe rather than merely
plausible: hardware identity, up-front and complete acquisition, a fixed order,
and nothing left behind on a partial acquisition.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import write_config

from agentic_hil.bench import BenchMutex, DeviceBusyError
from agentic_hil.config import ConfigError, load_config
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
    lock_keys,
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

# The key TWO_NAMES_ONE_PORT produces. A serial device is a host path, so its
# case rule is the host's: derived here rather than spelled out, because what
# the tests below pin is the collapse of two names to one key, not the platform
# they happen to run on. The case rules themselves are pinned, without deferring
# to the host, under "identity folds case the same way everywhere" — a test that
# passes on one platform for the wrong reason is what let this drift in.
ONE_PORT_KEY = f"com:{os.path.normcase('COM_TEST')}"

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
    assert dut.lock_key == bootloader.lock_key == ONE_PORT_KEY
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
    # A serial is an opaque hardware id, so this literal holds on every platform.
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


def test_the_mutex_is_reachable_from_the_device_itself(tmp_path: Path) -> None:
    """The base class carries the lock, so no backend has to spell one."""
    config = four_device_config(tmp_path)
    device = uart_device(config, "port_a")
    bench = BenchMutex(frontend="owner", label="single-device")
    contender = BenchMutex(frontend="contender")
    try:
        assert device.is_held(bench) is False
        assert device.acquire(bench) is True
        # Re-taking a device this owner holds is a no-op, not a second lock.
        assert device.acquire(bench) is False
        assert device.is_held(bench) is True
        assert device.holder(bench)["owner"]["label"] == "single-device"
        with pytest.raises(DeviceBusyError):
            device.acquire(contender)
    finally:
        bench.release_all()
    assert device.is_held(bench) is False


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
    assert declared.lock_keys == [ONE_PORT_KEY]

    coordinator = HardwareCoordinator(config, "owner")
    started = coordinator.begin_run(declared, label="two-names")
    try:
        assert started["declared_devices"] == [ONE_PORT_KEY]
        # Both names are inside the declaration, so neither is undeclared.
        coordinator.acquire(com_resource(config, "dut_uart")).release()
        coordinator.acquire(com_resource(config, "bootloader_uart")).release()
        stranger = BenchMutex(frontend="stranger")
        with pytest.raises(DeviceBusyError):
            stranger.acquire([ONE_PORT_KEY])
    finally:
        coordinator.end_run()
        coordinator.close()


def test_a_probe_and_its_virtual_com_port_are_one_lock(tmp_path: Path) -> None:
    """Across kinds, too: only resource_id can say two entries are one unit.

    The two entries collapse onto one primary key — the shared `resource_id` —
    so the set is one lock's worth of identity, not two. The probe half also
    names a serial, and the collapsed unit keeps it: `resource_id` is this
    operator's own alias, but the serial is what another workspace's first `init`
    derives from enumerating the same ST-Link, and holding both is what makes the
    two collide there rather than each connect believing it is alone
    (hardci-hq#108)."""
    config = config_for(tmp_path, debuggers_yaml=PROBE_AND_ITS_VCP_DEBUGGER, com_ports_yaml=PROBE_AND_ITS_VCP_PORT)

    probe = debugger_device(config, "second_probe")
    vcp = uart_device(config, "probe_vcp")

    # One identity: the primary key is the shared resource_id on both halves.
    assert probe.lock_key == vcp.lock_key == "physical:usb-stlink-0669"
    # One unit, held under every name either half of it answers to — and the
    # serial survives whichever order the two are declared in, so a plan naming
    # the port first cannot drop the probe's serial.
    assert DeviceSet.of([probe, vcp]).lock_keys == ["physical:usb-stlink-0669", "probe:0669ff-vcp"]
    assert DeviceSet.of([vcp, probe]).lock_keys == ["physical:usb-stlink-0669", "probe:0669ff-vcp"]


# --- identity folds case the same way everywhere -------------------------
#
# The rules these pin are deliberately asserted without asking the host what it
# would do, because os.path.normcase() folds on Windows and does nothing on
# POSIX: a suite that lets the platform supply the expectation agrees with the
# code on both and notices nothing when the two disagree with each other.


def test_an_opaque_hardware_id_folds_case_on_every_platform(tmp_path: Path) -> None:
    """A probe serial, a resource_id and a CAN channel name hardware, not a file.

    They name a unit that is one board wherever the bench runs, and two config
    entries that spell one of them in two cases must collapse to one lock on
    Windows and on Linux alike. Preserving case on POSIX only would leave the
    same bench with two different exclusivity guarantees and no error on either:
    two runs would each believe they held the board."""
    upper = config_for(tmp_path / "upper", probe_id="0669FF", can_buses_yaml='can_buses:\n  main:\n    adapter: peak\n    channel: "PCAN_USBBUS1"\n')
    lower = config_for(tmp_path / "lower", probe_id="0669ff", can_buses_yaml='can_buses:\n  main:\n    adapter: peak\n    channel: "pcan_usbbus1"\n')

    # No platform branch: both must hold on every host this suite runs on.
    assert debugger_device(upper).lock_key == debugger_device(lower).lock_key == "probe:0669ff"
    assert can_device(upper, "main").lock_key == can_device(lower, "main").lock_key == "can:peak:pcan_usbbus1"

    mixed = config_for(tmp_path / "mixed", com_ports_yaml='com_ports:\n  vcp:\n    device: "COM_VCP"\n    resource_id: "USB-STLink-0669"\n')
    assert uart_device(mixed, "vcp").lock_key == "physical:usb-stlink-0669"
    for device in (debugger_device(upper), can_device(upper, "main"), uart_device(mixed, "vcp")):
        assert device.lock_key == device.lock_key.casefold(), device.lock_key


def test_a_path_like_device_value_folds_the_way_its_own_host_does(tmp_path: Path) -> None:
    """A serial device name and a debugger executable are host paths.

    Here the platform dependence is the correct answer rather than an accident:
    COM7 and com7 open one port on Windows, and /dev/ttyACM0 and /dev/ttyacm0
    are two different devices on Linux. The expectation comes from which host
    this is, not from the normalisation the code under test happens to call."""
    upper = config_for(tmp_path / "upper", com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')
    lower = config_for(tmp_path / "lower", com_ports_yaml='com_ports:\n  dut_uart:\n    device: "com_test"\n')

    one_name_on_this_host = os.name == "nt"

    assert (uart_device(upper, "dut_uart").lock_key == uart_device(lower, "dut_uart").lock_key) is one_name_on_this_host
    assert uart_device(lower, "dut_uart").lock_key == "com:com_test"
    assert uart_device(upper, "dut_uart").lock_key == ("com:com_test" if one_name_on_this_host else "com:COM_TEST")

    # The debugger's executable fallback is the other path-like component, and
    # it is reached only when the entry names no hardware at all.
    toolchain = replace(config_for(tmp_path / "exe").debugger, probe_id=None, resource_id=None)
    as_written = DebuggerDevice(config_id="dut", debugger=replace(toolchain, executable="C:/Tools/OpenOCD/OpenOCD.exe"))
    as_lowercased = DebuggerDevice(config_id="dut", debugger=replace(toolchain, executable="C:/Tools/openocd/openocd.exe"))

    assert as_written.identity_source == "executable"
    assert (as_written.lock_key == as_lowercased.lock_key) is one_name_on_this_host
    # It keeps a prefix of its own so `probe:` can stay a hardware id that folds
    # case everywhere (see the probe-serial test below). The executable is the
    # one path-like fallback, and this is where that shows.
    assert as_written.lock_key.startswith("probe-exe:")


def test_a_hand_written_name_reaches_the_same_lock_as_the_device_it_names(tmp_path: Path) -> None:
    """The folding above belongs to the lock, not to `Device`.

    `HardwareCoordinator.acquire` takes devices *or* resource names, and only
    the device went through `lock_key`. An unfolded name is a second lock file
    for one board: the run holds `physical:board-a`, a caller asking for
    `physical:BOARD-A` is told it is free, and both then believe they have the
    board. A `resource_id` is an opaque hardware id, so this one has no platform
    branch — the two spellings are one board on every host."""
    config = config_for(tmp_path, com_ports_yaml=FOUR_PORTS)
    device = uart_device(config, "port_a")
    assert device.lock_key == "physical:board-a"

    coordinator = HardwareCoordinator(config, "owner")
    try:
        coordinator.begin_run(DeviceSet.of([device]), label="one-board")
        # Declared as a device and reached under a name somebody spelled: the run
        # has to recognise its own board rather than refuse it as undeclared.
        lease = coordinator.acquire("physical:BOARD-A")
        try:
            assert lease.resources == [device.lock_key]
        finally:
            lease.release()
        # And the machine-wide hold answers to either spelling. This is the lock
        # file that two keys would have split in two.
        stranger = BenchMutex(frontend="stranger")
        with pytest.raises(DeviceBusyError):
            stranger.acquire(["physical:BOARD-A"])
    finally:
        coordinator.end_run()
        coordinator.close()

    # The issue's own example. A serial device name is a host path rather than an
    # opaque id, so whether two spellings are one port stays the host's rule (see
    # the test above); what changed is that a hand-written name now follows that
    # rule instead of keeping a spelling of its own.
    ports = config_for(tmp_path / "com9", com_ports_yaml='com_ports:\n  dut_uart:\n    device: "com9"\n')
    assert uart_device(ports, "dut_uart").lock_key == "com:com9"
    assert (lock_keys(["com:COM9"]) == ["com:com9"]) is (os.name == "nt")


def test_a_hand_written_probe_serial_reaches_the_same_lock_on_every_host(tmp_path: Path) -> None:
    """A probe serial is an opaque hardware id, so it folds case on POSIX too.

    hardci-hq#106: `fold_resource_name` treated every `probe:` name as a host
    path, and a host path is left untouched on POSIX. A configured
    `probe_id: STLINK123` locks `probe:stlink123` — `DebuggerDevice.lock_key`
    builds it with `fold_hardware_id`, which casefolds everywhere — while a
    hand-written `acquire("probe:STLINK123")` stayed uppercase and split into a
    second lock file for one probe. Unlike a serial device name, a probe serial
    has no per-host reading: it is one probe on Windows and on Linux alike, so
    this assertion carries no platform branch. The executable fallback keeps its
    own `probe-exe:` prefix precisely so `probe:` can fold this way."""
    config = config_for(tmp_path, debuggers_yaml='debuggers:\n  dut:\n    type: stlink\n    probe_id: "STLINK123"\n')
    device = debugger_device(config, "dut")
    assert device.lock_key == "probe:stlink123"
    # The operator's own uppercase spelling folds onto that exact key, here and
    # on POSIX, with no `os.name` branch — the split this fixes.
    assert lock_keys(["probe:STLINK123"]) == [device.lock_key] == ["probe:stlink123"]

    coordinator = HardwareCoordinator(config, "owner")
    try:
        coordinator.begin_run(DeviceSet.of([device]), label="one-probe")
        # Declared as a device, reached under the uppercase spelling: the run
        # recognises its own probe instead of refusing it as undeclared.
        lease = coordinator.acquire("probe:STLINK123")
        try:
            assert lease.resources == [device.lock_key]
        finally:
            lease.release()
        # And the machine-wide hold answers to either spelling — the lock file
        # two keys would have split in two on POSIX.
        stranger = BenchMutex(frontend="stranger")
        with pytest.raises(DeviceBusyError):
            stranger.acquire(["probe:STLINK123"])
    finally:
        coordinator.end_run()
        coordinator.close()


def test_an_executable_debugger_holds_its_legacy_probe_key_too(tmp_path: Path) -> None:
    """The upgrade transition round 1's split left open (hardci-hq#106).

    Moving the executable from `probe:<path>` to `probe-exe:<path>` gave `probe:`
    an unambiguous fold, but it also split the new key from the one a process
    still on the old build — or a caller handing an already-derived name — keeps
    taking. An executable-identified debugger therefore holds both: its
    `probe-exe:` key and the legacy `probe:<path>`. `fold_resource_name` keeps a
    path-shaped `probe:` a host path, so the legacy spelling lands on one lock
    rather than casefolding into a name nothing else takes — shown here with an
    uppercase POSIX path, which is exactly where the fold change would otherwise
    make the two diverge."""
    exe = "/opt/Tools/OpenOCD"
    toolchain = replace(config_for(tmp_path).debugger, probe_id=None, resource_id=None, executable=exe)
    device = DebuggerDevice(config_id="dut", debugger=toolchain)

    assert device.identity_source == "executable"
    assert device.lock_key == f"probe-exe:{os.path.normcase(exe)}"
    legacy = f"probe:{os.path.normcase(exe)}"
    assert set(device.lock_keys) == {device.lock_key, legacy}
    # Both names a stranger might arrive under fold onto keys the device holds:
    # the new prefix, and the legacy path preserved rather than casefolded. A
    # probe *serial* under `probe:` still casefolds — the separator is the whole
    # distinction — so the round-1 fix stands beside this one.
    assert lock_keys([device.lock_key]) == [device.lock_key]
    assert lock_keys([f"probe:{exe}"]) == [legacy]
    assert lock_keys(["probe:STLINK123"]) == ["probe:stlink123"]
    # A serial is not an absolute path even if it carries a slash, so it stays a
    # hardware id and folds case. Testing for an absolute path rather than a bare
    # separator is what keeps this from re-splitting the round-1 fix.
    assert lock_keys(["probe:AB/CD-7"]) == ["probe:ab/cd-7"]


def test_a_raw_or_pre_upgrade_caller_cannot_bypass_an_executable_debugger(tmp_path: Path) -> None:
    """The two scenarios the transition has to cover, both directions.

    A run holding the executable debugger holds both its keys. Anyone else
    reaching the same probe collides on one of them: a caller passing the
    already-derived `probe:<path>` (a raw legacy caller), and — the upgrade window
    — a process still on the old build that derived that same legacy key, whether
    it arrives before or after the current run. Before the fix the new
    `probe-exe:` key and the old `probe:<path>` were two different locks, so both
    sides took the one debugger and each believed it was alone."""
    exe = "/opt/Tools/OpenOCD"
    toolchain = replace(config_for(tmp_path).debugger, probe_id=None, resource_id=None, executable=exe)
    device = DebuggerDevice(config_id="dut", debugger=toolchain)
    legacy = f"probe:{os.path.normcase(exe)}"

    # Current run holds the debugger; a raw legacy caller and a pre-upgrade
    # process are both refused the probe it is on.
    run = BenchMutex(frontend="current-run")
    device.acquire(run)
    try:
        raw_caller = BenchMutex(frontend="raw-caller")
        with pytest.raises(DeviceBusyError):
            raw_caller.acquire([f"probe:{exe}"])
        pre_upgrade = BenchMutex(frontend="pre-upgrade-process")
        with pytest.raises(DeviceBusyError):
            pre_upgrade.acquire([legacy])
    finally:
        run.release_all()

    # The other order of the same window: the old owner got in first, and the
    # current build's whole-device acquire is what is refused.
    old_owner = BenchMutex(frontend="pre-upgrade-process")
    old_owner.acquire([legacy])
    try:
        current = BenchMutex(frontend="current-run")
        with pytest.raises(DeviceBusyError):
            current.acquire(list(device.lock_keys))
    finally:
        old_owner.release_all()


def test_config_load_refuses_one_resource_id_spelled_in_two_cases(tmp_path: Path) -> None:
    """Folding must never merge two units behind the operator's back.

    An identical resource_id on two entries is the feature — it is how an
    ST-Link and its virtual COM port are declared one unit. Two spellings of one
    id are either a typo or an operator distinguishing two boards by case alone,
    and since the key folds everywhere the second reading would silently become
    the first. Refuse the pair instead of picking one meaning."""
    path = write_config(
        tmp_path,
        debuggers_yaml='debuggers:\n  probe_b:\n    type: openocd\n    probe_id: "SN-001"\n    resource_id: "bench-A"\n',
        com_ports_yaml='com_ports:\n  vcp:\n    device: "COM_VCP"\n    resource_id: "bench-a"\n',
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(str(path))

    assert excinfo.value.error_type == "config_invalid"
    assert "two cases" in excinfo.value.summary
    assert excinfo.value.details["other_entry"] == "debuggers.probe_b"

    # The identical spelling stays what it has always been: one unit, one lock.
    shared = config_for(tmp_path / "shared", debuggers_yaml=PROBE_AND_ITS_VCP_DEBUGGER, com_ports_yaml=PROBE_AND_ITS_VCP_PORT)
    assert debugger_device(shared, "second_probe").lock_key == uart_device(shared, "probe_vcp").lock_key


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


def test_only_a_debugger_may_be_declared_without_a_name(tmp_path: Path) -> None:
    """With one probe configured there is one board a bare selector can mean; a
    port or a bus always has to be named."""
    config = config_for(tmp_path, com_ports_yaml=TWO_NAMES_ONE_PORT)

    assert resolve_devices(config, [{"kind": "debugger"}]).lock_keys == sorted(debugger_device(config).lock_keys)
    with pytest.raises(DeviceError) as excinfo:
        resolve_devices(config, [{"kind": "uart"}])

    assert excinfo.value.result["error_type"] == "invalid_argument"
    assert excinfo.value.result["configured_devices"] == ["bootloader_uart", "dut_uart"]


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
        # The executable-identified debugger declares its `probe-exe:` key and the
        # legacy `probe:<path>` twin it also holds (hardci-hq#106).
        assert started["declared_devices"] == sorted(debugger_device(config).lock_keys)
        assert started["run_label"] == "boot-smoke"

        for _ in range(3):
            assert mcp_call(service, "probe_target")["ok"] is True
            # Between two calls of the run the board is still this run's.
            with pytest.raises(DeviceBusyError) as excinfo:
                stranger.acquire([probe_key])
            assert excinfo.value.result["holder"]["label"] == "boot-smoke"

        status = mcp_call(service, "bench_run_status")
        assert status["run_active"] is True
        assert status["declared_devices"] == sorted(debugger_device(config).lock_keys)
        assert status["devices"][0]["kind"] == "debugger"

        stopped = mcp_call(service, "bench_run_stop")
        assert stopped["released_devices"] == sorted(debugger_device(config).lock_keys)
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
        assert refused["declared_devices"] == [ONE_PORT_KEY]
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
    copy of the derivation. The two must not drift — case included, since the
    two branches that survive here fold by different rules."""
    from agentic_hil.config import debugger_resource_identity

    variants = (
        {},  # executable, the one path-like fallback
        {"probe_id": "0669FF"},
        {"probe_id": "0669ff"},
        {"debuggers_yaml": PROBE_AND_ITS_VCP_DEBUGGER},  # resource_id
    )
    for index, kwargs in enumerate(variants):
        config = config_for(tmp_path / f"case{index}", **kwargs)
        for name in config.debuggers:
            assert debugger_resource_identity(config.debuggers[name]) == debugger_device(config, name).lock_key
