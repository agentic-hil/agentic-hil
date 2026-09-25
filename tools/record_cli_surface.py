"""Record the command surface of the release the CI examples pin.

`examples/ci/github-actions.yml` and `examples/ci/gitlab-ci.yml` pin the newest
published release, so a copied example installs on the day it is copied (#525).
`tests/test_ci_examples.py` refuses an example that invokes a subcommand that
release does not define, and this checkout's parser cannot answer that: between
releases it may define a command the pinned release lacks. So the test reads a
committed recording instead, `tests/fixtures/published_cli_surface.json`: the
argparse tree of the pinned release, every subcommand with its option strings
and positional names, and no help text.

Two ways to take it, one for each moment it is taken:

- By default the pinned release is installed from the package index into a
  throwaway virtual environment and its parser is walked there. That is the
  distribution a reader who copies an example gets, so this recording speaks
  for it.
- `--from-tree` walks this checkout instead, and it is the release stamp's
  mode. On a release commit the pin is this tree's own version, which the index
  does not carry until the release job uploads it, and the stamped tree is what
  gets uploaded. A development tree is refused, because its parser is no
  release's.

The parser is walked in a separate interpreter started with `-I`, so neither
the caller's environment variables nor a user-site installation decide which
`agentic_hil` answers, and a walk whose module was imported from anywhere but
the throwaway environment or this checkout's `src` is refused.

It imports only the standard library and the two tools beside it, so it runs on
every Python the project supports.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import NoReturn

from check_version_consistency import is_development, package_version
from verify_published_examples import example_pin

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

RECORDING = "tests/fixtures/published_cli_surface.json"
# The index a reader's `pip install` reaches by default, named rather than left
# to whatever pip configuration the machine that records happens to carry.
INDEX_URL = "https://pypi.org/simple/"
# Creating an environment and installing a release with its dependencies takes
# seconds on a warm connection and minutes on a slow one.
TIMEOUT_S = 600

WHAT = (
    "The argparse command surface of the agentic-hil release the shipped CI examples pin: every subcommand, "
    "with its option strings and positional names and without help text. tests/test_ci_examples.py refuses an "
    "example that invokes a subcommand this surface does not define, and tools/record_cli_surface.py takes it."
)

CommandRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _default_run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=TIMEOUT_S, check=False
    )


def command_surface(parser: argparse.ArgumentParser) -> dict:
    """A parser's option strings, positional names and subcommands, recursively.

    Names only: help text changes with every wording fix and says nothing about
    whether a command line parses. A positional is recorded by the name argparse
    stores it under, in the order it is consumed, and a subcommand by every name
    a command line can type for it.
    """
    options = []
    positionals = []
    subcommands = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                subcommands[name] = command_surface(child)
        elif action.option_strings:
            options.extend(action.option_strings)
        else:
            positionals.append(action.dest)
    return {"options": sorted(options), "positionals": positionals, "subcommands": dict(sorted(subcommands.items()))}


def walker_program() -> str:
    """What the walking interpreter runs: `command_surface`, then the walk.

    An optional first argument is put in front of `sys.path`, which is how a
    source tree is walked instead of an installation.
    """
    return "\n".join(
        [
            "import argparse",
            "import json",
            "import sys",
            "",
            inspect.getsource(command_surface),
            "if len(sys.argv) > 1:",
            "    sys.path.insert(0, sys.argv[1])",
            "import agentic_hil",
            "from agentic_hil.cli import build_parser",
            "",
            "json.dump(",
            '    {"version": agentic_hil.__version__, "module": agentic_hil.__file__, '
            '"surface": command_surface(build_parser())},',
            "    sys.stdout,",
            ")",
            "",
        ]
    )


def _refuse(failed: str, completed: subprocess.CompletedProcess[str]) -> NoReturn:
    """Stop, with everything the child printed above and its decisive last line in the message.

    The last line of stderr is where pip names the requirement it could not
    satisfy and where a traceback names its exception; stdout stands in when
    stderr is empty.
    """
    for stream in (completed.stdout, completed.stderr):
        if stream and stream.strip():
            print(stream.rstrip(), file=sys.stderr)
    lines = [line.strip() for line in (completed.stderr or "").splitlines() if line.strip()]
    lines = lines or [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    raise SystemExit(f"{failed}: {lines[-1] if lines else 'nothing was printed'}")


def walk(python: str, source: Path | None = None, run: CommandRunner = _default_run) -> dict:
    """Walk the parser of the `agentic_hil` that `python -I` imports, or the one under `source`."""
    argv = [python, "-I", "-c", walker_program()]
    if source is not None:
        argv.append(str(source))
    completed = run(argv)
    if completed.returncode != 0:
        _refuse(f"walking the agentic-hil parser with {python} exited {completed.returncode}", completed)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        _refuse(f"walking the agentic-hil parser with {python} printed no surface", completed)


def _require(walked: dict, version: str, place: Path) -> None:
    """Refuse a walk of another release, or of an `agentic_hil` imported from somewhere else."""
    if walked["version"] != version:
        raise SystemExit(f"the parser walked is agentic-hil {walked['version']}, not the {version} being recorded")
    module = Path(walked["module"]).resolve()
    if not module.is_relative_to(place.resolve()):
        raise SystemExit(f"the parser walked was imported from {module}, not from {place}")


def published_surface(version: str, run: CommandRunner = _default_run) -> dict:
    """Install `agentic-hil==version` from the index into a throwaway environment and walk its parser."""
    with tempfile.TemporaryDirectory(prefix="agentic-hil-surface-", ignore_cleanup_errors=True) as scratch:
        environment = Path(scratch) / "venv"
        created = run([sys.executable, "-I", "-m", "venv", str(environment)])
        if created.returncode != 0:
            _refuse(f"creating a throwaway virtual environment exited {created.returncode}", created)
        python = str(environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        requirement = f"agentic-hil=={version}"
        installed = run(
            [
                python,
                "-I",
                "-m",
                "pip",
                "install",
                "--isolated",
                "--no-cache-dir",
                "--disable-pip-version-check",
                "--index-url",
                INDEX_URL,
                requirement,
            ]
        )
        if installed.returncode != 0:
            _refuse(f"installing {requirement} from {INDEX_URL} exited {installed.returncode}", installed)
        walked = walk(python, run=run)
        _require(walked, version, environment)
        return walked


def tree_surface(root: Path, run: CommandRunner = _default_run) -> dict:
    """Walk this checkout's parser, which on a release commit is the release being cut."""
    version = package_version(root)
    if is_development(version):
        raise SystemExit(
            f"pyproject.toml states development version {version}, and a development tree's parser is no "
            f"release's: record the published release the CI examples pin instead, without --from-tree"
        )
    source = (root / "src").resolve()
    walked = walk(sys.executable, source, run=run)
    _require(walked, version, source)
    return walked


def recording(walked: dict, source: str, recorded_on: str) -> dict:
    """The committed document: what it is, which release, how it was taken, when, and the surface."""
    return {
        "what": WHAT,
        "version": walked["version"],
        "source": source,
        "recorded_on": recorded_on,
        "surface": walked["surface"],
    }


def main(argv: list[str] | None = None, run: CommandRunner = _default_run) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    root = REPOSITORY_ROOT
    from_tree = False
    while arguments:
        argument = arguments.pop(0)
        if argument == "--from-tree":
            from_tree = True
        elif argument.startswith("--root="):
            root = Path(argument.split("=", 1)[1])
        elif argument.startswith("--"):
            raise SystemExit(f"unknown option {argument}")
        else:
            root = Path(argument)
    if from_tree:
        walked = tree_surface(root, run=run)
        source = (
            f"Walked from the agentic-hil {walked['version']} source tree on its release commit with "
            f"tools/record_cli_surface.py --from-tree, before the release reached the package index."
        )
    else:
        pin = example_pin(root)
        walked = published_surface(pin, run=run)
        source = (
            f"Installed agentic-hil=={pin} from {INDEX_URL} into a throwaway virtual environment and walked "
            f"its parser with tools/record_cli_surface.py."
        )
    document = recording(walked, source, date.today().isoformat())
    (root / RECORDING).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(
        f"recorded the agentic-hil {document['version']} command surface, "
        f"{len(document['surface']['subcommands'])} subcommands, in {RECORDING}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
