"""The shipped CI examples, held to what they promise a stranger.

`examples/ci/github-actions.yml` and `examples/ci/gitlab-ci.yml` are copied into
other people's repositories, where nothing checks them again. A drifted version
pin becomes a version somebody installs; a dropped refusal becomes a fork's
pull request executing on a machine with a board attached to it. So the two
files are parsed here rather than read, and every claim the issue behind them
made is a test: the pin equals the release the version gate holds, the hardware
job refuses a hosted runner and a foreign event, nothing any step runs fetches
something no version decides, and the evidence for a run is collected before
the next plan overwrites the report it comes from.

Two shapes of test, on purpose. The predicates below are pure functions over
text and parsed documents, exercised against synthetic inputs that must fail, so
a check that could no longer catch anything says so; and the real files are then
put through the same predicates.

`tools/` is repository tooling that never ships, and this module imports the
version gate out of it, so MANIFEST.in excludes this file from the source
distribution for the reason it excludes the other two.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from check_version_consistency import locations  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GITHUB_EXAMPLE = REPOSITORY_ROOT / "examples" / "ci" / "github-actions.yml"
GITLAB_EXAMPLE = REPOSITORY_ROOT / "examples" / "ci" / "gitlab-ci.yml"
PAGE = REPOSITORY_ROOT / "docs" / "ci-examples.md"
CHANGELOG = REPOSITORY_ROOT / "CHANGELOG.md"
# The argparse surface of the release the examples pin, recorded from that
# release as the package index serves it.
RECORDING = REPOSITORY_ROOT / "tests" / "fixtures" / "published_cli_surface.json"
# `## [X.Y.Z] - YYYY-MM-DD`: a release with the date it was published.
DATED_RELEASE_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}\s*$", re.MULTILINE)

# The label pair the trust rule names: the machine is a bench, and this is which
# board it holds.
BENCH_LABEL = "agentic-hil"
BOARD_LABEL = "nucleo-f446re"

# Anything that pulls bytes off the network in a shell step. A pinned action is
# a `uses:` and is checked separately; these are the spellings that reach past
# the pin, and `| sh` is how the pipe-a-script install is written.
FETCH_TOOL = re.compile(r"(?:^|[\s|(`$])(?:curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b")
# `owner/name@<commit>`, with the tag it stood for left in a YAML comment that
# the parser has already dropped by the time this reads the value.
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def triggers(workflow: dict) -> dict:
    """The workflow's `on:` block.

    PyYAML implements YAML 1.1, where a bare `on` is the boolean true, so the
    key this reads is `True` and not the string somebody typed. Asking for both
    keeps the test honest whichever way a future parser resolves it.
    """
    return workflow.get("on", workflow.get(True, {}))


def steps(workflow: dict, job: str) -> list[dict]:
    return list(workflow["jobs"][job]["steps"])


def github_commands(workflow: dict, job: str) -> list[str]:
    return [step["run"] for step in steps(workflow, job) if "run" in step]


def gitlab_commands(pipeline: dict, job: str) -> list[str]:
    """Everything the job runs, in the order the runner runs it."""
    definition = pipeline[job]
    ordered: list[str] = []
    for key in ("before_script", "script", "after_script"):
        ordered.extend(definition.get(key, []))
    return ordered


def unpinned_downloads(commands: Iterable[str]) -> list[str]:
    """Every command line that fetches something no version decides.

    A pinned install is a version somebody reviewed. A fetch of a script, or an
    install with no `==`, is whatever the network held at the moment the job
    ran, which is the one thing a bench with a board attached must not execute.
    """
    found = []
    for command in commands:
        for line in command.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fetches = FETCH_TOOL.search(stripped) is not None
            installs_a_range = "pip install" in stripped and "==" not in stripped
            if fetches or installs_a_range:
                found.append(stripped)
    return found


def unpinned_actions(workflow: dict) -> list[str]:
    """Every `uses:` that names a mutable reference instead of a commit."""
    found = []
    for job in workflow["jobs"]:
        for step in steps(workflow, job):
            used = step.get("uses")
            if used is not None and not PINNED_ACTION.match(used):
                found.append(used)
    return found


# A call to the tool: the program name, whitespace, and a subcommand that starts
# with a letter. A `--flag` after the name (`agentic-hil --version`) is not a
# subcommand and does not match, and `.agentic-hil/reports/` is a path, not a
# call, because nothing follows the name but a slash.
INVOKED_SUBCOMMAND = re.compile(r"\bagentic-hil\s+([a-z][a-z-]*)")


def newest_published_release(changelog: str) -> str:
    """The newest release CHANGELOG.md gives a date: the newest one the package index carries."""
    found = DATED_RELEASE_HEADING.search(changelog)
    assert found is not None, "CHANGELOG.md dates no release"
    return found.group(1)


def recorded_surface() -> dict:
    """The committed recording of the pinned release's command surface."""
    assert RECORDING.is_file(), (
        f"{RECORDING.relative_to(REPOSITORY_ROOT).as_posix()} does not exist: nothing records the command "
        "surface of the release the examples pin"
    )
    return json.loads(RECORDING.read_text(encoding="utf-8"))


def synthetic_recording(version: str, subcommands: Iterable[str]) -> dict:
    """A recording in the committed file's shape, with made-up content."""
    return {
        "version": version,
        "source": "Made up for a test; no release was installed.",
        "surface": {
            "options": ["--help", "--version", "-h"],
            "positionals": [],
            "subcommands": {
                name: {"options": ["--help", "-h"], "positionals": [], "subcommands": {}} for name in subcommands
            },
        },
    }


def surface_problems(recording: dict, pin: str, invoked: Iterable[str]) -> list[str]:
    """Every way a recorded command surface fails to vouch for the examples' pin.

    The recording is the argparse tree of one published release. It speaks for
    the pin only when it is that release, and a copied example runs only when
    every subcommand it invokes is in that tree.
    """
    recorded = recording.get("version")
    found = []
    if recorded != pin:
        found.append(f"the recorded command surface is agentic-hil {recorded}, but the examples pin {pin}")
    defined = set(recording.get("surface", {}).get("subcommands", {}))
    for command in sorted(set(invoked) - defined):
        found.append(
            f"the examples invoke `agentic-hil {command}`, which the recorded {recorded} surface does not define"
        )
    return found


def invoked_subcommands(commands: Iterable[str]) -> set[str]:
    """Every `agentic-hil <subcommand>` a set of shell command lines invokes."""
    found: set[str] = set()
    for command in commands:
        for line in command.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            found.update(INVOKED_SUBCOMMAND.findall(stripped))
    return found


def first_command_index(commands: list[str], needle: str) -> int | None:
    for index, command in enumerate(commands):
        if needle in command:
            return index
    return None


def refusing_rules(rules: list[dict]) -> list[dict]:
    return [rule for rule in rules if rule.get("when") == "never"]


def first_admitting_index(rules: list[dict]) -> int:
    """Where GitLab stops reading, since it takes the first rule that matches.

    A refusal after that point refuses nothing, which is the way this kind of
    list is usually got wrong.
    """
    for index, rule in enumerate(rules):
        if rule.get("when") != "never":
            return index
    return len(rules)


# The two documents, parsed once. A parse failure here is the first thing the
# issue asked for and it fails at collection, loudly, rather than in one test.
GITHUB = load(GITHUB_EXAMPLE)
GITLAB = load(GITLAB_EXAMPLE)


def test_both_examples_parse_into_the_jobs_they_promise() -> None:
    """Two paths, in one file each, so a reader sees the split without hunting."""
    assert set(GITHUB["jobs"]) == {"hardware", "check-plan"}
    assert {"hardware", "check-plan"} <= set(GITLAB)
    assert GITLAB["stages"] == ["check-plan", "hardware"]


def test_the_pinned_version_is_the_newest_published_release() -> None:
    """One number, in three files, equal to the newest release the package index carries.

    Not the release this tree builds toward: between releases the index does not
    carry that one, so a reader who copied either file got a pipeline whose first
    job stopped at the install with a resolver error (#525). The newest release
    CHANGELOG.md dates is on the index, and on a release commit it is that
    release itself. A range or a `latest` here would mean the board-free job and
    the bench were checking different code.
    """
    published = newest_published_release(CHANGELOG.read_text(encoding="utf-8"))

    assert GITHUB["env"]["AGENTIC_HIL_VERSION"] == published
    assert GITLAB["variables"]["AGENTIC_HIL_VERSION"] == published
    # The page prints the same line a reader copies, so the two cannot drift
    # apart while both still look right on their own.
    assert f'AGENTIC_HIL_VERSION: "{published}"' in PAGE.read_text(encoding="utf-8")


def test_a_release_stamps_the_examples_along_with_everything_else() -> None:
    """The pin follows the release because the version gate carries it.

    Without an entry here the examples would be the one place the release moves
    past, and `uncovered_files` would only say so until somebody excused it. The
    three files state the newest published release, as every other release
    position does, so the release stamp moves them and the development bump
    leaves them where the index can serve them.
    """
    published = newest_published_release(CHANGELOG.read_text(encoding="utf-8"))
    covered = {location.path: location for location in locations(REPOSITORY_ROOT)}

    for relative in ("examples/ci/github-actions.yml", "examples/ci/gitlab-ci.yml", "docs/ci-examples.md"):
        assert relative in covered, relative
        assert covered[relative].versions, relative
        assert set(covered[relative].versions) == {published}, covered[relative]


def test_the_github_hardware_job_refuses_a_hosted_runner() -> None:
    """`runs-on` selects the bench; the guard step is what survives an edit to it."""
    job = GITHUB["jobs"]["hardware"]

    assert job["runs-on"][0] == "self-hosted"
    assert BENCH_LABEL in job["runs-on"]
    assert BOARD_LABEL in job["runs-on"]
    guard = [command for command in github_commands(GITHUB, "hardware") if "RUNNER_ENVIRONMENT" in command]
    assert len(guard) == 1, guard
    assert "self-hosted" in guard[0]
    assert "exit 1" in guard[0]


def test_the_github_hardware_job_refuses_every_untrusted_event() -> None:
    """The trust rule's fourth condition, as an `if:` a copy of the file keeps.

    `pull_request_target` and `workflow_run` are refused by name because both
    hand base-repository trust to head-repository code, and a fork's pull
    request is refused by comparing the head repository with this one.
    """
    condition = " ".join(GITHUB["jobs"]["hardware"]["if"].split())

    assert "github.event_name != 'pull_request_target'" in condition
    assert "github.event_name != 'workflow_run'" in condition
    assert "github.event.pull_request.head.repo.full_name == github.repository" in condition
    # Joined, not offered as alternatives: one `||` and it belongs to the pull
    # request clause, which reads "not a pull request, or a pull request from
    # here".
    assert condition.count("&&") >= 2, condition
    assert condition.count("||") == 1, condition


def test_the_github_example_subscribes_to_no_event_it_would_have_to_refuse() -> None:
    """The refusal is the second line. Not subscribing is the first."""
    subscribed = set(triggers(GITHUB))

    assert subscribed == {"push", "pull_request", "workflow_dispatch"}


def test_the_gitlab_hardware_job_reaches_the_bench_only_through_its_tags() -> None:
    """GitLab has no `RUNNER_ENVIRONMENT`, so the tags are the whole selection."""
    job = GITLAB["hardware"]

    assert job["tags"] == [BENCH_LABEL, BOARD_LABEL]
    assert "tags" not in GITLAB["check-plan"]


def test_the_gitlab_hardware_job_refuses_every_untrusted_source() -> None:
    """The same four refusals, in the vocabulary GitLab has for them.

    Order is the half that is easy to get wrong: GitLab takes the first rule
    that matches, so a refusal below an admitting rule refuses nothing.
    """
    rules = GITLAB["hardware"]["rules"]
    refusals = " | ".join(str(rule.get("if", "")) for rule in refusing_rules(rules))

    assert "$CI_MERGE_REQUEST_SOURCE_PROJECT_ID != $CI_PROJECT_ID" in refusals
    assert 'external_pull_request_event"' in refusals
    assert '$CI_PIPELINE_SOURCE == "pipeline"' in refusals
    admitting = first_admitting_index(rules)
    for index, rule in enumerate(rules):
        if rule.get("when") == "never" and rule.get("if") is not None:
            assert index < admitting, f"rule {index} refuses below the first rule that admits: {rule}"
    # Default deny: an event nobody thought of does not reach a board.
    assert rules[-1] == {"when": "never"}


def test_the_bench_is_serialised_and_a_run_is_never_cancelled_mid_plan() -> None:
    """Cancelling a plan half way is how a board is left half flashed."""
    concurrency = GITHUB["jobs"]["hardware"]["concurrency"]

    assert BOARD_LABEL in concurrency["group"]
    assert concurrency["cancel-in-progress"] is False
    assert BOARD_LABEL in GITLAB["hardware"]["resource_group"]
    assert GITLAB["hardware"]["interruptible"] is False


def test_doctor_runs_before_any_plan_gets_near_the_board() -> None:
    """An unreadable bench is a clearer failure here than half way through a plan."""
    for commands in (github_commands(GITHUB, "hardware"), gitlab_commands(GITLAB, "hardware")):
        doctor = first_command_index(commands, "agentic-hil doctor")
        plan = first_command_index(commands, "agentic-hil test-reactor")
        assert doctor is not None, commands
        assert plan is not None, commands
        assert doctor < plan, commands


def test_every_plan_captures_its_own_report_and_never_the_shared_one() -> None:
    """One command each, in the same block, and never `last-report.json`.

    A plan refused before its first step, or refused for having no configuration
    at all, writes no `.agentic-hil/reports/last-report.json`, so a job that fed
    that shared file to `run-evidence` would publish the previous plan's report
    under the refused plan's name, and a first plan refused would leave the whole
    bundle empty. Each run's report is captured from `--json` into its own file,
    and that file, never the shared one, is what the evidence is built from;
    `--json` is written on every path a refusal included, so the capture is never
    empty. Being in the same block is what keeps the two commands from drifting
    when a plan is added.
    """
    for commands in (github_commands(GITHUB, "hardware"), gitlab_commands(GITLAB, "hardware")):
        running = [command for command in commands if "agentic-hil test-reactor" in command]
        assert len(running) == 1, running
        block = running[0]
        assert "--test-config" in block
        assert "--junit-xml" in block
        # The report is captured from --json into a per-plan file...
        assert "--json" in block
        assert 'report="artifacts/reports/' in block
        assert '> "$report"' in block
        # ...and that captured file, never the shared last-report.json, is the
        # evidence's input.
        assert 'agentic-hil run-evidence --report "$report"' in block
        assert " --out " in block
        assert "last-report.json" not in block


def test_github_does_not_append_the_job_summary_a_second_time() -> None:
    """`run-evidence` appends `job-summary.md` to `$GITHUB_STEP_SUMMARY` itself.

    A `cat` of the same file into the same variable in the workflow would put
    every plan's table and verdict on the job page twice, so the GitHub job does
    not touch `GITHUB_STEP_SUMMARY` at all. GitLab has no such variable, so
    `run-evidence` appends nowhere there and that example prints the summary into
    the log with `cat` on purpose: the same document, one destination each.
    """
    github = " ".join(github_commands(GITHUB, "hardware"))
    assert "GITHUB_STEP_SUMMARY" not in github
    assert "job-summary.md" not in github

    gitlab = " ".join(gitlab_commands(GITLAB, "hardware"))
    assert "cat " in gitlab
    assert "job-summary.md" in gitlab


def test_the_evidence_is_uploaded_whatever_the_run_did() -> None:
    """The red run is the one whose evidence matters most."""
    upload = [step for step in steps(GITHUB, "hardware") if "upload-artifact" in str(step.get("uses", ""))]
    assert len(upload) == 1, upload
    assert upload[0]["if"] == "always()"
    assert "artifacts/" in upload[0]["with"]["path"]

    artifacts = GITLAB["hardware"]["artifacts"]
    assert artifacts["when"] == "always"
    assert "artifacts/" in artifacts["paths"]
    assert artifacts["reports"]["junit"].startswith("artifacts/junit-")


def test_the_check_plan_job_is_the_board_free_path_and_touches_no_bench() -> None:
    """The other half of the file: no board, so any runner, and a real check.

    The check is `check-plan`, which loads each plan through the reactor's own
    loader, not a schema-only reader that would pass a plan the reactor then
    refuses. And it is never `test-reactor`, which drives the board.
    """
    board_free = GITHUB["jobs"]["check-plan"]

    assert "self-hosted" not in board_free["runs-on"]
    assert "concurrency" not in board_free
    installed = " ".join(github_commands(GITHUB, "check-plan"))
    assert "agentic-hil check-plan" in installed
    assert "agentic-hil test-reactor" not in installed
    gitlab_board_free = " ".join(gitlab_commands(GITLAB, "check-plan"))
    assert "agentic-hil check-plan" in gitlab_board_free
    assert "agentic-hil test-reactor" not in gitlab_board_free


def test_every_command_the_examples_invoke_is_one_the_pinned_release_defines() -> None:
    """No example invokes an `agentic-hil` subcommand the pinned release lacks.

    The pin names a published release, so this checkout's parser no longer
    stands in for it: between releases this tree may define a command the
    release a reader installs does not. What the examples are held to is a
    committed recording of that release's own argparse tree, taken from the
    distribution the package index serves and carrying the version it was taken
    from, which has to be the pin. The cost is the one #525 accepted: a release
    that adds a command the examples demonstrate has to ship before the examples
    can show it, which delays a demonstration rather than shipping a file that
    cannot run. The publish workflow's `verify-published-examples` job stays the
    alarm after an upload: it installs the exact pinned distribution and asks its
    CLI to answer for every command the examples invoke.
    """
    recording = recorded_surface()
    pin = GITHUB["env"]["AGENTIC_HIL_VERSION"]
    invoked = invoked_subcommands(
        [
            *github_commands(GITHUB, "hardware"),
            *github_commands(GITHUB, "check-plan"),
            *gitlab_commands(GITLAB, "hardware"),
            *gitlab_commands(GITLAB, "check-plan"),
        ]
    )

    # The evidence flow the examples exist to show; a check that stopped finding
    # these would be finding nothing.
    assert {"doctor", "test-reactor", "check-plan", "run-evidence"} <= invoked
    assert GITLAB["variables"]["AGENTIC_HIL_VERSION"] == pin
    assert surface_problems(recording, pin, invoked) == []


def test_the_recording_names_its_release_and_where_it_came_from() -> None:
    """A recording that cannot say what it recorded is a list somebody typed."""
    recording = recorded_surface()

    assert recording["version"] == newest_published_release(CHANGELOG.read_text(encoding="utf-8"))
    assert isinstance(recording["source"], str)
    assert recording["source"].strip()
    assert recording["surface"]["subcommands"], "the recording holds no subcommands"


def test_an_invented_subcommand_would_be_caught() -> None:
    """The predicate has to be able to fail, or the test above proves nothing."""
    recording = synthetic_recording("1.2.3", ["doctor"])
    invoked = invoked_subcommands(["agentic-hil doctor", "agentic-hil not-a-real-command"])

    found = surface_problems(recording, "1.2.3", invoked)

    assert {"doctor", "not-a-real-command"} <= invoked
    assert len(found) == 1, found
    assert "`agentic-hil not-a-real-command`" in found[0]


def test_a_command_the_pinned_release_does_not_ship_yet_is_refused() -> None:
    """A command the pinned release lacks is refused, so the demonstration waits for the release.

    A recording of a release from before `check-plan` and `run-evidence` shipped
    refuses the examples as they stand, naming both.
    """
    recording = synthetic_recording("1.2.3", ["doctor", "test-reactor"])
    invoked = invoked_subcommands(
        [
            *github_commands(GITHUB, "hardware"),
            *github_commands(GITHUB, "check-plan"),
            *gitlab_commands(GITLAB, "hardware"),
            *gitlab_commands(GITLAB, "check-plan"),
        ]
    )

    found = surface_problems(recording, "1.2.3", invoked)

    assert len(found) == 2, found
    assert "`agentic-hil check-plan`" in found[0]
    assert "`agentic-hil run-evidence`" in found[1]


def test_a_recording_of_another_release_than_the_pin_is_refused() -> None:
    """A surface speaks for the release it was taken from and for no other.

    A release stamp that moved the pin without recording the new release again
    would leave the examples held to the commands of the release before.
    """
    recording = synthetic_recording("1.2.3", ["doctor", "test-reactor", "check-plan", "run-evidence"])

    found = surface_problems(recording, "1.2.4", {"doctor"})

    assert len(found) == 1, found
    assert "1.2.3" in found[0]
    assert "1.2.4" in found[0]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("agentic-hil doctor", {"doctor"}),
        ("agentic-hil test-reactor --test-config plan.yaml --json > report", {"test-reactor"}),
        ('agentic-hil run-evidence --report "$report" --out out', {"run-evidence"}),
        # A flag after the name is not a subcommand.
        ('installed="$(agentic-hil --version)"', set()),
        # A path is not a call: nothing follows the name but a slash.
        ("ls .agentic-hil/reports/", set()),
        # A commented line runs nothing.
        ("# agentic-hil doctor, in a comment", set()),
    ],
)
def test_the_subcommand_extractor_reads_calls_and_not_flags_or_paths(command: str, expected: set[str]) -> None:
    assert invoked_subcommands([command]) == expected


def test_no_step_in_either_example_downloads_something_unpinned() -> None:
    """A bench executes what these lines say. Nothing here is decided by a clock."""
    commands = [
        *github_commands(GITHUB, "hardware"),
        *github_commands(GITHUB, "check-plan"),
        *gitlab_commands(GITLAB, "hardware"),
        *gitlab_commands(GITLAB, "check-plan"),
    ]

    assert unpinned_downloads(commands) == []
    assert unpinned_actions(GITHUB) == []


def test_the_page_says_what_the_runner_provides_and_what_the_run_leaves_behind() -> None:
    """The short page the issue asked for, checked for the answers it owes."""
    page = PAGE.read_text(encoding="utf-8")

    # What the runner has to provide, once, outside the workspace.
    assert "agentic-hil init" in page
    assert "workspace_root" in page
    assert "AGENTIC_HIL_CONFIG" in page
    # The evidence command and everything it writes, beside the JUnit file.
    assert "agentic-hil run-evidence --report" in page
    assert "run-summary.json" in page
    assert "job-summary.md" in page
    assert "logs/" in page
    assert "--junit-xml" in page
    # Which of the trust rule's conditions a workflow can state, and which only
    # the repository settings can.
    assert "RUNNER_ENVIRONMENT" in page
    assert "pull_request_target" in page
    assert "workflow_run" in page
    assert "external_pull_request_event" in page
    assert "Runner groups" in page
    # Both examples are named, so the page is a way in rather than a summary.
    assert "examples/ci/github-actions.yml" in page
    assert "examples/ci/gitlab-ci.yml" in page


@pytest.mark.parametrize(
    "command",
    [
        "curl -LsSf https://example.invalid/install.sh | sh",
        "wget -qO- https://example.invalid/install.sh | bash",
        "iwr https://example.invalid/install.ps1 | iex",
        "python -m pip install agentic-hil",
        "python -m pip install --upgrade agentic-hil",
    ],
)
def test_an_unpinned_download_is_caught(command: str) -> None:
    """The predicate has to be able to fail, or the test above proves nothing."""
    assert unpinned_downloads([command]) == [command]


@pytest.mark.parametrize(
    "command",
    [
        'python -m pip install "agentic-hil==1.2.3"',
        "agentic-hil doctor",
        "cmake --build --preset Debug",
        "# curl is named in a comment and nothing runs it",
    ],
)
def test_a_pinned_or_local_command_is_accepted(command: str) -> None:
    assert unpinned_downloads([command]) == []


@pytest.mark.parametrize(
    "used",
    ["actions/checkout@v7", "actions/checkout@main", "actions/checkout", "github/codeql-action/init@v4.37.8"],
)
def test_a_mutable_action_reference_is_caught(used: str) -> None:
    workflow = {"jobs": {"only": {"steps": [{"uses": used}]}}}

    assert unpinned_actions(workflow) == [used]


def test_a_commit_pinned_action_is_accepted() -> None:
    pinned = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    workflow = {"jobs": {"only": {"steps": [{"uses": pinned}, {"run": "echo hello"}]}}}

    assert unpinned_actions(workflow) == []


def test_a_refusal_below_an_admitting_rule_is_where_the_reading_stopped() -> None:
    """The ordering property the GitLab test rests on, shown failing.

    GitLab takes the first rule that matches, so this list admits a fork's merge
    request and never reads the refusal underneath it.
    """
    rules = [
        {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"', "when": "on_success"},
        {"if": "$CI_MERGE_REQUEST_SOURCE_PROJECT_ID != $CI_PROJECT_ID", "when": "never"},
    ]

    assert first_admitting_index(rules) == 0
    assert refusing_rules(rules) == [rules[1]]
