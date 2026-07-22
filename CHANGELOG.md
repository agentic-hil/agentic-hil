# Changelog

All notable changes to Agentic Hardware-in-the-Loop (Agentic HIL) will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning while pre-1.0 changes may still move quickly.

## [Unreleased]

## [0.4.0] - 2026-07-21

### Added

- `agentic-hil setup --agent <agent>`: one-shot project setup (authoritative config + skill install + MCP registration + doctor) in a single command. It prepares a safe external `state_root` and, before the fail-closed trust validators run, silently tightens the operator's own group/other-writable directories along the config, state, and MCP-launcher chains so setup succeeds on default umask-002 / private-group homes without hand-fixing permissions. Only user-owned components are changed — the walk stops at the first foreign-owned or symlinked ancestor, never touching shared or system directories — and every change is reported in the result's `permission_changes`.
- Per-agent USER-level MCP registration by `setup`: Codex `~/.codex/config.toml`, opencode `~/.config/opencode/opencode.json`, and Claude Code `~/.claude.json`. Each file is merged directly with secure atomic writes outside the repository to preserve the policy trust boundary; registration is idempotent and fails closed on unsafe files.
- Claude Code plugin (`plugins/agentic-hil/` plus `.claude-plugin/marketplace.json`): distributes the version-matched setup skill without persisting an unverifiable portable MCP command. The skill requires an exact persistent package install and delegates user-level MCP registration to `setup`, which records the verified absolute console-script path. The repo is also discoverable by the Vercel `skills` CLI (`npx skills add`) for cross-agent skill distribution.

### Changed

- MCP registration stores only a verified absolute persistent console-script path and rejects bare PATH commands, transient runners, workspace virtual environments, unsafe symlink chains, and unsafe ownership or permissions. Stable user-owned pipx/uv-tool links remain supported after their link and resolved target chains pass validation.
- `AI_AGENT_QUICKSTART.md` leads with `agentic-hil setup`, documents the MCP registration as user-level per agent, and adds guidance for pip-less environments (Ubuntu 24.04+/PEP-668).

## [0.3.0] - 2026-07-20

### Added

- Added cross-process hardware leases, persistent crash quarantine, `lease-status`, and incident-bound operator recovery through `recover --confirm-safe-state --quarantine-id <id>`.
- Versioned process bridges at protocol v2 with mandatory physical safe-state acknowledgement before resource release.
- Added a permission-gated test reactor with configured `Device` bindings, semantic preflight, duplicate-key rejection, and YAML/JSON sequences for flashing, UART lifecycle, run-to-breakpoint, and Intel HEX symbol dumps with exception-safe cleanup.
- Added multi-board support to the test reactor: a named `debuggers` map lets each device drive an independent debug probe (`devices.<id>.debugger` selects `true` for the top-level debugger or a named entry, with an optional per-device `target` that requires a named debugger), enforcing one device per probe — validated again after executable pinning so lexically distinct executables cannot silently share one physical probe — and running each named debugger on its own service under one shared project lease. `agentic-hil doctor` now reports each device's debugger selector as `"default"`, a named-debugger name, or `null` instead of a boolean. Added `agentic-hil test-schema` to emit the bundled test-plan schema.
- Added `debugger_probes_list` and `agentic-hil debugger-probes` for permission-gated enumeration of connected probe IDs through STM32CubeProgrammer or pyOCD.

### Fixed

- Made audit/target status part of overall tool success, validated MCP and direct tool arguments against one strict contract, and blocked later effects after unknown or unaudited outcomes.
- Pinned mandatory trusted `state_root` outside the workspace and made coordinator/service lifecycles terminal across threads and stale lease references.
- Moved canonical report state into config-and-workspace-namespaced user state, stopped importing workspace snapshots, and made process/session cleanup retryable across partial failures and interrupts.
- Reworked artifact/config/log I/O around bounded, nonblocking, single-descriptor checks and rejected non-finite configuration timeouts.
- Preserved final test-reactor failure reports after successful cleanup and kept `classify_last_error` anchored to the latest failure instead of later read-only status calls.
- Created nested debug-dump output directories only during real dump execution, while keeping test-reactor preflight read-only.
- Prevented COM, CAN, and adapter sessions from leaking when audit log paths become unavailable during session start.
- Made Windows directory-chain pinning sharing-compatible so concurrent Agentic HIL operations keep their rename/delete protection, made the Windows report-state lock block like the POSIX branch, kept coordination-poisoning failures from masking primary hardware errors, expanded `~` in state-root environment overrides before use, and released private staging directories after local artifact uploads.
- Made lease release a durable, retryable transition and hardware recovery convergent: partial persistence resumes idempotently, incident markers agree on their resource set, and a config-hash change now refuses recovery with `config_changed` until the operator reruns with the explicit `--accept-config-change` override instead of dead-ending. Wrote detailed hardware-effect logs canonically under `state_root` with a monotonic sequence and tamper-evident hash chain, exposing `canonical_audit` verification on reports; latched GDB audit-write failures permanently and reconciled breakpoint cleanup against the backend; and retained provisional raw handles for retryable close after constructor and interrupt faults.

### Security

- Enforced one live owner for each project and physical probe/COM/CAN/adapter resource across MCP, CLI, pytest, reactor, and direct Python service entry points; stale owners now require explicit safe-state recovery.
- Normalized symlink/reparse secure-I/O failures to structured `unsafe_configured_path` errors and avoided wrapping failed report reads as successful `get_last_report` calls.
- Rejected process bridge `args` entirely and kept process bridges pinned to operator-controlled executables.
- Bounded in-memory path locks to active operations and hardened child-process cleanup/decoding for debugger and bridge subprocesses.
- Replaced the project-local/two-file configuration model with one deny-by-default authoritative config outside the repository, discovered from the project root with an optional absolute-path `AGENTIC_HIL_CONFIG` override and bound to the project by mandatory `workspace_root`.
- Added explicit gates for debugger execution and target reset (`allow_reset`), deny-by-default config generation, startup pinning for debugger/GDB/bridge executables and OpenOCD scripts, deny-all empty symbol allowlists, and symlink-safe artifact/output paths.
- Revalidated firmware artifacts into private process staging before backend use, rejected multiply-linked inputs/outputs, switched structured writes to atomic replacement, disabled GDB auto-loading, and moved trusted subprocess working directories outside the workspace.
- Revalidated the trusted `state_root` and every derived coordination/audit/report directory for owner, type, and write mode on each open (rejecting sticky world-writable roots and foreign-owned subdirectories), and stopped emitting environment-derived absolute state-root/config paths and any secret-named field to operator/CLI/MCP output sinks.

## [0.2.3] - 2026-07-14

### Changed

- Updated canonical repository URLs after the transfer to `agentic-hil/agentic-hil`.
- Added official MCP Registry metadata, validation, and OIDC publishing after each successful PyPI release while retaining the host-independent local setup path.

## [0.2.2] - 2026-07-14

### Changed

- Canonicalized the Python distribution metadata spelling and all installation guidance to `agentic-hil`; Python imports, the pytest plugin, and the fixture remain `agentic_hil`.
- Renamed the CLI, MCP server, policy directory, package modules, examples, and documentation from the historical software name to Agentic HIL.
- Clarified that HardCI adapters are the reference hardware for Agentic HIL.

## [0.2.1] - 2026-07-09

### Changed

- Python package metadata and install guidance were prepared for the Agentic HIL migration; the CLI command, MCP server name, and repository URL use `agentic-hil`.
- Public project URLs, setup docs, issue templates, and release guidance were prepared for the canonical Agentic HIL repository.
- CLI setup hints and Codex skill-registration text use Agentic HIL naming instead of the legacy project command/prose.
- CI, CodeQL, and Scorecards workflows now run for the `master` default branch.

## [0.2.0] - 2026-07-06

### Added

- Debug sessions now classify abnormal target stops: unexpected breakpoints return `unexpected_breakpoint`, Cortex-M exception/fault contexts return `target_exception` with frame, signal, and suggested next actions, and session status picks up asynchronous stop records.
- Typed GDB/MI debug sessions for the OpenOCD backend: the eleven `debug_*` tools now run real sessions (start/stop/status, breakpoints by symbol or file:line, continue/halt with structured stop reasons, symbol resolution, Intel-HEX memory dumps) against OpenOCD's gdbserver, gated by `debug.allowed_symbols`, `debug.max_dump_size_bytes`, and the existing permission model (raw-command mode disables typed debugging; `load` mode requires flash permission). The session's gdbserver is pinned to `localhost` on an ephemeral port and torn down with the session.
- pyOCD debugger backend (`debugger.type: "pyocd"`) with probe/flash/reset support, `target_type` selection, and pyOCD-specific error classification.

### Changed

- Firmware flashing no longer resets the target by default. Pass `reset_after_flash: true` to `flash_firmware` to request the previous post-flash reset behavior.
- Removed the separate `permissions.allow_reset` policy field; reset remains a typed debugger action instead of a separately gated permission class.

### Fixed

- Hardened the MCP transport and sessions: bounded JSON-RPC message size, COM sessions recover after device disconnects, `com-stdio` relays board output while stdin is idle, artifact validation streams instead of loading whole files, and host serial-port discovery is gated by `allow_com_read`.

## [0.1.0] - 2026-07-05

First public release on PyPI.

### Added

- MCP stdio server exposing bounded tools for probing, flashing, resetting, artifact validation, serial and CAN stimuli/feedback, structured reports, and error classification, gated by a project-local `.agentic-hil/config.yaml` policy.
- Test-adapter layer for sensor/actuator/fault simulation with channel/fault allowlists, seven adapter MCP tools, a JSON-over-stdio bridge protocol, and an NTC simulator example.
- OpenOCD and STM32CubeProgrammer CLI (`stlink`) debugger backends with success-marker confirmation, structured error classification, and per-action logs.
- pytest plugin: installing `agentic_hil` registers the session-scoped `agentic_hil` fixture that drives the same policy-gated tools in CI regression suites, with per-test stimulus-session cleanup, rootdir-anchored config resolution, and skip-when-absent / fail-when-invalid config semantics.
- CLI commands: `init`, `doctor`, `com-ports`, `mcp-stdio`, `com-stdio`, `schema`, `mcp-config`, and `skill-install` for opencode, Claude Code, and Codex.
- Agent-first, no-admin installation flow: `AI_AGENT_QUICKSTART.md`, `llms.txt`, and `TROUBLESHOOTING.md`, built around `agentic_hil` package installs, the `agentic-hil` CLI command, and a repository-URL fallback.
- Nucleo-F446RE demo firmware (`examples/nucleo-f446re_demo/`) exercising the complete loop on real hardware: build → flash → reset → assert on the UART boot banner.
- PyPI trusted publishing with a release-tag/package-version guard and digital attestations; CI matrix across Linux/macOS/Windows and Python 3.10–3.13 with ruff linting.
