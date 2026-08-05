#!/usr/local/bin/python
"""Give each agent the environment of the commit it is working on.

Both agents run in one container, and they used to share one virtualenv built
once, before the first round, from the repository's pre-loop HEAD. Source
imports were already separated -- each agent's tree comes first on its own
PYTHONPATH -- but nothing else in that environment was: the installed
dependencies, the console and pytest entry points, and the distribution metadata
`importlib.metadata` answers out of all stayed at that first snapshot. This
suite reads that metadata on purpose, so a round that changed `pyproject.toml`
was reviewed against the wrong commit; and a `pip install` either agent ran
landed in the environment the other one inherited next.

This runs in their place. It is installed as `/opt/loop/bin/claude` and
`/opt/loop/bin/codex`, first on PATH, so the loop's own `shutil.which` finds it
without being told; it builds the environment for whichever name it was invoked
under, puts that environment first on the PATH the CLI inherits, and hands over
with exec.

Each environment is built from the committed tree its agent works in -- the
implementer's mount for one, the reviewer's checkout at the commit under review
for the other -- and from a copy of that tree rather than the tree itself,
because pip builds a directory install in place and its `build/` and
`*.egg-info` belong in neither. It is rebuilt only when that commit's
`pyproject.toml` differs from the one the environment already holds: that file
alone decides the dependencies, the entry points and every field of the
metadata.

What this deliberately does not do is follow uncommitted work. An implementer
that has just edited `pyproject.toml` and not yet committed it is running
against its previous commit's metadata, and it can `pip install` its way out --
into its own environment, which is the half of this that keeps the reviewer's
installs off the implementer.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

# Fixed rather than read from the environment: this is the container's home, and
# an agent that inherited something else would build its environment somewhere
# the wrapper does not delete afterwards. tools/loopimage/entrypoint.py agrees.
HOME = Path("/home/loop")
SCRATCH = Path("/tmp")
REAL_CLI_DIRECTORY = Path("/opt/agent-clis/node_modules/.bin")

# The extras the suite needs; the project's own requirements come with them.
EXTRAS = ("dev", "can")

# Which environment each CLI gets. The names are the two roles rather than the
# two CLIs, because what separates the environments is the tree each one works
# in: the implementer's mount and the reviewer's checkout.
ROLES = {"claude": "implement", "codex": "review"}

STAMP = ".loop-environment"


class EnvironmentFailure(RuntimeError):
    """The environment could not be rebuilt, with a reason worth printing."""


def virtualenv(role: str) -> Path:
    """Where a role's environment lives: the home volume, which the wrapper deletes.

    Not the tmpfs. A virtualenv there is charged against the container's memory
    limit, and two of them plus the suite would spend the measurement the
    --memory value in tools/loop_in_container.py rests on.
    """
    return HOME / f".venv-{role}"


def export_directory(role: str) -> Path:
    return SCRATCH / "loop-environment" / role


def run(command: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, capture_output=True, timeout=timeout, check=False)


def top_level(start: Path) -> Path:
    """The work tree the agent was started in, which is the tree it is judged on."""
    result = run(["git", "-C", str(start), "rev-parse", "--show-toplevel"], 60)
    if result.returncode != 0:
        raise EnvironmentFailure(f"{start} is not inside a git work tree: {result.stderr.decode(errors='replace').strip()}")
    return Path(result.stdout.decode(errors="replace").strip())


def recipe(source: Path) -> bytes:
    """The committed pyproject.toml, which is what an environment is built from."""
    result = run(["git", "-C", str(source), "show", "HEAD:pyproject.toml"], 60)
    if result.returncode != 0:
        raise EnvironmentFailure(f"{source} has no committed pyproject.toml: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def export_head(source: Path, destination: Path) -> None:
    """Copy the committed tree out, so that building it writes nothing back into it."""
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    process = subprocess.Popen(
        ["git", "-C", str(source), "archive", "--format=tar", "HEAD"],
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            archive.extractall(destination, filter="data")
    finally:
        process.stdout.close()
        code = process.wait(timeout=300)
    if code != 0:
        raise EnvironmentFailure(f"git archive HEAD failed in {source} with {code}")


def build_environment(role: str, source: Path) -> Path:
    """Bring a role's environment up to the committed state of its own tree."""
    venv = virtualenv(role)
    digest = hashlib.sha256(recipe(source)).hexdigest()
    stamp = venv / STAMP
    if (venv / "bin" / "python").is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == digest:
        return venv

    export = export_directory(role)
    print(f"{role} environment: building from {source} at {digest[:12]}", flush=True)
    export_head(source, export)
    if not (venv / "bin" / "python").is_file():
        result = run([sys.executable, "-m", "venv", str(venv)], 300)
        if result.returncode != 0:
            raise EnvironmentFailure(f"could not create {venv}: {result.stderr.decode(errors='replace').strip()}")
    result = run([str(venv / "bin" / "pip"), "install", "--quiet", f"{export}[{','.join(EXTRAS)}]"], 1800)
    if result.returncode != 0:
        raise EnvironmentFailure(f"could not install {export}: {result.stderr.decode(errors='replace').strip()}")
    stamp.write_text(f"{digest}\n", encoding="utf-8")
    return venv


def child_environment(venv: Path) -> dict[str, str]:
    """The environment the CLI is handed: its own virtualenv, first on PATH."""
    environment = dict(os.environ)
    entries = [str(venv / "bin"), *[entry for entry in environment.get("PATH", "").split(os.pathsep) if entry]]
    # dict.fromkeys keeps the first occurrence, so a second copy of this
    # virtualenv further down the inherited PATH does not survive twice.
    environment["PATH"] = os.pathsep.join(dict.fromkeys(entries))
    environment["VIRTUAL_ENV"] = str(venv)
    return environment


def main(argv: list[str]) -> int:
    name = Path(argv[0]).name
    role = ROLES.get(name)
    if role is None:
        raise SystemExit(f"{argv[0]}: this stands in for {' and '.join(sorted(ROLES))}, and for nothing else")
    real = REAL_CLI_DIRECTORY / name
    if not real.exists():
        raise SystemExit(f"the {name} CLI is not installed at {real}")

    try:
        build_environment(role, top_level(Path.cwd()))
    except (EnvironmentFailure, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        # Reported, not fatal. The agent is what this round is paying for, and a
        # tree whose pyproject.toml no longer installs is exactly the failure the
        # agent has been asked to find. It runs with whatever is already there.
        print(f"{name}: could not rebuild the {role} environment: {error}", file=sys.stderr, flush=True)

    os.execve(str(real), [str(real), *argv[1:]], child_environment(virtualenv(role)))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
