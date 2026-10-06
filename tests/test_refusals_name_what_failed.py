"""Refusals that named the wrong file or handed out another failure's advice.

Each case here was answered with a refusal whose subject or whose advice was
about something other than what had failed: a damaged report state answered as
a configuration error (#689), an unattached configured serial answered with
"attach the bench and run adoption again" (#690), an unreadable project record
named by the healthy copy beside it (#692), and a broken agent MCP file answered
with the advice for the Agentic HIL configuration (#693).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_config
from support import trusted_launcher
from test_bootstrap import NUCLEO_VCP, _linux_openocd_host
from test_config_adopt import placeholder_bench
from test_config_adopt import service as adopt_service
from test_virtualized_user_paths import external_project_records

from agentic_hil import report
from agentic_hil.adopt import PROJECT_CONFIG_ADOPT, discovery_remedy
from agentic_hil.bootstrap import _discovery_failure
from agentic_hil.cli import (
    _doctor_report_state,
    _record_external_configuration,
    adopt_hardware,
    build_parser,
    doctor,
    init_config,
    register_agent_mcp,
)
from agentic_hil.config import ConfigError, load_authoritative_config, load_config
from agentic_hil.humanize import render_result
from agentic_hil.knowledge import CONFIG_DESCRIPTION_RIGHT, ERROR_CATALOGUE
from agentic_hil.report import audit_unavailable, ensure_audit_ready, read_report_state, report_state_path
from agentic_hil.tools import AgenticHILToolService

# What the Agentic HIL configuration's own advice says, which none of these
# refusals is about.
CONFIGURATION_ADVICE = ("written_by_release", "agentic-hil schema", "rejected_fields", "init --force")

REPAIR_COMMAND = "agentic-hil report-state-repair"


def _advice(result: dict) -> str:
    return " ".join([*result.get("remediation", []), *result.get("do_not", [])])


# ---------------------------------------------------------------------------
# #689: a damaged report state.


@pytest.fixture
def damaged_state(tmp_path: Path) -> tuple[AgenticHILToolService, Path]:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    state_file = Path(report_state_path(service.config))
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"version": 1, "last_report": ', encoding="utf-8")
    yield service, state_file
    service.close()


@pytest.mark.parametrize("tool", ["get_last_report", "classify_last_error"])
def test_a_damaged_report_state_is_named_as_one_by_the_read_tools(damaged_state: tuple[AgenticHILToolService, Path], tool: str) -> None:
    service, state_file = damaged_state
    result = service.call(tool)
    assert result["ok"] is False, result
    assert result["error_type"] == "report_state_damaged", result
    assert "report-state.json" in result["summary"], result
    advice = _advice(result)
    assert REPAIR_COMMAND in advice, advice
    for configuration_advice in CONFIGURATION_ADVICE:
        assert configuration_advice not in advice, (configuration_advice, advice)
    # One rule for the path on every tool answer: withheld, the way the read
    # side always withheld the state root.
    assert "path" not in result, result
    assert str(state_file) not in json.dumps(result), result


def test_a_damaged_report_state_under_the_audit_gate_follows_the_same_rule(damaged_state: tuple[AgenticHILToolService, Path]) -> None:
    service, state_file = damaged_state
    with pytest.raises(ConfigError) as raised:
        ensure_audit_ready(service.config)
    refusal = audit_unavailable("probe_target", raised.value)
    nested = refusal["audit_error"]
    assert nested["error_type"] == "report_state_damaged", nested
    assert REPAIR_COMMAND in _advice(nested), nested
    assert "path" not in nested, nested
    assert str(state_file) not in json.dumps(refusal), refusal


@pytest.mark.parametrize(
    "content",
    ['{"version": 1, "last_report": ', '{"version": 7}', '{"version": 1, "last_failure": 3}', "[]"],
    ids=["truncated", "format", "entry", "not-an-object"],
)
def test_every_damaged_report_state_answers_the_same_type(tmp_path: Path, content: str) -> None:
    config = load_config(str(write_config(tmp_path)))
    state_file = Path(report_state_path(config))
    state_file.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as raised:
        read_report_state(config)
    assert raised.value.error_type == "report_state_damaged", raised.value.to_dict()


def test_doctor_names_the_damaged_report_state_and_its_repair(damaged_state: tuple[AgenticHILToolService, Path]) -> None:
    service, state_file = damaged_state
    check = _doctor_report_state(service.config)
    assert check["ok"] is False, check
    assert check["error_type"] == "report_state_damaged", check
    # The operator's own command is where the file is named.
    assert check["path"] == str(state_file), check
    assert REPAIR_COMMAND in check["summary"], check


def test_doctor_counts_a_damaged_report_state_apart_from_the_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "firmware"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    assert init_config()["ok"] is True
    state_file = Path(report_state_path(load_authoritative_config(workspace)))
    state_file.write_text("{", encoding="utf-8")

    report = doctor()

    assert report["ok"] is False, report
    assert "report_state" in report["unhealthy"], report
    # The root accepts writes; it is not what is wrong, and the repair is not a
    # rewritten configuration.
    assert report["state_root"]["ok"] is True, report
    assert "state_root" not in report["unhealthy"], report
    assert report["report_state"]["path"] == str(state_file), report
    assert REPAIR_COMMAND in report["summary"], report
    assert "init --force" not in report["report_state"]["summary"], report


def test_the_repair_moves_the_damaged_state_aside_and_starts_a_fresh_one(damaged_state: tuple[AgenticHILToolService, Path]) -> None:
    service, state_file = damaged_state
    damaged = state_file.read_bytes()
    result = report.repair_report_state(service.config)
    assert result["ok"] is True, result
    assert result["repaired"] is True, result
    assert result["path"] == str(state_file), result
    moved = Path(result["moved_to"])
    assert moved.parent == state_file.parent, result
    assert moved != state_file
    assert "damaged" in moved.name, result
    # The record the do_not protects is kept, byte for byte.
    assert moved.read_bytes() == damaged
    assert read_report_state(service.config) == {"version": 1, "last_report": None, "last_failure": None}
    ensure_audit_ready(service.config)
    assert service.call("get_last_report")["error_type"] == "report_not_found"
    assert _doctor_report_state(service.config)["ok"] is True


def test_the_repair_leaves_a_report_state_that_reads_alone(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    ensure_audit_ready(config)
    state_file = Path(report_state_path(config))
    before = state_file.read_bytes()
    result = report.repair_report_state(config)
    assert result["ok"] is True, result
    assert result["repaired"] is False, result
    assert state_file.read_bytes() == before
    assert [entry.name for entry in state_file.parent.iterdir() if "damaged" in entry.name] == []


def test_the_repair_is_a_command_the_operator_can_type() -> None:
    args = build_parser().parse_args(["report-state-repair"])
    assert args.command == "report-state-repair"


def test_the_catalogue_entry_for_a_damaged_report_state_names_its_repair() -> None:
    entry = ERROR_CATALOGUE["report_state_damaged"]
    assert any(REPAIR_COMMAND in step for step in entry.remediation), entry
    assert "report_state_damaged" in ERROR_CATALOGUE["report_unreadable"].meaning
    assert "config_invalid" not in ERROR_CATALOGUE["report_unreadable"].meaning


# ---------------------------------------------------------------------------
# #690: the configured serial is not attached.


def _requested_serial_refusal() -> dict:
    return _discovery_failure(
        "adapter_not_found",
        "No attached probe has the serial 'OLD'.",
        requested_probe_id="OLD",
        probes=[{"probe_id": "NEW"}],
        com_ports=[],
    )


def test_the_next_step_for_an_unattached_requested_serial_points_at_probes() -> None:
    for frontend, selector, absent in (
        ("mcp", "`probe_id`", "agentic-hil adopt-hardware"),
        ("cli", "--probe-id", PROJECT_CONFIG_ADOPT),
    ):
        step = discovery_remedy(_requested_serial_refusal(), frontend=frontend)
        assert "Attach the bench" not in step, (frontend, step)
        assert "`probes`" in step, (frontend, step)
        assert "OLD" in step, (frontend, step)
        assert selector in step, (frontend, step)
        assert absent not in step, (frontend, step)


def test_adoption_over_mcp_names_the_tool_and_its_probe_id_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    _linux_openocd_host(monkeypatch, ports=[NUCLEO_VCP])
    service = adopt_service(workspace)
    try:
        result = service.call(PROJECT_CONFIG_ADOPT, {"apply": True, "probe_id": "PROBE-OTHER-0009"})
    finally:
        service.close()
    assert result["error_type"] == "adapter_not_found", result
    step = result["next_step"]
    assert "Attach the bench" not in step, step
    assert "agentic-hil adopt-hardware" not in step, step
    assert PROJECT_CONFIG_ADOPT in step and "`probe_id`" in step and "`probes`" in step, step


def test_adoption_on_the_command_line_names_the_probe_id_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    _linux_openocd_host(monkeypatch, ports=[NUCLEO_VCP])
    result = adopt_hardware(probe_id="PROBE-OTHER-0009")
    assert result["error_type"] == "adapter_not_found", result
    step = result["next_step"]
    assert "Attach the bench" not in step, step
    assert "--probe-id" in step and "`probes`" in step, step


# ---------------------------------------------------------------------------
# #692: the record that does not read is the one named.


@pytest.mark.parametrize(
    ("content", "reason"),
    [("{ not json", "not_json"), ('{"configurations": ["relative/config.yaml"]}', "wrong_shape")],
)
def test_the_unreadable_project_record_is_the_one_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str, reason: str) -> None:
    monkeypatch.chdir(tmp_path)
    readable, damaged = external_project_records()
    readable.parent.mkdir(parents=True, exist_ok=True)
    readable.write_text(json.dumps({"configurations": []}) + "\n", encoding="utf-8")
    damaged.parent.mkdir(parents=True, exist_ok=True)
    damaged.write_text(content, encoding="utf-8")

    refusal = _record_external_configuration(Path.home() / "benches" / "alpha" / "config.yaml")

    assert refusal is not None
    assert refusal["error_type"] == "agent_project_record_unreadable", refusal
    assert refusal["path"] == str(damaged), refusal
    assert str(damaged) in refusal["summary"], refusal
    assert str(readable) not in refusal["summary"], refusal
    assert refusal["reason"] == reason, refusal
    # The record this user's commands write to is still reported, apart.
    assert refusal["write_path"] == str(readable), refusal
    assert damaged.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# #693: an agent's own MCP file that does not parse.


@pytest.mark.parametrize(
    ("agent", "relative", "content"),
    [
        ("claude-code", ".claude.json", "{ broken"),
        ("claude-code", ".claude.json", '{"mcpServers": []}'),
        ("opencode", ".config/opencode/opencode.json", "{ broken"),
        ("opencode", ".config/opencode/opencode.json", '{"mcp": 3}'),
        ("codex", ".codex/config.toml", "[mcp_servers\n"),
        ("codex", ".codex/config.toml", "# >>> agentic-hil mcp (managed) >>>\n"),
    ],
    ids=["claude-syntax", "claude-servers", "opencode-syntax", "opencode-mcp", "codex-syntax", "codex-markers"],
)
def test_a_broken_agent_mcp_file_is_refused_with_advice_about_that_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str, relative: str, content: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    # The refusal comes from the agent's file, not from where the launcher is
    # installed: a runner without a persistent install must reach it too.
    monkeypatch.setattr("agentic_hil.cli.mcp_server_command", lambda: str(trusted_launcher()))
    target = Path.home() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    result = register_agent_mcp(agent)

    assert result["ok"] is False, result
    assert result["error_type"] == "agent_mcp_config_invalid", result
    assert result["path"] == str(target), result
    advice = _advice(result)
    assert "`path`" in advice and "same command again" in advice, advice
    for configuration_advice in (*CONFIGURATION_ADVICE, "agentic-hil doctor"):
        assert configuration_advice not in advice, (configuration_advice, advice)
    rendered = render_result(result)
    for configuration_advice in (*CONFIGURATION_ADVICE, "agentic-hil doctor"):
        assert configuration_advice not in rendered, (configuration_advice, rendered)
    assert target.read_text(encoding="utf-8") == content
