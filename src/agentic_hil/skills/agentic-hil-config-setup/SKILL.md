---
name: agentic-hil-config-setup
description: Use for any embedded firmware or hardware request in this project — flashing, resetting, probing, debugging, reading UART or CAN traffic, driving bench adapters, collecting firmware artifacts or test reports — and for configuring Agentic HIL, the safe local MCP bridge that performs them. Use it instead of invoking a debugger, serial device, or CAN adapter directly.
metadata:
  origin: Agentic HIL
  agentic_hil_version: "0.4.0"
---

# Agentic HIL

Agentic HIL is this project's hardware gate. Every request that would touch the
target board goes through its MCP tools, which enforce the authoritative
configuration's workspace binding, permissions, and limits. A direct debugger,
serial, or CAN invocation bypasses that gate and is never the right answer when
an Agentic HIL tool exists for the job.

Names: the Python distribution/install target, CLI command, repository URL, and
MCP server name use `agentic-hil`. Python imports, pytest plugin names,
fixtures, and Python examples use `agentic_hil`.

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
| Stimulus and measurement through a bench adapter | `adapters_list`, `adapter_session_start`, `adapter_measure`, `adapter_set_value`, `adapter_inject_fault`, `adapter_clear_fault`, `adapter_session_stop` |
| Hand a build artifact to the target workflow | `artifact_upload` |
| What happened in the last run, why did it fail | `get_last_report`, `classify_last_error` |

Do not substitute `openocd`, `pyocd`, `st-flash`, `st-util`, `JLinkExe`, `gdb`,
`screen`, `minicom`, `picocom`, `cansend`, or `candump` for these tools, and do
not open `/dev/tty*`, `COM*`, or a SocketCAN interface yourself. Reach for a raw
command only when no Agentic HIL tool covers the request, and say plainly that
you are stepping outside the gate.

For automated regression runs the installed package registers a pytest plugin:
the `agentic_hil` fixture drives the same tools through
`agentic_hil.call(name, arguments)`, against the same discovered configuration.

## Answer hardware questions from the configuration, not from guesses

`agentic-hil doctor` reports the loaded configuration, target, devices, COM
ports, CAN buses, and whether the debugger check is permitted. Read it before
describing the bench, and prefer `debugger_probes_list` or `com_ports_list` over
assuming what is attached.

## Configure the bridge

Agentic HIL discovers one authoritative configuration file under the current
user's Agentic HIL projects directory; `AGENTIC_HIL_CONFIG` may provide an
explicit absolute-path override. That file holds the workspace binding, hardware
resources, permissions, and limits.

From the firmware project directory:

```bash
agentic-hil init
agentic-hil doctor
```

`agentic-hil init` prints the generated config path. Ask the human operator to
review permission changes, and set `AGENTIC_HIL_CONFIG` only when an explicit
absolute-path override is needed. Do not create a hardware configuration inside
the repository.

## When a tool refuses

A `permission_denied` result means the authoritative configuration denies that
action on purpose. Stop, tell the user which permission is denied, and ask
before changing it. Never work around a refusal with a raw debugger command,
direct serial access, or direct CAN access.
