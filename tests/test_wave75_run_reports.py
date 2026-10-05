"""What a failed plan run says about itself, in every answer that describes it.

Five reports, one subject: a run that did not pass cleanly, and the answers a
caller reads it from. The run's own result, the report on disk, the record
behind the handle, and the status and stop answers built from that record are
five readings of one event, and each of these issues is a place where two of
them disagree or where one of them carries nothing.

- #694: the failing step's `backend` decides which advice the catalogue holds
  for its refusal, and the run publishes the step's `error_type` without it, so
  a run stopped by an OpenOCD timeout answers `error_type: timeout` with no
  `remediation` and no `do_not`, while the step in `steps` carries both.
- #675: `audit_error` after a failed report write names a Python class as its
  `error_type` and drops a configuration refusal's own advice, while the same
  fault met before the action nests the whole refusal.
- #674: a run whose final report could not be written is recorded as passed and
  its status points at the shared workspace mirror, which holds another run's
  report or nothing.
- #667: an interrupted run's report says `interrupted` and its record says
  `reactor_exception`, and a reactor that crashed is summarized as an
  interruption.
- #666: `test_reactor_run` lets the reactor's exception reach the MCP envelope,
  so the caller gets JSON-RPC -32603 instead of the report the run wrote.

Nothing here touches hardware. The debugger refusals are the ones the backends
answer, handed to the production attach point every debugger result passes on
its way out of the service, so what a failing step carries is what a real
refusal carries rather than advice written here.
"""
from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest
from conftest import write_config
from test_run_lifecycle import bench_workspace
from test_test_reactor import RecordingService, write_test_config

from agentic_hil import reactorrun, report, runlifecycle
from agentic_hil.cli import entrypoint
from agentic_hil.config import ConfigError, load_authoritative_config, load_config
from agentic_hil.knowledge import ERROR_CATALOGUE, remediation_fields
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.test_reactor import TestReactor, load_test_config
from agentic_hil.tools import AgenticHILToolService, attach_debugger_remediation

# The scope a run result looks its own advice up under. Read as a literal, like
# the other catalogue tests read it, so a renamed constant fails the test that
# is about the rule instead of this module's collection.
REACTOR_SCOPE = "test_reactor"

# A plan with nothing in it but a wait: it declares the probe, so the run takes
# the same lock and writes the same record a driving run does, and it reaches no
# hardware, so what the run reports is decided by the test and not by a backend.
DELAY_PLAN = "version: 4\nsteps:\n  - {device: dut, action: delay, duration_ms: 1}\n"
RESET_PLAN = "version: 4\nsteps:\n  - {device: dut, action: reset}\n"
FLASH_PLAN = "version: 2\nsteps:\n  - {debugger: dut, action: flash, image_path: build/app.elf}\n"
BREAKPOINT_PLAN = (
    "version: 2\n"
    "steps:\n"
    "  - {debugger: dut, action: debug_start, image_path: build/app.elf, mode: attach}\n"
    "  - {debugger: dut, action: run_until_breakpoint, location: test_done, timeout_s: 5}\n"
)


def advice_of(payload: dict) -> dict:
    """The advice fields on an answer, and nothing else."""
    return {key: payload[key] for key in ("remediation", "do_not") if key in payload}


def report_on_disk(workspace: Path) -> dict:
    path = workspace / ".agentic-hil" / "reports" / "last-report.json"
    assert path.is_file(), f"no report was written at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def assert_the_entry_arrived(payload: dict, key: str) -> None:
    """The payload names the key's type and carries that entry's advice."""
    error_type, _, scope = key.partition(":")
    assert payload.get("error_type") == error_type, payload
    expected = remediation_fields(error_type, scope or None)
    assert expected.get("remediation"), f"the catalogue holds no advice for {key}"
    assert payload.get("remediation") == expected["remediation"], payload
    assert payload.get("do_not") == expected.get("do_not"), payload


# ---------------------------------------------------------------------------
# #694: a failed run drops its step's `backend`.

# Every debugger refusal #694 names: catalogued per backend, with no bare entry
# and no `test_reactor` entry behind it, so the lookup a run result uses finds
# nothing while the advice sits one level down on the failing step.
BACKEND_SCOPED_REFUSALS = (
    ("timeout", "openocd"),
    ("timeout", "pyocd"),
    ("timeout", "stlink"),
    ("flash_failed", "openocd"),
    ("flash_failed", "pyocd"),
    ("verify_failed", "pyocd"),
    ("verify_failed", "stlink"),
    ("target_not_detected", "openocd"),
    ("target_not_detected", "pyocd"),
    ("target_not_detected", "stlink"),
    ("adapter_not_found", "openocd"),
    ("adapter_not_found", "pyocd"),
    ("adapter_not_found", "stlink"),
    ("flash_erase_failed", "openocd"),
    ("flash_erase_failed", "pyocd"),
    ("flash_erase_failed", "stlink"),
)


def assert_the_run_carries_advice_for_its_failing_step(payload: dict, error_type: str, backend: str) -> None:
    """A run answers advice for the type it publishes, by whichever route #694 takes.

    The issue leaves the route open and the three options answer the same
    question: the run may fall back to the failing step's `backend` scope, copy
    the step's own advice, or gain a bare or `test_reactor` entry of its own
    that points the reader at the failing step in `steps`. So what is asserted
    is that one of them happened: the answer carries advice, and the advice is
    either the catalogue's for that backend or the catalogue's for the type the
    run publishes. An answer that carries neither carries nothing, which is the
    bug.
    """
    assert payload.get("error_type") == error_type, payload
    carried = advice_of(payload)
    by_backend = remediation_fields(error_type, backend)
    by_the_runs_own_entry = remediation_fields(error_type, REACTOR_SCOPE)
    assert carried.get("remediation"), (
        f"a run that stopped on {error_type} from {backend} answered no remediation, while the catalogue holds "
        f"{error_type}:{backend} and the failing step carries it: {payload.get('summary')!r}"
    )
    assert carried in (by_backend, by_the_runs_own_entry), (
        f"the advice on this answer is neither the {error_type}:{backend} entry (option 1 or 2 of #694) nor an entry "
        f"for {error_type} the run's own lookup finds (option 3): {carried}"
    )


class BackendRefusesOneTool(RecordingService):
    """One tool answering a debugger refusal, every other call unchanged.

    The refusal goes out through `attach_debugger_remediation`, which is where
    the service fills a debugger result's advice from its `backend`, so the
    failing step in `steps` carries exactly what it carries on a real bench.
    That is the whole of the setup: the question is what the run does with a
    step that already has the advice.
    """

    def __init__(self, tool: str, refusal: dict) -> None:
        super().__init__()
        self.tool = tool
        self.refusal = refusal

    def call(self, name: str, arguments: dict | None = None) -> dict:
        if name == self.tool:
            self.calls.append((name, arguments or {}))
            return attach_debugger_remediation(dict(self.refusal))
        return super().call(name, arguments)


@pytest.mark.parametrize(("error_type", "backend"), BACKEND_SCOPED_REFUSALS)
def test_the_catalogue_holds_the_advice_each_backend_refusal_needs(error_type: str, backend: str) -> None:
    """The premise of #694: the advice exists, one level down from the run.

    `do_not` is not asserted: `target_not_detected` and `adapter_not_found` are
    written without one on all three backends, and what the run owes its caller
    is the entry as the catalogue holds it, not a field every entry has."""
    assert remediation_fields(error_type, backend).get("remediation"), f"{error_type}:{backend} has no remediation"


@pytest.mark.parametrize(("error_type", "backend"), BACKEND_SCOPED_REFUSALS)
def test_a_run_stopped_by_a_backend_refusal_answers_that_backends_advice(tmp_path: Path, error_type: str, backend: str) -> None:
    """Each refusal in turn, on a bench configured for the backend that answers it.

    A flash is the step every one of these can stop: an absent adapter, a target
    that never answered, an erase, a write or a verify that failed, and a
    toolchain that ran out of time are all answers `flash_firmware` gives."""
    config = load_config(str(write_config(tmp_path, debugger_type=backend)))
    plan = write_test_config(tmp_path, FLASH_PLAN)
    service = BackendRefusesOneTool(
        "flash_firmware",
        {"ok": False, "tool": "flash_firmware", "backend": backend, "error_type": error_type, "summary": "The debugger refused the flash."},
    )

    result = TestReactor(config, service).run(load_test_config(str(plan), str(tmp_path)))  # type: ignore[arg-type]

    assert result.get("ok") is False, result
    assert result.get("step_error_type") == error_type, result
    # The failing step carries the entry its backend names; the run has to answer
    # the same failure with advice of its own rather than with the type alone.
    assert_the_entry_arrived(result["steps"][0]["result"], f"{error_type}:{backend}")
    assert_the_run_carries_advice_for_its_failing_step(result, error_type, backend)


def test_a_run_whose_breakpoint_was_not_reached_answers_the_timeout_advice(tmp_path: Path) -> None:
    """The issue's own example: `debug_continue` timed out inside a session.

    The step answers `timeout` from `openocd` with `target_state: unknown`, so
    the board may be running code nobody has read the state of. That is the
    advice the caller needs most and the answer that had none: `timeout:openocd`
    says to read `target_state` and `side_effect_status` and to call
    `debug_halt`, and forbids repeating the flash, the reset or the continue.
    """
    config = load_config(str(write_config(tmp_path)))
    plan = write_test_config(tmp_path, BREAKPOINT_PLAN)
    service = BackendRefusesOneTool(
        "debug_continue",
        {
            "ok": False,
            "tool": "debug_continue",
            "backend": "openocd",
            "error_type": "timeout",
            "summary": "Target did not stop before the timeout.",
            "target_state": "unknown",
            "side_effect_status": "unknown",
        },
    )

    result = TestReactor(config, service).run(load_test_config(str(plan), str(tmp_path)))  # type: ignore[arg-type]

    assert result.get("failed_step") == 2, result
    assert result.get("step_error_type") == "timeout", result
    assert_the_entry_arrived(result["steps"][1]["result"], "timeout:openocd")
    assert_the_run_carries_advice_for_its_failing_step(result, "timeout", "openocd")


def answer_one_tool_with(monkeypatch: pytest.MonkeyPatch, tool: str, refusal: dict) -> None:
    """Make one tool answer a backend refusal on every service in this process.

    Patched on the class, because the service the reactor builds for itself is
    not the one the test holds and both have to answer the same way. Every other
    call goes to the real implementation, so the run, its locks, its record and
    its report are the real ones.
    """
    original = AgenticHILToolService.call

    def call(self: AgenticHILToolService, name: str, arguments: dict | None = None) -> dict:
        if name == tool:
            return attach_debugger_remediation(dict(refusal))
        return original(self, name, arguments)

    monkeypatch.setattr(AgenticHILToolService, "call", call)


def worker_that_runs_the_plan_in_this_process():
    """A `spawn_run_worker` whose run is already over when the start command looks.

    `_detached_terminal_result` is the answer a start gives for a run that
    reached a terminal state before it could be caught at `running`, and it is
    one of the four answers #694 is about. Running the plan in this process is
    what makes the run terminal by then without racing a real worker.
    """

    class EndedWorker:
        def poll(self) -> int:
            return 0

    def spawn(config, handle: str, test_config_path: str, *, wait_s: float) -> EndedWorker:
        reactorrun.run_plan(config, test_config_path, wait_s=wait_s, run_handle=handle)
        return EndedWorker()

    return spawn


def test_every_answer_about_a_failed_run_carries_the_same_advice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The report, the detached start, the status and the stop, about one run.

    The record keeps the run's `error_type` and not its backend, so the three
    answers built from the record look the advice up again by a rule that finds
    nothing. Whatever the run's own report ends up carrying, these have to carry
    the same: they are answers about the same run.
    """
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    config = load_authoritative_config(workspace)
    answer_one_tool_with(
        monkeypatch,
        "reset_target",
        {"ok": False, "tool": "reset_target", "backend": "openocd", "error_type": "timeout", "summary": "Debugger command timed out.", "side_effect_status": "unknown"},
    )
    monkeypatch.setattr(runlifecycle, "spawn_run_worker", worker_that_runs_the_plan_in_this_process())
    service = AgenticHILToolService(config)
    try:
        started = service.call("test_reactor_run", {"test_config_path": str(plan), "detach": True})
        handle = started.get("run")
        assert isinstance(handle, str), started
        status = service.call("test_reactor_status", {"run": handle})
        stop = service.call("test_reactor_stop", {"run": handle})
    finally:
        service.close()

    written = report_on_disk(workspace)
    assert_the_run_carries_advice_for_its_failing_step(written, "timeout", "openocd")
    assert started.get("state") == "finished", started
    assert status.get("run_ok") is False and stop.get("ok") is True, (status, stop)
    for answer in (started, status, stop):
        assert answer.get("error_type") == written.get("error_type"), answer
        assert advice_of(answer) == advice_of(written), (
            f"this answer about {handle} carries {advice_of(answer)} where its report carries {advice_of(written)}"
        )


# ---------------------------------------------------------------------------
# #675: the shape of `audit_error` after a failed report write.

NO_SPACE = (errno.ENOSPC, "No space left on device")
UNSAFE_DESTINATION = ConfigError(
    "unsafe_configured_path",
    "Output file must be a single-link regular file without symlinked parents.",
    {
        "path": "/state-root/agentic-hil/reports/last-report.json",
        "resolved_parent": "/state-root/agentic-hil/reports",
    },
)


def test_a_filesystem_fault_after_the_action_names_a_catalogued_type() -> None:
    """`OSError` is a Python class, not an answer a reader can look up.

    `report_unreadable` is what the same fault looks like on the read side:
    a catalogued type with `error_class` and `errno` beside it and the path
    withheld. The write side has to be readable the same way.
    """
    audit_error = report.mark_audit_failure({"ok": True, "tool": "test_reactor"}, OSError(*NO_SPACE))["audit_error"]

    assert audit_error.get("error_type") != "OSError", audit_error
    assert audit_error.get("error_type") in ERROR_CATALOGUE, (
        f"a caller looking {audit_error.get('error_type')!r} up in the error catalogue finds no entry: {audit_error}"
    )
    assert remediation_fields(str(audit_error.get("error_type"))).get("remediation"), audit_error
    assert audit_error.get("error_class") == "OSError", audit_error
    assert audit_error.get("errno") == errno.ENOSPC, audit_error


def test_a_configuration_refusal_after_the_action_keeps_its_own_advice() -> None:
    """One shape on both paths: the refusal met before the action, and after it."""
    before = report.audit_unavailable("probe_target", UNSAFE_DESTINATION)["audit_error"]
    after = report.mark_audit_failure({"ok": True, "tool": "test_reactor"}, UNSAFE_DESTINATION)["audit_error"]

    assert before.get("remediation"), before
    assert after.get("error_type") == "unsafe_configured_path", after
    assert after.get("summary") == UNSAFE_DESTINATION.summary, after
    assert after.get("resolved_parent") == "/state-root/agentic-hil/reports", after
    # Every field the refusal carries before the action it carries after it, less
    # `path`, which the issue leaves open for the nested refusal to withhold.
    missing = {key: value for key, value in before.items() if key != "path" and after.get(key) != value}
    assert not missing, f"the refusal met after the write dropped {sorted(missing)}: {after}"


def test_a_run_whose_report_write_failed_answers_a_readable_audit_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller's own view of #675, through the real report writer."""
    config = load_config(str(write_config(tmp_path)))

    def no_space(*_args: object, **_kwargs: object) -> None:
        raise OSError(*NO_SPACE)

    monkeypatch.setattr("agentic_hil.report.safe_write_text", no_space)

    written = report.write_report(config, {"ok": True, "tool": "test_reactor", "summary": "Test reactor sequence completed."})

    assert written.get("audit_ok") is False, written
    for marker in (written["audit_error"], *written["audit_errors"]):
        assert marker.get("error_type") in ERROR_CATALOGUE, marker
        assert marker.get("error_class") == "OSError", marker
        assert marker.get("errno") == errno.ENOSPC, marker


# ---------------------------------------------------------------------------
# #674: a run whose final report could not be written.


def audit_failed_write(error: OSError):
    def write(config, prepared: dict) -> dict:
        return report.mark_audit_failure(prepared, error)

    return write


def test_a_run_whose_report_could_not_be_written_is_not_recorded_as_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The run passed its steps and has no record of having run.

    `overall_success` fails the result, so the call that ran the plan answered an
    error. The record is written from `ok` alone and said the run passed, and
    its `report_path` fell back to the shared workspace mirror, which holds
    another run's report or nothing. Which of the two routes the record takes
    (`run_ok` from `overall_success`, or an explicit `audit_ok: false` the
    summary reads) is open; that the status does not read as a clean pass, and
    names no report for a run that wrote none, is not.
    """
    workspace, plan = bench_workspace(tmp_path, monkeypatch, DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(reactorrun, "write_report", audit_failed_write(OSError(*NO_SPACE)))
    handle = runlifecycle.new_run_handle()

    result = reactorrun.run_plan(config, str(plan), run_handle=handle)

    assert result.get("ok") is True and result.get("audit_ok") is False, result
    assert report.overall_success(result) is False, result
    record = runlifecycle.read_run_record(config, handle) or {}
    status = runlifecycle.run_status(config, handle)
    assert status.get("state") == "finished", status
    assert status.get("run_ok") is False or status.get("audit_ok") is False, (
        f"the status of {handle} reads as a clean pass for a run whose report could not be written: {status}"
    )
    summary = str(status.get("summary", "")).lower()
    assert "passed" not in summary and any(
        word in summary for word in ("fail", "audit", "unaudited", "did not pass")
    ), status
    assert runlifecycle.run_report_named(record) is None, (
        f"the record of {handle} names {runlifecycle.run_report_named(record)!r} as this run's report, and no report was written"
    )
    assert "last-report.json" not in str(status.get("summary")), status


# ---------------------------------------------------------------------------
# #667: interrupted runs against crashed ones.


def raising_run(error: BaseException):
    def run(self, test_config):
        raise error

    return run


def run_the_reactor_left_by_raising(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException) -> tuple[dict, dict]:
    """A run the reactor left by raising, as its report and its status say it."""
    workspace, plan = bench_workspace(tmp_path, monkeypatch, DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(TestReactor, "run", raising_run(error))
    handle = runlifecycle.new_run_handle()
    with pytest.raises(type(error)):
        reactorrun.run_plan(config, str(plan), run_handle=handle)
    return report_on_disk(workspace), runlifecycle.run_status(config, handle)


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(1)], ids=["keyboard-interrupt", "system-exit"])
def test_an_interrupted_run_is_interrupted_in_its_report_and_in_its_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    written, status = run_the_reactor_left_by_raising(tmp_path, monkeypatch, error)

    assert written.get("error_type") == "interrupted", written
    assert written.get("exception_type") == type(error).__name__, written
    assert "interrupt" in str(written.get("summary")).lower(), written
    # The same run, asked by its handle. `reactor_exception` sends the reader to
    # advice that calls this a defect in Agentic HIL and asks them to report it.
    assert status.get("error_type") == "interrupted", status
    assert_the_entry_arrived(status, "interrupted")


def test_a_run_whose_reactor_raised_is_not_summarized_as_an_interruption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written, status = run_the_reactor_left_by_raising(tmp_path, monkeypatch, RuntimeError("reactor broke"))

    assert written.get("error_type") == "reactor_exception", written
    assert written.get("exception_type") == "RuntimeError", written
    assert "interrupt" not in str(written.get("summary")).lower(), (
        f"a reactor that raised is reported as an operator's interruption: {written.get('summary')!r}"
    )
    assert status.get("error_type") == "reactor_exception", status
    assert_the_entry_arrived(status, "reactor_exception")


def test_the_interrupted_entry_no_longer_states_the_mismatch() -> None:
    """The entry told the reader the record says `reactor_exception`.

    That sentence described the disagreement instead of resolving it, so it goes
    when the record starts carrying the report's own type.
    """
    entry = ERROR_CATALOGUE["interrupted"]

    assert "reactor_exception" not in entry.meaning, entry.meaning


# ---------------------------------------------------------------------------
# #666: the reactor's exception reaching the caller.


def mcp_response(service: AgenticHILToolService, name: str, arguments: dict) -> dict:
    response = handle_mcp_message({"jsonrpc": "2.0", "id": name, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, service)
    assert isinstance(response, dict), response
    return response


def test_a_reactor_that_raised_is_answered_as_a_failed_tool_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The report the run wrote, answered as the result of the call that wrote it.

    A protocol error carries no `error_type`, no `remediation`, no handle and no
    report path, so a client cannot tell a crashed run from a broken server and
    the obvious next move is the retry the entry's `do_not` warns against.
    """
    workspace, plan = bench_workspace(tmp_path, monkeypatch, DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(TestReactor, "run", raising_run(RuntimeError("reactor broke")))
    service = AgenticHILToolService(config)
    try:
        response = mcp_response(service, "test_reactor_run", {"test_config_path": str(plan)})
    finally:
        service.close()

    assert "error" not in response, (
        f"the call answered JSON-RPC {response.get('error', {}).get('code')} and the report it wrote never reached the caller: {response}"
    )
    answered = response["result"]["structuredContent"]
    assert response["result"]["isError"] is True, response["result"]
    assert_the_entry_arrived(answered, "reactor_exception")
    assert answered.get("exception_type") == "RuntimeError", answered
    assert isinstance(answered.get("run"), str) and answered["run"], answered
    assert isinstance(answered.get("report_path"), str) and answered["report_path"], answered
    written = report_on_disk(workspace)
    assert written.get("error_type") == "reactor_exception", written
    assert answered["run"] == written.get("run"), (answered, written)
    assert answered["report_path"] == written.get("report_path"), (answered, written)
    for field in ("cleanup", "cleanup_ok", "cleanup_errors", "cleanup_required"):
        if field in written:
            assert answered.get(field) == written[field], (field, answered, written)


def test_a_reactor_that_raised_is_rendered_as_a_refusal_at_the_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    workspace, plan = bench_workspace(tmp_path, monkeypatch, DELAY_PLAN)
    monkeypatch.setattr(TestReactor, "run", raising_run(RuntimeError("reactor broke")))

    exit_code = entrypoint(["test-reactor", "--test-config", str(plan), "--json"])

    assert exit_code == 1
    printed = json.loads(capsys.readouterr().out)
    assert_the_entry_arrived(printed, "reactor_exception")
    assert printed.get("run") == report_on_disk(workspace).get("run"), printed


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(1)], ids=["keyboard-interrupt", "system-exit"])
def test_an_interruption_still_reaches_the_terminal_it_came_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    """Ctrl+C and a process exit keep propagating, which is what #666 preserves."""
    workspace, plan = bench_workspace(tmp_path, monkeypatch, DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(TestReactor, "run", raising_run(error))
    service = AgenticHILToolService(config)
    try:
        with pytest.raises(type(error)):
            service.call("test_reactor_run", {"test_config_path": str(plan)})
    finally:
        service.close()

    assert report_on_disk(workspace).get("error_type") == "interrupted"


def test_the_reactor_exception_entry_no_longer_promises_a_protocol_failure() -> None:
    """The entry described the outcome #666 replaces.

    It told the reader that the run's own call raises, so an MCP client sees an
    internal error and the command line a traceback. Both are what the call now
    answers instead.
    """
    meaning = ERROR_CATALOGUE["reactor_exception"].meaning.lower()

    assert "internal error" not in meaning, meaning
    assert "traceback" not in meaning, meaning
