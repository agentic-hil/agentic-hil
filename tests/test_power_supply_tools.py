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
from power_supply_fakes import (
    THRESHOLD_TYPE,
    call,
    dut_entry,
    fresh_resource,
    install_fake_serial,
    make_bench,
    record_sleep,
    synthetic_device,
    write_elf,
)

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


# ---------------------------------------------------------------------------
# This server's own sessions on a board the supply powers (issue #734).
#
# A call that changes the supply locks every board it powers, so a session this
# server keeps on one of those boards would collide with it at the lease and
# answer as a hold nobody named. These are checked before the lease, and before
# anything reaches the supply: the refusal names the session and the call that
# frees it.


def _session_next_step(port_id: str, supply_id: str, tool: str) -> str:
    return (
        f"This server's own COM session on '{port_id}' holds the board that power supply '{supply_id}' powers, so "
        f"{tool} was refused before contact. Stop that session with com_session_stop and call {tool} again. A cold "
        "boot can be watched on a console only if the console's port shares no lock key with the debuggers this "
        "supply powers; only the owner can change that, in the authoritative configuration. See docs/power-supplies.md."
    )


def _cold_boot_order() -> list[str]:
    """The tool calls docs/power-supplies.md gives for watching a cold boot, in the order it gives them."""
    import re

    document = (Path(__file__).resolve().parents[1] / "docs" / "power-supplies.md").read_text(encoding="utf-8")
    assert "\n## Watching a cold boot\n" in document, "docs/power-supplies.md has no `## Watching a cold boot` section"
    section = document.split("\n## Watching a cold boot\n", 1)[1].split("\n## ", 1)[0]
    steps = [line for line in section.splitlines() if re.match(r"\d+\. ", line)]
    return [re.findall(r"`([a-z_]+)`", step)[0] for step in steps]


def test_an_open_debug_session_on_a_powered_debugger_refuses_with_held_by_debug_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.knowledge import remediation_fields

    bench = make_bench(tmp_path, monkeypatch)
    write_elf(bench.workspace)
    service = bench.service()
    try:
        started = service.call("debug_start_session", {"image_path": "build/app.elf"})
        assert started["ok"] is True, started
        result = service.call("power_supply_set", {"supply_id": bench.supply_id, "voltage_v": 3.3})
    finally:
        service.call("debug_stop_session", {})
        service.close()

    assert result["ok"] is False, result
    assert result["error_type"] == "device_busy", result
    assert result["held_by_debug_session"] is True, result
    assert result["side_effect_status"] == "not_started", result
    assert result["hardware_state"] == "unchanged", result
    assert result["last_known_output"]["output_on"] is None, result
    assert result["remediation"] == remediation_fields("device_busy")["remediation"], result
    assert "debug_stop_session" in result["next_step"], result
    # Not even the identity query reached the supply.
    assert bench.unit.calls == [], bench.unit.calls


def test_this_servers_com_session_on_a_powered_boards_key_refuses_the_cycle_until_it_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.knowledge import remediation_fields

    shared = fresh_resource()
    bench = make_bench(
        tmp_path,
        monkeypatch,
        debuggers={"dut": dut_entry(resource_id=shared)},
        extra_com_ports={"console": {"device": synthetic_device(), "resource_id": shared}},
    )
    install_fake_serial(monkeypatch)
    sleeps = record_sleep(monkeypatch, bench.unit)
    service = bench.service()
    try:
        opened = service.call("com_session_start", {"port_id": "console", "clear_buffer": True})
        assert opened["ok"] is True, opened
        refused = service.call("power_supply_cycle", {"supply_id": bench.supply_id, "off_s": 0.5})
        reached_while_refused = list(bench.unit.calls)
        stopped = service.call("com_session_stop", {"port_id": "console"})
        assert stopped["ok"] is True, stopped
        cycled = service.call("power_supply_cycle", {"supply_id": bench.supply_id, "off_s": 0.5})
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "device_busy", refused
    assert refused["held_by_com_session"] == "console", refused
    assert refused["side_effect_status"] == "not_started", refused
    assert refused["hardware_state"] == "unchanged", refused
    assert refused["last_known_output"]["output_on"] is None, refused
    assert refused["remediation"] == remediation_fields("device_busy")["remediation"], refused
    assert refused["next_step"] == _session_next_step("console", bench.supply_id, "power_supply_cycle"), refused
    # The fake records no call at all: not even the identity query was sent.
    assert reached_while_refused == [], reached_while_refused
    # The same cycle goes through once the session is stopped.
    assert cycled["ok"] is True, cycled
    assert sleeps == [0.5], sleeps


def test_a_console_session_sharing_no_key_stays_open_across_the_cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The order docs/power-supplies.md gives for watching a cold boot, run as written.

    The console shares no lock key with the supply or the board it powers, so the
    two leases take different locks, and the session stays open across the cycle."""
    order = _cold_boot_order()
    assert order == ["com_session_start", "power_supply_cycle", "com_read"], order

    bench = make_bench(tmp_path, monkeypatch, extra_com_ports={"console": {"device": synthetic_device(), "resource_id": fresh_resource()}})
    install_fake_serial(monkeypatch)
    sleeps = record_sleep(monkeypatch, bench.unit)
    service = bench.service()
    try:
        arguments = {
            "com_session_start": {"port_id": "console", "clear_buffer": True},
            "power_supply_cycle": {"supply_id": bench.supply_id, "off_s": 0.5},
            "com_read": {"port_id": "console"},
        }
        results = [service.call(name, arguments[name]) for name in order]
        still_open = "console" in service.com_ports.sessions
        service.call("com_session_stop", {"port_id": "console"})
    finally:
        service.close()

    for name, result in zip(order, results, strict=True):
        assert result["ok"] is True, (name, result)
    assert still_open
    assert sleeps == [0.5], sleeps
    assert [call[0] for call in bench.unit.writes()] == ["arm_protection", "set_output", "set_output"], bench.unit.calls
