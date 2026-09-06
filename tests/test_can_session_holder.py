"""A second session on a channel this process already holds names this process (#501).

The listen-only refusal tells an operator to add a second `can_buses` entry for
the same channel and send on that one, so two entries on one channel is a
configuration the product itself recommends. The channel is one lock, so the
second session is refused; what #501 observed is that the refusal blamed
"another Agentic HIL process" and carried neither `holder` nor
`holder_is_this_process`, because the coordinator's own per-resource lock
answered before the device mutex that knows who holds the channel.

Faked python-can, because the question is about the locks and not about the
socket; the same case is run against a real virtual interface in
tests/container/test_can_over_vcan.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config

from agentic_hil.config import load_config
from agentic_hil.tools import AgenticHILToolService

# Distinctive by design: device locks are machine-wide, so a channel shared with
# another test would contend across clones.
CHANNEL = "vcan501pin"

TWO_ENTRIES_ON_ONE_CHANNEL = f"""can_buses:
  bus_a:
    adapter: "socketcan"
    channel: "{CHANNEL}"
  bus_b:
    adapter: "socketcan"
    channel: "{CHANNEL}"
"""


class FakeSocketcanBus:
    """What the tests need of a bound socket: nothing to read, a send that is taken, a close."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.sent: list[object] = []
        self.closed = False

    def recv(self, timeout: float = 0.0) -> None:
        return None

    def send(self, message: object, timeout: float | None = None) -> None:
        self.sent.append(message)

    def shutdown(self) -> None:
        self.closed = True


def fake_can_module() -> SimpleNamespace:
    return SimpleNamespace(
        Bus=FakeSocketcanBus,
        Message=lambda **kwargs: SimpleNamespace(**kwargs),
        CanInitializationError=type("CanInitializationError", (Exception,), {}),
    )


def two_entry_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgenticHILToolService:
    monkeypatch.setitem(sys.modules, "can", fake_can_module())
    config = load_config(str(write_config(tmp_path, can_buses_yaml=TWO_ENTRIES_ON_ONE_CHANNEL)))
    return AgenticHILToolService(config)


def test_a_second_session_on_a_channel_this_process_holds_names_this_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = two_entry_service(tmp_path, monkeypatch)
    try:
        first = service.call("can_session_start", {"bus_id": "bus_a"})
        assert first["ok"] is True, first

        refused = service.call("can_session_start", {"bus_id": "bus_b"})

        assert refused["ok"] is False, refused
        assert refused["tool"] == "can_session_start", refused
        assert refused["bus_id"] == "bus_b", refused
        assert refused["retry_safe"] is True, refused
        assert refused["side_effect_committed"] is False, refused
        assert "another" not in refused["summary"], refused
        holder = refused.get("holder")
        assert isinstance(holder, dict), refused
        assert holder["pid"] == os.getpid(), refused
        assert refused["holder_is_this_process"] is True, refused
    finally:
        service.close()


def test_the_refusal_leaves_the_session_that_holds_the_channel_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour: the first session keeps the channel and still sends and reads."""
    service = two_entry_service(tmp_path, monkeypatch)
    try:
        assert service.call("can_session_start", {"bus_id": "bus_a"})["ok"] is True
        assert service.call("can_session_start", {"bus_id": "bus_b"})["ok"] is False

        listed = service.call("can_buses_list")["buses"]
        assert listed["bus_a"]["session_active"] is True, listed
        assert listed["bus_b"]["session_active"] is False, listed
        sent = service.call("can_send", {"bus_id": "bus_a", "frame_id": "0x100", "data_hex": "01"})
        assert sent["ok"] is True, sent
        assert service.call("can_read", {"bus_id": "bus_a", "wait_timeout_s": 0.0})["ok"] is True

        stopped = service.call("can_session_stop", {"bus_id": "bus_a"})
        assert stopped["ok"] is True and stopped["was_active"] is True, stopped
        # Released, the channel opens for the other entry.
        second = service.call("can_session_start", {"bus_id": "bus_b"})
        assert second["ok"] is True and second["already_active"] is False, second
    finally:
        service.close()
