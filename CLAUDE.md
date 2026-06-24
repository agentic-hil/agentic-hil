# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Project Overview

AI-HIL is a Python/FastAPI MCP-over-HTTP server for safe embedded hardware-in-the-loop access. It exposes narrow tools for probing, flashing, resetting, and reading structured reports from a configured local target.

## First Steps

```bash
python -m pip install -e .
aihil init
aihil doctor
aihil serve --config .aihil/config.yaml
```

MCP endpoint:

```text
http://127.0.0.1:8732/mcp
```

Project MCP config:

```text
.mcp.json
```

## Agent Rules

Use the AI-HIL MCP tools for hardware actions. Do not use raw OpenOCD commands or shell commands for probe, flash, or reset workflows when the MCP server is available.

Follow this sequence for hardware validation:

1. Build firmware.
2. Call `aihil_probe_target`.
3. Call `aihil_flash_firmware` with a validated artifact path, usually `build/firmware.elf`.
4. Read the returned JSON result.
5. Call `aihil_get_last_report`.
6. Call `aihil_classify_last_error` after failed actions.

Stop on `permission_denied` and report the local policy restriction.

## Tests

```bash
pytest
```

## Important Files

```text
src/aihil/server.py       FastAPI app and /mcp endpoint
src/aihil/mcp.py          MCP JSON-RPC implementation
src/aihil/tools.py        Shared tool service used by MCP
src/aihil/config.py       .aihil/config.yaml parsing and policy
src/aihil/artifacts.py    Firmware artifact validation
src/aihil/debuggers/      Debugger backends
tests/                    pytest suite
```
