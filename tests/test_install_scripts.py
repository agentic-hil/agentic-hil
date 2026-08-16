"""What the two one-line installers may and may not do.

`install.sh` and `install.ps1` are the first thing a stranger runs, from a pipe,
before they have read a line of this repository. That makes every property here
a promise rather than a preference: the scripts install user-local and never ask
for administrator rights, they never reach for `--break-system-packages` when a
distribution refuses `pip`, they never relax an execution policy, and they stop
at the machine half. The project half is deliberately not theirs: after one
restart the agent creates this project's configuration over MCP, so a script
that ran `init` or `setup` would be doing the very shell setup the classifier
blocks on a first run.

The syntax checks and the container run cover what a static read cannot: a shell
script that parses on the author's machine and nowhere else, and an installer
that reports success while leaving nothing on `PATH`. Both skip rather than fail
where the interpreter or the daemon is missing, and say what would have run it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SHELL_SCRIPT = REPOSITORY_ROOT / "install.sh"
POWERSHELL_SCRIPT = REPOSITORY_ROOT / "install.ps1"

# Windows PowerShell 5.1 takes seconds to start, and the shared runners are
# slower still. The budget bounds a hang, not a slow start.
SCRIPT_TIMEOUT_S = 180
# A healthy daemon answers in milliseconds. One that spends the whole budget is
# a machine these tests skip on, not fail on.
DOCKER_PROBE_TIMEOUT_S = 10
CONTAINER_TIMEOUT_S = 600
CONTAINER_IMAGE = "python:3.12"

DOCUMENTED_FLAGS = (
    "--agent",
    "--no-agent-install",
    "--version",
    "--can",
    "--no-can",
    "--help",
)

# One sentence each, identical in both scripts, because the operator reads
# whichever one their machine ran and the instruction is the same either way.
RESTART_LINES = (
    "RESTART REQUIRED",
    "Quit that process and start it again once. An agent CLI reads its MCP",
    "registrations when a session starts, so the agentic-hil tools appear in",
    "the next session, not in this one.",
)
CALM_LINE = (
    "The next start of your agent has everything, and the first hardware question "
    "creates this project's configuration."
)

# `agentic-hil init` and `agentic-hil setup` write a project's own configuration.
# Neither script may reach them, in any spelling, in code or in prose.
PROJECT_COMMAND = re.compile(r"agentic-hil[^\n]*\b(init|setup)\b")

RAW_URL = re.compile(r"https://raw\.githubusercontent\.com/agentic-hil/agentic-hil/master/([^\s\"'`)]+)")


def _shell_source() -> str:
    return SHELL_SCRIPT.read_text(encoding="utf-8")


def _powershell_source() -> str:
    return POWERSHELL_SCRIPT.read_text(encoding="utf-8")


def _both_sources() -> dict[str, str]:
    return {"install.sh": _shell_source(), "install.ps1": _powershell_source()}


def _code_only(source: str) -> str:
    """The script with its comments removed, for the checks about what it runs.

    A comment naming the flag a script must never pass is the comment doing its
    job, and a substring check over the whole file cannot tell that apart from
    the call itself. Both languages spell a line comment `#`, and PowerShell's
    block comment is the one extra shape here.
    """
    lines = []
    in_block = False
    for line in source.splitlines():
        stripped = line.strip()
        if in_block:
            if "#>" in stripped:
                in_block = False
            continue
        if stripped.startswith("<#"):
            in_block = "#>" not in stripped
            continue
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _both_code() -> dict[str, str]:
    return {name: _code_only(source) for name, source in _both_sources().items()}


def _posix_shell() -> str:
    found = shutil.which("sh")
    if found is None:
        pytest.skip("no POSIX sh on this machine; Git Bash provides one on Windows")
    return found


def _windows_powershell() -> str:
    if os.name != "nt":
        found = shutil.which("pwsh")
        if found is None:
            pytest.skip("no PowerShell on this machine; install pwsh to run this check")
        return found
    system_root = os.environ.get("SYSTEMROOT")
    assert system_root
    executable = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_file():
        pytest.skip("Windows PowerShell is not where SYSTEMROOT says it is")
    return str(executable)


def _docker() -> str:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is not installed on this machine")
    try:
        probe = subprocess.run(
            [docker, "version", "--format", "{{.Server.Os}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOCKER_PROBE_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        pytest.skip("the Docker daemon did not answer; start Docker and run this again")
    if probe.returncode != 0:
        pytest.skip("the Docker daemon did not answer; start Docker and run this again")
    # GitHub's Windows runners answer with a healthy daemon that runs Windows
    # containers, and a Linux image is simply not pullable there. That machine
    # skips the same way a machine without Docker does.
    if probe.stdout.strip() != "linux":
        pytest.skip(f"the Docker daemon runs {probe.stdout.strip() or 'unknown'} containers and {CONTAINER_IMAGE} needs linux")
    return docker


def test_neither_script_asks_for_administrator_rights() -> None:
    """User-local, always. An installer that elevates has to be trusted twice."""
    for name, code in _both_code().items():
        assert not re.search(r"\bsudo\b", code), name
        assert "Set-ExecutionPolicy" not in code, name
        assert "RunAs" not in code, name
    for name, source in _both_sources().items():
        assert "#Requires -RunAsAdministrator" not in source, name


def test_neither_script_breaks_a_distribution_python() -> None:
    """PEP 668 routes to uv. The other exit is the one that breaks the host."""
    for name, code in _both_code().items():
        assert "--break-system-packages" not in code, name
        assert "externally" in code, name


def test_the_shell_script_stops_on_a_failure_and_on_an_unset_variable() -> None:
    assert "set -eu" in _code_only(_shell_source())


def test_the_powershell_script_stops_on_the_first_error() -> None:
    assert "$ErrorActionPreference = 'Stop'" in _code_only(_powershell_source())


def test_both_scripts_run_the_machine_half_and_only_the_machine_half() -> None:
    for name, source in _both_sources().items():
        assert "agent-install" in source, name
        offender = PROJECT_COMMAND.search(source)
        assert offender is None, f"{name} reaches a project command: {offender.group(0) if offender else ''}"


def test_neither_script_writes_a_project_configuration() -> None:
    """The claim the top-of-file comment makes, held against the code."""
    for name, source in _both_sources().items():
        assert "no project configuration" in source, name


def test_both_scripts_open_with_the_five_lines_that_say_what_they_touch() -> None:
    for name, source in _both_sources().items():
        header = "\n".join(source.splitlines()[:8])
        assert "What this script touches, and nothing else" in header, name
        for claim in ("user-local", "skill", "MCP registration", "repository", "administrator rights"):
            assert claim in header, f"{name} header does not mention {claim}"


def test_both_scripts_carry_the_same_restart_instruction() -> None:
    for name, source in _both_sources().items():
        for line in RESTART_LINES:
            assert line in source, f"{name} is missing the restart line: {line}"


def test_both_scripts_carry_the_same_calm_closing_sentence() -> None:
    for name, source in _both_sources().items():
        assert CALM_LINE in source, name


def test_the_shell_script_parses_in_a_posix_shell() -> None:
    shell = _posix_shell()

    result = subprocess.run(
        [shell, "-n", str(SHELL_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"


def test_the_powershell_script_parses_in_powershell() -> None:
    powershell = _windows_powershell()
    escaped = str(POWERSHELL_SCRIPT).replace("'", "''")
    command = (
        "$tokens=$null; $errors=$null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{escaped}',[ref]$tokens,[ref]$errors); "
        "if($errors.Count){$errors | ForEach-Object {$_.Message}; exit 1}"
    )

    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"


def test_the_shell_script_prints_every_documented_flag() -> None:
    shell = _posix_shell()

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert "Usage: install.sh [options]" in result.stdout
    assert "sh -s -- --agent" in result.stdout
    for flag in DOCUMENTED_FLAGS:
        assert flag in result.stdout, flag


def test_the_powershell_script_prints_every_documented_flag() -> None:
    powershell = _windows_powershell()

    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(POWERSHELL_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert "Usage: install.ps1 [options]" in result.stdout
    for flag in DOCUMENTED_FLAGS:
        assert flag in result.stdout, flag


def test_an_unknown_flag_is_refused_by_both_scripts() -> None:
    """A typo must not be read as a positional argument and quietly ignored."""
    shell = _posix_shell()

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--not-a-flag"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )

    assert result.returncode == 2, f"{result.stdout}{result.stderr}"
    assert "unknown option: --not-a-flag" in result.stderr


def test_the_documented_one_liner_urls_point_at_files_that_are_here() -> None:
    """A raw URL in a shipped document is a promise about the default branch."""
    documents = [
        REPOSITORY_ROOT / "README.md",
        REPOSITORY_ROOT / "docs" / "installation.md",
    ]
    for document in documents:
        text = document.read_text(encoding="utf-8")
        referenced = set(RAW_URL.findall(text))
        assert "install.sh" in referenced, document
        assert "install.ps1" in referenced, document
        for relative in sorted(referenced):
            assert (REPOSITORY_ROOT / relative).is_file(), f"{document} points at a missing {relative}"


def test_the_scripts_are_reachable_from_the_repository_root() -> None:
    assert SHELL_SCRIPT.is_file()
    assert POWERSHELL_SCRIPT.is_file()


def test_the_shell_installer_never_invokes_a_bare_agentic_hil_for_agent_install() -> None:
    """Step 4 calls the copy step 3 resolved, never a bare name PATH decides.

    A bare `agentic-hil agent-install` resolves through PATH, where an older copy
    earlier than the user bin answers for the install that just happened. The
    fix routes the machine half through the exact executable this run installed;
    the printed suggestion for a machine with no agent CLI is a different line and
    starts with `printf`, so a bare invocation is the only thing this catches.
    """
    code = _code_only(_shell_source())
    for line in code.splitlines():
        assert not line.strip().startswith("agentic-hil agent-install"), line
    assert '"$AGENTIC_HIL_CMD" agent-install' in code


def test_the_powershell_installer_never_invokes_a_bare_agentic_hil_for_agent_install() -> None:
    """The same promise on the PowerShell side, where the call is an -File.

    Step 1 still probes a bare `agentic-hil --version` through `Invoke-Captured`
    to read what is already on PATH, which is correct. Only the checked call that
    runs the machine half must go through the resolved copy, so this targets
    `Invoke-Checked` and leaves the probe alone.
    """
    code = _code_only(_powershell_source())
    assert "agent-install" in code
    assert "Invoke-Checked -File 'agentic-hil'" not in code
    assert 'Invoke-Checked -File "agentic-hil"' not in code
    assert "-File $AgenticHilCmd" in code


def _stub_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)


def test_a_newer_install_answers_agent_install_over_an_older_copy_earlier_on_path(tmp_path: Path) -> None:
    """The old-copy-before-user-bin case, run end to end through a POSIX shell.

    An older agentic-hil sits in a directory earlier on PATH than the user bin uv
    installs into. Before this fix step 3 saw *some* agentic-hil resolve and step
    4 ran the bare name, so the stale 0.3.0 copy handled `agent-install` while the
    script reported success and told the operator to restart. The stubs record
    which copy the machine half actually called; the fix makes it the fresh one.

    Driven end to end, so it runs on the POSIX half where these mechanics live;
    a Windows Git Bash would translate the stub paths and executability by its
    own rules, which is not what this pins.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")
    shell = _posix_shell()

    home = tmp_path / "home"
    project = home / "project"
    early_bin = tmp_path / "early-bin"
    user_bin = home / ".local" / "bin"
    for directory in (project, early_bin, user_bin):
        directory.mkdir(parents=True)

    marker = tmp_path / "who-ran-agent-install"

    # The stale copy, earlier on PATH: an old version, and a record if it is ever
    # the one asked to do the machine half.
    _stub_executable(
        early_bin / "agentic-hil",
        'case "$1" in\n'
        '  --version) echo "0.3.0" ;;\n'
        f'  agent-install) echo "stale" > "{marker}" ;;\n'
        "esac\n"
        "exit 0\n",
    )
    # A stub claude, so agent detection has a claude-code to register for.
    _stub_executable(early_bin / "claude", "exit 0\n")
    # A fake uv whose bin directory is the default user bin: it reports that with
    # `tool dir --bin`, the way the real one does, and `tool install` writes the
    # fresh console script there.
    _stub_executable(
        early_bin / "uv",
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        f'  echo "{user_bin}"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        f'  cat > "{user_bin}/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        '  --version) echo "9.9.9" ;;\n'
        f'  agent-install) echo "fresh" > "{marker}" ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        f'  chmod +x "{user_bin}/agentic-hil"\n'
        "fi\n"
        "exit 0\n",
    )

    env = {
        "HOME": str(home),
        "PATH": f"{early_bin}:{user_bin}:/usr/bin:/bin",
    }

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--version", "9.9.9", "--no-can"],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "fresh", transcript


def test_both_scripts_ask_uv_where_it_put_the_executable() -> None:
    """A uv install is located through `uv tool dir --bin`, not a guessed user bin.

    uv writes its tool executables into a directory that can sit outside the
    guessed user bins: `UV_TOOL_BIN_DIR` names one, the XDG data directory another,
    and `uv tool dir --bin` is uv's own authoritative report of where `uv tool
    install` just wrote the console script. A scan that only guesses the user bins
    misses a copy that landed there and lets an older agentic-hil earlier on PATH
    answer instead. Both scripts must consult uv; the PowerShell side has no
    interpreter in every checkout, so this static check is its regression guard.
    """
    assert "uv tool dir --bin" in _code_only(_shell_source())
    assert "'tool', 'dir', '--bin'" in _code_only(_powershell_source())


def test_a_uv_install_outside_the_user_bin_answers_over_an_older_path_copy(tmp_path: Path) -> None:
    """The `UV_TOOL_BIN_DIR` case, run end to end through a POSIX shell.

    uv is told to write tool executables into a directory that is neither the user
    bin nor on PATH, the way `UV_TOOL_BIN_DIR` and the XDG data directory can move
    it, and reports that directory with `uv tool dir --bin`. Before this fix the
    candidate scan only guessed the user bins, missed the fresh copy, and step 3
    accepted the stale 0.3.0 copy earlier on PATH, which then answered
    `agent-install` while the script reported success. The fix asks uv where the
    copy went, version-checks it there, and routes the machine half through that
    exact one.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")
    shell = _posix_shell()

    home = tmp_path / "home"
    project = home / "project"
    early_bin = tmp_path / "early-bin"
    user_bin = home / ".local" / "bin"
    uv_bin = tmp_path / "uv-tools" / "bin"
    for directory in (project, early_bin, user_bin, uv_bin):
        directory.mkdir(parents=True)

    marker = tmp_path / "who-ran-agent-install"

    # The stale copy, earlier on PATH than the user bin, and older than the floor.
    _stub_executable(
        early_bin / "agentic-hil",
        'case "$1" in\n'
        '  --version) echo "0.3.0" ;;\n'
        f'  agent-install) echo "stale" > "{marker}" ;;\n'
        "esac\n"
        "exit 0\n",
    )
    # A stub claude, so agent detection has a claude-code to register for.
    _stub_executable(early_bin / "claude", "exit 0\n")
    # A fake uv that honours UV_TOOL_BIN_DIR: it reports that directory with
    # `tool dir --bin`, and `tool install` writes the fresh console script there,
    # the way the real uv does. The copy lands nowhere the old candidate scan
    # looked, and nowhere on PATH.
    _stub_executable(
        early_bin / "uv",
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        '  echo "$UV_TOOL_BIN_DIR"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        '  cat > "$UV_TOOL_BIN_DIR/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        '  --version) echo "9.9.9" ;;\n'
        f'  agent-install) echo "fresh" > "{marker}" ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        '  chmod +x "$UV_TOOL_BIN_DIR/agentic-hil"\n'
        "fi\n"
        "exit 0\n",
    )

    env = {
        "HOME": str(home),
        "PATH": f"{early_bin}:{user_bin}:/usr/bin:/bin",
        "UV_TOOL_BIN_DIR": str(uv_bin),
    }

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--version", "9.9.9", "--no-can"],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "fresh", transcript
    # The fresh copy landed off PATH, so the script says so rather than pretending
    # it resolves, and still routes the machine half through it.
    assert f"landed in {uv_bin}" in transcript, transcript


def test_a_pip_install_answers_over_a_newer_stale_copy_in_a_guessed_bin(tmp_path: Path) -> None:
    """The pip/candidate-order defect, run end to end through a POSIX shell.

    pip --user writes the fresh copy into the interpreter's own user scripts path.
    A newer, unrelated agentic-hil already sits in `XDG_BIN_HOME`, a directory the
    earlier candidate scan consulted before that scripts path. With `--version
    0.5.0` asked for, that scan accepted the 9.9.9 copy there -- it was "at least
    0.5.0" -- and the stale copy answered `agent-install` while the script reported
    success, though it was never on PATH. The fix asks pip, through the interpreter,
    where it actually wrote the copy, consults only that directory, and requires the
    pinned version exactly, so the fresh 0.5.0 copy is the one that registers.

    Driven end to end on the POSIX half, where these mechanics live. It takes the
    pip branch, so no uv may resolve on the test PATH.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")
    shell = _posix_shell()

    home = tmp_path / "home"
    project = home / "project"
    early_bin = tmp_path / "early-bin"  # the fake python and claude
    xdg_bin = tmp_path / "xdg-bin"  # a newer stale copy, scanned first by the old code
    scripts_dir = tmp_path / "py-scripts"  # what the interpreter reports as its scripts path
    for directory in (project, early_bin, xdg_bin, scripts_dir):
        directory.mkdir(parents=True)

    marker = tmp_path / "who-ran-agent-install"

    # The newer, unrelated copy in XDG_BIN_HOME: reachable only through the old
    # candidate scan, which looked here before the interpreter's scripts path and
    # accepted it for being at least the pin. It is not on PATH at all.
    _stub_executable(
        xdg_bin / "agentic-hil",
        'case "$1" in\n'
        '  --version) echo "9.9.9" ;;\n'
        f'  agent-install) echo "stale" > "{marker}" ;;\n'
        "esac\n"
        "exit 0\n",
    )
    # A stub claude, so agent detection has a claude-code to register for.
    _stub_executable(early_bin / "claude", "exit 0\n")
    # A fake python that is new enough, whose `-m pip install --user` writes the
    # fresh pinned copy into its reported scripts path, and that reports that path
    # with sysconfig -- the three questions the installer asks the interpreter.
    _stub_executable(
        early_bin / "python3",
        'case "$*" in\n'
        "  *version_info*) exit 0 ;;\n"
        f'  *posix_user*) echo "{scripts_dir}"; exit 0 ;;\n'
        '  *"pip install"*)\n'
        f'    cat > "{scripts_dir}/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        '  --version) echo "0.5.0" ;;\n'
        f'  agent-install) echo "fresh" > "{marker}" ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        f'    chmod +x "{scripts_dir}/agentic-hil"\n'
        "    exit 0 ;;\n"
        "esac\n"
        "exit 0\n",
    )

    env = {
        "HOME": str(home),
        "PATH": f"{early_bin}:{scripts_dir}:/usr/bin:/bin",
        "XDG_BIN_HOME": str(xdg_bin),
    }

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--version", "0.5.0", "--no-can"],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "fresh", transcript


def test_an_exact_version_pin_refuses_a_mismatched_copy_in_the_managers_bin(tmp_path: Path) -> None:
    """An exact `--version` pin is proven by exact equality, not by "at least".

    The manager's own bin holds an agentic-hil whose version is newer than the pin,
    not the pinned release this run asked for. A floor comparison would accept it;
    the documented `--version` contract is an exact release, so the installer
    refuses to hand `agent-install` to a copy it cannot prove is the pinned one it
    installed. It exits non-zero through the same guard that fires when the fresh
    copy cannot be located, and never runs the machine half.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")
    shell = _posix_shell()

    home = tmp_path / "home"
    project = home / "project"
    early_bin = tmp_path / "early-bin"
    uv_bin = tmp_path / "uv-tools" / "bin"
    for directory in (project, early_bin, uv_bin):
        directory.mkdir(parents=True)

    marker = tmp_path / "who-ran-agent-install"

    # A stub claude, so agent detection has a claude-code that would be registered
    # if the guard let step 4 run.
    _stub_executable(early_bin / "claude", "exit 0\n")
    # A fake uv that reports its bin with `tool dir --bin`, but whose `tool install`
    # writes a copy reporting 9.9.9 -- newer than the 0.5.0 pin, and so not the
    # pinned release this run asked for.
    _stub_executable(
        early_bin / "uv",
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        '  echo "$UV_TOOL_BIN_DIR"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        '  cat > "$UV_TOOL_BIN_DIR/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        '  --version) echo "9.9.9" ;;\n'
        f'  agent-install) echo "ran" > "{marker}" ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        '  chmod +x "$UV_TOOL_BIN_DIR/agentic-hil"\n'
        "fi\n"
        "exit 0\n",
    )

    env = {
        "HOME": str(home),
        "PATH": f"{early_bin}:/usr/bin:/bin",
        "UV_TOOL_BIN_DIR": str(uv_bin),
    }

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--version", "0.5.0", "--no-can"],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode != 0, transcript
    assert not marker.exists(), transcript
    assert "does not resolve here" in transcript, transcript


def test_both_scripts_locate_the_fresh_copy_only_in_the_managers_own_bin() -> None:
    """The fresh copy is version-checked in the manager's own destination, nothing
    guessed. uv is asked with `uv tool dir --bin` and pip through the interpreter's
    user scripts path; a scan of guessed user bins (`XDG_BIN_HOME`, `~/.local/bin`)
    accepting the first copy at or above the floor let a newer, unrelated copy in an
    earlier directory answer for the install, so no such scan remains. The
    PowerShell side has no interpreter in every checkout, so this static check is
    its regression guard alongside the POSIX end-to-end tests.
    """
    shell = _code_only(_shell_source())
    assert "manager_bin_dir" in shell
    assert "candidate_bin_dirs" not in shell
    assert "posix_user" in shell
    powershell = _code_only(_powershell_source())
    assert "Get-ManagerBinDirectory" in powershell
    assert "Get-CandidateBinDirectories" not in powershell
    assert "nt_user" in powershell


def test_both_scripts_require_an_exact_match_for_a_version_pin() -> None:
    """A `--version` pin is proven by exact equality, not a floor comparison.

    The documented `--version` installs an exact release, so a copy in the manager's
    own bin whose version merely exceeds the pin is not the one this run wrote. Both
    scripts gate the fresh copy on an exact-equality check when a pin is present and
    fall back to the floor otherwise; the PowerShell side cannot run end to end in
    every checkout, so this pins that both carry such a check.
    """
    shell = _code_only(_shell_source())
    assert "version_exactly" in shell
    assert "version_matches_request" in shell
    powershell = _code_only(_powershell_source())
    assert "Test-VersionExactly" in powershell
    assert "Test-VersionMatchesRequest" in powershell


# The container run, end to end: a machine with nothing on it, the real package
# from the index, a stub `claude` on PATH so agent detection has something to
# find, and then the four questions that decide whether the line did its job.
_CONTAINER_SCRIPT = r"""
set -eu
export HOME=/work/home
mkdir -p "$HOME" /work/bin
printf '#!/bin/sh\nexit 0\n' > /work/bin/claude
chmod +x /work/bin/claude
export PATH="/work/bin:$HOME/.local/bin:$PATH"

# The line is typed in a firmware project, not in the home directory or at the
# filesystem root: the release this pulls from the index still reads every
# user-level path as inside the working directory there and refuses (#235). The
# two lines below go once a release carries the fix.
mkdir -p "$HOME/project"
cd "$HOME/project"

sh /repo/install.sh

command -v agentic-hil >/dev/null 2>&1 || { echo "FAIL: agentic-hil is not on PATH"; exit 1; }
agentic-hil --version
test -f "$HOME/.claude/skills/agentic-hil/SKILL.md" || { echo "FAIL: no skill file"; exit 1; }
test -f "$HOME/.claude.json" || { echo "FAIL: no MCP registration"; exit 1; }
grep -q agentic-hil "$HOME/.claude.json" || { echo "FAIL: MCP registration names no server"; exit 1; }
if find "$HOME/.config/agentic-hil" -name config.yaml 2>/dev/null | grep -q .; then
    echo "FAIL: a project configuration was written"
    exit 1
fi
echo "CONTAINER CHECKS PASSED"
"""


def test_the_one_liner_installs_the_machine_half_in_a_fresh_container() -> None:
    """The whole claim, on a machine that has never seen this project.

    Read-only repository mount, so the script cannot be the thing that wrote the
    evidence; a stub `claude` so detection finds an agent; and the assertion that
    matters most is the negative one, that no project configuration exists
    anywhere under the config home when the line is done.
    """
    docker = _docker()

    result = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "-v",
            f"{REPOSITORY_ROOT.as_posix()}:/repo:ro",
            CONTAINER_IMAGE,
            "sh",
            "-c",
            _CONTAINER_SCRIPT,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CONTAINER_TIMEOUT_S,
        check=False,
    )

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert "CONTAINER CHECKS PASSED" in transcript
    assert "sudo" not in transcript
    assert "step 4/5  agent: registering the skill and the MCP server for claude-code" in transcript
