# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

Canonical agent instructions live in `AGENTS.md` and `AI_AGENT_QUICKSTART.md`. Human-facing setup and troubleshooting entry points live in `README.md`.

## Project Overview

AI-HIL is a TypeScript/Node.js MCP stdio server for safe embedded hardware-in-the-loop access. It exposes narrow tools for probing, flashing, resetting, configured COM port stimulus/feedback, configured CAN bus stimulus/feedback, and reading structured reports from a configured local target.

Use STM32 Nucleo-F446RE + ST-Link + OpenOCD + Node.js 16.16 or newer with npm as the supported first path unless project files or the user clearly identify another setup. Current Node.js LTS is recommended.

## Installation Model

Fast path:

1. If `aihil --version` works, do not reinstall. On Windows, also try `aihil.cmd --version`.
2. If AI-HIL is missing, first try installing the `aihil` command once on the local machine from npm:

```bash
npm i -g aihil
```

3. If that npm registry install fails after Node.js/npm are available and accepted by this repository, install directly from GitHub without keeping a checkout in the firmware project:

```bash
npm i -g github:hp-8472/aihil
```

Installing from npm or GitHub installs only the `aihil` CLI/MCP server. It does not install, register, or update agent skills. When an AI agent is asked to install AI-HIL, the agent must install the skills separately as an agent setup step.

If Node.js is missing or too old for AI-HIL's `engines`, do not refuse or stop at the error. Install or activate a supported Node.js/npm runtime, then rerun the AI-HIL install. Current Node.js LTS is fine, but do not pin a specific Node.js patch version unless the operator asks for that version; any runtime accepted by `package.json` is fine. Prefer an existing version manager such as `nvm`, `fnm`, Volta, or `asdf`, or the local platform package manager such as `winget install OpenJS.NodeJS.LTS` on Windows or `brew install node` on macOS. If no safe local installer is apparent, ask one concise question about how the operator wants Node.js installed.

Do not work around an old runtime with `--force`, `--ignore-engines`, or an older AI-HIL version. The correct fix is a supported Node.js/npm runtime.

## Agent Skill Installation

Agent-driven AI-HIL installation includes installing `skills/aihil-config-setup/SKILL.md` into the active agent's user-level skill directory after the `aihil` command is installed.

If this source checkout is not available, clone or fetch the AI-HIL repository outside the firmware project only for the skill source, then remove that temporary checkout if it is no longer needed.

Known user-level skill destinations:

- opencode: `$HOME/.config/opencode/skills/aihil-config-setup/SKILL.md`.
- Claude Code: `$HOME/.claude/skills/aihil-config-setup/SKILL.md`.

For other skill-capable agents, use that agent's documented user-level skill directory. If the active agent has no skill mechanism or the destination cannot be determined, ask one concise question instead of silently skipping skill installation.

Do not rely on npm for skills, and do not add npm `postinstall` hooks for skill installation. Skill installation is an agent workflow responsibility, not package-manager behavior.

From this repository checkout for AI-HIL development, install dependencies first and then link the checkout globally:

```bash
npm install
npm install --global .
```

For local AI-HIL development and tests, use the Node.js toolchain:

```bash
npm install
npm test
```

Each firmware project should contain its own `.aihil/` directory with `.aihil/config.yaml` for that project's target, debugger, named COM ports, named CAN buses, permissions, reports, logs, and artifact roots.

## First Steps Per Project

From the firmware project directory:

```bash
aihil init
aihil doctor
```

Use the standard portable `.mcp.json` launch entry when MCP project discovery is needed. It starts:

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

Use the AI-HIL MCP tools for hardware actions. Do not use raw OpenOCD commands or shell commands for probe, flash, reset, COM port, or CAN adapter workflows when the MCP server is available.

Follow this sequence for hardware validation:

1. Build firmware.
2. Call `aihil_probe_target`.
3. Call `aihil_flash_firmware` with a validated artifact path, usually `build/firmware.elf`, or upload first with `aihil_artifact_upload` using `image_path` and flash the returned `artifact_id`.
4. For serial stimuli or feedback, use only configured port ids with `aihil_com_session_start`, `aihil_com_write`, `aihil_com_read`, and `aihil_com_session_stop`.
5. For CAN stimuli or feedback, use only configured bus ids with `aihil_can_session_start`, `aihil_can_send`, `aihil_can_read`, and `aihil_can_session_stop`.
6. Read the returned JSON result.
7. Call `aihil_get_last_report`.
8. Call `aihil_classify_last_error` after failed actions.

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
src/aihil/can.ts          Configured CAN bus sessions and adapters
src/aihil/debuggers/      Debugger backends
tests-ts/                 Node-based TypeScript migration tests
```
