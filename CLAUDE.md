# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

Canonical agent instructions live in `AGENTS.md` and `AI_AGENT_QUICKSTART.md`. Human-facing setup and troubleshooting entry points live in `README.md` and `TROUBLESHOOTING.md`.

## Project Overview

Agentic HIL is a Python MCP stdio server for safe embedded firmware development with local hardware-in-the-loop targets. It exposes narrow tools for probing, flashing, resetting, configured COM port stimulus/feedback, configured CAN bus stimulus/feedback, and reading structured reports from a configured local embedded target.

Agentic HIL discovers the project's one authoritative configuration outside the repository; `AGENTIC_HIL_CONFIG` is an optional absolute-path override. Mandatory `workspace_root` binds either file to the exact project root. If an Agentic HIL tool returns `permission_denied`, stop and ask the user; never bypass the config.

Use STM32 Nucleo-F446RE + ST-Link + OpenOCD + Python 3.10 or newer as the supported first path unless project files or the user clearly identify another setup.

## Development

- Install: `python -m pip install -e '.[dev,can]'`
- Lint: `ruff check src tests`
- Test: `pytest`
- Package check: `python -m build` and `twine check dist/*`

Do not bypass Agentic HIL MCP tools with raw debugger commands, direct serial-device access, or direct CAN-adapter access when a Agentic HIL tool is available.
