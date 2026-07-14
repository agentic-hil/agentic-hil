# Security Policy

Agentic Hardware-in-the-Loop (Agentic HIL) is a safe, bounded gate between AI agents and real embedded hardware. Anything that lets an MCP client bypass the project policy in `.agentic-hil/config.yaml` is a security vulnerability, not just a bug. That includes:

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

Agentic HIL executes locally configured debugger executables (OpenOCD, STM32CubeProgrammer CLI) and, for `adapter: process` CAN bridges, an executable named in the project config. The config file itself is trusted policy: whoever can edit `.agentic-hil/config.yaml` controls the gate. Protect it like you protect your CI configuration.
