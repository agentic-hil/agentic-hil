---
name: hardci-config-setup
description: Configure Agentic HIL as the safe local hardware-in-the-loop MCP bridge for an embedded firmware project.
metadata:
  origin: HardCI
  hardci_version: "0.2.0"
---

# Agentic HIL Config Setup

Use Agentic HIL as the project-local hardware gate. The policy file is `.hardci/config.yaml`.

Install and initialize from the firmware project directory:

```bash
agentic-hil init
agentic-hil doctor
```

Never bypass Agentic HIL policy with raw debugger commands, direct serial device access, or direct CAN adapter access when an Agentic HIL MCP tool is available.

If any Agentic HIL tool returns `permission_denied`, stop and ask the user before changing policy.
