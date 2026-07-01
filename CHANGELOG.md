# Changelog

All notable changes to AI-HIL will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning while pre-1.0 changes may still move quickly.

## [Unreleased]

## [0.4.0] - 2026-07-01

### Added

- Added typed OpenOCD + GDB/MI debug-session MCP tools for starting and stopping sessions, managing breakpoints, continuing or halting execution, reading stop reasons, resolving symbols, and dumping symbol memory to Intel HEX.
- Added `debug` configuration for selecting the GDB executable, constraining allowed symbols, and bounding debug symbol dump size.
- Added fake OpenOCD and GDB fixtures plus end-to-end tests for the typed debug workflow.

### Changed

- Made MCP `initialize` report the package version instead of a stale hard-coded server version.
- Tied the agent setup skill to the AI-HIL package version and made the installed CLI authoritative for skill updates.
- Included the setup skill in the npm package so `aihil skill-install` can update version-drifted AI-HIL skills from the installed CLI.
- Added `aihil skill-install` support for opencode, Claude Code, Codex, common aliases, and explicit custom target paths.

### Fixed

- Rejected non-finite `debug.max_dump_size_bytes` values so YAML `.inf` cannot disable the debug dump size policy.

## [0.3.0] - 2026-06-30

### Added

- Added configured CAN MCP tools for listing buses, managing sessions, sending frames, and reading frames through named `bus_id` values.
- Added PEAK, Linux SocketCAN, and process bridge CAN adapter support so agents can run bounded CAN feedback loops without direct host adapter access.
- Added CAN configuration schema fields, permissions, and tests for named project-local bus access.

### Changed

- Replaced `aihil mcp-config` with a shipped portable MCP template at `dist/templates/mcp.json`.
- Documented CAN setup, MCP tool usage, safety boundaries, and troubleshooting for human and agent workflows.

## [0.2.1] - 2026-06-28

### Changed

- Relaxed the AI-HIL runtime engine from Node.js 22.14+ to Node.js 16.16+ while keeping current Node.js LTS recommended.
- Added explicit CI coverage for the current Node.js LTS alias so industrial deployment environments remain visible in release checks.
- Made `aihil mcp-config` emit a Node entrypoint launch instead of relying on the `aihil` command name, avoiding Windows PATH collisions with unrelated executables.
- Clarified the agent install fast path, GitHub direct install fallback, and mandatory agent-side skill installation in setup documentation.
- Kept human setup guidance in `README.md` while moving LLM-specific installation responsibilities to `AGENTS.md`.
- Removed the AI-HIL setup skill from npm package contents so the package remains CLI/MCP-only and agent skills remain separate agent configuration.

### Added

- Added `aihil --help` and `aihil --version` for faster install checks.

## [0.2.0] - 2026-06-28

### Added

- ST-Link debugger backend through STM32CubeProgrammer CLI with safe probe, flash, reset, report, and error-classification behavior.
- Dependency Review and OSSF Scorecard workflows for pull-request and repository hardening signals.
- `npm-shrinkwrap.json` as the authoritative publishable lockfile for the CLI dependency tree.
- Release SBOM generation and artifact attestations for GitHub Release tarballs.
- Shrinkwrap and release metadata validation scripts for CI, release, and npm publish workflows.

### Changed

- Node.js support is now explicit for Node.js 22.14 through 24, with CI covering both supported runtime lines on Linux, macOS, and Windows.
- CodeQL and Scorecards workflows now cover branch and pull-request commits without racing SAST result uploads.
- Release and contributor documentation now describe SBOMs, attestations, npm provenance, and branch-protection expectations.

## [0.1.1] - 2026-06-26

### Added

- README target-audience and safety callouts for first-time visitors.
- Split Quick Start paths for npm installation and the supported Nucleo demo.
- Windows first-run notes for OpenOCD paths and configured COM ports.
- Demo recording checklist for a real NUCLEO-F446RE proof asset.
- Community intake templates for bugs, feature requests, and pull requests.

### Changed

- Release workflow now packs and uploads the npm tarball as a GitHub Release asset.
- npm publishing is prepared for provenance and a Node 22.14+ / npm 11.5.1+ toolchain.
- Troubleshooting now includes Windows-specific OpenOCD and COM-port guidance.

## [0.1.0] - 2026-06-26

### Added

- Initial npm package for the `aihil` CLI.
- MCP stdio server for safe probe, flash, reset, report, and configured COM-port tools.
- Supported first path documentation for STM32 Nucleo-F446RE, ST-Link, and OpenOCD.
- Project-local `.aihil/config.yaml` setup with artifact roots, permissions, reports, and logs.
