from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows PowerShell installer tests require Windows",
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = REPOSITORY_ROOT / "evals" / "install" / "setup-environment-windows.ps1"
DOCKER_SCRIPT = REPOSITORY_ROOT / "evals" / "install" / "install-docker-windows.ps1"


def _windows_powershell() -> Path:
    system_root = os.environ.get("SYSTEMROOT")
    assert system_root
    executable = (
        Path(system_root)
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    assert executable.is_file()
    return executable


def _run_script(
    script: Path,
    *arguments: str,
    input_text: str | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(_windows_powershell()),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=timeout,
    )


def _copy_setup_fixture(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    install_directory = repository / "evals" / "install"
    install_directory.mkdir(parents=True)
    shutil.copy2(SETUP_SCRIPT, install_directory / SETUP_SCRIPT.name)
    (repository / "pyproject.toml").write_text(
        "[build-system]\nrequires = []\nbuild-backend = \"\"\n",
        encoding="utf-8",
    )
    return repository, install_directory / SETUP_SCRIPT.name


@WINDOWS_ONLY
def test_windows_install_eval_scripts_parse_in_windows_powershell() -> None:
    escaped_paths = [
        str(path).replace("'", "''") for path in (SETUP_SCRIPT, DOCKER_SCRIPT)
    ]
    command = (
        "$failed=$false; "
        f"foreach($path in @('{escaped_paths[0]}','{escaped_paths[1]}')) {{ "
        "$tokens=$null; $errors=$null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        "$path,[ref]$tokens,[ref]$errors); "
        "if($errors.Count){$errors | ForEach-Object {$_.Message}; $failed=$true} }; "
        "if($failed){exit 1}"
    )
    result = subprocess.run(
        [str(_windows_powershell()), "-NoProfile", "-Command", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout


@WINDOWS_ONLY
def test_windows_install_eval_preflight_is_read_only(tmp_path: Path) -> None:
    repository, script = _copy_setup_fixture(tmp_path)

    result = _run_script(script, "-PreflightOnly", "-SkipDocker")

    assert result.returncode == 0, result.stdout
    assert "no changes yet" in result.stdout
    assert "No changes were made" in result.stdout
    assert "quality checks are not part of setup" in result.stdout
    assert not (repository / ".venv").exists()


@WINDOWS_ONLY
def test_windows_install_eval_checks_are_explicit_opt_in(tmp_path: Path) -> None:
    repository, script = _copy_setup_fixture(tmp_path)

    result = _run_script(script, "-PreflightOnly", "-SkipDocker", "-RunChecks")

    assert result.returncode == 0, result.stdout
    assert "Ruff and evaluation unit tests will run" in result.stdout
    assert not (repository / ".venv").exists()


@WINDOWS_ONLY
def test_windows_install_eval_decline_is_clean_and_read_only(tmp_path: Path) -> None:
    repository, script = _copy_setup_fixture(tmp_path)

    result = _run_script(script, "-SkipDocker", input_text="n\r\n")

    assert result.returncode == 20, result.stdout
    assert "Setup deferred" in result.stdout
    assert "CategoryInfo" not in result.stdout
    assert "FullyQualifiedErrorId" not in result.stdout
    assert not (repository / ".venv").exists()


@WINDOWS_ONLY
def test_windows_install_eval_closed_input_defers_without_stack_trace(
    tmp_path: Path,
) -> None:
    repository, script = _copy_setup_fixture(tmp_path)

    result = _run_script(script, "-SkipDocker", input_text="")

    assert result.returncode == 20, result.stdout
    assert "No answer could be read from the console" in result.stdout
    assert "Setup deferred" in result.stdout
    assert "CategoryInfo" not in result.stdout
    assert "FullyQualifiedErrorId" not in result.stdout
    assert not (repository / ".venv").exists()


@WINDOWS_ONLY
def test_windows_install_eval_blocker_has_no_stack_trace(tmp_path: Path) -> None:
    repository, script = _copy_setup_fixture(tmp_path)
    (repository / ".venv").mkdir()

    result = _run_script(script, "-PreflightOnly", "-SkipDocker")

    assert result.returncode == 1, result.stdout
    assert "Setup did not complete:" in result.stdout
    assert "nothing has been installed" in result.stdout
    assert "CategoryInfo" not in result.stdout
    assert "FullyQualifiedErrorId" not in result.stdout


@WINDOWS_ONLY
def test_windows_install_eval_noninteractive_docker_gate_precedes_mutation(
    tmp_path: Path,
) -> None:
    repository, script = _copy_setup_fixture(tmp_path)
    fake_helper = script.parent / DOCKER_SCRIPT.name
    fake_helper.write_text(
        """param(
    [switch]$PreflightOnly,
    [switch]$PlanConfirmed,
    [switch]$NonInteractive,
    [switch]$NoStart,
    [int]$WaitSeconds = 240
)
if ($PreflightOnly) {
    Write-Host "FAKE_DOCKER_PREFLIGHT"
    exit 20
}
throw "Docker helper must not execute after a non-interactive blocker."
""",
        encoding="ascii",
    )

    result = _run_script(script, "-NonInteractive")

    assert result.returncode == 20, result.stdout
    assert "FAKE_DOCKER_PREFLIGHT" in result.stdout
    assert "Setup paused" in result.stdout
    assert not (repository / ".venv").exists()


@WINDOWS_ONLY
def test_windows_install_eval_reboot_gate_precedes_mutation(tmp_path: Path) -> None:
    repository, script = _copy_setup_fixture(tmp_path)
    fake_helper = script.parent / DOCKER_SCRIPT.name
    fake_helper.write_text(
        """param(
    [switch]$PreflightOnly,
    [switch]$PlanConfirmed,
    [switch]$NonInteractive,
    [switch]$NoStart,
    [int]$WaitSeconds = 240
)
if ($PreflightOnly) {
    Write-Host "FAKE_REBOOT_REQUIRED"
    exit 10
}
throw "Docker helper must not execute before a required restart."
""",
        encoding="ascii",
    )

    result = _run_script(script)

    assert result.returncode == 10, result.stdout
    assert "FAKE_REBOOT_REQUIRED" in result.stdout
    assert "Windows must be restarted" in result.stdout
    assert not (repository / ".venv").exists()


@WINDOWS_ONLY
def test_windows_setup_resolves_docker_cli_outside_process_path(
    tmp_path: Path,
) -> None:
    docker_cli = tmp_path / "Docker" / "resources" / "bin" / "docker.exe"
    docker_cli.parent.mkdir(parents=True)
    docker_cli.write_bytes(b"placeholder")
    escaped_script = str(SETUP_SCRIPT).replace("'", "''")
    escaped_docker = str(docker_cli).replace("'", "''")
    command = f"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{escaped_script}',
    [ref]$tokens,
    [ref]$errors
)
$functionAst = $ast.Find({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Get-DockerCli'
}}, $true)
if ($null -eq $functionAst) {{ throw 'Function not found.' }}
Invoke-Expression $functionAst.Extent.Text

$env:Path = [Environment]::GetFolderPath(
    [Environment+SpecialFolder]::System
)
$resolved = Get-DockerCli -CandidatePaths @('{escaped_docker}')
if ($resolved -ne [IO.Path]::GetFullPath('{escaped_docker}')) {{
    throw "Unexpected Docker CLI: $resolved"
}}
"""
    result = subprocess.run(
        [str(_windows_powershell()), "-NoProfile", "-Command", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout


@WINDOWS_ONLY
def test_windows_setup_reports_untrusted_repository_writers(tmp_path: Path) -> None:
    clean_directory = tmp_path / "clean"
    clean_directory.mkdir()
    shared_directory = tmp_path / "shared"
    shared_directory.mkdir()
    escaped_script = str(SETUP_SCRIPT).replace("'", "''")
    escaped_clean = str(clean_directory).replace("'", "''")
    escaped_shared = str(shared_directory).replace("'", "''")
    command = f"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{escaped_script}',
    [ref]$tokens,
    [ref]$errors
)
$functionAst = $ast.Find({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Get-UntrustedWriteIdentity'
}}, $true)
if ($null -eq $functionAst) {{ throw 'Function not found.' }}
Invoke-Expression $functionAst.Extent.Text

$full = [Security.AccessControl.FileSystemRights]::FullControl
$inherit = [Security.AccessControl.InheritanceFlags]"ObjectInherit, ContainerInherit"
$allow = [Security.AccessControl.AccessControlType]::Allow
$cleanAcl = Get-Acl -LiteralPath '{escaped_clean}'
# Runner temp directories may grant extra principals write access, so pin the
# trusted-only case instead of inheriting whatever the host provides.
$cleanAcl.SetAccessRuleProtection($true, $false)
foreach ($trusted in @(
    [Security.Principal.WindowsIdentity]::GetCurrent().User,
    (New-Object Security.Principal.SecurityIdentifier 'S-1-5-18'),
    (New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-544')
)) {{
    $cleanAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
        $trusted,
        $full,
        $inherit,
        [Security.AccessControl.PropagationFlags]::None,
        $allow
    )))
}}
Set-Acl -LiteralPath '{escaped_clean}' -AclObject $cleanAcl

$clean = @(Get-UntrustedWriteIdentity -LiteralPath '{escaped_clean}')
if ($clean.Count -ne 0) {{
    throw "Unexpected identities on a trusted-only directory: $($clean -join ', ')"
}}

$everyone = New-Object Security.Principal.SecurityIdentifier 'S-1-1-0'
$acl = Get-Acl -LiteralPath '{escaped_shared}'
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
    $everyone,
    [Security.AccessControl.FileSystemRights]::Modify,
    [Security.AccessControl.InheritanceFlags]"ObjectInherit, ContainerInherit",
    [Security.AccessControl.PropagationFlags]::None,
    [Security.AccessControl.AccessControlType]::Allow
)))
Set-Acl -LiteralPath '{escaped_shared}' -AclObject $acl

$shared = @(Get-UntrustedWriteIdentity -LiteralPath '{escaped_shared}')
if ($shared -notcontains 'S-1-1-0') {{
    throw "Everyone write access was not reported: $($shared -join ', ')"
}}
"""
    result = subprocess.run(
        [str(_windows_powershell()), "-NoProfile", "-Command", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout


def test_windows_setup_passes_resolved_docker_cli_to_image_build() -> None:
    source = SETUP_SCRIPT.read_text(encoding="utf-8")
    build_start = source.index('Write-Host "== Versioned evaluation image"')
    build_end = source.index('Write-Host ""', build_start)
    build_source = source[build_start:build_end]

    assert build_source.index("Refresh-ProcessPath") < build_source.index(
        "$dockerCli = Get-DockerCli"
    )
    assert (
        "& $virtualEnvironmentPython -m evals.install build --docker $dockerCli"
        in build_source
    )
    assert "A Windows restart is not required" in build_source
    assert "Correct the build error shown above" in build_source
    assert "Do not restart Windows or rerun the unchanged setup" in source


def test_windows_docker_install_eval_declares_safe_preflight_contract() -> None:
    source = DOCKER_SCRIPT.read_text(encoding="utf-8")

    for parameter in ("PreflightOnly", "PlanConfirmed", "NonInteractive"):
        assert f"[switch]${parameter}" in source
    assert "return 20" in source
    assert "exit [int]$exitCode" in source
    assert "Start-Process" in source
    assert "System32" in source or "SpecialFolder]::System" in source
    setup_start = source.index("function Invoke-Setup")
    setup_end = source.index("\ntry {", setup_start)
    setup_source = source[setup_start:setup_end]
    assert setup_source.index("if ($PreflightOnly)") < setup_source.index(
        "Install-DockerDesktop"
    )


@WINDOWS_ONLY
def test_windows_docker_install_eval_uac_cancel_can_defer_or_retry() -> None:
    escaped_path = str(DOCKER_SCRIPT).replace("'", "''")
    command = f"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{escaped_path}',
    [ref]$tokens,
    [ref]$errors
)
$functionAst = $ast.Find({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Invoke-ElevatedWslCommand'
}}, $true)
if ($null -eq $functionAst) {{ throw 'Function not found.' }}
Invoke-Expression $functionAst.Extent.Text

$script:mode = 'defer'
$script:startCount = 0
$script:readCount = 0
function Start-Process {{
    param($FilePath, $Verb, [switch]$Wait, [switch]$PassThru, $ArgumentList)
    $script:startCount += 1
    if ($script:mode -eq 'defer' -or $script:startCount -eq 1) {{
        throw 'simulated UAC cancellation'
    }}
    return [pscustomobject]@{{ ExitCode = 0 }}
}}
function Read-Host {{
    param($Prompt)
    $script:readCount += 1
    if ($script:mode -eq 'defer') {{
        if ($script:readCount -eq 1) {{ return 'invalid' }}
        return 'n'
    }}
    return 'r'
}}

$deferred = Invoke-ElevatedWslCommand `
    -WslExecutable 'C:\\Windows\\System32\\wsl.exe' `
    -Action Install
if ($deferred -ne 20 -or $script:startCount -ne 1 -or $script:readCount -ne 2) {{
    throw 'UAC defer path failed.'
}}

$script:mode = 'retry'
$script:startCount = 0
$script:readCount = 0
$retried = Invoke-ElevatedWslCommand `
    -WslExecutable 'C:\\Windows\\System32\\wsl.exe' `
    -Action Install
if ($retried -ne 0 -or $script:startCount -ne 2 -or $script:readCount -ne 1) {{
    throw 'UAC retry path failed.'
}}
"""
    result = subprocess.run(
        [str(_windows_powershell()), "-NoProfile", "-Command", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout
    assert "Enter R or N." in result.stdout
    assert "CategoryInfo" not in result.stdout
    assert "FullyQualifiedErrorId" not in result.stdout


@WINDOWS_ONLY
def test_windows_docker_install_eval_elevates_only_initial_wsl_install() -> None:
    escaped_path = str(DOCKER_SCRIPT).replace("'", "''")
    command = f"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{escaped_path}',
    [ref]$tokens,
    [ref]$errors
)
$functionAst = $ast.Find({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Invoke-PlannedWslAction'
}}, $true)
if ($null -eq $functionAst) {{ throw 'Function not found.' }}
Invoke-Expression $functionAst.Extent.Text

$script:currentUserCalls = 0
$script:elevatedCalls = 0
function Invoke-WslCommandAsCurrentUser {{
    param($WslExecutable, $Action)
    $script:currentUserCalls += 1
    return 0
}}
function Invoke-ElevatedWslCommand {{
    param($WslExecutable, $Action)
    $script:elevatedCalls += 1
    return 0
}}

$updatePlan = [pscustomobject]@{{
    WslAction = 'Update'
    IsAdministrator = $false
    WslExecutable = 'C:\\Windows\\System32\\wsl.exe'
}}
$updateResult = Invoke-PlannedWslAction -Plan $updatePlan
if (
    $updateResult -ne 0 -or
    $script:currentUserCalls -ne 1 -or
    $script:elevatedCalls -ne 0
) {{
    throw 'WSL update was not kept in the current user context.'
}}

$installPlan = [pscustomobject]@{{
    WslAction = 'Install'
    IsAdministrator = $false
    WslExecutable = 'C:\\Windows\\System32\\wsl.exe'
}}
$installResult = Invoke-PlannedWslAction -Plan $installPlan
if (
    $installResult -ne 0 -or
    $script:currentUserCalls -ne 1 -or
    $script:elevatedCalls -ne 1
) {{
    throw 'Initial WSL installation did not use elevation.'
}}
"""
    result = subprocess.run(
        [str(_windows_powershell()), "-NoProfile", "-Command", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout


@WINDOWS_ONLY
def test_windows_docker_install_eval_parses_wsl_utf16_output() -> None:
    escaped_path = str(DOCKER_SCRIPT).replace("'", "''")
    command = f"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{escaped_path}',
    [ref]$tokens,
    [ref]$errors
)
$functionAst = $ast.Find({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Get-WslVersion'
}}, $true)
if ($null -eq $functionAst) {{ throw 'Function not found.' }}
Invoke-Expression $functionAst.Extent.Text

$plainOutput = 'WSL-Version: 2.7.11.0'
$nulOutput = -join @(
    $plainOutput.ToCharArray() | ForEach-Object {{ "$($_)$([char]0)" }}
)
function Invoke-NativeProbe {{
    param($FilePath, $ArgumentList)
    return [pscustomobject]@{{
        ExitCode = 0
        Output = @($nulOutput)
    }}
}}

$version = Get-WslVersion -WslExecutable 'C:\\Windows\\System32\\wsl.exe'
if ($version -ne [version]'2.7.11.0') {{
    throw "Unexpected WSL version: $version"
}}
"""
    result = subprocess.run(
        [str(_windows_powershell()), "-NoProfile", "-Command", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout


@WINDOWS_ONLY
def test_windows_docker_preflight_does_not_repeat_restart_when_docker_is_ready() -> None:
    escaped_path = str(DOCKER_SCRIPT).replace("'", "''")
    command = f"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{escaped_path}',
    [ref]$tokens,
    [ref]$errors
)
$functionAst = $ast.Find({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Get-SetupPreflight'
}}, $true)
if ($null -eq $functionAst) {{ throw 'Function not found.' }}
Invoke-Expression $functionAst.Extent.Text

$minimumWslVersion = [version]'2.1.5'
$NoStart = $false
function Get-PinnedWslExecutable {{ return 'C:\\Windows\\System32\\wsl.exe' }}
function Get-WslVersion {{ return [version]'2.7.11.0' }}
function Get-DockerCli {{ return 'C:\\Docker\\docker.exe' }}
function Get-DockerDesktop {{ return 'C:\\Docker\\Docker Desktop.exe' }}
function Test-IsAdministrator {{ return $false }}
function Test-WslReady {{ return $false }}
function Invoke-NativeProbe {{
    return [pscustomobject]@{{
        ExitCode = 0
        Output = @('linux')
    }}
}}

$plan = Get-SetupPreflight
if ($plan.RebootRequired) {{
    throw 'A failed status probe incorrectly requested another restart.'
}}
if (-not $plan.DaemonReady) {{
    throw 'The mocked Linux Docker daemon was not detected.'
}}
if ($plan.WslStatus -notlike '*confirmed by Docker*') {{
    throw "Unexpected WSL status: $($plan.WslStatus)"
}}
if ($plan.Actions -match 'Restart Windows') {{
    throw 'The plan still contains a Windows restart action.'
}}
"""
    result = subprocess.run(
        [str(_windows_powershell()), "-NoProfile", "-Command", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout


@WINDOWS_ONLY
def test_windows_docker_post_action_status_failure_does_not_request_restart() -> None:
    escaped_path = str(DOCKER_SCRIPT).replace("'", "''")
    command = f"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{escaped_path}',
    [ref]$tokens,
    [ref]$errors
)
$functionAst = $ast.Find({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Invoke-Setup'
}}, $true)
if ($null -eq $functionAst) {{ throw 'Function not found.' }}
Invoke-Expression $functionAst.Extent.Text

$minimumWslVersion = [version]'2.1.5'
$PreflightOnly = $false
$NonInteractive = $false
$PlanConfirmed = $true
$NoStart = $false
function Get-SetupPreflight {{
    return [pscustomobject]@{{
        NeedsAction = $true
        RebootRequired = $false
        NeedsUserAction = $false
        WslAction = 'Update'
        WslExecutable = 'C:\\Windows\\System32\\wsl.exe'
        DockerInstalled = $true
    }}
}}
function Write-SetupPreflight {{ param($Plan) }}
function Invoke-PlannedWslAction {{ param($Plan) return 0 }}
function Get-WslVersion {{ return [version]'2.7.11.0' }}
function Test-WslReady {{ return $false }}
function Get-DockerCli {{ return 'C:\\Docker\\docker.exe' }}
function Get-DockerDesktop {{ return 'C:\\Docker\\Docker Desktop.exe' }}
function Add-UserPath {{ param($Directory) }}
function Invoke-NativeProbe {{
    return [pscustomobject]@{{
        ExitCode = 0
        Output = @('linux')
    }}
}}

$result = Invoke-Setup
if ($result -ne 0) {{
    throw "Unexpected setup result: $result"
}}
"""
    result = subprocess.run(
        [str(_windows_powershell()), "-NoProfile", "-Command", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout
    assert "Windows did not request a restart" in result.stdout
