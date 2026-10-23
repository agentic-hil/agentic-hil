"""The four power supply tools as this server advertises and dispatches them (issue #733).

What a host sees (names, schemas, annotations), how the dispatch classifies the
tools (audited, read-only, run inside an implicit run), the device kind a supply
is locked and declared as, the catalogue entries for the new error types, and the
rule that a supply's port is reserved for the supply.

Product imports sit inside each test, so each node fails on its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from power_supply_fakes import THRESHOLD_TYPE, call, make_bench

SUPPLY_TOOLS = ("power_supply_status", "power_supply_set", "power_supply_output", "power_supply_cycle")
WRITE_TOOLS = ("power_supply_set", "power_supply_output", "power_supply_cycle")
NEW_ERROR_TYPES = (
    "power_supply_limit_exceeded",
    "power_supply_identity_unknown",
    "power_supply_identity_mismatch",
    "power_supply_no_reply",
    "power_supply_readback_mismatch",
    "power_supply_protection_not_armed",
    "com_port_reserved_for_power_supply",
)


def tool(name: str) -> dict[str, Any]:
    from agentic_hil.contracts import MCP_TOOLS

    return next(entry for entry in MCP_TOOLS if entry["name"] == name)


# ---------------------------------------------------------------------------
# What a host sees.


def test_the_four_supply_tools_are_advertised() -> None:
    from agentic_hil.contracts import MCP_TOOL_NAMES

    assert set(SUPPLY_TOOLS) <= set(MCP_TOOL_NAMES)


@pytest.mark.parametrize(
    ("name", "read_only", "destructive", "idempotent"),
    [
        ("power_supply_status", True, None, None),
        ("power_supply_set", False, True, True),
        ("power_supply_output", False, True, True),
        ("power_supply_cycle", False, True, False),
    ],
)
def test_the_supply_tool_annotations(name: str, read_only: bool, destructive: bool | None, idempotent: bool | None) -> None:
    from agentic_hil.contracts import TOOL_ANNOTATIONS

    annotations = TOOL_ANNOTATIONS[name]

    assert annotations["readOnlyHint"] is read_only, annotations
    assert annotations["openWorldHint"] is False, annotations
    assert annotations.get("title"), annotations
    if destructive is not None:
        assert annotations["destructiveHint"] is destructive, annotations
        assert annotations["idempotentHint"] is idempotent, annotations


def test_every_supply_tool_takes_a_supply_and_an_optional_channel() -> None:
    for name in SUPPLY_TOOLS:
        schema = tool(name)["inputSchema"]
        assert "supply_id" in schema["properties"] and "channel" in schema["properties"], schema
        assert "supply_id" in schema.get("required", []), schema
        assert "channel" not in schema.get("required", []), schema
        assert schema["properties"]["channel"]["type"] == "integer", schema
        assert schema["properties"]["channel"]["minimum"] == 1, schema


def test_the_set_schema_takes_two_optional_values() -> None:
    properties = tool("power_supply_set")["inputSchema"]["properties"]
    required = tool("power_supply_set")["inputSchema"].get("required", [])

    assert {"voltage_v", "current_a"} <= set(properties), properties
    assert not {"voltage_v", "current_a"} & set(required), required


def test_the_output_schema_requires_the_state() -> None:
    schema = tool("power_supply_output")["inputSchema"]

    assert schema["properties"]["on"]["type"] == "boolean", schema
    assert "on" in schema["required"], schema


def test_the_cycle_schema_bounds_the_off_time() -> None:
    schema = tool("power_supply_cycle")["inputSchema"]
    off_s = schema["properties"]["off_s"]

    assert off_s["minimum"] == 0.5 and off_s["maximum"] == 30, off_s
    assert "off_s" in schema["required"], schema


def test_the_device_selector_offers_the_power_supply_kind() -> None:
    from agentic_hil.contracts import DEVICE_SELECTOR
    from agentic_hil.devices import DEVICE_KINDS

    assert "power_supply" in DEVICE_SELECTOR["properties"]["kind"]["enum"]
    assert "power_supply" in DEVICE_KINDS


# ---------------------------------------------------------------------------
# How the dispatch classifies them.


def test_the_supply_tools_are_audited_and_only_status_is_read_only() -> None:
    from agentic_hil.tools import _READ_ONLY_HARDWARE_TOOLS, audited_hardware_tools

    assert set(SUPPLY_TOOLS) <= audited_hardware_tools()
    assert "power_supply_status" in _READ_ONLY_HARDWARE_TOOLS
    assert not set(WRITE_TOOLS) & set(_READ_ONLY_HARDWARE_TOOLS)


def test_the_supply_writes_run_inside_an_implicit_run() -> None:
    from agentic_hil.tools import implicit_run_tools

    assert set(WRITE_TOOLS) <= implicit_run_tools()
    assert "power_supply_status" not in implicit_run_tools()


def test_the_implicit_run_holds_the_supply_and_every_board_it_powers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.devices import debugger_device, power_supply_device
    from agentic_hil.tools import implicit_run_resources

    bench = make_bench(tmp_path, monkeypatch)

    for name in WRITE_TOOLS:
        devices = implicit_run_resources(bench.config, name, {"supply_id": bench.supply_id})
        keys = {key for item in devices for key in ((item,) if isinstance(item, str) else item.lock_keys)}
        assert set(power_supply_device(bench.config, bench.supply_id).lock_keys) <= keys, (name, keys)
        assert set(debugger_device(bench.config, "dut").lock_keys) <= keys, (name, keys)


def test_a_write_result_declares_the_supply_and_its_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.bench import physical_resources
    from agentic_hil.devices import debugger_device, power_supply_device

    bench = make_bench(tmp_path, monkeypatch)

    result = call(bench, "power_supply_set", {"supply_id": bench.supply_id, "voltage_v": 5.0})

    assert result["ok"] is True, result
    assert result["run"]["implicit"] is True, result
    # The run block names what it declared by physical lock key: the supply and the board it powers, nothing else.
    expected = set(physical_resources(power_supply_device(bench.config, bench.supply_id).declared_keys)) | set(
        physical_resources(debugger_device(bench.config, "dut").declared_keys)
    )
    assert set(result["run"]["declared_devices"]) == expected, result


# ---------------------------------------------------------------------------
# The device kind.


def test_a_supply_resolves_as_a_device_keyed_by_its_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.devices import resolve_device, uart_device

    bench = make_bench(tmp_path, monkeypatch)

    device = resolve_device(bench.config, {"kind": "power_supply", "id": bench.supply_id})

    assert device.kind == "power_supply"
    assert device.config_id == bench.supply_id
    # The port's lock and declared keys: a supply is the unit behind that port.
    port = uart_device(bench.config, bench.port_id)
    assert device.lock_key == port.lock_key
    assert set(device.lock_keys) == set(port.lock_keys)
    assert set(device.declared_keys) == set(port.declared_keys)
    assert device.as_json()["kind"] == "power_supply"


def test_require_lockable_accepts_the_supply_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.devices import DeviceSet, debugger_device, power_supply_device

    bench = make_bench(tmp_path, monkeypatch)
    devices = DeviceSet((power_supply_device(bench.config, bench.supply_id), debugger_device(bench.config, "dut")))

    devices.require_lockable()

    assert devices.unlockable_keys() == []


@pytest.mark.parametrize(
    ("selector", "error_type"),
    [
        pytest.param({"kind": "power_supply"}, "invalid_argument", id="unnamed"),
        pytest.param({"kind": "power_supply", "id": "nowhere"}, "unknown_device", id="unknown"),
    ],
)
def test_a_supply_selector_needs_a_configured_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: dict[str, Any], error_type: str) -> None:
    from agentic_hil.devices import DeviceError, resolve_device

    bench = make_bench(tmp_path, monkeypatch)

    with pytest.raises(DeviceError) as refused:
        resolve_device(bench.config, selector)

    assert refused.value.result["error_type"] == error_type, refused.value.result
    assert refused.value.result["kind"] == "power_supply", refused.value.result
    assert refused.value.result["configured_devices"] == [bench.supply_id], refused.value.result


def test_a_run_may_declare_a_supply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)
    service = bench.service()
    try:
        started = service.call("bench_run_start", {"devices": [{"kind": "power_supply", "id": bench.supply_id}]})
        stopped = service.call("bench_run_stop")
    finally:
        service.close()

    assert started["ok"] is True, started
    assert {"kind": "power_supply", "id": bench.supply_id} in [{"kind": entry["kind"], "id": entry["id"]} for entry in started["devices"]], started
    assert stopped["ok"] is True, stopped


# ---------------------------------------------------------------------------
# A supply's port is the supply's (D3).


def test_a_com_session_on_the_supply_port_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)

    result = call(bench, "com_session_start", {"port_id": bench.port_id})

    assert result["ok"] is False, result
    assert result["error_type"] == "com_port_reserved_for_power_supply", result
    assert result["power_supply"] == bench.supply_id, result
    assert result["retry_safe"] is False, result
    assert result["remediation"], result
    assert bench.unit.calls == []


def test_a_flash_capture_on_the_supply_port_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make_bench(tmp_path, monkeypatch)
    image = bench.workspace / "build" / "app.elf"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"\x7fELF" + b"\x00" * 12)

    result = call(
        bench,
        "flash_firmware",
        {"image_path": "build/app.elf", "reset_after_flash": True, "capture": {"port_id": bench.port_id}},
    )

    assert result["ok"] is False, result
    assert result["error_type"] == "com_port_reserved_for_power_supply", result
    assert result["side_effect_status"] == "not_started", result
    assert bench.unit.calls == []


# ---------------------------------------------------------------------------
# The catalogue.


@pytest.mark.parametrize("error_type", NEW_ERROR_TYPES)
def test_each_new_error_type_has_a_catalogue_entry(error_type: str) -> None:
    from agentic_hil.knowledge import ERROR_CATALOGUE, remediation_fields

    assert error_type in ERROR_CATALOGUE
    assert remediation_fields(error_type).get("remediation"), error_type


@pytest.mark.parametrize(
    ("error_type", "scope"),
    [("not_supported", "power_supply"), ("not_supported", "unbound_power_supply"), ("permission_denied", "power_supply_limits")],
)
def test_the_scoped_entries_carry_advice(error_type: str, scope: str) -> None:
    from agentic_hil.knowledge import lookup_remedy, remediation_fields

    assert lookup_remedy(error_type, scope) is not lookup_remedy(error_type), (error_type, scope)
    assert remediation_fields(error_type, scope).get("remediation"), (error_type, scope)


def test_the_protection_entry_says_report_and_stop() -> None:
    from agentic_hil.knowledge import remediation_fields

    advice = remediation_fields("power_supply_protection_not_armed")
    text = " ".join([*advice.get("remediation", []), *advice.get("do_not", [])])

    assert "owner" in text and "retry" in text, advice


def test_the_unconfirmed_supply_state_has_a_quarantine_guide() -> None:
    from agentic_hil.knowledge import QUARANTINE_REASON_GUIDES

    guide = QUARANTINE_REASON_GUIDES["power_supply_state_unconfirmed"].as_json("power_supply_state_unconfirmed")

    assert guide["reason"] == "power_supply_state_unconfirmed", guide
    assert all(guide.get(field) for field in ("attempted", "confirmed", "unknown", "physical_check")), guide


def test_the_unbound_refusal_is_not_supported_and_not_retry_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.knowledge import remediation_fields
    from agentic_hil.tools import unbound_power_supply_error

    bench = make_bench(tmp_path, monkeypatch, bind=False)

    refusal = unbound_power_supply_error(bench.config, "power_supply_status", bench.supply_id)

    assert refusal["ok"] is False and refusal["error_type"] == "not_supported", refusal
    assert refusal["retry_safe"] is False, refusal
    assert refusal["supply_id"] == bench.supply_id, refusal
    assert refusal["supply_type"] == THRESHOLD_TYPE, refusal
    assert THRESHOLD_TYPE in refusal["summary"], refusal
    assert refusal["remediation"] == remediation_fields("not_supported", "unbound_power_supply")["remediation"], refusal
