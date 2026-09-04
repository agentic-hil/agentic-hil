"""The release-time proof that the CI examples' pin is a runnable distribution.

`tests/test_ci_examples.py` checks the shipped examples against this checkout's
`build_parser`: it proves the examples invoke only commands the code defines and
that the pin equals the release this tree builds toward. It cannot reach the
published distribution, because it has no network and, between releases, the pin
names a release the index does not carry yet.

`tools/verify_published_examples.py` is the other half, run by the publish
workflow after PyPI has the release: it installs the exact pinned distribution
and asks its CLI to answer for itself. This module holds that tool to what it
promises -- the pin it reads is the one both examples carry and the release this
tree builds toward, and the commands it probes are exactly the ones the examples
invoke -- with the network hop stubbed by a fake command runner, so the logic is
covered on a pull request while the real download stays a release-time step.

`tools/` is repository tooling that never ships, so MANIFEST.in excludes this
file from the source distribution for the reason it excludes the other tool
tests.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from verify_published_examples import (  # noqa: E402
    REQUIRED_COMMANDS,
    cli_problems,
    example_pin,
    main,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL = "tools/verify_published_examples.py"

# A synthetic tree's versions, chosen so nothing here matches the version this
# repository actually carries: the version sweep in check_version_consistency
# would otherwise read a test fixture as an uncovered home for the release.
DEVELOPMENT = "1.2.3.dev0"
ANTICIPATED = "1.2.3"
OTHER = "1.2.4"


def _write_tree(root: Path, github_pin: str, gitlab_pin: str, package: str = DEVELOPMENT) -> None:
    """A minimal tree: the two example pins and the pyproject the pin is held to."""
    (root / "pyproject.toml").write_text(f'[project]\nname = "agentic-hil"\nversion = "{package}"\n', encoding="utf-8")
    examples = root / "examples" / "ci"
    examples.mkdir(parents=True)
    (examples / "github-actions.yml").write_text(f'env:\n  AGENTIC_HIL_VERSION: "{github_pin}"\n', encoding="utf-8")
    (examples / "gitlab-ci.yml").write_text(
        f'variables:\n  AGENTIC_HIL_VERSION: "{gitlab_pin}"\n', encoding="utf-8"
    )


def _run(results: dict[tuple[str, ...], subprocess.CompletedProcess[str]]):
    """A fake command runner that answers from a table, keyed by the argv tail.

    The executable path is dropped from the key so a test does not have to name
    it, and an argv nobody planted is a loud failure rather than a silent pass.
    """

    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        key = tuple(argv[1:])
        if key not in results:
            raise AssertionError(f"unexpected command {list(argv)}")
        return results[key]

    return run


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_the_tool_runs_on_every_python_this_project_supports() -> None:
    """It imports only the standard library and the version gate beside it.

    The publish workflow runs it on `python-version: "3.x"`, but it also runs on
    a pull request through this test on the project's oldest Python, so a
    `tomllib` or `packaging` import would make the tool the thing that fails on a
    matrix leg rather than the check that catches a drifted pin.
    """
    source = (REPOSITORY_ROOT / TOOL).read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])

    assert "tomllib" not in modules
    assert "packaging" not in modules
    # The only non-stdlib import is the version gate, which is itself stdlib-only.
    assert "check_version_consistency" in modules


def test_the_probed_commands_are_exactly_the_ones_the_examples_invoke() -> None:
    """The release-time check probes every command a copied example runs, and no
    other. If an example gains a command, this fails until REQUIRED_COMMANDS
    gains it too, so the published-artifact proof never quietly stops covering a
    command the examples added.
    """
    from test_ci_examples import (  # noqa: PLC0415
        GITHUB,
        GITLAB,
        github_commands,
        gitlab_commands,
        invoked_subcommands,
    )

    invoked = invoked_subcommands(
        [
            *github_commands(GITHUB, "hardware"),
            *github_commands(GITHUB, "simulator"),
            *gitlab_commands(GITLAB, "hardware"),
            *gitlab_commands(GITLAB, "simulator"),
        ]
    )

    assert set(REQUIRED_COMMANDS) == invoked


def test_example_pin_reads_the_real_examples_as_the_release_they_build_toward() -> None:
    """Against the real tree, the pin the tool would install is the anticipated one."""
    sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
    from check_version_consistency import anticipated_release  # noqa: PLC0415

    assert example_pin(REPOSITORY_ROOT) == anticipated_release(REPOSITORY_ROOT)


def test_example_pin_reads_a_synthetic_tree(tmp_path: Path) -> None:
    _write_tree(tmp_path, ANTICIPATED, ANTICIPATED)

    assert example_pin(tmp_path) == ANTICIPATED


def test_example_pin_refuses_examples_that_disagree(tmp_path: Path) -> None:
    _write_tree(tmp_path, ANTICIPATED, OTHER)

    with pytest.raises(SystemExit, match="disagree on AGENTIC_HIL_VERSION"):
        example_pin(tmp_path)


def test_example_pin_refuses_a_pin_that_is_not_the_anticipated_release(tmp_path: Path) -> None:
    """A pin both examples agree on is still wrong if it is not what this tree builds."""
    _write_tree(tmp_path, OTHER, OTHER, package=DEVELOPMENT)

    with pytest.raises(SystemExit, match="builds toward"):
        example_pin(tmp_path)


def test_example_pin_refuses_a_range_that_carries_no_exact_version(tmp_path: Path) -> None:
    """A `>=` or a `latest` reads as zero pins, which is the unpinned install the
    examples exist to refuse, not one this tool can install.
    """
    _write_tree(tmp_path, ANTICIPATED, ANTICIPATED)
    (tmp_path / "examples" / "ci" / "github-actions.yml").write_text(
        'env:\n  AGENTIC_HIL_VERSION: ">=1.0.0"\n', encoding="utf-8"
    )

    with pytest.raises(SystemExit, match="expected exactly one"):
        example_pin(tmp_path)


def test_a_matching_install_that_exposes_every_command_passes() -> None:
    results = {("--version",): _completed(0, stdout=f"{ANTICIPATED}\n")}
    for command in REQUIRED_COMMANDS:
        results[(command, "--help")] = _completed(0, stdout="usage: ...")

    assert cli_problems("agentic-hil", ANTICIPATED, run=_run(results)) == []


def test_a_wrong_installed_version_is_caught() -> None:
    """An install that resolved to anything but the pin is not the pinned artifact."""
    results = {("--version",): _completed(0, stdout=f"{OTHER}\n")}
    for command in REQUIRED_COMMANDS:
        results[(command, "--help")] = _completed(0)

    problems = cli_problems("agentic-hil", ANTICIPATED, run=_run(results))

    assert len(problems) == 1
    assert OTHER in problems[0]
    assert ANTICIPATED in problems[0]


def test_a_missing_command_is_caught() -> None:
    """argparse exits non-zero for a subcommand a distribution does not define."""
    results = {("--version",): _completed(0, stdout=f"{ANTICIPATED}\n")}
    for command in REQUIRED_COMMANDS:
        results[(command, "--help")] = _completed(0)
    results[("run-evidence", "--help")] = _completed(2, stderr="invalid choice: 'run-evidence'")

    problems = cli_problems("agentic-hil", ANTICIPATED, run=_run(results))

    assert len(problems) == 1
    assert "run-evidence" in problems[0]


def test_a_version_probe_that_fails_outright_is_caught() -> None:
    results = {("--version",): _completed(1, stderr="No module named agentic_hil")}
    for command in REQUIRED_COMMANDS:
        results[(command, "--help")] = _completed(0)

    problems = cli_problems("agentic-hil", ANTICIPATED, run=_run(results))

    assert any("--version" in problem and "exited 1" in problem for problem in problems)


def test_main_prints_the_pin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_tree(tmp_path, ANTICIPATED, ANTICIPATED)

    assert main(["--print-pin", f"--root={tmp_path}"]) == 0
    assert capsys.readouterr().out.strip() == ANTICIPATED


def test_main_refuses_a_run_without_an_executable(tmp_path: Path) -> None:
    _write_tree(tmp_path, ANTICIPATED, ANTICIPATED)

    with pytest.raises(SystemExit, match="--agentic-hil"):
        main([f"--root={tmp_path}"])


def test_main_rejects_an_unknown_option() -> None:
    with pytest.raises(SystemExit, match="unknown option"):
        main(["--nope"])
