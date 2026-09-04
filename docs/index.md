# Agentic HIL

Agentic HIL is an MCP stdio server that lets a coding agent develop firmware
safely on the real board behind a debug probe: narrow tools for probing,
flashing, resetting, serial and CAN stimulus and feedback, and structured
reports, all gated by one authoritative bench configuration. The run on the
board is what decides whether the work is done, and the report it writes is
what a reviewer reads. The supported unit is the probe with its backend rather
than a board, so any board behind such a probe runs the same software.

Start with [Installation](installation.md) (two commands and a first real run
on a Nucleo-F446RE, the reference this path is proved on), then bind your bench
in [Configuration](configuration.md).
The repository's [README](https://github.com/agentic-hil/agentic-hil#readme) is the
short tour; the pages here carry the depth:

- [Installation](installation.md): install depth, extras, upgrade paths, platform backends.
- [Configuration](configuration.md), the authoritative bench file: permissions, identities, migration.
- [MCP tools](mcp-tools.md): the tool inventory and the typical loop.
- [Testing](testing.md): the test reactor, declarative plans, and the pytest plugin.
- [Test plan contract](test-plan-contract.md): what a plan may state, what only the bench configuration binds, and what a run attests.
- [CI examples](ci-examples.md): worked GitHub Actions and GitLab CI files that run a plan on a self-hosted bench and keep the evidence.
- [Safety model](safety-model.md): locks, runs, incidents and recovery.
- [Security design](security-design.md): permissions, quarantine and recovery, audit.
- [MCP host configuration](mcp-hosts.md): wiring the server into Claude Code, Codex, opencode and others.
- [CAN as a service](can-service-design.md): the design for shared buses with one owner and named participants.
- [GitHub Action](github-action-design.md), the design for `agentic-hil/run`: one plan, one self-hosted bench, one evidence bundle.
- [Release strategy](release-strategy.md): versioning, the release gate, and how publishing runs.

This site renders the markdown files in the repository's `docs/` directory;
those files remain the canonical source.
