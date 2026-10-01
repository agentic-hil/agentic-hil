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
through the broker's own answer. The scan is pinned as well, so a scanner that
silently stopped seeing a module would fail here rather than pass by finding
less.

Kept out of the required set, each for a reason stated where it is listed:

* `session_not_active` is answered by the COM, CAN and debug sessions alike, and
  its one bare entry is written with the COM refusals of #635. The CAN refusal
  is required to carry whatever that entry says.
* `can_broker_timeout` and `can_broker_not_attached` are spelled in the broker
  and returned by no CAN tool.
* The types every hardware tool shares (`audit_unavailable`,
  `hardware_action_exception` and the rest, #645; `resource_busy` and the
  coordination types, #646) are owned by those changes and named below so the
  boundary is a decision rather than an omission.

Required although no tool returns them at the top level: the three cleanup types
a caller of `can_session_start` reads under `cleanup_error` when a bridge that
refused to open also would not close. They are CAN types in practice, because
the CAN adapter session is the only process bridge there is, and a test below
holds that true.

No hardware and no CAN interface. The behavioural tests drive the real tool
functions over the fakes the CAN suites already use: a python-can module whose
bus is staged, a broker participant that answers a staged refusal, and an
adapter session that will not close.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from conftest import write_config
from test_can_likely_causes import RefusingCloseAdapter, call_site_strings, process_bridge_session_classes
from test_can_participant_sessions import SharedFakeParticipant

import agentic_hil.bridge as bridge_module_under_test
import agentic_hil.can as can_module_under_test
import agentic_hil.canbroker as canbroker_module_under_test
from agentic_hil.bridge import ProcessBridgeSession
from agentic_hil.canbroker import ParticipantError
from agentic_hil.config import load_config
from agentic_hil.knowledge import ERROR_CATALOGUE, ERROR_URI_PREFIX, ErrorRemedy, catalogue_entry, remediation_fields
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

# ---------------------------------------------------------------------------
# The inventory, read out of the code.


def spelled_error_types(module: ModuleType, attributes: dict[str, tuple[str, ...]] | None = None) -> tuple[dict[str, list[int]], list[tuple[int, str]]]:
    """Every error type this module writes into a result, and every place it
    writes one this scan cannot read.

    The shapes and the resolution follow `raised_can_error_types` in
    `test_can_likely_causes`, with two differences that matter here. Every
    value is recorded, not only the `can_` ones, because a CAN tool answers
    `invalid_argument` and `config_invalid` too and those need their entries as
    much. And a conditional expression is read branch by branch, because the
    broker writes ``"can_bus_incident" if scope == "bus" else
    "can_participant_incident"`` and the second type is otherwise invisible.

    An expression that resolves to nothing is returned rather than dropped, with
    its line and its source text, so that a refusal whose type is computed is a
    pinned decision and not a gap in the inventory.
    """
    source = Path(inspect.getsourcefile(module) or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    call_strings = call_site_strings(tree)
    attribute_values = attributes or {}
    found: dict[str, list[int]] = {}
    unresolved: list[tuple[int, str]] = []

    def parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
        return {argument.arg for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)}

    def resolve(node: ast.expr, enclosing: ast.FunctionDef | ast.AsyncFunctionDef | None) -> list[str]:
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else []
        if isinstance(node, ast.Name):
            value = getattr(module, node.id, None)
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

    def visit(node: ast.AST, enclosing: ast.FunctionDef | ast.AsyncFunctionDef | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            enclosing = node
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "error_type" and value is not None:
                    record(value, enclosing)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "error_type":
                    record(keyword.value, enclosing)
        for child in ast.iter_child_nodes(node):
            visit(child, enclosing)

    visit(tree, None)
    return found, unresolved


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
            "device_busy",
            "invalid_argument",
            "permission_denied",
        }
    ),
}

# The places a type is computed rather than spelled, by module and source text.
# Each one passes on a type that is already in the inventory or owned elsewhere,
# and a new one has to be added here, with its reason, before the guard passes.
PINNED_UNRESOLVED: dict[str, frozenset[str]] = {
    "can.py": frozenset(),
    "bridge.py": frozenset(),
    "canbroker.py": frozenset(
        {
            # `main` prints a `ConfigError` it could not load past. Its types are
            # the configuration's own (`config_invalid` and its neighbours) and
            # are answered with `ConfigError.to_dict`, which attaches their
            # entries itself.
            "error.error_type",
            # `_explained_exit_refusal` keeps the type of the refusal a broker
            # wrote before it exited: the adapter's own open refusal, which is a
            # type the scan of `agentic_hil.can` and `agentic_hil.bridge`
            # already finds, or the `ConfigError` above.
            "document.get('error_type')",
        }
    ),
}

# Answered by the COM, CAN and debug sessions alike. The one bare entry is written
# with the COM refusals of #635 and is true for all three; the CAN refusal at
# `CanBusService._active_session` carries it, which
# `test_a_can_call_without_a_session_carries_the_shared_entry` holds.
OWNED_BY_THE_COM_REFUSALS: dict[str, str] = {
    "session_not_active": "one bare entry, written with the COM refusals of #635, true for COM, CAN and debug sessions",
}

# Spelled on the CAN path and returned by no CAN tool, so an entry for them would
# describe an answer nobody reads.
NOT_RETURNED_BY_A_TOOL: dict[str, str] = {
    "can_broker_timeout": (
        "raised as a ParticipantError out of Participant._request; can_send and can_read re-raise it and the caller"
        " reads hardware_action_exception, which #645 owns"
    ),
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
    "resource_busy": "#646",
    "coordination_closed": "#646",
    "coordination_state_invalid": "#646",
}

# Not top-level, and required anyway: a caller of `can_session_start` reads them
# under `cleanup_error` (and `cleanup_error.close_response`) when a bridge refused
# to open and then would not close (`open_process_adapter`).
NESTED_UNDER_CLEANUP_ERROR = frozenset({"bridge_safe_state_unconfirmed", "bridge_process_reap_failed", "can_adapter_close_interrupted"})

ALL_PINNED = frozenset().union(*PINNED_INVENTORY.values())
REQUIRED = sorted(ALL_PINNED - set(OWNED_BY_THE_COM_REFUSALS) - set(NOT_RETURNED_BY_A_TOOL))

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
        "can_broker_invalid_message",
        "can_broker_not_bus_owner",
        "can_broker_protocol_mismatch",
        "can_broker_stopping",
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


def test_the_scan_finds_exactly_the_pinned_inventory() -> None:
    scanned = {name: frozenset(types) for name, types in scanned_inventory().items()}

    for name, pinned in PINNED_INVENTORY.items():
        found = scanned.get(name, frozenset())
        assert found == pinned, f"{name}: new {sorted(found - pinned)}, gone {sorted(pinned - found)}"
    assert set(scanned) == set(PINNED_INVENTORY), scanned


def test_every_type_the_scan_cannot_read_is_a_pinned_decision() -> None:
    for module, attributes in scanned_modules():
        unresolved = frozenset(text for _, text in spelled_error_types(module, attributes)[1])

        assert unresolved == PINNED_UNRESOLVED[file_name(module)], f"{file_name(module)}: {sorted(unresolved)}"


def test_the_bridge_types_reach_a_caller_only_through_the_can_adapter() -> None:
    """What makes `bridge_*` and `can_adapter_*` CAN types: the CAN adapter
    session is the only process bridge. A second one would build types under
    its own prefix and send the `bridge_*` cleanup types to another tool, and
    then whose entries they are is a question to take on purpose."""
    sessions = can_bridge_sessions()

    assert sessions and all(session_class.__module__ == can_module_under_test.__name__ for session_class in sessions.values()), sessions
    assert bridge_attributes() == {"error_prefix": (can_module_under_test.ProcessCanAdapterSession.error_prefix,)}


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
    missing = {error_type: sites.get(error_type, []) for error_type in REQUIRED if error_type not in ERROR_CATALOGUE}

    assert not missing, "CAN error types without a catalogue entry:\n" + "\n".join(f"  {error_type}: {', '.join(where)}" for error_type, where in sorted(missing.items()))


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


PACKAGE_SOURCE = "\n".join(path.read_text(encoding="utf-8") for path in sorted(Path(inspect.getsourcefile(can_module_under_test) or "").parent.rglob("*.py")))
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


def entries_offered(error_type: str) -> list[dict]:
    """The remediation fields each catalogue entry of this type would hand a refusal.

    The bare entry and every scoped one: a call site may pass a scope (the
    adapter, the field), and the refusal is right when it carries any entry of
    its own type whole.
    """
    offered = []
    for key in ERROR_CATALOGUE:
        entry_type, _, scope = key.partition(":")
        if entry_type == error_type:
            fields = remediation_fields(error_type, scope or None)
            if fields:
                offered.append(fields)
    return offered


def assert_carries_its_entry(result: dict, error_type: str) -> None:
    assert result.get("ok") is False and result.get("error_type") == error_type, result
    offered = entries_offered(error_type)
    assert offered, f"no catalogue entry for {error_type}, so its refusal carries no remediation"
    carried = {key: result[key] for key in ("remediation", "do_not") if key in result}
    assert carried in offered, f"{error_type} carries {carried!r}, not an entry of its own type"


class StagedInitializationError(Exception):
    pass


class StagedBus:
    """A python-can bus whose receive and send fail, or never run dry, on cue."""

    def __init__(self) -> None:
        self.fail_recv = False
        self.fail_send = False
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
        return None


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


def test_every_broker_refusal_has_a_behavioural_carrier() -> None:
    """Every CAN type the broker writes is driven through a tool below, so none
    of them can have an entry that no refusal carries."""
    broker_types = {error_type for error_type in PINNED_INVENTORY["canbroker.py"] if error_type.startswith("can_") and error_type in REQUIRED}

    assert broker_types <= {*ATTACH_REFUSALS, *SEND_REFUSALS, *READ_REFUSALS}, sorted(broker_types - {*ATTACH_REFUSALS, *SEND_REFUSALS, *READ_REFUSALS})


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
    could not close. The entry it carries is the one #633 constrains: it may
    not promise that a retried stop clears an unconfirmed bridge close."""
    config = can_config(tmp_path)
    service = can_module_under_test.CanBusService(config)
    service.sessions[(DIRECT_BUS, None)] = can_module_under_test.CanBusSession(DIRECT_BUS, config.can_buses[DIRECT_BUS], RefusingCloseAdapter(), str(tmp_path / "can-close.jsonl"))
    try:
        result = service.session_stop(DIRECT_BUS)
    finally:
        service.sessions.pop((DIRECT_BUS, None), None)
        service.close()

    assert_carries_its_entry(result, "can_adapter_close_failed")


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


def test_a_read_the_direct_adapter_refuses_carries_its_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path)
    bus = StagedBus()
    install_python_can(tmp_path, monkeypatch, bus)
    service = AgenticHILToolService(config)
    try:
        assert service.call("can_session_start", {"bus_id": DIRECT_BUS, "clear_rx_queue": False})["ok"] is True
        bus.fail_recv = True

        result = service.call("can_read", {"bus_id": DIRECT_BUS})
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


# --- the shared entry -------------------------------------------------------

# Stands in for the shared entry where it has not landed yet, so the wiring is
# tested on its own. Once the real entry is in the catalogue, it is the one used.
SESSION_NOT_ACTIVE_STAND_IN = ErrorRemedy(
    meaning="Stand-in for the shared entry.",
    remediation=("Start the session with `can_session_start`.",),
    do_not=("Do not open the bus outside the session.",),
)


@pytest.mark.parametrize(("tool", "arguments"), [("can_read", {"bus_id": DIRECT_BUS}), ("can_send", {"bus_id": DIRECT_BUS, "frame_id": 0x123, "data_hex": "01"})])
def test_a_can_call_without_a_session_carries_the_shared_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict) -> None:
    """`session_not_active` at `CanBusService._active_session`. The entry is the
    shared one, and the CAN refusal carries it like every other CAN refusal."""
    if "session_not_active" not in ERROR_CATALOGUE:
        monkeypatch.setitem(ERROR_CATALOGUE, "session_not_active", SESSION_NOT_ACTIVE_STAND_IN)
    service = AgenticHILToolService(can_config(tmp_path))
    try:
        result = service.call(tool, arguments)
    finally:
        service.close()

    assert_carries_its_entry(result, "session_not_active")
