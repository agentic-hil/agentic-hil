"""What a host reads about opening and closing a CAN session (#630).

`can_session_start` and `can_session_stop` are read here the way a host reads
them: through a real `tools/list` request answered by the server. What they
must carry is what the code does today. Which bus a call names and where that
name comes from, what each adapter needs installed or configured, what a
participant on a bus with `shares` is, what `clear_rx_queue` drains and where
it does nothing, what a repeat start and a stop with nothing open answer, what
stopping closes and releases and what it leaves held, which outcomes a caller
meets, how long a bridge may take, a valid start, read and stop sequence, and
how the session relates to `can_send` and `can_read`.

The checks are about meaning, not wording. A fact that could be stated the
wrong way round (required or never required, `false` or `true`, kept or
dropped) is checked as a relation inside one sentence or clause, and every
such check is run against its own inverted statement as well, which it must
refuse. A test never pins a sentence.

The second half holds the behaviour those definitions describe, with python-can
and the broker faked, where no existing test already holds it. The rest is held
elsewhere and not repeated: an unconfigured bus on both tools
(tests/test_sessions_devices_coordination.py), python-can missing
(the same file and tests/test_python_can_import_error.py), a bus another holder
has (tests/test_can_session_holder.py, tests/test_can_broker.py), a bridge that
does not answer `open` in time (tests/test_can_bridge_contact.py), a queue that
will not drain (tests/test_hardening.py), a listen-only mode the adapter does
not confirm or the link does not report (tests/test_can_listen_only.py,
tests/test_can_interface_down.py), a close that fails and is kept for a retry
(tests/test_hardening.py), the broker that stops with its last participant
(tests/test_can_broker.py), and a declared run that keeps its devices across
the calls inside it (tests/test_bench_mutex.py).
"""

from __future__ import annotations

import io
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
from conftest import write_config
from test_can_bridge_contact import RecordingBridge
from test_can_frame_and_routing import RecordingBus, fake_can_module
from test_can_participant_sessions import BUS as SHARED_BUS
from test_can_participant_sessions import SHARES_YAML, SharedFakeParticipant
from test_mcp_envelope import real_service
from test_tool_descriptions import DESCRIPTION_LIMIT, PROPERTY_DESCRIPTION_LIMIT, property_descriptions

import agentic_hil
from agentic_hil.can import (
    SUPPORTED_CAN_ADAPTERS,
    CanBusService,
    CanBusSession,
    ProcessCanAdapterSession,
    open_process_adapter,
)
from agentic_hil.config import load_config
from agentic_hil.knowledge import LISTEN_ONLY_MODE_ERROR, LISTEN_ONLY_UNCONFIRMED_ERROR, LISTEN_ONLY_UNSUPPORTED_ERROR
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

START = "can_session_start"
STOP = "can_session_stop"
SIBLINGS = ("can_buses_list", "can_send", "can_read")
SESSION_TOOLS = (START, "can_send", "can_read", STOP)

# Device locks are machine-wide, so every bus opened here gets a channel no
# other test module names.
CHANNEL = "vcan630doc"
SINGLE_OWNER_YAML = f'can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "{CHANNEL}"\n'
SHARED_YAML = SHARES_YAML.replace("channel: fake-can", "channel: fake-can-630doc")

# python-can interfaces this server does not open. A definition may name one
# only to exclude it.
UNSUPPORTED_INTERFACES = ("kvaser", "vector", "ixxat", "slcan", "gs_usb", "cantact", "nican", "neovi", "systec", "canalystii")

# The annotations as they stand. The definitions are rewritten around them, so
# what they say must stay true of the text and of the code.
ANNOTATIONS = {
    START: {"title": "Open a CAN session", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    STOP: {"title": "Close a CAN session", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
}


def listed_tools(tmp_path: Path) -> dict[str, dict]:
    """The tool list exactly as a host receives it."""
    service = real_service(tmp_path)
    try:
        response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, service)
    finally:
        service.close()
    assert response is not None and "result" in response, response
    return {tool["name"]: tool for tool in response["result"]["tools"]}


@pytest.fixture
def listed(tmp_path: Path) -> dict[str, dict]:
    return listed_tools(tmp_path)


def property_text(tool: dict, name: str) -> str:
    return str(tool["inputSchema"]["properties"][name].get("description", ""))


def fields(tool: dict) -> list[str]:
    """Every text a host shows a model about one tool, one entry per field."""
    return [str(tool["description"]), *(text for _, text in property_descriptions(tool["inputSchema"]))]


def definition(tool: dict) -> str:
    return "\n".join(fields(tool))


def names(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


def clauses(text: str) -> list[str]:
    """Sentences, and the parts a semicolon separates. A relation is read inside one of them only."""
    return [part.strip() for part in re.split(r"(?<=[.;!?])\s+|\n", text) if part.strip()]


def stated(text: str, *patterns: str, unless: str | None = None) -> bool:
    """Whether one clause carries every pattern and not the contradiction `unless` names."""
    return any(all(names(pattern, clause) for pattern in patterns) and not (unless and names(unless, clause)) for clause in clauses(text))


# ---------------------------------------------------------------------------
# The relations a definition could state the wrong way round. Each is a named
# check, so the controls below can show it refuses the inverted claim.

FALSE = r"`?\s*[:=]?\s*`?(?:is\s+)?`?false\b"
TRUE = r"`?\s*[:=]?\s*`?(?:is\s+)?`?true\b"


def participant_required_on_a_shared_bus(text: str) -> bool:
    """can.py:509-510 (start) and 699-700 (stop): a bus with `shares` refuses a call without one."""
    return stated(text, r"`?shares`?", r"\b(required|must)\b", unless=r"\b(not|never)\s+(be\s+)?required\b|\boptional\b|\bneed not\b")


def unknown_participant_refused(text: str) -> bool:
    """can.py:648-650: a name the bus does not declare, on any bus, is refused at start."""
    return stated(text, r"can_participant_not_configured", r"without `?shares`?|\bother\b|\bunknown\b|\bunconfigured\b|\bundeclared\b", unless=r"\bnever\b|\b(is|are) accepted\b")


def nothing_open_answers_was_active_false(text: str) -> bool:
    """can.py:701-703: no session under the name answers ok with `was_active: false`."""
    return stated(text, r"\b(no|not|nothing|without)\b", r"\b(session|open)\b", r"was_active" + FALSE)


def repeat_start_answers_already_active(text: str) -> bool:
    """can.py:516-523 and 659-660: a start on an open session answers `already_active: true`."""
    return stated(text, r"\b(repeat|again|second)\b", r"already_active" + TRUE)


def frames_drained_counts_discarded_frames(text: str) -> bool:
    return stated(text, r"frames_drained", r"\b(discard|drain|clear|count)")


def clear_rx_queue_defaults_to_true(text: str) -> bool:
    """can.py:501: `clear_rx_queue: bool = True`."""
    return stated(text, r"\bdefault", r"\btrue\b", unless=r"\bfalse\b")


def participant_queue_not_drained(text: str) -> bool:
    """can.py:684-685: a participant start only audits the request and drains nothing."""
    return stated(text, r"\bparticipant", r"\b(ignored|no effect|drains nothing|not drained|does nothing)\b", unless=r"\bnot ignored\b")


def drain_is_bounded(text: str) -> bool:
    """can.py:969-992: at most 16 passes within 1.0 s, then `can_queue_clear_limit`."""
    return stated(text, r"\bbound", r"can_queue_clear_limit", unless=r"\bunbound|\bnever\b")


def bridge_open_is_bounded(text: str) -> bool:
    """can.py:2139-2141 with config.py:3699 and bridge.py:140: the bridge has the entry's
    `timeout_s` (10 s unless configured) to answer `open`, else `can_adapter_timeout`."""
    return stated(text, r"timeout_s", r"\b(bridge|process)\b", r"\b10\s*(s|seconds?)\b", r"can_adapter_timeout", unless=r"\b(unlimited|unbounded|no limit|never|infinite)\b")


def bridge_close_is_bounded(text: str) -> bool:
    """bridge.py:68: the close request waits 1 s; unconfirmed, the stop answers `can_adapter_close_failed`."""
    return stated(text, r"\b(bridge|process)\b", r"\b(1\s*s|1 second|one second)\b", r"can_adapter_close_failed")


def failed_close_keeps_the_session(text: str) -> bool:
    """can.py:704-707: a close that fails keeps the session registered for a retry."""
    return stated(text, r"can_adapter_close_failed", r"\b(kept|keeps|remains|retry)\b", unless=r"\b(drop|drops|dropped|discard|discards|forgotten)\b")


def other_participants_stay(text: str) -> bool:
    """canbroker.py:962-975: one participant detaching leaves the others; the broker stops after the last."""
    return stated(text, r"\bdetach", r"\bothers?\b", r"\b(stay|stays|remain|remains|keep|keeps)\b", unless=r"\b(all|every)\b")


def lease_is_released(text: str) -> bool:
    """can.py:712-715: the stop releases the session's lease."""
    return stated(text, r"\breleas", r"\b(lease|lock)\b", unless=r"\bnot released\b|\bstays held\b")


def a_declared_run_keeps_the_bus(text: str) -> bool:
    """coordination.py:1170-1173: inside a declared run the released lease leaves the device held by the run."""
    return stated(text, r"\brun\b", r"\b(keeps|holds|retains)\b", unless=r"\brun\b[^.;,()]*\breleas")


RELATIONS: list[tuple[Callable[[str], bool], str, str]] = [
    (participant_required_on_a_shared_bus, "Required on a bus with `shares`.", "On a bus with `shares` a participant is never required."),
    (participant_required_on_a_shared_bus, "On a bus with `shares` it must name a participant.", "With `shares` the participant is optional."),
    (unknown_participant_refused, "Any other name fails `can_participant_not_configured`.", "Any name is accepted; `can_participant_not_configured` is never returned."),
    (unknown_participant_refused, "An unknown name fails `can_participant_not_configured`.", "An unknown name never fails `can_participant_not_configured`."),
    (nothing_open_answers_was_active_false, "With no session open it answers `was_active: false`.", "With no session open it answers `was_active: true`."),
    (nothing_open_answers_was_active_false, "A name with no open session answers `was_active: false`.", "was_active is always true, whatever is open."),
    (repeat_start_answers_already_active, "A repeat call answers `already_active: true`.", "A repeat call answers `already_active: false`."),
    (frames_drained_counts_discarded_frames, "Discarded frames are counted in `frames_drained`.", "`frames_drained` lists the frames read."),
    (clear_rx_queue_defaults_to_true, "Default true: discard queued frames.", "Default false: queued frames are kept."),
    (participant_queue_not_drained, "Ignored for a participant.", "For a participant it is not ignored."),
    (drain_is_bounded, "Bounded: a queue that keeps filling fails `can_queue_clear_limit`.", "Unbounded: it drains until empty and never fails `can_queue_clear_limit`."),
    (bridge_open_is_bounded, "`can_adapter_timeout` when a bridge misses `timeout_s` (default 10 s).", "A bridge `timeout_s` (default 10 s) is unlimited, never `can_adapter_timeout`."),
    (bridge_close_is_bounded, "A bridge silent for 1 s answers `can_adapter_close_failed`.", "A bridge may take as long as it needs, never `can_adapter_close_failed`."),
    (failed_close_keeps_the_session, "It answers `can_adapter_close_failed` and keeps the session for a retry.", "It answers `can_adapter_close_failed` and drops the session."),
    (other_participants_stay, "The participant detaches while others stay attached.", "The participant detaches and every other participant is detached too."),
    (lease_is_released, "The session lease is released.", "The session lease is not released."),
    (a_declared_run_keeps_the_bus, "A declared run keeps the bus.", "A declared run releases the bus as well."),
]


@pytest.mark.parametrize(("check", "true_statement", "inverted"), RELATIONS, ids=[f"{check.__name__}-{index}" for index, (check, _, _) in enumerate(RELATIONS)])
def test_each_relation_check_refuses_its_inverted_statement(check: Callable[[str], bool], true_statement: str, inverted: str) -> None:
    assert check(true_statement), true_statement
    assert not check(inverted), inverted


# ---------------------------------------------------------------------------
# A lifecycle example a model can copy: start, then send or read, then stop,
# on one bus and one participant, every call valid against the listed schema.

CALL = re.compile(r"\b(can_session_start|can_session_stop|can_send|can_read)(?:\(([^()]*)\)|\s*(\{[^{}]*\}))")
ARGUMENT = re.compile(r"(\w+)\s*=\s*(\"[^\"]*\"|'[^']*'|true|false|True|False|0[xX][0-9A-Fa-f]+|-?\d+(?:\.\d+)?)\s*,?\s*")


def literal(token: str) -> object:
    if token[0] in "\"'":
        return token[1:-1]
    if token in {"true", "True", "false", "False"}:
        return token in {"true", "True"}
    return float(token) if "." in token else int(token, 0)


def example_calls(text: str) -> list[tuple[str, dict]]:
    """The calls written out in one field, in order, as `name(key=value, ...)` or `name {json}`."""
    calls: list[tuple[str, dict]] = []
    for match in CALL.finditer(text):
        name, keywords, document = match.groups()
        if document is not None:
            arguments = json.loads(document)
        else:
            assert ARGUMENT.sub("", keywords).strip() == "", f"unreadable arguments in {match.group(0)!r}"
            arguments = {key: literal(value) for key, value in ARGUMENT.findall(keywords)}
        calls.append((name, arguments))
    return calls


def lifecycle_problems(calls: list[tuple[str, dict]], tools: dict[str, dict]) -> list[str]:
    """Why a written sequence is not a session a model could run, or nothing."""
    problems = []
    order = [name for name, _ in calls]
    if len(order) < 3 or order[0] != START or order[-1] != STOP or not {"can_send", "can_read"} & set(order[1:-1]) or {START, STOP} & set(order[1:-1]):
        problems.append(f"not start, then send or read, then stop: {order}")
    for name, arguments in calls:
        errors = sorted(error.message for error in jsonschema.Draft202012Validator(tools[name]["inputSchema"]).iter_errors(arguments))
        if errors:
            problems.append(f"{name}{arguments}: {errors}")
    for key in ("bus_id", "participant"):
        values = {arguments.get(key) for _, arguments in calls}
        if len(values) > 1:
            problems.append(f"{key} differs between calls: {values}")
    return problems


def test_the_example_check_refuses_a_sequence_a_model_could_not_run(listed: dict[str, dict]) -> None:
    good = 'can_session_start(bus_id="bench"), can_read(bus_id="bench", max_frames=4), can_session_stop(bus_id="bench")'
    refused = {
        "two buses": 'can_session_start(bus_id="bench"), can_read(bus_id="other"), can_session_stop(bus_id="bench")',
        "read first": 'can_read(bus_id="bench"), can_session_start(bus_id="bench"), can_session_stop(bus_id="bench")',
        "no stop": 'can_session_start(bus_id="bench"), can_read(bus_id="bench")',
        "unknown argument": 'can_session_start(port_id="bench"), can_read(bus_id="bench"), can_session_stop(bus_id="bench")',
        "wrong type": 'can_session_start(bus_id="bench"), can_read(bus_id="bench", max_frames="all"), can_session_stop(bus_id="bench")',
        "two participants": 'can_session_start(bus_id="b", participant="ecu_a"), can_send(bus_id="b", participant="ecu_b", frame_id=1), can_session_stop(bus_id="b", participant="ecu_a")',
    }

    assert lifecycle_problems(example_calls(good), listed) == []
    for reason, text in refused.items():
        assert lifecycle_problems(example_calls(text), listed), reason


def test_a_definition_shows_a_valid_start_read_or_send_stop_sequence(listed: dict[str, dict]) -> None:
    """The issue asks for a valid example. It has to be in what a host shows,
    in one field, and runnable as written."""
    examples = [(text, calls) for tool_name in (START, STOP) for text in fields(listed[tool_name]) if (calls := example_calls(text))]

    assert examples, "neither definition writes out a call sequence"
    runnable = [text for text, calls in examples if not lifecycle_problems(calls, listed)]
    assert runnable, [(text, lifecycle_problems(calls, listed)) for text, calls in examples]
    invalid = [(name, arguments) for _, calls in examples for name, arguments in calls if any(jsonschema.Draft202012Validator(listed[name]["inputSchema"]).iter_errors(arguments))]
    assert invalid == [], invalid


# ---------------------------------------------------------------------------
# Every input says what it means, within the size every entry is held to.


@pytest.mark.parametrize("tool_name", [START, STOP])
def test_every_input_carries_its_own_description_within_the_limits(listed: dict[str, dict], tool_name: str) -> None:
    tool = listed[tool_name]
    properties = tool["inputSchema"]["properties"]

    undescribed = sorted(name for name, schema in properties.items() if not str(schema.get("description", "")).strip())
    assert undescribed == [], f"{tool_name}: {undescribed}"
    over = {name: len(schema["description"]) for name, schema in properties.items() if len(schema["description"]) > PROPERTY_DESCRIPTION_LIMIT}
    assert over == {}, over
    assert len(tool["description"]) <= DESCRIPTION_LIMIT, len(tool["description"])


@pytest.mark.parametrize("tool_name", [START, STOP])
def test_bus_id_says_which_entry_it_names_and_where_to_find_them(listed: dict[str, dict], tool_name: str) -> None:
    """A bus is chosen by the name of its `can_buses` entry (can.py:901-907);
    can_buses_list shows them, and an unknown name is `can_bus_not_configured`."""
    text = property_text(listed[tool_name], "bus_id")

    assert "can_buses" in text, text
    assert "can_buses_list" in definition(listed[tool_name]), definition(listed[tool_name])
    assert "can_bus_not_configured" in definition(listed[tool_name]), definition(listed[tool_name])


# ---------------------------------------------------------------------------
# can_session_start: what it opens, what it needs, what it answers.


def test_start_says_what_it_opens_and_which_tools_use_the_session(listed: dict[str, dict]) -> None:
    """The lifecycle in order: the session is what can_send and can_read need,
    and can_session_stop or the server's exit (can.py:877-899) ends it."""
    text = listed[START]["description"]

    assert names(r"\b(open|start)", text) and names(r"\bbus\b", text), text
    for sibling in ("can_send", "can_read", STOP):
        assert sibling in text, (sibling, text)
    assert names(r"\b(exit|exits|shut|closes)\b", text), text


def test_start_names_what_each_adapter_needs_before_it_can_open(listed: dict[str, dict]) -> None:
    """peak opens through python-can and the PCAN-Basic library (can.py:1557-1560);
    socketcan through python-can on an interface that exists and is up
    (can.py:1698-1700); a process bus runs the bridge its `executable` names
    (can.py:2129-2137) and needs no python-can (can.py:1153-1155). Without
    python-can a peak or socketcan start is `can_backend_not_available`
    (can.py:1674), which `agentic-hil[can]` installs (pyproject.toml:63)."""
    text = definition(listed[START])

    assert stated(text, r"\bpeak\b", r"PCAN-?Basic"), text
    assert stated(text, r"\bsocketcan\b", r"\binterface\b", r"\bup\b"), text
    assert stated(text, r"\bprocess\b", r"`executable`"), text
    assert stated(text, r"python-can", r"\b(peak|socketcan)\b", r"can_backend_not_available"), text
    assert "agentic-hil[can]" in text, text
    assert not stated(text, r"\bprocess\b", r"python-can"), "a process bus needs no python-can"


def test_start_says_how_long_a_bridge_may_take(listed: dict[str, dict]) -> None:
    """A bridge's `open` is bounded by the entry's own `timeout_s` and fails as
    `can_adapter_timeout`; the receive-queue drain has its own bound."""
    text = definition(listed[START])

    assert bridge_open_is_bounded(text), text
    assert drain_is_bounded(text), text


def test_start_names_the_listen_only_refusals(listed: dict[str, dict]) -> None:
    """On a `listen_only` bus a direct session opens only where the mode is
    confirmed. A socketcan link that does not report it, or a python-can build
    that cannot ask PCAN for it, is refused before the open
    (`can_listen_only_unsupported`, can.py:1852-1874, 1879-1897); a PCAN channel
    or a bridge that does not confirm it is closed again
    (`can_listen_only_unconfirmed`, can.py:1919-1929, 2195-2205).
    `can_listen_only_mode` is can_send's refusal (can.py:727-729) and may
    appear only beside can_send."""
    text = definition(listed[START])

    assert stated(text, r"listen_only", rf"{LISTEN_ONLY_UNSUPPORTED_ERROR}|{LISTEN_ONLY_UNCONFIRMED_ERROR}"), text
    for clause in clauses(text):
        if LISTEN_ONLY_MODE_ERROR in clause:
            assert "can_send" in clause, clause


def test_start_names_its_result_fields_and_their_meaning(listed: dict[str, dict]) -> None:
    """`session` is the status of the opened session (can.py:631-639, 959-963);
    a repeat call answers `already_active: true` (can.py:516-523, 659-660);
    `frames_drained` counts what the drain discarded (can.py:602-630)."""
    text = definition(listed[START])

    assert "`session`" in text, text
    assert repeat_start_answers_already_active(text), text
    assert frames_drained_counts_discarded_frames(text), text


def test_start_names_its_refusals(listed: dict[str, dict]) -> None:
    """A bus held elsewhere is `device_busy` (bench.py:499, coordination.py:1004-1005,
    1026-1028) or `resource_busy` (coordination.py:2188-2202), named together."""
    text = definition(listed[START])

    assert stated(text, r"device_busy", r"resource_busy"), text
    assert "can_participant_not_configured" in text, text


def test_participant_says_when_it_is_required_and_what_it_opens(listed: dict[str, dict]) -> None:
    """On a bus with `shares` a participant is required (can.py:509-510). One
    broker owns the adapter (can.py:671-683), and each participant receives only
    what its share's filter accepts, never its own frames (canbroker.py:1190,
    499-510). A name the bus does not declare is refused (can.py:648-650), on a
    bus without `shares` as well."""
    text = property_text(listed[START], "participant")

    assert participant_required_on_a_shared_bus(text), text
    assert unknown_participant_refused(text), text
    assert names(r"\bbroker\b", text), text
    assert names(r"\bfilter", text), text
    assert names(r"\bprivate\b|\bown\b", text), text


def test_clear_rx_queue_names_its_default_what_it_drains_and_where_it_does_nothing(listed: dict[str, dict]) -> None:
    """Default true. It discards the frames already queued, on a repeat call as
    well, and counts them in `frames_drained`. The drain is bounded. On a
    participant session it does nothing (can.py:684-685)."""
    text = property_text(listed[START], "clear_rx_queue")

    assert clear_rx_queue_defaults_to_true(text), text
    assert names(r"\b(discard|drain|clear|empt)", text), text
    assert frames_drained_counts_discarded_frames(text), text
    assert names(r"\b(repeat|again|already)", text), text
    assert drain_is_bounded(text), text
    assert participant_queue_not_drained(text), text


# ---------------------------------------------------------------------------
# can_session_stop: what it closes, what it releases, what stays held.


def test_stop_says_what_it_closes_and_what_stays_held(listed: dict[str, dict]) -> None:
    """A single-owner stop closes the adapter (can.py:1002-1035). A participant
    stop detaches that participant only; the others stay attached and the
    broker stops after the last (canbroker.py:962-975). The session's lease is
    released (can.py:712-715), but inside a declared run the run keeps the bus
    (coordination.py:1170-1173). can_send and can_read then answer
    `session_not_active` (can.py:909-922)."""
    text = listed[STOP]["description"]

    assert START in text, text
    assert stated(text, r"\badapter\b", r"\b(leaves|closes|closed|shut)\b"), text
    assert other_participants_stay(text), text
    assert lease_is_released(text), text
    assert a_declared_run_keeps_the_bus(text), text
    for sibling in ("can_send", "can_read"):
        assert sibling in text, (sibling, text)
    assert "session_not_active" in text, text


def test_stop_names_what_a_stop_with_nothing_open_answers_and_how_it_fails(listed: dict[str, dict]) -> None:
    text = definition(listed[STOP])

    assert nothing_open_answers_was_active_false(listed[STOP]["description"]), listed[STOP]["description"]
    assert failed_close_keeps_the_session(text), text
    assert bridge_close_is_bounded(text), text


def test_stop_participant_says_what_a_name_with_no_open_session_answers(listed: dict[str, dict]) -> None:
    """A participant name with no session open under it (an unconfigured name,
    or any name on a bus without `shares`) closes nothing and answers
    `was_active: false`, whatever else is open on the bus (can.py:697-703)."""
    text = property_text(listed[STOP], "participant")

    assert participant_required_on_a_shared_bus(text), text
    assert nothing_open_answers_was_active_false(text), text
    assert names(r"\bcloses nothing\b|\bnothing is closed\b|\bstays open\b|\bleaves\b", text), text


# ---------------------------------------------------------------------------
# What neither definition may say.


def test_every_tool_either_definition_names_is_listed(listed: dict[str, dict]) -> None:
    for tool_name in (START, STOP):
        text = definition(listed[tool_name])
        named = {token for token in re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", text) if token.startswith(("can_session_", "can_buses_", "bench_run_"))}
        named |= {token for token in ("can_send", "can_read") if re.search(rf"\b{token}\b", text)}
        assert named <= set(listed), (tool_name, sorted(named - set(listed)))
    for sibling in SIBLINGS:
        assert sibling in listed, sibling


def test_every_field_and_code_either_definition_names_exists_in_the_code(listed: dict[str, dict]) -> None:
    """A result field or error code written in backticks has to be one the code
    spells. A name no code returns is an outcome a model would wait for in vain."""
    source = "\n".join(path.read_text(encoding="utf-8") for path in Path(agentic_hil.__file__).parent.rglob("*.py"))
    tools = set(listed)
    for tool_name in (START, STOP):
        text = definition(listed[tool_name])
        written = set(re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?=[`:\s])", text)) - tools
        unknown = sorted(token for token in written if f'"{token}"' not in source)
        assert unknown == [], (tool_name, unknown)


@pytest.mark.parametrize("tool_name", [START, STOP])
def test_no_definition_promises_an_adapter_or_a_bitrate_the_code_does_not_give(listed: dict[str, dict], tool_name: str) -> None:
    """Only peak, socketcan and process open (can.py:74-76, config.py:3674-3680);
    another interface may be named only to exclude it. A socketcan bus does not
    apply its configured bitrate (can.py:425-445), so a clause that speaks of a
    socketcan bitrate says so."""
    text = definition(listed[tool_name])

    assert SUPPORTED_CAN_ADAPTERS == ["peak", "socketcan", "process"]
    for clause in clauses(text):
        promised = [name for name in UNSUPPORTED_INTERFACES if names(rf"\b{name}\b", clause)]
        if promised:
            assert names(r"\b(not|no|unsupported|only)\b", clause), (promised, clause)
        if names(r"\bbitrate\b", clause) and names(r"\bsocketcan\b", clause):
            assert names(r"bitrate_verified" + FALSE + r"|\bnot (applied|set|verified)\b", clause), clause


@pytest.mark.parametrize("tool_name", [START, STOP])
def test_the_annotations_stay_and_the_text_does_not_contradict_them(listed: dict[str, dict], tool_name: str) -> None:
    tool = listed[tool_name]

    assert tool["annotations"] == ANNOTATIONS[tool_name], tool["annotations"]
    assert not names(r"read-only|does not (touch|change)|no effect on the bus", tool["description"]), tool["description"]


# ---------------------------------------------------------------------------
# The behaviour the definitions describe, with python-can and the broker faked.


class QueuedBus(RecordingBus):
    """A python-can bus whose receive queue the test fills."""

    def __init__(self, queued: int = 0) -> None:
        super().__init__()
        self.queue = [frame_on_the_wire(index) for index in range(queued)]

    def queue_frames(self, count: int) -> None:
        self.queue.extend(frame_on_the_wire(index) for index in range(count))

    def recv(self, timeout: float = 0.0) -> object | None:
        return self.queue.pop(0) if self.queue else None


def frame_on_the_wire(index: int) -> SimpleNamespace:
    return SimpleNamespace(arbitration_id=0x100 + index, is_extended_id=False, is_remote_frame=False, data=bytes([index]), dlc=1)


def single_owner_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, queued: int = 0) -> tuple[AgenticHILToolService, list[QueuedBus]]:
    opened: list[QueuedBus] = []

    def open_bus(**kwargs: object) -> QueuedBus:
        bus = QueuedBus(queued)
        opened.append(bus)
        return bus

    monkeypatch.setitem(sys.modules, "can", fake_can_module(open_bus))
    config = load_config(str(write_config(tmp_path, can_buses_yaml=SINGLE_OWNER_YAML)))
    return AgenticHILToolService(config), opened


def shared_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgenticHILToolService:
    SharedFakeParticipant.frames = []

    def attach(config, bus_id, participant, **kwargs):
        return SharedFakeParticipant(participant)

    monkeypatch.setattr("agentic_hil.canbroker.attach_participant", attach)
    return AgenticHILToolService(load_config(str(write_config(tmp_path, can_buses_yaml=SHARED_YAML))))


def test_a_repeat_start_keeps_the_open_adapter_and_drains_it_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first start drains what was queued before it; a second start on the
    open session reopens nothing, answers `already_active`, and drains again."""
    service, opened = single_owner_service(tmp_path, monkeypatch, queued=2)
    try:
        first = service.call(START, {"bus_id": "bench"})
        assert first["ok"] is True, first
        assert first["already_active"] is False, first
        assert first["frames_drained"] == 2, first
        assert first["session"]["session_active"] is True, first

        opened[0].queue_frames(3)
        again = service.call(START, {"bus_id": "bench"})
        assert again["ok"] is True, again
        assert again["already_active"] is True, again
        assert again["frames_drained"] == 3, again
        assert len(opened) == 1, "a repeat start opened a second adapter"

        kept = service.call(START, {"bus_id": "bench", "clear_rx_queue": False})
        assert kept["already_active"] is True and kept["frames_drained"] == 0, kept
    finally:
        service.close()


def test_stopping_closes_the_adapter_and_leaves_send_and_read_without_a_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, opened = single_owner_service(tmp_path, monkeypatch)
    try:
        assert service.call(START, {"bus_id": "bench"})["ok"] is True

        stopped = service.call(STOP, {"bus_id": "bench"})
        sent = service.call("can_send", {"bus_id": "bench", "frame_id": 0x123, "data_hex": "01"})
        read = service.call("can_read", {"bus_id": "bench"})

        assert stopped["ok"] is True and stopped["was_active"] is True, stopped
        assert opened[0].closed is True
        assert sent["error_type"] == "session_not_active", sent
        assert read["error_type"] == "session_not_active", read
        assert opened[0].sent == []

        reopened = service.call(START, {"bus_id": "bench"})
        assert reopened["ok"] is True and reopened["already_active"] is False, reopened
        assert len(opened) == 2
    finally:
        service.close()


def test_a_stop_with_nothing_open_answers_was_active_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, opened = single_owner_service(tmp_path, monkeypatch)
    try:
        before = service.call(STOP, {"bus_id": "bench"})
        assert service.call(START, {"bus_id": "bench"})["ok"] is True
        assert service.call(STOP, {"bus_id": "bench"})["was_active"] is True
        after = service.call(STOP, {"bus_id": "bench"})
    finally:
        service.close()

    for answer in (before, after):
        assert answer["ok"] is True, answer
        assert answer["was_active"] is False, answer
    assert len(opened) == 1


def test_closing_the_server_closes_an_open_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A session lasts until can_session_stop or until the server closes."""
    service, opened = single_owner_service(tmp_path, monkeypatch)
    try:
        assert service.call(START, {"bus_id": "bench"})["ok"] is True
        assert opened[0].closed is False
    finally:
        service.close()

    assert opened[0].closed is True


def test_a_participant_name_with_no_open_session_stops_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a bus without `shares` a participant name finds no session, so the
    stop answers `was_active: false` and the open session goes on."""
    service, opened = single_owner_service(tmp_path, monkeypatch)
    try:
        assert service.call(START, {"bus_id": "bench"})["ok"] is True

        named = service.call(STOP, {"bus_id": "bench", "participant": "ecu_a"})
        listed = service.call("can_buses_list", {})
        sent = service.call("can_send", {"bus_id": "bench", "frame_id": 0x123, "data_hex": "01"})
    finally:
        service.close()

    assert named["ok"] is True and named["was_active"] is False, named
    assert listed["buses"]["bench"]["session_active"] is True, listed
    assert sent["ok"] is True, sent
    assert len(opened[0].sent) == 1


def test_an_unconfigured_participant_on_a_shared_bus_stops_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = shared_service(tmp_path, monkeypatch)
    try:
        assert service.call(START, {"bus_id": SHARED_BUS, "participant": "ecu_a", "clear_rx_queue": False})["ok"] is True

        unknown = service.call(STOP, {"bus_id": SHARED_BUS, "participant": "nobody"})
        active = service.call("can_buses_list", {})["buses"][SHARED_BUS]["active_participants"]
    finally:
        service.close()

    assert unknown["ok"] is True and unknown["was_active"] is False, unknown
    assert active == ["ecu_a"], active


def test_a_shared_bus_needs_a_participant_to_start_and_to_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = shared_service(tmp_path, monkeypatch)
    try:
        started = service.call(START, {"bus_id": SHARED_BUS})
        stopped = service.call(STOP, {"bus_id": SHARED_BUS})
    finally:
        service.close()

    for refused in (started, stopped):
        assert refused["ok"] is False, refused
        assert refused["error_type"] == "can_participant_required", refused
        assert refused["configured_participants"] == ["ecu_a", "ecu_b"], refused


def test_a_participant_session_drains_nothing_and_keeps_what_is_queued_for_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`clear_rx_queue` is not applied to a participant: the start reports
    `frames_drained: 0` and a frame already queued for the view is still read."""
    service = shared_service(tmp_path, monkeypatch)
    try:
        SharedFakeParticipant.frames.append({"id": 0x321, "data": b"\x07", "extended": False, "rtr": False, "sender": "ecu_a"})

        started = service.call(START, {"bus_id": SHARED_BUS, "participant": "ecu_b", "clear_rx_queue": True})
        again = service.call(START, {"bus_id": SHARED_BUS, "participant": "ecu_b", "clear_rx_queue": True})
        read = service.call("can_read", {"bus_id": SHARED_BUS, "participant": "ecu_b", "max_frames": 4})
        stopped = service.call(STOP, {"bus_id": SHARED_BUS, "participant": "ecu_b"})
    finally:
        service.close()

    assert started["ok"] is True and started["frames_drained"] == 0, started
    assert started["adapter"] == "broker", started
    assert again["already_active"] is True and again["frames_drained"] == 0, again
    assert [frame["id"] for frame in read["frames"]] == [0x321], read
    assert stopped["ok"] is True and stopped["was_active"] is True, stopped
    assert stopped["participant"] == "ecu_b", stopped


def test_a_participant_name_on_a_bus_without_shares_is_refused_at_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A start that names a participant is a participant start, whatever the
    bus: on a bus without `shares` no name is declared, so the start is refused
    and nothing is opened (can.py:503-504, 648-650)."""
    service, opened = single_owner_service(tmp_path, monkeypatch)
    try:
        named = service.call(START, {"bus_id": "bench", "participant": "ecu_a"})
        listed = service.call("can_buses_list", {})
    finally:
        service.close()

    assert named["ok"] is False and named["error_type"] == "can_participant_not_configured", named
    assert named["configured_participants"] == [], named
    assert opened == []
    assert listed["buses"]["bench"]["session_active"] is False, listed


def test_one_participant_stopping_leaves_the_others_attached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A participant stop detaches that participant's view only; the other
    participant's session stays open and keeps reading (can.py:697-715,
    canbroker.py:962-975)."""
    views: dict[str, SharedFakeParticipant] = {}
    SharedFakeParticipant.frames = []

    def attach(config, bus_id, participant, **kwargs):
        views[participant] = SharedFakeParticipant(participant)
        return views[participant]

    monkeypatch.setattr("agentic_hil.canbroker.attach_participant", attach)
    service = AgenticHILToolService(load_config(str(write_config(tmp_path, can_buses_yaml=SHARED_YAML))))
    try:
        for name in ("ecu_a", "ecu_b"):
            assert service.call(START, {"bus_id": SHARED_BUS, "participant": name, "clear_rx_queue": False})["ok"] is True
        stopped = service.call(STOP, {"bus_id": SHARED_BUS, "participant": "ecu_a"})
        active = service.call("can_buses_list", {})["buses"][SHARED_BUS]["active_participants"]
        read = service.call("can_read", {"bus_id": SHARED_BUS, "participant": "ecu_b", "max_frames": 1})
        gone = service.call("can_read", {"bus_id": SHARED_BUS, "participant": "ecu_a", "max_frames": 1})
    finally:
        service.close()

    assert stopped["ok"] is True and stopped["was_active"] is True and stopped["participant"] == "ecu_a", stopped
    assert active == ["ecu_b"], active
    assert read["ok"] is True, read
    assert gone["error_type"] == "session_not_active", gone
    assert views["ecu_a"].detached is True


def test_a_bridge_open_is_bounded_by_the_entry_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A process bus's bridge is given the entry's own `timeout_s` to answer
    `open` (can.py:2139-2141), 10 s when the entry names none (config.py:3699)."""
    executable = tmp_path / "bridge.py"
    executable.write_text("", encoding="utf-8")
    yaml = f'can_buses:\n  bench:\n    adapter: "process"\n    channel: "{CHANNEL}"\n    executable: "{executable.as_posix()}"\n    timeout_s: 2.5\n'
    config = load_config(str(write_config(tmp_path, can_buses_yaml=yaml)))
    waits: list[tuple[str, float]] = []

    class TimedBridge(RecordingBridge):
        def request(self, method: str, params: dict[str, object], timeout_s: float) -> dict[str, object]:
            waits.append((method, timeout_s))
            return super().request(method, params, timeout_s)

    bridge = TimedBridge({"ok": True, "protocol_version": 2})

    def session(child: object, timeout_s: float = 10.0) -> TimedBridge:
        waits.append(("session", timeout_s))
        return bridge

    monkeypatch.setattr("agentic_hil.can.spawn_managed_process", lambda *args, **kwargs: SimpleNamespace(pid=1))
    monkeypatch.setattr("agentic_hil.can.ProcessCanAdapterSession", session)

    opened = open_process_adapter(config, "bench", config.can_buses["bench"], False)

    assert opened["ok"] is True, opened
    assert waits == [("session", 2.5), ("open", 2.5)], waits
    unset = load_config(str(write_config(tmp_path / "unset", can_buses_yaml=yaml.replace("    timeout_s: 2.5\n", ""))))
    assert unset.can_buses["bench"].timeout_s == 10.0


def test_a_bridge_silent_for_a_second_at_close_fails_the_stop_and_keeps_the_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The close request gives a bridge 1 s (bridge.py:68). Unanswered, the
    bridge's safe state is unconfirmed, the close raises (bridge.py:98-110), and
    the stop answers `can_adapter_close_failed` with the session still
    registered (can.py:704-707)."""
    waits: list[tuple[str, float]] = []

    class SilentBridge(ProcessCanAdapterSession):
        def request(self, method: str, params: dict[str, object], timeout_s: float) -> dict[str, object]:
            waits.append((method, timeout_s))
            return self._bridge_error("timeout", f"{self.bridge_label} request timed out.")

    monkeypatch.setattr("agentic_hil.bridge.terminate_process_tree", lambda child, timeout_s: None)
    child = SimpleNamespace(pid=1, stdout=io.StringIO(""), stderr=io.StringIO(""), poll=lambda: None)
    config = load_config(str(write_config(tmp_path, can_buses_yaml=SINGLE_OWNER_YAML)))
    log_path = tmp_path / ".agentic-hil" / "logs" / "can.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    service = CanBusService(config)
    service.sessions[("bench", None)] = CanBusSession("bench", config.can_buses["bench"], SilentBridge(child), str(log_path))  # type: ignore[arg-type]
    try:
        stopped = service.session_stop("bench")
        kept = ("bench", None) in service.sessions
    finally:
        service.sessions.clear()
        service.close()

    assert waits == [("close", 1)], waits
    assert stopped["ok"] is False and stopped["error_type"] == "can_adapter_close_failed", stopped
    assert kept is True
