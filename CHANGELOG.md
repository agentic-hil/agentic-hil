# Changelog

All notable changes to Agentic HIL will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning while pre-1.0 changes may still move quickly.

## [Unreleased]

## [0.2.1] - 2026-07-09

### Changed

- Python package metadata and install guidance now use `agentic_hil`; the CLI command, MCP server name, and repository URL remain `agentic-hil`.
- Public project URLs, setup docs, issue templates, and release guidance now point at `hp-8472/agentic-hil`.
- CLI setup hints and Codex skill-registration text use Agentic HIL naming instead of legacy Agentic HIL command/prose.
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

- MCP stdio server exposing 35 bounded tools for probing, flashing, resetting, artifact validation, serial and CAN stimuli/feedback, structured reports, and error classification, gated by a project-local `.agentic-hil/config.yaml` policy.
- OpenOCD and STM32CubeProgrammer CLI (`stlink`) debugger backends with success-marker confirmation, structured error classification, and per-action logs.
- Test-adapter layer for sensor/actuator/fault simulation: an `adapters:` config section with channel and fault allowlists enforced before anything reaches the adapter, seven `adapter_*` MCP tools, a JSON-over-stdio bridge protocol for physical adapters and simulators, and a bundled NTC simulator (`examples/adapters/sim_ntc_adapter.py`).
- pytest plugin: installing `agentic_hil` registers the session-scoped `agentic_hil` fixture that drives the same policy-gated tools in CI regression suites, with per-test stimulus-session cleanup, rootdir-anchored config resolution, and skip-when-absent / fail-when-invalid config semantics.
- CLI commands: `init`, `doctor`, `com-ports`, `mcp-stdio`, `com-stdio`, `schema`, `mcp-config`, and `skill-install` for opencode, Claude Code, and Codex.
- Agent-first, no-admin installation flow: `AI_AGENT_QUICKSTART.md`, `llms.txt`, and `TROUBLESHOOTING.md`, built around `agentic_hil` package installs, the `agentic-hil` CLI command, and a repository-URL fallback.
- Nucleo-F446RE demo firmware (`examples/nucleo-f446re_demo/`) exercising the complete loop on real hardware: build → flash → reset → assert on the UART boot banner.
- PyPI trusted publishing with a release-tag/package-version guard and digital attestations; CI matrix across Linux/macOS/Windows and Python 3.10–3.13 with ruff linting.
