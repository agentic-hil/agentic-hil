Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $PSCommandPath
$repoRoot = Split-Path -Parent $scriptDir
$configPath = Join-Path -Path $repoRoot -ChildPath '.aihil/config.yaml'

Push-Location $repoRoot
try {
    & python -m pip install -e .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    if (-not (Test-Path -LiteralPath $configPath)) {
        & aihil init --config $configPath
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    & aihil serve --config $configPath
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
