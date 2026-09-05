"""The reference a caller reads instead of this server's source code.

Measured over two real bring-up sessions: 17 reads of the Agentic HIL
implementation, 10 of them into the *installed* package under site-packages,
to recover facts nobody published: which fields a debugger backend needs,
where the programmer executable is configured, which `target_type` a board
takes. A caller installed with `uv tool install` has no source tree at all, so
these two channels are the only ones that can answer: the remediation a failing
result carries, and the MCP resources. Both read one module, and the last test
here is the one that keeps them from drifting apart.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from conftest import (
    FAKE_OPENOCD_NO_TARGET,
    FAKE_OPENOCD_POST_INIT_UNCONFIRMED,
    FAKE_OPENOCD_UNCONFIRMED,
    FAKE_STLINK_NO_PROBE,
    FAKE_STLINK_NO_TARGET,
    FAKE_STLINK_PARTIAL_CONFIRMATION,
    FAKE_STLINK_UNCONFIRMED,
    write_config,
)

from agentic_hil.config import ConfigError, load_config, project_config_directories, project_config_directory
from agentic_hil.knowledge import (
    BACKENDS,
    CONFIG_SCHEMA_URI,
    DEBUGGER_BACKEND_URI_PREFIX,
    DEBUGGER_BACKENDS_URI,
    DEFAULT_TEST_CONFIG_PATH,
    ERROR_CATALOGUE,
    ERROR_URI_PREFIX,
    ERRORS_URI,
    JSON_MIME,
    LEASE_LIFECYCLE_URI,
    MCP_RESOURCE_TEMPLATES,
    MCP_RESOURCES,
    PERMISSION_KEY_PLACEHOLDER,
    PLAN_COMPARATOR_EXAMPLE,
    PLAN_FEATURE_VERSION_KEY,
    PLAN_MINIMAL_EXAMPLE,
    PLAN_ROUTE_KEYS,
    PLATFORM_PATHS_URI,
    RESOURCE_SCHEME,
    TARGET_SUPPORT_URI,
    TEST_PLAN_SCHEMA_URI,
    TEST_PLAN_URI,
    catalogue_entry,
    plan_schema_document,
    plan_schema_text,
    remediation_fields,
    safe_user_root,
)
from agentic_hil.mcp import MCP_RESOURCE_NOT_FOUND, handle_mcp_message
from agentic_hil.tools import AgenticHILToolService


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


def test_the_nine_shell_calls_that_recovered_the_plan_format_are_answered(service: AgenticHILToolService) -> None:
    """The first-run session that motivated this, verbatim.

    Asked to pin a hardware test, an agent issued nine Bash calls: locate the
    installed package under site-packages, dump `schemas/testconfig.schema.json`
    with inline Python, then grep `cli.py` and `test_reactor.py` for the default
    plan filename. It was obeying the published rule (facts about this server
    are resources, never its installed package) and no resource answered the
    one question it had. Each assertion below is one of those calls, closed.
    """
    document = read_text(service, TEST_PLAN_URI)
    schema = plan_schema_document()

    # 1. Where the plan is, and what the reactor reads when nobody names one.
    assert DEFAULT_TEST_CONFIG_PATH in document
    assert "test_config_path" in document and "--test-config" in document
    assert "workspace_root" in document
    # 2. Which versions exist, and that a plan is held to its own.
    for version in schema["properties"]["version"]["enum"]:
        assert f"`{version}`" in document, version
    # 3. Every step the format admits, with the version that introduced it.
    for name, definition in schema["$defs"].items():
        action = (definition.get("properties") or {}).get("action", {}).get("const")
        if not isinstance(action, str):
            continue
        assert f"### `{action}`" in document, name
        since = definition.get(PLAN_FEATURE_VERSION_KEY, min(schema["properties"]["version"]["enum"]))
        assert f"### `{action}`\n\nVersion {since} on." in document, action
    # 4. The comparator family, its rules, and the numeric one version 5 added.
    assert "## The comparators" in document
    assert "Exactly one of `equals` and `pattern`. Both, or neither, is refused." in document
    assert "`range:` is written only beside `pattern`." in document
    assert "`mask:` is refused beside `signed`." in document


def test_the_plan_schema_resource_serves_the_shipped_file_unchanged(service: AgenticHILToolService) -> None:
    """Served the way the configuration schema is: the file, not a rendering of it.

    A caller that wants to validate a plan before sending it needs the document
    the reactor validates against, byte for byte, and a re-serialization would
    quietly drop the `$comment` and the `x-since-version` markers' formatting."""
    served = read_text(service, TEST_PLAN_SCHEMA_URI)

    assert served == plan_schema_text()
    assert json.loads(served) == plan_schema_document()
    assert json.loads(served)["$defs"]["repeat"][PLAN_FEATURE_VERSION_KEY] == 4


def test_the_reference_and_the_reactor_read_one_schema(service: AgenticHILToolService) -> None:
    """The property the whole design exists for, for this format.

    A document generated from a second copy of the schema is a document that
    will one day describe a plan the reactor refuses. Both sides call
    `plan_schema_document`, and this fails the moment somebody gives either its
    own read of the file."""
    from agentic_hil import test_reactor

    assert test_reactor.test_config_schema() is plan_schema_document()
    assert test_reactor.DEFAULT_TEST_CONFIG_PATH == DEFAULT_TEST_CONFIG_PATH
    # The keys the document says a step routes with are the keys the reactor
    # actually resolves a step by, which it builds from its device classes.
    assert PLAN_ROUTE_KEYS == test_reactor.ROUTE_FIELDS


@pytest.mark.parametrize(
    ("name", "plan"),
    [("minimal", PLAN_MINIMAL_EXAMPLE), ("comparator", PLAN_COMPARATOR_EXAMPLE)],
)
def test_the_published_example_plans_load(tmp_path: Path, service: AgenticHILToolService, name: str, plan: str) -> None:
    """An example a reader cannot run is worse than none.

    This is a document written to be copied from, so both plans go through the
    loader itself: the schema, the version gate that refuses a step older than
    the plan's own `version:`, and the build into steps. Nothing here touches
    hardware: a plan is loaded long before a device is opened."""
    from agentic_hil.test_reactor import load_test_config

    assert plan in read_text(service, TEST_PLAN_URI), f"the {name} example is not the one served"
    path = tmp_path / DEFAULT_TEST_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan, encoding="utf-8")

    loaded = load_test_config(str(path), str(tmp_path))

    assert loaded.steps
    assert all(step.action for step in loaded.steps)
    # And the plan is written the way the document tells a reader to write one:
    # one routing key per step, the version 3 spelling.
    for step in loaded.steps:
        assert step.route_keys == ["device"], step.action


def test_the_comparator_example_claims_something_on_every_medium() -> None:
    """The second example earns its place by covering the three families.

    One comparator would have shown the shape; three show that the vocabulary
    differs per medium, which is the mistake the schema's own text warns about:
    a `range` over a regular expression capture is not the `range` a symbol
    takes."""
    plan = yaml.safe_load(PLAN_COMPARATOR_EXAMPLE)
    comparators = {step["action"]: step["comparator"] for step in plan["steps"] if "comparator" in step}

    assert set(comparators) == {"uart_read", "can_read", "read_symbol"}
    assert comparators["uart_read"]["range"] == {"min": 20, "max": 30}
    assert comparators["uart_read"]["pattern"] == r"temp=(\d+)C"
    assert comparators["can_read"]["id"] == "0x201"
    assert comparators["read_symbol"]["range"] == {"min": 1, "max": 8}


def test_a_refused_path_names_the_component_and_a_location_that_works() -> None:
    """The refusal named the field and the path and no way forward.

    The refusal itself has changed (an ACL no longer decides anything, and what
    remains is a path that is not the object it claims to be), but the property
    that made it a defect has not: a caller told "no" has to be told what to do
    next, in the place they are already reading.
    """
    refusal = ConfigError(
        "unsafe_configured_path",
        "Configured path contains a symlink or non-directory component.",
        {"field": "user_config", "path": str(Path.home() / "projects" / "config.yaml"), "component": str(Path.home() / "projects")},
    ).to_dict()

    assert any("`component`" in step for step in refusal["remediation"])
    assert any(safe_user_root() in step for step in refusal["remediation"])
    # And it no longer sends anyone to an ACL, because there is no ACL rule left
    # to satisfy or to break by relaxing it.
    said = " ".join(refusal["remediation"])
    assert "windows_path_trust" not in said
    assert "untrusted_principals" not in said


def test_the_path_table_names_every_root_a_file_can_land_under(service: AgenticHILToolService) -> None:
    """The table is what a caller quotes when asked where a file lives.

    The configuration's own walk has named two roots since #354 and the record
    has stood beside both since #358, so a table naming only the platform
    default is wrong in the one place a caller trusts, and wrong in the
    direction that hides a bench: a project generated under the fallback would
    be looked for where it is not. Presence and order are both checked, the
    order against the walk itself rather than against a second copy of it.
    """
    document = read_text(service, PLATFORM_PATHS_URI)
    section = document.split("## Where things go", 1)[1].split("\n## ", 1)[0]
    cells = {row.split("|")[1].strip(): [cell.strip() for cell in row.split("|")[2:4]] for row in section.splitlines() if row.startswith("| ")}

    # The table is not the only place this document names that directory, and the
    # override example below it had drifted to a spelling of its own. A caller
    # who quotes one and then the other quotes two different directories.
    for line in document.splitlines():
        found = re.search(r"agentic-hil[\\/]projects[\\/]<(?!name>-<digest>)[^>]+>", line)
        assert found is None, f"the served table and its own example disagree: {found.group(0)!r} in {line}"

    # Holds both ways: the walk the table describes, read from the walk. The
    # platform default first, the root every path refusal recommends after it.
    assert project_config_directories()[0] == project_config_directory()
    assert project_config_directories()[-1] == Path(safe_user_root()) / "projects"

    # A table, still: one header and the same four items, in order.
    assert list(cells) == [
        "Item",
        "authoritative configuration",
        "`state_root`",
        "record of configurations bound by `AGENTIC_HIL_CONFIG`",
        "device locks",
    ]
    for item, default, fallback in (
        ("authoritative configuration", "%APPDATA%\\agentic-hil\\projects", "%USERPROFILE%\\.agentic-hil\\projects\\<name>-<digest>\\config.yaml"),
        ("authoritative configuration", "$XDG_CONFIG_HOME/agentic-hil/projects", "~/.agentic-hil/projects/<name>-<digest>/config.yaml"),
        ("`state_root`", "%LOCALAPPDATA%\\agentic-hil", "%USERPROFILE%\\.agentic-hil\\state"),
        ("`state_root`", "$XDG_STATE_HOME/agentic-hil", "~/.agentic-hil/state"),
        ("record of configurations bound by `AGENTIC_HIL_CONFIG`", "%APPDATA%\\agentic-hil\\external-projects.json", "%USERPROFILE%\\.agentic-hil\\external-projects.json"),
        ("record of configurations bound by `AGENTIC_HIL_CONFIG`", "$XDG_CONFIG_HOME/agentic-hil/external-projects.json", "~/.agentic-hil/external-projects.json"),
    ):
        named = [cell for cell in cells[item] if default in cell]
        assert len(named) == 1, (item, default, cells[item])
        assert fallback in named[0], (item, named[0])
        assert named[0].index(default) < named[0].index(fallback), (item, named[0])

    # The rule that outranks that order, and the one item the order does not
    # decide, because both of its files can hold a record at once.
    assert "an existing file wins over it" in section
    assert cells["record of configurations bound by `AGENTIC_HIL_CONFIG`"] == [
        "`%APPDATA%\\agentic-hil\\external-projects.json`, and `%USERPROFILE%\\.agentic-hil\\external-projects.json`",
        "`$XDG_CONFIG_HOME/agentic-hil/external-projects.json`, and `~/.agentic-hil/external-projects.json`",
    ]
    # And the one location that has no second root keeps saying so.
    assert cells["device locks"] == ["`%USERPROFILE%\\.agentic-hil\\device-locks`, fixed", "`~/.agentic-hil/device-locks`, fixed"]


def _prose_lines(text: str) -> list[str]:
    """Every line outside a fenced block, because a fence is syntax, not a claim.

    The override examples in `docs/mcp-hosts.md` spell a full configuration path
    under neither discovered root, which is the only thing the override is for:
    they show what an operator types to reach a location the walk does not, not
    where discovery looks.
    """
    lines, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            lines.append(line)
    return lines


def test_no_document_places_the_configuration_under_one_root_only() -> None:
    """The table is one channel, and it is not the one most readers arrive at.

    #362 gave `platform-paths` both roots. Four documents outside it still ended
    the sentence at the platform default: the configuration reference, the host
    guide, the security scope notes and the demo README. That is wrong in the
    direction that hides a bench, because a project generated under the fallback
    is looked for where it is not, and the reader who hits it is the one whose
    profile forced the fallback in the first place. So every place a document
    says where the generated configuration lives has to name the second root
    too, and say what decides between them.

    Swept rather than listed: a fifth document ending the sentence at the
    platform default is the same defect, and it should fail here rather than on
    a bench. One that names both roots is welcome and passes on these terms.

    The sweep recognises that sentence by the spelling of the per-project
    directory, so the spelling is part of what it guards. A document that named
    the directory some other way was read here as a document that says nothing
    about where the configuration lives, and the root check simply did not run
    on it; the placeholder went three ways across the documents and the served
    table before anyone noticed. One spelling is therefore asserted rather than
    assumed, and it is `platform-paths`' own, which is also the shape `init`
    writes on disk.
    """
    root = Path(__file__).resolve().parents[1]
    # The fallback root's name is read from the code that owns it, so renaming it
    # there fails here rather than leaving every document quietly wrong.
    fallback = Path(safe_user_root()).name
    roots = (f"%USERPROFILE%\\{fallback}", f"~/{fallback}")
    generated = ("projects/<name>-<digest>/config.yaml", "projects\\<name>-<digest>\\config.yaml")
    # The detection above is keyed on one spelling of the per-project directory,
    # so a document that spells the same directory another way is not a style
    # nit: it is invisible here, and the both-roots check never runs on it. The
    # spelling is `platform-paths`' own, which is also the shape `init` writes.
    other_spelling = re.compile(r"agentic-hil[\\/]projects[\\/]<(?!name>-<digest>)[^>]+>")

    documents, every_line = {}, {}
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        # Dot directories are tooling and virtualenvs (`.venv`, `.testenv`), and
        # `build`/`dist` are copies `python -m build` may leave; none of them is a
        # document a reader receives. CHANGELOG.md is excluded because it records
        # what a release said, and a past entry is not a claim in force.
        if any(part.startswith(".") or part in {"build", "dist"} for part in relative.parts) or relative.as_posix() == "CHANGELOG.md":
            continue
        text = path.read_text(encoding="utf-8")
        documents[relative.as_posix()] = _prose_lines(text)
        every_line[relative.as_posix()] = text.splitlines()

    # Fenced blocks are excluded from the root check below and rightly so, but not
    # from this one: how many roots a line names depends on whether it is prose or
    # an example an operator types, while what the directory is called does not.
    for name, lines in every_line.items():
        for line in lines:
            found = other_spelling.search(line)
            assert found is None, f"{name} spells the per-project directory {found.group(0)!r}, which this sweep cannot see: {line}"

    named_by_the_issue = {"docs/configuration.md", "docs/mcp-hosts.md", "SECURITY.md", "examples/nucleo-f446re_demo/README.md"}
    # The sweep has to be able to see the documents the issue named, or an empty
    # sweep would pass while every one of them said the wrong thing.
    assert named_by_the_issue <= set(documents)

    placed = set()
    for name, lines in documents.items():
        says_where = False
        for line in lines:
            if not any(spelling in line for spelling in generated):
                continue
            says_where = True
            assert any(named in line for named in roots), f"{name} places the configuration under one root only: {line}"
        if says_where:
            placed.add(name)
            assert "cannot be written" in "\n".join(lines), f"{name} names both roots without saying what decides between them"

    # The other half of the canary: the detection still recognises the sentence
    # in each of the four. A fifth document is welcome to say it and passes on
    # the same terms, so this is a floor rather than the exact set.
    assert named_by_the_issue <= placed, sorted(placed)

    # The two documents a reader consults about the rule itself carry the part no
    # ordering can express: a file that is already there wins over the order.
    # Anchored to the order rather than to the word, because SECURITY.md says
    # "outranks" of an unrelated thing (an opencode session rule) further down.
    for name in ("docs/configuration.md", "SECURITY.md"):
        text = "\n".join(documents[name])
        assert "outranks the order" in text or "outranks that order" in text, f"{name} does not say an existing file outranks the order"


def test_the_removed_trust_check_leaves_no_advice_behind_it(tmp_path: Path) -> None:
    """A catalogue entry outliving its mechanism is worse than none at all.

    `unsafe_configured_path:user_config`, `:state_root` and `:device_lock_root`
    existed only to explain an ACL or mode refusal and to offer
    `windows_path_trust: permissive` as the override. Nothing raises those
    refusals any more, so an entry still offering that key would send an operator
    to write a line the loader now refuses by name.
    """
    from agentic_hil.knowledge import ERROR_CATALOGUE as catalogue

    assert [key for key in catalogue if key.startswith("unsafe_configured_path:")] == []
    served = json.dumps([entry.as_json() for entry in catalogue.values()])
    for gone in ("windows_path_trust", "untrusted_principals", "principal_class", "app_package"):
        assert gone not in served, f"the catalogue still explains {gone}"


def test_a_second_ordering_is_a_reordering_and_never_a_second_text() -> None:
    """`cli_remediation` may move the steps; it may not be a different list.

    An entry that answers two readers is one text read twice, so what a person
    at a shell is told and what an agent over MCP is told can differ only in
    which move comes first. A step added to one tuple and forgotten in the other
    would be advice one reader never sees, and `command_line_remediation`
    silently declines a reordering whose steps are not the entry's, so the
    forgetting would be invisible at the surface: the shell reader would go back
    to the agent's ordering with nothing saying why.
    """
    from agentic_hil.knowledge import ERROR_CATALOGUE as catalogue

    ordered = {key: entry for key, entry in catalogue.items() if entry.cli_remediation}
    assert ordered, "nothing declares a command-line ordering; this guard is not reading the catalogue any more"
    for key, entry in ordered.items():
        assert sorted(entry.cli_remediation) == sorted(entry.remediation), key
        assert entry.cli_remediation != entry.remediation, f"{key} declares an ordering identical to the default"


def test_the_missing_configuration_entry_answers_both_of_its_readers() -> None:
    """#416: `config_file_not_found` is the one refusal that reaches both.

    An agent meets it on its first tool call and a person meets it on their
    first `agentic-hil doctor`, and each has a route the other does not have.
    Each ordering has to open with the route its own reader can take."""
    from agentic_hil.knowledge import ERROR_CATALOGUE as catalogue

    entry = catalogue["config_file_not_found"]

    assert entry.remediation[0].startswith("Over MCP, call `project_config_create` once.")
    assert entry.cli_remediation[0].startswith("At a shell, run `agentic-hil init` from the project root")
    assert "agentic-hil setup --agent <claude-code|codex|opencode>" in entry.cli_remediation[0]


def test_an_error_nobody_wrote_a_fix_for_grows_no_invented_advice() -> None:
    refusal = ConfigError("config_invalid", "state_root and workspace_root must not overlap.", {"field": "state_root"}).to_dict()

    assert "remediation" not in refusal
    assert "do_not" not in refusal


# The four the catalogue had no entry for, and the reason each gap mattered:
# `invalid_argument` is the most frequent refusal on this surface, the two CAN
# adapter ones are raised where a frame may or may not have reached the bus,
# which is precisely when a caller most needs to be told what to do next, and
# `upgrade_failed` is where every upgrade that does not finish lands, so the one
# path with anything to say had to attach its own `next_steps` to say it while
# every other reason an upgrade fails went out with nothing at all.
UNCOVERED_UNTIL_NOW = ("invalid_argument", "can_adapter_protocol_unsupported", "can_adapter_invalid_response", "upgrade_failed")


@pytest.mark.parametrize("error_type", UNCOVERED_UNTIL_NOW)
def test_the_refusals_that_carried_nothing_now_carry_a_way_forward(error_type: str) -> None:
    """A refusal carries the way forward, and these four did not.

    `remediation_fields` answered `{}` for all of them, so the most common
    refusal this server produces went out with no next step and no `do_not` line
    while `permission_denied`, `device_busy` and `com_port_identity_mismatch`
    each carried one."""
    fields = remediation_fields(error_type)

    assert fields["remediation"], error_type
    assert fields["do_not"], error_type
    assert catalogue_entry(error_type)["meaning"].strip(), error_type


def test_the_invalid_argument_entry_says_how_to_read_the_fields_the_refusal_names(service: AgenticHILToolService) -> None:
    """The entry has to be about `field` and `validator`, not about one argument.

    A per-argument entry would be a second copy of the input schemas with nothing
    keeping it in step with them, so the general entry earns its place only by
    telling a caller how to read what its own result already carries."""
    entry = catalogue_entry("invalid_argument")
    said = json.dumps(entry)

    assert "`field`" in said and "`validator`" in said
    # And the reference resource serves the same entry, over the connection a
    # caller with no source tree has.
    assert json.loads(read_text(service, ERROR_URI_PREFIX + "invalid_argument")) == entry


def test_the_test_config_invalid_entry_names_the_three_answers_and_their_commands(service: AgenticHILToolService) -> None:
    """The refusal every plan that does not run lands on, which had no entry (#431).

    A reader who reaches this has written a plan, so they are past setup and have
    the most to lose by guessing. The entry earns its place by separating the two
    documents a plan refusal can be about, and by naming the command for each: the
    plan is corrected against the `configured_*` list, or the configuration is
    filled in with `adopt-hardware` or written again with `init --force`.
    """
    entry = catalogue_entry("test_config_invalid")
    assert entry is not None
    said = json.dumps(entry)

    assert "`validation_error`" in said and "`next_step`" in said
    assert "adopt-hardware" in said and "init --force" in said
    # The plan reference has to be reachable rather than named, because a reader
    # over MCP has no source tree to look the format up in.
    assert TEST_PLAN_URI in said
    assert read_text(service, TEST_PLAN_URI).strip()
    # Widening the bench so a foreign plan loads is the wrong fix that looks
    # right, and this entry is the only place it is named as one.
    assert entry["do_not"]
    # And the reference resource serves the same entry, over the connection a
    # caller with no source tree has.
    assert json.loads(read_text(service, ERROR_URI_PREFIX + "test_config_invalid")) == entry


def test_a_schema_refusal_carries_the_catalogues_fix_in_the_result(service: AgenticHILToolService) -> None:
    """Every tool's schema refusal is built in one place, so it is fixed in one place.

    Without this the entry would exist and never reach the caller who met the
    error: `contracts.invalid_argument` is what every `tools/call` argument
    failure on this surface is rendered by."""
    refused = mcp(service, "tools/call", {"name": "bench_run_start", "arguments": {}})["result"]["structuredContent"]

    assert refused["error_type"] == "invalid_argument"
    assert refused["field"] == "devices"
    assert refused["remediation"] == remediation_fields("invalid_argument")["remediation"]
    assert refused["do_not"] == remediation_fields("invalid_argument")["do_not"]


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


@pytest.mark.parametrize(
    ("backend", "executable", "adapter_claim"),
    [("openocd", FAKE_OPENOCD_UNCONFIRMED, "reached the debug adapter"), ("stlink", FAKE_STLINK_UNCONFIRMED, "reached the ST-Link")],
)
def test_a_toolchain_that_confirmed_nothing_publishes_no_abort_point_to_the_reference(
    tmp_path: Path, backend: str, executable: Path, adapter_claim: str
) -> None:
    """The public half of the markerless-read rule, which the backend field alone did not hold.

    A caller follows `error_type` and the resource behind it; only a reader of
    this project's source knows `backend_error_type` exists. Both of these
    branches used to publish `target_not_detected`, whose shipped entry says the
    adapter was reached and no target answered, the one claim the branch has no
    evidence for, made in the channel a caller is told to read.
    """
    tools = AgenticHILToolService(
        load_config(str(write_config(tmp_path, debugger_type=backend, debugger_executable=executable)))
    )
    try:
        result = tools.call("probe_target")
        entry = json.loads(read_text(tools, ERROR_URI_PREFIX + f"target_state_unconfirmed:{backend}"))
        # The entry it no longer borrows, read over the same connection, so the
        # two are different answers rather than one text serving both.
        detected = json.loads(read_text(tools, ERROR_URI_PREFIX + f"target_not_detected:{backend}"))
    finally:
        tools.close()

    assert result["ok"] is False
    assert result["backend_error_type"] == "probe_unconfirmed"
    assert result["error_type"] == "target_state_unconfirmed"
    # The reason is unchanged and the bench is not held for it: since #216 an
    # unconfirmed target is what the next reset and probe speak for.
    assert result["quarantined"] is False
    assert result["cleanup_reasons"] == ["debugger_readonly_target_state_unconfirmed"]

    assert result["remediation"] == entry["remediation"]
    assert entry["meaning"] == catalogue_entry(f"target_state_unconfirmed:{backend}")["meaning"]
    # The resource says what is unknown, and says the word: no abort point, so
    # nothing here may read as "the target was reached" or as "it was not".
    assert "unknown" in entry["meaning"]
    assert adapter_claim not in entry["meaning"]
    assert any("target_not_detected" in step for step in entry["do_not"])
    assert adapter_claim in detected["meaning"]


@pytest.mark.parametrize(
    ("backend", "executable", "present", "absent"),
    [
        ("openocd", FAKE_OPENOCD_POST_INIT_UNCONFIRMED, "AGENTIC_HIL_STAGE:init:ok", "AGENTIC_HIL_RESULT:probe_target:ok"),
        ("stlink", FAKE_STLINK_PARTIAL_CONFIRMATION, "ST-LINK SN", "Device name"),
    ],
)
def test_a_partial_confirmation_is_not_described_as_an_absent_one(
    tmp_path: Path, backend: str, executable: Path, present: str, absent: str
) -> None:
    """The catalogue entry has to hold for the whole branch it documents.

    Neither backend needs *every* confirming line to be missing to reach this
    result: OpenOCD decides it on the success marker alone, so the stage marker
    may be in the same output, and ST-Link requires all of its expected lines,
    so one of two is enough. The entry used to describe both as having reported
    nothing at all, which a caller reading it against a log that does carry a
    marker would have found false, in the one channel it is told to trust. The
    marker that did arrive is now in the result instead of being asserted away
    by the resource.
    """
    tools = AgenticHILToolService(
        load_config(str(write_config(tmp_path, debugger_type=backend, debugger_executable=executable)))
    )
    try:
        result = tools.call("probe_target")
        entry = json.loads(read_text(tools, ERROR_URI_PREFIX + f"target_state_unconfirmed:{backend}"))
        log = json.loads((tmp_path / result["log_path"]).read_text(encoding="utf-8"))
    finally:
        tools.close()

    # Same branch as the markerless fixtures: still no abort point, still
    # the reason a re-read may not settle. Only the evidence published
    # alongside it differs.
    assert result["backend_error_type"] == "probe_unconfirmed"
    assert result["error_type"] == "target_state_unconfirmed"
    # The reason is unchanged and the bench is not held for it: since #216 an
    # unconfirmed target is what the next reset and probe speak for.
    assert result["quarantined"] is False
    assert result["cleanup_reasons"] == ["debugger_readonly_target_state_unconfirmed"]

    assert present in f"{log['stdout']}{log['stderr']}"
    assert result["operation_result"]["confirmed"] is False
    assert result["operation_result"]["matched_success_text"] == [present]
    assert absent in result["operation_result"]["expected_success_text"]
    assert absent not in result["operation_result"]["matched_success_text"]

    # The served entry must not contradict that result. It says the confirmation
    # is incomplete and points at the fields, rather than naming which lines were
    # missing on a run it cannot see.
    assert result["remediation"] == entry["remediation"]
    assert "operation_result" in entry["meaning"]
    assert "unknown" in entry["meaning"]
    for phrase in ("printed neither", "does not carry the lines", "reported nothing"):
        assert phrase not in entry["meaning"], entry["meaning"]


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
        # The one entry whose steps are written around a key the catalogue
        # cannot know supplies it here as the generic shape, which is exactly
        # what the reference serves a reader who has met no refusal yet (#443).
        inline = remediation_fields(error_type, scope or None, permission=PERMISSION_KEY_PLACEHOLDER)

        assert served["remediation"] == inline["remediation"], key
        assert served.get("do_not") == inline.get("do_not"), key
        assert served == catalogue_entry(key), key
    # Every cross-reference a document makes has to resolve over the same
    # connection, or it sends the reader back to the source tree.
    for uri in (PLATFORM_PATHS_URI, TARGET_SUPPORT_URI, CONFIG_SCHEMA_URI, LEASE_LIFECYCLE_URI):
        assert read_text(service, uri).strip()
