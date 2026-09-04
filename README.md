# Agentic HIL

<!-- mcp-name: io.github.agentic-hil/agentic-hil -->

[![PyPI version](https://img.shields.io/pypi/v/agentic-hil)](https://pypi.org/project/agentic-hil/)
[![CI](https://img.shields.io/github/actions/workflow/status/agentic-hil/agentic-hil/ci.yml?branch=master&label=CI)](https://github.com/agentic-hil/agentic-hil/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/agentic-hil/agentic-hil)](LICENSE)

**Your AI agent writes the firmware, flashes it to the board on your desk, drives UART and CAN against it, reads back what the hardware actually did, and fixes what it got wrong; you review the pull request.**

https://github.com/user-attachments/assets/d19b3b24-0250-4226-91c4-61bea65fa4b2

Nothing in that run is staged. One restart after the install line, in a freshly created firmware project, the first sentence makes the agent set the bench up itself and the second makes the board say Hello World and prove it said it: the configuration is created over MCP with the permissions reported out loud, the firmware is written on the spot, `flash_firmware` and `com_read` go through the gate, the twelve bytes come back off the wire, and the plan it pins is run once green and once against a wrong expectation, because a test that cannot fail proves nothing. What remains in the project afterwards is the plan as a reviewable file and the run's own report: lease released, safe state confirmed, nothing quarantined.

## Install

**Linux / macOS** (any shell):

```bash
curl -LsSf https://agentic-hil.github.io/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

**Windows**, in PowerShell:

```powershell
irm https://agentic-hil.github.io/install.ps1 | iex
[Environment]::SetEnvironmentVariable('Path', "$env:USERPROFILE\.local\bin;" + [Environment]::GetEnvironmentVariable('Path', 'User'), 'User')
```

Each second line adds `uv`'s default user bin directory (`$HOME/.local/bin`, or `%USERPROFILE%\.local\bin` on Windows) to your `PATH`, which is where the copy of `uv` the script fetches for itself installs the command. A `pip --user` install often lands there too, but its destination follows the selected interpreter on every platform, so it is `$HOME/.local/bin` only where that interpreter puts its user scripts there: a macOS framework Python installs into an interpreter-specific base instead (commonly `~/Library/Python/X.Y/bin`), and on Windows it is the interpreter's own user scripts directory (for example `%APPDATA%\Python\PythonXY\Scripts`), neither of them `.local\bin`. So the one line reaches a uv install everywhere and a pip install only where the interpreter's user bin happens to be that directory. The installer does not assume any of this, though: it asks the package manager where the command actually went (`uv tool dir --bin` honours `UV_TOOL_BIN_DIR` and XDG placement, and for `pip --user` it asks the selected interpreter itself, via `sysconfig.get_path("scripts", "posix_user")` on Linux and macOS) and when that directory is not already on your `PATH` it prints the exact line to add it. If the directory it names differs from the default above, copy the line the installer printed rather than the one here. Put the POSIX form in your shell profile and open a new shell, while the PowerShell form writes your user `Path` once.

One line installs the package user-local and registers the agent skill and the MCP server for every agent CLI it finds on your `PATH`. **No admin rights required, ever**, and it touches nothing inside any repository. Finding no `claude`, `codex` or `opencode` CLI there, it says so and writes nothing of any agent's: install the agent CLI, then run `agentic-hil agent-install --agent <claude-code|codex|opencode>` yourself, which is the line the installer prints for that case. Then **restart your agent once**, and after that one restart your agent sets this project up itself, at the first hardware question you ask it.

The line above and the checksummed route in [installation](docs/installation.md) run the same installer, and on a machine with nothing here yet both install the same thing, the release from PyPI: the script resolves `agentic-hil[can]` against the package index and carries no branch or git reference at all, and `--version <x.y.z>` pins one exact release instead. On a rerun it repairs what is already installed rather than forcing the public release over it: an installation reporting a `.devN` version (an editable checkout of this repository) is kept untouched, and a uv-managed tool installed from a path, URL or git reference is refreshed from that same recorded source rather than switched to the index. What the checksummed route adds is the script itself, read before it runs: `install.sh` and its `install.sh.sha256` come from the same release, one is checked against the other, and the file that runs is the file you checked.

[The STM32 starter](https://github.com/agentic-hil/stm32-starter) is the shortest way to watch that happen on hardware: three steps on a Nucleo-F446RE, with the firmware, the test plans and one planted defect already in place.

Claude Code can also take the skill from the plugin marketplace:
`/plugin marketplace add agentic-hil/agentic-hil`, then `/plugin install agentic-hil@agentic-hil`.
The plugin carries the skill and nothing else; the MCP server itself still comes from the install line above, which registers a verified absolute executable path outside your repository.

[Installation](docs/installation.md) has every other path: the `cmd.exe` spelling, the repair run (the same line again, which reinstalls in place when `agentic-hil upgrade` itself fails), that checksummed route in full, registering one agent instead of all of them, driving your own package manager, `setup` for a bench that is already attached, the optional extras, upgrading, and every platform and debugger backend. [TROUBLESHOOTING.md](TROUBLESHOOTING.md) covers what to do when something does not start.

A first command that needs no board, in a clone of this repository: `agentic-hil check-plan examples/nucleo-f446re_demo/testconfig.yaml` answers `All 1 test plan(s) load through the reactor's loader.` and exits 0, having loaded no configuration and touched no hardware.

Agentic Hardware-in-the-Loop (Agentic HIL) is a Python package that exposes bounded MCP tools for probing, flashing, resetting, artifact validation, serial and CAN stimulus/feedback, reports, and logs, all without giving an agent arbitrary host or debugger access. Each project has exactly one authoritative configuration stored outside the repository, out of reach of the agent's own file tools.

## Why

A green build is not enough in embedded development: firmware has to behave correctly on the real board. Classic tools automate single steps (flash here, read a log there), but the moment real hardware has to respond, a human is back in the loop. Handing an agent a raw debugger shell or direct serial access instead is neither safe nor reproducible.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/hero-loop-dark.svg">
  <img alt="The Agentic HIL loop: build, flash, stimulate, observe, then diagnose and fix, closing back onto build. Flash and stimulate write to the real board on your bench; observe reads back from it. Your agent runs the loop unattended; you review the pull request." src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/hero-loop.svg">
</picture>

Agentic HIL closes the gap with a small, auditable gate:

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

A device does not exist on this bench until your configuration declares it, and a call naming any other one is refused with `unknown_device` before a driver is opened. On a device it does declare, a generated configuration grants every permission that has a tool behind it and holds `allow_raw_debugger_commands` and `allow_mass_erase` false, the interlocked pair that refuses flashing while either is true — false by construction when MCP `project_config_create` writes a fresh file or first discovers a probe, and the default `agentic-hil init` writes unless your `agentic-hil.config.example.yaml` opens one; a `project_config_create` regenerating an existing bench carries the loaded permissions back instead, either interlock included. `agentic-hil revoke <key>` takes any single grant back and `agentic-hil grant <key>` reopens it, from your own shell; over MCP an agent narrows its own authority and never widens it. Every hardware action is validated, leased machine-wide, and written to a SHA-256 audit chain. The authoritative configuration lives outside the workspace, where the agent cannot edit it. Enforcement sits in the tool rather than in the agent host on purpose: a host's permission system judges shell strings and differs per host, while the bench's permissions judge the hardware action itself and travel with the bench, so the CLI, pytest, CI and the test reactor all walk the same gate. A failed run still gives the bench back: it aborts with its verdict, the recovery action resets and re-reads the target, and the standing quarantine is kept for the one state no later contact can rebuild, a broken audit trail. [The safety model](docs/safety-model.md) is the short version, [the security design](docs/security-design.md) the long one.

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
