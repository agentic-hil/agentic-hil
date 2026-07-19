# Security Policy

Agentic Hardware-in-the-Loop (Agentic HIL) is a safe, bounded gate between AI agents and real embedded hardware. Anything that lets an MCP client exceed the discovered authoritative project configuration or its explicit `AGENTIC_HIL_CONFIG` override is a security vulnerability, not just a bug. That includes:

- flashing or reading artifacts outside `artifacts.allowed_roots`
- reaching serial devices, CAN channels, or executables that are not named in the config
- executing actions whose permission switch is disabled
- command or path injection through MCP tool arguments or config values
- writing files outside the project through report, log, or dump paths
- bypassing resource leases, stale-owner quarantine, audit fail-closed behavior, or bridge safe-state confirmation

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

Each project has exactly one automatically discovered authoritative config, at `%APPDATA%/agentic-hil/projects/<project-id>/config.yaml` on Windows or `${XDG_CONFIG_HOME:-~/.config}/agentic-hil/projects/<project-id>/config.yaml` on POSIX. `AGENTIC_HIL_CONFIG` may select another absolute path, but either file must remain outside the workspace and mandatory `workspace_root` must bind it to the exact project root. Mandatory `state_root` must be absolute, operator-controlled, non-overlapping with the workspace, and shared by every trusted launcher that can address the same host hardware. Keep both roots and any override operator-controlled. Debugger/GDB/CAN process-bridge executables and OpenOCD scripts named by the config must also live outside the workspace. Repository `.agentic-hil/testconfig.yaml` files are test plans only and carry no hardware authority.

Mutable canonical report and hardware-lease state is stored under the pinned `state_root`; `agentic-hil init` selects `%LOCALAPPDATA%/agentic-hil` on Windows or `${XDG_STATE_HOME:-~/.local/state}/agentic-hil` on POSIX as its initial value. Agentic HIL rejects relative roots, workspace overlap, unsafe POSIX ownership/modes, and path redirection. Do not delete or edit quarantine records to bypass recovery. Inspect `agentic-hil lease-status`, confirm physical safe state for its current incident, then use `agentic-hil recover --confirm-safe-state --quarantine-id <id>`.

This boundary assumes the agent cannot modify the authoritative config, parent-process environment, host MCP registration, installed executables, or Agentic HIL process itself. If the agent has arbitrary shell access as the same OS identity, run Agentic HIL as a separate service account or isolated process and restrict the IPC boundary; an external YAML config cannot sandbox an already equivalent host principal.
