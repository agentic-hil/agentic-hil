"""The recovery routes describe an open incident that does not stand, as `lease-status` does.

`hardware_recover` and `agentic-hil recover` sign for an incident that stands,
and only for one. Asked about an incident of this workspace that is open and
does not stand, both answered `was_quarantined: false` and "Nothing on this
bench is standing", while `lease-status` for the same bench reported it
quarantined under that incident. They now say what `lease-status` says: which
incident is open and for what, that it does not stand, that nothing needs
signing, and that the next hardware call settles it or stands it down. The
adoption refusal for such an incident sent the caller to `agentic-hil recover`,
which does not act on it; it now gives the same step.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from test_implicit_single_action_run import fail_every_release, named_incidents
from test_recover_tool import config_for, open_incident
from test_recovered_leases_given_back import fail_the_first_releases, timed_out_bench

from agentic_hil.adopt import PROJECT_CONFIG_ADOPT
from agentic_hil.coordination import LEASE_RELEASE_RETRY_REASON
from agentic_hil.humanize import render_result
from agentic_hil.report import failed_success_check
from agentic_hil.tools import AgenticHILToolService

TOOL = "hardware_recover"
NOTHING_STANDING = "Nothing on this bench is standing"


def assert_describes_an_open_incident(answer: dict, reasons: list[str]) -> None:
    """What both routes say about an open incident that does not stand."""
    assert answer["ok"] is True, answer
    assert answer["tool"] == TOOL, answer
    assert answer["nothing_to_recover"] is True, answer
    assert answer["was_quarantined"] is True, answer
    assert answer["incident_stands"] is False, answer
    assert answer["resources"] == [], answer
    for reason in reasons:
        assert reason in answer["cleanup_reasons"], answer
    summary = answer["summary"]
    assert NOTHING_STANDING not in summary, summary
    assert "does not stand" in summary, summary
    assert "nothing needs signing" in summary, summary
    assert "next hardware call" in summary, summary
    assert "agentic-hil recover" not in answer["next_step"], answer["next_step"]
    # Nothing failed: the call had nothing to clear and says why, so neither the
    # command's exit status nor an MCP client's error flag reads it as a failure.
    assert failed_success_check(answer) is None, answer


def test_the_command_line_describes_the_incident_lease_status_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`agentic-hil recover` over an open incident answers what `lease-status` answers.

    The incident is this workspace's own, open, and does not stand: a target
    state nobody confirmed, which the next reset and probe speak for. The
    command clears nothing, and says so, but it may not say the bench is not
    quarantined while `lease-status` says it is: both name the same incident,
    the same reasons and the same next step."""
    from agentic_hil.cli import dispatch

    config = config_for(tmp_path)
    incident = open_incident(config, "debugger_result_unconfirmed")
    monkeypatch.setattr("agentic_hil.cli.load_cli_authoritative_config", lambda _path: config)

    status = dispatch(argparse.Namespace(command="lease-status"))
    answer = dispatch(argparse.Namespace(command="recover", confirm_safe_state=True, quarantine_id=incident, accept_config_change=False))

    assert status["blocked"] is True and status["incident_stands"] is False, status
    assert_describes_an_open_incident(answer, ["debugger_result_unconfirmed"])
    assert answer["quarantine_id"] == incident == status["quarantine_id"], answer
    assert answer["cleanup_reasons"] == status["cleanup_reasons"], answer
    assert answer["next_step"] == status["next_step"], (answer["next_step"], status["next_step"])
    assert "recovered_quarantine_id" not in answer and "cleared_reasons" not in answer, answer
    after = dispatch(argparse.Namespace(command="lease-status"))
    assert after["blocked"] is True and after["quarantine_id"] == incident, after


def test_the_command_line_prints_the_incident_it_describes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What an operator reads at the shell names the incident and that it does not stand.

    `agentic-hil recover` prints its answer for a person. The printed fields
    said whether the bench was quarantined and that there was nothing to
    recover, and not which incident or whether it stands, so an operator
    holding it beside `lease-status` had nothing to match."""
    from agentic_hil.cli import dispatch

    config = config_for(tmp_path)
    incident = open_incident(config, "debugger_result_unconfirmed")
    monkeypatch.setattr("agentic_hil.cli.load_cli_authoritative_config", lambda _path: config)

    answer = dispatch(argparse.Namespace(command="recover", confirm_safe_state=True, quarantine_id=incident, accept_config_change=False))
    printed = render_result(answer, "recover")

    fields = {line.split()[0]: line.split()[1:] for line in printed.splitlines() if line.startswith("  ") and len(line.split()) >= 2}
    assert fields.get("quarantine_id") == [incident], printed
    assert fields.get("incident_stands") == ["no"], printed


def test_hardware_recover_names_the_incident_that_still_holds_the_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reproduction: an adoption whose lease cannot be given back, then `hardware_recover`.

    Every release fails, so the bench is held under a `lease_release_unconfirmed`
    incident when the adoption returns, and again when the recover call does:
    the end of that call stands the incident down and cannot give the lease back
    either. The answer names the incident that holds the bench when it returns,
    the one `lease-status` reports, and describes it as open and not standing."""
    _, tools = timed_out_bench(tmp_path, monkeypatch, None)
    fail_every_release(tools, monkeypatch)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
        assert refused["error_type"] == "resource_quarantined", refused
        before = tools.coordinator.status()
        assert before["blocked"] is True and before["incident_stands"] is False, before

        answer = tools.call(TOOL, {})
        status = tools.coordinator.status()

        assert_describes_an_open_incident(answer, [LEASE_RELEASE_RETRY_REASON])
        assert status["blocked"] is True, status
        assert answer["quarantine_id"] == status["quarantine_id"], (answer, status["quarantine_id"])
        assert named_incidents(answer) == {status["quarantine_id"]}, answer
    finally:
        tools.close()


def test_hardware_recover_over_an_incident_its_own_call_ends_says_it_ended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same answer when the end of the recover call stands the incident down.

    Under `recovery.auto_recover: off` the adoption leaves its lease registered
    under a `lease_release_unconfirmed` incident, and this time the lease can be
    given back. `hardware_recover` finds the incident open and not standing, and
    the end of its own call stands it down. The answer describes the incident it
    found, says it has since been stood down, and leaves no step that says the
    next hardware call still has to settle it."""
    _, tools = timed_out_bench(tmp_path, monkeypatch, "off")
    fail_the_first_releases(tools, monkeypatch, 1)
    try:
        assert tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})["ok"] is False
        before = tools.coordinator.status()
        assert before["blocked"] is True and before["incident_stands"] is False, before

        answer = tools.call(TOOL, {})

        assert_describes_an_open_incident(answer, [LEASE_RELEASE_RETRY_REASON])
        assert tools.coordinator.blocked is False, tools.coordinator.status()
        assert answer["incident_stood_down"]["quarantine_id"] == before["quarantine_id"], answer
        assert "has since been stood down" in answer["summary"], answer["summary"]
        assert "next hardware call" not in answer["next_step"], answer["next_step"]
    finally:
        tools.close()


def test_the_adoption_refusal_for_an_incident_that_does_not_stand_sends_nobody_to_recover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal's next step is the one `lease-status` gives for the incident holding the bench.

    The adoption's lease cannot be given back, so a `lease_release_unconfirmed`
    incident holds the bench when the call returns. It does not stand, and
    `agentic-hil recover` answers `nothing_to_recover` for it, so the refusal
    may not send the caller there: it says nothing needs signing and that the
    next hardware call settles the incident, as `lease-status` does."""
    _, tools = timed_out_bench(tmp_path, monkeypatch, None)
    fail_every_release(tools, monkeypatch)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
        status = tools.coordinator.status()

        assert refused["error_type"] == "resource_quarantined", refused
        assert status["blocked"] is True and status["incident_stands"] is False, status
        assert "agentic-hil recover" not in refused["next_step"], refused["next_step"]
        assert refused["next_step"] == status["next_step"], (refused["next_step"], status["next_step"])
    finally:
        tools.close()


def test_a_bench_with_no_open_incident_still_answers_that_nothing_was_quarantined(tmp_path: Path) -> None:
    """The clean bench keeps its answer: nothing was quarantined and nothing is standing."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config)
    try:
        answer = service.call(TOOL, {})
    finally:
        service.close()

    assert answer["ok"] is True, answer
    assert answer["nothing_to_recover"] is True, answer
    assert answer["was_quarantined"] is False, answer
    assert answer["summary"].startswith(NOTHING_STANDING), answer["summary"]
