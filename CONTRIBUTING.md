# Contributing to AI-HIL

Thanks for helping improve AI-HIL. This project is a local MCP stdio server for safe, structured hardware-in-the-loop access, so changes should keep safety boundaries explicit and easy to audit.

## Development Setup

Use the Node.js toolchain from the repository root:

```bash
npm install
npm test
```

`npm test` builds the TypeScript project and runs the test suite.

## Pull Requests

- Keep changes focused and describe the user-facing behavior they affect.
- Run `npm test` before opening a pull request.
- Add or update tests when changing behavior.
- Do not bypass AI-HIL safety boundaries with raw debugger, flashing, reset, or COM-port access.
- Keep generated artifacts out of commits unless they are intentionally published source artifacts.

## Bug Reports

Use the bug report issue template when possible. Include enough information for someone else to reproduce the setup without guessing:

- AI-HIL version and installation method.
- Host OS, Node.js version, and OpenOCD version.
- Board, debug probe, debugger backend, and serial/COM hardware if relevant.
- Minimal command sequence that triggered the failure.
- Expected behavior and actual behavior.
- Sanitized `.aihil/config.yaml` with local paths, usernames, and secrets removed.
- Relevant `.aihil/reports/last-report.json` content.
- Relevant OpenOCD or COM `log_path` output, sanitized if needed.
- Whether the failure is reproducible after reconnecting the board and rerunning `aihil doctor`.

## Hardware Safety

AI-HIL is designed to let agents perform hardware actions through configured, narrow tools. Contributions should preserve these principles:

- Project-local `.aihil/config.yaml` is the authority for permissions and artifact roots.
- Raw debugger commands and mass erase behavior must remain disabled unless a future design explicitly documents a safe policy.
- Hardware reports and structured errors should stay machine-readable so agents can reason about failures safely.

## Releases

GitHub Releases are created by the release workflow when a strict SemVer tag matching the `package.json` version is pushed. The workflow runs `npm test`, checks the package contents with `npm pack --dry-run`, and then creates a release with generated release notes.
