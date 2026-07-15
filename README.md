# Agentic HIL

<!-- mcp-name: io.github.agentic-hil/agentic-hil -->

**Your AI agent can develop firmware on its own — because Agentic Hardware-in-the-Loop (Agentic HIL) closes the loop with real hardware.**

```
+--> build --> flash --> stimulate --> observe --+
|                                                |
+<-------------- diagnose & fix -----------------+

  your agent, unattended -- you review the pull request
```

Agentic HIL is a Python package that exposes bounded MCP tools for probing, flashing, resetting, artifact validation, serial and CAN stimulus/feedback, test adapters, reports, and logs — without giving an agent arbitrary host or debugger access. A project-local policy file (`.agentic-hil/config.yaml`) defines exactly which devices, actions, paths, and limits are allowed. That policy gate is what makes unattended hardware access workable in the first place.

HardCI adapters are the reference hardware for Agentic HIL: physical pytest fixtures for sensor simulation, loads, and fault injection.

Names: the Python distribution/install target, CLI command, repository URL, MCP server name, and docs prose use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

## Install

The easiest path: copy/paste this prompt to your AI agent:

```text
Install from https://github.com/agentic-hil/agentic-hil and set it up for this project.
```

Agents follow [AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) — everything installs user-local, **no admin rights required, ever**.

If you want to install it yourself anyway, install the Python package with pip and then use the `agentic-hil` command:

```bash
pip install agentic-hil
agentic-hil --version
```

If that fails because Python is externally managed, `agentic-hil` is not on `PATH`, or the package is unavailable through that interpreter, use the `uv`/`pipx` paths below instead. Never use `pip install --break-system-packages`.

Without installing anything (no `PATH` changes; needs [uv](https://docs.astral.sh/uv/) or pipx):

```bash
uvx --from agentic-hil agentic-hil --version
uvx --from git+https://github.com/agentic-hil/agentic-hil agentic-hil --version
```

Alternative isolated user-local install (recommended when the MCP client needs a stable command on `PATH`):

```bash
uv tool install agentic-hil      # or: pipx install agentic-hil
agentic-hil init
agentic-hil doctor
agentic-hil mcp-config --output .mcp.json
```

For direct PEAK/SocketCAN access install the CAN extra: `uv tool install 'agentic-hil[can]'`. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) when something does not start.

## Why

A green build is not enough in embedded development: firmware has to behave correctly on the real board. Classic tools automate single steps — flash here, read a log there — but the moment real hardware has to respond, a human is back in the loop. Handing an agent a raw debugger shell or direct serial access instead is neither safe nor reproducible. Agentic HIL closes the gap with a small, auditable gate:

```
AI agent / CI  ──MCP (stdio)──▶  Agentic HIL  ──policy check──▶  OpenOCD / pyOCD / STM32CubeProgrammer
                                    │                        serial ports (pyserial)
                                    │                        CAN (PEAK / SocketCAN / bridge)
                                    ▼
                       structured results, reports, logs
```

Every hardware action is validated against the project policy, executed with timeouts, logged to `.agentic-hil/logs/`, and answered with a structured JSON result (`ok`, `error_type`, `summary`, `likely_causes`, `report_path`, `log_path`) that an agent can act on.

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

Agentic HIL releases are published to the preview official MCP Registry as `io.github.agentic-hil/agentic-hil`, pointing to the same public PyPI package and `mcp-stdio` command. The registry is a discovery channel, not a substitute for project setup or policy review. The host-independent manual path remains supported:

```bash
uv tool install agentic-hil
agentic-hil init
agentic-hil doctor
agentic-hil mcp-config --output .mcp.json
```

Registry installation never grants additional hardware access. Every action is still constrained by the project-local `.agentic-hil/config.yaml`; review [SECURITY.md](SECURITY.md) before enabling unattended hardware access.

## Configuration

`agentic-hil init` writes a starter `.agentic-hil/config.yaml`. The file is the policy — it names the target, the debugger backend, allowed artifact roots, named serial ports and CAN buses, and per-action permissions:

```yaml
target:
  name: "sensor-board"
  controller: "stm32f4"

debugger:
  type: "openocd"            # or "pyocd" (most Cortex-M targets), or "stlink" (STM32CubeProgrammer CLI)
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
  timeout_s: 60

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
  allow_com_write: true
  allow_can_write: true
  allow_adapter_write: true
  allow_raw_debugger_commands: false
  allow_mass_erase: false
```

The MCP server reloads and validates this policy before every tool call, so configuration changes take effect without restarting the client. Invalid edits fail closed instead of continuing with the previously loaded policy.

For a project with more than one DUT or probe, keep the single project policy and add named debuggers and logical devices. The existing `debugger:` remains the default debugger used by MCP tools and can be selected with `debugger: "default"`:

```yaml
debuggers:
  controller_probe:
    type: "openocd"
    probe_id: "066DFF575051717867013749"
    interface_cfg: "interface/stlink.cfg"
    target_cfg: "target/stm32f4x.cfg"

devices:
  controller:
    debugger: "controller_probe"
    uart: "dut_uart"
    target:
      name: "controller-board"
      controller: "stm32f4"
```

Export the full JSON schema with `agentic-hil schema --output agentic-hil-config.schema.json`.

## MCP Tools

| Group | Tools | Notes |
|-------|-------|-------|
| Debugger | `debugger_info`, `debugger_probes_list`, `probe_target`, `reset_target` | Probe discovery with pyOCD or STM32CubeProgrammer; target access with OpenOCD, pyOCD, or STM32CubeProgrammer CLI |
| Firmware | `flash_firmware`, `artifact_upload` | artifacts are validated (path, extension, format, SHA-256) before flashing; post-flash reset requires `reset_after_flash: true` |
| Serial | `com_ports_list`, `com_session_start`, `com_session_stop`, `com_write`, `com_read` | named ports only, buffered background reader |
| CAN | `can_buses_list`, `can_session_start`, `can_session_stop`, `can_send`, `can_read` | PEAK, SocketCAN, or a process bridge |
| Test adapters | `adapters_list`, `adapter_session_start`, `adapter_session_stop`, `adapter_set_value`, `adapter_inject_fault`, `adapter_clear_fault`, `adapter_measure` | sensor/actuator/fault simulation via the [adapter bridge protocol](examples/adapters/README.md) |
| Diagnostics | `get_last_report`, `classify_last_error` | structured error classification with likely causes |
| Debug sessions | `debug_*` (start/stop/status, breakpoints, continue/halt, symbol info, memory dump) | typed GDB/MI sessions via the OpenOCD backend's gdbserver; unexpected breakpoints and target exceptions are returned as structured stop reasons; symbol allowlist and dump-size limits come from the `debug:` policy section |

A typical loop: build firmware → `flash_firmware` with `reset_after_flash: true` when a fresh boot is required → `com_session_start` → stimulate via `com_write`/`can_send`/`adapter_set_value` → assert on `com_read`/`can_read`/`adapter_measure` → on failure, `classify_last_error`.

## Test Adapters

Real-world firmware bugs show up under electrical conditions that standard lab tools cannot reproduce on demand: an open or shorted sensor, a drifting NTC, a missing load, a bouncing contact. The `adapters:` section connects Agentic HIL to test adapters that simulate exactly these states — physical adapter hardware or pure-software simulators, both speaking the same [JSON bridge protocol](examples/adapters/README.md).

Example diagnosis loop with the bundled NTC simulator (`examples/adapters/sim_ntc_adapter.py`): flash the firmware, set the simulated sensor to 25 °C and assert nominal behavior, inject an `open` fault and assert the firmware reports the sensor failure, clear the fault and assert recovery — every step automated, reproducible, and policy-gated.

## Test Reactor

The test reactor executes validated hardware workflows against the logical `devices:` in the one project-local `.agentic-hil/config.yaml`. A project can have any number of separate test configuration files. Pass the selected file explicitly; it may be inside the project, elsewhere on the machine, or below the user home directory via `~`:

```bash
agentic-hil test-reactor --config .agentic-hil/config.yaml --test-config tests/hardware/boot.yaml
agentic-hil test-reactor --config .agentic-hil/config.yaml --test-config ~/agentic-hil-tests/controller/diagnostics.yaml
```

Relative test-configuration paths are resolved from the project working directory. No directory is searched automatically and no project/user precedence rule applies. Tests in the selected file run strictly one at a time. A project-wide hardware lease blocks concurrent reactor, MCP, CLI, and pytest hardware access; policy changes during a run abort remaining hardware actions.

```yaml
version: 1
tests:
  - name: capture-diagnostic-buffer
    device: controller
    steps:
      - action: debug_start
        image_path: build/firmware.elf
        mode: attach
      - action: run_until_breakpoint
        location: test_done
        timeout_s: 10
      - action: dump_memory
        symbol: diagnostic_buffer
        output_path: build/diagnostic-buffer.hex
      - action: debug_stop
```

Supported steps are `flash`, `uart_open`, `uart_close`, `debug_start`, `run_until_breakpoint`, `dump_memory`, and `debug_stop`. The complete file is schema-validated and preflighted against device capabilities, artifacts, permissions, symbol allowlists, and step ordering before the first hardware action. Sessions opened by a failed test are cleaned up automatically.

Export the editor/validation schema with `agentic-hil test-schema --output agentic-hil-test.schema.json`. A complete starting point is available at [`examples/test-reactor/diagnostic.yaml`](examples/test-reactor/diagnostic.yaml).

## Safety Model

- The agent never gets a shell, a raw debugger, or a device path — only the named, configured resources.
- Firmware artifacts must live under `artifacts.allowed_roots`, match an allowed extension, pass format plausibility checks, and are hashed before flashing. Path traversal is rejected.
- Permission switches gate high-risk action classes; `permission_denied` results are authoritative and agents are instructed to stop (see [AGENTS.md](AGENTS.md)).
- Deliberate interlock: flashing is refused while `allow_raw_debugger_commands` or `allow_mass_erase` is enabled — validated flashing and unrestricted debugger access are mutually exclusive policies.
- Serial/CAN writes are size-capped (`max_write_bytes`, `max_frame_data_bytes`); reads are buffer-capped. Debugger calls run with timeouts and TCP servers disabled (OpenOCD `gdb_port`/`tcl_port`/`telnet_port disabled`); only a typed debug session opens a `gdb_port`, bound to `localhost` on an ephemeral port for exactly that session, and it is torn down with the session.
- Test adapter channels and fault names are explicit allowlists — Agentic HIL rejects anything not named in the config before it reaches the adapter bridge.
- All actions log to `.agentic-hil/logs/` and write a structured report to `.agentic-hil/reports/`.

## pytest Plugin

Installing `agentic-hil` registers the `agentic_hil` pytest plugin, so CI regression suites can drive the same policy-gated tools without an MCP client:

```python
def test_open_sensor_diagnosis(agentic_hil):
    started = agentic_hil.call("adapter_session_start", {"adapter_id": "ntc_sim"})
    assert started["ok"] is True
    injected = agentic_hil.call("adapter_inject_fault", {"adapter_id": "ntc_sim", "fault": "open"})
    assert injected["ok"] is True
    # ...assert the firmware's reaction via com_read...
```

The `agentic_hil` fixture loads `.agentic-hil/config.yaml` relative to the pytest rootdir (override with `--agentic-hil-config` or the `agentic_hil_config` ini option). Tests are skipped when no configuration file exists, but an existing invalid configuration fails loudly — a config typo must not silently disable the hardware suite in CI. Adapter, COM, and CAN sessions opened during a test are stopped afterwards so stimulus state cannot leak between tests. See [examples/pytest/](examples/pytest/) for a full diagnosis-loop example, and [examples/nucleo-f446re_demo/](examples/nucleo-f446re_demo/) for the complete loop on real hardware: a bare-metal STM32 firmware that is built, flashed, reset, and asserted on via its UART boot banner.

## Common Commands

```text
agentic-hil init
agentic-hil doctor
agentic-hil debugger-probes --config .agentic-hil/config.yaml --debugger controller_probe
agentic-hil com-ports
agentic-hil test-reactor --config .agentic-hil/config.yaml --test-config tests/hardware/boot.yaml
agentic-hil mcp-config --output .mcp.json
agentic-hil mcp-stdio --config .agentic-hil/config.yaml
agentic-hil com-stdio --config .agentic-hil/config.yaml --port dut_uart
agentic-hil schema --output agentic-hil-config.schema.json
agentic-hil test-schema --output agentic-hil-test.schema.json
agentic-hil skill-install --agent opencode
```

## Platform Support

Linux, macOS, and Windows (CI-tested on Python 3.10–3.13). Debugger backends: OpenOCD, pyOCD (`agentic-hil[pyocd]` — covers most ARM Cortex-M targets via CMSIS packs and CMSIS-DAP/ST-Link/J-Link probes, set `debugger.target_type`), and STM32CubeProgrammer CLI (auto-discovered on Windows). Direct CAN requires `agentic-hil[can]` (python-can); any other adapter can be attached through the `process` bridge protocol.

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
