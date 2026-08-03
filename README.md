# Agentic HIL

<!-- mcp-name: io.github.agentic-hil/agentic-hil -->

**Your AI agent can develop firmware on its own — because Agentic HIL closes the loop with real hardware.**

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/hero-loop-dark.svg">
  <img alt="The Agentic HIL loop: build, flash, stimulate, observe, then diagnose and fix, closing back onto build. Flash and stimulate write to the real board on your bench; observe reads back from it. Your agent runs the loop unattended and you review the pull request." src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/hero-loop.svg">
</picture>

Agentic Hardware-in-the-Loop (Agentic HIL) is a Python package that exposes bounded MCP tools for probing, flashing, resetting, artifact validation, serial and CAN stimulus/feedback, reports, and logs — without giving an agent arbitrary host or debugger access. Each project has exactly one authoritative configuration stored outside the repository. Agentic HIL discovers it from the project root, while `AGENTIC_HIL_CONFIG` can select an explicit absolute-path override. The file defines the workspace binding, devices, actions, paths, and limits.

Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

## Install

The easiest path: copy/paste this prompt to your AI agent:

```text
Read and follow the complete guide at https://github.com/agentic-hil/agentic-hil/blob/master/AI_AGENT_QUICKSTART.md to install Agentic HIL and set it up for this project.
```

Agents follow [AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) — everything installs user-local, **no admin rights required, ever**. The same is true doing it by hand.

### Installing it yourself

Two commands: install the package user-locally, then set up the project from its
root.

```bash
pip install --user agentic-hil
agentic-hil setup                 # add --agent codex or --agent opencode for those
```

`setup` installs the agent skill, registers the MCP server with a verified
absolute executable path, creates the deny-by-default policy outside the
repository, and runs `doctor`. It prints where the policy file landed — review it
before enabling anything.

Those are two scopes, and `setup` composes the commands that own them:

```bash
agentic-hil agent-install --agent <agent>   # skill + user-level MCP registration; once per user and agent
agentic-hil init --agent <agent>            # this project's policy + doctor; once per project, from its root
```

`agent-install` writes only under the invoking user's home — user-wide, per user
and per machine, not shared with other OS users — and needs no project and no
configuration. Each half rolls back only its own writes, so a project that will
not configure still leaves a working agent, and the next repository for this
user needs `init` alone.

If `pip` is missing, Python is externally managed, or `agentic-hil` does not end
up on `PATH`, install it persistently with a tool installer instead and rerun
`setup`:

```bash
uv tool install agentic-hil       # or: pipx install agentic-hil
```

Never use `pip install --break-system-packages`. Never persist a `uvx`
invocation, a workspace virtual environment, or a bare `PATH` name as the MCP
launcher: each resolves anew later, so the program behind the hardware gate
could change without anyone editing anything.
[AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) has the complete fallback chain,
and [TROUBLESHOOTING.md](TROUBLESHOOTING.md) covers what to do when something
does not start.

For direct PEAK/SocketCAN adapters add the CAN extra — `uv tool install
'agentic-hil[can]'` — and for the pyOCD backend `agentic-hil[pyocd]`. Both are
optional because they carry platform-specific drivers that flashing and UART do
not need; without them those tools refuse with `can_backend_not_available`
rather than failing at import.

To check a version without installing anything, `uvx --from agentic-hil
agentic-hil --version` is a diagnostic only.

## Why

A green build is not enough in embedded development: firmware has to behave correctly on the real board. Classic tools automate single steps — flash here, read a log there — but the moment real hardware has to respond, a human is back in the loop. Handing an agent a raw debugger shell or direct serial access instead is neither safe nor reproducible. Agentic HIL closes the gap with a small, auditable gate:

```
AI agent / CI  ──MCP (stdio)──▶  Agentic HIL  ──authoritative config──▶  OpenOCD / pyOCD / STM32CubeProgrammer
                                    │                        serial ports (pyserial)
                                    │                        CAN (PEAK / SocketCAN / bridge)
                                    ▼
                       structured results, reports, logs
```

Every hardware action is validated against the selected authoritative configuration, executed with timeouts, logged to `.agentic-hil/logs/`, and answered with a structured JSON result (`ok`, `error_type`, `summary`, `likely_causes`, `report_path`, `log_path`) that an agent can act on.

## MCP Entry

Every MCP host starts the same local stdio server from the firmware project root, using the reviewed absolute path of its persistent installation:

```text
/absolute/path/to/persistent/agentic-hil mcp-stdio
```

Host configuration schemas are not portable: VS Code uses `servers`, Claude Code uses `mcpServers`, Codex uses TOML, and OpenCode uses a command array. `agentic-hil agent-install --agent <agent>` — which `agentic-hil setup --agent <agent>` runs first — performs secure user-level registration for Claude Code, Codex, and OpenCode. See [MCP host configuration](docs/mcp-hosts.md) for the remaining hosts. `agentic-hil mcp-config --output .mcp.json` generates only a machine-local Claude-compatible form with an absolute executable path; keep it uncommitted.

`mcp-stdio` discovers the authoritative file from its project working directory: `%APPDATA%/agentic-hil/projects/<project-id>/config.yaml` on Windows or `${XDG_CONFIG_HOME:-~/.config}/agentic-hil/projects/<project-id>/config.yaml` on POSIX. Set `AGENTIC_HIL_CONFIG` only when an operator-controlled absolute-path override is needed; never commit a machine-specific override in repository-controlled MCP configuration.

## Configuration

`agentic-hil setup` already created this file; `agentic-hil init` creates it on its own, without the user-wide skill and MCP registration that `agentic-hil agent-install` owns. Either way it lands outside the repository with `workspace_root` bound to the current absolute project path. It defines the target, artifact roots, and the project's named devices — debug probes, serial ports, and CAN buses.

Reading a device needs no permission. What protects a read is exclusivity: every device a test plan names is locked machine-wide for the duration of the run, a device the plan does not name is refused, and a second session is turned away with the holder named. Everything that writes or changes state — flash, reset, serial and CAN sends, mass erase, raw debugger commands — stays deny-by-default, per device:

```yaml
version: 2                   # reading needs no permission; see Migration below
workspace_root: "/absolute/path/to/firmware-project"
state_root: "/absolute/operator-controlled/user-state/agentic-hil"

target:
  name: "sensor-board"
  controller: "stm32f4"

# Every device is named, and every device carries its own write permissions.
# Test plan steps address a device by that name. The MCP tool surface drives the
# single configured probe, so a project with several probes runs multi-board
# work through `agentic-hil test-reactor`.
debuggers:
  dut:
    type: "openocd"          # or "pyocd" (most Cortex-M targets), or "stlink" (STM32CubeProgrammer CLI)
    probe_id: "0668FF383036" # required once several probes are configured: it is
                             # the only field that selects a physical probe
    interface_cfg: "/absolute/path/to/openocd/scripts/interface/stlink.cfg"
    target_cfg: "/absolute/path/to/openocd/scripts/target/stm32f4x.cfg"
    timeout_s: 60
    permissions:
      allow_flash: true
      allow_reset: true
      allow_raw_debugger_commands: false
      allow_mass_erase: false
  probe_b:                   # a second, independently controlled board
    type: "openocd"
    probe_id: "0669FF505153" # pin the physical probe so boards cannot swap silently
    interface_cfg: "/absolute/path/to/openocd/scripts/interface/stlink.cfg"
    target_cfg: "/absolute/path/to/openocd/scripts/target/stm32f4x.cfg"
    target:                  # optional per-probe override of the project target
      name: "sensor-node"
    permissions:
      allow_flash: false     # this board is read-only for now

debug:
  allowed_symbols: ["main", "sensor_state", "capture_done", "capture_buffer"]
  allow_all_symbols: false

artifacts:
  allowed_roots: ["build"]   # firmware may only be flashed from here
  allowed_extensions: [".elf", ".hex", ".bin"]

com_ports:
  dut_uart:
    device: "/dev/ttyACM0"  # Windows example: "COM5"
    baudrate: 115200
    assert_dtr: false        # opening the port must not reset this board
    assert_rts: false
    permissions:
      allow_write: true

can_buses:
  dut_can:
    adapter: "socketcan"     # or "peak", or "process" for a custom bridge
    channel: "can0"
    bitrate: 500000
    listen_only: true        # receive without sending dominant ACK bits
    permissions:
      allow_write: false
```

### Migration

A configuration written before this release has no `version:` key and is read under version 1, where reading still needs `allow_probe` on a debugger and `allow_read` on a COM port or CAN bus. Nothing infers the new model from a missing key, so an update never widens what a bench already allows. Migrating is one edit: remove those keys and set `version: 2`. A version 2 file that still carries one is refused by name rather than silently ignoring it.

The operator reviews this file and explicitly enables only the required resources and permissions. `workspace_root` is mandatory and must exactly match the project root used to launch Agentic HIL. `state_root` is also mandatory: it must be an absolute, operator-controlled directory outside and non-overlapping with the workspace. Every trusted launcher for the same host resources must use this pinned root; changing `LOCALAPPDATA` or `XDG_STATE_HOME` after initialization does not change a running service's coordination namespace. Configured debugger/GDB/process-bridge executables and OpenOCD scripts must resolve to existing host-owned files outside the workspace. Empty symbol allowlists deny all symbols; unrestricted symbol access requires `allow_all_symbols: true`. Set optional `resource_id` on a debugger, COM, or CAN entry when different host paths/wrappers address the same physical resource; matching IDs share one cross-process lease. A `resource_id` names hardware rather than a file, so it is matched case-insensitively on every platform, and two entries that spell one id in two cases are refused rather than merged — make them identical if they are one unit, or different by more than case if they are two. Two `debuggers` entries that resolve to the same physical probe are rejected outright, so a plan naming one board can never drive another.

All hardware entry points use this same file: `doctor`, `mcp-stdio`, `com-stdio`, the pytest plugin, and `test-reactor`. Deprecated configuration-path options remain parseable for patch-release compatibility but cannot redirect authority away from the discovered external file.

Export the full JSON schema with `agentic-hil schema --output agentic-hil-config.schema.json`.

## MCP Tools

| Group | Tools | Notes |
|-------|-------|-------|
| Debugger | `debugger_info`, `debugger_probes_list`, `probe_target`, `reset_target` | Probe-ID listing uses pyOCD or STM32CubeProgrammer; OpenOCD cannot enumerate all attached probes |
| Firmware | `flash_firmware`, `artifact_upload` | artifacts are validated, rechecked, and copied to private process staging before flashing; `allow_reset` is additionally required when `reset_after_flash` is requested |
| Serial | `com_ports_list`, `com_session_start`, `com_session_stop`, `com_write`, `com_read` | named ports only, buffered background reader |
| CAN | `can_buses_list`, `can_session_start`, `can_session_stop`, `can_send`, `can_read` | PEAK, SocketCAN, or a process bridge |
| Diagnostics | `get_last_report`, `classify_last_error` | structured error classification with likely causes |
| Debug sessions | `debug_*` (start/stop/status, breakpoints, continue/halt, symbol info, memory dump) | typed GDB/MI sessions via the OpenOCD backend's gdbserver; unexpected breakpoints and target exceptions are returned as structured stop reasons; symbol allowlist and dump-size limits come from the `debug:` config section |
| Run boundary | `bench_run_start`, `bench_run_stop`, `bench_run_status` | declares the devices of a multi-call run and holds them for its whole duration; without it each call holds its device only for its own duration |

A typical loop: build firmware → `bench_run_start` naming the probe and the port → `flash_firmware` with `reset_after_flash: true` when a fresh boot is required → `com_session_start` → stimulate via `com_write`/`can_send` → assert on `com_read`/`can_read` → `bench_run_stop` → on failure, `classify_last_error`.

Devices are named as `{"kind": "debugger" | "uart" | "can", "id": "<config entry>"}`. The lock is keyed on the hardware behind the entry, so two entries describing one physical unit — a probe and its virtual COM port under a shared `resource_id` — collapse to one lock. The DUT is not a device: it is what the devices drive.

## Test Reactor

Write a hardware test as a plan, not a script: one reviewable YAML file describes flash → stimulate → break → dump, and the reactor guarantees it is either executed exactly as written or rejected before the first hardware action. No half-run plans, no leftover breakpoints, no orphaned debug or UART sessions — the same file behaves identically on your bench and in CI, and it diffs like code in a pull request.

The test reactor executes a strict, sequential YAML or JSON test plan against the named devices in the authoritative config. A debugger step names a `debuggers` entry (`debugger: <name>`); a UART step names a `com_ports` entry (`port_id: <name>`). `debugger` may be omitted while the config declares exactly one probe — with several, the plan must name one, because picking a board for the author is how the wrong board gets flashed. Every probe carries its own permissions, so a step is judged by the grants of the board it names. Probes other than the bound one run on their own service under one shared project lease. Typed debug actions currently require OpenOCD; flash/UART-only plans can use the other backends.

Before the first hardware action, the reactor validates every device name, permission, session order, artifact, breakpoint symbol, and dump path. Execution is fail-fast, each reactor-created breakpoint is removed after use, and debug/UART sessions opened by the runner are closed even when a step raises an exception. Breakpoint and dump symbols must be present in `debug.allowed_symbols` unless `allow_all_symbols: true` is explicitly set.

The run pipeline is deliberately simple — validate everything, then execute, then always clean up:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/reactor-pipeline-dark.svg">
  <img alt="Test reactor pipeline: plan → preflight (no hardware touched; any finding rejects the whole plan) → sequential fail-fast execution → guaranteed cleanup → structured report" src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/reactor-pipeline.svg">
</picture>

```yaml
# .agentic-hil/testconfig.yaml
version: 2
name: capture-state
steps:
  - {debugger: dut, action: flash, image_path: build/app.elf}
  - {port_id: dut_uart, action: uart_open}
  - {debugger: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {debugger: dut, action: run_until_breakpoint, location: capture_done, timeout_s: 5}
  - {debugger: dut, action: dump_memory, symbol: capture_buffer, output_path: build/capture.hex}
  - {debugger: dut, action: debug_stop}
  - {port_id: dut_uart, action: uart_close}
```

`.agentic-hil/testconfig.yaml` and `--test-config` select only this test plan: ordered test steps and the device names they run on. They contain no hardware resources or permissions. The reactor gets all hardware settings from the discovered authoritative config or its `AGENTIC_HIL_CONFIG` override:

```text
agentic-hil test-reactor --test-config .agentic-hil/testconfig.yaml
```

See [`examples/testconfig.example.yaml`](examples/testconfig.example.yaml) for the expanded form.

## Safety Model

Leave an agent alone with real bench hardware and still trust the board, the host, and the logs afterwards. The agent can edit every file inside the workspace, so nothing there is trusted — all authority sits outside, beyond its reach:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/trusted-boundary-dark.svg">
  <img alt="Trusted policy boundary: the agent-writable workspace talks to Agentic HIL over MCP stdio only; the authoritative config and state_root (leases, quarantine, audit chain) live on the operator-controlled host outside the workspace" src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/trusted-boundary.svg">
</picture>

Every hardware action, from every entry point, walks the same gate:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/action-gate-dark.svg">
  <img alt="Action gate: tool call → deny-by-default permission gate → validation → owner lease → execution with pinned executable and timeout → SHA-256 audit chain → structured JSON result; a crash or unknown effect quarantines the resource until operator recovery" src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/action-gate.svg">
</picture>

- Deny-by-default permission switches for everything that writes or changes state, with deliberate interlocks — flashing is refused while `allow_raw_debugger_commands` or `allow_mass_erase` is enabled. `permission_denied` results are authoritative and agents are instructed to stop (see [AGENTS.md](AGENTS.md)).
- Reading needs no permission, and exclusivity carries that load instead. The risk in a HIL tool is not damage but a false green: a test disturbed unnoticed produces a result nobody should trust. Whoever holds a board cannot be disturbed, and whoever reads while no run holds it disturbs nobody. `listen_only` for CAN and a DTR/RTS-free port open remain the way to observe a target provably undisturbed.
- Configured executables and OpenOCD scripts are pinned at startup and must resolve outside the workspace; firmware artifacts are validated, hashed, and staged in a private process directory before any backend consumes them.
- Serial/CAN writes and reads are size- and buffer-capped; debugger calls run with timeouts and OpenOCD's TCP servers disabled.
- One live owner per project and physical probe/port/bus across all entry points; a second process gets `resource_busy`, and crashes or unknown effects quarantine the resource until explicit operator recovery ([TROUBLESHOOTING.md](TROUBLESHOOTING.md#13-device_busy-resource_busy-or-quarantined-hardware)).
- Exclusivity per physical device is machine-wide and held for the whole run, kept in one agreed place (`~/.agentic-hil/device-locks`) rather than beside a configuration — two sessions with different `state_root` values are still one bench. A contender is refused with `device_busy` naming the holder; waiting happens only when asked for (`--wait-s`) and is bounded. Ownership is the operating system's lock, so a crashed run frees its devices immediately, with no quarantine and no manual recovery.
- Canonical reports and the tamper-evident audit chain live under the operator-pinned `state_root`; workspace logs and reports are untrusted mirrors, verified against the chain on read.

The complete threat model and design rationale: [docs/security-design.md](docs/security-design.md).

## pytest Plugin

Installing `agentic_hil` registers the `agentic_hil` pytest plugin, so CI regression suites can drive the same permission-gated tools without an MCP client.

The `agentic_hil` fixture uses the same discovered config or absolute-path override as every other entry point and verifies that its `workspace_root` matches the pytest rootdir. Tests using the fixture skip when no config exists and fail loudly when an available config is invalid. Pytest executes project code and is therefore not a sandbox or security boundary; real unattended hardware runners must still use OS isolation and host-managed invocation. COM and CAN sessions opened during a test are stopped afterwards so stimulus state cannot leak between tests. See [examples/nucleo-f446re_demo/](examples/nucleo-f446re_demo/) for the complete loop on real hardware.

## Common Commands

```text
agentic-hil setup --agent <claude-code|codex|opencode>         # both halves, first run
agentic-hil agent-install --agent <claude-code|codex|opencode> # user-wide half
agentic-hil init [--agent <agent>]                             # project half
agentic-hil doctor
agentic-hil debugger-probes
agentic-hil com-ports
agentic-hil mcp-config --output .mcp.json
agentic-hil mcp-stdio
agentic-hil test-reactor --test-config .agentic-hil/testconfig.yaml
agentic-hil com-stdio --port dut_uart
agentic-hil schema --output agentic-hil-config.schema.json
agentic-hil test-schema --output testconfig.schema.json
agentic-hil skill-install --agent opencode
```

## Platform Support

Linux, macOS, and Windows (CI-tested on Python 3.10–3.13). Debugger backends: OpenOCD, pyOCD (`agentic-hil[pyocd]` — covers most ARM Cortex-M targets via CMSIS packs and CMSIS-DAP/ST-Link/J-Link probes, set `debuggers.<name>.target_type`), and STM32CubeProgrammer CLI (auto-discovered on Windows). Direct CAN requires `agentic-hil[can]` (python-can); CAN also supports a configured `process` bridge backend.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check src tests evals tools
pytest
python -m build
twine check dist/*
```

The package is configured for PyPI publishing through GitHub trusted publishing in `.github/workflows/workflow.yml`.

## Security

Policy bypasses are treated as vulnerabilities — see [SECURITY.md](SECURITY.md).

## License

Apache-2.0 — see [LICENSE](LICENSE).
