# Agentic HIL

<!-- mcp-name: io.github.agentic-hil/agentic-hil -->

[![PyPI version](https://img.shields.io/pypi/v/agentic-hil)](https://pypi.org/project/agentic-hil/)
[![CI](https://img.shields.io/github/actions/workflow/status/agentic-hil/agentic-hil/ci.yml?branch=master&label=CI)](https://github.com/agentic-hil/agentic-hil/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/agentic-hil/agentic-hil)](LICENSE)

**Connect your AI to reality.**

Let your agent develop, test, and improve firmware autonomously. In the real world. Agentic HIL makes this possible as a hard gate: it enforces strict safety boundaries and verifies the results on real hardware.

https://github.com/user-attachments/assets/8d39ba93-beeb-484e-b9e9-d9ce79538523

Watch the agent set up a fresh firmware project, write the firmware, and make the board say Hello World after installation and one restart. The recording shows real hardware feedback through the gate. The agent saves a test plan and runs it with both a correct and a deliberately wrong expectation. You get the plan, a report, and evidence that the test can pass and fail. The run ends with the lease released, the safe state confirmed, and nothing quarantined.

## Get started

Install Agentic HIL for your user with one command. **No admin rights required.**

**Linux / macOS** (any shell):

```bash
curl -LsSf https://agentic-hil.github.io/install.sh | sh
```

**Windows**, in PowerShell:

```powershell
irm https://agentic-hil.github.io/install.ps1 | iex
```

The installer registers the skill and MCP server for every supported agent CLI it finds on your `PATH`. It keeps the installation outside your repositories. **Restart your agent once**, then ask your first hardware question. Your agent sets up the project's bench through MCP.

If no `claude`, `codex`, or `opencode` CLI is found, the installer prints the next step. Install your agent CLI, then run `agentic-hil agent-install --agent <claude-code|codex|opencode>` to register it.

The installer asks the package manager where its user bin directory is and adds that directory to your shell startup files or Windows user `Path` when needed. Open a new shell to use the command. Pass `--no-path` (`-NoPath` in PowerShell) to manage that change yourself; the installer prints the line to add.

A fresh installation gets `agentic-hil[can]` from PyPI. Use `--version <x.y.z>` to pin a release. Rerunning the installer repairs an existing installation: editable checkouts reporting a `.devN` version are kept, and uv tools installed from a path, URL, or git reference are refreshed from that recorded source. To inspect and verify the installer before running it, follow the checksummed route in [installation](docs/installation.md): download the script and its checksum from the same release, check the file, then run that file.

For a guided first run, try [the STM32 starter](https://github.com/agentic-hil/stm32-starter). It has the firmware, test plans, and a planted defect ready for a three-step walkthrough on a Nucleo-F446RE.

Claude Code can also install the skill from the plugin marketplace:
`/plugin marketplace add agentic-hil/agentic-hil`, then `/plugin install agentic-hil@agentic-hil`.
The install command above still provides and registers the MCP server, with a verified absolute executable path outside your repository.

See [installation](docs/installation.md) for `cmd.exe`, registering one agent, using your own package manager, setting up an attached bench, optional components, and upgrades. The same installer command can repair an installation when `agentic-hil upgrade` fails. [Troubleshooting](TROUBLESHOOTING.md) covers startup problems.

In a clone of this repository, try a first check without a board:

```bash
agentic-hil check-plan examples/nucleo-f446re_demo/testconfig.yaml
```

It answers `All 1 test plan(s) load through the reactor's loader.` and exits 0, without loading a bench configuration or touching hardware.

## Let your agent close the loop

You set the goal. Your agent writes and builds the firmware. Agentic HIL puts it on the board, exercises the device, and checks its responses against the test criteria. When a test fails, the agent gets concrete results it can use to diagnose the problem, change the code, and try again.

Agentic HIL supplies the hardware execution and feedback that make this development loop possible. Much of the flashing, testing, and checking you would do at the bench becomes part of the agent's workflow. You review the code alongside a record of what ran, what passed, and what failed, and focus your attention on requirements, design decisions, and test coverage.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/hero-loop-dark.svg">
  <img alt="Your agent builds firmware. Agentic HIL flashes it, exercises the board, and checks its responses within configured safety boundaries. The agent uses the results to diagnose, fix, and rebuild. You review the code and the hardware test results." src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/hero-loop.svg">
</picture>

Agentic Hardware-in-the-Loop (Agentic HIL) connects coding agents to the bench through MCP tools and CI suites through a pytest plugin. Setup is available for Claude Code, Codex, and OpenCode.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/architecture-dark.svg">
  <img alt="Your coding agent sends hardware requests through MCP. Agentic HIL enforces the operator's configuration, drives debug probes, serial ports, and CAN buses, and returns structured results and reports. The configuration and audit record stay outside the agent's workspace." src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/architecture.svg">
</picture>

Each hardware action returns a structured result with its outcome, diagnostics, and report and log paths. Your agent gets feedback it can act on, and you can trace the result back to the recorded run.

## Bring your bench into the loop

Agentic HIL connects to your probe through its backend: ST-Link through OpenOCD or the STM32CubeProgrammer CLI, and CMSIS-DAP probes through pyOCD. It also flashes and resets ESP32 targets through esptool and their USB-UART bridge, without opening a debug session. Hardware support follows the probe and backend.

Send commands and read responses over serial ports and CAN buses using PCAN, SocketCAN, or a custom bridge. Several runs can share a CAN bus through the broker.

Agentic HIL runs on Linux, macOS, and Windows with Python 3.10 or newer, with CI coverage across all three platforms. The [Nucleo-F446RE example](examples/nucleo-f446re_demo/) takes you through a complete run on the board used to demonstrate the loop. See [installation](docs/installation.md) for backend and platform details.

## Put the firmware to the test

Write down what the device should do in a reviewable YAML test plan. The test reactor validates the plan before touching hardware, then flashes, resets, and exercises the device in the order you specify. Checks compare actual responses with exact values, patterns, or numeric ranges. A check passes when the device meets that expectation.

Plans name logical devices, and the bench configuration connects them to real hardware. Use the same plan at your desk and in CI. A failed step stops the run; sessions are cleaned up, and configured recovery actions and their outcomes are recorded with the test results. [Write your first hardware test.](docs/testing.md)

## Set boundaries that hold

You decide which devices and actions your agent may use. Agentic HIL rejects undeclared devices before opening a driver. It enforces permissions, validates requests, applies timeouts, and records what happened. The CLI, MCP tools, pytest plugin, and test reactor all pass through the same gate.

The authoritative configuration lives outside the workspace, beyond the agent's own file tools. Review the permissions it grants. Use `agentic-hil revoke <key>` or `agentic-hil grant <key>` from your own shell to change individual grants. Over MCP, an agent can only narrow its authority. Flashing is refused while either `allow_raw_debugger_commands` or `allow_mass_erase` is enabled.

Device leases protect runs from interference across the machine, and a SHA-256 audit chain makes the hardware record tamper-evident. A failed run keeps its failed verdict while configured recovery works to restore the bench and records the outcome. A broken audit trail holds the bench until operator recovery, because another hardware test cannot rebuild missing evidence.

Read [the safety model](docs/safety-model.md) for the overview and [the security design](docs/security-design.md) for the full reasoning.

## Try it on a real board

Start with the Nucleo-F446RE demo. Connect the board, build the firmware, and set up Agentic HIL from the example's directory:

```bash
cd examples/nucleo-f446re_demo
cmake --preset Debug && cmake --build --preset Debug   # → build/Debug/nucleo-f446re_demo.elf
agentic-hil setup --agent claude-code                  # or: codex / opencode
agentic-hil doctor
```

`doctor` checks the configuration against the attached bench and reports missing tools or hardware before you flash. If you connected the board after setup, `agentic-hil adopt-hardware` fills in the probe identity, backend executable, and COM device left unset. Use `--dry-run` to see the proposed changes first. On a host with OpenOCD and no STM32CubeProgrammer, the host's USB inventory can identify an attached ST-Link. Use `--probe-id <serial>` to select one when several probes are attached.

Run the example's hardware test plan:

```bash
agentic-hil test-reactor --test-config testconfig.yaml
```

The [plan](examples/nucleo-f446re_demo/testconfig.yaml) flashes the firmware, opens the serial port, resets the board, and checks the boot output for `Hello World`. A missing or wrong boot banner fails the test and returns the device output for diagnosis. The run closes the sessions it opened, including when a step fails.

Your agent can drive the same steps through [MCP tools](docs/mcp-tools.md). Run `pytest tests/` from the example directory to put the boot check in a regression suite. The [example guide](examples/nucleo-f446re_demo/) covers all three paths, and [hardware testing](docs/testing.md) shows how to write plans for your own device.

## Go deeper

| Your next step | Read |
|---|---|
| Install, upgrade, or add CAN, pyOCD, or esptool support | [docs/installation.md](docs/installation.md) |
| Define the bench and its permissions | [docs/configuration.md](docs/configuration.md) |
| Drive hardware through MCP | [docs/mcp-tools.md](docs/mcp-tools.md) |
| Connect a specific MCP host | [docs/mcp-hosts.md](docs/mcp-hosts.md) |
| Write hardware tests with YAML or pytest | [docs/testing.md](docs/testing.md) |
| Understand how safety boundaries and recovery work | [docs/safety-model.md](docs/safety-model.md) and [docs/security-design.md](docs/security-design.md) |
| Find platform and response-time commitments | [docs/support.md](docs/support.md) |
| Diagnose a failed run | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| Set up your agent for this project | [AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) and [AGENTS.md](AGENTS.md) |

Use `agentic-hil` for the package installation, CLI command, repository URL, and MCP server name. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

## Development

To work on Agentic HIL itself:

```bash
python -m pip install -e '.[dev]'
ruff check src tests evals tools
pytest
python -m build
twine check dist/*
```

Releases publish to PyPI through GitHub trusted publishing in `.github/workflows/workflow.yml`. See [CONTRIBUTING.md](CONTRIBUTING.md) to contribute.

## Security

Policy bypasses are treated as vulnerabilities; see [SECURITY.md](SECURITY.md).

## Support

Linux, macOS, and Windows have equal support. Hardware support follows the probe and backend: ST-Link through OpenOCD or STM32CubeProgrammer, CMSIS-DAP through pyOCD, and ESP32 USB-UART bridges through esptool.

Issues receive a response within 24 hours on workdays, and security reports within seven days. See [the support policy](docs/support.md) for the full scope and commitments.

Ask questions in [Discussions Q&A](https://github.com/agentic-hil/agentic-hil/discussions/categories/q-a), share a run in [Show and tell](https://github.com/agentic-hil/agentic-hil/discussions/categories/show-and-tell), or submit [your first run report](https://github.com/agentic-hil/agentic-hil/issues/new?template=first-run.yml), whether it passed or failed.

## License

Apache-2.0. See [LICENSE](LICENSE).
