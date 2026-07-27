[CmdletBinding(DefaultParameterSetName = "Setup")]
param(
    [Parameter(Mandatory, ParameterSetName = "InstallWslOnly")]
    [switch]$InstallWslOnly,

    [Parameter(Mandatory, ParameterSetName = "UpdateWslOnly")]
    [switch]$UpdateWslOnly,

    [Parameter(ParameterSetName = "Setup")]
    [switch]$NoStart,

    [Parameter(ParameterSetName = "Setup")]
    [switch]$NonInteractive,

    [Parameter(ParameterSetName = "Setup")]
    [switch]$PreflightOnly,

    [Parameter(ParameterSetName = "Setup")]
    [switch]$PlanConfirmed,

    [Parameter(ParameterSetName = "Setup")]
    [ValidateRange(30, 900)]
    [int]$WaitSeconds = 240
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$minimumWslVersion = [version]"2.1.5"

function Invoke-NativeProbe {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$ArgumentList
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $probeOutput = @()
    $probeExitCode = 1
    try {
        # Expected probe failures must not become terminating NativeCommandError
        # records under Windows PowerShell 5.1.
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

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-PinnedWslExecutable {
    $systemDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
    if (-not $systemDirectory) {
        return $null
    }

    $candidate = Join-Path $systemDirectory "wsl.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        return [IO.Path]::GetFullPath($candidate)
    }
    return $null
}

function Get-DockerCli {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Docker\Docker\resources\bin\docker.exe"),
        (Join-Path $env:LOCALAPPDATA "Docker\resources\bin\docker.exe"),
        (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe")
    )
    return $candidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
}

function Get-DockerDesktop {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Docker\Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe"),
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe")
    )
    return $candidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
}

function Get-WslVersion {
    param([string]$WslExecutable = (Get-PinnedWslExecutable))

    if ($null -eq $WslExecutable) {
        return $null
    }

    $probe = Invoke-NativeProbe -FilePath $WslExecutable -ArgumentList @("--version")
    if ($probe.ExitCode -ne 0) {
        return $null
    }

    # Windows PowerShell 5.1 may decode wsl.exe UTF-16 output as strings with
    # embedded NUL characters. Normalize before matching localized labels.
    $output = ($probe.Output -join [Environment]::NewLine).Replace(
        [string][char]0,
        ""
    )
    $match = [regex]::Match($output, "(?im)^\s*WSL[^:]*:\s*(\d+(?:\.\d+){1,3})")
    if (-not $match.Success) {
        throw (
            "WSL responded, but its version could not be determined. " +
            "Run '$WslExecutable --update' manually, then run this setup again."
        )
    }
    return [version]$match.Groups[1].Value
}

function Test-WslReady {
    param([Parameter(Mandatory)][string]$WslExecutable)

    $probe = Invoke-NativeProbe -FilePath $WslExecutable -ArgumentList @("--status")
    return $probe.ExitCode -eq 0
}

function Get-DockerInstallArchitecture {
    $architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    switch ($architecture) {
        "X64" { return "amd64" }
        "Arm64" { return "arm64" }
        default {
            throw (
                "Unsupported Windows architecture: $architecture. " +
                "Docker Desktop supports only x64 or arm64 here."
            )
        }
    }
}

function Get-SetupPreflight {
    if ($env:OS -ne "Windows_NT") {
        throw "This script is intended for Windows only."
    }

    $wslExecutable = Get-PinnedWslExecutable
    $wslVersion = Get-WslVersion -WslExecutable $wslExecutable
    $docker = Get-DockerCli
    $desktop = Get-DockerDesktop
    $actions = New-Object System.Collections.Generic.List[string]
    $wslAction = $null
    $rebootRequired = $false
    $wslStatusProbeFailed = $false
    $isAdministrator = Test-IsAdministrator
    $needsUserAction = $false

    if ($null -eq $wslVersion) {
        if ($null -eq $wslExecutable) {
            throw (
                "The Windows component wsl.exe is missing. Run Windows Update " +
                "or enable WSL according to Microsoft's instructions."
            )
        }
        $wslStatus = "missing or not enabled yet"
        $wslAction = "Install"
        $actions.Add(
            "Install WSL2 as administrator: '$wslExecutable --install --no-distribution'."
        )
        if (-not $isAdministrator) {
            $needsUserAction = $true
        }
    }
    elseif ($wslVersion -lt $minimumWslVersion) {
        $wslStatus = "version $wslVersion (too old; minimum $minimumWslVersion)"
        $wslAction = "Update"
        $actions.Add("Update WSL without elevation: '$wslExecutable --update'.")
    }
    elseif (-not (Test-WslReady -WslExecutable $wslExecutable)) {
        # A failed `wsl --status` probe does not prove that Windows needs
        # another restart. Docker's Linux daemon is the authoritative runtime
        # check for this setup and may already be healthy.
        $wslStatus = "version $wslVersion installed; status probe unavailable"
        $wslStatusProbeFailed = $true
    }
    else {
        $wslStatus = "version $wslVersion (ready)"
    }

    $dockerInstalled = $null -ne $docker -and $null -ne $desktop
    if (-not $dockerInstalled) {
        $downloadArchitecture = Get-DockerInstallArchitecture
        $dockerStatus = "not fully installed"
        $actions.Add(
            "Download the official Docker Desktop installer ($downloadArchitecture), " +
            "verify its signature, and install it for the current user."
        )
    }
    else {
        $dockerStatus = "installed"
        $downloadArchitecture = $null
    }

    $daemonStatus = "not checked"
    $daemonReady = $false
    $windowsContainers = $false
    if ($null -ne $docker) {
        $daemonProbe = Invoke-NativeProbe `
            -FilePath $docker `
            -ArgumentList @("info", "--format", "{{.OSType}}")
        if ($daemonProbe.ExitCode -eq 0) {
            $dockerOperatingSystem = [string]($daemonProbe.Output | Select-Object -First 1)
            if ($dockerOperatingSystem -eq "linux") {
                $daemonStatus = "ready (Linux containers)"
                $daemonReady = $true
            }
            else {
                $daemonStatus = "ready, but '$dockerOperatingSystem' containers are active"
                $windowsContainers = $true
                $needsUserAction = $true
                $actions.Add(
                    "Open Docker Desktop and switch to Linux containers with the WSL2 backend."
                )
            }
        }
        else {
            $daemonStatus = "unreachable"
        }
    }

    if (-not $daemonReady -and -not $windowsContainers) {
        if ($NoStart) {
            $actions.Add(
                "Docker Desktop will remain stopped as requested; start it manually before an evaluation run."
            )
        }
        else {
            $needsUserAction = $true
            $actions.Add(
                "Start Docker Desktop; review and accept the license agreement in the GUI if prompted."
            )
        }
    }

    if ($wslStatusProbeFailed) {
        if ($daemonReady) {
            $wslStatus = "version $wslVersion (ready; confirmed by Docker)"
        }
        else {
            $needsUserAction = $true
            $actions.Insert(
                0,
                (
                    "WSL is installed, but its status probe failed and Docker is not " +
                    "ready. Run '$wslExecutable --status' once and follow its exact " +
                    "error; do not repeatedly restart Windows."
                )
            )
        }
    }

    $dockerDirectory = $null
    $pathChangeNeeded = $false
    if ($null -ne $docker) {
        $dockerDirectory = Split-Path -Parent $docker
        $userPathParts = @(
            [Environment]::GetEnvironmentVariable("Path", "User") -split ";" |
                Where-Object { $_ }
        )
        if ($userPathParts -notcontains $dockerDirectory) {
            $pathChangeNeeded = $true
            $actions.Add("Add the Docker CLI directory to the user PATH.")
        }
    }

    return [pscustomobject]@{
        WslExecutable = $wslExecutable
        WslVersion = $wslVersion
        WslStatus = $wslStatus
        WslAction = $wslAction
        RebootRequired = $rebootRequired
        DockerCli = $docker
        DockerDesktop = $desktop
        DockerInstalled = $dockerInstalled
        DockerStatus = $dockerStatus
        DownloadArchitecture = $downloadArchitecture
        DaemonStatus = $daemonStatus
        DaemonReady = $daemonReady
        WindowsContainers = $windowsContainers
        DockerDirectory = $dockerDirectory
        PathChangeNeeded = $pathChangeNeeded
        IsAdministrator = $isAdministrator
        Actions = @($actions)
        NeedsAction = $actions.Count -gt 0
        NeedsUserAction = $needsUserAction
    }
}

function Write-SetupPreflight {
    param([Parameter(Mandatory)]$Plan)

    Write-Host "== Read-only Preflight"
    Write-Host "WSL:            $($Plan.WslStatus)"
    Write-Host "Docker Desktop: $($Plan.DockerStatus)"
    Write-Host "Docker daemon:  $($Plan.DaemonStatus)"
    Write-Host "Administrator:  $($Plan.IsAdministrator)"
    Write-Host ""
    if (-not $Plan.NeedsAction) {
        Write-Host "No changes needed. WSL2 and Docker are ready."
        return
    }

    Write-Host "Required actions:"
    $number = 1
    foreach ($action in $Plan.Actions) {
        Write-Host "  $number. $action"
        $number += 1
    }
}

function Confirm-SetupPlan {
    while ($true) {
        $answer = Read-Host "Run these actions now? [Y]es / [N]o"
        if ($null -eq $answer) {
            # Redirected or closed input reaches end of input instead of
            # answering. Decline once instead of looping on empty reads.
            Write-Host ""
            Write-Host "No answer could be read from the console." -ForegroundColor Yellow
            Write-Host "Run this helper interactively, pipe 'Y' into it, or use -NonInteractive."
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

function Add-UserPath {
    param([Parameter(Mandatory)][string]$Directory)

    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($current -split ";" | Where-Object { $_ })
    if ($parts -notcontains $Directory) {
        [Environment]::SetEnvironmentVariable(
            "Path",
            (($parts + $Directory) -join ";"),
            "User"
        )
    }
    if (($env:Path -split ";") -notcontains $Directory) {
        $env:Path = "$Directory;$env:Path"
    }
}

function Invoke-WslCommandAsCurrentUser {
    param(
        [Parameter(Mandatory)][string]$WslExecutable,
        [Parameter(Mandatory)][ValidateSet("Install", "Update")][string]$Action
    )

    $arguments = if ($Action -eq "Install") {
        @("--install", "--no-distribution")
    }
    else {
        @("--update")
    }
    Write-Host "Running: $WslExecutable $($arguments -join ' ')"
    & $WslExecutable @arguments | Out-Host
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 3010) {
        return 10
    }
    if ($exitCode -ne 0) {
        throw (
            "WSL command '$WslExecutable $($arguments -join ' ')' " +
            "failed (exit $exitCode). Check Windows Update and Event Viewer."
        )
    }
    return 0
}

function Invoke-ElevatedWslCommand {
    param(
        [Parameter(Mandatory)][string]$WslExecutable,
        [Parameter(Mandatory)][ValidateSet("Install", "Update")][string]$Action
    )

    $arguments = if ($Action -eq "Install") {
        @("--install", "--no-distribution")
    }
    else {
        @("--update")
    }
    Write-Host ""
    Write-Host "Windows will now request administrator privileges once."
    Write-Host "The only command that will run is:"
    Write-Host "  $WslExecutable $($arguments -join ' ')"

    while ($true) {
        try {
            $process = Start-Process `
                -FilePath $WslExecutable `
                -Verb RunAs `
                -Wait `
                -PassThru `
                -ArgumentList $arguments
        }
        catch {
            Write-Host ""
            Write-Host "The UAC prompt was canceled or blocked." -ForegroundColor Yellow
            $retryElevation = $false
            while ($true) {
                $answer = Read-Host "[R]etry / [N]o"
                if ($null -eq $answer) {
                    Write-Host (
                        "No answer could be read from the console; the elevated " +
                        "step was deferred."
                    ) -ForegroundColor Yellow
                    return 20
                }
                $answer = [string]$answer
                if ($answer.Trim() -match "^(r|retry)$") {
                    $retryElevation = $true
                    break
                }
                if ($answer.Trim() -match "^(n|no)$") {
                    return 20
                }
                Write-Host "Enter R or N."
            }
            if ($retryElevation) {
                continue
            }
        }

        if ($process.ExitCode -eq 3010) {
            return 10
        }
        if ($process.ExitCode -ne 0) {
            throw (
                "Elevated WSL command '$WslExecutable $($arguments -join ' ')' " +
                "failed (exit $($process.ExitCode))."
            )
        }
        return 0
    }
}

function Invoke-PlannedWslAction {
    param([Parameter(Mandatory)]$Plan)

    if ($null -eq $Plan.WslAction) {
        return 0
    }

    if ($Plan.WslAction -eq "Update" -or $Plan.IsAdministrator) {
        return Invoke-WslCommandAsCurrentUser `
            -WslExecutable $Plan.WslExecutable `
            -Action $Plan.WslAction
    }
    return Invoke-ElevatedWslCommand `
        -WslExecutable $Plan.WslExecutable `
        -Action $Plan.WslAction
}

function Install-DockerDesktop {
    param([Parameter(Mandatory)][string]$DownloadArchitecture)

    $uri = (
        "https://desktop.docker.com/win/main/$DownloadArchitecture/" +
        "Docker%20Desktop%20Installer.exe"
    )
    $temporaryDirectory = Join-Path (
        [IO.Path]::GetTempPath()
    ) ("agentic-hil-docker-" + [guid]::NewGuid().ToString("N"))
    $installer = Join-Path $temporaryDirectory "Docker Desktop Installer.exe"
    $primaryError = $null
    $cleanupError = $null

    try {
        New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
        Write-Host "Downloading the official Docker Desktop installer ..."
        $downloadComplete = $false
        for ($attempt = 1; $attempt -le 3; $attempt += 1) {
            try {
                Invoke-WebRequest `
                    -UseBasicParsing `
                    -Uri $uri `
                    -OutFile $installer `
                    -TimeoutSec 300
                $downloadComplete = $true
                break
            }
            catch {
                if ($attempt -eq 3) {
                    throw
                }
                Write-Host "Download failed; attempt $($attempt + 1) of 3 ..."
                Start-Sleep -Seconds 2
            }
        }
        if (-not $downloadComplete) {
            throw "Could not download the Docker Desktop installer."
        }

        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        $signerSubject = [string]$signature.SignerCertificate.Subject
        if (
            $signature.Status -ne "Valid" -or
            $signerSubject -notmatch "(?i)(^|,\s*)CN=Docker Inc\.?(,|$)"
        ) {
            throw "The Docker installer does not have a valid Docker Authenticode signature."
        }
        $installerHash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
        $installerVersion = (Get-Item -LiteralPath $installer).VersionInfo.ProductVersion
        Write-Host "Docker installer: version $installerVersion"
        Write-Host "Signer: $signerSubject"
        Write-Host "SHA256: $installerHash"

        Write-Host "Installing Docker Desktop for the current user with the WSL2 backend ..."
        $arguments = @(
            "install",
            "--user",
            "--backend=wsl-2",
            "--no-windows-containers",
            "--quiet"
        )
        $process = Start-Process `
            -FilePath $installer `
            -Wait `
            -PassThru `
            -ArgumentList $arguments
        if ($process.ExitCode -ne 0) {
            throw "Docker Desktop installation failed (exit $($process.ExitCode))."
        }
    }
    catch {
        $primaryError = $_
    }
    finally {
        try {
            if (Test-Path -LiteralPath $temporaryDirectory) {
                $resolvedTemporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
                $resolvedTemporaryDirectory = [IO.Path]::GetFullPath($temporaryDirectory)
                if (-not $resolvedTemporaryDirectory.StartsWith(
                    $resolvedTemporaryRoot,
                    [StringComparison]::OrdinalIgnoreCase
                )) {
                    throw (
                        "Temporary Docker directory is outside the " +
                        "Windows temporary directory: $resolvedTemporaryDirectory"
                    )
                }
                Remove-Item -LiteralPath $resolvedTemporaryDirectory -Recurse -Force
            }
        }
        catch {
            $cleanupError = $_
        }
    }

    if ($null -ne $primaryError) {
        if ($null -ne $cleanupError) {
            Write-Warning "Also failed to remove the temporary directory: $($cleanupError.Exception.Message)"
        }
        throw $primaryError
    }
    if ($null -ne $cleanupError) {
        Write-Warning "Failed to remove the temporary directory: $($cleanupError.Exception.Message)"
    }
}

function Wait-ForDockerDaemon {
    param(
        [Parameter(Mandatory)][string]$DockerCli,
        [Parameter(Mandatory)][int]$TimeoutSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $probe = Invoke-NativeProbe `
            -FilePath $DockerCli `
            -ArgumentList @("info", "--format", "{{.OSType}}")
        $dockerOperatingSystem = [string]($probe.Output | Select-Object -First 1)
        if ($probe.ExitCode -eq 0) {
            if ($dockerOperatingSystem -ne "linux") {
                Write-Host (
                    "Docker is using '$dockerOperatingSystem' containers. " +
                    "Switch to Linux containers with the WSL2 backend in Docker Desktop."
                ) -ForegroundColor Yellow
                return 20
            }
            Write-Host "Docker daemon ready."
            & $DockerCli version | Out-Host
            return 0
        }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)

    Write-Host (
        "Docker Desktop is not ready yet. Complete the license dialog or initial " +
        "setup in the GUI, then run this setup again."
    ) -ForegroundColor Yellow
    return 20
}

function Invoke-AdminOnlyMode {
    param([Parameter(Mandatory)][ValidateSet("Install", "Update")][string]$Action)

    if (-not (Test-IsAdministrator)) {
        throw (
            "-${Action}WslOnly may only be used in a PowerShell session " +
            "that was already started as administrator."
        )
    }
    $wslExecutable = Get-PinnedWslExecutable
    if ($null -eq $wslExecutable) {
        throw "The pinned Windows system file wsl.exe is missing."
    }
    Write-Host "== WSL administrator mode"
    Write-Host "Verified: administrator privileges and pinned Windows wsl.exe are available."
    return Invoke-WslCommandAsCurrentUser `
        -WslExecutable $wslExecutable `
        -Action $Action
}

function Invoke-Setup {
    $plan = Get-SetupPreflight
    Write-SetupPreflight -Plan $plan

    if ($PreflightOnly) {
        Write-Host ""
        Write-Host "Preflight complete; no changes were made."
        if ($plan.RebootRequired) {
            return 10
        }
        if ($plan.NeedsUserAction) {
            return 20
        }
        return 0
    }

    if (-not $plan.NeedsAction) {
        return 0
    }

    if ($plan.RebootRequired) {
        Write-Host ""
        Write-Host (
            "Windows restart required. Then run the same setup command again; " +
            "completed steps will be detected."
        )
        return 10
    }

    if ($NonInteractive -and $plan.NeedsUserAction) {
        Write-Host ""
        Write-Host (
            "-NonInteractive does not open UAC, GUI, or license prompts. " +
            "Complete the user actions above manually, then run this setup again."
        ) -ForegroundColor Yellow
        return 20
    }

    if (
        -not $NonInteractive -and
        -not $PlanConfirmed -and
        -not (Confirm-SetupPlan)
    ) {
        Write-Host "Setup deferred as requested. Run it again when these actions are allowed."
        return 20
    }

    $wslResult = Invoke-PlannedWslAction -Plan $plan
    if ($wslResult -eq 20) {
        Write-Host "WSL installation deferred. You can run this setup again later."
        return 20
    }
    if ($wslResult -eq 10) {
        Write-Host (
            "WSL requires a restart. Restart Windows, then run the same setup " +
            "command again; completed steps will be detected."
        )
        return 10
    }

    if ($null -ne $plan.WslAction) {
        $updatedWslVersion = Get-WslVersion -WslExecutable $plan.WslExecutable
        if ($null -eq $updatedWslVersion) {
            Write-Host (
                "The WSL command completed, but its version is still unavailable. " +
                "Windows did not request a restart. Run '$($plan.WslExecutable) " +
                "--version' once and resolve its exact error; do not repeatedly " +
                "restart Windows."
            ) -ForegroundColor Yellow
            return 20
        }
        if ($updatedWslVersion -lt $minimumWslVersion) {
            throw (
                "WSL action completed, but version $updatedWslVersion was detected " +
                "instead of the required minimum $minimumWslVersion."
            )
        }
        if (-not (Test-WslReady -WslExecutable $plan.WslExecutable)) {
            Write-Host (
                "WSL version $updatedWslVersion is installed, but its status probe " +
                "is unavailable. Windows did not request a restart; Docker readiness " +
                "will decide whether setup can continue."
            ) -ForegroundColor Yellow
        }
    }

    if (-not $plan.DockerInstalled) {
        Install-DockerDesktop -DownloadArchitecture $plan.DownloadArchitecture
    }

    $docker = Get-DockerCli
    $desktop = Get-DockerDesktop
    if ($null -eq $docker -or $null -eq $desktop) {
        throw (
            "Docker Desktop was installed, but docker.exe or 'Docker Desktop.exe' " +
            "was not found. Open a new terminal, then run this setup again."
        )
    }

    $dockerDirectory = Split-Path -Parent $docker
    Add-UserPath -Directory $dockerDirectory
    Write-Host "Docker CLI: $docker"

    $readyProbe = Invoke-NativeProbe `
        -FilePath $docker `
        -ArgumentList @("info", "--format", "{{.OSType}}")
    if ($readyProbe.ExitCode -eq 0) {
        $operatingSystem = [string]($readyProbe.Output | Select-Object -First 1)
        if ($operatingSystem -eq "linux") {
            Write-Host "Docker daemon ready."
            return 0
        }
        Write-Host (
            "Docker is using '$operatingSystem' containers. In Docker Desktop, manually " +
            "switch to Linux containers with the WSL2 backend."
        ) -ForegroundColor Yellow
        return 20
    }

    if ($NoStart) {
        Write-Host (
            "Installation complete. Start Docker Desktop manually before the evaluation " +
            "run, then run this setup again."
        )
        return 0
    }

    if (-not (Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue)) {
        Write-Host "Starting Docker Desktop ..."
        Start-Process -FilePath $desktop | Out-Null
    }

    return Wait-ForDockerDaemon -DockerCli $docker -TimeoutSeconds $WaitSeconds
}

try {
    $exitCode = switch ($PSCmdlet.ParameterSetName) {
        "InstallWslOnly" { Invoke-AdminOnlyMode -Action "Install" }
        "UpdateWslOnly" { Invoke-AdminOnlyMode -Action "Update" }
        default { Invoke-Setup }
    }
    if ($null -eq $exitCode) {
        $exitCode = 0
    }
    exit [int]$exitCode
}
catch {
    Write-Host ""
    Write-Host "Docker/WSL setup did not complete." -ForegroundColor Red
    Write-Host "Reason: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host (
        "After resolving the issue, run this setup again; completed steps will be " +
        "detected during the read-only preflight."
    )
    exit 1
}
