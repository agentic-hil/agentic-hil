"""What a host reads about `can_send`, `can_read`, `can_buses_list` and `com_ports_list` (#641).

The four definitions are read the way a host reads them: through a real
`tools/list` request answered by the server. What they must carry is what the
code does today. For can_send: the session, permission and listen-only mode it
needs, that the frame follows the bus mode, what one call puts on the bus and
what its `ok` does and does not mean, what a failed send leaves behind, how
long the adapter may take, every input's spelling, default, range and meaning,
and what a participant on a shared bus needs and spends. For can_read: the
session it needs, where the frames come back, that it takes them off the
queue, what an empty read answers, the failures that end a read, the default,
range and cap of `max_frames`, how long a read waits with and without
`until_id`, the shape and range of `until_id`, and what a participant reads and
spends. For the two listings: what they list and under which key, which names
the session tools take from them, that they open nothing, and, for
com_ports_list, when the host ports are withheld and where each discovery
failure is reported.

The checks are about meaning, not wording. A fact that could be stated the
wrong way round (a default of false or true, accepted or delivered, consumed or
left queued, ok or failed, refused or allowed, a cap on each read or on the
whole call) is checked as a relation inside one sentence or clause, and every
such check is run against its own inverted statement as well, which it must
refuse. A field, error code or key is matched as a token, with or without its
backticks. A test never pins a sentence.

can_read's description was held to its two sentences from before `until_id`
plus one more by tests/test_read_until.py, because every description then
shared one total budget. That budget is gone, so test_read_until.py now keeps
what those sentences mean for can_read and no longer their wording, and what
decides how a read is used is asked of the description itself here.

The second half holds the behaviour those definitions describe, with python-can
and pyserial faked, where no existing test already holds it. The rest is held
elsewhere and not repeated: the listen-only refusal of a send
(tests/test_can_listen_only.py), the classic and FD payload limits and the
remote frame on an FD bus (tests/test_can_frame_and_routing.py, which also
shows a send on an `fd: true` bus goes out as an FD frame), a session that is
not open (tests/test_tool_definition_can.py), a participant's filter, budget
and own frames (tests/test_can_broker.py), an adapter that fails a send or a
read (tests/test_can_interface_down.py, tests/test_can_likely_causes.py), the
rest of `until_id` (tests/test_read_until.py), and host COM discovery refused
on a version-1 configuration whose ports do not allow reading
(tests/test_hardening.py).
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config
from test_can_frame_and_routing import fake_can_module
from test_tool_definition_can import (
    FALSE,
    QueuedBus,
    clauses,
    definition,
    listed_tools,
    names,
    participant_required_on_a_shared_bus,
    property_text,
    stated,
)
from test_tool_descriptions import DESCRIPTION_LIMIT, PROPERTY_DESCRIPTION_LIMIT

import agentic_hil
from agentic_hil.config import load_config
from agentic_hil.tools import AgenticHILToolService

SEND = "can_send"
READ = "can_read"
BUSES = "can_buses_list"
PORTS = "com_ports_list"
TOOLS = (SEND, READ, BUSES, PORTS)

# The annotations as they stand. The definitions are rewritten around them, so
# what they say must stay true of the text and of the code.
ANNOTATIONS = {
    SEND: {"title": "Send a CAN frame", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    READ: {"title": "Read CAN frames", "readOnlyHint": True, "openWorldHint": False},
    BUSES: {"title": "List CAN buses", "readOnlyHint": True, "openWorldHint": False},
    PORTS: {"title": "List COM ports", "readOnlyHint": True, "openWorldHint": False},
}

# What each definition still says it stands in for (tests/test_agentic_hil.py
# and tests/test_tool_descriptions.py hold the words; here they stay a route).
STANDS_IN_FOR = {SEND: (r"cansend",), READ: (r"candump",), BUSES: (r"ip link", r"candump"), PORTS: (r"picocom",)}

# Device locks are machine-wide, so every bus opened here gets a channel no
# other test module names.
CHANNEL = "vcan641doc"
WIDE_CHANNEL = "vcan641wide"
BUS_TIMEOUT_S = 2.5
BUS_MAX_BUFFER_FRAMES = 4
BENCH_YAML = (
    "can_buses:\n"
    "  bench:\n"
    '    adapter: "socketcan"\n'
    f'    channel: "{CHANNEL}"\n'
    f"    timeout_s: {BUS_TIMEOUT_S}\n"
    f"    max_buffer_frames: {BUS_MAX_BUFFER_FRAMES}\n"
)
# A bus whose own timeout is past the 60 s every read is held to.
WIDE_YAML = f'can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "{WIDE_CHANNEL}"\n    timeout_s: 120\n'
PORT_ID = "tooldef641"
COM_YAML = f'com_ports:\n  {PORT_ID}:\n    device: "/dev/ttyTOOLDEF641"\n'


@pytest.fixture
def listed(tmp_path: Path) -> dict[str, dict]:
    return listed_tools(tmp_path)


def field(name: str) -> str:
    """A result field, error code or key as one token, with or without backticks."""
    return rf"(?<![\w]){re.escape(name)}(?![\w])"


# ---------------------------------------------------------------------------
# The relations a definition could state the wrong way round. Each is a named
# check, so the controls below can show it refuses the inverted claim.

UNBOUNDED = r"\b(unlimited|unbounded|no limit|longer than|beyond|exceed|infinite|forever)"
# A bound that starts at zero: "0 to", "0-" or "0..", and not the 0 of a 0x.
FROM_ZERO = r"(?<![\w.])0\s*(?:to|-|\.\.)\s*"
# Every length an FD payload may have (can.py:2236).
FD_LENGTHS = (FROM_ZERO + r"8(?![\w])", *(rf"(?<![\w.]){length}(?![\w])" for length in (12, 16, 20, 24, 32, 48, 64)))
DEFAULT_TEN_S = r"\bdefault\w*\s*:?\s*(?:is\s+)?10\s*s\b|\b10\s*s\b[^.;,]{0,10}\bby default\b"
NOT_SPENT = r"\b(not|never|free|no)\b"


def write_permission_gates_the_send(text: str) -> bool:
    """can.py:730-731: without the bus `allow_write` the send is `permission_denied`."""
    return stated(text, field("allow_write"), field("permission_denied"), unless=r"\b(not needed|never)\b")


def listen_only_refuses_the_send(text: str) -> bool:
    """can.py:727-729, 1095: a `listen_only` bus refuses every send, before the permission check."""
    return stated(text, field("listen_only"), field("can_listen_only_mode"), unless=r"\b(sends normally|still sends|allowed|never)\b")


def ok_is_acceptance_not_delivery(text: str) -> bool:
    """can.py:1174-1193: `ok` is the adapter taking the frame, not an ACK or a receipt."""
    return stated(text, r"\baccept", r"\b(not|no)\b[^.;]*\b(ack|acked|acknowledg|deliver|arriv|receiv)")


def a_repeat_sends_again(text: str) -> bool:
    """can.py:717-763: nothing deduplicates; every call hands the adapter a new frame."""
    return stated(text, r"\b(repeat|again|each call|every call|retry)\b", r"\b(sends?|transmits?|puts?)\b", unless=r"\b(not|never|no|nothing|ignored|no-op|deduplicat\w*|only once)\b")


def send_is_bounded_by_the_bus_timeout(text: str) -> bool:
    """can.py:1199 and 2043-2044 with config.py:3699: the adapter is given the bus `timeout_s`, 10 s unless configured."""
    return stated(text, field("timeout_s"), r"\b10\s*s\b", unless=UNBOUNDED)


def failed_send_may_have_gone_out(text: str) -> bool:
    """can.py:748-760, knowledge.py:194-198: `can_send_failed` carries
    `cleanup_required`, because the frame may be on the bus already."""
    return stated(
        text,
        field("can_send_failed"),
        field("cleanup_required"),
        r"\b(may|might)\b[^.;]{0,30}\b(gone out|be out|been sent|reached the bus)\b",
        unless=r"\b(nothing|never|not)\b[^.;]{0,30}\b(sent|gone out|out)\b",
    )


def fd_bus_sends_fd_frames(text: str) -> bool:
    """can.py:1163-1170, 1198: on an `fd: true` bus the frame goes out as CAN FD."""
    return stated(
        text,
        r"`?fd`?\s*[:=]\s*`?true\b|\bFD bus",
        r"\bCAN FD\b|\bFD frames?\b",
        unless=r"\bonly classic\b|\bclassic only\b|\b(not|never)\b[^.;]{0,20}\b(CAN FD|FD frames?)\b",
    )


def sent_frame_called_classic(text: str) -> bool:
    """The claim can_send must not make: a clause that has the frame it sends
    classic without naming FD beside it. Saying what a classic bus takes is
    not that claim."""
    sent_classic = r"\b(send|sends|sent|transmits?)\b[^.;]{0,30}\bclassic\b|\bclassic\b[^.;]{0,30}\b(is |are )?(sent|transmitted)\b"
    return any(names(sent_classic, clause) and not names(r"\bFD\b", clause) for clause in clauses(text))


def id_ranges_by_kind(text: str) -> bool:
    """can.py:2249-2251: 0 to 0x7FF for a standard id, 0 to 0x1FFFFFFF for an extended one."""
    return stated(
        text,
        FROM_ZERO + r"0x7ff\b",
        FROM_ZERO + r"0x1fffffff\b[^.;,]{0,30}\b(extended|29-bit)\b|\b(extended|29-bit)\b[^.;,]{0,30}" + FROM_ZERO + r"0x1fffffff\b",
        unless=r"0x7ff[^.;,]{0,15}\b(when|if)\s+extended",
    )


def defaults_to_false(text: str) -> bool:
    """can.py:2245-2246: `extended` and `rtr` are false unless given."""
    return stated(text, r"\bdefaults?\b[^.;]*\bfalse\b|\bfalse\b[^.;]*\bdefault", unless=r"\bdefaults?\s*(?:is\s+|to\s+|:\s*)?`?true\b")


def extended_meaning(text: str) -> bool:
    """can.py:2245, 1198: false is an 11-bit standard id, true a 29-bit extended one."""
    standard = stated(text, r"\bfalse\b[^.;,]{0,40}\b(11-bit|standard)\b", unless=r"\bfalse\b[^.;,]{0,40}\b(29-bit|extended)\b")
    extended = stated(text, r"\btrue\b[^.;,]{0,40}\b(29-bit|extended)\b", unless=r"\btrue\b[^.;,]{0,40}\b(11-bit|standard)\b")
    return standard and extended


def rtr_meaning(text: str) -> bool:
    """can.py:2245-2246, 1198: false sends a data frame, true a remote frame."""
    data = stated(text, r"\bfalse\b[^.;]{0,40}\bdata frame\b", unless=r"\bfalse\b[^.;]{0,40}\bremote\b")
    remote = stated(text, r"\btrue\b[^.;]{0,40}\bremote\b", unless=r"\btrue\b[^.;]{0,40}\bdata frame\b")
    return data and remote


def remote_frames_refused_on_fd(text: str) -> bool:
    """can.py:2252-2264: a remote frame on an `fd: true` bus is `can_fd_remote_frame_unsupported`."""
    return stated(text, field("can_fd_remote_frame_unsupported"), r"\bfd\b", unless=r"\b(classic|non-fd)\b")


def hex_pairs(text: str) -> bool:
    """can.py:2312-2316: an even count of hex digits, two per byte."""
    return stated(text, r"\bhex", r"\b(pairs?|two digits|two hex digits|even)\b", unless=r"\bodd\b[^.;]*\b(accepted|allowed)\b")


def whitespace_ignored(text: str) -> bool:
    """can.py:2313: whitespace is dropped before the digits are read."""
    return stated(text, r"\b(whitespace|spaces?)\b", r"\b(ignored|allowed|skipped|dropped)\b", unless=r"\bnot (ignored|allowed|skipped|dropped)\b|\brefused\b")


def data_defaults_to_empty(text: str) -> bool:
    """can.py:2266: `data_hex` is "" unless given, a frame with no data bytes."""
    return stated(text, r"\bdefault", r"\bempty\b|\"\"|\bno data\b|\b0 bytes\b|\bzero bytes\b")


def classic_payload_capped_at_eight(text: str) -> bool:
    """can.py:2283-2293: a classic bus carries at most 8 data bytes, else `can_classic_frame_too_large`."""
    return stated(text, r"\bclassic\b", r"\b8\b|\beight\b", field("can_classic_frame_too_large"), unless=r"\b(64|any length)\b")


def fd_lengths_named(text: str) -> bool:
    """can.py:2236, 2271-2282: an FD payload is one of 0 to 8, 12, 16, 20, 24, 32, 48 or 64 bytes, every one of them named."""
    return stated(text, field("can_fd_frame_length_invalid"), *FD_LENGTHS, unless=r"\bany length\b")


def frames_are_consumed(text: str) -> bool:
    """can.py:1205-1217, canbroker.py:1147-1149: the frames a read returns are off the queue."""
    return stated(text, r"\b(consum|taken off|takes? them off|removed? from|off the queue)", unless=r"\b(not|never|nothing)\b[^.;]*\b(consum|removed|taken)")


def an_empty_read_is_ok(text: str) -> bool:
    """can.py:807: nothing queued answers ok, `frames_read: 0`."""
    return stated(text, r"\b(no frames?|nothing|empty|none)\b", r"\bok\b", unless=r"\bnot\s+ok\b|\b(error|fails?|failure|refused)\b")


def frames_come_back_with_their_count(text: str) -> bool:
    """can.py:807: the frames come back in `frames`, how many in `frames_read`."""
    return stated(text, r"\b(in|into)\s+`?frames`?(?![\w])", field("frames_read"))


def invalid_response_restarts_the_session(text: str) -> bool:
    """can.py:806, knowledge.py:3189-3200: after `can_adapter_invalid_response`
    the session is not read on; it is stopped with can_session_stop and opened again."""
    return stated(
        text,
        field("can_adapter_invalid_response"),
        r"can_session_stop|\brestart|\b(stop|close)\b[^.;]{0,30}\b(open|start)",
        unless=r"\b(read again|retry|keep reading|same session)\b",
    )


def max_frames_defaults_to_the_buffer(text: str) -> bool:
    """can.py:778: no `max_frames` asks for the bus `max_buffer_frames`."""
    return stated(text, r"\bdefaults?\b[^.;,]*max_buffer_frames|max_buffer_frames`?[^.;,]*\bby default\b")


def wait_defaults_to_zero_and_returns_at_once(text: str) -> bool:
    """can.py:779, 792: no `wait_timeout_s` is 0, so the read answers with what is already queued."""
    return stated(text, r"\bdefault\w*\s*:?\s*(?:is\s+)?`?0(?:\.0)?\b(?!\.)", r"\b(at once|immediately|without waiting|no wait)\b", unless=r"\bwaits? for\b")


def plain_wait_is_capped(text: str) -> bool:
    """can.py:792, canbroker.py:1144: a read without until_id waits at most
    the bus `timeout_s` and never past 60 s. The cap belongs to that read, not
    to every call: with until_id the call has a budget of its own."""
    return stated(text, r"\bwithout `?until_id\b", field("timeout_s"), r"\b60\s*s\b", unless=UNBOUNDED + r"|\b(whole|entire|total)\b")


def until_wait_budget(text: str) -> bool:
    """readuntil.py:24-36, can.py:786-790: with until_id the call waits 10 s
    unless given and never past 60 s, whatever the bus `timeout_s`; each adapter
    read inside it keeps the plain cap (can.py:825)."""
    return stated(text, field("until_id"), DEFAULT_TEN_S, r"\b60\s*s\b", unless=field("timeout_s") + r"|\bwithout `?until_id\b|" + UNBOUNDED)


def until_id_shape(text: str) -> bool:
    """readuntil.py:96-108, contracts.py UNTIL_ID: one id, or a list of 1 to 8."""
    return stated(text, r"\b(list|array)\b[^.;]{0,20}\b(of )?(up to|1 to|at most)\s*8\b|\b(up to|1 to|at most)\s*8\b[^.;]{0,10}\bids\b", unless=r"\b(any number|unlimited)\b")


def until_id_range(text: str) -> bool:
    """readuntil.py:109-113: every id is 0 to 0x1FFFFFFF."""
    return stated(text, FROM_ZERO + r"0x1fffffff\b")


def own_frames_excluded(text: str) -> bool:
    """canbroker.py:1190: a participant is never handed the frames it sent itself."""
    return stated(
        text,
        r"\b(never|not|no|except|excluding|without)\b\s+(?:\w+\s+){0,3}?own\s+(sends|frames)\b|\bown\s+(sends|frames)\s+(are\s+)?(excluded|skipped|never|not)\b",
        unless=r"\balso (gets|receives|reads)\b[^.;]{0,20}\bown\b",
    )


def participant_write_grant(text: str) -> bool:
    """can.py:736-737, canbroker.py:1068-1069: a participant's send also needs its own `allow_write`."""
    return stated(text, field("allow_write"), r"\b(its own|own|participant'?s?)\b", unless=r"\b(not needed|not required|ignored|never)\b|\bbus grant\b")


def sends_spend_the_budget(text: str) -> bool:
    """canbroker.py:1084-1086, 1235-1240: every frame a participant sends spends its budget."""
    return stated(text, r"\bbudget\b", r"\b(sends?|sent)\b", r"\b(spends?|spent|uses?|costs?)\b", unless=NOT_SPENT)


def reads_spend_the_budget(text: str) -> bool:
    """canbroker.py:1147-1151: every frame a participant reads spends its budget as well."""
    return stated(text, r"\bbudget\b", r"\bread\w*\b", r"\b(spends?|spent|uses?|costs?)\b", unless=NOT_SPENT)


def exhaustion_ends_the_run(text: str) -> bool:
    """canbroker.py:1242-1247: a spent budget aborts the participant's run; the bus keeps running."""
    return stated(text, field("can_participant_frame_budget_exhausted"), r"\b(ends?|abort\w*|stops?)\b", unless=r"\b(reset\w*|refill\w*|continues?|carries on)\b")


def sent_frame_echoed(text: str) -> bool:
    """can.py:761, 2319-2320: the result carries the frame as it was sent."""
    return stated(text, r"\b(echo\w*|result|returns?)\b", field("frame"), unless=r"\b(no|not|without)\b")


def buses_keyed_by_bus_id(text: str) -> bool:
    """can.py:483-499: the buses come back in `buses`, one entry per bus_id."""
    return stated(text, r"\b(in|into)\s+`?buses`?(?![\w])", field("bus_id"), r"\b(keyed|by)\b")


def ports_keyed_by_port_id(text: str) -> bool:
    """comports.py:953-957, 970-975: the configured ports come back in `ports`, one entry per port_id."""
    return stated(text, r"\b(in|into)\s+`?ports`?(?![\w])", field("port_id"), r"\b(keyed|by)\b")


def opens_nothing(text: str) -> bool:
    """comports.py:953-975 and can.py:483-499: a listing reads configuration and state only."""
    return stated(text, r"\b(opens? no|opens? nothing|without opening|nothing is opened|no (port|adapter) is opened)\b")


def needs_no_session(text: str) -> bool:
    return stated(text, r"\b(no|without|nor)\b[^.;]*\bsession\b|\bsession\b[^.;]*\bnot (needed|required)\b", unless=r"\bneeds an? (active |open )?session\b")


def port_id_not_device_path(text: str) -> bool:
    """comports.py:953-957: the configured entries are keyed by port_id, the name the session tools take."""
    return stated(text, field("port_id"), r"\b(not|never|rather than|instead of)\b[^.;]*\b(device|path)\b")


def host_ports_withheld_without_a_readable_port(text: str) -> bool:
    """comports.py `list_ports`: on a version-1 file host discovery runs only if a configured port may be
    read, so with none configured or none readable `available_com_ports` is `permission_denied`."""
    return stated(text, field("permission_denied"), r"\b(no|none|unless|without)\b", r"\bconfigured\b|`com_ports`", unless=r"\b(always listed|whatever)\b")


def discovery_failure_reported_inside(text: str, code: str) -> bool:
    """comports.py:66-94, 968-975: a discovery failure is reported inside `available_com_ports`."""
    return stated(text, field("available_com_ports"), field(code), unless=r"\b(the )?(call|tool|listing) (fails|is refused)\b")


def missing_pyserial_reported_inside(text: str) -> bool:
    """comports.py:67-78: pyserial that will not import is `serial_backend_not_available`."""
    return discovery_failure_reported_inside(text, "serial_backend_not_available")


def os_error_reported_inside(text: str) -> bool:
    """comports.py:86-94: an OS error while enumerating is `com_port_discovery_failed`."""
    return discovery_failure_reported_inside(text, "com_port_discovery_failed")


def the_listing_stays_ok(text: str) -> bool:
    """comports.py:970-975: the call answers ok whatever host discovery answered."""
    return stated(text, r"\bok\b", r"\b(stays?|still|remains?|itself)\b", unless=r"\bnot ok\b|\bfails\b")


def claims_an_effect(text: str) -> bool:
    """What a read-only tool must not claim of itself: that it transmits or opens something."""
    return stated(text, r"\b(transmits?|sends? (a|one|the) frame|opens? (the|a|each|every) (port|adapter|bus))\b", unless=r"\b(no|not|never|nothing|without)\b")


RELATIONS: list[tuple[Callable[[str], bool], str, str]] = [
    (write_permission_gates_the_send, "Needs the bus `allow_write` (else `permission_denied`).", "`allow_write` is not needed; `permission_denied` never comes."),
    (listen_only_refuses_the_send, "A `listen_only` bus refuses it with `can_listen_only_mode`.", "On a `listen_only` bus the frame is allowed, never `can_listen_only_mode`."),
    (ok_is_acceptance_not_delivery, "`ok` means the adapter accepted the frame, not that a node ACKed it.", "`ok` means the adapter accepted it and a node ACKed it."),
    (ok_is_acceptance_not_delivery, "`ok` is acceptance by the adapter, no proof of delivery.", "`ok` means a node received and acknowledged the frame."),
    (a_repeat_sends_again, "Every call transmits; a repeat sends again.", "A repeat call is ignored and sends nothing."),
    (a_repeat_sends_again, "A repeat sends again.", "A repeat does not send again."),
    (send_is_bounded_by_the_bus_timeout, "The adapter has the bus `timeout_s` (default 10 s) to take the frame.", "The adapter may take longer than `timeout_s` (default 10 s)."),
    (send_is_bounded_by_the_bus_timeout, "The adapter gets the bus `timeout_s` (default 10 s).", "The adapter gets `wait_timeout_s` (default 10 s)."),
    (failed_send_may_have_gone_out, "`can_send_failed` sets `cleanup_required`: the frame may have gone out.", "`can_send_failed` sets `cleanup_required`, but the frame was never sent."),
    (failed_send_may_have_gone_out, "`can_send_failed` comes with `cleanup_required`, as it might have been sent.", "`can_send_failed`: the frame may have gone out, so send it again."),
    (fd_bus_sends_fd_frames, "A classic bus takes 8 bytes. An `fd: true` bus sends CAN FD frames.", "FD buses send only classic CAN frames."),
    (fd_bus_sends_fd_frames, "Send one CAN frame (CAN FD on an `fd: true` bus).", "On an `fd: true` bus the frame is not sent as CAN FD."),
    (sent_frame_called_classic, "Send one classic CAN frame on an active configured CAN bus session.", "A classic bus takes 8 bytes. An `fd: true` bus sends CAN FD frames."),
    (id_ranges_by_kind, "0 to 0x7FF, or 0 to 0x1FFFFFFF when extended.", "0 to 0x7FF when extended, else 0 to 0x1FFFFFFF."),
    (id_ranges_by_kind, "Standard ids 0 to 0x7FF, extended ids 0 to 0x1FFFFFFF.", "Any id from 0 to 0x1FFFFFFF."),
    (id_ranges_by_kind, "0 to 0x7FF, or 0 to 0x1FFFFFFF when extended.", "1 to 0x7FF, or 1 to 0x1FFFFFFF when extended."),
    (defaults_to_false, "Default false: 11-bit standard id.", "Default true: 29-bit extended id."),
    (defaults_to_false, "False by default.", "Default true; false sends a standard id."),
    (extended_meaning, "Default false: 11-bit standard id. True: 29-bit extended id.", "Default false: 29-bit extended identifiers."),
    (extended_meaning, "False: a standard 11-bit id. True: an extended 29-bit id.", "False: an extended id. True: a standard id."),
    (rtr_meaning, "Default false: a data frame. True sends a remote frame.", "Default false: a remote frame. True sends a data frame."),
    (remote_frames_refused_on_fd, "An `fd: true` bus refuses it with `can_fd_remote_frame_unsupported`.", "Remote frames work on an FD bus; `can_fd_remote_frame_unsupported` is for classic buses."),
    (hex_pairs, "Hex byte pairs.", "Hex digits, any count."),
    (whitespace_ignored, "Whitespace is ignored.", "Whitespace is not allowed."),
    (data_defaults_to_empty, "Default empty: no data bytes.", "Default 00."),
    (classic_payload_capped_at_eight, "Classic over 8 bytes: `can_classic_frame_too_large`.", "A classic bus takes up to 64 bytes, then `can_classic_frame_too_large`."),
    (fd_lengths_named, "FD only 0-8,12,16,20,24,32,48,64: `can_fd_frame_length_invalid`.", "FD only 12 and 64 bytes: `can_fd_frame_length_invalid`."),
    (fd_lengths_named, "FD takes 0 to 8, 12, 16, 20, 24, 32, 48 or 64 bytes, else `can_fd_frame_length_invalid`.", "An FD bus takes any length up to 64, else `can_fd_frame_length_invalid`."),
    (frames_are_consumed, "Returned frames are consumed: a later read does not return them.", "Frames are not consumed; reading leaves them queued."),
    (an_empty_read_is_ok, "Nothing queued is still ok, with `frames_read: 0`.", "An empty queue fails the call."),
    (an_empty_read_is_ok, "An empty read is still ok, `frames_read: 0`.", "empty not ok (frames_read: 0)"),
    (frames_come_back_with_their_count, "Read CAN frames into `frames` with `frames_read`.", "Read CAN frames; `frames_read` says how many."),
    (invalid_response_restarts_the_session, "`can_adapter_invalid_response` means can_session_stop, then start again.", "`can_adapter_invalid_response`: read again on the same session."),
    (max_frames_defaults_to_the_buffer, "Default `max_buffer_frames`.", "Default 1, at most `max_buffer_frames`."),
    (wait_defaults_to_zero_and_returns_at_once, "Default 0: return what is queued at once.", "Default 10 s: wait for a frame."),
    (wait_defaults_to_zero_and_returns_at_once, "By default 0, so it answers at once.", "Default 0.5 s, then answers at once."),
    (plain_wait_is_capped, "Without until_id: at most the bus `timeout_s` (default 10 s) and 60 s.", "Capped at the bus `timeout_s` (default 10 s) and 60 s."),
    (plain_wait_is_capped, "Without until_id it waits at most the bus `timeout_s` and 60 s.", "Without until_id the whole call waits the bus `timeout_s`, beyond 60 s."),
    (until_wait_budget, "With until_id: default 10 s, at most 60 s.", "With until_id: default 10 s, at most the bus `timeout_s` and 60 s."),
    (until_wait_budget, "With until_id it waits 10 s by default, at most 60 s.", "Without until_id: default 10 s, at most 60 s."),
    (until_id_shape, "An id to wait for, or a list of 1 to 8.", "An id to wait for, or a list of up to 16."),
    (until_id_shape, "One id, or up to 8 ids.", "One id, or any number of ids up to 8 bytes long."),
    (until_id_range, "Integer id (0 to 0x1FFFFFFF) to wait for.", "Integer id (1 to 0x1FFFFFFF) to wait for."),
    (own_frames_excluded, "It gets only the frames its filter accepts, never its own sends.", "It also receives its own frames."),
    (own_frames_excluded, "Never its own sends.", "reads its own frames, not peer frames"),
    (participant_write_grant, "Needs its own `allow_write`.", "Its own `allow_write` is not needed; the bus grant covers it."),
    (sends_spend_the_budget, "Sends spend its budget.", "Sends are free; only reads spend its budget."),
    (reads_spend_the_budget, "Each frame read spends its budget.", "Reads never spend its budget."),
    (exhaustion_ends_the_run, "`can_participant_frame_budget_exhausted` ends its run.", "At `can_participant_frame_budget_exhausted` the budget resets and nothing ends."),
    (sent_frame_echoed, "The result echoes the sent `frame`.", "The result carries no frame."),
    (buses_keyed_by_bus_id, "List every `can_buses` entry in `buses`, keyed by bus_id.", "List configured CAN buses by bus_id."),
    (ports_keyed_by_port_id, "List `com_ports` entries in `ports` by port_id.", "List configured named COM ports and detected host serial ports."),
    (opens_nothing, "Opens no port.", "Opens each port to read its state."),
    (needs_no_session, "Needs no session, opens no adapter.", "Needs an active session first."),
    (port_id_not_device_path, "Entries by port_id, the name the session tools take (not a device path).", "port_id is the device path, such as COM3."),
    (host_ports_withheld_without_a_readable_port, "`available_com_ports` is `permission_denied` unless a configured port may be read.", "`available_com_ports` is `permission_denied` when a configured port may be read."),
    (host_ports_withheld_without_a_readable_port, "With no configured port to read, host ports are `permission_denied`.", "Host ports are always listed; `permission_denied` covers configured ports only."),
    (missing_pyserial_reported_inside, "`available_com_ports` holds `serial_backend_not_available` without pyserial.", "Without pyserial the call fails with `serial_backend_not_available`."),
    (missing_pyserial_reported_inside, "`available_com_ports` holds `serial_backend_not_available` without pyserial.", "`available_com_ports` holds `com_port_discovery_failed` on an OS error."),
    (os_error_reported_inside, "`available_com_ports` holds `com_port_discovery_failed` on an OS error.", "On an OS error the listing fails with `com_port_discovery_failed`."),
    (os_error_reported_inside, "`available_com_ports` holds `com_port_discovery_failed` on an OS error.", "`available_com_ports` holds `serial_backend_not_available` without pyserial."),
    (the_listing_stays_ok, "The call itself stays ok.", "The call is not ok then."),
    (claims_an_effect, "It transmits a frame.", "It transmits nothing."),
    (claims_an_effect, "Opens the adapter to read its state.", "Opens no adapter."),
]


@pytest.mark.parametrize(("check", "matches", "refused"), RELATIONS, ids=[f"{check.__name__}-{index}" for index, (check, _, _) in enumerate(RELATIONS)])
def test_each_relation_check_refuses_its_inverted_statement(check: Callable[[str], bool], matches: str, refused: str) -> None:
    assert check(matches), matches
    assert not check(refused), refused


# ---------------------------------------------------------------------------
# Every input says what it means, within the size every entry is held to.


@pytest.mark.parametrize("tool_name", TOOLS)
def test_every_input_carries_its_own_description_within_the_limits(listed: dict[str, dict], tool_name: str) -> None:
    tool = listed[tool_name]
    properties = tool["inputSchema"].get("properties", {})

    undescribed = sorted(name for name, schema in properties.items() if not str(schema.get("description", "")).strip())
    assert undescribed == [], f"{tool_name}: {undescribed}"
    over = {name: len(schema["description"]) for name, schema in properties.items() if len(schema["description"]) > PROPERTY_DESCRIPTION_LIMIT}
    assert over == {}, over
    assert len(tool["description"]) <= DESCRIPTION_LIMIT, len(tool["description"])


@pytest.mark.parametrize("tool_name", [BUSES, PORTS])
def test_the_listings_take_no_input(listed: dict[str, dict], tool_name: str) -> None:
    """can_buses_list and com_ports_list read no argument (tools.py:865, 870)."""
    schema = listed[tool_name]["inputSchema"]

    assert schema.get("properties", {}) == {}, schema
    assert schema.get("required", []) == [], schema


@pytest.mark.parametrize("tool_name", TOOLS)
def test_each_definition_still_says_what_it_stands_in_for(listed: dict[str, dict], tool_name: str) -> None:
    text = listed[tool_name]["description"]

    for command in STANDS_IN_FOR[tool_name]:
        assert stated(text, r"\binstead of\b", command), (command, text)


@pytest.mark.parametrize("tool_name", [SEND, READ])
def test_bus_id_says_which_entry_it_names_and_where_to_find_them(listed: dict[str, dict], tool_name: str) -> None:
    """A bus is chosen by the name of its `can_buses` entry (can.py:901-907);
    can_buses_list shows them, and an unknown name is `can_bus_not_configured`."""
    text = property_text(listed[tool_name], "bus_id")
    whole = definition(listed[tool_name])

    assert names(field("can_buses"), text), text
    assert names(field("can_buses_list"), whole), whole
    assert names(field("can_bus_not_configured"), whole), whole


# ---------------------------------------------------------------------------
# can_send: what it needs, what one call does, what it answers.


def test_can_send_says_what_it_needs_before_it_transmits(listed: dict[str, dict]) -> None:
    """In the order the code checks them (can.py:717-737): the bus mode, the
    bus `allow_write`, then an open session (`session_not_active`, can.py:921,
    which can_session_stop's definition also names)."""
    text = listed[SEND]["description"]

    assert names(field("can_session_start"), text), text
    assert names(field("session_not_active"), text), text
    assert write_permission_gates_the_send(text), text
    assert listen_only_refuses_the_send(text), text


def test_can_send_says_what_ok_means_what_a_repeat_does_and_how_long_it_may_take(listed: dict[str, dict]) -> None:
    """`ok` is the adapter taking the frame (can.py:1174-1193), every call sends
    again (can.py:717-763), and the adapter has the bus `timeout_s`
    (can.py:1199, 2043-2044)."""
    text = definition(listed[SEND])

    assert ok_is_acceptance_not_delivery(listed[SEND]["description"]), listed[SEND]["description"]
    assert a_repeat_sends_again(text), text
    assert send_is_bounded_by_the_bus_timeout(text), text


def test_can_send_names_its_result_what_a_failure_leaves_and_where_replies_are_read(listed: dict[str, dict]) -> None:
    """The result echoes the frame as sent (can.py:761, 2319-2320); an adapter
    that fails is `can_send_failed` (can.py:1202), and a failure that does not
    rule the frame out carries `cleanup_required` (can.py:758-759), because the
    frame may be on the bus; replies come from can_read."""
    text = definition(listed[SEND])

    assert sent_frame_echoed(text), text
    assert failed_send_may_have_gone_out(text), text
    assert names(field("can_read"), text), text


def test_can_send_says_the_frame_follows_the_bus_mode(listed: dict[str, dict]) -> None:
    """On an `fd: true` bus the frame goes out as CAN FD (can.py:1163-1170, 1198;
    tests/test_can_frame_and_routing.py:159), so the definition says so and no
    clause has the frame it sends classic."""
    text = definition(listed[SEND])

    assert fd_bus_sends_fd_frames(text), text
    assert not sent_frame_called_classic(text), text


def test_frame_id_names_both_spellings_and_both_ranges(listed: dict[str, dict]) -> None:
    """An integer, a decimal string, or a 0x hex string (can.py:2299-2309), from
    0 to 0x7FF, or from 0 to 0x1FFFFFFF when extended, else `invalid_argument`
    (can.py:2249-2251)."""
    text = property_text(listed[SEND], "frame_id")

    assert names(r"0x", text) and names(r"\bdecimal\b", text), text
    assert id_ranges_by_kind(text), text
    assert names(field("invalid_argument"), definition(listed[SEND])), definition(listed[SEND])


def test_extended_and_rtr_name_their_default_and_what_each_value_sends(listed: dict[str, dict]) -> None:
    """Both default to false (can.py:2245-2246): false is an 11-bit standard id
    and a data frame, true a 29-bit extended id and a remote frame, which an
    `fd: true` bus refuses (can.py:2252-2264)."""
    extended = property_text(listed[SEND], "extended")
    rtr = property_text(listed[SEND], "rtr")

    assert defaults_to_false(extended), extended
    assert extended_meaning(extended), extended
    assert defaults_to_false(rtr), rtr
    assert rtr_meaning(rtr), rtr
    assert remote_frames_refused_on_fd(rtr), rtr


def test_data_hex_names_its_format_its_default_and_the_length_limits(listed: dict[str, dict]) -> None:
    """Hex byte pairs with whitespace dropped, default empty (can.py:2266-2270,
    2312-2316); at most 8 bytes on a classic bus, one of every FD length on an
    FD bus, and never more than the bus `max_frame_data_bytes` (can.py:2271-2295)."""
    text = property_text(listed[SEND], "data_hex")
    whole = definition(listed[SEND])

    assert hex_pairs(text), text
    assert whitespace_ignored(text), text
    assert data_defaults_to_empty(text), text
    assert names(field("max_frame_data_bytes"), whole), whole
    assert classic_payload_capped_at_eight(whole), whole
    assert fd_lengths_named(whole), whole


def test_can_send_participant_names_what_it_needs_and_what_its_sends_spend(listed: dict[str, dict]) -> None:
    """Required on a bus with `shares` (can.py:913-914), with its own
    `allow_write` (can.py:736-737). Its filter must carry the id
    (canbroker.py:1074-1075), each frame spends its budget (canbroker.py:1084-1086,
    1235-1240), and a spent budget ends its run (canbroker.py:1242-1247)."""
    text = property_text(listed[SEND], "participant")

    assert participant_required_on_a_shared_bus(text), text
    assert participant_write_grant(text), text
    assert names(field("can_participant_filter_violation"), text), text
    assert sends_spend_the_budget(text), text
    assert exhaustion_ends_the_run(text), text


# ---------------------------------------------------------------------------
# can_read: what it needs, what it takes off the queue, how long it waits.


def test_can_read_says_in_its_description_what_it_needs_and_what_it_answers(listed: dict[str, dict]) -> None:
    """An open session (can.py:909-922); the frames come back in `frames` with
    their count in `frames_read` (can.py:807) and are off the queue
    (can.py:1205-1217); nothing queued is ok with `frames_read: 0` (can.py:807).
    These decide how a read is used, so the description says them."""
    text = listed[READ]["description"]

    assert names(field("can_session_start"), text), text
    assert names(field("session_not_active"), text), text
    assert frames_come_back_with_their_count(text), text
    assert frames_are_consumed(text), text
    assert an_empty_read_is_ok(text), text


def test_can_read_names_the_failures_of_a_read_and_what_follows_them(listed: dict[str, dict]) -> None:
    """An adapter that fails the read is `can_read_failed` (can.py:1223); one
    that answers what cannot be read is `can_adapter_invalid_response`
    (can.py:806, 845), after which the session is stopped and opened again
    (knowledge.py:3189-3200)."""
    text = definition(listed[READ])

    assert names(field("can_read_failed"), text), text
    assert invalid_response_restarts_the_session(text), text


def test_max_frames_names_its_range_and_its_default(listed: dict[str, dict]) -> None:
    """1 to the bus `max_buffer_frames` (1024 unless configured, config.py:3703),
    else `invalid_argument` (can.py:782-783); none given is `max_buffer_frames`
    (can.py:778)."""
    text = property_text(listed[READ], "max_frames")

    assert max_frames_defaults_to_the_buffer(text), text
    assert stated(text, r"\b1\b", field("max_buffer_frames")), text
    assert names(r"\b1024\b", text), text
    assert names(field("invalid_argument"), definition(listed[READ])), definition(listed[READ])


def test_wait_timeout_s_tells_a_plain_wait_from_an_until_id_wait(listed: dict[str, dict]) -> None:
    """Seconds. Without until_id: 0 when not given, so the read answers with
    what is queued (can.py:779), and never past the bus `timeout_s` or 60 s
    (can.py:792). With until_id the call has its own budget: 10 s when not
    given, never past 60 s, whatever the bus `timeout_s` (readuntil.py:24-36,
    can.py:786-790). Neither cap is said of the whole call in general."""
    text = property_text(listed[READ], "wait_timeout_s")

    assert names(r"\bseconds?\b", text), text
    assert wait_defaults_to_zero_and_returns_at_once(text), text
    assert plain_wait_is_capped(text), text
    assert until_wait_budget(definition(listed[READ])), definition(listed[READ])


def test_until_id_names_its_shape_and_its_range(listed: dict[str, dict]) -> None:
    """One id or a list of 1 to 8, each 0 to 0x1FFFFFFF (readuntil.py:96-113)."""
    text = property_text(listed[READ], "until_id")

    assert until_id_shape(text), text
    assert until_id_range(text), text


def test_can_read_participant_names_the_filter_its_own_frames_and_its_budget(listed: dict[str, dict]) -> None:
    """Required on a bus with `shares` (can.py:913-914); a participant reads only
    what its filter accepts and never its own sends (canbroker.py:1190); the
    frames it reads spend its budget (canbroker.py:1147-1151), and a spent
    budget ends its run (canbroker.py:1242-1247)."""
    text = property_text(listed[READ], "participant")

    assert participant_required_on_a_shared_bus(text), text
    assert names(r"\bfilter", text), text
    assert own_frames_excluded(text), text
    assert reads_spend_the_budget(text), text
    assert exhaustion_ends_the_run(text), text


# ---------------------------------------------------------------------------
# can_buses_list: what it lists, and that it opens nothing.


def test_can_buses_list_says_what_it_lists_and_what_each_bus_shows(listed: dict[str, dict]) -> None:
    """Every `can_buses` entry in `buses`, keyed by bus_id, with its status
    (can.py:483-499, 924-957): the session state, the listen-only flag and its
    evidence, the bus mode, the payload and queue limits can_send and can_read
    are held to, and on a bus with `shares` the participants attached."""
    text = listed[BUSES]["description"]

    assert names(field("can_buses"), text), text
    assert buses_keyed_by_bus_id(text), text
    assert names(field("can_session_start"), text), text
    for name in ("session_active", "listen_only", "listen_only_enforcement", "fd", "max_frame_data_bytes", "max_buffer_frames", "active_participants"):
        assert names(field(name), text), (name, text)


def test_can_buses_list_opens_nothing_and_needs_no_session(listed: dict[str, dict]) -> None:
    text = listed[BUSES]["description"]

    assert opens_nothing(text), text
    assert needs_no_session(text), text


# ---------------------------------------------------------------------------
# com_ports_list: what it lists, which name the session tools take, and when
# the host ports are withheld.


def test_com_ports_list_says_what_it_lists_and_which_names_the_session_tools_take(listed: dict[str, dict]) -> None:
    """The configured entries in `ports`, keyed by port_id, with their status
    (comports.py:953-957, 1624-1640), and the host serial ports pyserial finds
    in `available_com_ports` (comports.py:66-94). com_session_start's own
    definition sends a reader here for the names."""
    text = listed[PORTS]["description"]

    assert names(field("com_ports"), text), text
    assert ports_keyed_by_port_id(text), text
    assert port_id_not_device_path(text), text
    assert names(field("com_session_start"), text), text
    assert names(field("session_active"), text), text
    assert names(field("available_com_ports"), text), text


def test_com_ports_list_says_when_host_ports_are_withheld_and_where_each_failure_goes(listed: dict[str, dict]) -> None:
    """Withheld unless a configured port may be read (comports.py:959-967);
    pyserial that will not import and an OS error while enumerating are each
    reported inside `available_com_ports` (comports.py:67-94), and the call
    stays ok (comports.py:970-975)."""
    text = listed[PORTS]["description"]

    assert host_ports_withheld_without_a_readable_port(text), text
    assert missing_pyserial_reported_inside(text), text
    assert os_error_reported_inside(text), text
    assert the_listing_stays_ok(text), text


def test_com_ports_list_opens_nothing(listed: dict[str, dict]) -> None:
    text = listed[PORTS]["description"]

    assert opens_nothing(text), text


# ---------------------------------------------------------------------------
# What no definition may say.


def test_every_name_a_definition_writes_is_a_listed_tool_or_spelled_in_the_code(listed: dict[str, dict]) -> None:
    """A tool, result field, error code or configuration key named in a
    definition has to be one the server lists or the code spells. A name no
    code returns is an outcome a model would wait for in vain."""
    source = "\n".join(path.read_text(encoding="utf-8") for path in Path(agentic_hil.__file__).parent.rglob("*.py"))
    for tool_name in TOOLS:
        text = definition(listed[tool_name])
        written = set(re.findall(r"(?<![A-Za-z0-9_/.*-])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9_])", text)) - set(listed)
        unknown = sorted(token for token in written if f'"{token}"' not in source)
        assert unknown == [], (tool_name, unknown)


@pytest.mark.parametrize("tool_name", [SEND, READ, BUSES])
def test_no_definition_promises_a_bitrate_the_socketcan_adapter_does_not_apply(listed: dict[str, dict], tool_name: str) -> None:
    """A socketcan bus does not apply its configured bitrate (can.py:414), so
    a clause that speaks of a socketcan bitrate says so."""
    for clause in clauses(definition(listed[tool_name])):
        if names(r"\bbitrate\b", clause) and names(r"\bsocketcan\b", clause):
            assert names(r"bitrate_verified" + FALSE + r"|\bnot (applied|set|verified)\b", clause), clause


@pytest.mark.parametrize("tool_name", TOOLS)
def test_the_annotations_stay_and_the_text_does_not_contradict_them(listed: dict[str, dict], tool_name: str) -> None:
    tool = listed[tool_name]

    assert tool["annotations"] == ANNOTATIONS[tool_name], tool["annotations"]
    if tool["annotations"]["readOnlyHint"]:
        assert not claims_an_effect(definition(tool)), definition(tool)
    else:
        assert not names(r"read-only|does not (touch|change)|no effect on the bus", tool["description"]), tool["description"]


# ---------------------------------------------------------------------------
# The behaviour the definitions describe, with python-can and pyserial faked.


class TimedBus(QueuedBus):
    """A python-can bus that records the timeout each send was given."""

    def __init__(self) -> None:
        super().__init__()
        self.send_timeouts: list[float | None] = []

    def send(self, message: object, timeout: float | None = None) -> None:
        self.send_timeouts.append(timeout)
        super().send(message, timeout)


def bench_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yaml: str = BENCH_YAML) -> tuple[AgenticHILToolService, list[TimedBus]]:
    opened: list[TimedBus] = []

    def open_bus(**kwargs: object) -> TimedBus:
        bus = TimedBus()
        opened.append(bus)
        return bus

    monkeypatch.setitem(sys.modules, "can", fake_can_module(open_bus))
    return AgenticHILToolService(load_config(str(write_config(tmp_path, can_buses_yaml=yaml)))), opened


def record_reads(service: AgenticHILToolService) -> list[tuple[int, float]]:
    """Replace the open session's adapter read with one that records what it was asked."""
    asked: list[tuple[int, float]] = []

    def read(max_frames: int, wait_timeout_s: float) -> dict:
        asked.append((max_frames, wait_timeout_s))
        return {"ok": True, "frames": []}

    service.can_buses.sessions[("bench", None)].adapter_session.read = read  # type: ignore[method-assign]
    return asked


def test_a_send_puts_one_frame_on_the_bus_per_call_and_answers_with_what_the_adapter_took(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A decimal and a 0x hex `frame_id` name the same id, whitespace in
    `data_hex` is dropped, `extended`, `rtr` and `data_hex` default to a
    standard data frame with no bytes, the adapter is given the bus `timeout_s`,
    and a repeat is a second frame on the bus."""
    service, opened = bench_service(tmp_path, monkeypatch)
    try:
        assert service.call("can_session_start", {"bus_id": "bench"})["ok"] is True
        decimal = service.call(SEND, {"bus_id": "bench", "frame_id": "291", "data_hex": "CA FE"})
        hexadecimal = service.call(SEND, {"bus_id": "bench", "frame_id": "0x123", "data_hex": "cafe"})
        bare = service.call(SEND, {"bus_id": "bench", "frame_id": 0x7FF})
    finally:
        service.close()

    for answer in (decimal, hexadecimal):
        assert answer["ok"] is True, answer
        assert answer["frame"] == {"id": 0x123, "id_hex": "0x123", "extended": False, "rtr": False, "data_hex": "cafe", "dlc": 2}, answer
        assert answer["log_path"], answer
        assert "adapter_result" in answer, answer
    assert bare["ok"] is True, bare
    assert bare["frame"] == {"id": 0x7FF, "id_hex": "0x7ff", "extended": False, "rtr": False, "data_hex": "", "dlc": 0}, bare
    bus = opened[0]
    assert [(message.arbitration_id, bytes(message.data)) for message in bus.sent] == [(0x123, b"\xca\xfe"), (0x123, b"\xca\xfe"), (0x7FF, b"")]
    assert [message.is_extended_id for message in bus.sent] == [False, False, False]
    assert [message.is_remote_frame for message in bus.sent] == [False, False, False]
    assert bus.send_timeouts == [BUS_TIMEOUT_S] * 3, bus.send_timeouts


def test_an_id_outside_its_range_or_odd_hex_is_refused_before_anything_is_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """0x800 is past a standard id and inside an extended one; 0x20000000 is
    past both (can.py:2249-2251). An odd count of hex digits is no byte string
    (can.py:2312-2316). Each refusal is `invalid_argument` and sends nothing."""
    service, opened = bench_service(tmp_path, monkeypatch)
    try:
        assert service.call("can_session_start", {"bus_id": "bench"})["ok"] is True
        standard = service.call(SEND, {"bus_id": "bench", "frame_id": 0x800})
        past = service.call(SEND, {"bus_id": "bench", "frame_id": 0x20000000, "extended": True})
        odd = service.call(SEND, {"bus_id": "bench", "frame_id": 0x123, "data_hex": "ABC"})
        extended = service.call(SEND, {"bus_id": "bench", "frame_id": 0x800, "extended": True})
    finally:
        service.close()

    for refused in (standard, past, odd):
        assert refused["ok"] is False and refused["error_type"] == "invalid_argument", refused
    assert extended["ok"] is True and extended["frame"]["extended"] is True, extended
    assert [(message.arbitration_id, message.is_extended_id) for message in opened[0].sent] == [(0x800, True)]


def test_a_read_without_a_wait_takes_what_is_queued_at_once_and_consumes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three frames queued, two asked for: the read answers with the first two,
    the next with the third, and the one after with nothing, still ok."""
    service, opened = bench_service(tmp_path, monkeypatch)
    try:
        assert service.call("can_session_start", {"bus_id": "bench"})["ok"] is True
        opened[0].queue_frames(3)
        first = service.call(READ, {"bus_id": "bench", "max_frames": 2})
        second = service.call(READ, {"bus_id": "bench"})
        empty = service.call(READ, {"bus_id": "bench"})
    finally:
        service.close()

    assert first["ok"] is True and [frame["id"] for frame in first["frames"]] == [0x100, 0x101], first
    assert first["frames_read"] == 2, first
    assert second["ok"] is True and [frame["id"] for frame in second["frames"]] == [0x102], second
    assert empty["ok"] is True and empty["frames_read"] == 0 and empty["frames"] == [], empty


def test_a_read_asks_for_max_buffer_frames_and_caps_its_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No `max_frames` asks the adapter for the bus `max_buffer_frames`; more
    than that is `invalid_argument` and reads nothing (can.py:778, 782-783). No
    `wait_timeout_s` is 0, and a longer one is cut to the bus `timeout_s`
    (can.py:779, 792)."""
    service, _ = bench_service(tmp_path, monkeypatch)
    try:
        assert service.call("can_session_start", {"bus_id": "bench"})["ok"] is True
        asked = record_reads(service)
        assert service.call(READ, {"bus_id": "bench"})["ok"] is True
        assert service.call(READ, {"bus_id": "bench", "wait_timeout_s": 1.5})["ok"] is True
        assert service.call(READ, {"bus_id": "bench", "wait_timeout_s": 30})["ok"] is True
        over = service.call(READ, {"bus_id": "bench", "max_frames": BUS_MAX_BUFFER_FRAMES + 1})
    finally:
        service.close()

    assert asked == [(BUS_MAX_BUFFER_FRAMES, 0.0), (BUS_MAX_BUFFER_FRAMES, 1.5), (BUS_MAX_BUFFER_FRAMES, BUS_TIMEOUT_S)], asked
    assert over["ok"] is False and over["error_type"] == "invalid_argument", over
    assert over["max_buffer_frames"] == BUS_MAX_BUFFER_FRAMES, over


def test_no_read_waits_past_sixty_seconds_and_the_bus_defaults_are_1024_frames_and_10_s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bus whose own `timeout_s` is 120 s still waits at most 60 s per read
    (can.py:792); a bus that names neither limit gets 1024 frames and 10 s
    (config.py:3699, 3703)."""
    service, _ = bench_service(tmp_path, monkeypatch, WIDE_YAML)
    try:
        assert service.call("can_session_start", {"bus_id": "bench"})["ok"] is True
        asked = record_reads(service)
        assert service.call(READ, {"bus_id": "bench", "wait_timeout_s": 90})["ok"] is True
    finally:
        service.close()

    assert asked == [(1024, 60.0)], asked
    plain = load_config(str(write_config(tmp_path / "plain", can_buses_yaml=f'can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "{CHANNEL}"\n')))
    assert plain.can_buses["bench"].max_buffer_frames == 1024
    assert plain.can_buses["bench"].timeout_s == 10.0


def watch_reads(service: AgenticHILToolService) -> list[tuple[int, float]]:
    """Record what the open session's adapter read is asked, and let it read."""
    adapter_session = service.can_buses.sessions[("bench", None)].adapter_session
    original = adapter_session.read
    asked: list[tuple[int, float]] = []

    def read(max_frames: int, wait_timeout_s: float) -> dict:
        asked.append((max_frames, wait_timeout_s))
        return original(max_frames, wait_timeout_s)

    adapter_session.read = read  # type: ignore[method-assign]
    return asked


@pytest.mark.parametrize(
    ("yaml", "wait_timeout_s", "first_read_wait_s"),
    [
        pytest.param(BENCH_YAML, 30, BUS_TIMEOUT_S, id="each-read-keeps-the-bus-timeout"),
        pytest.param(WIDE_YAML, None, 10.0, id="ten-seconds-by-default"),
        pytest.param(WIDE_YAML, 90, 60.0, id="never-past-sixty-seconds"),
    ],
)
def test_an_until_id_read_has_its_own_budget_and_each_adapter_read_keeps_the_plain_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yaml: str, wait_timeout_s: float | None, first_read_wait_s: float
) -> None:
    """With until_id the call waits 10 s unless given and never past 60 s
    (readuntil.py:24-36, can.py:786-790), and reads the adapter one frame at a
    time, each read held to the bus `timeout_s` and 60 s as a plain read is
    (can.py:825-827). On a bus whose `timeout_s` is 2.5 s a 30 s wait is not
    cut to 2.5 s for the call; only each read is."""
    service, opened = bench_service(tmp_path, monkeypatch, yaml)
    arguments: dict[str, object] = {"bus_id": "bench", "until_id": 0x100}
    if wait_timeout_s is not None:
        arguments["wait_timeout_s"] = wait_timeout_s
    try:
        assert service.call("can_session_start", {"bus_id": "bench"})["ok"] is True
        asked = watch_reads(service)
        opened[0].queue_frames(1)
        matched = service.call(READ, arguments)
    finally:
        service.close()

    assert matched["ok"] is True and matched["until_matched"] is True, matched
    assert [frame["id"] for frame in matched["frames"]] == [0x100], matched
    assert len(asked) == 1 and asked[0][0] == 1, asked
    # The deadline is set a moment before the first read, so its wait can be a
    # hair under the budget; it is never over it.
    assert first_read_wait_s - 0.5 < asked[0][1] <= first_read_wait_s, asked


def test_can_buses_list_opens_no_adapter_and_answers_without_a_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, opened = bench_service(tmp_path, monkeypatch)
    try:
        before = service.call(BUSES, {})
        assert opened == [], "listing the buses opened an adapter"
        assert service.call("can_session_start", {"bus_id": "bench"})["ok"] is True
        during = service.call(BUSES, {})
    finally:
        service.close()

    assert before["ok"] is True, before
    bench = before["buses"]["bench"]
    assert bench["session_active"] is False, bench
    assert bench["listen_only"] is False and bench["listen_only_enforcement"] == "link_verified", bench
    assert bench["max_buffer_frames"] == BUS_MAX_BUFFER_FRAMES and bench["max_frame_data_bytes"] == 8 and bench["fd"] is False, bench
    assert before["supported_adapters"] == ["peak", "socketcan", "process"], before
    assert during["buses"]["bench"]["session_active"] is True, during
    assert len(opened) == 1 and opened[0].sent == []


def test_com_ports_list_lists_host_ports_on_version_2_with_no_port_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On version 2 reading needs no grant (types.py `read_free`), so host
    discovery is not withheld for want of a readable configured port. A new
    project without a `com_ports` entry lists the host's serial ports to find
    the board's adapter (#656), and nothing names `permissions.allow_read`, a
    key version 2 refuses to load."""
    import serial.tools.list_ports

    monkeypatch.setattr(serial.tools.list_ports, "comports", lambda: [SimpleNamespace(device="COM42", description="adapter", vid=0x0403, pid=0x6001, serial_number="TD4NEW")])
    service = AgenticHILToolService(load_config(str(write_config(tmp_path, config_version=2))))
    try:
        listed_ports = service.call(PORTS, {})
    finally:
        service.close()

    assert listed_ports["ok"] is True, listed_ports
    assert listed_ports["ports"] == {}, listed_ports
    available = listed_ports["available_com_ports"]
    assert available["ok"] is True, available
    assert [port["device"] for port in available["ports"]] == ["COM42"], available
    assert "allow_read" not in json.dumps(listed_ports), listed_ports


def test_com_ports_list_withholds_host_ports_on_version_1_and_names_the_missing_entry(tmp_path: Path) -> None:
    """A file without a version still needs a configured port that may be
    read. With no `com_ports` entry at all, the refusal says that the entry is
    what is missing, not only the grant on it (#656)."""
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        listed_ports = service.call(PORTS, {})
    finally:
        service.close()

    assert listed_ports["ok"] is True, listed_ports
    assert listed_ports["ports"] == {}, listed_ports
    available = listed_ports["available_com_ports"]
    assert available["ok"] is False and available["error_type"] == "permission_denied", available
    assert "`com_ports` entry" in available["summary"], available


def test_com_ports_list_names_host_ports_and_opens_none_when_a_configured_port_may_be_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On version 2 any configured port may be read, so host discovery runs
    (comports.py:959-961), through pyserial's `comports()` and without opening
    anything (comports.py:66-94)."""
    import serial
    import serial.tools.list_ports

    def refuse_to_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("com_ports_list opened a serial port")

    monkeypatch.setattr(serial, "Serial", refuse_to_open)
    monkeypatch.setattr(serial.tools.list_ports, "comports", lambda: [SimpleNamespace(device="COM41", description="board", vid=0x0483, pid=0x374B, serial_number="TD4")])
    service = AgenticHILToolService(load_config(str(write_config(tmp_path, com_ports_yaml=COM_YAML, config_version=2))))
    try:
        listed_ports = service.call(PORTS, {})
    finally:
        service.close()

    assert listed_ports["ok"] is True, listed_ports
    assert listed_ports["ports"][PORT_ID]["session_active"] is False, listed_ports
    assert listed_ports["ports"][PORT_ID]["device"] == "/dev/ttyTOOLDEF641", listed_ports
    assert listed_ports["available_com_ports"]["ok"] is True, listed_ports
    assert [port["device"] for port in listed_ports["available_com_ports"]["ports"]] == ["COM41"], listed_ports


def test_a_missing_pyserial_is_reported_inside_the_host_listing_and_the_call_stays_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """comports.py:67-78: the import failure becomes `serial_backend_not_available`
    in `available_com_ports`; the configured entries are still listed."""
    import serial.tools

    monkeypatch.delattr(serial.tools, "list_ports", raising=False)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", None)
    service = AgenticHILToolService(load_config(str(write_config(tmp_path, com_ports_yaml=COM_YAML, config_version=2))))
    try:
        listed_ports = service.call(PORTS, {})
    finally:
        service.close()

    assert listed_ports["ok"] is True, listed_ports
    assert PORT_ID in listed_ports["ports"], listed_ports
    assert listed_ports["available_com_ports"]["ok"] is False, listed_ports
    assert listed_ports["available_com_ports"]["error_type"] == "serial_backend_not_available", listed_ports


def test_an_os_error_while_enumerating_is_reported_inside_the_host_listing_and_the_call_stays_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """comports.py:86-94: pyserial's enumeration raising an OS error becomes
    `com_port_discovery_failed` in `available_com_ports`; the configured
    entries are still listed and the call is ok."""
    import serial.tools.list_ports

    def fail_to_enumerate() -> list[object]:
        raise OSError("the USB serial driver went away during enumeration")

    monkeypatch.setattr(serial.tools.list_ports, "comports", fail_to_enumerate)
    service = AgenticHILToolService(load_config(str(write_config(tmp_path, com_ports_yaml=COM_YAML, config_version=2))))
    try:
        listed_ports = service.call(PORTS, {})
    finally:
        service.close()

    assert listed_ports["ok"] is True, listed_ports
    assert listed_ports["ports"][PORT_ID]["session_active"] is False, listed_ports
    assert listed_ports["available_com_ports"]["ok"] is False, listed_ports
    assert listed_ports["available_com_ports"]["error_type"] == "com_port_discovery_failed", listed_ports
