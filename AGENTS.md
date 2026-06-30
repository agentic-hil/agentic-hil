# AI-HIL Agent Instructions

This file is for AI coding agents.

Use AI-HIL as the safe local hardware-in-the-loop bridge for embedded projects. AI-HIL gives the agent bounded MCP tools for real hardware actions such as probing, flashing, resetting, reading structured reports, and interacting with configured serial/COM ports and CAN buses.

Do not use raw OpenOCD commands, arbitrary debugger shells, direct host COM-port access, or direct CAN adapter access when an AI-HIL MCP tool is available.

## Core rule

AI-HIL is the hardware gate. The project-local `.aihil/config.yaml` is the policy.

If an AI-HIL command or MCP tool returns `permission_denied`, stop. Do not work around the policy with shell commands, raw OpenOCD, mass erase, direct serial tools, direct CAN adapter tools, or unconfigured host device access.

## Human vs agent documentation

Humans should start with:

- `README.md`
- `TROUBLESHOOTING.md`

Agents should use:

- `AGENTS.md`
- `AI_AGENT_QUICKSTART.md`
- `skills/aihil-config-setup/SKILL.md`

The setup skill is the preferred workflow for creating or fixing `.aihil/config.yaml`.

## Installation model

AI-HIL is installed once on the local machine as the `aihil` command. Each firmware project gets its own `.aihil/` directory and `.mcp.json`.

When the user says something like:

```text
Install https://github.com/hp-8472/aihil for this firmware project.
```

follow this model:

1. Stay aware of the difference between the firmware project and the AI-HIL source repository.
2. Install the `aihil` command.
3. Return to the firmware project.
4. Create or update the project-local `.aihil/config.yaml`.
5. Validate with `aihil doctor`.
6. Add the standard portable `.mcp.json` only if the MCP client needs project discovery.
7. Use AI-HIL MCP tools for hardware actions.
8. Do not copy or vendor the AI-HIL source tree into the firmware project unless the user explicitly asks for that.

Before installing, check whether AI-HIL is already available:

```bash
aihil --version
```

On Windows, also try:

```bash
aihil.cmd --version
```

If AI-HIL is not installed, install from npm:

```bash
npm i -g aihil
```

If the current working directory is a checkout of the AI-HIL repository and the task is AI-HIL development, install that local checkout with:

```bash
npm install
npm install --global .
```

For AI-HIL development and tests, use:

```bash
npm install
npm test
```

Do not bypass package `engines` with `--force` or `--ignore-engines`. Use a supported Node.js/npm runtime.

## Supported first path

Use this as the reference setup unless the firmware project or user clearly specifies a different target:

- Board: STM32 Nucleo-F446RE
- Debug probe: ST-Link, including the onboard Nucleo ST-Link
- Debug backend: OpenOCD
- Host runtime: Node.js with npm
- OpenOCD interface config: `interface/stlink.cfg`
- OpenOCD target config: `target/stm32f4x.cfg`
- Firmware artifact root: `build/`
- Firmware artifact formats: `.elf`, `.hex`, `.bin`

Other boards may work, but do not guess target, debugger, COM port, or artifact paths. If they cannot be inferred from project files, ask one concise question instead of inventing configuration.

## Project bootstrap

Run setup from the firmware project directory, not from the AI-HIL source repository:

```bash
aihil init
aihil doctor
```

Each firmware project owns its own `.aihil/` directory. That directory contains:

- `.aihil/config.yaml`
- hardware permissions
- allowed firmware artifact roots
- report paths
- raw log paths
- uploaded artifacts, if used
- optional named COM ports
- optional named CAN buses

Treat `.aihil/config.yaml` as project-local hardware policy. Preserve existing policy decisions when editing it. Do not overwrite an existing config with `aihil init --force` unless the user explicitly asks.

## Config editing rules

Do not hand-write `.aihil/config.yaml` from memory.

Use `aihil init` to create the starter config, then edit only project-specific values. Validate after each edit:

```bash
aihil doctor
```

Only change these fields unless the user explicitly asks for a broader policy change:

- `target.name`
- `target.controller`
- `debugger.executable`
- `debugger.probe_id`
- `debugger.interface`
- `debugger.flash_address`
- `debugger.interface_cfg`
- `debugger.target_cfg`
- `debugger.timeout_s`
- `artifacts.allowed_roots`
- `artifacts.allowed_extensions`
- `com_ports.<port_id>.device`
- `com_ports.<port_id>.baudrate`
- `com_ports.<port_id>.encoding`
- `can_buses.<bus_id>.adapter`
- `can_buses.<bus_id>.channel`
- `can_buses.<bus_id>.bitrate`
- `can_buses.<bus_id>.fd`
- `can_buses.<bus_id>.data_bitrate`
- `can_buses.<bus_id>.pcanbasic_dll`
- `can_buses.<bus_id>.executable`
- `can_buses.<bus_id>.args`

Do not enable these unless the user explicitly understands the risk and asks for a policy change:

```yaml
permissions:
  allow_raw_debugger_commands: true
  allow_mass_erase: true
```

The normal safe values are:

```yaml
permissions:
  allow_raw_debugger_commands: false
  allow_mass_erase: false
```

## MCP operating model

AI-HIL uses MCP over stdio. The project-local MCP client config is only a launch entry; it should normally be the stable portable `.mcp.json` shape below.

The MCP server entry starts AI-HIL like this:

```text
aihil mcp-stdio --config .aihil/config.yaml
```

`mcp-stdio` is project-scoped. Do not add `--port` to `mcp-stdio`.

COM MCP tools use configured `port_id` arguments when serial access is needed.

Example `.mcp.json` shape:

```json
{
  "mcpServers": {
    "aihil": {
      "command": "aihil",
      "args": ["mcp-stdio", "--config", ".aihil/config.yaml"]
    }
  }
}
```

## Plain COM stdio

Use the MCP COM tools for normal agent workflows.

If the user explicitly wants a separate continuous plain-text serial channel, use:

```bash
aihil com-stdio --config .aihil/config.yaml --port dut_uart
```

`com-stdio` is not MCP. It binds one plain text stream to one configured COM port. It always requires `--port`, and the value must be a configured `com_ports` id.

Do not mix plain COM text into `mcp-stdio`. MCP stdout must remain JSON-RPC only.

## Available MCP tools

Use `tools/list` to discover the current tool list. The expected AI-HIL tools are:

```text
aihil_debugger_info
aihil_probe_target
aihil_flash_firmware
aihil_reset_target
aihil_get_last_report
aihil_classify_last_error
aihil_com_ports_list
aihil_com_session_start
aihil_com_write
aihil_com_read
aihil_com_session_stop
aihil_can_buses_list
aihil_can_session_start
aihil_can_send
aihil_can_read
aihil_can_session_stop
```

Tool intent:

| Tool | Use |
| --- | --- |
| `aihil_debugger_info` | Check whether the configured debugger backend is available. |
| `aihil_probe_target` | Detect the configured embedded target before flashing. |
| `aihil_flash_firmware` | Flash exactly one validated `image_path` or `artifact_id`. |
| `aihil_reset_target` | Reset the target with mode `run`, `halt`, or `init`. |
| `aihil_get_last_report` | Read the most recent structured AI-HIL report. |
| `aihil_classify_last_error` | Classify the most recent hardware/debugger failure. |
| `aihil_com_ports_list` | List configured COM ports and detected host serial ports. |
| `aihil_com_session_start` | Start feedback capture on a configured COM port. |
| `aihil_com_write` | Send text or hex stimulus to a configured COM session. |
| `aihil_com_read` | Read buffered serial feedback from a configured COM session. |
| `aihil_com_session_stop` | Stop and close a configured COM session. |
| `aihil_can_buses_list` | List configured CAN buses and supported adapter backends. |
| `aihil_can_session_start` | Start a configured CAN bus session. |
| `aihil_can_send` | Send one CAN frame through a configured CAN session. |
| `aihil_can_read` | Read buffered CAN frames from a configured CAN session. |
| `aihil_can_session_stop` | Stop and close a configured CAN session. |

## Required hardware workflow

Use this loop for firmware tasks:

1. Build the firmware first.
2. Check debugger availability with `aihil_debugger_info` if setup is unclear.
3. Probe the target with `aihil_probe_target` before flashing.
4. Flash only validated artifacts with `aihil_flash_firmware`.
5. Reset with `aihil_reset_target` only when needed or requested.
6. For serial feedback, use configured COM port ids with `aihil_com_session_start`, `aihil_com_write`, `aihil_com_read`, and `aihil_com_session_stop`.
7. For CAN stimuli or feedback, use configured CAN bus ids with `aihil_can_session_start`, `aihil_can_send`, `aihil_can_read`, and `aihil_can_session_stop`.
8. Read `aihil_get_last_report` after hardware actions.
9. Use `aihil_classify_last_error` after failures.
10. Use the real hardware feedback for the next code change.
11. Repeat until the task is complete or a safety/configuration boundary is reached.

Do not open CAN adapters directly. Use AI-HIL's configured CAN MCP tools with named `bus_id` values.

Use `aihil com-stdio` only when the user explicitly wants a continuous text serial channel. The `--port` value must be a configured `com_ports` id.

Healthy signals:

- `aihil doctor` returns `ok: true`.
- `aihil_probe_target` returns `ok: true` and `target_detected: true`.
- `aihil_flash_firmware` returns `ok: true`, `verify: true`, and `reset_after_flash: true`.
- Hardware actions include `report_path` and `log_path` for auditability.

## Error handling

Treat structured JSON as the source of truth.

When an AI-HIL tool returns `ok: false`, inspect:

```text
error_type
backend_error_type
summary
likely_causes
report_path
log_path
```

Do not immediately change random configuration values. Use the reported error fields to decide the next step.

Common actions:

- `config_file_not_found`: run `aihil init` from the firmware project directory or check the working directory.
- `config_invalid`: fix YAML, field names, field types, or enum values reported by AI-HIL.
- `openocd_not_found` / `debugger_not_found`: report that OpenOCD or the configured debugger executable is missing or not on `PATH`.
- `adapter_not_found`: ask the user to connect/check the ST-Link or close another debugger session.
- `target_not_detected`: ask the user to check target power, wiring, boot mode, reset state, or OpenOCD target config.
- `artifact_not_found` / `artifact_validation_failed`: build firmware and flash an allowed `.elf`, `.hex`, or `.bin` from an allowed root.
- `permission_denied`: stop. The local config is authoritative.

## Safety rules

Never:

- run raw OpenOCD/debugger commands for hardware actions when AI-HIL tools are available,
- open host COM devices directly when AI-HIL COM tools are available,
- open CAN adapters directly when AI-HIL CAN tools are available,
- flash files outside configured artifact roots,
- bypass artifact validation,
- mass erase,
- loosen `.aihil/config.yaml` policy without explicit user instruction,
- vendor the AI-HIL source tree into a firmware project unless explicitly requested,
- mix plain serial text into MCP stdio.

Always:

- run from the firmware project directory for project setup,
- validate with `aihil doctor`,
- use project-local `.mcp.json`,
- probe before flashing,
- read structured results after hardware actions,
- preserve report and log paths in summaries,
- keep the engineer informed when a hardware/operator action is required.

## Windows notes

If `aihil doctor` reports that OpenOCD is not found, set `debugger.executable` in `.aihil/config.yaml` to the installed OpenOCD executable, for example:

```yaml
debugger:
  type: "openocd"
  executable: "C:/Program Files/OpenOCD/bin/openocd.exe"
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
```

For serial feedback, discover host ports with:

```bash
aihil com-ports
```

Then add only the intended device under `com_ports` and use its configured `port_id` in MCP COM tool calls.

## Development commands

For AI-HIL repository development:

```bash
npm install
npm test
npm install --global .
```

Useful CLI commands:

```bash
aihil init
aihil doctor
aihil com-ports
aihil mcp-stdio --config .aihil/config.yaml
aihil com-stdio --config .aihil/config.yaml --port dut_uart
```
