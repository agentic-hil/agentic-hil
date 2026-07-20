---
name: agentic-hil-config-setup
description: Configure Agentic Hardware-in-the-Loop (Agentic HIL) as the safe local MCP bridge for an embedded firmware project.
metadata:
  origin: Agentic HIL
  agentic_hil_version: "0.3.0"
---

# Agentic HIL Config Setup

Use Agentic HIL as the hardware gate. It discovers the single authoritative project configuration under the current user's Agentic HIL projects directory; `AGENTIC_HIL_CONFIG` may provide an explicit absolute-path override. That file contains the workspace binding, hardware resources, permissions, and limits.

Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

Install and initialize from the firmware project directory:

```bash
agentic-hil init
agentic-hil doctor
```

`agentic-hil init` prints the generated config path. Ask the human operator to review permission changes and set `AGENTIC_HIL_CONFIG` only when an explicit absolute-path override is needed. Never bypass Agentic HIL permissions with raw debugger commands, direct serial device access, or direct CAN adapter access when an Agentic HIL MCP tool is available.

If any Agentic HIL tool returns `permission_denied`, stop and ask the user before changing the authoritative config.
