# Let your coding agent develop firmware on your board

You already use a coding agent to edit firmware. You want it to try the image
on the board plugged into your computer, read what the board actually does,
and fix failures itself. **Agentic Hardware-in-the-Loop (Agentic HIL)** gives
that agent local hardware tools over MCP so it can complete that loop.

Your existing agent builds with your project's toolchain, flashes the image,
resets the target, sends a UART command and checks the answer. If the answer
fails the test, it reads the report, changes the firmware, rebuilds and runs
the check again. The agent edits and builds; Agentic HIL supplies the hardware
tools and test reports. A successful build alone does not establish that the
firmware works on the board.

## Begin in your existing firmware project

Keep your firmware repository and coding agent. You need the target's build
toolchain and a supported debug probe/backend installed on the same computer,
plus a serial connection for UART feedback. Agentic HIL does not install the
compiler or replace your build system.

Install once, with your agent CLI available on `PATH`:

Linux or macOS:

```bash
curl -LsSf https://agentic-hil.github.io/install.sh | sh
```

Windows, in PowerShell:

```powershell
irm https://agentic-hil.github.io/install.ps1 | iex
```

The installer registers the skill and MCP server for the Claude Code, Codex
and opencode CLIs it finds. **Restart your agent once**, open your firmware
project and give it a concrete task, for example:

> Set up the board attached to this computer for this firmware project.
> Build the firmware, flash it, and check that STATUS over UART answers READY.
> If the response is wrong, diagnose and fix the firmware, then rerun the check.
> Report the bench permissions and keep the test plan and run evidence.

Replace `STATUS` and `READY` with your firmware's actual
protocol. After the restart, the agent can create the project's bench
configuration over MCP and report the devices and permissions. That
configuration lives outside the repository and binds the intended probe,
target and serial port. If a required action is refused, the agent reports
the refusal; the operator decides whether to grant it.

[Installation](../installation.md) covers registration when your CLI was not
found, manual setup and the checksummed installer route. The
[agent quickstart](https://github.com/agentic-hil/agentic-hil/blob/master/AI_AGENT_QUICKSTART.md)
has the setup fallback path. [Configuration](../configuration.md) explains
how to bind a bench that automatic discovery cannot resolve.

## Share your own-board result

Try one observable check in your existing firmware project, then share a
[first run report](https://github.com/agentic-hil/agentic-hil/issues/new?template=first-run.yml).
A green run, a failing check, or a stop during installation or setup is welcome.
Name your coding agent, board with its probe/backend, host OS and version, and
how you flashed firmware before. Optionally describe the last real firmware bug
you wanted the agent to investigate.

If the run produced a report, the form explains how to prepare an evidence
bundle. Review it and remove secrets and private paths before attaching it or
linking it. If no report was produced, say where setup stopped instead.

## Check your probe and backend

Support depends on the debug probe, backend and configured target, rather
than a certified board list:

| Probe | Backend |
|---|---|
| ST-Link | OpenOCD or STM32CubeProgrammer CLI |
| CMSIS-DAP | pyOCD |

OpenOCD needs the appropriate interface and target scripts. pyOCD needs the
`agentic-hil[pyocd]` extra and a supported target type; many vendor targets
also need a separately installed CMSIS device-family pack. `agentic-hil doctor`
checks the configured backend and target support. See
[backend prerequisites](../installation.md#platforms-and-debugger-backends)
and [Support](../support.md) before assuming your bench is covered.

The software supports Linux, macOS and Windows. The recorded hardware runs
below cover Linux and Windows with ST-Link on a Nucleo-F446RE; they do not
prove every supported host, probe or target combination.

## What the recorded loop shows

The [STM32 starter](https://github.com/agentic-hil/stm32-starter) is the shortest
worked path: a Nucleo-F446RE project with firmware, three test plans and one
planted defect. Its diagnostic test sends `DIAG ON`, expects a self-test fault,
and receives a healthy status instead. The report names `comparator_unmet`
and preserves both the expectation and the actual UART response. Fixing the
parser's command string, rebuilding and rerunning makes all three plans pass.

The published [Linux record](https://github.com/agentic-hil/stm32-starter/blob/main/validation/2026-09-15-newcomer-linux/README.md)
uses Agentic HIL 0.21.5 with OpenOCD on 2026-09-15. The
[Windows record](https://github.com/agentic-hil/stm32-starter/blob/main/validation/2026-09-03-bench-windows/README.md)
uses 0.21.1 with STM32CubeProgrammer CLI on 2026-09-03. These are evidence
for that specific starter and bench, not a guarantee that an agent can fix
every firmware failure.

Follow the starter's README if you have its reference hardware. For your own
project, start with one observable firmware response and a test plan that
states the expected answer. The existing walkthroughs cover each part:

- [Flash and reset](flash-and-reset.md): image verification, probe access and recorded flash/reset results.
- [UART and CAN feedback](uart-and-can.md): the failing UART test and firmware fix, plus CAN support and its evidence limits. No published record on that page shows CAN driving a board.
- [Plans without a board](check-plan.md): validate the plan before hardware access. A passing plan check does not test the firmware or wiring.

Agents use MCP tools, and repeatable checks use declarative plans or the
[pytest integration](../testing.md). No object SDK is required. For sensor
stimulus and physical fault injection, HardCI adapters are the first-party
reference hardware; the UART starter loop above needs no such adapter.
