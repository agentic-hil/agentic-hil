# Security Design

AI-HIL is a local MCP stdio server for agent-driven embedded hardware workflows. Its security design focuses on keeping host and hardware actions explicit, narrow, configured, and auditable.

## Threat Model

AI-HIL assumes an agent can request hardware actions, but should not receive arbitrary host shell access, arbitrary debugger access, or unrestricted serial device access through AI-HIL. The project-local `.aihil/config.yaml` file is the authority for target configuration, artifact roots, COM port identifiers, and permissions.

The primary risks are:

- Arbitrary command execution through debugger or COM-port escape hatches.
- Flashing unintended firmware artifacts or files outside approved project roots.
- Performing destructive hardware actions such as mass erase without an explicit safe policy.
- Confusing MCP JSON-RPC control output with plain serial text output.
- Leaking host paths, serial logs, hardware identifiers, or local configuration details in reports.

## Mitigations

- MCP tools expose named, high-level actions such as probe, flash, reset, report retrieval, and configured COM sessions instead of a raw debugger shell.
- Firmware artifacts must be under configured artifact roots and match configured extensions before flashing or upload resolution.
- Uploaded artifacts are size-limited and identified with SHA-256 metadata.
- COM-port access uses configured `port_id` values. AI-HIL does not open arbitrary host serial devices from agent-provided paths.
- `mcp-stdio` is reserved for JSON-RPC. Plain serial text uses the separate `com-stdio` path only when explicitly requested.
- Reports and structured errors include `ok`, `error_type`, `backend_error_type`, `summary`, `likely_causes`, `report_path`, and `log_path` so failures can be audited without bypassing policy.

## Cryptography Scope

AI-HIL does not implement authentication, password storage, encryption protocols, key agreement, or custom cryptographic primitives. It uses Node.js standard library cryptography for SHA-256 artifact metadata. Release integrity is handled by GitHub/npm delivery over HTTPS, npm provenance, SBOMs, and GitHub artifact attestations.

## Secure Development Practices

The project uses TypeScript strict mode, Node.js tests, GitHub Actions CI, Dependency Review, npm production audits, and CodeQL for JavaScript/TypeScript static analysis. Major behavior changes should include or update automated tests and preserve the configured safety boundaries documented in `CONTRIBUTING.md` and `SECURITY.md`.
