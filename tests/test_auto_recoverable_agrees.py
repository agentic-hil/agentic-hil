"""`auto_recoverable` and what the bench says beside it agree about an incident that does not stand.

For an open incident that does not stand, `lease-status` took `auto_recoverable`
from what the bench's `recovery.auto_recover` policy lets a recovery action
settle, and its summary from whether the incident stands. Where the policy does
not cover the reason, the field said false beside "Nothing needs signing: the
next hardware call settles it on its own evidence", and the troubleshooting
guide and the `resource_quarantined` remediation read false as needing a person.
Neither holds for such an incident: no recovery action runs, the next hardware
call stands it down unconfirmed, and `recover` answers `nothing_to_recover`.

Whether a person is needed is `incident_stands`. `auto_recoverable` says whether
the next hardware call settles the incident on evidence it reads back, or stands
it down with nothing confirmed, and every text that reads it says so.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_implicit_single_action_run import FakeBackend, config_for, firmware

from agentic_hil.knowledge import remediation_fields
from agentic_hil.tools import AgenticHILToolService

ROOT = Path(__file__).resolve().parents[1]
UNCONFIRMED = "stands it down unconfirmed"


@pytest.mark.parametrize(("policy", "auto_recoverable"), [("reset_halt", True), ("readonly", False), ("off", False)])
def test_lease_status_says_what_auto_recoverable_says_about_an_incident_that_does_not_stand(tmp_path: Path, policy: str, auto_recoverable: bool) -> None:
    """The reproduction: a failed flash inside a declared run, then the lease status.

    The run holds the incident until it is stopped, so the status is read while
    it is open. The flash left `debugger_result_unconfirmed`, which only a
    verified reset into halt settles: `reset_halt` can, `readonly` and `off`
    cannot. Under those two the summary may not say the next hardware call
    settles the incident on its own evidence, because nothing reads the board
    back for it: it says that call stands the incident down unconfirmed."""
    config = config_for(tmp_path, auto_recover=policy)
    service = AgenticHILToolService(config, backend=FakeBackend(flash_unconfirmed=True))
    try:
        assert service.call("bench_run_start", {"devices": [{"kind": "debugger"}]})["ok"] is True
        flashed = service.call("flash_firmware", firmware(tmp_path))
        assert flashed["ok"] is False, flashed

        status = service.hardware_lease_status()

        assert status["blocked"] is True, status
        assert status["incident_stands"] is False, status
        assert status["auto_recoverable"] is auto_recoverable, status
        summary = status["summary"]
        assert "Nothing needs signing" in summary, summary
        if auto_recoverable:
            assert UNCONFIRMED not in summary, summary
        else:
            assert UNCONFIRMED in summary, summary
            assert "on its own evidence" not in summary, summary
            assert "what it has to confirm" not in summary, summary
    finally:
        service.close()


def _troubleshooting() -> str:
    return (ROOT / "TROUBLESHOOTING.md").read_text(encoding="utf-8")


def test_the_guide_reads_whether_a_person_is_needed_from_incident_stands() -> None:
    """The paragraph that introduces the two bullets says which field decides whether a person is needed."""
    text = _troubleshooting()
    intro = next(line for line in text.splitlines() if line.startswith("For a quarantine, read `cleanup_reasons`"))
    assert "`incident_stands`" in intro, intro


def test_the_guide_says_what_false_means_for_an_incident_that_does_not_stand() -> None:
    """`auto_recoverable: false` on an incident that does not stand needs nobody.

    The bullet said false needs a person. For an incident that does not stand it
    means that no recovery action runs for it and that the next hardware call
    stands it down unconfirmed; a person is needed only where the incident
    stands."""
    text = _troubleshooting()
    bullet = next(line for line in text.splitlines() if line.startswith("- `auto_recoverable: false`"))
    assert "`incident_stands`" in bullet, bullet
    assert "stood down" in bullet and "unconfirmed" in bullet, bullet
    assert "This needs a person." not in bullet, bullet


def test_the_policy_table_does_not_leave_off_to_an_operator() -> None:
    """The `off` row settled "nothing (operator only)"; an incident that does not stand is stood down there too."""
    text = _troubleshooting()
    row = next(line for line in text.splitlines() if re.match(r"\| `off` \|", line))
    assert "operator only" not in row, row
    assert "stood down" in row or "stands" in row, row


def test_the_quarantine_remediation_reads_incident_stands_before_auto_recoverable() -> None:
    """The catalogue said "False means an operator has to look at the board" whatever the incident.

    A refusal that carries it can be one for an incident that does not stand,
    the adoption's own among them. The step that reads `auto_recoverable` names
    `incident_stands` too and says what false means where it is false."""
    steps = remediation_fields("resource_quarantined")["remediation"]
    reading = [step for step in steps if "auto_recoverable" in step]
    assert len(reading) == 1, steps
    assert "`incident_stands`" in reading[0], reading
    assert "False means an operator has to look at the board." not in reading[0], reading
    assert UNCONFIRMED in reading[0], reading
