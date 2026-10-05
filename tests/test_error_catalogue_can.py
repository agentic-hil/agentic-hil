"""Every error type a CAN tool returns has an entry in the error catalogue (#635).

A refusal names its `error_type`, and `agentic-hil://reference/errors/<error_type>`
is where a caller looks the fix up. For most of what the five CAN tools
(`can_buses_list`, `can_session_start`, `can_session_stop`, `can_send`,
`can_read`) answer there was nothing to look up: `can_send_failed`,
`can_adapter_close_failed`, `can_participant_not_configured` and the broker's
own refusals resolved to a missing resource, and the refusal itself carried no
`remediation` and no `do_not`. A caller told only "no" reaches for the
workaround the refusal was protecting against: opening the adapter directly,
or retrying a call that cannot succeed.

The decided behaviour: every error type the CAN modules write into a result has
a catalogue entry, the reference URI serves it, and the refusal carries the
entry's `remediation` and `do_not` in its payload, so the fix in the result and
the fix a caller looks up are the same text.

The inventory is read out of the code, the way `test_can_likely_causes` reads
its own, and for the same reason: a list is something somebody has to remember
to extend. Three modules make up the CAN path. `agentic_hil.can` spells most
types in place. `agentic_hil.bridge` builds the adapter bridge's types out of
the session's `error_prefix` and a kind passed at the call site, so
`can_adapter_timeout` and its siblings appear as no literal anywhere.
`agentic_hil.canbroker` answers for the participant path, whose refusals reach
`can_session_start` through `ParticipantError` and `can_send` and `can_read`
through the broker's own answer. The scan reads every shape those modules use
to write a type (a dictionary key, a keyword, an exception built from an error
type, a subscript assignment, `setdefault`), and mutation tests hold that each
shape is seen: a new type written in any of them fails both the pinned inventory
and the entry guard. The scan is pinned as well, so a scanner that silently
stopped seeing a module would fail here rather than pass by finding less.

Two sources are forwarded rather than spelled. The broker loads the
configuration itself and prints the `ConfigError` it stopped on, and the
participant reads that document back as its refusal, so the types
`agentic_hil.config` raises are CAN refusals too; they are pinned from a scan of
that module and one of them is driven through the real broker exit. And
`can_session_start` forwards the coordinator's own refusal of a lease, of which
`undeclared_device` is the one the shared-type changes do not own.

Kept out of the required set, each for a reason stated where it is listed:

* `session_not_active` is answered by the COM, CAN and debug sessions alike, and
  its one bare entry is written with the COM refusals of #635. The CAN refusal
  is required to carry whatever that entry says, and the tests that hold it stay
  red until that entry exists.
* `can_broker_not_attached` is spelled in the broker and returned by no CAN
  tool.
* Two configuration types the broker can forward have no bare entry, and one
  configuration type is raised only on a path no CAN tool reaches.
* The types every hardware tool shares (`audit_unavailable`,
  `hardware_action_exception` and the rest, #645; `resource_busy` and the
  coordination types, #646) are owned by those changes and named below so the
  boundary is a decision rather than an omission.

Required although no tool returns them at the top level: the three cleanup types
a caller of `can_session_start` reads under `cleanup_error` when a bridge that
refused to open also would not close. They are CAN types in practice, because
the CAN adapter session is the only process bridge there is, and a test below
holds that true and drives the real bridge into each of them.

What an entry says is checked as well as that it exists. Each new entry has a
specification of the fields and tools its steps must name and of the relations
its sentences must state, and of the claims it must never make: above all, that
a retried stop settles a bridge close that was never confirmed: since #633 the
first call that meets the ended bridge answers once and gives the bus back, and
a retry finds nothing left to settle. Generic advice fails every specification, and
an entry that makes one of the forbidden claims fails its own.

No hardware and no CAN interface. The behavioural tests drive the real tool
functions over the fakes the CAN suites already use: a python-can module whose
bus is staged, a broker participant that answers a staged refusal, a bridge
child whose pipes are staged, and an adapter session that will not close.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import os
import queue
import re
import sys
import textwrap
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from conftest import write_config
from test_can_broker_deadline import BUS_ID as DEADLINE_BUS_ID
from test_can_broker_deadline import PARTICIPANT as DEADLINE_PARTICIPANT
from test_can_broker_deadline import FakeClock, ScriptedBroker, prepared_bus
from test_can_likely_causes import RefusingCloseAdapter, call_site_strings, process_bridge_session_classes
from test_can_participant_sessions import SharedFakeParticipant

import agentic_hil.bridge as bridge_module_under_test
import agentic_hil.can as can_module_under_test
import agentic_hil.canbroker as canbroker_module_under_test
import agentic_hil.config as config_module_under_test
import agentic_hil.coordination as coordination_module_under_test
from agentic_hil.bridge import ProcessBridgeSession
from agentic_hil.canbroker import BROKER_EXIT_CONFIG, ParticipantError
from agentic_hil.config import load_config
from agentic_hil.coordination import DetachedHardwareLease
from agentic_hil.knowledge import ERROR_CATALOGUE, ERROR_URI_PREFIX, ErrorRemedy, catalogue_entry, remediation_fields
from agentic_hil.mcp import handle_mcp_message, read_resource_response
from agentic_hil.tools import AgenticHILToolService

# ---------------------------------------------------------------------------
# The inventory, read out of the code.


def module_source(module: ModuleType) -> str:
    return Path(inspect.getsourcefile(module) or "").read_text(encoding="utf-8")


def takes_error_type_first(candidate: object) -> bool:
    """Whether calling this builds an error out of an error type passed first.

    `ConfigError("config_invalid", ...)` is how the broker raises a refusal about
    its socket directory, and `ParticipantError(error.to_dict())` carries it to
    the participant; a class whose first parameter is `error_type` is the shape,
    whatever the class is called, so a new exception of that shape is read too.
    """
    if not isinstance(candidate, type):
        return False
    try:
        parameters = list(inspect.signature(candidate).parameters)
    except (TypeError, ValueError):
        return False
    return bool(parameters) and parameters[0] == "error_type"


def scan_source(source: str, namespace: dict[str, object], attributes: dict[str, tuple[str, ...]] | None = None) -> tuple[dict[str, list[int]], list[tuple[int, str]]]:
    """Every error type this source writes into a result, and every place it
    writes one this scan cannot read.

    The shapes: an ``"error_type"`` key in a dictionary literal, an
    ``error_type=`` keyword (``dict(...)`` and the helpers that build a result),
    the first argument of a class whose first parameter is `error_type`, an
    assignment to ``result["error_type"]`` (annotated or not), and
    ``result.setdefault("error_type", ...)``.

    A value is read the way `raised_can_error_types` in `test_can_likely_causes`
    reads one, with two differences that matter here. Every value is recorded,
    not only the `can_` ones, because a CAN tool answers `invalid_argument` and
    `config_invalid` too and those need their entries as much. And a
    conditional expression is read branch by branch, because the broker writes
    ``"can_bus_incident" if scope == "bus" else "can_participant_incident"`` and
    the second type is otherwise invisible.

    An expression that resolves to nothing is returned rather than dropped, with
    its line and its source text, so that a refusal whose type is computed is a
    pinned decision and not a gap in the inventory.
    """
    tree = ast.parse(source)
    call_strings = call_site_strings(tree)
    attribute_values = attributes or {}
    found: dict[str, list[int]] = {}
    unresolved: list[tuple[int, str]] = []

    def parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
        return {argument.arg for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)}

    def lookup(node: ast.expr) -> object:
        if isinstance(node, ast.Name):
            return namespace.get(node.id)
        if isinstance(node, ast.Attribute):
            owner = lookup(node.value)
            return getattr(owner, node.attr, None) if owner is not None else None
        return None

    def resolve(node: ast.expr, enclosing: ast.FunctionDef | ast.AsyncFunctionDef | None) -> list[str]:
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else []
        if isinstance(node, ast.Name):
            value = namespace.get(node.id)
            if isinstance(value, str):
                return [value]
            if enclosing is not None and node.id in parameters(enclosing):
                return sorted(call_strings.get((enclosing.name, node.id), set()))
            return []
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"):
            return list(attribute_values.get(node.attr, ()))
        if isinstance(node, ast.JoinedStr):
            combinations = [""]
            for part in node.values:
                pieces = resolve(part.value if isinstance(part, ast.FormattedValue) else part, enclosing)
                if not pieces:
                    return []
                combinations = [start + piece for start in combinations for piece in pieces]
            return combinations
        return []

    def branches(node: ast.expr) -> list[ast.expr]:
        if isinstance(node, ast.IfExp):
            return [*branches(node.body), *branches(node.orelse)]
        return [node]

    def record(node: ast.expr, enclosing: ast.FunctionDef | ast.AsyncFunctionDef | None) -> None:
        for branch in branches(node):
            values = resolve(branch, enclosing)
            if not values:
                unresolved.append((branch.lineno, ast.unparse(branch)))
            for value in values:
                found.setdefault(value, []).append(branch.lineno)

    def is_the_key(node: ast.expr | None) -> bool:
        return isinstance(node, ast.Constant) and node.value == "error_type"

    def visit(node: ast.AST, enclosing: ast.FunctionDef | ast.AsyncFunctionDef | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            enclosing = node
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if is_the_key(key) and value is not None:
                    record(value, enclosing)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "error_type":
                    record(keyword.value, enclosing)
            if node.args and takes_error_type_first(lookup(node.func)):
                record(node.args[0], enclosing)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault" and len(node.args) == 2 and is_the_key(node.args[0]):
                record(node.args[1], enclosing)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None and any(isinstance(target, ast.Subscript) and is_the_key(target.slice) for target in targets):
                record(node.value, enclosing)
        for child in ast.iter_child_nodes(node):
            visit(child, enclosing)

    visit(tree, None)
    return found, unresolved


def spelled_error_types(module: ModuleType, attributes: dict[str, tuple[str, ...]] | None = None) -> tuple[dict[str, list[int]], list[tuple[int, str]]]:
    return scan_source(module_source(module), dict(vars(module)), attributes)


def can_bridge_sessions() -> dict[str, type]:
    """The process bridge sessions that build error types, which excludes the base.

    `ProcessBridgeSession` itself carries `error_prefix = "bridge"`, and nothing
    instantiates it: its `bridge_*` kinds would be types no tool returns.
    """
    return {name: session_class for name, session_class in process_bridge_session_classes().items() if session_class is not ProcessBridgeSession}


def bridge_attributes() -> dict[str, tuple[str, ...]]:
    return {"error_prefix": tuple(sorted({str(session_class.error_prefix) for session_class in can_bridge_sessions().values()}))}


def scanned_modules() -> list[tuple[ModuleType, dict[str, tuple[str, ...]] | None]]:
    return [(can_module_under_test, None), (bridge_module_under_test, bridge_attributes()), (canbroker_module_under_test, None)]


def file_name(module: ModuleType) -> str:
    return Path(inspect.getsourcefile(module) or "").name


def scanned_inventory() -> dict[str, dict[str, list[int]]]:
    """The scan, by file name, then by error type, with the lines it is written at."""
    return {file_name(module): spelled_error_types(module, attributes)[0] for module, attributes in scanned_modules()}


def scanned_sites() -> dict[str, list[str]]:
    """Every error type on the CAN path, with its `file:line` sites."""
    sites: dict[str, list[str]] = {}
    for name, types in scanned_inventory().items():
        for error_type, lines in types.items():
            sites.setdefault(error_type, []).extend(f"{name}:{line}" for line in lines)
    return sites


# What the scan finds today, by module. Pinned so the scan cannot shrink without
# failing: a refactor that moved a refusal into a helper the scanner does not
# read would otherwise make the guard below pass by seeing less.
PINNED_INVENTORY: dict[str, frozenset[str]] = {
    "can.py": frozenset(
        {
            "can_adapter_close_failed",
            "can_adapter_invalid_response",
            "can_adapter_library_missing",
            "can_adapter_not_found",
            "can_adapter_open_failed",
            "can_adapter_process_start_failed",
            "can_adapter_protocol_unsupported",
            "can_backend_not_available",
            "can_bus_not_configured",
            "can_channel_not_available",
            "can_classic_frame_too_large",
            "can_fd_frame_length_invalid",
            "can_fd_remote_frame_unsupported",
            "can_interface_down",
            "can_interface_not_found",
            "can_listen_only_mode",
            "can_listen_only_unconfirmed",
            "can_listen_only_unsupported",
            "can_participant_not_configured",
            "can_participant_required",
            "can_queue_clear_failed",
            "can_queue_clear_limit",
            "can_read_failed",
            "can_send_failed",
            "config_invalid",
            "invalid_argument",
            "permission_denied",
            "resource_quarantined",
            "session_lease_held",
            "session_not_active",
        }
    ),
    "bridge.py": frozenset(
        {
            "bridge_process_reap_failed",
            "bridge_safe_state_unconfirmed",
            "can_adapter_close_interrupted",
            "can_adapter_invalid_request",
            "can_adapter_invalid_response",
            "can_adapter_process_exited",
            "can_adapter_timeout",
        }
    ),
    "canbroker.py": frozenset(
        {
            "can_broker_authentication_failed",
            "can_broker_counter_mismatch",
            "can_broker_disconnected",
            "can_broker_invalid_message",
            "can_broker_not_attached",
            "can_broker_not_bus_owner",
            "can_broker_protocol_mismatch",
            "can_broker_stopping",
            "can_broker_timeout",
            "can_broker_unavailable",
            "can_broker_wrong_bus",
            "can_bus_gated",
            "can_bus_incident",
            "can_bus_not_configured",
            "can_bus_not_shared",
            "can_listen_only_conflict",
            "can_participant_busy",
            "can_participant_filter_violation",
            "can_participant_frame_budget_exhausted",
            "can_participant_incident",
            "can_participant_lock_required",
            "can_participant_not_configured",
            "can_send_failed",
            # `endpoint_address` raises it as a `ConfigError`, and
            # `_attach_once` hands it to the participant through
            # `ConfigError.to_dict`, which attaches its entry itself.
            "config_invalid",
            "device_busy",
            "invalid_argument",
            "permission_denied",
        }
    ),
}

# The places a type is computed rather than spelled, by module and source text.
# Each one passes on a type that is already in the inventory or pinned below,
# and a new one has to be added here, with its reason, before the guard passes.
PINNED_UNRESOLVED: dict[str, frozenset[str]] = {
    "can.py": frozenset(),
    "bridge.py": frozenset(),
    "canbroker.py": frozenset(
        {
            # `main` prints the `ConfigError` it could not load past as a bare
            # document, `error_type` and `summary` and nothing else: no entry
            # is attached there. Its types are the configuration's own, pinned
            # below from a scan of `agentic_hil.config`, and they reach the
            # participant through the next line.
            "error.error_type",
            # `_explained_exit_refusal` keeps the type of the document a broker
            # wrote before it exited: the adapter's own open refusal, which is a
            # type the scans of `agentic_hil.can` and `agentic_hil.bridge`
            # already find, `can_bus_not_shared` from `main`, or one of the
            # configuration types above. `_participant_session_start` is where
            # its entry is attached.
            "document.get('error_type')",
        }
    ),
}

# `load_authoritative_config` raises these, `main` prints whichever one stopped
# the broker, and `_explained_exit_refusal` hands it to `can_session_start` on a
# shared bus. The scan of `agentic_hil.config` finds exactly the three sets below
# together, which a test holds.
FORWARDED_CONFIG_WITH_AN_ENTRY = frozenset({"config_file_not_found", "config_invalid", "unsafe_configured_path"})
FORWARDED_CONFIG_WITHOUT_AN_ENTRY: dict[str, str] = {
    "config_unreadable": (
        "the catalogue has only `config_unreadable:running_server`; the bare entry is a configuration refusal every"
        " tool can meet and is not written by this change"
    ),
    "config_schema_invalid": (
        "raised when the bundled schema cannot be read, which is a broken install every tool meets; no entry exists"
        " and none is written by this change"
    ),
}
NOT_REACHED_BY_A_CAN_TOOL: dict[str, str] = {
    "mcp_command_untrusted": (
        "raised only inside `trusted_persistent_executable`, which `agentic_hil.config` never calls and no CAN module"
        " names; the command line checks an MCP host entry with it"
    ),
}
# `can_session_start` forwards `CoordinationError.result` whole when the lease is
# refused. Of what the coordinator answers there, this is the one type the
# shared-type changes below do not own.
FORWARDED_FROM_COORDINATION: dict[str, str] = {
    "undeclared_device": "HardwareCoordinator.acquire, inside a run that did not declare the bus",
}

# Answered by the COM, CAN and debug sessions alike. The one bare entry is written
# with the COM refusals of #635 and is true for all three; the CAN refusal at
# `CanBusService._active_session` carries it, which
# `test_a_can_call_without_a_session_carries_the_shared_entry` holds.
OWNED_BY_THE_COM_REFUSALS: dict[str, str] = {
    "session_not_active": "one bare entry, written with the COM refusals of #635, true for COM, CAN and debug sessions",
    "session_lease_held": "one bare entry, written with the COM stop of #660, true for COM and CAN sessions; tests/test_close_failure_state.py holds the CAN refusal carrying it",
}

# Spelled on the CAN path and returned by no CAN tool, so an entry for them would
# describe an answer nobody reads.
NOT_RETURNED_BY_A_TOOL: dict[str, str] = {
    "can_broker_not_attached": (
        "the broker answers it only to a connection whose first message is not an attach, and _attach_once always"
        " sends the attach first"
    ),
}

# Returned by the CAN tools and by every other hardware tool, and owned by the
# changes that write entries for the whole surface. Named so the boundary is
# visible; none of them is spelled in the CAN modules, which a test holds.
OWNED_BY_OTHER_CHANGES: dict[str, str] = {
    "audit_unavailable": "#645",
    "service_closed": "#645",
    "service_cleanup_required": "#645",
    "hardware_action_exception": "#645",
    "audit_failed_after_action": "#645",
    "unknown_tool": "#645",
    "unknown_device": "#645",
    "resource_busy": "#646",
    "coordination_closed": "#646",
    "coordination_state_invalid": "#646",
    "run_stopped": "#646",
}

# Not top-level, and required anyway: a caller of `can_session_start` reads them
# under `cleanup_error` (and `cleanup_error.close_response`) when a bridge refused
# to open and then would not close (`open_process_adapter`).
NESTED_UNDER_CLEANUP_ERROR = frozenset({"bridge_safe_state_unconfirmed", "bridge_process_reap_failed", "can_adapter_close_interrupted"})

ALL_PINNED = frozenset().union(*PINNED_INVENTORY.values())
# Types the entry guard does not require, each for the reason it is listed with.
NOT_REQUIRED = frozenset({*OWNED_BY_THE_COM_REFUSALS, *NOT_RETURNED_BY_A_TOOL, *FORWARDED_CONFIG_WITHOUT_AN_ENTRY, *NOT_REACHED_BY_A_CAN_TOOL})
REQUIRED = sorted((ALL_PINNED | FORWARDED_CONFIG_WITH_AN_ENTRY | set(FORWARDED_FROM_COORDINATION)) - NOT_REQUIRED)

# The types in REQUIRED that had no entry when this guard was written. Pinned
# rather than computed from the catalogue, because once they are written a
# computed set would be empty and the checks that apply to new entries would run
# over nothing.
NEW_ENTRIES = frozenset(
    {
        "bridge_process_reap_failed",
        "bridge_safe_state_unconfirmed",
        "can_adapter_close_failed",
        "can_adapter_close_interrupted",
        "can_adapter_invalid_request",
        "can_adapter_not_found",
        "can_adapter_open_failed",
        "can_adapter_process_exited",
        "can_adapter_process_start_failed",
        "can_adapter_timeout",
        "can_backend_not_available",
        "can_broker_authentication_failed",
        "can_broker_counter_mismatch",
        "can_broker_disconnected",
        "can_broker_invalid_message",
        "can_broker_not_bus_owner",
        "can_broker_protocol_mismatch",
        "can_broker_stopping",
        "can_broker_timeout",
        "can_broker_unavailable",
        "can_broker_wrong_bus",
        "can_bus_gated",
        "can_bus_incident",
        "can_bus_not_configured",
        "can_bus_not_shared",
        "can_listen_only_conflict",
        "can_participant_busy",
        "can_participant_filter_violation",
        "can_participant_frame_budget_exhausted",
        "can_participant_incident",
        "can_participant_lock_required",
        "can_participant_not_configured",
        "can_participant_required",
        "can_queue_clear_failed",
        "can_queue_clear_limit",
        "can_read_failed",
        "can_send_failed",
    }
)


def inventory_problems(name: str, found: dict[str, list[int]]) -> list[str]:
    """How a scan of one module differs from its pinned inventory."""
    pinned = PINNED_INVENTORY[name]
    new = [f"{name}: {error_type} at lines {found[error_type]} is not in the pinned inventory" for error_type in sorted(set(found) - pinned)]
    gone = [f"{name}: {error_type} is pinned and the scan no longer finds it" for error_type in sorted(pinned - set(found))]
    return new + gone


def unresolved_problems(name: str, unresolved: list[tuple[int, str]]) -> list[str]:
    """How the computed types of one module differ from its pinned decisions."""
    texts = {text for _, text in unresolved}
    pinned = PINNED_UNRESOLVED[name]
    new = [f"{name}: `{text}` at line {line} is a computed error_type nobody decided about" for line, text in sorted(unresolved) if text not in pinned]
    gone = [f"{name}: `{text}` is pinned and the scan no longer finds it" for text in sorted(pinned - texts)]
    return new + gone


def missing_entries(types: Iterable[str]) -> list[str]:
    """The types among these that need an entry and have none."""
    return sorted({error_type for error_type in types if error_type not in ERROR_CATALOGUE and error_type not in NOT_REQUIRED})


def test_the_scan_finds_exactly_the_pinned_inventory() -> None:
    scanned = scanned_inventory()
    problems = [problem for name, found in scanned.items() for problem in inventory_problems(name, found)]

    assert set(scanned) == set(PINNED_INVENTORY), scanned
    assert not problems, "\n".join(problems)


def test_every_type_the_scan_cannot_read_is_a_pinned_decision() -> None:
    problems = [problem for module, attributes in scanned_modules() for problem in unresolved_problems(file_name(module), spelled_error_types(module, attributes)[1])]

    assert not problems, "\n".join(problems)


def test_the_bridge_types_reach_a_caller_only_through_the_can_adapter() -> None:
    """What makes `bridge_*` and `can_adapter_*` CAN types: the CAN adapter
    session is the only process bridge. A second one would build types under
    its own prefix and send the `bridge_*` cleanup types to another tool, and
    then whose entries they are is a question to take on purpose."""
    sessions = can_bridge_sessions()

    assert sessions and all(session_class.__module__ == can_module_under_test.__name__ for session_class in sessions.values()), sessions
    assert bridge_attributes() == {"error_prefix": (can_module_under_test.ProcessCanAdapterSession.error_prefix,)}


def test_the_configuration_types_the_broker_forwards_are_pinned() -> None:
    """Whatever `agentic_hil.config` can raise, a broker that could not load its
    configuration prints, so the three sets together are the whole module."""
    found, unresolved = spelled_error_types(config_module_under_test)

    assert set(found) == FORWARDED_CONFIG_WITH_AN_ENTRY | set(FORWARDED_CONFIG_WITHOUT_AN_ENTRY) | set(NOT_REACHED_BY_A_CAN_TOOL), sorted(found)
    # `ConfigError.to_dict` passes its own type on, which is every type above.
    assert {text for _, text in unresolved} == {"self.error_type"}, unresolved
    assert not FORWARDED_CONFIG_WITH_AN_ENTRY & set(FORWARDED_CONFIG_WITHOUT_AN_ENTRY)


def test_the_untrusted_command_refusal_is_on_no_can_path() -> None:
    """`mcp_command_untrusted` is left out because nothing on the CAN path can
    raise it; this is that claim, read off the code."""
    tree = ast.parse(module_source(config_module_under_test))
    found, _ = spelled_error_types(config_module_under_test)
    definitions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "trusted_persistent_executable"]
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ((isinstance(node.func, ast.Name) and node.func.id == "trusted_persistent_executable") or (isinstance(node.func, ast.Attribute) and node.func.attr == "trusted_persistent_executable"))
    ]

    assert len(definitions) == 1, definitions
    function = definitions[0]
    assert found["mcp_command_untrusted"] and all(function.lineno <= line <= (function.end_lineno or function.lineno) for line in found["mcp_command_untrusted"]), found["mcp_command_untrusted"]
    assert not calls, [node.lineno for node in calls]
    for module, _ in scanned_modules():
        assert "trusted_persistent_executable" not in module_source(module), file_name(module)


def test_the_coordination_refusal_a_can_start_forwards_is_written_by_the_coordinator() -> None:
    found, _ = spelled_error_types(coordination_module_under_test)

    assert set(FORWARDED_FROM_COORDINATION) <= set(found), sorted(set(FORWARDED_FROM_COORDINATION) - set(found))
    assert not set(FORWARDED_FROM_COORDINATION) & set(OWNED_BY_OTHER_CHANGES)


def test_every_exclusion_names_a_type_the_scan_finds() -> None:
    """An exclusion for a type no code writes any more is a stale reason."""
    for excluded in (*OWNED_BY_THE_COM_REFUSALS, *NOT_RETURNED_BY_A_TOOL, *NESTED_UNDER_CLEANUP_ERROR):
        assert excluded in ALL_PINNED, excluded


def test_every_new_entry_is_a_required_type() -> None:
    assert not NEW_ENTRIES - set(REQUIRED), sorted(NEW_ENTRIES - set(REQUIRED))
    assert NESTED_UNDER_CLEANUP_ERROR <= NEW_ENTRIES, sorted(NESTED_UNDER_CLEANUP_ERROR - NEW_ENTRIES)


def test_the_types_other_changes_own_are_not_written_by_the_can_modules() -> None:
    assert not set(OWNED_BY_OTHER_CHANGES) & ALL_PINNED, sorted(set(OWNED_BY_OTHER_CHANGES) & ALL_PINNED)


def test_every_error_type_on_the_can_path_has_a_catalogue_entry() -> None:
    """The guard: a CAN refusal cannot ship without the entry that explains it."""
    sites = scanned_sites()
    missing = missing_entries([*sites, *FORWARDED_CONFIG_WITH_AN_ENTRY, *FORWARDED_FROM_COORDINATION])

    assert not missing, "CAN error types without a catalogue entry:\n" + "\n".join(f"  {error_type}: {', '.join(sites.get(error_type, ['forwarded']))}" for error_type in missing)


# ---------------------------------------------------------------------------
# The guards above fail on a type written in any shape the CAN modules use.

MUTANT_TYPE = "can_mutant_refusal"

# One function each, appended to the real `agentic_hil.can` and run in a copy of
# its namespace, so the scan reads them beside everything it already reads.
MUTANTS: dict[str, str] = {
    "dictionary literal": """
        def _mutant():
            return {"ok": False, "error_type": "can_mutant_refusal"}
        """,
    "dict keyword": """
        def _mutant():
            return dict(ok=False, error_type="can_mutant_refusal")
        """,
    "exception of its own": """
        class _MutantRefusal(Exception):
            def __init__(self, error_type, summary):
                super().__init__(summary)
                self.error_type = error_type

        def _mutant():
            raise _MutantRefusal("can_mutant_refusal", "staged")
        """,
    "ConfigError": """
        def _mutant():
            raise ConfigError("can_mutant_refusal", "staged")
        """,
    "subscript assignment": """
        def _mutant(result):
            result["error_type"] = "can_mutant_refusal"
        """,
    "annotated subscript assignment": """
        def _mutant(result):
            result["error_type"]: str = "can_mutant_refusal"
        """,
    "setdefault": """
        def _mutant(result):
            result.setdefault("error_type", "can_mutant_refusal")
        """,
    "conditional expression": """
        def _mutant(flag):
            return {"ok": False, "error_type": "can_listen_only_mode" if flag else "can_mutant_refusal"}
        """,
    "module constant": """
        _MUTANT_ERROR = "can_mutant_refusal"

        def _mutant():
            return {"ok": False, "error_type": _MUTANT_ERROR}
        """,
    "parameter set at the call site": """
        def _mutant_refusal(kind):
            return {"ok": False, "error_type": kind}

        def _mutant():
            return _mutant_refusal("can_mutant_refusal")
        """,
}

# The shape `agentic_hil.bridge` builds its types in: a prefix off the session
# class and a kind from the call site, neither of them the type.
BRIDGE_MUTANT = """
    class _MutantBridge:
        error_prefix = "bridge"

        def _mutant_error(self, kind):
            return {"ok": False, "error_type": f"{self.error_prefix}_{kind}"}

        def _mutant(self):
            return self._mutant_error("mutant_refusal")
    """

UNRESOLVED_MUTANT = """
    def _mutant(source):
        return {"ok": False, "error_type": source.kind}
    """


def scan_with(module: ModuleType, snippet: str, attributes: dict[str, tuple[str, ...]] | None = None) -> tuple[dict[str, list[int]], list[tuple[int, str]]]:
    """The scan of this module with `snippet` appended, resolved against its
    namespace with the snippet's own names added."""
    code = textwrap.dedent(snippet)
    namespace = dict(vars(module))
    exec(compile(code, f"<mutant of {file_name(module)}>", "exec"), namespace)
    return scan_source(module_source(module) + "\n\n" + code, namespace, attributes)


@pytest.mark.parametrize("shape", sorted(MUTANTS))
def test_a_new_refusal_in_any_shape_fails_both_guards(shape: str) -> None:
    found, unresolved = scan_with(can_module_under_test, MUTANTS[shape])

    assert any(MUTANT_TYPE in problem for problem in inventory_problems("can.py", found)), f"the pinned inventory does not see a {shape}"
    assert MUTANT_TYPE in missing_entries(found), f"the entry guard does not see a {shape}"
    # Read, not merely flagged: the scan resolved the type rather than reporting
    # an expression it could not follow.
    assert not unresolved_problems("can.py", unresolved), unresolved_problems("can.py", unresolved)


def test_a_new_bridge_refusal_built_from_its_prefix_fails_both_guards() -> None:
    found, _ = scan_with(bridge_module_under_test, BRIDGE_MUTANT, bridge_attributes())

    assert any("can_adapter_mutant_refusal" in problem for problem in inventory_problems("bridge.py", found)), sorted(found)
    assert "can_adapter_mutant_refusal" in missing_entries(found), sorted(found)


def test_a_refusal_whose_type_the_scan_cannot_read_fails_the_unresolved_guard() -> None:
    found, unresolved = scan_with(can_module_under_test, UNRESOLVED_MUTANT)

    assert not inventory_problems("can.py", found)
    assert any("source.kind" in problem for problem in unresolved_problems("can.py", unresolved)), unresolved


# ---------------------------------------------------------------------------
# Resolution: the URI a caller reads, and the fields a refusal carries.


@pytest.fixture
def reference(tmp_path: Path) -> Iterator[AgenticHILToolService]:
    tools = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        yield tools
    finally:
        tools.close()


def read_reference(tools: AgenticHILToolService, uri: str) -> dict:
    response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}}, tools)
    assert isinstance(response, dict) and "error" not in response, response
    contents = response["result"]["contents"]
    assert len(contents) == 1 and contents[0]["uri"] == uri, contents
    return json.loads(contents[0]["text"])


def served_entry(key: str) -> dict:
    """The entry `resources/read` serves under this catalogue key."""
    uri = ERROR_URI_PREFIX + key
    response = read_resource_response(1, {"uri": uri})
    assert "result" in response, f"{uri} does not resolve: {response}"
    contents = response["result"]["contents"]
    assert len(contents) == 1 and contents[0]["uri"] == uri, contents
    return json.loads(contents[0]["text"])


@pytest.mark.parametrize("error_type", REQUIRED)
def test_the_entry_resolves_at_its_reference_uri(reference: AgenticHILToolService, error_type: str) -> None:
    assert error_type in ERROR_CATALOGUE, f"no catalogue entry for {error_type}"

    served = read_reference(reference, ERROR_URI_PREFIX + error_type)

    assert served == catalogue_entry(error_type), served
    assert served["meaning"].strip(), served
    assert served["remediation"] and all(step.strip() for step in served["remediation"]), served
    assert served.get("do_not") and all(step.strip() for step in served["do_not"]), served


@pytest.mark.parametrize("error_type", REQUIRED)
def test_remediation_fields_hand_a_refusal_the_entry(error_type: str) -> None:
    assert error_type in ERROR_CATALOGUE, f"no catalogue entry for {error_type}"
    entry = catalogue_entry(error_type)
    assert entry is not None

    fields = remediation_fields(error_type)

    if error_type == "permission_denied":
        # The one entry written around the key a refusal is about: without that
        # key the fields are empty by design (`_needs_a_permission_key`, #443).
        fields = remediation_fields(error_type, permission="permissions.allow_write")
        assert fields["remediation"] and fields["do_not"], fields
        return
    assert fields == {"remediation": entry["remediation"], "do_not": entry["do_not"]}, fields


def assert_carries_its_entry(result: dict, error_type: str, scope: str | None = None) -> None:
    """The refusal carries, whole, the entry the call site asks for, and that is
    the entry a caller reading the reference is served.

    `scope` is the one the call site passes: an entry answers for its scoped
    key when one is written and for the bare key otherwise, and the refusal has
    to match the entry that answers, not any entry of its type."""
    assert result.get("ok") is False and result.get("error_type") == error_type, result
    expected = remediation_fields(error_type, scope)
    assert expected, f"no catalogue entry answers {error_type}{':' + scope if scope else ''}, so its refusal carries no remediation"
    carried = {key: result.get(key) for key in ("remediation", "do_not")}
    assert carried == {"remediation": expected.get("remediation"), "do_not": expected.get("do_not")}, f"{error_type} carries {carried!r}, not its entry {expected!r}"
    for key, steps in carried.items():
        assert isinstance(steps, list) and steps and all(isinstance(step, str) and step.strip() for step in steps), f"{error_type}: `{key}` holds no steps: {steps!r}"
    scoped_key = f"{error_type}:{scope}" if scope else None
    key = scoped_key if scoped_key in ERROR_CATALOGUE else error_type
    served = served_entry(key)
    assert served["remediation"] == carried["remediation"] and served.get("do_not") == carried["do_not"], f"{key} is served as {served!r}"


# ---------------------------------------------------------------------------
# Content: what each new entry has to say, and what it must never say.

TOKEN = re.compile(r"[A-Za-z0-9_\[\]-]+")
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")


def any_of(*words: str) -> str:
    return r"\b(?:" + "|".join(words) + r")\b"


# The claims an entry must never make, each one a whole sentence read at once so
# that the same words with the meaning turned round ("does not settle", "is not
# safe") do not count against it.
#
# That a retried stop or start settles a bridge close that was never confirmed:
# the first call that meets the ended bridge answers once and gives the bus back,
# so a retry has nothing left to settle (#633).
RETRY_SETTLES = r"^(?!.*\b(?:not|never|nothing|cannot|no longer|keeps? (?:answering|failing|refusing)|fails? again)\b)(?=.*\b(?:bridge|safe state)\b)(?=.*\b(?:again|retry|retries|retried|retrying|second|repeat\w*)\b)(?=.*\b(?:clears?|releases?|closes?|settles?|succeeds?|frees?|confirms?|recovers?)\b)"
# That sending again is safe, said without the condition that makes it so.
RESEND_IS_SAFE = r"^(?!.*\b(?:not|never|unless|only|when|if|after)\b)(?=.*\b(?:send|sending|resend|resending|retry|retrying|repeat|again)\b)(?=.*\bsafe\b)"
# That the bus keeps running after a bus-scoped incident.
BUS_KEEPS_RUNNING = r"^(?!.*\b(?:not|no longer|never)\b)(?=.*\bbus\b)(?=.*\bkeeps? (?:running|working|carrying)\b)"
# That a participant's own incident gated the bus.
BUS_GATED = r"^(?!.*\b(?:not|no|never|unlike)\b)(?=.*\bbus\b)(?=.*\bgated\b)"
# That a participant's own incident aborted every participant.
EVERY_PARTICIPANT_ABORTED = r"^(?!.*\b(?:not|only|unlike)\b)(?=.*\b(?:every|all)\b)(?=.*\bparticipants?\b)(?=.*\babort\w*)"
# That nothing of a request whose write failed reached the bridge.
NOTHING_SENT = r"^(?!.*\b(?:may|might|unless|part|partly)\b)(?=.*\bnothing\b)(?=.*\b(?:sent|reached|written|delivered)\b)"
# That the broker's key is something to copy, edit or replace.
KEY_COPY = r"^(?!.*\b(?:not|never)\b)(?=.*\b(?:copy|copying|edit|replace)\b)(?=.*\bkey\b)"
# That a lock, descriptor or socket file is something to delete.
DELETE_LOCK = r"^(?!.*\b(?:not|never)\b)(?=.*\b(?:delete|remove)\b)(?=.*\b(?:descriptor|lock|socket)\b)"


def spec(*, first: str | None = None, names: tuple[str, ...] = (), says: tuple[tuple[str, ...], ...] = (), never: tuple[str, ...] = ()) -> dict:
    """What one entry has to say.

    `first`: a name the first step carries in backticks, because the first step
    is the one that fixes the case that actually occurs and it starts from the
    field of the refusal that tells which case that is. `names`: names some step
    carries in backticks. `says`: relations, each a set of patterns one sentence
    of the entry (meaning, steps or do_not) must match together. `never`:
    claims no sentence of the meaning or the steps may match.
    """
    return {"first": first, "names": names, "says": says, "never": never}


CONTENT: dict[str, dict] = {
    "bridge_process_reap_failed": spec(
        first="backend_error",
        names=("backend_error", "cleanup_error", "command", "can_session_start"),
        says=((any_of("may"), any_of("still", "running")), (any_of("can_session_start"), any_of(r"retr\w*", "teardown", "tears", "tear"))),
    ),
    "bridge_safe_state_unconfirmed": spec(
        first="close_response",
        names=("close_response", "safe_state_confirmed", "can_session_stop", "can_session_start"),
        says=((any_of("ended", "terminated", "reaped", "gone"), any_of("nothing")), (any_of("given", "gives"), any_of("back"))),
        never=(RETRY_SETTLES,),
    ),
    "can_adapter_close_failed": spec(
        first="backend_error",
        names=("backend_error", "can_session_stop", "can_session_start", "can_buses_list", "adapter_status"),
        says=(
            (any_of("bridge"), any_of("once"), any_of("back")),
            (any_of("check"), any_of("bench"), any_of("can_session_start")),
            (any_of("socketcan", "peak", "direct"), any_of("can_session_stop"), any_of("again", r"retr\w*")),
        ),
        never=(RETRY_SETTLES,),
    ),
    "can_adapter_close_interrupted": spec(
        first="cleanup_error",
        names=("cleanup_error", "bridge_safe_state_unconfirmed", "stderr_tail"),
        says=((any_of("close"), any_of("raised", "interrupted")),),
        never=(RETRY_SETTLES,),
    ),
    "can_adapter_invalid_request": spec(
        first="stderr_tail",
        names=("stderr_tail", "side_effect_status", "can_session_stop"),
        says=((any_of("stdin"),), (any_of("part", "partly", "partway", "midway"), any_of("may", "might"))),
        never=(NOTHING_SENT,),
    ),
    "can_adapter_not_found": spec(
        first="can_buses",
        names=("can_buses", "can_session_start"),
        says=((any_of("relative"), any_of("workspace", "work_dir")),),
    ),
    "can_adapter_open_failed": spec(
        first="backend_error",
        names=("backend_error", "likely_causes", "can_session_start"),
        says=((any_of("another", "other"), any_of("program", "process", "application", "server"), any_of("adapter")),),
    ),
    "can_adapter_process_exited": spec(
        first="stderr_tail",
        names=("stderr_tail", "can_session_stop", "can_adapter_close_failed"),
        says=((any_of("can_session_stop"), any_of("can_adapter_close_failed"), any_of("once"), any_of("back")),),
        never=(RETRY_SETTLES,),
    ),
    "can_adapter_process_start_failed": spec(
        first="backend_error",
        says=((any_of("interpreter"),),),
    ),
    "can_adapter_timeout": spec(
        first="side_effect_status",
        names=("side_effect_status", "stderr_tail", "resource_quarantined"),
        says=((any_of("delivered", "sent", "reached", "acted"), any_of("unknown")),),
        never=(RESEND_IS_SAFE,),
    ),
    "can_backend_not_available": spec(
        first="backend_error",
        says=((r"agentic-hil\[can\]", any_of("environment")), (any_of("process"), any_of("python-can"))),
    ),
    "can_broker_authentication_failed": spec(
        names=("backend_error",),
        says=((any_of("key"), any_of("descriptor", "broker")),),
        never=(KEY_COPY,),
    ),
    "can_broker_counter_mismatch": spec(
        names=("broker_counter", "client_counter", "retry_safe"),
        says=((any_of("already", "retried"), any_of("deadline")),),
    ),
    "can_broker_disconnected": spec(
        first="backend_error",
        names=("side_effect_status", "resource_quarantined", "can_session_stop", "can_session_start"),
        says=((any_of("exited", "closed"), any_of("broker")), (any_of("audit"), any_of("no", "not"))),
        never=(RESEND_IS_SAFE,),
    ),
    "can_broker_invalid_message": spec(
        first="summary",
        says=((any_of("every", "all"), any_of("participant", "participants"), r"\bexit\w*"),),
    ),
    "can_broker_not_bus_owner": spec(
        first="bus_lock_held",
        names=("bus_lock_holder", "claimed_broker_pid"),
        says=((any_of("different"), any_of("owner")),),
        never=(DELETE_LOCK,),
    ),
    "can_broker_protocol_mismatch": spec(
        names=("broker_protocol_digest", "client_protocol_digest"),
        says=((any_of("different"), any_of("release", "version")),),
    ),
    "can_broker_stopping": spec(
        names=("retry_safe", "can_session_start"),
        says=((any_of("fresh"), any_of("broker")),),
    ),
    "can_broker_timeout": spec(
        first="side_effect_status",
        names=("side_effect_status", "resource_quarantined", "can_session_stop", "wait_timeout_s"),
        says=((any_of("late"), any_of("next")), (any_of("unknown"), any_of("bus"))),
        never=(RESEND_IS_SAFE,),
    ),
    "can_broker_unavailable": spec(
        first="summary",
        names=("backend_error", "broker_log", "broker_start_timeout_s", "bus_timeout_s"),
        says=((any_of("broker_log"), any_of("every", "all")), (any_of("already"), r"\bretr\w*", any_of("deadline"))),
        never=(DELETE_LOCK,),
    ),
    "can_broker_wrong_bus": spec(
        names=("broker_bus_key", "client_bus_key"),
        says=((any_of("different"), any_of("bus")),),
    ),
    "can_bus_gated": spec(
        first="incident",
        names=("can_session_stop",),
        says=((any_of("every", "all"), r"\bdetach\w*", r"\bexit\w*"),),
    ),
    "can_bus_incident": spec(
        first="abort",
        names=("aborted_participants", "bus_gated"),
        says=((any_of("every", "all"), any_of("participant", "participants"), r"\babort\w*"),),
        never=(BUS_KEEPS_RUNNING,),
    ),
    "can_bus_not_configured": spec(
        first="configured_buses",
        names=("can_buses_list", "can_buses"),
    ),
    "can_bus_not_shared": spec(
        names=("shares", "participant"),
        says=((any_of("without"), any_of("participant")), (any_of("changed", "edited"), any_of("after", "since"))),
    ),
    "can_listen_only_conflict": spec(
        first="conflicting_participants",
        names=("requires_listen_only", "allow_write", "listen_only"),
    ),
    "can_participant_busy": spec(
        names=("can_session_stop", "retry_safe"),
        says=((any_of("already", "still"), any_of("attached")), (r"\bretr\w*", any_of("deadline"))),
    ),
    "can_participant_filter_violation": spec(
        first="view",
        names=("frame", "extended"),
        says=((any_of("extended"), any_of("standard")), (any_of("not", "never"), any_of("sent"))),
    ),
    "can_participant_frame_budget_exhausted": spec(
        first="max_frames",
        names=("frames_used", "can_session_stop", "can_session_start"),
        says=((any_of("bus"), r"\bkeeps? (?:running|working|carrying)\b"),),
        never=(BUS_GATED,),
    ),
    "can_participant_incident": spec(
        first="abort",
        names=("can_session_stop", "can_session_start"),
        says=((any_of("only"), any_of("participant")),),
        never=(EVERY_PARTICIPANT_ABORTED,),
    ),
    "can_participant_lock_required": spec(
        first="participant_lock",
        names=("retry_safe", "can_session_start"),
        says=((any_of("restart"), any_of("server")),),
    ),
    "can_participant_not_configured": spec(
        first="configured_participants",
        names=("shares",),
    ),
    "can_participant_required": spec(
        first="configured_participants",
        says=((any_of("every"), any_of("call"), any_of("participant")),),
    ),
    "can_queue_clear_failed": spec(
        names=("backend_result", "frames_drained", "clear_rx_queue", "retry_safe"),
        says=((any_of("frames_drained"), any_of("discarded", "lost", "dropped")),),
    ),
    "can_queue_clear_limit": spec(
        first="frames_drained",
        names=("clear_rx_queue", "until_id", "can_read"),
        says=((any_of("clear_rx_queue"), any_of("false")),),
    ),
    "can_read_failed": spec(
        first="backend_error",
        names=("retry_safe",),
        says=((any_of("nothing"), any_of("transmitted", "sent")),),
    ),
    "can_send_failed": spec(
        first="side_effect_status",
        names=("not_started", "unknown", "backend_error"),
        says=((any_of("not_started"), any_of("again"), any_of("safe")), (any_of("unknown"), any_of("wire", "bus"), any_of("may"))),
        never=(RESEND_IS_SAFE,),
    ),
}


def sentences(*texts: str) -> list[str]:
    return [sentence for text in texts for sentence in SENTENCE_BREAK.split(text) if sentence.strip()]


def backticked_tokens(text: str) -> set[str]:
    return {token for span in re.findall(r"`([^`]+)`", text) for token in TOKEN.findall(span)}


def content_problems(error_type: str, remedy: ErrorRemedy) -> list[str]:
    """Where this entry falls short of what its type has to say, read off the
    text a caller is served."""
    required = CONTENT[error_type]
    entry = remedy.as_json()
    meaning, steps, do_not = entry["meaning"], entry["remediation"], entry.get("do_not", [])
    problems: list[str] = []
    if not do_not:
        problems.append("do_not: names no wrong fix")
    if not steps:
        return [*problems, "remediation: no steps"]
    if required["first"] and required["first"] not in backticked_tokens(steps[0]):
        problems.append(f"first: the first step does not name `{required['first']}`: {steps[0]!r}")
    named = set().union(*(backticked_tokens(step) for step in steps))
    problems.extend(f"names: no step names `{name}`" for name in required["names"] if name not in named)
    everything = sentences(meaning, *steps, *do_not)
    for patterns in required["says"]:
        if not any(all(re.search(pattern, sentence, re.IGNORECASE) for pattern in patterns) for sentence in everything):
            problems.append(f"says: no sentence relates {patterns}")
    for pattern in required["never"]:
        problems.extend(f"never: {sentence!r}" for sentence in sentences(meaning, *steps) if re.search(pattern, sentence, re.IGNORECASE))
    return problems


@pytest.mark.parametrize("error_type", sorted(NEW_ENTRIES))
def test_every_new_entry_says_what_its_refusal_means(error_type: str) -> None:
    assert error_type in ERROR_CATALOGUE, f"no catalogue entry for {error_type}"

    problems = content_problems(error_type, ERROR_CATALOGUE[error_type])

    assert not problems, f"{error_type}:\n" + "\n".join(f"  {problem}" for problem in problems)


def test_every_new_entry_has_a_specification_of_its_own() -> None:
    assert set(CONTENT) == NEW_ENTRIES, sorted(set(CONTENT) ^ NEW_ENTRIES)
    # The two tools every CAN refusal could name are no evidence that an entry
    # was written for its own failure.
    assert not {specification["first"] for specification in CONTENT.values()} & {"can_session_start", "can_buses_list"}


GENERIC = ErrorRemedy(
    meaning="The CAN tool refused the call.",
    remediation=("Call `can_buses_list`, then `can_session_start` again.",),
    do_not=("Do not open the CAN adapter directly.",),
)


@pytest.mark.parametrize("error_type", sorted(NEW_ENTRIES))
def test_advice_that_fits_any_refusal_fits_none(error_type: str) -> None:
    assert content_problems(error_type, GENERIC), f"the generic entry passes as {error_type}"


VALID_CLOSE_FAILED = ErrorRemedy(
    meaning="The adapter of a CAN session did not close.",
    remediation=(
        "Read `backend_error`.",
        "A bridge that did not confirm safe state cannot confirm it later, so this is answered once and the bus is given back.",
        "Check the bench, then call `can_session_start`.",
        "On a direct adapter (`socketcan`, `peak`), call `can_session_stop` again.",
        "`can_buses_list` shows the session and its `adapter_status`.",
    ),
    do_not=("Do not open the adapter yourself.",),
)
RETRY_PROMISE = "Call `can_session_stop` again after a bridge close fails: the retry closes the session and releases the bus."

VALID_SEND_FAILED = ErrorRemedy(
    meaning="The adapter did not send the frame.",
    remediation=(
        "Read `side_effect_status`: when it is `not_started`, nothing reached the bus and sending the frame again is safe.",
        "When it is `unknown`, the frame may be on the bus; read `backend_error` and check the bus before sending again.",
    ),
    do_not=("Do not send the frame again before reading the status.",),
)
RESEND_PROMISE = "Sending the frame again is safe."


def with_step(remedy: ErrorRemedy, step: str) -> ErrorRemedy:
    return ErrorRemedy(meaning=remedy.meaning, remediation=(*remedy.remediation, step), do_not=remedy.do_not)


@pytest.mark.parametrize(("error_type", "valid", "wrong"), [("can_adapter_close_failed", VALID_CLOSE_FAILED, RETRY_PROMISE), ("can_send_failed", VALID_SEND_FAILED, RESEND_PROMISE)])
def test_an_entry_that_makes_the_forbidden_claim_fails_on_that_claim_alone(error_type: str, valid: ErrorRemedy, wrong: str) -> None:
    """The specification accepts an entry that states the relations, and
    rejects the same entry once it also makes the claim the code disproves,
    for that claim and nothing else."""
    assert content_problems(error_type, valid) == []

    problems = content_problems(error_type, with_step(valid, wrong))

    assert problems and all(problem.startswith("never:") for problem in problems), problems


def test_no_two_new_entries_give_the_same_advice() -> None:
    """An entry copied between types is advice written for neither."""
    assert not NEW_ENTRIES - set(ERROR_CATALOGUE), sorted(NEW_ENTRIES - set(ERROR_CATALOGUE))
    meanings: dict[str, str] = {}
    steps: dict[str, str] = {}
    for error_type in sorted(NEW_ENTRIES):
        remedy = ERROR_CATALOGUE[error_type]
        assert remedy.meaning not in meanings, f"{error_type} and {meanings.get(remedy.meaning)} share a meaning"
        meanings[remedy.meaning] = error_type
        for step in remedy.remediation:
            assert step not in steps, f"{error_type} and {steps.get(step)} share a step: {step!r}"
            steps[step] = error_type


# The catalogue itself is left out: a step whose name only the catalogue
# spells would otherwise vouch for itself.
PACKAGE_SOURCE = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted(Path(inspect.getsourcefile(can_module_under_test) or "").parent.rglob("*.py"))
    if path.name != "knowledge.py"
)
# A name with an underscore in it is a tool, a field or a config key, which is
# what a step has to name. Words without one (`channel`, `ip link set`) are
# prose or commands and are not checked.
NAMED_THING = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")


@pytest.mark.parametrize("error_type", sorted(NEW_ENTRIES))
def test_every_new_step_names_something_the_code_has(error_type: str) -> None:
    """Each step names a tool, a field or a config key in backticks, and every
    such name is one the package spells, so a step cannot send the reader to a
    tool that does not exist. Over the new entries only: three existing CAN
    entries have a step that names nothing in backticks, and rewriting them is
    not this change."""
    assert error_type in ERROR_CATALOGUE, f"no catalogue entry for {error_type}"
    remedy = ERROR_CATALOGUE[error_type]

    for step in remedy.remediation:
        spans = re.findall(r"`([^`]+)`", step)
        assert spans, f"{error_type}: this step names no tool, field or config key: {step!r}"
        for span in spans:
            for token in re.split(r"[.\s]+", span):
                if NAMED_THING.fullmatch(token):
                    assert token in PACKAGE_SOURCE, f"{error_type}: `{token}` is named by no code in the package: {step!r}"


# ---------------------------------------------------------------------------
# Behaviour: the fields arrive in the payload of a real refusal, per tool.

DIRECT_BUS = "catalogue_direct_bus"
DIRECT_CHANNEL = "can635direct"
PROCESS_BUS = "catalogue_process_bus"
PROCESS_CHANNEL = "can635bridge"
SHARED_BUS = "catalogue_shared_bus"


def can_config(tmp_path: Path, *, bridge_exists: bool = False):
    """One direct bus, one bridge bus and one shared bus with two participants.

    Channel names are distinctive on purpose, as in `test_can_likely_causes`:
    nothing here may match an interface a host happens to have.
    """
    executable = tmp_path / "can-bridge.py"
    if bridge_exists:
        executable.write_text("", encoding="utf-8")
    yaml = "".join(
        [
            "can_buses:\n",
            f"  {DIRECT_BUS}:\n",
            '    adapter: "socketcan"\n',
            f'    channel: "{DIRECT_CHANNEL}"\n',
            "    max_buffer_frames: 2\n",
            f"  {PROCESS_BUS}:\n",
            '    adapter: "process"\n',
            f'    channel: "{PROCESS_CHANNEL}"\n',
            f'    executable: "{executable.as_posix()}"\n',
            f"  {SHARED_BUS}:\n",
            "    adapter: process\n",
            "    channel: fake-can\n",
            "    executable: fake-bridge\n",
            "    shares:\n",
            "      ecu_a:\n",
            "        permissions:\n",
            "          allow_read: true\n",
            "          allow_write: true\n",
            "      ecu_b:\n",
            "        permissions:\n",
            "          allow_read: true\n",
            "          allow_write: true\n",
        ]
    )
    return load_config(str(write_config(tmp_path, can_buses_yaml=yaml)))


class StagedInitializationError(Exception):
    pass


class StagedBus:
    """A python-can bus whose receive, send and shutdown fail, or whose receive
    never runs dry, on cue."""

    def __init__(self) -> None:
        self.fail_recv = False
        self.fail_send = False
        self.fail_shutdown = False
        self.never_empty = False

    def recv(self, timeout: float | None = None) -> object:
        if self.fail_recv:
            raise OSError("staged receive failure")
        if self.never_empty:
            return SimpleNamespace(arbitration_id=0x123, is_extended_id=False, is_remote_frame=False, data=b"\x00", dlc=1)
        return None

    def send(self, message: object, timeout: float | None = None) -> None:
        if self.fail_send:
            raise OSError("staged send failure")

    def shutdown(self) -> None:
        if self.fail_shutdown:
            raise OSError("staged shutdown failure")


def install_python_can(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bus: StagedBus | None = None, *, open_error: BaseException | None = None) -> None:
    """A python-can of this test's own, and an empty `/sys/class/net` beside it."""
    sysfs = tmp_path / "sys" / "class" / "net"
    sysfs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(can_module_under_test, "SYSFS_NET_CLASS", str(sysfs), raising=False)

    def open_bus(**kwargs: object) -> StagedBus:
        if open_error is not None:
            raise open_error
        assert bus is not None
        return bus

    monkeypatch.setitem(sys.modules, "can", SimpleNamespace(Bus=open_bus, CanInitializationError=StagedInitializationError, Message=lambda **kwargs: SimpleNamespace(**kwargs)))


class UnconfirmedLease(DetachedHardwareLease):
    """A lease whose release does not confirm, the way a real one answers after
    a quarantine it cannot clear itself."""

    def release(self, **_: object) -> bool:
        return False


class ClosingAdapter:
    """An adapter session that closes cleanly."""

    adapter_name = "fake"

    def read(self, max_frames: int, wait_timeout_s: float) -> dict:
        return {"ok": True, "frames": []}

    def status(self) -> dict:
        return {"active": True}

    def close(self) -> dict:
        return {"ok": True, "safe_state_confirmed": True, "process_reaped": True}


class RefusingParticipant(SharedFakeParticipant):
    """A broker participant that answers its send and its read with one refusal,
    shaped the way the broker writes it."""

    refusal: dict = {}

    def send(self, frame_id: int, data: bytes, *, extended: bool = False, rtr: bool = False) -> dict:
        return dict(self.refusal)

    def read(self, max_frames: int, wait_timeout_s: float) -> dict:
        return dict(self.refusal)

    def status(self) -> dict:
        return {"ok": True, "participant": self.name, "abort": None, "bus_gated": False, "attached_participants": ["ecu_a", "ecu_b"]}


def broker_refusal(error_type: str, participant: str) -> dict:
    refusal = {"ok": False, "error_type": error_type, "summary": "staged broker refusal", "bus_id": SHARED_BUS, "participant": participant, "retry_safe": False, "side_effect_committed": False}
    if error_type == "can_bus_incident":
        refusal.update({"abort": {"scope": "bus"}, "bus_gated": True})
    elif error_type in ("can_participant_incident", "can_participant_frame_budget_exhausted"):
        refusal["abort"] = {"scope": "participant"}
    return refusal


# --- can_buses_list ---------------------------------------------------------


def test_the_bus_listing_has_no_refusal_of_its_own(tmp_path: Path) -> None:
    """`CanBusService.list_buses` answers `ok: true` for any configuration, so it
    has no error type to carry an entry. Recorded here so the inventory says why
    one of the five tools is absent from it."""
    service = AgenticHILToolService(can_config(tmp_path))
    try:
        listed = service.call("can_buses_list")
    finally:
        service.close()

    assert listed["ok"] is True and "error_type" not in listed, listed


# --- can_session_start ------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "error_type"),
    [
        ({"bus_id": "no_such_bus_in_this_config"}, "can_bus_not_configured"),
        ({"bus_id": SHARED_BUS}, "can_participant_required"),
        ({"bus_id": SHARED_BUS, "participant": "ecu_z"}, "can_participant_not_configured"),
        ({"bus_id": PROCESS_BUS}, "can_adapter_not_found"),
    ],
)
def test_a_session_start_refused_by_the_configuration_carries_its_entry(tmp_path: Path, arguments: dict, error_type: str) -> None:
    service = AgenticHILToolService(can_config(tmp_path))
    try:
        result = service.call("can_session_start", arguments)
    finally:
        service.close()

    assert_carries_its_entry(result, error_type)


def test_a_session_start_without_python_can_carries_its_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path)
    monkeypatch.setitem(sys.modules, "can", None)
    service = AgenticHILToolService(config)
    try:
        result = service.call("can_session_start", {"bus_id": DIRECT_BUS})
    finally:
        service.close()

    assert_carries_its_entry(result, "can_backend_not_available")


def test_a_direct_adapter_that_will_not_open_carries_its_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path)
    install_python_can(tmp_path, monkeypatch, open_error=StagedInitializationError("staged open failure"))
    service = AgenticHILToolService(config)
    try:
        result = service.call("can_session_start", {"bus_id": DIRECT_BUS})
    finally:
        service.close()

    assert_carries_its_entry(result, "can_adapter_open_failed")


def test_a_bridge_process_that_will_not_start_carries_its_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path, bridge_exists=True)

    def refuse_to_spawn(*args: object, **kwargs: object) -> object:
        raise OSError("staged spawn failure")

    monkeypatch.setattr(can_module_under_test, "spawn_managed_process", refuse_to_spawn)
    service = AgenticHILToolService(config)
    try:
        result = service.call("can_session_start", {"bus_id": PROCESS_BUS})
    finally:
        service.close()

    assert_carries_its_entry(result, "can_adapter_process_start_failed")


@pytest.mark.parametrize(("stage", "error_type"), [("fail_recv", "can_queue_clear_failed"), ("never_empty", "can_queue_clear_limit")])
def test_a_receive_queue_that_will_not_clear_carries_its_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, error_type: str) -> None:
    config = can_config(tmp_path)
    bus = StagedBus()
    setattr(bus, stage, True)
    install_python_can(tmp_path, monkeypatch, bus)
    service = AgenticHILToolService(config)
    try:
        result = service.call("can_session_start", {"bus_id": DIRECT_BUS, "clear_rx_queue": True})
    finally:
        service.close()

    assert_carries_its_entry(result, error_type)


def test_a_start_whose_adapter_will_not_close_after_a_failed_clear_carries_its_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The queue would not clear, and the adapter then would not shut down: the
    session stays registered and the start answers `can_adapter_close_failed`."""
    config = can_config(tmp_path)
    bus = StagedBus()
    bus.fail_recv = True
    bus.fail_shutdown = True
    install_python_can(tmp_path, monkeypatch, bus)
    service = AgenticHILToolService(config)
    try:
        result = service.call("can_session_start", {"bus_id": DIRECT_BUS, "clear_rx_queue": True})
    finally:
        bus.fail_shutdown = False
        service.close()

    assert result.get("summary") == "CAN initialization failed and the session remains registered for cleanup retry.", result
    assert_carries_its_entry(result, "can_adapter_close_failed")


def test_a_start_whose_lease_will_not_release_after_a_failed_clear_carries_its_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter closed after the failed clear, and the lease release did not
    confirm: `can_adapter_close_failed` with no `backend_error`."""
    config = can_config(tmp_path)
    bus = StagedBus()
    bus.fail_recv = True
    install_python_can(tmp_path, monkeypatch, bus)
    service = can_module_under_test.CanBusService(config)
    monkeypatch.setattr(service.coordinator, "acquire", lambda *resources, **kwargs: UnconfirmedLease())
    try:
        result = service.session_start(DIRECT_BUS, True)
    finally:
        service.sessions.pop((DIRECT_BUS, None), None)
        service.close()

    assert result.get("summary") == "CAN lease release remained unconfirmed." and "backend_error" not in result, result
    assert_carries_its_entry(result, "can_adapter_close_failed")


# The refusals `attach_participant` raises as a `ParticipantError`, which
# `_participant_session_start` turns into the result. Most are the broker's own;
# `can_adapter_timeout` stands for the adapter refusal a broker that could not
# open its adapter leaves behind, which `_explained_exit_refusal` keeps by name.
ATTACH_REFUSALS = (
    "can_broker_authentication_failed",
    "can_broker_counter_mismatch",
    "can_broker_invalid_message",
    "can_broker_not_bus_owner",
    "can_broker_protocol_mismatch",
    "can_broker_stopping",
    "can_broker_unavailable",
    "can_broker_wrong_bus",
    "can_bus_gated",
    "can_bus_not_configured",
    "can_bus_not_shared",
    "can_listen_only_conflict",
    "can_participant_busy",
    "can_participant_lock_required",
    "can_participant_not_configured",
    "can_adapter_timeout",
)
# What the broker answers a participant's send and read with.
SEND_REFUSALS = ("can_participant_filter_violation", "can_participant_frame_budget_exhausted", "can_participant_incident", "can_bus_incident", "can_send_failed")
READ_REFUSALS = ("can_participant_incident", "can_bus_incident")
# What a send or read meets when the request to the broker fails in transport:
# `Participant._request` raises them, and the broker session answers them (#664).
TRANSPORT_FAILURES = ("can_broker_timeout", "can_broker_invalid_message", "can_broker_disconnected")


def test_every_broker_refusal_has_a_behavioural_carrier() -> None:
    """Every CAN type the broker writes is driven through a tool below, so none
    of them can have an entry that no refusal carries."""
    broker_types = {error_type for error_type in PINNED_INVENTORY["canbroker.py"] if error_type.startswith("can_") and error_type in REQUIRED}

    carried = {*ATTACH_REFUSALS, *SEND_REFUSALS, *READ_REFUSALS, *TRANSPORT_FAILURES}
    assert broker_types <= carried, sorted(broker_types - carried)


@pytest.mark.parametrize("error_type", ATTACH_REFUSALS)
def test_a_participant_attach_the_broker_refuses_carries_its_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: str) -> None:
    def refuse(config: object, bus_id: str, participant: str, **kwargs: object) -> object:
        raise ParticipantError(broker_refusal(error_type, participant))

    monkeypatch.setattr(canbroker_module_under_test, "attach_participant", refuse)
    service = AgenticHILToolService(can_config(tmp_path))
    try:
        result = service.call("can_session_start", {"bus_id": SHARED_BUS, "participant": "ecu_a", "clear_rx_queue": False})
    finally:
        service.close()

    assert_carries_its_entry(result, error_type)


def test_a_broker_that_could_not_load_its_configuration_hands_on_the_configuration_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The real path a configuration type takes to a CAN caller.

    The broker's own `main` runs against a project with no configuration and
    prints what stopped it, bare; that document is the broker log the client
    reads when the broker exits with `BROKER_EXIT_CONFIG`, and
    `_explained_exit_refusal` turns it into the participant's refusal. The
    entry is attached on the CAN side, because the broker attached none."""
    config, _, log_path = prepared_bus(tmp_path, monkeypatch, "can635cfg")
    configured = os.environ["AGENTIC_HIL_CONFIG"]
    unconfigured = tmp_path / "unconfigured"
    unconfigured.mkdir()
    monkeypatch.delenv("AGENTIC_HIL_CONFIG")
    capsys.readouterr()
    exit_code = canbroker_module_under_test.main(["--workspace-root", str(unconfigured), "--bus-id", DEADLINE_BUS_ID])
    printed = capsys.readouterr().err
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", configured)
    document = json.loads(printed.strip().splitlines()[-1])
    assert exit_code == BROKER_EXIT_CONFIG and document["error_type"] == "config_file_not_found", (exit_code, printed)
    assert "remediation" not in document, document
    log_path.write_text(printed, encoding="utf-8")

    clock = FakeClock()
    monkeypatch.setattr(canbroker_module_under_test, "time", clock)
    monkeypatch.setattr(canbroker_module_under_test, "_spawn_broker", lambda *args, **kwargs: ScriptedBroker(clock, dies_at=0.0, exit_code=BROKER_EXIT_CONFIG))
    service = can_module_under_test.CanBusService(config)
    try:
        result = service.session_start(DEADLINE_BUS_ID, False, DEADLINE_PARTICIPANT)
    finally:
        service.close()

    assert result.get("broker_exit_code") == BROKER_EXIT_CONFIG, result
    assert_carries_its_entry(result, "config_file_not_found")


class ScriptedStdin(io.StringIO):
    """The stdin of a bridge child that fails its writes, in order, as staged."""

    def __init__(self, *failures: BaseException | None) -> None:
        super().__init__()
        self.failures = list(failures)

    def write(self, text: str) -> int:
        failure = self.failures.pop(0) if self.failures else None
        if failure is not None:
            raise failure
        return super().write(text)


class AnsweringChild:
    """A bridge child that answers each request with the result staged for its
    method, until it is ended."""

    def __init__(self, answers: dict[str, dict]) -> None:
        self.answers = answers
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.stdin = self
        self.stdout = iter(self.lines.get, None)
        self.stderr = iter(())
        self.exit_code: int | None = None

    def write(self, text: str) -> int:
        request = json.loads(text)
        self.lines.put(json.dumps({"id": request["id"], "result": self.answers[request["method"]]}) + "\n")
        return len(text)

    def flush(self) -> None:
        return None

    def poll(self) -> int | None:
        return self.exit_code

    def end(self) -> None:
        if self.exit_code is None:
            self.exit_code = 0
            self.lines.put(None)


def end_child(child: object, timeout_s: float) -> None:
    """`terminate_process_tree` for a staged child: there is no process to end."""
    end = getattr(child, "end", None)
    if callable(end):
        end()


def refuse_to_reap(child: object, timeout_s: float) -> None:
    raise OSError("staged reap failure")


def test_a_bridge_that_refuses_its_open_carries_the_entry_of_its_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bridge's own `ok: false` to `open`, closed cleanly afterwards: its
    type is the bridge's, and the entry is the one a `process` bus is answered
    with."""
    config = can_config(tmp_path, bridge_exists=True)
    child = AnsweringChild(
        {
            "open": {"ok": False, "error_type": "can_adapter_open_failed", "summary": "staged bridge open refusal", "backend_error": "staged"},
            "close": {"ok": True, "protocol_version": 2, "safe_state_confirmed": True},
        }
    )
    monkeypatch.setattr(can_module_under_test, "spawn_managed_process", lambda *args, **kwargs: child)
    monkeypatch.setattr(bridge_module_under_test, "terminate_process_tree", end_child)
    service = can_module_under_test.CanBusService(config)
    try:
        result = service.session_start(PROCESS_BUS, False)
    finally:
        service.close()

    assert result.get("cleanup_confirmed") is True and "cleanup_error" not in result, result
    assert_carries_its_entry(result, "can_adapter_open_failed", "process")


@pytest.mark.parametrize(
    ("stage", "error_type", "cleanup_type", "close_type"),
    [
        ("unconfirmed", "can_adapter_invalid_request", "bridge_safe_state_unconfirmed", "can_adapter_close_interrupted"),
        ("unreaped", "can_adapter_invalid_request", "bridge_process_reap_failed", "can_adapter_close_interrupted"),
        ("exited", "can_adapter_process_exited", "bridge_safe_state_unconfirmed", "can_adapter_process_exited"),
    ],
)
def test_a_bridge_that_would_neither_open_nor_close_carries_its_entry_and_names_resolving_types(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, error_type: str, cleanup_type: str, close_type: str) -> None:
    """The open refusal that keeps a registered session (`cleanup_required`).

    The top level carries its entry. The cleanup types under `cleanup_error`
    and `cleanup_error.close_response` carry none, by decision, and are the
    ones a caller looks up: each is one the guard requires and the reference
    serves."""
    config = can_config(tmp_path, bridge_exists=True)
    child = SimpleNamespace(
        poll=(lambda: 1) if stage == "exited" else (lambda: None),
        stdin=ScriptedStdin(OSError("staged pipe failure"), RuntimeError("staged close failure")),
        stdout=iter(()),
        stderr=iter(()),
    )
    monkeypatch.setattr(can_module_under_test, "spawn_managed_process", lambda *args, **kwargs: child)
    monkeypatch.setattr(bridge_module_under_test, "terminate_process_tree", refuse_to_reap if stage == "unreaped" else end_child)
    service = can_module_under_test.CanBusService(config)
    try:
        result = service.session_start(PROCESS_BUS, False)
    finally:
        # The registered session holds a lease the refusal quarantined, and its
        # child is a namespace with no process behind it; the coordinator's own
        # close is what gives the quarantined lease's locks back.
        service.sessions.pop((PROCESS_BUS, None), None)
        service.close()

    assert result.get("cleanup_required") is True, result
    cleanup_error = result["cleanup_error"]
    assert cleanup_error["error_type"] == cleanup_type and cleanup_error["close_response"]["error_type"] == close_type, cleanup_error
    assert_carries_its_entry(result, error_type, "process")
    for nested in (cleanup_type, close_type):
        assert nested in REQUIRED, nested
        served = served_entry(nested)
        assert served["remediation"] and served.get("do_not"), served


# --- can_session_stop -------------------------------------------------------


@pytest.mark.parametrize(("arguments", "error_type"), [({"bus_id": "no_such_bus_in_this_config"}, "can_bus_not_configured"), ({"bus_id": SHARED_BUS}, "can_participant_required")])
def test_a_session_stop_refused_by_the_configuration_carries_its_entry(tmp_path: Path, arguments: dict, error_type: str) -> None:
    service = AgenticHILToolService(can_config(tmp_path))
    try:
        result = service.call("can_session_stop", arguments)
    finally:
        service.close()

    assert_carries_its_entry(result, error_type)


def test_a_stop_whose_adapter_will_not_close_carries_its_entry(tmp_path: Path) -> None:
    """`can_adapter_close_failed`, on the session `CanBusService.session_stop`
    could not close."""
    config = can_config(tmp_path)
    service = can_module_under_test.CanBusService(config)
    service.sessions[(DIRECT_BUS, None)] = can_module_under_test.CanBusSession(DIRECT_BUS, config.can_buses[DIRECT_BUS], RefusingCloseAdapter(), str(tmp_path / "can-close.jsonl"))
    try:
        result = service.session_stop(DIRECT_BUS)
    finally:
        service.sessions.pop((DIRECT_BUS, None), None)
        service.close()

    assert_carries_its_entry(result, "can_adapter_close_failed")


def test_a_stop_whose_lease_will_not_release_carries_its_entry(tmp_path: Path) -> None:
    config = can_config(tmp_path)
    service = can_module_under_test.CanBusService(config)
    service.sessions[(DIRECT_BUS, None)] = can_module_under_test.CanBusSession(DIRECT_BUS, config.can_buses[DIRECT_BUS], ClosingAdapter(), str(tmp_path / "can-release.jsonl"), UnconfirmedLease())
    try:
        result = service.session_stop(DIRECT_BUS)
    finally:
        service.sessions.pop((DIRECT_BUS, None), None)
        service.close()

    assert result.get("summary") == "CAN lease release remained unconfirmed.", result
    assert_carries_its_entry(result, "can_adapter_close_failed")


def test_a_bridge_that_never_confirmed_its_close_is_answered_once_and_gives_the_bus_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#633, through the real bridge close.

    The child is gone, so the close request finds no process, safe state is
    never confirmed, and the reap of a process that already ended succeeds. A
    retried close could only ask the same ended process, so the first stop is
    final: it answers `can_adapter_close_failed` with the entry that says so,
    records the unconfirmed close and gives the bus back, and the next stop
    finds nothing to stop. This replaces the earlier pin, under which every
    later stop and start answered the same refusal for the life of the server."""
    monkeypatch.setattr(bridge_module_under_test, "terminate_process_tree", end_child)
    config = can_config(tmp_path)
    service = can_module_under_test.CanBusService(config)
    child = SimpleNamespace(poll=lambda: 1, stdin=ScriptedStdin(), stdout=iter(()), stderr=iter(()))
    adapter = can_module_under_test.ProcessCanAdapterSession(child, 0.05)
    service.sessions[(PROCESS_BUS, None)] = can_module_under_test.CanBusSession(PROCESS_BUS, config.can_buses[PROCESS_BUS], adapter, str(tmp_path / "can-633.jsonl"))
    try:
        answers = [service.session_stop(PROCESS_BUS), service.session_stop(PROCESS_BUS)]
    finally:
        service.sessions.pop((PROCESS_BUS, None), None)
        service.close()

    assert adapter.process_reaped is True and adapter.safe_state_confirmed is False
    first, again = answers
    assert first.get("backend_error") == "Bridge did not confirm physical safe state before process cleanup.", first
    assert first.get("cleanup_confirmed") is not True, first
    assert_carries_its_entry(first, "can_adapter_close_failed")
    assert again["ok"] is True and again["was_active"] is False, again


# --- can_send ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "error_type"),
    [
        ({"bus_id": "no_such_bus_in_this_config", "frame_id": 0x123, "data_hex": "01"}, "can_bus_not_configured"),
        ({"bus_id": SHARED_BUS, "frame_id": 0x123, "data_hex": "01"}, "can_participant_required"),
        ({"bus_id": SHARED_BUS, "participant": "ecu_z", "frame_id": 0x123, "data_hex": "01"}, "can_participant_not_configured"),
    ],
)
def test_a_send_refused_by_the_configuration_carries_its_entry(tmp_path: Path, arguments: dict, error_type: str) -> None:
    service = AgenticHILToolService(can_config(tmp_path))
    try:
        result = service.call("can_send", arguments)
    finally:
        service.close()

    assert_carries_its_entry(result, error_type)


def test_a_send_the_direct_adapter_refuses_carries_its_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path)
    bus = StagedBus()
    install_python_can(tmp_path, monkeypatch, bus)
    service = AgenticHILToolService(config)
    try:
        assert service.call("can_session_start", {"bus_id": DIRECT_BUS, "clear_rx_queue": False})["ok"] is True
        bus.fail_send = True

        result = service.call("can_send", {"bus_id": DIRECT_BUS, "frame_id": 0x123, "data_hex": "01"})
    finally:
        service.close()

    assert_carries_its_entry(result, "can_send_failed")


class StagedStdin(io.StringIO):
    """The stdin of a bridge child: accepts every request, or refuses the write."""

    def __init__(self, *, refuse: bool) -> None:
        super().__init__()
        self.refuse = refuse

    def write(self, text: str) -> int:
        if self.refuse:
            raise OSError("staged pipe failure")
        return super().write(text)


def bridge_session(*, refuse_write: bool = False, exited: bool = False, timeout_s: float = 1.0) -> can_module_under_test.ProcessCanAdapterSession:
    """A real CAN adapter bridge session around a child that never answers.

    `exited` stages the race `CanBusService` cannot rule out: the session was
    found active, and the child had gone by the time the request was made. Its
    close is replaced, because the real one would terminate a process tree and
    there is none."""
    child = SimpleNamespace(poll=lambda: 1 if exited else None, stdin=StagedStdin(refuse=refuse_write), stdout=iter(()), stderr=iter(()))
    session = can_module_under_test.ProcessCanAdapterSession(child, timeout_s)
    if exited:
        session.status = lambda: {"active": True, "backend": session.adapter_name, "cleanup_required": False, "safe_state_confirmed": False, "process_reaped": False}
    session.close = lambda: {"ok": True, "safe_state_confirmed": True, "process_reaped": True}
    return session


def test_a_send_the_bridge_never_answers_carries_its_entry(tmp_path: Path) -> None:
    """`can_adapter_timeout`, built in `agentic_hil.bridge` and handed to
    `can_send` unchanged through the `**sent` spread."""
    config = can_config(tmp_path)
    service = can_module_under_test.CanBusService(config)
    service.sessions[(PROCESS_BUS, None)] = can_module_under_test.CanBusSession(PROCESS_BUS, config.can_buses[PROCESS_BUS], bridge_session(timeout_s=0.05), str(tmp_path / "can-timeout.jsonl"))
    try:
        result = service.send(PROCESS_BUS, {"frame_id": 0x123, "data_hex": "01"})
    finally:
        service.sessions.pop((PROCESS_BUS, None), None)
        service.close()

    assert_carries_its_entry(result, "can_adapter_timeout")


@pytest.mark.parametrize("error_type", SEND_REFUSALS)
def test_a_send_the_broker_refuses_carries_its_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: str) -> None:
    class Refusing(RefusingParticipant):
        refusal = broker_refusal(error_type, "ecu_a")

    monkeypatch.setattr(canbroker_module_under_test, "attach_participant", lambda config, bus_id, participant, **kwargs: Refusing(participant))
    service = AgenticHILToolService(can_config(tmp_path))
    try:
        assert service.call("can_session_start", {"bus_id": SHARED_BUS, "participant": "ecu_a", "clear_rx_queue": False})["ok"] is True

        result = service.call("can_send", {"bus_id": SHARED_BUS, "participant": "ecu_a", "frame_id": 0x123, "data_hex": "01"})
    finally:
        service.close()

    assert_carries_its_entry(result, error_type)


# --- can_read ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "error_type"),
    [
        ({"bus_id": "no_such_bus_in_this_config"}, "can_bus_not_configured"),
        ({"bus_id": SHARED_BUS}, "can_participant_required"),
        ({"bus_id": SHARED_BUS, "participant": "ecu_z"}, "can_participant_not_configured"),
    ],
)
def test_a_read_refused_by_the_configuration_carries_its_entry(tmp_path: Path, arguments: dict, error_type: str) -> None:
    service = AgenticHILToolService(can_config(tmp_path))
    try:
        result = service.call("can_read", arguments)
    finally:
        service.close()

    assert_carries_its_entry(result, error_type)


@pytest.mark.parametrize("arguments", [{}, {"until_id": 0x123}], ids=["read", "read_until_id"])
def test_a_read_the_direct_adapter_refuses_carries_its_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict) -> None:
    """Both read paths: the plain read, and `_read_until_id`, which reads one
    frame at a time and builds its own refusal around the adapter's."""
    config = can_config(tmp_path)
    bus = StagedBus()
    install_python_can(tmp_path, monkeypatch, bus)
    service = AgenticHILToolService(config)
    try:
        assert service.call("can_session_start", {"bus_id": DIRECT_BUS, "clear_rx_queue": False})["ok"] is True
        bus.fail_recv = True

        result = service.call("can_read", {"bus_id": DIRECT_BUS, **arguments})
    finally:
        service.close()

    assert_carries_its_entry(result, "can_read_failed")


@pytest.mark.parametrize(("stage", "error_type"), [({"refuse_write": True}, "can_adapter_invalid_request"), ({"exited": True}, "can_adapter_process_exited")])
def test_a_read_the_bridge_was_never_asked_for_carries_its_entry(tmp_path: Path, stage: dict, error_type: str) -> None:
    """The two transport refusals raised before the request reached the child,
    which `can_read` hands on through the `**read` spread."""
    config = can_config(tmp_path)
    service = can_module_under_test.CanBusService(config)
    service.sessions[(PROCESS_BUS, None)] = can_module_under_test.CanBusSession(PROCESS_BUS, config.can_buses[PROCESS_BUS], bridge_session(**stage), str(tmp_path / "can-request.jsonl"))
    try:
        result = service.read(PROCESS_BUS, 1, 0.0)
    finally:
        service.sessions.pop((PROCESS_BUS, None), None)
        service.close()

    assert_carries_its_entry(result, error_type)


@pytest.mark.parametrize("error_type", READ_REFUSALS)
def test_a_read_the_broker_refuses_carries_its_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: str) -> None:
    class Refusing(RefusingParticipant):
        refusal = broker_refusal(error_type, "ecu_a")

    monkeypatch.setattr(canbroker_module_under_test, "attach_participant", lambda config, bus_id, participant, **kwargs: Refusing(participant))
    service = AgenticHILToolService(can_config(tmp_path))
    try:
        assert service.call("can_session_start", {"bus_id": SHARED_BUS, "participant": "ecu_a", "clear_rx_queue": False})["ok"] is True

        result = service.call("can_read", {"bus_id": SHARED_BUS, "participant": "ecu_a"})
    finally:
        service.close()

    assert_carries_its_entry(result, error_type)


@pytest.mark.parametrize("error_type", TRANSPORT_FAILURES)
@pytest.mark.parametrize("tool", ["can_send", "can_read"])
def test_a_broker_request_that_fails_in_transport_carries_its_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, error_type: str) -> None:
    """A slow, garbled or ended broker, met by a send or a read (#664)."""

    def failure() -> BaseException:
        if error_type == "can_broker_disconnected":
            return BrokenPipeError(32, "The pipe has been ended")
        return ParticipantError(broker_refusal(error_type, "ecu_a"))

    class Failing(RefusingParticipant):
        def send(self, frame_id: int, data: bytes, *, extended: bool = False, rtr: bool = False) -> dict:
            raise failure()

        def read(self, max_frames: int, wait_timeout_s: float) -> dict:
            raise failure()

    monkeypatch.setattr(canbroker_module_under_test, "attach_participant", lambda config, bus_id, participant, **kwargs: Failing(participant))
    service = AgenticHILToolService(can_config(tmp_path))
    try:
        assert service.call("can_session_start", {"bus_id": SHARED_BUS, "participant": "ecu_a", "clear_rx_queue": False})["ok"] is True
        arguments = {"frame_id": 0x123, "data_hex": "01"} if tool == "can_send" else {}
        result = service.call(tool, {"bus_id": SHARED_BUS, "participant": "ecu_a", **arguments})
    finally:
        service.close()

    assert_carries_its_entry(result, error_type)


# --- the shared entry -------------------------------------------------------


@pytest.mark.parametrize(("tool", "arguments"), [("can_read", {"bus_id": DIRECT_BUS}), ("can_send", {"bus_id": DIRECT_BUS, "frame_id": 0x123, "data_hex": "01"})])
def test_a_can_call_without_a_session_carries_the_shared_entry(tmp_path: Path, tool: str, arguments: dict) -> None:
    """`session_not_active` at `CanBusService._active_session`. The entry is the
    shared one the COM refusals of #635 write, and the CAN refusal carries it
    like every other CAN refusal. Red until that entry is in the catalogue."""
    service = AgenticHILToolService(can_config(tmp_path))
    try:
        result = service.call(tool, arguments)
    finally:
        service.close()

    assert_carries_its_entry(result, "session_not_active")
