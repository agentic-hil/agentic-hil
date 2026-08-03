---
name: agentic-hil
description: Use for any embedded firmware or hardware request in this project — flashing, resetting, probing, debugging, reading UART or CAN traffic, collecting firmware artifacts or test reports — and for configuring Agentic HIL, the safe local MCP bridge that performs them. Use it instead of invoking a debugger, serial device, or CAN adapter directly.
metadata:
  origin: Agentic HIL
  agentic_hil_version: "0.7.1"
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
| A sequence of hardware calls that belong to one run | `bench_run_start`, `bench_run_stop`, `bench_run_status` |

Declare a multi-step sequence before its first call: `bench_run_start` with
`devices: [{"kind": "debugger"|"uart"|"can", "id": "<config entry>"}]` holds
those devices until `bench_run_stop`. Without it each call holds its device only
for its own duration, and between flash and read the board is free for anything
else on the machine. Inside a run only the declared devices may be touched.

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
configuration denies an action the tool returns `permission_denied`. Reading a
device needs no permission, so a refusal is about writing or changing state.
A busy-device refusal is different again: the board is held by another run for
its whole duration, and the result names who has it. Wait for that run or ask
its owner; the hold is not something to clear. Do not stop
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

The CLI installs, configures, and diagnoses: `agentic-hil agent-install`,
`agentic-hil init`, `agentic-hil setup`, and `agentic-hil doctor` report what is
configured and whether hardware access is permitted. None of them is a hardware
call.

Installation and project binding are separate commands with separate scopes.
`agent-install` installs this skill and the user-level MCP registration once per
user and agent — per user, per machine, so no other OS user on the host sees
either — and needs no project. `init` writes and verifies one project's
authoritative configuration. `setup` runs both. A later project for the same
user needs `init` alone, and a failing project step never removes the user-wide
installation.

## Configuration and regression runs

One authoritative configuration file holds the workspace binding, hardware
resources, permissions, and limits. It lives under the current user's Agentic
HIL projects directory, never inside the repository; `AGENTIC_HIL_CONFIG` may
give an explicit absolute-path override. `agentic-hil init` creates it and
prints its path; ask the operator to review any permission change.

When a project has none, every tool answers `config_file_not_found` and
`project_config_create` is the way out. It takes no arguments and generates the
file from the hardware attached to this machine, with every permission `false` —
including the one that would let it write the file again, so it succeeds once
and then refuses itself. That refusal lives in the configuration, so what holds
is what the operator reads there. Report where the file is and that an agent
generated it, and ask the operator for anything the task needs beyond reading
the bench. Never edit the file to grant yourself a permission, and never delete
or move a configuration to get a different one: regenerating only produces the
same restrictive file and throws the operator's settings away.

Changing an existing configuration goes through MCP too, never through your own
file tools, and two separate permissions gate it. `project_config_describe`
takes no arguments and answers, for this configuration in this state, which keys
you may change, which you may not, and which permission would open a locked one;
reading needs no grant. `project_config_set` then sets named keys with scalar
values checked against the shipped schema:
`permissions.allow_config_description_write` opens what the bench *is* — target,
`probe_id`, port device and baudrate, CAN bus settings — and
`permissions.allow_config_permissions_write` opens the `permissions:` blocks.
The split is deliberate. The first is the one an operator can leave open,
because it does not reach your own authority; a refusal names the permission
that is missing and is the answer to the request. A write is refused while a run
or a session holds hardware, the changed file is validated before it replaces
the working one, and what moved is recorded in `provenance`. This server keeps
serving the configuration it loaded, so report `reload_required` and ask the
operator to restart it. `agentic-hil://reference/config-shape` describes the
whole shape, a worked example, and what deliberately cannot be done.

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
uv tool install --upgrade "agentic-hil==0.7.1"
agentic-hil --version
```

The version check must report exactly `0.7.1`. If `uv` is unavailable or a
different `agentic-hil` resolves on `PATH`, stop and ask the operator to
establish the trusted user-local prerequisite; do not substitute `uvx`, a
workspace virtual environment, or an unversioned package. `invalid peer
certificate: UnknownIssuer` is not `uv` being unavailable: it is a
TLS-intercepting proxy, and the same command with `--system-certs` reads the
operating system's trust store instead of uv's bundled roots. Then, from the
firmware project directory:

```bash
agentic-hil setup --agent claude
```

`setup` installs the packaged copy of this skill, registers the MCP server with
the verified absolute persistent console-script path, and creates the
deny-by-default project configuration. The first two are available alone as
`agentic-hil agent-install --agent claude` (user scope), the last as
`agentic-hil init` (project scope).

Writing the agent's own skill directory and MCP registration is what a host's
permission system is built to stop, so expect it to ask before `setup` runs.
Say so before you call it and let the operator approve the prompt or add a
standing rule for the command prefix — `Bash(agentic-hil setup:*)`. That
refusal is the host's, not Agentic HIL's: it carries no `permission_denied`
result and no report, and it stands until the operator lifts it. Never route
around it with another shell or a permission-skipping flag, and never write the
rule into the host's settings yourself.
