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
    [switch]$SystemCerts,
    [switch]$NoSystemCerts,
    [switch]$Help,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'

# The release this script installs, and the version an installation already
# here has to reach to be left alone. Deliberately not a capability floor: the
# line that installs Agentic HIL is the same line people re-run to get current,
# and step 4 registers the skill out of whatever copy step 1 decided to keep, so
# a floor left a returning user on an old package and an old skill at once. A
# development tree reports X.Y.Z.devN, which compares as X.Y.Z and stays put.
$Release = '0.16.0'
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
       powershell -c "irm https://agentic-hil.github.io/install.ps1|iex"
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
  --system-certs      Validate TLS against this machine's own certificate
                      store, the one curl and apt already read, from the start.
                      Rarely needed: a failure that carries the signature of a
                      TLS-intercepting proxy is retried against that store by
                      itself. This only skips the first attempt.
  --no-system-certs   Never reach for this machine's store, not even after such
                      a failure. The install fails instead.
                      Neither flag disables verification. Nothing here does.
  --help              Print this text and exit.

PowerShell spells the same flags -Agent, -NoAgentInstall, -Version, -Can,
-NoCan, -SystemCerts, -NoSystemCerts and -Help; both spellings bind to the same
options.
'@
}

$WithCan = -not $NoCan
$WithAgentInstall = -not $NoAgentInstall
$ShowHelp = [bool]$Help
$SystemCertsMode = if ($NoSystemCerts) { 'never' } elseif ($SystemCerts) { 'always' } else { 'auto' }

# Two spellings carry an inner dash, which no PowerShell parameter name can, so
# they arrive here rather than bound. Everything else binds by itself.
foreach ($token in @($Rest)) {
    if ($null -eq $token) { continue }
    switch -Regex ($token) {
        '^--no-agent-install$' { $WithAgentInstall = $false }
        '^--no-can$' { $WithCan = $false }
        '^--can$' { $WithCan = $true }
        '^--system-certs$' { $SystemCertsMode = 'always' }
        '^--no-system-certs$' { $SystemCertsMode = 'never' }
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

# The certificate store a TLS-intercepting proxy needs, and the only concession
# this script makes to one. uv validates against roots bundled in its own
# binary, so on a managed network it fails where a browser on the same machine
# keeps working; this points it at the store Windows itself holds. pip takes a
# file rather than a switch and Windows ships no bundle file, so PIP_CERT is the
# operator's to set on the rare host where pip rather than uv does the install.
#
# Nobody has to know that in advance. The failed install is the detection: an
# install that came back with one of the signatures below is retried once
# against this machine's store, and only that. Verification is on in both
# attempts, and this is not a way to reach a switch that turns it off:
# -SkipCertificateCheck, --allow-insecure-host and --trusted-host are not
# options this script offers, and no path through it arrives at one.
# What a chain that ends outside the manager's own roots looks like, from uv
# (rustls), pip (OpenSSL) and .NET. A failure that says none of this is a
# failure about something else, and is never retried.
function Test-TrustFailure {
    param([string]$Output)
    return $Output -match 'invalid peer certificate|UnknownIssuer|self.signed certificate|certificate verify failed|CERTIFICATE_VERIFY_FAILED|unable to get local issuer certificate'
}

function Enable-SystemCerts {
    $env:UV_SYSTEM_CERTS = '1'
    Write-Say "certificates: uv reads this machine's own certificate store"
}

function Clear-SystemCerts {
    # --no-system-certs promises never to reach for this machine's own store, and
    # keeps it only by clearing an inherited override as well as by not setting
    # one. TROUBLESHOOTING.md recommends exporting UV_SYSTEM_CERTS so future
    # upgrades keep working behind a proxy, so an operator who then passes
    # --no-system-certs would still have uv reading the machine store from it.
    if ($env:UV_SYSTEM_CERTS) {
        Remove-Item Env:\UV_SYSTEM_CERTS
        Write-Say "certificates: --no-system-certs; cleared the inherited UV_SYSTEM_CERTS so uv does not read this machine's own store"
    }
}

function Install-WithUv {
    # uv's output is captured rather than streamed, because the text of a
    # failure is what decides whether there is a second attempt to make.
    $result = Invoke-Captured -File 'uv' -Arguments @('tool', 'install', '--upgrade', (Get-PackageSpec))
    Write-Host $result.Output.TrimEnd()
    if ($result.ExitCode -eq 0) { return }
    if ($script:SystemCertsMode -eq 'auto' -and (Test-TrustFailure $result.Output)) {
        Write-Say "certificates: that is a certificate uv cannot get to a root it carries, which is what a TLS-intercepting proxy looks like from inside uv; retrying once against this machine's own store, with verification still on"
        Enable-SystemCerts
        $script:SystemCertsMode = 'always'
        $retry = Invoke-Captured -File 'uv' -Arguments @('tool', 'install', '--upgrade', (Get-PackageSpec))
        Write-Host $retry.Output.TrimEnd()
        if ($retry.ExitCode -eq 0) { return }
        throw "uv could not install agentic-hil against this machine's own certificate store either; the proxy's own CA is missing from that store, and installing it there is the fix; TROUBLESHOOTING.md section 1 has the rest"
    }
    throw $UvInstallFailure
}

if ($SystemCertsMode -eq 'always') { Enable-SystemCerts }
elseif ($SystemCertsMode -eq 'never') { Clear-SystemCerts }

$UvInstallFailure = if ($SystemCertsMode -eq 'never') {
    'uv could not install agentic-hil, and a certificate failure would not have been retried against this machine own store because --no-system-certs was given; TROUBLESHOOTING.md section 1 has the rest'
} else {
    'uv could not install agentic-hil; TROUBLESHOOTING.md section 1 has the fallbacks'
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

        The records are unwrapped to their message before Out-String sees them,
        and that is not cosmetic. uv reports its progress on stderr, so a
        completely successful `uv tool install` handed the first of those records
        to Out-String, which rendered it the way 5.1 renders an error: the line
        itself, then "In Zeile:113", then a CategoryInfo block naming
        NativeCommandError. The install had worked. The transcript read like a
        crash. Taking the message off the record leaves a stderr line as the line
        it was and nothing else.
    #>
    param([string]$File, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $text = (& $File @Arguments 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { $_ }
        } | Out-String)
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

function Test-VersionExactly {
    param([string]$Found, [string]$Wanted)
    # Every one of the three numeric fields equal: the proof, for an explicit
    # --version pin, that a reported version is the pinned release and not merely a
    # newer one that happens to sit at least as high.
    $pattern = '(\d+)\.(\d+)\.(\d+)'
    $left = [regex]::Match($Found, $pattern)
    $right = [regex]::Match($Wanted, $pattern)
    if (-not $left.Success -or -not $right.Success) { return $false }
    for ($index = 1; $index -le 3; $index++) {
        if ([int]$left.Groups[$index].Value -ne [int]$right.Groups[$index].Value) { return $false }
    }
    return $true
}

function Test-VersionMatchesRequest {
    param([string]$Found)
    # Does a freshly installed copy's reported version answer what this run asked
    # for: exactly the pin when one was given, at least the release otherwise. The
    # pin must match exactly -- a newer copy left in the manager's bin is not the
    # pinned release this run wrote, and the documented --version contract is an
    # exact release, not a floor.
    if ($Version) { return (Test-VersionExactly -Found $Found -Wanted $Version) }
    return (Test-VersionAtLeast -Found $Found -Floor $Release)
}

function Get-UvBinDirectory {
    # uv's own executable directory, straight from uv. This is authoritative: it
    # honours UV_TOOL_BIN_DIR and the XDG data directory, neither of which the
    # guessed user bin can name, and it is where `uv tool install` just wrote the
    # console script.
    $probe = Invoke-Captured -File 'uv' -Arguments @('tool', 'dir', '--bin')
    if ($probe.ExitCode -eq 0) { return $probe.Output.Trim() }
    return ''
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

# The one release of Astral's uv installer this script is allowed to run, and the
# SHA-256 of exactly those bytes. They are one pair, and install.sh pins the same
# uv release: bumping any one of the four alone breaks an install rather than
# loosening it, which is the intended failure mode. Refreshing them is a release
# chore, written down in docs/release-strategy.md, not something an install
# decides on the operator's machine.
$UvInstallerVersion = '0.12.5'
$UvInstallerSha256 = 'ca1ad558c65d31e2d3a24464638aff90bfb81d6c72428b4e71d6f55944a68541'

function Get-Sha256Hex {
    param([byte[]]$Bytes)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        return ((($sha256.ComputeHash($Bytes)) | ForEach-Object { $_.ToString('x2') }) -join '')
    } finally {
        $sha256.Dispose()
    }
}

function Install-Uv {
    # Astral's own installer for uv, pinned to one release and checked before a
    # line of it runs. Three decisions are worth keeping visible here.
    #
    # Why the pin. The moving https://astral.sh/uv/install.ps1 serves whatever is
    # current at the second it is asked, so piping it into iex executes bytes
    # nobody here has ever read, on a machine that has just been told this script
    # touches five things and nothing else. Every other hop in the chain is
    # already pinned: this script is published with its own .sha256 beside it, and
    # the package comes hash-checked from PyPI. The versioned URL names one
    # release, the constant above names its exact bytes, and a download that is
    # not those bytes is not run at all.
    #
    # Why their checksums are the second layer, and why that layer is thinner
    # here than on the POSIX side. This pin vouches for the installer, not for the
    # uv binaries it goes on to fetch. Astral's POSIX installer carries a SHA-256
    # per release artifact and verifies the archive it downloaded against it;
    # their PowerShell installer, as of the pinned 0.12.5, downloads the archive
    # and unpacks it with no checksum step at all. So on Windows this pin is not a
    # belt beside their braces, it is the only integrity check between astral.sh
    # and an executed script, which is the strongest reason of all to keep it.
    #
    # Why bytes and not text. The digest has to speak for what is executed, so the
    # bytes are hashed and the same bytes are decoded and run. Nothing is saved
    # and started, so no execution policy is touched and nothing is elevated.
    $url = "https://astral.sh/uv/$UvInstallerVersion/install.ps1"
    $client = New-Object Net.WebClient
    try {
        $bytes = $client.DownloadData($url)
    } finally {
        $client.Dispose()
    }
    $foundHash = Get-Sha256Hex -Bytes $bytes
    if ($foundHash -ne $UvInstallerSha256) {
        Write-Say 'the pinned uv installer does not match its recorded hash, so it was not run.'
        Write-Say "  url      $url"
        Write-Say "  expected $UvInstallerSha256"
        Write-Say "  found    $foundHash"
        throw 'the pin in this script may be stale: check for a newer uv release, then refresh $UvInstallerVersion and $UvInstallerSha256 together. Until then, install uv or Python 3.10 or newer yourself and run this again.'
    }
    Invoke-Expression ([Text.Encoding]::UTF8.GetString($bytes))
    Add-UserBinToPath
}

function Add-UserBinToPath {
    $userBin = Join-Path $env:USERPROFILE '.local\bin'
    if (Test-Path $userBin) { $env:Path = "$userBin;$env:Path" }
}

function Get-ManagerBinDirectory {
    param([string]$PythonCommand, [string]$PackageManager)
    # The one directory the manager step 2 used just wrote agentic-hil.exe into,
    # asked of that manager itself and nothing guessed: `uv tool dir --bin` for uv,
    # the selected interpreter's own nt_user scripts path for pip --user. A copy in
    # any other directory cannot answer for this install, so when the manager
    # cannot name its destination this returns '' and the caller fails rather than
    # scanning a directory that may hold a stale copy.
    if ($PackageManager -eq 'uv') {
        return Get-UvBinDirectory
    }
    if ($PackageManager -eq 'pip' -and $PythonCommand) {
        $probe = Invoke-Captured -File $PythonCommand -Arguments @(
            '-c',
            'import sysconfig; print(sysconfig.get_path("scripts", "nt_user"))'
        )
        if ($probe.ExitCode -eq 0) { return $probe.Output.Trim() }
    }
    return ''
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
    } elseif (Test-VersionAtLeast -Found $installed -Floor $Release) {
        Write-Step 1 "probe: agentic-hil $installed is here and not older than $Release, skipping the package install"
        $needsPackage = $false
    } else {
        Write-Step 1 "probe: agentic-hil $installed is older than $Release, upgrading it"
    }
} else {
    Write-Step 1 'probe: no agentic-hil on this PATH, installing it user-local'
}

# Step 2: install the package, user-local, never elevated.
$pythonCommand = ''
# Which manager step 2 installed with, so step 3 can ask that manager where it
# actually put the executable rather than guess.
$packageManager = ''
if (-not $needsPackage) {
    Write-Step 2 'package: nothing to install'
} elseif (Test-Executable 'uv') {
    Write-Step 2 "package: uv is here, installing $(Get-PackageSpec) user-local with uv tool install"
    Install-WithUv
    $packageManager = 'uv'
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
                Install-WithUv
                $packageManager = 'uv'
            } else {
                throw "pip could not install $(Get-PackageSpec); TROUBLESHOOTING.md section 1 has the fallbacks"
            }
        } else {
            $packageManager = 'pip'
        }
    } else {
        Write-Step 2 "package: no uv and no Python 3.10 or newer here, fetching Astral's uv installer first"
        Install-Uv
        if (-not (Test-Executable 'uv')) { throw 'uv installed but does not resolve yet; open a new shell and run this again' }
        Write-Say "package: installing $(Get-PackageSpec) user-local with uv tool install"
        Install-WithUv
        $packageManager = 'uv'
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
    # The manager's own destination directory, and only that: the copy there must
    # answer and match what this run asked for (exactly the pin, or at least the
    # floor), which is the proof it is the one the manager just wrote and not an
    # older or unrelated one elsewhere. If the manager cannot name its destination,
    # or the copy there does not match, $found stays empty and the machine half is
    # refused rather than run against a guessed copy.
    $found = ''
    $directory = Get-ManagerBinDirectory -PythonCommand $pythonCommand -PackageManager $packageManager
    if ($directory) {
        $executable = Join-Path $directory 'agentic-hil.exe'
        if (Test-Path $executable) {
            $probe = Invoke-Captured -File $executable -Arguments @('--version')
            if ($probe.ExitCode -eq 0 -and (Test-VersionMatchesRequest -Found $probe.Output.Trim())) {
                $found = $directory
            }
        }
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
    } else {
        # Installed, but the fresh copy could not be located and version-checked
        # where the manager put it. We do not fall back to whatever bare
        # agentic-hil PATH resolves: that is exactly the older copy earlier on PATH
        # this step exists to step past. $AgenticHilCmd stays the bare name, and
        # step 4 refuses to run the machine half against it.
        Write-Step 3 'PATH: agentic-hil is installed but the fresh copy does not resolve here; TROUBLESHOOTING.md section 1 has the fix'
    }
}

# Refuse to run the machine half against a copy this run could not prove is the
# one it installed. $AgenticHilCmd is still the bare name only when a package was
# installed and step 3 could not resolve the fresh copy; running the bare name
# there would hand agent-install to whatever older agentic-hil PATH resolves,
# which is the failure this whole path guards against.
function Assert-ResolvedForAgentInstall {
    if ($needsPackage -and $AgenticHilCmd -eq 'agentic-hil') {
        throw 'the agentic-hil this run installed could not be located where the package manager put it, so the machine half was not run against a possibly stale PATH copy; TROUBLESHOOTING.md section 1 has the fix'
    }
}

function Register-Agent {
    # One agent registered, reported as a result rather than as a document.
    # `agent-install` answers with a report of every path it touched, and on a
    # machine with three agent CLIs the machine-readable form of it is roughly a
    # hundred and fifty lines inside a transcript whose whole job is to say five
    # calm things. On success the operator needs one line. On failure the report
    # is the diagnosis, so then it is printed whole and this stops.
    #
    # The verdict is the exit status, which is `overall_success` computed inside
    # the CLI. It used to be that status and a match on the top-level "ok" at its
    # own indentation, from back when the report arrived as JSON whoever was
    # reading. It is prose now, addressed to the operator who is about to read
    # it, and a text match on prose would be a worse check than the status it was
    # doubling.
    param([string]$AgentId)
    $result = Invoke-Captured -File $AgenticHilCmd -Arguments @('agent-install', '--agent', $AgentId)
    if ($result.ExitCode -eq 0) {
        Write-Say "agent: $AgentId registered (skill and MCP server, restart pending)"
        return
    }
    Write-Host $result.Output.TrimEnd()
    throw "agent-install failed for $AgentId; the report above says which half"
}

# Step 4: the machine half, and only the machine half.
$configured = @()
if (-not $WithAgentInstall) {
    Write-Step 4 'agent: --no-agent-install was given, so nothing of any agent''s was written'
} elseif ($Agent) {
    Assert-ResolvedForAgentInstall
    Write-Step 4 "agent: registering the skill and the MCP server for $Agent"
    Register-Agent -AgentId $Agent
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
        Assert-ResolvedForAgentInstall
        foreach ($agentId in $detected) {
            Write-Step 4 "agent: registering the skill and the MCP server for $agentId"
            Register-Agent -AgentId $agentId
            $configured += $agentId
        }
    }
}

# Step 5: the one thing that stays the operator's. Every running agent CLI is
# named, not the first one found: an operator with two of them open restarts
# both, and a warning that names one is read as clearing the other.
$running = @()
foreach ($agentId in $configured) {
    $processName = Get-ProcessNameForAgent $agentId
    $process = @(Get-Process -Name $processName -ErrorAction SilentlyContinue) | Select-Object -First 1
    if ($process) {
        $running += [pscustomobject]@{ Name = $processName; ProcessId = $process.Id }
    }
}
if ($running.Count -eq 0 -and $env:CLAUDECODE) {
    # Running inside a Claude Code session, which is the only signal where the
    # process list has nothing to show. It speaks for claude-code alone: an
    # operator who asked for codex is not told to restart something else.
    foreach ($agentId in $configured) {
        if ($agentId -eq 'claude-code' -or $agentId -eq 'claude') {
            $running += [pscustomobject]@{ Name = 'claude'; ProcessId = 'this session' }
        }
    }
}

if ($running.Count -eq 1) {
    Write-Step 5 "restart: $($running[0].Name) is running right now, and that is the one step left"
    Write-Host ''
    Write-Host '========================================================================'
    Write-Host "  RESTART REQUIRED: $($running[0].Name) is running right now (PID $($running[0].ProcessId))."
    Write-Host '  Quit that process and start it again once. An agent CLI reads its MCP'
    Write-Host '  registrations when a session starts, so the agentic-hil tools appear in'
    Write-Host '  the next session, not in this one.'
    Write-Host '========================================================================'
    Write-Host ''
} elseif ($running.Count -gt 1) {
    Write-Step 5 "restart: $($running.Count) of your agent CLIs are running right now, and that is the one step left"
    Write-Host ''
    Write-Host '========================================================================'
    Write-Host '  RESTART REQUIRED: these agent CLIs are running right now:'
    foreach ($entry in $running) {
        Write-Host "    $($entry.Name) (PID $($entry.ProcessId))"
    }
    Write-Host '  Quit each process and start it again once. An agent CLI reads its MCP'
    Write-Host '  registrations when a session starts, so the agentic-hil tools appear in'
    Write-Host '  the next session, not in this one.'
    Write-Host '========================================================================'
    Write-Host ''
} else {
    Write-Step 5 'restart: no agent CLI of yours is running, so there is nothing to restart'
    Write-Say "The next start of your agent has everything, and the first hardware question creates this project's configuration."
}
