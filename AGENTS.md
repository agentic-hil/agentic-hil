# Agentic HIL Agent Instructions

Agentic HIL is the hardware gate. It discovers the project's authoritative configuration outside the repository; `AGENTIC_HIL_CONFIG` may override that location with an absolute path. Mandatory `workspace_root` binds either file to the current project, and mandatory `state_root` pins trusted report and hardware-lease state outside the workspace.

For installation and first-time setup, follow [AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) — everything installs user-local without admin rights.

Names: the Python package/install target and Python-facing identifiers such as imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`. The CLI command, repository URL, and MCP server name use `agentic-hil`.

Use Agentic HIL MCP tools for hardware actions. Do not bypass them with raw OpenOCD commands, arbitrary debugger shells, direct serial-device access, or direct CAN-adapter access when an Agentic HIL tool is available.

If an Agentic HIL tool returns `permission_denied`, stop and ask the operator to review the authoritative config. Do not bypass it or replace the operator-controlled environment or MCP host registration.

Continue after a result only when `ok` is true, `target_ok`, `audit_ok`, and `cleanup_ok` are not false, `cleanup_required` and `quarantined` are not true, `lease_state` is one of `null`, `active`, or `released` (any other value, including `stale`, blocks success), `side_effect_status` is not `unknown` or `partial`, and `hardware_state` is not `unknown`. The public `overall_success()` helper in `agentic_hil.report` encodes exactly this predicate. For quarantined hardware, stop effects and ask the operator to inspect `agentic-hil lease-status`, physically confirm the current incident, and run `agentic-hil recover --confirm-safe-state --quarantine-id <id>`. If recovery returns `config_changed` (the authoritative config changed since the incident), the operator verifies the config delta and reruns with the explicit `--accept-config-change` override.

Install or update the local agent setup skill with:

```bash
agentic-hil skill-install --agent opencode
```
