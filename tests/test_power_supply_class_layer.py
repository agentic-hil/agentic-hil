"""The power supply class layer, tested against the backend interface (issue #733).

Every rule here is enforced above the backends, so it is checked against fake
backends declared the way a real supply type is declared: a static record in the
`POWER_SUPPLY_TYPES` shape and a factory in `POWER_SUPPLY_BACKENDS`. Two fakes
cover the shapes a backend can have:

- a single-channel supply that measures, reads its output state back, and arms
  over-voltage and over-current protection at a threshold it can read back;
- a two-channel supply that measures nothing and has no protection.

The limits are chosen so the effective limit of the channel differs from the
supply limit alone, the channel limit alone, the capability range and the
present set point. Every identifier is synthetic.

Product imports sit inside each test, so each node fails on its own.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from power_supply_fakes import (
    CURRENT_RESOLUTION,
    EFFECTIVE_CURRENT,
    EFFECTIVE_VOLTAGE,
    PRESENT_CURRENT,
    PRESENT_VOLTAGE,
    SETTERS,
    THRESHOLD_TYPE,
    TWO_CHANNEL_TYPE,
    VOLTAGE_RESOLUTION,
    Bench,
    call,
    fresh_serial,
    host_port,
    make_bench,
    record,
    record_sleep,
)

SUPPLY = "bench"
PREFIX = f"power_supplies.{SUPPLY}"
ALL_LIMIT_KEYS = (
    f"{PREFIX}.channels.1.current_limit_a",
    f"{PREFIX}.channels.1.voltage_limit_v",
    f"{PREFIX}.current_limit_a",
    f"{PREFIX}.voltage_limit_v",
)
CRITERION_6 = {
    "com_port_identity_unverified",
    "com_port_identity_mismatch",
    "power_supply_identity_unknown",
    "power_supply_no_reply",
    "power_supply_readback_mismatch",
}


def status(bench: Bench, **arguments: Any) -> dict[str, Any]:
    return call(bench, "power_supply_status", {"supply_id": bench.supply_id, **arguments})


def set_points(bench: Bench, **arguments: Any) -> dict[str, Any]:
    return call(bench, "power_supply_set", {"supply_id": bench.supply_id, **arguments})


def output(bench: Bench, on: bool, **arguments: Any) -> dict[str, Any]:
    return call(bench, "power_supply_output", {"supply_id": bench.supply_id, "on": on, **arguments})


def cycle(bench: Bench, off_s: float = 0.5, **arguments: Any) -> dict[str, Any]:
    return call(bench, "power_supply_cycle", {"supply_id": bench.supply_id, "off_s": off_s, **arguments})


def contacted(bench: Bench) -> list[tuple[Any, ...]]:
    """Every call on the backend apart from the static capabilities, which send nothing."""
    return [entry for entry in bench.unit.calls if entry[0] != "capabilities"]


def setter_calls(bench: Bench) -> list[tuple[Any, ...]]:
    return [entry for entry in bench.unit.calls if entry[0] in SETTERS]


def index_of(bench: Bench, name: str) -> int:
    return next(position for position, entry in enumerate(bench.unit.calls) if entry[0] == name)


def assert_last_known_output(result: dict[str, Any]) -> None:
    assert "last_known_output" in result, result
    known = result["last_known_output"]
    assert isinstance(known, dict), result
    assert set(known) >= {"output_on", "read_at"}, known


def assert_refused_before_contact(bench: Bench, result: dict[str, Any], error_type: str) -> None:
    assert result["ok"] is False, result
    assert result["error_type"] == error_type, result
    assert contacted(bench) == [], bench.unit.calls
    assert result.get("side_effect_status", "not_started") == "not_started", result


def assert_not_quarantined(result: dict[str, Any]) -> None:
    assert "power_supply_state_unconfirmed" not in (result.get("cleanup_reasons") or []), result


def assert_quarantined(result: dict[str, Any]) -> None:
    from agentic_hil.knowledge import QUARANTINE_REASON_GUIDES

    assert "power_supply_state_unconfirmed" in (result.get("cleanup_reasons") or []), result
    assert "power_supply_state_unconfirmed" in QUARANTINE_REASON_GUIDES
    guidance = result.get("quarantine_guidance") or []
    assert any(entry.get("reason") == "power_supply_state_unconfirmed" for entry in guidance), result


# ---------------------------------------------------------------------------
# Status.


def test_status_reads_every_field_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)

    result = status(bench)

    assert result["ok"] is True, result
    assert bench.unit.names() == ["identity", "channel_status"], bench.unit.calls
    assert bench.unit.writes() == []
    assert result["supply_id"] == SUPPLY
    assert result["model"] == "FAKE-PSU" and result["firmware"] == "F1", result
    [channel] = result["channels"]
    assert channel["channel"] == 1
    assert Decimal(channel["set_voltage_v"]) == PRESENT_VOLTAGE
    assert Decimal(channel["set_current_a"]) == PRESENT_CURRENT
    assert Decimal(channel["measured_voltage_v"]) == Decimal("0")
    assert Decimal(channel["measured_current_a"]) == Decimal("0")
    assert channel["output_on"] is False
    assert channel["read_at"], channel
    assert {"regulation", "protection_tripped"} <= set(channel), channel
    assert result["last_known_output"]["output_on"] is False, result


def test_status_reports_the_effective_limit_as_the_lower_of_supply_and_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)

    result = status(bench)

    assert result["ok"] is True, result
    limits = result["limits"]["1"]
    # Voltage comes from the channel (9.5 under 12), current from the supply
    # (0.8 under 1.2): neither source alone gives both.
    assert Decimal(limits["voltage_v"]) == EFFECTIVE_VOLTAGE, limits
    assert Decimal(limits["current_a"]) == EFFECTIVE_CURRENT, limits
    assert result.get("writes_refused_reason") is None, result


def test_status_works_without_limits_and_says_why_writes_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, limits=False)

    result = status(bench)

    assert result["ok"] is True, result
    assert result["limits"]["1"] == {"voltage_v": None, "current_a": None}, result
    assert result["missing_limits"] == list(ALL_LIMIT_KEYS), result
    assert result["writes_refused_reason"] == (
        f"Writes on power supply '{SUPPLY}' channel 1 were refused before contact: the authoritative configuration sets no "
        + ", ".join(ALL_LIMIT_KEYS)
        + "."
    ), result
    assert bench.unit.writes() == []


def test_status_needs_no_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, supply_changes={"permissions": {"allow_set": False, "allow_output": False}})

    result = status(bench)

    assert result["ok"] is True, result
    assert bench.unit.writes() == []


# ---------------------------------------------------------------------------
# Limits (D8).


@pytest.mark.parametrize(
    ("argument", "value", "limit"),
    [("voltage_v", 9.51, EFFECTIVE_VOLTAGE), ("current_a", 0.801, EFFECTIVE_CURRENT)],
)
def test_a_request_above_the_effective_limit_is_refused_with_nothing_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argument: str, value: float, limit: Decimal
) -> None:
    bench = make_bench(tmp_path, monkeypatch)

    result = set_points(bench, **{argument: value})

    assert_refused_before_contact(bench, result, "power_supply_limit_exceeded")
    assert Decimal(result["requested"]) == Decimal(str(value)), result
    assert Decimal(result["limit"]) == limit, result
    assert result["field"] == argument, result
    assert str(Decimal(str(value))) in result["summary"] and str(limit) in result["summary"], result
    assert_last_known_output(result)


def test_a_request_within_the_limit_is_written_and_read_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)

    result = set_points(bench, voltage_v=9.5, current_a=0.8)

    assert result["ok"] is True, result
    assert Decimal(result["requested"]["voltage_v"]) == EFFECTIVE_VOLTAGE, result
    assert Decimal(result["read_back"]["voltage_v"]) == EFFECTIVE_VOLTAGE, result
    assert Decimal(result["requested"]["current_a"]) == EFFECTIVE_CURRENT, result
    assert Decimal(result["read_back"]["current_a"]) == EFFECTIVE_CURRENT, result
    assert ("set_voltage", 1, EFFECTIVE_VOLTAGE) in bench.unit.calls
    assert ("set_current", 1, EFFECTIVE_CURRENT) in bench.unit.calls
    assert bench.unit.channels[1].set_voltage == EFFECTIVE_VOLTAGE
    assert "measured" in result and result["limits"]["1"]["voltage_v"] is not None, result
    # Every setter is followed by a read.
    names = [entry[0] for entry in bench.unit.calls]
    for setter in ("set_voltage", "set_current"):
        assert names[names.index(setter) + 1] == "channel_status", names


def test_set_point_values_are_exact_decimals_never_floats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)

    result = set_points(bench, voltage_v=0.1)

    assert result["ok"] is True, result
    [written] = [entry for entry in bench.unit.calls if entry[0] == "set_voltage"]
    assert isinstance(written[2], Decimal) and written[2] == Decimal("0.1"), written


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"voltage_v": 5.005}, id="voltage_off_resolution"),
        pytest.param({"current_a": 0.2505}, id="current_off_resolution"),
        pytest.param({"voltage_v": -1.0}, id="negative_voltage"),
        pytest.param({"current_a": -0.1}, id="negative_current"),
        pytest.param({"voltage_v": True}, id="bool_voltage"),
        pytest.param({}, id="neither_value"),
    ],
)
def test_an_invalid_set_point_is_refused_without_rounding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]) -> None:
    bench = make_bench(tmp_path, monkeypatch)

    result = set_points(bench, **arguments)

    assert_refused_before_contact(bench, result, "invalid_argument")
    assert bench.unit.connections == 0


def test_a_stored_set_point_above_the_limit_blocks_output_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)
    # The supply was left at 24 V by hand, as the recording in #730 shows a unit can be.
    bench.unit.channels[1].set_voltage = Decimal("24.00")

    result = output(bench, True)

    assert result["ok"] is False, result
    assert result["error_type"] == "power_supply_limit_exceeded", result
    assert result["field"] == "voltage_v", result
    assert Decimal(result["requested"]) == Decimal("24.00") and Decimal(result["limit"]) == EFFECTIVE_VOLTAGE, result
    assert bench.unit.writes() == [], bench.unit.calls
    assert bench.unit.channels[1].output_on is False
    assert_last_known_output(result)


def test_a_stored_set_point_above_the_limit_blocks_a_cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)
    sleeps = record_sleep(monkeypatch, bench.unit)
    bench.unit.channels[1].set_current = Decimal("1.000")

    result = cycle(bench)

    assert result["ok"] is False, result
    assert result["error_type"] == "power_supply_limit_exceeded", result
    assert result["field"] == "current_a", result
    assert bench.unit.writes() == [] and sleeps == [], bench.unit.calls


@pytest.mark.parametrize(
    ("supply_type", "arguments", "field", "stored", "limit"),
    [
        pytest.param(THRESHOLD_TYPE, {"voltage_v": 5}, "current_a", Decimal("2.000"), EFFECTIVE_CURRENT, id="current_left_above"),
        pytest.param(THRESHOLD_TYPE, {"current_a": 0.5}, "voltage_v", Decimal("24.00"), EFFECTIVE_VOLTAGE, id="voltage_left_above"),
        pytest.param("rs3005p", {"voltage_v": 5}, "current_a", Decimal("2.000"), EFFECTIVE_CURRENT, id="rs3005p_record"),
    ],
)
def test_a_set_that_leaves_a_stored_set_point_above_the_limit_is_refused_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supply_type: str, arguments: dict[str, Any], field: str, stored: Decimal, limit: Decimal
) -> None:
    from agentic_hil.power_supply import POWER_SUPPLY_TYPES

    bench = make_bench(tmp_path, monkeypatch, supply_type=supply_type, type_record=POWER_SUPPLY_TYPES.get(supply_type))
    # The supply was left at 24 V and 2 A by hand, as the recording in #730 shows a unit can be.
    bench.unit.channels[1].set_voltage = Decimal("24.00")
    bench.unit.channels[1].set_current = Decimal("2.000")

    result = set_points(bench, **arguments)

    assert result["ok"] is False, result
    assert result["error_type"] == "power_supply_limit_exceeded", result
    assert result["field"] == field, result
    assert Decimal(result["requested"]) == stored and Decimal(result["limit"]) == limit, result
    assert str(stored) in result["summary"] and str(limit) in result["summary"], result
    assert f"`{field}`" in result["summary"], result
    assert result["side_effect_status"] == "not_started", result
    assert bench.unit.writes() == [], bench.unit.calls
    assert bench.unit.channels[1].set_voltage == Decimal("24.00") and bench.unit.channels[1].set_current == Decimal("2.000")
    assert_not_quarantined(result)
    assert_last_known_output(result)


@pytest.mark.parametrize(
    ("stored", "arguments", "order"),
    [
        pytest.param(
            (Decimal("9.00"), Decimal("0.250")), {"voltage_v": 5, "current_a": 0.8}, ["set_voltage", "set_current"], id="voltage_falls_current_rises"
        ),
        pytest.param(
            (Decimal("5.00"), Decimal("0.700")), {"voltage_v": 9.5, "current_a": 0.3}, ["set_current", "set_voltage"], id="voltage_rises_current_falls"
        ),
    ],
)
def test_a_set_of_both_quantities_writes_every_decrease_before_any_increase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: tuple[Decimal, Decimal], arguments: dict[str, Any], order: list[str]
) -> None:
    # Written the other way round, the supply would hold the higher voltage and
    # the higher current together for a moment, which neither value alone allows.
    bench = make_bench(tmp_path, monkeypatch)
    bench.unit.channels[1].set_voltage, bench.unit.channels[1].set_current = stored

    result = set_points(bench, **arguments)

    assert result["ok"] is True, result
    assert [entry[0] for entry in setter_calls(bench)] == order, bench.unit.calls
    names = [entry[0] for entry in bench.unit.calls]
    for setter in order:
        assert names[names.index(setter) + 1] == "channel_status", names
    assert bench.unit.channels[1].set_voltage == Decimal(str(arguments["voltage_v"]))
    assert bench.unit.channels[1].set_current == Decimal(str(arguments["current_a"]))


# ---------------------------------------------------------------------------
# Missing limits (D8).


def d8_summary(keys: tuple[str, ...] | list[str]) -> str:
    return (
        f"Writes on power supply '{SUPPLY}' channel 1 were refused before contact: the authoritative configuration sets no "
        + ", ".join(keys)
        + "."
    )


def d8_next_step(keys: tuple[str, ...] | list[str]) -> str:
    return (
        "This refusal is the answer to the request. Report it and stop. Writes on this channel stay refused until the "
        f"authoritative configuration sets {', '.join(keys)}. Only the owner writes these limits, by hand in the "
        "authoritative configuration file, and then restarts the Agentic HIL server, because the power_supplies "
        "section is read only at startup. No tool on this server writes them. You must not propose, estimate or derive "
        "a value for any of them, whether from the supply's capability range, its present set point or a datasheet, "
        "and you must not drive the supply another way."
    )


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        pytest.param("power_supply_set", {"voltage_v": 5.0}, id="set"),
        pytest.param("power_supply_output", {"on": True}, id="output_on"),
        pytest.param("power_supply_output", {"on": False}, id="output_off"),
        pytest.param("power_supply_cycle", {"off_s": 0.5}, id="cycle"),
    ],
)
def test_missing_limits_refuse_every_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any]) -> None:
    from agentic_hil.knowledge import CONFIG_GRANT_COMMAND, permission_denied_next_step, remediation_fields

    # The capability range is declared all the same: it is a hardware bound, never a default.
    bench = make_bench(tmp_path, monkeypatch, limits=False)

    result = call(bench, tool, {"supply_id": SUPPLY, **arguments})

    assert_refused_before_contact(bench, result, "permission_denied")
    assert "permission" not in result, result
    assert result["supply_id"] == SUPPLY and result["channel"] == 1, result
    assert result["missing_limits"] == list(ALL_LIMIT_KEYS), result
    assert result["summary"] == d8_summary(ALL_LIMIT_KEYS), result
    assert result["next_step"] == d8_next_step(ALL_LIMIT_KEYS), result
    assert CONFIG_GRANT_COMMAND not in result["next_step"]
    assert permission_denied_next_step() not in result["next_step"]
    assert result["side_effect_status"] == "not_started" and result["hardware_state"] == "unchanged", result
    scoped = remediation_fields("permission_denied", "power_supply_limits")
    assert scoped, "the scoped entry has to carry advice"
    assert result["remediation"] == scoped["remediation"], result
    assert_last_known_output(result)


@pytest.mark.parametrize(
    "dropped",
    ["voltage_limit_v", "current_limit_a", "channels.1.voltage_limit_v", "channels.1.current_limit_a"],
)
def test_one_missing_limit_is_named_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dropped: str) -> None:
    bench = make_bench(tmp_path, monkeypatch, drop_limits=(dropped,))

    result = set_points(bench, voltage_v=5.0)

    assert_refused_before_contact(bench, result, "permission_denied")
    assert result["missing_limits"] == [f"{PREFIX}.{dropped}"], result
    assert result["summary"] == d8_summary([f"{PREFIX}.{dropped}"]), result


def test_missing_limits_on_one_channel_leave_the_other_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, supply_type=TWO_CHANNEL_TYPE, drop_limits=("channels.2.voltage_limit_v",))

    refused = set_points(bench, channel=2, voltage_v=5.0)
    allowed = set_points(bench, channel=1, voltage_v=5.0)

    assert refused["error_type"] == "permission_denied", refused
    assert refused["missing_limits"] == [f"{PREFIX}.channels.2.voltage_limit_v"], refused
    assert allowed["ok"] is True, allowed


# ---------------------------------------------------------------------------
# Permissions.


@pytest.mark.parametrize(
    ("tool", "arguments", "key"),
    [
        pytest.param("power_supply_set", {"voltage_v": 5.0}, "allow_set", id="set"),
        pytest.param("power_supply_output", {"on": True}, "allow_output", id="output"),
        pytest.param("power_supply_cycle", {"off_s": 0.5}, "allow_output", id="cycle"),
    ],
)
def test_a_closed_grant_is_refused_naming_its_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any], key: str) -> None:
    bench = make_bench(tmp_path, monkeypatch, supply_changes={"permissions": {"allow_set": False, "allow_output": False}})

    result = call(bench, tool, {"supply_id": SUPPLY, **arguments})

    assert_refused_before_contact(bench, result, "permission_denied")
    assert result["permission"] == f"{PREFIX}.permissions.{key}", result
    assert f"{PREFIX}.permissions.{key}" in result["summary"], result
    assert result["remediation"], result


def test_the_grant_is_checked_before_the_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, limits=False, supply_changes={"permissions": {"allow_set": False, "allow_output": True}})

    result = set_points(bench, voltage_v=5.0)

    assert result["permission"] == f"{PREFIX}.permissions.allow_set", result
    assert "missing_limits" not in result, result


# ---------------------------------------------------------------------------
# Read-back.


def test_a_read_back_mismatch_is_a_failure_without_quarantine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, unit_changes={"skew": {"set_voltage": Decimal(VOLTAGE_RESOLUTION)}})

    result = set_points(bench, voltage_v=5.0)

    assert result["ok"] is False, result
    assert result["error_type"] == "power_supply_readback_mismatch", result
    assert Decimal(result["requested"]["voltage_v"]) == Decimal("5.0"), result
    assert Decimal(result["read_back"]["voltage_v"]) == Decimal("5.01"), result
    assert_not_quarantined(result)
    assert_last_known_output(result)


def test_an_output_that_does_not_follow_is_a_read_back_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, unit_changes={"output_stuck": True})

    result = output(bench, True)

    assert result["ok"] is False, result
    assert result["error_type"] == "power_supply_readback_mismatch", result
    assert result["requested"] == {"output_on": True} and result["read_back"] == {"output_on": False}, result
    assert_not_quarantined(result)


@pytest.mark.parametrize(
    ("silent", "tool", "arguments"),
    [
        pytest.param("set_voltage", "power_supply_set", {"voltage_v": 5.0}, id="setter"),
        pytest.param("set_output", "power_supply_output", {"on": True}, id="output"),
    ],
)
def test_no_reply_after_a_write_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, silent: str, tool: str, arguments: dict[str, Any]) -> None:
    bench = make_bench(tmp_path, monkeypatch, unit_changes={"no_reply": {silent}})

    result = call(bench, tool, {"supply_id": SUPPLY, **arguments})

    assert result["ok"] is False, result
    assert result["error_type"] == "power_supply_no_reply", result
    assert result["retry_safe"] is False, result
    assert_quarantined(result)
    assert_last_known_output(result)


def test_no_reply_to_the_read_back_after_a_write_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil import power_supply

    bench = make_bench(tmp_path, monkeypatch)
    plain_factory = bench.unit.factory

    def factory(config: Any, supply_id: str) -> Any:
        backend = plain_factory(config, supply_id)
        plain_read = backend.channel_status

        def channel_status(channel: int) -> Any:
            # The read after the first set point: the query itself wrote
            # nothing, but the write before it is unconfirmed.
            if setter_calls(bench):
                bench.unit.no_reply.add("channel_status")
            return plain_read(channel)

        backend.channel_status = channel_status
        return backend

    monkeypatch.setitem(power_supply.POWER_SUPPLY_BACKENDS, THRESHOLD_TYPE, factory)

    result = set_points(bench, voltage_v=5.0)

    assert result["error_type"] == "power_supply_no_reply", result
    assert_quarantined(result)


def test_no_reply_to_identity_is_not_quarantined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, unit_changes={"no_reply": {"identity"}})

    result = set_points(bench, voltage_v=5.0)

    assert result["ok"] is False, result
    assert result["error_type"] == "power_supply_no_reply", result
    assert bench.unit.names() == ["identity"], bench.unit.calls
    assert_not_quarantined(result)
    assert_last_known_output(result)


# ---------------------------------------------------------------------------
# Identity (C4).


def test_a_usb_identity_mismatch_is_refused_before_the_port_is_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = fresh_serial()
    # The device name now leads to another unit of the same type.
    bench = make_bench(tmp_path, monkeypatch, inventory=[])
    from power_supply_fakes import fake_inventory

    fake_inventory(monkeypatch, host_port(bench.device, other))

    result = status(bench)

    assert result["ok"] is False, result
    assert result["error_type"] == "com_port_identity_mismatch", result
    assert bench.unit.connections == 0 and bench.unit.calls == []
    assert_last_known_output(result)


def test_an_unverified_usb_identity_is_refused_before_the_port_is_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The device is not attached at all: the missing-device case of criterion 6.
    bench = make_bench(tmp_path, monkeypatch, inventory=[])

    result = set_points(bench, voltage_v=5.0)

    assert result["ok"] is False, result
    assert result["error_type"] == "com_port_identity_unverified", result
    assert bench.unit.connections == 0 and bench.unit.calls == []
    assert_last_known_output(result)


def test_a_wrong_usb_type_is_refused_before_the_port_is_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, inventory=[])
    from power_supply_fakes import fake_inventory

    fake_inventory(monkeypatch, host_port(bench.device, bench.port_serial, vid=0x1209, pid=0x0002))

    result = status(bench)

    assert result["ok"] is False, result
    assert result["error_type"] == "com_port_identity_mismatch", result
    assert bench.unit.connections == 0


def test_an_unknown_identity_leaves_identity_the_only_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, unit_changes={"accepted": False, "model": "OTHER-PSU", "firmware": "V0"})

    result = set_points(bench, voltage_v=5.0)

    assert result["ok"] is False, result
    assert result["error_type"] == "power_supply_identity_unknown", result
    assert contacted(bench)[0] == ("identity",) and bench.unit.names() == ["identity"], bench.unit.calls
    assert_last_known_output(result)


@pytest.mark.parametrize("port_serial", [False, True], ids=["idn_serial_is_the_unit_check", "idn_serial_is_a_second_check"])
def test_an_idn_serial_mismatch_leaves_identity_the_only_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port_serial: bool) -> None:
    bench = make_bench(tmp_path, monkeypatch, port_serial=port_serial, idn_serial_in_config=True)
    bench.unit.idn_serial = fresh_serial()

    result = set_points(bench, voltage_v=5.0)

    assert result["ok"] is False, result
    assert result["error_type"] == "power_supply_identity_mismatch", result
    assert bench.unit.names() == ["identity"], bench.unit.calls
    assert_last_known_output(result)


def test_a_matching_idn_serial_without_a_port_serial_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, port_serial=False)

    result = status(bench)

    assert result["ok"] is True, result


def test_the_idn_serial_is_registered_as_a_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.power_supply import registered_config_secrets

    # A port serial is the unit check, so the identity reply's serial is not in the file.
    bench = make_bench(tmp_path, monkeypatch)
    reported = fresh_serial()
    bench.unit.idn_serial = reported

    status(bench)

    assert reported in registered_config_secrets()


# ---------------------------------------------------------------------------
# Protection (rule 3, section 12.12).


@pytest.mark.parametrize(
    ("tool", "arguments", "first_write"),
    [
        pytest.param("power_supply_set", {"voltage_v": 5.0}, "set_voltage", id="set"),
        pytest.param("power_supply_output", {"on": True}, "set_output", id="output_on"),
        pytest.param("power_supply_cycle", {"off_s": 0.5}, "set_output", id="cycle"),
    ],
)
def test_protection_is_armed_at_the_effective_limits_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any], first_write: str
) -> None:
    bench = make_bench(tmp_path, monkeypatch)
    record_sleep(monkeypatch, bench.unit)

    result = call(bench, tool, {"supply_id": SUPPLY, **arguments})

    assert result["ok"] is True, result
    arms = [entry for entry in bench.unit.calls if entry[0] == "arm_protection"]
    assert arms == [("arm_protection", 1, EFFECTIVE_VOLTAGE, EFFECTIVE_CURRENT)], bench.unit.calls
    assert index_of(bench, "identity") < index_of(bench, "arm_protection") < index_of(bench, first_write), bench.unit.calls
    protection = result["protection"]["1"]
    for name, value in (("ovp", EFFECTIVE_VOLTAGE), ("ocp", EFFECTIVE_CURRENT)):
        assert protection[name]["support"] == "threshold", protection
        assert protection[name]["armed"] == "confirmed", protection
        assert Decimal(protection[name]["threshold"]) == value, protection
        assert Decimal(protection[name]["threshold_read"]) == value, protection


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [pytest.param("power_supply_output", {"on": False}, id="output_off"), pytest.param("power_supply_status", {}, id="status")],
)
def test_output_off_and_status_never_arm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any]) -> None:
    bench = make_bench(tmp_path, monkeypatch)

    result = call(bench, tool, {"supply_id": SUPPLY, **arguments})

    assert result["ok"] is True, result
    assert "arm_protection" not in [entry[0] for entry in bench.unit.calls], bench.unit.calls
    protection = result["protection"]["1"]
    assert protection["ovp"]["armed"] is None and protection["ocp"]["armed"] is None, protection


def test_an_unreadable_threshold_is_armed_and_reported_unconfirmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, type_record=record(ovp_readable=False, ocp_readable=False))

    result = set_points(bench, voltage_v=5.0)

    assert result["ok"] is True, result
    assert ("arm_protection", 1, EFFECTIVE_VOLTAGE, EFFECTIVE_CURRENT) in bench.unit.calls
    protection = result["protection"]["1"]
    assert protection["ovp"]["armed"] == "unconfirmed" and protection["ocp"]["armed"] == "unconfirmed", protection
    assert protection["ovp"]["threshold_read"] is None, protection


@pytest.mark.parametrize(
    ("arm", "cause", "side_effect"),
    [
        pytest.param("fault", "backend_fault", "not_started", id="backend_fault"),
        pytest.param("mismatch", "readback_mismatch", "partial", id="readback_mismatch"),
        pytest.param("missing", "readback_missing", "partial", id="readback_missing"),
    ],
)
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        pytest.param("power_supply_set", {"voltage_v": 5.0}, id="set"),
        pytest.param("power_supply_output", {"on": True}, id="output_on"),
        pytest.param("power_supply_cycle", {"off_s": 0.5}, id="cycle"),
    ],
)
def test_a_failed_arm_refuses_with_its_cause_and_writes_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str, cause: str, side_effect: str, tool: str, arguments: dict[str, Any]
) -> None:
    bench = make_bench(tmp_path, monkeypatch, unit_changes={"arm": arm})
    record_sleep(monkeypatch, bench.unit)

    result = call(bench, tool, {"supply_id": SUPPLY, **arguments})

    assert result["ok"] is False, result
    assert result["error_type"] == "power_supply_protection_not_armed", result
    assert result["cause"] == cause, result
    assert result["protection"] in {"ovp", "ocp"}, result
    assert result["retry_safe"] is False, result
    assert result["side_effect_status"] == side_effect, result
    assert result["hardware_state"] == ("unchanged" if side_effect == "not_started" else "unknown"), result
    assert "requested" in result and "read_back" in result, result
    assert setter_calls(bench) == [], bench.unit.calls
    assert bench.unit.sleeps == []
    assert_last_known_output(result)
    assert result["last_known_output"]["read_at"] is not None, result
    assert_not_quarantined(result)


def test_an_arm_without_a_reply_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, unit_changes={"arm": "no_reply"})

    result = set_points(bench, voltage_v=5.0)

    assert result["ok"] is False, result
    assert result["error_type"] == "power_supply_no_reply", result
    assert setter_calls(bench) == [], bench.unit.calls
    assert_quarantined(result)


@pytest.mark.parametrize(
    ("supply_type", "type_record", "support"),
    [
        pytest.param(TWO_CHANNEL_TYPE, None, "unsupported", id="no_protection"),
        pytest.param(THRESHOLD_TYPE, record(ovp="at_setpoint", ocp="at_setpoint", ovp_readable=False, ocp_readable=False), "at_setpoint", id="at_setpoint"),
    ],
)
def test_a_supply_without_threshold_protection_is_never_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supply_type: str, type_record: dict[str, Any] | None, support: str
) -> None:
    bench = make_bench(tmp_path, monkeypatch, supply_type=supply_type, type_record=type_record)

    written = set_points(bench, channel=1, voltage_v=5.0)
    read = status(bench, channel=1)

    assert written["ok"] is True, written
    assert "arm_protection" not in [entry[0] for entry in bench.unit.calls], bench.unit.calls
    for result in (written, read):
        protection = result["protection"]["1"]
        assert protection["ovp"]["support"] == support and protection["ocp"]["support"] == support, protection
        assert protection["ovp"]["armed"] is None, protection


def test_only_the_threshold_protection_is_armed_when_the_other_is_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, type_record=record(ocp="unsupported", ocp_readable=False))

    result = set_points(bench, voltage_v=5.0)

    assert result["ok"] is True, result
    assert [entry for entry in bench.unit.calls if entry[0] == "arm_protection"] == [("arm_protection", 1, EFFECTIVE_VOLTAGE, None)]
    assert result["protection"]["1"]["ocp"]["support"] == "unsupported", result


# ---------------------------------------------------------------------------
# Channels.


def test_each_channel_of_a_two_channel_supply_is_addressed_on_its_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, supply_type=TWO_CHANNEL_TYPE)

    result = set_points(bench, channel=2, voltage_v=3.3)
    read = status(bench)

    assert result["ok"] is True, result
    assert bench.unit.channels[2].set_voltage == Decimal("3.3")
    assert bench.unit.channels[1].set_voltage == PRESENT_VOLTAGE
    assert read["ok"] is True, read
    assert [entry["channel"] for entry in read["channels"]] == [1, 2], read
    # This fake measures nothing: the fields are there and empty, never guessed.
    assert all(entry["measured_voltage_v"] is None and entry["measured_current_a"] is None for entry in read["channels"]), read


def test_a_multi_channel_supply_needs_its_channel_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, supply_type=TWO_CHANNEL_TYPE)

    result = set_points(bench, voltage_v=5.0)

    assert_refused_before_contact(bench, result, "invalid_argument")


@pytest.mark.parametrize(("supply_type", "channel"), [(THRESHOLD_TYPE, 2), (TWO_CHANNEL_TYPE, 3)])
def test_a_channel_the_supply_does_not_have_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supply_type: str, channel: int) -> None:
    bench = make_bench(tmp_path, monkeypatch, supply_type=supply_type)

    result = set_points(bench, channel=channel, voltage_v=5.0)

    assert_refused_before_contact(bench, result, "invalid_argument")


def test_an_unknown_supply_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)

    result = call(bench, "power_supply_status", {"supply_id": "nowhere"})

    assert_refused_before_contact(bench, result, "unknown_device")


# ---------------------------------------------------------------------------
# Output state (D7, C2).


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        pytest.param("power_supply_output", {"on": True}, id="output_on"),
        pytest.param("power_supply_output", {"on": False}, id="output_off"),
        pytest.param("power_supply_cycle", {"off_s": 0.5}, id="cycle"),
    ],
)
def test_an_unreadable_output_state_answers_not_supported_before_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any]
) -> None:
    bench = make_bench(tmp_path, monkeypatch, type_record=record(output_readable=False))

    result = call(bench, tool, {"supply_id": SUPPLY, **arguments})

    assert_refused_before_contact(bench, result, "not_supported")
    assert result["capability"] == "output_state", result
    assert result["retry_safe"] is False, result


def test_an_unreadable_output_state_still_allows_set_points_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, type_record=record(output_readable=False))

    written = set_points(bench, voltage_v=5.0)
    read = status(bench)

    assert written["ok"] is True, written
    assert read["ok"] is True and read["channels"][0]["output_on"] is None, read


# ---------------------------------------------------------------------------
# Output and power cycle.


def test_output_on_and_off_are_confirmed_by_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)

    on = output(bench, True)
    read_on = status(bench)
    off = output(bench, False)
    read_off = status(bench)

    assert on["ok"] is True and on["read_back"] == {"output_on": True}, on
    assert read_on["channels"][0]["output_on"] is True, read_on
    assert Decimal(read_on["channels"][0]["measured_voltage_v"]) == PRESENT_VOLTAGE, read_on
    assert off["ok"] is True and off["read_back"] == {"output_on": False}, off
    assert read_off["channels"][0]["output_on"] is False, read_off
    assert on["last_known_output"]["output_on"] is True and off["last_known_output"]["output_on"] is False


def test_a_cycle_sleeps_once_between_off_and_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)
    sleeps = record_sleep(monkeypatch, bench.unit)
    bench.unit.channels[1].output_on = True

    result = cycle(bench, off_s=2.5)

    assert result["ok"] is True, result
    assert sleeps == [2.5]
    names = [entry[0:3] for entry in bench.unit.calls if entry[0] in {"set_output", "sleep"}]
    assert names == [("set_output", 1, False), ("sleep", 2.5), ("set_output", 1, True)], bench.unit.calls
    assert result["off_s"] == 2.5, result
    assert bench.unit.channels[1].output_on is True


@pytest.mark.parametrize("off_s", [0.4, 30.5, 0])
def test_a_cycle_outside_its_bounds_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, off_s: float) -> None:
    bench = make_bench(tmp_path, monkeypatch)
    sleeps = record_sleep(monkeypatch, bench.unit)

    result = cycle(bench, off_s=off_s)

    assert_refused_before_contact(bench, result, "invalid_argument")
    assert sleeps == []


# ---------------------------------------------------------------------------
# Criterion 6: each failure names its cause and the last known output.


def test_the_five_failure_causes_are_distinct_error_types(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from power_supply_fakes import fake_inventory

    seen: dict[str, str] = {}

    missing = make_bench(tmp_path / "missing", monkeypatch, inventory=[])
    seen["missing_device"] = set_points(missing, voltage_v=5.0)["error_type"]

    wrong = make_bench(tmp_path / "wrong", monkeypatch, inventory=[])
    fake_inventory(monkeypatch, host_port(wrong.device, fresh_serial()))
    seen["wrong_usb_identity"] = set_points(wrong, voltage_v=5.0)["error_type"]

    unknown = make_bench(tmp_path / "unknown", monkeypatch, unit_changes={"accepted": False})
    seen["unknown_identity"] = set_points(unknown, voltage_v=5.0)["error_type"]

    silent = make_bench(tmp_path / "silent", monkeypatch, unit_changes={"no_reply": {"channel_status"}})
    seen["reply_timeout"] = status(silent)["error_type"]

    skewed = make_bench(tmp_path / "skewed", monkeypatch, unit_changes={"skew": {"set_current": Decimal(CURRENT_RESOLUTION)}})
    seen["failed_read_back"] = set_points(skewed, current_a=0.5)["error_type"]

    assert set(seen.values()) == CRITERION_6, seen
    assert len(set(seen.values())) == 5, seen


# ---------------------------------------------------------------------------
# Binding and connections (D5).


def test_a_type_without_a_backend_answers_the_unbound_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, bind=False)

    result = status(bench)

    assert result["ok"] is False, result
    assert result["error_type"] == "not_supported", result
    assert result["retry_safe"] is False, result
    assert bench.unit.calls == []


def test_the_unbound_backend_returns_its_refusal_and_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.debugger import UnboundPowerSupplyBackend

    bench = make_bench(tmp_path, monkeypatch, bind=False)
    backend = UnboundPowerSupplyBackend(bench.config)

    answers = [
        backend.identity(),
        backend.channel_status(1),
        backend.set_voltage(1, Decimal("1")),
        backend.set_current(1, Decimal("0.1")),
        backend.set_output(1, True),
        backend.arm_protection(1, None, None),
    ]

    for answer in answers:
        assert isinstance(answer, dict), answer
        assert answer["ok"] is False and answer["error_type"] == "not_supported", answer
        assert answer["retry_safe"] is False, answer


def test_each_call_opens_its_own_connection_and_closes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)
    service = bench.service()
    try:
        call(bench, "power_supply_status", {"supply_id": SUPPLY}, service=service)
        call(bench, "power_supply_set", {"supply_id": SUPPLY, "voltage_v": 5.0}, service=service)
        call(bench, "power_supply_output", {"supply_id": SUPPLY, "on": True}, service=service)
    finally:
        service.close()

    assert bench.unit.connections == 3
    assert [entry[0] for entry in bench.unit.calls].count("close") == 3
    assert [entry[0] for entry in bench.unit.calls].count("identity") == 3


def test_a_failed_call_still_closes_its_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, unit_changes={"skew": {"set_voltage": Decimal(VOLTAGE_RESOLUTION)}})

    set_points(bench, voltage_v=5.0)

    assert bench.unit.calls[-1] == ("close",), bench.unit.calls


# ---------------------------------------------------------------------------
# Leases (C3).


def test_a_write_holds_the_supply_and_every_powered_debugger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.bench import physical_resources
    from agentic_hil.devices import debugger_device, power_supply_device

    bench = make_bench(tmp_path, monkeypatch)

    result = set_points(bench, voltage_v=5.0)

    assert result["ok"] is True, result
    expected = set(power_supply_device(bench.config, SUPPLY).lock_keys) | set(debugger_device(bench.config, "dut").lock_keys)
    assert expected <= set(result["resources"]), result
    # The run block names what it declared by physical lock key.
    declared = set(result["run"]["declared_devices"])
    assert set(physical_resources(power_supply_device(bench.config, SUPPLY).declared_keys)) <= declared, result
    assert set(physical_resources(debugger_device(bench.config, "dut").declared_keys)) <= declared, result


def test_a_status_read_holds_the_supply_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.devices import debugger_device, power_supply_device

    bench = make_bench(tmp_path, monkeypatch)

    result = status(bench)

    assert result["ok"] is True, result
    assert set(power_supply_device(bench.config, SUPPLY).lock_keys) <= set(result["resources"]), result
    assert not set(debugger_device(bench.config, "dut").lock_keys) & set(result["resources"]), result
    assert "run" not in result, result


def test_inside_a_run_an_undeclared_supply_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)
    service = bench.service()
    try:
        started = service.call("bench_run_start", {"devices": [{"kind": "debugger", "id": "dut"}]})
        assert started["ok"] is True, started

        result = call(bench, "power_supply_set", {"supply_id": SUPPLY, "voltage_v": 5.0}, service=service)

        service.call("bench_run_stop")
    finally:
        service.close()

    assert result["ok"] is False, result
    assert result["error_type"] == "undeclared_device", result
    assert contacted(bench) == [], bench.unit.calls


def test_inside_a_run_a_declared_supply_and_board_are_driven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)
    service = bench.service()
    try:
        started = service.call(
            "bench_run_start", {"devices": [{"kind": "debugger", "id": "dut"}, {"kind": "power_supply", "id": SUPPLY}]}
        )
        assert started["ok"] is True, started

        result = call(bench, "power_supply_set", {"supply_id": SUPPLY, "voltage_v": 5.0}, service=service)

        service.call("bench_run_stop")
    finally:
        service.close()

    assert result["ok"] is True, result


# ---------------------------------------------------------------------------
# Reports.


def test_the_report_carries_the_operation_and_the_backend_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.report import read_last_report

    bench = make_bench(tmp_path, monkeypatch)

    result = set_points(bench, voltage_v=5.0)
    report = read_last_report(bench.config)

    assert result["ok"] is True, result
    assert "backend_evidence" not in result, result
    assert report.get("tool") == "power_supply_set", report
    assert report["backend_evidence"], report
    for key in ("requested", "read_back", "measured", "limits", "model", "firmware", "channels"):
        assert key in report, (key, report)


def test_a_backend_bug_is_contained_and_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch, unit_changes={"raises": {"set_voltage": RuntimeError("synthetic backend bug")}})

    result = set_points(bench, voltage_v=5.0)

    assert result["ok"] is False, result
    assert result.get("cleanup_reasons"), result
    assert bench.unit.calls[-1] == ("close",), bench.unit.calls
