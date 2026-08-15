# What this script touches, and nothing else:
#   1. the agentic-hil package, installed user-local (uv tool, or pip --user).
#   2. your agent's skill file, under your own home directory.
#   3. your agent's user-level MCP registration, under your own home directory.
#   4. nothing in any repository, no project configuration, no profile script.
#   5. nothing that needs administrator rights: it never elevates and changes no machine setting.

param(
    [string]$Agent = '',
    [switch]$NoAgentInstall,
    [string]$Version = '',
    [switch]$Can,
    [switch]$NoCan,
    [switch]$Help,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'

$Floor = '0.4.0'
$StepTotal = 5

function Write-Say {
    param([string]$Message)
    Write-Host "agentic-hil install: $Message"
}

function Write-Step {
    param([int]$Number, [string]$Message)
    Write-Host "agentic-hil install: step $Number/$StepTotal  $Message"
}

function Write-Usage {
    Write-Host @'
Usage: install.ps1 [options]
       irm https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/install.ps1 | iex
       powershell -NoProfile -File .\install.ps1 --agent claude

Installs Agentic HIL user-local and registers the skill and the MCP server for
your AI agent. It writes no project configuration and asks for no admin rights.

Options:
  --agent <name>      Register for this agent only: claude-code, codex or
                      opencode. Without it, every agent CLI found on PATH is
                      registered, and no agent CLI is installed for you.
  --no-agent-install  Install the package and stop there. Nothing belonging to
                      an agent is written.
  --version <x.y.z>   Install exactly this release, as agentic-hil==x.y.z.
                      Later upgrades go through "agentic-hil upgrade", which
                      keeps the extras and the manager that owns the
                      installation, not through a second run with a new pin.
  --can               Install the [can] extra for PEAK and SocketCAN adapters.
                      This is the default.
  --no-can            Install without the [can] extra.
  --help              Print this text and exit.

PowerShell spells the same flags -Agent, -NoAgentInstall, -Version, -Can,
-NoCan and -Help; both spellings bind to the same options.
'@
}

$WithCan = -not $NoCan
$WithAgentInstall = -not $NoAgentInstall
$ShowHelp = [bool]$Help

# Two spellings carry an inner dash, which no PowerShell parameter name can, so
# they arrive here rather than bound. Everything else binds by itself.
foreach ($token in @($Rest)) {
    if ($null -eq $token) { continue }
    switch -Regex ($token) {
        '^--no-agent-install$' { $WithAgentInstall = $false }
        '^--no-can$' { $WithCan = $false }
        '^--can$' { $WithCan = $true }
        '^(--help|-h|/\?)$' { $ShowHelp = $true }
        default {
            Write-Host "agentic-hil install: unknown option: $token"
            Write-Usage
            exit 2
        }
    }
}
if ($Can) { $WithCan = $true }

if ($ShowHelp) {
    Write-Usage
    exit 0
}

# Windows PowerShell 5.1 still negotiates TLS 1.0 by default, and every host
# this script reaches has stopped accepting it.
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {
    Write-Say 'TLS 1.2 could not be enabled explicitly; carrying on with this host default'
}

function Test-Executable {
    param([string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-Captured {
    <#
        A native command's output and exit code, with stderr folded in. Windows
        PowerShell 5.1 turns a native command's stderr into error records under
        a Stop preference, so the preference is lowered around the call alone.
    #>
    param([string]$File, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $text = (& $File @Arguments 2>&1 | Out-String)
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
        return [pscustomobject]@{ ExitCode = $code; Output = $text }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Invoke-Checked {
    param([string]$File, [string[]]$Arguments, [string]$Failure)
    $result = Invoke-Captured -File $File -Arguments $Arguments
    Write-Host $result.Output.TrimEnd()
    if ($result.ExitCode -ne 0) { throw $Failure }
}

function Test-VersionAtLeast {
    param([string]$Found, [string]$Floor)
    $pattern = '(\d+)\.(\d+)\.(\d+)'
    $left = [regex]::Match($Found, $pattern)
    $right = [regex]::Match($Floor, $pattern)
    if (-not $left.Success -or -not $right.Success) { return $false }
    for ($index = 1; $index -le 3; $index++) {
        $a = [int]$left.Groups[$index].Value
        $b = [int]$right.Groups[$index].Value
        if ($a -gt $b) { return $true }
        if ($a -lt $b) { return $false }
    }
    return $true
}

function Get-PackageSpec {
    $spec = 'agentic-hil'
    if ($WithCan) { $spec = 'agentic-hil[can]' }
    if ($Version) { $spec = "$spec==$Version" }
    return $spec
}

function Find-Python {
    foreach ($candidate in @('py', 'python', 'python3')) {
        if (-not (Test-Executable $candidate)) { continue }
        $probe = Invoke-Captured -File $candidate -Arguments @(
            '-c',
            'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
        )
        if ($probe.ExitCode -eq 0) { return $candidate }
    }
    return ''
}

function Install-Uv {
    # Astral's own installer for uv. It downloads the release archive for this
    # platform and verifies its published checksums itself, which is why nothing
    # here adds a second, weaker check of its own around the pipe. It is read
    # into this process rather than saved and started, so no execution policy is
    # touched and nothing has to be elevated.
    $installer = Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' -UseBasicParsing
    Invoke-Expression $installer
    Add-UserBinToPath
}

function Add-UserBinToPath {
    $userBin = Join-Path $env:USERPROFILE '.local\bin'
    if (Test-Path $userBin) { $env:Path = "$userBin;$env:Path" }
}

function Get-CandidateBinDirectories {
    param([string]$PythonCommand)
    $directories = @(Join-Path $env:USERPROFILE '.local\bin')
    if ($PythonCommand) {
        $probe = Invoke-Captured -File $PythonCommand -Arguments @(
            '-c',
            'import sysconfig; print(sysconfig.get_path("scripts", "nt_user"))'
        )
        if ($probe.ExitCode -eq 0) { $directories += $probe.Output.Trim() }
    }
    return $directories
}

function Get-AgentIdForCli {
    param([string]$Cli)
    if ($Cli -eq 'claude') { return 'claude-code' }
    return $Cli
}

function Get-ProcessNameForAgent {
    param([string]$AgentId)
    if ($AgentId -eq 'claude-code' -or $AgentId -eq 'claude') { return 'claude' }
    return $AgentId
}

# Step 1: is a new enough agentic-hil already here?
$needsPackage = $true
if (Test-Executable 'agentic-hil') {
    $probe = Invoke-Captured -File 'agentic-hil' -Arguments @('--version')
    $installed = $probe.Output.Trim()
    if ($probe.ExitCode -ne 0) {
        Write-Step 1 'probe: an agentic-hil on this PATH does not answer, installing it again'
    } elseif ($Version) {
        Write-Step 1 "probe: agentic-hil $installed is here, and --version $Version was asked for, so the package is installed again"
    } elseif (Test-VersionAtLeast -Found $installed -Floor $Floor) {
        Write-Step 1 "probe: agentic-hil $installed is here and at least $Floor, skipping the package install"
        $needsPackage = $false
    } else {
        Write-Step 1 "probe: agentic-hil $installed is older than $Floor, upgrading it"
    }
} else {
    Write-Step 1 'probe: no agentic-hil on this PATH, installing it user-local'
}

# Step 2: install the package, user-local, never elevated.
$pythonCommand = ''
if (-not $needsPackage) {
    Write-Step 2 'package: nothing to install'
} elseif (Test-Executable 'uv') {
    Write-Step 2 "package: uv is here, installing $(Get-PackageSpec) user-local with uv tool install"
    Invoke-Checked -File 'uv' -Arguments @('tool', 'install', '--upgrade', (Get-PackageSpec)) -Failure 'uv could not install agentic-hil'
} else {
    $pythonCommand = Find-Python
    if ($pythonCommand) {
        Write-Step 2 "package: installing $(Get-PackageSpec) user-local with $pythonCommand -m pip install --user"
        $pip = Invoke-Captured -File $pythonCommand -Arguments @(
            '-m', 'pip', 'install', '--user', '--upgrade', (Get-PackageSpec)
        )
        Write-Host $pip.Output.TrimEnd()
        if ($pip.ExitCode -ne 0) {
            # PEP 668: the distribution owns this interpreter. The answer is an
            # environment of uv's own, never --break-system-packages.
            if ($pip.Output -match 'externally.managed') {
                Write-Say 'package: this Python is externally managed (PEP 668), so pip cannot own it; falling back to uv'
                if (-not (Test-Executable 'uv')) { Install-Uv }
                Add-UserBinToPath
                if (-not (Test-Executable 'uv')) { throw 'uv installed but does not resolve yet; open a new shell and run this again' }
                Invoke-Checked -File 'uv' -Arguments @('tool', 'install', '--upgrade', (Get-PackageSpec)) -Failure 'uv could not install agentic-hil'
            } else {
                throw "pip could not install $(Get-PackageSpec); TROUBLESHOOTING.md section 1 has the fallbacks"
            }
        }
    } else {
        Write-Step 2 "package: no uv and no Python 3.10 or newer here, fetching Astral's uv installer first"
        Install-Uv
        if (-not (Test-Executable 'uv')) { throw 'uv installed but does not resolve yet; open a new shell and run this again' }
        Write-Say "package: installing $(Get-PackageSpec) user-local with uv tool install"
        Invoke-Checked -File 'uv' -Arguments @('tool', 'install', '--upgrade', (Get-PackageSpec)) -Failure 'uv could not install agentic-hil'
    }
}

# The exact agentic-hil the machine half calls. It stays the bare name only when
# step 1 found a new-enough copy already here and installed nothing; once this
# run installs a copy, it becomes that copy's own path, so an older agentic-hil
# earlier on PATH cannot answer for the install that just happened.
$AgenticHilCmd = 'agentic-hil'

# Step 3: say where it landed, and edit nobody's profile script.
if (-not $needsPackage) {
    # Nothing was installed: the copy step 1 probed and accepted on this PATH is
    # the one the machine half uses, and it already resolves here.
    Write-Step 3 "PATH: agentic-hil $installed is already here and was kept, nothing to add"
} else {
    $found = ''
    foreach ($directory in (Get-CandidateBinDirectories -PythonCommand $pythonCommand)) {
        if (-not $directory) { continue }
        if ((Test-Path (Join-Path $directory 'agentic-hil.exe')) -and -not $found) { $found = $directory }
    }
    if ($found) {
        # Call this exact copy for the machine half, and put its directory first
        # for the rest of the run: it, not an older agentic-hil earlier on PATH,
        # is what registers the skill and the MCP server.
        $AgenticHilCmd = Join-Path $found 'agentic-hil.exe'
        if (($env:Path -split ';') -contains $found) {
            Write-Step 3 "PATH: agentic-hil is installed in $found, already on your PATH"
        } else {
            Write-Step 3 "PATH: agentic-hil landed in $found, which is not on your PATH"
            Write-Say 'PATH: run this line yourself once, then open a new terminal:'
            Write-Host ''
            Write-Host "    [Environment]::SetEnvironmentVariable('Path', '$found;' + [Environment]::GetEnvironmentVariable('Path', 'User'), 'User')"
            Write-Host ''
        }
        $env:Path = "$found;$env:Path"
    } elseif (Test-Executable 'agentic-hil') {
        # Installed, but not into any directory this script installs into:
        # whatever put it elsewhere owns where it resolves, and if it resolves at
        # all that is the copy the machine half will use.
        Write-Step 3 'PATH: agentic-hil resolves on this PATH already, nothing to add'
    } else {
        Write-Step 3 'PATH: agentic-hil is installed but does not resolve here; TROUBLESHOOTING.md section 1 has the fix'
    }
}

# Step 4: the machine half, and only the machine half.
$configured = @()
if (-not $WithAgentInstall) {
    Write-Step 4 'agent: --no-agent-install was given, so nothing of any agent''s was written'
} elseif ($Agent) {
    Write-Step 4 "agent: registering the skill and the MCP server for $Agent"
    Invoke-Checked -File $AgenticHilCmd -Arguments @('agent-install', '--agent', $Agent) -Failure "agent-install failed for $Agent"
    $configured = @($Agent)
} else {
    $detected = @()
    foreach ($cli in @('claude', 'codex', 'opencode')) {
        if (Test-Executable $cli) { $detected += (Get-AgentIdForCli $cli) }
    }
    if ($detected.Count -eq 0) {
        Write-Step 4 'agent: no claude, codex or opencode CLI on this PATH, so nothing of an agent''s was written'
        Write-Say 'agent: the package is installed. Once your agent CLI is here, run this one line:'
        Write-Host ''
        Write-Host '    agentic-hil agent-install --agent <claude-code|codex|opencode>'
        Write-Host ''
    } else {
        foreach ($agentId in $detected) {
            Write-Step 4 "agent: registering the skill and the MCP server for $agentId"
            Invoke-Checked -File $AgenticHilCmd -Arguments @('agent-install', '--agent', $agentId) -Failure "agent-install failed for $agentId"
            $configured += $agentId
        }
    }
}

# Step 5: the one thing that stays the operator's.
$runningName = ''
$runningPid = ''
foreach ($agentId in $configured) {
    if ($runningName) { continue }
    $processName = Get-ProcessNameForAgent $agentId
    $process = @(Get-Process -Name $processName -ErrorAction SilentlyContinue) | Select-Object -First 1
    if ($process) {
        $runningName = $processName
        $runningPid = $process.Id
    }
}
if (-not $runningName -and $env:CLAUDECODE) {
    # Running inside a Claude Code session, which is the only signal where the
    # process list has nothing to show. It speaks for claude-code alone: an
    # operator who asked for codex is not told to restart something else.
    foreach ($agentId in $configured) {
        if ($agentId -eq 'claude-code' -or $agentId -eq 'claude') {
            $runningName = 'claude'
            $runningPid = 'this session'
        }
    }
}

if ($runningName) {
    Write-Step 5 "restart: $runningName is running right now, and that is the one step left"
    Write-Host ''
    Write-Host '========================================================================'
    Write-Host "  RESTART REQUIRED: $runningName is running right now (PID $runningPid)."
    Write-Host '  Quit that process and start it again once. An agent CLI reads its MCP'
    Write-Host '  registrations when a session starts, so the agentic-hil tools appear in'
    Write-Host '  the next session, not in this one.'
    Write-Host '========================================================================'
    Write-Host ''
} else {
    Write-Step 5 'restart: no agent CLI of yours is running, so there is nothing to restart'
    Write-Say "The next start of your agent has everything, and the first hardware question creates this project's configuration."
}
