"""The JUnit XML a reactor run can be asked to leave behind.

A plan run through the reactor answers with this project's own JSON report, and
nothing that reads test reports understands it. A suite driven through the
pytest fixtures inherits `--junitxml` for free, so the two ways to run hardware
were unequal in CI in exactly the wrong direction: the path the examples
recommend produced no artifact, and the fallback did.

The mapping is not invented here. `docs/github-action-design.md` specifies it
under "The JUnit mapping", and this module is that specification: one suite per
run named after the plan, one case per plan step named `<index>.<route>.<action>`,
`<failure>` on the step that failed, `<skipped>` for the steps after it rather
than passes, a `preflight` case carrying `<error>` when the run was refused
before it touched anything, and `cleanup` and `audit` cases for the two ways a
run whose steps all passed is still not a passing run.

It lives in the package rather than in a workflow file for the reason the design
gives: the mapping is a pure function of the run report, and here it is under
this project's own tests and is the same file for the action, for the pytest
plugin's users and for anybody running the reactor by hand.

Two properties the document is built for. It is derived and nothing else: every
number and every timestamp in it was measured by the run, and a step whose
backend reported no duration carries no `time` rather than a made-up one. And it
exists whenever the command answered at all, refusals included, because the
artifact matters most when the run is red and a CI job that has to explain a
missing file is a job that learned nothing.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree

from agentic_hil.test_reactor import result_error_type, result_failed, step_results
from agentic_hil.types import JsonObject

if TYPE_CHECKING:
    from agentic_hil.test_reactor import TestStep

# The one combination the flag cannot serve. `--detach` answers before the run
# has a report, so there is nothing to map yet, and no later command owns the
# path the start command was given: `test-reactor-status` is a reader that may
# be called never, once or ten times, and an artifact written by whichever call
# happened to catch the run finishing is an artifact a CI job cannot depend on.
# Refused by name instead, so the operator is told which half to drop.
JUNIT_DETACHED_ERROR = "junit_xml_requires_synchronous_run"

# What a failed write is called. A run that produced no artifact the operator
# asked for has not done what it was told, whatever the board did.
JUNIT_WRITE_ERROR = "junit_xml_write_failed"

# XML 1.0 admits tab, newline, carriage return and everything from 0x20 up,
# minus the surrogate block and the two non-characters at the end of the BMP.
# A summary carrying anything else would produce a document no CI reader can
# parse, which is the one thing this file must never do.
_ALLOWED_CONTROL_CHARACTERS = {0x09, 0x0A, 0x0D}


def detached_junit_refusal(junit_xml_path: str) -> JsonObject:
    """The answer to `--junit-xml` beside `--detach`, refused before anything starts."""
    return {
        "ok": False,
        "tool": "test_reactor_start",
        "error_type": JUNIT_DETACHED_ERROR,
        "summary": (
            "--junit-xml writes the report of a run this command waited for, and --detach returns before the run "
            "has one. Run the plan without --detach to get the file, or follow the detached run with "
            "test-reactor-status and read the JSON report it names."
        ),
        "junit_xml": junit_xml_path,
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "retry_safe": False,
    }


def write_junit_xml(junit_xml_path: str, result: JsonObject, *, plan_steps: Sequence[TestStep] = ()) -> str:
    """Write the run's JUnit document where the caller said, and answer with the path.

    Parents are created, because a CI job names a path under a directory the
    upload step will make and the run must not fail on a missing folder. The
    write is the plain one `schema` and `test-schema` use for an operator-named
    output path: this is a report for another tool to read, not a file this
    project later trusts as evidence, so it is not routed through the state
    root's locking and audit."""
    document = junit_xml_document(result, plan_steps=plan_steps)
    target = Path(junit_xml_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")
    return str(target)


def write_refusal_junit_xml(junit_xml_path: str | None, refusal: JsonObject, *, plan_steps: Sequence[TestStep] = ()) -> None:
    """Leave the artifact for a run refused before it could produce a report.

    Best effort, and deliberately so. The caller is on its way out of the command
    carrying the refusal an operator has to read, and a second failure raised out
    of that handler would replace it with a story about a file path. So a write
    that cannot happen here is silent: the refusal itself still reaches standard
    output and the exit code, which is where anybody looking is looking."""
    if junit_xml_path is None:
        return
    with suppress(OSError, ValueError):
        write_junit_xml(junit_xml_path, refusal, plan_steps=plan_steps)


def result_with_junit_xml(result: JsonObject, junit_xml_path: str, *, plan_steps: Sequence[TestStep] = ()) -> JsonObject:
    """The run's result with its JUnit artifact recorded, or with the write's failure.

    Everything the command already answers with is carried through untouched: the
    report is the report whether or not a second file was asked for. A write that
    failed is not silent, though, and not fatal either: the run happened, its JSON
    report stands and is still returned, and the missing artifact is named on it
    and pulls `ok` down, because an operator who asked for a CI artifact and got
    none has not had the command they typed."""
    try:
        written = write_junit_xml(junit_xml_path, result, plan_steps=plan_steps)
    except (OSError, ValueError) as error:
        failed = {
            **result,
            "ok": False,
            "junit_xml": junit_xml_path,
            "junit_xml_error": {
                "error_type": JUNIT_WRITE_ERROR,
                "summary": "The JUnit XML report could not be written where --junit-xml said.",
                "exception_type": type(error).__name__,
                "backend_error": str(error),
            },
        }
        failed.setdefault("error_type", JUNIT_WRITE_ERROR)
        return failed
    return {**result, "junit_xml": written}


def junit_xml_document(result: JsonObject, *, plan_steps: Sequence[TestStep] = ()) -> str:
    """The JUnit document for one run report. A pure function of what it is given.

    `plan_steps` is the loaded plan, and it is what makes the skipped cases
    honest: the report records the steps that executed, so the steps after an
    abort exist only in the plan. Without it the document falls back to the
    executed records and says nothing about steps it cannot know about, which is
    the case where the plan never loaded at all."""
    # Every attribute in this document is filtered on the way in, and the reason
    # is that almost none of the text is this project's: a plan names itself and
    # its devices, and a backend writes its own summary. One byte XML has no way
    # to carry would make the whole file unreadable to the tool it exists for.
    suite_name = _attribute(str(result.get("name") or "test-reactor"))
    named_steps = _named_steps(result, plan_steps)
    records = {int(record["index"]): record for record in _step_records(result) if isinstance(record.get("index"), int)}
    refusal = run_refusal(result)

    cases: list[ElementTree.Element] = []
    failures = 0
    errors = 0
    skipped = 0
    measured: list[float] = []

    skip_message = _attribute(_skip_message(result, refused=refusal is not None))
    for index, route, action in named_steps:
        case = ElementTree.Element("testcase", {"name": _attribute(f"{index}.{route}.{action}"), "classname": suite_name})
        record = None if refusal is not None else records.get(index)
        if record is None:
            skipped += 1
            ElementTree.SubElement(case, "skipped", {"message": skip_message})
            cases.append(case)
            continue
        step_result = record.get("result") if isinstance(record.get("result"), dict) else {}
        elapsed = _elapsed_seconds(step_result)
        if elapsed is not None:
            measured.append(elapsed)
            case.set("time", _format_seconds(elapsed))
        if result_failed(step_result):
            failures += 1
            failure = ElementTree.SubElement(
                case,
                "failure",
                {
                    "type": _attribute(result_error_type(step_result)),
                    "message": _attribute(str(step_result.get("summary") or "The step failed.")),
                },
            )
            # The whole record, not only its result: for a block step the
            # iterations are where the nested step that actually failed is
            # recorded, and a body without them would send a reader back to the
            # JSON report for the one thing they opened this file to find.
            failure.text = _body(record)
        cases.append(case)

    if refusal is not None:
        errors += 1
        case = ElementTree.Element("testcase", {"name": "preflight", "classname": suite_name})
        error_element = ElementTree.SubElement(
            case,
            "error",
            {
                "type": _attribute(str(refusal.get("error_type") or "test_config_invalid")),
                "message": _attribute(str(refusal.get("summary") or "The run was refused before any step ran.")),
            },
        )
        # The field, the route and the action the design asks for travel in the
        # body: they are whatever the refusal named, and naming them one at a
        # time here would be a second reader of a document this already carries.
        error_element.text = _body(refusal)
        cases.append(case)

    if result.get("cleanup_ok") is False:
        failures += 1
        cases.append(_verdict_case(suite_name, "cleanup", "cleanup_failed", _cleanup_summary(result), result.get("cleanup_errors") or result.get("cleanup") or []))
    if result.get("audit_ok") is False:
        failures += 1
        cases.append(_verdict_case(suite_name, "audit", "audit_failed", _audit_summary(result), result.get("audit_errors") or result.get("audit_error") or {}))

    suite = ElementTree.Element(
        "testsuite",
        {
            "name": suite_name,
            "tests": str(len(cases)),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
        },
    )
    timestamp = _earliest_started_at(result)
    if timestamp is not None:
        suite.set("timestamp", _attribute(timestamp))
    if measured:
        # The sum of what was measured, and only that. A run whose backends
        # reported no duration carries no suite time either, rather than a zero
        # that reads as an instant run.
        suite.set("time", _format_seconds(sum(measured)))
    suite.extend(cases)

    root = ElementTree.Element("testsuites")
    root.append(suite)
    ElementTree.indent(root, space="  ")
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8") + "\n"


def _named_steps(result: JsonObject, plan_steps: Sequence[TestStep]) -> list[tuple[int, str, str]]:
    """Every case the plan asks for, in plan order.

    The plan is the authority when there is one, because it is the only thing
    that knows about the steps after an abort. A refusal that never reached a
    plan has neither, and then the executed records are all there is, which for
    that case is nothing."""
    if plan_steps:
        return [(index, step.route, step.action) for index, step in enumerate(plan_steps, start=1)]
    return [
        (int(record["index"]), str(record.get("route") or "-"), str(record.get("action") or "-"))
        for record in _step_records(result)
        if isinstance(record.get("index"), int)
    ]


def _step_records(result: JsonObject) -> list[JsonObject]:
    steps = result.get("steps")
    return [record for record in steps if isinstance(record, dict)] if isinstance(steps, list) else []


def run_refusal(result: JsonObject) -> JsonObject | None:
    """The refusal that ended the run before any step ran, or None.

    Public because it is the whole of what "the run was refused" means in this
    project's evidence, and two documents assert it: this one's `preflight` case
    and the run summary's `outcome: refused`. One predicate, so the JUnit file
    and the JSON summary of one run cannot disagree about whether anything ran.

    Two shapes reach here and both mean the same thing to a reader: the
    reactor's own `validation_error` from a plan refused at preflight, and a
    `ConfigError` document from a run that never got as far as a plan. Neither
    touched hardware, and the design is explicit that a reader must not mistake
    either for a bench failure.

    A cooperative stop is not a refusal even when it stopped before step 1 and
    so carries no step records: the reactor sets `stopped` on it, the run did
    reach the bench and closed it cleanly, and the design maps it to skipped
    steps with the stop as their reason, not to an invented `preflight` error."""
    if result.get("ok") is True or result.get("stopped") is True or _step_records(result):
        return None
    detail = result.get("validation_error")
    return detail if isinstance(detail, dict) else result


def _skip_message(result: JsonObject, *, refused: bool) -> str:
    if refused:
        return "The plan was refused before any step ran, so no hardware was touched."
    failed_step = result.get("failed_step")
    if isinstance(failed_step, int):
        return f"The run stopped at step {failed_step}; this step never ran."
    if result.get("stopped") is True:
        return f"The run was stopped on request after step {result.get('stopped_after_step', 0)}; this step never ran."
    return "The run ended before reaching this step; it never ran."


def _verdict_case(suite_name: str, name: str, error_type: str, summary: str, body: object) -> ElementTree.Element:
    """A case for a verdict no step owns: the run's cleanup, and the run's audit."""
    case = ElementTree.Element("testcase", {"name": name, "classname": suite_name})
    failure = ElementTree.SubElement(case, "failure", {"type": error_type, "message": _attribute(summary)})
    failure.text = _body(body)
    return case


def _cleanup_summary(result: JsonObject) -> str:
    for record in result.get("cleanup_errors") or ():
        if isinstance(record, dict) and isinstance(record.get("result"), dict):
            summary = record["result"].get("summary")
            if summary:
                return str(summary)
    return "The run's sessions did not all close, so this is not a passing run whatever its steps did."


def _audit_summary(result: JsonObject) -> str:
    error = result.get("audit_error")
    if isinstance(error, dict) and error.get("summary"):
        return str(error["summary"])
    return "The run's audit record could not be written, so this is not a passing run whatever its steps did."


def _elapsed_seconds(step_result: JsonObject) -> float | None:
    """How long the step took, where something measured it.

    `elapsed_ms` is what the debugger backends report and what the design names.
    `elapsed_s` is the block step's own, measured by the reactor around the
    iterations it ran. Everything else has no duration and gets no `time`:
    inventing one would put a number in a test report that nothing measured."""
    for key, divisor in (("elapsed_ms", 1000.0), ("elapsed_s", 1.0)):
        value = step_result.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value >= 0:
            return float(value) / divisor
    return None


def _earliest_started_at(result: JsonObject) -> str | None:
    """When the run first reached the board, read off the run's own records.

    No wall clock is read here. A document is written after the run, and a
    timestamp taken then would say when the file was made rather than when the
    plan ran."""
    stamps = [
        step_result["started_at"]
        for step_result in step_results(_step_records(result))
        if isinstance(step_result.get("started_at"), str) and step_result["started_at"]
    ]
    return min(stamps) if stamps else None


def _format_seconds(seconds: float) -> str:
    return f"{seconds:.3f}"


def _body(value: object) -> str:
    """A JSON body for an element.

    The dump escapes the C0 controls itself, so a backend's raw bytes cannot
    reach the document as characters XML has no way to carry; the same filter
    the attributes get is applied over it anyway, because a lone surrogate is
    not something `json.dumps` escapes and is the one thing that would make the
    file unwritable."""
    return _xml_safe(json.dumps(value, indent=2, default=str, ensure_ascii=False))


def _attribute(value: str) -> str:
    return _xml_safe(value)


def _xml_safe(value: str) -> str:
    """The text with every character XML 1.0 has no way to carry removed."""
    return "".join(
        character
        for character in value
        if ord(character) in _ALLOWED_CONTROL_CHARACTERS or (0x20 <= ord(character) < 0xD800) or (0xE000 <= ord(character) <= 0xFFFD) or ord(character) >= 0x10000
    )
