# AI Agent Quickstart

Use AI-HIL as the local MCP server for embedded hardware actions.

## Install Once

Install the `aihil` command once on the local machine:

```bash
python -m pip install -e .
```

## Configure Each Project

In every firmware project that should use AI-HIL, create a project-local `.aihil/config.yaml`:

```bash
aihil init
```

Edit `.aihil/config.yaml` for the local board, OpenOCD interface, target config, and allowed firmware artifact roots.

Keep `.aihil/` with the project because it defines that project's hardware policy, reports, logs, and allowed artifact locations. Do not reinstall the MCP server inside every project.

## Check Setup

```bash
aihil doctor
```

## Start MCP Server

```bash
aihil serve --config .aihil/config.yaml
```

Endpoint:

```text
http://127.0.0.1:8732/mcp
```

Project-level MCP client discovery config belongs in:

```text
.mcp.json
```

## Use The Tools

Use `tools/list` to discover available MCP tools, then follow this loop:

1. Build firmware.
2. Probe with `aihil_probe_target`.
3. Flash with `aihil_flash_firmware` using `image_path`, usually `build/firmware.elf`.
4. Read the tool result and `aihil_get_last_report`.
5. Diagnose failures with `aihil_classify_last_error`.

Do not use raw OpenOCD commands.
