---
name: agentic-hil-config-setup
description: Configure Agentic Hardware-in-the-Loop (Agentic HIL) as the safe local MCP bridge for an embedded firmware project.
metadata:
  origin: Agentic HIL
  agentic_hil_version: "0.2.3"
---

# Agentic HIL Config Setup

Use Agentic HIL as the project-local hardware gate. The policy file is `.agentic-hil/config.yaml`.

Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

Install and initialize from the firmware project directory:

```bash
agentic-hil init
agentic-hil doctor
```

Never bypass Agentic HIL policy with raw debugger commands, direct serial device access, or direct CAN adapter access when an Agentic HIL MCP tool is available.

If any Agentic HIL tool returns `permission_denied`, stop and ask the user before changing policy.

If any Agentic HIL tool returns `hardware_state_unconfirmed`, or `agentic-hil hardware-status` shows an `active` or `quarantined` marker, stop hardware actions. Do not delete marker files manually. Ask the operator to inspect the rig and recover with `agentic-hil hardware-recover --quarantine-id <id> --acknowledge-hardware-checked` only after hardware is safe.
