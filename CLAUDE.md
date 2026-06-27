# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

Canonical agent instructions live in `AGENTS.md` and `AI_AGENT_QUICKSTART.md`. Human-facing setup and expected-output examples live in `README.md`.

## Project Overview

AI-HIL is a TypeScript/Node.js MCP stdio server for safe embedded hardware-in-the-loop access. It exposes narrow tools for probing, flashing, resetting, configured COM port stimulus/feedback, and reading structured reports from a configured local target.

Use STM32 Nucleo-F446RE + ST-Link + OpenOCD + Node.js >=22.14 <25 as the supported first path unless project files or the user clearly identify another setup.

## Installation Model

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

Each firmware project should contain its own `.aihil/` directory with `.aihil/config.yaml` for that project's target, debugger, named COM ports, permissions, reports, logs, and artifact roots.

## First Steps Per Project

From the firmware project directory:

```bash
aihil init
aihil doctor
aihil mcp-config > .mcp.json
```

The MCP client starts AI-HIL with stdio using:

```text
aihil mcp-stdio --config .aihil/config.yaml
```

`mcp-stdio` is project-scoped and does not take `--port`; COM MCP tool calls pass configured `port_id` values.

Project MCP discovery config:

```text
.mcp.json
```

There is no background network service. The MCP client owns the `aihil mcp-stdio` process lifecycle.

For a separate plain text serial channel, use `aihil com-stdio --config .aihil/config.yaml --port <configured_port_id>`. `com-stdio` is port-scoped and always requires `--port`. Do not mix this with MCP stdio; MCP stdout is JSON-RPC only and COM stdio stdout is decoded serial text only.

## Agent Rules

Use the AI-HIL MCP tools for hardware actions. Do not use raw OpenOCD commands or shell commands for probe, flash, reset, or COM port workflows when the MCP server is available.

Follow this sequence for hardware validation:

1. Build firmware.
2. Call `aihil_probe_target`.
3. Call `aihil_flash_firmware` with a validated artifact path, usually `build/firmware.elf`, or upload first with `aihil_artifact_upload` using `image_path` and flash the returned `artifact_id`.
4. For serial stimuli or feedback, use only configured port ids with `aihil_com_session_start`, `aihil_com_write`, `aihil_com_read`, and `aihil_com_session_stop`.
5. Read the returned JSON result.
6. Call `aihil_get_last_report`.
7. Call `aihil_classify_last_error` after failed actions.

Stop on `permission_denied` and report the local policy restriction.

## Tests

```bash
npm test
```

## Important Files

```text
src/aihil/stdio.ts        MCP stdio transport loop
src/aihil/comstdio.ts     Plain text COM stdio bridge
src/aihil/mcp.ts          MCP JSON-RPC implementation
src/aihil/tools.ts        Shared tool service used by MCP
src/aihil/config.ts       .aihil/config.yaml parsing and policy
src/aihil/artifacts.ts    Firmware artifact validation
src/aihil/comports.ts     Configured COM port streaming sessions
src/aihil/debuggers/      Debugger backends
tests-ts/                 Node-based TypeScript migration tests
```
