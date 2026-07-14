# Security Policy

Agentic HIL's purpose is to be a safe, bounded gate between AI agents and real embedded hardware. Anything that lets an MCP client exceed the host-managed trusted policy selected by `AGENTIC_HIL_POLICY` is a security vulnerability, not just a bug. That includes:

- flashing or reading artifacts outside `artifacts.allowed_roots`
- reaching serial devices, CAN channels, or executables that are not named in the config
- executing actions whose permission switch is disabled
- command or path injection through MCP tool arguments or config values
- writing files outside the project through report, log, or dump paths

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest release on PyPI | yes |
| older releases | no |

## Reporting a Vulnerability

Please do not open a public issue for security reports. Instead:

- use GitHub private vulnerability reporting on this repository, or
- email `mail@hannes-pauli.de` with a description, a proof-of-concept config/tool call, and the affected version.

You can expect an acknowledgement within 7 days. Please allow time for a fix and release before public disclosure.

## Scope Notes

The project-local `.agentic-hil/config.yaml` and `.mcp.json` are assumed agent-writable and are not trusted authorization inputs. MCP hardware access requires an absolute `AGENTIC_HIL_POLICY` path outside the workspace. Keep that file and the host/user-level MCP registration non-writable by the agent. Agentic HIL intersects project requests with the policy snapshot loaded at process startup; workspace edits cannot widen it. Debugger/GDB/process-bridge executables and OpenOCD scripts authorized by the policy must also live outside the workspace. Configured artifact/output directories must remain non-symlink paths inside the workspace.

This boundary assumes the agent cannot modify the trusted policy, parent-process environment, host MCP registration, installed executables, or Agentic HIL process itself. If the agent has arbitrary shell access as the same OS identity, run Agentic HIL as a separate service account or isolated process and restrict the IPC boundary; an in-process YAML policy cannot sandbox an already equivalent host principal.
