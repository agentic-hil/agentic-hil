"""Running a plan, for whichever frontend asked for it.

The reactor used to be reachable one way: a person typing `agentic-hil
test-reactor` at a shell. The machinery underneath was never the command's (it
is a configuration, a plan, a lock declaration and a report), but it lived in
`cli.py`, so the only door to it was a subprocess. An agent holding the MCP
tools therefore had to leave them to run the one feature that ties them
together, which is exactly what those tools exist to stop.

So the run lives here and the frontends are thin. `cli.py` binds these to the
configuration it discovers from the working directory; the MCP tools bind them
to the configuration their server was started on. Neither one decides anything
else: the devices a plan declares, the permissions each step is judged by and
the report at the end are the same because they are the same code, not because
two copies were kept in step.
"""
from __future__ import annotations

from agentic_hil.config import ConfigError
from agentic_hil.coordination import CoordinationError
from agentic_hil.junit import result_with_junit_xml, write_refusal_junit_xml
from agentic_hil.report import write_report
from agentic_hil.runlifecycle import RunRegistration, new_run_handle, start_detached_run
from agentic_hil.test_reactor import (
    DEFAULT_TEST_CONFIG_PATH,
    TestConfig,
    TestReactor,
    load_test_config,
    plan_devices,
    plan_digest_field,
)
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import AgenticHILConfig, JsonObject


def start_plan_detached(config: AgenticHILConfig, test_config_path: str | None = None, *, wait_s: float = 0.0) -> JsonObject:
    """Start a run in its own process and answer at once.

    The plan is loaded here as well as in the worker, and deliberately: a plan
    that does not load is a fault in the file, and answering it with a handle to
    go and ask about would put a refusal a caller could have had immediately
    behind a second command."""
    load_test_config(test_config_path, config.work_dir)
    return start_detached_run(config, test_config_path or DEFAULT_TEST_CONFIG_PATH, wait_s=wait_s)


def run_plan(
    config: AgenticHILConfig,
    test_config_path: str | None = None,
    *,
    wait_s: float = 0.0,
    run_handle: str | None = None,
    junit_xml: str | None = None,
) -> JsonObject:
    """Run a plan to its end and answer with the report.

    Registered under a run handle either way. A detached worker is handed the
    handle its start command printed; a synchronous run mints its own, so a
    plan running in one terminal can still be asked to stop from another
    instead of being killed, which is the dead-owner route.

    `junit_xml` asks for a second document beside the report, for a CI job that
    reads test reports rather than this project's JSON. It changes nothing about
    the run: the same plan, the same locks, the same report, the same answer.
    Written wherever the command answers at all, refusals included, and the
    refusals are why it is written from here rather than from the caller: the
    plan that would name the skipped cases is loaded here, and a run refused
    before its first step is exactly the run whose artifact a CI job needs.

    One path deliberately leaves without one, and it is the path that prints no
    JSON either: an interrupted run raises past this to the operator's terminal,
    and its record is the report `run_registered_plan` committed before it
    re-raised."""
    test_config: TestConfig | None = None
    try:
        test_config = load_test_config(test_config_path, config.work_dir)
        registration = RunRegistration.take(
            config,
            run_handle or new_run_handle(),
            name=test_config.name,
            test_config_path=test_config.path,
            detached=run_handle is not None,
        )
    except ConfigError as error:
        # A plan that does not load, or a handle another process already runs, is
        # as red to a CI job as a plan that failed on the board, and it is the
        # case where no report file is written at all either. The steps go in
        # when the plan did load, so the document still says what the run would
        # have done.
        #
        # Name the plan that was asked for, so the refusal document a CI job
        # captures says which plan it is about. A run that failed to load names
        # the file only as the absolute `path` the loader rejected; the evidence
        # bundle reads `test_config_path`, and a refusal that never named it
        # would produce a bundle that could not say which plan the outcome
        # belongs to.
        error.details.setdefault("test_config_path", test_config_path or DEFAULT_TEST_CONFIG_PATH)
        write_refusal_junit_xml(junit_xml, {"tool": "test_reactor", **error.to_dict()}, plan_steps=() if test_config is None else test_config.steps)
        raise
    with registration:
        result = run_registered_plan(config, test_config, wait_s=wait_s, registration=registration)
        registration.finish(result)
    if junit_xml is None:
        return result
    return result_with_junit_xml(result, junit_xml, plan_steps=test_config.steps)


def run_registered_plan(config: AgenticHILConfig, test_config: TestConfig, *, wait_s: float, registration: RunRegistration) -> JsonObject:
    service = AgenticHILToolService(config, frontend="reactor")
    # The plan's devices are held from before its first step to after its last,
    # not around each call: between two steps there would otherwise be no lock at
    # all, and an observation from outside could reach the board exactly where
    # the plan assumes nothing moved. The same declaration fixes what this run
    # may touch, so a step reaching past the plan is refused rather than
    # silently widening what the plan says it does.
    plan = plan_devices(config, test_config)
    devices = plan.lock_keys
    if devices:
        try:
            service.coordinator.begin_run(plan, label=test_config.name, wait_s=wait_s)
        except CoordinationError as error:
            service.close()
            return write_report(
                config,
                {
                    "tool": "test_reactor",
                    "name": test_config.name,
                    "test_config_path": test_config.path,
                    **plan_digest_field(test_config),
                    "steps": [],
                    "cleanup": [],
                    "cleanup_ok": True,
                    "declared_devices": devices,
                    **error.result,
                    "summary": str(error.result.get("summary", "A device this plan declares is unavailable.")) + " No step ran.",
                    "run": registration.handle,
                },
            )
    # Published only now: a run says it is running once it holds the devices it
    # declared, so a handle reported as running is a handle that has the bench.
    # A run refused the bench above never reaches this and is answered as the
    # finished run it is.
    registration.running()
    # A step naming another probe gets its own service driving that debugger,
    # sharing the base coordinator so the whole project stays one owner.
    def debugger_service_factory(bound_config: AgenticHILConfig) -> AgenticHILToolService:
        return AgenticHILToolService(bound_config, coordinator=service.coordinator, frontend="reactor")

    # Construction happens inside the guarded block: the factory builds real
    # per-probe services while the plan runs, so a failure there must still
    # produce a JSON error result and fall through to service.close() below.
    reactor: TestReactor | None = None
    primary_error: BaseException | None = None
    try:
        reactor = TestReactor(
            service.config,
            service,
            service_factory=debugger_service_factory,
            stop_requested=registration.stop_requested,
            on_progress=registration.progress,
        )
        result = reactor.run(test_config)
    except BaseException as error:
        primary_error = error
        result = {
            "ok": False,
            "tool": "test_reactor",
            "name": test_config.name,
            "test_config_path": test_config.path,
            **plan_digest_field(test_config),
            "error_type": "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "reactor_exception",
            "exception_type": type(error).__name__,
            "summary": "Test reactor was interrupted; all containment steps were attempted.",
            "steps": [],
            "cleanup": getattr(error, "agentic_hil_cleanup", []),
            "cleanup_ok": False,
        }
    try:
        if reactor is not None:
            reactor.close()
    except BaseException as error:
        cleanup_error = {
            "device": "reactor",
            "action": "close",
            "result": {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "cleanup_exception",
                "summary": "Per-device service cleanup raised an exception.",
                "exception_type": type(error).__name__,
                "backend_error": str(error),
            },
        }
        result["ok"] = False
        result["cleanup_ok"] = False
        result.setdefault("cleanup", []).append(cleanup_error)
        result.setdefault("cleanup_errors", []).append(cleanup_error)
        result.setdefault("step_error_type", result.get("error_type"))
        result["error_type"] = "cleanup_failed"
        result["summary"] = "Test reactor sequence failed during cleanup."
        if primary_error is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
            primary_error = error
    try:
        service.close()
    except BaseException as error:
        cleanup_error = {
            "device": "service",
            "action": "close",
            "result": {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "cleanup_exception",
                "summary": "Agentic HIL service cleanup raised an exception.",
                "exception_type": type(error).__name__,
                "backend_error": str(error),
            },
        }
        result["ok"] = False
        result["cleanup_ok"] = False
        result.setdefault("cleanup", []).append(cleanup_error)
        result.setdefault("cleanup_errors", []).append(cleanup_error)
        result.setdefault("step_error_type", result.get("error_type"))
        result["error_type"] = "cleanup_failed"
        result["summary"] = "Test reactor sequence failed during cleanup."
        if primary_error is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
            primary_error = error
    written = write_report(config, {**result, "run": registration.handle})
    if primary_error is not None:
        if written.get("audit_ok") is False:
            primary_error.args = (*primary_error.args, "Final reactor audit failed.")
        raise primary_error
    return written
