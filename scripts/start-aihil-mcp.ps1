Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $PSCommandPath
$repoRoot = Split-Path -Parent $scriptDir
$configPath = Join-Path -Path $repoRoot -ChildPath '.aihil/config.yaml'

Push-Location $repoRoot
try {
    if (-not (Get-Command aihil -ErrorAction SilentlyContinue)) {
        [Console]::Error.WriteLine('aihil is not installed. Install it once on this machine, then rerun this script.')
        [Console]::Error.WriteLine('Example from the AI-HIL repository: python -m pip install -e .')
        exit 127
    }

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
