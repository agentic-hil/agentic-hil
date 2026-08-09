"""What `tools/ci_linux.py` hands the container, and what it makes of the answer.

The tool's one documented promise about its arguments is that everything after
its own options reaches pytest unchanged. That promise is only worth as much as
the boundary between arguments survives the trip: the arguments go into a
`docker run ... bash -c` invocation, and a script that interpolates them into its
text hands every quote, `$` and `;` in a path or a `-k` expression to Bash, and
splits `-k "foo and bar"` into three arguments on the way.

The second half is what the tool does with the run. It used to forward the
container's exit status and assert nothing about what ran, which was observed
returning 0 after zero tests were collected: the clone inside the container
could not build a usable tree, and a green result came back for a suite that
never executed.

No Docker is needed for either: the command list is built before anything runs,
and the run is judged from its output.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import ci_linux  # noqa: E402

# What a container that did its job prints last.
PLAUSIBLE_RESULT = "1476 passed, 35 skipped in 41.23s\n"


class FakeContainer:
    """The `docker run` process: some output, then an exit status."""

    def __init__(self, output: str, returncode: int) -> None:
        self.stdout = iter(output.splitlines(keepends=True))
        self._returncode = returncode

    def wait(self) -> int:
        return self._returncode


def stub_docker(
    monkeypatch: pytest.MonkeyPatch,
    output: str = PLAUSIBLE_RESULT,
    returncode: int = 0,
) -> list[list[str]]:
    """Replace docker with a container that prints `output` and exits `returncode`.

    Returns the list the commands are captured into.
    """
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

    def fake_popen(command: list[str], **kwargs: object) -> FakeContainer:
        captured.append(command)
        return FakeContainer(output, returncode)

    # The modules the tool reaches through, replaced on the tool rather than on
    # the real `subprocess` and `shutil`, so nothing else in this process runs
    # against a patched standard library.
    monkeypatch.setattr(ci_linux, "shutil", SimpleNamespace(which=lambda name: f"/usr/bin/{name}"))
    monkeypatch.setattr(
        ci_linux,
        "subprocess",
        SimpleNamespace(run=fake_run, Popen=fake_popen, PIPE=subprocess.PIPE, STDOUT=subprocess.STDOUT),
    )
    return captured


def built_command(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> list[str]:
    """Run `main` with docker stubbed out and return the command it assembled."""
    captured = stub_docker(monkeypatch)
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


def test_only_the_wrappers_own_separator_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytest ends its own option parsing with a `--` of its own, so "everything
    after the wrapper's options, unchanged" has to mean exactly that. Dropping
    every `--` turned `-- tests -- -x` into `tests -x` and changed what ran."""
    command = built_command(monkeypatch, ["--", "tests", "--", "-x"])

    assert pytest_arguments(command) == ["tests", "--", "-x"]


def test_a_separator_pytest_owns_survives_without_one_of_ours(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pytest_arguments(built_command(monkeypatch, ["tests", "--", "-x"])) == ["tests", "--", "-x"]


def test_a_run_that_reported_no_result_at_all_is_not_a_pass(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """The observed failure: exit 0, and not one test executed. Nothing in the
    output claims otherwise, and that absence is the whole signal."""
    stub_docker(monkeypatch, output="Cloning into '/work'...\nSuccessfully installed agentic-hil\n", returncode=0)

    assert ci_linux.main([]) == ci_linux.EXIT_NO_RESULT
    assert "no pytest summary line" in capsys.readouterr().err


def test_zero_collected_is_never_a_success_of_this_tool(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    stub_docker(monkeypatch, output="no tests ran in 0.01s\n", returncode=0)

    assert ci_linux.main([]) == ci_linux.EXIT_NO_RESULT
    assert "collected no tests" in capsys.readouterr().err


def test_a_clone_that_failed_goes_red_naming_the_clone(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A run that dies in the clone must not surface as a pytest step that never
    happened, and must not be softened by whatever the container exited with."""
    stub_docker(
        monkeypatch,
        output=f"error: unable to normalize alternate object path\n{ci_linux.CLONE_FAILURE_MARKER}: git clone of /src into /work failed\n",
        returncode=0,
    )

    assert ci_linux.main([]) == ci_linux.EXIT_CLONE_FAILED
    assert "clone the repository into a usable tree" in capsys.readouterr().err


def test_a_failing_suite_still_leaves_this_process_with_pytests_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plausibility check adds a floor; it does not take over the result."""
    stub_docker(monkeypatch, output="==== 1 failed, 12 passed in 3.40s ====\n", returncode=1)

    assert ci_linux.main([]) == 1


def test_a_selection_that_only_skips_is_a_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skipped tests were collected. The floor is on what pytest found, not on
    what it chose to execute, or `ci_linux.py tests/test_windows_only.py` would
    fail on Linux for doing exactly what it is supposed to do."""
    stub_docker(monkeypatch, output="35 skipped in 1.20s\n", returncode=0)

    assert ci_linux.main([]) == 0


@pytest.mark.parametrize(
    ("line", "accounted"),
    [
        ("1476 passed, 35 skipped in 41.23s", 1511),
        ("======= 1 failed, 12 passed, 3 warnings in 3.40s =======", 13),
        ("37 tests collected in 0.42s", 37),
        ("1 error in 0.50s", 1),
        ("no tests ran in 0.01s", 0),
    ],
)
def test_the_summary_line_is_read_for_what_pytest_found(line: str, accounted: int) -> None:
    assert ci_linux.result_summary(f"some output\n{line}\n") is not None
    assert ci_linux.tests_accounted_for(ci_linux.result_summary(f"some output\n{line}\n")) == accounted


def test_a_line_that_merely_mentions_tests_is_not_a_summary() -> None:
    """The duration is what tells pytest's own last line from everything else
    that carries a number, including pip's."""
    assert ci_linux.result_summary("collected 1476 items\nSuccessfully installed 12 packages\n") is None


def posix_shell() -> str | None:
    """A shell that can run the container's clone step against this host's paths.

    `bash.exe` under System32 is the WSL launcher: it runs a Linux shell that
    cannot see the Windows paths this test writes, so it does not qualify.
    """
    bash = shutil.which("bash")
    if bash is None or Path(bash).parent.name.lower() == "system32":
        return None
    return bash


def commit_a_repository(path: Path) -> None:
    identity = ["-c", "user.email=ci@example.invalid", "-c", "user.name=ci", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")
    subprocess.run(["git", *identity, "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", *identity, "-C", str(path), "commit", "-qm", "init"], check=True)


@pytest.mark.skipif(posix_shell() is None, reason="no POSIX shell that can address this host's paths")
def test_an_unresolvable_alternates_path_fails_the_clone_step(tmp_path: Path) -> None:
    """The tree the tool was observed passing on: its object store borrows
    through `.git/objects/info/alternates` at a path the clone cannot resolve.
    `git clone` dies, and the step that died has to be the one that is named."""
    origin, source, work = tmp_path / "origin", tmp_path / "src", tmp_path / "work"
    commit_a_repository(origin)
    subprocess.run(["git", "clone", "-q", "--shared", str(origin), str(source)], check=True)
    (source / ".git" / "objects" / "info" / "alternates").write_text(
        (tmp_path / "gone" / "objects").as_posix() + "\n", encoding="utf-8"
    )

    script = ci_linux.CLONE_SCRIPT.replace("/src", source.as_posix()).replace("/work", work.as_posix())
    result = subprocess.run([posix_shell(), "-c", script, "ci_linux"], capture_output=True, text=True, check=False)

    assert result.returncode == ci_linux.EXIT_CLONE_FAILED
    assert ci_linux.CLONE_FAILURE_MARKER in result.stdout + result.stderr
    assert not work.exists()


def test_the_container_script_keeps_the_clone_guard() -> None:
    """The guard is only worth anything if the script the container runs has it."""
    assert ci_linux.CLONE_SCRIPT in ci_linux.SCRIPT
    assert ci_linux.CLONE_FAILURE_MARKER in ci_linux.SCRIPT
