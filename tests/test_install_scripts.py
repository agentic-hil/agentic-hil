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
    assert "skipping the package install" not in transcript, transcript
    assert "0.11.0 is older than" in transcript, transcript
    assert marker.is_file(), transcript
    assert marker.read_text(encoding="utf-8").strip() == "fresh", transcript


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
