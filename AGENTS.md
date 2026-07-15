# Agentic HIL Agent Instructions

Agentic HIL is the hardware gate. It discovers the project's authoritative configuration outside the repository; `AGENTIC_HIL_CONFIG` may override that location with an absolute path. Mandatory `workspace_root` binds either file to the current project.

For installation and first-time setup, follow [AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) — everything installs user-local without admin rights.

Names: the Python package/install target and Python-facing identifiers such as imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`. The CLI command, repository URL, and MCP server name use `agentic-hil`.

Use Agentic HIL MCP tools for hardware actions. Do not bypass them with raw OpenOCD commands, arbitrary debugger shells, direct serial-device access, or direct CAN-adapter access when an Agentic HIL tool is available.

If an Agentic HIL tool returns `permission_denied`, stop and ask the operator to review the authoritative config. Do not bypass it or replace the operator-controlled environment or MCP host registration.

Install or update the local agent setup skill with:

```bash
agentic-hil skill-install --agent opencode
```
