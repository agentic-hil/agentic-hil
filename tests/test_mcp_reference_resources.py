"""The reference a caller reads instead of this server's source code.

Measured over two real bring-up sessions: 17 reads of the Agentic HIL
implementation, 10 of them into the *installed* package under site-packages,
to recover facts nobody published — which fields a debugger backend needs,
where the programmer executable is configured, which `target_type` a board
takes. A caller installed with `uv tool install` has no source tree at all, so
these two channels are the only ones that can answer: the remediation a failing
result carries, and the MCP resources. Both read one module, and the last test
here is the one that keeps them from drifting apart.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FAKE_OPENOCD_NO_TARGET, FAKE_STLINK_NO_PROBE, FAKE_STLINK_NO_TARGET, write_config

from agentic_hil.config import ConfigError, load_config
from agentic_hil.knowledge import (
    BACKENDS,
    CONFIG_SCHEMA_URI,
    DEBUGGER_BACKEND_URI_PREFIX,
    DEBUGGER_BACKENDS_URI,
    ERROR_CATALOGUE,
    ERROR_URI_PREFIX,
    ERRORS_URI,
    JSON_MIME,
    LEASE_LIFECYCLE_URI,
    MCP_RESOURCE_TEMPLATES,
    MCP_RESOURCES,
    PLATFORM_PATHS_URI,
    RESOURCE_SCHEME,
    TARGET_SUPPORT_URI,
    catalogue_entry,
    remediation_fields,
    safe_state_root_suggestion,
    safe_user_root,
)
from agentic_hil.mcp import MCP_RESOURCE_NOT_FOUND, handle_mcp_message
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import JsonObject
from agentic_hil.windows_principals import JOIN_STATE_DOMAIN, JOIN_STATE_WORKGROUP


@pytest.fixture
def service(tmp_path: Path) -> Iterator[AgenticHILToolService]:
    tools = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        yield tools
    finally:
        tools.close()


def mcp(tools: AgenticHILToolService, method: str, params: dict | None = None) -> dict:
    response = handle_mcp_message({"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}}, tools)
    assert isinstance(response, dict)
    return response


def read_text(tools: AgenticHILToolService, uri: str) -> str:
    response = mcp(tools, "resources/read", {"uri": uri})
    assert "error" not in response, response
    contents = response["result"]["contents"]
    assert len(contents) == 1, contents
    assert contents[0]["uri"] == uri
    return contents[0]["text"]


def test_every_advertised_resource_is_declared_completely(service: AgenticHILToolService) -> None:
    resources = mcp(service, "resources/list")["result"]["resources"]

    assert resources, "an empty resources/list is what this replaced"
    assert len({entry["uri"] for entry in resources}) == len(resources)
    assert len({entry["name"] for entry in resources}) == len(resources)
    for entry in resources:
        assert entry["uri"].startswith(f"{RESOURCE_SCHEME}://"), entry
        assert entry["mimeType"] in {JSON_MIME, "text/markdown"}, entry
        for field in ("name", "title", "description"):
            assert entry[field].strip(), entry
    # A description is what a host shows before anything is fetched, so it has to
    # say which question the resource answers, not repeat its own title.
    assert {entry["uri"] for entry in resources} == {entry["uri"] for entry in MCP_RESOURCES}


def test_every_advertised_resource_delivers_the_content_it_declared(service: AgenticHILToolService) -> None:
    for entry in mcp(service, "resources/list")["result"]["resources"]:
        response = mcp(service, "resources/read", {"uri": entry["uri"]})
        contents = response["result"]["contents"]

        assert len(contents) == 1, entry["uri"]
        assert contents[0]["uri"] == entry["uri"]
        assert contents[0]["mimeType"] == entry["mimeType"], entry["uri"]
        assert contents[0]["text"].strip(), entry["uri"]
        if entry["mimeType"] == JSON_MIME:
            assert isinstance(json.loads(contents[0]["text"]), dict), entry["uri"]


def test_the_per_error_and_per_backend_entries_are_templates_not_dozens_of_resources(
    service: AgenticHILToolService,
) -> None:
    templates = mcp(service, "resources/templates/list")["result"]["resourceTemplates"]
    listed = {entry["uri"] for entry in mcp(service, "resources/list")["result"]["resources"]}

    assert templates
    for template in templates:
        assert "{" in template["uriTemplate"] and "}" in template["uriTemplate"], template
        assert template["uriTemplate"].startswith(f"{RESOURCE_SCHEME}://"), template
        for field in ("name", "title", "description", "mimeType"):
            assert template[field].strip(), template
    assert {template["uriTemplate"] for template in templates} == {
        entry["uriTemplate"] for entry in MCP_RESOURCE_TEMPLATES
    }
    # The catalogue is keyed by error_type plus an optional scope. Enumerating
    # every key as its own resource would bury the one entry a caller's own
    # result just named.
    assert not any(uri.startswith(ERROR_URI_PREFIX) for uri in listed)
    assert not any(uri.startswith(DEBUGGER_BACKEND_URI_PREFIX) for uri in listed)


def test_every_key_a_template_can_produce_is_readable(service: AgenticHILToolService) -> None:
    for key in ERROR_CATALOGUE:
        entry = json.loads(read_text(service, ERROR_URI_PREFIX + key))

        error_type, _, scope = key.partition(":")
        assert entry["error_type"] == error_type
        assert entry.get("scope", "") == scope
        assert entry["meaning"].strip()
        assert entry["remediation"]
    for backend in BACKENDS:
        matrix = json.loads(read_text(service, DEBUGGER_BACKEND_URI_PREFIX + backend))

        assert matrix["backend"] == backend
        assert matrix["fields"]["executable"]["status"]


def test_a_uri_this_server_does_not_serve_is_refused_as_a_missing_resource(service: AgenticHILToolService) -> None:
    unknown = mcp(service, "resources/read", {"uri": f"{RESOURCE_SCHEME}://reference/does-not-exist"})
    unwritten_scope = mcp(service, "resources/read", {"uri": ERROR_URI_PREFIX + "timeout:openocd"})

    # -32002, not a method error: a client has to be able to tell "no such
    # resource" from "this server cannot serve resources at all".
    assert unknown["error"]["code"] == MCP_RESOURCE_NOT_FOUND
    assert unknown["error"]["data"]["uri"].endswith("does-not-exist")
    assert unwritten_scope["error"]["code"] == MCP_RESOURCE_NOT_FOUND


@pytest.mark.parametrize("params", [{}, {"uri": 42}, {"uri": ""}])
def test_a_read_without_a_usable_uri_is_an_invalid_params_error(
    service: AgenticHILToolService, params: dict
) -> None:
    assert mcp(service, "resources/read", params)["error"]["code"] == -32602


def test_the_questions_that_sent_agents_into_the_installed_package_are_answered(
    service: AgenticHILToolService,
) -> None:
    """The three searches that repeated across both sessions, verbatim.

    `target_type|flash_address|probe_id|interface|executable`,
    `programmer|executable|command|cli_path|tool_path`, and `stm32f446`.
    """
    backends = json.loads(read_text(service, DEBUGGER_BACKENDS_URI))
    targets = read_text(service, TARGET_SUPPORT_URI)

    # 1. Which backends exist and which field does each of them need.
    assert set(backends["backends"]) == set(BACKENDS)
    for name, matrix in backends["backends"].items():
        for field in ("executable", "probe_id", "target_type", "interface", "interface_cfg", "target_cfg", "flash_address"):
            assert matrix[field]["status"] in set(backends["status_legend"]), (name, field)
    assert backends["backends"]["pyocd"]["target_type"]["status"] == "required"
    assert backends["backends"]["stlink"]["interface"]["status"] == "required"
    assert backends["backends"]["openocd"]["target_cfg"]["status"] == "required"

    # 2. Where the programmer's executable is configured.
    assert backends["config_path"] == "debuggers.<name>.<field>"
    assert backends["backends"]["stlink"]["tool"].startswith("STM32_Programmer_CLI")

    # 3. Which target_type this board takes, and where that value comes from.
    #    Three escalating searches through pyOCD and cmsis_pack_manager, then two
    #    hand-run downloads from keil.com, ended at this string.
    assert "stm32f446retx" in targets
    assert "pyocd pack install" in targets


def test_a_rejected_path_names_a_permitted_location_and_refuses_the_acl_shortcut() -> None:
    """#64: the refusal named the field and the path and no way forward.

    Relaxing the ACL is the obvious next move and the wrong one, so the refusal
    has to rule it out where the reader is already looking.
    """
    refusal = ConfigError(
        "unsafe_configured_path",
        "User configuration directory has a replaceable Windows ancestor directory.",
        {"field": "user_config", "path": str(Path.home() / "AppData" / "Roaming")},
    ).to_dict()

    assert any(safe_user_root() in step for step in refusal["remediation"])
    assert any("AGENTIC_HIL_CONFIG" in step for step in refusal["remediation"])
    assert refusal["do_not"]
    assert any("ACL" in step for step in refusal["do_not"])


def test_a_rejected_state_root_proposes_a_state_root_that_passes() -> None:
    refusal = ConfigError(
        "unsafe_configured_path",
        "state_root has a replaceable Windows ancestor directory.",
        {"field": "state_root", "path": str(Path.home() / "AppData" / "Local")},
    ).to_dict()

    assert any(safe_state_root_suggestion() in step for step in refusal["remediation"])
    # init and setup derive the default from %LOCALAPPDATA%, which is the
    # rejected location itself, so "set state_root" alone is not a way out.
    assert any("agentic-hil init" in step for step in refusal["remediation"])
    assert any("ACL" in step for step in refusal["do_not"])


@pytest.mark.parametrize("field", ["state_root", "user_config", None])
def test_a_rejected_path_does_not_send_a_domain_user_back_to_a_location_that_refuses(field: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The advice was written from one machine's measurement and read as a rule.

    "%USERPROFILE%, %APPDATA% and %LOCALAPPDATA% all pass from 0.8.0 on" holds in
    a workgroup, and holds because the ACEs on those folders are SIDs the local
    authority answers ERROR_NONE_MAPPED about with nothing else asked. On a domain
    member or an Entra-joined machine the identical ACEs are `unresolved_foreign`
    and refuse under `standard` — so a remedy string attached to the refusal was
    telling exactly the operator who had just been refused to move there, or to
    retry there, and the second failure reads as the tool being broken.
    """
    monkeypatch.setattr("agentic_hil.knowledge.machine_join_state", lambda: JOIN_STATE_DOMAIN)
    monkeypatch.setattr("agentic_hil.knowledge.os.name", "nt")
    details: JsonObject = {"path": str(Path.home() / "AppData" / "Local")}
    if field is not None:
        details["field"] = field

    refusal = ConfigError("unsafe_configured_path", "state_root has a replaceable Windows ancestor directory.", details).to_dict()
    said = " ".join([*refusal["remediation"], refusal.get("meaning", "")])

    assert "joined to a domain" in said
    assert "%LOCALAPPDATA%" not in said or "do not apply" in said
    assert "unresolved_foreign" in said
    assert safe_user_root() in said

    monkeypatch.setattr("agentic_hil.knowledge.machine_join_state", lambda: JOIN_STATE_WORKGROUP)
    workgroup = " ".join(ConfigError("unsafe_configured_path", "state_root has a replaceable Windows ancestor directory.", details).to_dict()["remediation"])
    assert "in a workgroup" in workgroup, "and the machine the verdicts were measured on still gets them"


def test_an_error_nobody_wrote_a_fix_for_grows_no_invented_advice() -> None:
    refusal = ConfigError("config_invalid", "state_root and workspace_root must not overlap.", {"field": "state_root"}).to_dict()

    assert "remediation" not in refusal
    assert "do_not" not in refusal


def test_a_target_that_does_not_answer_carries_that_backends_next_checks(tmp_path: Path) -> None:
    tools = AgenticHILToolService(load_config(str(write_config(tmp_path, debugger_executable=FAKE_OPENOCD_NO_TARGET))))
    try:
        result = tools.call("probe_target")
    finally:
        tools.close()

    assert result["ok"] is False
    assert result["error_type"] == "target_not_detected"
    assert result["remediation"] == remediation_fields("target_not_detected", "openocd")["remediation"]
    # Backend-specific, because the field that selects the target differs: an
    # OpenOCD target comes from target_cfg, a pyOCD one from target_type.
    assert any("target_cfg" in step for step in result["remediation"])
    assert not any("target_type" in step for step in result["remediation"])


def test_an_stlink_target_that_does_not_answer_carries_that_backends_next_checks(tmp_path: Path) -> None:
    """The same property as the openocd case, for the backend most likely to hit it.

    stlink is the STM32CubeProgrammer path, so it is what a Windows caller with
    neither OpenOCD nor pyOCD on PATH reaches first. The remediation is wired per
    backend in each `_failure_result`, which is why covering two of three leaves
    the third with nothing under it.
    """
    tools = AgenticHILToolService(
        load_config(str(write_config(tmp_path, debugger_type="stlink", debugger_executable=FAKE_STLINK_NO_TARGET)))
    )
    try:
        result = tools.call("probe_target")
    finally:
        tools.close()

    assert result["ok"] is False
    assert result["error_type"] == "target_not_detected"
    # Classified from STM32CubeProgrammer's own output, not the unconfirmed-exit
    # fallback, which reaches the same public error_type as `probe_unconfirmed`
    # down a different branch.
    assert result["backend_error_type"] == "target_not_detected"
    assert result["remediation"] == remediation_fields("target_not_detected", "stlink")["remediation"]
    # Backend-specific for the same reason as openocd's: an ST-Link transport is
    # selected with `interface`, passed as `port=`, never with interface_cfg.
    assert any("debuggers.<name>.interface`" in step for step in result["remediation"])
    assert not any("interface_cfg" in step for step in result["remediation"])


def test_an_stlink_that_enumerates_no_probe_carries_that_backends_next_checks(tmp_path: Path) -> None:
    tools = AgenticHILToolService(
        load_config(str(write_config(tmp_path, debugger_type="stlink", debugger_executable=FAKE_STLINK_NO_PROBE)))
    )
    try:
        result = tools.call("probe_target")
    finally:
        tools.close()

    assert result["ok"] is False
    assert result["error_type"] == "adapter_not_found"
    assert result["backend_error_type"] == "probe_not_found"
    assert result["remediation"] == remediation_fields("adapter_not_found", "stlink")["remediation"]
    # Each backend passes the probe selector under its own name: `sn=` here,
    # `adapter serial` for openocd, `--uid` for pyocd.
    assert any("`sn=`" in step for step in result["remediation"])


def test_an_ambiguous_pyocd_probe_selector_carries_the_substring_rule(tmp_path: Path) -> None:
    # "PYOCD" is a substring of both probes the fixture enumerates, and pyOCD
    # matches --uid as a substring, so it would silently take the first one.
    tools = AgenticHILToolService(load_config(str(write_config(tmp_path, debugger_type="pyocd", probe_id="PYOCD"))))
    try:
        result = tools.call("probe_target")
    finally:
        tools.close()

    assert result["ok"] is False
    assert result["error_type"] == "adapter_not_found"
    assert result["remediation"] == remediation_fields("adapter_not_found", "pyocd")["remediation"]
    assert any("substring" in step for step in result["remediation"])


def test_the_remediation_in_a_result_is_the_remediation_the_reference_serves(
    service: AgenticHILToolService,
) -> None:
    """The property the whole design exists for.

    A reference that says something other than the failing result is worse than
    no reference: it is wrong in a place a caller trusts. Both sides read one
    catalogue, and this fails the moment somebody gives either its own copy.
    """
    catalogue = json.loads(read_text(service, ERRORS_URI))
    by_key = {(entry["error_type"], entry.get("scope")): entry for entry in catalogue["entries"]}

    assert len(by_key) == len(ERROR_CATALOGUE)
    for key in ERROR_CATALOGUE:
        error_type, _, scope = key.partition(":")
        served = by_key[(error_type, scope or None)]
        inline = remediation_fields(error_type, scope or None)

        assert served["remediation"] == inline["remediation"], key
        assert served.get("do_not") == inline.get("do_not"), key
        assert served == catalogue_entry(key), key
    # Every cross-reference a document makes has to resolve over the same
    # connection, or it sends the reader back to the source tree.
    for uri in (PLATFORM_PATHS_URI, TARGET_SUPPORT_URI, CONFIG_SCHEMA_URI, LEASE_LIFECYCLE_URI):
        assert read_text(service, uri).strip()
