"""The JUnit XML `agentic-hil test-reactor --junit-xml` leaves behind.

The mapping under test is the one `docs/github-action-design.md` specifies, so
the assertions are about what a CI reader looks at: how many suites and cases
there are, which case failed and whether its own text is in the file, how many
cases were skipped and why. Nothing here asserts on indentation, attribute order
or where a line breaks; a document that says the same thing differently is not a
regression."""
from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree

import pytest
from conftest import write_authoritative_config

from agentic_hil.cli import entrypoint
from agentic_hil.junit import JUNIT_DETACHED_ERROR, JUNIT_WRITE_ERROR, junit_xml_document, write_junit_xml

# Aliased: pytest collects module-level names beginning with `Test`, and the
# plan step dataclass is not a test class.
from agentic_hil.test_reactor import TestStep as PlanStep

PLAN_STEPS = (
    PlanStep(action="flash", arguments={}, device="dut"),
    PlanStep(action="uart_read", arguments={}, device="dut_uart"),
    PlanStep(action="reset", arguments={}, device="dut"),
)


def parsed(document: str) -> ElementTree.Element:
    return ElementTree.fromstring(document)


def suite_of(document: str) -> ElementTree.Element:
    root = parsed(document)
    suites = root.findall("testsuite")
    assert len(suites) == 1
    return suites[0]


def cases_of(document: str) -> dict[str, ElementTree.Element]:
    return {case.get("name", ""): case for case in suite_of(document).findall("testcase")}


def green_result() -> dict:
    return {
        "ok": True,
        "tool": "test_reactor",
        "name": "demo-plan",
        "cleanup_ok": True,
        "audit_ok": True,
        "steps": [
            {"index": 1, "route": "dut", "action": "flash", "result": {"ok": True, "started_at": "2026-08-19T10:00:01.000Z", "elapsed_ms": 1500}},
            {"index": 2, "route": "dut_uart", "action": "uart_read", "result": {"ok": True, "started_at": "2026-08-19T10:00:03.000Z"}},
            {"index": 3, "route": "dut", "action": "reset", "result": {"ok": True, "started_at": "2026-08-19T10:00:04.000Z", "elapsed_ms": 250}},
        ],
    }


def comparator_miss_result() -> dict:
    return {
        "ok": False,
        "tool": "test_reactor",
        "name": "demo-plan",
        "cleanup_ok": True,
        "audit_ok": True,
        "error_type": "comparator_mismatch",
        "step_error_type": "comparator_mismatch",
        "failed_step": 2,
        "steps": [
            {"index": 1, "route": "dut", "action": "flash", "result": {"ok": True, "started_at": "2026-08-19T10:00:01.000Z", "elapsed_ms": 1500}},
            {
                "index": 2,
                "route": "dut_uart",
                "action": "uart_read",
                "result": {
                    "ok": False,
                    "error_type": "comparator_mismatch",
                    "summary": "UART payload did not match the comparator.",
                    "comparator": {"pattern": "temp=(\\d+)C", "range": {"min": 20, "max": 30}},
                    "observed": "temp=91C",
                },
            },
        ],
    }


def preflight_refusal_result() -> dict:
    return {
        "ok": False,
        "tool": "test_reactor",
        "name": "demo-plan",
        "error_type": "test_config_invalid",
        "cleanup_ok": True,
        "audit_ok": True,
        "steps": [],
        "validation_error": {
            "error_type": "test_config_invalid",
            "field": "steps[1].device",
            "route": "dut_uart",
            "action": "uart_read",
            "summary": "This plan reads a serial port the configuration does not allow reads on.",
        },
    }


def test_green_plan_is_one_suite_of_passing_cases_named_by_index_route_and_action() -> None:
    document = junit_xml_document(green_result(), plan_steps=PLAN_STEPS)

    suite = suite_of(document)
    assert suite.get("name") == "demo-plan"
    assert (suite.get("tests"), suite.get("failures"), suite.get("errors"), suite.get("skipped")) == ("3", "0", "0", "0")
    assert list(cases_of(document)) == ["1.dut.flash", "2.dut_uart.uart_read", "3.dut.reset"]
    assert all(len(case) == 0 for case in cases_of(document).values())


def test_suite_time_and_timestamp_come_only_from_what_the_run_measured() -> None:
    document = junit_xml_document(green_result(), plan_steps=PLAN_STEPS)

    suite = suite_of(document)
    # The first step's own start, not the moment the document was written.
    assert suite.get("timestamp") == "2026-08-19T10:00:01.000Z"
    assert suite.get("time") == "1.750"
    cases = cases_of(document)
    assert cases["1.dut.flash"].get("time") == "1.500"
    # The backend reported no duration for the read, so the case carries none
    # rather than a zero that reads as an instant step.
    assert cases["2.dut_uart.uart_read"].get("time") is None


def test_failing_step_carries_its_own_comparator_text_and_the_rest_are_skipped() -> None:
    document = junit_xml_document(comparator_miss_result(), plan_steps=PLAN_STEPS)

    suite = suite_of(document)
    assert (suite.get("tests"), suite.get("failures"), suite.get("errors"), suite.get("skipped")) == ("3", "1", "0", "1")
    cases = cases_of(document)
    failure = cases["2.dut_uart.uart_read"].find("failure")
    assert failure is not None
    assert failure.get("type") == "comparator_mismatch"
    assert failure.get("message") == "UART payload did not match the comparator."
    body = json.loads(failure.text or "{}")
    assert body["result"]["comparator"] == {"pattern": "temp=(\\d+)C", "range": {"min": 20, "max": 30}}
    assert body["result"]["observed"] == "temp=91C"
    assert cases["1.dut.flash"].find("failure") is None
    skipped = cases["3.dut.reset"].find("skipped")
    assert skipped is not None
    assert "step 2" in (skipped.get("message") or "")


def test_steps_after_the_abort_need_the_plan_and_are_lost_without_it() -> None:
    # The report records executed steps only, which is the whole reason the
    # writer is handed the loaded plan: without it there is no honest way to
    # say a third step existed at all.
    with_plan = suite_of(junit_xml_document(comparator_miss_result(), plan_steps=PLAN_STEPS))
    without_plan = suite_of(junit_xml_document(comparator_miss_result()))

    assert (with_plan.get("tests"), with_plan.get("skipped")) == ("3", "1")
    assert (without_plan.get("tests"), without_plan.get("skipped")) == ("2", "0")


def test_stopped_run_says_the_stop_is_why_the_rest_never_ran() -> None:
    stopped = {
        **green_result(),
        "ok": False,
        "error_type": "run_stopped",
        "stopped": True,
        "stopped_after_step": 1,
        "steps": green_result()["steps"][:1],
    }

    document = junit_xml_document(stopped, plan_steps=PLAN_STEPS)

    suite = suite_of(document)
    # A stop is not a failure in this product: the devices closed cleanly and no
    # step reported anything wrong, so nothing is invented to make it look red.
    assert (suite.get("failures"), suite.get("errors"), suite.get("skipped")) == ("0", "0", "2")
    message = cases_of(document)["2.dut_uart.uart_read"].find("skipped").get("message") or ""
    assert "stopped on request after step 1" in message


def test_preflight_refusal_skips_every_step_and_carries_the_refusal_as_an_error() -> None:
    document = junit_xml_document(preflight_refusal_result(), plan_steps=PLAN_STEPS)

    suite = suite_of(document)
    assert (suite.get("tests"), suite.get("failures"), suite.get("errors"), suite.get("skipped")) == ("4", "0", "1", "3")
    cases = cases_of(document)
    error = cases["preflight"].find("error")
    assert error is not None
    assert error.get("type") == "test_config_invalid"
    body = json.loads(error.text or "{}")
    assert (body["field"], body["route"], body["action"]) == ("steps[1].device", "dut_uart", "uart_read")
    for name in ("1.dut.flash", "2.dut_uart.uart_read", "3.dut.reset"):
        assert "refused before any step ran" in (cases[name].find("skipped").get("message") or "")


def test_a_run_refused_the_bench_skips_its_steps_without_a_validation_error() -> None:
    # A device another run holds is refused before the first step, and that
    # refusal carries no `validation_error`: the plan is fine, the bench is busy.
    # The result itself is the refusal then, and the plan's steps are still
    # skipped rather than absent, because a reader has to see what did not run.
    refused = {
        "ok": False,
        "tool": "test_reactor",
        "name": "demo-plan",
        "error_type": "device_busy",
        "summary": "A device this plan declares is unavailable. No step ran.",
        "steps": [],
        "cleanup": [],
        "cleanup_ok": True,
        "declared_devices": ["probe:0673FF", "com:COM7"],
    }

    document = junit_xml_document(refused, plan_steps=PLAN_STEPS)

    suite = suite_of(document)
    assert (suite.get("tests"), suite.get("failures"), suite.get("errors"), suite.get("skipped")) == ("4", "0", "1", "3")
    assert cases_of(document)["preflight"].find("error").get("type") == "device_busy"


def test_a_run_that_never_reached_a_plan_still_produces_one_error_case() -> None:
    refusal = {
        "ok": False,
        "tool": "test_reactor",
        "error_type": "config_invalid",
        "summary": "No Agentic HIL configuration was found for this workspace.",
    }

    document = junit_xml_document(refusal)

    suite = suite_of(document)
    assert (suite.get("tests"), suite.get("errors"), suite.get("skipped")) == ("1", "1", "0")
    error = cases_of(document)["preflight"].find("error")
    assert error.get("type") == "config_invalid"
    assert error.get("message") == "No Agentic HIL configuration was found for this workspace."


def test_a_block_step_that_failed_carries_the_iteration_that_failed_in_it() -> None:
    # A `repeat` block is one top-level case, and the nested step that actually
    # failed lives in the record's iterations. The body carries them, so a reader
    # does not have to go back to the JSON report for the one thing they opened
    # this file to find.
    plan = (PLAN_STEPS[0], PlanStep(action="repeat", arguments={"count": 3}, steps=(PLAN_STEPS[1],)))
    result = {
        "ok": False,
        "tool": "test_reactor",
        "name": "demo-plan",
        "cleanup_ok": True,
        "audit_ok": True,
        "error_type": "comparator_mismatch",
        "failed_step": 2,
        "steps": [
            {"index": 1, "route": "dut", "action": "flash", "result": {"ok": True, "elapsed_ms": 1500}},
            {
                "index": 2,
                "route": "-",
                "action": "repeat",
                "iterations": [
                    {"iteration": 1, "steps": [{"index": 1, "route": "dut_uart", "action": "uart_read", "result": {"ok": True}}]},
                    {"iteration": 2, "steps": [{"index": 1, "route": "dut_uart", "action": "uart_read", "result": {"ok": False, "error_type": "comparator_mismatch", "summary": "The window was missed."}}]},
                ],
                "result": {
                    "ok": False,
                    "error_type": "comparator_mismatch",
                    "summary": "A step failed on iteration 2 of this repeat block, so the run stopped there.",
                    "elapsed_s": 4.25,
                    "failed_iteration": 2,
                },
            },
        ],
    }

    document = junit_xml_document(result, plan_steps=plan)

    case = cases_of(document)["2.-.repeat"]
    assert case.get("time") == "4.250"
    body = json.loads(case.find("failure").text or "{}")
    assert body["iterations"][1]["steps"][0]["result"]["summary"] == "The window was missed."


def test_cleanup_and_audit_failures_each_add_their_own_case() -> None:
    result = {
        **green_result(),
        "ok": False,
        "cleanup_ok": False,
        "error_type": "cleanup_failed",
        "cleanup_errors": [{"device": "dut_uart", "action": "close", "result": {"ok": False, "summary": "The serial session did not close."}}],
        "audit_ok": False,
        "audit_error": {"error_type": "unsafe_configured_path", "summary": "The audit record could not be written."},
    }

    document = junit_xml_document(result, plan_steps=PLAN_STEPS)

    suite = suite_of(document)
    assert (suite.get("tests"), suite.get("failures")) == ("5", "2")
    cases = cases_of(document)
    assert cases["cleanup"].find("failure").get("message") == "The serial session did not close."
    assert cases["audit"].find("failure").get("message") == "The audit record could not be written."


def test_document_is_utf8_and_parses_from_the_bytes_on_disk(tmp_path: Path) -> None:
    result = comparator_miss_result()
    result["steps"][1]["result"]["observed"] = "temperatur 91°C ✓"
    target = tmp_path / "made" / "up" / "junit.xml"

    written = write_junit_xml(str(target), result, plan_steps=PLAN_STEPS)

    assert Path(written) == target
    assert target.read_bytes().startswith(b"<?xml version='1.0' encoding='utf-8'?>")
    tree = ElementTree.parse(target)
    failure = tree.getroot().find("testsuite/testcase[@name='2.dut_uart.uart_read']/failure")
    assert "91°C" in (failure.text or "")


def test_control_characters_a_backend_emitted_cannot_break_the_document(tmp_path: Path) -> None:
    result = comparator_miss_result()
    result["steps"][1]["result"]["summary"] = "Comparator\x07 missed\x00 the window."
    target = tmp_path / "junit.xml"

    write_junit_xml(str(target), result, plan_steps=PLAN_STEPS)

    failure = ElementTree.parse(target).getroot().find("testsuite/testcase[@name='2.dut_uart.uart_read']/failure")
    assert failure.get("message") == "Comparator missed the window."


def write_plan(workspace: Path) -> Path:
    path = workspace / ".agentic-hil" / "testconfig.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "version: 2\nsteps:\n"
        "  - {debugger: dut, action: flash, image_path: build/app.elf}\n"
        "  - {debugger: dut, action: reset}\n",
        encoding="utf-8",
    )
    return path


def test_cli_writes_the_file_and_leaves_the_json_and_the_exit_code_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_authoritative_config(tmp_path, monkeypatch)
    plan_path = write_plan(tmp_path)
    monkeypatch.chdir(tmp_path)
    ran = {
        "ok": True,
        "tool": "test_reactor",
        "name": "testconfig",
        "cleanup_ok": True,
        "steps": [
            {"index": 1, "route": "dut", "action": "flash", "result": {"ok": True, "started_at": "2026-08-19T10:00:01.000Z", "elapsed_ms": 900}},
            {"index": 2, "route": "dut", "action": "reset", "result": {"ok": True, "started_at": "2026-08-19T10:00:02.000Z", "elapsed_ms": 90}},
        ],
    }
    monkeypatch.setattr("agentic_hil.reactorrun.run_registered_plan", lambda *_args, **_kwargs: dict(ran))
    target = tmp_path / "artifacts" / "junit.xml"

    exit_code = entrypoint(["test-reactor", "--test-config", str(plan_path), "--junit-xml", str(target), "--json"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["ok"] is True
    assert result["steps"] == ran["steps"]
    assert result["junit_xml"] == str(target)
    suite = ElementTree.parse(target).getroot().find("testsuite")
    assert (suite.get("tests"), suite.get("failures"), suite.get("skipped")) == ("2", "0", "0")
    assert [case.get("name") for case in suite.findall("testcase")] == ["1.dut.flash", "2.dut.reset"]


def test_cli_writes_the_file_for_a_run_that_was_refused_before_a_plan_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The artifact matters most when the run is red, and a plan file that is not
    # there is as red to a CI job as a plan that failed on the board.
    write_authoritative_config(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "artifacts" / "junit.xml"

    exit_code = entrypoint(["test-reactor", "--test-config", "plans/absent.yaml", "--junit-xml", str(target), "--json"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    error = ElementTree.parse(target).getroot().find("testsuite/testcase[@name='preflight']/error")
    assert error is not None
    assert error.get("type") == "test_config_not_found"


def test_cli_writes_the_file_for_a_bench_that_has_no_configuration_at_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The refusal that lands before a configuration exists is the one refusal the
    # run itself never sees, and a CI job needs an artifact for it too.
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty-user-config"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-user-config"))
    monkeypatch.delenv("AGENTIC_HIL_CONFIG", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    target = tmp_path / "artifacts" / "junit.xml"

    exit_code = entrypoint(["test-reactor", "--junit-xml", str(target), "--json"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is not True
    suite = ElementTree.parse(target).getroot().find("testsuite")
    assert (suite.get("tests"), suite.get("errors")) == ("1", "1")
    assert suite.find("testcase[@name='preflight']/error") is not None


def test_a_refusal_is_still_the_answer_when_its_artifact_cannot_be_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The operator has to read the refusal, so a second failure raised out of the
    # artifact write must not replace it with a story about a file path.
    write_authoritative_config(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("agentic_hil.junit.write_junit_xml", _refusing_write)

    exit_code = entrypoint(["test-reactor", "--test-config", "plans/absent.yaml", "--junit-xml", str(tmp_path / "junit.xml"), "--json"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == "test_config_not_found"


def test_detach_refuses_the_flag_by_name_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_authoritative_config(tmp_path, monkeypatch)
    plan_path = write_plan(tmp_path)
    monkeypatch.chdir(tmp_path)
    started: list[object] = []
    monkeypatch.setattr("agentic_hil.cli.start_plan_detached", lambda *args, **kwargs: started.append((args, kwargs)) or {"ok": True})
    target = tmp_path / "junit.xml"

    exit_code = entrypoint(["test-reactor", "--detach", "--test-config", str(plan_path), "--junit-xml", str(target), "--json"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["error_type"] == JUNIT_DETACHED_ERROR
    assert result["side_effect_committed"] is False
    assert "test-reactor-status" in result["summary"]
    # Refused before the worker exists, so nothing was started and no half
    # document was left for a CI job to believe.
    assert started == []
    assert not target.exists()


def test_detach_without_the_flag_is_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_authoritative_config(tmp_path, monkeypatch)
    plan_path = write_plan(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("agentic_hil.cli.start_plan_detached", lambda *_args, **_kwargs: {"ok": True, "run": "run-1234"})

    exit_code = entrypoint(["test-reactor", "--detach", "--test-config", str(plan_path), "--json"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["run"] == "run-1234"


def test_a_file_that_cannot_be_written_is_named_rather_than_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_authoritative_config(tmp_path, monkeypatch)
    plan_path = write_plan(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("agentic_hil.reactorrun.run_registered_plan", lambda *_args, **_kwargs: {"ok": True, "tool": "test_reactor", "name": "testconfig", "cleanup_ok": True, "steps": []})
    monkeypatch.setattr("agentic_hil.junit.write_junit_xml", _refusing_write)

    exit_code = entrypoint(["test-reactor", "--test-config", str(plan_path), "--junit-xml", str(tmp_path / "junit.xml"), "--json"])

    result = json.loads(capsys.readouterr().out)
    # The run happened and its own report stands; the missing artifact is what
    # pulls the verdict down, because an operator who asked for one and got none
    # did not get the command they typed.
    assert exit_code == 1
    assert result["ok"] is False
    assert result["error_type"] == JUNIT_WRITE_ERROR
    assert result["junit_xml_error"]["exception_type"] == "OSError"


def _refusing_write(*_args, **_kwargs) -> str:
    raise OSError("read-only file system")
