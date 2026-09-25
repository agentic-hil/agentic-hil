"""The tool that records the command surface of the release the CI examples pin.

`tests/test_ci_examples.py` holds every subcommand the shipped examples invoke
to `tests/fixtures/published_cli_surface.json`, a recording of the pinned
release's argparse tree (#525). This module holds `tools/record_cli_surface.py`
to what that recording has to be: names only, taken from exactly the pinned
release in a throwaway environment or, on a release commit, from the stamped
tree, and refused when the parser walked is another release's or was imported
from anywhere else. The index and the package installer are stood in for by a
fake command runner, so all of that is covered on a pull request without a
network. One walk is real: this checkout's parser, in a separate interpreter,
which is what proves the program the tool hands that interpreter works.

`tools/` is repository tooling that never ships, so MANIFEST.in excludes this
file from the source distribution for the reason it excludes the other tool
tests.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from check_version_consistency import locations, package_version  # noqa: E402
from record_cli_surface import (  # noqa: E402
    INDEX_URL,
    RECORDING,
    WHAT,
    command_surface,
    main,
    published_surface,
    tree_surface,
    walk,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL = "tools/record_cli_surface.py"

# A synthetic tree's versions, chosen so nothing here matches the version this
# repository carries, which the version sweep would read as an uncovered pin.
# PUBLISHED is the release the synthetic examples pin, DEVELOPMENT the tree
# after it, and OTHER a release that is not the pin.
PUBLISHED = "1.2.3"
DEVELOPMENT = "1.2.4.dev0"
OTHER = "1.2.2"

# What a fake walk answers with: the committed recording's shape, made-up content.
SURFACE = {
    "options": ["--help", "--version", "-h"],
    "positionals": [],
    "subcommands": {"doctor": {"options": ["--help", "-h"], "positionals": [], "subcommands": {}}},
}


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Stands in for venv, the package installer and the walking interpreter.

    It keeps every argv it is handed, so a test can say exactly what would have
    run. The walk answers with `version` and a module path: inside the
    throwaway environment by default, or `module` where a test plants one
    somewhere else. An argv nobody planted is a loud failure, not a silent pass.
    """

    def __init__(
        self,
        version: str = PUBLISHED,
        module: Path | None = None,
        installed: subprocess.CompletedProcess[str] | None = None,
    ) -> None:
        self.version = version
        self.module = module
        self.installed = installed if installed is not None else _completed(0)
        self.environment: Path | None = None
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.calls.append(command)
        if command[1:4] == ["-I", "-m", "venv"]:
            self.environment = Path(command[4])
            return _completed(0)
        if command[1:5] == ["-I", "-m", "pip", "install"]:
            return self.installed
        if command[1:3] == ["-I", "-c"]:
            module = self.module
            if module is None:
                assert self.environment is not None, "a walk with no planted module and no environment"
                module = self.environment / "site-packages" / "agentic_hil" / "__init__.py"
            walked = {"version": self.version, "module": str(module), "surface": SURFACE}
            return _completed(0, stdout=json.dumps(walked))
        raise AssertionError(f"unexpected command {command}")


def _never(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"nothing should have run, but {list(argv)} did")


def _write_tree(root: Path, package: str) -> None:
    """The files the tool reads: the pyproject, the CHANGELOG that dates the pinned release, the two pins."""
    (root / "pyproject.toml").write_text(f'[project]\nname = "agentic-hil"\nversion = "{package}"\n', encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n\n## [{PUBLISHED}] - 2020-01-02\n\n## [{OTHER}] - 2020-01-01\n",
        encoding="utf-8",
    )
    examples = root / "examples" / "ci"
    examples.mkdir(parents=True)
    (examples / "github-actions.yml").write_text(f'env:\n  AGENTIC_HIL_VERSION: "{PUBLISHED}"\n', encoding="utf-8")
    (examples / "gitlab-ci.yml").write_text(f'variables:\n  AGENTIC_HIL_VERSION: "{PUBLISHED}"\n', encoding="utf-8")
    (root / "src").mkdir()
    (root / "tests" / "fixtures").mkdir(parents=True)


def test_the_tool_runs_on_every_python_this_project_supports() -> None:
    """It imports the standard library and the two tools beside it, and nothing else.

    The release stamp runs it on whatever Python the release is cut with, so a
    `tomllib` or `packaging` import would make the tool the thing that fails on
    the project's oldest Python rather than the step that records the surface.
    """
    source = (REPOSITORY_ROOT / TOOL).read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])

    assert "tomllib" not in modules
    assert modules - set(sys.stdlib_module_names) == {"check_version_consistency", "verify_published_examples"}


def test_the_surface_is_names_only_recursive_and_in_a_stable_order() -> None:
    """Option strings sorted, positionals in the order they are consumed, subcommands by every name.

    An option hidden from `--help` still parses, so it is recorded; help text
    changes with every wording fix and says nothing about what parses, so it is
    not.
    """
    parser = argparse.ArgumentParser(prog="synthetic")
    parser.add_argument("--zeta")
    parser.add_argument("-a", "--alpha", help="help text that is not recorded")
    parser.add_argument("--hidden", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")
    second = commands.add_parser("second")
    second.add_argument("target")
    second.add_argument("extra", nargs="?")
    first = commands.add_parser("first", aliases=["one"])
    first.add_subparsers(dest="action").add_parser("inner").add_argument("--flag", action="store_true")

    surface = command_surface(parser)

    first_surface = {
        "options": ["--help", "-h"],
        "positionals": [],
        "subcommands": {"inner": {"options": ["--flag", "--help", "-h"], "positionals": [], "subcommands": {}}},
    }
    assert surface == {
        "options": ["--alpha", "--help", "--hidden", "--zeta", "-a", "-h"],
        "positionals": [],
        "subcommands": {
            "first": first_surface,
            "one": first_surface,
            "second": {"options": ["--help", "-h"], "positionals": ["target", "extra"], "subcommands": {}},
        },
    }
    assert list(surface["subcommands"]) == ["first", "one", "second"]
    assert "help text" not in json.dumps(surface)


def test_the_walk_takes_this_checkout_s_parser_apart_in_a_separate_interpreter() -> None:
    """The one real walk: the program handed to `python -I -c` imports this tree's `src` and answers.

    Everything else here fakes the interpreter, so this is what proves the
    program the tool assembles runs at all, against the parser the examples'
    commands come from.
    """
    source = (REPOSITORY_ROOT / "src").resolve()

    walked = walk(sys.executable, source)

    assert set(walked) == {"version", "module", "surface"}
    assert walked["version"] == package_version(REPOSITORY_ROOT)
    assert Path(walked["module"]).resolve().is_relative_to(source)
    assert {"doctor", "test-reactor", "check-plan", "run-evidence"} <= set(walked["surface"]["subcommands"])
    assert "--version" in walked["surface"]["options"]


def test_a_development_tree_is_not_recorded_from_the_tree(tmp_path: Path) -> None:
    """Between releases this tree's parser is no release's, so `--from-tree` refuses before walking."""
    _write_tree(tmp_path, DEVELOPMENT)

    with pytest.raises(SystemExit) as refused:
        tree_surface(tmp_path, run=_never)

    assert DEVELOPMENT in str(refused.value)
    assert "without --from-tree" in str(refused.value)


def test_a_release_tree_is_walked_from_its_own_src(tmp_path: Path) -> None:
    """On a release commit the stamped tree is the release, walked from its `src` with the running interpreter."""
    _write_tree(tmp_path, PUBLISHED)
    source = (tmp_path / "src").resolve()
    run = FakeRunner(module=source / "agentic_hil" / "__init__.py")

    walked = tree_surface(tmp_path, run=run)

    assert walked["version"] == PUBLISHED
    assert walked["surface"] == SURFACE
    assert len(run.calls) == 1, run.calls
    assert run.calls[0][:3] == [sys.executable, "-I", "-c"]
    assert run.calls[0][-1] == str(source)


def test_a_tree_walk_that_imported_another_installation_is_refused(tmp_path: Path) -> None:
    """An `agentic_hil` from site-packages would be recorded under this tree's name."""
    _write_tree(tmp_path, PUBLISHED)
    elsewhere = tmp_path / "site-packages" / "agentic_hil" / "__init__.py"

    with pytest.raises(SystemExit, match="was imported from") as refused:
        tree_surface(tmp_path, run=FakeRunner(module=elsewhere))

    assert "site-packages" in str(refused.value)


def test_a_recording_from_the_index_installs_exactly_the_pin_in_a_throwaway_environment() -> None:
    """A fresh environment, the pin and nothing looser, the named index, and the walk inside it.

    `-I` and `--isolated` keep the recording machine's environment variables,
    user site and pip configuration out of what gets installed and imported,
    and the environment is gone once the surface is read.
    """
    run = FakeRunner()

    walked = published_surface(PUBLISHED, run=run)

    assert walked["version"] == PUBLISHED
    assert walked["surface"] == SURFACE
    created, installed, walking = run.calls
    assert created[:4] == [sys.executable, "-I", "-m", "venv"]
    environment = run.environment
    assert environment is not None
    assert Path(installed[0]).is_relative_to(environment)
    assert installed[1:5] == ["-I", "-m", "pip", "install"]
    for flag in ("--isolated", "--no-cache-dir", "--disable-pip-version-check"):
        assert flag in installed, installed
    assert installed[installed.index("--index-url") + 1] == INDEX_URL
    assert installed[-1] == f"agentic-hil=={PUBLISHED}"
    # The environment's own interpreter, and no source tree put in front of it.
    assert walking[:3] == [installed[0], "-I", "-c"]
    assert len(walking) == 4, walking[3:]
    assert not environment.parent.exists()


def test_a_walk_of_another_release_than_the_pin_is_refused() -> None:
    """A resolver that installed anything but the pin would record the wrong release's commands."""
    with pytest.raises(SystemExit) as refused:
        published_surface(PUBLISHED, run=FakeRunner(version=OTHER))

    assert OTHER in str(refused.value)
    assert PUBLISHED in str(refused.value)


def test_a_walk_that_imported_agentic_hil_from_outside_the_environment_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "somewhere-else" / "agentic_hil" / "__init__.py"

    with pytest.raises(SystemExit, match="was imported from") as refused:
        published_surface(PUBLISHED, run=FakeRunner(module=elsewhere))

    assert "somewhere-else" in str(refused.value)


def test_a_failed_install_keeps_its_decisive_line(capsys: pytest.CaptureFixture[str]) -> None:
    """The refusal ends with the installer's last line, and everything it printed stays on stderr."""
    failed = _completed(
        1,
        stdout="a line the installer printed first, made up for this test\n",
        stderr="a line of context\nthe decisive line, made up for this test\n",
    )
    run = FakeRunner(installed=failed)

    with pytest.raises(SystemExit) as refused:
        published_surface(PUBLISHED, run=run)

    message = str(refused.value)
    assert f"agentic-hil=={PUBLISHED}" in message
    assert "exited 1" in message
    assert message.endswith(": the decisive line, made up for this test")
    printed = capsys.readouterr().err
    assert "a line the installer printed first" in printed
    assert "a line of context" in printed
    assert len(run.calls) == 2, "nothing is walked after a failed install"


@pytest.mark.parametrize(
    ("completed", "ending"),
    [
        (_completed(3, stdout="only stdout said this\n"), "exited 3: only stdout said this"),
        (_completed(3), "exited 3: nothing was printed"),
        (_completed(0, stdout="not a surface\n"), "printed no surface: not a surface"),
    ],
)
def test_a_walk_that_fails_says_how_in_its_last_line(completed: subprocess.CompletedProcess[str], ending: str) -> None:
    with pytest.raises(SystemExit) as refused:
        walk(sys.executable, run=lambda argv: completed)

    assert str(refused.value).endswith(ending)


def test_the_recording_names_what_it_is_which_release_and_how_it_was_taken(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default mode, end to end on a synthetic tree: the pin is read, installed, walked and written.

    The document says what it is, which release, how it was taken and when, in
    that order, with no path of the machine that took it, and it is in the
    shape `tests/test_ci_examples.py` reads.
    """
    from test_ci_examples import surface_problems  # noqa: PLC0415

    _write_tree(tmp_path, DEVELOPMENT)
    run = FakeRunner()

    assert main([f"--root={tmp_path}"], run=run) == 0

    written = tmp_path / RECORDING
    assert b"\r\n" not in written.read_bytes()
    document = json.loads(written.read_text(encoding="utf-8"))
    assert list(document) == ["what", "version", "source", "recorded_on", "surface"]
    assert document["what"] == WHAT
    assert document["version"] == PUBLISHED
    assert f"agentic-hil=={PUBLISHED}" in document["source"]
    assert INDEX_URL in document["source"]
    assert run.environment is not None
    for place in (str(tmp_path), str(run.environment.parent)):
        assert place not in document["source"]
    date.fromisoformat(document["recorded_on"])
    assert document["surface"] == SURFACE
    assert surface_problems(document, PUBLISHED, {"doctor"}) == []
    assert f"recorded the agentic-hil {PUBLISHED} command surface" in capsys.readouterr().out


def test_a_release_stamp_records_the_stamped_tree(tmp_path: Path) -> None:
    """`--from-tree` on a release commit: the tree's own version, and a source sentence that says so."""
    _write_tree(tmp_path, PUBLISHED)
    run = FakeRunner(module=(tmp_path / "src").resolve() / "agentic_hil" / "__init__.py")

    assert main(["--from-tree", f"--root={tmp_path}"], run=run) == 0

    document = json.loads((tmp_path / RECORDING).read_text(encoding="utf-8"))
    assert document["version"] == PUBLISHED
    assert "--from-tree" in document["source"]
    assert PUBLISHED in document["source"]
    assert str(tmp_path) not in document["source"]


def test_the_recording_is_where_the_examples_test_reads_it_and_the_gate_holds_it() -> None:
    """One path for the tool, the examples test and the version gate, so a release stamp cannot miss it."""
    from test_ci_examples import RECORDING as READ  # noqa: PLC0415

    assert REPOSITORY_ROOT / RECORDING == READ
    assert RECORDING in {location.path for location in locations(REPOSITORY_ROOT)}


def test_the_committed_recording_is_in_the_form_this_tool_writes() -> None:
    """The keys in the tool's order, its description, one of its two source sentences, its serialisation.

    Hand edits to the committed file show up here as a difference in form; a
    difference in content is what the release-commit test below compares.
    """
    text = (REPOSITORY_ROOT / RECORDING).read_text(encoding="utf-8")
    recording = json.loads(text)

    assert list(recording) == ["what", "version", "source", "recorded_on", "surface"]
    assert recording["what"] == WHAT
    assert recording["source"].startswith(("Installed agentic-hil==", "Walked from the agentic-hil "))
    date.fromisoformat(recording["recorded_on"])
    assert text == json.dumps(recording, indent=2) + "\n"


def test_on_a_release_commit_the_recording_is_the_surface_of_this_tree() -> None:
    """The moment the recording can be compared with code instead of trusted.

    On a release commit the pin, the recording and this tree are one release,
    and the release stamp took the recording from this tree with `--from-tree`,
    so it has to be exactly the surface this checkout's parser has. A parser
    changed after the recording was taken, or a recording edited by hand, fails
    here before the release ships. Between releases the recording is of the
    published release and this tree has moved past it, so there is nothing to
    compare it with.
    """
    recording = json.loads((REPOSITORY_ROOT / RECORDING).read_text(encoding="utf-8"))
    package = package_version(REPOSITORY_ROOT)
    if package != recording["version"]:
        pytest.skip(
            f"this tree is agentic-hil {package} and the recording is of {recording['version']}: "
            "they are one release only on a release commit"
        )

    assert tree_surface(REPOSITORY_ROOT)["surface"] == recording["surface"]


def test_main_rejects_an_unknown_option() -> None:
    with pytest.raises(SystemExit, match="unknown option"):
        main(["--nope"])
