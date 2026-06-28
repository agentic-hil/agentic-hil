# AI-HIL [![Node CI](https://github.com/hp-8472/aihil/actions/workflows/ci.yml/badge.svg)](https://github.com/hp-8472/aihil/actions/workflows/ci.yml)

**Give your AI coding agent safe hands-on access to real embedded hardware.**

AI-HIL connects MCP-capable coding agents such as Claude Code, Codex, opencode, or similar tools to a local embedded hardware-in-the-loop setup.

For embedded engineers, the value is simple: **less manual hardware handling, fewer context switches, and faster feedback from the real target board.** Instead of repeatedly flashing firmware, resetting the board, opening serial tools, copying logs, and explaining the result back to the agent, AI-HIL gives the agent a safe, repeatable way to run that loop itself.

```text
change firmware -> build -> probe target -> flash -> reset -> read report/serial feedback -> improve -> repeat
```

AI-HIL is not an SDK and not a generic remote shell. It is a **local, project-scoped hardware bridge** that exposes only configured, high-level hardware actions to the agent.

> Current reference path: **STM32 Nucleo-F446RE + ST-Link + OpenOCD + Node.js/npm**. Other OpenOCD-supported targets may work through project configuration, but the Nucleo path is the documented baseline.

## Why embedded engineers use it

Embedded development is slow whenever the feedback loop leaves the coding environment.

Without AI-HIL, the engineer often has to:

1. wait for the agent to edit firmware,
2. build manually,
3. flash manually,
4. reset the board,
5. open a serial monitor,
6. copy the result back into the chat,
7. ask for the next fix,
8. repeat the same procedure again.

With AI-HIL, the agent can use bounded tools to probe, flash, reset, read structured reports, and optionally read/write configured serial ports. The engineer stays in control of the hardware policy, while the agent gets the feedback it needs to make the next code change.

That means AI-HIL is designed to save the time normally lost to repetitive hardware-operation steps and to make AI-assisted embedded development productive on **real boards**, not only in a simulator or editor.

## Start here: ask your agent to install it

The recommended setup path is agent-first: open your firmware project in your MCP-capable coding agent and paste this prompt.

```text
Install https://github.com/hp-8472/aihil for this firmware project and use it as the local MCP hardware-in-the-loop bridge.

Do not vendor the AI-HIL source tree into this firmware repository. Install the aihil command, return to this firmware project, create a project-local .aihil/config.yaml with aihil init, validate it with aihil doctor, generate .mcp.json, and then use AI-HIL for probe, flash, reset, reports, and configured serial feedback.
```

The agent should install the `aihil` command once on the machine, then configure the current firmware project with its own `.aihil/config.yaml` and `.mcp.json`.

## Manual setup fallback

If you prefer to set it up yourself, run this from your firmware project directory:

```bash
npm i -g aihil
aihil init
aihil doctor
aihil mcp-config > .mcp.json
```

Each firmware project owns its own `.aihil/` directory. That directory contains the local target configuration, debugger settings, permissions, allowed firmware artifact roots, reports, logs, and optional named COM ports.

If you are developing AI-HIL from this repository checkout instead of using the npm package:

```bash
git clone https://github.com/hp-8472/aihil.git
cd aihil
npm install
npm install --global .
```

Then return to your firmware project and run:

```bash
aihil init
aihil doctor
aihil mcp-config > .mcp.json
```

## First real hardware loop

Once `.aihil/config.yaml` and `.mcp.json` exist in your firmware project, open your agent in that project and ask:

```text
Use AI-HIL to build the firmware, probe the target, flash the firmware artifact from the configured build output directory, reset the target in run mode, read the last report, and read the configured COM port if one is available. Use the hardware feedback for the next firmware fix.
```

The expected loop is:

```text
1. build firmware
2. aihil_probe_target
3. aihil_flash_firmware
4. aihil_reset_target
5. aihil_get_last_report
6. optional COM session/read/write through configured port_id values
7. use the result for the next code change
```

Healthy signals include:

- `aihil doctor` returns `ok: true`.
- `aihil_probe_target` returns `ok: true` and `target_detected: true`.
- `aihil_flash_firmware` returns `ok: true`, `verify: true`, and `reset_after_flash: true`.
- Every hardware action writes structured reports and raw logs under `.aihil/`.

## Try the supported Nucleo demo

The repository contains a first-run demo for the STM32 Nucleo-F446RE.

```bash
npm i -g aihil
git clone https://github.com/hp-8472/aihil.git
cd aihil/examples/nucleo-f446re_demo
aihil init
aihil doctor
aihil mcp-config > .mcp.json
```

Build the demo firmware locally before flashing:

```bash
cmake --preset Debug
cmake --build --preset Debug
```

The flashable ELF is then available at:

```text
build/Debug/nucleo-f446re_demo.elf
```

With a NUCLEO-F446RE connected over USB/ST-LINK, ask your agent:

```text
Use AI-HIL to probe the target, flash build/Debug/nucleo-f446re_demo.elf, reset it in run mode, read the last report, and read the configured COM port if one is available.
```

If OpenOCD is not on `PATH`, set `debugger.executable` in `.aihil/config.yaml` to your local OpenOCD executable. On Windows this may look like:

```yaml
debugger:
  type: "openocd"
  executable: "C:/Program Files/OpenOCD/bin/openocd.exe"
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
```

To discover serial ports for optional UART feedback:

```bash
aihil com-ports
```

Only add COM ports that are intentionally part of the test setup.

## What AI-HIL provides

AI-HIL gives agents a narrow hardware control surface instead of raw host access:

| Capability | What it gives the engineer |
| --- | --- |
| Target probing | The agent can check whether the board is reachable before flashing. |
| Firmware flashing | The agent can flash only validated artifacts from allowed project roots. |
| Reset control | The agent can reset the target in configured modes. |
| Structured reports | Every run produces machine-readable JSON for diagnosis and repeatability. |
| Raw logs | OpenOCD and hardware-action logs remain available for human inspection. |
| Configured COM access | Serial feedback and stimuli can flow through named, approved port IDs. |
| Local policy | The project-local `.aihil/config.yaml` defines what is allowed. |

## Safety model

AI-HIL is designed around a simple rule: **the agent can only do what the project-local configuration allows.**

By default:

- hardware actions require explicit permissions,
- firmware files must be under configured allowed roots,
- firmware extensions are restricted, usually to `.elf`, `.hex`, and `.bin`,
- raw debugger commands are not exposed,
- mass erase is disabled,
- COM access is limited to named `com_ports` entries,
- every hardware action returns structured JSON and writes logs for review.

This is what makes the loop useful for AI agents without turning your development machine into an unrestricted hardware-control shell.

## Supported first path

The official reference setup is deliberately narrow so that the first user experience is reproducible:

- Board: STM32 Nucleo-F446RE
- Debug probe: ST-Link, including the onboard Nucleo ST-Link
- Debug backend: OpenOCD
- Host runtime: Node.js with npm
- OpenOCD interface config: `interface/stlink.cfg`
- OpenOCD target config: `target/stm32f4x.cfg`
- Firmware artifact root: `build/`
- Firmware artifact formats: `.elf`, `.hex`, `.bin`

Other OpenOCD-supported boards, probes, and targets may work when represented in `.aihil/config.yaml`, but the supported first path is the baseline for documentation, examples, and issue reproduction.

## How it works

```text
AI coding agent
  -> builds or receives a firmware artifact
  -> calls AI-HIL MCP tools
  -> AI-HIL checks .aihil/config.yaml policy
  -> OpenOCD / ST-Link / configured COM ports touch the board
  -> AI-HIL writes structured reports and logs
  -> agent uses real hardware feedback for the next firmware change
```

AI-HIL uses MCP over stdio internally. Most users should not need to hand-edit MCP details. Generate the project-local MCP config with:

```bash
aihil mcp-config > .mcp.json
```

Agent-facing MCP behavior, tool rules, and safety instructions live in [`AGENTS.md`](AGENTS.md). Human troubleshooting lives in [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).

## Project configuration

Create the starter config with:

```bash
aihil init
```

The default `.aihil/config.yaml` is intentionally local project state. Edit only the values that describe your board, debugger, artifact roots, permissions, and approved COM ports.

For the supported Nucleo path, the important values are:

```yaml
target:
  name: "nucleo-f446re"
  controller: "stm32f446re"

debugger:
  type: "openocd"
  executable: null
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
  allow_com_read: true
  allow_com_write: true
  allow_raw_debugger_commands: false
  allow_mass_erase: false
```

Add `com_ports` only for serial devices that are intentionally part of the project workflow.

## Troubleshooting

Start with:

```bash
aihil doctor
```

The most useful fields are:

```text
ok
error_type
backend_error_type
summary
likely_causes
report_path
log_path
```

Common setup issues are documented in [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md), including missing OpenOCD, wrong target configuration, target not detected, permission errors, artifact validation failures, and COM-port setup.

## Repository layout

```text
.
|-- AGENTS.md                         # agent-facing MCP and workflow rules
|-- AI_AGENT_QUICKSTART.md            # compact agent setup notes
|-- README.md                         # human-facing project overview
|-- TROUBLESHOOTING.md                # operator diagnostics
|-- examples/nucleo-f446re_demo/      # supported first hardware path
|-- skills/aihil-config-setup/        # agent setup skill
|-- src/aihil/                        # AI-HIL CLI, config, MCP, reports, tools
|-- tests-ts/                         # TypeScript test suite
`-- package.json
```

## Development

For AI-HIL development:

```bash
npm install
npm test
```

The npm package installs the `aihil` CLI. The CLI provides commands such as:

```text
aihil init
aihil doctor
aihil com-ports
aihil mcp-config
aihil mcp-stdio
aihil com-stdio
```

## License

Copyright 2026 Hannes Pauli.

Licensed under the Apache License 2.0. See [`LICENSE`](LICENSE).
