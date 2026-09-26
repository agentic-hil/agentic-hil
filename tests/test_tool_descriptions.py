"""What a tool description carries, and where the rest of it is read.

A host lists every tool before the first call, and one without deferred tool
loading sends the whole list with every request of a session, so a description
is paid for on every turn whether or not the tool is ever called. What it has to
carry is what a caller needs before the call: what the tool does, which
command or file edit it stands in for, and every rule the caller must follow
before or while calling it. What a result explains at the moment it applies
(why a call was refused, what a state it names means, what to do after a given
answer) is read in that result, or in the catalogue entry that result carries.

So this file holds three things. The size of the whole list and of each entry.
The rules that stay in a description whatever their length. And, for guidance a
description no longer carries, the result that carries it instead, reached
through the tool that returns it rather than read out of a table, because a
result that only exists in a table is not one a caller ever meets.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from test_agent_provisioning import attached_hardware, written_document
from test_agent_provisioning import bench as provisioning_bench
from test_can_listen_only import PCAN_PARAMETER_OFF, can_config, fake_can_module, fake_pcan_bus, install_pcan_basic
from test_config_reload import add_second_board, rewrite
from test_config_reload import bench as reload_bench
from test_config_reload import service as reload_service
from test_config_write import bench as write_bench
from test_config_write import changes
from test_config_write import service as write_service
from test_reactor_mcp_tools import RESET_PLAN, bound_service, call
from test_recover_tool import config_changed_incident, config_for, open_incident, quarantine
from test_run_lifecycle import bench_workspace

from agentic_hil.configreload import PROJECT_CONFIG_RELOAD
from agentic_hil.configwrite import PROJECT_CONFIG_SET
from agentic_hil.contracts import MCP_TOOL_NAMES, MCP_TOOLS
from agentic_hil.knowledge import (
    CONFIG_DESCRIPTION_RIGHT,
    CONFIG_PERMISSIONS_RIGHT,
    LISTEN_ONLY_UNCONFIRMED_ERROR,
    RECOVERY_PHYSICAL_CHECK_ERROR,
    TEST_PLAN_URI,
    recovery_operator_command,
)
from agentic_hil.tools import PROJECT_CONFIG_CREATE, AgenticHILToolService, UnprovisionedToolService

DESCRIPTIONS_TOTAL_LIMIT = 5000
DESCRIPTION_LIMIT = 400
PROPERTY_DESCRIPTION_LIMIT = 200

# The fields a result speaks to its caller in. A catalogue entry arrives in the
# last two, merged into the refusal it is about.
PROSE_FIELDS = ("summary", "next_step", "next_steps", "remediation", "do_not")

PLAN_NAMING_A_DEVICE_THIS_BENCH_LACKS = "version: 4\nsteps:\n  - {device: dut, action: reset}\n  - {device: bench_b, action: reset}\n"


def descriptions() -> dict[str, str]:
    return {str(tool["name"]): str(tool["description"]) for tool in MCP_TOOLS}


def description_of(name: str) -> str:
    return descriptions()[name]


def property_descriptions(node: object, where: str = "") -> Iterator[tuple[str, str]]:
    """Every `description` a property of an input schema carries, at any depth."""
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                if isinstance(child, dict) and isinstance(child.get("description"), str):
                    yield f"{where}/{name}", child["description"]
        for key, child in node.items():
            yield from property_descriptions(child, f"{where}/{key}" if key != "properties" else where)
    elif isinstance(node, list):
        for child in node:
            yield from property_descriptions(child, where)


def sentences(text: str) -> list[str]:
    return [sentence for sentence in re.split(r"(?<=[.;:])\s+", text) if sentence]


def prose_of(result: dict) -> str:
    """Everything a result says to its caller, as one string."""
    parts: list[str] = []
    for field in PROSE_FIELDS:
        value = result.get(field)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list | tuple):
            parts.extend(str(item) for item in value)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# The size of the list.


def test_all_descriptions_together_stay_within_five_thousand_characters() -> None:
    """The whole list, the three entries whose wording changes elsewhere included."""
    described = descriptions()
    assert sorted(described) == sorted(MCP_TOOL_NAMES)
    assert {"com_read", "can_read", "flash_firmware"} <= set(described)

    total = sum(len(text) for text in described.values())
    longest = sorted(((len(text), name) for name, text in described.items()), reverse=True)[:8]
    assert total <= DESCRIPTIONS_TOTAL_LIMIT, f"{total} characters, longest {longest}"


def test_no_description_is_over_four_hundred_characters() -> None:
    over = {name: len(text) for name, text in descriptions().items() if len(text) > DESCRIPTION_LIMIT}

    assert over == {}, over


def test_no_property_description_is_over_two_hundred_characters() -> None:
    found = {f"{tool['name']}{where}": text for tool in MCP_TOOLS for where, text in property_descriptions(tool["inputSchema"])}
    # The walk reaches nested properties too, so an empty answer is a broken walk
    # rather than a clean schema.
    assert "reset_target/mode" in found and "project_config_set/changes/items/value" in found, sorted(found)

    over = {where: len(text) for where, text in found.items() if len(text) > PROPERTY_DESCRIPTION_LIMIT}
    assert over == {}, over


# ---------------------------------------------------------------------------
# The rules a caller follows before or while calling, kept whatever they cost.


def test_hardware_recover_still_takes_only_a_statement_the_operator_gave() -> None:
    text = description_of("hardware_recover")
    lowered = text.lower()

    assert "operator_statement" in text
    assert "ask the operator" in lowered
    assert "verbatim" in lowered
    assert re.search(r"\bnever\b[^.]*\b(did not give|were not given|invent)", lowered), text
    # And the other half of the boundary: a reason that names no contact needs none.
    assert "no argument" in lowered or "no hardware contact" in lowered


def test_project_config_create_still_never_asks_for_the_two_interlocks_to_be_turned_on() -> None:
    text = description_of("project_config_create")

    assert "allow_raw_debugger_commands" in text and "allow_mass_erase" in text
    assert re.search(r"\b(never|do not) ask\b[^.]*\bturned on\b", text.lower()), text
    assert "the operator's call" in text


def test_project_config_set_says_a_permission_can_only_be_narrowed() -> None:
    text = description_of("project_config_set")

    assert re.search(r"\bonly\b[^.]*\bnarrow", text.lower()), text
    # Which grant opens which half is read before the call, so both stay named.
    assert CONFIG_DESCRIPTION_RIGHT in text and CONFIG_PERMISSIONS_RIGHT in text


def test_bench_run_start_says_what_needs_no_declared_run() -> None:
    """A single call holds its own device, and a flash that captures the UART
    output is a single call; a caller told only to declare a run before a
    sequence would declare one around both."""
    text = description_of("bench_run_start")
    about_capture = [sentence for sentence in sentences(text) if "capture" in sentence]

    assert about_capture, text
    sentence = about_capture[0]
    assert "flash_firmware" in sentence, sentence
    assert "single call" in sentence.lower(), sentence
    assert re.search(r"\bneeds? no\b[^.]*\brun\b", sentence), sentence
    assert "bench_run_stop" in text


def test_test_reactor_run_says_where_a_plan_is_read_up_before_it_is_written() -> None:
    assert TEST_PLAN_URI in description_of("test_reactor_run")


def test_accept_config_change_still_waits_for_the_operator() -> None:
    schema = next(tool["inputSchema"] for tool in MCP_TOOLS if tool["name"] == "hardware_recover")
    lowered = schema["properties"]["accept_config_change"]["description"].lower()

    assert "only after" in lowered
    assert "operator" in lowered
    assert "digest" in lowered


# ---------------------------------------------------------------------------
# What each shortened description still says it is for.

ROUTES: dict[str, tuple[str, ...]] = {
    "hardware_recover": (r"state files",),
    "project_config_create": (r"by hand",),
    "test_reactor_run": (r"`?agentic-hil test-reactor`?", r"workspace_root", r"detach", r"test_reactor_status", r"test_reactor_stop"),
    "project_config_reload_description": (r"restart",),
    "server_upgrade": (r"\buv\b", r"\bpipx\b", r"\bpip\b", r"no arguments"),
    "project_config_set": (r"editing the configuration file",),
    "project_config_adopt_hardware": (r"placeholder", r"retype", r"\bapply\b"),
    "project_config_describe": (r"project_config_set", r"guessing"),
    "bench_run_start": (r"bench_run_stop",),
    "debug_symbol_value": (r"\bgdb\b",),
    "can_buses_list": (r"ip link", r"candump"),
}


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_a_shortened_description_still_says_what_it_stands_in_for(name: str) -> None:
    text = description_of(name)

    missing = [pattern for pattern in ROUTES[name] if not re.search(pattern, text)]
    assert missing == [], text


# ---------------------------------------------------------------------------
# Where the guidance a description no longer carries is read.


def test_a_config_changed_refusal_says_what_to_show_the_operator_and_both_ways_on(tmp_path: Path) -> None:
    """The one recovery answer whose way forward lived only in the description.

    The refusal carries the two digests and the override's name, and nothing that
    says what to do with them: show the operator both digests, and pass the
    override only once they have confirmed the change, or relay their own line."""
    config, _, _ = config_changed_incident(tmp_path)
    service = AgenticHILToolService(config)
    try:
        result = service.call("hardware_recover", {})
    finally:
        service.close()

    assert result["error_type"] == "config_changed", result
    prose = prose_of(result)
    lowered = prose.lower()
    assert "digest" in lowered, prose
    assert "operator" in lowered, prose
    assert re.search(r"\b(confirm|review|understood)", lowered), prose
    assert "accept_config_change" in prose, prose
    assert "--accept-config-change" in prose, prose


def test_the_operator_adds_the_override_to_their_own_line_only_after_reviewing_both_digests(tmp_path: Path) -> None:
    """The flag is the operator's acceptance of the change, so it is theirs to add
    once they have looked, and the line the refusal hands over leaves it out."""
    config, incident, _ = config_changed_incident(tmp_path)
    service = AgenticHILToolService(config)
    try:
        refused = service.call("hardware_recover", {})
    finally:
        service.close()

    assert refused["error_type"] == "config_changed", refused
    steps = [step for step in refused.get("remediation", []) if "--accept-config-change" in step]
    assert steps, refused
    assert all("only after" in step and "both digests" in step for step in steps), steps
    assert refused["operator_command"] == recovery_operator_command(incident)


def test_a_bench_with_nothing_standing_is_told_what_a_quarantine_is(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    open_incident(config, "debugger_result_unconfirmed")
    service = AgenticHILToolService(config)
    try:
        result = service.call("hardware_recover", {})
    finally:
        service.close()

    assert result["nothing_to_recover"] is True, result
    assert "audit halt" in result["summary"]
    assert "next contact" in result["summary"]


def test_a_refusal_that_needs_a_person_says_how_their_statement_is_recorded(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    incident = quarantine(config, "safe_state_unconfirmed")
    service = AgenticHILToolService(config)
    try:
        result = service.call("hardware_recover", {})
    finally:
        service.close()

    assert result["error_type"] == RECOVERY_PHYSICAL_CHECK_ERROR, result
    next_step = result["next_step"]
    assert "verbatim" in next_step and "ledger" in next_step and "relayed by you" in next_step, next_step
    assert "false ledger record" in result["do_not"][0] and "actor" in result["do_not"][0], result["do_not"][0]
    remediation = " ".join(result["remediation"])
    assert "nobody to ask" in remediation and "operator_command" in remediation, remediation
    assert result["operator_command"] == recovery_operator_command(incident)
    # Deleting the server's state is named as no way out, in the refusal itself.
    assert any("state_root" in step for step in result["do_not"]), result["do_not"]


def test_a_first_generation_says_what_it_granted_and_what_to_ask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = provisioning_bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert created["created"] is True, created
    summary = created["summary"]
    assert "allow_raw_debugger_commands" in summary and "allow_mass_erase" in summary and "flash" in summary, summary
    steps = created["next_steps"]
    for fragment in (
        "Report where this configuration is",
        "Ask the operator which",
        "refuses `flash_firmware`",
        "Neither has a tool behind it",
        "Do not ask the operator to turn them on",
        "work as written",
    ):
        assert any(fragment in step for step in steps), (fragment, steps)


def test_a_regeneration_says_where_each_permission_came_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Carried over from what this server loaded, and the generated defaults for
    an entry found for the first time: said by the regeneration that did it."""
    workspace = provisioning_bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()
    narrowed = written_document(created)
    narrowed["debuggers"]["dut"]["permissions"]["allow_reset"] = False
    Path(created["path"]).write_text(yaml.safe_dump(narrowed, sort_keys=False), encoding="utf-8")

    attached_hardware(monkeypatch)
    reopened = UnprovisionedToolService(workspace)
    try:
        regenerated = reopened.call(PROJECT_CONFIG_CREATE)
    finally:
        reopened.close()

    assert regenerated["ok"] is True, regenerated
    summary = regenerated["summary"]
    assert "loaded at startup" in summary, summary
    assert "discovered for the first time" in summary and "generated defaults" in summary, summary


def test_a_reload_says_what_it_did_not_re_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = reload_bench(tmp_path, monkeypatch, probe_id="DUT-0001")
    tools = reload_service(workspace)
    try:
        rewrite(path, add_second_board)
        result = tools.call(PROJECT_CONFIG_RELOAD)
    finally:
        tools.close()

    assert result["ok"] is True, result
    assert result["permissions_reloaded"] is False
    assert "permissions in force are unchanged" in result["summary"], result["summary"]
    assert "carries no grant" in result["next_steps"][0] and "restarted" in result["next_steps"][0], result["next_steps"]
    assert any("either direction" in step for step in result["next_steps"]), result["next_steps"]
    assert result["not_reloaded_sections"] and all(entry["why"] for entry in result["not_reloaded_sections"])


def test_a_configuration_write_says_the_file_was_validated_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = write_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = write_service(workspace)
    try:
        written = tools.call(PROJECT_CONFIG_SET, changes(("com_ports.dut_uart.baudrate", 9600)))
    finally:
        tools.close()

    assert written["ok"] is True, written
    assert "validated before it replaced" in written["summary"], written["summary"]


def test_a_plan_run_without_a_path_names_the_default_plan_it_ran(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)

    result = call(workspace, "test_reactor_run", {})

    assert result["ok"] is True, result
    assert (workspace / result["test_config_path"]).resolve() == plan.resolve(), result["test_config_path"]


def test_a_plan_naming_a_device_this_bench_lacks_is_refused_before_any_hardware_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Validated in full: the first step is one this bench could run, and it does not."""
    workspace, plan = bench_workspace(tmp_path, monkeypatch, PLAN_NAMING_A_DEVICE_THIS_BENCH_LACKS)

    refused = call(workspace, "test_reactor_run", {"test_config_path": str(plan)})

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "test_config_invalid", refused
    # Refused on the second step, and the first, which this bench could run, did not.
    assert refused["validation_error"]["step"] == 2, refused["validation_error"]
    assert refused["steps"] == [], refused["steps"]
    assert "before the first hardware action" in " ".join(refused["remediation"]), refused["remediation"]


def test_a_plan_asked_for_inside_an_open_run_is_told_the_plan_holds_its_own_devices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    service = bound_service(workspace)
    try:
        opened = service.call("bench_run_start", {"devices": [{"kind": "debugger", "id": "dut"}], "label": "agent-run"})
        assert opened["ok"] is True, opened
        refused = service.call("test_reactor_run", {})
        assert service.call("bench_run_stop", {})["ok"] is True
    finally:
        service.close()

    assert refused["error_type"] == "run_already_active", refused
    assert "holds every device it names" in refused["next_step"], refused["next_step"]


def test_a_stop_for_a_run_that_already_ended_asks_nothing_of_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    finished = call(workspace, "test_reactor_run", {"test_config_path": str(plan)})
    assert finished["ok"] is True, finished

    answered = call(workspace, "test_reactor_stop", {"run": finished["run"]})

    assert answered["ok"] is True, answered
    assert answered["stop_requested"] is False
    assert "already ended" in answered["summary"], answered["summary"]


def test_a_listen_only_bus_the_adapter_cannot_be_held_to_is_refused_rather_than_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Said by the refusal: the adapter did not confirm the mode, so it was closed
    again and no session stands, rather than one listening anyway."""
    config = can_config(tmp_path, "peak", "PCAN_USBBUS1", listen_only=True)
    bus = fake_pcan_bus(reported=PCAN_PARAMETER_OFF)
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: bus))
    install_pcan_basic(monkeypatch)
    service = AgenticHILToolService(config)
    try:
        refused = service.call("can_session_start", {"bus_id": "bench", "clear_rx_queue": False})
        read = service.call("can_read", {"bus_id": "bench"})
    finally:
        service.close()

    assert refused["error_type"] == LISTEN_ONLY_UNCONFIRMED_ERROR, refused
    assert "closed rather than used" in refused["summary"], refused["summary"]
    assert any("listen_only" in step for step in refused["do_not"]), refused["do_not"]
    assert read["error_type"] == "session_not_active", read
    assert bus.closed is True
