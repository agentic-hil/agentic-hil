"""Which test each worker had in hand when a leg stopped (#536).

A leg that runs out of its budget is stopped from outside, and a pytest session
stopped that way prints no summary: the log ends in a row of dots, and the red
check names no test. While `AGENTIC_HIL_TEST_LEDGER` names a directory, this
plugin keeps a ledger there, one JSON object a line and one file per process: a
line as a test starts, and one as it finishes saying how it ended. Each line is
flushed before the test goes on, so a process killed in the middle of a test
leaves that test's start behind. The process that owns the session, the xdist
controller or the one process of a session without workers, also writes a line
as the session starts and one as it finishes, saying whether it was
interrupted.

The variable is for the session `Run tests` starts and for no other. That
session's own process takes it out of its environment at configure, before any
worker starts, and hands the directory to each worker through xdist's
workerinput, so a session a test starts inherits nothing and writes nothing.

Run as a script with the directory, this file is the reader the step after
`Run tests` runs when the job has failed:

    python tests/suite_ledger.py <directory>

A session that finished on its own terms, however badly, has had pytest name
its failures, and the reader adds nothing to it. A session that did not,
because it was killed or interrupted, becomes an error annotation and a job
summary naming every test that started and never finished, with its worker,
and below them every test that failed or ended in an error before that. The
reader needs nothing beyond the standard library: it also runs when a step
before `Run tests` failed, with no pytest to import and no ledger to read.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    import pytest

VARIABLE = "AGENTIC_HIL_TEST_LEDGER"
# What travels from the controller to a worker in its workerinput, and back in
# its workeroutput.
DIRECTORY_KEY = "agentic_hil_test_ledger"
INTERRUPTED_KEY = "agentic_hil_test_ledger_interrupted"
# The process that owns the session writes under this name: the xdist
# controller, whose file then holds the session's two lines alone, or the one
# process of a session without workers.
SESSION_OWNER = "main"
PLUGIN_NAME = "agentic-hil-test-ledger"
# The outcomes a finish line can carry that the reader names.
RED = ("failed", "error")
TITLE = "Run tests ran out of its budget"


def pytest_configure(config: pytest.Config) -> None:
    workerinput = getattr(config, "workerinput", None)
    if workerinput is None:
        # Taken out before any worker starts and before any test runs, so
        # neither inherits it. A worker gets the directory from the controller.
        directory = os.environ.pop(VARIABLE, "")
        name = SESSION_OWNER
    else:
        directory = workerinput.get(DIRECTORY_KEY, "")
        name = str(workerinput["workerid"])
    if not directory:
        return
    ledger = Ledger(config, Path(directory), name, owns_the_session=workerinput is None)
    config.pluginmanager.register(ledger, PLUGIN_NAME)
    if workerinput is None and config.pluginmanager.has_plugin("xdist"):
        # Its hooks are xdist's, and exist only where xdist is loaded.
        config.pluginmanager.register(Handover(ledger), f"{PLUGIN_NAME}-handover")


def pytest_unconfigure(config: pytest.Config) -> None:
    ledger = config.pluginmanager.get_plugin(PLUGIN_NAME)
    if ledger is not None:
        ledger.close()


class Ledger:
    """One process's file in the ledger directory, and the hooks that write it."""

    def __init__(self, config: pytest.Config, directory: Path, name: str, owns_the_session: bool) -> None:
        self.config = config
        self.directory = directory
        self.name = name
        self.owns_the_session = owns_the_session
        # A worker runs tests; the process that owns the session learns at the
        # session's start whether it is an xdist controller, which runs none.
        self.runs_tests = not owns_the_session
        self.interrupted = False
        self.current: str | None = None
        self.outcome = "passed"
        self.stream: TextIO | None = None

    def write(self, event: str, **fields: object) -> None:
        if self.stream is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.stream = (self.directory / f"{self.name}.jsonl").open("a", encoding="utf-8")
        self.stream.write(json.dumps({"event": event, "worker": self.name, **fields}) + "\n")
        # On disk before the test goes on: a process killed after this line
        # still leaves it behind.
        self.stream.flush()

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        if self.owns_the_session:
            # The controller hears every worker's test hooks as well; the
            # worker that ran a test is the one that writes it down.
            self.runs_tests = not session.config.pluginmanager.has_plugin("dsession")
            self.write("session-start")

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        if self.runs_tests:
            self.current = nodeid
            self.outcome = "passed"
            self.write("start", nodeid=nodeid)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        # The first phase that goes wrong decides, the way pytest's summary
        # counts it: a failed call is a failure, a broken setup or teardown an
        # error.
        if report.nodeid != self.current or self.outcome in RED:
            return
        if report.failed:
            self.outcome = "failed" if report.when == "call" else "error"
        elif report.skipped:
            self.outcome = "skipped"

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        # Not reached when the test is interrupted: pytest does not finish it.
        if self.runs_tests and nodeid == self.current:
            self.write("finish", nodeid=nodeid, outcome=self.outcome)
            self.current = None

    def pytest_keyboard_interrupt(self, excinfo: pytest.ExceptionInfo[BaseException]) -> None:
        # This hook also hears a collection error, --maxfail and pytest.exit
        # stop a session, each after pytest has said why, and xdist stop one
        # because a worker stopped: none of them raises KeyboardInterrupt
        # itself. That is what Ctrl+C raises, and the signal a runner sends a
        # step before it kills it.
        if type(excinfo.value) is not KeyboardInterrupt:
            return
        self.interrupted = True
        workeroutput = getattr(self.config, "workeroutput", None)
        if workeroutput is not None:
            workeroutput[INTERRUPTED_KEY] = True

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.owns_the_session:
            self.write("session-finish", exitstatus=int(exitstatus), interrupted=self.interrupted)


class Handover:
    """The controller's end of xdist: the directory out to each worker, and
    back whether the worker was interrupted."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def pytest_configure_node(self, node: Any) -> None:
        node.workerinput[DIRECTORY_KEY] = str(self.ledger.directory)

    def pytest_testnodedown(self, node: Any, error: object) -> None:
        # A worker that crashed sent no workeroutput, and xdist names the test
        # it crashed in.
        if (getattr(node, "workeroutput", None) or {}).get(INTERRUPTED_KEY):
            self.ledger.interrupted = True


def natural(name: str) -> list[object]:
    """`gw2` before `gw10`."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def read(directory: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]] | None:
    """The tests started and never finished, and the tests that went red, each with its worker.

    None when there is nothing to add to the leg: no ledger, because the
    session never started, or a session that finished on its own terms.
    """
    if not directory.is_dir():
        return None
    unfinished: dict[tuple[str, str], str] = {}
    red: list[tuple[str, str, str]] = []
    files = sorted((path for path in directory.glob("*.jsonl") if path.is_file()), key=lambda path: natural(path.stem))
    for path in files:
        for text in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                line = json.loads(text)
            except ValueError:
                # Half a line: its process was killed while writing it.
                continue
            if not isinstance(line, dict):
                continue
            event = line.get("event")
            nodeid = line.get("nodeid")
            worker = str(line.get("worker") or path.stem)
            if event == "session-finish" and not line.get("interrupted"):
                return None
            if not isinstance(nodeid, str):
                continue
            if event == "start":
                unfinished[(path.name, nodeid)] = worker
            elif event == "finish":
                unfinished.pop((path.name, nodeid), None)
                if line.get("outcome") in RED:
                    red.append((nodeid, worker, str(line["outcome"])))
    return [(nodeid, worker) for (_, nodeid), worker in unfinished.items()], red


def describe(unfinished: list[tuple[str, str]], red: list[tuple[str, str, str]]) -> tuple[list[str], list[str]]:
    """The annotation's lines and the job summary's, in that order."""
    stopped = "The test session was stopped before it finished."
    annotation = [stopped]
    summary = [f"### {TITLE}", "", stopped, ""]
    if unfinished:
        annotation.append("Started and never finished:")
        annotation.extend(f"  {nodeid} ({worker})" for nodeid, worker in unfinished)
        summary.extend(["Started and never finished:", ""])
        summary.extend(f"- {code(nodeid)} ({worker})" for nodeid, worker in unfinished)
        summary.append("")
    else:
        annotation.append("No test was left unfinished.")
        summary.extend(["No test was left unfinished.", ""])
    if red:
        annotation.append("Failed or ended in an error before that:")
        annotation.extend(f"  {nodeid} {outcome} ({worker})" for nodeid, worker, outcome in red)
        summary.extend(["Failed or ended in an error before that:", ""])
        summary.extend(f"- {code(nodeid)} {outcome} ({worker})" for nodeid, worker, outcome in red)
        summary.append("")
    return annotation, summary


def code(text: str) -> str:
    """`text` as a Markdown code span, whatever backticks it carries."""
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def escape_data(text: str) -> str:
    """A workflow command's message, which the runner decodes back to `text`."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    return escape_data(text).replace(":", "%3A").replace(",", "%2C")


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: python tests/suite_ledger.py <ledger directory>", file=sys.stderr)
        return 2
    reading = read(Path(arguments[0]))
    if reading is None:
        return 0
    annotation, summary = describe(*reading)
    message = escape_data("\n".join(annotation))
    print(f"::error title={escape_property(TITLE)}::{message}")
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        with open(destination, "a", encoding="utf-8") as stream:
            stream.write("\n".join(summary) + "\n")
    else:
        print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
