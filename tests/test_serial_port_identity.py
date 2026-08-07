"""A serial port is a board, not a kernel name.

`/dev/ttyACM0` and `COM7` say which device the host happened to enumerate first.
Attaching a second ST-Link renumbers them, and the failure that matters is not
the run that breaks — it is the run that keeps working against the wrong board,
because an unchanged configuration now names hardware nobody meant
(hardci-hq#100).

Everything here fakes the platform: the host inventory, the udev directory and
the serial handle. Nothing needs a probe attached, which is the only way these
properties can be pinned on every runner rather than on the one bench that has
two ST-Links in it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import write_authoritative_config, write_config

from agentic_hil import comports
from agentic_hil.adopt import plan_adoption
from agentic_hil.cli import doctor
from agentic_hil.comports import (
    COM_PORT_IDENTITY_MISMATCH,
    COM_PORT_IDENTITY_UNVERIFIED,
    ComPortService,
    available_port_info,
    expected_port_identity,
    port_identity_fields,
    port_names_device,
    serial_by_id_links,
    stable_device_name,
    verify_port_identity,
)
from agentic_hil.config import load_config
from agentic_hil.devices import VOLATILE_SERIAL_DEVICE_WARNING, uart_device
from agentic_hil.types import is_stable_device_name

# The two boards on the bench in the report: one plugged in, then a second, and
# the kernel names swap depending on which was seen first.
BOARD_A = "0669FF534948717867012345"
BOARD_B = "0670FF534948717867054321"
BY_ID_A = f"/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_{BOARD_A}-if02"
BY_ID_B = f"/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_{BOARD_B}-if02"


def config_for(workspace: Path, **kwargs):
    return load_config(str(write_config(workspace, **kwargs)))


def com_ports_yaml(**entry: object) -> str:
    body = "".join(f"    {key}: {value!r}\n".replace("'", '"') for key, value in entry.items())
    return f"com_ports:\n  dut_uart:\n{body}"


def host_port(device: str, serial: str | None, stable: str | None = None) -> dict:
    """One entry as `list_available_com_ports` shapes it."""
    port: dict = {"device": device}
    if serial is not None:
        port["serial_number"] = serial
    if stable is not None:
        port["stable_device"] = stable
    return port


def inventory(*ports: dict) -> dict:
    return {"ok": True, "tool": "com_ports_available", "ports": list(ports), "summary": f"{len(ports)} available COM port(s)."}


def fake_host(monkeypatch: pytest.MonkeyPatch, result: dict) -> None:
    """Replace the host inventory — the one place this code asks the platform."""
    monkeypatch.setattr(comports, "list_available_com_ports", lambda tool="com_ports_available": result)


class FakePortInfo:
    """What pyserial hands back from `list_ports.comports()`."""

    def __init__(self, device: str, serial_number: str) -> None:
        self.device = device
        self.name = device
        self.serial_number = serial_number
        self.vid = 0x0483
        self.pid = 0x374B


# --- the name: which spellings survive being replugged --------------------


def test_a_kernel_name_is_an_enumeration_order_and_a_by_id_name_is_not() -> None:
    """The rule is about the name, so it holds on every host that reads the file."""
    assert is_stable_device_name(BY_ID_A) is True
    assert is_stable_device_name("/dev/ttyACM0") is False
    assert is_stable_device_name("/dev/ttyUSB1") is False
    assert is_stable_device_name("COM7") is False
    # by-path is stable against a reboot and not against moving the board to the
    # next socket, which is the question this is asking.
    assert is_stable_device_name("/dev/serial/by-path/pci-0000:00:14.0-usb-0:1.2:1.0") is False
    # The directory itself names no port.
    assert is_stable_device_name("/dev/serial/by-id/") is False


def test_a_kernel_name_joins_to_the_stable_one_through_the_udev_listing() -> None:
    """The join is what lets a configuration hold the durable spelling.

    Kept a pure function of the listing so it is asserted on every platform;
    reading the real directory is POSIX-only and covered separately below."""
    links = {"/dev/ttyACM0": BY_ID_A, "/dev/ttyACM1": BY_ID_B}

    assert stable_device_name("/dev/ttyACM0", links) == BY_ID_A
    assert stable_device_name("/dev/ttyACM1", links) == BY_ID_B
    # A host with no udev directory, and a port udev published nothing for.
    assert stable_device_name("/dev/ttyACM0", {}) is None
    assert stable_device_name("/dev/ttyS0", links) is None
    # A name that already is the stable one answers itself, so a caller may hand
    # this either spelling.
    assert stable_device_name(BY_ID_A, {}) == BY_ID_A


def test_the_host_inventory_carries_the_stable_name_where_there_is_one() -> None:
    listed = available_port_info(FakePortInfo("/dev/ttyACM0", BOARD_A), {"/dev/ttyACM0": BY_ID_A})

    assert listed["device"] == "/dev/ttyACM0"
    assert listed["stable_device"] == BY_ID_A
    assert listed["serial_number"] == BOARD_A

    # Windows publishes no openable stable name, and the inventory says nothing
    # rather than inventing one.
    windows = available_port_info(FakePortInfo("COM7", BOARD_A), {})
    assert "stable_device" not in windows
    assert windows["serial_number"] == BOARD_A


@pytest.mark.skipif(os.name == "nt", reason="/dev/serial/by-id is a udev directory of symlinks; Windows publishes no openable equivalent.")
def test_the_udev_directory_is_read_as_a_map_from_device_to_stable_name(tmp_path: Path) -> None:
    """The one POSIX-only step: resolving udev's symlinks.

    Everything above is asserted on both platforms; this is the part that can
    only run where the directory shape exists."""
    devices = tmp_path / "dev"
    by_id = tmp_path / "by-id"
    devices.mkdir()
    by_id.mkdir()
    (devices / "ttyACM0").write_text("", encoding="utf-8")
    (devices / "ttyACM1").write_text("", encoding="utf-8")
    (by_id / Path(BY_ID_A).name).symlink_to(devices / "ttyACM0")
    (by_id / Path(BY_ID_B).name).symlink_to(devices / "ttyACM1")

    links = serial_by_id_links(str(by_id))

    # realpath on both sides: a macOS runner's tmp_path is reached through a
    # /var -> /private/var symlink, and the map is keyed by what a link resolves
    # to rather than by how the test spelled it.
    assert links[os.path.realpath(devices / "ttyACM0")] == str(by_id / Path(BY_ID_A).name)
    assert links[os.path.realpath(devices / "ttyACM1")] == str(by_id / Path(BY_ID_B).name)
    # A host without the directory is a host without stable names, not an error.
    assert serial_by_id_links(str(tmp_path / "absent")) == {}


def test_an_entry_and_the_inventory_may_spell_one_port_two_ways() -> None:
    """A configuration holds the by-id name; the enumerator reports ttyACM0."""
    enumerated = host_port("/dev/ttyACM0", BOARD_A, BY_ID_A)

    assert port_names_device(enumerated, BY_ID_A) is True
    assert port_names_device(enumerated, "/dev/ttyACM0") is True
    assert port_names_device(enumerated, "/dev/ttyACM1") is False
    assert port_names_device(enumerated, BY_ID_B) is False


# --- the lock: the mutex follows the board --------------------------------


def test_the_lock_key_follows_the_serial_and_not_the_kernel_name(tmp_path: Path) -> None:
    """One board, two kernel names on two boots, one lock either way.

    Without this the mutex protects a name: two runs meaning one board can miss
    each other under different names, and two boards can collide under one."""
    first = config_for(tmp_path / "first", com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", serial_number=BOARD_A))
    after_replug = config_for(tmp_path / "second", com_ports_yaml=com_ports_yaml(device="/dev/ttyACM1", serial_number=BOARD_A))
    other_board = config_for(tmp_path / "other", com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", serial_number=BOARD_B))

    assert uart_device(first, "dut_uart").lock_key == uart_device(after_replug, "dut_uart").lock_key
    assert uart_device(first, "dut_uart").lock_key == f"com:serial:{BOARD_A.casefold()}"
    assert uart_device(first, "dut_uart").identity_source == "serial_number"
    # The bad case the report names: same kernel name, different board. Two
    # locks, so the two runs do not silently share one.
    assert uart_device(first, "dut_uart").lock_key != uart_device(other_board, "dut_uart").lock_key


def test_a_port_serial_folds_case_on_every_platform(tmp_path: Path) -> None:
    """It is a hardware id, like a probe serial — not a path.

    Asserted without asking the host what it would do: `os.path.normcase` folds
    on Windows and does nothing on POSIX, so a suite that lets the platform
    supply the expectation agrees with the code on both and notices nothing when
    the two disagree with each other."""
    upper = config_for(tmp_path / "upper", com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", serial_number="0669FF"))
    lower = config_for(tmp_path / "lower", com_ports_yaml=com_ports_yaml(device="/dev/ttyACM1", serial_number="0669ff"))

    assert uart_device(upper, "dut_uart").lock_key == uart_device(lower, "dut_uart").lock_key == "com:serial:0669ff"


def test_a_shared_resource_id_still_wins_and_still_says_two_entries_are_one_unit(tmp_path: Path) -> None:
    """The one field that can collapse a probe and its VCP keeps doing exactly that.

    A serial number and a probe serial are not collapsed on the strength of being
    equal: an ST-Link's SWD interface and its virtual COM port are independently
    usable, and flashing while watching the UART is the normal shape of a HIL
    run, so one lock across the two would deadlock every single-board bench."""
    config = config_for(
        tmp_path,
        probe_id=BOARD_A,
        debuggers_yaml=f'debuggers:\n  dut:\n    type: stlink\n    probe_id: "{BOARD_A}"\n    resource_id: "bench-one"\n',
        com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", serial_number=BOARD_A, resource_id="bench-one"),
    )
    unshared = config_for(tmp_path / "unshared", probe_id=BOARD_A, com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", serial_number=BOARD_A))

    from agentic_hil.devices import debugger_device

    assert uart_device(config, "dut_uart").lock_key == "physical:bench-one"
    assert uart_device(config, "dut_uart").identity_source == "resource_id"
    assert debugger_device(config, "dut").lock_key == "physical:bench-one"
    # Equal serials alone are two units, deliberately.
    assert uart_device(unshared, "dut_uart").lock_key != debugger_device(unshared, "dut").lock_key


def test_an_entry_identified_only_by_a_kernel_name_says_so(tmp_path: Path) -> None:
    """It keeps working. It also stops being silent about what it is."""
    volatile = config_for(tmp_path / "volatile", com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0"))
    windows = config_for(tmp_path / "windows", com_ports_yaml=com_ports_yaml(device="COM7"))
    by_id = config_for(tmp_path / "by-id", com_ports_yaml=com_ports_yaml(device=BY_ID_A))
    named = config_for(tmp_path / "named", com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", serial_number=BOARD_A))

    assert uart_device(volatile, "dut_uart").identity_warning == VOLATILE_SERIAL_DEVICE_WARNING
    assert uart_device(windows, "dut_uart").identity_warning == VOLATILE_SERIAL_DEVICE_WARNING
    # udev builds the by-id name out of the device's own serial, so a key derived
    # from it already follows the board.
    assert uart_device(by_id, "dut_uart").identity_warning is None
    assert uart_device(named, "dut_uart").identity_warning is None
    # The lock key of the untouched entry is exactly what it always was: no
    # existing bench changes behaviour by loading this version.
    assert uart_device(volatile, "dut_uart").lock_key == f"com:{os.path.normcase('/dev/ttyACM0')}"


# --- what the entry claims -------------------------------------------------


def test_the_hardware_an_entry_claims_comes_from_the_file_or_from_nowhere(tmp_path: Path) -> None:
    """Two sources, and no guess.

    A project with one debugger and one COM port is *probably* one board, and
    probably is exactly what may not decide which hardware a write reaches: a
    board wired to a separate USB-serial adapter has that shape too."""
    declared = config_for(tmp_path / "declared", com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", serial_number=BOARD_A))
    shared = config_for(
        tmp_path / "shared",
        debuggers_yaml=f'debuggers:\n  dut:\n    type: stlink\n    probe_id: "{BOARD_A}"\n    resource_id: "bench-one"\n',
        com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", resource_id="bench-one"),
    )
    alone = config_for(tmp_path / "alone", probe_id=BOARD_A, com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0"))

    assert expected_port_identity(declared, "dut_uart").expected == BOARD_A
    assert expected_port_identity(declared, "dut_uart").field == "com_ports.dut_uart.serial_number"
    # The operator said this port and that probe are one unit; the probe's
    # serial is therefore the port's.
    assert expected_port_identity(shared, "dut_uart").expected == BOARD_A
    assert expected_port_identity(shared, "dut_uart").field == "debuggers.dut.probe_id"
    # One probe, one port, nothing linking them: no claim, no check, no guess.
    assert expected_port_identity(alone, "dut_uart") is None


def test_a_configuration_reports_how_each_port_is_identified(tmp_path: Path) -> None:
    """What `doctor` and `com_ports_list` show, answered with nothing attached."""
    config = config_for(tmp_path, com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", serial_number=BOARD_A))
    volatile = config_for(tmp_path / "volatile", com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0"))

    identified = port_identity_fields(config, "dut_uart")
    assert identified["identity_source"] == "serial_number"
    assert identified["serial_number"] == BOARD_A
    assert identified["expected_serial_number"] == BOARD_A
    assert "identity_warning" not in identified

    assert port_identity_fields(volatile, "dut_uart")["identity_source"] == "device"
    assert port_identity_fields(volatile, "dut_uart")["identity_warning"] == VOLATILE_SERIAL_DEVICE_WARNING


# --- the refusal: the case that matters ------------------------------------


def test_a_second_board_under_the_first_ones_name_is_refused_not_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure the whole change exists for.

    An entry was recorded against board A's port. A second ST-Link is attached,
    the kernel names swap, and `/dev/ttyACM0` is now board B. Opening it would
    flash, reset and write to hardware nobody meant, with nothing in the result
    saying so."""
    config = config_for(tmp_path, com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", serial_number=BOARD_A))
    fake_host(monkeypatch, inventory(host_port("/dev/ttyACM0", BOARD_B, BY_ID_B), host_port("/dev/ttyACM1", BOARD_A, BY_ID_A)))
    service = ComPortService(config)
    opened: list[object] = []
    monkeypatch.setattr(service, "_open_serial", lambda *args: opened.append(args) or {"ok": False})

    try:
        result = service.session_start("dut_uart")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == COM_PORT_IDENTITY_MISMATCH
    assert opened == [], "the port must not be opened at all; connecting to find out is the failure"
    assert result["expected_serial_number"] == BOARD_A
    assert result["found_serial_number"] == BOARD_B
    assert result["expected_from"] == "com_ports.dut_uart.serial_number"
    assert result["side_effect_committed"] is False
    assert result["side_effect_status"] == "not_started"
    # Nothing was reached, so the bench stays in service: a named cause to fix
    # and call again, not a hardware incident.
    assert result["retry_safe"] is True
    assert result.get("quarantined") is not True
    assert result.get("cleanup_required") is not True
    # The board this entry means is still attached, under the other name. Say
    # where, because that is the whole repair.
    assert result["expected_device"] == BY_ID_A


def test_the_refusal_also_protects_a_configuration_that_still_holds_a_kernel_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bench in the report has not been re-adopted yet, and is protected anyway.

    Its port entry names no serial of its own — it shares the probe's
    `resource_id`, which is the operator saying the two are one unit."""
    config = config_for(
        tmp_path,
        debuggers_yaml=f'debuggers:\n  dut:\n    type: stlink\n    probe_id: "{BOARD_A}"\n    resource_id: "bench-one"\n',
        com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", resource_id="bench-one"),
    )
    fake_host(monkeypatch, inventory(host_port("/dev/ttyACM0", BOARD_B), host_port("/dev/ttyACM1", BOARD_A)))
    service = ComPortService(config)
    monkeypatch.setattr(service, "_open_serial", lambda *args: pytest.fail("the wrong board was opened"))

    try:
        result = service.session_start("dut_uart")
    finally:
        service.close()

    assert result["error_type"] == COM_PORT_IDENTITY_MISMATCH
    assert result["expected_from"] == "debuggers.dut.probe_id"
    assert result["expected_device"] == "/dev/ttyACM1"


def test_the_board_the_entry_names_is_opened_under_either_spelling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A confirmed identity opens, and says on what grounds.

    Either spelling of the device is this port: a configuration written by
    adoption holds the by-id name while the enumerator reports ttyACM0."""
    for label, device in (("kernel", "/dev/ttyACM0"), ("stable", BY_ID_A)):
        config = config_for(tmp_path / label, com_ports_yaml=com_ports_yaml(device=device, serial_number=BOARD_A))
        fake_host(monkeypatch, inventory(host_port("/dev/ttyACM0", BOARD_A, BY_ID_A), host_port("/dev/ttyACM1", BOARD_B, BY_ID_B)))

        checked = verify_port_identity(config, "dut_uart", "com_session_start")

        assert checked["ok"] is True, (label, checked)
        assert checked["identity"]["status"] == "confirmed"
        assert checked["identity"]["found_serial_number"] == BOARD_A


def test_an_entry_that_names_no_hardware_behaves_exactly_as_it_did(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bestandsschutz: the host is not even enumerated, and the result says why.

    Refusing here would take working benches off the air over a check they never
    asked for. Naming the situation is what this does instead."""
    config = config_for(tmp_path, com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0"))
    asked: list[str] = []
    monkeypatch.setattr(comports, "list_available_com_ports", lambda tool="com_ports_available": asked.append(tool) or inventory())

    checked = verify_port_identity(config, "dut_uart", "com_session_start")

    assert checked["ok"] is True
    assert checked["identity"]["status"] == "not_declared"
    assert checked["identity"]["warning"] == VOLATILE_SERIAL_DEVICE_WARNING
    assert asked == [], "an entry that claims nothing costs no enumeration"


def test_a_declared_identity_that_cannot_be_checked_is_refused_not_guessed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three ways the question has no answer, and for a declared identity each is
    a refusal returned before the port is opened.

    A pseudo-terminal, a port that reports no serial, and a host whose serial
    backend is missing are all "unknown" — and for an entry that named a board so
    its name would be proved to still reach it before use, unknown is not
    permission to open (hardci-hq#100). Reporting the gap in the result does not
    protect the hardware once the connection has already been allowed. The port
    is never touched, so every one is retry-safe and no side effect committed."""
    config = config_for(tmp_path, com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", serial_number=BOARD_A))

    fake_host(monkeypatch, inventory(host_port("/dev/ttyACM1", BOARD_B)))
    not_enumerated = verify_port_identity(config, "dut_uart", "com_session_start")
    assert not_enumerated["ok"] is False
    assert not_enumerated["error_type"] == COM_PORT_IDENTITY_UNVERIFIED
    assert not_enumerated["identity"]["status"] == "port_not_enumerated"
    assert not_enumerated["retry_safe"] is True
    assert not_enumerated["side_effect_committed"] is False
    assert not_enumerated["expected_serial_number"] == BOARD_A

    fake_host(monkeypatch, inventory(host_port("/dev/ttyACM0", None)))
    no_serial = verify_port_identity(config, "dut_uart", "com_session_start")
    assert no_serial["ok"] is False
    assert no_serial["error_type"] == COM_PORT_IDENTITY_UNVERIFIED
    assert no_serial["identity"]["status"] == "serial_unknown"

    fake_host(monkeypatch, {"ok": False, "tool": "com_ports_available", "error_type": "serial_backend_not_available", "summary": "pyserial is not installed or could not be imported."})
    no_backend = verify_port_identity(config, "dut_uart", "com_session_start")
    assert no_backend["ok"] is False
    assert no_backend["error_type"] == COM_PORT_IDENTITY_UNVERIFIED
    assert no_backend["identity"]["status"] == "backend_unavailable"
    assert no_backend["identity"]["backend_error"]


def test_a_declared_port_is_not_opened_when_its_identity_cannot_be_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal is what stops the wrong-board write, so it has to precede the open.

    The value of failing closed is only real if nothing is contacted first: a
    result that says "the check did not run" is no protection once the port is
    already open. So `session_start` returns the refusal before it ever reaches
    `_open_serial` — here the host cannot enumerate the port at all."""
    config = config_for(tmp_path, com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", serial_number=BOARD_A))
    fake_host(monkeypatch, {"ok": False, "tool": "com_ports_available", "error_type": "serial_backend_not_available", "summary": "pyserial is not installed."})
    service = ComPortService(config)
    monkeypatch.setattr(service, "_open_serial", lambda *args: pytest.fail("a port whose identity was unverifiable was opened"))

    try:
        result = service.session_start("dut_uart")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == COM_PORT_IDENTITY_UNVERIFIED
    assert result["side_effect_committed"] is False


# --- carrying an existing bench forward ------------------------------------


def discovery_of(device: str, stable: str | None = None) -> dict:
    """One correlated board, as `bootstrap.discover_attached_hardware` reports it."""
    port = host_port(device, BOARD_A, stable)
    return {
        "ok": True,
        "backend": "stlink",
        "probe_id": BOARD_A,
        "executable": "/opt/st/bin/STM32_Programmer_CLI",
        "target": {"controller": "STM32F446RE"},
        "com_port": port,
        "available_com_ports": {"ok": True, "ports": [port]},
    }


def test_adoption_records_the_stable_name_and_the_serial(tmp_path: Path) -> None:
    """A newly filled entry names the port durably where the platform allows it."""
    document = {"debuggers": {"dut": {"type": "stlink", "probe_id": None, "executable": None}}, "com_ports": {}}

    planned = plan_adoption(document, discovery_of("/dev/ttyACM0", BY_ID_A))

    assert planned["ok"] is True
    written = {item["key"]: item["value"] for item in planned["carried"]}
    assert written["com_ports.dut_uart.device"] == BY_ID_A
    assert written["com_ports.dut_uart.serial_number"] == BOARD_A

    # Windows publishes no openable stable name, so the kernel name is written
    # and the serial carries the identity instead.
    on_windows = plan_adoption(document, discovery_of("COM9"))
    windows_written = {item["key"]: item["value"] for item in on_windows["carried"]}
    assert windows_written["com_ports.dut_uart.device"] == "COM9"
    assert windows_written["com_ports.dut_uart.serial_number"] == BOARD_A


def test_adoption_re_spells_an_existing_kernel_name_instead_of_keeping_it(tmp_path: Path) -> None:
    """The repair path for the bench in the report.

    A configured `/dev/ttyACM0` is a value somebody set, and adoption does not
    replace those — but this is not a replacement. It is the same port, written
    down so it survives the next replug, and recognising that is what lets an
    existing bench be fixed by running `adopt-hardware` rather than by editing
    YAML."""
    document = {
        "debuggers": {"dut": {"type": "stlink", "probe_id": BOARD_A, "executable": None}},
        "com_ports": {"dut_uart": {"device": "/dev/ttyACM0", "baudrate": 115200}},
    }

    planned = plan_adoption(document, discovery_of("/dev/ttyACM0", BY_ID_A))

    assert planned["ok"] is True
    assert planned["com_port_id"] == "dut_uart"
    carried = {item["key"]: item for item in planned["carried"]}
    assert carried["com_ports.dut_uart.device"]["value"] == BY_ID_A
    assert carried["com_ports.dut_uart.device"]["previous_value"] == "/dev/ttyACM0"
    assert "same port" in carried["com_ports.dut_uart.device"]["reason"]
    assert carried["com_ports.dut_uart.serial_number"]["value"] == BOARD_A
    assert planned["kept"] == [], "re-spelling one port is not overruling an operator's choice of another"

    # Run it again and there is nothing left to do: the entry now holds the
    # stable spelling, and the port is still recognised as this entry's.
    settled = plan_adoption(
        {**document, "com_ports": {"dut_uart": {"device": BY_ID_A, "baudrate": 115200, "serial_number": BOARD_A}}},
        discovery_of("/dev/ttyACM0", BY_ID_A),
    )
    assert [item["key"] for item in settled["carried"] if item["key"].startswith("com_ports.")] == []
    assert sorted(item["key"] for item in settled["already_current"] if item["key"].startswith("com_ports.")) == [
        "com_ports.dut_uart.device",
        "com_ports.dut_uart.serial_number",
    ]


def test_a_port_a_person_pointed_somewhere_else_is_still_left_alone(tmp_path: Path) -> None:
    """The carve-out is exactly one port wide.

    An entry naming a device that is *not* the discovered port is somebody's
    decision, and it stays `kept` — the stable-name rewrite must not become a
    licence to repoint entries."""
    document = {
        "debuggers": {"dut": {"type": "stlink", "probe_id": BOARD_A, "executable": None}},
        "com_ports": {"dut_uart": {"device": "/dev/ttyUSB3", "baudrate": 115200}},
    }

    planned = plan_adoption(document, discovery_of("/dev/ttyACM0", BY_ID_A), com_port_id="dut_uart")

    assert [item["key"] for item in planned["kept"]] == ["com_ports.dut_uart.device"]
    assert planned["kept"][0]["configured_value"] == "/dev/ttyUSB3"

    # And it is an upgrade, never a normalisation: with no durable spelling to
    # move to — every Windows name is an enumeration order — the name a person
    # wrote stays exactly as they wrote it, and `serial_number` carries the
    # identity by itself.
    windows = {
        "debuggers": {"dut": {"type": "stlink", "probe_id": BOARD_A, "executable": None}},
        "com_ports": {"dut_uart": {"device": "com9", "baudrate": 115200}},
    }
    on_windows = plan_adoption(windows, discovery_of("COM9"))
    assert [item["key"] for item in on_windows["carried"] if item["key"].startswith("com_ports.")] == ["com_ports.dut_uart.serial_number"]


def test_doctor_names_a_bench_whose_ports_are_identified_by_a_kernel_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It keeps loading, keeps working, and stops being silent.

    `doctor` is where an operator looks before a bench misbehaves rather than
    after, so this is where the situation is named."""
    volatile = tmp_path / "volatile"
    write_authoritative_config(volatile, monkeypatch, com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0"))
    monkeypatch.chdir(volatile)
    warned = doctor()

    assert VOLATILE_SERIAL_DEVICE_WARNING in warned["warnings"]
    assert warned["com_ports"]["dut_uart"]["identity_source"] == "device"

    named = tmp_path / "named"
    write_authoritative_config(named, monkeypatch, com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", serial_number=BOARD_A))
    monkeypatch.chdir(named)
    quiet = doctor()

    # `doctor` names every device whose identity is not hardware-derived, so a
    # bench may still be warned about its debugger; what an entry with a
    # serial_number must not produce is this one.
    assert VOLATILE_SERIAL_DEVICE_WARNING not in quiet.get("warnings", [])
    assert quiet["com_ports"]["dut_uart"]["identity_source"] == "serial_number"
    assert quiet["com_ports"]["dut_uart"]["serial_number"] == BOARD_A
    # A warning is not a verdict: the same bench passes or fails `doctor` for the
    # same reasons it did before, which is what "keeps working" has to mean.
    assert warned["ok"] == quiet["ok"]


def test_a_successful_open_carries_the_identity_it_was_opened_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reader can tell a verified session from an unverified one."""
    config = config_for(tmp_path, com_ports_yaml=com_ports_yaml(device="/dev/ttyACM0", serial_number=BOARD_A))
    fake_host(monkeypatch, inventory(host_port("/dev/ttyACM0", BOARD_A, BY_ID_A)))
    service = ComPortService(config)
    monkeypatch.setattr(service, "_open_serial", lambda *args: {"ok": False, "tool": "com_session_start", "port_id": "dut_uart", "error_type": "com_port_open_failed", "summary": "no board here", "cleanup_confirmed": True, "side_effect_committed": False})

    try:
        # The open is faked, so what is asserted is that the check ran, passed,
        # and let the call through to the handle.
        result = service.session_start("dut_uart")
    finally:
        service.close()

    assert result["error_type"] == "com_port_open_failed"
    assert verify_port_identity(config, "dut_uart", "com_session_start")["identity"]["status"] == "confirmed"
