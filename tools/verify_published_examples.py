"""Prove the CI examples' pin resolves to a published distribution whose CLI
exposes every command they invoke.

`examples/ci/github-actions.yml` and `examples/ci/gitlab-ci.yml` pin
`AGENTIC_HIL_VERSION` to the release this tree builds toward: the release that
first exposes the `check-plan` and `run-evidence` commands the examples invoke.
`tests/test_ci_examples.py` proves, against this checkout's `build_parser`, that
the examples invoke only commands the code defines and that the pin equals that
anticipated release. What a hermetic unit test cannot do is reach the published
distribution: it has no network, and between releases the pin names a release
that is not on the index yet.

This is the other half, and it can run only at the one moment the pin is a real
distribution: after the release job has published to PyPI. It reads the exact
string a reader copies out of the examples, confirms both examples agree on it
and that it is the release this tree builds toward, then -- given an
`agentic-hil` installed from exactly that pin -- confirms the installed CLI
reports that version and answers `--help` for every command the examples invoke.
The development checkout stops standing in for the published artifact; the
artifact answers for itself. See docs/release-strategy.md, "Verifying the CI
example pin after publication".

It reads files and shells out to an installed console script (no secret, no
OIDC), and imports only the standard library and the version gate beside it, so
it runs on every Python the project supports.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from check_version_consistency import anticipated_release

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# The commands the shipped examples invoke, so a copied example needs the pinned
# distribution to expose every one of them. `check-plan` and `run-evidence` are
# the two this tree added; `doctor` and `test-reactor` predate them. Held
# identical to the set tests/test_ci_examples.py asserts the examples invoke, so
# a command added to an example without being added here is caught there first.
REQUIRED_COMMANDS = ("doctor", "test-reactor", "check-plan", "run-evidence")

EXAMPLES = ("examples/ci/github-actions.yml", "examples/ci/gitlab-ci.yml")
# `AGENTIC_HIL_VERSION: "X.Y.Z"` (GitHub `env:`) and the same key under GitLab
# `variables:`. Both spell the value with quotes; a range or a `latest` carries
# no `X.Y.Z` and so is read as zero pins, not one.
PINNED_VERSION = re.compile(r"""AGENTIC_HIL_VERSION:\s*["'](\d+\.\d+\.\d+)["']""")

CommandRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _default_run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), text=True, capture_output=True, timeout=60, check=False)


def example_pin(root: Path) -> str:
    """The one `AGENTIC_HIL_VERSION` both examples carry, or a SystemExit.

    Both examples have to agree, and the value has to be the release this tree
    builds toward, because the install this verifies uses that one string and a
    reader copies whichever file is theirs. tests/test_ci_examples.py holds the
    same two equalities against the checkout; restating them here keeps the tool
    honest when it is run by hand, and it is what turns "a version" into "the
    exact pinned artifact" the check exists to download.
    """
    pins = set()
    for relative in EXAMPLES:
        found = PINNED_VERSION.findall((root / relative).read_text(encoding="utf-8"))
        if len(found) != 1:
            raise SystemExit(f"{relative} states {len(found)} AGENTIC_HIL_VERSION pins, expected exactly one")
        pins.add(found[0])
    if len(pins) != 1:
        raise SystemExit(f"the CI examples disagree on AGENTIC_HIL_VERSION: {sorted(pins)}")
    pin = pins.pop()
    anticipated = anticipated_release(root)
    if pin != anticipated:
        raise SystemExit(
            f"the CI examples pin agentic-hil=={pin}, but the release this tree builds toward is {anticipated}"
        )
    return pin


def cli_problems(
    executable: str,
    version: str,
    commands: Sequence[str] = REQUIRED_COMMANDS,
    run: CommandRunner = _default_run,
) -> list[str]:
    """Every way an installed `agentic-hil` fails to be the pinned distribution.

    The version has to be exactly the pin -- an install that resolved to anything
    else is not the artifact the examples name -- and every command the examples
    invoke has to answer `--help`, which argparse exits non-zero for a subcommand
    it does not define. That is the whole gap this closes: the release before the
    one these examples pin rejected `check-plan` and `run-evidence` at argument
    parsing, and nothing here reached a published distribution to see it.
    """
    found: list[str] = []
    reported = run([executable, "--version"])
    if reported.returncode != 0:
        found.append(f"`{executable} --version` exited {reported.returncode}: {reported.stderr.strip()}")
    elif reported.stdout.strip() != version:
        found.append(f"`{executable} --version` reports {reported.stdout.strip()!r}, not the pinned {version}")
    for command in commands:
        probe = run([executable, command, "--help"])
        if probe.returncode != 0:
            found.append(
                f"the published {version} distribution does not expose `agentic-hil {command}`, "
                f"which the CI examples invoke (`{command} --help` exited {probe.returncode})"
            )
    return found


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    root = REPOSITORY_ROOT
    executable: str | None = None
    print_pin = False
    while arguments:
        argument = arguments.pop(0)
        if argument == "--print-pin":
            print_pin = True
        elif argument == "--agentic-hil":
            executable = arguments.pop(0) if arguments else None
        elif argument.startswith("--agentic-hil="):
            executable = argument.split("=", 1)[1]
        elif argument.startswith("--root="):
            root = Path(argument.split("=", 1)[1])
        elif argument.startswith("--"):
            raise SystemExit(f"unknown option {argument}")
        else:
            root = Path(argument)
    pin = example_pin(root)
    if print_pin:
        print(pin)
        return 0
    if executable is None:
        raise SystemExit("pass --agentic-hil <path> to the installed console script, or --print-pin")
    found = cli_problems(executable, pin)
    for problem in found:
        print(problem, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
