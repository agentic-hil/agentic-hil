# AI-HIL Agent Instructions

AI-HIL is a local MCP stdio server that gives AI agents safe, structured access to an embedded hardware-in-the-loop setup.

Use the MCP server from this repository for hardware actions. Do not use raw OpenOCD commands or arbitrary shell commands for flashing, probing, or resetting hardware when an AI-HIL MCP tool is available.

## Installation Model

Install the `aihil` command once on the local machine:

```bash
python -m pip install -e .
```

Each firmware project should have its own `.aihil/` directory with `.aihil/config.yaml` for that project's target, debugger, permissions, reports, logs, and artifact roots.

Use `skills/aihil-config-setup/SKILL.md` as the agent-facing workflow for creating or fixing `.aihil/config.yaml`.

If a user says "Install this AI-HIL repo and set it up for this project", install the `aihil` command from the AI-HIL repo, then return to the firmware project and follow `skills/aihil-config-setup/SKILL.md`. Do not copy the AI-HIL source tree into the firmware project unless the user explicitly asks.

## Project Bootstrap

From the firmware project directory, create and inspect the project-local setup with:

```bash
aihil init
aihil doctor
aihil mcp-config > .mcp.json
```

The MCP client starts AI-HIL with stdio using:

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

## Available MCP Tools

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
pytest
python -m pip install -e .
aihil init --force
aihil doctor
aihil mcp-config
aihil mcp-stdio --config .aihil/config.yaml
aihil com-stdio --config .aihil/config.yaml --port dut_uart
```
