# AI Agent Quickstart

Use AI-HIL as the local MCP server for embedded firmware development and embedded hardware actions.

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

## Start AI-HIL

Fast path:

1. If `aihil --version` works, do not reinstall. On Windows, also try `aihil.cmd --version`.
2. If AI-HIL is missing but npm is available, use npm's package runner. This does not require admin rights or an `aihil` entry in `PATH`:

```bash
npm exec --yes --package aihil -- aihil --version
```

3. If that npm registry package lookup fails after Node.js/npm are available and accepted by this repository, try the GitHub package source as a package source only. Do not clone or keep a source checkout in the firmware project:

```bash
npm exec --yes --package github:hp-8472/aihil -- aihil --version
```

Running from npm or GitHub makes only the `aihil` CLI/MCP server available for that command. It does not install, register, or update agent skills. When an AI agent is asked to install AI-HIL, the agent must install the skills separately as an agent setup step.

If the host has no Node.js or an old Node.js, keep going by installing or activating a supported Node.js/npm runtime, then rerun the `npm exec` command. Current Node.js LTS is fine, but the agent does not need to pin a specific Node.js patch version; any runtime accepted by `package.json` is fine. An outdated Node.js is a remediable setup prerequisite, not a reason to refuse the AI-HIL setup. Prefer an existing version manager such as `nvm`, `fnm`, Volta, or `asdf`, or the local platform package manager such as `winget install OpenJS.NodeJS.LTS` on Windows or `brew install node` on macOS. If the safe local install path is unclear, ask one concise question.

Do not use `--force`, `--ignore-engines`, or an older AI-HIL version to bypass the Node.js requirement.

## Install Agent Skill

Agent-driven AI-HIL installation includes installing the matching `skills/aihil-config-setup/SKILL.md` into the active agent's user-level skill directory after the CLI is available through `aihil` or `npm exec`. The CLI package is authoritative: if the skill front matter version differs from `aihil --version`, update the skill from the CLI with `aihil skill-install --agent <agent>` or `npm exec --yes --package aihil -- aihil skill-install --agent <agent>`.

Do not clone the AI-HIL repository just to install the skill. The npm package includes the setup skill, and `skill-install` can copy it into the active agent's user-level skill directory.

Known user-level skill destinations:

- opencode: `$HOME/.config/opencode/skills/aihil-config-setup/SKILL.md`.
- Claude Code: `$HOME/.claude/skills/aihil-config-setup/SKILL.md`.
- Codex: `$HOME/.codex/skills/aihil-config-setup/SKILL.md`.

`skill-install` also performs the known registration step for the selected agent. opencode and Claude Code discover skills from their skill directories. Codex additionally gets a marked AI-HIL block in `$HOME/.codex/AGENTS.md` pointing at the installed skill.

CLI-supported agent names and aliases are `opencode`/`open-code`, `claude-code`/`claude`, and `codex`/`codex-cli`/`openai-codex`. For other skill-capable agents, use that agent's documented user-level skill directory with `aihil skill-install --agent <name> --target <path>` or `npm exec --yes --package aihil -- aihil skill-install --agent <name> --target <path>`. If the active agent has no skill mechanism or the destination cannot be determined, ask one concise question instead of silently skipping skill installation.

Do not rely on npm for skills, and do not add npm `postinstall` hooks for skill installation. Skill installation is an agent workflow responsibility, not package-manager behavior.

From this repository checkout for AI-HIL development only, install dependencies and run the local CLI directly:

```bash
npm install
npm run build
node dist/main.js --version
```

For local AI-HIL development and tests, use the Node.js toolchain:

```bash
npm install
npm test
```

If you were given only the AI-HIL repository URL and asked to set up the current firmware project, use AI-HIL with the fast path above, install the AI-HIL skill into the active agent's skill directory, then return to the firmware project. Do not clone, checkout, or vendor the AI-HIL source tree into the firmware project for normal setup.

## Configure Each Project

In every firmware project that should use AI-HIL, create a project-local `.aihil/config.yaml`:

```bash
npm exec --yes --package aihil -- aihil init
```

Edit `.aihil/config.yaml` for the local board, OpenOCD interface, target config, allowed firmware artifact roots, any named COM ports, and any named CAN buses.

Agents should follow `skills/aihil-config-setup/SKILL.md` for the exact setup workflow: use `aihil init` or `npm exec --yes --package aihil -- aihil init`, edit only project-specific fields, keep safety policy restrictive, then validate with `aihil doctor` or `npm exec --yes --package aihil -- aihil doctor`.

Keep `.aihil/` with the project because it defines that project's hardware policy, reports, logs, and allowed artifact locations. Do not reinstall the MCP server inside every project.

## Check Setup

```bash
npm exec --yes --package aihil -- aihil doctor
```

Expected healthy result: `ok: true`, `tool: "aihil_doctor"`, `summary: "AI-HIL configuration loaded and debugger checked."`, and a nested debugger result with `ok: true`.

## Configure MCP

AI-HIL uses MCP over stdio. `.mcp.json` is only the MCP launch entry and should normally be the stable portable shape below.

`mcp-stdio` does not take `--port`; it is project-scoped. COM MCP tool calls pass `port_id` and CAN MCP tool calls pass `bus_id` as tool arguments.

Project-level MCP client discovery config belongs in:

```text
.mcp.json
```

```json
{
  "mcpServers": {
    "aihil": {
      "command": "npm",
      "args": ["exec", "--yes", "--package", "aihil", "--", "aihil", "mcp-stdio", "--config", ".aihil/config.yaml"]
    }
  }
}
```

The same template is shipped with the package under `dist/templates/mcp.json`. If the machine already has a user-local `aihil` command on `PATH`, a direct `command: "aihil"` entry is also fine.

Use the configured COM MCP tools for serial stimuli and feedback. Use the configured CAN MCP tools for CAN stimuli and feedback. Do not open host COM devices or CAN adapters directly.

If the user explicitly wants a continuous plain text serial channel instead of MCP tool calls, start a separate process:

```bash
aihil com-stdio --config .aihil/config.yaml --port dut_uart
```

Do not mix plain COM text into `aihil mcp-stdio`; MCP stdio must remain JSON-RPC only. `com-stdio` is the command that requires `--port` because it binds one text stream to one configured COM port.

## Use The Tools

Use `tools/list` to discover available MCP tools, then follow this loop:

1. Build firmware.
2. Check debugger availability with `aihil_debugger_info` if setup is unclear.
3. Probe with `aihil_probe_target`.
4. Flash with `aihil_flash_firmware` using `image_path`, usually `build/firmware.elf`, or first call `aihil_artifact_upload` with `image_path` and flash the returned `artifact_id`.
5. For serial feedback, start `aihil_com_session_start`, send stimuli with `aihil_com_write`, read feedback with `aihil_com_read`, then stop with `aihil_com_session_stop`.
6. For CAN feedback, start `aihil_can_session_start`, send frames with `aihil_can_send`, read frames with `aihil_can_read`, then stop with `aihil_can_session_stop`.
7. Read the tool result and `aihil_get_last_report`.
8. Diagnose failures with `aihil_classify_last_error`.

Do not use raw OpenOCD commands, arbitrary COM port shell tools, or direct CAN adapter tools when an AI-HIL MCP tool is available.

Healthy probe and flash signals are `target_detected: true`, `success_confirmed: true`, `verify: true`, `reset_after_flash: true`, plus `report_path` and `log_path` for auditability.
