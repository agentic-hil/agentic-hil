# Agentic HIL

<!-- mcp-name: io.github.agentic-hil/agentic-hil -->

**Your AI agent can develop firmware on its own, because Agentic HIL closes the loop with real hardware.**

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/hero-loop-dark.svg">
  <img alt="The Agentic HIL loop: build, flash, stimulate, observe, then diagnose and fix, closing back onto build. Flash and stimulate write to the real board on your bench; observe reads back from it. Your agent runs the loop unattended; you review the pull request." src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/hero-loop.svg">
</picture>

Agentic Hardware-in-the-Loop (Agentic HIL) is a Python package that exposes bounded MCP tools for probing, flashing, resetting, artifact validation, serial and CAN stimulus/feedback, reports, and logs, all without giving an agent arbitrary host or debugger access. Each project has exactly one authoritative configuration stored outside the repository, out of reach of the agent's own file tools.

## Why

A green build is not enough in embedded development: firmware has to behave correctly on the real board. Classic tools automate single steps (flash here, read a log there), but the moment real hardware has to respond, a human is back in the loop. Handing an agent a raw debugger shell or direct serial access instead is neither safe nor reproducible. Agentic HIL closes the gap with a small, auditable gate:

```
AI agent / CI  ──MCP (stdio)──▶  Agentic HIL  ──authoritative config──▶  OpenOCD / pyOCD / STM32CubeProgrammer
                                    │                        serial ports (pyserial)
                                    │                        CAN (PEAK / SocketCAN / bridge)
                                    ▼
                       structured results, reports, logs
```

Every hardware action is validated against the selected authoritative configuration, executed with timeouts, logged to `.agentic-hil/logs/`, and answered with a structured JSON result (`ok`, `error_type`, `summary`, `likely_causes`, `report_path`, `log_path`) that an agent can act on. What the agent may do at all is per device and per permission, and reaching for a debugger escape hatch is what takes flashing away: [the safety model](docs/safety-model.md) is the short version, [docs/security-design.md](docs/security-design.md) the long one.

## What it drives

Three debugger backends (OpenOCD, pyOCD, and the STM32CubeProgrammer CLI), plus serial ports and CAN (PCAN, SocketCAN, or a custom bridge; several runs can share one bus), on Linux, macOS, and Windows, Python 3.10 or newer, all CI-tested. The worked example in [examples/nucleo-f446re_demo/](examples/nucleo-f446re_demo/) runs the whole loop on an ST Nucleo-F446RE; [installation](docs/installation.md) has every backend and platform in detail.

## Install

The easiest path: copy/paste this prompt to your AI agent:

```text
Read and follow the complete guide at https://github.com/agentic-hil/agentic-hil/blob/master/AI_AGENT_QUICKSTART.md to install Agentic HIL and set it up for this project.
```

Agents follow [AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md): everything installs user-local, **no admin rights required, ever**. The same is true doing it by hand, from the firmware project root:

```bash
pip install --user agentic-hil
agentic-hil setup                 # add --agent codex or --agent opencode for those
```

`setup` installs the agent skill, registers the MCP server with a verified absolute executable path, creates the policy file outside the repository, and runs `doctor`. It prints where that file landed: review it, and take back whatever this bench should not have. Your agent's host will ask you to approve the command once, because it writes the agent's own skill file and MCP registration.

If `pip` is missing, Python is externally managed, or `agentic-hil` does not end up on `PATH`, use `uv tool install agentic-hil` or `pipx install agentic-hil` instead and rerun `setup`. [Installation](docs/installation.md) has the two halves `setup` composes, the optional extras, and upgrading; [TROUBLESHOOTING.md](TROUBLESHOOTING.md) covers what to do when something does not start.

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
