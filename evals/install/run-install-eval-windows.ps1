[CmdletBinding()]
param(
    [string[]]$Agents = @("codex", "opencode"),
    [string[]]$Cases = @("quickstart", "preserve-user-config", "unsafe-existing-config", "firmware-routing"),
    # The skill is always installed in a real setup, so measuring without it is
    # not a shipped configuration: it answers one question, whether the skill
    # earns its place, and only on request.
    [switch]$WithControlArms,
    [ValidateRange(2, 20)]
    [int]$Repetitions = 2,
    [ValidateRange(2, 20)]
    [int]$MaxRepetitions = 5,
    [string]$Output,
    [string[]]$CodexModels = @("gpt-5.6-sol"),
    [string[]]$ClaudeModels = @("claude-sonnet-4-6"),
    [string[]]$OpencodeModels = @("openai/gpt-5.6-sol"),
    # Empty means the axis is not exercised: no flag is passed and each CLI's
    # own default applies. It is never a synonym for a named level.
    [string[]]$ReasoningEfforts = @(),
    [ValidateRange(60, 7200)]
    [int]$TimeoutSeconds = 1800,
    # How long a run may say nothing before it is stopped. A provider that stops
    # answering leaves a process alive and silent, and the total budget notices
    # that only at its very end.
    [ValidateRange(30, 3600)]
    [int]$IdleTimeoutSeconds = 300,
    # Each run in flight is capped at 2 CPUs and 2 GB, and the busiest container
    # of a measured matrix peaked at 654 MiB, so memory is not what limits this.
    # Writing a refreshed login back is serialized regardless, so a shared login
    # file cannot be raced.
    [ValidateRange(1, 16)]
    [int]$Concurrency = 1,
    # What actually limits it is the provider. Six workers plus a longest-first
    # order sent four runs of one model off within 21 seconds and every one went
    # silent for 300 s, though that model needs under a minute on its own.
    [ValidateRange(1, 8)]
    [int]$PerModelConcurrency = 2,
    # An earlier artifact directory whose measured durations decide what starts
    # first. Without it the order is as declared, and one long run started late
    # leaves every other worker idle at the end.
    [string]$OrderFrom,
    # Read the guide from the remote at this commit and install from it, instead
    # of from the mounted working tree.
    [switch]$FromBranch,
    # The guide link to hand the agents, verbatim. The one out of README.md
    # measures the release the way an engineer hands it over. Whatever that
    # guide's published path installs is what gets verified.
    [string]$Guide,
    # The release published mode is held to. Published mode installs the current
    # release from the index, not this working tree, which between releases
    # carries a .devN version the release does not; pinning that development
    # version fails every otherwise-correct published install against a version
    # nobody can install yet. Left empty, the release is read from the newest
    # vX.Y.Z tag in this clone. Name it here to pick a specific release instead
    # of the newest, e.g. to measure an older one. A matching vX.Y.Z tag has to
    # be present in this clone either way: published mode reads that tag to stage
    # the trusted package the install's bytes and the agent skill are checked
    # against, so it cannot stand in for a missing tag, only pick among the ones
    # present, and a version with no tag here is refused before the matrix
    # starts. Only -Guide reads it.
    [string]$ExpectedVersion,
    [switch]$SkipBuild,
    [switch]$NoFileLogin,
    [switch]$RefreshLogin,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

trap {
    Write-Host ""
    Write-Host "Evaluation did not run: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$virtualEnvironmentPython = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
$setupScript = Join-Path $PSScriptRoot "setup-environment-windows.ps1"

if (-not (Test-Path -LiteralPath $virtualEnvironmentPython -PathType Leaf)) {
    throw (
        "The development environment is missing: $virtualEnvironmentPython. " +
        "Run first: powershell -ExecutionPolicy Bypass -File `"$setupScript`""
    )
}

function Split-Values {
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Values)

    # "powershell -File" passes "a,b" as one string, so accept both that and a
    # real array from an interactive session.
    $items = @()
    foreach ($value in $Values) {
        foreach ($part in ([string]$value -split ",")) {
            $name = $part.Trim()
            if ($name) {
                $items += $name
            }
        }
    }
    return @($items | Select-Object -Unique)
}

function Expand-Selection {
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Values,
        [Parameter(Mandatory)][string[]]$Allowed,
        [Parameter(Mandatory)][string]$Label
    )

    $selected = Split-Values -Values $Values
    foreach ($name in $selected) {
        if ($Allowed -notcontains $name) {
            throw "Unsupported $Label '$name'. Choose from: $($Allowed -join ', ')."
        }
    }
    if ($selected.Count -eq 0) {
        throw "At least one $Label is required."
    }
    return $selected
}

function Get-DockerCli {
    $command = Get-Command docker.exe -ErrorAction SilentlyContinue
    $candidates = @()
    if ($null -ne $command -and $command.Source) {
        $candidates += $command.Source
    }
    $candidates += @(
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Docker\Docker\resources\bin\docker.exe"),
        (Join-Path $env:LOCALAPPDATA "Docker\resources\bin\docker.exe"),
        (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe")
    )
    $docker = $candidates |
        Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
        Select-Object -First 1
    if ($null -eq $docker) {
        throw (
            "docker.exe could not be resolved. Run the environment setup first: " +
            "powershell -ExecutionPolicy Bypass -File `"$setupScript`""
        )
    }
    return [IO.Path]::GetFullPath([string]$docker)
}

function Get-FirstSetEnvironmentName {
    param([Parameter(Mandatory)][string[]]$Names)

    foreach ($name in $Names) {
        if ([Environment]::GetEnvironmentVariable($name)) {
            return $name
        }
    }
    return $null
}

function New-AgentJob {
    param(
        [Parameter(Mandatory)][string]$Agent,
        [Parameter(Mandatory)][string]$Model,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Effort
    )

    # An environment credential is preferred because it needs no file mount. A
    # file-backed login is the documented fallback for Codex and OpenCode.
    $environmentNames = switch ($Agent) {
        "codex" { @("CODEX_ACCESS_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY") }
        "claude-code" { @("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN") }
        default { @("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY") }
    }
    $environmentName = Get-FirstSetEnvironmentName -Names $environmentNames
    if ($null -ne $environmentName) {
        Write-Host "$Agent : $Model$(if ($Effort) { " at $Effort" }) (credential $environmentName)"
        $job = @{
            agent = $Agent
            model = $Model
            credentials = @($environmentName)
        }
        if ($Effort) { $job.reasoning_effort = $Effort }
        return $job
    }

    $fileCredential = switch ($Agent) {
        "codex" {
            @{
                Kind = "codex-auth"
                Variable = "CODEX_AUTH_FILE"
                Path = Join-Path $env:USERPROFILE ".codex\auth.json"
            }
        }
        "claude-code" {
            @{
                Kind = "claude-auth"
                Variable = "CLAUDE_AUTH_FILE"
                Path = Join-Path $env:USERPROFILE ".claude\.credentials.json"
            }
        }
        "opencode" {
            @{
                Kind = "opencode-auth"
                Variable = "OPENCODE_AUTH_FILE"
                Path = Join-Path $env:USERPROFILE ".local\share\opencode\auth.json"
            }
        }
        default { $null }
    }
    if ($null -eq $fileCredential -or -not (Test-Path -LiteralPath $fileCredential.Path -PathType Leaf)) {
        throw (
            "No credential is available for $Agent. Set one of: $($environmentNames -join ', '), " +
            "or sign the agent CLI in so its own login file exists."
        )
    }

    if ($NoFileLogin) {
        throw (
            "$Agent has no evaluation credential, and -NoFileLogin forbids the interactive login at " +
            "$($fileCredential.Path). Set one of $($environmentNames -join ', ') instead; for Claude Code, " +
            "'claude setup-token' mints one for automation."
        )
    }
    # Refreshing a stored login inside the container can rotate its refresh
    # token, which leaves the copy on this machine rejected by the provider.
    Write-Host "$Agent : using the stored interactive login; a refresh may invalidate it here" -ForegroundColor Yellow

    # The matrix stores only the variable name; the runner reads the path from it.
    $resolved = (Resolve-Path -LiteralPath $fileCredential.Path).Path
    [Environment]::SetEnvironmentVariable($fileCredential.Variable, $resolved, "Process")
    Write-Host "$Agent : $Model$(if ($Effort) { " at $Effort" }) (file login $($fileCredential.Kind))"
    $job = @{
        agent = $Agent
        model = $Model
        credential_files = @(
            @{
                kind = $fileCredential.Kind
                path_environment = $fileCredential.Variable
            }
        )
    }
    if ($Effort) { $job.reasoning_effort = $Effort }
    return $job
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Document
    )

    # The matrix loader tolerates a byte order mark, but writing without one
    # keeps the file identical to a hand-written matrix.
    $json = $Document | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($Path, $json, (New-Object Text.UTF8Encoding $false))
}

function Get-PublishedReleaseVersion {
    # The release a published-mode run is held to, which is never this tree's
    # own version: between releases the working tree carries a development
    # version the published release does not, and the verifier requires the
    # installed distribution to equal `target.expected_version`, so pinning the
    # development version there fails a wholly correct install of the release.
    #
    # The release is the newest vX.Y.Z tag in this clone, or the one an
    # -ExpectedVersion override names when a specific release is wanted. Either
    # way a matching tag has to be present here, and not merely to name the
    # release: published mode reads `src/agentic_hil` at that tag to stage the
    # trusted package the installed bytes and the agent skill are compared
    # against (see `released_package_digest` and `packaged_skill_reference`),
    # so a release this clone carries no tag for cannot produce a passing
    # published verdict at all. An override for a version with no tag is refused
    # here, before the matrix starts, rather than after a run has spent model
    # time and API budget on a verdict it could never reach.
    param(
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [string]$Override
    )

    $tags = @(
        git -C $RepositoryRoot tag --list 'v[0-9]*' 2>$null |
            Where-Object { $_ -match '^v[0-9]+\.[0-9]+\.[0-9]+$' } |
            ForEach-Object { $_.Substring(1) }
    )

    if ($Override) {
        if ($Override -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
            throw "-ExpectedVersion must name a released version as X.Y.Z, not '$Override'."
        }
        if ($tags -notcontains $Override) {
            throw (
                "-ExpectedVersion $Override names a release this clone carries no v$Override tag for. Published " +
                "mode reads that tag to stage the trusted package the install's bytes and the agent skill are " +
                "checked against, so a run pinned to a version with no tag here cannot pass. Fetch the tags " +
                "(git fetch --tags), or name a release this clone already has a tag for."
            )
        }
        return $Override
    }

    if ($tags.Count -eq 0) {
        throw (
            "Published mode names the current release, which this clone cannot state: it carries no vX.Y.Z " +
            "release tag. That tag is also what published mode reads to stage the trusted package it verifies " +
            "the install against, so fetch the tags (git fetch --tags) before running it; -ExpectedVersion " +
            "cannot stand in for a missing tag, only pick among the ones present."
        )
    }
    return ($tags | Sort-Object { [version]$_ } | Select-Object -Last 1)
}

$projectVersion = & $virtualEnvironmentPython -c "import tomllib,pathlib;print(tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'])"
if ($LASTEXITCODE -ne 0 -or -not $projectVersion) {
    throw "The project version could not be read from pyproject.toml."
}

if ($MaxRepetitions -lt $Repetitions) {
    throw "-MaxRepetitions ($MaxRepetitions) must not be below -Repetitions ($Repetitions)."
}
$Agents = Expand-Selection -Values $Agents -Allowed @("codex", "claude-code", "opencode") -Label "agent"
$Cases = Expand-Selection `
    -Values $Cases `
    -Allowed @(
        "quickstart",
        "preserve-user-config",
        "unsafe-existing-config",
        "firmware-routing",
        "firmware-routing-without-skill",
        "firmware-readiness",
        "firmware-readiness-without-skill",
        "firmware-flash-request",
        "firmware-flash-request-without-skill"
    ) `
    -Label "case"

# A control arm is only meaningful beside the case it is the control for, so it
# is derived from the selection rather than typed out, and never runs unless
# it is asked for.
$controlArms = @($Cases | Where-Object { $_ -like "*-without-skill" })
if ($WithControlArms) {
    $derived = @($Cases | Where-Object { $_ -notlike "*-without-skill" } | ForEach-Object { "$_-without-skill" })
    $available = @($derived | Where-Object { Test-Path -LiteralPath (Join-Path $PSScriptRoot "cases\$_.json") })
    if ($available.Count -eq 0) {
        throw "-WithControlArms was given, but none of the selected cases has one: $($Cases -join ', ')."
    }
    $Cases = @($Cases) + @($available | Where-Object { $Cases -notcontains $_ })
    Write-Host "Arms:       treatment and control, $($available.Count) pair(s)"
}
elseif ($controlArms.Count -gt 0) {
    throw (
        "Naming a control arm directly measures it without its treatment arm, which answers nothing. " +
        "Select $($controlArms[0] -replace '-without-skill$', '') and pass -WithControlArms instead."
    )
}

if (-not $Output) {
    $Output = Join-Path $repositoryRoot ("evals\install\artifacts\run-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}
# The runner creates the artifact directory itself and refuses an existing one,
# so that a run can never overwrite earlier evidence.
if (-not [IO.Path]::IsPathRooted($Output)) {
    $Output = Join-Path (Get-Location).Path $Output
}
$Output = [IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $Output) {
    throw "The artifact directory already exists: $Output. Choose another -Output."
}

Write-Host "== Installation evaluation"
Write-Host "Repository: $repositoryRoot"
Write-Host "Version:    $($projectVersion.Trim())"
Write-Host "Output:     $Output"
Write-Host "Cases:      $($Cases -join ', ')"
Write-Host "Runs:       $Repetitions repetitions per combination, up to $MaxRepetitions while they disagree"

$models = @{
    "codex" = (Split-Values -Values $CodexModels)
    "claude-code" = (Split-Values -Values $ClaudeModels)
    "opencode" = (Split-Values -Values $OpencodeModels)
}
$efforts = Split-Values -Values $ReasoningEfforts
if ($efforts.Count -gt 0) {
    $efforts = Expand-Selection -Values $efforts -Allowed @("low", "medium", "high", "xhigh", "max") -Label "reasoning effort"
}
else {
    # One job per model, with no effort flag at all.
    $efforts = @("")
}

$measured = @{}
if ($OrderFrom) {
    if (-not (Test-Path -LiteralPath $OrderFrom)) {
        throw "The artifact directory to order from does not exist: $OrderFrom"
    }
    $timings = & $virtualEnvironmentPython -m evals.install timings --output $OrderFrom
    if ($LASTEXITCODE -ne 0) {
        throw "Measured durations could not be read from $OrderFrom."
    }
    ($timings -join "`n" | ConvertFrom-Json).PSObject.Properties | ForEach-Object {
        $measured[$_.Name] = [double]$_.Value
    }
    Write-Host "Order:      slowest first, rotating between models, from $($measured.Count) measured combination(s) in $OrderFrom"
}

$jobs = @()
foreach ($agent in $Agents) {
    if ($models[$agent].Count -eq 0) {
        throw "At least one model is required for $agent."
    }
    foreach ($model in $models[$agent]) {
        foreach ($effort in $efforts) {
            $job = New-AgentJob -Agent $agent -Model $model -Effort $effort
            $expected = $measured["$agent/$model"]
            if ($expected) { $job.expected_seconds = $expected }
            $jobs += $job
        }
    }
}

$casePaths = @()
foreach ($case in $Cases) {
    $casePath = Join-Path $PSScriptRoot "cases\$case.json"
    if (-not (Test-Path -LiteralPath $casePath -PathType Leaf)) {
        throw "Case definition is missing: $casePath"
    }
    $casePaths += ((Resolve-Path -LiteralPath $casePath).Path -replace "\\", "/")
}

# Point the models at the branch, not at a copy of it: the guide they read is
# the one on the remote at this commit, so a change measured here is a change
# anyone else pulling this branch gets. Requires the commit to be pushed.
#
# Local and remote install this working tree, so the version every artifact is
# held to is the tree's own, development suffix and all; -ExpectedVersion names
# the published release and has nothing to override on either, so it is refused
# here rather than silently ignored.
if ($ExpectedVersion -and -not $Guide) {
    throw "-ExpectedVersion names the published release and only applies with -Guide; local and remote install this tree and take its own version."
}
$target = @{ mode = "local"; expected_version = $projectVersion.Trim() }
if ($FromBranch) {
    $head = (git rev-parse HEAD).Trim()
    $onRemote = (git branch -r --contains $head 2>$null | Measure-Object).Count
    if ($onRemote -eq 0) { throw "HEAD $head is not on any remote branch. Push it, then run again." }
    $target = @{
        mode = "remote"
        expected_version = $projectVersion.Trim()
        install_spec = "git+https://github.com/agentic-hil/agentic-hil@$head"
        expected_commit = $head
    }
    Write-Host "Source:     the remote at $head"
}
if ($Guide) {
    if ($FromBranch) { throw "-Guide and -FromBranch name different sources; pick one." }
    # The release the guide's own path installs, never this tree's development
    # version: published mode preserves expected_version through to the verifier,
    # which fails an install that does not report exactly it.
    $publishedVersion = Get-PublishedReleaseVersion -RepositoryRoot $repositoryRoot -Override $ExpectedVersion
    $target = @{ mode = "published"; expected_version = $publishedVersion; guide_url = $Guide }
    Write-Host "Guide:      $Guide"
    Write-Host "Release:    $publishedVersion (the guide installs the published release; this tree is $($projectVersion.Trim()))"
}

$matrixPath = Join-Path ([IO.Path]::GetTempPath()) ("agentic-hil-eval-matrix-" + [Guid]::NewGuid().ToString("N") + ".json")
Write-JsonFile -Path $matrixPath -Document @{
    schema_version = 1
    image = "agentic-hil-install-eval:local"
    cases = $casePaths
    repetitions = $Repetitions
    max_repetitions = $MaxRepetitions
    timeout_seconds = $TimeoutSeconds
    idle_timeout_seconds = $IdleTimeoutSeconds
    target = $target
    jobs = $jobs
}
Write-Host "Jobs:       $($jobs.Count) agent/model combination(s)"

Push-Location $repositoryRoot
try {
    if (-not $DryRun -and -not $SkipBuild) {
        Write-Host ""
        Write-Host "== Versioned evaluation image"
        # The image embeds the evaluator and verifier, so it must be rebuilt
        # whenever evals/ changes; otherwise the run measures stale code.
        & $virtualEnvironmentPython -m evals.install build --docker (Get-DockerCli)
        if ($LASTEXITCODE -ne 0) {
            throw "The evaluation image could not be built."
        }
    }

    Write-Host ""
    Write-Host "== Matrix run"
    $runArguments = @("-m", "evals.install", "run", "--matrix", $matrixPath, "--output", $Output)
    if ($DryRun) {
        $runArguments += "--dry-run"
    }
    else {
        $runArguments += @("--docker", (Get-DockerCli), "--concurrency", $Concurrency, "--per-model-concurrency", $PerModelConcurrency)
        if ($RefreshLogin) {
            $runArguments += "--refresh-login"
        }
    }
    & $virtualEnvironmentPython @runArguments
    $runExitCode = $LASTEXITCODE
    if ($DryRun) {
        Write-Host ""
        Write-Host "Dry run complete. No container was started."
        exit $runExitCode
    }

    if (-not (Get-ChildItem -Path $Output -Filter "result.json" -Recurse -File -ErrorAction SilentlyContinue)) {
        throw "The matrix run produced no results (exit code $runExitCode). See the message above."
    }
    # Keep the exact input beside the evidence it produced.
    Copy-Item -LiteralPath $matrixPath -Destination (Join-Path $Output "matrix.json")
    Write-Host "Matrix:     $(Join-Path $Output 'matrix.json')"

    Write-Host ""
    Write-Host "== Report"
    & $virtualEnvironmentPython -m evals.install report --output $Output
    $reportExitCode = $LASTEXITCODE

    if (@($Cases | Where-Object { $_ -like "firmware-*" }).Count -gt 0) {
        Write-Host ""
        Write-Host "== Firmware routing"
        # Which surface answered the hardware question: the MCP server, the CLI,
        # a raw command, or the configuration file.
        & $virtualEnvironmentPython -m evals.install routing --output $Output
        $routingExitCode = $LASTEXITCODE
    }
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $matrixPath -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($reportExitCode -eq 0 -and $routingExitCode -and $routingExitCode -ne 0) {
    Write-Host "Every run passed verification, but not every one used the MCP server." -ForegroundColor Yellow
    Write-Host "Read the routing report above: the skill has to send firmware work through MCP."
    exit $routingExitCode
}
if ($reportExitCode -eq 0) {
    Write-Host "Every run passed independent verification." -ForegroundColor Green
    exit 0
}
Write-Host "At least one run failed. Read the transcripts listed above, then improve" -ForegroundColor Yellow
Write-Host "AI_AGENT_QUICKSTART.md or the product where the agent stumbled, and run again."
if ($runExitCode -ne 0 -and $reportExitCode -eq 0) {
    exit $runExitCode
}
exit $reportExitCode
