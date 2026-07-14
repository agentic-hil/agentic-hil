# Security Design

Agentic Hardware-in-the-Loop (Agentic HIL) is a local MCP stdio server for agent-driven embedded hardware workflows. Its security design focuses on keeping host and hardware actions explicit, narrow, configured, and auditable.

## Threat Model

Agentic HIL assumes an agent can request hardware actions and edit every file in the project workspace, including `.agentic-hil/config.yaml` and `.mcp.json`. Those files are therefore untrusted input, not an authorization boundary. The authority is a human-reviewed policy selected through `AGENTIC_HIL_POLICY`, stored outside the workspace, and loaded once when the MCP process starts. The project configuration may narrow that startup ceiling but cannot widen it.

The primary risks are:

- Arbitrary command execution through debugger, COM-port, CAN, or adapter-bridge escape hatches.
- Self-granting permissions or adding resources by editing project configuration.
- Flashing unintended firmware artifacts or files outside approved project roots.
- Performing destructive hardware actions such as mass erase without an explicit safe policy.
- Driving stimulus channels or fault states that the effective policy did not allow.
- Confusing MCP JSON-RPC control output with plain serial text output.
- Leaking host paths, serial logs, hardware identifiers, or local configuration details in reports.

## Mitigations

- MCP startup fails unless `AGENTIC_HIL_POLICY` names an absolute policy path outside the workspace.
- The trusted policy is an immutable per-process ceiling. Permission booleans are ANDed, validation requirements are ORed, allowlists and named resources are intersected, numeric limits use the stricter value, and physical endpoints/executables come from the trusted policy.
- Project configuration is reloaded before each tool call only after the trusted ceiling is fixed. Invalid edits fail closed; valid edits can revoke but never grant access.
- MCP tools expose named, high-level actions — probe, flash, reset, report retrieval, configured COM/CAN sessions, and configured test-adapter actions — instead of a raw debugger shell or direct host device access.
- Firmware artifacts must be under configured artifact roots, match configured extensions, and pass format plausibility checks before flashing or upload resolution; path traversal is rejected.
- Before backend use, artifacts are reopened without following links, checked for replacement and multiple links, and copied to a private process staging directory. Debuggers never reopen the agent-controlled source path.
- Uploaded artifacts are size-limited and identified with SHA-256 metadata.
- COM, CAN, and adapter access use `port_id`/`bus_id`/`adapter_id` values present in both configurations. Physical device and executable settings are taken from the trusted policy.
- Trusted debugger, GDB, process-bridge executables, and OpenOCD scripts are resolved and pinned at startup; missing, relative OpenOCD-script, and workspace-resident paths are rejected.
- Artifact roots and report/log/upload directories are frozen to lexical workspace paths. Symlink pivots and symlinked output files fail closed.
- Test-adapter channel and fault names are intersected explicit allowlists validated before any request reaches the adapter bridge.
- Empty symbol allowlists mean deny-all. Unrestricted symbol access requires `debug.allow_all_symbols: true` in both project and trusted configurations.
- Debugger discovery/execution requires `allow_probe`; target reset requires the separate `allow_reset` permission.
- Flashing requires both `allow_flash` and `allow_reset` because supported flash backends perform reset sequences internally.
- Serial/CAN writes are size-capped; reads are buffer-capped; debugger calls run with timeouts and with OpenOCD's TCP servers disabled.
- Flashing is refused while `allow_raw_debugger_commands` or `allow_mass_erase` is enabled — validated flashing and unrestricted debugger access are mutually exclusive policies.
- `mcp-stdio` is reserved for JSON-RPC. Plain serial text uses the separate `com-stdio` path only when explicitly requested.
- Reports and structured errors include `ok`, `error_type`, `backend_error_type`, `summary`, `likely_causes`, `report_path`, and `log_path` so failures can be audited without bypassing policy.

## Cryptography Scope

Agentic HIL does not implement authentication, password storage, encryption protocols, key agreement, or custom cryptographic primitives. It uses the Python standard library (`hashlib`) for SHA-256 artifact metadata. Release integrity is handled by PyPI delivery over HTTPS, GitHub Actions OIDC trusted publishing, and GitHub artifact attestations.

## Secure Development Practices

The project uses type-annotated Python with schema-validated configuration, pytest end-to-end tests against fake debugger/bridge fixtures, ruff linting in CI, a 3-OS × 4-Python-version CI matrix, and Dependabot for dependency monitoring. Major behavior changes should include or update automated tests and preserve the configured safety boundaries documented in `CONTRIBUTING.md` and `SECURITY.md`. Policy bypasses are treated as vulnerabilities — see `SECURITY.md` for reporting.
