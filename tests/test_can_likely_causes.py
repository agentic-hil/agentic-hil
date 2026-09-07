"""Every CAN refusal names causes about the bus, whatever backend the service has (#517).

`classify_failure_report` takes the causes off the report when it carries
`likely_causes` and otherwise asks a table for them. The tables it is handed are
`comports.likely_causes` and each debugger backend's `_likely_causes`, both keyed
by their own error types with a generic fallback, so no CAN error type reaches
either: `can_interface_not_found`, `can_adapter_library_missing`,
`can_channel_not_available`, `can_listen_only_unsupported`,
`can_queue_clear_failed` and `can_send_failed` all fall through to
`inspect the COM port log for details` or, through a service whose backend is a
debugger, to `inspect the debugger log for details`. An operator whose SocketCAN
channel is not a network device is sent to read a serial log that does not exist
for this failure and would say nothing about it if it did.

`can_interface_down` (#511) closes this for one type by carrying its own
`likely_causes` on the refusal, which the classifier passes through. The decided
behaviour is that every CAN refusal does the same: one causes table in
`agentic_hil.can` keyed by the CAN error types, each refusal carrying its causes
from that table, and the classifier asking that table for a report whose error
type is a CAN type, so `classify_last_error` and the refusal say the same thing
over every backend. `can_interface_down` keeps exactly the causes #511 pinned, so
the table and that refusal agree, and the COM port and debugger tables are
untouched.

These tests assert what a cause is about rather than its wording: each one names
the bus, the interface, the adapter, the channel or the frame, none of them names
another transport, and no CAN type answers one of the three generic fallbacks.
The one exception is `can_interface_down`, whose three causes are pinned verbatim
because #511 decided them and this change must not move them.

No hardware and no CAN interface: the reports are written straight into the
failure record the classifier reads, which is the seam the defect lives on.

#523 closes the rest of it. #517 keyed the table by seven names and recorded the
boundary: fifteen further CAN error types that `agentic_hil.can` raises stayed
outside it and kept answering whichever generic table the classifier was handed,
`can_listen_only_mode` among them, so a transmit refused on a bus declared
listen-only sent its reader to a serial log. The decided behaviour is the first
of the issue's two options: the table names every CAN error type the module
raises, each refusal carries its entry through the path #517 built, and a test
reads the raised types out of the code rather than off a list somebody has to
remember to extend, so a type added later cannot fall through unnoticed. A type
no code raises keeps falling through, which is what the boundary test becomes.

The code the inventory reads is two modules, not one. `agentic_hil.bridge`
builds the transport's error types out of a prefix carried by the session class
and a kind passed at the call site, so `can_adapter_timeout`,
`can_adapter_process_exited`, `can_adapter_invalid_request` and
`can_adapter_close_interrupted` are real CAN error types that appear as a
literal nowhere and reach the recorded failure on the same path as the fifteen.
Four more rows, and nineteen types in all.

The broker's own families, `can_broker_*`, `can_participant_*` and the three bus
words beside them, are deliberately not in this table: the participant path is
being designed in #500 and its refusals are answered there, not here. They are
pinned below as the recorded boundary so that folding them in stays a decision
somebody takes on purpose.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from conftest import write_config

import agentic_hil.bridge as bridge_module_under_test
import agentic_hil.can as can_module_under_test
import agentic_hil.canbroker as canbroker_module_under_test
import agentic_hil.knowledge as knowledge_module_under_test
from agentic_hil.bridge import BridgeCleanupError, ProcessBridgeSession
from agentic_hil.comports import likely_causes as com_port_likely_causes
from agentic_hil.config import load_config
from agentic_hil.debugger import UnboundDebuggerBackend
from agentic_hil.knowledge import (
    CAN_ADAPTER_LIBRARY_MISSING_ERROR,
    CAN_CHANNEL_NOT_AVAILABLE_ERROR,
    CAN_CLASSIC_FRAME_TOO_LARGE_ERROR,
    CAN_FD_FRAME_LENGTH_INVALID_ERROR,
    CAN_FD_REMOTE_FRAME_ERROR,
    CAN_INTERFACE_DOWN_ERROR,
    CAN_INTERFACE_NOT_FOUND_ERROR,
    LISTEN_ONLY_MODE_ERROR,
    LISTEN_ONLY_UNCONFIRMED_ERROR,
    LISTEN_ONLY_UNSUPPORTED_ERROR,
)
from agentic_hil.report import classify_failure_report, write_report
from agentic_hil.tools import AgenticHILToolService

QUEUE_CLEAR_FAILED_ERROR = "can_queue_clear_failed"
SEND_FAILED_ERROR = "can_send_failed"
QUEUE_CLEAR_LIMIT_ERROR = "can_queue_clear_limit"
READ_FAILED_ERROR = "can_read_failed"
ADAPTER_OPEN_FAILED_ERROR = "can_adapter_open_failed"
ADAPTER_CLOSE_FAILED_ERROR = "can_adapter_close_failed"
ADAPTER_NOT_FOUND_ERROR = "can_adapter_not_found"
ADAPTER_PROCESS_START_FAILED_ERROR = "can_adapter_process_start_failed"
ADAPTER_INVALID_RESPONSE_ERROR = "can_adapter_invalid_response"
ADAPTER_PROTOCOL_UNSUPPORTED_ERROR = "can_adapter_protocol_unsupported"
BACKEND_NOT_AVAILABLE_ERROR = "can_backend_not_available"
BUS_NOT_CONFIGURED_ERROR = "can_bus_not_configured"
ADAPTER_TIMEOUT_ERROR = "can_adapter_timeout"
ADAPTER_PROCESS_EXITED_ERROR = "can_adapter_process_exited"
ADAPTER_INVALID_REQUEST_ERROR = "can_adapter_invalid_request"
ADAPTER_CLOSE_INTERRUPTED_ERROR = "can_adapter_close_interrupted"

# Distinctive by design: device locks are machine-wide, so a bus id and channel
# shared with another clone's tests would contend across checkouts.
DOWN_BUS_ID = "likely_causes_down_bus"
DOWN_CHANNEL = "can517down"
# Recorded inside the container image on 2026-09-06 for #511 (kernel 6.18 under
# WSL2, iproute2-6.15.0, python-can 4.6.1, Python 3.12.14) and carried here
# verbatim: a down vcan's `flags` and `operstate`. IFF_UP is bit 0; 0x80 is
# IFF_NOARP, which a vcan carries whatever its state. Copies of
# `tests/test_can_interface_down.py`'s constants of the same names rather than
# imports of them, because importing that module here would close a cycle: it
# imports `test_can_listen_only`, which imports this module.
RECORDED_DOWN_FLAGS = "0x80\n"
RECORDED_DOWN_OPERSTATE = "down\n"

# The seven #517 keyed the table by. The order is that issue's.
CAN_ERROR_TYPES_FROM_517 = [
    CAN_INTERFACE_NOT_FOUND_ERROR,
    CAN_ADAPTER_LIBRARY_MISSING_ERROR,
    CAN_CHANNEL_NOT_AVAILABLE_ERROR,
    LISTEN_ONLY_UNSUPPORTED_ERROR,
    QUEUE_CLEAR_FAILED_ERROR,
    SEND_FAILED_ERROR,
    CAN_INTERFACE_DOWN_ERROR,
]

# The fifteen #523 names, in the issue's order: every other CAN error type
# `agentic_hil.can` raises, each of which answered the COM port's or the bound
# debugger's generic line before this change.
CAN_ERROR_TYPES_FROM_523 = [
    LISTEN_ONLY_MODE_ERROR,
    LISTEN_ONLY_UNCONFIRMED_ERROR,
    QUEUE_CLEAR_LIMIT_ERROR,
    READ_FAILED_ERROR,
    ADAPTER_OPEN_FAILED_ERROR,
    ADAPTER_CLOSE_FAILED_ERROR,
    ADAPTER_NOT_FOUND_ERROR,
    ADAPTER_PROCESS_START_FAILED_ERROR,
    ADAPTER_INVALID_RESPONSE_ERROR,
    ADAPTER_PROTOCOL_UNSUPPORTED_ERROR,
    BACKEND_NOT_AVAILABLE_ERROR,
    BUS_NOT_CONFIGURED_ERROR,
    CAN_FD_REMOTE_FRAME_ERROR,
    CAN_CLASSIC_FRAME_TOO_LARGE_ERROR,
    CAN_FD_FRAME_LENGTH_INVALID_ERROR,
]

# The four the CAN adapter bridge builds and the issue's list does not name,
# because they are spelled nowhere: `agentic_hil.bridge` writes
# `f"{self.error_prefix}_{kind}"` and `ProcessCanAdapterSession` sets that prefix
# to `can_adapter`. They reach the recorded failure on the same path as the
# fifteen, through `open_process_adapter` and `CanBusService.session_start`, so
# they fall through to the same generic line and belong in the same table.
# `can_adapter_invalid_response` is the fifth kind the bridge builds and is
# already among the fifteen, because `agentic_hil.can` happens to spell that one
# literal itself as well.
CAN_ERROR_TYPES_BUILT_BY_THE_BRIDGE = [
    ADAPTER_TIMEOUT_ERROR,
    ADAPTER_PROCESS_EXITED_ERROR,
    ADAPTER_INVALID_REQUEST_ERROR,
    ADAPTER_CLOSE_INTERRUPTED_ERROR,
]

# The kinds `agentic_hil.bridge` builds an error type from, which the prefix of
# the CAN session class turns into CAN types. Pinned so that a kind added to the
# transport shows up here as a name somebody has to decide a row for.
BRIDGE_ERROR_KINDS = ["close_interrupted", "invalid_request", "invalid_response", "process_exited", "timeout"]

# Every error type a CAN refusal answers with that reaches the classifier.
CAN_ERROR_TYPES = CAN_ERROR_TYPES_FROM_517 + CAN_ERROR_TYPES_FROM_523 + CAN_ERROR_TYPES_BUILT_BY_THE_BRIDGE

# The broker's own error types, which this table does not answer and this issue
# does not decide: the participant path they belong to is being designed in #500,
# and a cause list written here would be an answer given ahead of that design.
# Recorded rather than left implicit, so that the day they are folded in, this
# list is what has to be edited on purpose.
BROKER_ONLY_ERROR_TYPES = [
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
    "can_bus_not_shared",
    "can_listen_only_conflict",
    "can_participant_busy",
    "can_participant_filter_violation",
    "can_participant_frame_budget_exhausted",
    "can_participant_not_configured",
]

# A `can_` type no code raises anywhere, which is what the boundary is once the
# table names every type that is raised. Deliberately unspellable by accident.
UNRAISED_CAN_ERROR_TYPE = "can_no_code_raises_this_one"

# The three generic answers a CAN type currently reaches, one per table the
# classifier is handed.
COM_PORT_FALLBACK = ["inspect the COM port log for details"]
DEBUGGER_FALLBACK = ["inspect the debugger log for details"]
UNBOUND_FALLBACK = ["inspect the report and log for details"]

# Pinned by #511 and not this issue's to move: the causes the `can_interface_down`
# refusal carries, which the table has to answer with so that the refusal and
# `classify_last_error` cannot drift apart.
INTERFACE_DOWN_CAUSES = [
    "the interface was created and never brought up (`ip link set <dev> up` has not been run for it)",
    "the link was taken down out of band, by an operator or by a script, and nothing brought it back",
    "a USB CAN adapter was re-enumerated and its interface came back down",
]

# What "names the bus" means, as whole words: a cause about a CAN failure talks
# about one of these. Whole words because a substring test for "can" passes on
# the word "cannot", which every sentence may carry.
BUS_WORDS = (
    "can",
    "bus",
    "buses",
    "interface",
    "interfaces",
    "channel",
    "channels",
    "adapter",
    "adapters",
    "link",
    "controller",
    "frame",
    "frames",
    "queue",
    "driver",
    "drivers",
    "library",
    "socketcan",
    "pcan",
    "bitrate",
    "listen",
    "listening",
    "node",
    "vcan",
    # Added with #523's fifteen, which reach further out along the same path:
    # the adapter bridge that a `process` bus is driven through, the python-can
    # backend behind the rest, and the frame itself.
    "bridge",
    "backend",
    "protocol",
    "payload",
    "dlc",
    "socket",
    "transmit",
    "transmits",
    "receive",
    "python-can",
)
# The transports a CAN cause must never send the reader to. Matched as whole
# words, the way `BUS_WORDS` are: "serial" is a substring of "serialized", which
# is the natural word for a request that could not be written to the bridge, and
# "probe" of "probed", so a raw substring test would refuse a correct cause for
# the wrong reason.
OTHER_TRANSPORT_WORDS = ("com port", "serial", "debugger", "gdb", "openocd", "st-link", "swd", "jtag", "probe")

# One anchor per type, so that "names the bus" cannot be satisfied by seven
# copies of the same sentence: each list has to say something about its own
# failure. Alternatives, because the wording is the implementation's to choose.
TYPE_ANCHORS = {
    CAN_INTERFACE_NOT_FOUND_ERROR: ("interface", "channel", "netdev"),
    CAN_ADAPTER_LIBRARY_MISSING_ERROR: ("library", "driver", "install", "installed"),
    CAN_CHANNEL_NOT_AVAILABLE_ERROR: ("channel", "driver", "adapter"),
    LISTEN_ONLY_UNSUPPORTED_ERROR: ("listen", "listening", "controller"),
    QUEUE_CLEAR_FAILED_ERROR: ("queue", "receive", "buffer", "drain", "drained"),
    SEND_FAILED_ERROR: ("send", "sent", "transmit", "transmitted", "ack", "frame"),
    CAN_INTERFACE_DOWN_ERROR: ("interface", "link"),
    LISTEN_ONLY_MODE_ERROR: ("listen", "listen_only", "transmit", "send"),
    LISTEN_ONLY_UNCONFIRMED_ERROR: ("confirm", "confirmed", "unconfirmed", "listen"),
    QUEUE_CLEAR_LIMIT_ERROR: ("queue", "drain", "drained", "traffic", "buffer", "frames"),
    READ_FAILED_ERROR: ("read", "receive", "frames", "interface"),
    ADAPTER_OPEN_FAILED_ERROR: ("open", "opened", "driver", "adapter"),
    ADAPTER_CLOSE_FAILED_ERROR: ("close", "closed", "adapter", "driver"),
    ADAPTER_NOT_FOUND_ERROR: ("executable", "command", "path", "adapter"),
    ADAPTER_PROCESS_START_FAILED_ERROR: ("process", "start", "started", "executable", "command"),
    ADAPTER_INVALID_RESPONSE_ERROR: ("response", "answered", "protocol", "bridge"),
    ADAPTER_PROTOCOL_UNSUPPORTED_ERROR: ("protocol", "version", "bridge"),
    BACKEND_NOT_AVAILABLE_ERROR: ("python-can", "install", "installed", "backend"),
    BUS_NOT_CONFIGURED_ERROR: ("buses", "configuration", "configured", "entry", "id"),
    CAN_FD_REMOTE_FRAME_ERROR: ("remote", "rtr", "fd", "frame"),
    CAN_CLASSIC_FRAME_TOO_LARGE_ERROR: ("eight", "bytes", "payload", "classic", "fd"),
    CAN_FD_FRAME_LENGTH_INVALID_ERROR: ("length", "dlc", "bytes", "payload"),
    ADAPTER_TIMEOUT_ERROR: ("timeout", "timed", "answer", "answered", "reply", "bridge"),
    ADAPTER_PROCESS_EXITED_ERROR: ("process", "exited", "running", "bridge"),
    ADAPTER_INVALID_REQUEST_ERROR: ("request", "write", "stdin", "bridge"),
    ADAPTER_CLOSE_INTERRUPTED_ERROR: ("close", "closed", "interrupted", "bridge"),
}

DEBUGGER_TYPES = ["openocd", "stlink", "pyocd"]

# One debugger cause list per backend, verbatim from each backend's own table, so
# that a rewrite of the debugger causes is caught here rather than passing as
# "still not about a bus". `target_not_detected` because all three answer it and
# all three word it differently.
DEBUGGER_TARGET_NOT_DETECTED = {
    "openocd": ["DUT is not powered", "wrong interface configuration", "SWD/JTAG wiring issue", "debug probe already in use"],
    "stlink": ["DUT is not powered", "wrong SWD/JTAG interface selection", "SWD/JTAG wiring issue", "debug probe already in use"],
    "pyocd": ["DUT is not powered", "SWD/JTAG wiring issue", "debug probe already in use", "wrong debuggers.<name>.target_type for this device"],
}

# The seven rows #517 decided, verbatim. #523 adds rows and moves none: an
# operator who learned what `can_channel_not_available` means keeps that answer,
# and a rewrite of a settled cause list would show up here rather than passing as
# "still about the bus".
ROWS_FROM_517 = {
    CAN_INTERFACE_NOT_FOUND_ERROR: [
        "the SocketCAN interface was never created (`ip link add dev <dev> type can` has not been run for it, or `type vcan` for a virtual one)",
        "`can_buses.<id>.channel` names an interface this host does not have, or names it with a typo",
        "a USB CAN adapter was unplugged, and the interface it registered went away with it",
    ],
    CAN_INTERFACE_DOWN_ERROR: [
        "the interface was created and never brought up (`ip link set <dev> up` has not been run for it)",
        "the link was taken down out of band, by an operator or by a script, and nothing brought it back",
        "a USB CAN adapter was re-enumerated and its interface came back down",
    ],
    CAN_ADAPTER_LIBRARY_MISSING_ERROR: [
        "the python-can interface for this adapter is not installed (`agentic-hil[can]` installs python-can itself, and several adapters need a vendor library beside it)",
        "the vendor library this adapter is driven through is not on the library search path of the interpreter running this server",
        "the library is installed for a different Python, or for a different architecture, than the one running this server",
    ],
    CAN_CHANNEL_NOT_AVAILABLE_ERROR: [
        "the adapter is not connected, so the driver has no channel of that name to open",
        "`can_buses.<id>.channel` names a channel handle this driver does not have, and the driver's own enumeration lists the ones it does",
        "another program holds the channel, and the driver reports it unavailable for as long as that lasts",
    ],
    LISTEN_ONLY_UNSUPPORTED_ERROR: [
        "the kernel CAN controller is not in listen-only mode, which belongs to the link and is set out of band with `ip link set <dev> type can listen-only on`",
        "the interface is virtual, and a vcan has no controller, so it has no mode to be in and cannot carry the claim",
        "this python-can installation is too old to express listen-only for this adapter",
    ],
    QUEUE_CLEAR_FAILED_ERROR: [
        "the adapter stopped answering between the session opening and the queue drain, so the frames already buffered could not be read out",
        "the link went down under the open session, and every read on it fails from there on",
        "the driver or the interface was reset out of band while the queue was being drained",
    ],
    SEND_FAILED_ERROR: [
        "the interface went down under the open session, so the controller had nothing to put the frame on",
        "no other node is on the bus to acknowledge the frame, and the controller gave up retransmitting it",
        "the controller is bus-off or error-passive after earlier failed transmissions",
    ],
}


def call_site_strings(tree: ast.AST) -> dict[tuple[str, str], set[str]]:
    """The constant strings each function in this module is called with, by parameter.

    Needed because the transport builds its error types out of pieces: the kind
    arrives as an argument at the call site and the prefix off the session class,
    so the string never appears anywhere as a literal. Reading the call sites is
    what lets the inventory below see a type that is assembled rather than
    spelled.

    A method call passes no ``self``, so the first parameter of a function called
    as an attribute is dropped before the positional arguments are lined up.
    """
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    found: dict[tuple[str, str], set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else None
        target = functions.get(called or "")
        if target is None:
            continue
        parameters = [argument.arg for argument in target.args.args]
        if isinstance(node.func, ast.Attribute) and parameters and parameters[0] in ("self", "cls"):
            parameters = parameters[1:]
        for index, argument in enumerate(node.args):
            if index < len(parameters) and isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                found.setdefault((target.name, parameters[index]), set()).add(argument.value)
        for keyword in node.keywords:
            if keyword.arg and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                found.setdefault((target.name, keyword.arg), set()).add(keyword.value.value)
    return found


def raised_can_error_types(module: ModuleType, attributes: dict[str, tuple[str, ...]] | None = None) -> dict[str, list[int]]:
    """Every CAN error type this module writes into a result, read out of its source.

    The point of reading the code rather than a list: a list is something
    somebody has to remember to extend, and #517's boundary was exactly a list
    that had stopped matching what the module raises. Two shapes, because those
    are the two the module uses: an ``"error_type"`` key in a result dictionary,
    and an ``error_type=`` keyword to a helper that builds one. A name is
    resolved through the module's own namespace, so the knowledge constants the
    module imports count the same as a literal spelled in place.

    An f-string is resolved too, and it has to be: `agentic_hil.bridge` writes
    ``f"{self.error_prefix}_{kind}"``, so the four types the CAN adapter bridge
    builds are real error types that appear as no literal anywhere. Each
    interpolation is expanded from what it can be, ``self.<name>`` from
    ``attributes`` (the values the concrete session classes carry) and a
    parameter of the enclosing function from the constant strings its call sites
    pass, and every combination is recorded. An interpolation nothing resolves
    yields nothing rather than a guess.

    Returns the line numbers as well as the names so that a failure says where
    the type without a row is raised.
    """
    source = Path(inspect.getsourcefile(module) or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    call_strings = call_site_strings(tree)
    attribute_values = attributes or {}
    found: dict[str, list[int]] = {}

    def resolve(node: ast.expr, enclosing: ast.FunctionDef | ast.AsyncFunctionDef | None) -> list[str]:
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else []
        if isinstance(node, ast.Name):
            value = getattr(module, node.id, None)
            if isinstance(value, str):
                return [value]
            if enclosing is not None and node.id in {argument.arg for argument in enclosing.args.args}:
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

    def record(node: ast.expr, enclosing: ast.FunctionDef | ast.AsyncFunctionDef | None) -> None:
        for value in resolve(node, enclosing):
            if value.startswith("can_"):
                found.setdefault(value, []).append(node.lineno)

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
    return found


def process_bridge_session_classes() -> dict[str, type]:
    """Every session class built on the process bridge, base included, by name."""
    seen: dict[str, type] = {}
    pending = [ProcessBridgeSession]
    while pending:
        session_class = pending.pop()
        seen[session_class.__name__] = session_class
        pending.extend(session_class.__subclasses__())
    return seen


def bridge_error_prefixes() -> tuple[str, ...]:
    """The `error_prefix` values a bridge error type can be built from."""
    return tuple(sorted({str(session_class.error_prefix) for session_class in process_bridge_session_classes().values()}))


def raised_can_error_types_on_the_can_path() -> dict[str, list[str]]:
    """The whole inventory: what `agentic_hil.can` spells and what the bridge builds.

    Two modules for one path, because the CAN adapter bridge is a
    `ProcessBridgeSession` whose refusals are built in `agentic_hil.bridge` and
    reach the recorded failure through `open_process_adapter` unchanged. A
    reader of `agentic_hil.can` alone sees fewer types than a caller can be
    handed, which is exactly how four of them stayed invisible.
    """
    sources = {can_module_under_test: None, bridge_module_under_test: {"error_prefix": bridge_error_prefixes()}}
    found: dict[str, list[str]] = {}
    for module, attributes in sources.items():
        file_name = Path(inspect.getsourcefile(module) or "").name
        for error_type, lines in raised_can_error_types(module, attributes).items():
            found.setdefault(error_type, []).extend(f"{file_name}:{line}" for line in lines)
    return found


def declared_can_error_types() -> dict[str, str]:
    """The `can_` error types `agentic_hil.knowledge` declares, by constant name.

    Where a CAN error type is born: the module carries one constant per type with
    the paragraph that decided it. A type declared there and not keyed here is a
    type whose refusal will answer a serial log.
    """
    return {name: value for name, value in vars(knowledge_module_under_test).items() if name.endswith("_ERROR") and isinstance(value, str) and value.startswith("can_")}

# Distinctive by design, as above: a bus whose receive queue refuses to drain.
QUEUE_BUS_ID = "likely_causes_queue_bus"
QUEUE_CHANNEL = "can517queue"
# And a `process` bus, for the two bridge refusals that had no test at all.
PROCESS_BUS_ID = "likely_causes_process_bus"
PROCESS_CHANNEL = "can523bridge"


def says_any(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text.lower()) for word in words)


def can_report(error_type: str) -> dict:
    """A failure record shaped like the CAN refusals, carrying no causes of its own.

    No `likely_causes` key on purpose: the pass-through branch is already proven
    by #511, and the branch this issue is about is the one that asks a table.
    """
    return {
        "ok": False,
        "tool": "can_session_start",
        "bus_id": "likely_causes_probe_bus",
        "adapter": "socketcan",
        "channel": "can517probe",
        "error_type": error_type,
        "summary": f"CAN session was refused as {error_type}.",
        "target_contacted": False,
        "side_effect_committed": False,
        "side_effect_status": "not_started",
    }


def config_for(tmp_path: Path, *, debugger_type: str = "openocd"):
    return load_config(str(write_config(tmp_path, debugger_type=debugger_type)))


def down_link_config(tmp_path: Path):
    yaml = "".join(
        [
            "can_buses:\n",
            f"  {DOWN_BUS_ID}:\n",
            '    adapter: "socketcan"\n',
            f'    channel: "{DOWN_CHANNEL}"\n',
            "    listen_only: false\n",
        ]
    )
    return load_config(str(write_config(tmp_path, can_buses_yaml=yaml)))


def publish_down_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `/sys/class/net` of this test's own, holding one interface that is down."""
    root = tmp_path / "sys" / "class" / "net" / DOWN_CHANNEL
    root.mkdir(parents=True)
    (root / "flags").write_text(RECORDED_DOWN_FLAGS, encoding="utf-8")
    (root / "operstate").write_text(RECORDED_DOWN_OPERSTATE, encoding="utf-8")
    monkeypatch.setattr(can_module_under_test, "SYSFS_NET_CLASS", str(root.parent))


def queue_bus_config(tmp_path: Path):
    yaml = "".join(
        [
            "can_buses:\n",
            f"  {QUEUE_BUS_ID}:\n",
            '    adapter: "socketcan"\n',
            f'    channel: "{QUEUE_CHANNEL}"\n',
            "    max_buffer_frames: 2\n",
        ]
    )
    return load_config(str(write_config(tmp_path, can_buses_yaml=yaml)))


def process_bus_config(tmp_path: Path, *, executable_exists: bool):
    """A `process` bus, whose bridge executable is there or is not."""
    executable = tmp_path / "can-bridge.py"
    if executable_exists:
        executable.write_text("", encoding="utf-8")
    yaml = "".join(
        [
            "can_buses:\n",
            f"  {PROCESS_BUS_ID}:\n",
            '    adapter: "process"\n',
            f'    channel: "{PROCESS_CHANNEL}"\n',
            f'    executable: "{executable.as_posix()}"\n',
        ]
    )
    return load_config(str(write_config(tmp_path, can_buses_yaml=yaml)))


class RefusingDrainAdapter:
    """An adapter session whose receive queue will not drain: `read` refuses.

    Enough of the adapter-session surface for `CanBusService` to drive it, the
    way `tests/test_hardening.py` drives the drain limit with a queue that never
    empties. Nothing is opened and no bus exists.
    """

    adapter_name = "fake"

    def read(self, max_frames: int, wait_timeout_s: float) -> dict:
        return {"ok": False, "error_type": "can_read_failed", "summary": "CAN adapter failed to read frames.", "backend_error": "staged drain failure"}

    def status(self) -> dict:
        return {"active": True}

    def close(self) -> dict:
        return {"ok": True, "safe_state_confirmed": True, "process_reaped": True}


class NeverEmptyingAdapter:
    """An adapter session whose receive queue keeps answering with a frame.

    The drain is bounded, so a queue that always has one more frame in it walks
    the whole budget and refuses with `can_queue_clear_limit`.
    """

    adapter_name = "fake"

    def read(self, max_frames: int, wait_timeout_s: float) -> dict:
        return {"ok": True, "frames": [{"id": 0x123, "id_hex": "0x123", "extended": False, "rtr": False, "data_hex": "00", "dlc": 1}]}

    def status(self) -> dict:
        return {"active": True}

    def close(self) -> dict:
        return {"ok": True, "safe_state_confirmed": True, "process_reaped": True}


class RefusedOpenBridgeThatWillNotClose:
    """A bridge transport that refuses `open` with an error type of its own and
    then fails its own cleanup, which is the branch where the refusal and a
    cleanup error are reported together."""

    def __init__(self, error_type: str) -> None:
        self.error_type = error_type

    def request(self, method: str, params: dict, timeout_s: float) -> dict:
        return {"ok": False, "error_type": self.error_type, "summary": "CAN adapter bridge refused to open the channel."}

    def close(self) -> dict:
        raise BridgeCleanupError({"ok": False, "error_type": "bridge_process_reap_failed", "summary": "Bridge process cleanup could not be confirmed."})


class MalformedFrameAdapter:
    """An adapter session that answers a read with frame data that cannot be read
    back, which is the second place `can_adapter_invalid_response` is written."""

    adapter_name = "fake"

    def read(self, max_frames: int, wait_timeout_s: float) -> dict:
        return {"ok": True, "frames": [{"id": "not an identifier"}]}

    def status(self) -> dict:
        return {"active": True}

    def close(self) -> dict:
        return {"ok": True, "safe_state_confirmed": True, "process_reaped": True}


class RefusingCloseAdapter:
    """An adapter session that cannot be closed, which is what leaves a bus
    registered for a cleanup retry."""

    adapter_name = "fake"

    def read(self, max_frames: int, wait_timeout_s: float) -> dict:
        return {"ok": True, "frames": []}

    def status(self) -> dict:
        return {"active": True}

    def close(self) -> dict:
        raise RuntimeError("staged close failure")


class RefusingReceiveBus:
    """A python-can bus object whose `recv` raises, so the real direct adapter
    writes the real `can_read_failed` refusal around it."""

    def recv(self, timeout: float) -> object:
        raise OSError("staged receive failure")

    def shutdown(self) -> None:
        return None


def assert_causes_are_about_the_bus(causes: object, error_type: str, context: object) -> None:
    assert isinstance(causes, list) and causes, context
    assert causes not in (COM_PORT_FALLBACK, DEBUGGER_FALLBACK, UNBOUND_FALLBACK), context
    for cause in causes:
        assert isinstance(cause, str) and cause.strip(), context
        assert says_any(cause, BUS_WORDS), f"{cause!r} names nothing about the bus: {context}"
        assert not says_any(cause, OTHER_TRANSPORT_WORDS), f"{cause!r} sends the reader to another transport: {context}"
    assert any(says_any(cause, TYPE_ANCHORS[error_type]) for cause in causes), f"no cause is about {error_type}: {context}"


# ---------------------------------------------------------------------------
# The gap: a CAN report classified over each table the classifier is handed.


@pytest.mark.parametrize("error_type", CAN_ERROR_TYPES)
def test_a_can_report_is_classified_about_the_bus_over_the_com_port_table(tmp_path: Path, error_type: str) -> None:
    """The COM port table is `comports.likely_causes`, keyed by the serial error
    types with `inspect the COM port log for details` for everything else, and a
    CAN error type is everything else."""
    config = config_for(tmp_path)
    write_report(config, can_report(error_type))

    classified = classify_failure_report(config, com_port_likely_causes)

    assert classified["ok"] is True, classified
    assert classified["error_type"] == error_type, classified
    assert_causes_are_about_the_bus(classified["likely_causes"], error_type, classified)


@pytest.mark.parametrize("debugger_type", DEBUGGER_TYPES)
@pytest.mark.parametrize("error_type", CAN_ERROR_TYPES)
def test_a_can_report_is_classified_about_the_bus_through_a_service_on_a_debugger(tmp_path: Path, error_type: str, debugger_type: str) -> None:
    """The whole path an agent takes: the failure is recorded by the CAN tool and
    read back with `classify_last_error`, whose table is the bound debugger's."""
    config = config_for(tmp_path, debugger_type=debugger_type)
    write_report(config, can_report(error_type))
    service = AgenticHILToolService(config)
    try:
        classified = service.call("classify_last_error")
    finally:
        service.close()

    assert classified["ok"] is True, classified
    assert classified["error_type"] == error_type, classified
    assert classified["source_tool"] == "can_session_start", classified
    assert_causes_are_about_the_bus(classified["likely_causes"], error_type, classified)


@pytest.mark.parametrize("error_type", CAN_ERROR_TYPES)
def test_a_can_report_is_classified_the_same_whatever_table_the_backend_hands_over(tmp_path: Path, error_type: str) -> None:
    """A bus failure is a fact about the bus, so the answer must not depend on
    which probe, or no probe at all, happens to be bound in this project."""
    config = config_for(tmp_path)
    write_report(config, can_report(error_type))

    tables = {"com_port": com_port_likely_causes, "unbound": None}
    answers = {name: (UnboundDebuggerBackend(config).classify_last_error() if table is None else classify_failure_report(config, table))["likely_causes"] for name, table in tables.items()}
    for debugger_type in DEBUGGER_TYPES:
        backend_config = config_for(tmp_path / debugger_type, debugger_type=debugger_type)
        write_report(backend_config, can_report(error_type))
        service = AgenticHILToolService(backend_config)
        try:
            answers[debugger_type] = service.call("classify_last_error")["likely_causes"]
        finally:
            service.close()

    assert len(set(map(tuple, answers.values()))) == 1, answers


def test_each_can_error_type_gets_causes_of_its_own(tmp_path: Path) -> None:
    """One answer per type. A list reused across two of them would satisfy every
    assertion above and still tell an operator nothing about which failure
    happened, which is the way a table grown in one sitting goes wrong."""
    config = config_for(tmp_path)
    answers = {}
    for error_type in CAN_ERROR_TYPES:
        write_report(config, can_report(error_type))
        answers[error_type] = classify_failure_report(config, com_port_likely_causes)["likely_causes"]

    assert len(set(map(tuple, answers.values()))) == len(CAN_ERROR_TYPES), answers


def test_the_table_answers_the_interface_down_causes_the_refusal_carries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The two agree, which is what keeps #511's answer from splitting in two.

    The refusal carries its causes and the classifier passes them through, so a
    table that answered something else for the same type would be a second
    wording of a decided answer, reachable from a record written before the
    refusal grew the field.
    """
    publish_down_link(tmp_path, monkeypatch)
    link_config = down_link_config(tmp_path)
    refused = can_module_under_test.socketcan_interface_down(DOWN_BUS_ID, link_config.can_buses[DOWN_BUS_ID])
    assert refused is not None and refused["likely_causes"] == INTERFACE_DOWN_CAUSES, refused

    config = config_for(tmp_path / "classifier")
    write_report(config, can_report(CAN_INTERFACE_DOWN_ERROR))
    classified = classify_failure_report(config, com_port_likely_causes)

    assert classified["likely_causes"] == INTERFACE_DOWN_CAUSES, classified


def test_a_queue_clear_that_will_not_drain_refuses_with_causes_of_its_own(tmp_path: Path) -> None:
    """`can_queue_clear_failed` at the refusal, not only at the classifier.

    The other six types are pinned on a real payload by the modules that own
    them; this one is written by `CanBusService._drain_rx_queue` and had no
    refusal-level test anywhere, so an implementation that keyed the table and
    left this payload bare would have gone unnoticed. The refusal and
    `classify_last_error` have to answer with the same list.
    """
    config = queue_bus_config(tmp_path)
    service = can_module_under_test.CanBusService(config)
    session = can_module_under_test.CanBusSession(QUEUE_BUS_ID, config.can_buses[QUEUE_BUS_ID], RefusingDrainAdapter(), str(tmp_path / "can-queue.jsonl"))
    service.sessions[QUEUE_BUS_ID] = session

    result = service.session_start(QUEUE_BUS_ID, clear_rx_queue=True)

    assert result["ok"] is False, result
    assert result["error_type"] == QUEUE_CLEAR_FAILED_ERROR, result
    assert_causes_are_about_the_bus(result["likely_causes"], QUEUE_CLEAR_FAILED_ERROR, result)
    classified = classify_failure_report(config, com_port_likely_causes)
    assert classified["likely_causes"] == result["likely_causes"], classified


def test_a_queue_that_never_empties_refuses_at_the_drain_limit_with_causes_of_its_own(tmp_path: Path) -> None:
    """`can_queue_clear_limit`, which the container tier pins on the real kernel
    and nothing pinned in the default run: an implementation that keyed the table
    and left this payload bare would have passed every local test."""
    config = queue_bus_config(tmp_path)
    service = can_module_under_test.CanBusService(config)
    session = can_module_under_test.CanBusSession(QUEUE_BUS_ID, config.can_buses[QUEUE_BUS_ID], NeverEmptyingAdapter(), str(tmp_path / "can-limit.jsonl"))
    service.sessions[QUEUE_BUS_ID] = session

    result = service.session_start(QUEUE_BUS_ID, clear_rx_queue=True)

    assert result["ok"] is False and result["error_type"] == QUEUE_CLEAR_LIMIT_ERROR, result
    assert_causes_are_about_the_bus(result["likely_causes"], QUEUE_CLEAR_LIMIT_ERROR, result)
    assert classify_failure_report(config, com_port_likely_causes)["likely_causes"] == result["likely_causes"]


def test_a_read_the_adapter_refuses_carries_causes_of_its_own(tmp_path: Path) -> None:
    """`can_read_failed`, from the direct adapter that writes it: the same case as
    the drain limit above, pinned on the real refusal site rather than on a
    double that hands the type back ready made."""
    config = queue_bus_config(tmp_path)
    service = can_module_under_test.CanBusService(config)
    adapter = can_module_under_test.PythonCanAdapterSession("socketcan", RefusingReceiveBus(), 1.0)
    service.sessions[QUEUE_BUS_ID] = can_module_under_test.CanBusSession(QUEUE_BUS_ID, config.can_buses[QUEUE_BUS_ID], adapter, str(tmp_path / "can-read.jsonl"))

    result = service.read(QUEUE_BUS_ID, 1, 0.0)

    assert result["ok"] is False and result["error_type"] == READ_FAILED_ERROR, result
    assert_causes_are_about_the_bus(result["likely_causes"], READ_FAILED_ERROR, result)
    assert classify_failure_report(config, com_port_likely_causes)["likely_causes"] == result["likely_causes"]


def test_an_open_refused_by_the_bridge_whose_cleanup_also_fails_still_carries_causes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`open_process_adapter` returns the refusal at two places, and the second
    one reports a cleanup error beside it.

    A bridge is a program this project did not write: it may name one of these
    CAN error types and carry no causes with it. Both returns attach the row for
    that reason, and this is the one that a failing close reaches.
    """
    config = process_bus_config(tmp_path, executable_exists=True)
    monkeypatch.setattr(can_module_under_test, "spawn_managed_process", lambda *args, **kwargs: SimpleNamespace(pid=1))
    monkeypatch.setattr(can_module_under_test, "ProcessCanAdapterSession", lambda child, timeout_s=10.0: RefusedOpenBridgeThatWillNotClose(ADAPTER_OPEN_FAILED_ERROR))

    result = can_module_under_test.open_process_adapter(config, PROCESS_BUS_ID, config.can_buses[PROCESS_BUS_ID], False)

    assert result["ok"] is False and result["error_type"] == ADAPTER_OPEN_FAILED_ERROR, result
    assert result["cleanup_required"] is True, result
    assert_causes_are_about_the_bus(result["likely_causes"], ADAPTER_OPEN_FAILED_ERROR, result)


def test_a_read_the_bridge_answers_in_the_wrong_shape_refuses_with_causes_of_its_own(tmp_path: Path) -> None:
    """`can_adapter_invalid_response` on the read path, which is the one place it
    is not covered by another spread.

    `can_read` copies the adapter dictionary into the report unchanged, so the
    causes have to be on the dictionary `invalid_can_bridge_response` builds. The
    open path attaches them a second time in `open_process_adapter`; the read
    path does not, and a bare payload here reaches the operator generic.
    """
    config = queue_bus_config(tmp_path)
    service = can_module_under_test.CanBusService(config)
    adapter = can_module_under_test.ProcessCanAdapterSession(SimpleNamespace(poll=lambda: None, stdout=iter(()), stderr=iter(())), 1.0)
    adapter.request = lambda method, params, timeout_s: {"ok": True, "frames": [], "unexpected": 1}
    service.sessions[QUEUE_BUS_ID] = can_module_under_test.CanBusSession(QUEUE_BUS_ID, config.can_buses[QUEUE_BUS_ID], adapter, str(tmp_path / "can-shape.jsonl"))

    result = service.read(QUEUE_BUS_ID, 1, 0.0)

    assert result["ok"] is False and result["error_type"] == ADAPTER_INVALID_RESPONSE_ERROR, result
    assert_causes_are_about_the_bus(result["likely_causes"], ADAPTER_INVALID_RESPONSE_ERROR, result)
    assert classify_failure_report(config, com_port_likely_causes)["likely_causes"] == result["likely_causes"]


def test_frames_that_cannot_be_read_back_refuse_with_causes_of_their_own(tmp_path: Path) -> None:
    """The second `can_adapter_invalid_response` on the read path: the adapter
    claimed success and handed over frame data `CanBusService` cannot read."""
    config = queue_bus_config(tmp_path)
    service = can_module_under_test.CanBusService(config)
    service.sessions[QUEUE_BUS_ID] = can_module_under_test.CanBusSession(QUEUE_BUS_ID, config.can_buses[QUEUE_BUS_ID], MalformedFrameAdapter(), str(tmp_path / "can-frames.jsonl"))

    result = service.read(QUEUE_BUS_ID, 1, 0.0)

    assert result["ok"] is False and result["error_type"] == ADAPTER_INVALID_RESPONSE_ERROR, result
    assert_causes_are_about_the_bus(result["likely_causes"], ADAPTER_INVALID_RESPONSE_ERROR, result)
    assert classify_failure_report(config, com_port_likely_causes)["likely_causes"] == result["likely_causes"]


def test_a_stop_whose_adapter_will_not_close_refuses_with_causes_of_its_own(tmp_path: Path) -> None:
    """`can_adapter_close_failed` is written in five places in `agentic_hil.can`
    and had no refusal-level test anywhere. It is the refusal an operator reads
    while a bus is still registered for cleanup, so it is the last one that may
    send them to a serial log."""
    config = queue_bus_config(tmp_path)
    service = can_module_under_test.CanBusService(config)
    service.sessions[QUEUE_BUS_ID] = can_module_under_test.CanBusSession(QUEUE_BUS_ID, config.can_buses[QUEUE_BUS_ID], RefusingCloseAdapter(), str(tmp_path / "can-close.jsonl"))
    try:
        result = service.session_stop(QUEUE_BUS_ID)
    finally:
        service.sessions.pop(QUEUE_BUS_ID, None)
        service.close()

    assert result["ok"] is False and result["error_type"] == ADAPTER_CLOSE_FAILED_ERROR, result
    assert_causes_are_about_the_bus(result["likely_causes"], ADAPTER_CLOSE_FAILED_ERROR, result)
    assert classify_failure_report(config, com_port_likely_causes)["likely_causes"] == result["likely_causes"]


def test_a_bus_that_is_not_configured_refuses_with_causes_of_its_own(tmp_path: Path) -> None:
    """`can_bus_not_configured` is a fact about the configuration, and the entry
    it names is `can_buses`, not a port or a probe."""
    config = queue_bus_config(tmp_path)
    service = can_module_under_test.CanBusService(config)
    try:
        result = service.session_start("no_such_bus_in_this_config", clear_rx_queue=False)
    finally:
        service.close()

    assert result["ok"] is False and result["error_type"] == BUS_NOT_CONFIGURED_ERROR, result
    assert_causes_are_about_the_bus(result["likely_causes"], BUS_NOT_CONFIGURED_ERROR, result)
    assert classify_failure_report(config, com_port_likely_causes)["likely_causes"] == result["likely_causes"]


def test_an_interpreter_without_python_can_refuses_with_causes_of_its_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`can_backend_not_available`. Nothing was opened, so the causes are about
    the installation the direct adapters are driven through."""
    config = queue_bus_config(tmp_path)
    monkeypatch.setitem(sys.modules, "can", None)

    result = can_module_under_test.open_python_can_adapter(config, QUEUE_BUS_ID, config.can_buses[QUEUE_BUS_ID], False)

    assert result["ok"] is False and result["error_type"] == BACKEND_NOT_AVAILABLE_ERROR, result
    assert_causes_are_about_the_bus(result["likely_causes"], BACKEND_NOT_AVAILABLE_ERROR, result)


def test_a_bridge_executable_that_is_not_there_refuses_with_causes_of_its_own(tmp_path: Path) -> None:
    """`can_adapter_not_found`, which had no test of any kind."""
    config = process_bus_config(tmp_path, executable_exists=False)

    result = can_module_under_test.open_process_adapter(config, PROCESS_BUS_ID, config.can_buses[PROCESS_BUS_ID], False)

    assert result["ok"] is False and result["error_type"] == ADAPTER_NOT_FOUND_ERROR, result
    assert_causes_are_about_the_bus(result["likely_causes"], ADAPTER_NOT_FOUND_ERROR, result)


def test_a_bridge_process_that_will_not_start_refuses_with_causes_of_its_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`can_adapter_process_start_failed`, the other one with no test of any kind.
    The executable is there and the operating system refused to run it."""
    config = process_bus_config(tmp_path, executable_exists=True)

    def refuse_to_spawn(*args: object, **kwargs: object) -> object:
        raise OSError("staged spawn failure")

    monkeypatch.setattr(can_module_under_test, "spawn_managed_process", refuse_to_spawn)

    result = can_module_under_test.open_process_adapter(config, PROCESS_BUS_ID, config.can_buses[PROCESS_BUS_ID], False)

    assert result["ok"] is False and result["error_type"] == ADAPTER_PROCESS_START_FAILED_ERROR, result
    assert_causes_are_about_the_bus(result["likely_causes"], ADAPTER_PROCESS_START_FAILED_ERROR, result)


def test_the_transport_carries_causes_on_the_error_types_it_builds() -> None:
    """The four types no module spells, at the place they are built.

    `request` on a bridge whose child has exited answers `can_adapter_process_exited`
    out of `agentic_hil.bridge`, and that dictionary is what `open_process_adapter`
    hands on and `CanBusService` writes into the report. If the causes are not on
    it here, they are on nothing that reaches a reader.
    """
    session = can_module_under_test.ProcessCanAdapterSession(SimpleNamespace(poll=lambda: 0, stdout=iter(()), stderr=iter(())), 1.0)

    exited = session.request("open", {}, 1.0)

    assert exited["error_type"] == ADAPTER_PROCESS_EXITED_ERROR, exited
    assert_causes_are_about_the_bus(exited["likely_causes"], ADAPTER_PROCESS_EXITED_ERROR, exited)


def test_a_can_type_no_code_raises_still_answers_the_table_it_was_handed(tmp_path: Path) -> None:
    """The boundary, now that the table names every type the module raises.

    #517's boundary was fifteen real error types, which is the defect #523
    closes. What is left is a `can_`-shaped string that no code writes: the table
    is keyed by name and not by the prefix, so a record carrying one of those
    keeps the generic answer, and a type invented in a report cannot make the
    classifier claim knowledge of a failure this project never raises.
    """
    assert UNRAISED_CAN_ERROR_TYPE not in raised_can_error_types(can_module_under_test), "pick a type no code raises"
    assert UNRAISED_CAN_ERROR_TYPE not in raised_can_error_types(canbroker_module_under_test), "pick a type no code raises"
    config = config_for(tmp_path)
    write_report(config, can_report(UNRAISED_CAN_ERROR_TYPE))

    classified = classify_failure_report(config, com_port_likely_causes)

    assert classified["error_type"] == UNRAISED_CAN_ERROR_TYPE, classified
    assert classified["likely_causes"] == COM_PORT_FALLBACK, classified


# ---------------------------------------------------------------------------
# The gap cannot reopen: the table is checked against the code, not against a list.


def test_every_can_error_type_the_can_path_raises_has_a_row_in_the_table() -> None:
    """The test the issue asks for: read the raised types out of the code.

    Both modules of the one path, `agentic_hil.can` and the process bridge it
    drives its `process` buses through, because a type is no less real for being
    assembled out of a prefix and a kind.

    Red before #523 with the fifteen the issue names and the four the bridge
    builds. Red again the day a twentieth is raised without a row, which is the
    whole reason it reads the code instead of a list.
    """
    raised = raised_can_error_types_on_the_can_path()
    missing = {name: lines for name, lines in sorted(raised.items()) if name not in can_module_under_test.CAN_LIKELY_CAUSES}

    assert missing == {}, f"CAN error types raised with no row in CAN_LIKELY_CAUSES: {missing}"


def test_every_can_error_type_knowledge_declares_has_a_row_in_the_table() -> None:
    """The other end of the same rule, at the place a CAN error type is born.

    A type gets its constant and its paragraph in `agentic_hil.knowledge` before
    anything raises it, so a row asked for here is asked for while the type is
    still being decided rather than after a refusal has shipped without causes.
    """
    declared = declared_can_error_types()
    missing = {name: value for name, value in sorted(declared.items()) if value not in can_module_under_test.CAN_LIKELY_CAUSES}

    assert missing == {}, f"CAN error types declared in knowledge with no row in CAN_LIKELY_CAUSES: {missing}"


def test_the_raised_types_are_the_ones_this_issue_decided() -> None:
    """The inventory itself, so a new raise site is a name somebody has to decide.

    Without this the completeness test above could be satisfied by a row nobody
    thought about; with it, adding a raise makes the diff say which type is new.
    """
    assert sorted(raised_can_error_types_on_the_can_path()) == sorted(CAN_ERROR_TYPES), sorted(raised_can_error_types_on_the_can_path())


def test_the_table_holds_no_row_for_a_type_nothing_raises() -> None:
    """A row for a type no code writes would be an answer to a failure that cannot
    happen, and would hide the day its raise site is deleted."""
    raised = set(raised_can_error_types_on_the_can_path()) | set(raised_can_error_types(canbroker_module_under_test))
    assert sorted(set(can_module_under_test.CAN_LIKELY_CAUSES) - raised) == [], sorted(set(can_module_under_test.CAN_LIKELY_CAUSES) - raised)


def test_the_brokers_own_error_types_stay_with_the_participant_design() -> None:
    """The recorded boundary of this change, and the fact that draws it.

    The broker raises a family of its own about attachment, ownership, gating and
    participant budgets. None of them can become the failure this table is read
    for: the broker writes no report, and no tool or plan step reaches
    `attach_participant` today, so a broker error type never becomes the recorded
    last failure and never falls through to the COM port's line. The participant
    path that will change that is being designed in #500, and a cause list
    written here would be that design decided in passing.

    The `write_report` check is the one that moves: the day the broker writes a
    report, this test fails and the rows are owed.
    """
    broker_source = Path(inspect.getsourcefile(canbroker_module_under_test) or "").read_text(encoding="utf-8")
    assert "write_report" not in broker_source, "the broker now writes reports, so its refusals reach the classifier and owe rows"

    broker_only = sorted(set(raised_can_error_types(canbroker_module_under_test)) - set(raised_can_error_types_on_the_can_path()))

    assert broker_only == sorted(BROKER_ONLY_ERROR_TYPES), broker_only
    assert [name for name in BROKER_ONLY_ERROR_TYPES if name in can_module_under_test.CAN_LIKELY_CAUSES] == []


def test_the_process_bridge_builds_can_error_types_out_of_the_adapter_prefix() -> None:
    """The transport spells no CAN error type and builds four of them anyway.

    `agentic_hil.bridge` writes `f"{self.error_prefix}_{kind}"`, and the CAN
    session class sets that prefix, so `can_adapter_timeout`,
    `can_adapter_process_exited`, `can_adapter_invalid_request` and
    `can_adapter_close_interrupted` exist as error types that appear as a literal
    nowhere in the tree. They are why the inventory reads two modules: read
    `agentic_hil.can` alone and those four are invisible while a caller is being
    handed them.
    """
    assert can_module_under_test.ProcessCanAdapterSession.error_prefix == "can_adapter"
    assert sorted(BRIDGE_ERROR_KINDS) == sorted(call_site_strings(ast.parse(Path(inspect.getsourcefile(bridge_module_under_test) or "").read_text(encoding="utf-8"))).get(("_bridge_error", "kind"), set()))

    built = raised_can_error_types(bridge_module_under_test, {"error_prefix": bridge_error_prefixes()})

    assert sorted(built) == sorted([*CAN_ERROR_TYPES_BUILT_BY_THE_BRIDGE, ADAPTER_INVALID_RESPONSE_ERROR]), sorted(built)


def test_every_session_class_built_on_the_bridge_is_read_for_the_inventory() -> None:
    """The prefixes are collected off the imported classes, so a session class in
    a module nothing imports would carry a prefix the inventory never sees. The
    source tree is the check on that."""
    source_root = Path(inspect.getsourcefile(bridge_module_under_test) or "").parent
    declared = {
        node.name
        for path in sorted(source_root.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef)
        for base in node.bases
        if (base.attr if isinstance(base, ast.Attribute) else base.id if isinstance(base, ast.Name) else "") == ProcessBridgeSession.__name__
    }

    assert declared <= set(process_bridge_session_classes()), sorted(declared - set(process_bridge_session_classes()))


# ---------------------------------------------------------------------------
# The rows themselves: the seven unchanged, and the one the issue calls out.


@pytest.mark.parametrize("error_type", CAN_ERROR_TYPES_FROM_517)
def test_the_rows_decided_in_517_are_unchanged(error_type: str) -> None:
    assert can_module_under_test.CAN_LIKELY_CAUSES[error_type] == ROWS_FROM_517[error_type]


def test_the_listen_only_mode_row_says_what_the_bus_refuses_and_what_decides_it() -> None:
    """`can_listen_only_mode` is the conspicuous one of the fifteen.

    It is not a fault at all: the bus was declared to carry no transmit, and the
    refusal is that declaration being kept. So its causes have to say the two
    things an operator needs, what was refused and where the declaration lives,
    rather than reading like a broken adapter.
    """
    causes = can_module_under_test.CAN_LIKELY_CAUSES[LISTEN_ONLY_MODE_ERROR]
    assert_causes_are_about_the_bus(causes, LISTEN_ONLY_MODE_ERROR, causes)
    joined = " ".join(causes).lower()

    assert any(word in joined for word in ("transmit", "send", "sending", "sent")), causes
    assert "listen_only" in joined, causes
    assert "can_buses" in joined, causes


def test_a_can_report_that_carries_its_own_causes_still_wins(tmp_path: Path) -> None:
    """The pass-through branch is unchanged: a refusal that carried causes has
    already decided them, and the table must not overwrite what the failing call
    measured."""
    config = config_for(tmp_path)
    carried = ["the interface was renamed while this session was being opened"]
    write_report(config, {**can_report(CAN_INTERFACE_NOT_FOUND_ERROR), "likely_causes": carried})

    classified = classify_failure_report(config, com_port_likely_causes)

    assert classified["likely_causes"] == carried, classified


# ---------------------------------------------------------------------------
# The neighbours: the two tables this change does not touch.


def test_the_com_port_causes_are_unchanged() -> None:
    assert com_port_likely_causes("com_port_open_failed") == [
        "configured COM port device does not exist",
        "COM port is already open in another program",
        "USB serial adapter is unplugged or driver is missing",
    ]
    assert com_port_likely_causes("serial_read_failed") == [
        "COM port was disconnected",
        "serial driver reported an I/O error",
        "another process interfered with the port",
    ]
    assert com_port_likely_causes("serial_write_failed") == [
        "COM port was disconnected",
        "serial driver write timed out",
        "target or USB serial adapter stopped responding",
    ]
    assert com_port_likely_causes("serial_write_incomplete") == [
        "configured write_timeout_s is too short for this payload size and baudrate",
        "target or USB serial adapter is applying flow control",
        "COM port was disconnected partway through the write",
    ]
    assert com_port_likely_causes("no_such_serial_error") == COM_PORT_FALLBACK


@pytest.mark.parametrize("debugger_type", DEBUGGER_TYPES)
def test_the_debugger_causes_are_unchanged(tmp_path: Path, debugger_type: str) -> None:
    """Verbatim, and over the public path a caller takes.

    One type per backend rather than the whole table, chosen because the three
    backends word it differently: a rewrite that flattened them would be caught
    here. Read back through `classify_last_error` rather than off the backend's
    table attribute, so this pins what a caller receives.
    """
    config = config_for(tmp_path, debugger_type=debugger_type)
    write_report(config, {"ok": False, "tool": "probe_target", "error_type": "target_not_detected", "summary": "Debugger could not detect the target."})
    service = AgenticHILToolService(config)
    try:
        detected = service.call("classify_last_error")["likely_causes"]
        write_report(config, {"ok": False, "tool": "probe_target", "error_type": "no_such_debugger_error", "summary": "Debugger failed."})
        unknown = service.call("classify_last_error")["likely_causes"]
    finally:
        service.close()

    assert detected == DEBUGGER_TARGET_NOT_DETECTED[debugger_type], detected
    assert unknown == DEBUGGER_FALLBACK, unknown


def test_a_serial_report_still_answers_the_com_port_table(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_report(config, {"ok": False, "tool": "com_read", "port_id": "bench", "error_type": "serial_read_failed", "summary": "COM port read failed."})

    classified = classify_failure_report(config, com_port_likely_causes)

    assert classified["likely_causes"] == com_port_likely_causes("serial_read_failed"), classified


def test_a_type_that_is_neither_still_falls_back_to_the_table_it_was_handed(tmp_path: Path) -> None:
    """The fallbacks stay reachable: this change narrows what falls through, it
    does not remove the generic answer for a type no table knows."""
    config = config_for(tmp_path)
    write_report(config, {"ok": False, "tool": "probe_target", "error_type": "some_unmapped_error", "summary": "Something failed."})

    assert classify_failure_report(config, com_port_likely_causes)["likely_causes"] == COM_PORT_FALLBACK
    assert UnboundDebuggerBackend(config).classify_last_error()["likely_causes"] == UNBOUND_FALLBACK
