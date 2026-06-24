# AI-HIL

**AI-HIL makes it possible for AI agents to work on real embedded hardware.**

AI can already write firmware. The hard part is the embedded feedback loop: build, flash, reset, observe, diagnose, and improve based on what actually happens on a real board.

AI-HIL is a local bridge between an AI agent and an embedded development setup. It does not replace existing tools. It makes tools like OpenOCD usable by AI agents in a controlled, structured, and safe way.

```text
AI agent
  ↓ MCP
AI-HIL
  ↓ configuration + policy
OpenOCD / debug probe / programmer / logs / tests
  ↓
real embedded target
  ↓ structured feedback
AI agent
```

## Quick Start For AI Agents

From a fresh clone:

```bash
python -m pip install -e .
aihil init
aihil doctor
aihil serve --config .aihil/config.yaml
```

AI-HIL includes project-level MCP discovery config in `.mcp.json`:

```json
{
  "mcpServers": {
    "aihil": {
      "type": "http",
      "url": "http://127.0.0.1:8732/mcp"
    }
  }
}
```

Agents should use MCP `tools/list` and `tools/call` against `http://127.0.0.1:8732/mcp`. Do not use raw OpenOCD commands for hardware actions.

## Why this exists

AI-assisted software development works best when the agent can run code and see the result. Embedded development is different: the meaningful result often only exists on real hardware.

Without a hardware bridge, an AI agent can edit firmware but cannot reliably answer questions like:

```text
Did the target flash successfully?
Did the board boot?
What did the UART log say?
Did the firmware crash?
Did the output pin change?
Did the hardware behave differently after the patch?
```

AI-HIL tries to close that gap.

> **AI writes firmware. AI-HIL helps the AI run it on real hardware and understand the result.**

## What this repository is

This repository starts as a free, practical infrastructure project for AI-assisted embedded development.

The first concrete bridge is an **OpenOCD MCP bridge**. It should allow an AI agent to perform a small number of hardware actions safely:

```text
probe target
flash configured firmware
reset target
return structured result
store raw logs
classify common OpenOCD errors
```

The idea is simple:

```text
The AI should not get arbitrary shell access.
The AI should get a small set of safe hardware tools.
```

## What this repository is not

AI-HIL is intentionally not framed as a product here.

It is also not:

```text
a customer SDK
a library customers need to import
a generic OpenOCD shell
a replacement for OpenOCD, J-Link, ST-Link, probe-rs, or vendor tools
a cloud service
a complete HIL system
a pricing or sales story
```

The purpose of this repository is to explore and build the missing bridge between AI agents and real embedded hardware.

## Core idea

AI-HIL is the controlled gate between an AI agent and a local embedded hardware setup.

```text
AI agent
  ↓
AI-HIL MCP interface
  ↓
AI-HIL configuration + policy layer
  ↓
OpenOCD / build tools / UART logs / hardware actions
  ↓
real target board
```

The agent does not receive unrestricted command execution. It receives explicit tools with narrow responsibilities.

Examples:

```text
aihil_debugger_info
aihil_probe_target
aihil_flash_firmware
aihil_reset_target
aihil_get_last_report
aihil_classify_last_error
```

## MCP and skills

AI-HIL will likely need both an MCP interface and optional skills, but they have different jobs.

```text
MCP server: performs hardware actions
Skill: explains the workflow to the AI agent
```

For AI-HIL:

```text
MCP = the gate to the hardware
Skill = guidance for the agent
Configuration = the permission boundary
Reports = feedback the agent can reason about
```

A skill does not flash hardware by itself. A skill can teach an agent how to use the available tools properly.

For example, a future AI-HIL skill could tell an agent:

```text
1. Build the firmware first.
2. Probe the target before flashing.
3. Flash only the configured image.
4. Reset after flashing.
5. Read the report before changing code again.
6. Do not request raw OpenOCD commands.
```

The actual hardware access remains behind the MCP server.

## First bridge: OpenOCD

OpenOCD is a good first bridge because many embedded developers already use it with ST-Link, CMSIS-DAP, J-Link, FTDI, and other debug probes.

The first AI-HIL bridge should wrap OpenOCD in safe, high-level operations.

Instead of exposing this to the agent:

```text
run arbitrary openocd command
```

AI-HIL exposes this:

```text
probe the configured target
flash the configured image
reset the configured target
return a structured report
```

## Example `.aihil/config.yaml`

`.aihil/config.yaml` describes the local hardware setup and what the AI is allowed to do. Create a starter file with `aihil init`.

```yaml
server:
  listen: "127.0.0.1:8732"

target:
  name: fan-controller-v1
  controller: stm32f4

debugger:
  type: openocd
  executable: openocd
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
  timeout_s: 60

artifacts:
  allowed_roots:
    - "build"
  allowed_extensions:
    - ".elf"
    - ".hex"
    - ".bin"

permissions:
  allow_probe: true
  allow_flash: true
  allow_reset: true
  allow_raw_debugger_commands: false
  allow_mass_erase: false

reports:
  directory: ".aihil/reports"

logs:
  directory: ".aihil/logs"
```

The configuration file is not just convenience. It is the contract between the human developer, the local hardware setup, and the AI agent.

## Intended MCP tools

### `aihil_debugger_info`

Returns information about the configured debugger backend, including the detected OpenOCD version when available.

### `aihil_probe_target`

Checks whether the configured target can be reached through OpenOCD.

Example result:

```json
{
  "ok": true,
  "tool": "aihil_probe_target",
  "target_detected": true,
  "elapsed_ms": 1834,
  "summary": "Target detected through OpenOCD."
}
```

### `aihil_flash_firmware`

Flashes a validated firmware image.

The image path must be under a configured allowed artifact root. Raw OpenOCD commands are not exposed to the AI agent.

Example result:

```json
{
  "ok": true,
  "tool": "aihil_flash_firmware",
  "artifact": {
    "source": "path",
    "path": "build/firmware.elf",
    "sha256": "..."
  },
  "verify": true,
  "reset_after_flash": true,
  "elapsed_ms": 4217,
  "summary": "Firmware flashed, verified, and target reset.",
  "report_path": ".aihil/reports/last-report.json",
  "log_path": ".aihil/logs/openocd-20260624T164357926542Z-aihil_flash_firmware.log"
}
```

### `aihil_reset_target`

Resets the configured target through OpenOCD.

Possible reset modes:

```text
run
halt
init
```

### `aihil_get_last_report`

Returns the most recent structured AI-HIL report.

### `aihil_classify_last_error`

Classifies the most recent OpenOCD failure into a useful category.

Initial error classes:

```text
target_not_detected
adapter_not_found
openocd_not_found
config_file_not_found
firmware_image_not_found
flash_failed
verify_failed
timeout
permission_denied
unknown_openocd_error
```

## Safety principles

AI-HIL is meant to give AI agents access to real hardware, so the defaults must be conservative.

### No raw OpenOCD access by default

The agent should not be able to execute arbitrary OpenOCD commands.

```text
not exposed: openocd_command("...")
exposed:     aihil_probe_target
exposed:     aihil_flash_firmware
exposed:     aihil_reset_target
```

### Configuration is the permission boundary

The `.aihil/config.yaml` file defines what the agent is allowed to do.

```yaml
permissions:
  allow_flash: true
  allow_reset: true
  allow_mass_erase: false
  allow_raw_debugger_commands: false
```

### Firmware paths are restricted

The agent should only flash firmware images from explicitly allowed directories.

```yaml
artifacts:
  allowed_roots:
    - "build"
```

### Every hardware action returns a report

Every probe, flash, reset, or failure should create a structured report that the agent can reason about.

### Raw logs remain available

Structured reports are for AI agents. Raw OpenOCD logs are for humans.

## Implementation direction

The first implementation can keep OpenOCD simple by running it as a controlled external process.

Example internal command shape:

```bash
openocd \
  -f interface/stlink.cfg \
  -f target/stm32f4x.cfg \
  -c "program build/firmware.elf verify reset exit"
```

This avoids building a persistent debug session too early.

A later implementation can add a persistent OpenOCD session through the Tcl interface.

No implementation language is fixed in this README. AI-HIL is a host-side bridge, not firmware running on the target. The implementation language should be chosen for reliable local tooling, MCP support, process handling, configuration parsing, and packaging.

## Repository layout

```text
.
├── .mcp.json
├── AGENTS.md
├── AI_AGENT_QUICKSTART.md
├── CLAUDE.md
├── README.md
├── LICENSE
├── scripts/
│   ├── start-aihil-mcp.ps1
│   └── start-aihil-mcp.sh
├── src/
│   └── aihil/
└── tests/
```

## Local usage shape

The main interface for AI agents is MCP over local HTTP.

Run AI-HIL with the configured local listen address:

```bash
aihil serve --config .aihil/config.yaml
```

By default, the server listens on:

```text
127.0.0.1:8732
```

The MCP endpoint is:

```text
POST http://127.0.0.1:8732/mcp
```

Tool execution is exposed through MCP at `/mcp`. `/health` is available for readiness checks.

Keep `server.listen` bound to `127.0.0.1` unless the deployment has explicit authentication, transport security, and operator approval controls.

## MCP client configuration

A local MCP client can connect to AI-HIL as an HTTP MCP server.

Example shape:

```json
{
  "mcpServers": {
    "aihil": {
      "type": "http",
      "url": "http://127.0.0.1:8732/mcp"
    }
  }
}
```

Exact configuration depends on the MCP client.

## Example agent loop

```text
Task: Fix the firmware so the target boots correctly.

AI agent:
1. Inspects the firmware code.
2. Changes the code.
3. Builds the firmware.
4. Calls aihil_probe_target.
5. Calls aihil_flash_firmware.
6. Calls aihil_reset_target.
7. Reads the AI-HIL report.
8. Uses the hardware result to continue debugging.
```

This is the basic loop AI-HIL tries to enable:

```text
change firmware
→ run on real hardware
→ observe result
→ improve firmware
```

## Design rules

### Keep the agent interface small

Expose a few safe, high-level tools. Do not expose all OpenOCD features.

### Return deterministic output

The agent should receive predictable JSON results, not only raw terminal text.

### Fail usefully

When something fails, return a useful diagnosis.

```json
{
  "ok": false,
  "error_type": "target_not_detected",
  "summary": "OpenOCD could not detect the target.",
  "likely_causes": [
    "DUT is not powered",
    "wrong interface configuration",
    "SWD/JTAG wiring issue",
    "debug probe already in use"
  ]
}
```

### Do not hide raw logs

Reports are for agents. Logs are for developers.

### Do not become a generic shell

The value of AI-HIL is controlled hardware access, not arbitrary command execution.

## Possible next bridges

OpenOCD is only the first bridge.

Other useful bridges could be:

```text
UART log bridge
build-system bridge
hardware smoke-test bridge
measurement bridge
stimulus bridge
power-control bridge
```

These should follow the same rule:

```text
small safe tools
clear permissions
structured feedback
human-readable logs
```

## License

Copyright 2026 Hannes Pauli.

This project is licensed under the Apache License 2.0. See [LICENSE](LICENSE).
