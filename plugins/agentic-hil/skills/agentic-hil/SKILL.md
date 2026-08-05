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
file from the hardware attached to this machine, with every permission `true` —
flashing, reset, raw debugger commands, mass erase, COM and CAN writes, and all
three `permissions.allow_config_*` grants. The bench is workable from that file
as it stands: nobody has to open an editor between an empty project and the
first hardware action. Report where the file is, that an agent generated it, and
what it granted — name `allow_mass_erase`, because a mass erase cannot be taken
back — then ask the operator which permissions this bench should not have.
Never edit the file with your own tools, and never delete or move one to get a
different one. Regenerating carries over the permissions of the configuration
this server loaded at startup for the entries already in it — not the file as it
stands now, so a permission you narrowed with `project_config_set` in this
session comes back granted — while a configuration deleted first comes back as
the open skeleton with every narrowing the operator asked for gone, and anything
a regeneration discovers for the first time arrives open. Regenerating is the
operator's call, not yours.

Changing an existing configuration goes through MCP too, never through your own
file tools, and two separate permissions gate it. `project_config_describe`
takes no arguments and answers, for this configuration in this state, which keys
you may change, which you may not, and which permission would open a locked one;
reading needs no grant. `project_config_set` then sets named keys with scalar
values checked against the shipped schema:
`permissions.allow_config_description_write` opens what the bench *is* —
`target.*`, `debuggers.<name>.probe_id`, `com_ports.<name>.device`, CAN bus
settings — and `permissions.allow_config_permissions_write` opens every
permission key: each `permissions:` block, plus the two grants that sit directly
on a section, `artifacts.allow_upload` and `debug.allow_all_symbols`. The split
is deliberate. The first is the one an operator
can leave open, because it does not reach what the bench may do; a refusal names
the permission that is missing and is the answer to the request. A write is
refused while a run or a session holds hardware, the changed file is validated
before it replaces the working one, and what moved is recorded in `provenance`.
This server keeps serving the configuration it loaded, so report the result's
reload_required field and ask the operator to restart it.
`agentic-hil://reference/config-shape` describes the whole shape, a worked
example, and what deliberately cannot be done.

**`project_config_set` only ever narrows.** Write `false` into a permission when
the operator asks you to; you can send no other value for one, not `true` for a
permission you never touched and not `true` for one you set to `false` a moment
ago. Such a call is refused as `permission_widening_denied`, whichever key it
named: the value you sent is checked, and the file's own permissions are
compared before and after the write as well. A person reopens a configuration
with `agentic-hil init --force` in the project root — say that and stop.

Setting `permissions.allow_config_permissions_write: false` is the last
permission change **that call** can make: after it, `project_config_set` cannot
move any permission in that file again. The result of that call says so itself,
under `permissions_frozen` — what stands frozen, that you cannot undo it, and the
command a person reopens it with. Make it when that is what was asked for, never
in passing.

Regenerating is not covered by that rule and is not a way around it.
`project_config_create` is creation rather than a permissions write, and
`agentic-hil init --force` is the operator's own command; both may write open
permissions. Ask for one, do not engineer one.

A configuration written before the board was plugged in holds placeholders:
`probe_id: null`, `executable: null`, `controller: "unknown-controller"`. Do not
print those for a person to retype. `project_config_adopt_hardware` reads the
attached probe and fills in the identity keys that are still unset — the probe
serial, the backend's executable, the detected controller, and the probe's own
COM device. It supplies no value of its own; its arguments only select, and it
returns the plan unless you send `{"apply": true}`. A key that already holds
somebody's value comes back under `kept` and is left alone. Several attached
probes, none at all, or no serial port carrying the probe's serial: each is an
answer naming what to do next rather than a choice made for you. An entry that
already names a probe with a different one attached is `hardware_mismatch` and
no plan at all. Reading the probe is a hardware call: it takes the same
machine-wide lock every board read takes, so a board somebody else holds answers
`device_busy` and nothing is read. Refused for
want of `permissions.allow_config_description_write`, it still returns the exact
keys and values, so the operator gets one command to run — `agentic-hil
adopt-hardware` — and not a serial to transcribe.

`config_stale: true` on any result says the authoritative file is no longer the
one this server parsed. Nothing is reloaded while the server runs, so the backend
that result names, the devices it knows and the permissions it enforces are all
from the version loaded at startup. The `config_status` block names the file and
says which of three states it is in:

- `changed` — loaded_digest and current_digest differ. That is the whole of the
  claim: the file on disk is not the one this server loaded. It says nothing
  about what the file now contains and does not promise a restart will succeed.
  Ask for the restart. If the server does not come back, the startup refusal
  names what is wrong with the file — report that and have it repaired.
  `agentic-hil doctor` produces the same refusal without stopping anything.
- `missing` — the file is gone and current_digest is `null`, so there is
  nothing to restart onto. It has to be put back first. `project_config_describe`
  still answers in this state, out of the loaded policy: permissions_in_force
  is what is still being enforced and `document_source: loaded_policy` says it
  came from memory rather than from a file.
- `unreadable` — it is there and will not open: a permission on the file or a
  directory above it, a path component that is no longer a directory, bytes that
  are no longer UTF-8. current_digest is `null` and backend_error names what the
  read failed on. It has to be made readable before it can be compared at all.

`unknown` is not one of these and never sets `config_stale`: no comparison was
made, so nothing is claimed either way and reload_required is `false`. Report
what you get, ask the operator once, and do not try to make the server pick the
file up by editing again or by calling a configuration tool. `debugger_info` and
`project_config_describe` carry the block either way, so a disagreement between
`agentic-hil doctor` — which reads the file fresh every time — and
`debugger_info` is this and nothing else. A permission revoked since startup
already binds; one added since does not.

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
the verified absolute persistent console-script path, and creates the project
configuration, with every permission granted. The first two are available alone as
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
