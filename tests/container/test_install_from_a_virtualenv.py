"""install.sh run from a shell with a virtual environment activated.

A developer who activates a project's virtualenv and then pastes the one-liner
has `python3` resolving to that environment's interpreter. It is new enough
and it has pip, so step 2 takes the pip route and runs `pip install --user`,
and pip answers what it always answers there:

    ERROR: Can not perform a '--user' install. User site-packages are not visible in this virtualenv.

(pip 26.1.2, recorded on 2026-09-06 from a venv made by this suite's own
interpreter). The run then ended on `pip could not install`, exit 1, on a
machine where uv was one pinned fetch away, which is exactly the shape 0.21.3
fixed for the interpreter that had no pip at all.

The pip here is the real pip inside a real venv made from this image's
interpreter, because the refusal is pip's sentence and a stub could only
repeat what somebody remembered of it. The uv the fetch route delivers is
this image's real uv, handed over by a stand-in for Astral's installer so the
test reaches no network for the bootstrap; the package itself resolves the way
it does on a bench.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import CONTAINER_ONLY, INSTALL_TIMEOUT_S, ONE_RELEASE_ABOVE_THAT, REPOSITORY_ROOT, UvTool, Wheelhouse

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

SHELL_SCRIPT = REPOSITORY_ROOT / "install.sh"
PIP_USER_REFUSAL = "ERROR: Can not perform a '--user' install. User site-packages are not visible in this virtualenv."


def pinned_installer_digest() -> str:
    found = re.search(r'^UV_INSTALLER_SHA256="([0-9a-f]{64})"$', SHELL_SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert found is not None, "install.sh carries no UV_INSTALLER_SHA256"
    return found.group(1)


def executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)


def a_fetch_route_that_delivers(real_uv: str, into: Path, home: Path) -> None:
    """A `curl` that writes an installer of this test's own, and a `sha256sum` that vouches for it.

    The installer copies the image's real uv into `~/.local/bin`, which is
    where Astral's puts it. The digest check is not what is under test here
    (its own test runs the real hashing tools against a substituted payload),
    so the stand-in answers the pinned digest for whatever it is handed.
    """
    into.mkdir(parents=True)
    user_bin = home / ".local" / "bin"
    executable(
        into / "curl",
        'out=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then out="$2"; fi\n'
        "  shift\n"
        "done\n"
        '[ -n "$out" ] || exit 1\n'
        'cat > "$out" <<\'PAYLOAD\'\n'
        "#!/bin/sh\n"
        f'mkdir -p "{user_bin}"\n'
        f'cp "{real_uv}" "{user_bin}/uv"\n'
        f'chmod +x "{user_bin}/uv"\n'
        "PAYLOAD\n"
        "exit 0\n",
    )
    executable(into / "sha256sum", f'echo "{pinned_installer_digest()}  $1"\nexit 0\n')


@pytest.mark.parametrize("activated", [True, False], ids=["with-virtual-env-exported", "with-nothing-in-the-environment"])
def test_an_activated_virtualenv_does_not_end_the_run_on_pips_user_refusal(uv_tool: UvTool, wheelhouse: Wheelhouse, tmp_path: Path, activated: bool) -> None:
    """The venv's python3 first on PATH and no uv anywhere on it, run twice over.

    What is asserted: the run does not stop on pip's refusal, step 2 says
    which interpreter is a virtual environment's and that it is falling back
    to uv, and the package lands where uv puts tools. The pip route is not
    forbidden from being tried; what is forbidden is ending the run on a
    refusal the script can read.

    Once with `VIRTUAL_ENV` exported, which is the activated shell, and once
    without it, which is a PATH that reaches a venv's `bin` for any other
    reason: a wrapper, a `direnv` that edited PATH alone, a Makefile. pip
    refuses `--user` in both, because what it reads is the interpreter's own
    prefixes and not the environment, so a script that answered this question
    out of `$VIRTUAL_ENV` would still end the second run on the refusal.
    """
    real_uv = shutil.which("uv")
    assert real_uv is not None
    venv = tmp_path / "venv"
    made = subprocess.run([sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S, check=False)
    assert made.returncode == 0, made.stderr
    venv_python = venv / "bin" / "python3"
    assert venv_python.is_file(), sorted((venv / "bin").iterdir())
    # The premise, from the real pip: this is what step 2 meets.
    refused = subprocess.run([str(venv_python), "-m", "pip", "install", "--user", "--no-index", "--disable-pip-version-check", "agentic-hil"], capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S, check=False)
    assert refused.returncode != 0
    assert PIP_USER_REFUSAL in f"{refused.stdout}{refused.stderr}", f"{refused.stdout}\n{refused.stderr}"

    home = Path(os.environ["HOME"])
    project = tmp_path / "project"
    project.mkdir()
    stand_ins = tmp_path / "fetch-route"
    a_fetch_route_that_delivers(real_uv, stand_ins, home)
    path = f"{venv / 'bin'}{os.pathsep}{stand_ins}{os.pathsep}/usr/bin:/bin"
    assert shutil.which("uv", path=path) is None, path
    assert shutil.which("python3", path=path) == str(venv_python), path
    environment = uv_tool.environment(PATH=path, UV_FIND_LINKS=str(wheelhouse.only(ONE_RELEASE_ABOVE_THAT)))
    environment.pop("VIRTUAL_ENV", None)
    if activated:
        environment["VIRTUAL_ENV"] = str(venv)

    installed = subprocess.run(
        ["sh", str(SHELL_SCRIPT), "--no-agent-install", "--no-can"],
        cwd=str(project),
        capture_output=True,
        text=True,
        env=environment,
        timeout=INSTALL_TIMEOUT_S,
        check=False,
    )

    transcript = f"{installed.stdout}{installed.stderr}"
    assert installed.returncode == 0, transcript
    assert "pip could not install" not in transcript, transcript
    # Everything said between step 2 and step 3: the step line itself and
    # whatever the fallback says beside it, the way the PEP 668 fallback does.
    lines = transcript.splitlines()
    starts = [index for index, line in enumerate(lines) if "step 2/" in line]
    assert starts, transcript
    ends = [index for index, line in enumerate(lines) if "step 3/" in line]
    named = "\n".join(lines[starts[0] : ends[0] if ends else None])
    assert re.search(r"virtual ?env", named, re.IGNORECASE), transcript
    assert str(venv_python) in named or str(venv) in named, transcript
    assert "falling back to uv" in named, transcript
    assert uv_tool.launcher.is_file(), transcript
    assert uv_tool.run("--version").stdout.strip() == ONE_RELEASE_ABOVE_THAT, transcript
