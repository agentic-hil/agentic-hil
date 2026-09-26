"""The plan runner as a run: started, watched, stopped, killed and read back.

`test_bench_reactor_steps.py` drives what a plan's steps do to the board. This
file drives what happens around them, once a plan has become a run with a
handle: started detached from the caller, watched while it works, asked to stop
between two steps, killed outright, and then read back the ways a CI job reads a
run (the JSON report, the JUnit document and the evidence bundle) and the way a
project's own pytest suite does.

None of that is a question a fake can answer. A detached run is a second process
that holds the board's locks after the command that started it has exited; a
stop is a file that process reads between its steps on its own clock; a killed
worker is a process that ends with a port open and a lease on disk. Every claim
here is read off the product's own answers, through the CLI, through
`agentic-hil mcp-stdio` and through the pytest plugin, and every test leaves the
board running the demo with nothing held and no run left alive.

The demo firmware is the counterparty throughout: its banner after a reset is
what each plan waits for, so the board decides whether a step passed. Nothing
here names a host, a user, a probe serial or a device path. Those are read out
of the answers, and where the claim is that they stay out of a document they are
compared without being printed.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
import yaml
from result_text import assert_text_projects
from support import scaled_time_bound

from .conftest import BENCH_ONLY, COMMAND_TIMEOUT_S, Bench, child_command

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# How long a server may take to answer `initialize`, and how long a shutdown is
# given before the process is killed instead. The coordination tests' bounds,
# for their reason: no timeout plugin is configured in this repository, so an
# unbounded read would run the job to its ceiling instead of failing a test.
HANDSHAKE_TIMEOUT_S = 60.0
SHUTDOWN_TIMEOUT_S = scaled_time_bound(60.0)

# What a teardown swallows on its way to giving the bench back. `pytest.fail`
# raises an outcome exception that does not derive from `Exception`, so a
# teardown suppressing only `Exception` would let a dead server's diagnosis
# escape as a teardown error and bury the assertion that actually failed.
TEARDOWN_FAULTS = (Exception, pytest.fail.Exception)

# The demo's banner after a reset, read as the steps tests read it.
BANNER_PATTERN = r"Hello\s+World"
BANNER_TIMEOUT_S = 5

# A claim the demo cannot meet: it prints its banner and nothing else. Short on
# purpose, because the run that makes it is a run that is meant to fail.
NEVER_PRINTED = "this board never prints this"
UNMET_TIMEOUT_S = 3
UNMET_SUMMARY = "The COM port output never equalled the expected value before this step's timeout."

# A run handle, as the product mints one.
RUN_HANDLE = re.compile(r"^run-[0-9a-f]{16}$")

# How often a test asks a run what it is doing, and how long it gives the run to
# get somewhere. The window is generous because a slow probe is not what these
# tests measure; a run that never gets there is, and the last answer it gave is
# what the failure prints.
POLL_INTERVAL_S = 0.3
REACH_S = 120.0

# The waits a stop has to cut short. Each is far longer than the test around it
# is allowed to take, so a stop that was ignored cannot pass for one that was
# honoured: the run would still be waiting when the poll gives up.
LONG_DELAY_MS = 60_000
KILLED_DELAY_MS = 120_000
REPEAT_COUNT = 50
REPEAT_DELAY_MS = 500

# Where this file writes what a CI job would upload, relative to the project,
# which is the workspace the product is bound to.
JUNIT_DIRECTORY = Path("build") / "junit"
EVIDENCE_DIRECTORY = Path("artifacts") / "reactor-runs"

# The two states a run ends in, and the one a status derives for a run whose
# process is gone without ending it.
TERMINAL = frozenset({"finished", "stopped"})
WORKER_GONE = "worker_gone"

# Where the product sends a reader whose run lost its process. Compared whole,
# because the sentence is the whole of the recovery instructions a caller gets.
WORKER_GONE_NEXT_STEP = (
    "The bench is the dead-owner case the coordinator already handles: `agentic-hil lease-status` reads and heals it, "
    "and names a quarantine id for `agentic-hil recover --confirm-safe-state --quarantine-id <id>` if the run had reached the board."
)

# The report fields the evidence design names as identities (a probe serial, an
# executable, a lock key keyed on the physical device), and the prefixes a lock
# key carries wherever it turns up.
IDENTITY_FIELDS = frozenset({"executable", "executable_path", "probe_id", "declared_devices", "lock_key", "lock_keys", "resource", "resources"})
LOCK_KEY_PREFIXES = ("physical:", "probe:", "probe-exe:", "com:", "can:")

# The configuration fields that say which physical bench this is.
CONFIG_IDENTITY_FIELDS = frozenset({"probe_id", "device", "serial_number", "executable"})

# The project a plugin-driven suite lives in, inside this bench's project.
PLUGIN_SUITE = "plugin-suite"
PLUGIN_MODULE = "test_plan_through_the_plugin.py"
PLUGIN_TESTS = '''"""A project's own suite, running its plans through the Agentic HIL pytest plugin."""
from __future__ import annotations

from agentic_hil.report import overall_success


def run_the_plan(agentic_hil, plan: str) -> None:
    result = agentic_hil.call("test_reactor_run", {"test_config_path": plan})
    assert overall_success(result), f"{result.get('error_type')}: {result.get('summary')}"


def test_the_demo_plan(agentic_hil) -> None:
    run_the_plan(agentic_hil, "testconfig.yaml")


def test_a_claim_the_demo_cannot_meet(agentic_hil) -> None:
    run_the_plan(agentic_hil, "UNMET_PLAN")
'''

# What a pytest run inherits from the one around it and must not: options, an
# explicit plugin list, a switch that turns entry-point plugins off, and the
# outer run's own current-test marker.
OUTER_PYTEST_VARIABLES = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_CURRENT_TEST")


class McpServer:
    """One `agentic-hil mcp-stdio` process, spoken to over its own stdio.

    The coordination tests' client, kept in this file for the reason that one is
    kept in its own: several authors write into this directory at once. It frames
    JSON-RPC the way the server does (one object per line, flushed), keeps the ids
    apart, and reads with a bound so a server that stops answering fails the test
    that is waiting instead of hanging the session. `call` also holds every tool
    result to the envelope the MCP door promises: the text content is the
    structured document, and a result that is not `ok` is flagged as an error.
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
    def launch(cls, bench: Bench) -> McpServer:
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
        """The tail of what this server wrote to stderr, for a failure message."""
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
        assert_text_projects(answered["result"])
        if structured.get("ok") is not True:
            assert answered["result"].get("isError") is True, f"tools/call {name} answered a result that is not ok without flagging it as an error: {answered}"
        return structured

    def shut_down(self) -> None:
        """Give the bench back and end the process, whatever the test did to it.

        Best effort by construction and in this order: close any open run first so
        the devices are released by the product rather than by the operating
        system, then close stdin, which ends the server's read loop and runs its
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
        # Guarded as well as the `finally`: a teardown closes this stream while
        # the thread is sitting in it, and an unguarded reader would print a
        # traceback from a daemon thread over the report of what actually failed.
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


def write_plan(bench: Bench, name: str, version: int, steps: list[dict[str, Any]]) -> str:
    """A plan written into the project under its own name; the path a command is given."""
    plan = bench.project / f"{name}.yaml"
    plan.write_text(yaml.safe_dump({"version": version, "name": name, "steps": steps}, sort_keys=False), encoding="utf-8")
    return plan.name


def banner_plan(bench: Bench, name: str) -> str:
    """The shortest plan the board can pass: open the port, reset, read the banner, close."""
    port = bench.com_port_name()
    return write_plan(
        bench,
        name,
        3,
        [
            {"device": port, "action": "uart_open", "clear_buffer": True},
            {"device": bench.debugger_name(), "action": "reset", "mode": "run"},
            {"device": port, "action": "uart_read", "comparator": {"pattern": BANNER_PATTERN}, "timeout_s": BANNER_TIMEOUT_S},
            {"device": port, "action": "uart_close"},
        ],
    )


def a_free_bench(bench: Bench) -> dict:
    """The lease status of a bench nothing holds and nothing blocks, or a failure saying otherwise.

    Read before a run, so a leftover fails as what it is rather than as this
    file's claim, and after one, where it is the claim: a run that ended, however
    it ended short of a killed process, leaves no lock, no lease and no incident.
    `lease-status` exits 1 whenever anything stands on the bench, so the document
    is read and the exit status is not.
    """
    _, document = bench.document("lease-status")
    assert document["ok"] is True, document
    assert document["owner_active"] is False, f"a session lease is still open on this project: {document}"
    assert document["bench_held"] is False, f"a device is still held: {document}"
    assert document["held_devices"] == [], document
    assert document["blocked"] is False, f"an incident blocks this project: {document}"
    assert document["incident_stands"] is False, document
    return document


def leave_the_board_running(bench: Bench) -> None:
    """Clear whatever a test left standing, then reset the board into `run` through a plan.

    A stopped run leaves nothing to clear and a failed one leaves the board
    halted by its own recovery; a killed worker can leave an incident, which the
    operator's `recover` answers for the class that owes a signature and the
    next contact ends for the class that does not. The reset plan is that
    contact, and the demo's banner is running again after it.
    """
    _, status = bench.document("lease-status")
    if status.get("blocked") or status.get("incident_stands"):
        quarantine_id = status.get("quarantine_id")
        assert isinstance(quarantine_id, str) and quarantine_id, f"the bench is not free and names no quarantine id to clear: {status.get('cleanup_reasons')}"
        _, recovered = bench.document("recover", "--confirm-safe-state", "--quarantine-id", quarantine_id)
        assert recovered.get("ok") is True, recovered
    plan = write_plan(bench, "reactor-runs-leave-the-board-running", 3, [{"device": bench.debugger_name(), "action": "reset", "mode": "run"}])
    status_code, report = bench.document("test-reactor", "--test-config", plan)
    assert status_code == 0, report


def poll(read: Callable[[], dict], until: Callable[[dict], bool], what: str, deadline_s: float = REACH_S) -> dict:
    """Ask until the answer says `what` happened, and fail with the last answer if it never does."""
    deadline = time.monotonic() + deadline_s
    answer = read()
    while not until(answer):
        if time.monotonic() >= deadline:
            pytest.fail(f"{what} did not happen within {deadline_s:.0f}s; the last answer was {answer}", pytrace=False)
        time.sleep(POLL_INTERVAL_S)
        answer = read()
    return answer


def cli_status(bench: Bench, handle: str) -> dict:
    """`test-reactor-status --run`, as the document it prints."""
    _, answer = bench.document("test-reactor-status", "--run", handle)
    return answer


def cli_handles(bench: Bench) -> list[str]:
    """Every run this bench has a record of, newest first, by handle."""
    _, listing = bench.document("test-reactor-status")
    assert listing["ok"] is True, listing
    return [entry["run"] for entry in listing["runs"]]


def gone_or_ended(answer: dict) -> bool:
    return answer.get("state") in TERMINAL or answer.get("state") == WORKER_GONE


def publishes_no_host_fields(answer: dict) -> None:
    """A status or stop answer is the run's record less the fields that name this host."""
    assert "pid" not in answer, f"the answer for {answer.get('run')} published the worker's process id"
    assert "version" not in answer, f"the answer for {answer.get('run')} published the record's internal version"


def own_report(bench: Bench, ended: dict) -> dict:
    """The run's own report, where its terminal status says it is kept."""
    canonical = ended.get("canonical_report_path")
    assert isinstance(canonical, str) and canonical, ended
    path = Path(canonical)
    assert path.is_file(), "the report a terminal status names does not exist"
    assert path.resolve().is_relative_to(bench.state_root.resolve()), "the run's own report is not under the state root"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def nothing_left_running(bench: Bench, firmware: Path) -> Iterator[None]:
    """Every run a test started has ended, and the board runs the demo, whatever the test did.

    Requests `firmware` so the demo is on the board before the first plan waits
    for its banner. The teardown is for the failing path: a test that stopped
    asserting halfway through a detached run would otherwise leave a worker
    holding the bench for as long as its plan says, so every run the bench still
    lists as active is asked to stop and awaited. Then the board is handed on
    running, which after a stopped or failed run it may not be.
    """
    yield
    with suppress(*TEARDOWN_FAULTS):
        _, listing = bench.document("test-reactor-status")
        active = [handle for handle in listing.get("active_runs") or [] if isinstance(handle, str)]
        for handle in active:
            bench.run("test-reactor-stop", "--run", handle)
        for handle in active:
            poll(lambda handle=handle: cli_status(bench, handle), gone_or_ended, "a run this test left behind ending on its stop")
    leave_the_board_running(bench)


def test_a_run_started_over_mcp_is_watched_and_stopped_between_two_steps_and_the_bench_is_free_after_it(bench: Bench) -> None:
    """The MCP door: `test_reactor_run` detached, `test_reactor_status`, `test_reactor_stop`.

    A plan that resets the board and reads its banner fifty times in a `repeat`
    block is started detached, watched until it is on its second iteration, and
    asked to stop. The stop is cooperative: the run finishes the step it is in,
    ends the block at the next step boundary, closes the port it opened and
    writes its report. Every answer on the way is compared to what the spec
    says it is, the handle the start printed is the one every later answer is
    about, and the report the terminal status names is the run's own copy under
    the state root.

    Then the claim that makes a stop worth having: nothing is left behind. No
    lock, no lease, no incident, and a following plan on the same server runs
    green, finishes under a handle of its own, and is listed beside the stopped
    one, newest first, with neither of them active.
    """
    debugger = bench.debugger_name()
    port = bench.com_port_name()
    name = "reactor-runs-mcp-repeat"
    plan = write_plan(
        bench,
        name,
        4,
        [
            {"device": port, "action": "uart_open", "clear_buffer": True},
            {
                "action": "repeat",
                "count": REPEAT_COUNT,
                "steps": [
                    {"device": debugger, "action": "reset", "mode": "run"},
                    {"device": port, "action": "uart_read", "comparator": {"pattern": BANNER_PATTERN}, "timeout_s": BANNER_TIMEOUT_S},
                    {"device": port, "action": "delay", "duration_ms": REPEAT_DELAY_MS},
                ],
            },
            {"device": port, "action": "uart_close"},
        ],
    )
    a_free_bench(bench)
    server = McpServer.launch(bench)
    try:
        started = server.call("test_reactor_run", {"test_config_path": plan, "detach": True})
        assert started["ok"] is True, started
        assert started["tool"] == "test_reactor_start", started
        handle = started["run"]
        assert isinstance(handle, str) and RUN_HANDLE.match(handle), started
        # A start answers once the run holds its devices, so a printed handle is
        # a handle with the bench.
        assert started["state"] == "running", started
        assert started["detached"] is True, started
        assert started["name"] == name, started
        assert started["test_config_path"] == plan, started
        assert isinstance(started["report_path"], str) and started["report_path"], started
        assert isinstance(started["started_at"], str) and started["started_at"], started
        assert started["summary"] == (
            f"The run is detached under handle {handle}; ask `agentic-hil test-reactor-status --run {handle}` what it is doing "
            f"and `agentic-hil test-reactor-stop --run {handle}` to end it early."
        ), started["summary"]

        def status() -> dict:
            return server.call("test_reactor_status", {"run": handle})

        working = poll(
            status,
            lambda answer: answer.get("state") != "running" or (answer.get("progress") or {}).get("iteration", 0) >= 2,
            "the run reaching the second iteration of its repeat block",
        )
        assert working["state"] == "running", working
        assert working["ok"] is True, working
        assert working["tool"] == "test_reactor_status", working
        assert working["run"] == handle, working
        assert working["detached"] is True, working
        assert working["stop_requested_at"] is None, working
        publishes_no_host_fields(working)
        progress = working["progress"]
        assert (progress["step"], progress["action"], progress["route"]) == (2, "repeat", "-"), progress
        assert working["summary"] == f"This run is on step 2 (repeat), iteration {progress['iteration']}.", working["summary"]

        stopped = server.call("test_reactor_stop", {"run": handle})
        assert stopped["ok"] is True, stopped
        assert stopped["tool"] == "test_reactor_stop", stopped
        assert stopped["run"] == handle, stopped
        assert stopped["state"] == "running", stopped
        assert stopped["stop_requested"] is True, stopped
        requested_at = stopped["stop_requested_at"]
        assert isinstance(requested_at, str) and requested_at, stopped
        assert stopped["summary"] == "A stop was requested; the run finishes the step it is in, closes its devices in the usual order and writes its report.", stopped["summary"]
        publishes_no_host_fields(stopped)

        # Whichever side of the run's end this lands on, it says so: still
        # running with the request acknowledged, or already stopped.
        pending = status()
        if pending["state"] == "running":
            assert pending["stop_requested_at"] == requested_at, pending
            assert pending["summary"].endswith(" A stop has been requested; it ends after the step it is in."), pending["summary"]

        ended = poll(status, lambda answer: answer.get("state") != "running", "the stopped run ending")
        assert ended["state"] == "stopped", ended
        assert ended["ok"] is True, ended
        assert ended["run"] == handle, ended
        assert ended["run_ok"] is False, ended
        assert ended["error_type"] == "run_stopped", ended
        assert ended["failed_step"] is None, ended
        assert ended["stopped_after_step"] == 2, ended
        assert ended["stop_requested_at"] == requested_at, ended
        publishes_no_host_fields(ended)
        report = own_report(bench, ended)
        canonical = ended["canonical_report_path"]
        assert ended["summary"] == f"This run was stopped on request and did not pass (run_stopped); its report is at {canonical}.", ended["summary"]

        # A stopped run is neither a pass nor a failure: `ok: false` with
        # `run_stopped`, no failed step, no recovery, and the records of every
        # step that did run.
        assert report["run"] == handle, report
        assert report["ok"] is False, report
        assert report["stopped"] is True, report
        assert report["stopped_after_step"] == 2, report
        assert report["error_type"] == "run_stopped", report
        assert "failed_step" not in report, report
        assert "recovery" not in report, report
        assert report["summary"].startswith("Test reactor sequence was stopped on request after step 2; the devices it opened were closed and this report was written."), report["summary"]
        assert [record["action"] for record in report["steps"]] == ["uart_open", "repeat"], report["steps"]
        assert report["steps"][0]["result"]["ok"] is True, report["steps"][0]

        block = report["steps"][1]
        repeated = block["result"]
        iterations = block["iterations"]
        assert repeated["error_type"] == "run_stopped", repeated
        assert repeated["exit_reason"] == "stopped", repeated
        assert repeated["count"] == REPEAT_COUNT, repeated
        assert repeated["iterations_run"] == len(iterations), (repeated, len(iterations))
        assert 2 <= len(iterations) < REPEAT_COUNT, (repeated, len(iterations))
        assert repeated["summary"] == f"A stop was requested on iteration {len(iterations)} of this repeat block.", repeated
        last = iterations[-1]["steps"]
        assert repeated["stopped_after_nested_step"] == len(last), (repeated, last)
        for iteration in iterations[:-1]:
            assert [nested["action"] for nested in iteration["steps"]] == ["reset", "uart_read", "delay"], iteration
        for iteration in iterations:
            for nested in iteration["steps"]:
                assert nested["result"]["ok"] is True, nested
        # Only the step the stop landed in may have been cut short, and only if
        # it was the wait.
        earlier = [nested for iteration in iterations for nested in iteration["steps"]][: -1 if last else None]
        assert all("stop_requested" not in nested["result"] for nested in earlier), earlier

        # The port the plan opened and never reached its own close for.
        assert [(entry.get("port_id"), entry["action"]) for entry in report["cleanup"]] == [(port, "uart_close")], report["cleanup"]
        assert report["cleanup"][0]["result"]["ok"] is True, report["cleanup"]
        assert report["cleanup_ok"] is True, report

        a_free_bench(bench)
        after = server.call("test_reactor_run", {"test_config_path": banner_plan(bench, "reactor-runs-mcp-banner")})
        assert after["ok"] is True, after
        following = after["run"]
        assert isinstance(following, str) and RUN_HANDLE.match(following) and following != handle, after
        finished = server.call("test_reactor_status", {"run": following})
        assert finished["state"] == "finished", finished
        assert finished["run_ok"] is True, finished
        assert finished["detached"] is False, finished
        assert finished["summary"] == f"This run ended and passed; its report is at {after['canonical_report_path']}.", finished["summary"]

        listing = server.call("test_reactor_status", {})
        assert listing["ok"] is True, listing
        handles = [entry["run"] for entry in listing["runs"]]
        assert handle in handles and following in handles, listing
        assert handles.index(following) < handles.index(handle), f"the listing is not newest first: {handles}"
        states = {entry["run"]: entry["state"] for entry in listing["runs"]}
        assert (states[handle], states[following]) == ("stopped", "finished"), states
        assert handle not in listing["active_runs"] and following not in listing["active_runs"], listing
        assert listing["summary"] == f"This bench has records of {len(handles)} test run(s); name one with --run to see what it is doing.", listing["summary"]
    finally:
        with suppress(*TEARDOWN_FAULTS):
            server.shut_down()


def test_a_plan_asked_for_while_this_session_holds_an_open_run_is_refused_before_anything_is_spawned(bench: Bench) -> None:
    """`test_reactor_run` inside an open `bench_run_start` run: refused by name, both forms.

    A plan is a run of its own and takes the devices it names up front, so a
    server whose agent already opened a run is holding what the plan would take.
    The answer is `run_already_active` with the open run's own label and the way
    out, decided before anything is spawned or locked: the listing of runs is the
    same afterwards, for the detached form as for the synchronous one. Once the
    open run is closed, the same plan on the same server is green.
    """
    label = "reactor-runs-open-run"
    plan = banner_plan(bench, "reactor-runs-inside-an-open-run")
    a_free_bench(bench)
    server = McpServer.launch(bench)
    try:
        before = server.call("test_reactor_status", {})
        assert before["ok"] is True, before
        opened = server.call("bench_run_start", {"devices": [{"kind": "debugger", "id": bench.debugger_name()}], "label": label})
        assert opened["ok"] is True, opened
        for detach in (True, False):
            refused = server.call("test_reactor_run", {"test_config_path": plan, "detach": detach})
            assert refused["ok"] is False, refused
            assert refused["tool"] == "test_reactor_run", refused
            assert refused["error_type"] == "run_already_active", refused
            assert refused["summary"] == (
                f"This session already holds an open run through `bench_run_start` ({label}), and a test plan is a run of its own "
                "that takes the devices it names up front; end the open run with `bench_run_stop`, then run the plan."
            ), refused["summary"]
            assert refused["run_label"] == label, refused
            assert refused["declared_devices"] == opened["declared_devices"], refused
            assert isinstance(refused["run_started_at"], str) and refused["run_started_at"], refused
            assert refused["next_step"] == "Call `bench_run_stop`, then `test_reactor_run` again; the plan declares and holds every device it names for itself.", refused
            assert refused["retry_safe"] is False, refused
            assert refused["side_effect_committed"] is False, refused
            assert refused["side_effect_status"] == "not_started", refused
            assert refused["hardware_state"] == "unchanged", refused
        after = server.call("test_reactor_status", {})
        assert [entry["run"] for entry in after["runs"]] == [entry["run"] for entry in before["runs"]], "a refused plan left a run record behind"

        closed = server.call("bench_run_stop")
        assert closed["ok"] is True, closed
        ran = server.call("test_reactor_run", {"test_config_path": plan})
        assert ran["ok"] is True, ran
    finally:
        with suppress(*TEARDOWN_FAULTS):
            server.shut_down()
    a_free_bench(bench)


def test_a_run_detached_from_the_command_line_is_stopped_inside_a_long_delay_and_its_report_says_how_long_it_waited(bench: Bench) -> None:
    """`test-reactor --detach`, `test-reactor-status`, `test-reactor-stop`, with the stop inside a step.

    The plan reads the banner and then waits a minute on the port. The status is
    watched until the run is on that wait, and the stop is asked for there, which
    is the case a stop between steps would not reach: the wait is read in slices
    for exactly this, so it ends early, says so in its own result, and the run
    ends after it as stopped. A minute is longer than the test waits for the end,
    so a wait that ignored the stop fails here rather than passing slowly.

    A second stop on the ended run is answered as the no-op it is, the listing
    shows the run stopped and not active, and the bench is free and runs a
    following plan green.
    """
    port = bench.com_port_name()
    name = "reactor-runs-cli-delay"
    plan = write_plan(
        bench,
        name,
        3,
        [
            {"device": port, "action": "uart_open", "clear_buffer": True},
            {"device": bench.debugger_name(), "action": "reset", "mode": "run"},
            {"device": port, "action": "uart_read", "comparator": {"pattern": BANNER_PATTERN}, "timeout_s": BANNER_TIMEOUT_S},
            {"device": port, "action": "delay", "duration_ms": LONG_DELAY_MS},
            {"device": port, "action": "uart_close"},
        ],
    )
    a_free_bench(bench)
    status, started = bench.document("test-reactor", "--test-config", plan, "--detach")
    assert status == 0, started
    assert started["ok"] is True, started
    assert started["tool"] == "test_reactor_start", started
    handle = started["run"]
    assert isinstance(handle, str) and RUN_HANDLE.match(handle), started
    assert started["state"] == "running", started
    assert started["detached"] is True, started
    assert started["name"] == name, started
    assert started["test_config_path"] == plan, started

    waiting = poll(
        lambda: cli_status(bench, handle),
        lambda answer: answer.get("state") != "running" or (answer.get("progress") or {}).get("step") == 4,
        "the run reaching its long delay",
    )
    assert waiting["state"] == "running", waiting
    assert waiting["progress"] == {"step": 4, "action": "delay", "route": port}, waiting["progress"]
    assert waiting["summary"] == "This run is on step 4 (delay).", waiting["summary"]
    assert waiting["stop_requested_at"] is None, waiting
    publishes_no_host_fields(waiting)

    status, stopped = bench.document("test-reactor-stop", "--run", handle)
    assert status == 0, stopped
    assert stopped["ok"] is True, stopped
    assert stopped["tool"] == "test_reactor_stop", stopped
    assert stopped["run"] == handle, stopped
    assert stopped["state"] == "running", stopped
    assert stopped["stop_requested"] is True, stopped
    assert stopped["summary"] == "A stop was requested; the run finishes the step it is in, closes its devices in the usual order and writes its report.", stopped["summary"]

    ended = poll(lambda: cli_status(bench, handle), lambda answer: answer.get("state") != "running", "the run ending on the stop inside its delay")
    assert ended["state"] == "stopped", ended
    assert ended["run_ok"] is False, ended
    assert ended["error_type"] == "run_stopped", ended
    assert ended["failed_step"] is None, ended
    assert ended["stopped_after_step"] == 4, ended
    assert ended["stop_requested_at"] == stopped["stop_requested_at"], ended
    report = own_report(bench, ended)
    assert ended["summary"] == f"This run was stopped on request and did not pass (run_stopped); its report is at {ended['canonical_report_path']}.", ended["summary"]

    assert report["run"] == handle, report
    assert report["stopped"] is True, report
    assert report["stopped_after_step"] == 4, report
    assert report["error_type"] == "run_stopped", report
    assert "failed_step" not in report, report
    assert "recovery" not in report, report
    records = report["steps"]
    assert [record["action"] for record in records] == ["uart_open", "reset", "uart_read", "delay"], records
    for record in records:
        assert record["result"]["ok"] is True, record
    waited = records[3]["result"]
    assert waited["summary"] == "Test plan waited until a stop was requested.", waited
    assert waited["stop_requested"] is True, waited
    assert waited["duration_ms"] == LONG_DELAY_MS, waited
    assert waited["port_id"] == port, waited
    assert isinstance(waited["waited_ms"], (int, float)) and 0 <= waited["waited_ms"] < LONG_DELAY_MS, waited
    assert records[3]["elapsed_ms"] < LONG_DELAY_MS, records[3]
    assert [(entry.get("port_id"), entry["action"]) for entry in report["cleanup"]] == [(port, "uart_close")], report["cleanup"]
    assert report["cleanup"][0]["result"]["ok"] is True, report["cleanup"]
    assert report["cleanup_ok"] is True, report

    status, again = bench.document("test-reactor-stop", "--run", handle)
    assert status == 0, again
    assert again["ok"] is True, again
    assert again["state"] == "stopped", again
    assert again["stop_requested"] is False, again
    assert again["summary"] == "This run had already ended, so nothing was asked of it.", again["summary"]

    _, listing = bench.document("test-reactor-status")
    entries = {entry["run"]: entry for entry in listing["runs"]}
    assert entries[handle]["state"] == "stopped", entries[handle]
    assert entries[handle]["name"] == name, entries[handle]
    assert handle not in listing["active_runs"], listing

    a_free_bench(bench)
    status, green = bench.document("test-reactor", "--test-config", banner_plan(bench, "reactor-runs-cli-banner"))
    assert status == 0, green
    assert green["ok"] is True, green


def the_worker_holding_the_bench(bench: Bench, handle: str) -> int:
    """The process id of the detached worker running `handle`, confirmed before anything is done to it.

    Read from the product: the bench's holds name the process holding each
    device, and a declared run holds every device from one process. That process
    is then confirmed to be this run's worker by its own command line, which the
    start command gave the handle to, so the only process this file ever signals
    is one the product started for this run.
    """
    _, status = bench.document("lease-status")
    holders = {
        hold["holder"]["pid"]
        for hold in status.get("device_holds") or []
        if isinstance(hold.get("holder"), dict) and isinstance(hold["holder"].get("pid"), int)
    }
    assert len(holders) == 1, f"a detached run holds its devices from one process, and the bench named {len(holders)} holder(s)"
    pid = holders.pop()
    arguments = [part.decode("utf-8", "replace") for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]
    assert "--run-handle" in arguments, "the process holding the bench is not a detached run's worker"
    assert arguments[arguments.index("--run-handle") + 1] == handle, "the process holding the bench is another run's worker"
    return pid


def test_a_worker_killed_mid_run_is_named_gone_refuses_a_stop_and_leaves_a_bench_the_documented_route_clears(bench: Bench) -> None:
    """Fault injection: the detached worker is killed inside a step, with its port open.

    This is the case a cooperative stop exists to replace, and the spec says what
    is left: the status names the run `worker_gone` rather than guessing what it
    reached, a stop is refused because nobody is left to honour it, the operating
    system has dropped the device locks, and `lease-status` reads and heals the
    rest. A dead owner that provably made no contact is released and says so;
    one that had reached the board, as this one had, is an incident with a
    quarantine id, which the operator's `recover` answers as the spec says for
    its class and the next contact with the board ends. The bench is then free,
    and a following plan runs green.

    The only process signalled is the worker the product started for this run,
    confirmed by its own command line first.
    """
    if not hasattr(signal, "SIGKILL") or not Path("/proc").is_dir():
        pytest.skip("killing the worker needs SIGKILL and /proc to confirm which process it is")
    port = bench.com_port_name()
    name = "reactor-runs-killed-worker"
    plan = write_plan(
        bench,
        name,
        3,
        [
            {"device": port, "action": "uart_open", "clear_buffer": True},
            {"device": bench.debugger_name(), "action": "reset", "mode": "run"},
            {"device": port, "action": "uart_read", "comparator": {"pattern": BANNER_PATTERN}, "timeout_s": BANNER_TIMEOUT_S},
            {"device": port, "action": "delay", "duration_ms": KILLED_DELAY_MS},
            {"device": port, "action": "uart_close"},
        ],
    )
    a_free_bench(bench)
    status, started = bench.document("test-reactor", "--test-config", plan, "--detach")
    assert status == 0, started
    handle = started["run"]
    assert isinstance(handle, str) and RUN_HANDLE.match(handle), started
    assert started["state"] == "running", started
    waiting = poll(
        lambda: cli_status(bench, handle),
        lambda answer: answer.get("state") != "running" or (answer.get("progress") or {}).get("step") == 4,
        "the run reaching its long delay",
    )
    assert waiting["state"] == "running", waiting

    os.kill(the_worker_holding_the_bench(bench, handle), signal.SIGKILL)

    gone = poll(lambda: cli_status(bench, handle), lambda answer: answer.get("state") != "running", "the killed worker being noticed")
    assert gone["state"] == WORKER_GONE, gone
    assert gone["ok"] is True, gone
    assert gone["tool"] == "test_reactor_status", gone
    assert gone["run"] == handle, gone
    assert gone["name"] == name, gone
    assert gone["stop_requested_at"] is None, gone
    assert gone["summary"] == "The process that was running this plan is gone, so this run has no orderly end and no report of its own.", gone["summary"]
    assert gone["next_step"] == WORKER_GONE_NEXT_STEP, gone["next_step"]
    publishes_no_host_fields(gone)

    status, refused = bench.document("test-reactor-stop", "--run", handle)
    assert status == 1, refused
    assert refused["ok"] is False, refused
    assert refused["tool"] == "test_reactor_stop", refused
    assert refused["run"] == handle, refused
    assert refused["error_type"] == "run_worker_gone", refused
    assert refused["state"] == WORKER_GONE, refused
    assert refused["summary"] == "The process that was running this plan is gone, so there is nobody left to honour a cooperative stop.", refused["summary"]
    assert refused["next_step"] == WORKER_GONE_NEXT_STEP, refused["next_step"]
    assert refused["retry_safe"] is False, refused
    assert refused["side_effect_committed"] is False, refused
    publishes_no_host_fields(refused)

    _, listing = bench.document("test-reactor-status")
    entries = {entry["run"]: entry for entry in listing["runs"]}
    assert entries[handle]["state"] == WORKER_GONE, entries[handle]
    assert handle not in listing["active_runs"], listing

    _, incident = bench.document("lease-status")
    assert incident["ok"] is True, incident
    # The device locks are operating system locks, and the process is gone.
    assert incident["bench_held"] is False, incident
    # The run had reset the board and read its port before it was killed, so
    # it is not a dead owner that provably made no contact: the bench names a
    # quarantine id for `recover`, as the status's next step says it does.
    assert "released_dead_owner" not in incident, incident
    assert incident["blocked"] is True, incident
    assert incident["cleanup_required"] is True, incident
    quarantine_id = incident["quarantine_id"]
    assert isinstance(quarantine_id, str) and quarantine_id.strip(), incident
    assert "owner_process_exited_without_release" in incident["cleanup_reasons"], incident
    assert incident["quarantine_guidance"], incident
    stands = incident["incident_stands"]
    assert isinstance(stands, bool), incident
    status, recovered = bench.document("recover", "--confirm-safe-state", "--quarantine-id", quarantine_id)
    assert status == 0, recovered
    assert recovered["ok"] is True, recovered
    assert recovered["tool"] == "hardware_recover", recovered
    if stands:
        assert recovered["was_quarantined"] is True, recovered
    else:
        assert recovered["nothing_to_recover"] is True, recovered
        assert recovered["was_quarantined"] is False, recovered
        assert "audit halt" in recovered["summary"], recovered["summary"]

    heir = McpServer.launch(bench)
    try:
        contact = heir.call("reset_target", {"mode": "run"})
        assert contact.get("error_type") != "resource_quarantined", f"the incident a killed worker left refused the contact that settles it: {contact}"
        assert contact["ok"] is True, contact
        # Back into `run` explicitly: the recovery seam may drive the target
        # into halt to establish a known state.
        settled = heir.call("reset_target", {"mode": "run"})
        assert settled["ok"] is True, settled
    finally:
        with suppress(*TEARDOWN_FAULTS):
            heir.shut_down()

    cleared = a_free_bench(bench)
    assert cleared["quarantine_id"] is None, cleared
    status, green = bench.document("test-reactor", "--test-config", banner_plan(bench, "reactor-runs-after-the-kill"))
    assert status == 0, green
    assert green["ok"] is True, green


@pytest.fixture(scope="module")
def red_run(bench: Bench, firmware: Path) -> dict[str, Any]:
    """One run the board fails, with the JUnit document it was asked for.

    Shared by the two tests that read a red run back, so the JUnit file and the
    evidence bundle they check are two views of one run. The claim is unmeetable:
    the port is read for text the demo never prints, so step 3 fails on the
    board's own output, steps 4 and 5 never run, and the run's recovery resets
    the board into halt, which the per-test teardown undoes.
    """
    port = bench.com_port_name()
    plan = write_plan(
        bench,
        "reactor-runs-unmet-claim",
        3,
        [
            {"device": port, "action": "uart_open", "clear_buffer": True},
            {"device": bench.debugger_name(), "action": "reset", "mode": "run"},
            {"device": port, "action": "uart_read", "comparator": {"equals": NEVER_PRINTED}, "timeout_s": UNMET_TIMEOUT_S},
            {"device": port, "action": "delay", "duration_ms": 100},
            {"device": port, "action": "uart_close"},
        ],
    )
    junit = JUNIT_DIRECTORY / "reactor-runs-red.xml"
    (bench.project / junit).unlink(missing_ok=True)
    answered = bench.run("test-reactor", "--test-config", plan, "--junit-xml", str(junit), "--json")
    assert answered.stdout.strip(), f"the red run printed no document (exit {answered.returncode}):\n{answered.stderr}"
    return {"plan": plan, "status": answered.returncode, "report": json.loads(answered.stdout), "junit": bench.project / junit}


def junit_suite(path: Path) -> ElementTree.Element:
    """The one `<testsuite>` a run's JUnit document holds, parsed."""
    root = ElementTree.parse(path).getroot()
    assert root.tag == "testsuites", root.tag
    suites = list(root)
    assert [suite.tag for suite in suites] == ["testsuite"], [suite.tag for suite in suites]
    return suites[0]


def measured_seconds(result: dict) -> float | None:
    """What the spec lets a case's `time` come from: the step result's own measurement, or nothing."""
    for key, divisor in (("elapsed_ms", 1000.0), ("elapsed_s", 1.0)):
        value = result.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value) / divisor
    return None


def times_are_the_measured_ones(suite: ElementTree.Element, cases: list[ElementTree.Element], records: list[dict]) -> None:
    """Each executed case carries the time its result measured and no other, and the suite their sum."""
    measured = []
    for case, record in zip(cases, records, strict=True):
        seconds = measured_seconds(record["result"])
        if seconds is None:
            assert "time" not in case.attrib, f"{case.get('name')} carries a time nothing measured: {case.attrib}"
        else:
            assert case.get("time") == f"{seconds:.3f}", (case.attrib, record["result"])
            measured.append(seconds)
    if measured:
        assert suite.get("time") == f"{sum(measured):.3f}", suite.attrib
    else:
        assert "time" not in suite.attrib, suite.attrib
    stamps = [record["result"]["started_at"] for record in records if isinstance(record["result"].get("started_at"), str) and record["result"]["started_at"]]
    if stamps:
        assert suite.get("timestamp") == min(stamps), suite.attrib
    else:
        assert "timestamp" not in suite.attrib, suite.attrib


def test_a_red_plan_leaves_a_junit_document_with_its_failure_its_skipped_steps_and_only_measured_times(bench: Bench, red_run: dict[str, Any]) -> None:
    """`--junit-xml` on a run the board failed, on one it passed, and beside `--detach`.

    The red run's document is parsed and held to the mapping the spec states:
    one suite named after the plan, one case per plan step named
    `<index>.<route>.<action>` under the plan's name, the failing step carrying
    `<failure>` with its own error type, its own summary and its whole record as
    the body, and the two steps after it `<skipped>` with the reason rather than
    passed. Times are only the ones a result measured, and the suite's are their
    sum and the earliest start the results recorded.

    Then a green run's document has every case passing, and `--detach` with
    `--junit-xml` is refused by name before anything starts: no file, no run.
    """
    debugger = bench.debugger_name()
    port = bench.com_port_name()
    report = red_run["report"]
    assert red_run["status"] == 1, report
    assert report["ok"] is False, report
    assert report["failed_step"] == 3, report
    assert report["error_type"] == "comparator_unmet", report
    records = report["steps"]
    assert [record["action"] for record in records] == ["uart_open", "reset", "uart_read"], records
    unmet = records[2]["result"]
    assert unmet["summary"] == UNMET_SUMMARY, unmet
    assert (bench.project / report["junit_xml"]).resolve() == red_run["junit"].resolve(), report["junit_xml"]

    suite = junit_suite(red_run["junit"])
    assert (suite.get("name"), suite.get("tests"), suite.get("failures"), suite.get("errors"), suite.get("skipped")) == ("reactor-runs-unmet-claim", "5", "1", "0", "2"), suite.attrib
    cases = suite.findall("testcase")
    assert [case.get("name") for case in cases] == [
        f"1.{port}.uart_open",
        f"2.{debugger}.reset",
        f"3.{port}.uart_read",
        f"4.{port}.delay",
        f"5.{port}.uart_close",
    ], [case.get("name") for case in cases]
    assert {case.get("classname") for case in cases} == {"reactor-runs-unmet-claim"}, [case.attrib for case in cases]
    assert [list(case) for case in cases[:2]] == [[], []], "a step that passed carries a child element"
    failures = list(cases[2])
    assert [child.tag for child in failures] == ["failure"], [child.tag for child in failures]
    assert failures[0].get("type") == "comparator_unmet", failures[0].attrib
    assert failures[0].get("message") == UNMET_SUMMARY, failures[0].attrib
    assert json.loads(failures[0].text or "") == records[2], "the failure's body is not the failing step's whole record"
    for case in cases[3:]:
        assert [child.tag for child in case] == ["skipped"], case.get("name")
        assert case[0].get("message") == "The run stopped at step 3; this step never ran.", case[0].attrib
        assert "time" not in case.attrib, case.attrib
    times_are_the_measured_ones(suite, cases[:3], records)

    green_junit = bench.project / JUNIT_DIRECTORY / "reactor-runs-green.xml"
    green_junit.unlink(missing_ok=True)
    green_plan = banner_plan(bench, "reactor-runs-banner-junit")
    status, green = bench.document("test-reactor", "--test-config", green_plan, "--junit-xml", str(JUNIT_DIRECTORY / "reactor-runs-green.xml"))
    assert status == 0, green
    assert green["ok"] is True, green
    assert (bench.project / green["junit_xml"]).resolve() == green_junit.resolve(), green["junit_xml"]
    suite = junit_suite(green_junit)
    assert (suite.get("name"), suite.get("tests"), suite.get("failures"), suite.get("errors"), suite.get("skipped")) == ("reactor-runs-banner-junit", "4", "0", "0", "0"), suite.attrib
    cases = suite.findall("testcase")
    assert [case.get("name") for case in cases] == [f"1.{port}.uart_open", f"2.{debugger}.reset", f"3.{port}.uart_read", f"4.{port}.uart_close"], [case.get("name") for case in cases]
    assert all(list(case) == [] for case in cases), [case.get("name") for case in cases if list(case)]
    times_are_the_measured_ones(suite, cases, green["steps"])

    refused_junit = JUNIT_DIRECTORY / "reactor-runs-detached.xml"
    (bench.project / refused_junit).unlink(missing_ok=True)
    before = cli_handles(bench)
    status, refused = bench.document("test-reactor", "--test-config", green_plan, "--detach", "--junit-xml", str(refused_junit))
    assert status == 1, refused
    assert refused == {
        "ok": False,
        "tool": "test_reactor_start",
        "error_type": "junit_xml_requires_synchronous_run",
        "summary": (
            "--junit-xml writes the report of a run this command waited for, and --detach returns before the run "
            "has one. Run the plan without --detach to get the file, or follow the detached run with "
            "test-reactor-status and read the JSON report it names."
        ),
        "junit_xml": str(refused_junit),
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "retry_safe": False,
    }, refused
    assert not (bench.project / refused_junit).exists(), "a refused detached start wrote a JUnit document"
    assert cli_handles(bench) == before, "a refused detached start left a run record behind"


def test_a_run_stopped_inside_a_repeat_block_leaves_a_junit_document_with_the_steps_it_ran_passing_and_the_rest_skipped(bench: Bench, tmp_path: Path) -> None:
    """`--junit-xml` on a synchronous run another command stops while it is inside a `repeat` block.

    The synchronous run is registered under a handle like a detached one, so it is
    found in the listing by its plan's name, watched from a second command until
    its block is on the second iteration, and stopped from a third. The run it
    was started as then answers the way a stopped run does, `ok: false` with
    `run_stopped` after step 2, and writes the document it was asked for.

    The spec maps that document: a run stopped on request keeps the steps it ran
    as passes and marks the rest `<skipped>` with the stop as the reason, because
    a stop is not a failure and nothing is invented to make it look like one. So
    the block the stop landed in, every iteration of which passed on the board,
    is a passing case, and the close it never reached is skipped.
    """
    port = bench.com_port_name()
    name = "reactor-runs-stopped-junit"
    plan = write_plan(
        bench,
        name,
        4,
        [
            {"device": port, "action": "uart_open", "clear_buffer": True},
            {
                "action": "repeat",
                "count": REPEAT_COUNT,
                "steps": [
                    {"device": bench.debugger_name(), "action": "reset", "mode": "run"},
                    {"device": port, "action": "uart_read", "comparator": {"pattern": BANNER_PATTERN}, "timeout_s": BANNER_TIMEOUT_S},
                    {"device": port, "action": "delay", "duration_ms": REPEAT_DELAY_MS},
                ],
            },
            {"device": port, "action": "uart_close"},
        ],
    )
    junit = JUNIT_DIRECTORY / "reactor-runs-stopped.xml"
    (bench.project / junit).unlink(missing_ok=True)
    a_free_bench(bench)
    before = set(cli_handles(bench))
    output = tmp_path / "stdout.json"
    errors = tmp_path / "stderr.txt"
    with output.open("w", encoding="utf-8") as stdout, errors.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            child_command("test-reactor", "--test-config", plan, "--junit-xml", str(junit), "--json"),
            cwd=str(bench.project),
            env=bench.environment(),
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
    handle: str | None = None
    try:

        def registered() -> dict:
            _, listing = bench.document("test-reactor-status")
            fresh = [entry for entry in listing.get("runs") or [] if entry.get("run") not in before and entry.get("name") == name]
            return fresh[0] if fresh else {}

        entry = poll(registered, lambda answer: bool(answer) or process.poll() is not None, "the synchronous run being registered under a handle")
        assert entry, f"the synchronous run ended (exit {process.poll()}) without a handle in the listing:\n{errors.read_text(encoding='utf-8')[-1500:]}"
        handle = entry["run"]
        assert isinstance(handle, str) and RUN_HANDLE.match(handle), entry

        working = poll(
            lambda: cli_status(bench, handle),
            lambda answer: answer.get("state") not in ("starting", "running") or (answer.get("progress") or {}).get("iteration", 0) >= 2,
            "the synchronous run reaching the second iteration of its repeat block",
        )
        assert working["state"] == "running", working
        assert working["detached"] is False, working

        status, stopped = bench.document("test-reactor-stop", "--run", handle)
        assert status == 0, stopped
        assert stopped["stop_requested"] is True, stopped
        returncode = process.wait(timeout=scaled_time_bound(REACH_S))
    finally:
        if process.poll() is None:
            if handle is not None:
                with suppress(*TEARDOWN_FAULTS):
                    bench.run("test-reactor-stop", "--run", handle)
            try:
                process.wait(timeout=SHUTDOWN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=SHUTDOWN_TIMEOUT_S)

    printed = output.read_text(encoding="utf-8")
    assert printed.strip(), f"the stopped run printed no document (exit {returncode}):\n{errors.read_text(encoding='utf-8')[-1500:]}"
    report = json.loads(printed)
    assert returncode == 1, report
    assert report["run"] == handle, report
    assert report["ok"] is False, report
    assert report["stopped"] is True, report
    assert report["stopped_after_step"] == 2, report
    assert report["error_type"] == "run_stopped", report
    assert "failed_step" not in report, report
    assert [record["action"] for record in report["steps"]] == ["uart_open", "repeat"], report["steps"]
    for iteration in report["steps"][1]["iterations"]:
        for nested in iteration["steps"]:
            assert nested["result"]["ok"] is True, nested
    assert (bench.project / report["junit_xml"]).resolve() == (bench.project / junit).resolve(), report["junit_xml"]

    suite = junit_suite(bench.project / junit)
    cases = suite.findall("testcase")
    assert [case.get("name") for case in cases] == [f"1.{port}.uart_open", "2.-.repeat", f"3.{port}.uart_close"], [case.get("name") for case in cases]
    assert [[(child.tag, child.get("type")) for child in case] for case in cases[:2]] == [[], []], "a step the stopped run ran is not a passing case"
    assert [child.tag for child in cases[2]] == ["skipped"], cases[2].get("name")
    assert cases[2][0].get("message") == "The run was stopped on request after step 2; this step never ran.", cases[2][0].attrib
    assert (suite.get("name"), suite.get("tests"), suite.get("failures"), suite.get("errors"), suite.get("skipped")) == (name, "3", "0", "0", "1"), suite.attrib

    a_free_bench(bench)


def walked(document: object, key: str = "") -> Iterator[tuple[str, object]]:
    """Every scalar in a document, with the field it sits under (a list's items under the list's field)."""
    if isinstance(document, dict):
        for name, value in document.items():
            yield from walked(value, str(name))
    elif isinstance(document, list):
        for value in document:
            yield from walked(value, key)
    else:
        yield key, document


def identities(bench: Bench, report: dict) -> dict[str, str]:
    """Every value that says which bench, which machine or which operator this was, by a label.

    Read off the report the evidence was written from and off this bench's
    configuration, plus the session's own root and the operator's home. The
    values are what the documents must not contain; the labels are all a failure
    message ever prints.
    """
    found: dict[str, str] = {}
    for key, value in walked(report):
        if not isinstance(value, str) or len(value) < 4:
            continue
        if key in IDENTITY_FIELDS:
            found.setdefault(value, f"the report's {key}")
        elif value.startswith(LOCK_KEY_PREFIXES):
            found.setdefault(value, f"a lock key under the report's {key}")
    for key, value in walked(bench.configuration()):
        if key in CONFIG_IDENTITY_FIELDS and isinstance(value, str) and len(value) >= 4:
            found.setdefault(value, f"the configuration's {key}")
    found.setdefault(str(bench.project.parent), "the session's temporary root")
    found.setdefault(str(Path.home()), "the operator's home directory")
    return found


def evidence_of(bench: Bench, report_path: str, out: Path) -> tuple[dict, dict, str]:
    """`run-evidence` over one report: its answer, its run summary and its job summary."""
    shutil.rmtree(bench.project / out, ignore_errors=True)
    # Never appended to a job page from here, whatever runner this is.
    status, answer = bench.document("run-evidence", "--report", report_path, "--out", str(out), GITHUB_STEP_SUMMARY="")
    assert status == 0, answer
    assert answer["ok"] is True, answer
    assert answer["tool"] == "run_evidence", answer
    assert "step_summary_path" not in answer, answer
    # A report this workspace's own run wrote names only logs that are here.
    assert "logs_missing" not in answer, answer
    assert "logs_outside_workspace" not in answer, answer
    logs = bench.project / out / "logs"
    assert answer["logs_copied"], answer
    for copied in answer["logs_copied"]:
        assert (logs / copied).is_file(), copied
    summary = json.loads((bench.project / out / "run-summary.json").read_text(encoding="utf-8"))
    job = (bench.project / out / "job-summary.md").read_text(encoding="utf-8")
    assert summary["outcome"] == answer["outcome"], (summary, answer)
    return answer, summary, job


def step_rows(report: dict) -> list[str]:
    """The job summary's step table as the report says it must read: one row per record, its own time."""
    return [
        f"| {record['index']} | {record['route']} | {record['action']} | {'pass' if record['result'].get('ok') is True else 'fail'} | {record['elapsed_ms']} |"
        for record in report["steps"]
    ]


def table_rows(job: str) -> list[str]:
    lines = job.splitlines()
    header = lines.index("| # | Route | Action | Result | Elapsed (ms) |")
    assert lines[header + 1] == "| --- | --- | --- | --- | --- |", lines[header + 1]
    rows = []
    for line in lines[header + 2 :]:
        if not line.startswith("| "):
            break
        rows.append(line)
    return rows


def test_run_evidence_reads_a_red_run_and_a_detached_run_back_without_naming_the_bench(bench: Bench, red_run: dict[str, Any]) -> None:
    """`run-evidence` over the red run's own report and over a detached run's.

    The red bundle says `failure`, names the step that failed with its error
    type and the board's own sentence, tabulates every step that ran with the
    time the reactor measured for it, groups the devices the plan addressed by
    kind under their logical names, and records the recovery the failed run
    attempted. The plan is named by its path in the workspace and by the digest
    of the bytes that ran.

    The detached run is started with `--detach`, followed to its end through
    `test-reactor-status`, and its evidence is written from the report that
    status names: `success`, every row a pass, no failure and no recovery
    section invented for it.

    Neither bundle's two documents contains anything that says which bench this
    was: no value the report holds under an identity or lock-key field, no
    configured probe or device, no path under the session's root or the
    operator's home.
    """
    debugger = bench.debugger_name()
    port = bench.com_port_name()
    plan = red_run["plan"]
    red = red_run["report"]
    canonical = red.get("canonical_report_path")
    assert isinstance(canonical, str) and Path(canonical).is_file(), red

    answer, summary, job = evidence_of(bench, canonical, EVIDENCE_DIRECTORY / "evidence-red")
    assert answer["outcome"] == "failure", answer
    digest = hashlib.sha256((bench.project / plan).read_bytes()).hexdigest()
    assert summary["plan"] == {"name": "reactor-runs-unmet-claim", "path": plan, "sha256": digest}, summary["plan"]
    assert isinstance(summary["tools"]["agentic_hil"], str) and isinstance(summary["tools"]["python"], str), summary["tools"]
    assert summary["bench"]["devices"] == {"debuggers": [debugger], "com_ports": [port]}, summary["bench"]
    run = summary["run"]
    assert run["failed_step"] == 3, run
    assert run["error_type"] == "comparator_unmet", run
    assert run["cleanup_ok"] is True, run
    assert run["recovery"]["attempted"] is True, run
    assert run["recovery"]["outcome"] == red["recovery"]["outcome"], (run, red["recovery"])

    lines = job.splitlines()
    assert job.startswith("## Agentic HIL: reactor-runs-unmet-claim\n\n**Outcome:** failure\n\n"), job
    assert f"Plan: `{plan}` (sha256 `{digest}`)" in lines, job
    assert table_rows(job) == step_rows(red), job
    failed = lines.index("### Step 3 failed: `comparator_unmet`")
    assert lines[failed + 1 : failed + 3] == ["", UNMET_SUMMARY], lines[failed : failed + 3]
    assert "### Bench" in lines, job
    assert f"| Debuggers | `{debugger}` |" in lines and f"| COM ports | `{port}` |" in lines, job
    assert "### Recovery" in lines and "- attempted: yes" in lines, job
    assert f"- outcome: `{red['recovery']['outcome']}`" in lines, job

    name = "reactor-runs-banner-detached"
    status, started = bench.document("test-reactor", "--test-config", banner_plan(bench, name), "--detach")
    assert status == 0, started
    assert started["ok"] is True, started
    handle = started["run"]
    assert isinstance(handle, str) and RUN_HANDLE.match(handle), started
    ended = poll(lambda: cli_status(bench, handle), gone_or_ended, "the detached run ending")
    assert ended["state"] == "finished", ended
    assert ended["run_ok"] is True, ended
    assert ended["detached"] is True, ended
    detached = own_report(bench, ended)
    assert detached["run"] == handle, detached

    answer, green_summary, green_job = evidence_of(bench, ended["canonical_report_path"], EVIDENCE_DIRECTORY / "evidence-detached")
    assert answer["outcome"] == "success", answer
    assert green_summary["plan"]["name"] == name, green_summary["plan"]
    assert green_summary["bench"]["devices"] == {"debuggers": [debugger], "com_ports": [port]}, green_summary["bench"]
    assert "failed_step" not in green_summary.get("run", {}), green_summary
    assert green_job.startswith(f"## Agentic HIL: {name}\n\n**Outcome:** success\n\n"), green_job
    assert table_rows(green_job) == step_rows(detached), green_job
    assert all(row.split(" | ")[3] == "pass" for row in table_rows(green_job)), green_job
    assert " failed: " not in green_job, green_job
    assert "### Recovery" not in green_job.splitlines(), green_job

    for label, report, documents in (
        ("the red run's evidence", red, (summary, job)),
        ("the detached run's evidence", detached, (green_summary, green_job)),
    ):
        texts = [json.dumps(documents[0]), documents[1], *(value for _, value in walked(documents[0]) if isinstance(value, str))]
        for value, what in identities(bench, report).items():
            assert not any(value in text for text in texts), f"{what} reached {label}"


def test_the_pytest_plugin_runs_a_projects_plans_green_for_the_demos_claim_and_red_with_the_products_failure(bench: Bench) -> None:
    """The `agentic_hil` fixture in a project's own suite, run as that project would run it.

    A tiny suite is written into the project and run by a separate pytest in the
    project directory, whose `pytest.ini` anchors the rootdir the fixture binds
    the configuration's `workspace_root` to, with `AGENTIC_HIL_CONFIG` naming the
    configuration as the demo's own suite documents. Its tests run plans through
    the fixture's `test_reactor_run`, and the verdict is the product's own
    `overall_success`.

    The demo's own plan (flash, open, reset, read the banner) must pass. A claim
    the demo cannot meet must fail the test, with the product's error type and
    summary in the failure pytest reports, and both runs are the product's runs:
    registered under a handle of their own with the verdict the suite reported.
    """
    port = bench.com_port_name()
    unmet = write_plan(
        bench,
        "reactor-runs-plugin-unmet",
        3,
        [
            {"device": port, "action": "uart_open", "clear_buffer": True},
            {"device": bench.debugger_name(), "action": "reset", "mode": "run"},
            {"device": port, "action": "uart_read", "comparator": {"equals": NEVER_PRINTED}, "timeout_s": UNMET_TIMEOUT_S},
            {"device": port, "action": "uart_close"},
        ],
    )
    suite = bench.project / PLUGIN_SUITE
    suite.mkdir(exist_ok=True)
    (suite / PLUGIN_MODULE).write_text(PLUGIN_TESTS.replace("UNMET_PLAN", unmet), encoding="utf-8")
    environment = bench.environment()
    for variable in OUTER_PYTEST_VARIABLES:
        environment.pop(variable, None)

    def run_the_suite(test: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        before = set(cli_handles(bench))
        ran = subprocess.run(
            [sys.executable, "-s", "-m", "pytest", f"{PLUGIN_SUITE}/{PLUGIN_MODULE}::{test}", "-p", "no:cacheprovider", "-q", "-rfE"],
            cwd=str(bench.project),
            env=environment,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )
        return ran, [handle for handle in cli_handles(bench) if handle not in before]

    green, new = run_the_suite("test_the_demo_plan")
    assert green.returncode == 0, green.stdout + green.stderr
    assert "1 passed" in green.stdout, green.stdout
    assert len(new) == 1, new
    passed = cli_status(bench, new[0])
    assert (passed["state"], passed["run_ok"], passed["name"]) == ("finished", True, "nucleo-f446re-hello-world"), passed

    red, new = run_the_suite("test_a_claim_the_demo_cannot_meet")
    assert red.returncode == 1, red.stdout + red.stderr
    assert "1 failed" in red.stdout, red.stdout
    assert "comparator_unmet: Test reactor sequence failed." in red.stdout, red.stdout
    assert len(new) == 1, new
    failed = cli_status(bench, new[0])
    assert (failed["state"], failed["run_ok"], failed["error_type"], failed["failed_step"], failed["name"]) == (
        "finished",
        False,
        "comparator_unmet",
        3,
        "reactor-runs-plugin-unmet",
    ), failed
