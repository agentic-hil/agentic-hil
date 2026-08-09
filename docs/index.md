# Agentic HIL

Agentic HIL is an MCP stdio server for safe embedded firmware development
against local hardware-in-the-loop targets: narrow tools for probing,
flashing, resetting, serial and CAN stimulus and feedback, and structured
reports, all gated by one authoritative bench configuration.

Start with [Installation](installation.md) — two commands and a first real run
on a Nucleo-F446RE — then bind your bench in [Configuration](configuration.md).
The repository's [README](https://github.com/agentic-hil/agentic-hil#readme) is the
short tour; the pages here carry the depth:

- [Installation](installation.md) — install depth, extras, upgrade paths, platform backends.
- [Configuration](configuration.md) — the authoritative bench file: permissions, identities, migration.
- [MCP tools](mcp-tools.md) — the tool inventory and the typical loop.
- [Testing](testing.md) — the test reactor, declarative plans, and the pytest plugin.
- [Safety model](safety-model.md) — locks, runs, incidents and recovery.
- [Security design](security-design.md) — permissions, quarantine and recovery, audit.
- [MCP host configuration](mcp-hosts.md) — wiring the server into Claude Code, Codex, opencode and others.
- [CAN as a service](can-service-design.md) — the design for shared buses with one owner and named participants.
- [Release strategy](release-strategy.md) — versioning, the release gate, and how publishing runs.

This site renders the markdown files in the repository's `docs/` directory;
those files remain the canonical source.
