# Contributing to Agentic HIL

Thanks for helping improve Agentic Hardware-in-the-Loop (Agentic HIL). This project is a local MCP stdio server for safe, structured hardware-in-the-loop access, so changes should keep safety boundaries explicit and easy to audit.

## Development Setup

Use the Python toolchain from the repository root (Python 3.10+):

```bash
python -m pip install -e '.[dev,can]'
ruff check src tests examples
pytest
```

Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

### Working on Windows

Part of this suite only runs on POSIX, and that part is where the platform bugs
are. Three changes in two days were verified green on Windows and failed on all
four POSIX runners: a test that assumed a debugger was installed, two that wrote
`"nt"` into the shared `os` module and killed pytest itself, and one that
asserted a refusal its own branch had removed. None was visible locally.

If Docker is available, run the Linux job before opening a pull request:

```bash
python tools/ci_linux.py
```

It clones the committed tree into a container and runs the suite there — about
ninety seconds, and the count matches the Linux CI job exactly. Uncommitted work
is not covered, so commit first.

If you cannot run it, say in the pull request which tests you did not run.

The agent review loop belongs in a container for the same reason and two more:

```bash
python tools/loop_in_container.py --task "..." --max-rounds 3
```

`tools/agent_review_loop.py` drives Claude Code and Codex against this
repository. Run on Windows, the reviewer's test runs die with
`PermissionError: [WinError 5]` on temporary directories a previous round
hardened, and Codex under its own sandbox could not run the suite at all. In a
Linux container both go away, and the container is itself the isolation
boundary, so Codex runs inside it with its sandbox off. The wrapper mounts this
repository read-write — the commits are the product and they land here — hands
the two CLIs their stored logins as individual read-only files, keeps every
temporary directory on the container's own filesystem, and deletes the
container's home afterwards. Every option it does not consume goes to
`agent_review_loop.py` unchanged, except the four that decide what the container
isolates — `--repo`, `--codex-sandbox`, `--review-checkout` and
`--review-checkout-dir`. Those it sets itself and refuses to forward, because
forwarding one is how the isolation stops being the isolation that was measured.
It also refuses a repository that would carry your profile into the read-write
mount, and it exits non-zero when the container's home outlives the run, since
that home is the one place a copy of a login can be. Nothing is pushed and no
pull request is opened.

## Pull Requests

- Keep changes focused and describe the user-facing behavior they affect.
- Run `ruff check src tests examples` and `pytest` before opening a pull request.
- Run `python -m build` and inspect the sdist/wheel when package contents, bundled data files (schemas, templates, skills), or release files change.
- Add or update tests when changing behavior.
- Do not bypass Agentic HIL safety boundaries with raw debugger, flashing, reset, COM-port, or CAN access.
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
- Board, debug probe, debugger backend, and serial/CAN hardware if relevant.
- Minimal command sequence that triggered the failure.
- Expected behavior and actual behavior.
- Sanitized discovered authoritative config or `AGENTIC_HIL_CONFIG` override, with local paths, usernames, and secrets removed.
- Relevant `.agentic-hil/reports/last-report.json` content.
- Relevant debugger, COM, or CAN `log_path` output, sanitized if needed.
- Whether the failure is reproducible after reconnecting the board and rerunning `agentic-hil doctor`.

## Hardware Safety

Agentic HIL is designed to let agents perform hardware actions through configured, narrow tools. Contributions should preserve these principles:

- Agentic HIL discovers one operator-controlled config outside the repository; `AGENTIC_HIL_CONFIG` may provide an absolute-path override, and mandatory `workspace_root` binds either file to the exact project.
- Raw debugger commands and mass erase behavior must remain disabled unless explicitly authorized by a reviewed design.
- Hardware reports and structured errors should stay machine-readable so agents can reason about failures safely.

## Releases

See [docs/release-strategy.md](docs/release-strategy.md). In short: bump the version in every position `python tools/check_version_consistency.py --list` prints — do not work from a list you remember, that is what drifted — let CI pass, then create a GitHub Release with a `vX.Y.Z` tag that exactly matches the package version. The same check runs on every pull request, so a position left behind is red before the release exists; the publish workflow re-runs it with the tag, builds, and publishes to PyPI through trusted publishing.
