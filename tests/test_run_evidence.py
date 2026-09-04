"""The CI evidence `agentic-hil run-evidence` writes out of one run report.

The three outputs under test are the ones `docs/github-action-design.md`
specifies: `run-summary.json` in the shape that document gives, a job summary in
Markdown, and a copy of the workspace event logs the report names. The
assertions are about what a reviewer with no access to the bench can and cannot
read: which fields are there, which are absent because nothing supplied them,
and which identities are absent because this command may not publish them.

Nothing here asserts on the Markdown's exact wording or on where a table breaks;
a document that says the same thing differently is not a regression. What is
pinned is every fact the design names and every value the design excludes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FAKE_OPENOCD, write_authoritative_config

from agentic_hil import runevidence
from agentic_hil.cli import entrypoint
from agentic_hil.config import ConfigError
from agentic_hil.runevidence import WITHHELD, write_run_evidence

# The identities the design keeps out of a public summary, planted in a report
# so a test can assert on each one by name. Realistic spellings, because the
# sweep is a substring sweep and a value like "x" would prove nothing.
PROBE_SERIAL = "0673FF485155878281141442"
OPENOCD_EXECUTABLE = "C:\\tools\\openocd-0.12.0\\bin\\openocd.exe"
CONFIG_PATH = "C:\\Users\\operator\\AppData\\Roaming\\agentic-hil\\config.yaml"
FOREIGN_LOG = "/home/runner/other-workspace/.agentic-hil/logs/com-20260901-090000-dut_uart.jsonl"
LOCK_KEYS = [f"probe:{PROBE_SERIAL}", "com:COM7", "can:pcan:PCAN_USBBUS1"]

# The credentials, which are not identities: nothing in the report says these
# are secret, so only the redaction the other two sinks apply finds them. One of
# each shape it knows, spelled the way a tool prints them.
INDEX_PASSWORD = "s3cr3t-index-password"
INDEX_URL = f"https://ci-bot:{INDEX_PASSWORD}@packages.example.com/simple/"
BEARER_TOKEN = "gh0_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
BEARER_HEADER = f"Authorization: Bearer {BEARER_TOKEN}"
STEP_TOKEN = "gh0_Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"

PLAN_TEXT = (
    "version: 3\n"
    "name: nucleo-f446re-hello-world\n"
    "steps:\n"
    "  - {device: dut, action: flash, image_path: build/app.elf}\n"
    "  - {device: dut_uart, action: uart_open}\n"
    "  - {device: dut_uart, action: uart_read}\n"
    "  - {device: dut_can, action: can_open}\n"
)

UART_LOG = ".agentic-hil/logs/com-20260819-100003-dut_uart.jsonl"
OPENOCD_LOG = ".agentic-hil/logs/openocd-20260819-100001-flash_firmware.log"


def write_plan(workspace: Path, text: str = PLAN_TEXT) -> Path:
    path = workspace / "testconfig.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def write_logs(workspace: Path) -> None:
    logs = workspace / ".agentic-hil" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / Path(UART_LOG).name).write_text('{"event": "start", "port_id": "dut_uart"}\n', encoding="utf-8")
    (logs / Path(OPENOCD_LOG).name).write_text("Open On-Chip Debugger 0.12.0\n", encoding="utf-8")


def config_in_force() -> dict:
    return {
        "path": CONFIG_PATH,
        "digest_algorithm": "sha256:",
        "digest": "5f2c0f8ba2f6a2f7f5f6d4c3b2a1908877665544332211009988776655443322",
        "description_source": "config",
        "file_state": "unchanged",
        "diverged_from_file": False,
        "checked_at": "2026-08-19T10:00:06.000Z",
    }


def green_report(workspace: Path) -> dict:
    """A passing run, in the shape `write_report` commits one."""
    return {
        "ok": True,
        "tool": "test_reactor",
        "name": "nucleo-f446re-hello-world",
        "test_config_path": str(write_plan(workspace)),
        "run": "run-3f9c2a1b4e6d8071",
        "cleanup_ok": True,
        "cleanup_errors": [],
        "audit_ok": True,
        "declared_devices": list(LOCK_KEYS),
        "steps": [
            {
                "index": 1,
                "route": "dut",
                "action": "flash",
                "result": {
                    "ok": True,
                    "tool": "flash_firmware",
                    "backend": "openocd",
                    "started_at": "2026-08-19T10:00:01.000Z",
                    "elapsed_ms": 1500,
                    "executable": OPENOCD_EXECUTABLE,
                    "probe_id": PROBE_SERIAL,
                    "log_path": OPENOCD_LOG,
                },
            },
            {"index": 2, "route": "dut_uart", "action": "uart_open", "result": {"ok": True, "tool": "com_port_open", "log_path": UART_LOG}},
            {"index": 3, "route": "dut_uart", "action": "uart_read", "result": {"ok": True, "tool": "com_port_read", "elapsed_ms": 40, "log_path": UART_LOG}},
        ],
        "summary": "Test reactor sequence completed.",
        "config_in_force": config_in_force(),
        "target": {"name": "nucleo-f446re", "controller": "stm32f446re"},
    }


def failed_report(workspace: Path) -> dict:
    report = green_report(workspace)
    report["ok"] = False
    report["error_type"] = "uart_expect_timeout"
    report["step_error_type"] = "uart_expect_timeout"
    report["failed_step"] = 3
    report["steps"][2]["result"] = {
        "ok": False,
        "tool": "com_port_read",
        "error_type": "uart_expect_timeout",
        "summary": f"The board sent nothing within 5000 ms; the probe {PROBE_SERIAL} was driven by {OPENOCD_EXECUTABLE}.",
        "log_path": UART_LOG,
    }
    report["summary"] = "Test reactor sequence failed."
    report["recovery"] = {
        "attempted": True,
        "outcome": "recovered",
        "actions": [{"action": "reset_target", "ok": True}],
        "devices": ["dut"],
        "auto_recover_policy": "on_failure",
        "auto_recover_policy_source": "config",
        "summary": "The board was reset into a halted state and the probe answered.",
    }
    return report


def refused_report(workspace: Path) -> dict:
    """A run refused before its first step, the shape `run_registered_plan` writes."""
    return {
        "ok": False,
        "tool": "test_reactor",
        "name": "nucleo-f446re-hello-world",
        "test_config_path": str(write_plan(workspace)),
        "error_type": "device_busy",
        "summary": "A device this plan declares is unavailable. No step ran.",
        "resource": LOCK_KEYS[0],
        "steps": [],
        "cleanup": [],
        "cleanup_ok": True,
        "declared_devices": list(LOCK_KEYS),
        "config_in_force": config_in_force(),
        "run": "run-3f9c2a1b4e6d8071",
    }


def evidence(workspace: Path, report: dict, *, environ: dict | None = None, out: str = "evidence") -> tuple[dict, dict, str]:
    """Write the evidence for `report` and answer with (result, run summary, job summary)."""
    report_path = workspace / "report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    result = write_run_evidence(str(report_path), str(workspace / out), workspace=workspace, environ=environ or {})
    summary = json.loads((workspace / out / "run-summary.json").read_text(encoding="utf-8"))
    return result, summary, (workspace / out / "job-summary.md").read_text(encoding="utf-8")


def outputs(workspace: Path, out: str = "evidence") -> str:
    """Everything the command wrote itself, as one string, for an absence assertion.

    The two documents it derives, and the names of the log files it collected.
    Not the contents of those logs: they are the run's own record, copied byte
    for byte, and a redacted log is not evidence of anything (see
    `test_a_collected_log_is_the_run_s_own_bytes_and_is_never_rewritten`). The
    exclusions govern what this command writes, which is these two documents and
    the names beside them."""
    root = workspace / out
    files = [root / "run-summary.json", root / "job-summary.md"]
    return "\n".join(path.read_text(encoding="utf-8") for path in files) + "\n" + "\n".join(path.name for path in sorted((root / "logs").iterdir()))


# ---------------------------------------------------------------------------
# The shape, on a green, a failed and a refused report.


def test_a_green_report_produces_the_summary_shape_the_design_gives(tmp_path: Path) -> None:
    write_logs(tmp_path)

    _result, summary, _document = evidence(
        tmp_path,
        green_report(tmp_path),
        environ={"GITHUB_REPOSITORY": "acme/firmware", "GITHUB_SHA": "a" * 40, "GITHUB_REF": "refs/heads/main", "RUNNER_NAME": "bench-01", "RUNNER_LABELS": "self-hosted, agentic-hil"},
    )

    assert summary["outcome"] == "success"
    assert summary["plan"]["name"] == "nucleo-f446re-hello-world"
    assert summary["plan"]["path"] == "testconfig.yaml"
    assert len(summary["plan"]["sha256"]) == 64
    assert summary["firmware"] == {"repository": "acme/firmware", "commit": "a" * 40, "ref": "refs/heads/main"}
    assert summary["tools"]["agentic_hil"] and summary["tools"]["python"]
    assert summary["bench"]["config_digest"] == config_in_force()["digest"]
    assert summary["bench"]["digest_algorithm"] == "sha256:"
    assert summary["bench"]["diverged_from_file"] is False
    assert summary["bench"]["runner"] == {"name": "bench-01", "labels": ["self-hosted", "agentic-hil"]}
    assert summary["bench"]["target"] == {"name": "nucleo-f446re", "controller": "stm32f446re"}
    # From the plan in the workspace, so the bus the run never reached is named
    # too, and every name is the logical one the plan gave it.
    assert summary["bench"]["devices"] == {"debuggers": ["dut"], "com_ports": ["dut_uart"], "can_buses": ["dut_can"]}
    assert summary["run"] == {"cleanup_ok": True, "audit_ok": True}


def test_a_failed_report_carries_the_failing_step_its_error_type_and_the_recovery(tmp_path: Path) -> None:
    write_logs(tmp_path)

    _result, summary, document = evidence(tmp_path, failed_report(tmp_path))

    assert summary["outcome"] == "failure"
    assert summary["run"]["failed_step"] == 3
    assert summary["run"]["error_type"] == "uart_expect_timeout"
    assert summary["run"]["recovery"] == {"attempted": True, "outcome": "recovered", "auto_recover_policy": "on_failure"}
    # The design asks the job summary for the executed steps and the failing
    # step's own words, and both are there.
    assert "| 1 | dut | flash | pass | 1500 |" in document
    assert "| 3 | dut_uart | uart_read | fail |" in document
    assert "uart_expect_timeout" in document
    assert "The board sent nothing within 5000 ms" in document
    assert "recovered" in document


def test_a_refused_report_produces_the_same_three_outputs_with_outcome_refused(tmp_path: Path) -> None:
    write_logs(tmp_path)

    result, summary, document = evidence(tmp_path, refused_report(tmp_path))

    # The whole point: a workflow's evidence step never has to special-case it.
    assert result["ok"] is True
    assert summary["outcome"] == "refused"
    assert summary["run"]["error_type"] == "device_busy"
    assert "No step ran." in document
    assert "device_busy" in document
    for name in ("run-summary.json", "job-summary.md"):
        assert (tmp_path / "evidence" / name).is_file()
    assert (tmp_path / "evidence" / "logs").is_dir()
    # The plan never ran, so its device names exist only in the plan file, and
    # that is exactly where a refused run's summary has to read them from.
    assert summary["bench"]["devices"] == {"debuggers": ["dut"], "com_ports": ["dut_uart"], "can_buses": ["dut_can"]}


def test_a_refused_plan_after_a_green_one_gets_its_own_evidence(tmp_path: Path) -> None:
    """The CI loop's central promise, as a regression on the report each plan feeds.

    A plan refused before its first step, or refused for having no configuration
    at all, writes nothing to the shared `.agentic-hil/reports/last-report.json`,
    so a loop that fed that file to `run-evidence` after the refusal published
    the *previous* plan's report under the refused plan's name. Both examples now
    capture each run's `--json` report into its own file and build the evidence
    from that, so the two bundles are modelled here as two separate report files:
    a green nominal run, then a diagnostic plan refused at preflight, the refusal
    document `test-reactor --json` emits and which now names the plan it was
    about. The refused plan's bundle must be its own refusal, and nothing of the
    green run may reach it.
    """
    write_logs(tmp_path)

    nominal = tmp_path / "nominal.testconfig.yaml"
    nominal.write_text(PLAN_TEXT.replace("nucleo-f446re-hello-world", "nucleo-f446re-nominal"), encoding="utf-8")
    green = green_report(tmp_path)
    green["name"] = "nucleo-f446re-nominal"
    green["test_config_path"] = str(nominal)

    diagnostic = tmp_path / "diagnostic.testconfig.yaml"
    diagnostic.write_text(PLAN_TEXT.replace("nucleo-f446re-hello-world", "nucleo-f446re-diagnostic"), encoding="utf-8")
    # The document a bench with no configuration answers with, captured verbatim:
    # no report was ever written to disk, but `--json` carries it and names the
    # plan, which is what lets the bundle be the diagnostic plan's own.
    refusal = {
        "ok": False,
        "tool": "test_reactor",
        "error_type": "config_file_not_found",
        "summary": "No Agentic HIL configuration was found for this workspace.",
        "test_config_path": "diagnostic.testconfig.yaml",
    }

    _green_result, green_summary, _green_doc = evidence(tmp_path, green, out="evidence/nominal")
    refused_result, refused_summary, refused_doc = evidence(tmp_path, refusal, out="evidence/diagnostic")

    # The nominal plan's bundle is the green run's.
    assert green_summary["outcome"] == "success"
    assert green_summary["plan"]["name"] == "nucleo-f446re-nominal"

    # The refused plan's bundle is its own refusal and names its own plan.
    assert refused_result["ok"] is True
    assert refused_summary["outcome"] == "refused"
    assert refused_summary["plan"]["path"] == "diagnostic.testconfig.yaml"
    assert refused_summary["plan"]["name"] == "nucleo-f446re-diagnostic"
    assert "config_file_not_found" in refused_doc
    # Nothing of the green run leaked into it: not its outcome, not its name.
    assert refused_summary["plan"]["name"] != green_summary["plan"]["name"]
    assert "nucleo-f446re-nominal" not in refused_doc
    assert "success" not in refused_summary["outcome"]


def test_a_stopped_run_is_a_failure_and_not_a_refusal(tmp_path: Path) -> None:
    # A stop reached the bench and closed it cleanly, so it must not be read as
    # a run that never started; the JUnit mapping draws the same line.
    report = green_report(tmp_path)
    report.update({"ok": False, "error_type": "run_stopped", "stopped": True, "stopped_after_step": 1, "steps": report["steps"][:1]})

    _result, summary, _document = evidence(tmp_path, report)

    assert summary["outcome"] == "failure"


# ---------------------------------------------------------------------------
# The exclusions.


def test_every_excluded_value_planted_in_a_report_is_absent_from_all_three_outputs(tmp_path: Path) -> None:
    write_logs(tmp_path)
    report = failed_report(tmp_path)
    # One of each, in the places a report really carries them.
    report["steps"][1]["result"]["log_path"] = UART_LOG
    report["steps"][0]["result"]["executable_path"] = OPENOCD_EXECUTABLE
    report["canonical_report_path"] = "C:\\Users\\operator\\AppData\\Roaming\\agentic-hil\\state\\reports\\run-3f9c.json"

    _result, _summary, _document = evidence(tmp_path, report)

    published = outputs(tmp_path)
    assert PROBE_SERIAL not in published
    assert OPENOCD_EXECUTABLE not in published
    assert CONFIG_PATH not in published
    assert report["canonical_report_path"] not in published
    for key in LOCK_KEYS:
        assert key not in published
    # Redacted where they stood, not deleted with the sentence around them: the
    # decisive line is still readable.
    document = (tmp_path / "evidence" / "job-summary.md").read_text(encoding="utf-8")
    assert "The board sent nothing within 5000 ms" in document
    assert WITHHELD in document


def test_a_credential_planted_in_a_report_is_masked_in_both_documents(tmp_path: Path) -> None:
    """The three credential shapes, in the places a report really carries them.

    An identity is a value the report names as one; a credential is not, and no
    field list would find it, which is why it takes the redaction the other two
    sinks apply rather than the sweep above. The masking is what a reviewer can
    check: the account, the host and the header's scheme word survive, and the
    sentence they sit in is still the sentence the backend wrote.

    `run-summary.json` is a copy-by-name mapping and carries no prose at all, so
    its assertion here is an absence: the credential has no field to arrive
    under, and this pins that adding one would not change that.
    """
    write_logs(tmp_path)
    report = failed_report(tmp_path)
    report["steps"][2]["result"]["token"] = STEP_TOKEN
    report["steps"][2]["result"]["summary"] = (
        f"The board sent nothing within 5000 ms; the image was fetched from {INDEX_URL} with {BEARER_HEADER}, token={STEP_TOKEN}"
    )
    report["recovery"]["summary"] = f"The board was reset after re-fetching from {INDEX_URL}"

    _result, summary, document = evidence(tmp_path, report)

    published = outputs(tmp_path)
    assert INDEX_PASSWORD not in published
    assert BEARER_TOKEN not in published
    assert STEP_TOKEN not in published
    assert INDEX_PASSWORD not in json.dumps(summary)
    assert BEARER_TOKEN not in json.dumps(summary)
    assert STEP_TOKEN not in json.dumps(summary)
    # Only the credential span goes. What is left is what the sentence was for.
    assert "The board sent nothing within 5000 ms" in document
    assert "ci-bot:[redacted]@packages.example.com" in document
    assert "Authorization: Bearer [redacted]" in document
    assert "token=[redacted]" in document
    # Every free-text field of the two documents, not just the failing step's.
    assert "The board was reset after re-fetching from" in document
    assert document.count("ci-bot:[redacted]@packages.example.com") == 2


def test_a_busy_device_is_named_by_its_logical_name_and_never_by_its_lock_key(tmp_path: Path) -> None:
    write_logs(tmp_path)

    _result, summary, document = evidence(tmp_path, refused_report(tmp_path))

    published = outputs(tmp_path)
    for key in LOCK_KEYS:
        assert key not in published
    assert PROBE_SERIAL not in published
    assert summary["bench"]["devices"]["com_ports"] == ["dut_uart"]
    # Each group named the way a person spells it, not the way the JSON key does.
    assert "| COM ports | `dut_uart` |" in document
    assert "| CAN buses | `dut_can` |" in document


def test_an_absolute_path_from_outside_the_workspace_is_withheld_and_one_inside_is_relative(tmp_path: Path) -> None:
    write_logs(tmp_path)
    report = failed_report(tmp_path)
    inside = (tmp_path / "build" / "app.elf").as_posix()
    report["steps"][2]["result"]["summary"] = f"Flashed {inside} after reading {FOREIGN_LOG}."

    _result, _summary, document = evidence(tmp_path, report)

    assert FOREIGN_LOG not in document
    assert str(tmp_path) not in document
    assert "build/app.elf" in document
    assert WITHHELD in document


def test_an_absolute_path_with_spaces_is_withheld_whole_in_either_flavour(tmp_path: Path) -> None:
    """A path is swept by its value, not only by its shape.

    Both matter, and this is the case that proves the first: a path with a space
    in it is not one token, so a sweep that only recognised the shape would
    withhold `C:\\Program` and publish the rest of somebody's home directory.
    Both flavours are planted, because a report travels between platforms and a
    `WindowsPath` says a POSIX path is relative."""
    windows_path = "C:\\Program Files\\Open On-Chip Debugger\\logs\\openocd-20260901-090000-flash.log"
    posix_path = "/home/build agent/other workspace/.agentic-hil/logs/com-20260901-090000-dut_uart.jsonl"
    report = failed_report(tmp_path)
    # Under `log_path`, not under `executable`: a field the design excludes by
    # name would be swept whatever its shape, and the rule under test here is
    # the one about the shape.
    report["steps"][0]["result"]["log_path"] = windows_path
    report["steps"][1]["result"]["log_path"] = posix_path
    report["steps"][2]["result"]["summary"] = f"The board sent nothing; {windows_path} was written beside {posix_path}."

    _result, _summary, _document = evidence(tmp_path, report)

    published = outputs(tmp_path)
    for planted in (windows_path, posix_path):
        assert planted not in published
    # Not a fragment of one either: a sweep that stopped at the first space
    # would leave the directory the rest of the path sits under in plain view.
    for fragment in ("Program Files", "Open On-Chip Debugger", "build agent", "other workspace"):
        assert fragment not in published


def test_the_configuration_path_is_excluded_by_name_and_not_only_by_being_elsewhere(tmp_path: Path) -> None:
    # Normally it is an absolute path from outside the workspace and the path
    # sweep would have caught it anyway. The design names it, so it is excluded
    # by name, and a path that happens to sit inside the workspace proves which
    # of the two rules is doing the work.
    report = failed_report(tmp_path)
    planted = (tmp_path / "committed-config.yaml").as_posix()
    report["config_in_force"] = {**config_in_force(), "path": planted}
    # In a sentence as well as in the field, because the field is never copied:
    # what the rule has to stop is the backend that named the file in its own
    # summary, where relativising it would publish it under a shorter name.
    report["steps"][2]["result"]["summary"] = f"The step was judged by {planted} and the board sent nothing."

    _result, _summary, _document = evidence(tmp_path, report)

    published = outputs(tmp_path)
    assert planted not in published
    assert "committed-config.yaml" not in published


def test_a_slash_inside_prose_is_not_mistaken_for_a_path(tmp_path: Path) -> None:
    # The sweep looks for a path and must not eat a sentence: a comparator's own
    # text is the run's evidence, and a summary that mangled it would be worse
    # than one that published nothing. Three shapes that are not paths: a token
    # with one slash, a token with two, and a bare unit after a space.
    report = failed_report(tmp_path)
    report["steps"][2]["result"]["summary"] = "The reading was 20 /min, and/or outside the pass/fail/error window."

    _result, _summary, document = evidence(tmp_path, report)

    assert "The reading was 20 /min, and/or outside the pass/fail/error window." in document


# ---------------------------------------------------------------------------
# The event logs.


def test_the_workspace_event_logs_the_report_names_are_copied_once_each(tmp_path: Path) -> None:
    write_logs(tmp_path)

    result, _summary, _document = evidence(tmp_path, green_report(tmp_path))

    collected = sorted(path.name for path in (tmp_path / "evidence" / "logs").iterdir())
    # Two files for three `log_path` mentions: the serial session is named by
    # both of its steps and is one file.
    assert collected == sorted({Path(OPENOCD_LOG).name, Path(UART_LOG).name})
    assert result["logs_copied"] == collected
    assert (tmp_path / "evidence" / "logs" / Path(UART_LOG).name).read_text(encoding="utf-8") == '{"event": "start", "port_id": "dut_uart"}\n'


def test_a_collected_log_is_the_run_s_own_bytes_and_is_never_rewritten(tmp_path: Path) -> None:
    """The two documents are swept; a log is copied, and the difference is the point.

    A log is what the run recorded, and the trusted hash-chained copy of it stays
    under `state_root` on the runner precisely so this mirror can be checked
    against it. A mirror this command had edited could never be checked against
    anything, so a device path a backend wrote into its own command line stays in
    the log and stays out of the summary, which is where a reader who is not
    meant to have it would find it."""
    write_logs(tmp_path)
    original = tmp_path / ".agentic-hil" / "logs" / Path(OPENOCD_LOG).name
    original.write_text(f"Running {OPENOCD_EXECUTABLE} against probe {PROBE_SERIAL}\n", encoding="utf-8")
    report = failed_report(tmp_path)

    _result, _summary, _document = evidence(tmp_path, report)

    collected = tmp_path / "evidence" / "logs" / original.name
    assert collected.read_bytes() == original.read_bytes()
    assert PROBE_SERIAL not in outputs(tmp_path)


def test_a_log_the_report_names_and_this_workspace_does_not_have_is_named_not_invented(tmp_path: Path) -> None:
    # The plain case of a report written somewhere else: the paths are the same
    # shape and the files are not here.
    report = green_report(tmp_path)

    result, _summary, _document = evidence(tmp_path, report)

    assert result["logs_copied"] == []
    assert result["logs_missing"] == sorted({OPENOCD_LOG, UART_LOG})
    assert "are not in this workspace" in result["summary"]


def test_a_plan_from_another_workspace_leaves_the_plan_path_and_digest_absent(tmp_path: Path) -> None:
    # The whole of how a foreign report is handled: it is served, and every
    # field that would have needed a file this workspace does not have is
    # absent. The plan's absolute path is an identity, so it is not published
    # under `plan.path` either, and there is nothing to digest.
    write_logs(tmp_path)
    report = green_report(tmp_path)
    report["test_config_path"] = "/home/runner/other-workspace/testconfig.yaml"

    _result, summary, _document = evidence(tmp_path, report)

    assert "path" not in summary["plan"]
    assert "sha256" not in summary["plan"]
    assert summary["plan"]["name"] == "nucleo-f446re-hello-world"
    # Only what the run recorded is left, and that is the executed routes.
    assert summary["bench"]["devices"] == {"debuggers": ["dut"], "com_ports": ["dut_uart"]}
    assert report["test_config_path"] not in outputs(tmp_path)


def test_a_log_named_by_an_absolute_path_from_another_workspace_is_counted_and_never_published(tmp_path: Path) -> None:
    write_logs(tmp_path)
    report = green_report(tmp_path)
    report["steps"][1]["result"]["log_path"] = FOREIGN_LOG
    report["steps"][2]["result"]["log_path"] = FOREIGN_LOG

    result, _summary, _document = evidence(tmp_path, report)

    assert result["logs_copied"] == [Path(OPENOCD_LOG).name]
    assert result["logs_outside_workspace"] == 1
    assert FOREIGN_LOG not in json.dumps(result)
    assert "another workspace" in result["summary"]


def test_a_log_path_that_climbs_out_of_the_workspace_is_never_read(tmp_path: Path) -> None:
    outside = tmp_path.parent / "secret.jsonl"
    outside.write_text("not this run's\n", encoding="utf-8")
    report = green_report(tmp_path)
    report["steps"][0]["result"]["log_path"] = f"../{outside.name}"
    report["steps"][1]["result"]["log_path"] = f"../{outside.name}"
    report["steps"][2]["result"]["log_path"] = f"../{outside.name}"

    result, _summary, _document = evidence(tmp_path, report)

    assert result["logs_copied"] == []
    assert result["logs_outside_workspace"] == 1
    assert not any((tmp_path / "evidence" / "logs").iterdir())


# ---------------------------------------------------------------------------
# The job summary's second destination.


def test_the_job_summary_is_appended_to_the_file_github_step_summary_names(tmp_path: Path) -> None:
    write_logs(tmp_path)
    step_summary = tmp_path / "runner" / "step-summary.md"
    step_summary.parent.mkdir(parents=True)
    step_summary.write_text("## An earlier step\n", encoding="utf-8")

    result, _summary, document = evidence(tmp_path, failed_report(tmp_path), environ={"GITHUB_STEP_SUMMARY": str(step_summary)})

    assert result["step_summary_path"] == str(step_summary)
    written = step_summary.read_text(encoding="utf-8")
    # Appended, so the step before it keeps what it wrote.
    assert written.startswith("## An earlier step\n")
    assert document in written
    assert "GITHUB_STEP_SUMMARY" in result["summary"]


def test_the_job_summary_is_written_under_out_whether_or_not_the_variable_is_set(tmp_path: Path) -> None:
    write_logs(tmp_path)

    result, _summary, document = evidence(tmp_path, failed_report(tmp_path))

    assert "step_summary_path" not in result
    assert (tmp_path / "evidence" / "job-summary.md").read_text(encoding="utf-8") == document


# ---------------------------------------------------------------------------
# What the environment did not supply.


def test_a_field_the_environment_cannot_supply_is_absent_rather_than_invented(tmp_path: Path) -> None:
    write_logs(tmp_path)
    report = green_report(tmp_path)
    del report["target"]

    _result, summary, _document = evidence(tmp_path, report)

    assert "firmware" not in summary
    assert "runner" not in summary["bench"]
    assert "target" not in summary["bench"]
    assert "debuggers" not in summary["tools"]


def test_gitlab_supplies_the_firmware_block_when_github_does_not(tmp_path: Path) -> None:
    write_logs(tmp_path)

    _result, summary, _document = evidence(
        tmp_path,
        green_report(tmp_path),
        environ={"CI_PROJECT_PATH": "acme/firmware", "CI_COMMIT_SHA": "b" * 40, "CI_COMMIT_REF_NAME": "main"},
    )

    assert summary["firmware"] == {"repository": "acme/firmware", "commit": "b" * 40, "ref": "main"}


def test_the_pull_request_head_is_recorded_beside_the_merge_commit(tmp_path: Path) -> None:
    write_logs(tmp_path)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"head": {"sha": "c" * 40}}}), encoding="utf-8")

    _result, summary, _document = evidence(
        tmp_path,
        green_report(tmp_path),
        environ={"GITHUB_SHA": "a" * 40, "GITHUB_REF": "refs/pull/7/merge", "GITHUB_EVENT_PATH": str(event)},
    )

    # Beside, not instead of: the merge commit is what was built.
    assert summary["firmware"]["commit"] == "a" * 40
    assert summary["firmware"]["head_commit"] == "c" * 40


def test_a_pull_request_head_that_is_not_a_commit_id_is_dropped(tmp_path: Path) -> None:
    # The one value here that comes out of a document a fork's event wrote.
    write_logs(tmp_path)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"head": {"sha": "../../etc/passwd"}}}), encoding="utf-8")

    _result, summary, _document = evidence(tmp_path, green_report(tmp_path), environ={"GITHUB_SHA": "a" * 40, "GITHUB_EVENT_PATH": str(event)})

    assert "head_commit" not in summary["firmware"]


def test_debugger_version_lines_come_from_a_debugger_info_the_report_carries(tmp_path: Path) -> None:
    write_logs(tmp_path)
    report = green_report(tmp_path)
    report["debuggers"] = {
        "dut": {
            "type": "openocd",
            "probe_id": PROBE_SERIAL,
            "check": {"ok": True, "tool": "debugger_info", "backend": "openocd", "version": "Open On-Chip Debugger 0.12.0", "executable": OPENOCD_EXECUTABLE, "probe_id": PROBE_SERIAL},
        }
    }

    _result, summary, _document = evidence(tmp_path, report)

    assert summary["tools"]["debuggers"] == {"dut": {"backend": "openocd", "version": "Open On-Chip Debugger 0.12.0"}}
    published = outputs(tmp_path)
    assert PROBE_SERIAL not in published
    assert OPENOCD_EXECUTABLE not in published


# ---------------------------------------------------------------------------
# The command.


def test_the_command_writes_the_three_outputs_and_needs_no_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # No authoritative configuration anywhere, which is the case a workflow's
    # evidence step meets after a run refused for having none.
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty-user-config"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-user-config"))
    monkeypatch.delenv("AGENTIC_HIL_CONFIG", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_logs(workspace)
    report_path = workspace / "report.json"
    report_path.write_text(json.dumps(failed_report(workspace), indent=2), encoding="utf-8")
    monkeypatch.chdir(workspace)

    exit_code = entrypoint(["run-evidence", "--report", str(report_path), "--out", "artifacts/evidence", "--json"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["ok"] is True
    assert result["outcome"] == "failure"
    out = workspace / "artifacts" / "evidence"
    assert json.loads((out / "run-summary.json").read_text(encoding="utf-8"))["outcome"] == "failure"
    assert (out / "job-summary.md").read_text(encoding="utf-8").startswith("## Agentic HIL:")
    assert sorted(path.name for path in (out / "logs").iterdir()) == sorted({Path(OPENOCD_LOG).name, Path(UART_LOG).name})


def test_the_command_renders_for_a_person_through_the_shared_humanize_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_logs(tmp_path)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(green_report(tmp_path), indent=2), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    exit_code = entrypoint(["run-evidence", "--report", str(report_path), "--out", "evidence"])

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert printed.lstrip().startswith("Run evidence written")
    assert "run-summary.json" in printed


def test_a_report_that_is_not_there_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as raised:
        write_run_evidence(str(tmp_path / "absent.json"), str(tmp_path / "evidence"), workspace=tmp_path, environ={})

    assert raised.value.to_dict()["error_type"] == "run_report_not_found"
    assert not (tmp_path / "evidence").exists()


def test_a_report_that_is_not_json_is_refused_by_name(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text("not a report\n", encoding="utf-8")

    with pytest.raises(ConfigError) as raised:
        write_run_evidence(str(path), str(tmp_path / "evidence"), workspace=tmp_path, environ={})

    assert raised.value.to_dict()["error_type"] == "run_report_invalid"


def test_the_evidence_step_stays_green_when_the_run_it_reports_was_red(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A workflow runs this with `if: always()`, so the evidence step's verdict
    # has to be about the evidence. The run's verdict is `outcome`.
    write_logs(tmp_path)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(refused_report(tmp_path), indent=2), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    exit_code = entrypoint(["run-evidence", "--report", str(report_path), "--out", "evidence", "--json"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "refused"


@pytest.mark.parametrize("answer", [None, ["ok", "token", "hunter2"]])
def test_a_redaction_that_answers_with_no_document_withholds_the_whole_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: object,
) -> None:
    """The case the guard exists for, and the case that must publish nothing.

    Forced rather than provoked, exactly as the two other sinks pin it:
    `redact_sensitive` answers a document with a document, so no report's own
    contents can reach this branch, and it is precisely the branch in which the
    report must not be written out. The three files are still written, because
    they are what a workflow's `if: always()` step uploads and a missing bundle
    reads as a step that never ran, but every byte of them comes from the
    command's own name.
    """
    write_logs(tmp_path)
    report = failed_report(tmp_path)
    report["steps"][2]["result"]["token"] = STEP_TOKEN
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    monkeypatch.setattr(runevidence, "redact_sensitive", lambda value: answer)

    result = write_run_evidence(str(report_path), str(tmp_path / "evidence"), workspace=tmp_path, environ={})

    out = tmp_path / "evidence"
    summary = json.loads((out / "run-summary.json").read_text(encoding="utf-8"))
    document = (out / "job-summary.md").read_text(encoding="utf-8")
    # The command's own verdict, so the step that wrote a refusal is red.
    assert result["ok"] is False
    assert result["error_type"] == "redaction_unavailable"
    assert result["command"] == "run-evidence"
    # The same refusal in both documents, and no run outcome in either: a
    # consumer must not be able to read this as a run that ended somehow.
    assert summary["ok"] is False
    assert summary["error_type"] == "redaction_unavailable"
    assert summary["command"] == "run-evidence"
    assert "outcome" not in summary
    assert "redaction_unavailable" in document
    assert "was not written from the run report" in document
    # Not one field of the report, in either of them or in the result.
    published = "\n".join([json.dumps(summary), document, json.dumps(result)])
    for value in (STEP_TOKEN, PROBE_SERIAL, OPENOCD_EXECUTABLE, CONFIG_PATH, "nucleo-f446re-hello-world", "uart_expect_timeout", "dut_uart", "The board sent nothing"):
        assert value not in published
    # The logs are named by the report, and the report is what nothing vouched
    # for, so the directory is there and empty.
    assert result["logs_copied"] == []
    assert list((out / "logs").iterdir()) == []


def test_a_withheld_bundle_fails_the_command_closed_and_is_still_on_the_job_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end: the exit code, and the second destination the summary has.

    The evidence step's verdict is about the evidence, and here the evidence is
    what failed, so this is the one thing that turns it red. A reviewer looking
    at the job page has to find the statement there rather than an empty space
    where the run's table usually is.
    """
    write_logs(tmp_path)
    step_summary = tmp_path / "runner" / "step-summary.md"
    step_summary.parent.mkdir(parents=True)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(failed_report(tmp_path), indent=2), encoding="utf-8")
    monkeypatch.setattr(runevidence, "redact_sensitive", lambda value: None)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
    monkeypatch.chdir(tmp_path)

    exit_code = entrypoint(["run-evidence", "--report", str(report_path), "--out", "evidence", "--json"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["error_type"] == "redaction_unavailable"
    assert "redaction_unavailable" in step_summary.read_text(encoding="utf-8")
    assert "The board sent nothing" not in step_summary.read_text(encoding="utf-8")


def test_a_real_reactor_run_maps_to_the_evidence_a_reviewer_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end, over a report this project's own reactor wrote.

    The hand-built documents above pin the mapping field by field; this one pins
    that the mapping is about the report the reactor actually commits, so a
    field renamed in `write_report` cannot leave the evidence quietly empty."""
    from agentic_hil.cli import run_test_reactor

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    probe = tmp_path / "fake_openocd.py"
    probe.write_bytes(FAKE_OPENOCD.read_bytes())
    write_authoritative_config(workspace, monkeypatch, debugger_executable=probe)
    monkeypatch.chdir(workspace)
    plan = workspace / ".agentic-hil" / "testconfig.yaml"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("version: 3\nname: evidence-demo\nsteps:\n  - {device: dut, action: reset}\n", encoding="utf-8")

    report = run_test_reactor(str(plan))
    assert report["ok"] is True, report
    report_path = workspace / "report-copy.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    result = write_run_evidence(str(report_path), str(workspace / "evidence"), workspace=workspace, environ={})

    summary = json.loads((workspace / "evidence" / "run-summary.json").read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert summary["outcome"] == "success"
    assert summary["plan"] == {"name": "evidence-demo", "path": ".agentic-hil/testconfig.yaml", "sha256": summary["plan"]["sha256"]}
    assert summary["bench"]["config_digest"] == report["config_in_force"]["digest"]
    assert summary["bench"]["devices"] == {"debuggers": ["dut"]}
    assert summary["run"]["cleanup_ok"] is True
    # The report the reactor writes names the state root, the configuration and
    # the backend executable by absolute path; none of them reaches either
    # document. The backend's own log still carries its command line, and that
    # is the line the test above draws.
    published = outputs(workspace)
    assert str(workspace.parent / "user-config") not in published
    assert str(probe) not in published
    assert report["config_in_force"]["path"] not in published
    # The backend log the run wrote is in the bundle, by the name the run gave it.
    assert any(name.startswith("openocd-") for name in result["logs_copied"]), result
