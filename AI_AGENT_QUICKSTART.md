# AI Agent Quickstart

Use AI-HIL as the local MCP server for embedded hardware actions.

This file is for agents. Humans should start with `README.md` and use `TROUBLESHOOTING.md` for operator-facing diagnostics.

## Reference Setup

Prefer the supported first path unless the firmware project or user clearly says otherwise:

- STM32 Nucleo-F446RE.
- ST-Link.
- OpenOCD.
- Node.js 22.14 or newer LTS with npm.
- `interface/stlink.cfg`.
- `target/stm32f4x.cfg`.
- Firmware artifacts under `build/`.

If the board, debugger, COM port, or artifact path cannot be inferred, ask one concise question instead of guessing.

## Install Once

Install the `aihil` command once on the local machine from npm with:

```bash
npm i -g aihil
```

From this repository checkout, install with:

```bash
npm install --global .
```

For local AI-HIL development and tests, use the Node.js toolchain:

```bash
npm install
npm test
```

If you were given only the AI-HIL repository URL and asked to set up the current firmware project, install AI-HIL with `npm i -g aihil`, then return to the firmware project. Do not vendor the AI-HIL source tree into the firmware project.

## Configure Each Project

In every firmware project that should use AI-HIL, create a project-local `.aihil/config.yaml`:

```bash
aihil init
```

Edit `.aihil/config.yaml` for the local board, OpenOCD interface, target config, allowed firmware artifact roots, and any named COM ports.

Agents should follow `skills/aihil-config-setup/SKILL.md` for the exact setup workflow: use `aihil init`, edit only project-specific fields, keep safety policy restrictive, then validate with `aihil doctor`.

Keep `.aihil/` with the project because it defines that project's hardware policy, reports, logs, and allowed artifact locations. Do not reinstall the MCP server inside every project.

## Check Setup

```bash
aihil doctor
```

Expected healthy result: `ok: true`, `tool: "aihil_doctor"`, `summary: "AI-HIL configuration loaded and debugger checked."`, and a nested debugger result with `ok: true`.

## Configure MCP

AI-HIL uses MCP over stdio. The MCP client starts `aihil mcp-stdio` automatically from `.mcp.json`.

`mcp-stdio` does not take `--port`; it is project-scoped. COM MCP tool calls pass `port_id` as tool arguments.

Project-level MCP client discovery config belongs in:

```text
.mcp.json
```

Generate it with:

```bash
aihil mcp-config > .mcp.json
```

Use the configured COM MCP tools for serial stimuli and feedback. Do not open host COM devices directly.

If the user explicitly wants a continuous plain text serial channel instead of MCP tool calls, start a separate process:

```bash
aihil com-stdio --config .aihil/config.yaml --port dut_uart
```

Do not mix plain COM text into `aihil mcp-stdio`; MCP stdio must remain JSON-RPC only. `com-stdio` is the command that requires `--port` because it binds one text stream to one configured COM port.

## Use The Tools

Use `tools/list` to discover available MCP tools, then follow this loop:

1. Build firmware.
2. Probe with `aihil_probe_target`.
3. Flash with `aihil_flash_firmware` using `image_path`, usually `build/firmware.elf`, or first call `aihil_artifact_upload` with `image_path` and flash the returned `artifact_id`.
4. For serial feedback, start `aihil_com_session_start`, send stimuli with `aihil_com_write`, read feedback with `aihil_com_read`, then stop with `aihil_com_session_stop`.
5. Read the tool result and `aihil_get_last_report`.
6. Diagnose failures with `aihil_classify_last_error`.

Do not use raw OpenOCD commands or arbitrary COM port shell tools when an AI-HIL MCP tool is available.

Healthy probe and flash signals are `target_detected: true`, `success_confirmed: true`, `verify: true`, `reset_after_flash: true`, plus `report_path` and `log_path` for auditability.
