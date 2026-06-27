# Security Policy

AI-HIL is a local MCP stdio bridge for agent-driven embedded hardware workflows. Security issues include both host access risks and hardware-safety risks.

## Supported Versions

AI-HIL is pre-1.0. Security fixes target the latest published npm version and the current default branch.

## Reporting A Vulnerability

Use GitHub private vulnerability reporting if it is available for this repository: https://github.com/hp-8472/aihil/security/advisories/new.

If GitHub private vulnerability reporting is unavailable, contact the maintainer directly at mail@hannes-pauli.de.

If neither private vulnerability reporting nor direct email is available, open a minimal public issue without exploit details, secrets, private hardware identifiers, or sensitive logs. Ask for a private follow-up channel in the issue.

The maintainer aims to acknowledge vulnerability reports within 14 days. Security fixes target the latest published npm version and the current default branch.

## In Scope

- Bypassing configured artifact roots.
- Gaining arbitrary shell or raw debugger command execution through AI-HIL.
- Bypassing configured COM-port ids or opening arbitrary host serial devices.
- Enabling destructive hardware actions such as mass erase without explicit safe policy.
- Leaking sensitive local paths, environment data, serial logs, or reports unexpectedly.
- MCP stdio behavior that lets non-JSON output corrupt or confuse the control channel.

## Out Of Scope

- Bugs in OpenOCD, Node.js, serial drivers, debug probes, or target firmware unless AI-HIL exposes them in an unsafe way.
- Physical attacks on local hardware.
- Issues requiring already-unrestricted local shell access without an AI-HIL-specific privilege expansion.

## Safety Expectations

Stop on `permission_denied`. Do not work around AI-HIL by using raw debugger commands, arbitrary host COM tools, or mass erase while investigating a report.

Sanitize `.aihil/config.yaml`, `.aihil/reports/last-report.json`, OpenOCD logs, and COM logs before sharing them.

See [docs/security-design.md](docs/security-design.md) for the project threat model, common error classes, and cryptography scope.
