# AI-HIL Agent Instructions

AI-HIL is a local MCP-over-HTTP server that gives AI agents safe, structured access to an embedded hardware-in-the-loop setup.

Use the MCP server from this repository for hardware actions. Do not use raw OpenOCD commands or arbitrary shell commands for flashing, probing, or resetting hardware when an AI-HIL MCP tool is available.

## Installation Model

Install the `aihil` command and MCP server once on the local machine:

```bash
python -m pip install -e .
```

Each firmware project should have its own `.aihil/` directory with `.aihil/config.yaml` for that project's target, debugger, permissions, reports, logs, and artifact roots.

## Project Bootstrap

From the firmware project directory, create and inspect the project-local setup with:

```bash
aihil init
aihil doctor
aihil serve --config .aihil/config.yaml
```

The local MCP endpoint is:

```text
http://127.0.0.1:8732/mcp
```

Each project can include `.mcp.json` for MCP clients that discover project-level MCP configuration.

## Required Workflow

1. Build the firmware first.
2. Check debugger availability with `aihil_debugger_info` if setup is unclear.
3. Probe the target with `aihil_probe_target` before flashing.
4. Flash only validated artifacts with `aihil_flash_firmware`.
5. Use `aihil_reset_target` only with mode `run`, `halt`, or `init`.
6. Read `aihil_get_last_report` after hardware actions.
7. Use `aihil_classify_last_error` after failures.
8. Stop on `permission_denied`; the local AI-HIL configuration is authoritative.

## Available MCP Tools

```text
aihil_debugger_info
aihil_probe_target
aihil_flash_firmware
aihil_reset_target
aihil_get_last_report
aihil_classify_last_error
```

## Safety Rules

Never request or run raw OpenOCD/debugger commands for hardware actions.

Never flash files outside configured artifact roots.

Never mass erase.

Keep the server bound to `127.0.0.1` unless explicit authentication, transport security, and operator approval are in place.

Treat structured JSON results as the source of truth. Always inspect `ok`, `error_type`, `backend_error_type`, `summary`, `likely_causes`, `report_path`, and `log_path` before deciding what to do next.

## Development Commands

```bash
pytest
python -m pip install -e .
aihil init --force
aihil doctor
aihil mcp-config
aihil serve --config .aihil/config.yaml
```
