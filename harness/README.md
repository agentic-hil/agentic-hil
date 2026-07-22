# Agentic HIL install + usage eval harness (VMware snapshot)

Repeatable test of **installing and using an exact `agentic-hil` artifact** on a clean
box, driven off a VMware Workstation snapshot. Each run reverts to a pristine
golden image, so every run starts bit-identical. It always exercises OpenOCD and,
when installed in the image, STM32CubeProgrammer CLI (the `stlink` backend).

## What it tests, and how repeatable each layer is

| Layer | What it proves | Repeatable? |
|-------|----------------|-------------|
| Install replay (`run-all.sh`, part 1) | The fallback chain installs the caller-supplied exact version or Git commit; the installed version must match `ExpectedVersion` | Yes |
| Setup transaction (part 3) | `agentic-hil setup` preserves unrelated user config, pins the persistent MCP command, preserves an edited config on rerun, is idempotent, and rolls back an isolated late failure | Yes |
| Config model | Hardware runs use an **external** `AGENTIC_HIL_CONFIG`; repository test plans contain no hardware policy | Yes |
| Usage — OpenOCD (`test-reactor`) | flash → uart_open → reset-halt debug session → **run until the `delay` breakpoint** → debug_stop → uart_close | Yes |
| Usage — stlink / STM32CubeProgrammer (`test-reactor`) | flash (with CubeProgrammer's own `-v` verify) + reset-to-run + uart lifecycle. Runs only if CubeProgrammer is installed, else cleanly skipped | Yes |
| Integrity (`mcp_probe.py`) | The installed MCP server exposes exactly the frozen 36-tool surface | Yes |
| Asserts (`assert.sh`) | Exact version, setup transaction, external config, **no sudo / no `--break-system-packages`**, both reactor variants, and MCP surface; JSON success uses `agentic_hil.report.overall_success` | Yes |

**Why the `delay` breakpoint (OpenOCD only).** The reactor has no UART-content
assertion action, so it cannot match the `Hello World` banner. `delay` is only
reached after `main()` runs past the banner `printf` into the blink loop —
hitting it proves the firmware executed on real silicon, stronger than a string
match. The **stlink** backend has no typed debug sessions (that path requires
OpenOCD), so its variant proves the ST-tool flash+verify+reset path instead;
execution behavior is already proven by the OpenOCD variant on the same firmware.

There is deliberately **no LLM in this harness** — that is what makes it repeatable.

## One-time setup

### 1. Create the VM
- New VM in VMware Workstation, **Ubuntu 24.04 Server** (headless; the harness drives it via vmrun).
- Create a user (e.g. `tester`) — you pass its name/password to `run-eval.ps1`.

### 2. Provision the golden image
Copy `provision-golden.sh` into the VM and run it **inside the VM**:
```bash
bash provision-golden.sh
```
Installs OS deps (sudo is fine here — the base image, not the install under test),
a prebuilt Nucleo firmware fixture in `~/fixture`, and puts the user in
`dialout`/`plugdev`. It does **not** install uv/pipx/agentic-hil — those are the
test subject. OpenOCD scripts come from Ubuntu's package at
`/usr/share/openocd/scripts/...`.

**Optional — enable the stlink variant.** STM32CubeProgrammer is gated behind an
ST login and cannot be apt-installed. To test ST's tool too, download the Linux
package from ST, drop it in the VM, and install it headlessly to the default path
(`~/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI`) —
see the comments in `provision-golden.sh`. If it is absent, the stlink variant is
skipped and the OpenOCD variant still runs.

### 3. Snapshot
1. `sudo reboot` (group membership takes effect).
2. Plug in the Nucleo-F446RE. In **VM ▸ Removable Devices ▸ ST-Link**, connect it and set it to auto-connect.
3. Power off the VM.
4. Take a snapshot named **`clean`** (with the ST-Link attached).

## Run it (from the Windows host)
```powershell
cd harness\host
.\run-eval.ps1 -Vmx 'C:\VMs\ahil-ubuntu\ahil-ubuntu.vmx' `
               -GuestUser tester -GuestPass 'secret' `
               -InstallSpec 'git+https://github.com/agentic-hil/agentic-hil@<full-pr-head-sha>' `
               -ExpectedVersion '0.4.0' -Runs 3
```
`InstallSpec` is mandatory and accepts only `agentic-hil==<exact-version>` or the
official repository at a full 40-character commit SHA. The spec and expected
version are copied back with every run's artifacts.

Per run: revert to `clean`, copy the harness in, run `run-all.sh` (install →
setup transaction → OpenOCD reactor → stlink reactor if available → probe) then
`assert.sh`, and copy the reports to `harness\artifacts\run-NN\`. Prints
`PASS/FAIL` per run and a `pass / total` summary (exit 1 if any run failed).

## Files
- `provision-golden.sh` — build the golden image (run once, in the VM).
- `config.openocd.template.yaml` / `config.stlink.template.yaml` — external
  authoritative configs per backend. `run-all.sh` renders `__WORKSPACE__` /
  `__STATE__` / `__CUBECLI__` to real paths, writes them under `~/ahil/`, and
  points `AGENTIC_HIL_CONFIG` at each in turn.
- `testconfig.openocd.yaml` / `testconfig.stlink.yaml` — the two reactor plans.
- `guest/run-all.sh` — pinned install → setup transaction → backend reactors → probe, one process.
- `guest/mcp_probe.py` — lease-free MCP `tools/list` snapshot check.
- `guest/assert.sh` — deterministic checks; exit code is the verdict.
- `guest/tools.list.expected` — frozen MCP tool surface (36 tools).
- `host/run-eval.ps1` — orchestrator.

## Two debugger backends
- **openocd** — always runs; drives the ST-Link via OpenOCD, incl. a typed GDB/MI
  debug session (the `delay` breakpoint behavioral proof).
- **stlink** — STM32CubeProgrammer CLI (`STM32_Programmer_CLI`), ST's standard
  tool. Flash+verify+reset+uart only (no typed debug sessions). Runs only when
  CubeProgrammer is installed; otherwise recorded as `skipped` (not a failure).
- Both drive the **same** physical ST-Link, so `run-all.sh` runs them
  **sequentially** — never overlap processes against the board (leases
  assume a single owner).

## External config model (important)
- The authoritative config lives **outside** the repo. `agentic-hil setup` writes
  it under `~/.config/agentic-hil/projects/<name>-<hash>/config.yaml`, deny-by-default.
- `--config <repo-path>` is **rejected** (`config_invalid`). The only sanctioned
  override is `AGENTIC_HIL_CONFIG=<absolute external path>`, which this harness uses.
- Config must start with `workspace_root:` (= cwd) and `state_root:` (absolute,
  outside the workspace, user-owned, not group/world writable).
- Permissions are deny-by-default; the templates enable only probe/flash/reset/com_read.

## Limitations (by design)
- **Linux install path only.** The Windows-host path (PATH, COM enumeration, and
  CubeProgrammer's own Windows auto-discovery) needs a Windows guest VM.
- **One board ⇒ serial runs.** A passed-through ST-Link is visible to the guest
  only; run `-Runs` sequentially.
- **Network is live.** Package identity is immutable (exact version or full Git
  SHA), but availability still depends on PyPI/GitHub/astral. Mirror those inputs
  and re-snapshot to remove the availability dependency.
- **No UART-content assertion.** Covered by the `delay` breakpoint (OpenOCD) and
  by CubeProgrammer's `-v` verify (stlink). To also assert the `Hello World`
  banner text, add a small MCP loop after the reactor releases the board.

## Optional: agent-eval layer (NOT a gate)
Replace the install part of `run-all.sh` with a headless Claude Code run to also
measure whether an agent *interprets the docs* correctly:
```bash
CLAUDE_CODE_OAUTH_TOKEN=... claude -p \
  "Install from https://github.com/agentic-hil/agentic-hil and set it up for this project." \
  --dangerously-skip-permissions --output-format stream-json > transcript.jsonl
```
Get the token with `claude setup-token` on the host and pass it per run as an env
var — never bake it into the snapshot. Run N times, report the pass rate; a single
run is not deterministic.
