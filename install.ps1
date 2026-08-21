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

# The release this script installs, and the number step 1 compares an
# installation already here against. It decides what the transcript calls the
# run, not whether the run happens: this line is the emergency anchor people
# re-run when `agentic-hil upgrade` itself is what broke, so an existing
# installation always reaches step 2's manager invocation, which is idempotent
# and answers for a copy that is already current in one quick resolve.
# Deliberately not a capability floor either: step 4 registers the skill out of
# whatever copy step 1 left in place, so a floor left a returning user on an old
# package and an old skill at once.
$Release = '0.18.0'
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

function Invoke-Uv {
    # One uv command, run with the certificate-retry branch around it. uv's
    # output is captured rather than streamed, because the text of a failure is
    # what decides whether there is a second attempt to make, and the refresh,
    # fresh and pin paths hand their own argument list to this one retry.
    param([string[]]$Arguments)
    $result = Invoke-Captured -File 'uv' -Arguments $Arguments
    Write-Host $result.Output.TrimEnd()
    if ($result.ExitCode -eq 0) { return }
    if ($script:SystemCertsMode -eq 'auto' -and (Test-TrustFailure $result.Output)) {
        Write-Say "certificates: that is a certificate uv cannot get to a root it carries, which is what a TLS-intercepting proxy looks like from inside uv; retrying once against this machine's own store, with verification still on"
        Enable-SystemCerts
        $script:SystemCertsMode = 'always'
        $retry = Invoke-Captured -File 'uv' -Arguments $Arguments
        Write-Host $retry.Output.TrimEnd()
        if ($retry.ExitCode -eq 0) { return }
        throw "uv could not install agentic-hil against this machine's own certificate store either; the proxy's own CA is missing from that store, and installing it there is the fix; TROUBLESHOOTING.md section 1 has the rest"
    }
    throw $UvInstallFailure
}

function Test-UvManagesTool {
    # Whether uv already owns this tool. A tool uv installed is one `uv tool
    # upgrade` can rebuild from the requirement uv itself recorded, extras and pin
    # included; a copy pip put on PATH is not one. The probe keeps a refresh on
    # the command that preserves what the tool was installed with, and falls back
    # to a plain install only when there is no uv tool for uv to upgrade.
    $list = Invoke-Captured -File 'uv' -Arguments @('tool', 'list')
    if ($list.ExitCode -ne 0) { return $false }
    foreach ($line in ($list.Output -split "`n")) {
        if ($line -match '^agentic-hil\s') { return $true }
    }
    return $false
}

function Get-UvRecordedRequirements {
    # The requirement set uv recorded for the tool it owns, as
    # @{ Extras = <string[]>; Withs = <string[]> }, or $null when the
    # reconstruction would change what uv recorded so the caller keeps to the
    # upgrade that preserves it verbatim instead. That covers a missing, unreadable
    # or empty receipt, a requirements array that never opened or never closed, a
    # recorded tool option (an explicit `[tool.options]` index), a root carrying a
    # pin or a git/path/url source rather than a plain name and extras, and any
    # `--with` this must not rebuild (a marker, a url, a git/path source). uv writes
    # `agentic-hil[can,pyocd] --with requests==2.32.5` into a receipt beside the
    # tool environment as a TOML array of requirement objects, inline for a lone
    # root requirement and one-per-line the moment a `--with` is added. Extras is
    # the agentic-hil root's extras; Withs is every other recorded requirement
    # rebuilt as a `--with` PEP 508 string (`name[extras]specifier`). Reading the
    # whole set back is what lets a refresh add the extra THIS run asks for while
    # keeping both a root extra an earlier install recorded and a `--with` it
    # recorded; a bare `tool install agentic-hil[...]` drops the latter.
    $probe = Invoke-Captured -File 'uv' -Arguments @('tool', 'dir')
    if ($probe.ExitCode -ne 0) { return $null }
    $receipt = Join-Path (Join-Path $probe.Output.Trim() 'agentic-hil') 'uv-receipt.toml'
    if (-not (Test-Path -LiteralPath $receipt)) { return $null }
    # $ErrorActionPreference is 'Stop' for the whole script, so an unreadable
    # receipt would terminate the repair; catch it and fall back to the preserving
    # upgrade instead. An empty receipt reads back as $null, which has no IndexOf.
    try {
        $text = Get-Content -LiteralPath $receipt -Raw
    } catch {
        return $null
    }
    if ([string]::IsNullOrEmpty($text)) { return $null }

    # A recorded tool option (an explicit index, a python pin) cannot be replayed
    # by the reconstruction, which passes none, so a receipt that records any is
    # refused and the caller keeps to the preserving upgrade. uv writes these under
    # a `[tool.options]` table (or sub-table / array of tables) only when there are
    # some, so a bare header with no key before the next section is no options.
    $inOptions = $false
    foreach ($line in ($text -split "`n")) {
        if ($line -match '^\s*\[\[?tool\.options') {
            if ($line -match '^\s*\[tool\.options\]\s*$') { $inOptions = $true }
            else { return $null }
        } elseif ($inOptions) {
            if ($line -match '^\s*\[') { $inOptions = $false }
            elseif ($line -match '\S' -and $line -notmatch '^\s*#') { return $null }
        }
    }

    # Isolate the requirements array by bracket depth, from `requirements = [` to
    # its matching `]`, so the nested `extras = [...]` arrays and the trailing
    # `entrypoints = [...]` block do not confuse where the requirement list ends.
    $anchor = 'requirements = ['
    $start = $text.IndexOf($anchor)
    if ($start -lt 0) { return $null }
    $depth = 1
    $interior = New-Object System.Text.StringBuilder
    for ($i = $start + $anchor.Length; $i -lt $text.Length -and $depth -gt 0; $i++) {
        $ch = $text[$i]
        if ($ch -eq '[') { $depth++; [void]$interior.Append($ch) }
        elseif ($ch -eq ']') { $depth--; if ($depth -gt 0) { [void]$interior.Append($ch) } }
        else { [void]$interior.Append($ch) }
    }
    if ($depth -ne 0) { return $null }

    $extras = New-Object System.Collections.Generic.List[string]
    $withs = New-Object System.Collections.Generic.List[string]
    $rootSeen = $false
    foreach ($object in [regex]::Matches($interior.ToString(), '\{[^{}]*\}')) {
        $obj = $object.Value
        $nameMatch = [regex]::Match($obj, 'name = "([^"]*)"')
        $name = if ($nameMatch.Success) { $nameMatch.Groups[1].Value } else { '' }
        if ($name -eq 'agentic-hil' -and -not $rootSeen) {
            # The root is rebuilt from its extras plus this run pin, so only name
            # and extras replay faithfully; a recorded specifier (a pin), a
            # directory/url/git source or a marker would be dropped, so refuse the
            # whole receipt.
            foreach ($key in [regex]::Matches($obj, '([A-Za-z][A-Za-z_-]*)[ ]*=')) {
                $k = $key.Groups[1].Value
                if ($k -ne 'name' -and $k -ne 'extras') { return $null }
            }
            $rootSeen = $true
            $extrasMatch = [regex]::Match($obj, 'extras = \[([^\]]*)\]')
            if ($extrasMatch.Success) {
                foreach ($quoted in [regex]::Matches($extrasMatch.Groups[1].Value, '"([^"]*)"')) {
                    $extras.Add($quoted.Groups[1].Value)
                }
            }
            continue
        }
        # A non-root requirement is replayed as --with, so it must rebuild to a bare
        # PEP 508 string: only name/extras/specifier, and no whitespace in the
        # result. Anything else (a marker, a url) refuses the whole receipt.
        foreach ($key in [regex]::Matches($obj, '([A-Za-z][A-Za-z_-]*)[ ]*=')) {
            $k = $key.Groups[1].Value
            if ($k -ne 'name' -and $k -ne 'extras' -and $k -ne 'specifier') { return $null }
        }
        if (-not $nameMatch.Success) { return $null }
        $req = $name
        $extrasMatch = [regex]::Match($obj, 'extras = \[([^\]]*)\]')
        if ($extrasMatch.Success) {
            $items = New-Object System.Collections.Generic.List[string]
            foreach ($quoted in [regex]::Matches($extrasMatch.Groups[1].Value, '"([^"]*)"')) {
                $items.Add($quoted.Groups[1].Value)
            }
            if ($items.Count -gt 0) { $req += '[' + ([string]::Join(',', $items)) + ']' }
        }
        $specMatch = [regex]::Match($obj, 'specifier = "([^"]*)"')
        if ($specMatch.Success) { $req += $specMatch.Groups[1].Value }
        if ($req -match '\s') { return $null }
        $withs.Add($req)
    }
    if (-not $rootSeen) { return $null }
    # Hashtable member assignment preserves an array as-is (no unrolling and no
    # array-wrap comma), so an empty or single Extras/Withs stays the array
    # Get-RefreshSpec and the --with loop iterate over.
    return @{ Extras = $extras.ToArray(); Withs = $withs.ToArray() }
}

function Get-RefreshSpec {
    # This run's extras merged with the ones uv already recorded, spelled
    # agentic-hil[...] with this run's pin if there is one. Merging adds the [can]
    # a --can run wants even to a bare recorded requirement, and keeps a recorded
    # extra (a hand-added pyocd) a bare `tool install agentic-hil[can]` would drop.
    param([string[]]$Recorded)
    $extras = New-Object System.Collections.Generic.List[string]
    if ($WithCan) { $extras.Add('can') }
    foreach ($extra in $Recorded) {
        if (-not $extras.Contains($extra)) { $extras.Add($extra) }
    }
    $spec = 'agentic-hil'
    if ($extras.Count -gt 0) { $spec = "agentic-hil[$([string]::Join(',', $extras))]" }
    if ($Version) { $spec = "$spec==$Version" }
    return $spec
}

function Install-WithUv {
    if ($script:InstallMode -eq 'refresh') {
        if (Test-UvManagesTool) {
            # uv owns this tool. Reinstall from the requirement uv recorded merged
            # with this run's extras: the recorded `[can,pyocd]` survives (a
            # `tool install agentic-hil[can]` would drop pyocd) and a `--can` a
            # bare recorded requirement never had is added (a `tool upgrade` would
            # never add it). --reinstall replaces the files even when the recorded
            # version is already current, which is the repair the anchor exists
            # for. A recorded `--with` requirement is replayed as its own --with so
            # the reinstall keeps it too; a bare `tool install agentic-hil[...]`
            # would drop it. When uv keeps no readable receipt, or records a
            # requirement this cannot rebuild without changing it, fall back to the
            # upgrade that preserves whatever it did record.
            $recorded = Get-UvRecordedRequirements
            if ($null -ne $recorded) {
                $uvArgs = @('tool', 'install', '--upgrade', '--reinstall', (Get-RefreshSpec -Recorded $recorded.Extras))
                foreach ($recordedWith in $recorded.Withs) { $uvArgs += @('--with', $recordedWith) }
                Invoke-Uv -Arguments $uvArgs
            } else {
                Invoke-Uv -Arguments @('tool', 'upgrade', '--reinstall', 'agentic-hil')
            }
            return
        }
        Invoke-Uv -Arguments @('tool', 'install', '--upgrade', '--reinstall', (Get-PackageSpec))
        return
    }
    if ($script:InstallMode -eq 'pin') {
        # A named release sets the requirement outright, so it goes through
        # install; --reinstall forces the replacement even when the installed
        # version already equals the pin.
        Invoke-Uv -Arguments @('tool', 'install', '--upgrade', '--reinstall', (Get-PackageSpec))
        return
    }
    Invoke-Uv -Arguments @('tool', 'install', '--upgrade', (Get-PackageSpec))
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

function Test-VersionIsDevelopment {
    param([string]$Found)
    # A development tree: an editable checkout of this repository, which reports
    # X.Y.Z.devN. It is the one installation already here that this script keeps
    # instead of sending through the manager, whatever number it carries, because
    # step 2 would replace the operator's own working copy with a release from
    # PyPI (#291). The marker is a suffix and not a number, so it is read as one:
    # the comparisons above match three numeric fields by design, which is what
    # lets a development version compare as its X.Y.Z, and is also why neither of
    # them can see this.
    return ($Found -match '\.dev(\d|$)')
}

function Test-VersionMatchesRequest {
    param([string]$Found)
    # Does a freshly installed copy's reported version answer what this run asked
    # for. A pin has to match exactly: the documented --version contract is an exact
    # release, so a newer copy left in the manager's bin is not the release this run
    # wrote. An unpinned run named no version at all, so there is nothing here to
    # compare it against; the release decides one thing only, in step 1, where it
    # is what that step calls the run, and what proves a copy at step 3 is where it
    # sits.
    if ($Version) { return (Test-VersionExactly -Found $Found -Wanted $Version) }
    return $true
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

# Step 1: what is already here, and what does this run call itself.
#
# An installation found here goes through step 2 either way. This line is the
# rescue path for an installation that is broken, or whose own `agentic-hil
# upgrade` is what broke, and answering "nothing to install" to the person who
# just watched their upgrade fail left the anchor with nothing to anchor. The
# comparison against the release chooses the word, upgrading or refreshing, and
# a development tree is the one copy that is kept.
$needsPackage = $true
# InstallMode says what step 2 asks the manager to do, not whether it runs.
# `fresh` is a first install with nothing to reinstall; `refresh` is a copy
# already here that this run replaces in place, which neither uv nor pip does for
# an already-current version unless told to; `pin` is `refresh` with the
# operator's own release named, which sets the requirement outright.
$InstallMode = 'fresh'
if (Test-Executable 'agentic-hil') {
    $probe = Invoke-Captured -File 'agentic-hil' -Arguments @('--version')
    $installed = $probe.Output.Trim()
    if ($probe.ExitCode -ne 0) {
        $InstallMode = 'refresh'
        Write-Step 1 'probe: an agentic-hil on this PATH does not answer, installing it again'
    } elseif ($Version) {
        $InstallMode = 'pin'
        Write-Step 1 "probe: agentic-hil $installed is here, and --version $Version was asked for, so the package is installed again"
    } elseif (Test-VersionIsDevelopment -Found $installed) {
        Write-Step 1 "probe: agentic-hil $installed is a development version, so it is kept: installing over it would replace an editable checkout with a release from PyPI"
        $needsPackage = $false
    } elseif (Test-VersionAtLeast -Found $installed -Floor $Release) {
        $InstallMode = 'refresh'
        Write-Step 1 "probe: agentic-hil $installed is here and not older than $Release, refreshing this current installation"
    } else {
        $InstallMode = 'refresh'
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
    Write-Step 2 'package: nothing to install, the development installation stays as it is'
} elseif (Test-Executable 'uv') {
    Write-Step 2 "package: uv is here, installing $(Get-PackageSpec) user-local with uv tool install"
    Install-WithUv
    $packageManager = 'uv'
} else {
    $pythonCommand = Find-Python
    if ($pythonCommand) {
        Write-Step 2 "package: installing $(Get-PackageSpec) user-local with $pythonCommand -m pip install --user"
        # pip leaves a package whose installed version already satisfies the
        # request untouched under --upgrade, so a refresh or a pin adds
        # --force-reinstall to make it replace the files; a first install has
        # nothing to reinstall. pip keeps no requirement of its own, so the extras
        # a refresh preserves are the ones already on disk, which --force-reinstall
        # does not remove.
        $pipArguments = @('-m', 'pip', 'install', '--user', '--upgrade')
        if ($InstallMode -eq 'refresh' -or $InstallMode -eq 'pin') { $pipArguments += '--force-reinstall' }
        $pipArguments += (Get-PackageSpec)
        $pip = Invoke-Captured -File $pythonCommand -Arguments $pipArguments
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
# step 1 kept a development installation and this run installed nothing; once
# this run installs a copy, it becomes that copy's own path, so an older
# agentic-hil earlier on PATH cannot answer for the install that just happened.
$AgenticHilCmd = 'agentic-hil'

# Step 3: say where it landed, and edit nobody's profile script.
if (-not $needsPackage) {
    # Nothing was installed: the development copy step 1 kept on this PATH is the
    # one the machine half uses, and it already resolves here.
    Write-Step 3 "PATH: agentic-hil $installed is already here and was kept, nothing to add"
} else {
    # The manager's own destination directory, and only that. Step 2 installed into
    # exactly this directory, with --upgrade, so a copy that answers --version here
    # is the copy the manager just wrote and the newest the index served. The
    # placement is the proof: an older or unrelated agentic-hil elsewhere cannot get
    # into the directory the manager names.
    #
    # Requiring the release floor on top of that placement added no stale-copy
    # protection and cost the release window. Between the merge of a release commit
    # and the PyPI publish the index still serves the release below, so the script
    # demanded a version nobody could install yet and refused a wholly correct fresh
    # install as possibly stale (#310). The release keeps its one real job, in step
    # 1, where it decides whether a copy already here is called upgraded or
    # refreshed.
    #
    # A pin is the one case where a version still has to be checked here. There the
    # operator named a release, step 2 either installed exactly that or failed
    # outright, and a copy in the manager's bin reporting anything else is a
    # leftover rather than this run's work.
    #
    # If the manager cannot name its destination, or the copy there does not answer,
    # $found stays empty and the machine half is refused rather than run against a
    # guessed copy.
    $found = ''
    $directory = Get-ManagerBinDirectory -PythonCommand $pythonCommand -PackageManager $packageManager
    if ($directory) {
        $executable = Join-Path $directory 'agentic-hil.exe'
        if (Test-Path $executable) {
            $probe = Invoke-Captured -File $executable -Arguments @('--version')
            if ($probe.ExitCode -eq 0) {
                $reported = "$($probe.Output)".Trim()
                if ($reported -and (Test-VersionMatchesRequest -Found $reported)) {
                    $found = $directory
                }
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
