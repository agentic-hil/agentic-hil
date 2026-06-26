# Changelog

All notable changes to AI-HIL will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning while pre-1.0 changes may still move quickly.

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
