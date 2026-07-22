# Agentic HIL

<!-- mcp-name: io.github.agentic-hil/agentic-hil -->

**Your AI agent can develop firmware on its own — because Agentic HIL closes the loop with real hardware.**

```
+--> build --> flash --> stimulate --> observe --+
|                                                |
+<-------------- diagnose & fix -----------------+

  your agent, unattended -- you review the pull request
```

Agentic Hardware-in-the-Loop (Agentic HIL) is a Python package that exposes bounded MCP tools for probing, flashing, resetting, artifact validation, serial and CAN stimulus/feedback, reports, and logs — without giving an agent arbitrary host or debugger access. Each project has exactly one authoritative configuration stored outside the repository. Agentic HIL discovers it from the project root, while `AGENTIC_HIL_CONFIG` can select an explicit absolute-path override. The file defines the workspace binding, devices, actions, paths, and limits.

Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

## Install

The easiest path: copy/paste this prompt to your AI agent:

```text
Install from https://github.com/agentic-hil/agentic-hil and set it up for this project.
```

Agents follow [AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) — everything installs user-local, **no admin rights required, ever**.

If you want to install it yourself anyway, install the Python package user-locally and verify the minimum setup-capable version:

```bash
python -m pip install --user --upgrade "agentic-hil>=0.4.0"
agentic-hil --version
agentic-hil setup --help
```

If that fails because Python is externally managed, `agentic-hil` is not on `PATH`, or the package is unavailable through that interpreter, use the `uv`/`pipx` paths below instead. Never use `pip install --break-system-packages`.

For a transient version check only (no `PATH` changes; needs [uv](https://docs.astral.sh/uv/) or pipx):

```bash
uvx --from agentic-hil agentic-hil --version
uvx --from git+https://github.com/agentic-hil/agentic-hil agentic-hil --version
```

For project setup, install 0.4.0 or newer persistently and let `setup` create the external policy, install the skill, and register the selected host with a verified absolute executable path:

```bash
uv tool install --upgrade "agentic-hil>=0.4.0"      # or: pipx install "agentic-hil>=0.4.0"
agentic-hil setup --agent claude-code                # or: codex / opencode
```

Do not persist an `uvx`, workspace-virtualenv, or bare `PATH` command as the MCP launcher. See the agent quickstart for the complete fallback chain.

For direct PEAK/SocketCAN access install the CAN extra: `uv tool install 'agentic-hil[can]'`. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) when something does not start.

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

Host configuration schemas are not portable: VS Code uses `servers`, Claude Code uses `mcpServers`, Codex uses TOML, and OpenCode uses a command array. `agentic-hil setup --agent <agent>` performs secure user-level registration for Claude Code, Codex, and OpenCode. See [MCP host configuration](docs/mcp-hosts.md) for the remaining hosts. `agentic-hil mcp-config --output .mcp.json` generates only a machine-local Claude-compatible form with an absolute executable path; keep it uncommitted.

`mcp-stdio` discovers the authoritative file from its project working directory: `%APPDATA%/agentic-hil/projects/<project-id>/config.yaml` on Windows or `${XDG_CONFIG_HOME:-~/.config}/agentic-hil/projects/<project-id>/config.yaml` on POSIX. Set `AGENTIC_HIL_CONFIG` only when an operator-controlled absolute-path override is needed; never commit a machine-specific override in repository-controlled MCP configuration.

## Configuration

Run `agentic-hil init` from the project root. It creates the automatically discovered deny-by-default authoritative file outside the repository and binds `workspace_root` to the current absolute project path. The file defines the target, debugger backend, artifact roots, named serial ports, CAN buses, test adapters, and per-action permissions:

```yaml
workspace_root: "/absolute/path/to/firmware-project"
state_root: "/absolute/operator-controlled/user-state/agentic-hil"

target:
  name: "sensor-board"
  controller: "stm32f4"

debugger:
  type: "openocd"            # or "pyocd" (most Cortex-M targets), or "stlink" (STM32CubeProgrammer CLI)
  interface_cfg: "/absolute/path/to/openocd/scripts/interface/stlink.cfg"
  target_cfg: "/absolute/path/to/openocd/scripts/target/stm32f4x.cfg"
  timeout_s: 60

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

devices:
  dut:
    debugger: true            # at most one Device may use the top-level debugger
    uart: "dut_uart"          # reference a named com_ports entry
  dut_b:
    debugger: "probe_b"       # named entry in `debuggers` for an independent board
    target:                   # optional override; requires a named debugger
      name: "sensor-node"

debuggers:                    # additional probes for multi-board test-reactor plans
  probe_b:
    type: "openocd"
    probe_id: "0669FF505153"  # pin the physical probe so boards cannot swap silently

can_buses:
  dut_can:
    adapter: "socketcan"     # or "peak", or "process" for a custom bridge
    channel: "can0"
    bitrate: 500000

adapters:
  ntc_sim:
    executable: "/operator-controlled/agentic-hil-bridges/sim_ntc_adapter.py"
    channels: ["temperature", "resistance"]
    faults: ["open", "short_to_gnd", "short_to_vcc"]

permissions:
  allow_probe: true
  allow_flash: true
  allow_reset: true
  allow_com_read: true
  allow_com_write: true
  allow_can_read: true
  allow_can_write: true
  allow_adapter_read: true
  allow_adapter_write: true
  allow_raw_debugger_commands: false
  allow_mass_erase: false
```

The operator reviews this file and explicitly enables only the required resources and permissions. `workspace_root` is mandatory and must exactly match the project root used to launch Agentic HIL. `state_root` is also mandatory: it must be an absolute, operator-controlled directory outside and non-overlapping with the workspace. Every trusted launcher for the same host resources must use this pinned root; changing `LOCALAPPDATA` or `XDG_STATE_HOME` after initialization does not change a running service's coordination namespace. Configured debugger/GDB/process-bridge executables and OpenOCD scripts must resolve to existing host-owned files outside the workspace. Empty symbol allowlists deny all symbols; unrestricted symbol access requires `allow_all_symbols: true`. Set optional `resource_id` on debugger, COM, CAN, or adapter entries when different host paths/wrappers address the same physical resource; matching IDs share one cross-process lease.

All hardware entry points use this same file: `doctor`, `mcp-stdio`, `com-stdio`, the pytest plugin, and `test-reactor`. Deprecated configuration-path options remain parseable for patch-release compatibility but cannot redirect authority away from the discovered external file.

Export the full JSON schema with `agentic-hil schema --output agentic-hil-config.schema.json`.

## MCP Tools

| Group | Tools | Notes |
|-------|-------|-------|
| Debugger | `debugger_info`, `debugger_probes_list`, `probe_target`, `reset_target` | Probe-ID listing uses pyOCD or STM32CubeProgrammer; OpenOCD cannot enumerate all attached probes |
| Firmware | `flash_firmware`, `artifact_upload` | artifacts are validated, rechecked, and copied to private process staging before flashing; `allow_reset` is additionally required when `reset_after_flash` is requested |
| Serial | `com_ports_list`, `com_session_start`, `com_session_stop`, `com_write`, `com_read` | named ports only, buffered background reader |
| CAN | `can_buses_list`, `can_session_start`, `can_session_stop`, `can_send`, `can_read` | PEAK, SocketCAN, or a process bridge |
| Test adapters | `adapters_list`, `adapter_session_start`, `adapter_session_stop`, `adapter_set_value`, `adapter_inject_fault`, `adapter_clear_fault`, `adapter_measure` | externally pinned bridge entry point with channel/fault allowlists |
| Diagnostics | `get_last_report`, `classify_last_error` | structured error classification with likely causes |
| Debug sessions | `debug_*` (start/stop/status, breakpoints, continue/halt, symbol info, memory dump) | typed GDB/MI sessions via the OpenOCD backend's gdbserver; unexpected breakpoints and target exceptions are returned as structured stop reasons; symbol allowlist and dump-size limits come from the `debug:` config section |

A typical loop: build firmware → `flash_firmware` with `reset_after_flash: true` when a fresh boot is required → `com_session_start` → stimulate via `com_write`/`can_send` → assert on `com_read`/`can_read` → on failure, `classify_last_error`.

## Test Reactor

Write a hardware test as a plan, not a script: one reviewable YAML file describes flash → stimulate → break → dump, and the reactor guarantees it is either executed exactly as written or rejected before the first hardware action. No half-run plans, no leftover breakpoints, no orphaned debug or UART sessions — the same file behaves identically on your bench and in CI, and it diffs like code in a pull request.

The test reactor executes a strict, sequential YAML or JSON test plan against logical `devices` from the authoritative config. A Device binds to a debugger, to one named UART, or to both — nothing more is required, and a UART-only device runs UART-only plans without any debugger configured. The debugger is either the top-level `debugger` (`devices.<id>.debugger: true`) or a named entry in the `debuggers` map for an independently controlled board (`devices.<id>.debugger: <name>`, with an optional per-device `target`). Each physical probe drives exactly one device; named-debugger devices run on their own service under one shared project lease. Typed debug actions currently require OpenOCD; flash/UART-only plans can use the other backends.

Before the first hardware action, the reactor validates every device, capability, session order, artifact, breakpoint symbol, and dump path. Execution is fail-fast, each reactor-created breakpoint is removed after use, and debug/UART sessions opened by the runner are closed even when a step raises an exception. Breakpoint and dump symbols must be present in `debug.allowed_symbols` unless `allow_all_symbols: true` is explicitly set.

The run pipeline is deliberately simple — validate everything, then execute, then always clean up:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/reactor-pipeline-dark.svg">
  <img alt="Test reactor pipeline: plan → preflight (no hardware touched; any finding rejects the whole plan) → sequential fail-fast execution → guaranteed cleanup → structured report" src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/reactor-pipeline.svg">
</picture>

```yaml
# .agentic-hil/testconfig.yaml
version: 1
name: capture-state
steps:
  - {device: dut, action: flash, image_path: build/app.elf}
  - {device: dut, action: uart_open}
  - {device: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {device: dut, action: run_until_breakpoint, location: capture_done, timeout_s: 5}
  - {device: dut, action: dump_memory, symbol: capture_buffer, output_path: build/capture.hex}
  - {device: dut, action: debug_stop}
  - {device: dut, action: uart_close}
```

`.agentic-hil/testconfig.yaml` and `--test-config` select only this test plan: ordered test steps and logical device names. They contain no hardware resources or permissions. The reactor gets all hardware settings from the discovered authoritative config or its `AGENTIC_HIL_CONFIG` override:

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

- Deny-by-default permission switches per action class, with deliberate interlocks — flashing is refused while `allow_raw_debugger_commands` or `allow_mass_erase` is enabled. `permission_denied` results are authoritative and agents are instructed to stop (see [AGENTS.md](AGENTS.md)).
- Configured executables and OpenOCD scripts are pinned at startup and must resolve outside the workspace; firmware artifacts are validated, hashed, and staged in a private process directory before any backend consumes them.
- Serial/CAN writes and reads are size- and buffer-capped; debugger calls run with timeouts and OpenOCD's TCP servers disabled.
- One live owner per project and physical probe/port/bus across all entry points; a second process gets `resource_busy`, and crashes or unknown effects quarantine the resource until explicit operator recovery ([TROUBLESHOOTING.md](TROUBLESHOOTING.md#13-resource_busy-or-quarantined-hardware)).
- Canonical reports and the tamper-evident audit chain live under the operator-pinned `state_root`; workspace logs and reports are untrusted mirrors, verified against the chain on read.

The complete threat model and design rationale: [docs/security-design.md](docs/security-design.md).

## pytest Plugin

Installing `agentic_hil` registers the `agentic_hil` pytest plugin, so CI regression suites can drive the same permission-gated tools without an MCP client.

The `agentic_hil` fixture uses the same discovered config or absolute-path override as every other entry point and verifies that its `workspace_root` matches the pytest rootdir. Tests using the fixture skip when no config exists and fail loudly when an available config is invalid. Pytest executes project code and is therefore not a sandbox or security boundary; real unattended hardware runners must still use OS isolation and host-managed invocation. COM and CAN sessions opened during a test are stopped afterwards so stimulus state cannot leak between tests. See [examples/nucleo-f446re_demo/](examples/nucleo-f446re_demo/) for the complete loop on real hardware.

## Common Commands

```text
agentic-hil setup --agent <claude-code|codex|opencode>
agentic-hil init
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

Linux, macOS, and Windows (CI-tested on Python 3.10–3.13). Debugger backends: OpenOCD, pyOCD (`agentic-hil[pyocd]` — covers most ARM Cortex-M targets via CMSIS packs and CMSIS-DAP/ST-Link/J-Link probes, set `debugger.target_type`), and STM32CubeProgrammer CLI (auto-discovered on Windows). Direct CAN requires `agentic-hil[can]` (python-can); CAN also supports a configured `process` bridge backend.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check src tests
pytest
python -m build
twine check dist/*
```

The package is configured for PyPI publishing through GitHub trusted publishing in `.github/workflows/workflow.yml`.

## Security

Policy bypasses are treated as vulnerabilities — see [SECURITY.md](SECURITY.md).

## License

Apache-2.0 — see [LICENSE](LICENSE).
