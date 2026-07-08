# Agentic HIL Agent Instructions

Agentic HIL is the hardware gate. The project-local `.hardci/config.yaml` is the policy. HardCI adapters are the reference hardware.

For installation and first-time setup, follow [AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) — everything installs user-local without admin rights.

Use Agentic HIL MCP tools for hardware actions. Do not bypass them with raw OpenOCD commands, arbitrary debugger shells, direct serial-device access, direct CAN-adapter access, or direct test-adapter access when an Agentic HIL tool is available.

If a HardCI tool returns `permission_denied`, stop. Do not loosen policy unless the user explicitly asks.

Install or update the local agent setup skill with:

```bash
agentic-hil skill-install --agent opencode
```
