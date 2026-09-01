"""What a refused CAN bridge open is allowed to claim about the bus.

The defect these tests exist for: `open_process_adapter` answered
`side_effect_committed: False` on the protocol-shape refusal even when the bridge
had already answered `ok: true` to `open`. A bridge that has opened is on the bus;
whether it sent anything only the bridge knows. `False` there was not a finding
but an assumption, and the optimistic one: the reader of that report is told the
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

from agentic_hil.can import CanBusService, ProcessCanAdapterSession, bridge_opened_before_failing, open_process_adapter
from agentic_hil.config import load_config
from agentic_hil.report import ContactMarker


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


def bridge_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: dict[str, object], contact: ContactMarker | None = None
) -> tuple[dict[str, object], RecordingBridge]:
    config = process_bus_config(tmp_path)
    bridge = install_bridge(monkeypatch, response)
    return open_process_adapter(config, "bench", config.can_buses["bench"], False, contact), bridge


def started_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: dict[str, object]) -> dict[str, object]:
    """The report a caller is handed, which is where the claim has to be right."""
    config = process_bus_config(tmp_path)
    install_bridge(monkeypatch, response)
    service = CanBusService(config)
    try:
        return service.session_start("bench", clear_rx_queue=False)
    finally:
        service.close()


# --- The bridge opened, and then the answer disqualified itself ----------------


def test_a_bridge_that_opened_then_answered_in_the_wrong_shape_does_not_claim_nothing_happened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case as it was reported: `ok: true`, then a response in the wrong shape."""
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


# --- The two halves of a bridge's own `ok: false` -----------------------------
#
# agentic-hil/agentic-hil#141. A bridge's refusal of `open` was one case and is
# now two, split on what the bridge says about its own channel. The clean
# refusal (the mistyped channel, by far the most common bridge failure) keeps
# the answer it has always had, because it is a bad config and not a bench
# incident. The bridge that opened a channel and then failed at a later step of
# its own initialization is on the bus while it says so, and gets the treatment
# the direct-adapter path gives a post-contact failure.


def test_a_mistyped_channel_stays_a_retry_safe_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The case the split had to leave alone, pinned end to end.

    `open` failed cleanly, before the bridge's channel was on the bus and with
    nothing claiming otherwise. Nothing was touched, and the caller is told to
    fix the channel name and call again.
    """
    started = started_session(
        tmp_path,
        monkeypatch,
        {"ok": False, "error_type": "can_adapter_channel_unavailable", "summary": "vcan9 does not exist."},
    )

    assert started["ok"] is False
    assert started["error_type"] == "can_adapter_channel_unavailable"
    assert started["side_effect_committed"] is False
    assert started["side_effect_status"] == "not_started"
    assert started["retry_safe"] is True
    assert started["target_contacted"] is False


def test_a_bridge_that_failed_after_opening_its_channel_does_not_claim_nothing_happened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: `ok: false`, and the bridge reports the channel was already up."""
    result, bridge = bridge_result(
        tmp_path,
        monkeypatch,
        {
            "ok": False,
            "error_type": "can_adapter_filters_rejected",
            "channel_open": True,
            "summary": "The channel opened on vcan0 and the receive filters were rejected.",
        },
    )

    assert result["ok"] is False
    assert result["error_type"] == "can_adapter_filters_rejected"
    # The bridge was on the bus when it failed, so what it did there is its own to
    # know. No marker, rather than the claim that the session never started.
    assert "side_effect_committed" not in result
    assert result["cleanup_confirmed"] is True
    assert bridge.closed is True


def test_the_report_for_a_post_open_bridge_failure_says_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end, because `mark_side_effect` is what turns the absence into wording."""
    started = started_session(
        tmp_path,
        monkeypatch,
        {
            "ok": False,
            "error_type": "can_adapter_filters_rejected",
            "channel_open": True,
            "summary": "The channel opened on vcan0 and the receive filters were rejected.",
        },
    )

    assert started["ok"] is False
    assert started["side_effect_status"] == "unknown"
    assert started["retry_safe"] is False
    assert "side_effect_committed" not in started
    assert "target_contacted" not in started
    # The lease still comes back: the bridge was closed and that close was
    # confirmed, so nothing here sends an operator to a bench.
    assert started.get("quarantined") is not True


def test_the_contact_marker_carries_why_the_post_open_failure_is_unproven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker is what the lease decision reads, so it has to be the thing that moves."""
    contact = ContactMarker()

    bridge_result(
        tmp_path,
        monkeypatch,
        {"ok": False, "error_type": "can_adapter_filters_rejected", "channel_open": True, "summary": "Filters rejected."},
        contact,
    )

    assert contact.proves_no_contact is False
    assert contact.at is None, "nothing proved contact either; this is the third state"


def test_a_clean_refusal_leaves_the_marker_proving_no_contact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same marker, on the half of the split that did not move."""
    contact = ContactMarker()

    bridge_result(
        tmp_path,
        monkeypatch,
        {"ok": False, "error_type": "can_adapter_channel_unavailable", "summary": "vcan9 does not exist."},
        contact,
    )

    assert contact.proves_no_contact is True


@pytest.mark.parametrize(
    "channel_open",
    [False, "true", 1, None],
    ids=["false", "string", "integer", "null"],
)
def test_only_a_positive_statement_from_the_bridge_withholds_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel_open: object
) -> None:
    """Anything that is not `true` leaves the refusal saying the bus was not touched.

    A bridge is code this project did not write, and the field only ever widens
    an unknown. Reading a truthy `1` or the string `"true"` as the claim would
    let a typo in someone else's bridge take a bench out of service.
    """
    result, _ = bridge_result(
        tmp_path,
        monkeypatch,
        {"ok": False, "error_type": "can_adapter_channel_unavailable", "channel_open": channel_open, "summary": "vcan9 does not exist."},
    )

    assert result["side_effect_committed"] is False


def test_the_classifier_reads_the_field_only_on_a_first_person_refusal() -> None:
    """`ok: true` is settled by the shape check above it; this only speaks for `ok: false`."""
    assert bridge_opened_before_failing({"ok": False, "channel_open": True}) is True
    assert bridge_opened_before_failing({"ok": False}) is False
    assert bridge_opened_before_failing({"ok": True, "channel_open": True}) is False


def test_a_bridge_that_never_ran_cannot_acquire_the_field_from_the_transport() -> None:
    """The pre-write refusals are synthesized here, so they never carry an open channel."""
    session = ProcessCanAdapterSession(SimpleNamespace(poll=lambda: 0, stdout=iter(()), stderr=iter(())), 1.0)

    exited = session.request("open", {}, 1.0)

    assert exited["error_type"] == "can_adapter_process_exited"
    assert bridge_opened_before_failing(exited) is False


def test_the_transport_spells_these_failures_the_way_this_module_classifies_them() -> None:
    """The classification is by error type, so the spelling has to be the real one."""
    from agentic_hil.can import BRIDGE_OPEN_UNANSWERED

    session = ProcessCanAdapterSession(SimpleNamespace(poll=lambda: 0, stdout=iter(()), stderr=iter(())), 1.0)

    exited = session.request("open", {}, 1.0)

    assert exited["error_type"] == "can_adapter_process_exited"
    assert exited["error_type"] not in BRIDGE_OPEN_UNANSWERED
    assert all(error.startswith(f"{ProcessCanAdapterSession.error_prefix}_") for error in BRIDGE_OPEN_UNANSWERED)
