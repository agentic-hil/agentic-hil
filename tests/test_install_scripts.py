"""What the two one-line installers may and may not do.

`install.sh` and `install.ps1` are the first thing a stranger runs, from a pipe,
before they have read a line of this repository. That makes every property here
a promise rather than a preference: the scripts install user-local and never ask
for administrator rights, they never reach for `--break-system-packages` when a
distribution refuses `pip`, they never relax an execution policy, they execute
nothing they have not first checked against a hash they carry, and they stop at
the machine half. The project half is deliberately not theirs: after one restart
the agent creates this project's configuration over MCP, so a script that ran
`init` or `setup` would be doing the very shell setup the classifier blocks on a
first run.

The syntax checks and the container run cover what a static read cannot: a shell
script that parses on the author's machine and nowhere else, and an installer
that reports success while leaving nothing on `PATH`. Both skip rather than fail
where the interpreter or the daemon is missing, and say what would have run it.
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
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
    "--system-certs",
    "--no-system-certs",
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
# The other closing sentence, for the run that replaced an installation instead
# of creating one. The calm line above is written for somebody meeting this tool
# for the first time, and it was printed unchanged after a refresh from one
# release to the next, on a host that already had four project configurations.
REFRESH_LINE = (
    "This installation was refreshed in place, and your project configurations were not touched. Any agentic-hil "
    "MCP server still running keeps answering with the release it started with, so restart the agent CLIs that "
    "started one to pick this installation up."
)
# And the third and fourth, for the two runs that kept what was already here. The
# refresh sentence was printed on both: a run that had just said "nothing to
# install, the development installation stays as it is" closed on having
# refreshed this installation in place, and so did a run that met `Nothing to
# upgrade`. Measured on a bench.
KEPT_DEVELOPMENT_LINE = (
    "The development installation already on this PATH was kept and nothing was installed over it, and your "
    "project configurations were not touched. Nothing moved to a newer release, so no agentic-hil MCP server is "
    "behind this installation and none of them needs restarting on account of this run."
)
# The version this one names comes out of the run, so the shared text is the rest
# of the sentence. It covers the anchor run too, where the same release is written
# again over a copy that had stopped working: the files moved, the release did
# not, and what a running server is or is not behind is the question this
# sentence answers.
KEPT_CURRENT_TAIL = (
    "the release it was already on, and your project configurations were not touched. Nothing moved to a newer "
    "release, so no agentic-hil MCP server is behind this installation and none of them needs restarting on "
    "account of this run."
)
# The plural form of the restart block. Step 5 names every running agent CLI
# rather than the first one found: a warning that names one is read as clearing
# the others, and the operator who had two open restarted only one.
MULTI_RESTART_LINES = (
    "RESTART REQUIRED: these agent CLIs are running right now:",
    "Quit each process and start it again once. An agent CLI reads its MCP",
)

# `agentic-hil init` and `agentic-hil setup` write a project's own configuration.
# Neither script may reach them, in any spelling, in code or in prose.
PROJECT_COMMAND = re.compile(r"agentic-hil[^\n]*\b(init|setup)\b")

# The three hosts a shipped document may take an installer from: the raw file on
# the default branch, the short mirror that serves the same bytes, and the
# release assets. Each resolves to a name this repository must actually hold.
INSTALLER_URL = re.compile(
    r"https://(?:raw\.githubusercontent\.com/agentic-hil/agentic-hil/master"
    r"|agentic-hil\.github\.io"
    r"|github\.com/agentic-hil/agentic-hil/releases/latest/download)"
    r"/([^\s\"'`)|]+)"
)

# The moving Astral bootstrap URL, in either spelling. It serves whatever uv is
# current at the second it is asked, so a script that fetches it executes bytes
# nobody here has read. Neither installer may name it in code again.
UNPINNED_UV_INSTALLER = re.compile(r"https://astral\.sh/uv/install\.(?:sh|ps1)")

# The pinned shape: a version segment between `uv` and the file name, spelled as
# a literal release or as the constant each script keeps for it. The segment is
# not optional, which is what makes the bare URL above unreachable through this
# pattern.
PINNED_UV_INSTALLER = re.compile(r"https://astral\.sh/uv/(?:\d+\.\d+\.\d+|\$\{?[A-Za-z_]\w*\}?)/install\.(?:sh|ps1)")

SHA256_HEX = r"[0-9a-f]{64}"
SHELL_UV_VERSION = re.compile(r'UV_INSTALLER_VERSION="(\d+\.\d+\.\d+)"')
SHELL_UV_SHA256 = re.compile(rf'UV_INSTALLER_SHA256="({SHA256_HEX})"')
SHELL_UV_COMPARISON = re.compile(r'"\$found_hash"\s*!=\s*"\$UV_INSTALLER_SHA256"')
SHELL_UV_EXECUTION = re.compile(r'\bsh "\$installer_path"')
POWERSHELL_UV_VERSION = re.compile(r"\$UvInstallerVersion = '(\d+\.\d+\.\d+)'")
POWERSHELL_UV_SHA256 = re.compile(rf"\$UvInstallerSha256 = '({SHA256_HEX})'")
POWERSHELL_UV_COMPARISON = re.compile(r"\$foundHash\s*-ne\s*\$UvInstallerSha256")
POWERSHELL_UV_EXECUTION = re.compile(r"Invoke-Expression \(\[Text\.Encoding\]::UTF8\.GetString\(\$bytes\)\)")

# One line per agent on a successful registration, identical in both scripts,
# because the operator reads whichever one their machine ran.
REGISTERED_LINE = "registered (skill and MCP server, restart pending)"


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


def test_neither_script_fetches_the_uv_bootstrap_unpinned() -> None:
    """The Pinned-Dependencies finding, held shut from both sides.

    Both installers can bootstrap Astral's uv on a machine with neither uv nor a
    new-enough Python, and both used to take it from the moving
    `https://astral.sh/uv/install.sh` and hand it straight to an interpreter.
    That was the one unpinned hop in a chain where this script is published with
    its own checksum beside it and the package comes hash-checked from PyPI. The
    versioned URL is now the only shape either script may name: the bare one can
    never come back, because the version segment is mandatory in the pattern that
    has to match and absent from the pattern that must not.
    """
    for name, code in _both_code().items():
        offender = UNPINNED_UV_INSTALLER.search(code)
        assert offender is None, f"{name} fetches {offender.group(0) if offender else ''} unpinned"
        assert PINNED_UV_INSTALLER.search(code) is not None, f"{name} names no versioned astral.sh installer URL"


def test_neither_script_pipes_an_astral_download_into_an_interpreter() -> None:
    """Fetched bytes reach an interpreter through a check, never through a pipe.

    The documented one-liner for *this* project is still a pipe, and correctly so:
    it is published with a `.sha256` beside it and the usage text keeps showing it.
    What may not survive is a line that both names `astral.sh` and feeds what it
    fetched into `sh`, `iex` or `Invoke-Expression`, because that is the shape
    that leaves no room for a hash to be checked in between.
    """
    for name, code in _both_code().items():
        for line in code.splitlines():
            if "astral.sh" not in line:
                continue
            lowered = line.lower()
            assert "| sh" not in lowered, f"{name} pipes an astral.sh download into sh: {line.strip()}"
            assert "| iex" not in lowered, f"{name} pipes an astral.sh download into iex: {line.strip()}"
            assert "invoke-expression" not in lowered, f"{name} expands an astral.sh download inline: {line.strip()}"


def test_both_scripts_check_the_pinned_uv_installer_before_they_run_it() -> None:
    """A hash the script carries, compared before anything is executed.

    The constant is the whole mechanism: a download that is not those bytes is not
    the pinned release, whatever the URL said, and it is refused rather than run.
    The order matters as much as the presence, so the comparison is required to sit
    ahead of the execution in the source of both scripts. The PowerShell side has no
    interpreter in every checkout, so this static check is its regression guard.
    """
    shell = _code_only(_shell_source())
    assert SHELL_UV_SHA256.search(shell) is not None, "install.sh carries no pinned uv installer digest"
    comparison = SHELL_UV_COMPARISON.search(shell)
    execution = SHELL_UV_EXECUTION.search(shell)
    assert comparison is not None, "install.sh never compares the download against its pinned digest"
    assert execution is not None, "install.sh no longer runs the downloaded installer from a file"
    assert comparison.start() < execution.start(), "install.sh runs the uv installer before checking it"

    powershell = _code_only(_powershell_source())
    assert POWERSHELL_UV_SHA256.search(powershell) is not None, "install.ps1 carries no pinned uv installer digest"
    comparison = POWERSHELL_UV_COMPARISON.search(powershell)
    execution = POWERSHELL_UV_EXECUTION.search(powershell)
    assert comparison is not None, "install.ps1 never compares the download against its pinned digest"
    assert execution is not None, "install.ps1 no longer expands the downloaded installer from the bytes it hashed"
    assert comparison.start() < execution.start(), "install.ps1 runs the uv installer before checking it"


def test_both_scripts_pin_the_same_uv_release() -> None:
    """Four constants, one release. A half-done bump is refused here, not on a bench.

    `install.sh` and `install.ps1` fetch two different files from the same uv
    release, so their digests differ and their versions must not. Bumping one
    script and forgetting the other leaves two machines installing two different
    uv versions from one commit, which no operator would ever see reported.
    """
    shell_version = SHELL_UV_VERSION.search(_code_only(_shell_source()))
    powershell_version = POWERSHELL_UV_VERSION.search(_code_only(_powershell_source()))
    assert shell_version is not None, "install.sh names no pinned uv version"
    assert powershell_version is not None, "install.ps1 names no pinned uv version"
    assert shell_version.group(1) == powershell_version.group(1), (
        f"install.sh pins uv {shell_version.group(1)} and install.ps1 pins uv {powershell_version.group(1)}"
    )


def test_a_uv_digest_mismatch_names_both_hashes_and_says_the_pin_may_be_stale() -> None:
    """The abort an operator has to act on, so it has to say what it saw.

    A mismatch is either a stale pin or a substituted download, and the operator is
    the only one who can tell those apart. Printing the digest expected, the digest
    found, and the sentence that the pin may be stale is what makes that a decision
    rather than a mystery, and the fix is a release chore documented in
    docs/release-strategy.md rather than anything the install may decide by itself.
    """
    for name, code in _both_code().items():
        assert "does not match its recorded hash" in code, name
        assert "the pin in this script may be stale" in code, name
    shell = _code_only(_shell_source())
    assert "expected %s" in shell
    assert "found    %s" in shell
    powershell = _code_only(_powershell_source())
    assert "expected $UvInstallerSha256" in powershell
    assert "found    $foundHash" in powershell


def test_the_pin_bump_is_written_down_as_a_release_chore() -> None:
    """The pin ages on purpose, so the place it is brought forward is the release."""
    strategy = (REPOSITORY_ROOT / "docs" / "release-strategy.md").read_text(encoding="utf-8")

    assert "UV_INSTALLER_VERSION" in strategy
    assert "UV_INSTALLER_SHA256" in strategy
    assert "$UvInstallerVersion" in strategy
    assert "$UvInstallerSha256" in strategy


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


def test_both_scripts_name_every_running_agent_cli_not_the_first() -> None:
    """Two open agent CLIs mean two restarts, and the block says so.

    The first shape of step 5 stopped at the first running CLI it found, so an
    operator with claude and codex both open was told about claude and read the
    silence about codex as codex being fine. Measured on a real install."""
    for name, source in _both_sources().items():
        for line in MULTI_RESTART_LINES:
            assert line in source, f"{name} is missing the plural restart line: {line}"


def test_both_scripts_carry_the_same_calm_closing_sentence() -> None:
    for name, source in _both_sources().items():
        assert CALM_LINE in source, name


def test_both_scripts_carry_the_same_closing_sentence_for_a_refresh() -> None:
    """Four closing sentences, one per outcome, identical in both scripts.

    The calm line is an answer about what happens next, and after a refresh
    there is a different thing to say: the installation on disk moved, and every
    server already running has not. Each script decides between them on the same
    facts, and the first-install flag is its own rather than the install mode's,
    because the development branch installs nothing at all and is still a machine
    that already has this tool.

    A run that kept what was here says so, and says which kind of keeping it was.
    The refresh sentence stood for every run that was not a first install, so a
    run that had just printed "nothing to install, the development installation
    stays as it is" closed on having refreshed this installation in place.
    """
    for name, source in _both_sources().items():
        assert REFRESH_LINE in source, name
        assert KEPT_DEVELOPMENT_LINE in source, name
        assert KEPT_CURRENT_TAIL in source, name
    shell = SHELL_SCRIPT.read_text(encoding="utf-8")
    powershell = POWERSHELL_SCRIPT.read_text(encoding="utf-8")
    assert 'FIRST_INSTALL=1\n    step 1 "probe: no agentic-hil on this PATH' in shell
    assert 'if [ "$FIRST_INSTALL" -eq 1 ]; then' in shell
    assert 'elif [ "$NEEDS_PACKAGE" -eq 0 ]; then' in shell
    assert 'elif [ -n "$RESOLVED_VERSION" ] && [ "$RESOLVED_VERSION" = "${installed:-}" ]; then' in shell
    assert 'RESOLVED_VERSION=$("$AGENTIC_HIL_CMD" --version 2>/dev/null) || RESOLVED_VERSION=""' in shell
    assert "$FirstInstall = $true\n    Write-Step 1 'probe: no agentic-hil on this PATH" in powershell
    assert "if ($FirstInstall) {" in powershell
    assert "} elseif (-not $needsPackage) {" in powershell
    assert "} elseif ($ResolvedVersion -and $ResolvedVersion -eq $installed) {" in powershell


def _closing_sentence_run(tmp_path: Path, *, installed_version: str, manager_version: str | None) -> str:
    """One shell run over a machine that already has `installed_version`.

    `manager_version` is what the fresh copy in the manager's own bin answers, or
    None for a run where step 2 installs nothing at all, which is the development
    branch. Everything else is the ordinary stub bench the other flow tests use.
    """
    shell = _posix_shell()
    home = tmp_path / "home"
    project = home / "project"
    user_bin = home / ".local" / "bin"
    uv_bin = tmp_path / "uv-tools" / "bin"
    tools = tmp_path / "tools"
    for directory in (project, user_bin, uv_bin, tools):
        directory.mkdir(parents=True)

    answer = 'case "$1" in\n  --version) echo "%s" ;;\n  agent-install) printf \'{\\n  "ok": true\\n}\\n\' ;;\nesac\nexit 0\n'
    _stub_executable(user_bin / "agentic-hil", answer % installed_version)
    _stub_executable(tools / "claude", "exit 0\n")
    # The heredoc is unquoted, so every `$` the stub body carries has to reach
    # the written file escaped or the outer shell expands it while writing.
    escaped = (answer % manager_version).replace("$", "\\$") if manager_version is not None else ""
    writes = (
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        '  cat > "$UV_TOOL_BIN_DIR/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        f"{escaped}"
        "STUB\n"
        '  chmod +x "$UV_TOOL_BIN_DIR/agentic-hil"\n'
        "fi\n"
        if manager_version is not None
        else ""
    )
    _stub_executable(
        tools / "uv",
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n  echo "$UV_TOOL_BIN_DIR"\n  exit 0\nfi\n' + writes + "exit 0\n",
    )

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--no-can"],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"HOME": str(home), "PATH": f"{tools}:{user_bin}:/usr/bin:/bin", "UV_TOOL_BIN_DIR": str(uv_bin)},
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )
    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    return transcript


def test_a_run_that_kept_a_development_installation_does_not_claim_a_refresh(tmp_path: Path) -> None:
    """Measured on a bench: two sentences that contradict each other, in one run.

    `sh install.sh` with a development installation on this PATH printed
    "package: nothing to install, the development installation stays as it is"
    and closed on "This installation was refreshed in place". The closing
    sentence follows what step 2 did.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    transcript = _closing_sentence_run(tmp_path, installed_version="0.99.0.dev3", manager_version=None)

    assert "nothing to install, the development installation stays as it is" in transcript, transcript
    assert KEPT_DEVELOPMENT_LINE in transcript, transcript
    assert REFRESH_LINE not in transcript, transcript
    assert CALM_LINE not in transcript, transcript


def test_a_run_that_moved_nothing_says_the_installation_was_kept_at_its_version(tmp_path: Path) -> None:
    """The same on the other keeping run: the manager had nothing to upgrade.

    Step 2 asked the manager to reinstall and the version did not move, so
    nothing on disk is a release ahead of what any running server imported. The
    closing sentence said the installation had been refreshed in place.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    transcript = _closing_sentence_run(tmp_path, installed_version="99.0.0", manager_version="99.0.0")

    assert f"This installation stayed at 99.0.0, {KEPT_CURRENT_TAIL}" in transcript, transcript
    assert REFRESH_LINE not in transcript, transcript


def test_a_run_that_moved_the_version_still_closes_on_the_refresh_sentence(tmp_path: Path) -> None:
    """The neighbouring behaviour: a version that moved is a refresh and says so."""
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    transcript = _closing_sentence_run(tmp_path, installed_version="0.11.0", manager_version="99.0.0")

    assert REFRESH_LINE in transcript, transcript
    assert KEPT_DEVELOPMENT_LINE not in transcript, transcript
    assert KEPT_CURRENT_TAIL not in transcript, transcript


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
    """An installer URL in a shipped document is a promise this repository keeps.

    Whichever host a document names, the raw default branch, the mirror that
    serves the same bytes, or a release asset, the name resolves to a file that
    exists here: the mirror copies these files verbatim, and a release asset is
    uploaded from the tagged tree, so a `.sha256` suffix vouches for the file it
    is named after.
    """
    documents = [
        REPOSITORY_ROOT / "README.md",
        REPOSITORY_ROOT / "docs" / "installation.md",
    ]
    for document in documents:
        text = document.read_text(encoding="utf-8")
        referenced = set(INSTALLER_URL.findall(text))
        assert "install.sh" in referenced, document
        assert "install.ps1" in referenced, document
        for relative in sorted(referenced):
            name = relative.removesuffix(".sha256")
            assert (REPOSITORY_ROOT / name).is_file(), f"{document} points at a missing {name}"


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
    to read what is already on PATH, which is correct. Only the call that runs the
    machine half must go through the resolved copy. That call is now a capture
    rather than a checked run, because step 4 reports a result instead of the
    document, so both spellings are named here and the probe is left alone.
    """
    code = _code_only(_powershell_source())
    assert "agent-install" in code
    for invocation in ("Invoke-Checked", "Invoke-Captured"):
        assert f"{invocation} -File 'agentic-hil' -Arguments @('agent-install'" not in code
        assert f'{invocation} -File "agentic-hil" -Arguments @(\'agent-install\'' not in code
    assert "-File $AgenticHilCmd" in code


def test_step_four_reports_a_result_and_does_not_stream_the_agent_install_report() -> None:
    """One line per agent on success, the whole report only when it is the diagnosis.

    `agent-install` answers with a report of every path it touched. Streamed, a
    machine with three agent CLIs turned a successful install into roughly a
    hundred and fifty lines of detail inside a five-step transcript. Captured,
    the operator gets one line per agent and still gets the report whole on a
    failure, where it is the only thing that says which half broke.

    The verdict is the exit status and nothing else. Both scripts used to also
    match the top-level `ok` at its own indentation, which was a check on a
    document; the report is prose now, addressed to the operator who is going to
    read it, and matching text in prose would be weaker than the status it was
    doubling.
    """
    shell = _code_only(_shell_source())
    assert "register_agent" in shell
    assert f'"agent: $registering_agent {REGISTERED_LINE}"' in shell
    assert "agent_install_report=$(" in shell, "install.sh streams the report instead of capturing it"
    assert '"$agent_install_report" >&2' in shell, "install.sh does not print the report on a failure"
    assert '"ok": true' not in shell, "install.sh still matches a document it is no longer handed"

    powershell = _code_only(_powershell_source())
    assert "function Register-Agent" in powershell
    assert f'"agent: $AgentId {REGISTERED_LINE}"' in powershell
    assert "Invoke-Captured -File $AgenticHilCmd" in powershell, "install.ps1 streams the report instead of capturing it"
    assert "Invoke-Checked -File $AgenticHilCmd" not in powershell
    assert '"ok": true' not in powershell, "install.ps1 still matches a document it is no longer handed"


def test_neither_script_asks_agent_install_for_a_rendering_it_gets_anyway() -> None:
    """Rendering is the default, so the frontend that shows a person says nothing.

    This was a flag for a while, on the reasoning that capturing the output is
    indistinguishable from a machine reading it. The reasoning held and the
    conclusion was backwards: a wrapper that shows a person what came back is
    what almost every caller is, so the rendering is the default and `--json` is
    what the few that parse ask for. Neither script parses.
    """
    for name, code in _both_code().items():
        assert "--human" not in code, f"{name} asks for what it is given"
        assert "--json" not in code, f"{name} asks for a document it does not read"


def test_the_powershell_capture_unwraps_a_stderr_error_record() -> None:
    """A successful command's stderr is a line, not an error block.

    Windows PowerShell 5.1 wraps every native stderr line in an ErrorRecord, and
    `2>&1 | Out-String` renders the first of those the way it renders a failure:
    the line, then the source position, then a CategoryInfo block naming
    NativeCommandError. uv reports its progress on stderr, so a completely
    successful `uv tool install` printed what read like a crash. Taking the message
    off the record before Out-String sees it is the fix, and the raw form is what
    must not come back.
    """
    code = _code_only(_powershell_source())
    assert "[System.Management.Automation.ErrorRecord]" in code
    assert "$_.Exception.Message" in code
    assert "2>&1 | Out-String" not in code, "install.ps1 renders error records into captured output again"


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
        f'  agent-install) echo "stale" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
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
        f'  agent-install) echo "fresh" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
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
        f'  agent-install) echo "stale" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
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
        f'  agent-install) echo "fresh" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
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
        f'  agent-install) echo "stale" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
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
        f'  agent-install) echo "fresh" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
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


def _machine_whose_only_python_is(tmp_path: Path, python_body: str) -> tuple[dict[str, str], Path, Path, Path, Path]:
    """A fresh POSIX machine with no uv, no agentic-hil, and one Python of our own.

    Step 2 reaches a discovered Python only on a machine where `uv` does not
    resolve, so every uv fallback from that point has to fetch uv first. That
    fetch is pinned to one release and checked against a hash this repository
    carries, and no test may reach astral.sh for it, so the machine here supplies
    the whole hop: a `curl` that writes an installer of our own instead of
    downloading one, a `sha256sum` that answers with the digest the script
    expects, and an installer that delivers a stub `uv` into `~/.local/bin` the
    way Astral's does. The stub answers `tool dir --bin` with that directory and
    `tool install` by writing a console script into it, so steps 3 to 5 run their
    ordinary shape.

    `python_body` is the whole difference between the cases: what this machine's
    one interpreter answers to `-m pip --version` and to `-m pip install`.

    Returns the environment, the project directory, and three files that record
    what happened: which copy ran `agent-install`, the arguments the stub uv was
    given, and whether the pinned installer was fetched at all.
    """
    home = tmp_path / "home"
    project = home / "project"
    early_bin = tmp_path / "early-bin"
    staging = tmp_path / "staging"
    user_bin = home / ".local" / "bin"
    for directory in (project, early_bin, staging):
        directory.mkdir(parents=True)

    marker = tmp_path / "who-ran-agent-install"
    uv_log = tmp_path / "uv-arguments"
    fetched = tmp_path / "installer-was-fetched"

    # A stub claude, so agent detection has a claude-code to register for.
    _stub_executable(early_bin / "claude", "exit 0\n")
    _stub_executable(early_bin / "python3", python_body)
    # The uv the fake installer delivers. It sits outside PATH until then, so
    # `have uv` is false where step 2 asks, which is the machine this is about.
    _stub_executable(
        staging / "uv",
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        f'  echo "{user_bin}"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        f'  echo "$*" >> "{uv_log}"\n'
        f'  cat > "{user_bin}/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        '  --version) echo "9.9.9" ;;\n'
        f'  agent-install) echo "uv" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        f'  chmod +x "{user_bin}/agentic-hil"\n'
        "fi\n"
        "exit 0\n",
    )
    # curl, writing the pinned installer rather than downloading it, and recording
    # that it was asked at all so a test can assert the fetch never happened.
    _stub_executable(
        early_bin / "curl",
        'out=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then out="$2"; fi\n'
        "  shift\n"
        "done\n"
        '[ -n "$out" ] || exit 1\n'
        f'echo "fetched" > "{fetched}"\n'
        'cat > "$out" <<\'PAYLOAD\'\n'
        "#!/bin/sh\n"
        # What Astral's installer reads to decide whether it edits the shell rc
        # files, recorded as this installer saw it. Nothing the stub writes
        # depends on it; the test about the header promise reads the record.
        f'echo "${{UV_NO_MODIFY_PATH:-unset}}" > "{tmp_path / "installer-saw-no-modify-path"}"\n'
        f'mkdir -p "{user_bin}"\n'
        f'cp "{staging}/uv" "{user_bin}/uv"\n'
        f'chmod +x "{user_bin}/uv"\n'
        "PAYLOAD\n"
        "exit 0\n",
    )
    # The digest the script carries, answered for whatever file it is handed. The
    # hash check itself has its own tests; here it is the hop, not the subject.
    digest = SHELL_UV_SHA256.search(_shell_source())
    assert digest is not None
    _stub_executable(early_bin / "sha256sum", f'echo "{digest.group(1)}  $1"\nexit 0\n')

    env = {"HOME": str(home), "PATH": f"{early_bin}:/usr/bin:/bin"}
    return env, project, marker, uv_log, fetched


# A python3 that answers the version probe and then has no pip at all: the
# default state of a Debian or Ubuntu server without python3-pip, and of most
# minimal container images.
_PYTHON_WITHOUT_PIP = (
    'case "$*" in\n'
    "  *version_info*) exit 0 ;;\n"
    "esac\n"
    'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then\n'
    '  echo "/usr/bin/python3: No module named pip" >&2\n'
    "  exit 1\n"
    "fi\n"
    "exit 0\n"
)

# A python3 whose pip is there and answers, and whose install is refused by the
# distribution under PEP 668.
_PYTHON_EXTERNALLY_MANAGED = (
    'case "$*" in\n'
    "  *version_info*) exit 0 ;;\n"
    '  *"pip --version"*) echo "pip 24.0 from /usr/lib/python3/dist-packages/pip (python 3.12)"; exit 0 ;;\n'
    '  *"pip install"*) echo "error: externally-managed-environment" >&2; exit 1 ;;\n'
    "esac\n"
    "exit 0\n"
)

# A python3 whose pip is there, answers, and then fails for a reason that is
# nothing to do with who owns the interpreter.
_PYTHON_WITH_A_FAILING_PIP = (
    'case "$*" in\n'
    "  *version_info*) exit 0 ;;\n"
    '  *"pip --version"*) echo "pip 24.0 from /usr/lib/python3/dist-packages/pip (python 3.12)"; exit 0 ;;\n'
    '  *"pip install"*) echo "ERROR: Could not find a version that satisfies the requirement agentic-hil"; exit 1 ;;\n'
    "esac\n"
    "exit 0\n"
)


def _install_on(env: dict[str, str], project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_posix_shell(), str(SHELL_SCRIPT), "--no-can"],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )


def test_a_python_without_a_pip_module_falls_back_to_uv(tmp_path: Path) -> None:
    """The commonest Linux of all, run end to end through a POSIX shell.

    Debian and Ubuntu ship `python3` with no pip module unless `python3-pip` is
    installed, and so do most minimal container images. `find_python` accepts that
    interpreter, because it is new enough, and step 2 then ran `-m pip install
    --user` with it. pip answered `No module named pip` and exited 1, which
    carries none of the words the PEP 668 branch reads, so the script stopped at
    step 2 with `pip could not install` on the most ordinary host there is, while
    it already knew how to fetch uv and install with that.

    Now the question is asked before the install: an interpreter with no pip takes
    the same uv route PEP 668 takes, and the line says which interpreter and why.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    env, project, marker, uv_log, fetched = _machine_whose_only_python_is(tmp_path, _PYTHON_WITHOUT_PIP)
    result = _install_on(env, project)

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert "has no pip module, so pip cannot install with it; falling back to uv" in transcript, transcript
    assert "python3" in transcript, transcript
    assert "pip could not install" not in transcript, transcript
    # The whole uv route, not just the sentence: the pinned installer was fetched,
    # uv was asked to install, and the copy it wrote is the one that registered.
    assert fetched.is_file(), transcript
    assert "tool install" in uv_log.read_text(encoding="utf-8"), transcript
    assert marker.read_text(encoding="utf-8").strip() == "uv", transcript


def test_an_externally_managed_python_still_falls_back_to_uv(tmp_path: Path) -> None:
    """The neighbour that must not move: PEP 668 keeps its own words and its route.

    This interpreter has a pip that answers, so the new probe passes it through to
    the install, which the distribution refuses. The transcript still says
    `externally managed (PEP 668)` and not the missing-module line, and the run
    still finishes through uv.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    env, project, marker, uv_log, fetched = _machine_whose_only_python_is(tmp_path, _PYTHON_EXTERNALLY_MANAGED)
    result = _install_on(env, project)

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert "this Python is externally managed (PEP 668), so pip cannot own it; falling back to uv" in transcript, transcript
    assert "has no pip module" not in transcript, transcript
    assert fetched.is_file(), transcript
    assert "tool install" in uv_log.read_text(encoding="utf-8"), transcript
    assert marker.read_text(encoding="utf-8").strip() == "uv", transcript


def test_a_pip_that_fails_for_another_reason_still_stops_the_run(tmp_path: Path) -> None:
    """The other half of the fix: the fallback is not widened into a catch-all.

    This interpreter's pip is there and answers, and then fails at the install for
    a reason that is neither PEP 668 nor a missing module. That is a real pip
    failure with a real message in it, and hiding it behind a silent uv install
    would lose the diagnosis, so the run still stops with the recorded line. The
    machine could have fallen back here: uv is one fetch away and every stub for
    it is in place, and the assertions are that none of it was used.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    env, project, marker, uv_log, fetched = _machine_whose_only_python_is(tmp_path, _PYTHON_WITH_A_FAILING_PIP)
    result = _install_on(env, project)

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode != 0, transcript
    assert "pip could not install agentic-hil; TROUBLESHOOTING.md section 1 has the fallbacks" in transcript, transcript
    assert "Could not find a version that satisfies" in transcript, transcript
    assert "has no pip module" not in transcript, transcript
    assert "falling back to uv" not in transcript, transcript
    assert not fetched.exists(), transcript
    assert not uv_log.exists(), transcript
    assert not marker.exists(), transcript


def test_both_scripts_ask_whether_a_discovered_python_has_pip_before_using_it() -> None:
    """The probe, pinned on both scripts, because only one of them runs here.

    A discovered Python with no pip module took the same fatal exit as a pip that
    failed on the network, and that is the default state of Debian and Ubuntu
    servers. Both installers now ask the interpreter whether it can run pip at all
    before they install with it, and the branch that says so is the one that hands
    the install to uv. The PowerShell side has no end-to-end run in every
    checkout, so this static check is its regression guard.
    """
    shell = _code_only(_shell_source())
    assert "python_has_pip" in shell
    assert "-m pip --version" in shell
    assert "python_has_pip" in _the_branch_that_holds(shell, "has no pip module")

    powershell = _code_only(_powershell_source())
    assert "Test-PythonHasPip" in powershell
    assert "'-m', 'pip', '--version'" in powershell
    assert "Test-PythonHasPip" in _the_branch_that_holds(powershell, "has no pip module")


def test_step_three_does_not_call_our_own_path_edit_the_operators_path(tmp_path: Path) -> None:
    """The newcomer's first stop, on the commonest Linux there is (#430).

    This machine's only python3 is externally managed, so step 2 fetches uv and
    installs with it, and the fetch prepends the user bin to PATH so that uv
    resolves for the rest of the run. Step 3 then compared the copy's directory
    against that edited PATH, found it, and printed `already on your PATH` about
    a shell where `agentic-hil` resolves to nothing. Worse, the export line is
    printed only on the other branch, so the reader was told the thing was fine
    and handed nothing to fix it with.

    The startup PATH here has no user bin in it, which is what a fresh account
    looks like, so the honest report is the other branch: name the directory, say
    it is not on PATH, and print the export line to copy.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    env, project, marker, _uv_log, _fetched = _machine_whose_only_python_is(tmp_path, _PYTHON_EXTERNALLY_MANAGED)
    user_bin = Path(env["HOME"]) / ".local" / "bin"
    assert str(user_bin) not in env["PATH"]

    result = _install_on(env, project)

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    # The install itself is unchanged: uv wrote the copy into the user bin and
    # that copy did the machine half. Only the sentence about it is at stake.
    assert marker.read_text(encoding="utf-8").strip() == "uv", transcript
    assert f"PATH: agentic-hil landed in {user_bin}, which is not on your PATH" in transcript, transcript
    assert "already on your PATH" not in transcript, transcript
    # The line the reader has to copy, printed on exactly the branch that admits
    # the directory is missing.
    assert f'export PATH="{user_bin}:$PATH"' in transcript, transcript


def test_step_three_still_says_a_user_bin_that_is_really_on_path_is_on_path(tmp_path: Path) -> None:
    """The other direction: the claim is kept where it is true.

    Same machine, same uv route, same destination, and one difference: the shell
    that started the installer already had the user bin on PATH. The reader's
    `agentic-hil` will resolve in this shell, so step 3 says so and prints no
    export line for them to paste over a PATH that already carries it.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    env, project, marker, _uv_log, _fetched = _machine_whose_only_python_is(tmp_path, _PYTHON_EXTERNALLY_MANAGED)
    user_bin = Path(env["HOME"]) / ".local" / "bin"
    env["PATH"] = f"{user_bin}:{env['PATH']}"

    result = _install_on(env, project)

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert marker.read_text(encoding="utf-8").strip() == "uv", transcript
    assert f"PATH: agentic-hil is installed in {user_bin}, already on your PATH" in transcript, transcript
    assert "which is not on your PATH" not in transcript, transcript
    assert "export PATH=" not in transcript, transcript


def test_both_scripts_report_step_three_from_the_path_they_were_started_with() -> None:
    """The snapshot, pinned on both scripts, because only one of them runs here.

    Both installers put a directory of their own in front of PATH before step 3
    reports, so both had the same way of calling their own edit the operator's
    environment. The comparison has to read the value the run was handed, taken
    before the first edit; the PowerShell side has no end-to-end run in every
    checkout, so this static check is its regression guard.
    """
    shell = _code_only(_shell_source())
    assert 'STARTUP_PATH="$PATH"' in shell
    assert 'case ":$STARTUP_PATH:" in' in shell
    assert 'case ":$PATH:" in' not in shell

    powershell = _code_only(_powershell_source())
    assert "$StartupPath = $env:Path" in powershell
    assert "($StartupPath -split ';') -contains $found" in powershell
    assert "($env:Path -split ';') -contains $found" not in powershell


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
        f'  agent-install) echo "ran" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
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
    """A `--version` pin is proven by exact equality, and an unpinned run by nothing.

    The documented `--version` installs an exact release, so a copy in the manager's
    own bin whose version merely exceeds the pin is not the one this run wrote. An
    unpinned run named no version, so the same check has nothing to compare against:
    it used to fall back to the release floor there, which refused every correct
    fresh install made during a release window (#310). The PowerShell side cannot run
    end to end in every checkout, so this pins both halves of the rule structurally:
    the exact check is present, and the floor is not reachable from it.
    """
    shell = _code_only(_shell_source())
    assert "version_exactly" in shell
    shell_body = re.search(r"^version_matches_request\(\) \{\n(.*?)\n\}", shell, re.MULTILINE | re.DOTALL)
    assert shell_body is not None, "install.sh has no version_matches_request"
    assert "version_exactly" in shell_body.group(1)
    assert "version_at_least" not in shell_body.group(1), "install.sh compares an unpinned copy against the floor again"

    powershell = _code_only(_powershell_source())
    assert "Test-VersionExactly" in powershell
    powershell_body = re.search(r"^function Test-VersionMatchesRequest \{\n(.*?)\n\}", powershell, re.MULTILINE | re.DOTALL)
    assert powershell_body is not None, "install.ps1 has no Test-VersionMatchesRequest"
    assert "Test-VersionExactly" in powershell_body.group(1)
    assert "Test-VersionAtLeast" not in powershell_body.group(1), "install.ps1 compares an unpinned copy against the floor again"


# The opener of a branch in either language: `if`, `elif`, `else` in the shell,
# and the same three spelled `if`, `} elseif`, `} else` in PowerShell.
BRANCH_OPENER = re.compile(r"^\}?\s*(?:if|elif|elseif|else)\b")


def _the_branch_that_holds(source: str, needle: str) -> str:
    """The guard of the branch the one line holding `needle` sits in.

    Both scripts decide "install nothing" in a single assignment inside a single
    branch of step 1's chain, and which branch that is, is the rule under test. The
    assignment is found by exact text and then the chain is walked upward to the
    opener above it. A second such assignment anywhere is itself the failure, and is
    reported as one rather than silently picked between.
    """
    lines = source.splitlines()
    found = [index for index, line in enumerate(lines) if needle in line]
    assert len(found) == 1, f"{needle} appears {len(found)} times, not once"
    for index in range(found[0] - 1, -1, -1):
        if BRANCH_OPENER.match(lines[index].strip()):
            return lines[index]
    raise AssertionError(f"{needle} sits in no branch at all")


def test_only_a_development_installation_is_kept_from_the_package_step() -> None:
    """The release names the run, it does not decide whether the run happens.

    An installation at or above the release used to be kept, step 2 never ran, and
    the transcript said "nothing to install" to the one person most likely to be
    re-running the line: someone whose `agentic-hil upgrade` had just failed on a
    current installation. The one-line installer is the emergency anchor, so an
    existing installation always reaches the manager, which is idempotent, and the
    comparison against the release only chooses the word (#315).

    A development tree is the single exception, and it is the exception #291 built:
    step 2 would replace an editable checkout with a release from PyPI. Both scripts
    are held to that being the only branch that installs nothing, and to still making
    the release comparison that names the other two.
    """
    shell = _code_only(_shell_source())
    assert 'version_at_least "$installed" "$RELEASE"' in shell
    assert "refreshing this current installation" in shell
    assert "version_is_development" in _the_branch_that_holds(shell, "NEEDS_PACKAGE=0")

    powershell = _code_only(_powershell_source())
    assert "Test-VersionAtLeast -Found $installed -Floor $Release" in powershell
    assert "refreshing this current installation" in powershell
    assert "Test-VersionIsDevelopment" in _the_branch_that_holds(powershell, "$needsPackage = $false")


def test_both_scripts_install_the_same_release() -> None:
    """The version an installation already here has to reach, stated once per script.

    It is the release now, not the capability floor it used to be. Both scripts
    have to move together for the same line to mean the same thing on either
    platform; tools/check_version_consistency.py holds the number to the release,
    and this holds the two scripts to each other.
    """
    shell = re.search(r'^RELEASE="(\d+\.\d+\.\d+)"$', _shell_source(), re.MULTILINE)
    powershell = re.search(r"^\$Release = '(\d+\.\d+\.\d+)'$", _powershell_source(), re.MULTILINE)
    assert shell is not None, "install.sh states no RELEASE"
    assert powershell is not None, "install.ps1 states no $Release"
    assert shell.group(1) == powershell.group(1)


def test_an_installation_below_the_release_is_upgraded_rather_than_kept(tmp_path: Path) -> None:
    """The returning user, run end to end through a POSIX shell.

    Step 1 compared against a capability floor of 0.4.0, so every copy installed
    since answered "skipping the package install" and step 4 registered the skill
    out of that copy: the one line the README hands a stranger left a 0.11.0 bench
    on 0.11.0 and wrote it a 0.11.0 skill, silently, while reporting success. The
    comparison is against the release now, so an older copy is upgraded and the
    machine half runs out of the fresh one.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")
    shell = _posix_shell()

    home = tmp_path / "home"
    project = home / "project"
    user_bin = home / ".local" / "bin"
    uv_bin = tmp_path / "uv-tools" / "bin"
    tools = tmp_path / "tools"
    for directory in (project, user_bin, uv_bin, tools):
        directory.mkdir(parents=True)

    marker = tmp_path / "who-ran-agent-install"

    # A real earlier release: far above the floor step 1 used to accept, far
    # below the release it compares against now.
    _stub_executable(
        user_bin / "agentic-hil",
        'case "$1" in\n'
        '  --version) echo "0.11.0" ;;\n'
        f'  agent-install) echo "stale" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
        "esac\n"
        "exit 0\n",
    )
    _stub_executable(tools / "claude", "exit 0\n")
    _stub_executable(
        tools / "uv",
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        '  echo "$UV_TOOL_BIN_DIR"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        '  cat > "$UV_TOOL_BIN_DIR/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        '  --version) echo "99.0.0" ;;\n'
        f'  agent-install) echo "fresh" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        '  chmod +x "$UV_TOOL_BIN_DIR/agentic-hil"\n'
        "fi\n"
        "exit 0\n",
    )

    env = {
        "HOME": str(home),
        "PATH": f"{tools}:{user_bin}:/usr/bin:/bin",
        "UV_TOOL_BIN_DIR": str(uv_bin),
    }

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--no-can"],
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
    assert "nothing to install" not in transcript, transcript
    assert "0.11.0 is older than" in transcript, transcript
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "fresh", transcript


def _release() -> str:
    """The release install.sh states, read from the script rather than copied here.

    The number moves with every release and these tests have to move with it. A
    literal written into this file would keep passing while saying nothing about the
    version the installer actually carries.
    """
    found = re.search(r'^RELEASE="(\d+\.\d+\.\d+)"$', _shell_source(), re.MULTILINE)
    assert found is not None, "install.sh states no RELEASE"
    return found.group(1)


def _the_release_below(release: str) -> str:
    """What the index still serves inside a release window: one release below.

    One minor down where there is a minor to spend, one major otherwise. Either way
    it is a real release number that sits below the floor, which is the only
    property the window depends on.
    """
    major, minor, _patch = (int(part) for part in release.split("."))
    if minor > 0:
        return f"{major}.{minor - 1}.0"
    assert major > 0, f"there is no release below {release} for a window to serve"
    return f"{major - 1}.0.0"


def _rerun_over_an_existing_installation(tmp_path: Path, *, installed: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """A machine that already has agentic-hil, and one unpinned rerun of the line.

    The copy on PATH reports `installed` and writes "existing" to the marker if it is
    the one the machine half ends up calling; the copy `uv tool install` writes into
    the manager's own bin reports the release and writes "fresh". Which name is in
    the marker is therefore which installation step 4 registered the skill out of.

    The stub uv appends every invocation to a log, so "the manager ran" is a fact
    about this run rather than a reading of the transcript: on the kept path no uv
    invocation happens at all and the log is never created.

    The copy in the manager's bin starts damaged: it answers --version so step 3
    still locates it, but writes "damaged" from agent-install. The stub `tool
    install` replaces it with the "fresh" copy only when it is given --reinstall,
    so a marker that reads "fresh" is proof the flag was passed and the broken
    files were actually replaced, not merely proof that the manager was invoked.
    This uv does not answer `tool list`, so it stands for a copy uv does not
    manage, and a refresh falls back to `tool install --reinstall`.

    Returns the finished run, the marker file, and that log.
    """
    shell = _posix_shell()

    home = tmp_path / "home"
    project = home / "project"
    user_bin = home / ".local" / "bin"
    uv_bin = tmp_path / "uv-tools" / "bin"
    tools = tmp_path / "tools"
    for directory in (project, user_bin, uv_bin, tools):
        directory.mkdir(parents=True)

    marker = tmp_path / "who-ran-agent-install"
    uv_log = tmp_path / "uv-invocations"

    _stub_executable(
        user_bin / "agentic-hil",
        'case "$1" in\n'
        f'  --version) echo "{installed}" ;;\n'
        f'  agent-install) echo "existing" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
        "esac\n"
        "exit 0\n",
    )
    # The manager's own bin already holds a copy, and it is the broken one the
    # refresh has to replace: it answers --version but is not the fresh install.
    _stub_executable(
        uv_bin / "agentic-hil",
        'case "$1" in\n'
        f'  --version) echo "{_release()}" ;;\n'
        f'  agent-install) echo "damaged" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
        "esac\n"
        "exit 0\n",
    )
    # A stub claude, so agent detection has a claude-code to register for.
    _stub_executable(tools / "claude", "exit 0\n")
    _stub_executable(
        tools / "uv",
        f'echo "$*" >> "{uv_log}"\n'
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        '  echo "$UV_TOOL_BIN_DIR"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        # Without --reinstall uv leaves an already-current copy in place, so the
        # damaged one survives; the stub models exactly that.
        '  case "$*" in\n'
        "    *--reinstall*) : ;;\n"
        "    *) exit 0 ;;\n"
        "  esac\n"
        '  cat > "$UV_TOOL_BIN_DIR/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        f'  --version) echo "{_release()}" ;;\n'
        f'  agent-install) echo "fresh" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        '  chmod +x "$UV_TOOL_BIN_DIR/agentic-hil"\n'
        "fi\n"
        "exit 0\n",
    )

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--no-can"],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            "HOME": str(home),
            "PATH": f"{tools}:{user_bin}:/usr/bin:/bin",
            "UV_TOOL_BIN_DIR": str(uv_bin),
        },
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )
    return result, marker, uv_log


def test_an_installation_at_the_release_is_refreshed_through_the_manager(tmp_path: Path) -> None:
    """The emergency anchor, run end to end through a POSIX shell.

    A bench watched `agentic-hil upgrade` fail on a current installation and did the
    one thing the README tells it to do: it ran the install line again. Step 1 found
    a copy at the release, kept it, and step 2 answered "nothing to install", so the
    documented rescue path had nothing to rescue with (#315).

    An existing installation reaches the manager now whatever it reports, and a
    copy that is current but broken is reinstalled rather than resolved as
    already-current and left in place. `uv tool install --upgrade` alone would
    report the requirement already satisfied and repair nothing, so the refresh
    carries --reinstall. Step 1's comparison still runs; it names the run instead
    of deciding it.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    release = _release()
    result, marker, uv_log = _rerun_over_an_existing_installation(tmp_path, installed=release)

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert f"agentic-hil {release} is here and not older than {release}, refreshing this current installation" in transcript, transcript
    assert "nothing to install" not in transcript, transcript
    # The manager ran, and it ran an install that carried --reinstall: the stub
    # replaces the damaged copy only for that flag, so the assertion below on the
    # marker is what proves the broken files were actually replaced.
    assert uv_log.is_file(), transcript
    invocations = uv_log.read_text(encoding="utf-8")
    assert "tool install --upgrade --reinstall agentic-hil" in invocations, invocations
    # And step 4 registered out of the copy the reinstall just wrote, not out of
    # the damaged copy that was already in the manager's bin.
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "fresh", transcript


def test_a_development_installation_is_kept_and_the_transcript_says_why(tmp_path: Path) -> None:
    """The one installation the anchor must not drag through PyPI (#291).

    An editable checkout reports X.Y.Z.devN, and `uv tool install --upgrade` would
    replace the operator's own working copy with a release. That copy is kept, no
    manager is invoked at all, and the transcript says which kind of installation it
    kept and what installing over it would have cost, because "nothing to install"
    on its own is exactly the answer #315 removed everywhere else.

    Both directions of the number are run. The keep used to be a side effect of the
    comparison, so it held only while the development tree sat above the release;
    a checkout of an older branch reported a lower number and was quietly replaced.
    The marker is the proof of the second half: the development copy is still the one
    step 4 registers the skill out of.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    release = _release()
    major, minor, patch = (int(part) for part in release.split("."))
    above = f"{major}.{minor}.{patch + 1}.dev0"
    below = f"{_the_release_below(release)}.dev1"

    for index, installed in enumerate((above, below)):
        result, marker, uv_log = _rerun_over_an_existing_installation(tmp_path / f"tree{index}", installed=installed)

        transcript = f"{result.stdout}{result.stderr}"
        assert result.returncode == 0, transcript
        assert f"agentic-hil {installed} is a development version, so it is kept" in transcript, transcript
        assert "would replace an editable checkout with a release from PyPI" in transcript, transcript
        assert "nothing to install, the development installation stays as it is" in transcript, transcript
        # Nothing reached the manager: not the install, not even the question about
        # where its bin directory is.
        assert not uv_log.exists(), transcript
        assert marker.is_file(), transcript
        assert marker.read_text(encoding="utf-8").strip() == "existing", transcript


def _uv_refresh_stub(release: str, uv_log: Path, pycan_marker: Path, marker: Path) -> str:
    """A uv stub for the merge-on-refresh path, shared by the preserve and add
    cases below. It answers `tool dir` with the tool root and `tool dir --bin`
    with the bin directory, so the script can find the receipt and the console
    script; it answers `tool list` so uv is seen to own the tool; and its `tool
    install` models real uv, recording exactly the requirement it is handed. It
    rewrites the receipt to the extras of that requirement, drops a python-can
    marker when `can` is among them, and (only under --reinstall) writes the
    repaired console script whose agent-install reports "fresh"."""
    return (
        f'echo "$*" >> "{uv_log}"\n'
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        '  if [ "$3" = "--bin" ]; then echo "$UV_TOOL_BIN_DIR"; else echo "$UV_TOOL_ROOT"; fi\n'
        "  exit 0\n"
        "fi\n"
        # uv owns this tool, so the probe finds it and the refresh reads the receipt.
        'if [ "$1" = "tool" ] && [ "$2" = "list" ]; then\n'
        f'  echo "agentic-hil v{release}"\n'
        "  exit 0\n"
        "fi\n"
        # `tool install` records the requirement it is handed, extras and all. The
        # merge is what hands it the recorded extras plus this run's, so the receipt
        # it writes here is what proves both survived. --reinstall repairs the files.
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        '  case "$*" in\n'
        "    *--reinstall*) : ;;\n"
        "    *) exit 0 ;;\n"
        "  esac\n"
        '  for spec in "$@"; do :; done\n'
        '  extras=$(printf \'%s\' "$spec" | sed -n \'s/[^[]*\\[\\([^]]*\\)\\].*/\\1/p\' | tr \',\' \' \')\n'
        '  quoted=""\n'
        '  for e in $extras; do\n'
        '    if [ -z "$quoted" ]; then quoted="\\"$e\\""; else quoted="$quoted, \\"$e\\""; fi\n'
        "  done\n"
        '  printf \'requirements = [{ name = "agentic-hil", extras = [%s] }]\\n\' "$quoted" > "$UV_TOOL_ROOT/agentic-hil/uv-receipt.toml"\n'
        '  case " $extras " in\n'
        f'    *" can "*) echo installed > "{pycan_marker}" ;;\n'
        "  esac\n"
        '  cat > "$UV_TOOL_BIN_DIR/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        f'  --version) echo "{release}" ;;\n'
        f'  agent-install) echo "fresh" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        '  chmod +x "$UV_TOOL_BIN_DIR/agentic-hil"\n'
        "  exit 0\n"
        "fi\n"
        # The upgrade path is the fallback for a receipt this cannot read. It must
        # not be taken when the receipt is present, so it leaves the copy damaged;
        # taking it wrongly is then caught by the "fresh" marker assertion.
        'if [ "$1" = "tool" ] && [ "$2" = "upgrade" ]; then\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )


def _uv_refresh_with_stub(release: str, uv_log: Path, pycan_marker: Path, marker: Path) -> str:
    """A uv stub for the multiline `--with` refresh path. Like ``_uv_refresh_stub``
    it answers the probes and repairs the console script under --reinstall, but its
    `tool install` models real uv's receipt for a tool that carries `--with`
    requirements: it finds the reconstructed `agentic-hil[...]` spec and every
    `--with` value it was handed, and rewrites the receipt to the multiline shape
    uv writes, the root requirement first, then one object per replayed `--with`.
    That receipt is what proves both the recorded root extra and the recorded
    `--with` survived the refresh instead of being dropped."""
    return (
        f'echo "$*" >> "{uv_log}"\n'
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        '  if [ "$3" = "--bin" ]; then echo "$UV_TOOL_BIN_DIR"; else echo "$UV_TOOL_ROOT"; fi\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "list" ]; then\n'
        f'  echo "agentic-hil v{release}"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        '  case "$*" in\n'
        "    *--reinstall*) : ;;\n"
        "    *) exit 0 ;;\n"
        "  esac\n"
        # Separate the reconstructed root spec from the replayed --with values.
        '  spec=""; withs=""; prev=""\n'
        '  for a in "$@"; do\n'
        '    if [ "$prev" = "--with" ]; then withs="$withs $a"; fi\n'
        '    case "$a" in agentic-hil*) spec="$a" ;; esac\n'
        '    prev="$a"\n'
        "  done\n"
        '  extras=$(printf \'%s\' "$spec" | sed -n \'s/[^[]*\\[\\([^]]*\\)\\].*/\\1/p\' | tr \',\' \' \')\n'
        '  quoted=""\n'
        '  for e in $extras; do\n'
        '    if [ -z "$quoted" ]; then quoted="\\"$e\\""; else quoted="$quoted, \\"$e\\""; fi\n'
        "  done\n"
        '  {\n'
        '    echo "requirements = ["\n'
        '    if [ -n "$quoted" ]; then\n'
        '      echo "    { name = \\"agentic-hil\\", extras = [$quoted] },"\n'
        '    else\n'
        '      echo "    { name = \\"agentic-hil\\" },"\n'
        '    fi\n'
        '    for w in $withs; do\n'
        '      case "$w" in\n'
        '        *==*) echo "    { name = \\"${w%%==*}\\", specifier = \\"==${w#*==}\\" }," ;;\n'
        '        *) echo "    { name = \\"$w\\" }," ;;\n'
        '      esac\n'
        "    done\n"
        '    echo "]"\n'
        '  } > "$UV_TOOL_ROOT/agentic-hil/uv-receipt.toml"\n'
        '  case " $extras " in\n'
        f'    *" can "*) echo installed > "{pycan_marker}" ;;\n'
        "  esac\n"
        '  cat > "$UV_TOOL_BIN_DIR/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        f'  --version) echo "{release}" ;;\n'
        f'  agent-install) echo "fresh" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        '  chmod +x "$UV_TOOL_BIN_DIR/agentic-hil"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "upgrade" ]; then\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )


def _run_uv_refresh(
    tmp_path: Path,
    recorded_receipt: str,
    stub: Callable[[str, Path, Path, Path], str] = _uv_refresh_stub,
) -> tuple[subprocess.CompletedProcess[str], str, Path, Path, Path]:
    """Drive `install.sh` end to end against a uv-managed tool whose receipt records
    ``recorded_receipt``. Returns the process, the uv invocation log, and the three
    files the assertions read: the rewritten receipt, the python-can marker, and the
    agent-install marker."""
    shell = _posix_shell()
    release = _release()
    home = tmp_path / "home"
    project = home / "project"
    uv_bin = tmp_path / "uv-tools" / "bin"
    uv_root = tmp_path / "uv-tools" / "tools"
    receipt_dir = uv_root / "agentic-hil"
    tools = tmp_path / "tools"
    for directory in (project, uv_bin, receipt_dir, tools):
        directory.mkdir(parents=True)

    marker = tmp_path / "who-ran-agent-install"
    pycan_marker = tmp_path / "python-can-was-installed"
    uv_log = tmp_path / "uv-invocations"
    # The requirement uv recorded when the tool was installed, in the receipt uv
    # keeps beside the tool environment. The refresh reads it to decide the
    # requirement it reinstalls from.
    receipt = receipt_dir / "uv-receipt.toml"
    receipt.write_text(recorded_receipt, encoding="utf-8")

    # The tool uv already owns, sitting in its own bin, which is on PATH. It is the
    # damaged copy the refresh has to replace: it answers --version but writes
    # "damaged" from agent-install.
    _stub_executable(
        uv_bin / "agentic-hil",
        'case "$1" in\n'
        f'  --version) echo "{release}" ;;\n'
        f'  agent-install) echo "damaged" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
        "esac\n"
        "exit 0\n",
    )
    _stub_executable(tools / "claude", "exit 0\n")
    _stub_executable(tools / "uv", stub(release, uv_log, pycan_marker, marker))

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT)],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            "HOME": str(home),
            "PATH": f"{tools}:{uv_bin}:/usr/bin:/bin",
            "UV_TOOL_BIN_DIR": str(uv_bin),
            "UV_TOOL_ROOT": str(uv_root),
        },
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )
    invocations = uv_log.read_text(encoding="utf-8") if uv_log.is_file() else ""
    return result, invocations, receipt, pycan_marker, marker


def test_a_first_install_still_closes_on_the_sentence_written_for_one(tmp_path: Path) -> None:
    """The neighbouring run, unchanged: nothing here yet, so nothing to say about a restart.

    Somebody on this machine has never used this tool, has no project
    configuration and has no server running out of anything, and the calm line
    is exactly right for them. It is the run the refresh sentence must not
    reach."""
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")
    shell = _posix_shell()

    home = tmp_path / "home"
    project = home / "project"
    uv_bin = tmp_path / "uv-tools" / "bin"
    tools = tmp_path / "tools"
    for directory in (project, uv_bin, tools):
        directory.mkdir(parents=True)

    _stub_executable(
        tools / "uv",
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        '  echo "$UV_TOOL_BIN_DIR"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        '  cat > "$UV_TOOL_BIN_DIR/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        '  --version) echo "99.0.0" ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        '  chmod +x "$UV_TOOL_BIN_DIR/agentic-hil"\n'
        "fi\n"
        "exit 0\n",
    )

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--no-can", "--no-agent-install"],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"HOME": str(home), "PATH": f"{tools}:/usr/bin:/bin", "UV_TOOL_BIN_DIR": str(uv_bin)},
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert "no agentic-hil on this PATH" in transcript, transcript
    assert CALM_LINE in transcript, transcript
    assert REFRESH_LINE not in transcript, transcript


def test_a_refresh_closes_on_the_installation_it_replaced_and_not_on_a_first_install(tmp_path: Path) -> None:
    """The reported defect, on the run that produced it: an installation already here.

    `install.sh` closed a refresh from one release to the next with "The next
    start of your agent has everything, and the first hardware question creates
    this project's configuration", on a host with four project configurations on
    it. That sentence is an answer for somebody meeting the tool for the first
    time, and no run over an installation that is already here may reach it.

    This one is the anchor run: a copy already at the release, reinstalled in
    place because it had stopped working. The files moved and the release did
    not, so what it closes on is the sentence about staying where it is, not the
    one asking for a restart to pick a new release up.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    result, _invocations, _receipt, _pycan, _marker = _run_uv_refresh(
        tmp_path,
        'requirements = [{ name = "agentic-hil" }]\n',
    )

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert "refreshing this current installation" in transcript, transcript
    assert f"This installation stayed at {_release()}, {KEPT_CURRENT_TAIL}" in transcript, transcript
    assert REFRESH_LINE not in transcript, transcript
    assert CALM_LINE not in transcript, transcript


def test_refreshing_a_uv_tool_keeps_the_extras_it_was_installed_with(tmp_path: Path) -> None:
    """The regression a `uv tool install --upgrade agentic-hil[can]` refresh caused.

    A bench installed as `agentic-hil[can,pyocd]` and re-ran the canonical line,
    which asks for `[can]` only. `uv tool install` records the requirement it is
    handed, so refreshing through it with this run's spec alone rewrote the tool to
    `[can]` and dropped the pyOCD dependency the bench was using. The refresh now
    reads the extras uv recorded and merges them with this run's, so it reinstalls
    from `agentic-hil[can,pyocd]` and `[can,pyocd]` survives.

    --reinstall is still what makes the install replace a current-but-broken copy:
    the stub only writes the repaired console script for that flag, so the "fresh"
    marker also proves the repair happened.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    result, invocations, receipt, pycan_marker, marker = _run_uv_refresh(
        tmp_path,
        'requirements = [{ name = "agentic-hil", extras = ["can", "pyocd"] }]\n',
    )

    release = _release()
    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert f"agentic-hil {release} is here and not older than {release}, refreshing this current installation" in transcript, transcript
    # The refresh reinstalled from the merged requirement, not a bare
    # `agentic-hil[can]` that would have dropped pyocd, and not the `tool upgrade`
    # fallback that leaves the copy damaged.
    assert "tool install --upgrade --reinstall agentic-hil[can,pyocd]" in invocations, invocations
    assert "tool upgrade" not in invocations, invocations
    # The repair happened: step 4 registered out of the reinstalled copy.
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "fresh", transcript
    # The receipt the reinstall recorded still names both extras, and the can extra
    # pulled its dependency in.
    recorded = receipt.read_text(encoding="utf-8")
    assert '"pyocd"' in recorded, recorded
    assert '"can"' in recorded, recorded
    assert pycan_marker.is_file(), transcript


def test_refreshing_a_bare_uv_tool_adds_the_can_extra_this_run_asks_for(tmp_path: Path) -> None:
    """The other half of the merge: a requested extra that was missing is added.

    A bench installed the tool bare, `agentic-hil`, no extras, and re-ran the
    canonical line, which enables `[can]` by default. A `uv tool upgrade` would
    reinstall from the bare recorded requirement and never add the extra, so the
    documented --can option could not enable CAN on an existing uv installation and
    the run reported success without installing what it named. The refresh now
    merges this run's `can` into the recorded requirement and reinstalls from
    `agentic-hil[can]`, so the receipt records `can` and python-can is pulled in.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    result, invocations, receipt, pycan_marker, marker = _run_uv_refresh(
        tmp_path,
        'requirements = [{ name = "agentic-hil" }]\n',
    )

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    # The refresh reinstalled from the merged requirement, adding the can this run
    # asked for that the bare recorded requirement did not have.
    assert "tool install --upgrade --reinstall agentic-hil[can]" in invocations, invocations
    assert "tool upgrade" not in invocations, invocations
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "fresh", transcript
    # The resulting receipt records the extra, and the dependency it pulls in landed.
    recorded = receipt.read_text(encoding="utf-8")
    assert '"can"' in recorded, recorded
    assert pycan_marker.is_file(), transcript


def test_refreshing_a_uv_tool_keeps_a_recorded_with_requirement_and_extra(tmp_path: Path) -> None:
    """The regression a multiline receipt caused: a recorded `--with` was dropped.

    uv writes a multiline requirement list the moment the tool carries a `--with`
    dependency, and the previous reader only saw the first object on the
    `requirements = [` line, so a real `agentic-hil[pyocd] --with requests==2.32.5`
    parsed to no extras, and the refresh reinstalled from a bare `agentic-hil[can]`
    that dropped both the recorded pyOCD extra and the recorded requests `--with`.
    The refresh now reads the whole recorded set across lines: it reinstalls from
    `agentic-hil[can,pyocd]` and replays `--with requests==2.32.5`, so the recorded
    extra, the requested extra, and the recorded `--with` all survive.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    result, invocations, receipt, pycan_marker, marker = _run_uv_refresh(
        tmp_path,
        'requirements = [\n'
        '    { name = "agentic-hil", extras = ["pyocd"] },\n'
        '    { name = "requests", specifier = "==2.32.5" },\n'
        ']\n'
        'entrypoints = [\n'
        '    { name = "agentic-hil", install-path = "/x", from = "agentic-hil" },\n'
        ']\n',
        stub=_uv_refresh_with_stub,
    )

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    # The reinstall merged the recorded pyocd with this run's can AND replayed the
    # recorded --with, rather than dropping either the way the bare-spec reinstall
    # did, and rather than falling back to the `tool upgrade` that cannot add can.
    assert "tool install --upgrade --reinstall agentic-hil[can,pyocd] --with requests==2.32.5" in invocations, invocations
    assert "tool upgrade" not in invocations, invocations
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "fresh", transcript
    # The receipt the reinstall recorded still names both extras and the --with.
    recorded = receipt.read_text(encoding="utf-8")
    assert '"pyocd"' in recorded, recorded
    assert '"can"' in recorded, recorded
    assert '"requests"' in recorded, recorded
    assert "==2.32.5" in recorded, recorded
    assert pycan_marker.is_file(), transcript


def test_both_scripts_merge_the_recorded_extras_into_a_uv_refresh() -> None:
    """The merge-on-refresh contract, pinned on both scripts.

    A uv-managed refresh must reinstall from the extras uv recorded merged with
    this run's, so it neither drops a recorded extra nor ignores a requested one,
    and it must replay every recorded `--with` requirement so a multiline receipt
    does not lose it. The PowerShell side has no interpreter in every checkout, so
    this static check is its regression guard: it reads uv's receipt, builds the
    merged spec plus the recorded `--with` arguments, and hands them to
    `tool install --reinstall` rather than reinstalling the bare name through
    `tool upgrade` when the receipt is there.
    """
    shell = _code_only(_shell_source())
    powershell = _code_only(_powershell_source())

    # Both read the requirement uv recorded, from the receipt beside the tool env.
    assert "uv-receipt.toml" in shell, shell
    assert "uv-receipt.toml" in powershell, powershell
    # Both build the merged requirement and reinstall from it under --reinstall,
    # with the recorded --with requirements replayed after it.
    assert re.search(r'tool install --upgrade --reinstall "\$\(refresh_spec .*\$with_flags', shell), shell
    assert re.search(r"'tool', 'install', '--upgrade', '--reinstall', \(Get-RefreshSpec", powershell), powershell
    assert re.search(r"foreach \(\$recordedWith in \$recorded\.Withs\).*'--with', \$recordedWith", powershell), powershell
    # Both read the whole recorded requirement set, not just the first object, so a
    # multiline receipt's --with requirements are seen.
    assert "uv_recorded_requirements" in shell, shell
    assert "Get-UvRecordedRequirements" in powershell, powershell
    # And the pre-fix bare-name upgrade is only the fallback for an unreadable
    # receipt, not the path a present receipt takes.
    assert "refresh_spec" in shell, shell
    assert "Get-RefreshSpec" in powershell, powershell


def test_refreshing_a_uv_tool_with_a_recorded_source_falls_back_to_the_upgrade(tmp_path: Path) -> None:
    """A root recorded with a local `directory` source (a url or git source the
    same) cannot be rebuilt as `agentic-hil[...]` without switching the tool to the
    public index, so the refresh reconstruction is refused and the preserving
    `uv tool upgrade --reinstall` runs instead.

    Before this fix the parser validated only the non-root requirements and read a
    root's extras while ignoring its source, so a path/url/git installation was
    silently reinstalled from PyPI. The stub's upgrade is a no-op, so the tool is
    left as it was: the `damaged` marker is what proves the reconstruction install
    did not run in its place.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    result, invocations, receipt, pycan_marker, marker = _run_uv_refresh(
        tmp_path,
        "[tool]\n"
        'requirements = [{ name = "agentic-hil", directory = "/opt/agentic-hil-src" }]\n',
    )

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert "tool upgrade --reinstall agentic-hil" in invocations, invocations
    assert "tool install --upgrade --reinstall agentic-hil[" not in invocations, invocations
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "damaged", transcript


def test_refreshing_a_uv_tool_with_a_recorded_index_option_falls_back_to_the_upgrade(tmp_path: Path) -> None:
    """A recorded `[tool.options]` index cannot be replayed by a reconstruction
    that passes no options, so a refresh that reinstalled from `agentic-hil[...]`
    would switch a private-index installation to the public index. The refresh now
    refuses the reconstruction and keeps to the `uv tool upgrade --reinstall` that
    preserves whatever uv recorded, options and all.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    result, invocations, receipt, pycan_marker, marker = _run_uv_refresh(
        tmp_path,
        "[tool]\n"
        'requirements = [{ name = "agentic-hil", extras = ["can"] }]\n'
        "\n"
        "[tool.options]\n"
        'index = ["https://buildbot:tok3n@packages.example.internal/simple/"]\n',
    )

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert "tool upgrade --reinstall agentic-hil" in invocations, invocations
    assert "tool install --upgrade --reinstall agentic-hil[" not in invocations, invocations
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "damaged", transcript


# uv's receipt for `uv tool install --python 3.10 "agentic-hil[can]"`, recorded with
# uv 0.11.27 on 2026-09-06: the interpreter is a `[tool]`-level key beside
# `requirements`, not a `[tool.options]` entry, and the entrypoints array closes
# the file. The container tier reads the same layout off uv 0.12.9
# (tests/container/test_uv_receipt.py). The install path is a neutral one in
# place of the recorded one, which named the recording machine's directories.
_RECEIPT_WITH_A_RECORDED_INTERPRETER = (
    "[tool]\n"
    'requirements = [{ name = "agentic-hil", extras = ["can"] }]\n'
    'python = "3.10"\n'
    "entrypoints = [\n"
    '    { name = "agentic-hil", install-path = "/opt/uv/bin/agentic-hil", from = "agentic-hil" },\n'
    "]\n"
)
# The spelling older uv used, which the upgrade path reads second
# (`_recorded_python` in upgrade.py) and which the suite already carries as a
# fixture (tests/test_agentic_hil.py, `recorded-under-tool-options`): receipts
# written that way are still on disk, so both installers read both levels.
_RECEIPT_WITH_THE_INTERPRETER_UNDER_OPTIONS = (
    "[tool]\n"
    'requirements = [{ name = "agentic-hil", extras = ["can"] }]\n'
    "\n"
    "[tool.options]\n"
    'python = "3.10"\n'
)
# Both spellings, one case each, in both installers.
_RECORDED_INTERPRETER_RECEIPTS = [
    pytest.param(_RECEIPT_WITH_A_RECORDED_INTERPRETER, id="under-tool-as-uv-writes-it-now"),
    pytest.param(_RECEIPT_WITH_THE_INTERPRETER_UNDER_OPTIONS, id="under-tool-options-as-older-uv-wrote-it"),
]
# The two recorded layouts in one receipt: the multiline requirement list uv
# writes the moment a `--with` is recorded (the fixture the with-replay tests
# above carry) and the `[tool]`-level interpreter of the recording above. Not a
# third recording; the reinstall line has to carry both `--with` and `--python`,
# and the with-replay tests have no interpreter while the interpreter tests
# have no `--with`.
_RECEIPT_WITH_AN_INTERPRETER_AND_A_WITH = (
    "[tool]\n"
    "requirements = [\n"
    '    { name = "agentic-hil", extras = ["pyocd"] },\n'
    '    { name = "requests", specifier = "==2.32.5" },\n'
    "]\n"
    'python = "3.10"\n'
    "entrypoints = [\n"
    '    { name = "agentic-hil", install-path = "/opt/uv/bin/agentic-hil", from = "agentic-hil" },\n'
    "]\n"
)
# What the refresh says when it replays the interpreter, so that the choice
# being kept is on screen: #476 observed that the record was lost "with nothing
# said on screen", and the same line says it is kept.
RECORDED_INTERPRETER_SAID = "the receipt records the interpreter 3.10, so the reinstall keeps it"


def _uv_refresh_interpreter_stub(release: str, uv_log: Path, pycan_marker: Path, marker: Path) -> str:
    """A uv stub for the recorded-interpreter refresh path. Like ``_uv_refresh_stub``
    it answers the probes and repairs the console script under --reinstall, and its
    `tool install` models what uv records about the interpreter: the receipt it
    rewrites carries `python = "<value>"` at the `[tool]` level when the install
    line handed it `--python <value>`, and no `python` key at all when it did not.
    Both measured with uv 0.11.27 on 2026-09-06: `uv tool install --upgrade
    --reinstall` without `--python` left no `python` key in the receipt, and with
    `--python 3.10` it recorded `python = "3.10"` again; #476 measured the same
    on uv 0.12.9. A replayed `--with` goes into the receipt the way
    ``_uv_refresh_with_stub`` writes it, one object per requirement under a
    multiline list. `tool upgrade` leaves the receipt exactly as it is, which is
    the preserving behaviour the fallback relies on (measured the same way). That
    it also leaves the copy unrepaired is the stub's own device and no claim
    about uv: the marker then still reads "damaged", which tells a refresh that
    took the fallback wrongly apart from one that repaired the copy."""
    return (
        f'echo "$*" >> "{uv_log}"\n'
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        '  if [ "$3" = "--bin" ]; then echo "$UV_TOOL_BIN_DIR"; else echo "$UV_TOOL_ROOT"; fi\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "list" ]; then\n'
        f'  echo "agentic-hil v{release}"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        '  case "$*" in\n'
        "    *--reinstall*) : ;;\n"
        "    *) exit 0 ;;\n"
        "  esac\n"
        # The reconstructed root spec, the interpreter the line replays, if any,
        # and every replayed --with.
        '  spec=""; python=""; withs=""; prev=""\n'
        '  for a in "$@"; do\n'
        '    if [ "$prev" = "--python" ]; then python="$a"; fi\n'
        '    if [ "$prev" = "--with" ]; then withs="$withs $a"; fi\n'
        '    case "$a" in agentic-hil*) spec="$a" ;; esac\n'
        '    prev="$a"\n'
        "  done\n"
        '  extras=$(printf \'%s\' "$spec" | sed -n \'s/[^[]*\\[\\([^]]*\\)\\].*/\\1/p\' | tr \',\' \' \')\n'
        '  quoted=""\n'
        '  for e in $extras; do\n'
        '    if [ -z "$quoted" ]; then quoted="\\"$e\\""; else quoted="$quoted, \\"$e\\""; fi\n'
        "  done\n"
        '  if [ -n "$quoted" ]; then root="{ name = \\"agentic-hil\\", extras = [$quoted] }"; else root="{ name = \\"agentic-hil\\" }"; fi\n'
        "  {\n"
        '    echo "[tool]"\n'
        '    if [ -z "$withs" ]; then\n'
        '      echo "requirements = [$root]"\n'
        "    else\n"
        '      echo "requirements = ["\n'
        '      echo "    $root,"\n'
        "      for w in $withs; do\n"
        '        case "$w" in\n'
        '          *==*) echo "    { name = \\"${w%%==*}\\", specifier = \\"==${w#*==}\\" }," ;;\n'
        '          *) echo "    { name = \\"$w\\" }," ;;\n'
        "        esac\n"
        "      done\n"
        '      echo "]"\n'
        "    fi\n"
        '    if [ -n "$python" ]; then echo "python = \\"$python\\""; fi\n'
        '    echo "entrypoints = ["\n'
        '    echo "    { name = \\"agentic-hil\\", install-path = \\"$UV_TOOL_BIN_DIR/agentic-hil\\", from = \\"agentic-hil\\" },"\n'
        '    echo "]"\n'
        '  } > "$UV_TOOL_ROOT/agentic-hil/uv-receipt.toml"\n'
        '  case " $extras " in\n'
        f'    *" can "*) echo installed > "{pycan_marker}" ;;\n'
        "  esac\n"
        '  cat > "$UV_TOOL_BIN_DIR/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        f'  --version) echo "{release}" ;;\n'
        f'  agent-install) echo "fresh" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        '  chmod +x "$UV_TOOL_BIN_DIR/agentic-hil"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "upgrade" ]; then\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )


def _reinstall_lines(invocations: str, spec: str = "agentic-hil[can]") -> list[str]:
    """The `tool install --upgrade --reinstall <spec> ...` lines a refresh ran."""
    return [line for line in invocations.splitlines() if line.startswith(f"tool install --upgrade --reinstall {spec}")]


@pytest.mark.parametrize("recorded_receipt", _RECORDED_INTERPRETER_RECEIPTS)
def test_refreshing_a_uv_tool_installed_with_an_interpreter_replays_it(tmp_path: Path, recorded_receipt: str) -> None:
    """#476: the recorded interpreter survives the refresh, and is on the line that rebuilds it.

    `uv tool install --python 3.10 "agentic-hil[can]"` records the interpreter,
    and current uv records it as a `[tool]`-level key. The reader refused only a
    `[tool.options]` table, so that receipt passed straight through and the
    refresh reached `uv tool install --upgrade --reinstall agentic-hil[can]` with
    no `--python`: uv then rewrote the receipt without the key, and the
    operator's choice of interpreter was gone from the record, with nothing said
    on screen.

    The decided behaviour is the second of the two the issue allows: replay, not
    refusal. The refresh reads the interpreter at either level and replays it as
    `--python`, which is what `agentic-hil upgrade` already prints for the same
    receipt, so the reinstall keeps the extras, the `--with` packages and the
    interpreter alike, and it can still merge the `[can]` this run asks for,
    which the preserving `tool upgrade` never adds. The older spelling under
    `[tool.options]` used to be refused outright and sent to that upgrade, which
    kept the record but never repaired the copy; it is replayed now too, and the
    line that says so is on screen.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    result, invocations, receipt, pycan_marker, marker = _run_uv_refresh(tmp_path, recorded_receipt, stub=_uv_refresh_interpreter_stub)

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    reinstalls = _reinstall_lines(invocations)
    assert len(reinstalls) == 1, invocations
    assert "--python 3.10" in reinstalls[0], invocations
    assert "tool upgrade" not in invocations, invocations
    assert RECORDED_INTERPRETER_SAID in transcript, transcript
    # The repair happened out of the reconstruction, not out of the fallback.
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "fresh", transcript
    # And what uv recorded afterwards still names the interpreter and the extra.
    recorded = receipt.read_text(encoding="utf-8")
    assert 'python = "3.10"' in recorded, recorded
    assert '"can"' in recorded, recorded
    assert pycan_marker.is_file(), transcript


def test_refreshing_a_uv_tool_with_an_interpreter_and_a_with_replays_both(tmp_path: Path) -> None:
    """A recorded `--with` beside the interpreter: the line carries both flags.

    The with-replay and the interpreter-replay are two readings of one receipt,
    and a reader that took the interpreter out of the requirement list's place
    or the other way round would keep one and drop the other.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    result, invocations, receipt, pycan_marker, marker = _run_uv_refresh(tmp_path, _RECEIPT_WITH_AN_INTERPRETER_AND_A_WITH, stub=_uv_refresh_interpreter_stub)

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    reinstalls = _reinstall_lines(invocations, "agentic-hil[can,pyocd]")
    assert len(reinstalls) == 1, invocations
    assert "--with requests==2.32.5" in reinstalls[0], invocations
    assert "--python 3.10" in reinstalls[0], invocations
    assert "tool upgrade" not in invocations, invocations
    assert marker.read_text(encoding="utf-8").strip() == "fresh", transcript
    recorded = receipt.read_text(encoding="utf-8")
    assert 'python = "3.10"' in recorded, recorded
    assert '"pyocd"' in recorded and '"can"' in recorded, recorded
    assert '"requests"' in recorded and "==2.32.5" in recorded, recorded
    assert pycan_marker.is_file(), transcript


def test_a_recorded_interpreter_does_not_make_a_recorded_index_replayable(tmp_path: Path) -> None:
    """The neighbour that must not move: a `[tool.options]` index is still refused.

    Reading `python` out of `[tool.options]` must not turn the table into one
    the reconstruction accepts. An index recorded beside the interpreter cannot
    be replayed, so the refresh keeps to the preserving upgrade, which keeps the
    interpreter along with everything else.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    result, invocations, receipt, pycan_marker, marker = _run_uv_refresh(
        tmp_path,
        "[tool]\n"
        'requirements = [{ name = "agentic-hil", extras = ["can"] }]\n'
        'python = "3.10"\n'
        "\n"
        "[tool.options]\n"
        'index = ["https://buildbot:tok3n@packages.example.internal/simple/"]\n',
        stub=_uv_refresh_interpreter_stub,
    )

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert "tool upgrade --reinstall agentic-hil" in invocations, invocations
    assert _reinstall_lines(invocations) == [], invocations
    assert marker.read_text(encoding="utf-8").strip() == "damaged", transcript
    assert 'python = "3.10"' in receipt.read_text(encoding="utf-8")
    # Nothing was replayed, so nothing says it was.
    assert "records the interpreter" not in transcript, transcript


def test_refreshing_a_uv_tool_with_an_empty_receipt_falls_back_to_the_upgrade(tmp_path: Path) -> None:
    """An empty receipt has no `requirements = [` anchor, so the reader must return
    failure rather than accept it as `no extras` and reinstall a bare `agentic-hil`
    that drops whatever the tool actually carried. The refresh falls back to the
    preserving upgrade.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    result, invocations, receipt, pycan_marker, marker = _run_uv_refresh(tmp_path, "")

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert "tool upgrade --reinstall agentic-hil" in invocations, invocations
    assert "tool install --upgrade --reinstall agentic-hil" not in invocations, invocations
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "damaged", transcript


def test_refreshing_a_uv_tool_with_a_truncated_receipt_falls_back_to_the_upgrade(tmp_path: Path) -> None:
    """A receipt whose requirements array never closes is a partial read, so the
    reader must return failure rather than reconstruct from the objects it did see.
    Before this fix the awk `END` block ran `process` on the unclosed array; now a
    never-closed array falls back to the preserving upgrade.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    result, invocations, receipt, pycan_marker, marker = _run_uv_refresh(
        tmp_path,
        "requirements = [\n"
        '    { name = "agentic-hil", extras = ["can", "pyocd"] }\n',
    )

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert "tool upgrade --reinstall agentic-hil" in invocations, invocations
    assert "tool install --upgrade --reinstall agentic-hil[" not in invocations, invocations
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "damaged", transcript


def test_both_scripts_refuse_a_receipt_they_cannot_replay_in_full() -> None:
    """The preserving-fallback contract, pinned on both scripts.

    A reconstruction that would change what uv recorded must be refused so the
    caller keeps to the upgrade that preserves it. The PowerShell side has no
    interpreter in every checkout, so this static check is its regression guard: it
    validates the root requirement, refuses a recorded `[tool.options]`, and reaches
    the fallback on an empty or unreadable receipt rather than terminating or
    reconstructing from a partial read. The shell side is exercised end to end
    above; the same guards are asserted here for symmetry.
    """
    shell = _code_only(_shell_source())
    powershell = _code_only(_powershell_source())

    # The root requirement is validated too, not just the non-root ones.
    assert "root_ok" in shell, shell
    assert re.search(r"if \(\$k -ne 'name' -and \$k -ne 'extras'\) \{ return \$null \}", powershell), powershell
    # A recorded tool option refuses the reconstruction (the dot is escaped in
    # both scripts' matchers, so the literal in the code is `tool\.options`).
    assert "tool\\.options" in shell, shell
    assert "tool\\.options" in powershell, powershell
    # An unreadable receipt is caught rather than terminating the run, and an empty
    # one is refused before it is indexed as a string.
    assert re.search(r"try \{\s*\$text = Get-Content -LiteralPath \$receipt -Raw\s*\} catch \{\s*return \$null", powershell), powershell
    assert "[string]::IsNullOrEmpty($text)" in powershell, powershell


def _powershell_function(source: str, name: str) -> str:
    """One `function Name { ... }` of install.ps1, closed at the first `}` in column 0."""
    found = re.search(rf"^function {re.escape(name)} \{{\n.*?^\}}\n", source, re.MULTILINE | re.DOTALL)
    assert found is not None, f"install.ps1 defines no {name}"
    return found.group(0)


# The functions install.ps1 reaches on a uv-managed refresh, from the decision
# down to the uv line, in the order the script defines them. `Invoke-Captured`
# is deliberately not among them: the harness below supplies one that records
# what uv was asked and answers the two probes, so the decision is driven
# against a receipt on disk without a uv on the machine. A helper added between
# the reader and `Install-WithUv` has to be added here too, or the harness
# fails on a name it does not know rather than on an assertion.
_POWERSHELL_REFRESH_FUNCTIONS = ("Invoke-Uv", "Test-UvManagesTool", "Get-UvRecordedRequirements", "Get-RefreshSpec", "Install-WithUv")


def _run_powershell_uv_refresh(tmp_path: Path, recorded_receipt: str) -> tuple[str, str]:
    """Drive install.ps1's refresh decision in Windows PowerShell against a receipt.

    `Install-WithUv` and the functions it reads through are taken out of the
    script verbatim and run with the script-scope state a refresh has (`refresh`
    mode, the `can` extra wanted, no version pin). Returns the uv invocation
    log, one line per call, the way the shell harness returns its own, and what
    the script said on the way, one `Write-Say` per line.
    """
    powershell = _windows_powershell()
    tool_root = tmp_path / "tools"
    receipt_dir = tool_root / "agentic-hil"
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "uv-receipt.toml").write_text(recorded_receipt, encoding="utf-8")
    log = tmp_path / "uv-invocations"
    said = tmp_path / "said"
    functions = "".join(_powershell_function(_powershell_source(), name) for name in _POWERSHELL_REFRESH_FUNCTIONS)
    harness = (
        "$ErrorActionPreference = 'Stop'\n"
        f"$toolRoot = '{tool_root}'\n"
        f"$log = '{log}'\n"
        f"$said = '{said}'\n"
        "$InstallMode = 'refresh'\n"
        "$WithCan = $true\n"
        "$Version = ''\n"
        "$SystemCertsMode = 'never'\n"
        "$UvInstallFailure = 'uv could not install agentic-hil'\n"
        "function Write-Say { param([string]$Text) Add-Content -LiteralPath $said -Value $Text -Encoding utf8 }\n"
        "function Invoke-Captured {\n"
        "    param([string]$File, [string[]]$Arguments)\n"
        "    Add-Content -LiteralPath $log -Value ($Arguments -join ' ') -Encoding utf8\n"
        "    if ($Arguments[0] -eq 'tool' -and $Arguments[1] -eq 'dir') { return @{ ExitCode = 0; Output = \"$toolRoot`n\" } }\n"
        "    if ($Arguments[0] -eq 'tool' -and $Arguments[1] -eq 'list') { return @{ ExitCode = 0; Output = \"agentic-hil v0.1.0`n- agentic-hil`n\" } }\n"
        "    return @{ ExitCode = 0; Output = '' }\n"
        "}\n"
        f"{functions}"
        "Install-WithUv\n"
    )
    script = tmp_path / "refresh-harness.ps1"
    script.write_text(harness, encoding="utf-8")

    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    invocations = log.read_text(encoding="utf-8-sig") if log.is_file() else ""
    return invocations, (said.read_text(encoding="utf-8-sig") if said.is_file() else "")


@pytest.mark.parametrize("recorded_receipt", _RECORDED_INTERPRETER_RECEIPTS)
def test_the_powershell_refresh_replays_the_recorded_interpreter(tmp_path: Path, recorded_receipt: str) -> None:
    """#476 on the PowerShell side: `Get-UvRecordedRequirements` had the same gap.

    The same receipt, the same decision, the same line: the reconstruction is
    taken, and it carries `--python 3.10`, at either level the interpreter was
    recorded at, and the script says so. There is no real-uv tier for this
    installer, since the image is Linux; this harness is its coverage.
    """
    invocations, said = _run_powershell_uv_refresh(tmp_path, recorded_receipt)

    reinstalls = _reinstall_lines(invocations)
    assert len(reinstalls) == 1, invocations
    assert "--python 3.10" in reinstalls[0], invocations
    assert "tool upgrade" not in invocations, invocations
    assert RECORDED_INTERPRETER_SAID in said, said


def test_the_powershell_refresh_replays_an_interpreter_beside_a_with(tmp_path: Path) -> None:
    """The PowerShell twin of the two-flag case: `--with` and `--python` on one line."""
    invocations, _said = _run_powershell_uv_refresh(tmp_path, _RECEIPT_WITH_AN_INTERPRETER_AND_A_WITH)

    reinstalls = _reinstall_lines(invocations, "agentic-hil[can,pyocd]")
    assert len(reinstalls) == 1, invocations
    assert "--with requests==2.32.5" in reinstalls[0], invocations
    assert "--python 3.10" in reinstalls[0], invocations
    assert "tool upgrade" not in invocations, invocations


def test_the_powershell_refresh_still_keeps_to_the_upgrade_for_a_recorded_index(tmp_path: Path) -> None:
    """The neighbour on the PowerShell side: an index beside the interpreter is still refused."""
    invocations, said = _run_powershell_uv_refresh(
        tmp_path,
        "[tool]\n"
        'requirements = [{ name = "agentic-hil", extras = ["can"] }]\n'
        'python = "3.10"\n'
        "\n"
        "[tool.options]\n"
        'index = ["https://buildbot:tok3n@packages.example.internal/simple/"]\n',
    )

    assert "tool upgrade --reinstall agentic-hil" in invocations, invocations
    assert _reinstall_lines(invocations) == [], invocations
    assert "records the interpreter" not in said, said


def test_refreshing_a_pip_installation_forces_the_reinstall(tmp_path: Path) -> None:
    """The pip half of the same repair, run end to end through a POSIX shell.

    `pip install --user --upgrade` reports a current requirement already satisfied
    and replaces no files, so a refresh on the pip path carries --force-reinstall.
    The interpreter stub writes the repaired copy only for that flag, so a "fresh"
    marker is proof the flag reached pip and the damaged copy was replaced.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")
    shell = _posix_shell()

    release = _release()
    home = tmp_path / "home"
    project = home / "project"
    early_bin = tmp_path / "early-bin"  # the fake python and claude
    scripts_dir = tmp_path / "py-scripts"  # what the interpreter reports as its scripts path
    for directory in (project, early_bin, scripts_dir):
        directory.mkdir(parents=True)

    marker = tmp_path / "who-ran-agent-install"
    pip_log = tmp_path / "pip-invocations"

    # The copy already on PATH, in the interpreter's own scripts directory: it is
    # current but broken, answering --version yet writing "damaged" from
    # agent-install. The refresh has to replace it.
    _stub_executable(
        scripts_dir / "agentic-hil",
        'case "$1" in\n'
        f'  --version) echo "{release}" ;;\n'
        f'  agent-install) echo "damaged" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
        "esac\n"
        "exit 0\n",
    )
    _stub_executable(early_bin / "claude", "exit 0\n")
    _stub_executable(
        early_bin / "python3",
        'case "$*" in\n'
        "  *version_info*) exit 0 ;;\n"
        f'  *posix_user*) echo "{scripts_dir}"; exit 0 ;;\n'
        '  *"pip install"*)\n'
        f'    echo "$*" >> "{pip_log}"\n'
        # Without --force-reinstall pip leaves the already-current copy in place,
        # so the damaged one survives; the stub models exactly that.
        '    case "$*" in\n'
        "      *--force-reinstall*) : ;;\n"
        "      *) exit 0 ;;\n"
        "    esac\n"
        f'    cat > "{scripts_dir}/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        f'  --version) echo "{release}" ;;\n'
        f'  agent-install) echo "fresh" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
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
    }

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--no-can"],
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
    assert f"agentic-hil {release} is here and not older than {release}, refreshing this current installation" in transcript, transcript
    assert pip_log.is_file(), transcript
    invocations = pip_log.read_text(encoding="utf-8")
    assert "pip install --user --upgrade --force-reinstall agentic-hil" in invocations, invocations
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "fresh", transcript


def _release_window_run(
    tmp_path: Path,
    *,
    served: str,
    pin: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """A fresh machine, an index serving one version, one install run through `sh`.

    Nothing of this project is on the machine, so step 1 takes the "no agentic-hil
    on this PATH" branch and step 2 installs. The stub uv is the index: it reports
    its destination with `tool dir --bin` the way the real one does, and `tool
    install` writes a console script reporting `served` into it. That directory is
    on PATH here, the way `~/.local/bin` is on a real machine, so step 3 has the
    ordinary shape rather than the off-PATH one.

    The stub writes the one version it has whatever spec it is handed. A real index
    that cannot serve a pin fails the install itself, at step 2; keeping the stub
    going past that is what lets a pinned run reach step 3, where the check under
    test is the one that answers.

    Returns the finished run, the marker file the machine half writes, and the
    directory the manager reported.
    """
    shell = _posix_shell()

    home = tmp_path / "home"
    project = home / "project"
    tools = tmp_path / "tools"
    uv_bin = home / ".local" / "bin"
    for directory in (project, tools, uv_bin):
        directory.mkdir(parents=True)

    marker = tmp_path / "who-ran-agent-install"

    # A stub claude, so agent detection has a claude-code to register for.
    _stub_executable(tools / "claude", "exit 0\n")
    _stub_executable(
        tools / "uv",
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        f'  echo "{uv_bin}"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        f'  cat > "{uv_bin}/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        f'  --version) echo "{served}" ;;\n'
        f'  agent-install) echo "fresh" > "{marker}"; printf \'{{\\n  "ok": true\\n}}\\n\' ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        f'  chmod +x "{uv_bin}/agentic-hil"\n'
        "fi\n"
        "exit 0\n",
    )

    arguments = [shell, str(SHELL_SCRIPT), "--no-can"]
    if pin is not None:
        arguments += ["--version", pin]

    result = subprocess.run(
        arguments,
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"HOME": str(home), "PATH": f"{tools}:{uv_bin}:/usr/bin:/bin"},
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )
    return result, marker, uv_bin


def test_a_fresh_install_during_a_release_window_is_not_refused(tmp_path: Path) -> None:
    """The release window, run end to end through a POSIX shell.

    A release commit lands on master and the one-line installer host mirrors it
    within the hour, but PyPI has not published yet, so the index still serves the
    release below. Step 3 asked the fresh copy in the manager's own bin to be at
    least `RELEASE` before it would accept it, so it refused the copy the manager had
    just written, step 4 stopped with "the machine half was not run against a
    possibly stale PATH copy", and a completely correct install read as a broken
    machine to everyone who ran it during the window (#310).

    The floor never protected anything here: step 2 installed with `--upgrade` into
    the directory the manager itself names, so what answers there is this run's own
    work whatever number it reports. The directory is the proof now, and the install
    goes through.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    release = _release()
    served = _the_release_below(release)
    assert tuple(int(part) for part in served.split(".")) < tuple(int(part) for part in release.split(".")), (
        f"the window only exists while the index serves less than {release}"
    )

    result, marker, uv_bin = _release_window_run(tmp_path, served=served)

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert "does not resolve here" not in transcript, transcript
    # Step 3 resolved the manager's own copy, and named the directory it sits in.
    assert f"agentic-hil is installed in {uv_bin}" in transcript, transcript
    # Step 4 was reached, and reached through that copy rather than a bare name.
    assert "agent: registering the skill and the MCP server for claude-code" in transcript, transcript
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "fresh", transcript


def test_a_pinned_run_in_a_release_window_still_gets_the_version_it_named(tmp_path: Path) -> None:
    """The pinned mirror of the window: the exact check is untouched by the fix.

    An operator who names a release has named it, and step 3 still proves that
    exactly. Pinning the release the index serves inside the window succeeds, the
    same as an unpinned run in the same window does. Pinning one the index cannot
    serve fails, loudly, without the machine half running: on a real index the
    install itself would already have failed, and even past that the copy in the
    manager's bin is not the release this run asked for.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    served = _the_release_below(_release())

    matched, matched_marker, uv_bin = _release_window_run(tmp_path / "matched", served=served, pin=served)
    transcript = f"{matched.stdout}{matched.stderr}"
    assert matched.returncode == 0, transcript
    assert f"agentic-hil is installed in {uv_bin}" in transcript, transcript
    assert matched_marker.is_file(), transcript
    assert matched_marker.read_text(encoding="utf-8").strip() == "fresh", transcript

    unserved, unserved_marker, _ = _release_window_run(tmp_path / "unserved", served=served, pin=_release())
    transcript = f"{unserved.stdout}{unserved.stderr}"
    assert unserved.returncode != 0, transcript
    assert "does not resolve here" in transcript, transcript
    assert not unserved_marker.exists(), transcript


def test_neither_script_offers_a_way_to_disable_certificate_verification() -> None:
    """A proxied network is answered by a trust store, never by a switch.

    `--system-certs` exists because uv validates against roots bundled in its own
    binary and a TLS-intercepting proxy is not in them. The neighbouring switches
    that make the same symptom go away do it by not checking, and one of them
    reaching a script a stranger pipes into a shell would be the whole promise of
    these two files gone. Comments are stripped first, so the comment that names
    what must never be passed is not read as passing it.
    """
    for name, code in _both_code().items():
        for forbidden in ("--allow-insecure-host", "--trusted-host", "--insecure", "-SkipCertificateCheck", "ServerCertificateValidationCallback"):
            assert forbidden not in code, f"{name} carries {forbidden}"


def test_both_scripts_point_the_package_manager_at_the_system_store() -> None:
    """The flag sets the variable rather than passing a switch to one command.

    `agentic-hil upgrade` shells out to `uv tool upgrade` and passes no TLS flags
    of its own, so a proxied host whose install command carried the switch would
    lose the upgrade path the next time. An exported variable is inherited by
    both, which is the shape TROUBLESHOOTING.md already recommends.
    """
    shell = _code_only(_shell_source())
    assert "UV_SYSTEM_CERTS" in shell
    assert "export UV_SYSTEM_CERTS" in shell
    # pip takes a file rather than a switch, so the flag has to find one.
    assert "PIP_CERT" in shell
    powershell = _code_only(_powershell_source())
    assert "UV_SYSTEM_CERTS" in powershell


def test_the_system_certs_flag_reaches_the_package_manager(tmp_path: Path) -> None:
    """`--system-certs`, run end to end through a POSIX shell.

    A static read can show the variable is set somewhere in the file. It cannot
    show that the process which needs it is started after that, and inherits it.
    The stub uv records what it was given.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")
    shell = _posix_shell()

    home = tmp_path / "home"
    project = home / "project"
    uv_bin = tmp_path / "uv-tools" / "bin"
    tools = tmp_path / "tools"
    for directory in (project, uv_bin, tools):
        directory.mkdir(parents=True)

    seen = tmp_path / "what-uv-was-given"

    _stub_executable(
        tools / "uv",
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        '  echo "$UV_TOOL_BIN_DIR"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        f'  echo "${{UV_SYSTEM_CERTS:-unset}}" > "{seen}"\n'
        '  cat > "$UV_TOOL_BIN_DIR/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        '  --version) echo "99.0.0" ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        '  chmod +x "$UV_TOOL_BIN_DIR/agentic-hil"\n'
        "fi\n"
        "exit 0\n",
    )

    env = {
        "HOME": str(home),
        "PATH": f"{tools}:/usr/bin:/bin",
        "UV_TOOL_BIN_DIR": str(uv_bin),
    }

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--system-certs", "--no-can", "--no-agent-install"],
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
    assert seen.is_file(), transcript
    assert seen.read_text(encoding="utf-8").strip() == "1", transcript
    assert "certificates: uv" in transcript, transcript


def _proxied_uv_stub(attempts: Path) -> str:
    """A uv that fails the way a TLS-intercepting proxy makes it fail.

    It records what it was given on each attempt, refuses the install while it
    is validating against roots of its own, and writes the console script once
    it is pointed at the machine's store.
    """
    return (
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        '  echo "$UV_TOOL_BIN_DIR"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        f'  echo "${{UV_SYSTEM_CERTS:-unset}}" >> "{attempts}"\n'
        '  if [ -z "${UV_SYSTEM_CERTS:-}" ]; then\n'
        '    echo "error: Failed to fetch: https://pypi.org/simple/agentic-hil/" >&2\n'
        '    echo "  Caused by: invalid peer certificate: UnknownIssuer" >&2\n'
        "    exit 2\n"
        "  fi\n"
        '  cat > "$UV_TOOL_BIN_DIR/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        '  --version) echo "99.0.0" ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        '  chmod +x "$UV_TOOL_BIN_DIR/agentic-hil"\n'
        "fi\n"
        "exit 0\n"
    )


def test_both_scripts_recognise_a_trust_failure_by_its_own_words() -> None:
    """The retry is decided by what the failure says, not by trying twice always.

    A second attempt against a different set of roots is only ever an answer to
    a chain that ended outside the first set. Anything else that can fail an
    install, a proxy refusing the host, an index that is down, a version that
    does not exist, is not helped by it and must not spend a second attempt on
    it. Both scripts carry the words each manager uses for the one case.
    """
    for name, code in _both_code().items():
        assert "invalid peer certificate" in code, name
        assert "UnknownIssuer" in code, name
        assert "certificate verify failed" in code, name


def test_a_certificate_failure_is_retried_against_the_system_store(tmp_path: Path) -> None:
    """The proxied machine, run end to end through a POSIX shell.

    The person typing the line does not know that uv carries its own roots, and
    should not have to. The failed attempt is the detection: uv comes back
    saying the chain ended outside them, and the second attempt validates
    against the store this machine already trusts for everything else. The stub
    records what it was given on each attempt.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")
    shell = _posix_shell()

    home = tmp_path / "home"
    project = home / "project"
    uv_bin = tmp_path / "uv-tools" / "bin"
    tools = tmp_path / "tools"
    for directory in (project, uv_bin, tools):
        directory.mkdir(parents=True)

    attempts = tmp_path / "attempts"

    _stub_executable(tools / "uv", _proxied_uv_stub(attempts))

    env = {
        "HOME": str(home),
        "PATH": f"{tools}:/usr/bin:/bin",
        "UV_TOOL_BIN_DIR": str(uv_bin),
    }

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--no-can", "--no-agent-install"],
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
    assert attempts.read_text(encoding="utf-8").split() == ["unset", "1"], transcript
    assert "retrying once against this machine's own store" in transcript, transcript


def test_the_retry_is_refused_when_it_was_told_to_be(tmp_path: Path) -> None:
    """`--no-system-certs` keeps the install on uv's own roots and lets it fail.

    The automatic retry reaches for a different set of trust anchors, and an
    operator who wants that decision made by nobody but themselves has to be
    able to say so. Then the install fails, and the refusal says which flag
    stopped the second attempt rather than leaving the reader with uv's message
    alone.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")
    shell = _posix_shell()

    home = tmp_path / "home"
    project = home / "project"
    uv_bin = tmp_path / "uv-tools" / "bin"
    tools = tmp_path / "tools"
    for directory in (project, uv_bin, tools):
        directory.mkdir(parents=True)

    attempts = tmp_path / "attempts"

    _stub_executable(tools / "uv", _proxied_uv_stub(attempts))

    env = {
        "HOME": str(home),
        "PATH": f"{tools}:/usr/bin:/bin",
        "UV_TOOL_BIN_DIR": str(uv_bin),
    }

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--no-system-certs", "--no-can", "--no-agent-install"],
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
    assert attempts.read_text(encoding="utf-8").split() == ["unset"], transcript
    assert "--no-system-certs" in transcript, transcript


def _recording_uv_stub(seen: Path) -> str:
    """A uv that always succeeds and records the two cert variables it inherited.

    Real uv reads only UV_SYSTEM_CERTS, but a shell stub can read any variable in
    its environment, so it records PIP_CERT too: --no-system-certs clears both
    before the manager runs, and this is where that clearing is observed.
    """
    return (
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        '  echo "$UV_TOOL_BIN_DIR"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        f'  echo "${{UV_SYSTEM_CERTS:-unset}} ${{PIP_CERT:-unset}}" > "{seen}"\n'
        '  cat > "$UV_TOOL_BIN_DIR/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        '  --version) echo "99.0.0" ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        '  chmod +x "$UV_TOOL_BIN_DIR/agentic-hil"\n'
        "fi\n"
        "exit 0\n"
    )


def _no_system_certs_run(tmp_path: Path, pip_cert: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run install.sh with --no-system-certs and the two cert variables already set.

    The environment starts the way a host does that took TROUBLESHOOTING.md's
    advice and exported UV_SYSTEM_CERTS for future upgrades. Returns the process
    and the file the stub uv wrote the variables it saw into.
    """
    shell = _posix_shell()
    home = tmp_path / "home"
    project = home / "project"
    uv_bin = tmp_path / "uv-tools" / "bin"
    tools = tmp_path / "tools"
    for directory in (project, uv_bin, tools):
        directory.mkdir(parents=True)
    seen = tmp_path / "what-uv-inherited"
    _stub_executable(tools / "uv", _recording_uv_stub(seen))
    env = {
        "HOME": str(home),
        "PATH": f"{tools}:/usr/bin:/bin",
        "UV_TOOL_BIN_DIR": str(uv_bin),
        "UV_SYSTEM_CERTS": "1",
        "PIP_CERT": pip_cert,
    }
    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--no-system-certs", "--no-can", "--no-agent-install"],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )
    return result, seen


def test_no_system_certs_clears_an_inherited_machine_store_override(tmp_path: Path) -> None:
    """The flag has to override the environment, not merely decline to set it.

    A host that exported UV_SYSTEM_CERTS for future upgrades, and a PIP_CERT that
    already points at this machine's own bundle, would otherwise have uv and pip
    reading that store even as the operator passes --no-system-certs. The stub uv
    records what it inherited; both must be gone.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    result, seen = _no_system_certs_run(tmp_path, pip_cert="/etc/ssl/cert.pem")

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert seen.read_text(encoding="utf-8").split() == ["unset", "unset"], transcript
    assert "cleared the inherited UV_SYSTEM_CERTS" in transcript, transcript
    assert "cleared the inherited system-bundle PIP_CERT" in transcript, transcript


def test_no_system_certs_keeps_a_pip_cert_the_operator_chose_for_themselves(tmp_path: Path) -> None:
    """"Never reach for this machine's store" is not "throw away my own bundle."

    A PIP_CERT that names a file of the operator's, rather than this machine's
    system bundle, is their deliberate choice and not the reach the flag refuses.
    The inherited UV_SYSTEM_CERTS still goes, because that one does point uv at
    the machine store.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    own_bundle = tmp_path / "my-proxy-ca.pem"
    own_bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")

    result, seen = _no_system_certs_run(tmp_path, pip_cert=str(own_bundle))

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert seen.read_text(encoding="utf-8").split() == ["unset", str(own_bundle)], transcript
    assert "cleared the inherited system-bundle PIP_CERT" not in transcript, transcript


def test_both_scripts_clear_an_inherited_machine_store_override_when_refused() -> None:
    """Both installers, not just the one this platform can run end to end.

    The PowerShell flow is not exercised as a subprocess here, so its half of the
    contract is pinned structurally: --no-system-certs clears the inherited
    UV_SYSTEM_CERTS rather than only declining to set it.
    """
    shell = _code_only(_shell_source())
    assert "unset UV_SYSTEM_CERTS" in shell
    # And the clearing is what "never" reaches, not the "always" path.
    assert re.search(r'SYSTEM_CERTS"?\s*=\s*"never"[^\n]*\n\s*clear_system_certs', shell), shell

    powershell = _code_only(_powershell_source())
    assert r"Remove-Item Env:\UV_SYSTEM_CERTS" in powershell
    assert re.search(r"SystemCertsMode -eq 'never'[^\n]*Clear-SystemCerts", powershell), powershell


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

# Typed in the home directory, which is where an install line is actually typed
# and where every release from 0.15.0 on accepts it: the boundary collapses at
# home rather than swallowing it (#235), so this run exercises the case the fix
# exists for instead of stepping around it (#244).
cd "$HOME"

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
    # Step 4 says what happened in one line, and the report it read stays unprinted
    # on a success. The document's own tool name is the thing that would give a
    # streamed dump away.
    assert f"agent: claude-code {REGISTERED_LINE}" in transcript
    assert "agentic_hil_agent_install" not in transcript


# ---------------------------------------------------------------------------
# #488: what the stubs assumed about uv and about Astral's installer, and what
# the real ones do. The container tier runs both for real in
# tests/container/test_install_routes.py; these are the unit-fake tier's
# recordings of the same two facts, so the suite that runs everywhere keeps them.

# uv 0.12.9, recorded 2026-09-06 in the container test image, on `uv tool install
# agentic-hil` with a console script pip had written in uv's own bin directory.
# uv installed every package first, then stopped on this line and exited 2.
UV_REFUSES_AN_EXISTING_EXECUTABLE = "error: Executable already exists: agentic-hil (use `--force` to overwrite)"


def test_the_anchor_replaces_a_copy_uv_did_not_write_in_its_own_bin(tmp_path: Path) -> None:
    """A `pip install --user` copy in `~/.local/bin`, then uv arrives, then the anchor.

    pip's console script and uv's own bin are the same directory on Linux and
    macOS, and uv refuses to overwrite an executable it did not write. The stub
    uv here keeps uv's rule: it refuses with uv's recorded line unless it is
    told to replace the file. Before this fix the anchor stopped at step 2 on
    that line, with the old copy still on PATH and a closing sentence naming no
    fix; the run has to end with the fresh copy answering in that directory.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")
    shell = _posix_shell()

    home = tmp_path / "home"
    project = home / "project"
    user_bin = home / ".local" / "bin"
    tools = tmp_path / "tools"
    for directory in (project, user_bin, tools):
        directory.mkdir(parents=True)
    uv_wrote = user_bin / "agentic-hil.written-by-uv"

    # The copy pip wrote: an old release, and not a file uv knows.
    _stub_executable(user_bin / "agentic-hil", 'case "$1" in\n  --version) echo "0.3.0" ;;\nesac\nexit 0\n')
    _stub_executable(
        tools / "uv",
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        f'  echo "{user_bin}"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "list" ]; then\n'
        '  echo "No tools installed"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        f'  if [ -e "{user_bin}/agentic-hil" ] && [ ! -e "{uv_wrote}" ]; then\n'
        '    case " $* " in\n'
        '      *" --force "*) ;;\n'
        f"      *) echo '{UV_REFUSES_AN_EXISTING_EXECUTABLE}' >&2; exit 2 ;;\n"
        "    esac\n"
        "  fi\n"
        f'  cat > "{user_bin}/agentic-hil" <<STUB\n'
        "#!/bin/sh\n"
        'case "\\$1" in\n'
        '  --version) echo "99.0.0" ;;\n'
        "esac\n"
        "exit 0\n"
        "STUB\n"
        f'  chmod +x "{user_bin}/agentic-hil"\n'
        f'  : > "{uv_wrote}"\n'
        "fi\n"
        "exit 0\n",
    )

    result = subprocess.run(
        [shell, str(SHELL_SCRIPT), "--no-agent-install", "--no-can"],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"HOME": str(home), "PATH": f"{tools}:{user_bin}:/usr/bin:/bin"},
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert f"agentic-hil 0.3.0 is older than {_release()}, upgrading it" in transcript, transcript
    assert "could not install" not in transcript, transcript
    answered = subprocess.run([str(user_bin / "agentic-hil"), "--version"], capture_output=True, text=True, timeout=SCRIPT_TIMEOUT_S, check=False)
    assert answered.stdout.strip() == "99.0.0", transcript


def test_the_fetched_uv_installer_is_told_not_to_edit_shell_profiles(tmp_path: Path) -> None:
    """The fetch route keeps the promise in the header, by saying so to the installer.

    Astral's installer appends to `~/.profile` and `~/.bashrc` and creates
    `~/.zshrc` unless `UV_NO_MODIFY_PATH` is set, and the five lines a stranger
    reads before piping this script into a shell say it touches no shell rc
    file. The stub installer here records what it was told; the real one runs in
    the container tier and its rc files are read back there.
    """
    if os.name != "posix":
        pytest.skip("the shell install flow is exercised on the POSIX half")

    env, project, _marker, _uv_log, fetched = _machine_whose_only_python_is(tmp_path, _PYTHON_WITHOUT_PIP)
    result = _install_on(env, project)

    transcript = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, transcript
    assert fetched.is_file(), transcript
    seen = tmp_path / "installer-saw-no-modify-path"
    assert seen.is_file(), transcript
    assert seen.read_text(encoding="utf-8").strip() == "1", transcript


def test_both_scripts_tell_the_fetched_uv_installer_not_to_edit_profiles() -> None:
    """The same instruction on the Windows side, where the installer edits the user Path.

    The pinned install.ps1 writes `%USERPROFILE%\\.local\\bin` into the user's
    Path in the registry unless `UV_NO_MODIFY_PATH` is set, and step 3 then
    prints a line that prepends the same directory again. Running the real
    installer on a developer's Windows machine would edit that developer's
    registry, so the Windows half is pinned on the text: the variable is set,
    and it is set before the line that executes the fetched bytes.
    """
    shell = _code_only(_shell_source())
    assert "UV_NO_MODIFY_PATH" in shell
    assert shell.index("UV_NO_MODIFY_PATH") < shell.index('sh "$installer_path"'), "install.sh sets the variable after it has already run the installer"
    powershell = _code_only(_powershell_source())
    assert "UV_NO_MODIFY_PATH" in powershell
    assert powershell.index("UV_NO_MODIFY_PATH") < powershell.index("Invoke-Expression ([Text.Encoding]"), "install.ps1 sets the variable after it has already run the installer"


# ---------------------------------------------------------------------------
# install.ps1, run whole, on the one platform that has Windows PowerShell 5.1.
#
# Every behavioural test above runs install.sh and skips on Windows, and the
# PowerShell script was executed only to parse it, print its help and refuse an
# unknown flag; its equivalence to the shell script was pinned by regexes over
# the two texts, which is the assumption that identical wording means identical
# behaviour, and the shape #462 broke for install.sh on a bench. The harness
# here runs `install.ps1` itself, through `powershell.exe -File`, against a
# stub uv on PATH and a real `agentic-hil.exe` in the manager's bin, built the
# way pip builds one. What it cannot reach is written down at the end.

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="install.ps1's flow runs under Windows PowerShell 5.1, which exists on Windows alone")

# The refresh over a receipt that records a root extra and a `--with`, in the
# shape uv writes once a `--with` is present: one requirement per line.
_RECEIPT_WITH_PYOCD_AND_A_WITH = (
    "[tool]\n"
    "requirements = [\n"
    '    { name = "agentic-hil", extras = ["pyocd"] },\n'
    '    { name = "requests", specifier = "==2.32.5" },\n'
    "]\n"
    "entrypoints = [\n"
    '    { name = "agentic-hil", install-path = "C:\\\\x\\\\agentic-hil.exe", from = "agentic-hil" },\n'
    "]\n"
)


def _powershell_release() -> str:
    found = re.search(r"^\$Release = '(\d+\.\d+\.\d+)'$", _powershell_source(), re.MULTILINE)
    assert found is not None, "install.ps1 states no $Release"
    return found.group(1)


def _windows_launcher(path: Path, version: str, marker: Path | None = None, answers: str = "fresh") -> None:
    """A real `agentic-hil.exe` answering `version`, built the way pip builds one.

    pip's console-script launcher is a small executable followed by a shebang
    line and a zip archive whose `__main__` is what runs. The same three parts,
    with this interpreter on the shebang and a `__main__` of the test's own,
    give install.ps1 the `.exe` it looks for by that name: `Get-Command`, the
    call operator and `Test-Path` all see an executable, and `--version` and
    `agent-install` answer what the test decided.
    """
    from pip._vendor import distlib

    stub = {"AMD64": "t64.exe", "ARM64": "t64-arm.exe", "x86": "t32.exe"}[platform.machine()]
    launcher = (Path(distlib.__file__).parent / stub).read_bytes()
    body = (
        "import sys\n"
        "from pathlib import Path\n"
        f"if '--version' in sys.argv:\n    print({version!r})\n    raise SystemExit(0)\n"
        f"if 'agent-install' in sys.argv:\n    Path({str(marker)!r}).write_text({answers!r}, encoding='utf-8')\n    raise SystemExit(0)\n"
        "raise SystemExit(0)\n"
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("__main__.py", body)
    path.write_bytes(launcher + f"#!{sys.executable}\r\n".encode() + archive.getvalue())


_UV_STUB_BODY = '''"""The uv install.ps1 talks to, driven by the plan written beside this file."""
import json
import os
import shutil
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[0]).with_suffix(".json").read_text(encoding="utf-8"))
args = sys.argv[1:]
with open(plan["log"], "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\\n")
if args[:2] == ["tool", "dir"]:
    print(os.environ["UV_TOOL_BIN_DIR"] if "--bin" in args else os.environ["UV_TOOL_DIR"])
    raise SystemExit(0)
if args[:2] == ["tool", "list"]:
    print(plan["tool_list"])
    raise SystemExit(0)
if args[:2] in (["tool", "install"], ["tool", "upgrade"]):
    if plan.get("attempts"):
        with open(plan["attempts"], "a", encoding="utf-8") as attempts:
            attempts.write(os.environ.get("UV_SYSTEM_CERTS", "unset") + "\\n")
    if plan.get("proxied") and not os.environ.get("UV_SYSTEM_CERTS"):
        print("error: Failed to fetch: https://pypi.org/simple/agentic-hil/", file=sys.stderr)
        print("  Caused by: invalid peer certificate: UnknownIssuer", file=sys.stderr)
        raise SystemExit(2)
    if plan.get("writes"):
        shutil.copy2(plan["writes"], Path(os.environ["UV_TOOL_BIN_DIR"]) / "agentic-hil.exe")
    print("Installed 1 executable: agentic-hil.exe", file=sys.stderr)
    raise SystemExit(0)
raise SystemExit(0)
'''


class _WindowsBench:
    """One Windows machine for install.ps1: a stub uv on PATH and a bin of its own.

    `installed` is what an `agentic-hil.exe` already on PATH answers, or None
    for a machine that has none; `manager_writes` is what the copy the stub uv
    installs answers, or None for a uv that installs nothing. The uv is a
    Python script behind a `uv.cmd`, because a batch file cannot read a
    receipt or decide on an environment variable, and the plan it follows is
    written beside it.
    """

    def __init__(self, tmp_path: Path, *, installed: str | None, manager_writes: str | None, tool_list: str = "No tools installed", receipt: str | None = None, proxied: bool = False) -> None:
        self.home = tmp_path / "home"
        self.project = self.home / "project"
        self.early_bin = tmp_path / "early-bin"
        self.uv_bin = tmp_path / "uv-tools" / "bin"
        self.uv_tools = tmp_path / "uv-tools" / "tools"
        self.staging = tmp_path / "staging"
        for directory in (self.project, self.early_bin, self.uv_bin, self.uv_tools, self.staging):
            directory.mkdir(parents=True)
        self.marker = tmp_path / "who-ran-agent-install"
        self.log = tmp_path / "uv-invocations"
        self.attempts = tmp_path / "attempts"
        if installed is not None:
            _windows_launcher(self.uv_bin / "agentic-hil.exe", installed, self.marker, "stale")
        staged = None
        if manager_writes is not None:
            staged = self.staging / "agentic-hil.exe"
            _windows_launcher(staged, manager_writes, self.marker, "fresh")
        if receipt is not None:
            (self.uv_tools / "agentic-hil").mkdir()
            (self.uv_tools / "agentic-hil" / "uv-receipt.toml").write_text(receipt, encoding="utf-8")
        stub = self.early_bin / "uv-stub.py"
        stub.write_text(_UV_STUB_BODY, encoding="utf-8")
        stub.with_suffix(".json").write_text(
            json.dumps({"log": str(self.log), "attempts": str(self.attempts), "tool_list": tool_list, "writes": str(staged) if staged else None, "proxied": proxied}),
            encoding="utf-8",
        )
        (self.early_bin / "uv.cmd").write_text(f'@echo off\r\n"{sys.executable}" "{stub}" %*\r\nexit /b %ERRORLEVEL%\r\n', encoding="utf-8")
        # A claude on PATH, so agent detection has a claude-code to register for
        # where a test lets step 4 run.
        (self.early_bin / "claude.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")

    def environment(self, *, uv_bin_on_path: bool = True) -> dict[str, str]:
        """What the script inherits: Windows itself, this bench, and nothing of the developer's.

        The system directories are what powershell.exe and cmd.exe need to
        start; the two uv variables are what the stub answers `tool dir` with;
        and `PATH` carries the stub's directory ahead of the manager's own bin,
        the way a machine with a uv on PATH is arranged, unless the test is
        about the machine before its first install.
        """
        system_root = os.environ["SYSTEMROOT"]
        temp = self.home / "tmp"
        temp.mkdir(exist_ok=True)
        path = [str(self.early_bin), *([str(self.uv_bin)] if uv_bin_on_path else []), str(Path(system_root) / "System32"), system_root, str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0")]
        return {
            "SYSTEMROOT": system_root,
            "SystemRoot": system_root,
            "WINDIR": system_root,
            "COMSPEC": os.environ.get("COMSPEC", str(Path(system_root) / "System32" / "cmd.exe")),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "PATH": os.pathsep.join(path),
            "USERPROFILE": str(self.home),
            "TEMP": str(temp),
            "TMP": str(temp),
            "UV_TOOL_BIN_DIR": str(self.uv_bin),
            "UV_TOOL_DIR": str(self.uv_tools),
        }

    def run(self, *arguments: str, uv_bin_on_path: bool = True) -> tuple[subprocess.CompletedProcess[str], str]:
        result = subprocess.run(
            [_windows_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(POWERSHELL_SCRIPT), *arguments],
            cwd=str(self.project),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.environment(uv_bin_on_path=uv_bin_on_path),
            timeout=SCRIPT_TIMEOUT_S,
            check=False,
        )
        return result, f"{result.stdout}{result.stderr}"

    def invocations(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.is_file() else ""

    def version_in_uv_bin(self) -> str:
        answered = subprocess.run([str(self.uv_bin / "agentic-hil.exe"), "--version"], capture_output=True, text=True, env=self.environment(), timeout=SCRIPT_TIMEOUT_S, check=False)
        return answered.stdout.strip()


@WINDOWS_ONLY
def test_an_exact_version_pin_refuses_a_mismatched_copy_in_the_managers_bin_under_powershell(tmp_path: Path) -> None:
    """The shell test of the same name, on the script Windows actually runs.

    uv's bin ends up holding a 9.9.9 where 0.5.0 was pinned. Step 3 has to say
    the fresh copy does not resolve, step 4 has to refuse the machine half
    rather than hand `agent-install` to that copy, and the script has to exit
    non-zero for it, all of which are PowerShell 5.1 semantics no text
    comparison with install.sh can see.
    """
    bench = _WindowsBench(tmp_path, installed=None, manager_writes="9.9.9")

    result, transcript = bench.run("-Version", "0.5.0", "--no-can")

    assert result.returncode != 0, transcript
    assert not bench.marker.exists(), transcript
    assert "does not resolve here" in transcript, transcript
    assert REGISTERED_LINE not in transcript, transcript
    assert "tool install --upgrade agentic-hil==0.5.0" in bench.invocations(), bench.invocations()


@WINDOWS_ONLY
def test_the_powershell_refresh_keeps_the_recorded_extras_and_with_end_to_end(tmp_path: Path) -> None:
    """The receipt replay, reached from the top of the script rather than from an extracted function.

    A copy at the release is on PATH, uv says it owns it, and the receipt
    records a pyOCD extra and a `--with`. The reinstall line uv is given has to
    carry both merged with this run's `can`, the run has to close on the
    refresh sentence because the version moved, and the copy in uv's bin has to
    be the one uv wrote.
    """
    release = _powershell_release()
    bench = _WindowsBench(tmp_path, installed=release, manager_writes="99.0.0", tool_list=f"agentic-hil v{release}\n- agentic-hil", receipt=_RECEIPT_WITH_PYOCD_AND_A_WITH)

    result, transcript = bench.run("--no-agent-install")

    assert result.returncode == 0, transcript
    assert f"agentic-hil {release} is here and not older than {release}, refreshing this current installation" in transcript, transcript
    invocations = bench.invocations()
    assert "tool install --upgrade --reinstall agentic-hil[can,pyocd] --with requests==2.32.5" in invocations, invocations
    assert "tool upgrade" not in invocations, invocations
    assert bench.version_in_uv_bin() == "99.0.0", transcript
    assert REFRESH_LINE in transcript, transcript
    assert CALM_LINE not in transcript, transcript


@WINDOWS_ONLY
def test_the_powershell_run_that_moved_nothing_closes_on_the_kept_sentence(tmp_path: Path) -> None:
    """The closing sentence follows what step 2 did: the version stayed, so no server is behind it."""
    release = _powershell_release()
    bench = _WindowsBench(tmp_path, installed=release, manager_writes=release, tool_list=f"agentic-hil v{release}\n- agentic-hil", receipt=_RECEIPT_WITH_PYOCD_AND_A_WITH)

    result, transcript = bench.run("--no-agent-install")

    assert result.returncode == 0, transcript
    assert f"This installation stayed at {release}, {KEPT_CURRENT_TAIL}" in transcript, transcript
    assert REFRESH_LINE not in transcript, transcript
    assert CALM_LINE not in transcript, transcript


@WINDOWS_ONLY
def test_the_powershell_first_install_closes_on_the_calm_sentence_and_reports_the_path(tmp_path: Path) -> None:
    """No agentic-hil anywhere: a first install, with nothing to reinstall and a directory to report.

    Step 2's line carries no `--reinstall`, step 3 reads the PATH the run was
    started with and finds uv's bin missing from it, so it prints the line the
    operator runs once, and the run closes on the first-install sentence.
    """
    bench = _WindowsBench(tmp_path, installed=None, manager_writes="99.0.0")

    result, transcript = bench.run("--no-agent-install", "--no-can", uv_bin_on_path=False)

    assert result.returncode == 0, transcript
    assert "no agentic-hil on this PATH, installing it user-local" in transcript, transcript
    invocations = bench.invocations()
    assert "tool install --upgrade agentic-hil\n" in invocations, invocations
    assert "--reinstall" not in invocations, invocations
    assert f"agentic-hil landed in {bench.uv_bin}, which is not on your PATH" in transcript, transcript
    assert f"[Environment]::SetEnvironmentVariable('Path', '{bench.uv_bin};'" in transcript, transcript
    assert CALM_LINE in transcript, transcript
    assert REFRESH_LINE not in transcript, transcript


@WINDOWS_ONLY
def test_the_powershell_install_retries_a_certificate_failure_against_the_system_store(tmp_path: Path) -> None:
    """The proxied machine, through the script Windows runs.

    uv comes back saying the chain ended outside its own roots; the second
    attempt validates against the store this machine already trusts. The stub
    records what it was given on each attempt, exactly as the shell test's does.
    """
    bench = _WindowsBench(tmp_path, installed=None, manager_writes="99.0.0", proxied=True)

    result, transcript = bench.run("--no-agent-install", "--no-can")

    assert result.returncode == 0, transcript
    assert bench.attempts.read_text(encoding="utf-8").split() == ["unset", "1"], transcript
    assert "retrying once against this machine's own store" in transcript, transcript
    assert bench.version_in_uv_bin() == "99.0.0", transcript


@WINDOWS_ONLY
def test_the_powershell_retry_is_refused_when_it_was_told_to_be(tmp_path: Path) -> None:
    """`--no-system-certs` keeps uv on its own roots, and the refusal names the flag."""
    bench = _WindowsBench(tmp_path, installed=None, manager_writes="99.0.0", proxied=True)

    result, transcript = bench.run("--no-agent-install", "--no-can", "--no-system-certs")

    assert result.returncode != 0, transcript
    assert bench.attempts.read_text(encoding="utf-8").split() == ["unset"], transcript
    assert "--no-system-certs was given" in transcript, transcript
    assert not (bench.uv_bin / "agentic-hil.exe").exists(), transcript


# What this harness does not reach, so nobody reads the tests above as reaching
# it: the `irm ... | iex` form (the script arrives here as a file, and a script
# read from a pipe binds its parameters differently); the fetch route, where
# Install-Uv downloads and executes Astral's pinned installer (WebClient reaches
# astral.sh directly and the installer edits the user's registry Path, which no
# test may do on a developer's machine); and step 5's restart block, which reads
# the real process table for a running claude, codex or opencode.
