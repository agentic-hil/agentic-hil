"""What a plan does on the board, and what the results of one say afterwards.

The green run, the red run, the refused run and the run that never starts, each
read off the surface an operator or an agent actually sees. A plan is the
product's primary path, and the three ways it can end are told apart by the word
the result is headed with: `Failed:` for a run whose claim did not hold,
`Refused:` for one where no step ran, and neither for one that passed. Getting
that wrong made a deliberate red run read like a setup error and a policy
refusal read like a broken plan.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import BENCH_ONLY, Bench

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# Where the plan's own report is captured, so the evidence step reads this run's
# report rather than the shared `last-report.json`, which a refused plan never
# writes and which would therefore still hold an earlier run's.
REPORT = Path("artifacts") / "reports" / "testconfig.json"


@pytest.fixture(scope="session")
def green_run(bench: Bench, firmware: Path) -> dict:
    """The demo's declared plan, run once on the board, for the tests that read it.

    Build, flash, open the port with a clean buffer, reset, read the banner:
    the demo's primary path and the one the nightly job runs. Session-scoped
    because it drives hardware, and a board is not something to flash once per
    assertion.
    """
    (bench.project / REPORT).parent.mkdir(parents=True, exist_ok=True)
    status, report = bench.document("test-reactor", "--test-config", "testconfig.yaml")
    (bench.project / REPORT).write_text(json.dumps(report), encoding="utf-8")
    assert status == 0, report.get("summary")
    return report


def test_the_declared_demo_plan_is_green_on_the_board(green_run: dict) -> None:
    """Flash, open, reset, read: every step, on the real board, with the real firmware."""
    assert green_run["ok"] is True, green_run
    assert green_run["cleanup_ok"] is True, green_run
    assert green_run["audit_ok"] is True, green_run
    steps = green_run["steps"]
    assert [step["action"] for step in steps] == ["flash", "uart_open", "reset", "uart_read"], steps
    for step in steps:
        assert step["result"]["ok"] is True, step


def test_run_evidence_prints_one_digest_prefix_and_an_elapsed_time_in_every_step_row(bench: Bench, green_run: dict) -> None:
    """The job summary a reviewer with no access to this bench reads.

    Two things it got wrong. The configuration digest was printed by joining the
    algorithm onto a digest that already carried its own prefix, which published
    `sha256sha256:` where a reader was meant to compare a hash. And the elapsed
    column was taken from whatever duration the tool underneath happened to
    report, so the COM steps, whose tools report none, had an empty cell in a
    column headed as if it were always filled.
    """
    built = bench.run("run-evidence", "--report", str(REPORT), "--out", "artifacts/evidence")
    assert built.returncode == 0, built.stdout + built.stderr

    summary = (bench.project / "artifacts" / "evidence" / "job-summary.md").read_text(encoding="utf-8")

    digest_rows = [line for line in summary.splitlines() if "Configuration digest" in line]
    assert len(digest_rows) == 1, summary
    assert digest_rows[0].count("sha256:") == 1, digest_rows[0]
    assert "sha256sha256" not in digest_rows[0], digest_rows[0]

    rows = [line for line in summary.splitlines() if line.startswith("| ") and line.split("|")[1].strip().isdigit()]
    assert len(rows) == len(green_run["steps"]), summary
    for row in rows:
        elapsed = row.split("|")[5].strip()
        assert elapsed.isdigit(), f"a step row carries no elapsed time: {row}"


def test_a_plan_whose_claim_fails_is_headed_by_the_runs_own_outcome(bench: Bench, green_run: dict) -> None:
    """A deliberate red run is a result, not a setup error.

    `Refused:` is reserved for a result where no step ran; a run that executed
    its steps and failed its claim is headed by the outcome it reached, so a
    reader can tell a board that answered wrongly from a bench that was never
    ready.
    """
    plan = bench.project / "claim-that-cannot-hold.yaml"
    plan.write_text(
        f"""version: 3
name: claim-that-cannot-hold
steps:
  - device: {bench.com_port_name()}
    action: uart_open
    clear_buffer: true
  - device: {bench.debugger_name()}
    action: reset
    mode: run
  - device: {bench.com_port_name()}
    action: uart_read
    comparator:
      equals: "this board never prints this"
    timeout_s: 3
""",
        encoding="utf-8",
    )

    answered = bench.run("test-reactor", "--test-config", "claim-that-cannot-hold.yaml")

    assert answered.returncode == 1, answered.stdout
    assert answered.stdout.startswith("Failed: comparator_unmet"), answered.stdout[:400]
    assert "Refused:" not in answered.stdout, answered.stdout[:400]


def test_a_permission_a_plan_needs_is_named_at_every_level_and_the_grant_line_is_offered(bench: Bench) -> None:
    """The refusal an agent has to read out, and the line only the operator runs.

    Three things had to be right at once and were not. The refusal is
    `permission_denied` and not a fault in the plan, at the top level as well as
    on the finding. The permission is named as the dotted key the file uses and
    `agentic-hil grant` takes, including in the outermost summary, which used to
    report that the configuration had failed semantic validation. And the next
    step names the exact command that opens that one key, at the operator's own
    shell, because nothing on the agent's surface may open it.

    The permission is revoked and granted back inside this test, on this tier's
    own configuration. No configuration the operator owns is reachable from here.
    """
    plan = bench.project / "needs-a-reset.yaml"
    plan.write_text(
        f"""version: 3
name: needs-a-reset
steps:
  - device: {bench.debugger_name()}
    action: reset
    mode: run
""",
        encoding="utf-8",
    )
    key = f"debuggers.{bench.debugger_name()}.permissions.allow_reset"
    revoked = bench.run("revoke", key)
    assert revoked.returncode == 0, revoked.stdout + revoked.stderr
    try:
        status, result = bench.document("test-reactor", "--test-config", "needs-a-reset.yaml")
        rendered = bench.run("test-reactor", "--test-config", "needs-a-reset.yaml")
    finally:
        granted = bench.run("grant", key)
        assert granted.returncode == 0, granted.stdout + granted.stderr

    assert status == 1, result
    assert result["ok"] is False, result
    assert result["error_type"] == "permission_denied", result
    assert result["permission"] == key, result
    assert result["step_error_type"] == "permission_denied", result
    # The outermost sentence, which is the one a caller relays.
    assert key in result["summary"], result["summary"]
    assert "policy refused the plan" in result["summary"], result["summary"]
    finding = result["validation_error"]
    assert finding["error_type"] == "permission_denied", finding
    assert finding["permission"] == key, finding
    assert f"agentic-hil grant {key}" in finding["next_step"], finding["next_step"]

    assert rendered.returncode == 1, rendered.stdout
    assert rendered.stdout.startswith("Refused: permission_denied"), rendered.stdout[:400]
    assert f"agentic-hil grant {key}" in rendered.stdout, rendered.stdout


def test_check_plan_strict_fails_on_a_plan_naming_a_device_this_bench_does_not_declare(bench: Bench) -> None:
    """The board-free gate, which is the one check a plan gets before it reaches hardware.

    It used to open on `All 1 test plan(s) load through the reactor's loader` and
    exit 1, so the first line of a failed run said everything was fine. The
    outcome and the count it failed on are the heading now.
    """
    plan = bench.project / "names-a-device-that-is-not-declared.yaml"
    plan.write_text(
        """version: 3
name: names-a-device-that-is-not-declared
steps:
  - device: a_port_this_bench_does_not_declare
    action: uart_open
""",
        encoding="utf-8",
    )

    answered = bench.run("check-plan", "names-a-device-that-is-not-declared.yaml", "--strict")

    assert answered.returncode == 1, answered.stdout
    assert answered.stdout.startswith("Failed: 1 of 1 test plan(s) name device(s)"), answered.stdout[:400]
    assert "a_port_this_bench_does_not_declare" in answered.stdout, answered.stdout
