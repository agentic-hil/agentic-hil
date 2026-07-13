# Contributing to Agentic HIL

Thanks for helping improve Agentic HIL. This project is a local MCP stdio server for safe, structured hardware-in-the-loop access, so changes should keep safety boundaries explicit and easy to audit.

## Development Setup

Use the Python toolchain from the repository root (Python 3.10+):

```bash
python -m pip install -e '.[dev,can]'
ruff check src tests examples
pytest
```

Names: the Python package/install target and Python-facing identifiers such as imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`. The CLI command, repository URL, and MCP server name use `agentic-hil`.

## Pull Requests

- Keep changes focused and describe the user-facing behavior they affect.
- Run `ruff check src tests examples` and `pytest` before opening a pull request.
- Run `python -m build` and inspect the sdist/wheel when package contents, bundled data files (schemas, templates, skills), or release files change.
- Add or update tests when changing behavior.
- Do not bypass Agentic HIL safety boundaries with raw debugger, flashing, reset, COM-port, CAN, or test-adapter access.
- Keep generated artifacts (`build/`, `dist/`, lockfiles) out of commits unless they are intentionally published source artifacts.
- If README onboarding or demo behavior changes, update the affected examples and docs in the same pull request.
- If a change affects Windows setup, confirm the docs still work for explicit OpenOCD paths and COM ports.
- If a change affects platform-specific behavior, describe which hosts were tested and which remain untested.

## Good First Issues

Good first contributions are usually docs, examples, setup diagnostics, error-message clarity, or tests that do not widen hardware permissions. Label beginner-friendly work with `good first issue` once the expected behavior and validation steps are clear.

## Bug Reports

Use the bug report issue template when possible. Include enough information for someone else to reproduce the setup without guessing:

- Agentic HIL version (`agentic-hil --version`) and installation method (uv, pipx, pip, source).
- Host OS, Python version, and OpenOCD or STM32CubeProgrammer version.
- Board, debug probe, debugger backend, and serial/CAN/adapter hardware if relevant.
- Minimal command sequence that triggered the failure.
- Expected behavior and actual behavior.
- Sanitized `.agentic-hil/config.yaml` with local paths, usernames, and secrets removed.
- Relevant `.agentic-hil/reports/last-report.json` content.
- Relevant debugger, COM, CAN, or adapter `log_path` output, sanitized if needed.
- Whether the failure is reproducible after reconnecting the board and rerunning `agentic-hil doctor`.

## Hardware Safety

Agentic HIL is designed to let agents perform hardware actions through configured, narrow tools. Contributions should preserve these principles:

- Project-local `.agentic-hil/config.yaml` is the authority for permissions, artifact roots, and named devices.
- Raw debugger commands and mass erase behavior must remain disabled unless a future design explicitly documents a safe policy.
- Test-adapter channels and faults stay explicit allowlists; never widen them implicitly.
- Hardware reports and structured errors should stay machine-readable so agents can reason about failures safely.

## Releases

See [docs/release-strategy.md](docs/release-strategy.md). In short: update `CHANGELOG.md` and the `pyproject.toml` version together, let CI pass, then create a GitHub Release with a `vX.Y.Z` tag that exactly matches the package version — the publish workflow validates the tag, builds, and publishes to PyPI through trusted publishing.
