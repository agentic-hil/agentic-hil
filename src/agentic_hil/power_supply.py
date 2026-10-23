"""Bench power supplies: the type table, the config checks and the secret registry.

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
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from agentic_hil.config import ConfigError
from agentic_hil.types import (
    READ_FREE_CONFIG_VERSION,
    AgenticHILConfig,
    CanBusConfig,
    ComPortConfig,
    DebuggerConfig,
    PowerSupplyChannelConfig,
    PowerSupplyConfig,
    PowerSupplyPermissions,
    com_port_is_unbound,
    fold_hardware_id,
)

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


__all__ = [
    "DEFAULT_POWER_SUPPLY_TIMEOUT_S",
    "POWER_SUPPLY_PERMISSION_FLAGS",
    "POWER_SUPPLY_TYPES",
    "config_secret_fields",
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
    "supply_config_secrets",
    "supply_port_names",
    "supply_secrets",
    "supply_usb_ids",
    "validate_supply_ports",
]
