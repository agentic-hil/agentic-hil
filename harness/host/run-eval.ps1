<#
.SYNOPSIS
  Repeatable Agentic HIL install+usage eval on a VMware snapshot.

  Each run: revert the golden snapshot, copy the harness scripts in, replay the
  documented install and setup, run the backend plans against real hardware,
  probe the MCP tool surface, and pull the artifacts out. Repeat N
  times and print the pass rate.

.EXAMPLE
  .\run-eval.ps1 -Vmx 'C:\VMs\ahil-ubuntu\ahil-ubuntu.vmx' -GuestUser tester -GuestPass secret `
    -InstallSpec 'git+https://github.com/agentic-hil/agentic-hil@0123456789abcdef0123456789abcdef01234567' `
    -ExpectedVersion '0.4.0' -Runs 3
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Vmx,
  [string]$Snapshot = "clean",
  [Parameter(Mandatory = $true)][string]$GuestUser,
  [Parameter(Mandatory = $true)][string]$GuestPass,
  [Parameter(Mandatory = $true)][string]$InstallSpec,
  [Parameter(Mandatory = $true)][string]$ExpectedVersion,
  [int]$Runs = 1,
  [string]$Vmrun = "C:\Program Files\VMware\VMware Workstation\vmrun.exe",
  [string]$ArtifactRoot = "$PSScriptRoot\..\artifacts"
)

$ErrorActionPreference = "Stop"
$guestHome    = "/home/$GuestUser"
$guestHarness = "$guestHome/harness"
$guestFixture = "$guestHome/fixture"
$localGuest    = Join-Path $PSScriptRoot "..\guest"
$guestFiles    = @("run-all.sh", "assert.sh", "mcp_probe.py", "tools.list.expected")
$localTopFiles = @("config.openocd.template.yaml", "config.stlink.template.yaml", "testconfig.openocd.yaml", "testconfig.stlink.yaml")

if (-not (Test-Path $Vmrun)) { throw "vmrun not found at $Vmrun" }
if ($InstallSpec -notmatch '^agentic-hil==[0-9A-Za-z.+-]+$' -and
    $InstallSpec -notmatch '^git\+https://github\.com/agentic-hil/agentic-hil(\.git)?@[0-9a-fA-F]{40}$') {
  throw "InstallSpec must be an exact version or the Agentic HIL repository at a full commit SHA"
}
if ($ExpectedVersion -notmatch '^[0-9A-Za-z.+-]+$') { throw "ExpectedVersion is invalid" }
$requiredLocalPaths = @($guestFiles | ForEach-Object { Join-Path $localGuest $_ })
$requiredLocalPaths += @($localTopFiles | ForEach-Object { Join-Path $PSScriptRoot "..\$_" })
foreach ($path in $requiredLocalPaths) {
  if (-not (Test-Path $path)) { throw "required harness path missing: $path" }
}

# vmrun with guest credentials; throws on failure.
function VMauth([string]$op, [string[]]$rest) {
  & $Vmrun -T ws -gu $GuestUser -gp $GuestPass $op @rest
  if ($LASTEXITCODE -ne 0) { throw "vmrun $op failed (exit $LASTEXITCODE)" }
}
# vmrun without guest credentials (host-side VM ops).
function VMbare([string]$op, [string[]]$rest) {
  & $Vmrun -T ws $op @rest
  if ($LASTEXITCODE -ne 0) { throw "vmrun $op failed (exit $LASTEXITCODE)" }
}

$pass = 0
for ($i = 1; $i -le $Runs; $i++) {
  Write-Host "==== RUN $i / $Runs ====" -ForegroundColor Cyan
  $art = Join-Path $ArtifactRoot ("run-{0:D2}" -f $i)
  New-Item -ItemType Directory -Force -Path $art | Out-Null

  VMbare "revertToSnapshot" @($Vmx, $Snapshot)
  VMbare "start"            @($Vmx, "nogui")

  # Wait for VMware Tools guest operations to answer.
  $ready = $false
  for ($t = 0; $t -lt 60; $t++) {
    & $Vmrun -T ws -gu $GuestUser -gp $GuestPass listProcessesInGuest $Vmx *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 2
  }
  if (-not $ready) { throw "guest tools did not become ready" }

  # Copy the harness in, normalize CRLF -> LF, make executable.
  VMauth "createDirectoryInGuest" @($Vmx, $guestHarness)
  foreach ($f in $guestFiles) {
    VMauth "CopyFileFromHostToGuest" @($Vmx, (Join-Path $localGuest $f), "$guestHarness/$f")
  }
  foreach ($f in $localTopFiles) {
    VMauth "CopyFileFromHostToGuest" @($Vmx, (Join-Path $PSScriptRoot "..\$f"), "$guestHarness/$f")
  }
  VMauth "runProgramInGuest" @($Vmx, "/bin/bash", "-lc",
    "sed -i 's/\r`$//' $guestHarness/*.sh $guestHarness/*.py; chmod +x $guestHarness/*.sh $guestHarness/*.py")

  $runOk = $true
  try {
    # run-all.sh does install -> configure -> test-reactor -> MCP probe in one process.
    VMauth "runProgramInGuest" @(
      $Vmx,
      "/bin/bash",
      "-lc",
      "$guestHarness/run-all.sh $guestFixture '$InstallSpec' '$ExpectedVersion'"
    )
  } catch {
    $runOk = $false
    Write-Warning "run ${i}: $_"
  }

  # assert.sh exit code is the verdict.
  & $Vmrun -T ws -gu $GuestUser -gp $GuestPass runProgramInGuest $Vmx "/bin/bash" "-lc" "$guestHarness/assert.sh $guestFixture"
  $assertOk = ($LASTEXITCODE -eq 0)

  foreach ($f in @(
    "transcript.txt",
    "install_spec.txt",
    "expected_version.txt",
    "setup_result.first.json",
    "setup_result.second.json",
    "setup_result.rollback.json",
    "preservation_status.txt",
    "rollback_status.txt",
    "mcp_registration_status.txt",
    "doctor_report.openocd.json",
    "doctor_report.stlink.json",
    "reactor_report.openocd.json",
    "reactor_report.stlink.json",
    "stlink_status.txt",
    "mcp_probe.json"
  )) {
    & $Vmrun -T ws -gu $GuestUser -gp $GuestPass CopyFileFromGuestToHost $Vmx "$guestHarness/$f" (Join-Path $art $f) *> $null
  }

  if ($runOk -and $assertOk) {
    $pass++
    Write-Host "RUN ${i}: PASS" -ForegroundColor Green
  } else {
    Write-Host "RUN ${i}: FAIL (artifacts in $art)" -ForegroundColor Red
  }
}

Write-Host "==== SUMMARY: $pass / $Runs passed ====" -ForegroundColor Cyan
if ($pass -lt $Runs) { exit 1 }
