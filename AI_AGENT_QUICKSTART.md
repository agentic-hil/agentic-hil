# AI Agent Quickstart

Use AI-HIL as the local MCP server for embedded hardware actions.

This file is for agents. Humans should start with `README.md` and use `TROUBLESHOOTING.md` for operator-facing diagnostics.

## Reference Setup

Prefer the supported first path unless the firmware project or user clearly says otherwise:

- STM32 Nucleo-F446RE.
- ST-Link.
- OpenOCD.
- Node.js 16.16 or newer with npm; current Node.js LTS is recommended.
- `interface/stlink.cfg`.
- `target/stm32f4x.cfg`.
- Firmware artifacts under `build/`.

If the board, debugger, COM port, or artifact path cannot be inferred, ask one concise question instead of guessing.

## Install Once

Fast path:

1. If `aihil --version` works, do not reinstall. On Windows, also try `aihil.cmd --version`.
2. If AI-HIL is missing, first try installing the `aihil` command once on the local machine from npm:

```bash
npm i -g aihil
```

3. If that npm registry install fails after Node.js/npm are available and accepted by this repository, install directly from GitHub:

```bash
npm i -g github:hp-8472/aihil
```

Installing from npm or GitHub installs only the `aihil` CLI/MCP server. It does not install, register, or update agent skills. When an AI agent is asked to install AI-HIL, the agent must install the skills separately as an agent setup step.

If the host has no Node.js or an old Node.js, keep going by installing or activating a supported Node.js/npm runtime, then rerun `npm i -g aihil`. Current Node.js LTS is fine, but the agent does not need to pin a specific Node.js patch version; any runtime accepted by `package.json` is fine. An outdated Node.js is a remediable setup prerequisite, not a reason to refuse the AI-HIL setup. Prefer an existing version manager such as `nvm`, `fnm`, Volta, or `asdf`, or the local platform package manager such as `winget install OpenJS.NodeJS.LTS` on Windows or `brew install node` on macOS. If the safe local install path is unclear, ask one concise question.

Do not use `--force`, `--ignore-engines`, or an older AI-HIL version to bypass the Node.js requirement.

## Install Agent Skill

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

If you were given only the AI-HIL repository URL and asked to set up the current firmware project, install AI-HIL with the fast path above, install the AI-HIL skill into the active agent's skill directory, then return to the firmware project. Do not vendor the AI-HIL source tree into the firmware project.

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

AI-HIL uses MCP over stdio. `.mcp.json` is only the MCP launch entry and should normally be the stable portable shape below.

`mcp-stdio` does not take `--port`; it is project-scoped. COM MCP tool calls pass `port_id` as tool arguments.

Project-level MCP client discovery config belongs in:

```text
.mcp.json
```

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

The same template is shipped with the package under `dist/templates/mcp.json`. If the MCP client cannot resolve `aihil` from `PATH`, edit `.mcp.json` for that machine instead of changing the project template.

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
