[CmdletBinding()]
param(
    [string[]]$Agents = @("codex", "opencode"),
    [string[]]$Cases = @("quickstart", "preserve-user-config", "unsafe-existing-config"),
    [ValidateRange(1, 20)]
    [int]$Repetitions = 1,
    [string]$Output,
    [string]$CodexModel = "gpt-5.6-sol",
    [string]$ClaudeModel = "claude-sonnet-4-6",
    [string]$OpencodeModel = "openai/gpt-5.6-sol",
    [ValidateRange(60, 7200)]
    [int]$TimeoutSeconds = 1800,
    [switch]$SkipBuild,
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

function Expand-Selection {
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Values,
        [Parameter(Mandatory)][string[]]$Allowed,
        [Parameter(Mandatory)][string]$Label
    )

    # "powershell -File" passes "a,b" as one string, so accept both that and a
    # real array from an interactive session.
    $selected = @()
    foreach ($value in $Values) {
        foreach ($part in ([string]$value -split ",")) {
            $name = $part.Trim()
            if (-not $name) {
                continue
            }
            if ($Allowed -notcontains $name) {
                throw "Unsupported $Label '$name'. Choose from: $($Allowed -join ', ')."
            }
            $selected += $name
        }
    }
    if ($selected.Count -eq 0) {
        throw "At least one $Label is required."
    }
    return @($selected | Select-Object -Unique)
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
        [Parameter(Mandatory)][string]$Model
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
        Write-Host "$Agent : $Model (credential $environmentName)"
        return @{
            agent = $Agent
            model = $Model
            credentials = @($environmentName)
        }
    }

    $fileCredential = switch ($Agent) {
        "codex" {
            @{
                Kind = "codex-auth"
                Variable = "CODEX_AUTH_FILE"
                Path = Join-Path $env:USERPROFILE ".codex\auth.json"
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
            "No credential is available for $Agent. Set one of: $($environmentNames -join ', '). " +
            "Codex and OpenCode also accept a file login."
        )
    }

    # The matrix stores only the variable name; the runner reads the path from it.
    $resolved = (Resolve-Path -LiteralPath $fileCredential.Path).Path
    [Environment]::SetEnvironmentVariable($fileCredential.Variable, $resolved, "Process")
    Write-Host "$Agent : $Model (file login $($fileCredential.Kind))"
    return @{
        agent = $Agent
        model = $Model
        credential_files = @(
            @{
                kind = $fileCredential.Kind
                path_environment = $fileCredential.Variable
            }
        )
    }
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

$Agents = Expand-Selection -Values $Agents -Allowed @("codex", "claude-code", "opencode") -Label "agent"
$Cases = Expand-Selection `
    -Values $Cases `
    -Allowed @("quickstart", "preserve-user-config", "unsafe-existing-config") `
    -Label "case"

if (-not $Output) {
    $Output = Join-Path $repositoryRoot ("evals\install\artifacts\run-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}
New-Item -ItemType Directory -Force -Path $Output | Out-Null
$Output = (Resolve-Path -LiteralPath $Output).Path

Write-Host "== Installation evaluation"
Write-Host "Repository: $repositoryRoot"
Write-Host "Version:    $($projectVersion.Trim())"
Write-Host "Output:     $Output"
Write-Host "Cases:      $($Cases -join ', ') x $Repetitions repetition(s)"

$models = @{
    "codex" = $CodexModel
    "claude-code" = $ClaudeModel
    "opencode" = $OpencodeModel
}
$jobs = @()
foreach ($agent in $Agents) {
    $jobs += New-AgentJob -Agent $agent -Model $models[$agent]
}

$casePaths = @()
foreach ($case in $Cases) {
    $casePath = Join-Path $PSScriptRoot "cases\$case.json"
    if (-not (Test-Path -LiteralPath $casePath -PathType Leaf)) {
        throw "Case definition is missing: $casePath"
    }
    $casePaths += ((Resolve-Path -LiteralPath $casePath).Path -replace "\\", "/")
}

$matrixPath = Join-Path $Output "matrix.json"
Write-JsonFile -Path $matrixPath -Document @{
    schema_version = 1
    image = "agentic-hil-install-eval:local"
    cases = $casePaths
    repetitions = $Repetitions
    timeout_seconds = $TimeoutSeconds
    target = @{
        mode = "local"
        expected_version = $projectVersion.Trim()
    }
    jobs = $jobs
}
Write-Host "Matrix:     $matrixPath"

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
        $runArguments += @("--docker", (Get-DockerCli))
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

    Write-Host ""
    Write-Host "== Report"
    & $virtualEnvironmentPython -m evals.install report --output $Output
    $reportExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

Write-Host ""
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
