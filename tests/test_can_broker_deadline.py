"""What a participant is told when the broker it started never publishes (#532).

`_attach_with_broker` has two ends. A broker that exits on its own with an
explained code is turned into the adapter's or the configuration's own refusal.
A client whose deadline expires first kills whatever it started and raises the
refusal it set up before the loop began: `can_broker_unavailable`, "No CAN
broker for this bus could be reached or started", and nothing else. That second
end had no test at all, and the three situations it covers want three different
answers:

* a broker that exited inside the final poll window is dead with a code the
  explained branch one screen above already knows how to read, and the loop
  simply never looks again;
* a broker that printed its refusal and is still inside a blocking shutdown is
  killed with its answer already on disk, where a short wait would have let it
  be classified;
* a broker still inside its adapter open wrote nothing about this attempt at
  all, and the only honest refusal is one that says a broker was started and
  terminated at the deadline, names the log, and names the attach deadline
  beside the bus's own adapter timeout so the mismatch between the two is
  visible.

Reading the log's tail is deliberately *not* the answer to the third one, and
the last test here is the reason: `_last_broker_document` returns the last
document in a file appended by every broker ever started for the bus, and it is
right on the explained path only because the exit code attributes it. A client
that read that tail after killing a live broker would hand the caller an earlier
broker's failure as this attempt's cause.

The client's own clock is the fake below, so every one of these situations is
exact rather than raced: the broker's death and the loop's deadline are the same
number, which is what makes "inside the final poll window" a fact and not a
timing hope. One test keeps the real clock, because "bounded" is a claim about
wall-clock time and has to be measured on one.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import time
from pathlib import Path

import pytest
from conftest import write_authoritative_config
from support import scaled_time_bound

from agentic_hil import canbroker
from agentic_hil.bench import BenchMutex
from agentic_hil.canbroker import (
    BROKER_EXIT_ADAPTER,
    BROKER_EXIT_BUS_BUSY,
    BROKER_EXIT_CONFIG,
    BROKER_EXITS_EXPLAINED,
    ParticipantError,
    attach_participant,
    broker_log_path,
    bus_lock_key,
)
from agentic_hil.config import load_authoritative_config

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_CAN_BRIDGE = FIXTURES / "fake_can_bridge.py"

BUS_ID = "deadline_bus"
PARTICIPANT = "alpha"

# The client's attach deadline in every fake-clock test here. The clock starts
# at zero, so the deadline the loop computes is this number exactly, and a
# broker scripted to die "at the deadline" dies on the same comparison the loop
# ends on.
ATTACH_DEADLINE_S = 3.0
# The bus's own adapter timeout, deliberately longer than the attach deadline.
# That is the ordinary configuration behind case one: the broker is still inside
# an adapter open the client will never wait out, and the two numbers side by
# side in the refusal are what makes that visible.
BUS_ADAPTER_TIMEOUT_S = 11.0

# An exit code no branch of the client knows. It stands for the whole class the
# issue is about: an unhandled exception, a failed import, a signal, or an exit
# a future broker adds.
UNRECOGNISED_EXIT_CODE = 1

# The most a broker that is mid shutdown may be waited for at the deadline, and
# the bound the wall-clock test below allows the whole failed attach on top of
# its own deadline. A budget larger than this stops being "short".
GRACE_CEILING_S = 3.0
# The deadline of the one test that runs on the real clock. Short, because what
# it measures is the budget spent after the deadline, not the deadline.
REAL_ATTACH_DEADLINE_S = 0.5

# The document an explained broker leaves as the last line of its log. Seeded by
# hand here: what these tests drive is the client, and the broker that wrote it
# is scripted rather than run.
ADAPTER_DOCUMENT = {
    "ok": False,
    "error_type": "can_adapter_timeout",
    "summary": "The CAN adapter did not answer the open request.",
    "backend_error": "the bridge closed the pipe",
    "stderr_tail": "the bridge closed the pipe",
}
CONFIG_DOCUMENT = {
    "ok": False,
    "error_type": "can_bus_not_shared",
    "summary": "The authoritative config declares no shared CAN bus of that name.",
}
# A document from a broker started for this bus at some earlier time, sitting in
# the log where every broker for this bus appends. Its marker must never reach a
# refusal raised for a later broker whose exit code does not attribute it.
EARLIER_DOCUMENT = {
    "ok": False,
    "error_type": "can_adapter_timeout",
    "summary": "An earlier broker on this bus could not open its adapter.",
    "backend_error": "an earlier broker's bridge, hours ago",
    "stderr_tail": "an earlier broker's bridge, hours ago",
}
EARLIER_MARKER = "an earlier broker's bridge, hours ago"

GENERIC_SUMMARY = "No CAN broker for this bus could be reached or started."


class FakeClock:
    """The client's clock, advanced only by the client's own sleeps.

    Installed over `canbroker.time`, which rebinds the name inside that module
    and nowhere else. Everything the module might read that is not the two calls
    the attach loop makes falls through to the real module.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)

    def __getattr__(self, name: str):
        return getattr(time, name)


class ScriptedBroker:
    """A spawned broker whose whole life is one moment of death and one code.

    Stands in for the `Popen` the client holds. `poll` and `wait` both read the
    clock the test drives, so a broker dies where the test says it does whether
    the client waits for it, polls for it, or does neither.
    """

    def __init__(self, clock, *, dies_at: float | None = None, exit_code: int | None = None) -> None:
        self.clock = clock
        self.dies_at = dies_at
        self.exit_code = exit_code
        self.pid = -1
        self.terminated = False
        self.killed = False
        self.returncode: int | None = None

    def _code(self) -> int | None:
        if self.dies_at is None or self.clock.monotonic() < self.dies_at:
            return None
        self.returncode = self.exit_code
        return self.exit_code

    def poll(self) -> int | None:
        return self._code()

    def wait(self, timeout: float | None = None) -> int:
        code = self._code()
        if code is not None:
            return code
        if self.dies_at is not None and (timeout is None or self.dies_at - self.clock.monotonic() <= timeout):
            self.clock.sleep(max(0.0, self.dies_at - self.clock.monotonic()))
            return self._code()
        self.clock.sleep(float(timeout or 0.0))
        raise subprocess.TimeoutExpired(cmd="agentic-hil-can-broker", timeout=timeout)

    def terminate(self) -> None:
        self.terminated = True
        if self._code() is None:
            self.dies_at = self.clock.monotonic()
            self.exit_code = -15

    def kill(self) -> None:
        self.killed = True
        self.terminate()


@dataclasses.dataclass
class Attempt:
    """What one failed attach left behind."""

    result: dict
    spawned: list
    clock: FakeClock
    bus_key: str
    log_path: Path


def deadline_bus_yaml(channel: str, timeout_s: float) -> str:
    return (
        "can_buses:\n"
        f"  {BUS_ID}:\n"
        '    adapter: "process"\n'
        f'    channel: "{channel}"\n'
        f'    executable: "{FAKE_CAN_BRIDGE.as_posix()}"\n'
        f"    timeout_s: {timeout_s}\n"
        "    shares:\n"
        f"      {PARTICIPANT}:\n"
        "        max_frames: 16\n"
        "        permissions:\n"
        "          allow_read: true\n"
        "          allow_write: true\n"
    )


def prepared_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str, *, documents=(), timeout_s: float = BUS_ADAPTER_TIMEOUT_S):
    """A configured shared bus whose broker log holds exactly `documents`.

    Every test here names its own channel, because the bus lock key, the broker
    log and the participant lock are all derived from it and are machine wide.
    """
    workspace = tmp_path / "project"
    write_authoritative_config(workspace, monkeypatch, can_buses_yaml=deadline_bus_yaml(channel, timeout_s))
    config = load_authoritative_config(workspace)
    bus_key = bus_lock_key(config, BUS_ID)
    log_path = broker_log_path(bus_key, BenchMutex().root)
    log_path.write_text("".join(f"{json.dumps(document)}\n" for document in documents), encoding="utf-8")
    return config, bus_key, log_path


def attach_against(config, bus_key: str, log_path: Path, monkeypatch: pytest.MonkeyPatch, script, *, clock: FakeClock | None = None) -> Attempt:
    """Run one attach whose brokers are `script(clock, index)` and never real."""
    clock = FakeClock() if clock is None else clock
    spawned: list = []

    def spawn(*args, **kwargs):
        child = script(clock, len(spawned))
        spawned.append(child)
        return child

    monkeypatch.setattr(canbroker, "time", clock)
    monkeypatch.setattr(canbroker, "_spawn_broker", spawn)
    with pytest.raises(ParticipantError) as refused:
        attach_participant(config, BUS_ID, PARTICIPANT, start_timeout_s=ATTACH_DEADLINE_S)
    return Attempt(result=refused.value.result, spawned=spawned, clock=clock, bus_key=bus_key, log_path=log_path)


# ---------------------------------------------------------------------------
# Case three: the broker exited inside the final poll window.


def test_a_broker_that_exited_inside_the_final_poll_window_is_refused_with_its_own_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop ended one comparison before the answer was there to be read.

    The broker dies on the same moment the loop's deadline names, so every poll
    the loop makes is a poll of a living process and the loop leaves without
    ever classifying it. The code is `BROKER_EXIT_ADAPTER`, the branch that
    reads that code sits one screen above, and the document it wants is already
    the last line of the log. Asking `poll` once more after the loop is the
    whole fix, and the participant gets the adapter's refusal rather than the
    sentence that names no cause.
    """
    config, bus_key, log_path = prepared_bus(tmp_path, monkeypatch, "vcan532a", documents=[ADAPTER_DOCUMENT])

    attempt = attach_against(config, bus_key, log_path, monkeypatch, lambda clock, index: ScriptedBroker(clock, dies_at=ATTACH_DEADLINE_S, exit_code=BROKER_EXIT_ADAPTER))
    result = attempt.result

    assert result["summary"] != GENERIC_SUMMARY, result
    assert result["error_type"] == "can_adapter_timeout", result
    assert result["summary"] == "The CAN broker started for this bus could not open its adapter: The CAN adapter did not answer the open request.", result
    assert result["broker_exit_code"] == BROKER_EXIT_ADAPTER, result
    assert result["broker_log"] == str(log_path), result
    assert result["backend_error"] == "the bridge closed the pipe", result
    assert result["retry_safe"] is True, result
    assert result["bus_id"] == BUS_ID and result["participant"] == PARTICIPANT, result
    # A dead process is not something to terminate, and one broker met one
    # adapter: the loop never got to spawn a second.
    assert len(attempt.spawned) == 1, attempt.spawned
    assert attempt.spawned[0].terminated is False, "a broker that had already exited was terminated"


# ---------------------------------------------------------------------------
# Case two: the broker printed its refusal and is still shutting down.


def test_a_broker_still_inside_its_shutdown_is_waited_for_and_reclassifies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Its answer is on disk and its process has not finished leaving.

    The broker entry point prints its refusal and then runs `broker.shutdown()`
    in a `finally`, which closes the adapter session, which a wedged adapter can
    block. In that window the client kills a process whose answer it already
    had. A short budget spent after the deadline, and only on an attach that has
    already failed, lets the exit be read and the refusal be the explained one.
    """
    config, bus_key, log_path = prepared_bus(tmp_path, monkeypatch, "vcan532b", documents=[CONFIG_DOCUMENT])
    grace = canbroker.BROKER_SHUTDOWN_GRACE_S
    assert 0.0 < grace <= GRACE_CEILING_S, f"the shutdown grace is not a short budget: {grace}"

    attempt = attach_against(config, bus_key, log_path, monkeypatch, lambda clock, index: ScriptedBroker(clock, dies_at=ATTACH_DEADLINE_S + grace / 2, exit_code=BROKER_EXIT_CONFIG))
    result = attempt.result

    assert result["summary"] != GENERIC_SUMMARY, result
    assert result["error_type"] == "can_bus_not_shared", result
    assert result["summary"] == "The CAN broker started for this bus could not load its configuration: The authoritative config declares no shared CAN bus of that name.", result
    assert result["broker_exit_code"] == BROKER_EXIT_CONFIG, result
    assert result["broker_log"] == str(log_path), result
    assert result["retry_safe"] is True, result
    assert attempt.spawned[-1].terminated is False, "a broker that answered inside the budget was killed anyway"
    # The budget is spent, and it is spent once: the clock stands past the
    # deadline by the lag and no further.
    assert attempt.clock.now < ATTACH_DEADLINE_S + grace + canbroker.BROKER_POLL_INTERVAL_S, attempt.clock.now


def test_the_budget_after_the_deadline_is_bounded_on_a_real_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broker that never answers is still refused, and promptly.

    The one test here that keeps the real clock, because "short" is a claim
    about wall-clock time and the fake clock cannot make it. A budget that grew
    into a second deadline would show up nowhere else.
    """
    config, bus_key, log_path = prepared_bus(tmp_path, monkeypatch, "vcan532c")
    assert 0.0 < canbroker.BROKER_SHUTDOWN_GRACE_S <= GRACE_CEILING_S, canbroker.BROKER_SHUTDOWN_GRACE_S
    monkeypatch.setattr(canbroker, "_spawn_broker", lambda *args, **kwargs: ScriptedBroker(time))

    started_at = time.monotonic()
    with pytest.raises(ParticipantError):
        attach_participant(config, BUS_ID, PARTICIPANT, start_timeout_s=REAL_ATTACH_DEADLINE_S)
    elapsed_s = time.monotonic() - started_at

    assert elapsed_s < scaled_time_bound(REAL_ATTACH_DEADLINE_S + GRACE_CEILING_S), elapsed_s


# ---------------------------------------------------------------------------
# Case one: nothing was written, so the refusal has to be honest about itself.


def test_a_broker_terminated_at_the_deadline_names_the_log_and_both_timeouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The broker is still inside an adapter open this client will never wait out.

    Nothing about this attempt is in the log, because the adapter has not failed
    yet, and terminating means the cause is never written at all. This is not a
    narrow race: the attach deadline and the bus's `timeout_s` are set
    independently, and a bus whose adapter timeout is the longer of the two
    makes this the ordinary outcome rather than the unlucky one.

    So the refusal says what happened instead of what did not: a broker was
    started and terminated at the deadline, the log is where to look, the two
    timeouts stand beside each other so the mismatch is readable, and
    `retry_safe` and `side_effect_committed` are stated the way every sibling
    refusal on this path states them. It carries no document, because there is
    none belonging to this attempt.
    """
    config, bus_key, log_path = prepared_bus(tmp_path, monkeypatch, "vcan532d")

    attempt = attach_against(config, bus_key, log_path, monkeypatch, lambda clock, index: ScriptedBroker(clock))
    result = attempt.result

    assert result["ok"] is False, result
    assert result["error_type"] == "can_broker_unavailable", result
    assert result["summary"] != GENERIC_SUMMARY, result
    assert "started" in result["summary"], result
    assert "terminated" in result["summary"], result
    assert "deadline" in result["summary"], result
    assert result["broker_log"] == str(log_path), result
    assert result["broker_start_timeout_s"] == ATTACH_DEADLINE_S, result
    assert result["bus_timeout_s"] == BUS_ADAPTER_TIMEOUT_S, result
    assert result["retry_safe"] is True, result
    assert result["side_effect_committed"] is False, result
    assert result["bus_id"] == BUS_ID and result["bus_key"] == bus_key, result
    assert result["participant"] == PARTICIPANT, result
    # Nothing exited, so nothing is attributed: no exit code, and no fields out
    # of a broker's document.
    assert "broker_exit_code" not in result, result
    assert "backend_error" not in result, result
    assert "stderr_tail" not in result, result
    # One broker, started once and terminated once. A live process is what the
    # loop leaves behind, and killing it is the client's own doing.
    assert len(attempt.spawned) == 1, attempt.spawned
    assert attempt.spawned[0].terminated is True, "the broker the client started was left running"


# ---------------------------------------------------------------------------
# The attribution hazard, pinned directly.


@pytest.mark.parametrize(
    ("channel", "dies_at", "exit_code"),
    [
        ("vcan532e", None, None),
        ("vcan532f", ATTACH_DEADLINE_S, UNRECOGNISED_EXIT_CODE),
    ],
    ids=["still-running-at-the-deadline", "dead-with-an-unrecognised-code"],
)
def test_an_earlier_brokers_document_is_never_this_attempts_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str, dies_at: float | None, exit_code: int | None) -> None:
    """The reason the obvious fix is the wrong one.

    `_last_broker_document` reads the last document in a file appended by every
    broker ever started for this bus. On the explained path that is safe because
    the exit code proves the document belongs to the broker that just exited.
    Here no exit code proves anything: the broker is still running, or it exited
    with a code no branch knows. A refusal that quoted the tail anyway would
    hand the caller an earlier broker's failure as this attempt's cause, and a
    confidently wrong cause is worse than a generic one.

    Naming the log is not quoting it: `broker_log` is a place to look and stays.
    """
    config, bus_key, log_path = prepared_bus(tmp_path, monkeypatch, channel, documents=[EARLIER_DOCUMENT])
    assert exit_code not in BROKER_EXITS_EXPLAINED, exit_code

    attempt = attach_against(config, bus_key, log_path, monkeypatch, lambda clock, index: ScriptedBroker(clock, dies_at=dies_at, exit_code=exit_code))
    result = attempt.result

    assert EARLIER_MARKER not in json.dumps(result, default=str), result
    assert EARLIER_MARKER in log_path.read_text(encoding="utf-8"), "the earlier document is supposed to still be in the log"
    assert result["error_type"] == "can_broker_unavailable", result
    assert "backend_error" not in result, result
    assert "stderr_tail" not in result, result
    assert result["broker_log"] == str(log_path), result
    # The one field the exit code does attribute is the exit code itself, and
    # only when there was one.
    assert result.get("broker_exit_code") in (None, exit_code), result


# ---------------------------------------------------------------------------
# The neighbours that must not move.


def test_the_configuration_exit_still_refuses_at_once_with_its_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The working shape, well inside the deadline, unchanged by any of the above.

    The adapter half of this pair is covered end to end against a real broker
    and a real failing bridge in
    `tests/test_sessions_devices_coordination.py::test_a_broker_whose_adapter_cannot_open_refuses_the_participant_with_the_adapter_error`;
    the configuration exit had no test, and it is the branch a post-loop poll is
    most able to disturb.
    """
    config, bus_key, log_path = prepared_bus(tmp_path, monkeypatch, "vcan532g", documents=[CONFIG_DOCUMENT])
    dies_at = 2 * canbroker.BROKER_POLL_INTERVAL_S

    attempt = attach_against(config, bus_key, log_path, monkeypatch, lambda clock, index: ScriptedBroker(clock, dies_at=dies_at, exit_code=BROKER_EXIT_CONFIG))
    result = attempt.result

    assert result["error_type"] == "can_bus_not_shared", result
    assert result["summary"] == "The CAN broker started for this bus could not load its configuration: The authoritative config declares no shared CAN bus of that name.", result
    assert result["broker_exit_code"] == BROKER_EXIT_CONFIG, result
    assert result["broker_log"] == str(log_path), result
    assert result["retry_safe"] is True, result
    assert len(attempt.spawned) == 1, "a broker that explained itself was replaced by another"
    # The refusal came when the broker exited, not when the deadline arrived.
    assert attempt.clock.now < ATTACH_DEADLINE_S / 2, attempt.clock.now


def test_a_bus_busy_exit_is_still_retried_rather_than_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Somebody else owning the bus is a race, and the deadline is its answer.

    The first broker loses the race for the bus lock and exits
    `BROKER_EXIT_BUS_BUSY`; the client drops it and starts another, which is the
    behaviour that lets the winner's descriptor appear. That code is not
    explained and must not become one: the refusal at the end carries no exit
    code as a cause and no broker's document.
    """
    config, bus_key, log_path = prepared_bus(tmp_path, monkeypatch, "vcan532h", documents=[EARLIER_DOCUMENT])

    def script(clock, index):
        if index == 0:
            return ScriptedBroker(clock, dies_at=clock.monotonic(), exit_code=BROKER_EXIT_BUS_BUSY)
        return ScriptedBroker(clock)

    attempt = attach_against(config, bus_key, log_path, monkeypatch, script)
    result = attempt.result

    assert len(attempt.spawned) == 2, "a bus busy exit stopped being retried"
    assert attempt.spawned[0].terminated is False, "a broker that had already exited was terminated"
    assert attempt.spawned[1].terminated is True, "the broker running at the deadline was left behind"
    assert result["error_type"] == "can_broker_unavailable", result
    assert result["broker_log"] == str(log_path), result
    assert "broker_exit_code" not in result, result
    assert EARLIER_MARKER not in json.dumps(result, default=str), result
