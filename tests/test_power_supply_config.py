"""Acceptance tests for the `power_supplies` config section (issue #731, sub-issue 1 of 5).

Every value is synthetic: serials and resource ids come from uuid4, and no real
bench limit, serial or COM device appears anywhere in this file.

Most tests load a document that carries a supply. On the current tree that load
reaches the product gap `agentic_hil.power_supply.register_config_secrets`, the
first step the loader takes for such a document, so those tests are the expected
failures of this stage. The rest pin behaviour that does not depend on the gap:
the pinned legacy OpenOCD reload, and `write_config` writing no section by default.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import write_authoritative_config, write_config

from agentic_hil.config import ConfigError, carry_over_permissions, config_schema, load_authoritative_config
from agentic_hil.configreload import reload_description
from agentic_hil.configwrite import resolve_permission_key
from agentic_hil.knowledge import CONFIG_PERMISSIONS_RIGHT, config_shape_document, resolve_config_key
from agentic_hil.power_supply import POWER_SUPPLY_TYPES

ROOT = Path(__file__).resolve().parents[1]
# Synthetic. The vid and pid happen to match the rs3005p's USB identity, which is
# not a serial and identifies no particular unit.
DEVICE = "COM250"
VID = "0416"
PID = "5011"
DASHES = (chr(0x2013), chr(0x2014))


@dataclass(frozen=True)
class Values:
    """Identifiers fresh for each test. Membership is the only thing checked against them."""

    serial: str
    other_serial: str
    resource: str
    number: int

    @classmethod
    def fresh(cls) -> Values:
        return cls(
            serial=f"SN{uuid.uuid4().hex.upper()}",
            other_serial=f"SN{uuid.uuid4().hex.upper()}",
            resource=f"AHIL-{uuid.uuid4().hex.upper()}",
            number=uuid.uuid4().int % 10**12,
        )

    def secrets(self) -> tuple[str, ...]:
        return (self.serial, self.other_serial, self.resource, str(self.number))


def port(serial: Any, resource: str, *, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    entry: dict[str, Any] = {"device": DEVICE, "resource_id": resource, "vid": VID, "pid": PID, "serial_number": serial}
    for key in drop:
        del entry[key]
    return entry


def supply(**changes: Any) -> dict[str, Any]:
    """A valid rs3005p entry on port `psu`, powering the debugger `dut`."""
    entry: dict[str, Any] = {"type": "rs3005p", "com_port": "psu", "powers": ["dut"]}
    entry.update(changes)
    return entry


def document(values: Values, **changes: Any) -> dict[str, Any]:
    """The keyword arguments `write_bench` takes for a valid bench, with `changes` replacing any part."""
    kwargs: dict[str, Any] = {
        "com_ports": {"psu": port(values.serial, values.resource)},
        "power_supplies": {"bench": supply()},
    }
    kwargs.update(changes)
    return kwargs


def write_bench(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    com_ports: dict[str, Any],
    power_supplies: dict[str, Any],
    **kwargs: Any,
) -> tuple[Path, Path]:
    """Write the authoritative file with these two sections, at version 3 unless a case asks otherwise."""
    workspace = (tmp_path / "workspace").resolve()
    kwargs.setdefault("config_version", 3)
    path = write_authoritative_config(
        workspace,
        monkeypatch,
        com_ports_yaml=yaml.safe_dump({"com_ports": com_ports}, sort_keys=False),
        power_supplies_yaml=yaml.safe_dump({"power_supplies": power_supplies}, sort_keys=False),
        **kwargs,
    )
    monkeypatch.chdir(workspace)
    return workspace, path


def rewrite(path: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    """Edit the file the way an operator would, and write it back in its own shape."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(raw)
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def name_the_debugger(raw: dict[str, Any], current: str, new: str, powers: list[str]) -> None:
    raw["debuggers"][new] = raw["debuggers"].pop(current)
    raw["power_supplies"]["bench"]["powers"] = powers


def close_the_entry_and_break_its_script_paths(raw: dict[str, Any]) -> None:
    """Every access grant narrowed to false, then script paths nothing in that file can run."""
    entry = raw["debuggers"]["dut"]
    entry["permissions"] = dict.fromkeys(entry["permissions"], False)
    entry["interface_cfg"] = "./interface/stlink.cfg"
    entry["target_cfg"] = "./target/stm32f4x.cfg"


def assert_no_secret(error: ConfigError, *secrets: str) -> None:
    """No secret in the refusal, whether its text or its JSON form; the JSON must also encode without `default=`."""
    text = f"{error}\n{json.dumps(error.to_dict(), ensure_ascii=False)}".lower()
    for secret in secrets:
        assert secret.lower() not in text


def spellings_of(value: Any) -> set[str]:
    """The texts a registry would hold if it took a malformed value at its word: its str form, each element's, and blank."""
    items = value if isinstance(value, list) else [value]
    return {str(item) for item in items} | {str(value), ""}


def assert_names_a_restart_and_no_file_repair(refusal: dict[str, Any], *secrets: str) -> None:
    """A refusal on a startup supply, reported on a reload: the way out is a restart, not a file edit."""
    steps = refusal["next_steps"]
    joined = " ".join(steps).lower()
    assert "restart" in joined and "startup" in joined and "power_supplies" in joined
    assert not any("has to be repaired" in step for step in steps)
    assert "remediation" not in refusal
    assert "do_not" not in refusal
    text = json.dumps(refusal, ensure_ascii=False)
    for dash in DASHES:
        assert dash not in text
    for secret in secrets:
        assert secret.lower() not in text.lower()


def assert_names_a_restart_for_a_port(refusal: dict[str, Any], *secrets: str) -> None:
    """A refusal on a port a startup supply uses: repairing the port in the file is a real remedy, and so is a restart."""
    joined = " ".join(refusal["next_steps"]).lower()
    assert "restart" in joined and "power_supplies" in joined
    assert "remediation" in refusal
    text = json.dumps(refusal, ensure_ascii=False)
    for dash in DASHES:
        assert dash not in text
    for secret in secrets:
        assert secret.lower() not in text.lower()


@dataclass(frozen=True)
class Refusal:
    """One refused document: the fields the refusal may name, and how to build it from fresh values."""

    name: str
    fields: tuple[str, ...]
    build: Callable[[Values], dict[str, Any]]
    secret_field: str | None = None
    # A limit refusal: the quantity, the record's key for the bound it broke, and the detail key that names it.
    limit: tuple[str, str, str] | None = None


def _port_without(v: Values, key: str) -> dict[str, Any]:
    return document(v, com_ports={"psu": port(v.serial, v.resource, drop=(key,))})


REFUSALS: tuple[Refusal, ...] = (
    Refusal("R1-version-1", ("power_supplies",), lambda v: document(v, config_version=1)),
    Refusal(
        "R4-unknown-type",
        ("power_supplies.bench.type",),
        lambda v: document(v, power_supplies={"bench": supply(type="ghost-psu")}),
    ),
    Refusal(
        "R5-unknown-debugger",
        ("power_supplies.bench.powers[0]",),
        lambda v: document(v, power_supplies={"bench": supply(powers=["ghost"])}),
    ),
    Refusal(
        "R6-channel-above-count",
        ("power_supplies.bench.channels.2",),
        lambda v: document(v, power_supplies={"bench": supply(channels={"2": {"voltage_limit_v": 1}})}),
    ),
    # The R7 values sit a step or half a step from the rs3005p record (maximum 30 V, resolution 0.01 V;
    # maximum 5 A, resolution 0.001 A): 30.01 is one step over, 29.995 half a step off, 5.001 one step over.
    Refusal(
        "R7-voltage-above-maximum",
        ("power_supplies.bench.voltage_limit_v",),
        lambda v: document(v, power_supplies={"bench": supply(voltage_limit_v=30.01)}),
        limit=("voltage", "max", "maximum"),
    ),
    Refusal(
        "R7-current-off-resolution",
        ("power_supplies.bench.current_limit_a",),
        lambda v: document(v, power_supplies={"bench": supply(current_limit_a=1.0005)}),
        limit=("current", "resolution", "resolution"),
    ),
    Refusal(
        "R7-channel-voltage-above-maximum",
        ("power_supplies.bench.channels.1.voltage_limit_v",),
        lambda v: document(v, power_supplies={"bench": supply(channels={"1": {"voltage_limit_v": 31}})}),
        limit=("voltage", "max", "maximum"),
    ),
    Refusal(
        "R7-current-above-maximum",
        ("power_supplies.bench.current_limit_a",),
        lambda v: document(v, power_supplies={"bench": supply(current_limit_a=5.001)}),
        limit=("current", "max", "maximum"),
    ),
    Refusal(
        "R7-voltage-off-resolution",
        ("power_supplies.bench.voltage_limit_v",),
        lambda v: document(v, power_supplies={"bench": supply(voltage_limit_v=29.995)}),
        limit=("voltage", "resolution", "resolution"),
    ),
    Refusal(
        "R7-channel-current-above-maximum",
        ("power_supplies.bench.channels.1.current_limit_a",),
        lambda v: document(v, power_supplies={"bench": supply(channels={"1": {"current_limit_a": 5.001}})}),
        limit=("current", "max", "maximum"),
    ),
    Refusal(
        "R7-channel-current-off-resolution",
        ("power_supplies.bench.channels.1.current_limit_a",),
        lambda v: document(v, power_supplies={"bench": supply(channels={"1": {"current_limit_a": 4.9995}})}),
        limit=("current", "resolution", "resolution"),
    ),
    Refusal(
        "R8-unknown-port",
        ("power_supplies.bench.com_port",),
        lambda v: document(v, power_supplies={"bench": supply(com_port="ghost")}),
    ),
    Refusal("R9-port-without-device", ("com_ports.psu.device",), lambda v: _port_without(v, "device")),
    Refusal("R9-port-without-resource-id", ("com_ports.psu.resource_id",), lambda v: _port_without(v, "resource_id")),
    Refusal("R9-port-without-vid", ("com_ports.psu.vid",), lambda v: _port_without(v, "vid")),
    Refusal("R9-port-without-pid", ("com_ports.psu.pid",), lambda v: _port_without(v, "pid")),
    Refusal(
        "R10-no-serial",
        ("power_supplies.bench.idn_serial",),
        lambda v: document(v, com_ports={"psu": port(v.serial, v.resource, drop=("serial_number",))}),
    ),
    Refusal(
        "R11-second-supply",
        ("power_supplies.bench-b.com_port",),
        lambda v: document(v, power_supplies={"bench-a": supply(), "bench-b": supply()}),
    ),
    Refusal(
        "R11-esptool-debugger",
        ("power_supplies.bench.com_port",),
        lambda v: document(v, debugger_type="esptool", com_port="psu"),
    ),
    Refusal(
        "R12-shared-resource-id",
        ("com_ports.psu.resource_id", "com_ports.aux.resource_id"),
        lambda v: document(v, com_ports={"psu": port(v.serial, v.resource), "aux": port(v.other_serial, v.resource)}),
    ),
    Refusal(
        "R12-case-only",
        ("com_ports.psu.resource_id", "com_ports.aux.resource_id"),
        lambda v: document(
            v, com_ports={"psu": port(v.serial, v.resource), "aux": port(v.other_serial, v.resource.lower())}
        ),
    ),
    Refusal(
        "R13-contains-idn-serial",
        ("com_ports.psu.resource_id",),
        lambda v: document(
            v,
            com_ports={"psu": port(v.serial, f"AHIL-{v.serial.lower()}", drop=("serial_number",))},
            power_supplies={"bench": supply(idn_serial=v.serial)},
        ),
        secret_field="power_supplies.bench.idn_serial",
    ),
    Refusal(
        "R13-contains-port-serial",
        ("com_ports.psu.resource_id",),
        lambda v: document(v, com_ports={"psu": port(v.serial, f"usb-{v.serial}")}),
        secret_field="com_ports.psu.serial_number",
    ),
)


# ---------------------------------------------------------------------------
# The types and the schema.


def test_the_power_supply_types_default_to_no_grant_and_a_file_without_the_section_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = Values.fresh()
    workspace, _ = write_bench(tmp_path, monkeypatch, **document(values))

    bench = load_authoritative_config(workspace).power_supplies["bench"]
    assert bench.permissions.allow_set is False
    assert bench.permissions.allow_output is False

    bare = (tmp_path / "bare").resolve()
    write_authoritative_config(bare, monkeypatch)
    assert load_authoritative_config(bare).power_supplies == {}


def test_every_type_record_has_the_seven_keys_and_rs3005p_is_pinned_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record is read only once a supply has loaded. On this tree the loader's first step for a document with
    # a supply stops the test, so the gap is reached through the product rather than through an empty table.
    workspace, _ = write_bench(tmp_path, monkeypatch, **document(Values.fresh()))
    load_authoritative_config(workspace)

    keys = {"channels", "ranges", "measures", "output_readable", "protection", "protection_readable", "usb_ids"}
    record = POWER_SUPPLY_TYPES["rs3005p"]
    assert set(record) == keys
    assert record["channels"] == 1
    assert record["ranges"] == {
        "1": {"voltage": {"max": "30", "resolution": "0.01"}, "current": {"max": "5", "resolution": "0.001"}}
    }
    assert record["usb_ids"] == [[0x0416, 0x5011]]
    assert record["measures"] == {"voltage": True, "current": True}
    assert record["output_readable"] is True
    assert record["protection"] == {"ovp": "unsupported", "ocp": "unsupported"}
    assert record["protection_readable"] == {"ovp": False, "ocp": False}
    for name, other in POWER_SUPPLY_TYPES.items():
        assert set(other) == keys, name


def test_the_schema_puts_the_supply_entry_under_additional_properties() -> None:
    schema = config_schema()
    assert "power_supplies" in schema["properties"], "the config schema declares no power_supplies section"
    section = schema["properties"]["power_supplies"]
    assert section["type"] == "object"
    assert "patternProperties" not in section
    entry = section["additionalProperties"]
    assert entry["additionalProperties"] is False
    assert set(entry["required"]) == {"type", "com_port", "powers"}
    assert "enum" not in entry["properties"]["type"]
    assert entry["properties"]["permissions"] == {"$ref": "#/$defs/power_supply_permissions"}
    assert "power_supply_permissions" in schema["$defs"]
    text = json.dumps(section, ensure_ascii=False)
    for dash in DASHES:
        assert dash not in text


@pytest.mark.parametrize("where", ["supply", "channel"])
@pytest.mark.parametrize("key", ["voltage_limit_v", "current_limit_a"])
def test_a_limit_description_says_an_absent_limit_refuses_every_write(where: str, key: str) -> None:
    """The code refuses every write while a limit is missing and never takes one from the type's range."""
    entry = config_schema()["properties"]["power_supplies"]["additionalProperties"]
    if where == "channel":
        entry = entry["properties"]["channels"]["additionalProperties"]
    description = entry["properties"][key]["description"]

    assert "only bound" not in description
    assert "every write" in description and "refused" in description
    assert "never stands in" in description
    assert "the type's own range is the only bound" not in config_shape_document()


# ---------------------------------------------------------------------------
# Loading: limits, secrets and the refusals.


def test_supply_limits_load_as_exact_decimals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = Values.fresh()
    kwargs = document(
        values,
        power_supplies={
            "bench": supply(
                voltage_limit_v=12.5,
                current_limit_a=0.123,
                channels={"1": {"voltage_limit_v": 3.3, "current_limit_a": 1.001}},
            )
        },
    )
    workspace, _ = write_bench(tmp_path, monkeypatch, **kwargs)

    bench = load_authoritative_config(workspace).power_supplies["bench"]
    assert bench.voltage_limit_v == Decimal("12.5")
    assert bench.current_limit_a == Decimal("0.123")
    assert bench.channels[1].voltage_limit_v == Decimal("3.3")
    assert bench.channels[1].current_limit_a == Decimal("1.001")


def test_supply_secrets_are_registered_before_the_schema_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Both sources in one document: the port's serial_number (values.serial) and the supply's idn_serial (values.other_serial).
    values = Values.fresh()
    kwargs = document(
        values,
        power_supplies={"bench": supply(idn_serial=values.other_serial, voltage_limit_v="high")},
    )
    workspace, _ = write_bench(tmp_path, monkeypatch, **kwargs)

    with pytest.raises(ConfigError) as caught:
        load_authoritative_config(workspace)
    assert caught.value.to_dict()["error_type"] == "config_invalid"
    assert_no_secret(caught.value, values.serial, values.other_serial)

    from agentic_hil.power_supply import registered_config_secrets

    registered = registered_config_secrets()
    assert values.other_serial in registered, "the idn_serial was not registered before the schema refused"
    assert values.serial in registered, "the port's serial_number was not registered before the schema refused"


def test_an_integer_serial_is_registered_as_a_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = Values.fresh()
    workspace, _ = write_bench(tmp_path, monkeypatch, **document(values, com_ports={"psu": port(values.number, values.resource)}))

    with pytest.raises(ConfigError) as caught:
        load_authoritative_config(workspace)
    assert caught.value.to_dict()["error_type"] == "config_invalid"
    assert_no_secret(caught.value, str(values.number))

    from agentic_hil.power_supply import registered_config_secrets

    assert str(values.number) in registered_config_secrets(), "the integer serial was not registered as its str form"


@pytest.mark.parametrize("value", [True, 1.5, ["a"], "   "], ids=["bool", "float", "list", "blank"])
def test_a_value_that_is_not_a_serial_is_not_registered(value: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = Values.fresh()
    kwargs = document(
        values,
        com_ports={"psu": port(values.serial, values.resource, drop=("serial_number",))},
        power_supplies={"bench": supply(idn_serial=value)},
    )
    workspace, _ = write_bench(tmp_path, monkeypatch, **kwargs)

    with pytest.raises(ConfigError) as caught:
        load_authoritative_config(workspace)
    payload = caught.value.to_dict()
    assert payload["error_type"] == "config_invalid"
    assert payload["field"] == "power_supplies.bench.idn_serial"

    from agentic_hil.power_supply import registered_config_secrets

    assert not (spellings_of(value) & registered_config_secrets()), "a malformed idn_serial was registered"


@pytest.mark.parametrize("case", REFUSALS, ids=[case.name for case in REFUSALS])
def test_a_supply_refusal_names_its_field_and_path(case: Refusal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = Values.fresh()
    workspace, _ = write_bench(tmp_path, monkeypatch, **case.build(values))

    with pytest.raises(ConfigError) as caught:
        load_authoritative_config(workspace)
    error = caught.value
    payload = error.to_dict()
    assert payload["error_type"] == "config_invalid"
    assert payload["field"] in case.fields
    assert payload["path"]
    if case.secret_field is not None:
        assert payload["secret_field"] == case.secret_field
    if case.limit is not None:
        # AC5: the offending limit is text, and the bound it broke is the record's own text for that bound.
        quantity, record_key, detail = case.limit
        expected = POWER_SUPPLY_TYPES["rs3005p"]["ranges"]["1"][quantity][record_key]
        assert isinstance(payload["value"], str), payload
        assert payload[detail] == expected, payload
    # Every value in a new refusal's details is JSON-native: no `default=` is needed to write it.
    assert json.dumps(payload)
    for dash in DASHES:
        assert dash not in str(error)


@pytest.mark.parametrize(
    "value",
    ["12", True, float("inf"), float("nan"), 0, -1],
    ids=["string", "bool", "infinity", "nan", "zero", "negative"],
)
def test_an_invalid_limit_refuses_the_whole_load(value: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same malformed value refuses the whole load at each limit: the supply's and the channel's, for both quantities."""
    places: dict[str, Callable[[Any], dict[str, Any]]] = {
        "power_supplies.bench.voltage_limit_v": lambda limit: supply(voltage_limit_v=limit),
        "power_supplies.bench.current_limit_a": lambda limit: supply(current_limit_a=limit),
        "power_supplies.bench.channels.1.voltage_limit_v": lambda limit: supply(channels={"1": {"voltage_limit_v": limit}}),
        "power_supplies.bench.channels.1.current_limit_a": lambda limit: supply(channels={"1": {"current_limit_a": limit}}),
    }
    values = Values.fresh()
    for index, (field, place) in enumerate(places.items()):
        workspace, _ = write_bench(
            tmp_path / f"case-{index}", monkeypatch, **document(values, power_supplies={"bench": place(value)})
        )

        with pytest.raises(ConfigError) as caught:
            load_authoritative_config(workspace)
        payload = caught.value.to_dict()
        assert payload["error_type"] == "config_invalid", field
        assert payload["field"] == field, field


def test_a_supply_without_limits_loads_with_none_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = Values.fresh()
    workspace, _ = write_bench(tmp_path, monkeypatch, **document(values))

    bench = load_authoritative_config(workspace).power_supplies["bench"]
    assert bench.voltage_limit_v is None
    assert bench.current_limit_a is None
    assert not bench.channels


def test_a_limit_exactly_at_its_maximum_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = Values.fresh()
    workspace, path = write_bench(tmp_path, monkeypatch, **document(values))
    # The premise: a supply with no limits loads. The record is read only after that.
    load_authoritative_config(workspace)

    ranges = POWER_SUPPLY_TYPES["rs3005p"]["ranges"]["1"]
    voltage = float(ranges["voltage"]["max"])
    current = float(ranges["current"]["max"])
    rewrite(
        path,
        lambda raw: raw["power_supplies"]["bench"].update(
            voltage_limit_v=voltage,
            current_limit_a=current,
            channels={"1": {"voltage_limit_v": voltage, "current_limit_a": current}},
        ),
    )

    bench = load_authoritative_config(workspace).power_supplies["bench"]
    assert bench.voltage_limit_v == Decimal(ranges["voltage"]["max"])
    assert bench.current_limit_a == Decimal(ranges["current"]["max"])
    assert bench.channels[1].voltage_limit_v == Decimal(ranges["voltage"]["max"])
    assert bench.channels[1].current_limit_a == Decimal(ranges["current"]["max"])


# ---------------------------------------------------------------------------
# Reload (D6): the section is adopted only by a restart.


def test_power_supplies_are_adopted_only_by_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = Values.fresh()
    workspace, path = write_bench(
        tmp_path, monkeypatch, **document(values, power_supplies={"bench": supply(voltage_limit_v=12.5)})
    )
    existing = load_authoritative_config(workspace)

    # A lowered limit on disk: the server keeps the limit it started with.
    rewrite(path, lambda raw: raw["power_supplies"]["bench"].update(voltage_limit_v=5.0))
    reloaded, answer = reload_description(existing)

    assert answer["ok"] is True
    assert reloaded is not None
    assert reloaded.power_supplies["bench"].voltage_limit_v == Decimal("12.5")
    assert any("power_supplies" in str(item) for item in answer["restart_required_for"])
    # config_status is a mapping. A pending restart makes it the changed state, which asks for a reload.
    status = answer["config_status"]
    assert status["state"] == "changed", status
    assert status["reload_required"] is True, status


def test_a_reload_refused_on_a_startup_supply_names_a_restart_and_no_file_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = Values.fresh()
    workspace, path = write_bench(tmp_path, monkeypatch, **document(values))
    existing = load_authoritative_config(workspace)

    # The debugger is renamed and the disk supply follows it, so the disk file
    # loads on its own. The reload checks the startup supply, which still names `dut`.
    rewrite(path, lambda raw: name_the_debugger(raw, "dut", "dut-renamed", ["dut-renamed"]))
    assert tuple(load_authoritative_config(workspace).power_supplies["bench"].powers) == ("dut-renamed",)

    reloaded, refusal = reload_description(existing)
    assert reloaded is None
    assert refusal["error_type"] == "config_invalid"
    assert refusal["field"] == "power_supplies.bench.powers[0]"
    assert refusal["path"]
    assert_names_a_restart_and_no_file_repair(refusal, *values.secrets())

    # Editing the supply's powers on disk does not change the answer: the reload never re-reads them.
    rewrite(path, lambda raw: raw["power_supplies"]["bench"].update(powers=[]))
    reloaded, refusal = reload_description(existing)
    assert reloaded is None
    assert refusal["field"] == "power_supplies.bench.powers[0]"

    # Restoring the debugger's name and the startup powers is what lets the reload through.
    rewrite(path, lambda raw: name_the_debugger(raw, "dut-renamed", "dut", ["dut"]))
    reloaded, answer = reload_description(existing)
    assert reloaded is not None
    assert answer["ok"] is True


def test_a_reload_refused_on_a_supply_port_names_the_port_repair_and_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = Values.fresh()
    workspace, path = write_bench(tmp_path, monkeypatch, **document(values))
    existing = load_authoritative_config(workspace)

    def drop_the_section_and_the_vid(raw: dict[str, Any]) -> None:
        del raw["power_supplies"]
        del raw["com_ports"]["psu"]["vid"]

    rewrite(path, drop_the_section_and_the_vid)
    # The premise: the disk file loads on its own, with no supply in it to check.
    load_authoritative_config(workspace)

    reloaded, refusal = reload_description(existing)
    assert reloaded is None
    assert refusal["error_type"] == "config_invalid"
    assert refusal["field"] == "com_ports.psu.vid"
    assert_names_a_restart_for_a_port(refusal, *values.secrets())

    rewrite(path, lambda raw: raw["com_ports"]["psu"].update(vid=VID))
    reloaded, answer = reload_description(existing)
    assert reloaded is not None
    assert answer["ok"] is True


def test_a_reload_refused_on_unvalidated_scripts_keeps_its_wording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned legacy path, replicated from test_config_reload.py rather than imported. It names no supply."""
    workspace = (tmp_path / "workspace").resolve()
    path = write_authoritative_config(workspace, monkeypatch, debugger_type="openocd")
    monkeypatch.chdir(workspace)
    existing = load_authoritative_config(workspace)

    rewrite(path, close_the_entry_and_break_its_script_paths)
    assert load_authoritative_config(workspace).debuggers["dut"].interface_cfg == "./interface/stlink.cfg"

    reloaded, refusal = reload_description(existing)
    assert reloaded is None
    assert refusal["error_type"] == "config_invalid"
    assert refusal["field"] == "debuggers.dut.interface_cfg"
    assert "has to be repaired" in refusal["next_steps"][0]
    assert "wider than the ones the file states" in refusal["summary"]
    assert "remediation" in refusal


# ---------------------------------------------------------------------------
# The key model, the grants and the regeneration.


def test_only_the_two_supply_grants_are_settable_keys() -> None:
    allow_set = resolve_config_key("power_supplies.bench.permissions.allow_set")
    assert allow_set is not None and allow_set.right == CONFIG_PERMISSIONS_RIGHT, (
        "power_supplies.bench.permissions.allow_set does not resolve to the permissions right"
    )
    allow_output = resolve_config_key("power_supplies.bench.permissions.allow_output")
    assert allow_output is not None and allow_output.right == CONFIG_PERMISSIONS_RIGHT, (
        "power_supplies.bench.permissions.allow_output does not resolve to the permissions right"
    )
    for key in (
        "power_supplies.bench.voltage_limit_v",
        "power_supplies.bench.current_limit_a",
        "power_supplies.bench.channels.1.voltage_limit_v",
        "power_supplies.bench.idn_serial",
        "power_supplies.bench.com_port",
        "power_supplies.bench.powers",
        "power_supplies.bench.timeout_s",
    ):
        assert resolve_config_key(key) is None, f"{key} resolves as a settable key"


def test_grant_and_revoke_reach_the_two_supply_grants_and_nothing_else() -> None:
    resolved, _reason = resolve_permission_key("power_supplies.bench.permissions.allow_set")
    assert resolved is not None and resolved.right == CONFIG_PERMISSIONS_RIGHT, (
        "agentic-hil grant does not reach power_supplies.bench.permissions.allow_set"
    )
    short, _reason = resolve_permission_key("power_supplies.bench.allow_output")
    assert short is not None and short.right == CONFIG_PERMISSIONS_RIGHT, (
        "agentic-hil grant does not reach power_supplies.bench.allow_output"
    )
    for key in ("power_supplies.bench.voltage_limit_v", "power_supplies.bench.idn_serial"):
        resolved, reason = resolve_permission_key(key)
        assert resolved is None and reason is not None and key in reason, key


def test_a_regeneration_names_a_dropped_supply_in_dropped_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = Values.fresh()
    workspace, _ = write_bench(tmp_path, monkeypatch, **document(values))
    existing = load_authoritative_config(workspace)

    # A regenerated document that carries no supply: the supply is named, not dropped in silence.
    dropped = carry_over_permissions({"debuggers": {}, "com_ports": {}, "can_buses": {}}, existing)
    assert "power_supplies.bench" in dropped


# ---------------------------------------------------------------------------
# Documentation and the fixture default.


def test_the_docs_and_the_config_shape_describe_power_supplies() -> None:
    assert "### `power_supplies`" in config_shape_document(), "the config-shape resource has no power_supplies section"
    docs = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    assert "## Bench power supplies" in docs
    section = docs.split("## Bench power supplies", 1)[1].split("\n## ", 1)[0]
    lowered = section.lower()
    assert "owner" in lowered and "restart" in lowered
    assert "version 2" in lowered
    for dash in DASHES:
        assert dash not in section


def test_write_config_writes_no_power_supplies_unless_asked(tmp_path: Path) -> None:
    plain = write_config(tmp_path / "plain").read_text(encoding="utf-8")
    assert "power_supplies" not in plain
    with_section = write_config(tmp_path / "with", power_supplies_yaml="power_supplies: {}\n").read_text(encoding="utf-8")
    assert "power_supplies: {}" in with_section
