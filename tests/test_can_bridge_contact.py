"""What a refused CAN bridge open is allowed to claim about the bus.

The defect these tests exist for (hardci-hq#112): `open_process_adapter` answered
`side_effect_committed: False` on the protocol-shape refusal even when the bridge
had already answered `ok: true` to `open`. A bridge that has opened is on the bus;
whether it sent anything only the bridge knows. `False` there was not a finding
but an assumption, and the optimistic one — the reader of that report is told the
session never started.

agentic-hil/agentic-hil#115 settled this for the listen-only refusal, which is the
one *about* bus contact, and left the older shape path alone so that review stayed
about listen-only. These tests cover the rest of the path: the shape refusal the
issue names, and its neighbours where the open request was delivered and no usable
answer came back.

The other half of the rule matters as much and is pinned here too. Withholding the
marker everywhere would be no more honest than asserting it: where the bridge was
provably never asked, or answered a refusal of its own, the report still says the
bus was not touched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config

from agentic_hil.can import CanBusService, ProcessCanAdapterSession, open_process_adapter
from agentic_hil.config import load_config


class RecordingBridge:
    """Stands in for the transport, so a test can name the answer `open` gets."""

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def request(self, method: str, params: dict[str, object], timeout_s: float) -> dict[str, object]:
        self.requests.append((method, params))
        return dict(self.response)

    def close(self) -> dict[str, object]:
        self.closed = True
        return {"ok": True}


def process_bus_config(tmp_path: Path) -> object:
    executable = tmp_path / "bridge.py"
    executable.write_text("", encoding="utf-8")
    can_buses_yaml = (
        "can_buses:\n"
        "  bench:\n"
        '    adapter: "process"\n'
        '    channel: "vcan0"\n'
        f'    executable: "{executable.as_posix()}"\n'
    )
    return load_config(str(write_config(tmp_path, can_buses_yaml=can_buses_yaml)))


def install_bridge(monkeypatch: pytest.MonkeyPatch, response: dict[str, object]) -> RecordingBridge:
    bridge = RecordingBridge(response)
    monkeypatch.setattr("agentic_hil.can.spawn_managed_process", lambda *args, **kwargs: SimpleNamespace(pid=1))
    monkeypatch.setattr("agentic_hil.can.ProcessCanAdapterSession", lambda child, timeout_s=10.0: bridge)
    return bridge


def bridge_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: dict[str, object]) -> tuple[dict[str, object], RecordingBridge]:
    config = process_bus_config(tmp_path)
    bridge = install_bridge(monkeypatch, response)
    return open_process_adapter(config, "bench", config.can_buses["bench"], False), bridge


# --- The bridge opened, and then the answer disqualified itself ----------------


def test_a_bridge_that_opened_then_answered_in_the_wrong_shape_does_not_claim_nothing_happened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof hardci-hq#112 asked for: `ok: true`, then a response in the wrong shape."""
    result, bridge = bridge_result(tmp_path, monkeypatch, {"ok": True, "protocol_version": 2, "channels_open": 1})

    assert result["ok"] is False
    assert result["error_type"] == "can_adapter_protocol_unsupported"
    # The bridge said yes to `open`. What it did on the bus after that is its own
    # to know, so the refusal withholds the marker instead of claiming there was
    # no contact.
    assert "side_effect_committed" not in result
    assert result["cleanup_confirmed"] is True
    assert bridge.closed is True


def test_a_bridge_on_the_wrong_protocol_version_does_not_claim_nothing_happened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same refusal, reached by the other half of the shape check."""
    result, _ = bridge_result(tmp_path, monkeypatch, {"ok": True, "protocol_version": 1})

    assert result["error_type"] == "can_adapter_protocol_unsupported"
    assert "side_effect_committed" not in result


def test_the_report_a_reader_gets_says_unknown_rather_than_never_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, because the claim that mattered is the one in the written report.

    `mark_side_effect` derives the wording from the marker's absence, so this is
    where "never started" would have survived a fix applied only to the dict.
    """
    config = process_bus_config(tmp_path)
    install_bridge(monkeypatch, {"ok": True, "protocol_version": 2, "channels_open": 1})
    service = CanBusService(config)
    try:
        started = service.session_start("bench", clear_rx_queue=False)
    finally:
        service.close()

    assert started["ok"] is False
    assert started["error_type"] == "can_adapter_protocol_unsupported"
    assert started["side_effect_status"] == "unknown"
    assert started["retry_safe"] is False
    assert "side_effect_committed" not in started
    # The lease is still released: the bridge was closed and that close was
    # confirmed. What changed is what the report claims, not whether an operator
    # is sent to a bench.
    assert started.get("quarantined") is not True


# --- The request was delivered and no usable answer came back -----------------


def test_a_bridge_that_never_answered_the_open_is_not_read_as_never_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout is the weakest evidence of all: the request went out, nothing came back."""
    result, _ = bridge_result(
        tmp_path,
        monkeypatch,
        {"ok": False, "adapter": "process", "error_type": "can_adapter_timeout", "summary": "CAN adapter bridge request timed out."},
    )

    assert result["ok"] is False
    assert result["error_type"] == "can_adapter_timeout"
    assert "side_effect_committed" not in result


def test_an_unreadable_answer_to_open_is_not_read_as_never_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge answered, so it was processing the open; the envelope says no more."""
    result, _ = bridge_result(
        tmp_path,
        monkeypatch,
        {"ok": False, "adapter": "process", "error_type": "can_adapter_invalid_response", "summary": "CAN adapter bridge returned a result without boolean ok."},
    )

    assert result["ok"] is False
    assert "side_effect_committed" not in result


# --- Where the bus provably was not touched, the report still says so ---------


def test_an_open_request_that_never_reached_the_bridge_still_says_the_bus_was_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raised before the write, so nothing was asked of the bridge and nothing opened."""
    result, _ = bridge_result(
        tmp_path,
        monkeypatch,
        {"ok": False, "adapter": "process", "error_type": "can_adapter_process_exited", "summary": "CAN adapter bridge process is not running."},
    )

    assert result["side_effect_committed"] is False


def test_a_bridge_that_refused_the_open_itself_still_says_the_bus_was_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first-person refusal is an answer, and this path takes it as one."""
    result, _ = bridge_result(
        tmp_path,
        monkeypatch,
        {"ok": False, "error_type": "can_adapter_channel_unavailable", "summary": "vcan0 does not exist."},
    )

    assert result["side_effect_committed"] is False


def test_the_transport_spells_these_failures_the_way_this_module_classifies_them() -> None:
    """The classification is by error type, so the spelling has to be the real one."""
    from agentic_hil.can import BRIDGE_OPEN_UNANSWERED

    session = ProcessCanAdapterSession(SimpleNamespace(poll=lambda: 0, stdout=iter(()), stderr=iter(())), 1.0)

    exited = session.request("open", {}, 1.0)

    assert exited["error_type"] == "can_adapter_process_exited"
    assert exited["error_type"] not in BRIDGE_OPEN_UNANSWERED
    assert all(error.startswith(f"{ProcessCanAdapterSession.error_prefix}_") for error in BRIDGE_OPEN_UNANSWERED)
