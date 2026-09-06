"""install.sh, run for real on the two routes #488 found it failing on.

The suite's stub uv writes its console script into the bin directory whatever
is already there, and its stub curl delivers an installer of the suite's own
that touches no file it was not asked to. Both are assumptions about somebody
else's program, and both were wrong:

* uv refuses to overwrite an executable it did not write. A machine that
  installed agentic-hil with `pip install --user` and later gained uv has the
  console script pip wrote in `~/.local/bin`, which is also uv's own bin, so
  the anchor's `uv tool install` installed every package and then stopped on
  `error: Executable already exists: agentic-hil (use --force to overwrite)`
  (uv 0.12.9, recorded here on 2026-09-06), and the script closed on a line
  naming no fix. The old copy stayed on PATH.

* Astral's installer edits `~/.profile` and `~/.bashrc`, and creates `~/.zshrc`,
  unless `UV_NO_MODIFY_PATH` is set, and install.sh never set it. The five lines
  at the top of the script, which a stranger reads before piping it into a
  shell, promise it touches no shell rc file, and step 3 then tells the reader
  to add the same directory to their profile a second time.

Both routes run here against the real tools: the real pip and the real uv in
this image, and the pinned Astral installer fetched from astral.sh and checked
against the hash install.sh carries, which is the only way the rc-file edit can
be observed at all. The refresh over a uv-managed tool already runs in
`test_install_refresh.py`; these are the other two ways a machine arrives at
step 2.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import (
    BELOW_EVERY_RELEASE,
    CONTAINER_ONLY,
    INSTALL_TIMEOUT_S,
    ONE_RELEASE_ABOVE_THAT,
    REPOSITORY_ROOT,
    Wheelhouse,
)

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

SHELL_SCRIPT = REPOSITORY_ROOT / "install.sh"

# What the suite's own sandbox sets and a real machine's shell would not: with
# any of these in the environment uv and Astral's installer put their bin
# somewhere other than `~/.local/bin`, and the collision and the rc-file edit
# are both about that one directory.
SANDBOX_LOCATION_VARIABLES = ("XDG_BIN_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "UV_TOOL_BIN_DIR", "UV_INSTALL_DIR", "PYTHONUSERBASE")


def release_named_by_the_script() -> str:
    found = re.search(r'^RELEASE="(\d+\.\d+\.\d+)"$', SHELL_SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert found is not None, "install.sh states no RELEASE"
    return found.group(1)


def a_machines_environment(home: Path, *, path: str, **overrides: str | None) -> dict[str, str]:
    """This test's environment as a shell on a real machine would have it.

    HOME moved to the sandbox, every location override the suite sets removed,
    and nothing that could steer uv or pip away from the directories the two
    defects are about. `UV_NO_MODIFY_PATH` is deliberately not set here: whether
    the installer is told to leave the rc files alone is install.sh's business
    and the whole question.
    """
    environment = {key: value for key, value in os.environ.items() if key not in SANDBOX_LOCATION_VARIABLES}
    environment.pop("AGENTIC_HIL_CONFIG", None)
    environment.pop("UV_NO_MODIFY_PATH", None)
    environment["HOME"] = str(home)
    environment["PATH"] = path
    for key, value in overrides.items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    return environment


def run_install(project: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(SHELL_SCRIPT), "--no-agent-install", "--no-can"],
        cwd=str(project),
        capture_output=True,
        text=True,
        env=environment,
        timeout=INSTALL_TIMEOUT_S,
        check=False,
    )


def version_answered_by(executable: Path, environment: dict[str, str]) -> str:
    answered = subprocess.run([str(executable), "--version"], capture_output=True, text=True, env=environment, timeout=INSTALL_TIMEOUT_S, check=False)
    assert answered.returncode == 0, f"{executable} --version failed:\n{answered.stdout}\n{answered.stderr}"
    return answered.stdout.strip()


def test_the_anchor_upgrades_a_pip_user_copy_when_uv_is_present(tmp_path: Path, wheelhouse: Wheelhouse, uv_cache: Path) -> None:
    """`pip install --user` first, uv later, then the one-line installer.

    The copy pip wrote sits in `~/.local/bin`, which is also where uv writes its
    console scripts, so a `uv tool install` that is not told to replace it
    refuses. The old copy is a release below everything, so step 1 has to call
    the run an upgrade, and the fresh copy uv is offered sits above everything
    the index serves, so what answers afterwards is decided here and not by the
    index. Everything else comes from the index, the way it does on a bench.
    """
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    user_bin = home / ".local" / "bin"
    tools = tmp_path / "uv" / "tools"
    environment = a_machines_environment(home, path=f"{user_bin}{os.pathsep}{os.environ.get('PATH', '')}", UV_TOOL_DIR=str(tools), UV_CACHE_DIR=str(uv_cache))

    pip = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--user", "--find-links", str(wheelhouse.only(BELOW_EVERY_RELEASE)), f"agentic-hil=={BELOW_EVERY_RELEASE}"],
        capture_output=True,
        text=True,
        env=environment,
        timeout=INSTALL_TIMEOUT_S,
        check=False,
    )
    assert pip.returncode == 0, f"{pip.stdout}\n{pip.stderr}"
    old_copy = user_bin / "agentic-hil"
    assert old_copy.is_file(), sorted(user_bin.iterdir()) if user_bin.is_dir() else "no user bin"
    assert version_answered_by(old_copy, environment) == BELOW_EVERY_RELEASE
    # The premise of the collision: uv's own bin is the directory pip wrote into.
    uv_bin = subprocess.run(["uv", "tool", "dir", "--bin"], capture_output=True, text=True, env=environment, timeout=INSTALL_TIMEOUT_S, check=False)
    assert uv_bin.returncode == 0, uv_bin.stderr
    assert Path(uv_bin.stdout.strip()) == user_bin, uv_bin.stdout

    refreshed = run_install(project, {**environment, "UV_FIND_LINKS": str(wheelhouse.only(ONE_RELEASE_ABOVE_THAT))})

    transcript = f"{refreshed.stdout}{refreshed.stderr}"
    assert refreshed.returncode == 0, transcript
    assert f"is older than {release_named_by_the_script()}, upgrading it" in transcript, transcript
    assert "could not install" not in transcript, transcript
    assert "Executable already exists" not in transcript, transcript
    assert version_answered_by(old_copy, environment) == ONE_RELEASE_ABOVE_THAT, transcript
    listed = subprocess.run(["uv", "tool", "list"], capture_output=True, text=True, env=environment, timeout=INSTALL_TIMEOUT_S, check=False)
    assert re.search(r"^agentic-hil v", listed.stdout, re.MULTILINE), f"{listed.stdout}\n{listed.stderr}"


def test_the_uv_fetch_route_leaves_every_shell_rc_file_untouched(tmp_path: Path, uv_cache: Path) -> None:
    """No uv and no Python on PATH, so step 2 fetches Astral's pinned installer and runs it.

    The bytes that run are the ones the pin in install.sh vouches for, fetched
    from astral.sh and checked here by the script itself, because the rc-file
    edit is that installer's own behaviour and a stand-in cannot reproduce it.
    Two rc files exist before the run and one does not; afterwards the two are
    byte for byte what they were and the third is still absent, and uv and
    agentic-hil are both where the run says they landed.
    """
    curl = shutil.which("curl")
    assert curl is not None, "curl is not on PATH: install.sh fetches the pinned uv installer with curl or wget, so this image has to carry one of them"
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    profile = home / ".profile"
    bashrc = home / ".bashrc"
    zshrc = home / ".zshrc"
    profile.write_text("# the operator's own profile\nexport EDITOR=vi\n", encoding="utf-8")
    bashrc.write_text("# the operator's own bashrc\nalias ll='ls -l'\n", encoding="utf-8")
    recorded = {path: path.read_bytes() for path in (profile, bashrc)}
    assert not zshrc.exists()

    # A PATH with the distribution's tools and neither uv nor an interpreter: the
    # Debian server and the macOS laptop the issue names. Checked rather than
    # assumed, because this image has both one directory further along.
    path = "/usr/bin:/bin"
    for absent in ("uv", "python3", "python"):
        assert shutil.which(absent, path=path) is None, f"{absent} resolves on {path}, so this would not be the fetch route"
    assert shutil.which("curl", path=path) is not None, path
    environment = a_machines_environment(home, path=path, UV_TOOL_DIR=str(tmp_path / "uv" / "tools"), UV_CACHE_DIR=str(uv_cache))
    assert "UV_NO_MODIFY_PATH" not in environment

    installed = run_install(project, environment)

    transcript = f"{installed.stdout}{installed.stderr}"
    assert installed.returncode == 0, transcript
    assert "fetching Astral's uv installer first" in transcript, transcript
    assert (home / ".local" / "bin" / "uv").is_file(), transcript
    assert (home / ".local" / "bin" / "agentic-hil").is_file(), transcript
    for rc_file, before in recorded.items():
        assert rc_file.read_bytes() == before, f"{rc_file.name} was edited:\n{rc_file.read_text(encoding='utf-8')}\n{transcript}"
    assert not zshrc.exists(), f"{zshrc.name} was created:\n{zshrc.read_text(encoding='utf-8')}\n{transcript}"
