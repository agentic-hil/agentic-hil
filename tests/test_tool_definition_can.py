"""What a host reads about opening and closing a CAN session (#630).

`can_session_start` and `can_session_stop` are read here the way a host reads
them: through a real `tools/list` request answered by the server. What they
must carry is what the code does today. Which bus a call names and where that
name comes from, what each adapter needs installed or configured, what a
participant on a bus with `shares` is, what `clear_rx_queue` drains and where
it does nothing, what a repeat start and a stop with nothing open answer, what
stopping closes and releases, which outcomes a caller meets, how long an open
may wait, and how the session relates to `can_send` and `can_read`.

The checks are about meaning, not wording: a fact has to be named, in any
sentence. A test never pins a sentence.

The second half holds the behaviour those definitions describe, with python-can
and the broker faked, where no existing test already holds it. The rest is held
elsewhere and not repeated: an unconfigured bus on both tools
(tests/test_sessions_devices_coordination.py), python-can missing
(the same file and tests/test_python_can_import_error.py), a bus another holder
has (tests/test_can_session_holder.py, tests/test_can_broker.py), a queue that
will not drain (tests/test_hardening.py), a listen-only mode the adapter does
not confirm or the link does not report (tests/test_can_listen_only.py,
tests/test_can_interface_down.py), and the broker that stops with its last
participant (tests/test_can_broker.py).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config
from test_can_bridge_contact import RecordingBridge
from test_can_frame_and_routing import RecordingBus, fake_can_module
from test_can_participant_sessions import BUS as SHARED_BUS
from test_can_participant_sessions import SHARES_YAML, SharedFakeParticipant
from test_mcp_envelope import real_service
from test_tool_descriptions import DESCRIPTION_LIMIT, PROPERTY_DESCRIPTION_LIMIT, property_descriptions

from agentic_hil.can import SUPPORTED_CAN_ADAPTERS, open_process_adapter
from agentic_hil.config import load_config
from agentic_hil.knowledge import LISTEN_ONLY_MODE_ERROR, LISTEN_ONLY_UNCONFIRMED_ERROR, LISTEN_ONLY_UNSUPPORTED_ERROR
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

START = "can_session_start"
STOP = "can_session_stop"
SIBLINGS = ("can_buses_list", "can_send", "can_read")

# Device locks are machine-wide, so every bus opened here gets a channel no
# other test module names.
CHANNEL = "vcan630doc"
SINGLE_OWNER_YAML = f'can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "{CHANNEL}"\n'
SHARED_YAML = SHARES_YAML.replace("channel: fake-can", "channel: fake-can-630doc")

# python-can interfaces this server does not open. A definition naming one would
# promise an adapter the configuration schema refuses.
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


def definition(tool: dict) -> str:
    """Everything a host shows a model about one tool: the description and every property description."""
    return "\n".join([str(tool["description"]), *(text for _, text in property_descriptions(tool["inputSchema"]))])


def names(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


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
    """A bus is chosen by the name of its `can_buses` entry; can_buses_list shows them."""
    text = property_text(listed[tool_name], "bus_id")

    assert "can_buses" in text, text
    assert "can_buses_list" in text, text


# ---------------------------------------------------------------------------
# can_session_start: what it opens, what it needs, what it answers.


def test_start_says_what_it_opens_and_which_tools_use_the_session(listed: dict[str, dict]) -> None:
    """The lifecycle in order: the session is what can_send and can_read need,
    and can_session_stop ends it."""
    text = listed[START]["description"]

    assert names(r"\b(open|start)", text) and names(r"\bbus\b", text), text
    for sibling in ("can_send", "can_read", STOP):
        assert sibling in text, (sibling, text)


def test_start_names_what_each_adapter_needs_before_it_can_open(listed: dict[str, dict]) -> None:
    """peak and socketcan open through python-can, which `agentic-hil[can]`
    installs; a process bus runs the bridge named by its `executable`. Without
    python-can the refusal is `can_backend_not_available`."""
    text = definition(listed[START])

    for needed in ("python-can", "agentic-hil[can]", "peak", "socketcan", "process", "executable", "can_backend_not_available"):
        assert needed in text, (needed, text)


def test_start_says_how_long_an_open_may_wait(listed: dict[str, dict]) -> None:
    """A bridge's open request is bounded by the entry's own `timeout_s`, and the
    receive-queue drain is bounded too (`can_queue_clear_limit`)."""
    text = definition(listed[START])

    assert "timeout_s" in text, text
    assert "can_queue_clear_limit" in text, text


def test_start_names_the_listen_only_condition(listed: dict[str, dict]) -> None:
    """On a `listen_only` bus a direct session opens only where the mode is
    confirmed: a socketcan link that does not report it is refused before the
    open (`can_listen_only_unsupported`), a PCAN channel or a bridge that does not
    confirm it is closed again (`can_listen_only_unconfirmed`). Any listen-only
    code the text names has to be one the code answers."""
    text = definition(listed[START])
    codes = set(re.findall(r"can_listen_only_[a-z_]+", text))

    assert "listen_only" in text, text
    assert codes, text
    assert codes <= {LISTEN_ONLY_UNSUPPORTED_ERROR, LISTEN_ONLY_UNCONFIRMED_ERROR, LISTEN_ONLY_MODE_ERROR}, codes


def test_start_names_its_result_fields_and_its_refusals(listed: dict[str, dict]) -> None:
    text = definition(listed[START])

    for field in ("already_active", "frames_drained"):
        assert field in text, (field, text)
    for outcome in ("can_bus_not_configured", "device_busy", "resource_busy"):
        assert outcome in text, (outcome, text)


def test_participant_says_when_it_is_required_and_what_it_opens(listed: dict[str, dict]) -> None:
    """On a bus with `shares` a participant is required. One broker owns the
    adapter, and each participant gets its own filtered, private receive queue."""
    text = property_text(listed[START], "participant")

    assert "shares" in text, text
    assert names(r"\brequired\b|\bmust\b|can_participant_required", text), text
    assert names(r"\bbroker\b", text), text
    assert names(r"\bfilter", text), text
    assert names(r"\bprivate\b|\bown\b", text), text


def test_clear_rx_queue_names_its_default_what_it_drains_and_where_it_does_nothing(listed: dict[str, dict]) -> None:
    """Default true. It discards the frames already queued, on a repeat call as
    well, and counts them in `frames_drained`. The drain is bounded. On a
    participant session it does nothing (can.py `_participant_session_start`)."""
    text = property_text(listed[START], "clear_rx_queue")

    assert names(r"\bdefault", text) and names(r"\btrue\b", text), text
    assert names(r"\b(discard|drain|clear|empt)", text), text
    assert "frames_drained" in text, text
    assert names(r"\b(repeat|again|already)", text), text
    assert "can_queue_clear_limit" in text, text
    assert names(r"\bparticipant", text) and names(r"\b(ignored|no effect|nothing)\b", text), text


# ---------------------------------------------------------------------------
# can_session_stop: what it closes and releases, and what it answers.


def test_stop_says_what_it_closes_and_releases(listed: dict[str, dict]) -> None:
    """The adapter is closed or the participant detaches from the broker, the
    bus lock is released, and can_send and can_read then have no session."""
    text = listed[STOP]["description"]

    assert START in text, text
    assert names(r"\badapter\b", text), text
    assert names(r"\bdetach", text) and names(r"\bbroker\b", text), text
    assert names(r"\breleas", text), text
    for sibling in ("can_send", "can_read"):
        assert sibling in text, (sibling, text)
    assert "session_not_active" in text, text


def test_stop_names_what_a_stop_with_nothing_open_answers_and_its_failure(listed: dict[str, dict]) -> None:
    text = definition(listed[STOP])

    assert "was_active" in text, text
    assert "can_adapter_close_failed" in text, text


def test_stop_participant_says_what_a_name_with_no_open_session_answers(listed: dict[str, dict]) -> None:
    """A participant name with no session open under it (an unconfigured name,
    or any name on a bus without `shares`) closes nothing and answers
    `was_active: false`, whatever else is open on the bus."""
    text = property_text(listed[STOP], "participant")

    assert "shares" in text, text
    assert names(r"\brequired\b|\bmust\b|can_participant_required", text), text
    assert "was_active" in text, text


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


@pytest.mark.parametrize("tool_name", [START, STOP])
def test_no_definition_promises_an_adapter_or_a_bitrate_the_code_does_not_give(listed: dict[str, dict], tool_name: str) -> None:
    """Only peak, socketcan and process open. A socketcan bus does not apply its
    configured bitrate, so a definition that speaks of the bitrate names
    `bitrate_verified` beside it."""
    text = definition(listed[tool_name])

    assert SUPPORTED_CAN_ADAPTERS == ["peak", "socketcan", "process"]
    promised = [name for name in UNSUPPORTED_INTERFACES if names(rf"\b{name}\b", text)]
    assert promised == [], promised
    if names(r"\bbitrate\b", text):
        assert "bitrate_verified" in text, text


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


def test_a_bridge_open_is_bounded_by_the_entry_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A process bus's bridge is given the entry's own `timeout_s` to answer `open`."""
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
