from __future__ import annotations

import ast
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from check_version_consistency import (  # noqa: E402
    UNTRACKED_MENTIONS,
    contract_problems,
    locations,
    main,
    package_version,
    problems,
    render_list,
    unclaimed_pins,
    uncovered_files,
    version_problems,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHECK = "tools/check_version_consistency.py"


def test_the_gate_runs_on_every_python_this_project_supports() -> None:
    """tomllib is stdlib only from 3.11 and this project still supports 3.10.

    The version gate now blocks the merge, so a gate that cannot import on a
    matrix leg would block every merge on that leg instead of catching anything.
    """
    source = (REPOSITORY_ROOT / CHECK).read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])

    assert "tomllib" not in modules
    assert modules <= {"__future__", "ast", "json", "re", "sys", "dataclasses", "pathlib"}, sorted(modules)


def test_this_tree_agrees_with_itself() -> None:
    assert main([str(REPOSITORY_ROOT)]) == 0


def test_the_release_carries_its_version_where_the_release_that_exposed_this_carried_it() -> None:
    """Every position issue hardci-hq#75 counted, including the five it found missing."""
    covered = {location.path for location in locations(REPOSITORY_ROOT)}

    assert covered >= {
        "pyproject.toml",
        "src/agentic_hil/__init__.py",
        "server.json",
        "plugins/agentic-hil/.claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        "CHANGELOG.md",
        "src/agentic_hil/skills/agentic-hil/SKILL.md",
        "plugins/agentic-hil/skills/agentic-hil/SKILL.md",
        # The five the hand-maintained checklist never named. Both matrices sat
        # at 0.4.0 for two releases and aborted every run before it started.
        "TROUBLESHOOTING.md",
        "evals/install/matrix.example.json",
        "evals/install/matrix.full.json",
        "evals/install/README.md",
    }


def test_every_stated_position_actually_carries_a_version() -> None:
    version = package_version(REPOSITORY_ROOT)
    for location in locations(REPOSITORY_ROOT):
        assert location.versions, f"{location.path} states no version"
        assert set(location.versions) == {version}, location


def test_the_troubleshooting_pins_are_all_of_them() -> None:
    """Five `agentic-hil>=` pins and one `@vX.Y.Z` git-tag pin.

    Two entries for one file, because they are two claims: the requirement
    pins the fix instructions hand a reader, and the git-tag pin hardci-hq#86
    found drifting by hand while `_requirement_pins` could not see it.
    """
    entries = [location for location in locations(REPOSITORY_ROOT) if location.path == "TROUBLESHOOTING.md"]

    assert len(entries) == 2, entries
    requirement_pins, tag_pins = entries
    assert ">=" in requirement_pins.carries
    assert len(requirement_pins.versions) == 5, requirement_pins
    assert "git-tag pin" in tag_pins.carries
    assert len(tag_pins.versions) == 1, tag_pins


def test_the_checklist_points_at_the_check_instead_of_repeating_it() -> None:
    """The list in docs drifted once. It is no longer kept there."""
    strategy = (REPOSITORY_ROOT / "docs" / "release-strategy.md").read_text(encoding="utf-8")

    assert CHECK in strategy
    assert "--list" in strategy
    # The old enumeration, which named seven of twelve positions.
    assert "and both packaged/plugin skill versions together" not in strategy


def test_the_pull_request_job_runs_the_check_without_a_paths_filter() -> None:
    """A paths filter would skip exactly the pull request that forgets a file."""
    ci = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (REPOSITORY_ROOT / ".github" / "workflows" / "workflow.yml").read_text(encoding="utf-8")

    assert f"python {CHECK}" in ci
    assert "release_metadata" in ci
    assert "pull_request" in ci
    # A `paths:` key, not the word in the comment that explains its absence.
    assert re.search(r"^\s+paths:", ci, re.MULTILINE) is None
    # Required CI is the branch-protection status check; the new job has to be
    # part of it or it blocks nothing.
    assert "needs.release_metadata.result" in ci
    # The release job runs the same module, and only the tag stays behind.
    assert f"python {CHECK} --release-tag" in release


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of the tree the check reads, cheap enough to break on purpose."""
    root = tmp_path / "repository"
    root.mkdir()
    for relative in {location.path for location in locations(REPOSITORY_ROOT)}:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPOSITORY_ROOT / relative, destination)
    return root


def test_the_copied_tree_is_a_fair_starting_point(tree: Path) -> None:
    assert version_problems(tree) == []
    assert contract_problems(tree) == []
    assert uncovered_files(tree) == []
    assert unclaimed_pins(tree) == []


@pytest.mark.parametrize(
    ("relative", "replace", "names"),
    [
        # pyproject.toml is the reference every other position is compared with,
        # so moving it alone reports the other eleven rather than itself. Either
        # way the tree is refused, which is what a release depends on.
        (
            "pyproject.toml",
            lambda text, version: text.replace(f'version = "{version}"', 'version = "9.9.9"', 1),
            "but this release is 9.9.9",
        ),
        (
            "src/agentic_hil/__init__.py",
            lambda text, version: text.replace(f'__version__ = "{version}"', '__version__ = "9.9.9"'),
            "src/agentic_hil/__init__.py",
        ),
        (
            "server.json",
            lambda text, version: text.replace(f'"version": "{version}"', '"version": "9.9.9"', 1),
            "server.json",
        ),
        (
            "plugins/agentic-hil/.claude-plugin/plugin.json",
            lambda text, version: text.replace(f'"version": "{version}"', '"version": "9.9.9"'),
            "plugins/agentic-hil/.claude-plugin/plugin.json",
        ),
        (
            ".claude-plugin/marketplace.json",
            lambda text, version: text.replace(f'"version": "{version}"', '"version": "9.9.9"'),
            ".claude-plugin/marketplace.json",
        ),
        (
            "CHANGELOG.md",
            lambda text, version: text.replace(f"## [{version}]", "## [9.9.9]", 1),
            "CHANGELOG.md",
        ),
        (
            "src/agentic_hil/skills/agentic-hil/SKILL.md",
            lambda text, version: text.replace(version, "9.9.9", 1),
            "src/agentic_hil/skills/agentic-hil/SKILL.md",
        ),
        (
            "plugins/agentic-hil/skills/agentic-hil/SKILL.md",
            lambda text, version: text.replace(f"agentic-hil=={version}", "agentic-hil==9.9.9"),
            "plugins/agentic-hil/skills/agentic-hil/SKILL.md",
        ),
        (
            "TROUBLESHOOTING.md",
            lambda text, version: text.replace(f"agentic-hil>={version}", "agentic-hil>=9.9.9", 1),
            "TROUBLESHOOTING.md",
        ),
        # The pin hardci-hq#86 found: a git tag in an install URL, which is not
        # a requirement pin and went unchecked while the file counted as covered.
        (
            "TROUBLESHOOTING.md",
            lambda text, version: text.replace(f"agentic-hil@v{version}", "agentic-hil@v0.6.0", 1),
            "TROUBLESHOOTING.md states 0.6.0",
        ),
        (
            "evals/install/matrix.example.json",
            lambda text, version: text.replace(f'"expected_version": "{version}"', '"expected_version": "9.9.9"'),
            "evals/install/matrix.example.json",
        ),
        (
            "evals/install/matrix.full.json",
            lambda text, version: text.replace(f'"expected_version": "{version}"', '"expected_version": "9.9.9"'),
            "evals/install/matrix.full.json",
        ),
        (
            "evals/install/README.md",
            lambda text, version: text.replace(f'"expected_version": "{version}"', '"expected_version": "9.9.9"'),
            "evals/install/README.md",
        ),
    ],
)
def test_one_position_out_of_step_fails(tree: Path, relative: str, replace, names: str) -> None:
    """The proof. Break the version in exactly one file and the check goes red.

    Every position in turn, because a check that covers eleven of twelve is the
    thing this replaced.
    """
    version = package_version(tree)
    path = tree / relative
    broken = replace(path.read_text(encoding="utf-8"), version)
    assert broken != path.read_text(encoding="utf-8"), f"the test did not actually change {relative}"
    path.write_text(broken, encoding="utf-8")

    found = problems(tree)

    assert found, relative
    assert any(names in problem for problem in found), found


def test_a_version_the_release_notes_mention_in_prose_is_not_a_mismatch(tree: Path) -> None:
    """CHANGELOG.md keeps every past release. Only its newest heading tracks."""
    changelog = tree / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text(encoding="utf-8") + "\n## [0.1.0] - 2020-01-01\n\nThe first one.\n",
        encoding="utf-8",
    )

    assert version_problems(tree) == []


def test_a_new_home_for_the_version_cannot_appear_unnoticed(tree: Path) -> None:
    """The half that keeps the list from drifting again.

    Extending the checklist was never enough: what failed was that a thirteenth
    file could start carrying the version with nothing to notice.
    """
    version = package_version(tree)
    (tree / "docs").mkdir(parents=True, exist_ok=True)
    (tree / "docs" / "installation.md").write_text(f'pip install "agentic-hil=={version}"\n', encoding="utf-8")

    found = uncovered_files(tree)

    assert found
    assert "docs/installation.md" in found[0]
    assert CHECK in found[0]


def test_a_deliberate_mention_is_declared_rather_than_forgotten(tree: Path) -> None:
    """`AI_AGENT_QUICKSTART.md` states a capability floor, not this release.

    It is exempt because it says so in UNTRACKED_MENTIONS with a reason, which is
    a claim a reviewer can argue with. A file merely left out of the list is not.
    """
    version = package_version(tree)
    for relative in UNTRACKED_MENTIONS:
        (tree / relative).parent.mkdir(parents=True, exist_ok=True)
        (tree / relative).write_text(f"{version} or newer is required\n", encoding="utf-8")

    assert uncovered_files(tree) == []
    assert "AI_AGENT_QUICKSTART.md" in UNTRACKED_MENTIONS


def test_a_current_tag_pin_is_recognised_and_passes(tree: Path) -> None:
    """The other half of the hardci-hq#86 test: a correct pin is not noise."""
    version = package_version(tree)
    path = tree / "TROUBLESHOOTING.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        + f'\nuv tool install "git+https://github.com/agentic-hil/agentic-hil@v{version}"\n',
        encoding="utf-8",
    )

    assert problems(tree) == []


def test_a_tag_pin_in_a_covered_file_whose_entry_does_not_claim_it_fails(tree: Path) -> None:
    """The class, not the instance, of hardci-hq#86.

    evals/install/README.md is covered, but its entry extracts expected_version
    fields — a git-tag pin there would again be a mention nothing checks, and
    `uncovered_files` would again stay silent because the file is covered.
    """
    readme = tree / "evals" / "install" / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\nuvx --from git+https://github.com/agentic-hil/agentic-hil@v0.6.0 agentic-hil --version\n",
        encoding="utf-8",
    )

    found = unclaimed_pins(tree)

    assert found
    assert "evals/install/README.md" in found[0]
    assert "@v0.6.0" in found[0]
    assert CHECK in found[0]
    assert any("@v0.6.0" in problem for problem in problems(tree))


def test_a_tag_pin_in_an_untracked_file_is_reported(tree: Path) -> None:
    """The sweep's version regex must see through the `@v` spelling."""
    version = package_version(tree)
    (tree / "docs").mkdir(parents=True, exist_ok=True)
    (tree / "docs" / "outage.md").write_text(
        f"uvx --from git+https://github.com/agentic-hil/agentic-hil@v{version} agentic-hil --version\n",
        encoding="utf-8",
    )

    found = uncovered_files(tree)

    assert found
    assert "docs/outage.md" in found[0]


def test_a_third_party_action_pin_is_not_this_projects_version(tree: Path) -> None:
    """`actions/setup-python@v5.1.0` is somebody else's version.

    The reference before the `@` must name this project, or the pin says
    nothing about this release: a stale third-party pin in a covered file must
    not fail the gate, and `@v4` is not even a release-version shape.
    """
    path = tree / "TROUBLESHOOTING.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n- uses: actions/checkout@v4\n- uses: actions/setup-python@v5.1.0\n",
        encoding="utf-8",
    )

    assert problems(tree) == []


def test_a_version_inside_a_longer_number_is_not_a_mention(tree: Path) -> None:
    version = package_version(tree)
    major, minor, patch = version.split(".")
    (tree / "docs").mkdir(parents=True, exist_ok=True)
    (tree / "docs" / "numbers.md").write_text(f"1{version} and {major}.{minor}.{patch}9\n", encoding="utf-8")

    assert uncovered_files(tree) == []


@pytest.mark.parametrize("tag", ["v9.9.9", "9.9.9"])
def test_a_tag_that_is_not_this_version_fails(tree: Path, tag: str) -> None:
    found = version_problems(tree, release_tag=tag)

    assert found
    assert "release tag" in found[0]


@pytest.mark.parametrize("tag", ["", None])
def test_no_tag_is_not_a_failure(tree: Path, tag: str | None) -> None:
    """A pull request has no tag. That is the whole point of running there."""
    assert version_problems(tree, release_tag=tag) == []


def test_the_release_tag_is_accepted_with_and_without_its_v(tree: Path) -> None:
    version = package_version(tree)

    assert version_problems(tree, release_tag=f"v{version}") == []
    assert version_problems(tree, release_tag=version) == []


@pytest.mark.parametrize(
    ("relative", "mutate"),
    [
        (
            "server.json",
            lambda document: document.__setitem__("websiteUrl", "https://example.invalid"),
        ),
        (
            "plugins/agentic-hil/.claude-plugin/plugin.json",
            lambda document: document.__setitem__("license", "MIT"),
        ),
        (
            ".claude-plugin/marketplace.json",
            lambda document: document["plugins"][0].__setitem__("source", "./elsewhere"),
        ),
    ],
)
def test_a_published_manifest_that_drifts_from_its_contract_fails(tree: Path, relative: str, mutate) -> None:
    """These reach a registry or a plugin host, and cannot be withdrawn either."""
    path = tree / relative
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    found = contract_problems(tree)

    assert found
    assert "release contract" in found[0]


def test_the_package_must_keep_its_name(tree: Path) -> None:
    pyproject = tree / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace('name = "agentic-hil"', 'name = "agentic_hil"', 1),
        encoding="utf-8",
    )

    assert any("package name" in problem for problem in contract_problems(tree))


def test_a_persisted_plugin_mcp_command_is_refused(tree: Path) -> None:
    (tree / "plugins" / "agentic-hil" / ".mcp.json").write_text("{}", encoding="utf-8")

    assert any("MCP server command" in problem for problem in contract_problems(tree))


def test_the_printed_list_names_every_position_and_its_count() -> None:
    listing = render_list(REPOSITORY_ROOT)
    version = package_version(REPOSITORY_ROOT)
    carried = locations(REPOSITORY_ROOT)

    assert version in listing
    for location in carried:
        assert location.path in listing
        assert location.carries in listing
    for path in UNTRACKED_MENTIONS:
        assert path in listing
    counted = re.search(r"in (\d+) files, (\d+) occurrences", listing)
    assert counted is not None
    # Distinct files, not entries: TROUBLESHOOTING.md holds two claims.
    assert int(counted.group(1)) == len({location.path for location in carried})
    assert int(counted.group(2)) == sum(len(location.versions) for location in carried)


def test_an_unknown_option_is_refused() -> None:
    with pytest.raises(SystemExit):
        main(["--publish"])
