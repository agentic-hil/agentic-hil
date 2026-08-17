---
name: agentic-hil
description: Use when a request operates this project's connected target board through the configured bench (flashing, resetting, probing, debugging, UART and CAN stimulus and feedback, firmware artifacts, test reports) or configures Agentic HIL, the safe local MCP bridge that performs them. Use it instead of invoking a debugger, serial device, or CAN adapter directly. Not for designing hardware (PCB layout, schematic capture, EDA, mechanical design), and not for firmware authoring that never touches a board.
metadata:
  origin: Agentic HIL
  agentic_hil_version: "0.15.0"
---

# Agentic HIL

Agentic HIL is this project's hardware gate. Every request that would touch the
target board goes through its MCP tools, which enforce the authoritative
configuration's workspace binding, permissions, and limits. A direct debugger,
serial, or CAN invocation bypasses that gate.

The gate is the whole of the scope. Designing hardware (PCB layout, schematic
capture, EDA, mechanical design) and authoring firmware that never reaches a
board have no tool here: nothing below applies to them, so do that work
directly.

## Route firmware work to these tools

| The request | The tools |
|---|---|
| Flash, program, or load firmware | `flash_firmware` |
| Reset or reboot the target | `reset_target` |
| Is the board connected, which probe, what target | `probe_target`, `debugger_info`, `debugger_probes_list` |
| Halt, continue, breakpoints, stop reason | `debug_start_session`, `debug_halt`, `debug_continue`, `debug_set_breakpoint`, `debug_list_breakpoints`, `debug_clear_breakpoints`, `debug_get_stop_reason`, `debug_get_session_status`, `debug_stop_session` |
| Read a variable, symbol, or memory region | `debug_symbol_info`, `debug_symbol_value`, `debug_dump_symbol_ihex` |
| Serial console, boot log, UART traffic | `com_ports_list`, `com_session_start`, `com_read`, `com_write`, `com_session_stop` |
| CAN frames | `can_buses_list`, `can_read`, `can_send`, `can_session_start`, `can_session_stop` |
| Hand a build artifact to the target workflow | `artifact_upload` |
| What happened in the last run, why did it fail | `get_last_report`, `classify_last_error` |
| A sequence of hardware calls that belong to one run | `bench_run_start`, `bench_run_stop`, `bench_run_status` |
| Run this project's written test plan on the board | `test_reactor_run`, `test_reactor_status`, `test_reactor_stop` |
| Create, read, or change this project's configuration | `project_config_create`, `project_config_describe`, `project_config_set`, `project_config_adopt_hardware`, `project_config_reload_description` |
| The bench is quarantined on a broken audit trail | `hardware_recover` |
| Update Agentic HIL itself to the newest release | `server_upgrade` |

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
tool that matches the question (`probe_target`, `debugger_probes_list`,
`com_ports_list`, `get_last_report`) and report what it returns.

A refusal is an answer, and it is not the end of the job. A refusal also belongs
to the turn it happened in and to no later one: on a new operator request, attempt
the command again before concluding anything, because the machine, the host's
settings and its classifier all move between turns, and only an attempt reads
today's answer. When the authoritative
configuration denies an action the tool returns `permission_denied`. Reading a
device needs no permission, so a refusal is about writing or changing state.
A busy-device refusal is different again: the board is held by another run for
its whole duration, and the result names who has it. Wait for that run or ask
its owner; the hold is not something to clear. Do not stop
there: finish through the gate. `get_last_report` and `classify_last_error` say
what the attempt recorded and why it failed, and `debugger_info` or
`probe_target` say whether the hardware would have been reachable at all.

A failed call and quarantined hardware are two different refusals, and the
result says which one you have. A failure that proves it never reached the
board (a toolchain that is not installed, an unplugged probe or adapter, a port
that would not open, a read whose backend reports that no target answered)
carries `target_contacted: false`, `retry_safe: true` and no
`quarantined: true`: the bench stays in service, so report the named cause, fix
it or ask for it to be fixed, and retry. Being a read is not the proof; the
backend's claim is. `probe_target` and `debugger_probes_list` open an incident
like anything else when the backend named no abort point (a call killed at its
deadline, an exit without complete confirmation), because an SWD attach halts
the core and nothing there says whether it got that far.

An incident is not a padlock on the bench, and since the quarantine narrowed it
is not even a state that outlives the call. A failed run aborts with its
verdict, the abort drives a recovery action (reap, reset into halt where the
policy and the probe's grants allow it, then a probe re-read), and a recovery
that answers the reason the incident names clears it with a ledger line. What
the recovery did not settle stands down when the call ends, with its own ledger
line: the target's state is what the next reset and probe say it is, a serial
handle's and a CAN adapter's is what their next open says, and the operating
system refuses a handle that is really still held without anybody being asked.
So put the target back into a known state and carry on; the result's
`cleanup_reasons` and `quarantine_guidance` still tell you what was left
unconfirmed and what to check, and reading them is the point of them.

`quarantined: true` now means one thing: the evidence chain for this bench
could not be written or read (`*_audit_broken`). That one holds the bench,
because no reset writes a report that was never written. The calls that *are*
the remedy (`probe_target`, `reset_target`, `flash_firmware`) keep working while
it stands; the stimulus class (`com_write`, `can_send`, session starts, new
runs) waits for it. Relay the result's `quarantine_guidance` entries to the
operator (per reason: what was attempted, what is confirmed, what remains
unknown, and the `physical_check` to perform on the board), then clear it with
`hardware_recover`, below, carrying their statement rather than sending them to
a shell. Never clear the server's own state files yourself.

`hardware_recover` is how you clear that one. On a bench with nothing standing
it answers `ok` with `nothing_to_recover: true` and changes nothing, so it is
free to call and never the thing to reach for after an ordinary failed run. A
standing quarantine is refused with `recovery_requires_physical_check`, and that
refusal tells you what to do: **ask the operator, in chat, and pass back what
they say** as `operator_statement`. Show them the result's
`quarantine_guidance` while you ask, then call again with their answer in their
words. It goes into the recovery ledger verbatim, recorded as their statement
relayed by you.

Never write an `operator_statement` you were not given, and never strengthen one
on the way through. The ledger names you as the actor who cleared the bench, so
a line that reflects no actual operator utterance is a false record with your
name on it, and nobody reading it later can tell it from one somebody said.
"It should be fine" is not "the board is powered down". If there is nobody to
ask, relay the `operator_command` instead and stop there; that is the operator's
own route and it stays open. The boundary has not moved (a claim about a
physical board still comes from a person); what moved is that you may carry
their sentence instead of sending them hunting for a shell.

An incident inherited from a process that died holding the bench
(`owner_process_exited_without_release`) is not yours to hand to a person
either, and on a bench whose `recovery.auto_recover` is `reset_halt` it is not
even yours to work around: the next `probe_target`, `reset_target` or
`flash_firmware` drives the target into a defined state, reads it back, and
clears the incident on that evidence, because what the dead owner left
unconfirmed is exactly what the reset erased. Where that cannot run or does not
confirm, the incident stands down anyway and the next call meets the bench with
nothing on it, so the next probe refusing is the honest answer about the board
rather than a leftover hold.

`com_port_identity_mismatch` is a refusal about *which board*, not about
permissions. A serial device name (`/dev/ttyACM0`, `COM7`) is an enumeration
order, so attaching a second adapter can leave a configured entry pointing at a
different board; the check at open time compares the entry's `serial_number` (or
the probe serial it shares a `resource_id` with) against what is actually behind
that name and refuses rather than connecting. Nothing was opened. Report
`expected_serial_number` and `found_serial_number`, and read `expected_device`:
present, it names where the configured board is now, and
`agentic-hil adopt-hardware` rewrites the entry from the attached hardware;
absent, the configured board is not attached and the operator has to plug it in.
When the entry also names `vid`/`pid`, the same refusal carries
`expected_vid`/`expected_pid` and `found_vid`/`found_pid`, and report those too:
a USB serial number is unique only within a vendor, so a matching serial under a
foreign device type is refused as well, and an adapter that publishes no serial
at all is compared on the type alone. Never resolve any of it by writing the
found values into `serial_number`, `vid` or `pid`, or by removing those keys:
that makes the one check that noticed agree with whatever is plugged in, which is
the silent wrong-board flash it exists to prevent.

`can_listen_only_unsupported` and `can_listen_only_unconfirmed` are a third kind
again, and the one most likely to look like an obstacle worth removing. The bus
is configured `listen_only: true`, which is the claim that observing it sends
nothing: a CAN controller outside listen-only sends dominant ACK bits, and on a
bus with one other participant that ACK decides whether the sender considers its
frame delivered. The adapter could not be held to that claim, so the session was
refused instead of downgraded. `link_state` or `driver_state` on the result says
what was found. Relay it and let the operator decide: put the interface into
listen-only outside Agentic HIL, or set `listen_only: false` because this bus may
be ACKed. Never clear it by removing the flag yourself, and never read the bus
with `candump`, `ip link` or a python-can script instead: the controller mode is
the same for those, and the only thing that changes is that no report says the
bus was ACKed.

Report those findings together with the refusal, name the permission that is
denied, and ask the user before changing it. Never work around it with a raw
command, with the configuration file, or with the CLI.

## Reach the tools through the MCP server

Use the `agentic-hil` MCP server, not the command line. That is the interface
`setup` registers, the one an operator can reason about, and the one whose calls
are coordinated and reported as hardware actions. Agent CLIs load MCP servers
when they start, so when the server is missing (typically in the session that
just installed it), say so and ask the user to restart the agent rather than
answering the hardware question another way.

The CLI installs, configures, and diagnoses: `agentic-hil agent-install`,
`agentic-hil init`, `agentic-hil setup`, and `agentic-hil doctor` report what is
configured and whether hardware access is permitted. `init` reads the attached
board, so it takes the same machine-wide lock every board read takes and answers
`device_busy` when another run holds the probe; `setup` does the same when it
writes the file rather than keeping one.

Installation and project binding are separate commands with separate scopes.
`agent-install` installs this skill and the user-level MCP registration once per
user and agent (per user, per machine, so no other OS user on the host sees
either) and needs no project. `init` writes and verifies one project's
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
file from the hardware attached to this machine, with every permission `true`
(flashing, reset, COM and CAN writes, and all three `permissions.allow_config_*`
grants) except `allow_raw_debugger_commands` and `allow_mass_erase`, which it
writes `false`. Neither of those is a capability you are missing: there is no
tool here for either, and while either is true `flash_firmware` on that probe is
refused. The bench is workable from that file as it stands, flashing included:
nobody has to open an editor between an empty project and the first hardware
action. Report where the file is, that an agent generated it, and what it
granted, then ask the operator which permissions this bench should not have.
Never ask for those two to be turned on: that is the one change that stops
flashing working.
Never edit the file with your own tools, and never delete or move one to get a
different one. Regenerating carries over the permissions of the configuration
this server loaded at startup for the entries already in it (not the file as it
stands now, so a permission you narrowed with `project_config_set` in this
session comes back granted), while a configuration deleted first comes back as
the skeleton at its generated defaults with every narrowing the operator asked
for gone, and anything a regeneration discovers for the first time arrives at
those defaults. Regenerating is the
operator's call, not yours.

Changing an existing configuration goes through MCP too, never through your own
file tools, and two separate permissions gate it. `project_config_describe`
takes no arguments and answers, for this configuration in this state, which keys
you may change, which you may not, and which permission would open a locked one;
reading needs no grant. `project_config_set` then sets named keys with scalar
values checked against the shipped schema:
`permissions.allow_config_description_write` opens what the bench *is*
(`target.*`, `debuggers.<name>.probe_id`, `com_ports.<name>.device` /
`serial_number`, CAN bus settings), and `permissions.allow_config_permissions_write` opens every
permission key: each `permissions:` block, plus the two grants that sit directly
on a section, `artifacts.allow_upload` and `debug.allow_all_symbols`. The split
is deliberate. The first is the one an operator
can leave open, because it does not reach what the bench may do; a refusal names
the permission that is missing and is the answer to the request. A write is
refused while a run or a session holds hardware, the changed file is validated
before it replaces the working one, and what moved is recorded in `provenance`.
This server keeps serving the configuration it loaded, so report the result's
reload_required field: `project_config_reload_description` is what makes a
changed device description the one it answers out of, and a changed permission
is what an operator restarts the server for.
`agentic-hil://reference/config-shape` describes the whole shape, a worked
example, and what deliberately cannot be done.

**`project_config_set` only ever narrows.** Write `false` into a permission when
the operator asks you to; you can send no other value for one, not `true` for a
permission you never touched and not `true` for one you set to `false` a moment
ago. Such a call is refused as `permission_widening_denied`, whichever key it
named: the value you sent is checked, and the file's own permissions are
compared before and after the write as well. A person reopens a configuration
and no tool does; say that and stop.

**Say which command, and name the key.** `agentic-hil grant <key>` in the project
root opens one named permission and changes nothing else in the file:
`agentic-hil grant can_buses.dut.allow_write`, several keys in one command when
several are needed, and `agentic-hil revoke <key>` to close one again. It takes
effect once the operator restarts this server, because permissions are read at
startup and `project_config_reload_description` re-reads none of them.
`agentic-hil init --force` is the other command and is much larger (it
regenerates the whole file from attached hardware), so ask for that one only when
the bench itself has to be rebuilt. Neither is in any tool list; there is nothing
to call.

Setting `permissions.allow_config_permissions_write: false` is the last
permission change **that call** can make: after it, `project_config_set` cannot
move any permission in that file again. The result of that call says so itself,
under `permissions_frozen`: what stands frozen, that you cannot undo it, and the
commands a person reopens it with. Make it when that is what was asked for, never
in passing.

Regenerating is not covered by that rule and is not a way around it.
`project_config_create` is creation rather than a permissions write, and
`agentic-hil init --force` and `agentic-hil grant` are the operator's own
commands; all three may write open permissions. Ask for one, do not engineer one.

A configuration written before the board was plugged in holds placeholders:
`probe_id: null`, `executable: null`, `controller: "unknown-controller"`. Do not
print those for a person to retype. `project_config_adopt_hardware` reads the
attached probe and fills in the identity keys that are still unset: the probe
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
keys and values, so the operator gets one command to run (`agentic-hil
adopt-hardware`) and not a serial to transcribe.

`config_stale: true` on any result says the authoritative file is no longer the
one this server is enforcing, so the backend that result names, the devices it
knows and the permissions it enforces are from an older document.
`config_status.description_source` says which one: `startup` for all of it,
which is the normal case, or `description_reload` when a
`project_config_reload_description` has since moved the devices and the backend
onto a newer document (`description_reloaded_at`, and `loaded_digest` is that
document's) while the permissions stayed the startup ones (`loaded_at`). The
`config_status` block names the file and says which of three states it is in:

- `changed` means loaded_digest and current_digest differ. That is the whole of
  the claim: the file on disk is not the one this server loaded. It says nothing
  about what the file now contains and does not promise a restart will succeed.
  If what moved is the bench's description, call
  `project_config_reload_description`; see below. Otherwise ask for the
  restart. If the server does not come back, the startup refusal
  names what is wrong with the file; report that and have it repaired.
  `agentic-hil doctor` produces the same refusal without stopping anything.
- `missing` means the file is gone and current_digest is `null`, so there is
  nothing to restart onto. It has to be put back first. `project_config_describe`
  still answers in this state, out of the loaded policy: permissions_in_force
  is what is still being enforced and `document_source: loaded_policy` says it
  came from memory rather than from a file.
- `unreadable` means it is there and will not open: a permission on the file or
  a directory above it, a path component that is no longer a directory, bytes
  that are no longer UTF-8. current_digest is `null` and backend_error names
  what the read failed on. It has to be made readable before it can be compared
  at all.

`unknown` is not one of these and never sets `config_stale`: no comparison was
made, so nothing is claimed either way and reload_required is `false`.
`debugger_info` and
`project_config_describe` carry the block either way, so a disagreement between
`agentic-hil doctor` (which reads the file fresh every time) and
`debugger_info` is this and nothing else. A revocation that binds before a
restart is exactly three grants and two calls: `project_config_describe` and
`project_config_set` take the narrower of what this server loaded and what the
file now says for `allow_config_write`, `allow_config_description_write` and
`allow_config_permissions_write`, so revoking one of those closes that surface
on the next call and adding one does nothing until a restart. Every other
permission is the startup object and moves for nobody: `allow_flash`,
`allow_reset`, `allow_write`, `allow_mass_erase`,
`allow_raw_debugger_commands` and every per-device grant, plus
`debug.allow_all_symbols` and `artifacts.allow_upload`, keep enforcing what was
loaded until the MCP server is restarted, and a description reload re-reads no
permission in either direction. Narrowing one of those in the file does not
close it: ask for the restart before you rely on it.

**A board plugged in after this server started does not need a restart.**
`project_config_reload_description` takes no arguments, needs no permission
because it writes nothing, and re-reads exactly four sections (`target`,
`debuggers`, `com_ports`, `can_buses`) minus every `permissions:` block inside
them. It re-reads **no permission at all**, in either direction, and there is no
argument that changes that: a device this server has never seen arrives holding
nothing, so you can probe and read it (from `version: 2` on, reading needs no
grant) and cannot flash, reset, mass-erase or write to it until an operator
restarts the server. A renamed entry is the same case. Everything outside those
four sections (every permission, `version`, `workspace_root`, `state_root`,
`debug`, `artifacts`, `validation`, `recovery`, `reports`, `logs`) is adopted by
a restart and by nothing else; the result names them under
`not_reloaded_sections`, and `permission_differences` lists every grant the file
states that this server is not enforcing. The call is refused while a run or a
session holds the bench (`config_reload_in_open_run`), while an incident is
unresolved (`resource_quarantined`), and on a file that is missing, unreadable
or will not load. Report the refusal and stop. Ask the operator once for the
restart when a permission is what moved, and do not try to make the server pick
a change up by editing the file again or by calling a configuration write tool.

Run a written test plan with `test_reactor_run`, not by shelling out to
`agentic-hil test-reactor`. Both drive the same reactor, so the plan is validated,
locked, permitted and reported identically either way; only the tool is coordinated
with the rest of the bench and visible to the operator as a hardware action.
`test_config_path` is `--test-config` and is held to the workspace the same way,
`detach: true` is `--detach`, and `test_reactor_status` and `test_reactor_stop`
answer for the handle it returns. The command line stays the operator's own route.

**The plan format is published, so read it there.**
`agentic-hil://reference/test-plan` says where a plan lives (`.agentic-hil/testconfig.yaml`,
relative to `workspace_root`, and how `test_config_path` overrides it), which format
version admits which step, every step with the entry it routes to and its required and
optional keys, the three comparator families with their rules, and two plans that run.
`agentic-hil://reference/test-plan-schema` serves the schema itself, and the document is
generated from that schema, so the two cannot disagree. Read those before writing or
changing a plan. Never recover the format by opening the installed package under
`site-packages`, by dumping the shipped schema file yourself, or by reading this
project's source for the default filename: those are the same fact, published, and a
reference an operator can see beats a shell command they cannot.

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
uv tool install --upgrade "agentic-hil==0.15.0"
agentic-hil --version
```

The version check must report exactly `0.15.0`. If `uv` is unavailable or a
different `agentic-hil` resolves on `PATH`, stop and ask the operator to
establish the trusted user-local prerequisite; do not substitute `uvx`, a
workspace virtual environment, or an unversioned package. `invalid peer
certificate: UnknownIssuer` is not `uv` being unavailable: it is a
TLS-intercepting proxy, and the same command with `--system-certs` reads the
operating system's trust store instead of uv's bundled roots.

**Why `==` here, and what it costs.** This is the one install command in the
project that pins exactly, because this file *is* the plugin's copy of the
guidance for this release: a package a release ahead of the plugin answers
differently from what you are reading. The cost is that `uv` records that exact
pin, and `uv tool upgrade` (what `agentic-hil upgrade` runs underneath) cannot
move an installation off one. It exits successfully and prints the reason
instead of upgrading, so `agentic-hil upgrade` reports it as
`upgrade_blocked_by_pin`, changes nothing, and asks for no restart. **Move to a
newer release by rerunning the line above from the newer plugin**, not by
expecting `agentic-hil upgrade` to reach past the pin; that reinstall replaces
the recorded requirement. Name any extras on that line as well
(`uv tool install --upgrade "agentic-hil[can]==<that version>"`), because `uv`
records the requirement literally and uninstalls what it was not told about. An
installation the operator wants upgraded on its own from now on takes the
unpinned `uv tool install --upgrade agentic-hil` instead, at the price of the
plugin and the package drifting apart. Then, from the firmware project
directory:

```bash
agentic-hil setup --agent claude
```

`setup` installs the packaged copy of this skill, registers the MCP server with
the verified absolute persistent console-script path, and creates the project
configuration, with every permission granted except the two flashing is
interlocked against. The first two are available alone as
`agentic-hil agent-install --agent claude` (user scope), the last as
`agentic-hil init` (project scope).

Writing the agent's own skill directory and MCP registration is what a host's
permission system is built to stop, so expect it to ask before `setup` runs.
Say so before you call it, then call it in the same turn: the prompt is the
operator's chance to approve and it never appears until something asks for it,
a standing rule for the command prefix, `Bash(agentic-hil setup:*)`, is the
other way to grant it, and a refusal is what tells you to run `agentic-hil init`
and hand over `agentic-hil agent-install --agent claude`. Never end a turn with
the question asked and nothing attempted. That refusal is the host's, not
Agentic HIL's: it carries no `permission_denied` result and no report, and it
stands until the operator lifts it. Never route around it with another shell or
a permission-skipping flag, and never write the rule into the host's settings
yourself.
