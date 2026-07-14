# Agentic HIL

**Your AI agent can develop firmware on its own — because Agentic HIL closes the loop with real hardware.**

```
+--> build --> flash --> stimulate --> observe --+
|                                                |
+<-------------- diagnose & fix -----------------+

  your agent, unattended -- you review the pull request
```

Agentic HIL is a Python package that exposes bounded MCP tools for probing, flashing, resetting, artifact validation, serial and CAN stimulus/feedback, test adapters, reports, and logs — without giving an agent arbitrary host or debugger access. The project-local `.agentic-hil/config.yaml` requests project resources, while a host-managed trusted policy outside the agent-writable workspace defines the maximum devices, actions, paths, and limits. That independent policy gate is what makes unattended hardware access workable in the first place.

Agentic HIL adapters are the reference hardware for Agentic HIL: physical pytest fixtures for sensor simulation, loads, and fault injection.

Names: the Python package/install target and Python-facing identifiers such as imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`. The CLI command, repository URL, MCP server name, and docs prose use `agentic-hil`.

## Install

The easiest path: copy/paste this prompt to your AI agent:

```text
Install from https://github.com/hp-8472/agentic-hil and set it up for this project.
```

Agents follow [AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) — everything installs user-local, **no admin rights required, ever**.

If you want to install it yourself anyway, install the Python package with pip and then use the `agentic-hil` command:

```bash
pip install agentic_hil
agentic-hil --version
```

If that fails because Python is externally managed, `agentic-hil` is not on `PATH`, or the package is unavailable through that interpreter, use the `uv`/`pipx` paths below instead. Never use `pip install --break-system-packages`.

Without installing anything (no `PATH` changes; needs [uv](https://docs.astral.sh/uv/) or pipx):

```bash
uvx --from agentic_hil agentic-hil --version
uvx --from git+https://github.com/hp-8472/agentic-hil agentic-hil --version
```

Alternative isolated user-local install (recommended when the MCP client needs a stable command on `PATH`):

```bash
uv tool install agentic_hil      # or: pipx install agentic_hil
agentic-hil init
agentic-hil mcp-config --output .mcp.json
# Human/operator step, run from the project directory:
agentic-hil policy-init --output /absolute/host-owned/path/project-policy.yaml
# Set AGENTIC_HIL_POLICY to that absolute path in the MCP host environment.
agentic-hil doctor
```

For direct PEAK/SocketCAN access install the CAN extra: `uv tool install 'agentic_hil[can]'`. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) when something does not start.

## Why

A green build is not enough in embedded development: firmware has to behave correctly on the real board. Classic tools automate single steps — flash here, read a log there — but the moment real hardware has to respond, a human is back in the loop. Handing an agent a raw debugger shell or direct serial access instead is neither safe nor reproducible. Agentic HIL closes the gap with a small, auditable gate:

```
AI agent / CI  ──MCP (stdio)──▶  Agentic HIL  ──trusted policy──▶  OpenOCD / pyOCD / STM32CubeProgrammer
                                    │                        serial ports (pyserial)
                                    │                        CAN (PEAK / SocketCAN / bridge)
                                    ▼
                       structured results, reports, logs
```

Every hardware action is validated against the effective intersection of project configuration and trusted host policy, executed with timeouts, logged to `.agentic-hil/logs/`, and answered with a structured JSON result (`ok`, `error_type`, `summary`, `likely_causes`, `report_path`, `log_path`) that an agent can act on.

## MCP Entry

Project-local `.mcp.json`:

```json
{
  "mcpServers": {
    "agentic-hil": {
      "command": "agentic-hil",
      "args": ["mcp-stdio", "--config", ".agentic-hil/config.yaml"]
    }
  }
}
```

`mcp-stdio` also requires `AGENTIC_HIL_POLICY` in the MCP host process environment. It must contain an absolute path to a human-reviewed policy outside the project workspace. Do not put that path or an environment override in repository-controlled MCP configuration. A project-local `.mcp.json` is discovery convenience, not a root of trust; unattended hardware hosts should register the server in host/user-level MCP configuration that the agent cannot edit.

## Configuration

`agentic-hil init` writes a starter `.agentic-hil/config.yaml`. This project file requests the target, debugger backend, artifact roots, named serial ports and CAN buses, and per-action permissions:

```yaml
target:
  name: "sensor-board"
  controller: "stm32f4"

debugger:
  type: "openocd"            # or "pyocd" (most Cortex-M targets), or "stlink" (STM32CubeProgrammer CLI)
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
  timeout_s: 60

debug:
  allowed_symbols: ["main", "sensor_state"]
  allow_all_symbols: false

artifacts:
  allowed_roots: ["build"]   # firmware may only be flashed from here
  allowed_extensions: [".elf", ".hex", ".bin"]

com_ports:
  dut_uart:
    device: "/dev/ttyACM0"
    baudrate: 115200

can_buses:
  dut_can:
    adapter: "socketcan"     # or "peak", or "process" for a custom bridge
    channel: "can0"
    bitrate: 500000

adapters:
  ntc_sim:                   # sensor/actuator/fault-simulation bridge
    executable: "examples/adapters/sim_ntc_adapter.py"
    channels: ["temperature", "resistance"]
    faults: ["open", "short_to_gnd", "short_to_vcc"]

permissions:
  allow_flash: true
  allow_reset: true
  allow_com_write: true
  allow_can_write: true
  allow_adapter_write: true
  allow_raw_debugger_commands: false
  allow_mass_erase: false
```

The human operator creates a second file with the same schema using `agentic-hil policy-init --output <absolute-path>`, reviews it, stores it outside the workspace, and exposes its path through `AGENTIC_HIL_POLICY`. The generated trusted policy denies every hardware permission and artifact upload until the operator explicitly enables them. When debugger access is enabled, `debugger.executable` and (for OpenOCD) `debugger.interface_cfg`/`target_cfg` must resolve to existing host-owned files outside the workspace; GDB and process bridges are pinned under the same rule.

The trusted file is loaded once at MCP startup. On every tool call, the project file is reloaded and intersected with that immutable startup ceiling: permission booleans are ANDed, validation requirements cannot be disabled, allowlists/resources are intersected, limits use the stricter value, and executable/device settings come from the trusted policy. Empty symbol allowlists deny all symbols; unrestricted symbol access requires `allow_all_symbols: true` in both files. Project edits can revoke access immediately but cannot exceed the startup ceiling; widening it requires a human-controlled policy edit and MCP restart.

Export the full JSON schema with `agentic-hil schema --output agentic-hil-config.schema.json`.

## MCP Tools

| Group | Tools | Notes |
|-------|-------|-------|
| Debugger | `debugger_info`, `probe_target`, `reset_target` | OpenOCD, pyOCD, or STM32CubeProgrammer CLI |
| Firmware | `flash_firmware`, `artifact_upload` | artifacts are validated, rechecked, and copied to private process staging before flashing; all supported flash backends reset internally and therefore require `allow_reset` |
| Serial | `com_ports_list`, `com_session_start`, `com_session_stop`, `com_write`, `com_read` | named ports only, buffered background reader |
| CAN | `can_buses_list`, `can_session_start`, `can_session_stop`, `can_send`, `can_read` | PEAK, SocketCAN, or a process bridge |
| Test adapters | `adapters_list`, `adapter_session_start`, `adapter_session_stop`, `adapter_set_value`, `adapter_inject_fault`, `adapter_clear_fault`, `adapter_measure` | sensor/actuator/fault simulation via the [adapter bridge protocol](examples/adapters/README.md) |
| Diagnostics | `get_last_report`, `classify_last_error` | structured error classification with likely causes |
| Debug sessions | `debug_*` (start/stop/status, breakpoints, continue/halt, symbol info, memory dump) | typed GDB/MI sessions via the OpenOCD backend's gdbserver; unexpected breakpoints and target exceptions are returned as structured stop reasons; symbol allowlist and dump-size limits come from the `debug:` policy section |

A typical loop: build firmware → `flash_firmware` with `reset_after_flash: true` when a fresh boot is required → `com_session_start` → stimulate via `com_write`/`can_send`/`adapter_set_value` → assert on `com_read`/`can_read`/`adapter_measure` → on failure, `classify_last_error`.

## Test Adapters

Real-world firmware bugs show up under electrical conditions that standard lab tools cannot reproduce on demand: an open or shorted sensor, a drifting NTC, a missing load, a bouncing contact. The `adapters:` section connects Agentic HIL to test adapters that simulate exactly these states — physical adapter hardware or pure-software simulators, both speaking the same [JSON bridge protocol](examples/adapters/README.md).

Example diagnosis loop with the bundled NTC simulator (`examples/adapters/sim_ntc_adapter.py`): flash the firmware, set the simulated sensor to 25 °C and assert nominal behavior, inject an `open` fault and assert the firmware reports the sensor failure, clear the fault and assert recovery — every step automated, reproducible, and policy-gated.

## Safety Model

- The agent never gets a shell, a raw debugger, or a device path — only resources present in both the project configuration and trusted host policy.
- The trusted policy must be an absolute path outside the workspace. Configured debugger and process-bridge executables are pinned at startup and rejected if they resolve inside the workspace.
- Firmware artifacts must live under `artifacts.allowed_roots`, match an allowed extension, pass format plausibility checks, and are hashed before flashing. Path traversal is rejected.
- Validated artifacts are reopened without following links, checked for replacement/hard links, and staged in a private process directory before any debugger backend can consume them.
- Permission switches gate high-risk action classes, including debugger execution and reset; flashing requires both `allow_flash` and `allow_reset` because all supported flash backends perform internal resets. `permission_denied` results are authoritative and agents are instructed to stop (see [AGENTS.md](AGENTS.md)).
- Deliberate interlock: flashing is refused while `allow_raw_debugger_commands` or `allow_mass_erase` is enabled — validated flashing and unrestricted debugger access are mutually exclusive policies.
- Serial/CAN writes are size-capped (`max_write_bytes`, `max_frame_data_bytes`); reads are buffer-capped. Debugger calls run with timeouts and TCP servers disabled (OpenOCD `gdb_port`/`tcl_port`/`telnet_port disabled`); only a typed debug session opens a `gdb_port`, bound to `localhost` on an ephemeral port for exactly that session, and it is torn down with the session.
- Test adapter channels and fault names are explicit allowlists — Agentic HIL rejects anything not named in the config before it reaches the adapter bridge.
- All actions log to `.agentic-hil/logs/` and write a structured report to `.agentic-hil/reports/`.

## pytest Plugin

Installing `agentic_hil` registers the `agentic_hil` pytest plugin, so CI regression suites can drive the same policy-gated tools without an MCP client:

```python
def test_open_sensor_diagnosis(agentic_hil):
    started = agentic_hil.call("adapter_session_start", {"adapter_id": "ntc_sim"})
    assert started["ok"] is True
    injected = agentic_hil.call("adapter_inject_fault", {"adapter_id": "ntc_sim", "fault": "open"})
    assert injected["ok"] is True
    # ...assert the firmware's reaction via com_read...
```

The `agentic_hil` fixture loads `.agentic-hil/config.yaml` relative to the pytest rootdir (override with `--agentic-hil-config` or the `agentic_hil_config` ini option). Its startup configuration is frozen as the maximum for that process, so a test cannot widen it during the session. Pytest executes project code and is therefore not a sandbox or security boundary; real unattended hardware runners must still use OS isolation and host-managed invocation. Tests are skipped when no configuration file exists, but an existing invalid configuration fails loudly. Adapter, COM, and CAN sessions opened during a test are stopped afterwards so stimulus state cannot leak between tests. See [examples/pytest/](examples/pytest/) for a full diagnosis-loop example, and [examples/nucleo-f446re_demo/](examples/nucleo-f446re_demo/) for the complete loop on real hardware.

## Common Commands

```text
agentic-hil init
agentic-hil policy-init --output /absolute/host-owned/path/project-policy.yaml
agentic-hil doctor
agentic-hil com-ports
agentic-hil mcp-config --output .mcp.json
AGENTIC_HIL_POLICY=/absolute/host-owned/path/project-policy.yaml agentic-hil mcp-stdio --config .agentic-hil/config.yaml
agentic-hil com-stdio --config .agentic-hil/config.yaml --port dut_uart
agentic-hil schema --output agentic-hil-config.schema.json
agentic-hil skill-install --agent opencode
```

## Platform Support

Linux, macOS, and Windows (CI-tested on Python 3.10–3.13). Debugger backends: OpenOCD, pyOCD (`agentic_hil[pyocd]` — covers most ARM Cortex-M targets via CMSIS packs and CMSIS-DAP/ST-Link/J-Link probes, set `debugger.target_type`), and STM32CubeProgrammer CLI (auto-discovered on Windows). Direct CAN requires `agentic_hil[can]` (python-can); any other adapter can be attached through the `process` bridge protocol.

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
