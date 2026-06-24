# AI Agent Quickstart

Use this repository as the AI-HIL MCP server for embedded hardware actions.

## Install From A Clone

```bash
python -m pip install -e .
```

## Create Local Config

```bash
aihil init
```

Edit `.aihil/config.yaml` for the local board, OpenOCD interface, target config, and allowed firmware artifact roots.

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

MCP client config is available in:

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
