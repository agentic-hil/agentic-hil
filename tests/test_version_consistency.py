from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from check_version_consistency import (  # noqa: E402
    DISTRIBUTION,
    RELEASE,
    UNTRACKED_MENTIONS,
    contract_problems,
    is_development,
    is_newer,
    locations,
    main,
    moved_tree_findings,
    next_development_version,
    package_version,
    problems,
    release_version,
    render_list,
    unclaimed_pins,
    uncovered_files,
    version_problems,
    warnings,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHECK = "tools/check_version_consistency.py"


def test_the_gate_runs_on_every_python_this_project_supports() -> None:
    """tomllib is stdlib only from 3.11 and this project still supports 3.10.

    The version gate now blocks the merge, so a gate that cannot import on a
    matrix leg would block every merge on that leg instead of catching anything.
    `packaging` is on the same list for the same reason: it is not stdlib at all,
    which is why the two version shapes are ordered inside the module.
    """
    source = (REPOSITORY_ROOT / CHECK).read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])

    assert "tomllib" not in modules
    assert "packaging" not in modules
    # subprocess joined the list with the rule that a tree which has moved past
    # its release must say so: the release tag is the only thing that can answer
    # that offline, and git is what holds it.
    assert modules <= {
        "__future__",
        "ast",
        "json",
        "re",
        "subprocess",
        "sys",
        "dataclasses",
        "pathlib",
    }, sorted(modules)


def test_this_tree_agrees_with_itself() -> None:
    assert main([str(REPOSITORY_ROOT)]) == 0


def test_the_release_carries_its_version_where_the_release_that_exposed_this_carried_it() -> None:
    """Every position the version gate counted, including the five it found missing."""
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
    distribution = package_version(REPOSITORY_ROOT)
    release = release_version(REPOSITORY_ROOT)
    for location in locations(REPOSITORY_ROOT):
        assert location.versions, f"{location.path} states no version"
        expected = distribution if location.tracks == DISTRIBUTION else release
        assert set(location.versions) == {expected}, location


def test_only_the_two_positions_that_identify_the_artifact_track_the_tree() -> None:
    """The named exception list, read off the check rather than off a brief.

    Everything else states a floor, an install pin or a published manifest: what
    a reader gets when they install, which is the release and never this tree.
    """
    tracked = {location.path for location in locations(REPOSITORY_ROOT) if location.tracks == DISTRIBUTION}

    assert tracked == {"pyproject.toml", "src/agentic_hil/__init__.py"}


def test_the_troubleshooting_pins_are_all_of_them() -> None:
    """Five `agentic-hil>=` pins and one `@vX.Y.Z` git-tag pin.

    Two entries for one file, because they are two claims: the requirement
    pins the fix instructions hand a reader, and the git-tag pin that drifted
    by hand while `_requirement_pins` could not see it.
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


def _set_distribution_version(root: Path, version: str) -> None:
    """Move the two positions that identify the built artifact, and only those."""
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        re.sub(
            r'^version = "[^"]+"',
            f'version = "{version}"',
            pyproject.read_text(encoding="utf-8"),
            count=1,
            flags=re.MULTILINE,
        ),
        encoding="utf-8",
    )
    init = root / "src" / "agentic_hil" / "__init__.py"
    init.write_text(
        re.sub(
            r'^__version__ = "[^"]+"',
            f'__version__ = "{version}"',
            init.read_text(encoding="utf-8"),
            count=1,
            flags=re.MULTILINE,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def released_tree(tree: Path) -> Path:
    """A release commit: one version, stated everywhere, as it always was.

    Built from the copy rather than assumed of it, so these tests say what they
    mean whichever shape `master` happens to carry when they run.
    """
    _set_distribution_version(tree, release_version(tree))
    return tree


@pytest.fixture
def development_tree(tree: Path) -> Path:
    """The commit after a release: the tree moved on, the release positions did not."""
    _set_distribution_version(tree, next_development_version(release_version(tree)))
    return tree


def test_the_copied_tree_is_a_fair_starting_point(tree: Path) -> None:
    assert version_problems(tree) == []
    assert contract_problems(tree) == []
    assert uncovered_files(tree) == []
    assert unclaimed_pins(tree) == []


@pytest.mark.parametrize(
    ("relative", "tracks", "replace", "names"),
    [
        # pyproject.toml is the reference every other position is compared with,
        # so moving it alone reports the other eleven rather than itself. Either
        # way the tree is refused, which is what a release depends on.
        (
            "pyproject.toml",
            DISTRIBUTION,
            lambda text, version: text.replace(f'version = "{version}"', 'version = "9.9.9"', 1),
            "but this release is 9.9.9",
        ),
        (
            "src/agentic_hil/__init__.py",
            DISTRIBUTION,
            lambda text, version: text.replace(f'__version__ = "{version}"', '__version__ = "9.9.9"'),
            "src/agentic_hil/__init__.py",
        ),
        (
            "server.json",
            RELEASE,
            lambda text, version: text.replace(f'"version": "{version}"', '"version": "9.9.9"', 1),
            "server.json",
        ),
        (
            "plugins/agentic-hil/.claude-plugin/plugin.json",
            RELEASE,
            lambda text, version: text.replace(f'"version": "{version}"', '"version": "9.9.9"'),
            "plugins/agentic-hil/.claude-plugin/plugin.json",
        ),
        (
            ".claude-plugin/marketplace.json",
            RELEASE,
            lambda text, version: text.replace(f'"version": "{version}"', '"version": "9.9.9"'),
            ".claude-plugin/marketplace.json",
        ),
        (
            "CHANGELOG.md",
            RELEASE,
            lambda text, version: text.replace(f"## [{version}]", "## [9.9.9]", 1),
            "CHANGELOG.md",
        ),
        (
            "src/agentic_hil/skills/agentic-hil/SKILL.md",
            RELEASE,
            lambda text, version: text.replace(version, "9.9.9", 1),
            "src/agentic_hil/skills/agentic-hil/SKILL.md",
        ),
        (
            "plugins/agentic-hil/skills/agentic-hil/SKILL.md",
            RELEASE,
            lambda text, version: text.replace(f"agentic-hil=={version}", "agentic-hil==9.9.9"),
            "plugins/agentic-hil/skills/agentic-hil/SKILL.md",
        ),
        (
            "TROUBLESHOOTING.md",
            RELEASE,
            lambda text, version: text.replace(f"agentic-hil>={version}", "agentic-hil>=9.9.9", 1),
            "TROUBLESHOOTING.md",
        ),
        # The pin that was found drifting: a git tag in an install URL, which is not
        # a requirement pin and went unchecked while the file counted as covered.
        (
            "TROUBLESHOOTING.md",
            RELEASE,
            lambda text, version: text.replace(f"agentic-hil@v{version}", "agentic-hil@v0.6.0", 1),
            "TROUBLESHOOTING.md states 0.6.0",
        ),
        (
            "evals/install/matrix.example.json",
            RELEASE,
            lambda text, version: text.replace(f'"expected_version": "{version}"', '"expected_version": "9.9.9"'),
            "evals/install/matrix.example.json",
        ),
        (
            "evals/install/matrix.full.json",
            RELEASE,
            lambda text, version: text.replace(f'"expected_version": "{version}"', '"expected_version": "9.9.9"'),
            "evals/install/matrix.full.json",
        ),
        (
            "evals/install/README.md",
            RELEASE,
            lambda text, version: text.replace(f'"expected_version": "{version}"', '"expected_version": "9.9.9"'),
            "evals/install/README.md",
        ),
    ],
)
@pytest.mark.parametrize("shape", ["released_tree", "development_tree"])
def test_one_position_out_of_step_fails(
    request: pytest.FixtureRequest,
    shape: str,
    relative: str,
    tracks: str,
    replace,
    names: str,
) -> None:
    """The proof. Break the version in exactly one file and the check goes red.

    Every position in turn, because a check that covers eleven of twelve is the
    thing this replaced, and in both tree shapes, because the two-version world
    doubled the number of ways a position can be compared against the wrong one.
    """
    tree = request.getfixturevalue(shape)
    version = package_version(tree) if tracks == DISTRIBUTION else release_version(tree)
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


@pytest.mark.parametrize(
    "name",
    [
        # The name a developer gives an in-clone basetemp, and the automatic
        # root, both recognised by name alone -- pytest leaves nothing else to
        # find beneath either one.
        pytest.param(".pytest-tmp", id="the-conventional-name"),
        pytest.param("pytest-of-someone", id="the-automatic-root"),
    ],
)
def test_a_pytest_temporary_root_in_the_tree_is_scratch_and_not_a_position(tree: Path, name: str) -> None:
    """A basetemp inside the clone is a command-line choice, not a version.

    `--basetemp` gets pointed here on Windows on purpose: the automatic root
    nests a per-user, a per-run and a per-test directory before a fixture's own
    paths begin, which is how a suite meets MAX_PATH. Every configuration and
    report a test then writes carries this tree's version, and the gate used to
    refuse the tree for them, which is a gate reporting on how the suite was
    invoked rather than on the tree.
    """
    root = tree / name
    (root / "test_something0").mkdir(parents=True)
    (root / "test_something0" / "config.yaml").write_text(
        f"# written by a fixture at {package_version(tree)}\n", encoding="utf-8"
    )

    assert uncovered_files(tree) == []


def test_a_numbered_root_with_pytests_own_current_symlink_is_scratch_with_no_lock_at_all(tree: Path) -> None:
    """`--basetemp`'s own numbered roots carry no `.lock` -- only the symlink `make_numbered_dir` leaves.

    `create_cleanup_lock` runs only for a numbered root under pytest's default,
    unmarked temp directory (already caught by the `pytest-of-` name), never for
    one `tmp_path_factory.mktemp` creates directly under a given `--basetemp`.
    The `<prefix>current` symlink beside it is what actually marks that shape.
    """
    root = tree / "scratch7"
    root.mkdir()
    (root / "config.yaml").write_text(f"# written by a fixture at {package_version(tree)}\n", encoding="utf-8")
    try:
        (tree / "scratchcurrent").symlink_to(root, target_is_directory=True)
    except OSError as error:
        # Creating one needs SeCreateSymbolicLinkPrivilege, which a Windows
        # account without developer mode does not have, and the shape this is
        # about cannot be built without it. CI has the privilege; a developer
        # bench frequently does not, and an unconditional failure there is a
        # standing red mark that teaches everyone to ignore this file.
        pytest.skip(f"directory symlinks unavailable: {error}")

    assert uncovered_files(tree) == []


def test_a_scratch_directory_pytest_never_marked_is_still_swept(tree: Path) -> None:
    """The skip is narrow: only a root pytest named or marked is left out.

    A directory the gate cannot tell from content is content, or the exemption
    would be a hole anyone could put a version in by choosing a directory name.
    """
    (tree / "scratch").mkdir()
    (tree / "scratch" / "installation.md").write_text(
        f'pip install "agentic-hil=={package_version(tree)}"\n', encoding="utf-8"
    )

    found = uncovered_files(tree)

    assert found
    assert "scratch/installation.md" in found[0]


def test_a_lock_file_alone_does_not_hide_an_ordinary_tracked_directory(tree: Path) -> None:
    """`.lock` is not pytest-specific, so it cannot stand in for pytest's own shape.

    A directory some unrelated tool marked with a `.lock` of its own -- and
    whose name does not even end in the digits `make_numbered_dir` always
    appends -- is not the numbered-root shape pytest actually leaves. Accepting
    a bare `.lock` used to exclude the whole directory from this scan, silently,
    version strings under it included.
    """
    directory = tree / "vendor" / "some-tool"
    directory.mkdir(parents=True)
    (directory / ".lock").write_text("", encoding="utf-8")
    (directory / "installation.md").write_text(
        f'pip install "agentic-hil=={package_version(tree)}"\n', encoding="utf-8"
    )

    found = uncovered_files(tree)

    assert found
    assert "vendor/some-tool/installation.md" in found[0]


def test_a_numbered_directory_marked_with_a_lock_file_is_still_swept(tree: Path) -> None:
    """`.lock` beside a numbered directory is not pytest's own shape either.

    `create_cleanup_lock` only ever marks a numbered root under pytest's own
    default `pytest-of-*` temp directory, which is already caught by name. A
    directory made directly under an explicit `--basetemp` carries the
    `<prefix>current` symlink instead and never a `.lock`, so a `.lock` next to
    an otherwise ordinary numbered directory is not pytest-specific evidence
    and must not exempt it -- unlike the now-removed standalone `.lock` check,
    which treated exactly this shape as scratch.
    """
    directory = tree / "release2"
    directory.mkdir()
    (directory / ".lock").write_text("", encoding="utf-8")
    (directory / "installation.md").write_text(
        f'pip install "agentic-hil=={package_version(tree)}"\n', encoding="utf-8"
    )

    found = uncovered_files(tree)

    assert found
    assert "release2/installation.md" in found[0]


def test_a_numbered_looking_directory_with_no_lock_and_no_current_symlink_is_still_swept(tree: Path) -> None:
    """A name ending in digits is necessary but not sufficient on its own.

    Nothing here proves pytest made this directory without the cleanup lock or
    the matching `current` symlink beside it, so a release directory that simply
    happens to be named like a version number must still be covered.
    """
    directory = tree / "release2"
    directory.mkdir()
    (directory / "installation.md").write_text(
        f'pip install "agentic-hil=={package_version(tree)}"\n', encoding="utf-8"
    )

    found = uncovered_files(tree)

    assert found
    assert "release2/installation.md" in found[0]


def test_a_new_home_for_the_development_version_cannot_appear_either(development_tree: Path) -> None:
    """The sweep learned the second version, or half of it would be unwatched.

    A document that pins the tree's own development version is a document
    telling a reader to install something that was never released, and it is
    exactly the mistake a version bump invites.
    """
    (development_tree / "docs").mkdir(parents=True, exist_ok=True)
    (development_tree / "docs" / "installation.md").write_text(
        f'pip install "agentic-hil=={package_version(development_tree)}"\n', encoding="utf-8"
    )

    found = uncovered_files(development_tree)

    assert found
    assert "docs/installation.md" in found[0]
    assert package_version(development_tree) in found[0]


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
    """The other half of the tag-pin test: a correct pin is not noise."""
    version = package_version(tree)
    path = tree / "TROUBLESHOOTING.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        + f'\nuv tool install "git+https://github.com/agentic-hil/agentic-hil@v{version}"\n',
        encoding="utf-8",
    )

    assert problems(tree) == []


def test_a_tag_pin_in_a_covered_file_whose_entry_does_not_claim_it_fails(tree: Path) -> None:
    """The class, not the instance, of that defect.

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
    """Both versions the sweep looks for, and a digit on either side of each."""
    swept = {release_version(tree), package_version(tree)}
    (tree / "docs").mkdir(parents=True, exist_ok=True)
    (tree / "docs" / "numbers.md").write_text(
        "\n".join(f"1{version} and {version}9" for version in sorted(swept)) + "\n", encoding="utf-8"
    )

    assert uncovered_files(tree) == []


def test_a_release_shaped_tree_is_the_check_it_always_was(released_tree: Path) -> None:
    """One version, in every position, and no development suffix anywhere."""
    assert problems(released_tree) == []
    assert not is_development(package_version(released_tree))
    assert release_version(released_tree) == package_version(released_tree)


def test_the_tree_after_a_release_states_two_versions_and_passes(development_tree: Path) -> None:
    """The shape every commit between two releases carries.

    The distribution positions name the tree, the release positions keep naming
    the release a reader can install, and the gate is silent: that is the whole
    of the decision, checked rather than described.
    """
    distribution = package_version(development_tree)
    release = release_version(development_tree)

    assert is_development(distribution)
    assert distribution != release
    assert problems(development_tree) == []
    for location in locations(development_tree):
        expected = distribution if location.tracks == DISTRIBUTION else release
        assert set(location.versions) == {expected}, location


def test_a_development_version_that_does_not_follow_the_release_is_refused(development_tree: Path) -> None:
    """The suffix extends the newest release; it cannot precede or repeat it.

    `1.2.3.dev1` is not "after 1.2.3", it is a pre-release *of* it, which is how
    a tree could carry a suffix and still shadow the release it claims to have
    moved past.
    """
    release = release_version(development_tree)
    _set_distribution_version(development_tree, f"{release}.dev1")

    found = version_problems(development_tree)

    assert found
    assert "does not follow it" in found[0]
    assert release in found[0]


@pytest.mark.parametrize(
    ("version", "other"),
    [
        # Synthetic numbers throughout: a test that spelled out the current
        # release would itself become a file carrying the version, which the
        # sweep refuses and rightly so.
        ("1.2.4.dev0", "1.2.3"),
        ("1.2.4.dev1", "1.2.4.dev0"),
        ("1.3.0.dev0", "1.2.9"),
        ("2.0.0.dev0", "1.99.99"),
        ("1.2.4", "1.2.4.dev0"),
    ],
)
def test_a_development_version_orders_the_way_pep_440_says(version: str, other: str) -> None:
    """The ordering the whole decision rests on, and it is not string order.

    A development release is newer than every earlier release and older than the
    one it anticipates. Every installer and every `>=` floor reads it this way,
    which is why a suffixed tree can be published, resolved and upgraded from
    without anything downstream learning a new rule.
    """
    assert is_newer(version, other)
    assert not is_newer(other, version)


def test_the_suffix_the_gate_suggests_is_the_one_that_follows_the_release() -> None:
    assert next_development_version("1.2.3") == "1.2.4.dev0"
    assert is_newer(next_development_version("1.2.3"), "1.2.3")


@pytest.mark.parametrize("tag", ["v9.9.9", "9.9.9"])
def test_a_tag_that_is_not_this_version_fails(tree: Path, tag: str) -> None:
    found = version_problems(tree, release_tag=tag)

    assert found
    assert "release tag" in found[0]


def test_a_release_tag_is_refused_outright_on_a_development_tree(development_tree: Path) -> None:
    """A tag never carries a development suffix, so this path never sees one.

    The release job runs the same module with `--release-tag` after the release
    exists. Reaching it from a tree that never stopped being between releases
    means the release was cut from the wrong commit, and PyPI cannot take a
    version back.
    """
    version = package_version(development_tree)

    for tag in (f"v{version}", version, f"v{release_version(development_tree)}"):
        found = version_problems(development_tree, release_tag=tag)
        assert any("cannot be cut from a development tree" in problem for problem in found), tag


@pytest.mark.parametrize("tag", ["", None])
def test_no_tag_is_not_a_failure(tree: Path, tag: str | None) -> None:
    """A pull request has no tag. That is the whole point of running there."""
    assert version_problems(tree, release_tag=tag) == []


def test_the_release_tag_is_accepted_with_and_without_its_v(released_tree: Path) -> None:
    """A release commit, because that is the only tree a tag is ever cut from."""
    version = package_version(released_tree)

    assert version_problems(released_tree, release_tag=f"v{version}") == []
    assert version_problems(released_tree, release_tag=version) == []


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


def _checkout(root: Path, tag: str | None) -> None:
    """Commit this tree, and tag it as the release it states.

    The line-ending pin this repository carries, because the comparison the
    check makes is a `git diff` against a tag and a machine with
    core.autocrlf=true would otherwise read every file as changed.
    """
    (root / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
    commands = [
        ["init", "--initial-branch=main"],
        ["add", "-A"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "release"],
    ]
    if tag is not None:
        commands.append(["tag", tag])
    for command in commands:
        result = subprocess.run(["git", "-C", str(root), *command], capture_output=True)
        assert result.returncode == 0, result.stderr


def test_a_package_that_moved_past_its_release_must_carry_a_development_version(released_tree: Path) -> None:
    """The rule that makes the suffix a rule rather than a habit.

    A tree whose package has moved while its version stands still is
    indistinguishable from the release it shadows, which is what an install, an
    upgrade and the install eval all get wrong at once.
    """
    release = release_version(released_tree)
    _checkout(released_tree, f"v{release}")
    assert problems(released_tree) == []

    package = released_tree / "src" / "agentic_hil" / "__init__.py"
    package.write_text(package.read_text(encoding="utf-8") + "\n# a change nobody released\n", encoding="utf-8")

    found, notes = moved_tree_findings(released_tree)

    assert notes == []
    assert found
    assert f"has moved since v{release}" in found[0]
    assert next_development_version(release) in found[0]
    assert found[0] in problems(released_tree)


def test_a_file_nothing_has_added_yet_is_a_moved_package_too(released_tree: Path) -> None:
    """`git diff` cannot see an untracked file, and a new module is one."""
    _checkout(released_tree, f"v{release_version(released_tree)}")

    (released_tree / "src" / "agentic_hil" / "newcomer.py").write_text("value = 1\n", encoding="utf-8")

    found, notes = moved_tree_findings(released_tree)

    assert notes == []
    assert found


def test_the_suffix_is_what_lets_the_package_move(development_tree: Path) -> None:
    """The other side of the rule: with the suffix, moving on is the point."""
    _checkout(development_tree, f"v{release_version(development_tree)}")

    package = development_tree / "src" / "agentic_hil" / "__init__.py"
    package.write_text(package.read_text(encoding="utf-8") + "\n# the work this cycle\n", encoding="utf-8")

    assert moved_tree_findings(development_tree) == ([], [])
    assert problems(development_tree) == []


def test_a_clone_without_the_release_tag_warns_rather_than_fails(released_tree: Path) -> None:
    """A fork and a shallow clone did nothing wrong and must not go red.

    The tag is the only thing that can say offline what the release shipped, so
    where it is absent the answer is that nobody asked, not that somebody erred.
    """
    _checkout(released_tree, tag=None)
    package = released_tree / "src" / "agentic_hil" / "__init__.py"
    package.write_text(package.read_text(encoding="utf-8") + "\n# a change nobody released\n", encoding="utf-8")

    found, notes = moved_tree_findings(released_tree)

    assert found == []
    assert problems(released_tree) == []
    assert notes
    assert f"v{release_version(released_tree)} is not reachable here" in notes[0]
    assert notes == warnings(released_tree)


def test_a_tree_that_is_no_checkout_at_all_is_a_warning(released_tree: Path) -> None:
    """An unpacked sdist has the files and no history to compare them against."""
    found, notes = moved_tree_findings(released_tree)

    assert found == []
    assert notes


def test_the_release_metadata_job_fetches_the_tags_the_check_needs() -> None:
    """Without the tags the new rule reports that it could not check, and passes.

    `actions/checkout` fetches one commit and no tags by default, which is
    exactly the condition the check treats as unanswerable.
    """
    ci = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = ci.split("  release_metadata:", 1)[1].split("\n  audit:", 1)[0]

    assert "fetch-depth: 0" in job
    assert f"python {CHECK}" in job
