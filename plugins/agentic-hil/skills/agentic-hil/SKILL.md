---
name: agentic-hil
description: Use for any embedded firmware or hardware request in this project — flashing, resetting, probing, debugging, reading UART or CAN traffic, collecting firmware artifacts or test reports — and for configuring Agentic HIL, the safe local MCP bridge that performs them. Use it instead of invoking a debugger, serial device, or CAN adapter directly.
metadata:
  origin: Agentic HIL
  agentic_hil_version: "0.7.0"
---

# Agentic HIL

Agentic HIL is this project's hardware gate. Every request that would touch the
target board goes through its MCP tools, which enforce the authoritative
configuration's workspace binding, permissions, and limits. A direct debugger,
serial, or CAN invocation bypasses that gate.

## Route firmware work to these tools

| The request | The tools |
|---|---|
| Flash, program, or load firmware | `flash_firmware` |
| Reset or reboot the target | `reset_target` |
| Is the board connected, which probe, what target | `probe_target`, `debugger_info`, `debugger_probes_list` |
| Halt, continue, breakpoints, stop reason | `debug_start_session`, `debug_halt`, `debug_continue`, `debug_set_breakpoint`, `debug_list_breakpoints`, `debug_clear_breakpoints`, `debug_get_stop_reason`, `debug_get_session_status`, `debug_stop_session` |
| Read a variable, symbol, or memory region | `debug_symbol_info`, `debug_dump_symbol_ihex` |
| Serial console, boot log, UART traffic | `com_ports_list`, `com_session_start`, `com_read`, `com_write`, `com_session_stop` |
| CAN frames | `can_buses_list`, `can_read`, `can_send`, `can_session_start`, `can_session_stop` |
| Hand a build artifact to the target workflow | `artifact_upload` |
| What happened in the last run, why did it fail | `get_last_report`, `classify_last_error` |

Do not substitute `openocd`, `pyocd`, `st-flash`, `st-util`, `JLinkExe`, `gdb`,
`screen`, `minicom`, `picocom`, `cansend`, or `candump` for these tools, and do
not open `/dev/tty*`, `COM*`, or a SocketCAN interface yourself. Reach for a raw
command only when no Agentic HIL tool covers the request, and say plainly that
you are stepping outside the gate.

## Answer hardware questions by calling a tool

Reading the configuration file tells you what is *configured*; it never tells
you what the hardware is doing, so it does not answer the question. Call the
tool that matches the question — `probe_target`, `debugger_probes_list`,
`com_ports_list`, `get_last_report` — and report what it returns.

A refusal is an answer, and it is not the end of the job. When the authoritative
configuration denies an action the tool returns `permission_denied`. Do not stop
there: finish through the gate. `get_last_report` and `classify_last_error` say
what the attempt recorded and why it failed, and `debugger_info` or
`probe_target` say whether the hardware would have been reachable at all.

Report those findings together with the refusal, name the permission that is
denied, and ask the user before changing it. Never work around it with a raw
command, with the configuration file, or with the CLI.

## Reach the tools through the MCP server

Use the `agentic-hil` MCP server, not the command line. That is the interface
`setup` registers, the one an operator can reason about, and the one whose calls
are coordinated and reported as hardware actions. Agent CLIs load MCP servers
when they start, so when the server is missing — typically in the session that
just installed it — say so and ask the user to restart the agent rather than
answering the hardware question another way.

The CLI installs, configures, and diagnoses: `agentic-hil init`, `agentic-hil
setup`, and `agentic-hil doctor` report what is configured and whether hardware
access is permitted. None of them is a hardware call.

## Configuration and regression runs

One authoritative configuration file holds the workspace binding, hardware
resources, permissions, and limits. It lives under the current user's Agentic
HIL projects directory, never inside the repository; `AGENTIC_HIL_CONFIG` may
give an explicit absolute-path override. `agentic-hil init` creates it and
prints its path; ask the operator to review any permission change.

For automated regression runs the installed package registers a pytest plugin:
the `agentic_hil` fixture drives the same tools through
`agentic_hil.call(name, arguments)`, against the same discovered configuration.

Names: the Python distribution, CLI command, repository URL, and MCP server name
use `agentic-hil`; Python imports, the pytest plugin, and fixtures use
`agentic_hil`.

## This plugin ships guidance, not the bridge

A portable plugin cannot embed the verified absolute path of a persistent,
user-local executable, so it registers no MCP server command. Install the
package version matching this plugin first:

```bash
uv tool install --upgrade "agentic-hil==0.7.0"
agentic-hil --version
```

The version check must report exactly `0.7.0`. If `uv` is unavailable or a
different `agentic-hil` resolves on `PATH`, stop and ask the operator to
establish the trusted user-local prerequisite; do not substitute `uvx`, a
workspace virtual environment, or an unversioned package. Then, from the
firmware project directory:

```bash
agentic-hil setup --agent claude
```

`setup` creates the deny-by-default project configuration, installs the packaged
copy of this skill, and registers the MCP server with the verified absolute
persistent console-script path.
