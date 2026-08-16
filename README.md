# Agentic HIL

<!-- mcp-name: io.github.agentic-hil/agentic-hil -->

**Your AI agent can develop firmware on its own, because Agentic HIL closes the loop with real hardware.**

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/hero-loop-dark.svg">
  <img alt="The Agentic HIL loop: build, flash, stimulate, observe, then diagnose and fix, closing back onto build. Flash and stimulate write to the real board on your bench; observe reads back from it. Your agent runs the loop unattended; you review the pull request." src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/hero-loop.svg">
</picture>

Agentic Hardware-in-the-Loop (Agentic HIL) is a Python package that exposes bounded MCP tools for probing, flashing, resetting, artifact validation, serial and CAN stimulus/feedback, reports, and logs, all without giving an agent arbitrary host or debugger access. Each project has exactly one authoritative configuration stored outside the repository, out of reach of the agent's own file tools.

## Install

**Linux / macOS** (any shell):

```bash
curl -LsSf https://agentic-hil.github.io/install.sh | sh
```

**Windows** (any shell: PowerShell, `cmd.exe`, or the Run box):

```powershell
powershell -c "irm https://agentic-hil.github.io/install.ps1|iex"
```

One line installs the package user-local (through `uv` where it exists, `pip --user` otherwise) and registers the agent skill and the MCP server for every agent CLI it finds on your `PATH`. **No admin rights required, ever**, and it touches nothing inside any repository: no project configuration is written, no shell profile is edited. Then **restart your agent once**, and after that one restart your agent sets this project up itself, at the first hardware question you ask it.

Prefer to read before you run? Take the script and its SHA-256 from the same release, check one against the other, and run the file you checked:

```bash
curl -LsSfO https://github.com/agentic-hil/agentic-hil/releases/latest/download/install.sh
curl -LsSfO https://github.com/agentic-hil/agentic-hil/releases/latest/download/install.sh.sha256
sha256sum -c install.sh.sha256 && sh install.sh
```

```powershell
iwr -OutFile install.ps1 https://github.com/agentic-hil/agentic-hil/releases/latest/download/install.ps1
iwr -OutFile install.ps1.sha256 https://github.com/agentic-hil/agentic-hil/releases/latest/download/install.ps1.sha256
if ((Get-FileHash install.ps1).Hash -eq (-split (Get-Content install.ps1.sha256))[0]) { .\install.ps1 }
```

The one-line form above installs from the default branch, which is where a fix lands first; this form installs the release, which is the pair a checksum can speak for.

Pass `--agent claude-code` (or `codex`, `opencode`) to register one agent instead of all of them, `--help` for the rest; piped, that reads `| sh -s -- --agent claude-code`. If you would rather drive your own package manager, the same two halves by hand:

```bash
uv tool install "agentic-hil[can]"               # or: pip install --user "agentic-hil[can]"
agentic-hil agent-install --agent claude-code    # or: codex / opencode
```

[Installation](docs/installation.md) has `setup` for a bench that is already attached, the optional extras, upgrading, and every platform and debugger backend; [TROUBLESHOOTING.md](TROUBLESHOOTING.md) covers what to do when something does not start.

## Why

A green build is not enough in embedded development: firmware has to behave correctly on the real board. Classic tools automate single steps (flash here, read a log there), but the moment real hardware has to respond, a human is back in the loop. Handing an agent a raw debugger shell or direct serial access instead is neither safe nor reproducible. Agentic HIL closes the gap with a small, auditable gate:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/architecture-dark.svg">
  <img alt="An AI agent or CI reaches Agentic HIL over MCP stdio only. The authoritative configuration, owned by the operator outside the workspace, gates every action. Agentic HIL drives debug probes (OpenOCD, pyOCD, STM32CubeProgrammer), serial ports, and CAN buses shared through the broker, and answers with structured results, reports, and a SHA-256 audit chain." src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/architecture.svg">
</picture>

Every hardware action is validated against the selected authoritative configuration, executed with timeouts, logged to `.agentic-hil/logs/`, and answered with a structured JSON result (`ok`, `error_type`, `summary`, `likely_causes`, `report_path`, `log_path`) that an agent can act on. What the agent may do at all is per device and per permission, and reaching for a debugger escape hatch is what takes flashing away.

## What it drives

Three debugger backends (OpenOCD, pyOCD, and the STM32CubeProgrammer CLI), plus serial ports and CAN (PCAN, SocketCAN, or a custom bridge; several runs can share one bus), on Linux, macOS, and Windows, Python 3.10 or newer, all CI-tested. The worked example in [examples/nucleo-f446re_demo/](examples/nucleo-f446re_demo/) runs the whole loop on an ST Nucleo-F446RE; [installation](docs/installation.md) has every backend and platform in detail.

## The test reactor

One YAML plan drives the whole bench: flash, reset, write, read with a comparator (exact text, a pattern, or a numeric range over a captured value), delays, and sessions that close themselves. Plans name logical devices; the bench configuration binds them to real hardware, so the same plan runs unchanged on every machine that has one. A failing step aborts the run, and the bench recovers itself: reap, reset into halt, probe, all attested in the run result. [How plans work.](docs/testing.md)

## Security by construction

Deny-by-default permissions per device, every hardware action validated, leased machine-wide, and written to a SHA-256 audit chain. The authoritative configuration lives outside the workspace, where the agent cannot edit it. Enforcement sits in the tool rather than in the agent host on purpose: a host's permission system judges shell strings and differs per host, while the bench's permissions judge the hardware action itself and travel with the bench, so the CLI, pytest, CI and the test reactor all walk the same gate. A failed run still gives the bench back: it aborts with its verdict, the recovery action resets and re-reads the target, and the standing quarantine is kept for the one state no later contact can rebuild, a broken audit trail. [The safety model](docs/safety-model.md) is the short version, [the security design](docs/security-design.md) the long one.

## Quickstart: one real run

The worked example is a firmware project of its own. Plug the board in, build it, and point Agentic HIL at it from that directory:

```bash
cd examples/nucleo-f446re_demo
cmake --preset Debug && cmake --build --preset Debug   # → build/Debug/nucleo-f446re_demo.elf
agentic-hil setup --agent claude-code                  # or: codex / opencode
agentic-hil doctor
```

`doctor` checks the configuration against the attached bench and names what it finds (a missing toolchain, an unreachable probe, a target type this host cannot resolve) before anything is flashed. If the board arrived after `setup` ran, `agentic-hil adopt-hardware` fills in the probe serial, the backend executable and the COM device it left unset (`--dry-run` shows the plan first).

With the MCP host started from that directory, the agent drives four calls:

```text
flash_firmware     {"image_path": "build/Debug/nucleo-f446re_demo.elf"}
com_session_start  {"port_id": "dut_uart"}
reset_target       {"mode": "run"}
com_read           {"port_id": "dut_uart", "wait_timeout_s": 5}
→ feedback contains "Hello World"
```

The same loop runs headless as a pytest regression: `pytest tests/` in that directory flashes the ELF, resets the target and asserts the boot banner on the UART. [examples/nucleo-f446re_demo/](examples/nucleo-f446re_demo/) walks through both, and [docs/testing.md](docs/testing.md) covers writing the run down as a reviewable YAML plan instead.

## Where the depth lives

| If you want | Read |
|---|---|
| to install, upgrade, add CAN or pyOCD, or look up a command | [docs/installation.md](docs/installation.md) |
| what the authoritative configuration declares and who may change it | [docs/configuration.md](docs/configuration.md) |
| the complete MCP tool surface and how a run is composed from it | [docs/mcp-tools.md](docs/mcp-tools.md) |
| to register the server in a specific MCP host | [docs/mcp-hosts.md](docs/mcp-hosts.md) |
| to write hardware tests (YAML plans or pytest) | [docs/testing.md](docs/testing.md) |
| why it is safe to leave an agent alone with the bench | [docs/safety-model.md](docs/safety-model.md) and [docs/security-design.md](docs/security-design.md) |
| a failure diagnosed | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| to point your agent at this repository | [AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) and [AGENTS.md](AGENTS.md) |

Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check src tests evals tools
pytest
python -m build
twine check dist/*
```

The package is configured for PyPI publishing through GitHub trusted publishing in `.github/workflows/workflow.yml`. Contribution guidelines: [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Policy bypasses are treated as vulnerabilities; see [SECURITY.md](SECURITY.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
