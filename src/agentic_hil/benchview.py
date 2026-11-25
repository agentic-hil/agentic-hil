"""The bench as the open read endpoints show it, read without reaching a device (#889).

Built from the authoritative configuration, the records the device locks and
the coordinator leave on disk, the canonical audit ledger and the host's USB
inventory. Nothing here takes a device lock or a lease, opens a serial port or
a bus, connects to a target or talks to a supply: whether a device is held is
read off the holder record beside its lock, never found out by trying the lock.
The one listing that opens the attached probes over USB runs only while no
debugger is held.

Every answer names devices by their logical names. A probe serial, a supply's
USB or IDN serial, a device path or a CAN channel tells a stranger which
hardware sits on this bench, so each one is masked wherever it would otherwise
reach an answer.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from agentic_hil import __version__
from agentic_hil.bench import BENCH_ROOT_NAME, DEVICE_LOCK_DIRECTORY_NAME, BenchMutex, machine_device_lock_root
from agentic_hil.comports import list_available_com_ports, port_names_device
from agentic_hil.config import ConfigError, bind_debugger, permission_summary, safe_read_text
from agentic_hil.coordination import (
    DEBUGGER_DISCOVERY_RESOURCE,
    foreign_incident_next_step,
    project_resource,
    standing_foreign_incidents,
)
from agentic_hil.debugger import DebuggerBackend, create_debugger_backend
from agentic_hil.devices import CanDevice, DebuggerDevice, PowerSupplyDevice, UartDevice
from agentic_hil.httpapi import INVENTORY_MAX_AGE_S, DeviceActivity, JobService, JobServiceError, ServerSettings
from agentic_hil.httpread import public_job_record, public_queue_entry
from agentic_hil.power_supply import supply_secrets
from agentic_hil.redact import redact_sensitive, redact_values
from agentic_hil.report import utc_now_iso
from agentic_hil.types import AgenticHILConfig, ComPortConfig, JsonObject, com_port_is_unbound, fold_hardware_id

# The frontend the holder-record reader is named by. It never writes a record.
_FRONTEND = "bench-view"
# What a hold shows, as `BenchMutex.busy_result` reads it off the holder record:
# since when, the last heartbeat, its age and whether the owner stopped beating,
# and who the owner is, in the terms a `device_busy` refusal already names it.
_HOLD_FIELDS = ("held_since", "heartbeat_at", "heartbeat_age_s", "holder_heartbeat_stale")
_HOLDER_FIELDS = ("owner_id", "pid", "host", "frontend", "label")
# The next step for an incident whose coordination record could not be read,
# which says nothing about whose incident it is.
_UNREADABLE_NEXT_STEP = (
    "Its coordination record could not be read, so whether it holds the device is unknown. `agentic-hil lease-status` "
    "on the bench stops on the same record and names what is wrong with it; an operator judges it there, and after "
    "checking the board runs `agentic-hil recover --confirm-safe-state --retire-records`."
)
# The probe enumerations that open every attached probe over USB, which run only
# while no debugger is held, and the one that reads the host's USB serial
# inventory and opens nothing. esptool has no probe to list.
_LISTED_BY_OPENING = frozenset({"pyocd", "stlink", "picotool"})
_LISTED_FROM_INVENTORY = frozenset({"openocd"})
_LISTABLE = _LISTED_BY_OPENING | _LISTED_FROM_INVENTORY
# The host's serial port inventory, kept beside the probe listings.
_PORTS = "com_ports"
# What a ledger line may show beside the devices it names: when, which job, what
# was done and how it ended. Never what was sent or received, and never a summary.
_LEDGER_FIELDS = frozenset({"time", "job_id", "tool", "event", "direction", "ok", "error_type", "side_effect_committed"})
# The fields that name a device by its logical name, and those that carry the
# digest of the image or artifact an effect put on a board.
_LEDGER_DEVICE_FIELDS = ("debugger_id", "port_id", "bus_id", "supply_id")
_LEDGER_DIGEST_FIELDS = ("image_sha256", "artifact_sha256", "sha256")
# The ledger's own bookkeeping beside each log: its chain digest and its append lock.
_LEDGER_SIDECARS = (".digest", ".lock")
_LEDGER_LIMIT = 200
_LEDGER_FILE_LIMIT = 64
# What a listed probe or host port reports that identifies its hardware, and
# which of those are paths, whose last component names the device on its own.
_LISTED_IDENTIFIERS = ("probe_id", "name", "serial_number", "hwid", "location")
_LISTED_PATHS = ("device", "stable_device")
# Shorter values are not masked: a one- or two-character value names no unit,
# and masking it would take every occurrence of that text out of an answer.
_SHORTEST_IDENTIFIER = 3


@dataclass(frozen=True)
class _Device:
    """One configured device: its logical name, the lock keys that hold it, and what an answer may say of it."""

    name: str
    kind: str
    keys: tuple[str, ...]
    facts: JsonObject


@dataclass(frozen=True)
class _Reading:
    """One host enumeration, kept for INVENTORY_MAX_AGE_S. What it listed is matched and masked, never shown."""

    observed_at: str
    taken: float
    ok: bool
    listed: tuple[JsonObject, ...]


@dataclass(frozen=True)
class _Situation:
    """What every device's state is read from, asked once per answer."""

    # The holder record of each lock key that says it is held; None where the
    # lock directory cannot be read.
    held: Mapping[str, JsonObject] | None
    # The logical names a standing incident stands on.
    incidents: frozenset[str]
    activity: Mapping[str, DeviceActivity]


class BenchView:
    """Implements httpapi.BenchReads from the configuration, lock records and host inventory.

    Constructing one reads nothing. A debugger's backend is built the first time
    a read needs it, for its version and its probe listing, and closed with the
    view."""

    def __init__(
        self, config: AgenticHILConfig, settings: ServerSettings,
        activity: Callable[[], Mapping[str, DeviceActivity]] | None = None,
    ) -> None:
        self.config = config
        self.settings = settings
        self._activity = activity
        self._started = time.monotonic()
        self._guard = threading.Lock()
        self._backends: dict[str, DebuggerBackend] = {}
        self._readings: dict[str, _Reading] = {}
        # One enumeration at a time per source: a reader that arrives while one
        # runs waits for it and is given its answer instead of starting another.
        self._listing_locks = {source: threading.Lock() for source in (*sorted(_LISTABLE), _PORTS)}

    def __enter__(self) -> BenchView:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._guard:
            backends = list(self._backends.values())
            self._backends.clear()
        for backend in backends:
            backend.close()

    def server_info(self) -> JsonObject:
        settings = self.settings
        return self._answer({
            "version": __version__,
            "config_version": self.config.config_version,
            "config_digest": self.config.config_digest,
            "uptime_s": round(time.monotonic() - self._started, 3),
            "inventory_max_age_s": INVENTORY_MAX_AGE_S,
            "limits": {
                "max_upload_mb": settings.max_upload_mb,
                "max_artifact_wait_s": settings.max_artifact_wait_s,
                "prefer_wait_cap_s": settings.prefer_wait_cap_s,
                "sse_keepalive_s": settings.sse_keepalive_s,
                "retry_after_s": settings.retry_after_s,
            },
        })

    def devices(self) -> JsonObject:
        devices = self._configured()
        situation = self._situation(devices)
        return self._answer({"devices": [_described(device, situation) for device in devices]})

    def device(self, name: str) -> JsonObject:
        devices = self._configured()
        named = next((device for device in devices if device.name == name), None)
        if named is None:
            raise JobServiceError(
                "device_not_found",
                f"This bench declares no device named {name!r}.",
                {"device": name, "devices": sorted({device.name for device in devices})},
            )
        situation = self._situation(devices)
        known = situation.activity.get(name)
        return self._answer({**_described(named, situation), "recent_jobs": list(known.recent_jobs) if known is not None else []})

    def probes(self) -> JsonObject:
        """Whether each configured probe is attached, as the host last listed it.

        A listing that opens the probes runs only while no debugger is held;
        while one is, the last listing stands with the time it was taken, and a
        probe never listed reads `unknown` with no time at all."""
        config = self.config
        readings = {backend: self._probe_reading(backend, name) for backend, name in self._probe_sources().items()}
        configured: dict[str, set[str]] = {}
        probes: JsonObject = {}
        picotool_reading = readings.get("picotool")
        picotool_rows = {item["debugger_id"]: item for item in picotool_reading.listed} if picotool_reading is not None else {}
        for name, debugger in config.debuggers.items():
            if debugger.probe_id:
                configured.setdefault(debugger.type, set()).add(fold_hardware_id(debugger.probe_id))
            if debugger.type not in _LISTABLE:
                probes[name] = {"backend": debugger.type, "status": "not_listable"}
            elif not config.probe_allowed(debugger):
                probes[name] = {"backend": debugger.type, "status": "not_listed"}
            elif debugger.type == "picotool":
                row = picotool_rows.get(name, {})
                present = row.get("present")
                probes[name] = {
                    "backend": "picotool",
                    "status": "present" if present is True else "absent" if present is False else "unknown",
                    "present": present,
                    "target_state": row.get("target_state"),
                    "chip": row.get("chip"),
                    "observed_at": picotool_reading.observed_at if picotool_reading is not None else None,
                }
            else:
                reading = readings[debugger.type]
                probes[name] = {
                    "backend": debugger.type,
                    "status": _probe_status(debugger.probe_id, reading),
                    "observed_at": reading.observed_at if reading is not None else None,
                }
        counts = [
            len(_probe_ids(reading) - configured.get(backend, set())) if reading is not None and reading.ok else None
            for backend, reading in readings.items()
        ]
        unconfigured = None if None in counts else sum(count for count in counts if count is not None)
        answer: JsonObject = {"probes": probes, "present_not_configured": unconfigured}
        if any(debugger.type == "picotool" for debugger in config.debuggers.values()):
            from agentic_hil.backends.picotool import PICOTOOL_LISTING_NOTICE

            answer.update({"picotool_scope": PICOTOOL_LISTING_NOTICE, "present_not_configured": None})
        return self._answer(answer)

    def ports(self) -> JsonObject:
        config = self.config
        # A version 1 file lists host ports only once one entry may be read.
        listable = config.read_free or any(config.com_read_allowed(port) for port in config.com_ports.values())
        reading = self._reading(_PORTS, "ports", list_available_com_ports) if listable else None
        host = list(reading.listed) if reading is not None and reading.ok else None
        ports: JsonObject = {}
        claimed: set[int] = set()
        for port_id, port in config.com_ports.items():
            if com_port_is_unbound(port):
                status = "unbound"
            elif host is None:
                status = "unknown" if listable else "not_listed"
            else:
                matched = {index for index, entry in enumerate(host) if _names_port(entry, port)}
                claimed |= matched
                status = "present" if matched else "absent"
            ports[port_id] = {"status": status}
        return self._answer({
            "ports": ports,
            "present_not_configured": None if host is None else len(host) - len(claimed),
            "observed_at": reading.observed_at if reading is not None else None,
        })

    def backends(self) -> JsonObject:
        """Each debugger's toolchain and its version, which asks the toolchain and no probe."""
        answers: JsonObject = {}
        for name, debugger in self.config.debuggers.items():
            try:
                info = self._backend(name).info()
            except (ConfigError, OSError):
                info = {}
            version = info.get("version")
            answers[name] = {"backend": debugger.type, "ok": info.get("ok") is True, **({"version": version} if isinstance(version, str) else {})}
        return self._answer({"backends": answers})

    def leases(self) -> JsonObject:
        """Device holds from active jobs and machine holder records, plus standing incidents."""
        devices = self._configured()
        names = _names_by_key(devices)
        activity = self._activity_now()
        occupied_keys = {key for known in activity.values() for keys in known.held_job_keys.values() for key in keys}
        held = self._held_records(names.keys() | occupied_keys)
        held_by_key = held or {}
        holds: list[JsonObject] = []
        for device in devices:
            device_record = next((held_by_key[key] for key in device.keys if key in held_by_key), None)
            known = activity.get(device.name)
            if known is not None and known.held_job_keys:
                for job_id, keys in known.held_job_keys.items():
                    record = next((held_by_key[key] for key in keys if key in held_by_key), device_record)
                    holds.append({**_lease_hold(device, record or {}), "job_id": job_id})
            elif device_record is not None:
                holds.append(_lease_hold(device, device_record))
        incidents = self._incidents()
        own = project_resource(self.config)
        return self._answer({
            "holds": holds,
            "holds_readable": held is not None,
            "incidents": [_incident(entry, names, own) for entry in incidents or []],
            "incidents_readable": incidents is not None,
        })

    def ledger(self) -> JsonObject:
        """The newest hardware effects of this project's canonical audit ledger, by device and tool."""
        devices = self._configured()
        names = _names_by_key(devices)
        declared = {device.name for device in devices}
        directory = self._audit_directory()
        entries: list[JsonObject] = []
        unreadable = 0
        for path in _newest_logs(directory) if directory is not None else []:
            try:
                text = safe_read_text(path)
            except (ConfigError, OSError, UnicodeDecodeError):
                unreadable += 1
                continue
            logged = [_ledger_entry(event, names, declared) for event in _events(text)]
            # One log is one session or one call: its later lines (what a COM or
            # CAN session sent) name no device, and are on the one its start named.
            session = sorted({device for entry in logged for device in entry["devices"]})
            entries.extend({**entry, "devices": entry["devices"] or session} for entry in logged)
        entries.sort(key=lambda entry: str(entry.get("time", "")), reverse=True)
        return self._answer({"entries": entries[:_LEDGER_LIMIT], "total": len(entries), "unreadable_logs": unreadable})

    def doctor(self) -> JsonObject:
        # Imported here: the command line imports this module to serve.
        from agentic_hil import cli

        report = cli.doctor()
        unhealthy = report.get("unhealthy")
        warnings = report.get("warnings")
        # A device-access sentence in the summary names the node it checked: a
        # probe's /dev/bus/usb node, or the tty a by-id port resolves to. Neither
        # is in the configuration, so the checks' own fields are masked with it.
        return self._answer({
            "ok": report.get("ok") is True,
            "summary": str(report.get("summary") or ""),
            "unhealthy": [str(item) for item in unhealthy] if isinstance(unhealthy, list) else [],
            "warnings": [str(item) for item in warnings] if isinstance(warnings, list) else [],
        }, _access_nodes(report))

    def _configured(self) -> list[_Device]:
        return _configured_devices(self.config)

    def _situation(self, devices: list[_Device]) -> _Situation:
        names = _names_by_key(devices)
        incidents = self._incidents() or []
        return _Situation(
            held=self._held_records(names),
            incidents=frozenset(name for entry in incidents for name in names.get(str(entry.get("resource")), ())),
            activity=self._activity_now(),
        )

    def _activity_now(self) -> Mapping[str, DeviceActivity]:
        return self._activity() if self._activity is not None else {}

    def _held_records(self, keys: Iterable[str]) -> dict[str, JsonObject] | None:
        """Each of `keys` whose holder record says it is held, with what `busy_result` reads off that record.

        None where the lock directory cannot be read. A single read supplies both
        the state and details without trying the lock. A record outlives an owner
        that died holding it, so a device can read as held after its holder is gone (its
        heartbeat then reads stale), until the next owner takes it and gives it
        back. That errs the way a reader of a bench has to: toward leaving a
        device alone."""
        try:
            root = _lock_root()
        except ConfigError:
            return None
        if root is None:
            return {}
        reader = BenchMutex(frontend=_FRONTEND, root=root)
        records: dict[str, JsonObject] = {}
        for key in sorted(set(keys)):
            record = reader.holder(key)
            if record is not None and record.get("state") == "held":
                records[key] = reader.busy_result(key, observed_holder=record)
        return records

    def _incidents(self) -> list[JsonObject] | None:
        """Every standing incident on a resource this bench declares, this project's own among them.

        The walk `doctor` makes, over records written atomically and without a
        lock. It is asked for a project key no record carries, so this
        project's incidents are answered too instead of passed over as its own."""
        try:
            return standing_foreign_incidents(self.config, "")
        except (ConfigError, OSError):
            return None

    def _no_debugger_held(self) -> bool:
        """Whether a probe listing may open the attached probes now.

        Not while any debugger is held, by a job of this server or by any owner
        on this machine, nor while the job store has granted one to a job: the
        listing opens each probe over USB, and a probe in use is not opened
        behind its holder's back. A lock directory that cannot be read counts as
        held."""
        keys = {key for device in self._configured() if device.kind == DebuggerDevice.kind for key in device.keys}
        held = self._held_records({*keys, DEBUGGER_DISCOVERY_RESOURCE})
        if held is None or held:
            return False
        activity = self._activity_now()
        return not any(activity[name].state in ("held", "reserved") for name in self.config.debuggers if name in activity)

    def _probe_sources(self) -> dict[str, str]:
        """One debugger per listable backend, whose toolchain lists the probes for all of that backend's entries."""
        sources: dict[str, str] = {}
        for name, debugger in self.config.debuggers.items():
            if debugger.type in _LISTABLE and self.config.probe_allowed(debugger):
                sources.setdefault(debugger.type, name)
        return sources

    def _probe_reading(self, backend: str, name: str) -> _Reading | None:
        may_list = self._no_debugger_held if backend in _LISTED_BY_OPENING else None
        if backend == "picotool":
            return self._reading(backend, "probes", lambda: self._picotool_listing(name), may_list)
        return self._reading(backend, "probes", lambda: self._backend(name).list_probes(), may_list)

    def _picotool_listing(self, name: str) -> JsonObject:
        from agentic_hil.tools import AgenticHILToolService

        service = AgenticHILToolService(bind_debugger(self.config, name))
        try:
            return service.call("debugger_probes_list")
        finally:
            service.close()

    def _reading(
        self, source: str, key: str, enumerate_host: Callable[[], JsonObject], may_enumerate: Callable[[], bool] | None = None,
    ) -> _Reading | None:
        """The host's listing for `source`, taken at most once per INVENTORY_MAX_AGE_S.

        Where `may_enumerate` says no, the last reading stands however old it
        is, and there is none before the first."""
        with self._listing_locks[source]:
            with self._guard:
                kept = self._readings.get(source)
            if kept is not None and time.monotonic() - kept.taken < INVENTORY_MAX_AGE_S:
                return kept
            if may_enumerate is not None and not may_enumerate():
                return kept
            try:
                listing = enumerate_host()
            except (ConfigError, OSError):
                listing = {}
            ok = listing.get("ok") is True
            listed = listing.get(key) if ok else None
            entries = tuple(item for item in listed if isinstance(item, dict)) if isinstance(listed, list) else ()
            reading = _Reading(utc_now_iso(), time.monotonic(), ok, entries)
            with self._guard:
                self._readings[source] = reading
            return reading

    def _backend(self, name: str) -> DebuggerBackend:
        with self._guard:
            backend = self._backends.get(name)
            if backend is None:
                backend = self._backends[name] = create_debugger_backend(bind_debugger(self.config, name))
            return backend

    def _audit_directory(self) -> Path | None:
        """This project's canonical audit ledger directory where it exists. Never created here.

        `project_state_directory` names the project by the first 24 hex digits
        of the digest `project_resource` carries: both hash the same
        configuration path and working directory. The plain path is asked
        because reaching the directory through `project_state_directory` would
        create it."""
        digest = project_resource(self.config).removeprefix("project:")[:24]
        plain = Path(os.path.abspath(self.config.state_root)) / "projects" / digest / "audit-logs"
        return plain if plain.is_dir() else None

    def _identifiers(self, extra: Iterable[str] = ()) -> frozenset[str]:
        """Every value that identifies this bench's hardware or where it lives.

        From the configuration, from what the host listed, and `extra`: what one
        answer read beside them, such as the nodes doctor's access checks named."""
        config = self.config
        found: set[str] = set(supply_secrets(config)) | set(extra)
        for debugger in config.debuggers.values():
            if debugger.probe_id:
                found.add(debugger.probe_id)
        for port in config.com_ports.values():
            found |= _spellings(port.device)
            if port.serial_number:
                found.add(port.serial_number)
        for bus in config.can_buses.values():
            found |= _spellings(bus.channel)
        found.update((config.state_root, config.workspace_root, config.work_dir, config.config_path))
        with self._guard:
            readings = list(self._readings.values())
        for reading in readings:
            for entry in reading.listed:
                for field in (*_LISTED_IDENTIFIERS, *_LISTED_PATHS):
                    listed = entry.get(field)
                    if isinstance(listed, str):
                        found |= _spellings(listed) if field in _LISTED_PATHS else {listed}
        return frozenset(value for value in found if len(value.strip()) >= _SHORTEST_IDENTIFIER)

    def _answer(self, answer: JsonObject, extra: Iterable[str] = ()) -> JsonObject:
        """`answer` with every credential-shaped field and every hardware identifier masked."""
        return redact_values(redact_sensitive(answer), self._identifiers(extra))


class McpBenchView:
    """Implements httpapi.McpBenchReads from one bench view and the job service beside it.

    Every answer is built from what the view and the service already answer, so
    it reads no device the view does not read and carries nothing the view
    masked. The five tools that also exist locally answer in the local tools'
    field names."""

    def __init__(self, view: BenchView, service: JobService) -> None:
        self.view = view
        self.service = service

    def bench_describe(self) -> JsonObject:
        return {
            "ok": True,
            "tool": "bench_describe",
            "server": self.view.server_info(),
            "devices": self.view.devices().get("devices", []),
            "plan_schema": "/v1/schema/plan",
        }

    def bench_queue_status(self) -> JsonObject:
        return {
            "ok": True,
            "tool": "bench_queue_status",
            "devices": self.view.devices().get("devices", []),
            "queue": [public_queue_entry(entry) for entry in self.service.queue().entries],
            "holds": self.view.leases().get("holds", []),
        }

    def bench_jobs_list(self, *, state: str | None, device: str | None, user: str | None, since: str | None) -> JsonObject:
        jobs = self.service.jobs(state=state, device=device, user=user, since=since)
        return {"ok": True, "tool": "bench_jobs_list", "jobs": [public_job_record(job) for job in jobs]}

    def debugger_probes_list(self) -> JsonObject:
        listed = self.view.probes()
        return {
            "ok": True,
            "tool": "debugger_probes_list",
            "probes": listed.get("probes", {}),
            "present_not_configured": listed.get("present_not_configured"),
        }

    def debugger_info(self) -> JsonObject:
        return {"ok": True, "tool": "debugger_info", "debuggers": self.view.backends().get("backends", {})}

    def com_ports_list(self) -> JsonObject:
        listed = self.view.ports()
        return {
            "ok": True,
            "tool": "com_ports_list",
            "ports": listed.get("ports", {}),
            "present_not_configured": listed.get("present_not_configured"),
            "observed_at": listed.get("observed_at"),
        }

    def can_buses_list(self) -> JsonObject:
        devices = self.view.devices().get("devices", [])
        buses = [device for device in devices if isinstance(device, dict) and device.get("kind") == CanDevice.kind]
        return {"ok": True, "tool": "can_buses_list", "buses": buses}

    def hardware_lease_status(self) -> JsonObject:
        leases = self.view.leases()
        holds = leases.get("holds", [])
        incidents = leases.get("incidents", [])
        return {
            "ok": True,
            "tool": "hardware_lease_status",
            "bench_held": bool(holds),
            "device_holds": holds,
            "incidents": incidents,
            "incident_stands": bool(incidents),
        }


def configured_device_keys(config: AgenticHILConfig) -> dict[str, tuple[str, ...]]:
    """The lock keys of every device this bench declares, by its logical name."""
    return {device.name: tuple(device.keys) for device in _configured_devices(config)}


def _configured_devices(config: AgenticHILConfig) -> list[_Device]:
    granted = permission_summary(config)
    devices: list[_Device] = []
    for name, debugger in config.debuggers.items():
        devices.append(_Device(name, DebuggerDevice.kind, DebuggerDevice(config_id=name, debugger=debugger).lock_keys, {
            "backend": debugger.type, "target_type": debugger.target_type, "permissions": granted["debuggers"][name],
        }))
    for name, port in config.com_ports.items():
        devices.append(_Device(name, UartDevice.kind, UartDevice(config_id=name, port=port).lock_keys, {
            "baudrate": port.baudrate, "bound": not com_port_is_unbound(port), "permissions": granted["com_ports"][name],
        }))
    for name, bus in config.can_buses.items():
        devices.append(_Device(name, CanDevice.kind, CanDevice(config_id=name, bus=bus).lock_keys, {
            "adapter": bus.adapter, "bitrate": bus.bitrate, "fd": bus.fd, "listen_only": bus.listen_only,
            "participants": sorted(bus.shares), "permissions": granted["can_buses"][name],
        }))
    for name, supply in config.power_supplies.items():
        # A supply is locked by its port: the two are one unit and take one key.
        supply_port = config.com_ports.get(supply.com_port)
        keys = UartDevice(config_id=supply.com_port, port=supply_port).lock_keys if supply_port is not None else ()
        devices.append(_Device(name, PowerSupplyDevice.kind, keys, {
            "supply_type": supply.type, "com_port": supply.com_port, "powers": list(supply.powers),
        }))
    return devices


def _lock_root() -> Path | None:
    """Where this machine's device locks live, without creating the directory: None where it does not exist."""
    machine = machine_device_lock_root()
    if machine is not None:
        return machine
    home = Path(os.path.expanduser("~")) / BENCH_ROOT_NAME / DEVICE_LOCK_DIRECTORY_NAME
    return home if home.is_dir() else None


def _names_by_key(devices: list[_Device]) -> dict[str, list[str]]:
    """The logical names each lock key stands for. Lock keys embed serials and paths, so no answer carries one."""
    names: dict[str, set[str]] = {}
    for device in devices:
        for key in device.keys:
            names.setdefault(key, set()).add(device.name)
    # Not a device: the host-wide probe enumeration, which every debugger needs.
    names.setdefault(DEBUGGER_DISCOVERY_RESOURCE, set()).update(device.name for device in devices if device.kind == DebuggerDevice.kind)
    return {key: sorted(value) for key, value in names.items()}


def _lease_hold(device: _Device, record: JsonObject) -> JsonObject:
    holder: JsonObject = record["holder"] if isinstance(record.get("holder"), dict) else {}
    return {
        "device": device.name,
        "kind": device.kind,
        **{key: record[key] for key in _HOLD_FIELDS if key in record},
        "holder": {key: holder[key] for key in _HOLDER_FIELDS if key in holder},
    }


def _described(device: _Device, situation: _Situation) -> JsonObject:
    known = situation.activity.get(device.name)
    held = situation.held or {}
    if device.name in situation.incidents:
        state = "incident"
    elif any(key in held for key in device.keys) or (known is not None and known.state == "held"):
        state = "held"
    elif known is not None and known.state == "reserved":
        state = "reserved"
    elif situation.held is None:
        state = "unknown"
    elif known is not None and known.drained:
        state = "drained"
    else:
        state = "free"
    described: JsonObject = {"name": device.name, "kind": device.kind, **device.facts, "state": state, "drained": known is not None and known.drained}
    if known is not None and known.job_id and state in ("held", "reserved"):
        described["job_id"] = known.job_id
    return described


def _probe_ids(reading: _Reading) -> set[str]:
    return {fold_hardware_id(str(item["probe_id"])) for item in reading.listed if item.get("probe_id")}


def _probe_status(probe_id: str | None, reading: _Reading | None) -> str:
    if reading is None:
        return "unknown"
    if not reading.ok:
        return "listing_failed"
    if not probe_id:
        return "unpinned"
    return "present" if fold_hardware_id(probe_id) in _probe_ids(reading) else "absent"


def _incident(entry: JsonObject, names: Mapping[str, list[str]], own_project: str) -> JsonObject:
    state = entry.get("state")
    reasons = entry.get("cleanup_reasons")
    devices = names.get(str(entry.get("resource")), [])
    # An unreadable record cannot say whose incident it is.
    own = None if state == "unreadable" else entry.get("project_resource") == own_project
    incident: JsonObject = {
        "devices": devices,
        "state": state,
        "quarantine_id": entry.get("quarantine_id"),
        "cleanup_reasons": [str(reason) for reason in reasons] if isinstance(reasons, list) else [],
        "own_project": own,
    }
    # Only where the record names it: one written by a version that did not
    # record what its policy permitted makes no claim either way.
    if isinstance(entry.get("auto_recoverable"), bool):
        incident["auto_recoverable"] = entry["auto_recoverable"]
    if own is None:
        incident["next_step"] = _UNREADABLE_NEXT_STEP
    elif own:
        device = devices[0] if devices else "{name}"
        incident["next_step"] = (
            f"Check the bench, then send POST /v1/devices/{device}/recover from 127.0.0.1 with an "
            "operator_statement saying what was checked."
        )
    else:
        incident["next_step"] = foreign_incident_next_step(entry)
    return incident


def _newest_logs(directory: Path) -> list[Path]:
    """The canonical ledger's newest logs, whatever each was named after. Not its digest sidecars or append locks."""
    try:
        paths = list(directory.iterdir())
    except OSError:
        return []
    dated: list[tuple[float, Path]] = []
    for path in paths:
        if path.name.endswith(_LEDGER_SIDECARS):
            continue
        with suppress(OSError):
            if path.is_file():
                dated.append((path.stat().st_mtime, path))
    return [path for _, path in sorted(dated, reverse=True)[:_LEDGER_FILE_LIMIT]]


def _events(text: str) -> Iterator[JsonObject]:
    """Each line of a ledger log that is an effect: every line the ledger writes carries its `time`."""
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("time"), str):
            yield event


def _ledger_entry(event: JsonObject, names: Mapping[str, list[str]], declared: set[str]) -> JsonObject:
    entry: JsonObject = {key: value for key, value in event.items() if key in _LEDGER_FIELDS and isinstance(value, (str, int, float, bool))}
    artifact = event.get("artifact")
    digests = [event.get(field) for field in _LEDGER_DIGEST_FIELDS] + [artifact.get("sha256") if isinstance(artifact, dict) else None]
    digest = next((value for value in digests if isinstance(value, str) and value), None)
    if digest is not None:
        entry["artifact_sha256"] = digest
    named: set[str] = set()
    resources = event.get("resources")
    for resource in resources if isinstance(resources, list) else []:
        named.update(names.get(str(resource), ()))
    for field in _LEDGER_DEVICE_FIELDS:
        value = event.get(field)
        if isinstance(value, str) and value in declared:
            named.add(value)
    entry["devices"] = sorted(named)
    return entry


def _names_port(host_port: JsonObject, port: ComPortConfig) -> bool:
    """Whether a host port is the configured one: by its device, or by the adapter serial an entry names, whose COM number may have moved."""
    if port_names_device(host_port, port.device):
        return True
    found = host_port.get("serial_number")
    if not port.serial_number or not isinstance(found, str):
        return False
    return fold_hardware_id(found) == fold_hardware_id(port.serial_number)


def _access_nodes(report: JsonObject) -> set[str]:
    """Every device node, and every link to one, that doctor's access checks name."""
    found: set[str] = set()
    for section in ("debuggers", "com_ports"):
        entries = report.get(section)
        for entry in entries.values() if isinstance(entries, dict) else ():
            access = entry.get("device_access") if isinstance(entry, dict) else None
            for field in ("node", "link"):
                value = access.get(field) if isinstance(access, dict) else None
                if isinstance(value, str):
                    found |= _spellings(value)
    return found


def _spellings(value: str) -> set[str]:
    """A value and, for a path, its last component, which names the device on its own."""
    return {value, value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]}
