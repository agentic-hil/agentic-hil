# Security Design

Agentic HIL is a local MCP stdio server for agent-driven embedded hardware workflows. Its security design focuses on keeping host and hardware actions explicit, narrow, configured, and auditable.

## Threat Model

Agentic HIL assumes an agent can request hardware actions, but should not receive arbitrary host shell access, arbitrary debugger access, or unrestricted device access through Agentic HIL. The project-local `.hardci/config.yaml` file is the authority for target configuration, artifact roots, named COM ports, CAN buses, test adapters, and permissions. Whoever can edit that file controls the gate — protect it like CI configuration.

The primary risks are:

- Arbitrary command execution through debugger, COM-port, CAN, or adapter-bridge escape hatches.
- Flashing unintended firmware artifacts or files outside approved project roots.
- Performing destructive hardware actions such as mass erase without an explicit safe policy.
- Driving stimulus channels or fault states that the project policy did not allow.
- Confusing MCP JSON-RPC control output with plain serial text output.
- Leaking host paths, serial logs, hardware identifiers, or local configuration details in reports.

## Mitigations

- MCP tools expose named, high-level actions — probe, flash, reset, report retrieval, configured COM/CAN sessions, and configured test-adapter actions — instead of a raw debugger shell or direct host device access.
- Firmware artifacts must be under configured artifact roots, match configured extensions, and pass format plausibility checks before flashing or upload resolution; path traversal is rejected.
- Uploaded artifacts are size-limited and identified with SHA-256 metadata.
- COM, CAN, and adapter access use configured `port_id`/`bus_id`/`adapter_id` values. Agentic HIL never opens host devices or executables from agent-provided paths.
- Test-adapter channel and fault names are explicit allowlists validated before any request reaches the adapter bridge.
- Serial/CAN writes are size-capped; reads are buffer-capped; debugger calls run with timeouts and with OpenOCD's TCP servers disabled.
- Flashing is refused while `allow_raw_debugger_commands` or `allow_mass_erase` is enabled — validated flashing and unrestricted debugger access are mutually exclusive policies.
- `mcp-stdio` is reserved for JSON-RPC. Plain serial text uses the separate `com-stdio` path only when explicitly requested.
- Reports and structured errors include `ok`, `error_type`, `backend_error_type`, `summary`, `likely_causes`, `report_path`, and `log_path` so failures can be audited without bypassing policy.

## Cryptography Scope

Agentic HIL does not implement authentication, password storage, encryption protocols, key agreement, or custom cryptographic primitives. It uses the Python standard library (`hashlib`) for SHA-256 artifact metadata. Release integrity is handled by PyPI delivery over HTTPS, GitHub Actions OIDC trusted publishing, and GitHub artifact attestations.

## Secure Development Practices

The project uses type-annotated Python with schema-validated configuration, pytest end-to-end tests against fake debugger/bridge fixtures, ruff linting in CI, a 3-OS × 4-Python-version CI matrix, and Dependabot for dependency monitoring. Major behavior changes should include or update automated tests and preserve the configured safety boundaries documented in `CONTRIBUTING.md` and `SECURITY.md`. Policy bypasses are treated as vulnerabilities — see `SECURITY.md` for reporting.
