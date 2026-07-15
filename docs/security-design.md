# Security Design

Agentic Hardware-in-the-Loop (Agentic HIL) is a local MCP stdio server for agent-driven embedded hardware workflows. Its security design focuses on keeping host and hardware actions explicit, narrow, configured, and auditable.

## Threat Model

Agentic HIL assumes an agent can request hardware actions and edit every file in the project workspace, including `.mcp.json` and test plans. The one authoritative project configuration is therefore stored outside the workspace, discovered from the canonical project path or selected through an absolute `AGENTIC_HIL_CONFIG` override, bound to the exact workspace path by mandatory `workspace_root`, and controlled by the operator.

The primary risks are:

- Arbitrary command execution through debugger, COM-port, or CAN escape hatches.
- Self-granting permissions or adding resources by editing project configuration.
- Flashing unintended firmware artifacts or files outside approved project roots.
- Performing destructive hardware actions such as mass erase without explicit authorization.
- Confusing MCP JSON-RPC control output with plain serial text output.
- Leaking host paths, serial logs, hardware identifiers, or local configuration details in reports.

## Mitigations

- MCP startup fails unless the discovered config or absolute-path override is outside the workspace and its `workspace_root` exactly matches the current project.
- The authoritative config contains hardware resources, permissions, validation requirements, allowlists, and limits. It is the only project/hardware configuration used by MCP, `doctor`, `com-stdio`, pytest, and the test reactor.
- `agentic-hil init` creates this external config deny-by-default from the project root and prints the environment setting for the operator to install.
- MCP tools expose named, high-level actions — probe, flash, reset, report retrieval, and configured COM/CAN sessions — instead of a raw debugger shell or direct host device access.
- Firmware artifacts must be under configured artifact roots, match configured extensions, and pass format plausibility checks before flashing or upload resolution; path traversal is rejected.
- Before backend use, artifacts are reopened without following links, checked for replacement and multiple links, and copied to a private process staging directory. Debuggers never reopen the agent-controlled source path.
- Uploaded artifacts are size-limited and identified with SHA-256 metadata.
- COM and CAN access use only `port_id`/`bus_id` values present in the authoritative config.
- Configured debugger, GDB, CAN process-bridge executables, and OpenOCD scripts are resolved and pinned at startup; missing, relative OpenOCD-script, and workspace-resident paths are rejected.
- Artifact roots and report/log/upload directories are frozen to lexical workspace paths. Symlink pivots and symlinked output files fail closed.
- Empty symbol allowlists mean deny-all. Unrestricted symbol access requires `debug.allow_all_symbols: true` in the authoritative config.
- Debugger discovery/execution requires `allow_probe`; this includes listing every probe serial visible to the configured backend through `debugger_probes_list`. Target reset requires the separate `allow_reset` permission.
- Flashing requires `allow_flash`; an explicit post-flash reset additionally requires `allow_reset`.
- Serial/CAN writes are size-capped; reads are buffer-capped; debugger calls run with timeouts and with OpenOCD's TCP servers disabled.
- Flashing is refused while `allow_raw_debugger_commands` or `allow_mass_erase` is enabled — validated flashing and unrestricted debugger access are mutually exclusive policies.
- `mcp-stdio` is reserved for JSON-RPC. Plain serial text uses the separate `com-stdio` path only when explicitly requested.
- `.agentic-hil/testconfig.yaml` and `--test-config` select test steps only. A test plan cannot select hardware, grant permissions, or replace the discovered config or its override.
- Reports and structured errors include `ok`, `error_type`, `backend_error_type`, `summary`, `likely_causes`, `report_path`, and `log_path` so failures can be audited without bypassing configured controls.

## Cryptography Scope

Agentic HIL does not implement authentication, password storage, encryption protocols, key agreement, or custom cryptographic primitives. It uses the Python standard library (`hashlib`) for SHA-256 artifact metadata. Release integrity is handled by PyPI delivery over HTTPS, GitHub Actions OIDC trusted publishing, and GitHub artifact attestations.

## Secure Development Practices

The project uses type-annotated Python with schema-validated configuration, pytest end-to-end tests against fake backend fixtures, ruff linting in CI, a 3-OS × 4-Python-version CI matrix, and Dependabot for dependency monitoring. Major behavior changes should include or update automated tests and preserve the configured safety boundaries documented in `CONTRIBUTING.md` and `SECURITY.md`. Configuration bypasses are treated as vulnerabilities — see `SECURITY.md` for reporting.

## Same-Identity Limitation

The external config and operator-controlled environment prevent repository edits from silently selecting another hardware configuration, but they are not an OS sandbox. An agent with arbitrary shell access as the same OS identity can modify that user's config or process environment. For that threat model, run Agentic HIL under a separate service account or isolated process and restrict the IPC boundary.
