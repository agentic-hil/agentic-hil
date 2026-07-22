---
name: agentic-hil-config-setup
description: Configure Agentic Hardware-in-the-Loop (Agentic HIL) as the safe local MCP bridge for an embedded firmware project.
metadata:
  origin: Agentic HIL
  agentic_hil_version: "0.4.0"
---

# Agentic HIL Config Setup

Use Agentic HIL as the hardware gate. It discovers the single authoritative project configuration under the current user's Agentic HIL projects directory; `AGENTIC_HIL_CONFIG` may provide an explicit absolute-path override. That file contains the workspace binding, hardware resources, permissions, and limits.

Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

This plugin distributes setup guidance only. It deliberately does not store an MCP server command because a portable plugin cannot embed the verified absolute path of a persistent, user-local executable. Install the package version matching this plugin before configuring a project:

```bash
uv tool install --upgrade "agentic-hil==0.4.0"
agentic-hil --version
```

The version check must report exactly `0.4.0`. If `uv` is unavailable or a different `agentic-hil` resolves on `PATH`, stop and ask the operator to establish the trusted user-local prerequisite; do not substitute `uvx`, a workspace virtual environment, or an unversioned package.

Initialize and diagnose from the firmware project directory:

```bash
agentic-hil setup --agent claude
```

`setup` creates the deny-by-default project config, installs the packaged copy of this skill, and registers the MCP server with the verified absolute persistent console-script path. Ask the human operator to review permission changes and set `AGENTIC_HIL_CONFIG` only when an explicit absolute-path override is needed. Never bypass Agentic HIL permissions with raw debugger commands, direct serial device access, or direct CAN adapter access when an Agentic HIL MCP tool is available.

If any Agentic HIL tool returns `permission_denied`, stop and ask the user before changing the authoritative config.
