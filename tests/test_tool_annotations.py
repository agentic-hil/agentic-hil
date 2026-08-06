"""Every advertised tool declares what it does, and a host receives it.

hardci-hq#101: none of our tools carried MCP annotations, so a host had the
name and nothing else and blocked `project_config_reload_description` — the one
call that re-reads the configuration and writes nothing — because it sits next
to two tools that do write. These tests are the gate that keeps a new tool from
shipping without the same declaration, and they check the wire, not the table:
an annotation that never reaches `tools/list` is not an annotation.
"""

from __future__ import annotations

import json
import re
from io import StringIO
from pathlib import Path

from conftest import write_config

from agentic_hil.config import load_config
from agentic_hil.contracts import MCP_TOOL_NAMES, MCP_TOOLS, TOOL_ANNOTATIONS
from agentic_hil.knowledge import LEASE_LIFECYCLE_DOCUMENT
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.stdio import run_stdio_server
from agentic_hil.tools import AgenticHILToolService

# The five fields the MCP schema defines for ToolAnnotations, revision
# 2025-06-18 (the version this server advertises) and unchanged in 2026-07-28.
# Anything else in the object is a typo a host will silently ignore.
ANNOTATION_FIELDS = {"title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
# Meaningful only when readOnlyHint is false, per the schema.
EFFECT_ONLY_FIELDS = {"destructiveHint", "idempotentHint"}


def annotations_by_tool() -> dict[str, dict]:
    """An unannotated tool maps to `{}`, never to None: the test below reports
    it by name, and the rest then fail on the missing hints rather than on an
    AttributeError that names nothing."""
    return {str(tool["name"]): tool.get("annotations") or {} for tool in MCP_TOOLS}


def test_every_advertised_tool_carries_annotations() -> None:
    """A tool added without annotations fails here by name, the same shape as
    the test that fails when a quarantine reason has no catalogue entry."""
    advertised = annotations_by_tool()

    missing = sorted(name for name, annotations in advertised.items() if not annotations)
    assert not missing, f"tools advertised without MCP annotations: {missing}"
    untitled = sorted(name for name, annotations in advertised.items() if not str(annotations.get("title", "")).strip())
    assert not untitled, f"tools whose annotations carry no title: {untitled}"
    # The table is not allowed to grow entries for tools that do not exist
    # either; that is how a rename leaves a stale annotation behind.
    orphaned = sorted(set(TOOL_ANNOTATIONS) - set(MCP_TOOL_NAMES))
    assert not orphaned, f"annotations for tools this server does not advertise: {orphaned}"


def test_annotations_declare_only_the_fields_the_schema_defines() -> None:
    unknown = sorted(
        f"{name}.{field}"
        for name, annotations in annotations_by_tool().items()
        for field in annotations
        if field not in ANNOTATION_FIELDS
    )
    assert not unknown, f"annotation fields the MCP schema does not define: {unknown}"
    wrong_type = sorted(
        f"{name}.{field}"
        for name, annotations in annotations_by_tool().items()
        for field in annotations
        if field != "title" and not isinstance(annotations[field], bool)
    )
    assert not wrong_type, f"annotation hints that are not booleans: {wrong_type}"


def test_read_only_and_destructive_are_never_claimed_together() -> None:
    """`destructiveHint` and `idempotentHint` are meaningful only when
    `readOnlyHint` is false, so a read-only tool that declares either has said
    something the schema gives no meaning to — and `destructiveHint: true` on a
    read-only tool is a flat contradiction."""
    advertised = annotations_by_tool()

    contradictory = sorted(
        name
        for name, annotations in advertised.items()
        if annotations.get("readOnlyHint") is True and annotations.get("destructiveHint") is True
    )
    assert not contradictory, f"tools declared read-only and destructive at once: {contradictory}"
    meaningless = sorted(
        f"{name}.{field}"
        for name, annotations in advertised.items()
        if annotations.get("readOnlyHint") is True
        for field in EFFECT_ONLY_FIELDS
        if field in annotations
    )
    assert not meaningless, f"read-only tools declaring hints the schema only gives meaning to for effects: {meaningless}"


def test_every_tool_states_the_hints_whose_default_is_not_silence() -> None:
    """`destructiveHint` and `openWorldHint` both default to **true**, so
    leaving them out is a claim rather than an omission: an effect tool with no
    `destructiveHint` has told the host it may destroy, and any tool with no
    `openWorldHint` has told it the bench is an open world."""
    advertised = annotations_by_tool()

    unstated_world = sorted(name for name, annotations in advertised.items() if "openWorldHint" not in annotations)
    assert not unstated_world, f"tools that leave openWorldHint at its default of true: {unstated_world}"
    unstated_read = sorted(name for name, annotations in advertised.items() if not isinstance(annotations.get("readOnlyHint"), bool))
    assert not unstated_read, f"tools that state no readOnlyHint: {unstated_read}"
    unstated_effect = sorted(
        f"{name}.{field}"
        for name, annotations in advertised.items()
        if annotations.get("readOnlyHint") is False
        for field in EFFECT_ONLY_FIELDS
        if field not in annotations
    )
    assert not unstated_effect, f"effect tools leaving a hint at a default that claims more than the code does: {unstated_effect}"


def test_the_tools_that_change_nothing_are_declared_read_only() -> None:
    """The set hardci-hq#101 named, minus the two it named that this server
    does not expose: there is no `mass_erase` tool (it is a permission,
    `allow_mass_erase`, and while it is true flashing is refused outright) and
    `hardware_lease_status` is CLI-only, reachable as `agentic-hil
    lease-status`."""
    advertised = annotations_by_tool()

    for name in ("debugger_info", "debugger_probes_list", "com_read", "can_read", "bench_run_status", "project_config_describe", "project_config_reload_description"):
        assert advertised[name]["readOnlyHint"] is True, name
    # The reload is the one the issue turns on: a host that blocks it sends the
    # operator back to reconnecting the server, which is the thing the reload
    # was built to abolish.
    assert advertised["project_config_reload_description"] == {
        "title": "Reload the bench description",
        "readOnlyHint": True,
        "openWorldHint": False,
    }
    assert "mass_erase" not in MCP_TOOL_NAMES
    assert "hardware_lease_status" not in MCP_TOOL_NAMES


def test_what_changes_hardware_irreversibly_is_declared_destructive() -> None:
    advertised = annotations_by_tool()

    for name in ("flash_firmware", "debug_start_session"):
        assert advertised[name]["readOnlyHint"] is False, name
        assert advertised[name]["destructiveHint"] is True, name
    # probe_target reads, and is still not read-only: an SWD attach halts the
    # core, which is the line the quarantine inventory (hardci-hq#97) already
    # drew. It is reversible and repeatable, and says so.
    assert advertised["probe_target"] == {
        "title": "Probe the target",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


def test_titles_are_distinct_so_a_host_can_tell_the_tools_apart() -> None:
    titles = [str(annotations.get("title", "")) for annotations in annotations_by_tool().values()]

    duplicated = sorted({title for title in titles if titles.count(title) > 1})
    assert not duplicated, f"annotation titles used by more than one tool: {duplicated}"


def test_the_host_documentation_classifies_every_tool_the_way_the_table_does() -> None:
    """docs/mcp-hosts.md prints the classification for an operator writing host
    permission rules. A table a reader trusts and a table a host receives have
    to be one table."""
    document = (Path(__file__).resolve().parents[1] / "docs" / "mcp-hosts.md").read_text(encoding="utf-8")
    section = document.split("## Tool Annotations", 1)[1].split("\n## ", 1)[0]
    rows = {
        line.split("|")[1].strip(): set(re.findall(r"`([a-z_]+)`", line.split("|")[2]))
        for line in section.splitlines()
        if line.startswith("|") and "---" not in line and "Class" not in line
    }
    documented = {name: label for label, names in rows.items() for name in names}

    expected = {}
    for name, entry in annotations_by_tool().items():
        if entry["readOnlyHint"]:
            expected[name] = "changes nothing (`readOnlyHint: true`)"
        elif entry["destructiveHint"]:
            expected[name] = "may destroy what it replaces (`destructiveHint: true`)"
        else:
            expected[name] = "changes something reversible (`destructiveHint: false`)"

    misfiled = sorted(f"{name}: documented as {documented.get(name)!r}, annotated as {label!r}" for name, label in expected.items() if documented.get(name) != label)
    assert not misfiled, misfiled
    assert set(documented) == set(MCP_TOOL_NAMES), sorted(set(documented) ^ set(MCP_TOOL_NAMES))


def test_the_lease_lifecycle_resource_names_the_same_hints() -> None:
    """The resource an agent reads and the annotations a host reads make the
    same claims about the same tools, or one of them is wrong."""
    advertised = annotations_by_tool()

    assert "MCP `annotations`" in LEASE_LIFECYCLE_DOCUMENT
    for name in ("project_config_reload_description", "com_read", "can_read", "bench_run_status", "project_config_describe"):
        assert f"`{name}`" in LEASE_LIFECYCLE_DOCUMENT, name
        assert advertised[name]["readOnlyHint"] is True, name
    for name in ("flash_firmware", "debug_start_session"):
        assert advertised[name]["destructiveHint"] is True, name
    assert advertised["probe_target"]["readOnlyHint"] is False
    # The resource states outright that the hints enforce nothing; nothing in
    # the package may start reading them to decide anything.
    assert "enforces nothing by them" in LEASE_LIFECYCLE_DOCUMENT
    source = (Path(__file__).resolve().parents[1] / "src" / "agentic_hil").rglob("*.py")
    readers = sorted(path.name for path in source if path.name != "contracts.py" and "TOOL_ANNOTATIONS" in path.read_text(encoding="utf-8"))
    assert not readers, f"modules reading the annotation table to decide behaviour: {readers}"


def test_annotations_reach_a_client_through_tools_list(tmp_path: Path) -> None:
    """The table existing is not the deliverable; the host receiving it is."""
    config = load_config(str(write_config(tmp_path)))
    service = AgenticHILToolService(config)
    try:
        response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, service)
    finally:
        service.close()

    assert isinstance(response, dict)
    served = {str(tool["name"]): tool.get("annotations") for tool in response["result"]["tools"]}
    assert sorted(served) == sorted(MCP_TOOL_NAMES)
    assert all(served.values()), sorted(name for name, annotations in served.items() if not annotations)
    assert served["project_config_reload_description"]["readOnlyHint"] is True
    assert served["flash_firmware"]["destructiveHint"] is True


def test_annotations_survive_the_stdio_wire(tmp_path: Path) -> None:
    """Serialized by the real server loop and parsed back, because that is the
    only form a host ever sees."""
    config = load_config(str(write_config(tmp_path)))
    output = StringIO()

    exit_code = run_stdio_server(
        config,
        input_stream=StringIO('{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}\n'),
        output_stream=output,
    )

    assert exit_code == 0
    tools = json.loads(output.getvalue().splitlines()[0])["result"]["tools"]
    annotations = {str(tool["name"]): tool.get("annotations") for tool in tools}
    assert len(annotations) == len(MCP_TOOL_NAMES)
    unannotated = sorted(name for name, entry in annotations.items() if not entry)
    assert not unannotated, f"tools that reached the client with no annotations: {unannotated}"
    assert annotations["project_config_reload_description"] == {
        "title": "Reload the bench description",
        "readOnlyHint": True,
        "openWorldHint": False,
    }
    assert annotations["com_read"]["readOnlyHint"] is True
    assert annotations["flash_firmware"]["destructiveHint"] is True
    assert annotations["probe_target"]["readOnlyHint"] is False
