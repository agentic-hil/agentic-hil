"""The bench as the open read endpoints show it (#889).

One bench carries every kind of hardware identity this host has: a pyOCD probe
serial, a supply's USB serial and IDN serial, a COM device, a device-identified
console, a CAN channel, and a stray adapter the host lists beside them, with its
by-id path, hwid and location. Nothing here is real hardware: the inventory and
the probe listing are fakes, and every identifier is synthetic.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest
import serial
import yaml
from conftest import write_authoritative_config
from httpserver_fakes import ScriptedJobService, answer, running_reads, short_settings
from power_supply_fakes import (
    PID_TEXT,
    THRESHOLD_RECORD,
    THRESHOLD_TYPE,
    VID_TEXT,
    fake_inventory,
    fresh_resource,
    fresh_serial,
    host_port,
    register_fake_type,
    supply_entry,
)
from support import scaled_time_bound

from agentic_hil import power_supply
from agentic_hil.backends.esptool import EsptoolBackend
from agentic_hil.backends.gdbdebug import GdbDebugSessions
from agentic_hil.backends.openocd import OpenOCDBackend
from agentic_hil.backends.pyocd import PyOCDBackend
from agentic_hil.backends.stlink import STLinkBackend
from agentic_hil.bench import BenchMutex
from agentic_hil.benchview import BenchView
from agentic_hil.config import load_authoritative_config
from agentic_hil.coordination import HardwareCoordinator
from agentic_hil.devices import config_devices
from agentic_hil.httpapi import QueueSnapshot
from agentic_hil.report import append_jsonl_audited, logs_directory
from agentic_hil.types import JsonObject

try:
    import can
except ImportError:  # Without python-can no code path can open a bus at all.
    can = None

# Every read endpoint, over the transport.
READ_PATHS = (
    "/v1/server", "/v1/devices", "/v1/devices/dut", "/v1/probes", "/v1/ports", "/v1/backends", "/v1/leases",
    "/v1/queue", "/v1/jobs", "/v1/ledger", "/v1/doctor", "/v1/errors", "/v1/schema/plan", "/v1/metrics",
)
# The logical names the bench below configures: probe, supply port, console, bus, supply.
LOGICAL_NAMES = ("dut", "psu", "console", "bus", "bench")


def synthetic(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12].upper()}"


@dataclass
class PlantedBench:
    config: Any
    identifiers: tuple[str, ...]
    console_device: str
    stray_serial: str
    # One entry per pyOCD enumeration that ran.
    listings: list[int]


def planted_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, gate: Callable[[], None] | None = None) -> PlantedBench:
    """Write and load the bench, fake the host inventory and the pyOCD listing.

    `gate` runs inside every pyOCD enumeration before it answers, so a test can
    hold one open."""
    register_fake_type(monkeypatch, THRESHOLD_TYPE, THRESHOLD_RECORD)
    probe = synthetic("AHILPROBE")
    stray_probe = synthetic("AHILSTRAYPROBE")
    supply_serial = fresh_serial()
    idn_serial = fresh_serial()
    supply_device = f"COM{200 + uuid.uuid4().int % 50}"
    console_device = f"/dev/ttyAHIL{uuid.uuid4().hex[:8]}"
    channel = f"vcan{uuid.uuid4().hex[:6]}"
    stray_device = f"/dev/serial/by-id/usb-AHIL_{uuid.uuid4().hex[:10]}-if00"
    stray_serial = synthetic("AHILSERIAL")
    supply_location = synthetic("AHILLOCATION")
    stray_location = synthetic("AHILLOCATION")

    com_ports = {
        "psu": {"device": supply_device, "resource_id": fresh_resource(), "vid": VID_TEXT, "pid": PID_TEXT, "serial_number": supply_serial},
        "console": {"device": console_device, "identity_source": "device"},
    }
    can_buses = {"bus": {"adapter": "socketcan", "channel": channel}}
    supplies = {"bench": supply_entry(supply_type=THRESHOLD_TYPE, port_id="psu", powers=("dut",), idn_serial=idn_serial)}
    workspace = (tmp_path / "workspace").resolve()
    write_authoritative_config(
        workspace,
        monkeypatch,
        config_version=3,
        debugger_type="pyocd",
        probe_id=probe,
        target_type="stm32f446re",
        com_ports_yaml=yaml.safe_dump({"com_ports": com_ports}, sort_keys=False),
        can_buses_yaml=yaml.safe_dump({"can_buses": can_buses}, sort_keys=False),
        power_supplies_yaml=yaml.safe_dump({"power_supplies": supplies}, sort_keys=False),
    )
    monkeypatch.chdir(workspace)
    config = load_authoritative_config(workspace)

    supply_port = host_port(supply_device, supply_serial)
    supply_port.location = supply_location
    stray_port = SimpleNamespace(
        device=stray_device, name=None, description="synthetic stray adapter",
        hwid=f"USB VID:PID=1209:0002 SER={stray_serial} LOCATION={stray_location}",
        manufacturer=None, product=None, interface=None, location=stray_location,
        serial_number=stray_serial, vid=0x1209, pid=0x0002,
    )
    fake_inventory(monkeypatch, supply_port, stray_port)

    listings: list[int] = []

    def list_probes(self: PyOCDBackend) -> JsonObject:
        listings.append(len(listings) + 1)
        if gate is not None:
            gate()
        return {
            "ok": True,
            "tool": "debugger_probes_list",
            "backend": "pyocd",
            "probes": [{"probe_id": probe}, {"probe_id": stray_probe}],
            "summary": f"2 connected debugger probe(s) detected: {probe} and {stray_probe}.",
            "side_effect_committed": False,
            "target_contacted": False,
        }

    monkeypatch.setattr(PyOCDBackend, "list_probes", list_probes)
    paths = (console_device, stray_device)
    tails = tuple(path.rsplit("/", 1)[-1] for path in paths)
    identifiers = (
        probe, stray_probe, supply_serial, idn_serial, supply_device, *paths, *tails, channel,
        stray_serial, supply_location, stray_location,
    )
    return PlantedBench(config=config, identifiers=identifiers, console_device=console_device, stray_serial=stray_serial, listings=listings)


def refuse(what: str, record: list[str]) -> Callable[..., Any]:
    def refused(*args: Any, **kwargs: Any) -> Any:
        record.append(what)
        raise AssertionError(f"a read endpoint {what}")

    return refused


def refuse_every_device_path(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make every way to a device fail the moment anything takes it, and record each attempt."""
    refused: list[str] = []
    monkeypatch.setattr(BenchMutex, "acquire", refuse("took a device lock", refused))
    monkeypatch.setattr(BenchMutex, "acquire_named", refuse("took a device lock", refused))
    monkeypatch.setattr(HardwareCoordinator, "acquire", refuse("took a hardware lease", refused))
    monkeypatch.setattr(serial, "Serial", refuse("opened a serial port", refused))
    if can is not None:
        monkeypatch.setattr(can, "Bus", refuse("opened a CAN bus", refused))
    monkeypatch.setattr(GdbDebugSessions, "start_session", refuse("started a debugger session", refused))
    for backend in (OpenOCDBackend, PyOCDBackend, STLinkBackend, EsptoolBackend):
        for method in ("probe_target", "flash_firmware", "reset_target"):
            monkeypatch.setattr(backend, method, refuse(f"reached the target through {backend.__name__}.{method}", refused), raising=False)
    monkeypatch.setitem(power_supply.POWER_SUPPLY_BACKENDS, THRESHOLD_TYPE, refuse("connected a power supply", refused))
    return refused


def observed_at_values(value: object) -> list[object]:
    """Every `observed_at` an answer carries, at any depth."""
    if isinstance(value, dict):
        own = [value["observed_at"]] if "observed_at" in value else []
        return own + [found for item in value.values() for found in observed_at_values(item)]
    if isinstance(value, list):
        return [found for item in value for found in observed_at_values(item)]
    return []


def test_no_answer_carries_a_serial_number_or_a_device_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    planted = planted_bench(tmp_path, monkeypatch)
    config = planted.config
    devices = {device.config_id: device for device in config_devices(config)}
    # A ledger line written the way every hardware effect writes one.
    ledger_path = str(Path(logs_directory(config)) / "pyocd-planted-flash_firmware.log")
    assert append_jsonl_audited(config, ledger_path, {
        "tool": "flash_firmware",
        "probe_id": planted.identifiers[0],
        "device": planted.console_device,
        "serial_number": planted.stray_serial,
        "resources": list(devices["dut"].lock_keys),
        "image_sha256": "c" * 64,
    }) is None
    # A standing incident on the probe, whose lock key is its serial.
    incident = HardwareCoordinator(config, "planted-incident")
    incident.acquire(devices["dut"]).quarantine("planted_incident")
    incident.close()
    # Another owner holding the console, whose lock key is its device path.
    holder = BenchMutex(frontend="planted-holder", label="job-elsewhere")
    holder.acquire(list(devices["console"].lock_keys))
    try:
        view = BenchView(config, short_settings())
        try:
            answers = {
                "server": view.server_info(),
                "devices": view.devices(),
                **{f"device {name}": view.device(name) for name in LOGICAL_NAMES},
                "probes": view.probes(),
                "ports": view.ports(),
                "backends": view.backends(),
                "leases": view.leases(),
                "ledger": view.ledger(),
                "doctor": view.doctor(),
            }
        finally:
            view.close()
    finally:
        holder.release_all()

    text = json.dumps(answers, ensure_ascii=False).lower()
    assert sorted(value for value in planted.identifiers if value.lower() in text) == []
    inventory = json.dumps(answers["devices"])
    assert [name for name in LOGICAL_NAMES if f'"{name}"' not in inventory] == []
    assert '"console"' in json.dumps(answers["leases"])


@pytest.mark.usefixtures("no_loopback_name_lookup")
def test_reads_take_no_lock_and_reach_no_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    planted = planted_bench(tmp_path, monkeypatch)
    config = planted.config
    service = ScriptedJobService()
    service.script("queue", QueueSnapshot(()))
    service.script("jobs", ())
    service.script("metrics", "# TYPE agentic_hil_queue_length gauge\nagentic_hil_queue_length 0\n")
    view = BenchView(config, short_settings(), activity=lambda: {})
    holder = BenchMutex(frontend="another-owner", label="job-elsewhere")
    try:
        first_holder = BenchMutex(frontend="another-owner", label="job-elsewhere")
        first_holder.acquire(config_devices(config).lock_keys)
        try:
            with BenchView(config, short_settings(), activity=lambda: {}) as unlisted:
                held_first = unlisted.probes()
        finally:
            first_holder.release_all()
        assert planted.listings == [], "a probe a job holds was listed"
        assert observed_at_values(held_first) and set(observed_at_values(held_first)) == {None}, held_first
        free = view.probes()
        assert planted.listings == [1]
        holder.acquire(config_devices(config).lock_keys)
        refused = refuse_every_device_path(monkeypatch)
        with running_reads(service, view) as server:
            answers = {path: answer(server, "GET", path) for path in READ_PATHS}
        assert refused == [], refused
        assert {path: response.status for path, response in answers.items() if response.status != 200} == {}
        assert planted.listings == [1], "the probe a job holds was listed again"
        earlier = observed_at_values(free)
        assert earlier and None not in earlier, free
        assert set(earlier) <= set(observed_at_values(answers["/v1/probes"].json()))
    finally:
        holder.release_all()
        view.close()


def test_concurrent_probe_reads_share_one_enumeration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered = Event()
    release = Event()

    def gate() -> None:
        entered.set()
        assert release.wait(scaled_time_bound(10)), "the test never let the enumeration answer"

    planted = planted_bench(tmp_path, monkeypatch, gate=gate)
    view = BenchView(planted.config, short_settings(), activity=lambda: {})
    try:
        with ThreadPoolExecutor(max_workers=2) as readers:
            first = readers.submit(view.probes)
            assert entered.wait(scaled_time_bound(10)), "the first reader never reached the enumeration"
            second = readers.submit(view.probes)
            release.set()
            answers = [first.result(timeout=scaled_time_bound(10)), second.result(timeout=scaled_time_bound(10))]
    finally:
        release.set()
        view.close()
    assert planted.listings == [1]
    assert answers[0] == answers[1]
