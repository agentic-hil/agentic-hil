[CmdletBinding()]
param(
    [string[]]$Agents = @("codex", "opencode"),
    [string[]]$Cases = @("quickstart", "preserve-user-config", "unsafe-existing-config", "firmware-routing"),
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
    # Each run in flight holds 2 CPUs and 4 GB. Writing a refreshed login back is
    # serialized regardless, so a shared login file cannot be raced.
    [ValidateRange(1, 8)]
    [int]$Concurrency = 1,
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

$jobs = @()
foreach ($agent in $Agents) {
    if ($models[$agent].Count -eq 0) {
        throw "At least one model is required for $agent."
    }
    foreach ($model in $models[$agent]) {
        foreach ($effort in $efforts) {
            $jobs += New-AgentJob -Agent $agent -Model $model -Effort $effort
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

$matrixPath = Join-Path ([IO.Path]::GetTempPath()) ("agentic-hil-eval-matrix-" + [Guid]::NewGuid().ToString("N") + ".json")
Write-JsonFile -Path $matrixPath -Document @{
    schema_version = 1
    image = "agentic-hil-install-eval:local"
    cases = $casePaths
    repetitions = $Repetitions
    max_repetitions = $MaxRepetitions
    timeout_seconds = $TimeoutSeconds
    target = @{
        mode = "local"
        expected_version = $projectVersion.Trim()
    }
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
        $runArguments += @("--docker", (Get-DockerCli), "--concurrency", $Concurrency)
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
