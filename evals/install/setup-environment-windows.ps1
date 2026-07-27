[CmdletBinding()]
param(
    [switch]$PreflightOnly,
    [switch]$SkipDocker,
    [switch]$SkipImageBuild,
    [switch]$RunChecks,
    [switch]$NoStartDocker,
    [switch]$NonInteractive,
    [switch]$AcceptPackageAgreements,
    [ValidateRange(30, 900)]
    [int]$DockerWaitSeconds = 240
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

trap {
    $failureMessage = $_.Exception.Message
    Write-Host ""
    Write-Host "Setup did not complete: $failureMessage" -ForegroundColor Red
    Write-Host "Completed steps have been preserved."
    if ($failureMessage -like "Docker image build failed*") {
        Write-Host (
            "Do not restart Windows or rerun the unchanged setup: this build error " +
            "must be corrected first."
        ) -ForegroundColor Yellow
        Write-Host "After correcting it, run:"
    }
    else {
        Write-Host "After resolving the reason above, run again:"
    }
    Write-Host "powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit 1
}

$setupStartedAt = [DateTime]::UtcNow
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$dockerInstaller = Join-Path $PSScriptRoot "install-docker-windows.ps1"
$virtualEnvironment = Join-Path $repositoryRoot ".venv"
$virtualEnvironmentPython = Join-Path $virtualEnvironment "Scripts\python.exe"

if (-not (Test-Path -LiteralPath (Join-Path $repositoryRoot "pyproject.toml") -PathType Leaf)) {
    throw "Repository root could not be resolved from $PSScriptRoot."
}

function Invoke-NativeProbe {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$ArgumentList
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $probeOutput = @()
    $probeExitCode = 1
    try {
        # Expected native probe failures must not become terminating
        # NativeCommandError records under Windows PowerShell.
        $ErrorActionPreference = "Continue"
        $probeOutput = @(& $FilePath @ArgumentList 2>$null)
        $probeExitCode = $LASTEXITCODE
    }
    catch {
        $probeOutput = @()
        $probeExitCode = 1
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    return [pscustomobject]@{
        ExitCode = $probeExitCode
        Output = $probeOutput
    }
}

function Refresh-ProcessPath {
    $segments = @()
    foreach ($pathValue in @(
        $env:Path,
        [Environment]::GetEnvironmentVariable("Path", "User"),
        [Environment]::GetEnvironmentVariable("Path", "Machine")
    )) {
        if ($pathValue) {
            $segments += $pathValue -split ";"
        }
    }

    $seen = @{}
    $deduplicated = foreach ($segment in $segments) {
        $trimmed = $segment.Trim()
        if ($trimmed -and -not $seen.ContainsKey($trimmed)) {
            $seen[$trimmed] = $true
            $trimmed
        }
    }
    $env:Path = $deduplicated -join ";"
}

function Get-DockerCli {
    param([string[]]$CandidatePaths)

    if ($null -eq $CandidatePaths) {
        $CandidatePaths = @(
            (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Docker\Docker\resources\bin\docker.exe"),
            (Join-Path $env:LOCALAPPDATA "Docker\resources\bin\docker.exe"),
            (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe")
        )
    }

    $command = Get-Command docker.exe -ErrorAction SilentlyContinue
    $paths = @()
    if ($null -ne $command -and $command.Source) {
        $paths += $command.Source
    }
    $paths += $CandidatePaths

    $docker = $paths |
        Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
        Select-Object -First 1
    if ($null -eq $docker) {
        return $null
    }
    return [IO.Path]::GetFullPath([string]$docker)
}

function Get-CompatiblePythonRuntime {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$PrefixArguments = @()
    )

    $arguments = @($PrefixArguments) + @(
        "-I",
        "-S",
        "-c",
        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); print(sys.executable)"
    )
    $probe = Invoke-NativeProbe -FilePath $FilePath -ArgumentList $arguments
    if ($probe.ExitCode -ne 0) {
        return $null
    }

    $versionText = $probe.Output | Select-Object -First 1
    $executable = $probe.Output | Select-Object -Skip 1 -First 1
    $version = $null
    if (
        -not [version]::TryParse([string]$versionText, [ref]$version) -or
        $version -lt [version]"3.10" -or
        -not $executable -or
        -not (Test-Path -LiteralPath $executable -PathType Leaf)
    ) {
        return $null
    }

    return [pscustomobject]@{
        Executable = [IO.Path]::GetFullPath([string]$executable)
        Version = $version
    }
}

function Get-CompatiblePython {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        $runtime = Get-CompatiblePythonRuntime -FilePath $launcher.Source
        if ($null -ne $runtime) {
            return $runtime
        }
    }

    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    $isWindowsStoreAlias = (
        $null -ne $command -and
        $command.Source -match "\\Microsoft\\WindowsApps\\python(?:3)?\.exe$"
    )
    if ($null -ne $command -and -not $isWindowsStoreAlias) {
        $runtime = Get-CompatiblePythonRuntime -FilePath $command.Source
        if ($null -ne $runtime) {
            return $runtime
        }
    }

    $candidates = @()
    foreach ($root in @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python"),
        (Join-Path $env:ProgramFiles "Python")
    )) {
        if (-not (Test-Path -LiteralPath $root -PathType Container)) {
            continue
        }
        foreach ($directory in Get-ChildItem -LiteralPath $root -Directory -Filter "Python*" -ErrorAction SilentlyContinue) {
            $candidate = Join-Path $directory.FullName "python.exe"
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $candidates += $candidate
            }
        }
    }
    foreach ($candidate in $candidates | Sort-Object -Unique -Descending) {
        $runtime = Get-CompatiblePythonRuntime -FilePath $candidate
        if ($null -ne $runtime) {
            return $runtime
        }
    }
    return $null
}

function Install-WingetPackage {
    param(
        [Parameter(Mandatory)][string]$Id,
        [Parameter(Mandatory)][string]$Name
    )

    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($null -eq $winget) {
        throw "$Name is missing and winget.exe is unavailable. Install Microsoft App Installer, then run this script again."
    }

    Write-Host "Installing $Name for the current user ..."
    $arguments = @(
        "install",
        "--exact",
        "--id", $Id,
        "--scope", "user",
        "--silent",
        "--disable-interactivity",
        "--accept-package-agreements",
        "--accept-source-agreements"
    )
    & $winget.Source @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed to install $Name (exit code $LASTEXITCODE)."
    }
    Refresh-ProcessPath
}

function Write-PreflightStatus {
    param(
        [Parameter(Mandatory)][string]$State,
        [Parameter(Mandatory)][string]$Message
    )

    $color = switch ($State) {
        "OK" { "Green" }
        "BLOCK" { "Red" }
        default { "Yellow" }
    }
    Write-Host ("[{0}] {1}" -f $State, $Message) -ForegroundColor $color
}

function Confirm-SetupPlan {
    while ($true) {
        $answer = Read-Host "Run this plan now? [Y]es / [N]o"
        if ($null -eq $answer) {
            # Redirected or closed input reaches end of input instead of
            # answering. Decline once instead of looping on empty reads.
            Write-Host ""
            Write-Host "No answer could be read from the console." -ForegroundColor Yellow
            Write-Host "Run this setup interactively, pipe 'Y' into it, or use -NonInteractive."
            return $false
        }
        switch -Regex ([string]$answer.Trim()) {
            "^(y|yes)$" {
                return $true
            }
            "^(n|no)$" {
                return $false
            }
            default {
                Write-Host "Enter Y or N."
            }
        }
    }
}

function Get-UntrustedWriteIdentity {
    param([Parameter(Mandatory)][string]$LiteralPath)

    # Mirrors the ownership rule that agentic_hil.config enforces for a
    # persistent MCP executable: only the current user, SYSTEM, and the local
    # administrators may write to the path that holds it.
    $writeRights = (
        [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Modify -bor
        [Security.AccessControl.FileSystemRights]::FullControl -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions
    )

    try {
        # Same trusted set as windows_acl_grants_untrusted_access: the current
        # user, CREATOR OWNER, SYSTEM, the local administrators, and OWNER RIGHTS.
        $trusted = @(
            [Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
            "S-1-3-0",
            "S-1-3-4",
            "S-1-5-18",
            "S-1-5-32-544"
        )
        $accessRules = (Get-Acl -LiteralPath $LiteralPath).Access
    }
    catch {
        # A probe failure must not block a setup that is otherwise healthy.
        return @()
    }

    $offenders = @()
    foreach ($rule in $accessRules) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            continue
        }
        if (([int]$rule.FileSystemRights -band [int]$writeRights) -eq 0) {
            continue
        }
        $identity = [string]$rule.IdentityReference.Value
        try {
            $identity = $rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        }
        catch {
            # An unresolvable account keeps its raw value and stays reported.
        }
        if ($trusted -notcontains $identity) {
            $offenders += $identity
        }
    }
    return @($offenders | Sort-Object -Unique)
}

function Get-SystemWindowsPowerShell {
    $systemDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
    $candidate = Join-Path $systemDirectory "WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "System Windows PowerShell was not found: $candidate"
    }
    return $candidate
}

Write-Host "== Preflight check (no changes yet)"
Refresh-ProcessPath

$preflightBlocked = $false
$git = Get-Command git.exe -ErrorAction SilentlyContinue
if ($null -eq $git) {
    Write-PreflightStatus -State "INSTALL" -Message "Git is missing; a per-user installation through winget is planned."
}
else {
    Write-PreflightStatus -State "OK" -Message "Git: $($git.Source)"
}

$pythonRuntime = Get-CompatiblePython
if ($null -eq $pythonRuntime) {
    Write-PreflightStatus -State "INSTALL" -Message "Python 3.10+ was not found; a per-user Python 3.13 fallback installation is planned."
}
else {
    Write-PreflightStatus -State "OK" -Message "Python $($pythonRuntime.Version): $($pythonRuntime.Executable)"
}

$virtualEnvironmentRuntime = $null
if (Test-Path -LiteralPath $virtualEnvironmentPython -PathType Leaf) {
    $virtualEnvironmentRuntime = Get-CompatiblePythonRuntime -FilePath $virtualEnvironmentPython
    if ($null -eq $virtualEnvironmentRuntime) {
        Write-PreflightStatus -State "BLOCK" -Message "The existing .venv does not contain a working Python 3.10+ runtime."
        Write-Host "  Inspect, rename, or remove .venv manually, then run the setup again."
        $preflightBlocked = $true
    }
    else {
        Write-PreflightStatus -State "OK" -Message ".venv: Python $($virtualEnvironmentRuntime.Version)"
    }
}
elseif (Test-Path -LiteralPath $virtualEnvironment -PathType Container) {
    Write-PreflightStatus -State "BLOCK" -Message "The existing .venv is incomplete."
    Write-Host "  Inspect, rename, or remove .venv manually, then run the setup again."
    $preflightBlocked = $true
}
else {
    Write-PreflightStatus -State "CREATE" -Message ".venv will be created."
}

$untrustedWriters = Get-UntrustedWriteIdentity -LiteralPath $repositoryRoot
if ($untrustedWriters.Count -gt 0) {
    Write-PreflightStatus -State "WARN" -Message "The repository directory grants write access to other Windows principals."
    foreach ($identity in $untrustedWriters) {
        Write-Host "  $identity"
    }
    Write-Host "  .venv inherits these entries, so 'agentic-hil setup' will reject"
    Write-Host "  $virtualEnvironmentPython as an MCP server command."
    Write-Host "  Remove them in PowerShell, then run the setup again."
    Write-Host "  icacls cannot do this: it fails with 1332 once an account no longer resolves."
    Write-Host "  `$acl = Get-Acl -LiteralPath '$repositoryRoot'"
    foreach ($identity in $untrustedWriters) {
        Write-Host "  `$acl.PurgeAccessRules((New-Object Security.Principal.SecurityIdentifier '$identity'))"
    }
    Write-Host "  Set-Acl -LiteralPath '$repositoryRoot' -AclObject `$acl"
    Write-Host "  The evaluation setup itself continues; only agent registration is affected."
}

$needsGitInstall = $null -eq $git
$needsPythonInstall = $null -eq $pythonRuntime -and $null -eq $virtualEnvironmentRuntime
$needsWinget = $needsGitInstall -or $needsPythonInstall
if ($needsWinget) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($null -eq $winget) {
        Write-PreflightStatus -State "BLOCK" -Message "winget is missing. Install Microsoft App Installer."
        $preflightBlocked = $true
    }
    else {
        Write-PreflightStatus -State "ACTION" -Message "winget source and package agreements will be accepted during installation."
    }
}

Write-PreflightStatus -State "ACTION" -Message "Project and development dependencies will be installed or updated in .venv."
if ($RunChecks) {
    Write-PreflightStatus -State "ACTION" -Message "Ruff and evaluation unit tests will run."
}
else {
    Write-PreflightStatus -State "SKIP" -Message "Repository quality checks are not part of setup. Use -RunChecks to run them."
}

$dockerPreflightExitCode = 0
$windowsPowerShell = $null
if ($SkipDocker) {
    Write-PreflightStatus -State "SKIP" -Message "WSL2, Docker Desktop, and the evaluation image."
}
else {
    if (-not (Test-Path -LiteralPath $dockerInstaller -PathType Leaf)) {
        throw "Docker installer is missing: $dockerInstaller"
    }
    $windowsPowerShell = Get-SystemWindowsPowerShell
    $dockerPreflightArguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $dockerInstaller,
        "-PreflightOnly",
        "-WaitSeconds", [string]$DockerWaitSeconds
    )
    if ($NoStartDocker) {
        $dockerPreflightArguments += "-NoStart"
    }
    if ($NonInteractive) {
        $dockerPreflightArguments += "-NonInteractive"
    }
    & $windowsPowerShell @dockerPreflightArguments
    $dockerPreflightExitCode = $LASTEXITCODE
    if ($dockerPreflightExitCode -notin @(0, 10, 20)) {
        throw "Docker preflight failed (exit code $dockerPreflightExitCode). See the message above."
    }
}

if ($preflightBlocked) {
    throw "The preflight check found blocking issues. Follow the instructions above; nothing has been installed."
}

Write-Host ""
if ($PreflightOnly) {
    Write-Host "Preflight check complete. No changes were made."
    exit 0
}

if ($dockerPreflightExitCode -eq 10) {
    Write-Host "Setup paused: Windows must be restarted before WSL or Docker setup can continue." -ForegroundColor Yellow
    Write-Host "No changes were made. Restart Windows, then run the same setup command again."
    exit 10
}

if ($NonInteractive -and $needsWinget -and -not $AcceptPackageAgreements) {
    Write-Host "Setup paused: package agreements require explicit acceptance." -ForegroundColor Yellow
    Write-Host "Run again with -AcceptPackageAgreements or install Git and Python manually."
    exit 20
}
if ($NonInteractive -and $dockerPreflightExitCode -eq 20) {
    Write-Host "Setup paused: WSL or Docker requires a user action such as UAC, license, or UI confirmation." -ForegroundColor Yellow
    Write-Host "Run interactively or use -SkipDocker first."
    exit 20
}

if (-not $NonInteractive) {
    Write-Host "Enter Y to confirm the installation plan above."
    if ($needsWinget) {
        Write-Host "This includes the displayed winget source and package agreements."
    }
    if ($dockerPreflightExitCode -eq 20) {
        Write-Host "WSL or Docker requires one of the user actions listed above."
        Write-Host "Canceling UAC is safe: the setup will offer to retry or continue later."
    }
    if (-not (Confirm-SetupPlan)) {
        Write-Host "Setup deferred. No changes were made."
        Write-Host "Run again later:"
        Write-Host "powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`""
        exit 20
    }
}

Write-Host ""
Write-Host "== Host tools"

if ($needsGitInstall) {
    Install-WingetPackage -Id "Git.Git" -Name "Git"
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
}
if ($null -eq $git) {
    throw "Git was installed but cannot be found in this session. Open a new terminal and run the setup again."
}
Write-Host "Git: $($git.Source)"

if ($needsPythonInstall) {
    Install-WingetPackage -Id "Python.Python.3.13" -Name "Python 3.13 (fallback)"
    $pythonRuntime = Get-CompatiblePython
}
if ($null -eq $pythonRuntime -and $null -eq $virtualEnvironmentRuntime) {
    throw "No compatible Python 3.10+ runtime was found. Open a new terminal and run the setup again."
}
if ($null -ne $pythonRuntime) {
    Write-Host "Python $($pythonRuntime.Version): $($pythonRuntime.Executable)"
}

Write-Host "== Python development environment"
$virtualEnvironmentCreated = $false
if ($null -eq $virtualEnvironmentRuntime) {
    if ($null -eq $pythonRuntime) {
        throw ".venv is missing and no compatible host Python runtime is available."
    }
    Write-Host "Creating .venv ..."
    & $pythonRuntime.Executable -m venv $virtualEnvironment
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create $virtualEnvironment."
    }
    $virtualEnvironmentRuntime = Get-CompatiblePythonRuntime -FilePath $virtualEnvironmentPython
    if ($null -eq $virtualEnvironmentRuntime) {
        throw ".venv was created, but its Python runtime is not working."
    }
    $virtualEnvironmentCreated = $true
}

# A fresh environment downloads everything, so its progress is worth showing.
# A reused environment mostly reports "already satisfied" lines, which only
# bury the steps that matter.
$pipOptions = @("--disable-pip-version-check")
if (-not $virtualEnvironmentCreated) {
    $pipOptions += "--quiet"
}

Write-Host "Updating pip ..."
& $virtualEnvironmentPython -m pip install @pipOptions --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Failed to update pip."
}
Write-Host "Installing project and development dependencies (first run downloads packages) ..."
$editableTarget = "$($repositoryRoot)[dev]"
& $virtualEnvironmentPython -m pip install @pipOptions --editable $editableTarget
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install project and development dependencies."
}
Write-Host "Dependencies are up to date."

Push-Location $repositoryRoot
try {
    if ($RunChecks) {
        Write-Host "== Static checks and evaluation unit tests"
        & $virtualEnvironmentPython -m ruff check evals tests
        if ($LASTEXITCODE -ne 0) {
            throw "Ruff failed."
        }
        & $virtualEnvironmentPython -m pytest tests -q -k install_eval -o pythonpath=src
        if ($LASTEXITCODE -ne 0) {
            throw "Evaluation unit tests failed."
        }
    }
}
finally {
    Pop-Location
}

if (-not $SkipDocker) {
    Write-Host "== WSL2 + Docker Desktop"
    $dockerArguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $dockerInstaller,
        "-PlanConfirmed",
        "-WaitSeconds", [string]$DockerWaitSeconds
    )
    if ($NoStartDocker) {
        $dockerArguments += "-NoStart"
    }
    if ($NonInteractive) {
        $dockerArguments += "-NonInteractive"
    }
    & $windowsPowerShell @dockerArguments
    $dockerExitCode = $LASTEXITCODE
    if ($dockerExitCode -eq 10) {
        Write-Host ""
        Write-Host "The host environment is ready: $virtualEnvironmentPython"
        Write-Host "Restart Windows, then run the setup again. Existing progress will be reused."
        Write-Host "powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`""
        exit 10
    }
    if ($dockerExitCode -eq 20) {
        Write-Host ""
        Write-Host "The host environment is ready: $virtualEnvironmentPython"
        Write-Host "The Docker step was safely deferred. Run the same setup command again later."
        Write-Host "powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`""
        exit 20
    }
    if ($dockerExitCode -ne 0) {
        throw "Docker setup failed (exit code $dockerExitCode). See the message above."
    }
}

if (-not $SkipDocker -and -not $SkipImageBuild -and -not $NoStartDocker) {
    Push-Location $repositoryRoot
    try {
        Write-Host "== Versioned evaluation image"
        # The Docker helper runs in a child PowerShell process. Reload the
        # persisted PATH and still pass the resolved executable explicitly so a
        # newly installed Docker CLI works without opening another terminal.
        Refresh-ProcessPath
        $dockerCli = Get-DockerCli
        if ($null -eq $dockerCli) {
            throw (
                "Docker Desktop is ready, but docker.exe could not be resolved in " +
                "this session. Verify the Docker Desktop installation, then run " +
                "the setup again."
            )
        }
        Write-Host "Docker CLI: $dockerCli"
        & $virtualEnvironmentPython -m evals.install build --docker $dockerCli
        if ($LASTEXITCODE -ne 0) {
            throw (
                "Docker image build failed. A Windows restart is not required. " +
                "Correct the build error shown above, then run the setup again."
            )
        }
    }
    finally {
        Pop-Location
    }
}

Write-Host ""
$setupDuration = [DateTime]::UtcNow - $setupStartedAt
Write-Host ("Environment ready in {0:0} min {1:00} s." -f
    [Math]::Floor($setupDuration.TotalMinutes), $setupDuration.Seconds)
Write-Host "Python: $virtualEnvironmentPython"
if (-not $SkipDocker -and -not $SkipImageBuild -and -not $NoStartDocker) {
    Write-Host "Docker image: agentic-hil-install-eval:local"
}
elseif (-not $SkipDocker -and -not $SkipImageBuild -and $NoStartDocker) {
    Write-Host "Docker image: build skipped because Docker Desktop was not started."
}
Write-Host "Next step:"
Write-Host "& '$virtualEnvironmentPython' -m evals.install run --matrix evals\install\matrix.example.json --output evals\install\artifacts\run-001 --dry-run"
