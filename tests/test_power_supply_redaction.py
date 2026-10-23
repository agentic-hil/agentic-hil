"""No load-time refusal of a power supply repeats a serial, an idn_serial or a port's resource id (issue #731).

The first twenty-three cases are the refusals of `test_power_supply_config.py`, reached
again from the outside: `str(error)`, `to_dict()`, the stderr of `stdio.mcp_stdio`
and the stderr of `cli.emit_protocol_error`. The last four are schema refusals at
the fields that carry a secret. Every value is synthetic and built from uuid4.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_power_supply_config import REFUSALS, Refusal, Values, document, port, supply, write_bench

from agentic_hil import cli, stdio
from agentic_hil.config import ConfigError, load_authoritative_config

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
