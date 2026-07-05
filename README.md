# HardCI

**HardCI gives AI coding agents safe, project-scoped access to real embedded hardware.**

HardCI is a Python package that exposes bounded MCP tools for probing, flashing, resetting, artifact validation, serial feedback, CAN stimuli, reports, and logs — without giving an agent arbitrary host or debugger access. A project-local policy file (`.hardci/config.yaml`) defines exactly which devices, actions, paths, and limits are allowed.

## Why

A green build is not enough in embedded development: firmware has to behave correctly on the real board. AI agents and CI pipelines need that hardware feedback loop, but handing them a raw debugger shell or direct serial access is neither safe nor reproducible. HardCI closes the gap with a small, auditable gate:

```
AI agent / CI  ──MCP (stdio)──▶  HardCI  ──policy check──▶  OpenOCD / STM32CubeProgrammer
                                    │                        serial ports (pyserial)
                                    │                        CAN (PEAK / SocketCAN / bridge)
                                    ▼
                       structured results, reports, logs
```

Every hardware action is validated against the project policy, executed with timeouts, logged to `.hardci/logs/`, and answered with a structured JSON result (`ok`, `error_type`, `summary`, `likely_causes`, `report_path`, `log_path`) that an agent can act on.

## Install

Recommended user install:

```bash
pipx install hardci
hardci init
hardci doctor
```

Without pipx:

```bash
python -m pip install --user hardci
python -m hardci init
python -m hardci doctor
```

For direct PEAK/SocketCAN access install the CAN extra: `pipx install 'hardci[can]'`.

## MCP Entry

Project-local `.mcp.json`:

```json
{
  "mcpServers": {
    "hardci": {
      "command": "hardci",
      "args": ["mcp-stdio", "--config", ".hardci/config.yaml"]
    }
  }
}
```

## Configuration

`hardci init` writes a starter `.hardci/config.yaml`. The file is the policy — it names the target, the debugger backend, allowed artifact roots, named serial ports and CAN buses, and per-action permissions:

```yaml
target:
  name: "sensor-board"
  controller: "stm32f4"

debugger:
  type: "openocd"            # or "stlink" (STM32CubeProgrammer CLI)
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

Export the full JSON schema with `hardci schema --output hardci-config.schema.json`.

## MCP Tools

| Group | Tools | Notes |
|-------|-------|-------|
| Debugger | `hardci_debugger_info`, `hardci_probe_target`, `hardci_reset_target` | OpenOCD or STM32CubeProgrammer CLI |
| Firmware | `hardci_flash_firmware`, `hardci_artifact_upload` | artifacts are validated (path, extension, format, SHA-256) before flashing |
| Serial | `hardci_com_ports_list`, `hardci_com_session_start`, `hardci_com_session_stop`, `hardci_com_write`, `hardci_com_read` | named ports only, buffered background reader |
| CAN | `hardci_can_buses_list`, `hardci_can_session_start`, `hardci_can_session_stop`, `hardci_can_send`, `hardci_can_read` | PEAK, SocketCAN, or a process bridge |
| Test adapters | `hardci_adapters_list`, `hardci_adapter_session_start`, `hardci_adapter_session_stop`, `hardci_adapter_set_value`, `hardci_adapter_inject_fault`, `hardci_adapter_clear_fault`, `hardci_adapter_measure` | sensor/actuator/fault simulation via the [adapter bridge protocol](examples/adapters/README.md) |
| Diagnostics | `hardci_get_last_report`, `hardci_classify_last_error` | structured error classification with likely causes |
| Debug sessions | `hardci_debug_*` (start/stop/status, breakpoints, continue/halt, symbol info, memory dump) | reserved API — returns `not_supported` in this build |

A typical loop: build firmware → `hardci_flash_firmware` → `hardci_com_session_start` → stimulate via `hardci_com_write`/`hardci_can_send`/`hardci_adapter_set_value` → assert on `hardci_com_read`/`hardci_can_read`/`hardci_adapter_measure` → on failure, `hardci_classify_last_error`.

## Test Adapters

Real-world firmware bugs show up under electrical conditions that standard lab tools cannot reproduce on demand: an open or shorted sensor, a drifting NTC, a missing load, a bouncing contact. The `adapters:` section connects HardCI to test adapters that simulate exactly these states — physical adapter hardware or pure-software simulators, both speaking the same [JSON bridge protocol](examples/adapters/README.md).

Example diagnosis loop with the bundled NTC simulator (`examples/adapters/sim_ntc_adapter.py`): flash the firmware, set the simulated sensor to 25 °C and assert nominal behavior, inject an `open` fault and assert the firmware reports the sensor failure, clear the fault and assert recovery — every step automated, reproducible, and policy-gated.

## Safety Model

- The agent never gets a shell, a raw debugger, or a device path — only the named, configured resources.
- Firmware artifacts must live under `artifacts.allowed_roots`, match an allowed extension, pass format plausibility checks, and are hashed before flashing. Path traversal is rejected.
- Every action class has its own permission switch; `permission_denied` results are authoritative and agents are instructed to stop (see [AGENTS.md](AGENTS.md)).
- Deliberate interlock: flashing is refused while `allow_raw_debugger_commands` or `allow_mass_erase` is enabled — validated flashing and unrestricted debugger access are mutually exclusive policies.
- Serial/CAN writes are size-capped (`max_write_bytes`, `max_frame_data_bytes`); reads are buffer-capped. Debugger calls run with timeouts and TCP servers disabled (OpenOCD `gdb_port`/`tcl_port`/`telnet_port disabled`).
- Test adapter channels and fault names are explicit allowlists — HardCI rejects anything not named in the config before it reaches the adapter bridge.
- All actions log to `.hardci/logs/` and write a structured report to `.hardci/reports/`.

## pytest Plugin

Installing `hardci` registers a pytest plugin, so CI regression suites can drive the same policy-gated tools without an MCP client:

```python
def test_open_sensor_diagnosis(hardci):
    started = hardci.call("hardci_adapter_session_start", {"adapter_id": "ntc_sim"})
    assert started["ok"] is True
    injected = hardci.call("hardci_adapter_inject_fault", {"adapter_id": "ntc_sim", "fault": "open"})
    assert injected["ok"] is True
    # ...assert the firmware's reaction via hardci_com_read...
```

The `hardci` fixture loads `.hardci/config.yaml` relative to the pytest rootdir (override with `--hardci-config` or the `hardci_config` ini option). Tests are skipped when no configuration file exists, but an existing invalid configuration fails loudly — a config typo must not silently disable the hardware suite in CI. Adapter, COM, and CAN sessions opened during a test are stopped afterwards so stimulus state cannot leak between tests. See [examples/pytest/](examples/pytest/) for a full diagnosis-loop example, and [examples/nucleo-f446re_demo/](examples/nucleo-f446re_demo/) for the complete loop on real hardware: a bare-metal STM32 firmware that is built, flashed, reset, and asserted on via its UART boot banner.

## Common Commands

```text
hardci init
hardci doctor
hardci com-ports
hardci mcp-stdio --config .hardci/config.yaml
hardci com-stdio --config .hardci/config.yaml --port dut_uart
hardci schema --output hardci-config.schema.json
hardci skill-install --agent opencode
```

## Platform Support

Linux, macOS, and Windows (CI-tested on Python 3.10–3.13). Debugger backends: OpenOCD and STM32CubeProgrammer CLI (auto-discovered on Windows). Direct CAN requires `hardci[can]` (python-can); any other adapter can be attached through the `process` bridge protocol.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check src tests
pytest
python -m build
twine check dist/*
```

The package is configured for PyPI publishing through GitHub trusted publishing in `.github/workflows/pypi-publish.yml`.

## Security

Policy bypasses are treated as vulnerabilities — see [SECURITY.md](SECURITY.md).

## License

Apache-2.0 — see [LICENSE](LICENSE).
