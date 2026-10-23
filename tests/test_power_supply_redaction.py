"""No supply serial leaves this process in clear (issues #731 and #732).

The first part is #731: no load-time refusal of a power supply repeats a serial,
an idn_serial or a port's resource id. The first twenty-three cases are the
refusals of `test_power_supply_config.py`, reached again from the outside:
`str(error)`, `to_dict()`, the stderr of `stdio.mcp_stdio` and the stderr of
`cli.emit_protocol_error`. The last four are schema refusals at the fields that
carry a secret.

The second part is #732: the serials of a supply, its port's `serial_number`, its
`idn_serial` and every serial the host inventory reports for a supply's USB
identity, are masked at every point that writes or prints a result. Only
`agentic-hil com-ports` prints them in clear, because the owner binds a supply
from it. Every value is synthetic and built from uuid4.
"""

from __future__ import annotations

import copy
import dataclasses
import io
import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import serial
from power_supply_fakes import Bench as SupplyBench
from power_supply_fakes import fake_inventory, host_port
from power_supply_fakes import make_bench as make_supply_bench
from test_adopt_failed_com_inventory import KEY, PROBE, _stm32cubeprogrammer_host
from test_config_adopt import placeholder_bench
from test_contact_marker import FakePort
from test_device_access import BY_ID_PATH, ON_THE_HOST, recorded_host
from test_power_supply_config import REFUSALS, Refusal, Values, document, port, supply, write_bench

from agentic_hil import cli, comstdio, coordination, device_access, mcp, power_supply, redact, stdio
from agentic_hil.adopt import PROJECT_CONFIG_ADOPT
from agentic_hil.comports import list_available_com_ports
from agentic_hil.config import ConfigError, load_authoritative_config
from agentic_hil.coordination import HardwareCoordinator
from agentic_hil.report import (
    canonical_run_report_path,
    last_failure_path,
    last_report_path,
    report_state_path,
    write_report,
)
from agentic_hil.runevidence import write_run_evidence
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import AgenticHILConfig, JsonObject, PowerSupplyFault

SCHEMA_CASES: tuple[Refusal, ...] = (
    Refusal(
        "schema-idn-serial-pattern",
        ("power_supplies.bench.idn_serial",),
        lambda v: document(
            v,
            com_ports={"psu": port(v.serial, v.resource, drop=("serial_number",))},
            power_supplies={"bench": supply(idn_serial=f"{v.serial} spaced")},
        ),
    ),
    Refusal(
        "schema-port-serial-pattern",
        ("com_ports.psu.serial_number",),
        lambda v: document(v, com_ports={"psu": port(f"{v.serial} spaced", v.resource)}),
    ),
    Refusal(
        "schema-port-serial-integer",
        ("com_ports.psu.serial_number",),
        lambda v: document(v, com_ports={"psu": port(v.number, v.resource)}),
    ),
    Refusal(
        "schema-port-resource-id-pattern",
        ("com_ports.psu.resource_id",),
        lambda v: document(v, com_ports={"psu": port(v.serial, f"AHIL {v.resource}")}),
    ),
)
CASES: tuple[Refusal, ...] = REFUSALS + SCHEMA_CASES


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_a_load_time_refusal_never_repeats_a_supply_serial(
    case: Refusal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    values = Values.fresh()
    workspace, _ = write_bench(tmp_path, monkeypatch, **case.build(values))

    with pytest.raises(ConfigError) as caught:
        load_authoritative_config(workspace)
    error = caught.value
    secrets = values.secrets()

    # The MCP startup path: the refusal is written to stderr, and the server does not start.
    started: list[object] = []
    monkeypatch.setattr(stdio, "run_stdio_server", lambda *args, **kwargs: started.append(args))
    assert stdio.mcp_stdio() == 2
    assert started == []
    stdio_stderr = capsys.readouterr().err
    assert json.loads(stdio_stderr)["error_type"] == "config_invalid"
    assert json.loads(stdio_stderr)["field"] in case.fields

    # The CLI protocol-error path, fed the same refusal.
    cli.emit_protocol_error(error.to_dict(), "mcp-stdio")
    cli_stderr = capsys.readouterr().err
    cli_refusal = json.loads(cli_stderr)
    assert cli_refusal["error_type"] == "config_invalid"
    assert cli_refusal["field"] in case.fields

    for text in (str(error), json.dumps(error.to_dict()), stdio_stderr, cli_stderr):
        for secret in secrets:
            assert secret.lower() not in text.lower(), case.name


# ---------------------------------------------------------------------------
# #732: the serials of a supply are masked wherever a result is written or printed.

MARKER = "[redacted]"
# The USB identity of the rs3005p's serial bridge, as the inventory reports it.
SUPPLY_VID = 0x0416
SUPPLY_PID = 0x5011


def fresh_serial() -> str:
    return f"SN{uuid.uuid4().hex.upper()}"


def supply_port_info(device: str, serial: str, *, vid: int = SUPPLY_VID, pid: int = SUPPLY_PID) -> SimpleNamespace:
    """One entry of `list_ports.comports()`, in the attributes the inventory reads."""
    return SimpleNamespace(
        device=device,
        name=device,
        description="USB serial bridge",
        hwid=f"USB VID:PID={vid:04X}:{pid:04X} SER={serial} LOCATION=1-4:1.0",
        location="1-4:1.0",
        serial_number=serial,
        vid=vid,
        pid=pid,
    )


def host_inventory(monkeypatch: pytest.MonkeyPatch, *entries: SimpleNamespace) -> None:
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: list(entries))


@pytest.fixture(autouse=True)
def _no_host_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here reads the serial ports of the machine it runs on."""
    host_inventory(monkeypatch)


def assert_masked(text: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret.lower() not in text.lower()


def files_below(root: Path) -> Iterator[tuple[Path, str]]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path, path.read_bytes().decode("utf-8", errors="replace")


# --- redact_values ----------------------------------------------------------


def test_redact_values_matches_a_secret_whatever_its_case() -> None:
    secret = fresh_serial()
    masked = redact.redact_values({"summary": f"unit {secret.lower()} and unit {secret} answered"}, frozenset({secret}))
    assert masked == {"summary": f"unit {MARKER} and unit {MARKER} answered"}


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("USB VID:PID=0416:5011 SER={s} LOCATION=1-4", f"USB VID:PID=0416:5011 SER={MARKER} LOCATION=1-4"),
        ("/dev/serial/by-id/usb-Vendor_Bridge_{s}-if00", f"/dev/serial/by-id/usb-Vendor_Bridge_{MARKER}-if00"),
        ("SN:{s}", f"SN:{MARKER}"),
        ("the unit '{s}' answered", f"the unit '{MARKER}' answered"),
        ('"{s}"', f'"{MARKER}"'),
        ("{s}", MARKER),
    ],
    ids=["hwid", "by_id_path", "idn_field", "single_quoted", "double_quoted", "whole_value"],
)
def test_redact_values_masks_a_secret_with_no_letter_or_digit_beside_it(template: str, expected: str) -> None:
    secret = fresh_serial()
    assert redact.redact_values({"value": template.format(s=secret)}, frozenset({secret})) == {"value": expected}


@pytest.mark.parametrize("template", ["X{s}", "{s}0", "a{s}b", "9{s}Z"], ids=["letter_before", "digit_after", "letters_around", "digit_and_letter"])
def test_redact_values_leaves_a_secret_inside_a_longer_word(template: str) -> None:
    secret = fresh_serial()
    value = {"value": template.format(s=secret)}
    assert redact.redact_values(value, frozenset({secret})) == value


def test_redact_values_never_alters_a_digest_that_contains_a_secret() -> None:
    secret = uuid.uuid4().hex[:12]
    digest = f"{uuid.uuid4().hex}{secret}{uuid.uuid4().hex}"
    value = {"config_sha256": digest, "token": f"a{secret}0"}
    assert redact.redact_values(value, frozenset({secret})) == value


def test_redact_values_replaces_the_longest_secret_first() -> None:
    short = fresh_serial()
    long = f"{short}-01"
    masked = redact.redact_values({"summary": f"id {long} end, id {short} end"}, frozenset({short, long}))
    assert masked == {"summary": f"id {MARKER} end, id {MARKER} end"}


def test_redact_values_searches_keys_and_values_at_any_depth() -> None:
    secret = fresh_serial()
    value = {
        f"port {secret}": 1,
        secret: {"serial_number": secret, "ports": [{"hwid": f"SER={secret}"}, f"SN:{secret}"]},
        "count": 3,
        "flag": True,
        "nothing": None,
        "ratio": 1.5,
    }
    assert redact.redact_values(value, frozenset({secret})) == {
        f"port {MARKER}": 1,
        MARKER: {"serial_number": MARKER, "ports": [{"hwid": f"SER={MARKER}"}, f"SN:{MARKER}"]},
        "count": 3,
        "flag": True,
        "nothing": None,
        "ratio": 1.5,
    }


def test_redact_values_leaves_preserved_keys_as_they_are() -> None:
    secret = fresh_serial()
    value = {
        "workspace": f"/work/{secret}/firmware",
        "resources": [f"physical:{secret}"],
        "summary": f"unit {secret}",
        "lease": {"workspace": f"/work/{secret}", "summary": f"unit {secret}"},
    }
    masked = redact.redact_values(value, frozenset({secret}), preserve_keys=("workspace", "resources"))
    assert masked == {
        "workspace": f"/work/{secret}/firmware",
        "resources": [f"physical:{secret}"],
        "summary": f"unit {MARKER}",
        "lease": {"workspace": f"/work/{secret}", "summary": f"unit {MARKER}"},
    }


def test_redact_values_with_no_secret_returns_its_input_unchanged() -> None:
    value = {"summary": f"unit {fresh_serial()}", "ports": [{"serial_number": fresh_serial()}]}
    assert redact.redact_values(value, frozenset()) == value
    # A blank secret is no secret: it would otherwise match every gap between words.
    assert redact.redact_values(value, frozenset({"", "   "})) == value


def test_redact_values_returns_a_copy_and_leaves_its_input_alone() -> None:
    secret = fresh_serial()
    value: JsonObject = {"summary": f"unit {secret}", "ports": [{"serial_number": secret}]}
    before = copy.deepcopy(value)
    masked = redact.redact_values(value, frozenset({secret}))
    assert value == before
    assert masked != value


# --- supply_secrets ---------------------------------------------------------


def test_the_host_inventory_registers_supply_serials_for_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    supply_serial, other_serial = fresh_serial(), fresh_serial()
    host_inventory(monkeypatch, supply_port_info("COM251", supply_serial), supply_port_info("COM252", other_serial, vid=0x0403, pid=0x6001))
    assert supply_serial not in power_supply.supply_secrets(None)

    inventory = list_available_com_ports()

    assert inventory["ok"] is True, inventory
    # The inventory itself is the owner's reading, so it carries the serial as the host reported it.
    assert [entry["serial_number"] for entry in inventory["ports"]] == [supply_serial, other_serial]
    secrets = power_supply.supply_secrets(None)
    assert supply_serial in secrets
    assert other_serial not in secrets


def test_an_idn_serial_read_from_a_unit_is_registered_for_this_process() -> None:
    serial = fresh_serial()
    power_supply.register_idn_serial(f"  {serial}  ")
    power_supply.register_idn_serial("   ")
    secrets = power_supply.supply_secrets(None)
    assert serial in secrets
    assert "" not in secrets


def test_supply_secrets_of_a_config_are_its_port_serial_and_idn_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = Values.fresh()
    idn_serial, uart_serial, padded = fresh_serial(), fresh_serial(), fresh_serial()
    workspace, _ = write_bench(
        tmp_path,
        monkeypatch,
        com_ports={"psu": port(values.serial, values.resource), "uart": {"device": "COM251", "serial_number": uart_serial}},
        power_supplies={"bench": supply(idn_serial=idn_serial)},
    )
    config = load_authoritative_config(workspace)

    secrets = power_supply.supply_secrets(config)

    assert {values.serial, idn_serial} <= secrets
    # A port no supply uses is not a supply's, and a resource_id is a lock key, never a serial.
    assert uart_serial not in secrets
    assert values.resource not in secrets
    # Loading the file registered both, so a point with no config at hand knows them too.
    assert {values.serial, idn_serial} <= power_supply.supply_secrets(None)

    # Every value is stripped, and a blank one is no secret.
    bench = dataclasses.replace(config.power_supplies["bench"], idn_serial=f"  {padded}  ")
    psu = dataclasses.replace(config.com_ports["psu"], serial_number="   ")
    edited = dataclasses.replace(config, power_supplies={"bench": bench}, com_ports={**config.com_ports, "psu": psu})
    secrets = power_supply.supply_secrets(edited)
    assert padded in secrets
    assert f"  {padded}  " not in secrets
    assert not any(not secret.strip() for secret in secrets)


# --- tool results -----------------------------------------------------------


def _com_ports_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AgenticHILToolService, str, JsonObject, str]:
    values = Values.fresh()
    workspace, _ = write_bench(tmp_path, monkeypatch, **document(values))
    host_inventory(monkeypatch, supply_port_info("COM250", values.serial))
    return AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp"), "com_ports_list", {}, values.serial


def _project_config_describe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AgenticHILToolService, str, JsonObject, str]:
    values = Values.fresh()
    workspace, _ = write_bench(tmp_path, monkeypatch, **document(values))
    return AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp"), "project_config_describe", {}, values.serial


def _adopt_hardware_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AgenticHILToolService, str, JsonObject, str]:
    """No supply is configured: the serial is known only because the inventory reported it."""
    serial = fresh_serial()
    workspace, _ = placeholder_bench(tmp_path, monkeypatch)
    _stm32cubeprogrammer_host(monkeypatch)
    host_inventory(monkeypatch, supply_port_info("COM251", serial))
    return AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp"), PROJECT_CONFIG_ADOPT, {"probe_id": PROBE}, serial


def _careless_supply_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SupplyBench:
    """A fake supply whose identity parser left the serial in the firmware field.

    The class layer reports the firmware it was handed, so the serial reaches the
    result, and only the masking at the exit of `call` keeps it out."""
    bench = make_supply_bench(tmp_path, monkeypatch)
    assert bench.port_serial is not None
    bench.unit.firmware = f"F1 SN:{bench.port_serial}"
    return bench


def _power_supply_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AgenticHILToolService, str, JsonObject, tuple[str, ...]]:
    bench = _careless_supply_bench(tmp_path, monkeypatch)
    return bench.service(), "power_supply_status", {"supply_id": bench.supply_id}, bench.secrets()


def _power_supply_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AgenticHILToolService, str, JsonObject, tuple[str, ...]]:
    bench = _careless_supply_bench(tmp_path, monkeypatch)
    return bench.service(), "power_supply_set", {"supply_id": bench.supply_id, "voltage_v": 3.3}, bench.secrets()


def _identity_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AgenticHILToolService, str, JsonObject, tuple[str, ...]]:
    """The supply's device is now a unit with another serial: both serials are in the refusal."""
    bench = make_supply_bench(tmp_path, monkeypatch)
    other = fresh_serial()
    fake_inventory(monkeypatch, host_port(bench.device, other))
    return bench.service(), "power_supply_set", {"supply_id": bench.supply_id, "voltage_v": 3.3}, (*bench.secrets(), other)


def _identity_unverified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AgenticHILToolService, str, JsonObject, tuple[str, ...]]:
    """The supply's device reports no serial, so the expected one could not be compared."""
    bench = make_supply_bench(tmp_path, monkeypatch)
    fake_inventory(monkeypatch, host_port(bench.device, None))
    return bench.service(), "power_supply_status", {"supply_id": bench.supply_id}, bench.secrets()


TOOL_CASES: dict[str, Callable[[Path, pytest.MonkeyPatch], tuple[AgenticHILToolService, str, JsonObject, str | tuple[str, ...]]]] = {
    "com_ports_list": _com_ports_list,
    "project_config_describe": _project_config_describe,
    "adopt_hardware_refusal": _adopt_hardware_refusal,
    "power_supply_status": _power_supply_status,
    "power_supply_set": _power_supply_set,
    "identity_mismatch": _identity_mismatch,
    "identity_unverified": _identity_unverified,
}

# What each supply case answers, so a case that never reached the supply's port
# cannot pass on a refusal that holds no serial at all.
SUPPLY_TOOL_OUTCOMES: dict[str, str | None] = {
    "power_supply_status": None,
    "power_supply_set": None,
    "identity_mismatch": "com_port_identity_mismatch",
    "identity_unverified": "com_port_identity_unverified",
}


@pytest.mark.parametrize("case", list(TOOL_CASES))
def test_tool_results_carry_no_supply_serial(case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, name, arguments, planted = TOOL_CASES[case](tmp_path, monkeypatch)
    secrets = (planted,) if isinstance(planted, str) else planted
    try:
        result = tools.call(name, arguments)
        reply = mcp.call_tool({"name": name, "arguments": arguments}, tools)
    finally:
        tools.close()

    if case in SUPPLY_TOOL_OUTCOMES:
        expected = SUPPLY_TOOL_OUTCOMES[case]
        if expected is None:
            assert result["ok"] is True, result
        else:
            assert result.get("error_type") == expected, result
    for text in (json.dumps(result), json.dumps(reply)):
        assert_masked(text, *secrets)
        assert MARKER in text, case
    if case == "adopt_hardware_refusal":
        entries = [item for item in result["unavailable"] if item.get("key") == KEY]
        assert [entry["serial_number"] for entry in entries[0]["host_com_ports"]] == [MARKER], entries


# --- the command line -------------------------------------------------------

CLI_SINKS: dict[str, Callable[[JsonObject], object]] = {
    "emit_result_json": lambda result: cli.emit_result(result, "doctor", human=False),
    "emit_result_human": lambda result: cli.emit_result(result, "doctor", human=True),
    "emit_protocol_error": lambda result: cli.emit_protocol_error(result, "mcp-stdio"),
    "print_json": lambda result: cli.print_json(result, "doctor"),
}


@pytest.mark.parametrize("sink", list(CLI_SINKS))
def test_the_cli_output_functions_mask_supply_serials(sink: str, capsys: pytest.CaptureFixture[str]) -> None:
    serial = fresh_serial()
    power_supply.register_secrets([serial])
    result: JsonObject = {
        "ok": False,
        "tool": "agentic_hil_doctor",
        "error_type": "config_invalid",
        "summary": f"The unit SER={serial} did not answer.",
        "serial_number": serial,
    }

    CLI_SINKS[sink](result)

    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert_masked(text, serial)
    assert MARKER in text


def test_agentic_hil_com_ports_prints_the_supply_serial_in_clear(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The owner binds a supply by the serial this command shows, so this one command prints it."""
    serial = fresh_serial()
    host_inventory(monkeypatch, supply_port_info("COM251", serial))

    assert cli.entrypoint(["com-ports", "--json"]) == 0
    printed = capsys.readouterr().out
    inventory = json.loads(printed)
    assert inventory["ports"][0]["serial_number"] == serial
    assert cli.entrypoint(["com-ports"]) == 0
    assert serial in capsys.readouterr().out

    # The reading registered the serial, so every other command masks it.
    assert serial in power_supply.supply_secrets(None)
    cli.emit_result(inventory, "doctor", human=False)
    assert_masked(capsys.readouterr().out, serial)


def _doctor_by_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[list[str]], str]:
    """A supply bound by its by-id link, on the recorded Linux host.

    The host is the recording's own, with one link added beside the recorded
    probe link: the supply's, which names the same recorded port node."""
    values = Values.fresh()
    link = f"/dev/serial/by-id/usb-Vendor_Bridge_{values.serial}-if00"
    entry = port(values.serial, values.resource)
    entry["device"] = link
    write_bench(tmp_path, monkeypatch, com_ports={"psu": entry}, power_supplies={"bench": supply()})
    host = recorded_host(tmp_path, ON_THE_HOST)
    host.nodes[link] = {**host.nodes[BY_ID_PATH], "path": link}
    monkeypatch.setattr(device_access, "HOST", host)
    return [["doctor", "--json"], ["doctor"]], values.serial


def _adopt_hardware(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[list[str]], str]:
    serial = fresh_serial()
    placeholder_bench(tmp_path, monkeypatch)
    _stm32cubeprogrammer_host(monkeypatch)
    host_inventory(monkeypatch, supply_port_info("COM251", serial))
    command = ["adopt-hardware", "--probe-id", PROBE, "--dry-run"]
    return [[*command, "--json"], command], serial


def _init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[list[str]], str]:
    serial = fresh_serial()
    workspace = tmp_path / "firmware"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    host_inventory(monkeypatch, supply_port_info("COM251", serial))
    return [["init", "--json"], ["init"]], serial


CLI_CASES: dict[str, Callable[[Path, pytest.MonkeyPatch], tuple[list[list[str]], str]]] = {
    "doctor_by_id": _doctor_by_id,
    "adopt_hardware": _adopt_hardware,
    "init": _init,
}


@pytest.mark.parametrize("case", list(CLI_CASES))
def test_doctor_and_adopt_hardware_print_no_supply_serial(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands, serial = CLI_CASES[case](tmp_path, monkeypatch)
    for argv in commands:
        cli.entrypoint(argv)
        captured = capsys.readouterr()
        assert_masked(captured.out + captured.err, serial)
        if "--json" in argv:
            # The serial was there to print, and the document holds the marker in its place.
            assert MARKER in json.dumps(json.loads(captured.out)), argv


# --- what is kept on disk ---------------------------------------------------


def _supply_bench_with_a_uart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, values: Values) -> AgenticHILConfig:
    workspace, _ = write_bench(
        tmp_path,
        monkeypatch,
        com_ports={"psu": port(values.serial, values.resource), "uart": {"device": "COM_TEST", "identity_source": "device"}},
        power_supplies={"bench": supply()},
        auto_recover="off",
    )
    return load_authoritative_config(workspace)


def test_the_reports_of_a_run_carry_no_supply_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The last report, the last-failure report, report-state and the canonical run report."""
    values = Values.fresh()
    config = _supply_bench_with_a_uart(tmp_path, monkeypatch, values)
    handle = f"run-{uuid.uuid4().hex}"

    written = write_report(
        config,
        {
            "ok": False,
            "tool": "com_write",
            "error_type": "hardware_action_exception",
            "run": handle,
            "summary": f"The adapter beside unit {values.serial} stopped answering.",
            "backend_error": f"SER={values.serial}",
        },
    )

    assert_masked(json.dumps(written), values.serial)
    assert written["backend_error"] == f"SER={MARKER}"
    for path in (last_report_path(config), last_failure_path(config), report_state_path(config), canonical_run_report_path(config, handle)):
        text = Path(path).read_text(encoding="utf-8")
        assert_masked(text, values.serial)
        assert MARKER in text, path


@pytest.mark.parametrize("error_class", [RuntimeError, OSError], ids=["runtime_error", "os_error"])
def test_an_exception_carrying_a_supply_serial_is_masked_wherever_it_is_kept(
    error_class: type[Exception], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`backend_error`, the reports, `errors[].summary` in the lease record, lease status, and every file of the state root."""
    values = Values.fresh()
    config = _supply_bench_with_a_uart(tmp_path, monkeypatch, values)
    records: list[str] = []
    write_record_text = coordination.atomic_write_text

    def recording(path: Any, text: str, *args: Any, **kwargs: Any) -> Any:
        records.append(text)
        return write_record_text(path, text, *args, **kwargs)

    monkeypatch.setattr(coordination, "atomic_write_text", recording)
    # A fake handle on an open session, whose write raises: the write failure
    # quarantines the lease with the exception, so the record keeps its text.
    monkeypatch.setattr(serial, "Serial", lambda *args, **kwargs: FakePort().handle())
    tools = AgenticHILToolService(config, frontend="mcp")
    error = error_class(f"the adapter beside unit {values.serial} stopped answering")
    try:
        started = tools.call("com_session_start", {"port_id": "uart"})
        assert started["ok"] is True, started
        monkeypatch.setattr(tools.com_ports.sessions["uart"].serial_handle, "write", lambda data: (_ for _ in ()).throw(error))
        result = tools.call("com_write", {"port_id": "uart", "text": "x"})
        status = tools.call("hardware_lease_status", {})
    finally:
        tools.close()

    assert result["ok"] is False, result
    assert result["backend_error"] == f"the adapter beside unit {MARKER} stopped answering", result
    assert_masked(json.dumps(result), values.serial)
    assert_masked(json.dumps(status), values.serial)
    # The lease record took the error, and took it masked.
    summaries = [item.get("summary") for text in records for item in json.loads(text).get("errors", [])]
    assert f"the adapter beside unit {MARKER} stopped answering" in summaries, summaries
    for text in records:
        assert_masked(text, values.serial)

    cli.entrypoint(["lease-status", "--json"])
    assert_masked(capsys.readouterr().out, values.serial)

    for path in (last_report_path(config), last_failure_path(config), report_state_path(config)):
        assert_masked(Path(path).read_text(encoding="utf-8"), values.serial)
    swept = list(files_below(Path(config.state_root)))
    assert swept
    for path, text in swept:
        assert values.serial.lower() not in text.lower(), path


def test_a_lease_record_keeps_the_fields_records_are_matched_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace whose path holds a serial: the record keeps it, and the next owner still recognises the project."""
    values = Values.fresh()
    config = _supply_bench_with_a_uart(tmp_path / values.serial, monkeypatch, values)
    resource = "physical:uart"
    owner = HardwareCoordinator(config, "owner-a")
    try:
        lease = owner.acquire(resource)
        # The records themselves; the lock files beside them are held open while the lease stands.
        records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(Path(owner.record_directory).glob("*.json"))]
        kept = [record for record in records if "workspace" in record]
        assert kept, "no record carries the workspace"
        for record in kept:
            assert record["workspace"] == str(Path(config.work_dir).resolve()), record
            assert values.serial in record["workspace"]
        lease.release()
    finally:
        owner.close()
    successor = HardwareCoordinator(config, "owner-b")
    try:
        successor.acquire(resource).release()
    finally:
        successor.close()


def test_lease_status_masks_a_record_another_process_wrote_in_clear() -> None:
    serial = fresh_serial()
    power_supply.register_secrets([serial])
    public = coordination._public_record({"state": "cleanup_required", "errors": [{"summary": f"unit {serial} stopped"}]})
    assert public == {"state": "cleanup_required", "errors": [{"summary": f"unit {MARKER} stopped"}]}


def test_run_evidence_carries_no_registered_supply_serial(tmp_path: Path) -> None:
    serial = fresh_serial()
    power_supply.register_secrets([serial])
    report_path = tmp_path / "last-report.json"
    report_path.write_text(
        json.dumps(
            {
                "ok": False,
                "tool": "com_write",
                "error_type": "hardware_action_exception",
                "summary": f"The adapter beside unit {serial} stopped answering.",
                "backend_error": f"SER={serial}",
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "evidence"

    result = write_run_evidence(str(report_path), str(out), workspace=tmp_path, environ={})

    assert_masked(json.dumps(result), serial)
    written = list(files_below(out))
    assert written
    for path, text in written:
        assert serial.lower() not in text.lower(), path


# --- the supply tools on disk (#733) -----------------------------------------


def _recorded_lease_records(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    records: list[str] = []
    write_record_text = coordination.atomic_write_text

    def recording(path: Any, text: str, *args: Any, **kwargs: Any) -> Any:
        records.append(text)
        return write_record_text(path, text, *args, **kwargs)

    monkeypatch.setattr(coordination, "atomic_write_text", recording)
    return records


def _assert_kept_masked(config: AgenticHILConfig, records: list[str], *secrets: str) -> None:
    assert records, "no lease record was written"
    for text in records:
        assert_masked(text, *secrets)
    for path in (last_report_path(config), report_state_path(config)):
        assert_masked(Path(path).read_text(encoding="utf-8"), *secrets)
    swept = list(files_below(Path(config.state_root)))
    assert swept
    for path, text in swept:
        for secret in secrets:
            assert secret.lower() not in text.lower(), path


def test_a_supply_call_keeps_no_supply_serial_in_its_reports_or_below_the_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake's transcript holds its `*IDN?` reply with the serial in clear; the report keeps that transcript, masked."""
    bench = make_supply_bench(tmp_path, monkeypatch)
    records = _recorded_lease_records(monkeypatch)
    tools = bench.service()
    try:
        result = tools.call("power_supply_set", {"supply_id": bench.supply_id, "voltage_v": 3.3})
    finally:
        tools.close()

    assert result["ok"] is True, result
    report = json.loads(Path(last_report_path(bench.config)).read_text(encoding="utf-8"))
    assert report["tool"] == "power_supply_set", report
    assert MARKER in json.dumps(report["backend_evidence"]), report["backend_evidence"]
    _assert_kept_masked(bench.config, records, *bench.secrets())


def test_a_supply_backend_exception_carrying_a_serial_is_masked_wherever_it_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The error line is kept, in the result and the lease record, with the serial masked in both."""
    bench = make_supply_bench(tmp_path, monkeypatch)
    assert bench.port_serial is not None
    bench.unit.raises["set_voltage"] = RuntimeError(f"the supply link beside unit {bench.port_serial} dropped")
    records = _recorded_lease_records(monkeypatch)
    tools = bench.service()
    try:
        result = tools.call("power_supply_set", {"supply_id": bench.supply_id, "voltage_v": 3.3})
    finally:
        tools.close()

    assert result["ok"] is False, result
    assert result.get("cleanup_reasons"), result
    assert f"the supply link beside unit {MARKER} dropped" in json.dumps(result), result
    assert_masked(json.dumps(result), *bench.secrets())
    summaries = [item.get("summary") for text in records for item in json.loads(text).get("errors", [])]
    assert f"the supply link beside unit {MARKER} dropped" in summaries, summaries
    _assert_kept_masked(bench.config, records, *bench.secrets())
    cli.entrypoint(["lease-status", "--json"])
    assert_masked(capsys.readouterr().out, *bench.secrets())


def test_lease_status_after_a_supply_quarantine_carries_no_supply_serial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A set point without a reply quarantines the lease with the fault, and every status of it is masked."""
    bench = make_supply_bench(tmp_path, monkeypatch)
    assert bench.port_serial is not None
    bench.unit.raises["set_voltage"] = PowerSupplyFault(
        "power_supply_no_reply", f"The supply beside unit {bench.port_serial} did not answer.", wrote=True
    )
    records = _recorded_lease_records(monkeypatch)
    tools = bench.service()
    try:
        result = tools.call("power_supply_set", {"supply_id": bench.supply_id, "voltage_v": 3.3})
        status = tools.call("hardware_lease_status", {})
    finally:
        tools.close()

    assert result.get("error_type") == "power_supply_no_reply", result
    assert "power_supply_state_unconfirmed" in json.dumps(status), status
    assert_masked(json.dumps(result), *bench.secrets())
    assert_masked(json.dumps(status), *bench.secrets())
    summaries = [item.get("summary") for text in records for item in json.loads(text).get("errors", [])]
    assert f"The supply beside unit {MARKER} did not answer." in summaries, summaries
    _assert_kept_masked(bench.config, records, *bench.secrets())
    cli.entrypoint(["lease-status", "--json"])
    printed = capsys.readouterr().out
    assert_masked(printed, *bench.secrets())
    assert "power_supply_state_unconfirmed" in printed


def test_run_evidence_over_a_supply_report_carries_no_supply_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run-evidence` loads no config, so its registry is empty: the report it reads was masked when it was written."""
    bench = make_supply_bench(tmp_path, monkeypatch)
    tools = bench.service()
    try:
        result = tools.call("power_supply_set", {"supply_id": bench.supply_id, "voltage_v": 3.3})
    finally:
        tools.close()
    assert result["ok"] is True, result
    monkeypatch.setattr(power_supply, "_CONFIG_SECRETS", set())
    out = tmp_path / "evidence"

    evidence = write_run_evidence(str(last_report_path(bench.config)), str(out), workspace=bench.workspace, environ={})

    assert evidence.get("ok") is True, evidence
    written = list(files_below(out))
    assert written
    assert any("power_supply_set" in text for _, text in written), written
    for path, text in written:
        for secret in bench.secrets():
            assert secret.lower() not in text.lower(), path


def test_a_com_stdio_refusal_carries_no_supply_serial() -> None:
    serial = fresh_serial()
    power_supply.register_secrets([serial])
    output = io.StringIO()
    comstdio.write_error(output, {"ok": False, "error_type": "com_port_open_failed", "summary": f"SER={serial} is gone."})
    assert json.loads(output.getvalue())["summary"] == f"SER={MARKER} is gone."


def test_an_mcp_reply_carries_no_supply_serial_whatever_the_service_returned() -> None:
    serial = fresh_serial()
    power_supply.register_secrets([serial])

    class Service:
        config = None

        def call(self, name: str, arguments: JsonObject) -> JsonObject:
            return {"ok": True, "tool": name, "ports": [{"serial_number": serial}], "summary": f"unit {serial} listed"}

    reply = mcp.call_tool({"name": "com_ports_list", "arguments": {}}, Service())  # type: ignore[arg-type]

    assert_masked(json.dumps(reply), serial)
    assert reply["structuredContent"]["ports"] == [{"serial_number": MARKER}]


def test_the_mcp_server_start_up_refusal_carries_no_supply_serial(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    serial = fresh_serial()
    power_supply.register_secrets([serial])
    device = f"/dev/serial/by-id/usb-Vendor_Bridge_{serial}-if00"

    def refuse(workspace: Path) -> AgenticHILConfig:
        raise ConfigError("config_invalid", f"The port that carries {serial} is not reachable.", {"device": device})

    monkeypatch.setattr(stdio, "load_authoritative_config", refuse)
    assert stdio.mcp_stdio() == 2
    stderr = capsys.readouterr().err
    assert_masked(stderr, serial)
    assert json.loads(stderr)["device"] == f"/dev/serial/by-id/usb-Vendor_Bridge_{MARKER}-if00"
