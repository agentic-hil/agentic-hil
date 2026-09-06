"""The lock, the incident, and the way back, on a bench two callers can reach.

Everything here is about what happens when more than one caller wants the one
board this machine has. That is not a question a fake can answer: the exclusion
is an operating system lock under the operator's own home, the holder record
beside it is written by a real process with a real pid, and the case that
matters most is the one where that process is gone. A mutex test with two
threads proves the bookkeeping; only two processes over one probe prove the lock.

Four claims, and each of them broke a bench once:

* a declared run holds its devices from `bench_run_start` to `bench_run_stop`,
  and a second caller meeting that hold is refused *at once*, told who holds it,
  and told that retrying is safe. Silent waiting is the failure mode this
  design exists to prevent: it hides a collision instead of reporting one, and
  the wait that does happen happens because somebody asked for it and is bounded.
* a run whose owner disappears does not take the board with it. The operating
  system drops the lock, the next caller takes it, and is told that the previous
  owner died rather than being handed a board with no explanation.
* an incident a dead owner left is named by the next caller and cleared by the
  next contact. Since the quarantine narrowed, `agentic-hil recover` is not the
  thing that clears it and says so; a bench that answered anything else here
  would either padlock itself after a crash or claim to have signed for a board
  nobody looked at.
* the operator's signature is not optional. `recover` without
  `--confirm-safe-state` never reaches the bench at all.

Every test in this file leaves the bench free and unquarantined, and says so in
its own docstring. Nothing here names a host, a user, a path under a home
directory or a probe serial: the device keys, the holder identities and the
quarantine identifiers are all read out of the product's own answers and
asserted on their shape.
"""

from __future__ import annotations

import itertools
import json
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress

import pytest

from .conftest import BENCH_ONLY, COMMAND_TIMEOUT_S, Bench, child_command

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# How long a server may take to answer `initialize` before that is a failure
# rather than a slow machine. Bounded because a server that starts and never
# answers would otherwise block a readline forever, and no timeout plugin is
# configured in this repository, so the job would run to its ceiling and report
# a timeout instead of a test saying what did not answer.
HANDSHAKE_TIMEOUT_S = 60.0

# How long a shutdown is given before the process is killed instead.
SHUTDOWN_TIMEOUT_S = 60.0

# How long a caller that asked for no wait may take to be refused a held device.
# Generous on purpose: what this catches is a refusal that quietly waits for the
# holder, which is unbounded, not one that took a second to open a lock file.
REFUSAL_CEILING_S = 30.0

# The wait a caller asks for when the point is that the wait is real and bounded.
# Long enough that a refusal which ignored it is unmistakable, short enough that
# a bench run does not pay for it.
ASKED_WAIT_S = 3.0

# What a real wait may fall short of the wait that was asked for. Clock
# granularity and the poll interval, not slack: the mutex polls, so the last
# poll can land a fraction before the deadline.
WAIT_SHORTFALL_S = 0.5

# The plans this file writes into the project. Named for this file so the six
# other authors writing into the same project directory cannot collide with it.
RESET_PLAN = "coordination-reset-only.yaml"

# A quarantine identifier that is not this bench's, for the one call that has to
# supply one to a bench with nothing standing. `--quarantine-id` is required by
# the parser, and the answer on a clean bench must not depend on what it says.
NOT_A_LIVE_INCIDENT = "0" * 32

# What `lease-status` must never publish about the machine it ran on. The record
# is stripped before it reaches an operator terminal or an MCP client, and these
# are the two keys that carry an absolute, environment-derived path.
RECORD_KEYS_THAT_NAME_THE_MACHINE = ("config_path", "workspace")

# What a teardown swallows on its way to giving the bench back. `pytest.fail`
# raises an outcome exception that does not derive from `Exception`, so a
# teardown suppressing only `Exception` would let a dead server's diagnosis
# escape as a teardown error and bury the assertion that actually failed.
TEARDOWN_FAULTS = (Exception, pytest.fail.Exception)


class Server:
    """One `agentic-hil mcp-stdio` process, spoken to over its own stdio.

    Written here rather than in `conftest.py` because six authors are writing
    into this directory at once. It is a client and nothing more: it frames
    JSON-RPC the way the server frames it (one object per line, flushed), keeps
    the ids apart, and reads with a bound so a server that stops answering fails
    the test that is waiting instead of hanging the session.
    """

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._process = process
        self._identifiers = itertools.count(1)
        self._answers: queue.Queue[str | None] = queue.Queue()
        self._noise: list[str] = []
        self._out = threading.Thread(target=self._pump_answers, daemon=True)
        self._out.start()
        self._err = threading.Thread(target=self._pump_noise, daemon=True)
        self._err.start()

    @classmethod
    def launch(cls, bench: Bench) -> Server:
        """Start a server on this session's configuration and finish its handshake."""
        process = subprocess.Popen(
            child_command("mcp-stdio"),
            cwd=str(bench.project),
            env=bench.environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        server = cls(process)
        answered = server.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "bench-tier", "version": "1"}},
            timeout_s=HANDSHAKE_TIMEOUT_S,
        )
        assert answered.get("result", {}).get("protocolVersion"), f"the server did not negotiate a protocol version: {answered}{server.diagnostics()}"
        server.notify("notifications/initialized", {})
        return server

    @property
    def alive(self) -> bool:
        return self._process.poll() is None

    @property
    def pid(self) -> int:
        return self._process.pid

    def diagnostics(self) -> str:
        """The tail of what this server wrote to stderr, for a failure message.

        Read out of the answer rather than swallowed: a server that refused its
        own configuration says why there and nowhere else, and a test that hid
        it would report only that nothing came back.
        """
        recent = self._noise[-20:]
        return "" if not recent else "\nthe server's stderr said:\n" + "".join(recent)

    def request(self, method: str, params: dict, timeout_s: float = COMMAND_TIMEOUT_S) -> dict:
        identifier = next(self._identifiers)
        self._write({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        while True:
            line = self._read(timeout_s, method)
            message = json.loads(line)
            if message.get("id") == identifier:
                return message

    def notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def call(self, name: str, arguments: dict | None = None, timeout_s: float = COMMAND_TIMEOUT_S) -> dict:
        """One tool result, as the structured document the contract is written in."""
        answered = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout_s=timeout_s)
        assert "error" not in answered, f"tools/call {name} answered a protocol error: {answered}{self.diagnostics()}"
        structured = answered["result"].get("structuredContent")
        assert isinstance(structured, dict), f"tools/call {name} answered no structuredContent: {answered}"
        return structured

    def kill(self) -> None:
        """End this server the way a crash ends one: no teardown, no release."""
        self._process.kill()
        self._process.wait(timeout=SHUTDOWN_TIMEOUT_S)

    def shut_down(self) -> None:
        """Give the bench back and end the process, whatever the test did to it.

        Best effort by construction and in this order: close the run first so the
        devices are released by the product rather than by the operating system,
        then close stdin, which is what ends the server's read loop and runs its
        own cleanup, then kill whatever is left.
        """
        if self._process.poll() is None:
            with suppress(*TEARDOWN_FAULTS):
                self.call("bench_run_stop", timeout_s=SHUTDOWN_TIMEOUT_S)
            with suppress(*TEARDOWN_FAULTS):
                if self._process.stdin is not None:
                    self._process.stdin.close()
            try:
                self._process.wait(timeout=SHUTDOWN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=SHUTDOWN_TIMEOUT_S)
        for stream in (self._process.stdout, self._process.stderr):
            with suppress(*TEARDOWN_FAULTS):
                if stream is not None:
                    stream.close()

    def _write(self, message: dict) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    def _read(self, timeout_s: float, method: str) -> str:
        try:
            line = self._answers.get(timeout=timeout_s)
        except queue.Empty:
            pytest.fail(f"the server did not answer {method} within {timeout_s:.0f}s{self.diagnostics()}", pytrace=False)
        if line is None:
            pytest.fail(f"the server closed its output before answering {method} (exit {self._process.poll()}){self.diagnostics()}", pytrace=False)
        return line

    def _pump_answers(self) -> None:
        # The read is guarded as well as the `finally`: a teardown closes this
        # stream while the thread is sitting in it, and an unguarded reader would
        # print a traceback from a daemon thread over the report of whatever
        # actually failed.
        try:
            with suppress(Exception):
                if self._process.stdout is not None:
                    for line in self._process.stdout:
                        if line.strip():
                            self._answers.put(line)
        finally:
            self._answers.put(None)

    def _pump_noise(self) -> None:
        with suppress(Exception):
            if self._process.stderr is not None:
                for line in self._process.stderr:
                    self._noise.append(line)


@pytest.fixture
def servers(bench: Bench) -> Iterator[Callable[[], Server]]:
    """Start MCP servers, and take the bench back from every one of them.

    The teardown runs on the failing path as well as the passing one, and it is
    the reason it exists: a test that fails half way through a declared run must
    not leave the next test meeting a lock nobody is using. Servers are shut down
    in reverse order, so the one holding a run is closed before the one that was
    waiting on it.
    """
    started: list[Server] = []

    def start() -> Server:
        server = Server.launch(bench)
        started.append(server)
        return server

    try:
        yield start
    finally:
        for server in reversed(started):
            with suppress(*TEARDOWN_FAULTS):
                server.shut_down()


def reset_plan(bench: Bench) -> str:
    """A plan whose only step resets the board, written into the project.

    The smallest plan that declares the probe, which is what makes it useful
    here: this file needs a command line run that wants the one device another
    caller is holding, and a reset is the cheapest way to want it.
    """
    (bench.project / RESET_PLAN).write_text(
        f"""version: 3
name: coordination-reset-only
steps:
  - device: {bench.debugger_name()}
    action: reset
    mode: run
""",
        encoding="utf-8",
    )
    return RESET_PLAN


def probe_device(bench: Bench) -> dict:
    """The selector that names this bench's debugger to `bench_run_start`."""
    return {"kind": "debugger", "id": bench.debugger_name()}


def uart_device(bench: Bench) -> dict:
    """The selector that names this bench's serial port, or a skip."""
    return {"kind": "uart", "id": bench.com_port_name()}


def a_free_bench(bench: Bench) -> dict:
    """The lease status of a bench nothing is holding, or a failure saying otherwise.

    Read at the start of the tests whose whole subject is what a held or blocked
    bench looks like: starting one of those on a bench somebody else left held
    would measure the leftover rather than the claim, and reporting that as this
    test's failure would send the reader to the wrong file.
    """
    status, document = bench.document("lease-status")
    # `lease-status` exits 1 whenever something is standing on the bench, a hold or
    # an incident, because the exit status answers "is there anything standing";
    # the document is what these tests read, so the status is not asserted here.
    assert document["ok"] is True, document
    assert document["bench_held"] is False, f"this test needs a free bench and found one that is held: {document}"
    assert document["blocked"] is False, f"this test needs an unblocked bench and found an incident standing on it: {document}"
    return document


def release(holder: Server) -> None:
    """End a holder's run from a `finally`, without replacing the test's failure.

    Suppressed rather than asserted, and only where the result is not the thing
    under test: what says the devices really came back is the `lease-status`
    read at the end of each of those tests, and a fault raised out of a
    `finally` would bury the assertion that put the test there. The `servers`
    fixture stops every server it started either way.
    """
    with suppress(*TEARDOWN_FAULTS):
        holder.call("bench_run_stop")


def hand_the_bench_back(bench: Bench) -> None:
    """Clear whatever incident is standing, by the two routes the product names.

    The operator's `recover` for an incident that stands, and, for the class
    that ends at the next contact, a contact: the smallest plan this file
    writes, run through the reactor, which resets the target into `run` and
    leaves the starter firmware executing. Both go through the product; neither
    invents an identifier, and the second is skipped where the first was enough.
    """
    status, document = bench.document("lease-status")
    if status != 0 or document.get("blocked") is not True:
        return
    quarantine_id = document.get("quarantine_id")
    if isinstance(quarantine_id, str) and quarantine_id.strip():
        bench.run("recover", "--confirm-safe-state", "--quarantine-id", quarantine_id)
    status, document = bench.document("lease-status")
    if status == 0 and document.get("blocked") is True:
        bench.run("test-reactor", "--test-config", reset_plan(bench))


@pytest.fixture
def unquarantined(bench: Bench) -> Iterator[None]:
    """Give the bench back free and unblocked, whatever the test left standing.

    Requested *before* `servers` by every test that kills an owner or opens a
    session, so it is set up first and torn down last: the servers a test
    started are gone by the time this runs, and what it is looking at is the
    incident that outlived them. It is the failing path this exists for. A test
    that asserts its way to a green bench needs no help; one that fails halfway
    through a crashed-session case would otherwise hand the next test, and the
    next author's file, a bench that answers `blocked` to everything.

    Best effort by construction: the assertions inside the test are what say the
    bench came back, and a teardown that raised here would replace the failure
    that put it here.
    """
    try:
        yield
    finally:
        with suppress(*TEARDOWN_FAULTS):
            hand_the_bench_back(bench)


def evidence(server: Server) -> tuple:
    """What this bench's report and last error say right now, as one comparable value.

    Both documents at once, reduced to the fields that identify *which* record
    they are, so a test can say that a coordination refusal moved neither of
    them. Comparing the whole document would compare timestamps and digests that
    move for reasons this file is not about.
    """
    report = server.call("get_last_report")
    classified = server.call("classify_last_error")
    recorded = report.get("report") if isinstance(report.get("report"), dict) else {}
    return (
        report.get("ok"),
        report.get("error_type"),
        recorded.get("tool"),
        recorded.get("summary"),
        recorded.get("report_path"),
        classified.get("ok"),
        classified.get("error_type"),
        classified.get("source_tool"),
        classified.get("summary"),
    )


def test_a_declared_run_holds_the_board_across_calls_and_gives_it_back_at_stop(bench: Bench, servers) -> None:
    """The whole point of a run: the board is this caller's between two calls, and free after.

    Without the declaration every call took its device, released it at the end of
    the call, and left the board free between flash, reset and read, so a second
    session could slip in between two steps of a sequence and meet no lock at
    all. What proves the hold is not this server's own bookkeeping but a second
    process asking: `lease-status` runs in its own interpreter, probes the
    machine-wide lock, and reports a held bench for a run that never took the
    project lock at all. That reading is the one an operator asking "is the bench
    free" actually gets.

    Leaves the bench free: the run is stopped inside the test and the release is
    read back from `lease-status` before the test ends, and the fixture's
    teardown stops it again on the failing path.
    """
    a_free_bench(bench)
    server = servers()

    idle = server.call("bench_run_status")
    assert idle["ok"] is True, idle
    assert idle["tool"] == "bench_run_status", idle
    assert idle["run_active"] is False, idle
    assert idle["declared_devices"] == [], idle
    assert "No run is open" in idle["summary"], idle["summary"]

    started = server.call("bench_run_start", {"devices": [probe_device(bench)], "label": "coordination-hold"})
    assert started["ok"] is True, started
    assert started["tool"] == "bench_run_start", started
    declared = started["declared_devices"]
    assert declared and all(isinstance(key, str) and key.strip() for key in declared), started
    assert started["run_label"] == "coordination-hold", started
    assert isinstance(started["run_started_at"], str) and started["run_started_at"].strip(), started
    assert isinstance(started["owner"], dict) and started["owner"], started

    try:
        held = server.call("bench_run_status")
        assert held["run_active"] is True, held
        assert held["declared_devices"] == declared, held
        assert held["run_label"] == "coordination-hold", held
        assert set(declared) <= set(held["held_devices"]), held
        assert "until bench_run_stop" in held["summary"], held["summary"]

        # The second process, which is the reading that counts.
        status, outside = bench.document("lease-status")
        # `lease-status` exits 1 whenever something is standing on the bench, a hold or
        # an incident, because the exit status answers "is there anything standing";
        # the document is what these tests read, so the status is not asserted here.
        assert outside["ok"] is True, outside
        assert outside["bench_held"] is True, outside
        assert set(declared) <= set(outside["held_devices"]), outside
        holders = {hold["resource"]: hold for hold in outside["device_holds"] if isinstance(hold.get("resource"), str)}
        for key in declared:
            assert key in holders, outside["device_holds"]
            holder = holders[key].get("holder")
            assert isinstance(holder, dict) and isinstance(holder.get("pid"), int), holders[key]
            assert holder["pid"] == server.pid, "the hold is recorded against a process that is not the run's own server"
    finally:
        stopped = server.call("bench_run_stop")

    assert stopped["ok"] is True, stopped
    assert stopped["tool"] == "bench_run_stop", stopped
    assert stopped["run_was_active"] is True, stopped
    assert stopped["released_devices"] == declared, stopped
    assert "open_leases" not in stopped, stopped

    after = server.call("bench_run_status")
    assert after["run_active"] is False, after
    assert after["declared_devices"] == [], after

    status, freed = bench.document("lease-status")
    # `lease-status` exits 1 whenever something is standing on the bench, a hold or
    # an incident, because the exit status answers "is there anything standing";
    # the document is what these tests read, so the status is not asserted here.
    assert freed["ok"] is True, freed
    assert freed["bench_held"] is False, freed
    assert freed["held_devices"] == [], freed


def test_bench_run_stop_answers_a_run_that_was_never_open_and_answers_it_the_same_way_twice(bench: Bench, servers) -> None:
    """Closing a run that is not open is how an agent that lost track of itself recovers.

    So it answers rather than refusing, and it answers the same way however many
    times it is asked. A refusal here would leave an agent that had already
    stopped its run with an error it cannot act on, and the obvious reaction to
    that error is to try the hardware again.

    Leaves the bench free: nothing is ever held.
    """
    server = servers()

    first = server.call("bench_run_stop")
    second = server.call("bench_run_stop")

    for answer in (first, second):
        assert answer["ok"] is True, answer
        assert answer["tool"] == "bench_run_stop", answer
        assert answer["run_was_active"] is False, answer
        assert answer["released_devices"] == [], answer
    assert first["summary"] == second["summary"], (first, second)

    status, free = bench.document("lease-status")
    # `lease-status` exits 1 whenever something is standing on the bench, a hold or
    # an incident, because the exit status answers "is there anything standing";
    # the document is what these tests read, so the status is not asserted here.
    assert free["ok"] is True, free
    assert free["bench_held"] is False, free


def test_a_second_declaration_on_one_server_is_refused_and_the_open_run_keeps_its_devices(bench: Bench, servers) -> None:
    """One run per owner, and the refusal names the run that is already open.

    A second declaration that silently replaced the first would leave the first
    run's devices held under a declaration nobody is checking against any more,
    which is exactly the widening the declaration exists to prevent. The refusal
    is checked against the run afterwards: the open run must still hold what it
    declared, so this is a refusal and not a half-applied second declaration.

    Leaves the bench free: the run is stopped in the test and again by the
    fixture's teardown on the failing path.
    """
    a_free_bench(bench)
    server = servers()
    started = server.call("bench_run_start", {"devices": [probe_device(bench)], "label": "coordination-first"})
    assert started["ok"] is True, started
    declared = started["declared_devices"]

    try:
        again = server.call("bench_run_start", {"devices": [probe_device(bench)], "label": "coordination-second"})

        assert again["ok"] is False, again
        assert again["error_type"] == "run_already_active", again
        assert again["declared_devices"] == declared, again
        assert again["run_label"] == "coordination-first", again
        assert again["retry_safe"] is False, again
        assert again["side_effect_committed"] is False, again

        unchanged = server.call("bench_run_status")
        assert unchanged["run_active"] is True, unchanged
        assert unchanged["declared_devices"] == declared, unchanged
        assert unchanged["run_label"] == "coordination-first", unchanged
    finally:
        stopped = server.call("bench_run_stop")
    assert stopped["released_devices"] == declared, stopped

    status, free = bench.document("lease-status")
    # `lease-status` exits 1 whenever something is standing on the bench, a hold or
    # an incident, because the exit status answers "is there anything standing";
    # the document is what these tests read, so the status is not asserted here.
    assert free["ok"] is True, free
    assert free["bench_held"] is False, free


@pytest.mark.parametrize("wait", [True, "soon", -1, 1200])
def test_a_wait_that_is_not_a_bounded_number_of_seconds_is_refused_before_a_device_is_taken(bench: Bench, servers, wait: object) -> None:
    """The wait is validated before the lock, not converted on the way to it.

    `float(True)` is `1.0`, so a frontend that converted first turned a
    `wait_s: true` nobody could have meant into a one second wait, and handed
    the mutex an exception for a string or a negative. Each of these has to be
    named as an invalid argument on the field it was given for, and the run must
    not be open afterwards: a declaration refused for its wait that had already
    taken the board would hold a device for a call that answered an error.

    Leaves the bench free: no declaration succeeds, and the status read after
    each one says so.
    """
    server = servers()

    refused = server.call("bench_run_start", {"devices": [probe_device(bench)], "wait_s": wait})

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "invalid_argument", refused
    assert refused["field"] == "wait_s", refused

    idle = server.call("bench_run_status")
    assert idle["run_active"] is False, idle
    assert idle["declared_devices"] == [], idle
    status, free = bench.document("lease-status")
    # `lease-status` exits 1 whenever something is standing on the bench, a hold or
    # an incident, because the exit status answers "is there anything standing";
    # the document is what these tests read, so the status is not asserted here.
    assert free["ok"] is True, free
    assert free["bench_held"] is False, free


def test_a_second_caller_meeting_the_lock_is_refused_at_once_and_told_who_holds_it(bench: Bench, servers) -> None:
    """Silent waiting hides a collision, so a caller that asked for none gets none.

    Three things at once, and the third is the one that makes the refusal
    actionable rather than merely correct. It is refused, immediately, with no
    wait nobody asked for; it is `device_busy` and `retry_safe`, because the
    board will be free again and the caller is allowed to come back; and it
    names the holder, so the person reading it knows whether the run in their
    way is theirs. The elapsed time of the call is measured here rather than
    read off the result, because a refusal that waited and then reported no wait
    is exactly the failure this is looking for.

    Leaves the bench free: the holder's run is stopped in the test, and the
    fixture's teardown stops both servers on the failing path.
    """
    a_free_bench(bench)
    holder = servers()
    contender = servers()
    started = holder.call("bench_run_start", {"devices": [probe_device(bench)], "label": "coordination-holder"})
    assert started["ok"] is True, started
    declared = started["declared_devices"]

    try:
        began = time.monotonic()
        refused = contender.call("bench_run_start", {"devices": [probe_device(bench)]})
        elapsed = time.monotonic() - began

        assert refused["ok"] is False, refused
        assert refused["tool"] == "bench_run_start", refused
        assert refused["error_type"] == "device_busy", refused
        assert refused["retry_safe"] is True, refused
        assert refused["side_effect_committed"] is False, refused
        assert refused["resource"] in declared, refused
        assert refused["declared_devices"] == declared, refused
        assert isinstance(refused["held_since"], str) and refused["held_since"].strip(), refused
        holder_record = refused["holder"]
        assert isinstance(holder_record, dict), refused
        assert holder_record["pid"] == holder.pid, refused
        for field in ("host", "frontend"):
            assert isinstance(holder_record.get(field), str) and holder_record[field].strip(), refused
        assert "holder_is_this_process" not in refused, refused
        assert elapsed < REFUSAL_CEILING_S, f"a refusal with no wait asked for took {elapsed:.1f}s"

        # The contender took nothing on its way to being refused.
        assert contender.call("bench_run_status")["run_active"] is False, "the refused caller opened a run anyway"
        # And the holder still has exactly what it declared.
        assert holder.call("bench_run_status")["declared_devices"] == declared, "the refusal disturbed the run that was holding"
    finally:
        release(holder)

    status, free = bench.document("lease-status")
    # `lease-status` exits 1 whenever something is standing on the bench, a hold or
    # an incident, because the exit status answers "is there anything standing";
    # the document is what these tests read, so the status is not asserted here.
    assert free["ok"] is True, free
    assert free["bench_held"] is False, free


def test_a_second_caller_that_asked_to_wait_waits_that_long_and_is_then_refused(bench: Bench, servers) -> None:
    """A wait happens because somebody asked for it, and it ends.

    The other half of the rule above. The wait is real (the call does not come
    back before the deadline it was given) and it is bounded (it does come back,
    with the same named refusal, rather than polling the holder forever). The
    result says how long it waited, and that number has to be the wait that was
    asked for rather than a field carried over from a call that did not wait.

    Leaves the bench free: the holder's run is stopped in the test, and the
    fixture's teardown stops both servers on the failing path.
    """
    a_free_bench(bench)
    holder = servers()
    contender = servers()
    started = holder.call("bench_run_start", {"devices": [probe_device(bench)], "label": "coordination-holder"})
    assert started["ok"] is True, started
    declared = started["declared_devices"]

    try:
        began = time.monotonic()
        refused = contender.call("bench_run_start", {"devices": [probe_device(bench)], "wait_s": ASKED_WAIT_S})
        elapsed = time.monotonic() - began

        assert refused["ok"] is False, refused
        assert refused["error_type"] == "device_busy", refused
        assert refused["retry_safe"] is True, refused
        assert refused["resource"] in declared, refused
        assert elapsed >= ASKED_WAIT_S - WAIT_SHORTFALL_S, f"a wait of {ASKED_WAIT_S:.0f}s came back after {elapsed:.2f}s"
        assert elapsed < ASKED_WAIT_S + REFUSAL_CEILING_S, f"a wait of {ASKED_WAIT_S:.0f}s came back after {elapsed:.2f}s and was not bounded by it"
        waited = refused["waited_s"]
        assert isinstance(waited, (int, float)), refused
        assert waited >= ASKED_WAIT_S - WAIT_SHORTFALL_S, refused
        assert contender.call("bench_run_status")["run_active"] is False, "the caller that waited and was refused opened a run anyway"
    finally:
        release(holder)

    status, free = bench.document("lease-status")
    # `lease-status` exits 1 whenever something is standing on the bench, a hold or
    # an incident, because the exit status answers "is there anything standing";
    # the document is what these tests read, so the status is not asserted here.
    assert free["ok"] is True, free
    assert free["bench_held"] is False, free


def test_a_lock_this_bench_refused_is_not_written_into_its_report_or_its_last_error(bench: Bench, servers) -> None:
    """A collision is not a hardware failure, so it must not become the bench's evidence.

    `classify_last_error` is what an agent reads to find out what the board did
    wrong, and `get_last_report` is what a person reads afterwards. A refusal to
    even declare a run touched no hardware and produced no report, so both have
    to say exactly what they said before it: a refused lock that overwrote them
    would have the next reader diagnosing a board for a queue.

    Leaves the bench free: the holder's run is stopped in the test, and the
    fixture's teardown stops both servers on the failing path.
    """
    a_free_bench(bench)
    holder = servers()
    contender = servers()
    before = evidence(contender)
    started = holder.call("bench_run_start", {"devices": [probe_device(bench)], "label": "coordination-holder"})
    assert started["ok"] is True, started

    try:
        refused = contender.call("bench_run_start", {"devices": [probe_device(bench)]})
        assert refused["error_type"] == "device_busy", refused

        assert evidence(contender) == before, "a refused lock moved this bench's last report or its last error"
    finally:
        release(holder)

    status, free = bench.document("lease-status")
    # `lease-status` exits 1 whenever something is standing on the bench, a hold or
    # an incident, because the exit status answers "is there anything standing";
    # the document is what these tests read, so the status is not asserted here.
    assert free["ok"] is True, free
    assert free["bench_held"] is False, free


def test_the_command_line_meeting_the_lock_records_a_refusal_that_names_no_step(bench: Bench, servers) -> None:
    """The other frontend, the wait it was given, and what the refusal leaves behind.

    A plan refused the bench is a different thing from a plan that failed on it,
    and both are read by whoever comes next. The run has to be reported as one
    where no step ran, and that document is the one `get_last_report` and
    `classify_last_error` then serve: a refusal recorded as a hardware failure
    would send the next reader to the board, and a refusal recorded as nothing at
    all would leave a red command line with no evidence behind it.

    `--wait-s` is the command line's half of the bounded wait, so the elapsed
    time of the process is measured too.

    Leaves the bench free: the holder's run is stopped in the test, and the
    fixture's teardown stops it on the failing path.
    """
    a_free_bench(bench)
    plan = reset_plan(bench)
    holder = servers()
    started = holder.call("bench_run_start", {"devices": [probe_device(bench)], "label": "coordination-holder"})
    assert started["ok"] is True, started
    declared = started["declared_devices"]

    try:
        began = time.monotonic()
        status, report = bench.document("test-reactor", "--test-config", plan, "--wait-s", str(ASKED_WAIT_S))
        elapsed = time.monotonic() - began

        assert status == 1, report
        assert report["ok"] is False, report
        assert report["error_type"] == "device_busy", report
        assert report["tool"] == "test_reactor", report
        assert report["steps"] == [], report
        assert report["resource"] in declared, report
        assert report["declared_devices"], report
        # The sentence closes the refusal's own part of the summary; the report
        # writer appends where the run's report is kept, so it is not the last sentence.
        assert "No step ran." in report["summary"], report["summary"]
        assert elapsed >= ASKED_WAIT_S - WAIT_SHORTFALL_S, f"--wait-s {ASKED_WAIT_S:.0f} came back after {elapsed:.2f}s"

        recorded = holder.call("get_last_report")
        assert recorded["ok"] is True, recorded
        assert recorded["report"]["tool"] == "test_reactor", recorded
        assert recorded["report"]["error_type"] == "device_busy", recorded
        assert recorded["report"]["steps"] == [], recorded

        classified = holder.call("classify_last_error")
        assert classified["ok"] is True, classified
        assert classified["tool"] == "classify_last_error", classified
        assert classified["error_type"] == "device_busy", classified
        assert classified["source_tool"] == "test_reactor", classified
        assert "No step ran." in classified["summary"], classified["summary"]
    finally:
        release(holder)

    status, free = bench.document("lease-status")
    # `lease-status` exits 1 whenever something is standing on the bench, a hold or
    # an incident, because the exit status answers "is there anything standing";
    # the document is what these tests read, so the status is not asserted here.
    assert free["ok"] is True, free
    assert free["bench_held"] is False, free
    assert free["blocked"] is False, free


def test_lease_status_on_a_free_bench_says_so_without_naming_the_machine_it_ran_on(bench: Bench) -> None:
    """What an operator asking "is the bench free" is answered, and what that answer may carry.

    Two claims. The idle reading is unambiguous: nothing held, nothing blocked,
    no incident standing, no reasons and no quarantine identifier, and no
    `cleanup_required` key invented for a bench that owes nothing. And the record
    it publishes is the redacted one: the durable record carries the absolute
    configuration path and workspace of the machine it was written on, and this
    answer goes to an operator terminal and to an MCP client, so those keys are
    stripped before it leaves the process.

    Leaves the bench free: this reads and changes nothing.
    """
    status, document = bench.document("lease-status")

    # `lease-status` exits 1 whenever something is standing on the bench, a hold or

    # an incident, because the exit status answers "is there anything standing";

    # the document is what these tests read, so the status is not asserted here.

    assert document["ok"] is True, document
    assert document["ok"] is True, document
    assert document["tool"] == "hardware_lease_status", document
    assert document["owner_active"] is False, document
    assert document["bench_held"] is False, document
    assert document["held_devices"] == [], document
    assert document["device_holds"] == [], document
    assert document["blocked"] is False, document
    assert document["incident_stands"] is False, document
    assert document["cleanup_reasons"] == [], document
    assert document["quarantine_id"] is None, document
    assert document["snapshot_atomic"] is True, document
    assert document["leases"] == [], document
    assert "cleanup_required" not in document, document
    assert "quarantine_guidance" not in document, document
    assert isinstance(document["project_resource"], str) and document["project_resource"].strip(), document

    record = document["record"]
    assert record is None or isinstance(record, dict), document
    if isinstance(record, dict):
        for key in RECORD_KEYS_THAT_NAME_THE_MACHINE:
            assert key not in record, f"lease-status published {key}, which names the machine it ran on"


def test_a_run_whose_owner_disappears_leaves_the_board_to_the_next_caller_and_names_the_dead_one(unquarantined: None, bench: Bench, servers) -> None:
    """A crash frees the bench, and the next caller is told that is what happened.

    The exclusion is an operating system lock, so a process that dies loses it
    without anybody doing anything, and the whole question is what the record
    beside it does. A holder record still saying `held` must not be read as a
    hold, or one crash would padlock the board for good; and taking it silently
    would leave the next operator with no idea a run had died on their bench.
    Both are asserted here: the second declaration succeeds, and it says whose
    run it inherited and when that run last spoke.

    Leaves the bench free: the inheriting run is stopped in the test, the killed
    server holds nothing the operating system did not already take back, and the
    lease status is read back before the test ends. On the failing path the
    `unquarantined` fixture clears anything the killed owner left standing, so a
    half-finished crash case does not hand the next test a blocked bench.
    """
    a_free_bench(bench)
    doomed = servers()
    started = doomed.call("bench_run_start", {"devices": [probe_device(bench)], "label": "coordination-doomed"})
    assert started["ok"] is True, started
    declared = started["declared_devices"]
    dead_pid = doomed.pid
    doomed.kill()
    assert not doomed.alive, "the owner this test needs gone is still running"

    heir = servers()
    inherited = heir.call("bench_run_start", {"devices": [probe_device(bench)], "label": "coordination-heir"})
    try:
        assert inherited["ok"] is True, inherited
        assert inherited["declared_devices"] == declared, inherited
        reclaimed = inherited["reclaimed"]
        assert isinstance(reclaimed, list) and reclaimed, inherited
        for detail in reclaimed:
            assert detail["reason"] == "owner_process_exited_without_release", detail
            assert isinstance(detail["held_since"], str) and detail["held_since"].strip(), detail
            owner = detail["owner"]
            assert isinstance(owner, dict), detail
            assert owner["pid"] == dead_pid, "the reclaim names a process that is not the one that died"
            assert owner["pid"] != heir.pid, detail
            assert "exited without releasing" in detail["summary"], detail["summary"]
    finally:
        stopped = heir.call("bench_run_stop")
    assert stopped["released_devices"] == declared, stopped

    status, free = bench.document("lease-status")
    # `lease-status` exits 1 whenever something is standing on the bench, a hold or
    # an incident, because the exit status answers "is there anything standing";
    # the document is what these tests read, so the status is not asserted here.
    assert free["ok"] is True, free
    assert free["bench_held"] is False, free
    assert free["blocked"] is False, free


def test_bench_run_stop_says_a_session_the_run_left_open_still_holds_the_board(unquarantined: None, bench: Bench, servers) -> None:
    """Ending the run does not pull a board out from under a live session, and says so.

    A session outlives the run that opened it by design, so `bench_run_stop`
    cannot report a release that did not happen. The result has to name the
    leases that are still open and the devices they keep, or an agent reads
    "released" and starts the next run against a board that is still held, which
    is a collision it was told would not happen.

    Leaves the bench free: the session is stopped in the test's own teardown
    whatever the assertions do, and the release is read back from `lease-status`.
    A session whose close could not be confirmed is an incident, so the
    `unquarantined` fixture clears one behind the test if the teardown left it.
    """
    port = uart_device(bench)
    a_free_bench(bench)
    server = servers()
    started = server.call("bench_run_start", {"devices": [port], "label": "coordination-session"})
    assert started["ok"] is True, started
    declared = started["declared_devices"]

    try:
        opened = server.call("com_session_start", {"port_id": port["id"], "clear_buffer": True})
        assert opened["ok"] is True, opened

        stopped = server.call("bench_run_stop")
        assert stopped["ok"] is True, stopped
        assert stopped["run_was_active"] is True, stopped
        assert stopped["released_devices"] == declared, stopped
        open_leases = stopped["open_leases"]
        assert isinstance(open_leases, list) and open_leases, stopped
        assert all(isinstance(lease, str) and lease.strip() for lease in open_leases), stopped
        assert stopped["still_held_devices"], stopped
        assert "lease(s) are still open" in stopped["summary"], stopped["summary"]
    finally:
        # Suppressed rather than asserted: the fixture's teardown closes this
        # server's stdin, which is what runs its own session cleanup, so the
        # bench is given back either way and a fault here must not replace the
        # assertion that actually failed. The lease status below is what says
        # the port really came back.
        with suppress(*TEARDOWN_FAULTS):
            server.call("com_session_stop", {"port_id": port["id"]})
        with suppress(*TEARDOWN_FAULTS):
            server.call("bench_run_stop")

    status, free = bench.document("lease-status")
    # `lease-status` exits 1 whenever something is standing on the bench, a hold or
    # an incident, because the exit status answers "is there anything standing";
    # the document is what these tests read, so the status is not asserted here.
    assert free["ok"] is True, free
    assert free["bench_held"] is False, free
    assert free["blocked"] is False, free


def test_a_session_a_dead_owner_left_is_an_incident_the_next_caller_reads_and_the_next_contact_clears(unquarantined: None, bench: Bench, servers) -> None:
    """The incident a crash leaves, who is told about it, and what actually ends it.

    A session lease that outlived its owner is the case where the bench cannot
    say by itself what happened, so the next reader inherits an incident: named,
    with an identifier, with the reasons it is held for and the guidance that
    goes with them. What it is not is a padlock. Since the quarantine narrowed to
    the audit halt, `agentic-hil recover` answers this class with
    `nothing_to_recover` rather than signing for a board nobody looked at, and
    the incident ends at the next contact with the hardware. Both halves are
    asserted, and against each other: whatever `lease-status` says about whether
    the incident stands is what `recover` has to do about it.

    This test can leave the bench quarantined, and clears the quarantine itself:
    it runs `agentic-hil recover --confirm-safe-state` with the identifier read
    out of `lease-status`, then makes the contact that settles the class, then
    resets the board back into `run` so the starter firmware is executing, and
    reads the free, unblocked bench back before it ends. On the path where one
    of those assertions fails, the same two routes are taken again by the
    `unquarantined` fixture's teardown, so a red test still hands the bench on
    free: an incident is what this test manufactures, and clearing it is not
    something the next author's file should have to do.
    """
    port = uart_device(bench)
    a_free_bench(bench)
    doomed = servers()
    started = doomed.call("bench_run_start", {"devices": [port], "label": "coordination-doomed-session"})
    assert started["ok"] is True, started
    opened = doomed.call("com_session_start", {"port_id": port["id"], "clear_buffer": True})
    assert opened["ok"] is True, opened
    doomed.kill()
    assert not doomed.alive, "the owner this test needs gone is still running"

    status, incident = bench.document("lease-status")
    # `lease-status` exits 1 whenever something is standing on the bench, a hold or
    # an incident, because the exit status answers "is there anything standing";
    # the document is what these tests read, so the status is not asserted here.
    assert incident["ok"] is True, incident
    assert incident["blocked"] is True, incident
    assert incident["cleanup_required"] is True, incident
    quarantine_id = incident["quarantine_id"]
    assert isinstance(quarantine_id, str) and quarantine_id.strip(), incident
    assert "owner_process_exited_without_release" in incident["cleanup_reasons"], incident
    assert incident["quarantine_guidance"], incident
    stands = incident["incident_stands"]
    assert isinstance(stands, bool), incident

    # The operator's own route, with this incident's identifier and nothing
    # invented: the id comes out of the answer above.
    status, recovered = bench.document("recover", "--confirm-safe-state", "--quarantine-id", quarantine_id)
    assert status == 0, recovered
    assert recovered["ok"] is True, recovered
    assert recovered["tool"] == "hardware_recover", recovered
    if stands:
        # The audit halt: a person signed for it, so it is cleared here.
        assert recovered["was_quarantined"] is True, recovered
    else:
        # The narrowed case: nothing to sign for, and the answer says which of
        # the two this is rather than reporting a recovery that did nothing.
        assert recovered["nothing_to_recover"] is True, recovered
        assert recovered["was_quarantined"] is False, recovered
        assert "audit halt" in recovered["summary"], recovered["summary"]

    heir = servers()
    contact = heir.call("reset_target", {"mode": "run"})
    # The padlock check first, because it is the one that says which defect this
    # is: a bench refusing the very contact that would settle its incident.
    assert contact.get("error_type") != "resource_quarantined", f"an incident a crashed session left refused the contact that settles it: {contact}"
    assert contact["ok"] is True, contact
    # Back into `run` explicitly: the recovery seam may drive the target into
    # halt to establish a known state, and the bench is handed on with the
    # starter firmware executing.
    settled = heir.call("reset_target", {"mode": "run"})
    assert settled["ok"] is True, settled

    status, cleared = bench.document("lease-status")
    # `lease-status` exits 1 whenever something is standing on the bench, a hold or
    # an incident, because the exit status answers "is there anything standing";
    # the document is what these tests read, so the status is not asserted here.
    assert cleared["ok"] is True, cleared
    assert cleared["blocked"] is False, cleared
    assert cleared["incident_stands"] is False, cleared
    assert cleared["bench_held"] is False, cleared
    assert cleared["quarantine_id"] is None, cleared


def test_recover_without_the_operators_confirmation_never_reaches_the_bench(bench: Bench) -> None:
    """The signature is the point of the command, so it is not optional.

    `--confirm-safe-state` attests that a person looked at a physical board, and
    `--quarantine-id` says which incident they looked at it for. Neither can be
    defaulted: a recover that ran without them would clear an audit halt on
    nobody's word, which is the one thing the halt exists to require. The refusal
    is the parser's, before a configuration is loaded and before a coordinator
    exists, and the bench is read either side of it to show nothing moved.

    Leaves the bench free: nothing runs.
    """
    # The precondition, named before the comparison rather than inside it: the
    # two readings below are only equal on a bench nothing is holding and no
    # incident is standing, because a held device's heartbeat moves between two
    # reads. Asserted here so a bench somebody left held fails as the leftover
    # it is, instead of as this test's claim about `recover`.
    a_free_bench(bench)
    before = bench.document("lease-status")

    missing_signature = bench.run("recover", "--quarantine-id", NOT_A_LIVE_INCIDENT)
    assert missing_signature.returncode == 2, missing_signature.stdout + missing_signature.stderr
    assert "--confirm-safe-state" in missing_signature.stderr, missing_signature.stderr

    missing_incident = bench.run("recover", "--confirm-safe-state")
    assert missing_incident.returncode == 2, missing_incident.stdout + missing_incident.stderr
    assert "--quarantine-id" in missing_incident.stderr, missing_incident.stderr

    assert bench.document("lease-status") == before, "a refused recover changed this bench's lease status"


def test_recover_on_a_bench_with_nothing_standing_answers_and_leaves_it_untouched(bench: Bench) -> None:
    """The ordinary state of a bench that had a bad run, answered as ok and not as an error.

    Since an incident ends at the next contact, a bench with nothing standing is
    where an agent following the recovery advice most often arrives, and an error
    there would read as a bench that needs a person. It answers `ok` with
    `nothing_to_recover`, it says why in the sentence a caller relays, and it
    does it again identically the second time, because an agent that recovers
    twice must read one thing in both cases.

    Leaves the bench free: the call changes nothing, and the lease status is
    compared either side of it to show that.
    """
    a_free_bench(bench)
    before = bench.document("lease-status")

    first = bench.document("recover", "--confirm-safe-state", "--quarantine-id", NOT_A_LIVE_INCIDENT)
    second = bench.document("recover", "--confirm-safe-state", "--quarantine-id", NOT_A_LIVE_INCIDENT)

    for status, answered in (first, second):
        assert status == 0, answered
        assert answered["ok"] is True, answered
        assert answered["tool"] == "hardware_recover", answered
        assert answered["nothing_to_recover"] is True, answered
        assert answered["was_quarantined"] is False, answered
        assert answered["resources"] == [], answered
        assert "audit halt" in answered["summary"], answered["summary"]
    assert first == second, (first, second)

    assert bench.document("lease-status") == before, "a recover on a bench with nothing standing changed its lease status"
