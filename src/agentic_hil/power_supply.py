"""Bench power supplies: the type table, the config checks, the secret registry and the class layer.

A bench supply is declared under ``power_supplies`` in the authoritative
configuration. This module holds what the loader needs to read that section:

- ``POWER_SUPPLY_TYPES``, one static record per supply model this release can
  drive. A record states hardware bounds only. The limits an operator may run a
  bench at are the owner's, written in the configuration and never derived here.
- The parse of one entry, with its limit checks, and the checks that bind a
  supply to its ``com_ports`` entry and to the debuggers it powers.
- The registry of serials that identify a supply. A serial is a secret: it names
  one physical unit, so it is kept out of every refusal, and the loader registers
  the serials of a document before any check can refuse that document.

It also holds ``PowerSupplyService``, the class layer behind the four supply
tools. Every rule a supply call obeys lives there, the same for every backend:
the grants and the owner's limits before any contact, the lease, the unit's
identity, the device-side protection armed at the limits before any write, an
exact read-back after every write, and one connection per call, closed and
reported whatever happened. A backend only speaks its own protocol, through the
device-neutral interface in ``debugger.PowerSupplyBackend``.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from agentic_hil.config import ConfigError
from agentic_hil.contracts import invalid_argument
from agentic_hil.debugger import PowerSupplyBackend, UnboundPowerSupplyBackend, create_power_supply_backend
from agentic_hil.devices import Device, DeviceError, PowerSupplyDevice, debugger_device, lock_keys, power_supply_device
from agentic_hil.knowledge import (
    permission_denied_fields,
    permission_denied_summary,
    permission_key,
    remediation_fields,
)
from agentic_hil.types import (
    READ_FREE_CONFIG_VERSION,
    AgenticHILConfig,
    CanBusConfig,
    ChannelStatus,
    ComPortConfig,
    DebuggerConfig,
    JsonObject,
    PowerSupplyChannelConfig,
    PowerSupplyConfig,
    PowerSupplyFault,
    PowerSupplyPermissions,
    com_port_is_unbound,
    fold_hardware_id,
)

if TYPE_CHECKING:
    from agentic_hil.coordination import HardwareCoordinator, HardwareLease

# One record per supply model. Every record carries exactly these keys:
#
# - `channels`: how many output channels the model has.
# - `ranges`: per channel number (as text), the voltage and current maximum and
#   resolution, each a decimal string so it converts to `Decimal` exactly.
# - `measures`: whether the model reports measured voltage and current.
# - `output_readable`: whether the model reports its output state.
# - `protection` and `protection_readable`: per protection (`ovp`, `ocp`), how it
#   is armed and whether its threshold reads back. `unsupported` until a recording
#   of the unit has shown how it behaves.
# - `usb_ids`: the [vid, pid] pairs the model's USB serial bridge enumerates as.
#
# The rs3005p range and resolutions are datasheet drafts: the published figures
# for the model, not yet measured on a unit.
POWER_SUPPLY_TYPES: dict[str, dict[str, Any]] = {
    "rs3005p": {
        "channels": 1,
        "ranges": {
            "1": {
                "voltage": {"max": "30", "resolution": "0.01"},
                "current": {"max": "5", "resolution": "0.001"},
            }
        },
        "measures": {"voltage": True, "current": True},
        "output_readable": False,
        "protection": {"ovp": "unsupported", "ocp": "unsupported"},
        "protection_readable": {"ovp": False, "ocp": False},
        "usb_ids": [[0x0416, 0x5011]],
    },
}

# How long one exchange with a supply may take before the call gives up. A
# provisional default until the reply time of a unit has been measured.
DEFAULT_POWER_SUPPLY_TIMEOUT_S = 1.0

POWER_SUPPLY_PERMISSION_FLAGS = ("allow_set", "allow_output")

_QUANTITY_UNITS = {"voltage": "V", "current": "A"}
_CHANNEL_KEY = re.compile(r"[1-9][0-9]*")


# ---------------------------------------------------------------------------
# The secret registry.
#
# Kept for the life of the process, so a refusal written where no configuration
# is at hand can still be checked against the serials of every configuration
# this process has read.

_SECRETS_LOCK = threading.Lock()
_CONFIG_SECRETS: set[str] = set()


def secret_text(value: object) -> str | None:
    """The text a serial value stands for, or None when it is not a serial.

    A str is taken as written and an int that is not a bool by its decimal form
    (an unquoted numeric serial in YAML). Anything else, and anything blank, is
    not a serial and is never registered."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return None
    text = text.strip()
    return text or None


def config_secret_fields(raw_document: object) -> dict[str, str]:
    """The supply serials a raw document names, keyed by the dotted key holding each.

    Each supply's `idn_serial`, and the `serial_number` of the `com_ports` entry
    its `com_port` names. Read off the raw document with no validation at all,
    because this runs before the document has been checked."""
    found: dict[str, str] = {}
    if not isinstance(raw_document, Mapping):
        return found
    supplies = raw_document.get("power_supplies")
    ports = raw_document.get("com_ports")
    if not isinstance(supplies, Mapping):
        return found
    for name, entry in supplies.items():
        if not isinstance(entry, Mapping):
            continue
        idn_serial = secret_text(entry.get("idn_serial"))
        if idn_serial is not None:
            found[f"power_supplies.{name}.idn_serial"] = idn_serial
        port_name = entry.get("com_port")
        port = ports.get(port_name) if isinstance(ports, Mapping) and isinstance(port_name, str) else None
        if isinstance(port, Mapping):
            serial = secret_text(port.get("serial_number"))
            if serial is not None:
                found[f"com_ports.{port_name}.serial_number"] = serial
    return found


def supply_port_names(raw_document: object) -> set[str]:
    """The `com_ports` entries a raw document's supplies name."""
    if not isinstance(raw_document, Mapping):
        return set()
    supplies = raw_document.get("power_supplies")
    if not isinstance(supplies, Mapping):
        return set()
    return {entry["com_port"] for entry in supplies.values() if isinstance(entry, Mapping) and isinstance(entry.get("com_port"), str)}


def secret_bearing_fields(raw_document: object) -> frozenset[str]:
    """The dotted keys whose value a schema refusal of this document may not repeat.

    Each supply's `idn_serial`, and the `serial_number` and `resource_id` of every
    port a supply names. The refusal still names the field; only its value stays
    out, which also covers an unquoted numeric serial that no text match finds."""
    fields: set[str] = set()
    if isinstance(raw_document, Mapping) and isinstance(raw_document.get("power_supplies"), Mapping):
        fields.update(f"power_supplies.{name}.idn_serial" for name in raw_document["power_supplies"])
    for port in supply_port_names(raw_document):
        fields.add(f"com_ports.{port}.serial_number")
        fields.add(f"com_ports.{port}.resource_id")
    return frozenset(fields)


def register_secrets(values: Iterable[object]) -> None:
    """Add serials to this process's registry. Values that are not serials are ignored."""
    texts = {text for text in map(secret_text, values) if text is not None}
    if texts:
        with _SECRETS_LOCK:
            _CONFIG_SECRETS.update(texts)


def register_config_secrets(raw_document: object) -> None:
    """Register the supply serials of a raw document before anything can refuse it."""
    register_secrets(config_secret_fields(raw_document).values())


def registered_config_secrets() -> frozenset[str]:
    """Every supply serial this process has registered so far."""
    with _SECRETS_LOCK:
        return frozenset(_CONFIG_SECRETS)


def supply_usb_ids() -> frozenset[tuple[int, int]]:
    """The (vid, pid) pairs a supply model this release knows enumerates as."""
    return frozenset((vid, pid) for record in POWER_SUPPLY_TYPES.values() for vid, pid in record["usb_ids"])


def register_inventory_serials(ports: Iterable[object]) -> None:
    """Register the serial of every inventory entry whose USB identity is a supply's.

    Called with the host inventory before it is returned, so a serial the host
    reported for a supply's serial bridge is masked wherever it is printed later,
    whether or not any configuration names that unit yet."""
    known = supply_usb_ids()
    serials: list[object] = []
    for entry in ports:
        if not isinstance(entry, Mapping):
            continue
        vid, pid = entry.get("vid"), entry.get("pid")
        if isinstance(vid, int) and not isinstance(vid, bool) and isinstance(pid, int) and not isinstance(pid, bool) and (vid, pid) in known:
            serials.append(entry.get("serial_number"))
    register_secrets(serials)


def register_idn_serial(serial: object) -> None:
    """Register the serial a unit reported in its `*IDN?` answer."""
    register_secrets([serial])


def supply_secrets(config: AgenticHILConfig | None) -> frozenset[str]:
    """Every supply serial an output must not carry in clear.

    The `serial_number` of each supply's port and each `idn_serial` of `config`,
    and every serial this process has registered: those of each configuration it
    loaded, those the host inventory reported for a supply's USB identity, and
    those a unit reported in its `*IDN?` answer. Every value is stripped, and a
    blank one is no secret."""
    found = set(registered_config_secrets())
    if config is not None:
        supplies = getattr(config, "power_supplies", None) or {}
        com_ports = getattr(config, "com_ports", None) or {}
        for value in supply_config_secrets(supplies, com_ports).values():
            text = secret_text(value)
            if text is not None:
                found.add(text)
    return frozenset(found)


# ---------------------------------------------------------------------------
# Reading one entry.


def reject_legacy_power_supplies(raw: Mapping[str, Any], config_path: str, config_version: int) -> None:
    """A version 1 file may not declare a supply.

    Version 1 gates every read behind a grant, and a supply is reached only under
    the version 2 model, where a run's exclusive hold is what keeps a reading
    from being disturbed."""
    if "power_supplies" not in raw or config_version >= READ_FREE_CONFIG_VERSION:
        return
    raise ConfigError(
        "config_invalid",
        "A power_supplies section is read only from version 2 on, and this file is read under version 1. Move the "
        "file to version 2 or 3 first, then declare the supply.",
        {"field": "power_supplies", "path": config_path, "config_version": config_version},
    )


def power_supply_config(name: str, raw: Mapping[str, Any], config_path: str) -> PowerSupplyConfig:
    """One `power_supplies` entry, parsed against the record of its type."""
    prefix = f"power_supplies.{name}"
    type_name = raw.get("type")
    record = POWER_SUPPLY_TYPES.get(type_name) if isinstance(type_name, str) else None
    if record is None or not isinstance(type_name, str):
        raise ConfigError(
            "config_invalid",
            f"`{prefix}.type` names a power supply type this release cannot drive.",
            {
                "field": f"{prefix}.type",
                "path": config_path,
                "value": str(type_name)[:64],
                "allowed_values": sorted(POWER_SUPPLY_TYPES),
            },
        )
    ranges = record["ranges"]
    count = int(record["channels"])
    raw_channels = raw.get("channels")
    channels: dict[int, PowerSupplyChannelConfig] = {}
    for key, entry in (raw_channels.items() if isinstance(raw_channels, Mapping) else ()):
        field = f"{prefix}.channels.{key}"
        number = int(key) if isinstance(key, str) and _CHANNEL_KEY.fullmatch(key) else None
        if number is None or number > count:
            raise ConfigError(
                "config_invalid",
                f"`{field}` names a channel a {type_name} does not have: it has {count}, numbered from 1.",
                {"field": field, "path": config_path, "channel": str(key)[:16], "channel_count": count},
            )
        limits = entry if isinstance(entry, Mapping) else {}
        channel_range = (ranges[str(number)],)
        channels[number] = PowerSupplyChannelConfig(
            voltage_limit_v=power_supply_limit(limits.get("voltage_limit_v"), f"{field}.voltage_limit_v", "voltage", channel_range, config_path),
            current_limit_a=power_supply_limit(limits.get("current_limit_a"), f"{field}.current_limit_a", "current", channel_range, config_path),
        )
    every_channel = tuple(ranges.values())
    raw_permissions = raw.get("permissions")
    permissions = raw_permissions if isinstance(raw_permissions, Mapping) else {}
    idn_serial = raw.get("idn_serial")
    return PowerSupplyConfig(
        type=type_name,
        com_port=str(raw.get("com_port")),
        powers=tuple(str(item) for item in raw.get("powers") or ()),
        voltage_limit_v=power_supply_limit(raw.get("voltage_limit_v"), f"{prefix}.voltage_limit_v", "voltage", every_channel, config_path),
        current_limit_a=power_supply_limit(raw.get("current_limit_a"), f"{prefix}.current_limit_a", "current", every_channel, config_path),
        channels=channels,
        idn_serial=idn_serial.strip() if isinstance(idn_serial, str) and idn_serial.strip() else None,
        timeout_s=float(raw.get("timeout_s", DEFAULT_POWER_SUPPLY_TIMEOUT_S)),
        permissions=PowerSupplyPermissions(**{flag: bool(permissions.get(flag, False)) for flag in POWER_SUPPLY_PERMISSION_FLAGS}),
    )


def power_supply_limit(
    value: object,
    field: str,
    quantity: str,
    ranges: tuple[Mapping[str, Any], ...],
    config_path: str,
) -> Decimal | None:
    """One limit as an exact decimal, or None where the file names none.

    Refused, never rounded: a value that is not a number, a bool, a non-finite
    value, one at or below zero, one above the type's range and one off its
    resolution. A supply-wide limit has to fit at least one channel of the type;
    a channel's limit has to fit that channel."""
    if value is None:
        return None
    unit = _QUANTITY_UNITS[quantity]
    details: dict[str, Any] = {"field": field, "path": config_path, "unit": unit}
    limit: Decimal | None = None
    if not isinstance(value, bool) and isinstance(value, (int, float)):
        try:
            limit = Decimal(str(value))
        except InvalidOperation:  # pragma: no cover - str() of an int or float always parses
            limit = None
    if limit is None or not limit.is_finite() or limit <= 0:
        if limit is not None:
            details["value"] = str(limit) if limit.is_finite() else "non-finite"
        raise ConfigError(
            "config_invalid",
            f"`{field}` must be a finite number of {unit} above zero.",
            {**details, "expected_type": "number"},
        )
    bounds = [entry[quantity] for entry in ranges if limit <= Decimal(entry[quantity]["max"])]
    if not bounds:
        maximum = max((str(entry[quantity]["max"]) for entry in ranges), key=Decimal)
        raise ConfigError(
            "config_invalid",
            f"`{field}` is {limit} {unit}, above the {maximum} {unit} this supply type can deliver.",
            {**details, "value": str(limit), "maximum": maximum},
        )
    if not any(limit % Decimal(bound["resolution"]) == 0 for bound in bounds):
        resolution = str(bounds[0]["resolution"])
        raise ConfigError(
            "config_invalid",
            f"`{field}` is {limit} {unit}, which this supply type cannot set: it sets {quantity} in steps of {resolution} {unit}.",
            {**details, "value": str(limit), "resolution": resolution},
        )
    return limit


# ---------------------------------------------------------------------------
# Binding a supply to its port and its boards.


def supply_config_secrets(supplies: Mapping[str, PowerSupplyConfig], com_ports: Mapping[str, ComPortConfig]) -> dict[str, str]:
    """The serials that identify these supplies, keyed by the dotted key holding each."""
    found: dict[str, str] = {}
    for name, supply in supplies.items():
        if supply.idn_serial:
            found[f"power_supplies.{name}.idn_serial"] = supply.idn_serial
        port = com_ports.get(supply.com_port)
        if port is not None and port.serial_number:
            found[f"com_ports.{supply.com_port}.serial_number"] = port.serial_number
    return found


def validate_supply_ports(
    supplies: Mapping[str, PowerSupplyConfig],
    debuggers: Mapping[str, DebuggerConfig],
    com_ports: Mapping[str, ComPortConfig],
    can_buses: Mapping[str, CanBusConfig],
    config_path: str,
) -> None:
    """Each supply powers known boards through a port that is its own and names no serial.

    Runs before every generic check that could repeat a `resource_id`, so a supply
    port's `resource_id` that holds a serial is refused here, without its value,
    before any other refusal can print it. No refusal below carries a serial, an
    `idn_serial` or a supply port's `resource_id`."""
    if not supplies:
        return
    # Who uses each port. An esptool debugger flashes through the port it names,
    # so it is a consumer like a supply, and a supply on that port is the second.
    consumers: dict[str, str] = {
        debugger.com_port: f"debuggers.{name}.com_port"
        for name, debugger in debuggers.items()
        if debugger.type == "esptool" and debugger.com_port is not None
    }
    resource_ids = [(f"debuggers.{name}", entry.resource_id) for name, entry in debuggers.items()]
    resource_ids += [(f"com_ports.{name}", entry.resource_id) for name, entry in com_ports.items()]
    resource_ids += [(f"can_buses.{name}", entry.resource_id) for name, entry in can_buses.items()]
    secrets = supply_config_secrets(supplies, com_ports)
    for name, supply in supplies.items():
        prefix = f"power_supplies.{name}"
        for index, debugger_id in enumerate(supply.powers):
            if debugger_id not in debuggers:
                raise ConfigError(
                    "config_invalid",
                    f"`{prefix}.powers[{index}]` names a debugger this configuration does not have. A supply powers "
                    "the boards of configured debuggers, named as they are named under `debuggers`.",
                    {"field": f"{prefix}.powers[{index}]", "path": config_path, "value": debugger_id[:64], "debuggers": sorted(debuggers)},
                )
        port = com_ports.get(supply.com_port)
        if port is None:
            raise ConfigError(
                "config_invalid",
                f"`{prefix}.com_port` names no entry under `com_ports`. A supply is reached through a configured port.",
                {"field": f"{prefix}.com_port", "path": config_path, "value": supply.com_port[:64], "com_ports": sorted(com_ports)},
            )
        port_prefix = f"com_ports.{supply.com_port}"
        for key, present in (
            ("device", not com_port_is_unbound(port)),
            ("resource_id", bool(port.resource_id)),
            ("vid", port.vid is not None),
            ("pid", port.pid is not None),
        ):
            if not present:
                raise ConfigError(
                    "config_invalid",
                    f"`{port_prefix}.{key}` is missing, and the port of a power supply has to name its `device`, its "
                    f"`resource_id`, its `vid` and its `pid`, so the supply is opened only on the adapter it was "
                    f"declared on and holds a lock of its own. `{prefix}` uses this port.",
                    {"field": f"{port_prefix}.{key}", "path": config_path, "power_supply": name},
                )
        if not port.serial_number and not supply.idn_serial:
            raise ConfigError(
                "config_invalid",
                f"`{prefix}.idn_serial` is required, because `{port_prefix}` names no `serial_number`. Without one of "
                "the two, nothing tells this unit apart from another unit of the same model.",
                {"field": f"{prefix}.idn_serial", "path": config_path, "com_port": supply.com_port},
            )
        other = consumers.get(supply.com_port)
        if other is not None:
            raise ConfigError(
                "config_invalid",
                f"`{prefix}.com_port` names a port that `{other}` already uses. A supply's port is reserved for that "
                "supply alone, so no other entry may open it.",
                {"field": f"{prefix}.com_port", "path": config_path, "other_consumer": other},
            )
        consumers[supply.com_port] = f"{prefix}.com_port"
        resource_id = str(port.resource_id)
        folded = fold_hardware_id(resource_id)
        for entry, other_resource_id in resource_ids:
            if entry != port_prefix and other_resource_id and fold_hardware_id(other_resource_id) == folded:
                raise ConfigError(
                    "config_invalid",
                    f"`{port_prefix}.resource_id` is also the resource_id of `{entry}`, in this or another case. The "
                    "port of a power supply has a lock of its own, shared with no other entry.",
                    {"field": f"{port_prefix}.resource_id", "path": config_path, "other_entry": entry, "power_supply": name},
                )
        lowered = resource_id.casefold()
        for secret_field, secret in secrets.items():
            if secret.casefold() in lowered:
                raise ConfigError(
                    "config_invalid",
                    f"`{port_prefix}.resource_id` contains the serial held in `{secret_field}`. A resource_id becomes "
                    "a lock key and is printed wherever locks are, so the port of a power supply needs one that "
                    "names no serial.",
                    {"field": f"{port_prefix}.resource_id", "path": config_path, "secret_field": secret_field, "power_supply": name},
                )


# ---------------------------------------------------------------------------
# The class layer.

def _rs3005p_backend(config: AgenticHILConfig, supply_id: str) -> PowerSupplyBackend:
    """The RS-3005P backend for `power_supplies.<supply_id>`, over the port it names; nothing is opened yet.

    Imported when a call needs it, so loading this module never loads the
    backends package."""
    from agentic_hil.backends.rs3005p import rs3005p_factory

    return rs3005p_factory(config, supply_id)


# One factory per supply type this build can drive, keyed by the type name of
# `POWER_SUPPLY_TYPES`. A factory opens one connection to one configured supply;
# the class layer calls it once per tool call and closes what it returns. A
# configured type with no factory here is answered by `UnboundPowerSupplyBackend`.
POWER_SUPPLY_BACKENDS: dict[str, Callable[[AgenticHILConfig, str], PowerSupplyBackend]] = {
    "rs3005p": _rs3005p_backend,
}

# The bounds of `power_supply_cycle`'s off time, in seconds. The tool schema
# states the same two numbers, and the class layer checks them again so a
# caller that reaches it another way meets the same rule.
CYCLE_OFF_MIN_S = 0.5
CYCLE_OFF_MAX_S = 30.0

POWER_SUPPLY_TOOLS = ("power_supply_status", "power_supply_set", "power_supply_output", "power_supply_cycle")
POWER_SUPPLY_WRITE_TOOLS = frozenset({"power_supply_set", "power_supply_output", "power_supply_cycle"})

# What a refusal before the first command carries: nothing was sent, so nothing changed.
NOT_STARTED: JsonObject = {"side_effect_committed": False, "side_effect_status": "not_started", "hardware_state": "unchanged"}

_LIMIT_FIELDS = (("voltage_v", "voltage", "V"), ("current_a", "current", "A"))
_PROTECTIONS = (("ovp", "ovp_threshold_v", "voltage_v"), ("ocp", "ocp_threshold_a", "current_a"))
_SET_POINTS = (("voltage_v", "set_voltage_v"), ("current_a", "set_current_a"))


def _write_order(requested: Mapping[str, Decimal], before: ChannelStatus) -> list[tuple[str, str]]:
    """The requested set points, every one that lowers its stored value before any that raises it.

    Written the other way round, the supply would hold the higher voltage and
    the higher current together between the two writes, a pair neither request
    allows. A stored value that did not read back counts as raised."""
    asked = [(name, attribute) for name, attribute in _SET_POINTS if name in requested]

    def lowers(entry: tuple[str, str]) -> bool:
        stored = getattr(before, entry[1])
        return stored is not None and requested[entry[0]] < stored

    return [entry for entry in asked if lowers(entry)] + [entry for entry in asked if not lowers(entry)]


def sleep(seconds: float) -> None:
    """The wait between output off and output on in a power cycle."""
    time.sleep(seconds)


def _text(value: Decimal | None) -> str | None:
    """An exact decimal as the text a result carries, never a float."""
    return None if value is None else str(value)


def effective_limits(supply: PowerSupplyConfig, channel: int) -> dict[str, Decimal | None]:
    """The lower of the supply limit and the channel limit, per quantity; None where either is unwritten."""
    own = supply.channels.get(channel) or PowerSupplyChannelConfig()
    pairs = (
        ("voltage_v", supply.voltage_limit_v, own.voltage_limit_v),
        ("current_a", supply.current_limit_a, own.current_limit_a),
    )
    return {name: (min(whole, part) if whole is not None and part is not None else None) for name, whole, part in pairs}


def missing_limit_keys(supply_id: str, supply: PowerSupplyConfig, channel: int) -> list[str]:
    """The dotted keys of every limit a write on this channel needs and the configuration does not set."""
    own = supply.channels.get(channel) or PowerSupplyChannelConfig()
    prefix = f"power_supplies.{supply_id}"
    found: list[str] = []
    if supply.voltage_limit_v is None:
        found.append(f"{prefix}.voltage_limit_v")
    if supply.current_limit_a is None:
        found.append(f"{prefix}.current_limit_a")
    if own.voltage_limit_v is None:
        found.append(f"{prefix}.channels.{channel}.voltage_limit_v")
    if own.current_limit_a is None:
        found.append(f"{prefix}.channels.{channel}.current_limit_a")
    return sorted(found)


def missing_limits_summary(supply_id: str, channel: int, keys: list[str]) -> str:
    return (
        f"Writes on power supply '{supply_id}' channel {channel} were refused before contact: the authoritative "
        f"configuration sets no {', '.join(keys)}."
    )


def missing_limits_next_step(keys: list[str]) -> str:
    return (
        "This refusal is the answer to the request. Report it and stop. Writes on this channel stay refused until the "
        f"authoritative configuration sets {', '.join(keys)}. Only the owner writes these limits, by hand in the "
        "authoritative configuration file, and then restarts the Agentic HIL server, because the power_supplies "
        "section is read only at startup. No tool on this server writes them. You must not propose, estimate or derive "
        "a value for any of them, whether from the supply's capability range, its present set point or a datasheet, "
        "and you must not drive the supply another way."
    )


def channel_json(channel: int, status: ChannelStatus) -> JsonObject:
    """One channel reading as a result carries it: every value exact, every unread one null."""
    return {
        "channel": channel,
        "set_voltage_v": _text(status.set_voltage_v),
        "set_current_a": _text(status.set_current_a),
        "measured_voltage_v": _text(status.measured_voltage_v),
        "measured_current_a": _text(status.measured_current_a),
        "output_on": status.output_on,
        "regulation": status.regulation,
        "protection_tripped": status.protection_tripped,
        "ovp_threshold_v": _text(status.ovp_threshold_v),
        "ocp_threshold_a": _text(status.ocp_threshold_a),
        "read_at": status.read_at,
    }


def _limits_json(limits: dict[str, Decimal | None]) -> JsonObject:
    return {name: _text(value) for name, value in limits.items()}


def _protection_block(record: Mapping[str, Any], status: ChannelStatus | None) -> JsonObject:
    """The protection block of a channel this call did not arm: what the type supports and what reads back."""
    readable = record["protection_readable"]
    block: JsonObject = {}
    for name, attribute, _ in _PROTECTIONS:
        read = getattr(status, attribute) if status is not None and readable[name] else None
        block[name] = {"support": record["protection"][name], "armed": None, "threshold": None, "threshold_read": _text(read)}
    return block


@dataclass
class _Call:
    """One supply tool call: what was asked, and what has been done on the supply so far."""

    tool: str
    supply_id: str
    supply: PowerSupplyConfig
    record: Mapping[str, Any]
    channels: list[int]
    requested: dict[str, Decimal] = dataclass_field(default_factory=dict)
    on: bool | None = None
    off_s: float | None = None
    limits: dict[str, Decimal | None] = dataclass_field(default_factory=dict)
    model: str | None = None
    firmware: str | None = None
    # Set before the first command that changes the supply is sent. From then
    # on a lost reply leaves the supply in a state nobody has read.
    wrote: bool = False
    last: ChannelStatus | None = None
    last_channel: int | None = None
    # A set point the supply read back above the owner's limit: the field, the
    # value and the limit, which hold the bench until the owner has checked it.
    above_limit: JsonObject | None = None

    @property
    def channel(self) -> int:
        return self.channels[0]

    def known_output(self) -> JsonObject:
        """The last output state this call read, and when; all null before the first read."""
        if self.last is None:
            return {"channel": self.channels[0] if len(self.channels) == 1 else None, "output_on": None, "read_at": None}
        return {"channel": self.last_channel, "output_on": self.last.output_on, "read_at": self.last.read_at}


def _unknown_output(channel: int | None = None) -> JsonObject:
    return {"channel": channel, "output_on": None, "read_at": None}


class PowerSupplyService:
    """The four supply tools, with the same rules for every backend.

    A call that changes the supply runs in this order: the checks that need no
    contact (arguments, grant, the owner's limits, what the type can read
    back, the requested values against the limits), this server's own debug and
    COM sessions on a board the supply powers, the port's USB identity, the
    lease, the unit's own identity, the stored set points against the limits
    before an output is switched on, the device-side protection armed at the
    limits, and then the writes, each read back exactly. `power_supply_status`
    reads and never writes. Each call opens one connection and closes it, and
    writes one report carrying the connection's transcript.

    `com_sessions` and `debug_session` are how this server's own sessions are
    seen: read-only callables returning the lock keys each open COM session
    holds, by port id, and the lock keys the open debug session holds, empty
    when there is none."""

    def __init__(
        self,
        config: AgenticHILConfig,
        coordinator: HardwareCoordinator,
        *,
        com_sessions: Callable[[], Mapping[str, Sequence[str]]] | None = None,
        debug_session: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        self.config = config
        self.coordinator = coordinator
        self.com_sessions: Callable[[], Mapping[str, Sequence[str]]] = com_sessions or (lambda: {})
        self.debug_session: Callable[[], Sequence[str]] = debug_session or (lambda: ())

    def reconfigure(self, config: AgenticHILConfig) -> None:
        self.config = config

    def status(self, args: JsonObject) -> JsonObject:
        return self._call("power_supply_status", args)

    def set(self, args: JsonObject) -> JsonObject:
        return self._call("power_supply_set", args)

    def output(self, args: JsonObject) -> JsonObject:
        return self._call("power_supply_output", args)

    def cycle(self, args: JsonObject) -> JsonObject:
        return self._call("power_supply_cycle", args)

    def refuses_before_contact(self, tool: str, args: JsonObject) -> bool:
        """Whether this call ends at a check that needs no contact, so it holds nothing.

        Asked before a bare write is wrapped in a run of its own: a call refused
        here leases nothing, and a run declared around it would only meet this
        server's own session on a powered board as a hold that names nobody."""
        try:
            device = power_supply_device(self.config, str(args.get("supply_id", "")))
        except DeviceError:
            return True
        return not isinstance(self._plan(tool, device, args), _Call)

    # -- before contact -----------------------------------------------------

    def _call(self, tool: str, args: JsonObject) -> JsonObject:
        from agentic_hil.report import write_report

        try:
            device = power_supply_device(self.config, str(args.get("supply_id", "")))
        except DeviceError as error:
            refusal: JsonObject = {"tool": tool, **error.result}
            refusal.update(NOT_STARTED)
            refusal["last_known_output"] = _unknown_output()
            return write_report(self.config, refusal)
        planned = self._plan(tool, device, args)
        if not isinstance(planned, _Call):
            planned.update(NOT_STARTED)
            planned.setdefault("supply_id", device.config_id)
            planned["last_known_output"] = _unknown_output(args.get("channel") if isinstance(args.get("channel"), int) else None)
            return write_report(self.config, planned)
        return self._contact(device, planned)

    def _plan(self, tool: str, device: PowerSupplyDevice, args: JsonObject) -> _Call | JsonObject:
        """Every check that needs no contact, in order; the call to make, or the refusal."""
        supply_id = device.config_id
        supply = device.supply
        record = POWER_SUPPLY_TYPES[supply.type]
        count = int(record["channels"])
        raw_channel = args.get("channel")
        if raw_channel is None:
            if count != 1 and tool != "power_supply_status":
                return invalid_argument(tool, "channel", "required", f"Power supply '{supply_id}' has {count} channels, so `channel` has to name one, numbered from 1.")
            channels = list(range(1, count + 1)) if tool == "power_supply_status" else [1]
        else:
            if isinstance(raw_channel, bool) or not isinstance(raw_channel, int):
                return invalid_argument(tool, "channel", "type", "`channel` must be an integer.")
            if raw_channel < 1:
                return invalid_argument(tool, "channel", "minimum", "`channel` is numbered from 1.")
            if raw_channel > count:
                return invalid_argument(tool, "channel", "maximum", f"Power supply '{supply_id}' has {count} channel(s), numbered from 1; it has no channel {raw_channel}.")
            channels = [raw_channel]
        call = _Call(tool=tool, supply_id=supply_id, supply=supply, record=record, channels=channels)
        refusal = self._arguments(call, args)
        if refusal is not None:
            return refusal
        if supply.type not in POWER_SUPPLY_BACKENDS:
            from agentic_hil.tools import unbound_power_supply_error

            return unbound_power_supply_error(self.config, tool, supply_id)
        if tool not in POWER_SUPPLY_WRITE_TOOLS:
            return call
        flag = "allow_set" if tool == "power_supply_set" else "allow_output"
        if not getattr(supply.permissions, flag):
            return power_supply_grant_closed_refusal(tool, supply_id, call.channel, flag)
        missing = missing_limit_keys(supply_id, supply, call.channel)
        if missing:
            return power_supply_missing_limits_refusal(tool, supply_id, call.channel, missing)
        if tool != "power_supply_set" and not record["output_readable"]:
            return power_supply_output_unreadable_refusal(tool, supply_id, supply.type, call.channel)
        call.limits = effective_limits(supply, call.channel)
        for name, value in call.requested.items():
            limit = call.limits[name]
            if limit is not None and value > limit:
                return power_supply_limit_exceeded_refusal(tool, supply_id, call.channel, name, value, limit, stored=False)
        return self._own_sessions(device, call) or call

    def _own_sessions(self, device: PowerSupplyDevice, call: _Call) -> JsonObject | None:
        """This server's own sessions on a board the supply powers, before the lease.

        A call that changes the supply locks the supply and every board it
        powers. A session this server keeps on one of those lock keys would meet
        that lock as a hold that names nobody, so it is checked here, the debug
        session first and then the COM sessions in port order, and the refusal
        names the session and the call that ends it. Nothing reaches the supply."""
        powered = sorted(call.supply.powers)
        debug_keys = set(self.debug_session())
        if debug_keys:
            for name in powered:
                if debug_keys & set(lock_keys([debugger_device(self.config, name)])):
                    return power_supply_held_by_debug_session_refusal(call.tool, call.supply_id, call.channel, name)
        keys = set(lock_keys([device, *(debugger_device(self.config, name) for name in powered)]))
        for port_id, resources in sorted(self.com_sessions().items()):
            if keys & set(resources):
                return power_supply_held_by_com_session_refusal(call.tool, call.supply_id, call.channel, port_id)
        return None

    def _arguments(self, call: _Call, args: JsonObject) -> JsonObject | None:
        tool = call.tool
        if tool == "power_supply_set":
            ranges = call.record["ranges"][str(call.channel)]
            for name, quantity, unit in _LIMIT_FIELDS:
                value = args.get(name)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return invalid_argument(tool, name, "type", f"`{name}` must be a number of {unit}.")
                parsed = Decimal(str(value))
                if not parsed.is_finite():
                    return invalid_argument(tool, name, "finite", f"`{name}` must be a finite number of {unit}.")
                if parsed < 0:
                    return invalid_argument(tool, name, "minimum", f"`{name}` cannot be below zero.")
                resolution = Decimal(ranges[quantity]["resolution"])
                try:
                    off_step = parsed % resolution != 0
                except InvalidOperation:
                    # So far above any range that the step itself cannot be
                    # computed at this precision; refused here, never raised.
                    return invalid_argument(
                        tool, name, "maximum",
                        f"`{name}` is {parsed} {unit}, far above the {ranges[quantity]['max']} {unit} this supply type can set at most.",
                    )
                if off_step:
                    return invalid_argument(
                        tool, name, "multipleOf",
                        f"`{name}` is {parsed} {unit}, which this supply type cannot set: it sets {quantity} in steps of {resolution} {unit}. Nothing is rounded.",
                    )
                call.requested[name] = parsed
            if not call.requested:
                return invalid_argument(tool, "voltage_v", "required", "`power_supply_set` needs `voltage_v`, `current_a` or both.")
        elif tool == "power_supply_output":
            on = args.get("on")
            if not isinstance(on, bool):
                return invalid_argument(tool, "on", "type", "`on` must be true or false.")
            call.on = on
        elif tool == "power_supply_cycle":
            off_s = args.get("off_s")
            if isinstance(off_s, bool) or not isinstance(off_s, (int, float)):
                return invalid_argument(tool, "off_s", "type", "`off_s` must be a number of seconds.")
            if off_s != off_s or off_s in {float("inf"), float("-inf")}:
                return invalid_argument(tool, "off_s", "finite", "`off_s` must be a finite number of seconds.")
            if off_s < CYCLE_OFF_MIN_S:
                return invalid_argument(tool, "off_s", "minimum", f"`off_s` is at least {CYCLE_OFF_MIN_S} s.")
            if off_s > CYCLE_OFF_MAX_S:
                return invalid_argument(tool, "off_s", "maximum", f"`off_s` is at most {CYCLE_OFF_MAX_S:g} s.")
            call.off_s = float(off_s)
        return None

    # -- contact ------------------------------------------------------------

    def _contact(self, device: PowerSupplyDevice, call: _Call) -> JsonObject:
        from agentic_hil.comports import verify_port_identity
        from agentic_hil.coordination import CoordinationError
        from agentic_hil.report import write_report

        identity = verify_port_identity(self.config, call.supply.com_port, call.tool)
        if not identity["ok"]:
            refused: JsonObject = {**identity, "supply_id": call.supply_id}
            refused["last_known_output"] = call.known_output()
            return write_report(self.config, refused)
        resources: list[Device | str] = [device]
        if call.tool in POWER_SUPPLY_WRITE_TOOLS:
            resources += [debugger_device(self.config, name) for name in call.supply.powers]
        try:
            lease = self.coordinator.acquire(*resources)
        except CoordinationError as error:
            held: JsonObject = {"tool": call.tool, "supply_id": call.supply_id, **error.result, "side_effect_committed": False}
            held["last_known_output"] = call.known_output()
            return write_report(self.config, held)
        backend = create_power_supply_backend(self.config, call.supply_id)
        if isinstance(backend, UnboundPowerSupplyBackend):
            from agentic_hil.tools import unbound_power_supply_error

            unbound = unbound_power_supply_error(self.config, call.tool, call.supply_id)
            unbound["last_known_output"] = call.known_output()
            return self._commit(lease, unbound, [])
        evidence: list[JsonObject] = []
        try:
            result = self._converse(backend, lease, call)
        finally:
            try:
                evidence = list(backend.evidence())
            except Exception as error:
                evidence = [{"evidence_error": str(error)}]
            backend.close()
        return self._commit(lease, result, evidence)

    def _converse(self, backend: PowerSupplyBackend, lease: HardwareLease, call: _Call) -> JsonObject:
        try:
            result = self._exchange(backend, call)
        except PowerSupplyFault as fault:
            if fault.error_type != "power_supply_no_reply":
                if call.wrote:
                    lease.quarantine("power_supply_state_unconfirmed", fault)
                raise
            return self._no_reply(lease, call, fault, fault.summary, unconfirmed=call.wrote or fault.wrote, cause=fault.cause or "no_reply")
        except OSError as error:
            # The serial line failing under a backend that let pyserial's own
            # error through: the same fault as one the backend named, and the
            # supply is unconfirmed only once this call had sent it a write.
            return self._no_reply(lease, call, error, f"{type(error).__name__}: {error}", unconfirmed=call.wrote, cause="serial_error")
        except Exception as error:
            # A backend bug, contained by the dispatch above. Once a write was
            # sent, what the supply holds is unknown, and the lease says so.
            if call.wrote:
                lease.quarantine("power_supply_state_unconfirmed", error)
            raise
        if call.above_limit is not None:
            # The supply answered with a set point above the limit. Its state is
            # known and outside what the owner allowed, so no next call may
            # proceed from it until the owner has brought the unit back.
            lease.quarantine("power_supply_above_limit", result["summary"])
        return result

    def _no_reply(self, lease: HardwareLease, call: _Call, error: BaseException, backend_error: str, *, unconfirmed: bool, cause: str) -> JsonObject:
        if unconfirmed:
            lease.quarantine("power_supply_state_unconfirmed", error)
        result = power_supply_no_reply_refusal(
            call.tool, call.supply_id, call.last_channel or call.channel, backend_error, unconfirmed=unconfirmed, cause=cause
        )
        if not unconfirmed:
            result.update(NOT_STARTED)
        result["last_known_output"] = call.known_output()
        return result

    def _read(self, backend: PowerSupplyBackend, call: _Call, channel: int) -> ChannelStatus:
        status = backend.channel_status(channel)
        call.last = status
        call.last_channel = channel
        return status

    def _exchange(self, backend: PowerSupplyBackend, call: _Call) -> JsonObject:
        identity = backend.identity()
        register_idn_serial(identity.serial)
        call.model, call.firmware = identity.model, identity.firmware
        if not identity.accepted:
            unknown = power_supply_identity_unknown_refusal(call.tool, call.supply_id, call.supply.type, identity.model, identity.firmware)
            unknown["last_known_output"] = call.known_output()
            return unknown
        expected = call.supply.idn_serial
        if expected is not None and secret_text(identity.serial) != expected:
            mismatch = power_supply_identity_mismatch_refusal(call.tool, call.supply_id, identity.model, identity.firmware, expected, secret_text(identity.serial))
            mismatch["last_known_output"] = call.known_output()
            return mismatch
        if call.tool == "power_supply_status":
            return self._status(backend, call)
        return self._write(backend, call)

    def _status(self, backend: PowerSupplyBackend, call: _Call) -> JsonObject:
        channels: list[JsonObject] = []
        limits: JsonObject = {}
        protection: JsonObject = {}
        missing: set[str] = set()
        refused: list[str] = []
        for channel in call.channels:
            status = self._read(backend, call, channel)
            channels.append(channel_json(channel, status))
            limits[str(channel)] = _limits_json(effective_limits(call.supply, channel))
            protection[str(channel)] = _protection_block(call.record, status)
            keys = missing_limit_keys(call.supply_id, call.supply, channel)
            if keys:
                missing.update(keys)
                refused.append(missing_limits_summary(call.supply_id, channel, keys))
        return {
            "ok": True,
            "tool": call.tool,
            "supply_id": call.supply_id,
            "supply_type": call.supply.type,
            "model": call.model,
            "firmware": call.firmware,
            "channels": channels,
            "limits": limits,
            "missing_limits": sorted(missing),
            "writes_refused_reason": " ".join(refused) if refused else None,
            "protection": protection,
            "last_known_output": call.known_output(),
            "summary": f"Read power supply '{call.supply_id}': {len(channels)} channel(s), nothing written.",
        }

    def _write(self, backend: PowerSupplyBackend, call: _Call) -> JsonObject:
        channel = call.channel
        before = self._read(backend, call, channel)
        # The set point already stored reaches the board the moment the output
        # switches on, and stays beside a set that leaves it, so it is held to
        # the limits like a request: before an output on or a cycle, and for
        # every quantity a set does not write. Nothing is lowered unasked.
        held = (
            [name for name, _ in _SET_POINTS if name not in call.requested]
            if call.tool == "power_supply_set"
            else [name for name, _ in _SET_POINTS]
            if call.tool == "power_supply_cycle" or call.on is True
            else []
        )
        for name, attribute in _SET_POINTS:
            if name not in held:
                continue
            stored = getattr(before, attribute)
            limit = call.limits[name]
            if limit is not None and (stored is None or stored > limit):
                exceeded = power_supply_limit_exceeded_refusal(call.tool, call.supply_id, channel, name, stored, limit, stored=True)
                exceeded["last_known_output"] = call.known_output()
                return exceeded
        protection = _protection_block(call.record, before)
        if call.tool != "power_supply_output" or call.on is True:
            failure = self._arm(backend, call, protection)
            if failure is not None:
                failure["last_known_output"] = call.known_output()
                return failure
        requested: JsonObject
        read_back: JsonObject
        if call.tool == "power_supply_set":
            requested = {name: str(value) for name, value in call.requested.items()}
            read_back = {}
            for name, attribute in _write_order(call.requested, before):
                value = call.requested[name]
                setter = backend.set_voltage if name == "voltage_v" else backend.set_current
                self._send(call, setter, channel, value)
                read = getattr(self._read(backend, call, channel), attribute)
                read_back[name] = _text(read)
                if read != value:
                    limit = call.limits[name]
                    if read is not None and limit is not None and read > limit:
                        call.above_limit = {"field": name, "read_back": _text(read), "limit": _text(limit)}
                    return self._mismatch(call, requested, read_back)
        else:
            steps = [False, True] if call.tool == "power_supply_cycle" else [bool(call.on)]
            requested = {"output_on": steps[-1]}
            read_back = {}
            for position, on in enumerate(steps):
                if position:
                    sleep(float(call.off_s or CYCLE_OFF_MIN_S))
                self._send(call, backend.set_output, channel, on)
                state = self._read(backend, call, channel).output_on
                read_back = {"output_on": state}
                if state is not on:
                    return self._mismatch(call, {"output_on": on}, read_back)
        assert call.last is not None
        result: JsonObject = {
            "ok": True,
            "tool": call.tool,
            "supply_id": call.supply_id,
            "supply_type": call.supply.type,
            "channel": channel,
            "model": call.model,
            "firmware": call.firmware,
            "requested": requested,
            "read_back": read_back,
            "measured": {"voltage_v": _text(call.last.measured_voltage_v), "current_a": _text(call.last.measured_current_a)},
            "limits": {str(channel): _limits_json(call.limits)},
            "protection": {str(channel): protection},
            "channels": [channel_json(channel, call.last)],
            "last_known_output": call.known_output(),
            "side_effect_committed": True,
            "side_effect_status": "committed",
            "summary": _success_summary(call, requested),
        }
        if call.off_s is not None:
            result["off_s"] = call.off_s
        return result

    def _send(self, call: _Call, operation: Callable[..., None], *arguments: Any) -> None:
        """One backend operation that may change the supply, and whether it did.

        A fault the backend raises says itself whether anything was sent, so a
        refusal that sent nothing leaves the call as it was. Any other error
        leaves that unknown, and the call counts as having written."""
        try:
            operation(*arguments)
        except PowerSupplyFault as fault:
            call.wrote = call.wrote or fault.wrote
            raise
        except BaseException:
            call.wrote = True
            raise
        call.wrote = True

    def _mismatch(self, call: _Call, requested: JsonObject, read_back: JsonObject) -> JsonObject:
        failure = power_supply_readback_mismatch_refusal(call.tool, call.supply_id, call.channel, requested, read_back)
        if call.above_limit is not None:
            above = call.above_limit
            failure["read_back_above_limit"] = above
            failure["summary"] += (
                f" The read-back {above['field']} {above['read_back']} is above the limit {above['limit']}: "
                "the bench is held until the owner has brought the unit within the limit."
            )
        failure["last_known_output"] = call.known_output()
        return failure

    def _arm(self, backend: PowerSupplyBackend, call: _Call, protection: JsonObject) -> JsonObject | None:
        """Arm every threshold protection of the channel at the effective limits, and confirm what reads back."""
        channel = call.channel
        support = call.record["protection"]
        readable = call.record["protection_readable"]
        thresholds: dict[str, Decimal | None] = {
            name: call.limits[limit_name] if support[name] == "threshold" else None for name, _, limit_name in _PROTECTIONS
        }
        if all(value is None for value in thresholds.values()):
            return None
        first = next(name for name, value in thresholds.items() if value is not None)
        try:
            self._send(call, backend.arm_protection, channel, thresholds["ovp"], thresholds["ocp"])
        except PowerSupplyFault as fault:
            if fault.error_type != "power_supply_protection_not_armed":
                raise
            return power_supply_protection_not_armed_refusal(
                call.tool, call.supply_id, channel, first, "backend_fault", thresholds[first], None, wrote=fault.wrote
            )
        armed = [name for name, value in thresholds.items() if value is not None]
        status = self._read(backend, call, channel) if any(readable[name] for name in armed) else None
        for name, attribute, _ in _PROTECTIONS:
            value = thresholds[name]
            if value is None:
                continue
            entry = protection[name]
            entry["threshold"] = _text(value)
            if not readable[name] or status is None:
                entry["armed"] = "unconfirmed"
                continue
            read = getattr(status, attribute)
            entry["threshold_read"] = _text(read)
            if read is None or read != value:
                cause = "readback_missing" if read is None else "readback_mismatch"
                return power_supply_protection_not_armed_refusal(call.tool, call.supply_id, channel, name, cause, value, read, wrote=True)
            entry["armed"] = "confirmed"
        return None

    # -- after contact --------------------------------------------------------

    def _commit(self, lease: HardwareLease, result: JsonObject, evidence: list[JsonObject]) -> JsonObject:
        """Report the call with its transcript, give the lease back unless it holds an incident, and answer."""
        from agentic_hil.report import recommit_report_with_status, report_write_failed, write_report

        prepared = {**result, "backend_evidence": evidence, **lease.status()}
        written = write_report(self.config, prepared)
        if report_write_failed(prepared, written):
            lease.quarantine("power_supply_report_audit_broken", audit_broken=True)
            final = write_report(self.config, {**written, **lease.status()})
        elif lease.status()["quarantined"]:
            final = written
        elif not lease.release():
            unreleased: JsonObject = {**written, **lease.status(), "ok": False, "cleanup_required": True}
            unreleased["summary"] = "Power supply lease release remained unconfirmed."
            final = write_report(self.config, unreleased)
        else:
            final = recommit_report_with_status(self.config, written, lease.status())
        return {key: value for key, value in final.items() if key != "backend_evidence"}


def _success_summary(call: _Call, requested: JsonObject) -> str:
    where = f"power supply '{call.supply_id}' channel {call.channel}"
    if call.tool == "power_supply_set":
        values = " and ".join(f"{value} {'V' if name == 'voltage_v' else 'A'}" for name, value in requested.items())
        return f"Set {values} on {where}; every value read back exactly."
    if call.tool == "power_supply_cycle":
        return f"Cycled the output of {where}: off, {call.off_s:g} s, on, each state read back."
    return f"Switched the output of {where} {'on' if call.on else 'off'}; the state read back."


# ---------------------------------------------------------------------------
# The refusals of the class layer, one builder per kind.


def power_supply_missing_limits_refusal(tool: str, supply_id: str, channel: int, missing: list[str]) -> JsonObject:
    """D8: a write on a channel whose limits the configuration does not set is refused before contact."""
    result: JsonObject = {
        "ok": False,
        "tool": tool,
        "error_type": "permission_denied",
        "supply_id": supply_id,
        "channel": channel,
        "missing_limits": list(missing),
        "retry_safe": False,
        **remediation_fields("permission_denied", "power_supply_limits"),
    }
    result["summary"] = missing_limits_summary(supply_id, channel, missing)
    result["next_step"] = missing_limits_next_step(missing)
    result.update(NOT_STARTED)
    return result


def power_supply_held_by_debug_session_refusal(tool: str, supply_id: str, channel: int, debugger_id: str) -> JsonObject:
    """C3: this server's own debug session holds a debugger the supply powers; refused before contact."""
    result: JsonObject = {
        "ok": False,
        "tool": tool,
        "error_type": "device_busy",
        "summary": (
            f"This server's own debug session holds debugger '{debugger_id}', which power supply '{supply_id}' powers, "
            f"so {tool} was refused before anything was sent to the supply."
        ),
        "supply_id": supply_id,
        "channel": channel,
        "held_by_debug_session": True,
        "retry_safe": True,
        **remediation_fields("device_busy"),
    }
    result["next_step"] = (
        f"This server's own debug session holds debugger '{debugger_id}', which power supply '{supply_id}' powers, so "
        f"{tool} was refused before contact. Stop that session with debug_stop_session and call {tool} again."
    )
    result.update(NOT_STARTED)
    return result


def power_supply_held_by_com_session_refusal(tool: str, supply_id: str, channel: int, port_id: str) -> JsonObject:
    """C3: this server's own COM session holds a lock key of a board the supply powers; refused before contact."""
    result: JsonObject = {
        "ok": False,
        "tool": tool,
        "error_type": "device_busy",
        "summary": (
            f"This server's own COM session on '{port_id}' holds a lock key of the board that power supply "
            f"'{supply_id}' powers, so {tool} was refused before anything was sent to the supply."
        ),
        "supply_id": supply_id,
        "channel": channel,
        "held_by_com_session": port_id,
        "retry_safe": True,
        **remediation_fields("device_busy"),
    }
    result["next_step"] = (
        f"This server's own COM session on '{port_id}' holds the board that power supply '{supply_id}' powers, so "
        f"{tool} was refused before contact. Stop that session with com_session_stop and call {tool} again. A cold "
        "boot can be watched on a console only if the console's port shares no lock key with the debuggers this "
        "supply powers; only the owner can change that, in the authoritative configuration. See docs/power-supplies.md."
    )
    result.update(NOT_STARTED)
    return result


def power_supply_grant_closed_refusal(tool: str, supply_id: str, channel: int, flag: str) -> JsonObject:
    """A write the supply's grant does not open, refused before contact and naming its key."""
    key = permission_key("power_supplies", supply_id, flag)
    text = {
        "allow_set": f"Set points on power supply '{supply_id}' are not granted by the authoritative configuration.",
        "allow_output": f"Switching the output of power supply '{supply_id}' is not granted by the authoritative configuration.",
    }[flag]
    result: JsonObject = {
        "ok": False,
        "tool": tool,
        "error_type": "permission_denied",
        "summary": permission_denied_summary(text, key),
        "supply_id": supply_id,
        "channel": channel,
        "retry_safe": False,
    }
    result.update(permission_denied_fields(key))
    result.update(remediation_fields("permission_denied", permission=key))
    result.update(NOT_STARTED)
    return result


def power_supply_output_unreadable_refusal(tool: str, supply_id: str, supply_type: str, channel: int) -> JsonObject:
    """D7: a supply type that cannot read its output state back is never told to switch it."""
    result: JsonObject = {
        "ok": False,
        "tool": tool,
        "error_type": "not_supported",
        "summary": (
            f"Power supply '{supply_id}' is a {supply_type}, which cannot read its output state back, so `{tool}` is "
            "refused before contact: a switch nobody can read back would leave the board's power state unknown."
        ),
        "supply_id": supply_id,
        "supply_type": supply_type,
        "channel": channel,
        "capability": "output_state",
        "retry_safe": False,
        **remediation_fields("not_supported", "power_supply"),
    }
    result.update(NOT_STARTED)
    return result


def power_supply_limit_exceeded_refusal(
    tool: str, supply_id: str, channel: int, name: str, value: Decimal | None, limit: Decimal, *, stored: bool
) -> JsonObject:
    """A value above the channel's effective limit: the one asked for, or the set point already stored."""
    quantity, unit = ("voltage", "V") if name == "voltage_v" else ("current", "A")
    where = f"power supply '{supply_id}' channel {channel}"
    if not stored:
        summary = f"The requested {quantity} of {value} {unit} is above the effective limit of {limit} {unit} on {where}, so nothing was written."
    elif tool == "power_supply_set":
        shown = (
            "did not read back, so it cannot be shown to be within"
            if value is None
            else f"is {value} {unit}, above"
        )
        summary = (
            f"The {quantity} set point stored on {where} {shown} the effective limit of {limit} {unit}, and this call "
            f"leaves it as it is, so nothing was written: the call has to set `{name}` too, at or below {limit} {unit}. "
            "A stored value is never lowered unasked."
        )
    elif value is None:
        summary = (
            f"The {quantity} set point stored on {where} did not read back, so it cannot be shown to be within the "
            f"effective limit of {limit} {unit}, and the output was not switched."
        )
    else:
        summary = (
            f"The {quantity} set point stored on {where} is {value} {unit}, above the effective limit of {limit} {unit}, "
            "so the output was not switched: the stored value would reach the board the moment it turned on."
        )
    result: JsonObject = {
        "ok": False,
        "tool": tool,
        "error_type": "power_supply_limit_exceeded",
        "summary": summary,
        "supply_id": supply_id,
        "channel": channel,
        "field": name,
        "requested": _text(value),
        "limit": str(limit),
        "retry_safe": False,
        **remediation_fields("power_supply_limit_exceeded"),
    }
    result.update(NOT_STARTED)
    return result


def power_supply_identity_unknown_refusal(tool: str, supply_id: str, supply_type: str, model: str | None, firmware: str | None) -> JsonObject:
    """The unit answered with a model or firmware no verified record covers; only the identity query was sent."""
    result: JsonObject = {
        "ok": False,
        "tool": tool,
        "error_type": "power_supply_identity_unknown",
        "summary": (
            f"The unit behind power supply '{supply_id}' reported model {model!r} and firmware {firmware!r}, which this "
            f"server has no verified {supply_type} record for. Only the identity query was sent."
        ),
        "supply_id": supply_id,
        "supply_type": supply_type,
        "model": model,
        "firmware": firmware,
        "retry_safe": False,
        **remediation_fields("power_supply_identity_unknown"),
    }
    result.update(NOT_STARTED)
    return result


def power_supply_identity_mismatch_refusal(
    tool: str, supply_id: str, model: str | None, firmware: str | None, expected: str, reported: str | None
) -> JsonObject:
    """The unit reported a serial other than the configured `idn_serial`; only the identity query was sent."""
    result: JsonObject = {
        "ok": False,
        "tool": tool,
        "error_type": "power_supply_identity_mismatch",
        "summary": (
            f"The unit behind power supply '{supply_id}' reported a serial other than "
            f"`power_supplies.{supply_id}.idn_serial`, so another unit is on this port. Only the identity query was sent."
        ),
        "supply_id": supply_id,
        "model": model,
        "firmware": firmware,
        "expected_idn_serial": expected,
        "reported_idn_serial": reported,
        "retry_safe": False,
        **remediation_fields("power_supply_identity_mismatch"),
    }
    result.update(NOT_STARTED)
    return result


# What stood between this server and the supply, by `PowerSupplyFault.cause`.
_NO_REPLY_CAUSES = {
    "no_reply": "did not answer",
    "port_open_failed": "could not be reached because its port did not open",
    "write_timeout": "could not be reached because a write on its serial line timed out",
    "serial_error": "could not be reached because its serial line failed",
    "not_sent": "was not sent the command, because its backend refused it before sending anything",
}


def power_supply_no_reply_refusal(
    tool: str, supply_id: str, channel: int, backend_error: str, *, unconfirmed: bool, cause: str = "no_reply"
) -> JsonObject:
    """The supply did not answer, for `cause`; after a write that leaves its state unconfirmed."""
    what = _NO_REPLY_CAUSES.get(cause, _NO_REPLY_CAUSES["no_reply"])
    if unconfirmed:
        summary = (
            f"Power supply '{supply_id}' {what} after this call had sent it a write, so whether that write "
            "took effect is unconfirmed: the unit itself has to be checked before the bench is used again."
        )
    else:
        summary = f"Power supply '{supply_id}' {what}, and nothing that changes it had been sent."
    result: JsonObject = {
        "ok": False,
        "tool": tool,
        "error_type": "power_supply_no_reply",
        "summary": summary,
        "supply_id": supply_id,
        "channel": channel,
        "cause": cause,
        "backend_error": backend_error,
        "retry_safe": not unconfirmed,
        **remediation_fields("power_supply_no_reply"),
    }
    if unconfirmed:
        result.update({"side_effect_status": "unknown", "hardware_state": "unknown", "cleanup_required": True})
    return result


def power_supply_protection_not_armed_refusal(
    tool: str,
    supply_id: str,
    channel: int,
    protection: str,
    cause: str,
    requested: Decimal | None,
    read_back: Decimal | None,
    *,
    wrote: bool,
) -> JsonObject:
    """The device-side protection could not be armed at the limits, so nothing else was written."""
    how = {
        "backend_fault": "the supply refused the threshold",
        "readback_mismatch": f"the threshold read back as {read_back}",
        "readback_missing": "the threshold did not read back",
    }[cause]
    result: JsonObject = {
        "ok": False,
        "tool": tool,
        "error_type": "power_supply_protection_not_armed",
        "summary": (
            f"The {protection} protection of power supply '{supply_id}' channel {channel} could not be armed at "
            f"{requested}: {how}. No set point was written and no output switched."
        ),
        "supply_id": supply_id,
        "channel": channel,
        "protection": protection,
        "cause": cause,
        "requested": _text(requested),
        "read_back": _text(read_back),
        "retry_safe": False,
        **remediation_fields("power_supply_protection_not_armed"),
    }
    if wrote:
        result.update({"side_effect_committed": True, "side_effect_status": "partial", "hardware_state": "unknown"})
    else:
        result.update(NOT_STARTED)
    return result


def power_supply_readback_mismatch_refusal(tool: str, supply_id: str, channel: int, requested: JsonObject, read_back: JsonObject) -> JsonObject:
    """A write that read back as something else: the supply answered, so its state is the read-back."""
    told = ", ".join(f"{name} {value}" for name, value in requested.items())
    holds = ", ".join(f"{name} {value}" for name, value in read_back.items())
    result: JsonObject = {
        "ok": False,
        "tool": tool,
        "error_type": "power_supply_readback_mismatch",
        "summary": f"Power supply '{supply_id}' channel {channel} was told {told} and reads back {holds}.",
        "supply_id": supply_id,
        "channel": channel,
        "requested": requested,
        "read_back": read_back,
        "retry_safe": False,
        "side_effect_committed": True,
        "side_effect_status": "partial",
        **remediation_fields("power_supply_readback_mismatch"),
    }
    return result


__all__ = [
    "CYCLE_OFF_MAX_S",
    "CYCLE_OFF_MIN_S",
    "DEFAULT_POWER_SUPPLY_TIMEOUT_S",
    "NOT_STARTED",
    "POWER_SUPPLY_BACKENDS",
    "POWER_SUPPLY_PERMISSION_FLAGS",
    "POWER_SUPPLY_TOOLS",
    "POWER_SUPPLY_TYPES",
    "POWER_SUPPLY_WRITE_TOOLS",
    "PowerSupplyService",
    "channel_json",
    "config_secret_fields",
    "effective_limits",
    "missing_limit_keys",
    "power_supply_config",
    "power_supply_limit",
    "register_config_secrets",
    "register_idn_serial",
    "register_inventory_serials",
    "register_secrets",
    "registered_config_secrets",
    "reject_legacy_power_supplies",
    "secret_bearing_fields",
    "secret_text",
    "sleep",
    "supply_config_secrets",
    "supply_port_names",
    "supply_secrets",
    "supply_usb_ids",
    "validate_supply_ports",
]
