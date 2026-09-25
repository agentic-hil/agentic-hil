"""Where the real bash finds the command after install.sh's step 3 (#548).

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


def startup_files(home: Path) -> str:
    """The account's startup files as the run left them, for a failure message."""
    shown = []
    for name in (".bashrc", ".bash_profile", ".bash_login", ".profile"):
        path = home / name
        shown.append(f"--- {name}\n{path.read_text(encoding='utf-8')}" if path.is_file() else f"--- {name}: absent")
    return "\n".join(shown)


def install_with_bash_as_the_shell(home: Path, tmp_path: Path, wheelhouse: Wheelhouse, uv_cache: Path) -> subprocess.CompletedProcess[str]:
    """install.sh with the real uv, `SHELL=/bin/bash`, and a PATH that does not carry `~/.local/bin`.

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
        SHELL="/bin/bash",
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


def where_bash_finds_it(home: Path, *options: str) -> tuple[str, str]:
    """What `command -v agentic-hil` answers in a new bash started with `options`, and the whole exchange.

    The environment is what a new session of the account starts from: its home
    and the system PATH, and nothing a startup file could have added.
    """
    bash = shutil.which("bash", path=STARTUP_PATH)
    assert bash is not None, STARTUP_PATH
    answered = subprocess.run(
        [bash, *options, "-c", PROBE],
        cwd=str(home),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": STARTUP_PATH},
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    found = [line[len("found=") :] for line in answered.stdout.splitlines() if line.startswith("found=")]
    exchange = f"bash {' '.join(options)} -c: exit {answered.returncode}\n{answered.stdout}{answered.stderr}"
    return (found[-1] if found else ""), exchange


@pytest.mark.parametrize("kind", ["fresh", "skeleton", "own-profile", "own-bash-profile", "own-bash-login"])
def test_a_login_shell_and_an_interactive_shell_find_the_command_and_bash_c_does_not(tmp_path: Path, wheelhouse: Wheelhouse, uv_cache: Path, kind: str) -> None:
    """Three new shells of the account after step 3, and where each one finds `agentic-hil`.

    `bash -l` is the login shell an ssh session or a console login starts, and
    `bash -i` is a new terminal window on a Linux desktop; both have to find the
    copy this run installed, which is what step 3 says of them. `bash -c` reads
    no startup file of the account's, and the sentence step 3 prints for it says
    so, which is true only while it finds nothing through them.

    Before #548 the fresh home and every home with a login file of the owner's
    own were red for `bash -l`. The skeleton's was green, because its `.profile`
    puts an existing `~/.local/bin` on PATH itself, and it stays here as the
    guard that the second file changes nothing a stock account already had.
    """
    home = tmp_path / "home"
    a_home(kind, home)
    user_bin = home / ".local" / "bin"

    installed = install_with_bash_as_the_shell(home, tmp_path, wheelhouse, uv_cache)

    transcript = f"{installed.stdout}{installed.stderr}"
    assert installed.returncode == 0, transcript
    assert f"PATH: agentic-hil landed in {user_bin}, which is not on your PATH" in transcript, transcript
    copy = user_bin / "agentic-hil"
    assert copy.is_file(), transcript
    login, login_exchange = where_bash_finds_it(home, "-l")
    interactive, interactive_exchange = where_bash_finds_it(home, "-i")
    plain, plain_exchange = where_bash_finds_it(home)
    assert (login, interactive, plain) == (str(copy), str(copy), ""), f"{login_exchange}\n{interactive_exchange}\n{plain_exchange}\n{startup_files(home)}\n{transcript}"
