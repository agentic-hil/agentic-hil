"""Where the real shells find the command after install.sh's step 3 (#548).

Release 0.21.5, on Ubuntu 24.04 in a fresh home with bash as the account's
shell, wrote its PATH line into `~/.bashrc` alone and said the next shell would
find the command. The next login shell did not. As a login shell bash reads the
first of `~/.bash_profile`, `~/.bash_login` and `~/.profile` that exists and no
other startup file of the account's, and it reaches `~/.bashrc` only through a
`~/.profile` that sources it. `/etc/skel/.profile` carries that bridge, so an
account made from the skeleton hid the defect; a fresh home, or a login file
its owner wrote, does not.

Which file a shell reads is the shell's to decide, and a fake can only repeat
what somebody believed about it. So install.sh runs here with the real uv, and
the real bash of this image is then asked where `agentic-hil` resolves: as a
login shell, as an interactive shell, and as the plain `bash -c` a script or a
CI step runs, which reads no startup file of the account's and so must not find
the command through them. The homes are the ones the issue and the file choice
name: fresh, seeded from this image's `/etc/skel`, with a `~/.profile` of the
owner's own, and with a `~/.bash_profile` or a `~/.bash_login` that bash reads
instead of that `~/.profile`.

For an account whose shell is bash, `ssh host 'agentic-hil doctor'` runs its
command in a `bash -c` too, and step 3 says such a shell *may* read none of
these files, not that it reads none, because of SSH_SOURCE_BASHRC. Built with
that compile option, bash reads `~/.bashrc` for a `bash -c` that finds
SSH_CLIENT set and SHLVL unset, which is what sshd hands a remote command, and
this image's bash is built with it: in a fresh home, whose `~/.bashrc` holds
the installer's line and nothing else, that shell finds the command. Recorded
on 2026-09-25 with this image's bash 5.2.37 (Debian 13) and with the bash
5.2.21 of ubuntu:24.04, which answered alike. That half depends on how the
shell was built and is not asserted here. The other half is: in a home seeded
from the skeleton the line lands after the early return its `~/.bashrc` makes
for a shell that is not interactive, so the same `bash -c` does not find the
command. No sshd runs here; the variables it would set are set by hand.

zsh, which this image carries for this module, is asked the same question
about the one file step 3 writes for it. zsh reads `~/.zshrc` in an
interactive shell only, so `zsh -i -c` has to find the command and `zsh -c`
must not. fish is not in this image, and tools/container/Dockerfile says why.
Its claims were read off Debian's fish 4.0.2 installed over this image's base
on 2026-09-25: with the `conf.d` file step 3 writes and nothing else,
`fish -c` and `fish -i -c` found the command, which is why the sentence step 3
prints for fish leaves ssh out, and the `sh -c` a cron job or a CI step runs
its command in did not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import (
    COMMAND_TIMEOUT_S,
    CONTAINER_ONLY,
    INSTALL_TIMEOUT_S,
    ONE_RELEASE_ABOVE_THAT,
    REPOSITORY_ROOT,
    Wheelhouse,
)

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

SHELL_SCRIPT = REPOSITORY_ROOT / "install.sh"
SKELETON = Path("/etc/skel")

# The PATH a new shell of the account's starts with, before any startup file has
# run. A login shell's `/etc/profile` replaces it; for root in this image that
# replacement carries `/usr/local/bin`, where the editable agentic-hil the image
# was built with lives, so a login shell that never read the line still finds
# a command, and what is asserted is which one.
STARTUP_PATH = "/usr/bin:/bin"

# What the suite's own sandbox sets and a real machine's shell would not. With
# any of these uv puts its bin somewhere other than `~/.local/bin`, the
# directory the issue is about.
SANDBOX_LOCATION_VARIABLES = ("XDG_BIN_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "UV_TOOL_BIN_DIR", "UV_INSTALL_DIR", "PYTHONUSERBASE")

# One line per answer, so it can be read out whatever a startup file prints.
PROBE = 'printf "PATH=%s\\n" "$PATH"; printf "found=%s\\n" "$(command -v agentic-hil)"'

# The variable sshd sets for the command it runs, with a documentation address
# in it. bash looks for it, and SHLVL stays unset because the environment
# where_the_shell_finds_it builds has none.
SSHD_VARIABLES = {"SSH_CLIENT": "192.0.2.1 50000 22"}

OWN_PROFILE = "# the owner's own profile, which does not read ~/.bashrc\nexport EDITOR=vi\n"
OWN_BASH_PROFILE = "# the owner's own bash_profile, which reads neither ~/.bashrc nor ~/.profile\nexport EDITOR=vi\n"
OWN_BASH_LOGIN = "# the owner's own bash_login, which reads neither ~/.bashrc nor ~/.profile\nexport EDITOR=vi\n"

# Every home but the skeleton's, as the files it holds before the installer runs.
OWNED_HOMES = {
    "fresh": {},
    "own-profile": {".profile": OWN_PROFILE},
    "own-bash-profile": {".bash_profile": OWN_BASH_PROFILE, ".profile": OWN_PROFILE},
    "own-bash-login": {".bash_login": OWN_BASH_LOGIN, ".profile": OWN_PROFILE},
}


def a_home(kind: str, home: Path) -> None:
    home.mkdir(parents=True)
    if kind == "skeleton":
        # The bridge the issue gives the skeleton credit for, read off this
        # image rather than assumed of it.
        assert '. "$HOME/.bashrc"' in (SKELETON / ".profile").read_text(encoding="utf-8"), (SKELETON / ".profile").read_text(encoding="utf-8")
        shutil.copytree(SKELETON, home, dirs_exist_ok=True)
        return
    for name, text in OWNED_HOMES[kind].items():
        (home / name).write_text(text, encoding="utf-8")


def startup_files(home: Path, *names: str) -> str:
    """The account's startup files as the run left them, for a failure message."""
    shown = []
    for name in names or (".bashrc", ".bash_profile", ".bash_login", ".profile"):
        path = home / name
        shown.append(f"--- {name}\n{path.read_text(encoding='utf-8')}" if path.is_file() else f"--- {name}: absent")
    return "\n".join(shown)


def the_shell(name: str) -> str:
    """Where `name` lives on the system PATH, which is where a new session finds it."""
    executable = shutil.which(name, path=STARTUP_PATH)
    assert executable is not None, f"{name} is not on {STARTUP_PATH}, and this module starts it: the base image carries bash and sh, and tools/container/Dockerfile installs zsh"
    return executable


def install_with_the_shell(shell: str, home: Path, tmp_path: Path, wheelhouse: Wheelhouse, uv_cache: Path) -> subprocess.CompletedProcess[str]:
    """install.sh with the real uv, `shell` as `SHELL`, and a PATH that does not carry `~/.local/bin`.

    The PATH holds uv and an interpreter for it to build the tool environment
    with, and nothing else of `/usr/local/bin`, which also carries the image's
    own agentic-hil. The package is this checkout at a version above every
    release, so what lands in `~/.local/bin` is decided here.
    """
    real_uv = shutil.which("uv")
    assert real_uv is not None
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "uv").symlink_to(real_uv)
    (tools / "python3").symlink_to(os.path.realpath(sys.executable))
    path = f"{tools}{os.pathsep}{STARTUP_PATH}"
    assert shutil.which("agentic-hil", path=path) is None, path
    environment = {key: value for key, value in os.environ.items() if key not in SANDBOX_LOCATION_VARIABLES}
    environment.pop("AGENTIC_HIL_CONFIG", None)
    environment.update(
        HOME=str(home),
        PATH=path,
        SHELL=the_shell(shell),
        UV_TOOL_DIR=str(tmp_path / "uv" / "tools"),
        UV_CACHE_DIR=str(uv_cache),
        UV_FIND_LINKS=str(wheelhouse.only(ONE_RELEASE_ABOVE_THAT)),
    )
    project = home / "project"
    project.mkdir()
    return subprocess.run(
        ["sh", str(SHELL_SCRIPT), "--no-agent-install", "--no-can"],
        cwd=str(project),
        capture_output=True,
        text=True,
        env=environment,
        timeout=INSTALL_TIMEOUT_S,
        check=False,
    )


def where_the_shell_finds_it(shell: str, home: Path, *options: str, **variables: str) -> tuple[str, str]:
    """What `command -v agentic-hil` answers in a new `shell` started with `options`, and the whole exchange.

    The environment is what a new session of the account starts from: its home
    and the system PATH, and nothing a startup file could have added, plus the
    `variables` a caller names for the program that starts the shell.
    """
    command = [the_shell(shell), *options, "-c", PROBE]
    answered = subprocess.run(
        command,
        cwd=str(home),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": STARTUP_PATH, **variables},
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    found = [line[len("found=") :] for line in answered.stdout.splitlines() if line.startswith("found=")]
    exchange = f"{' '.join([shell, *options, '-c'])} with {sorted(variables) or 'no variable'} added: exit {answered.returncode}\n{answered.stdout}{answered.stderr}"
    return (found[-1] if found else ""), exchange


@pytest.mark.parametrize("kind", ["fresh", "skeleton", "own-profile", "own-bash-profile", "own-bash-login"])
def test_a_login_shell_and_an_interactive_shell_find_the_command_and_bash_c_does_not(tmp_path: Path, wheelhouse: Wheelhouse, uv_cache: Path, kind: str) -> None:
    """Three new shells of the account after step 3, and where each one finds `agentic-hil`.

    `bash -l` is the login shell an ssh session or a console login starts, and
    `bash -i` is a new terminal window on a Linux desktop; both have to find the
    copy this run installed, which is what step 3 says of them. `bash -c`, with
    nothing in its environment saying sshd started it, reads no startup file of
    the account's, and must find nothing through them for the sentence step 3
    prints about a non-interactive shell to hold.

    Before #548 the fresh home and every home with a login file of the owner's
    own were red for `bash -l`. The skeleton's was green, because its `.profile`
    puts an existing `~/.local/bin` on PATH itself, and it stays here as the
    guard that the second file changes nothing a stock account already had.
    """
    home = tmp_path / "home"
    a_home(kind, home)
    user_bin = home / ".local" / "bin"

    installed = install_with_the_shell("bash", home, tmp_path, wheelhouse, uv_cache)

    transcript = f"{installed.stdout}{installed.stderr}"
    assert installed.returncode == 0, transcript
    assert f"PATH: agentic-hil landed in {user_bin}, which is not on your PATH" in transcript, transcript
    assert "PATH: these files are read by new interactive bash shells and bash login shells;" in transcript, transcript
    copy = user_bin / "agentic-hil"
    assert copy.is_file(), transcript
    login, login_exchange = where_the_shell_finds_it("bash", home, "-l")
    interactive, interactive_exchange = where_the_shell_finds_it("bash", home, "-i")
    plain, plain_exchange = where_the_shell_finds_it("bash", home)
    assert (login, interactive, plain) == (str(copy), str(copy), ""), f"{login_exchange}\n{interactive_exchange}\n{plain_exchange}\n{startup_files(home)}\n{transcript}"


def test_the_bash_c_sshd_starts_misses_the_line_below_the_skeletons_early_return(tmp_path: Path, wheelhouse: Wheelhouse, uv_cache: Path) -> None:
    """The half of step 3's "may" that holds however this bash was built.

    For an account whose shell is bash, `ssh host 'agentic-hil doctor'` runs
    the command in `bash -c` with SSH_CLIENT set and SHLVL unset. A bash built
    to read `~/.bashrc` for that shell reads the skeleton's, which returns
    before its end for a shell that is not interactive, and the line step 3
    appended is past that return; a bash built without it reads nothing. Either
    way the command is not found, and the full path step 3 names for such a
    shell is what calls it there.
    """
    home = tmp_path / "home"
    a_home("skeleton", home)
    skeleton_bashrc = (SKELETON / ".bashrc").read_text(encoding="utf-8")
    # The early return, read off this image rather than assumed of it.
    assert "*) return;;" in skeleton_bashrc, skeleton_bashrc
    user_bin = home / ".local" / "bin"

    installed = install_with_the_shell("bash", home, tmp_path, wheelhouse, uv_cache)

    transcript = f"{installed.stdout}{installed.stderr}"
    assert installed.returncode == 0, transcript
    assert "PATH: a non-interactive shell, such as ssh host 'agentic-hil doctor', a cron job or a CI step, may read none of these files;" in transcript, transcript
    assert f"call the command by its full path, {user_bin}/agentic-hil" in transcript, transcript
    bashrc = (home / ".bashrc").read_text(encoding="utf-8")
    assert bashrc.startswith(skeleton_bashrc), bashrc
    assert f'export PATH="{user_bin}:$PATH"' in bashrc[len(skeleton_bashrc) :].splitlines(), bashrc
    assert (user_bin / "agentic-hil").is_file(), transcript
    over_ssh, exchange = where_the_shell_finds_it("bash", home, **SSHD_VARIABLES)
    assert over_ssh == "", f"{exchange}\n{startup_files(home)}\n{transcript}"


def test_an_interactive_zsh_finds_the_command_and_zsh_c_does_not(tmp_path: Path, wheelhouse: Wheelhouse, uv_cache: Path) -> None:
    """The one file step 3 writes for zsh, and the two zsh shells it says do and do not read it.

    `zsh -i` is a new terminal window, and it has to find the copy this run
    installed, which is what step 3 says of new interactive zsh shells. `zsh -c`
    does not read `~/.zshrc`, and must find nothing for the sentence step 3
    prints about a non-interactive shell to hold.
    """
    home = tmp_path / "home"
    a_home("fresh", home)
    user_bin = home / ".local" / "bin"

    installed = install_with_the_shell("zsh", home, tmp_path, wheelhouse, uv_cache)

    transcript = f"{installed.stdout}{installed.stderr}"
    assert installed.returncode == 0, transcript
    assert f"PATH: added one line to {home / '.zshrc'}" in transcript, transcript
    assert "PATH: this file is read by new interactive zsh shells;" in transcript, transcript
    assert "PATH: a non-interactive shell, such as ssh host 'agentic-hil doctor', a cron job or a CI step, may not read this file;" in transcript, transcript
    copy = user_bin / "agentic-hil"
    assert copy.is_file(), transcript
    interactive, interactive_exchange = where_the_shell_finds_it("zsh", home, "-i")
    plain, plain_exchange = where_the_shell_finds_it("zsh", home)
    assert (interactive, plain) == (str(copy), ""), f"{interactive_exchange}\n{plain_exchange}\n{startup_files(home, '.zshrc')}\n{transcript}"
