# Agentic HIL Agent Instructions

Agentic Hardware-in-the-Loop (Agentic HIL) is the hardware gate. The project-local `.agentic-hil/config.yaml` is the policy. HardCI adapters are the reference hardware.

For installation and first-time setup, follow [AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) — everything installs user-local without admin rights.

Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

Use Agentic HIL MCP tools for hardware actions. Do not bypass them with raw OpenOCD commands, arbitrary debugger shells, direct serial-device access, direct CAN-adapter access, or direct test-adapter access when an Agentic HIL tool is available.

If an Agentic HIL tool returns `permission_denied`, stop. Do not loosen policy unless the user explicitly asks.

If an Agentic HIL tool returns `hardware_state_unconfirmed`, or `agentic-hil hardware-status` shows an `active` or `quarantined` marker, stop hardware actions. Do not delete marker files manually. Tell the operator to inspect the rig and use `agentic-hil hardware-status` followed by `agentic-hil hardware-recover --quarantine-id <id> --acknowledge-hardware-checked` only after hardware is physically safe.

Install or update the local agent setup skill with:

```bash
agentic-hil skill-install --agent opencode
```
