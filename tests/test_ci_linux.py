"""What `tools/ci_linux.py` actually hands the container.

The tool's one documented promise about its arguments is that everything after
its own options reaches pytest unchanged. That promise is only worth as much as
the boundary between arguments survives the trip: the arguments go into a
`docker run ... bash -c` invocation, and a script that interpolates them into its
text hands every quote, `$` and `;` in a path or a `-k` expression to Bash, and
splits `-k "foo and bar"` into three arguments on the way.

No Docker is needed to check that: the command list is built before anything
runs, so these tests assemble it and read it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import ci_linux  # noqa: E402


def built_command(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> list[str]:
    """Run `main` with docker stubbed out and return the command it assembled."""
    captured: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = str(Path(__file__).resolve().parents[1])

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        # `repository_root` shells out to git first; only the docker invocation
        # is the one under test.
        if command[:1] != ["git"]:
            captured.append(command)
        return Completed()

    # The modules the tool reaches through, replaced on the tool rather than on
    # the real `subprocess` and `shutil`, so nothing else in this process runs
    # against a patched standard library.
    monkeypatch.setattr(ci_linux, "shutil", SimpleNamespace(which=lambda name: f"/usr/bin/{name}"))
    monkeypatch.setattr(ci_linux, "subprocess", SimpleNamespace(run=fake_run))
    assert ci_linux.main(argv) == 0
    return captured[-1]


def pytest_arguments(command: list[str]) -> list[str]:
    """Everything after the script and its `$0`, which is what `"$@"` expands to."""
    index = command.index(ci_linux.SCRIPT)
    assert command[index + 1] == ci_linux.SCRIPT_ARGV0
    return command[index + 2 :]


def test_an_expression_with_spaces_stays_one_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """`-k "foo and bar"` is one argument to pytest and three to a shell."""
    command = built_command(monkeypatch, ["tests/test_hardening.py", "-k", "foo and bar", "-x"])

    assert pytest_arguments(command) == ["tests/test_hardening.py", "-k", "foo and bar", "-x"]


def test_shell_metacharacters_are_not_interpreted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path or expression carrying `$`, a quote or a `;` reaches pytest as
    written. Interpolated into the script text, Bash would have expanded the
    first and run the last."""
    hostile = "tests/test_it.py::test_x[$HOME; echo 'hi']"
    command = built_command(monkeypatch, [hostile])

    assert pytest_arguments(command) == [hostile]
    assert hostile not in ci_linux.SCRIPT


def test_the_script_forwards_the_positional_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the arrangement: the script has to consume them."""
    assert 'exec python -m pytest "$@"' in ci_linux.SCRIPT
    assert "{args}" not in ci_linux.SCRIPT


def test_no_arguments_still_runs_the_whole_suite_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pytest_arguments(built_command(monkeypatch, [])) == ["-q"]


def test_the_separator_argparse_keeps_is_not_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pytest_arguments(built_command(monkeypatch, ["--", "-x"])) == ["-x"]
