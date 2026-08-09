# MCP Tools

The complete tool surface Agentic HIL exposes over MCP, what each group does,
and how a run is put together from it. Host registration syntax is in
[MCP host configuration](mcp-hosts.md); the permissions each of these calls is
judged by are in [the authoritative configuration](configuration.md).

## MCP Entry

Every MCP host starts the same local stdio server from the firmware project root, using the reviewed absolute path of its persistent installation:

```text
/absolute/path/to/persistent/agentic-hil mcp-stdio
```

Host configuration schemas are not portable: VS Code uses `servers`, Claude Code uses `mcpServers`, Codex uses TOML, and OpenCode uses a command array. `agentic-hil agent-install --agent <agent>` — which `agentic-hil setup --agent <agent>` runs first — performs secure user-level registration for Claude Code, Codex, and OpenCode. See [MCP host configuration](mcp-hosts.md) for the remaining hosts. `agentic-hil mcp-config --output .mcp.json` generates only a machine-local Claude-compatible form with an absolute executable path; keep it uncommitted.

`mcp-stdio` discovers the authoritative file from its project working directory. [The authoritative configuration](configuration.md) describes where it is found and how an absolute-path override is selected.

## The tool surface

| Group | Tools | Notes |
|-------|-------|-------|
| Debugger | `debugger_info`, `debugger_probes_list`, `probe_target`, `reset_target` | Probe-ID listing uses pyOCD or STM32CubeProgrammer; OpenOCD cannot enumerate all attached probes. `reset_target` modes `run` and `halt` work on every backend; `init` also runs the target's reset-init event script and is OpenOCD-only — stlink and pyocd refuse it with `not_supported` rather than halting instead |
| Firmware | `flash_firmware`, `artifact_upload` | artifacts are validated, rechecked, and copied to private process staging before flashing; `allow_reset` is additionally required when `reset_after_flash` is requested |
| Serial | `com_ports_list`, `com_session_start`, `com_session_stop`, `com_write`, `com_read` | named ports only, buffered background reader |
| CAN | `can_buses_list`, `can_session_start`, `can_session_stop`, `can_send`, `can_read` | PEAK, SocketCAN, or a process bridge |
| Diagnostics | `get_last_report`, `classify_last_error` | structured error classification with likely causes |
| Project setup | `project_config_create` | generates this workspace's configuration from attached hardware when it has none; takes no arguments and writes every permission `true` except `allow_raw_debugger_commands` and `allow_mass_erase`, which are false so that flashing works, so the bench is workable from the file it produces. Regenerating an existing one carries that file's permissions onto the entries still in it, gives an entry discovered for the first time those same defaults, and reads a probe under the same locks and audit trail as every other hardware call |
| Project config | `project_config_describe`, `project_config_set`, `project_config_adopt_hardware`, `project_config_reload_description` | field-wise changes to an existing configuration, gated by `allow_config_description_write` (what the bench is) and `allow_config_permissions_write` (every permission key: the `permissions:` blocks plus `artifacts.allow_upload` and `debug.allow_all_symbols`). `describe` needs no permission and says which keys are open in this state; `set` takes named keys with scalar values; `adopt_hardware` reads the attached probe and fills in the identity keys that are still unset, through `set`; `reload_description` makes a changed description the one this running server answers out of, re-reading no permission at all and needing no grant because it writes nothing |
| Debug sessions | `debug_start_session`, `debug_stop_session`, `debug_get_session_status`, `debug_set_breakpoint`, `debug_list_breakpoints`, `debug_clear_breakpoints`, `debug_continue`, `debug_halt`, `debug_get_stop_reason`, `debug_symbol_info`, `debug_dump_symbol_ihex` | typed GDB/MI sessions via the OpenOCD backend's gdbserver; unexpected breakpoints and target exceptions are returned as structured stop reasons; symbol allowlist and dump-size limits come from the `debug:` config section |
| Run boundary | `bench_run_start`, `bench_run_stop`, `bench_run_status` | declares the devices of a multi-call run and holds them for its whole duration; without it each call holds its device only for its own duration |
| Recovery | `hardware_recover` | clears a quarantine, gated by `permissions.allow_recover`. Reasons that name a call which never reached the hardware clear with no arguments; any other reason needs `operator_statement` — the agent asks the operator in chat and passes back what they said, which the recovery ledger keeps verbatim under an attestation label kept distinct from the operator's own signature. There is no confirmation boolean and will not be: a flag an agent sets for itself is not a confirmation, while a sentence about a bench has to come from somebody. The `agentic-hil recover --confirm-safe-state --quarantine-id <id>` line stays in every refusal as the operator's direct route, and the `audit_broken` families still take it |
| Installation | `server_upgrade` | lifts this installation to the newest release, gated by `permissions.allow_upgrade`. There is no version argument, so it can only go forward — an agent that could name a version could install one that reads this file's permissions differently. It replaces the package on disk and not the code this server is running: a successful result carries `previous_version`, `version`, `running_version` and `restart_required: true`, and an operator restarting the MCP server is what loads it. Refused while a run or session holds the bench (`upgrade_in_open_run`), on Windows, where a running process's files are locked and `agentic-hil upgrade` at a shell is the way (`upgrade_cli_only_on_host`), and on a pinned installation (`upgrade_blocked_by_pin`, which names an extras-preserving reinstall command and never runs it) |

A typical loop: build firmware → `bench_run_start` naming the probe and the port → `flash_firmware` with `reset_after_flash: true` when a fresh boot is required → `com_session_start` → stimulate via `com_write`/`can_send` → assert on `com_read`/`can_read` → `bench_run_stop` → on failure, `classify_last_error`.

Devices are named as `{"kind": "debugger" | "uart" | "can", "id": "<config entry>"}`. The lock is keyed on the hardware behind the entry, so two entries describing one physical unit — a probe and its virtual COM port under a shared `resource_id` — collapse to one lock. The DUT is not a device: it is what the devices drive.

Every result is structured JSON (`ok`, `error_type`, `summary`, `likely_causes`, `report_path`, `log_path`), and each hardware action is validated against the authoritative configuration, executed with a timeout, and logged to `.agentic-hil/logs/`. Which calls a host should treat as read-only or destructive is published per tool in the MCP `annotations` object — see [Tool Annotations](mcp-hosts.md#tool-annotations).

Agent-facing routing — which tool answers which request, and what to do with a refusal — lives in [AGENTS.md](../AGENTS.md).
