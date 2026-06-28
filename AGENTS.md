# AI-HIL Agent Instructions

AI-HIL is a local MCP stdio server that gives AI agents safe, structured access to an embedded hardware-in-the-loop setup.

Use the MCP server from this repository for hardware actions. Do not use raw OpenOCD commands or arbitrary shell commands for flashing, probing, or resetting hardware when an AI-HIL MCP tool is available.

This file is for AI agents. Humans should start with `README.md` and `TROUBLESHOOTING.md`; agents should use this file and `AI_AGENT_QUICKSTART.md`. When working from this source checkout, the optional repository-local skill lives at `skills/aihil-config-setup/SKILL.md`.

## Supported First Path

Use this as the reference setup unless project files or the user clearly specify another target:

- Board: STM32 Nucleo-F446RE.
- Debug probe: ST-Link, including the onboard Nucleo ST-Link.
- Debug backend: OpenOCD.
- Host runtime: Node.js 16.16 or newer with npm; current Node.js LTS is recommended.
- OpenOCD interface config: `interface/stlink.cfg`.
- OpenOCD target config: `target/stm32f4x.cfg`.
- Firmware artifact root: `build/`.
- Firmware artifact formats: `.elf`, `.hex`, or `.bin`.

Other boards may work, but do not guess target, debugger, COM port, or artifact paths. If they cannot be inferred from project files, ask one concise question.

## Installation Model

Fast path for agents:

1. If `aihil --version` succeeds, do not reinstall. On Windows, also try `aihil.cmd --version` before deciding AI-HIL is missing.
2. If AI-HIL is missing, first try installing the `aihil` command once on the local machine from npm:

```bash
npm i -g aihil
```

3. If that npm registry install fails after Node.js/npm are available and accepted by this repository, install directly from GitHub without keeping a checkout in the firmware project:

```bash
npm i -g github:hp-8472/aihil
```

Installing from npm or GitHub installs only the `aihil` CLI/MCP server. It does not install, register, or update agent skills.

If Node.js is missing or too old for AI-HIL's `engines`, do not refuse or stop at the error. Treat it as a host setup prerequisite: install or activate a supported Node.js/npm runtime, then rerun the AI-HIL install. Current Node.js LTS is fine, but do not pin a specific Node.js patch version unless the operator asks for that version; any runtime accepted by `package.json` is fine. Prefer an existing version manager such as `nvm`, `fnm`, Volta, or `asdf`, or the local platform package manager such as `winget install OpenJS.NodeJS.LTS` on Windows or `brew install node` on macOS. If no safe local installer is apparent, ask one concise question about how the operator wants Node.js installed.

Do not work around an old runtime with `--force`, `--ignore-engines`, or an older AI-HIL version. The correct fix is a supported Node.js/npm runtime.

From this repository checkout for AI-HIL development, install dependencies first and then link the checkout globally:

```bash
npm install
npm install --global .
```

For local AI-HIL development and tests, use:

```bash
npm install
npm test
```

Each firmware project should have its own `.aihil/` directory with `.aihil/config.yaml` for that project's target, debugger, permissions, reports, logs, and artifact roots.

`skills/aihil-config-setup/SKILL.md` is a repository-local agent asset, not an npm-installed file. Use it only when this repository checkout is available. Do not copy it into an agent's global skill directory unless the user explicitly asks for that.

If a user says "Install this AI-HIL repo and set it up for this project", install the `aihil` command from the AI-HIL repo, then return to the firmware project and follow this file and `AI_AGENT_QUICKSTART.md`. Do not expect a skill to be installed, and do not copy the AI-HIL source tree into the firmware project unless the user explicitly asks.

## Project Bootstrap

From the firmware project directory, create and inspect the project-local setup with:

```bash
aihil init
aihil doctor
aihil mcp-config > .mcp.json
```

The generated MCP config starts the installed Node entrypoint directly. That is equivalent to:

```text
aihil mcp-stdio --config .aihil/config.yaml
```

`mcp-stdio` is project-scoped. Do not add `--port` to it; MCP COM tool calls provide `port_id` arguments when needed.

For a separate plain-text serial data plane, use:

```text
aihil com-stdio --config .aihil/config.yaml --port <configured_port_id>
```

`com-stdio` is port-scoped and always requires `--port`.

Do not mix these streams. `mcp-stdio` stdout is JSON-RPC only; `com-stdio` stdout is decoded COM text only.

Each project can include `.mcp.json` for MCP clients that discover project-level MCP configuration.

Create project-level MCP discovery config with:

```bash
aihil mcp-config > .mcp.json
```

## Required Workflow

1. Build the firmware first.
2. Check debugger availability with `aihil_debugger_info` if setup is unclear.
3. Probe the target with `aihil_probe_target` before flashing.
4. Flash only validated artifacts with `aihil_flash_firmware`.
5. Use `aihil_reset_target` only with mode `run`, `halt`, or `init`.
6. Use configured COM port ids with `aihil_com_session_start`, `aihil_com_write`, `aihil_com_read`, and `aihil_com_session_stop` for serial stimuli and feedback.
7. Read `aihil_get_last_report` after hardware actions.
8. Use `aihil_classify_last_error` after failures.
9. Stop on `permission_denied`; the local AI-HIL configuration is authoritative.

Expected healthy signals are `aihil doctor` with `ok: true`, `aihil_probe_target` with `ok: true` and `target_detected: true`, and `aihil_flash_firmware` with `ok: true`, `verify: true`, and `reset_after_flash: true`. The README contains full expected-output JSON examples.

## Available MCP Tools

```text
aihil_debugger_info
aihil_probe_target
aihil_artifact_upload
aihil_flash_firmware
aihil_reset_target
aihil_get_last_report
aihil_classify_last_error
aihil_com_ports_list
aihil_com_session_start
aihil_com_write
aihil_com_read
aihil_com_session_stop
```

## Safety Rules

Never request or run raw OpenOCD/debugger commands for hardware actions.

Never use arbitrary shell COM-port tools when an AI-HIL COM-port MCP tool is available.

Do not open host COM devices directly. Use AI-HIL's configured COM MCP tools with named `port_id` values.

Use `aihil com-stdio` only when the user explicitly wants a continuous text serial channel. The `--port` value must be a configured `com_ports` id.

Never flash files outside configured artifact roots.

Never mass erase.

Treat structured JSON results as the source of truth. Always inspect `ok`, `error_type`, `backend_error_type`, `summary`, `likely_causes`, `report_path`, and `log_path` before deciding what to do next.

## Development Commands

```bash
npm install
npm test
aihil init --force
aihil doctor
aihil mcp-config
aihil mcp-stdio --config .aihil/config.yaml
aihil com-stdio --config .aihil/config.yaml --port dut_uart
```
