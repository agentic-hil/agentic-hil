---
name: hardci-config-setup
description: Configure Agentic HIL as the safe local hardware-in-the-loop MCP bridge for an embedded firmware project.
metadata:
  origin: HardCI
  hardci_version: "0.2.0"
---

# Agentic HIL Config Setup

Use Agentic HIL as the project-local hardware gate. The policy file is `.hardci/config.yaml`.

Names: the Python package/install target and Python-facing identifiers such as imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`. The CLI command, repository URL, and MCP server name use `agentic-hil`.

Install and initialize from the firmware project directory:

```bash
agentic-hil init
agentic-hil doctor
```

Never bypass Agentic HIL policy with raw debugger commands, direct serial device access, or direct CAN adapter access when an Agentic HIL MCP tool is available.

If any Agentic HIL tool returns `permission_denied`, stop and ask the user before changing policy.
