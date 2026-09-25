"""How this repository's own workflows invoke the suite and reach the bench.

Two halves, because this repository runs on two kinds of machine. The hosted
jobs in `ci.yml` run the Python suite, and how they invoke it is the first half
below. The nightly job in `hardware-bench.yml` runs the Nucleo-F446RE demo on a
machine with that board attached, and the second half holds it to the rules a
self-hosted runner cannot recover from getting wrong: no `pull_request` trigger,
a runner addressed by labels alone, one queue for one board, every action pinned
by commit, every hardware action through the `agentic-hil` CLI, and evidence
uploaded whatever the run did.

The first half:

In one process the Windows legs of this suite grew to about fourteen minutes of
pytest on an ordinary runner and twenty-three on a slow one, against a job limit
of twenty-five, and the limit cancelled two runs whose tests were green: one
inside the test step with 2307 tests already passed, one in the step after
pytest had printed its own summary. Nothing was hanging. The runner was slow,
and a serial suite has no answer to that except the retry, which costs the same
again for a result that was already known. Running across the runner's cores
bought those minutes back; at 4174 tests the Windows legs reached the same limit
again, so the limit is now held here as a floor with the measurements behind it,
and why a Windows leg costs four times a Linux one is #536.

The suite is parallel by construction rather than by permission: every test gets
its own HOME, config, state and temporary storage, and its device-lock root, its
CAN broker endpoint and its run records all follow that HOME, which is why every
developer run has used `-n auto` all along. The hosted jobs pass it too.

What is pinned here is the split, because one word moves it in either direction:
the hosted run is parallel, the collection out of the built source distribution
stays a collection, and `addopts` still says nothing about workers, so a bare
`pytest` in a checkout is one process and a failure can be read without a worker
id in front of it.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

import pytest
import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
BENCH_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "hardware-bench.yml"
# Every spelling pytest-xdist takes for "how many workers". A step that reaches
# parallelism the long way round is still parallel, and a test that only knew
# `-n auto` would call it serial.
WORKER_OPTIONS = ("-n", "--numprocesses")


def worker_setting(command: list[str]) -> str | None:
    """What a command line says about xdist workers, or None if it says nothing."""
    for index, argument in enumerate(command):
        for option in WORKER_OPTIONS:
            if argument == option:
                return command[index + 1] if index + 1 < len(command) else ""
            if argument.startswith(f"{option}="):
                return argument.split("=", 1)[1]
        if argument.startswith("-n") and len(argument) > 2:
            return argument[2:]
    return None


def hosted_step(name: str) -> list[str]:
    """What one step of the matrix job runs, split the way a shell splits it.

    `.github/` is repository content and MANIFEST.in does not ship it, so an
    unpacked source distribution has no workflow to read. That checkout is
    collected rather than run in CI, and every checkout that runs this file has
    the file this reads.
    """
    if not WORKFLOW.is_file():
        pytest.skip(f"{WORKFLOW.name} is repository content and this checkout has none")
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["test"]["steps"]
    named = [step for step in steps if step.get("name") == name]
    assert len(named) == 1, f"the test job has {len(named)} steps named {name!r}; this test is not reading it any more"
    return shlex.split(named[0]["run"])


def test_the_hosted_suite_runs_across_the_runners_cores() -> None:
    """One process is what put a green Windows run over the job's time limit."""
    command = hosted_step("Run tests")

    assert command[0] == "pytest", command
    assert worker_setting(command) == "auto", command


def test_the_matrix_limit_stays_above_what_a_slow_windows_leg_takes() -> None:
    """The limit is margin for a slow runner, not a deadline a slow runner beats.

    Running the suite across the runner's cores bought back the minutes the
    serial run had lost, and then the suite grew again. On one commit, the four
    Windows legs of the same suite took 20m13s, 21m17s and 22m17s, and the
    fourth was cancelled at the limit with every test that had run green; the
    next run cancelled a different leg the same way. A Linux leg of that same
    suite is about five and a half minutes, so it is the platform's own cost
    and not one slow machine.

    A cancelled leg is the least readable red this repository can produce,
    because the summary names no test, so the limit is held here well above the
    slowest leg anybody has measured. It is a floor rather than the number
    itself: raising it needs no edit here, lowering it back into the measured
    range does. Why a Windows leg costs four times a Linux one is #536 and is
    not answered by this bound.
    """
    job = workflow_document(WORKFLOW)["jobs"]["test"]

    assert job["timeout-minutes"] >= 40, job["timeout-minutes"]


# What `Run tests` leaves behind for the step after it, and what reads it (#536).
LEDGER_VARIABLE = "AGENTIC_HIL_TEST_LEDGER"
LEDGER_READER = "tests/suite_ledger.py"

# The Windows legs of ten runs between 2026-09-16 and 2026-09-24, and the leg
# before them that the job's limit cancelled, read step by step. `Run tests` took
# 25m48s at the slowest. The steps before it took up to 1m14s, and 2m13s on the
# cancelled leg, where installing the dependencies alone took 1m37s; the steps
# after it took up to 37s, and after a `Run tests` that fails only the ledger's
# reader and the post steps run. So five minutes between the two limits is
# about twice what the steps around the suite have been seen to take, and the
# suite's own limit starts well above the slowest suite anybody has measured.
SUITE_STEP_FLOOR_MINUTES = 35
ROOM_AROUND_THE_SUITE_MINUTES = 5


def matrix_steps() -> list[dict]:
    """The matrix job's steps, parsed, in the order the runner takes them."""
    return workflow_document(WORKFLOW)["jobs"]["test"]["steps"]


def option_value(command: list[str], option: str) -> str | None:
    """What a command line gives one long option, in either spelling, or None."""
    for index, argument in enumerate(command):
        if argument == option:
            return command[index + 1] if index + 1 < len(command) else ""
        if argument.startswith(f"{option}="):
            return argument.split("=", 1)[1]
    return None


def test_every_leg_prints_its_slowest_tests() -> None:
    """Where a leg's time goes is printed by the leg itself.

    A Windows leg costs about four times a Linux one on the same commit, and a
    green log said nothing about where that goes: dots, a count and a total.
    Thirty is the list #536 asked every leg for. `--durations=0` would print
    every test, four and a half thousand lines that bury the thirty that matter.
    """
    command = hosted_step("Run tests")

    assert option_value(command, "--durations") == "30", command


def test_every_leg_names_its_worker_count() -> None:
    """pytest-xdist names the workers it started only at the default verbosity.

    A leg's log says `created: 2/2 workers` and `2 workers [4572 items]` before
    the first dot, which is what tells a slow leg from one that had fewer cores.
    Under `-q` the same place says `bringing up nodes...` with no count in it, so
    the step passes no quiet flag.
    """
    command = hosted_step("Run tests")

    quiet = [argument for argument in command if argument == "--quiet" or re.fullmatch(r"-q+", argument)]
    assert quiet == [], command


def test_a_leg_runs_out_of_time_inside_run_tests_with_room_left_after_it() -> None:
    """The suite has a limit of its own, below the job's, so the job outlives it.

    A leg that reached the job's limit was cancelled whole: `Run tests` ended in
    `The operation was canceled.`, every step after it was skipped, and nothing
    was left running that could say which tests had been going. A limit on the
    step ends the step instead, and the job carries on to the step that reads
    the ledger. So the step's limit sits below the job's by the time the steps
    around the suite take, and above the slowest `Run tests` measured by a
    margin; both are floors, for the reason the job's limit is one.
    """
    job = workflow_document(WORKFLOW)["jobs"]["test"]
    run_tests = [step for step in job["steps"] if step.get("name") == "Run tests"]
    assert len(run_tests) == 1, run_tests
    step_limit = run_tests[0].get("timeout-minutes")

    assert isinstance(step_limit, int), run_tests[0]
    assert step_limit >= SUITE_STEP_FLOOR_MINUTES, step_limit
    assert job["timeout-minutes"] - step_limit >= ROOM_AROUND_THE_SUITE_MINUTES, (job["timeout-minutes"], step_limit)


def test_the_ledger_is_kept_for_run_tests_alone() -> None:
    """The variable reaches the one session whose tests it records.

    Set on the job or the workflow, it would also reach the build and the
    collection out of the source distribution, and a ledger that a collection
    wrote beside the suite's would be read as part of the suite. It names a
    directory under the runner's temporary root, outside the checkout, so
    nothing a later step builds or collects can pick it up.
    """
    workflow = workflow_document(WORKFLOW)
    carriers = [
        (job_name, step.get("name"))
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if LEDGER_VARIABLE in (step.get("env") or {})
    ]

    assert carriers == [("test", "Run tests")], carriers
    assert LEDGER_VARIABLE not in (workflow.get("env") or {})
    for job_name, job in workflow["jobs"].items():
        assert LEDGER_VARIABLE not in (job.get("env") or {}), job_name
        for step in job.get("steps", []):
            assert LEDGER_VARIABLE not in step.get("run", ""), (job_name, step.get("name"))
    run_tests = [step for step in matrix_steps() if step.get("name") == "Run tests"][0]
    assert "runner.temp" in str(run_tests["env"][LEDGER_VARIABLE]), run_tests["env"]


def test_the_ledger_is_read_after_run_tests_fails() -> None:
    """The step that names the tests a leg ran out of time on runs on a failure.

    The runner skips a step with no condition once an earlier step has failed,
    and a `Run tests` that runs out of its own time has failed, so the reader
    carries `failure()`. It does not carry `always()` or `cancelled()`: a push
    that supersedes a run cancels it, which is not a leg running out of time,
    and a reader running then would say it was. The directory reaches it on its
    command line, because the variable belongs to `Run tests` alone.
    """
    steps = matrix_steps()
    names = [step.get("name") for step in steps]
    run_tests = names.index("Run tests")
    readers = [index for index, step in enumerate(steps) if LEDGER_READER in step.get("run", "")]

    assert len(readers) == 1, names
    reader = steps[readers[0]]
    assert readers[0] > run_tests, names
    condition = str(reader.get("if", ""))
    assert "failure()" in condition, reader
    assert "always()" not in condition and "cancelled()" not in condition, reader
    ledger = str((steps[run_tests].get("env") or {}).get(LEDGER_VARIABLE, ""))
    assert ledger, steps[run_tests]
    assert ledger in reader["run"], reader


def test_the_source_distribution_is_still_only_collected() -> None:
    """That step asks whether pytest can find its way through what the sdist ships.

    Collecting is the whole question, the dev dependencies are already on the
    interpreter that built it, and running the suite a second time per matrix
    leg would buy nothing and cost every minute the step above just saved.
    """
    command = hosted_step("Collect the source distribution's tests")

    assert "--collect-only" in command, command
    assert worker_setting(command) is None, command


def test_a_bare_pytest_in_a_checkout_is_still_one_process(pytestconfig: pytest.Config) -> None:
    """The parallel run is a command line, not a property of the checkout.

    A worker count in `addopts` would reach every invocation, including the one
    a developer types to read a single failure without a worker id in front of
    it, and including tooling that runs pytest for reasons of its own.
    """
    addopts = list(pytestconfig.getini("addopts"))

    # The real ini, not an empty answer that would pass whatever it carried.
    assert "--strict-markers" in addopts, addopts
    assert worker_setting(addopts) is None, addopts


# The production dependency audit: what `pip install 'agentic-hil[can,pyocd]'`
# resolves to, locked with hashes so the audit reads pins rather than whatever
# the index served that minute, and the auditor's own lock beside it.
PRODUCTION_LOCK = REPOSITORY_ROOT / "requirements" / "production.txt"
AUDIT_LOCK = REPOSITORY_ROOT / "requirements" / "audit.txt"


def hosted_installs() -> list[str]:
    """Every `pip install` any job of the hosted workflow runs."""
    workflow = workflow_document(WORKFLOW)
    return [line for job in workflow["jobs"].values() for line in run_lines(job) if re.search(r"\bpip install\b", line)]


def locked_pins(lock: Path) -> dict[str, list[str]]:
    """Each `name==version` in a lock, with the lines that follow it up to the next pin."""
    entries: dict[str, list[str]] = {}
    current: list[str] = []
    for line in lock.read_text(encoding="utf-8").splitlines():
        pin = re.match(r"^([A-Za-z0-9._-]+)==", line)
        if pin is not None:
            current = entries.setdefault(pin.group(1).lower().replace("_", "-"), [])
        else:
            current.append(line)
    return entries


def test_no_hosted_job_installs_from_the_index_without_hashes() -> None:
    """A version is a name the index answers for; a hash is the bytes.

    The matrix installs its dependency set from `requirements/dev.txt` with
    `--require-hashes` and the checkout with `--no-deps`, so nothing it runs
    came off the index on the index's word alone. The audit job installed the
    production extras and pip-audit the other way, and Scorecard read both
    commands (code scanning alerts 77 and 78). One unpinned install in any job
    is what every other pin is cosmetic against, so every job is held to the
    matrix's rule: a hash for everything fetched, and `--no-deps` for the
    checkout, which comes off the runner's disk and has no artifact to hash.
    """
    installs = hosted_installs()

    assert installs
    for line in installs:
        if "--require-hashes" in line:
            continue
        assert re.search(r"--no-deps(\s+-e)?\s+\.$", line), line


def test_the_auditor_is_installed_from_its_own_lock() -> None:
    """pip-audit is the one program whose word the audit's verdict is.

    A release of it substituted on the index would run inside the job that
    exists to notice substituted releases, so it is installed from
    `requirements/audit.txt` with `--require-hashes` like the build tooling of
    the release pipeline is, and the lock pins it with its hash.
    """
    audit = workflow_document(WORKFLOW)["jobs"]["audit"]
    installs = [line for line in run_lines(audit) if re.search(r"\bpip install\b", line)]

    assert installs == ["python -m pip install --require-hashes -r requirements/audit.txt"], installs
    pins = locked_pins(AUDIT_LOCK)
    assert "pip-audit" in pins, sorted(pins)
    for name, tail in pins.items():
        assert any("--hash=sha256:" in line for line in tail), name


def test_the_audit_reads_the_locked_production_dependency_set() -> None:
    """What is audited is what a bench's install resolves to, pinned.

    `requirements/production.txt` is compiled from `pyproject.toml` with the two
    optional extras a bench installs and without the dev extra, so a pytest
    advisory does not turn the production audit red and a python-can or pyOCD
    one does. pip-audit reads it with `--require-hashes`, which is the mode in
    which it audits the pins as written instead of resolving through pip, and
    every pin carries its hash so that mode has nothing to fall back to. Every
    dependency `pyproject.toml` declares for that set is in the lock, so a
    dependency added without regenerating it fails here rather than going
    unaudited.
    """
    audit = workflow_document(WORKFLOW)["jobs"]["audit"]
    audits = [line for line in run_lines(audit) if line.startswith("pip-audit")]

    assert audits == ["pip-audit --require-hashes -r requirements/production.txt"], run_lines(audit)

    lock = PRODUCTION_LOCK.read_text(encoding="utf-8")
    recorded = re.search(r"^#\s+(uv pip compile .*)$", lock, flags=re.MULTILINE)
    assert recorded is not None, lock[:200]
    command = shlex.split(recorded.group(1))
    assert "--generate-hashes" in command and "--universal" in command, command
    assert "pyproject.toml" in command, command
    extras = [command[index + 1] for index, word in enumerate(command) if word == "--extra"]
    assert sorted(extras) == ["can", "pyocd"], extras

    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras_declared = project["optional-dependencies"]
    declared = project["dependencies"] + extras_declared["can"] + extras_declared["pyocd"]
    pins = locked_pins(PRODUCTION_LOCK)
    for requirement in declared:
        name = re.match(r"[A-Za-z0-9._-]+", requirement).group(0).lower().replace("_", "-")
        assert name in pins, (requirement, sorted(pins))
    for name, tail in pins.items():
        assert any("--hash=sha256:" in line for line in tail), name


# The nightly bench job, held to the rules that keep a machine with a board on
# it out of reach of code nobody reviewed.

# The demo the bench job runs, and the directory the bench's authoritative
# configuration is bound to by its mandatory absolute `workspace_root`. A job
# that ran the CLI anywhere else would be refused rather than reading another
# configuration.
DEMO_DIRECTORY = "examples/nucleo-f446re_demo"

# How the machine is named, and the whole of how it is named: the bench label,
# and the board label that says which board it holds.
BENCH_LABELS = ["self-hosted", "agentic-hil", "nucleo-f446re"]

# This repository's own bench queue. A GitHub concurrency group is scoped to one
# repository, so this serialises this workflow's own runs and nothing across
# repositories; the single self-hosted runner (see BENCH_LABELS) is what keeps
# the STM32 starter and this one off the same board.
BENCH_CONCURRENCY_GROUP = "nucleo-f446re-bench"

# Every trigger that would put code from a head repository, or from a branch
# nobody has merged, on that machine.
UNTRUSTED_TRIGGERS = ("pull_request", "pull_request_target", "workflow_run")

# `owner/name@<commit>`. The tag the commit stood for lives in a trailing YAML
# comment the parser has already dropped by the time this reads the value.
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")

# Reaching the board without the tool: a debugger driven directly, a serial line
# opened behind the CLI's back, a CAN interface poked at by hand. Any of these
# in a step would be a hardware action nothing validated, nothing locked and
# nothing recorded.
RAW_HARDWARE = re.compile(
    r"\b(openocd|st-?flash|st-?info|STM32_Programmer_CLI|pyocd|arm-none-eabi-gdb|gdbserver"
    r"|minicom|picocom|stty|cansend|candump|cangen|ip\s+link)\b",
    re.IGNORECASE,
)

# Anything that pulls bytes off the network in a shell step. The bench's
# installation is the operator's, and a bench may be deliberately offline.
FETCH_TOOL = re.compile(r"(?:^|[\s|(`$])(?:curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b")


def workflow_document(path: Path) -> dict:
    """One workflow, parsed, or a skip where this checkout has no `.github/`."""
    if not path.is_file():
        pytest.skip(f"{path.name} is repository content and this checkout has none")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def triggers(workflow: dict) -> dict:
    """The workflow's `on:` block.

    PyYAML implements YAML 1.1, where a bare `on` is the boolean true, so the key
    this reads is `True` and not the string somebody typed. Asking for both keeps
    the test honest whichever way a future parser resolves it.
    """
    return workflow.get("on", workflow.get(True, {}))


def unpinned_actions(workflow: dict) -> list[str]:
    """Every `uses:` in the document that names a mutable reference."""
    found = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            used = step.get("uses")
            if used is not None and not PINNED_ACTION.match(used):
                found.append(used)
    return found


def run_lines(job: dict) -> list[str]:
    """Every command line the job runs, comments and blank lines dropped."""
    lines = []
    for step in job.get("steps", []):
        for raw in step.get("run", "").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines


def line_index(lines: list[str], needle: str) -> int:
    for index, line in enumerate(lines):
        if needle in line:
            return index
    raise AssertionError(f"no step runs {needle!r}: {lines}")


def bench_job() -> dict:
    return workflow_document(BENCH_WORKFLOW)["jobs"]["bench"]


def test_the_bench_workflow_never_subscribes_to_a_pull_request() -> None:
    """The rule a self-hosted runner cannot recover from getting wrong.

    The runner executes whatever the checked-out branch says, on a machine with a
    board wired to it. A `pull_request` trigger would hand that machine to
    whoever opened the pull request; `pull_request_target` and `workflow_run`
    hand base-repository trust to head-repository code, which is the same thing
    in another spelling. The bench is reached by merging first and then by the
    schedule or by a maintainer, and nothing else.
    """
    subscribed = set(triggers(workflow_document(BENCH_WORKFLOW)))

    assert subscribed == {"workflow_dispatch", "schedule"}
    for trigger in UNTRUSTED_TRIGGERS:
        assert trigger not in subscribed, trigger


def test_the_bench_workflow_asks_the_board_for_one_run_a_night() -> None:
    """One nightly cron, not an hourly one and not a weekly one.

    The minute is deliberately not the top of an hour, which is when every
    scheduled workflow on the service asks at once, but the property held here is
    the cadence: every day, once.
    """
    schedule = triggers(workflow_document(BENCH_WORKFLOW))["schedule"]

    assert len(schedule) == 1, schedule
    minute, hour, day_of_month, month, day_of_week = schedule[0]["cron"].split()
    assert (day_of_month, month, day_of_week) == ("*", "*", "*"), schedule[0]
    assert minute.isdigit() and hour.isdigit(), schedule[0]


def test_the_bench_job_selects_its_runner_by_label_alone() -> None:
    """No host name, no user and no address: three labels and a time limit.

    These labels are also the cross-repository serialisation this bench relies
    on. One self-hosted runner is registered on the machine wired to the board,
    it runs one job at a time, and the STM32 starter's hardware workflow selects
    the same runner by the same labels, so a run from either repository waits in
    that one runner's queue while the other holds the board. The concurrency
    group cannot do this, it is scoped to one repository.
    """
    job = bench_job()

    assert job["runs-on"] == BENCH_LABELS
    assert job["timeout-minutes"] == 20


def test_a_forks_schedule_never_goes_looking_for_this_bench() -> None:
    """A fork inherits the file and the schedule, and has no runner behind it."""
    condition = " ".join(bench_job()["if"].split())

    assert condition == "github.repository == 'agentic-hil/agentic-hil'"


def test_the_bench_workflow_reads_the_repository_and_nothing_more() -> None:
    assert workflow_document(BENCH_WORKFLOW)["permissions"] == {"contents": "read"}


def test_the_bench_serialises_its_own_runs_and_is_never_cancelled_mid_plan() -> None:
    """This repository's own runs share one queue, and a plan is never stopped
    half way through it.

    A GitHub concurrency group only coordinates runs within one repository, so
    this group serialises this workflow's own runs, a manual dispatch that lands
    while the nightly is on the board, and not the STM32 starter's; the single
    self-hosted runner selected by BENCH_LABELS is what serialises the two
    repositories on one board (see that test). Cancelling in progress is how a
    board is left half flashed, so the group never does.
    """
    concurrency = workflow_document(BENCH_WORKFLOW)["concurrency"]

    assert concurrency["group"] == BENCH_CONCURRENCY_GROUP
    assert concurrency["cancel-in-progress"] is False


def test_the_bench_workflow_pins_every_action_by_commit() -> None:
    """A tag moves; a commit does not. This job runs on a machine with a board."""
    assert unpinned_actions(workflow_document(BENCH_WORKFLOW)) == []


@pytest.mark.parametrize(
    "used",
    ["actions/checkout@v7", "actions/checkout@main", "actions/checkout", "actions/upload-artifact@v7.0.1"],
)
def test_a_mutable_action_reference_is_caught(used: str) -> None:
    """The predicate has to be able to fail, or the test above proves nothing."""
    workflow = {"jobs": {"only": {"steps": [{"uses": used}]}}}

    assert unpinned_actions(workflow) == [used]


def test_a_commit_pinned_action_is_accepted() -> None:
    pinned = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    workflow = {"jobs": {"only": {"steps": [{"uses": pinned}, {"run": "echo hello"}]}}}

    assert unpinned_actions(workflow) == []


def test_the_bench_job_runs_the_demo_where_its_configuration_is_bound() -> None:
    """`workspace_root` is absolute and mandatory, and it names this directory."""
    assert bench_job()["defaults"]["run"]["working-directory"] == DEMO_DIRECTORY


def test_the_bench_job_runs_the_demo_the_way_its_readme_documents_it() -> None:
    """Doctor, then the build, then the declared plan, then the pytest variant.

    Doctor before the plan because an unreadable bench is a clearer failure there
    than half way through a run, and the evidence after the plan because it is
    built from that run's own report.
    """
    lines = run_lines(bench_job())

    doctor = line_index(lines, "agentic-hil doctor")
    plan = line_index(lines, "agentic-hil test-reactor --test-config testconfig.yaml")
    suite = line_index(lines, "pytest tests/")
    evidence = line_index(lines, "agentic-hil run-evidence")
    assert doctor < plan < suite < evidence, lines
    assert "cmake --preset Debug" in lines
    assert "cmake --build --preset Debug" in lines


def test_the_evidence_is_built_from_this_runs_own_report() -> None:
    """Never the shared `last-report.json`, which a refused plan never writes.

    A plan refused before its first step leaves the shared file holding an
    earlier run's report, so a job that read it would publish that report under
    this run's name. `--json` is written on every path, a refusal included.
    """
    lines = run_lines(bench_job())
    plan = lines[line_index(lines, "agentic-hil test-reactor")]
    evidence = lines[line_index(lines, "agentic-hil run-evidence")]

    assert "--json" in plan
    report = plan.split(">", 1)[1].strip()
    assert report, plan
    assert f"--report {report}" in evidence
    assert " --out " in evidence
    assert "last-report.json" not in evidence


def test_every_hardware_action_on_the_bench_goes_through_the_cli() -> None:
    """No raw debugger command, no serial line opened behind the tool's back.

    A hardware action outside the CLI is one nothing validated against the
    configuration, nothing locked against a second run, and nothing recorded in
    the audit chain.
    """
    lines = run_lines(bench_job())

    assert [line for line in lines if line.startswith("agentic-hil ")], lines
    for line in lines:
        assert not RAW_HARDWARE.search(line), line


def test_the_bench_job_installs_nothing_and_downloads_nothing() -> None:
    """The installation on that machine is the operator's, and may be offline."""
    for line in run_lines(bench_job()):
        assert "pip install" not in line, line
        assert not FETCH_TOOL.search(line), line


@pytest.mark.parametrize(
    "command",
    [
        "openocd -f board/st_nucleo_f4.cfg -c 'program firmware.elf'",
        "STM32_Programmer_CLI -c port=SWD -w firmware.elf",
        "stty -F /dev/ttyACM0 115200",
        "candump can0",
    ],
)
def test_a_raw_hardware_command_is_caught(command: str) -> None:
    """The predicate has to be able to fail, or the test above proves nothing."""
    assert RAW_HARDWARE.search(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "agentic-hil doctor",
        "agentic-hil test-reactor --test-config testconfig.yaml",
        "cmake --build --preset Debug",
        "pytest tests/",
    ],
)
def test_a_command_that_goes_through_the_tool_is_accepted(command: str) -> None:
    assert RAW_HARDWARE.search(command) is None
    assert FETCH_TOOL.search(command) is None


def test_the_bench_run_uploads_its_evidence_whatever_the_run_did() -> None:
    """The red run is the one whose evidence matters most, and it is kept."""
    upload = [step for step in bench_job()["steps"] if "upload-artifact" in str(step.get("uses", ""))]

    assert len(upload) == 1, upload
    assert upload[0]["if"] == "always()"
    assert upload[0]["with"]["retention-days"] == 14
    assert f"{DEMO_DIRECTORY}/artifacts/" in upload[0]["with"]["path"]
