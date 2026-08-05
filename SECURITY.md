# Security Policy

Agentic Hardware-in-the-Loop (Agentic HIL) is a safe, bounded gate between AI agents and real embedded hardware. Anything that lets an MCP client exceed the discovered authoritative project configuration or its explicit `AGENTIC_HIL_CONFIG` override is a security vulnerability, not just a bug. That includes:

- flashing or reading artifacts outside `workspace_root`, or outside `artifacts.allowed_roots` where those are narrower
- reaching serial devices, CAN channels, or executables that are not named in the config
- executing actions whose permission switch is disabled
- command or path injection through MCP tool arguments or config values
- writing files outside the project through report, log, or dump paths
- bypassing resource leases, stale-owner quarantine, audit fail-closed behavior, or bridge safe-state confirmation

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest release on PyPI | yes |
| older releases | no |

## Reporting a Vulnerability

Please do not open a public issue for security reports. Instead:

- use GitHub private vulnerability reporting on this repository, or
- email `mail@hannes-pauli.de` with a description, a proof-of-concept config/tool call, and the affected version.

You can expect an acknowledgement within 7 days. Please allow time for a fix and release before public disclosure.

## Scope Notes

Each project has exactly one automatically discovered authoritative config, at `%APPDATA%/agentic-hil/projects/<project-id>/config.yaml` on Windows or `${XDG_CONFIG_HOME:-~/.config}/agentic-hil/projects/<project-id>/config.yaml` on POSIX. `AGENTIC_HIL_CONFIG` may select another absolute path, but either file must remain outside the workspace and mandatory `workspace_root` must bind it to the exact project root. Mandatory `state_root` must be absolute, operator-controlled, non-overlapping with the workspace, and shared by every trusted launcher that can address the same host hardware. Keep both roots and any override operator-controlled. Debugger/GDB/CAN process-bridge executables and OpenOCD scripts named by the config must also live outside the workspace. Repository `.agentic-hil/testconfig.yaml` files are test plans only and carry no hardware authority.

Mutable canonical report and hardware-lease state is stored under the pinned `state_root`; `agentic-hil init` selects `%LOCALAPPDATA%/agentic-hil` on Windows or `${XDG_STATE_HOME:-~/.local/state}/agentic-hil` on POSIX as its initial value. Agentic HIL rejects relative roots, workspace overlap, and path redirection, and it refuses a configured path whose components are not what the path claims — a symlinked component, a file where a directory belongs — because following one would act on an object other than the one named. It does **not** ask which other principal on the machine could write the path. A Windows ACL walk and a POSIX ownership/mode walk over every ancestor did ask that until 0.8.0; both were removed (hardci-hq#95). They could only ever defend against a different account on the same machine, which is not a guarantee this project makes, and they could not defend against code running as the operator, which owns these objects and can rewrite them regardless of any ACL. What is in force instead is detection: the SHA-256 in `config_status` says on every answer whether the configuration on disk is still the one the running server enforces, and the audit trail records the digest each hardware action ran under. Do not delete or edit quarantine records to bypass recovery. Inspect `agentic-hil lease-status`, confirm physical safe state for its current incident, then use `agentic-hil recover --confirm-safe-state --quarantine-id <id>`.

This boundary assumes the agent cannot modify the authoritative config, parent-process environment, host MCP registration, installed executables, or Agentic HIL process itself. If the agent has arbitrary shell access as the same OS identity, run Agentic HIL as a separate service account or isolated process and restrict the IPC boundary; an external YAML config cannot sandbox an already equivalent host principal.

## What a generated configuration grants, and which direction it can move

A configuration produced by `agentic-hil init` or `project_config_create` grants **every** permission it declares: flashing, reset, raw debugger commands, mass erase, serial and CAN writes, artifact upload, unrestricted symbol access, and all three project-scoped `permissions.allow_config_*` grants. That is the owner's decision (hardci-hq#96), taken after the destructive ones were named: the person who runs these benches owns them, and the previous closed default cost a working day of hand-edited YAML while protecting against nothing an agent with a shell was actually stopped by. Review the file `setup` prints and take back what your bench should not have — `allow_mass_erase` first, on any bench where a board you did not program can end up in the socket, because a mass erase cannot be undone.

The property that carries the weight is no longer what a generation withholds. It is the direction of the one MCP call that writes a permission, `project_config_set`:

- An agent may set any permission in the file to `false` through it, gated by `permissions.allow_config_permissions_write` and recorded in `provenance`.
- An agent may **never** send any other value for a permission — not `true` for one it never touched, not `true` for one it set to `false` itself, and not a value the schema would coerce. Such a call is refused as `permission_widening_denied` and nothing is written. It is refused twice: on the value the request carried, and again on a comparison of the permissions present in the document before and after the change, which catches a permission that came on however it got there rather than depending on a path parser being right.
- An entry an agent creates under `debuggers`, `com_ports` or `can_buses` arrives with every permission `false`, written by the server. Adding a device is a write, and a write never grants.
- Setting `permissions.allow_config_permissions_write: false` is terminal for that call: after it, `project_config_set` cannot move a permission in that file again. The call that does it reports what stands frozen, that the agent cannot undo it, and that `agentic-hil init --force` is what reopens the file.

**That is the whole claim, and it is deliberately narrow.** The rule binds the MCP write path and nothing else:

- **Regeneration is creation, not a permissions write, and it is not bound by it.** `project_config_create` on a workspace whose configuration is gone writes the open skeleton, and so does `agentic-hil init --force` at the command line. A regeneration over a configuration that is still there carries the permissions on disk over for the entries that are still in it — but anything it discovers for the first time arrives at the skeleton's open default. Regenerating is the operator's call and is treated as theirs.
- **Anything a person does at the command line is theirs.** The `false`-only rule is not applied to CLI paths.

So an agent can only ever reduce its own authority *through the tool it has*. Widening is a person's, at the command line, and `agentic-hil init --force` is the command — it regenerates the configuration from attached hardware with every permission granted again. Treat a report that a permission is missing as a request for that command, not as something to work around.

The file-level deny rules `setup` writes into an agent host are a different mechanism and are unaffected by any of this.

`setup --agent claude-code` writes one deny rule per protected tree into that host's user settings, which is a lock on the front door and not a wall — a shell writes those files regardless. **For opencode, Agentic HIL writes no such rule, deliberately.** That host puts the decision in the operator's hands at the moment of the prompt: answering one permission prompt with "always" adds a session rule that allows every subsequent edit, and it outranks anything the configuration says. A rule written by setup would therefore be a lock a single click removes, and presenting it as protection would say something about this bench that is not true. Whether opencode's file tools may reach the authoritative config and `state_root` is set by the operator in `~/.config/opencode/opencode.json`, under `permission.edit`; `setup` reports that it wrote nothing, and removes the ineffective patterns earlier releases left there. Codex needs no rule: it sandboxes model-generated shell commands and leaves MCP servers outside that sandbox.

Treat that as an expectation, not an edge case. Agentic HIL gates the tools it
offers; it cannot gate a shell it does not own. The installation evaluation
measured this with a small model: in two of five runs it reached for `st-flash`
and `pyocd` before it had called a single Agentic HIL tool, so neither the
refusal a denied tool returns nor the bundled skill had been read at the moment
it decided. A capable model called the tool first, was refused, and reported the
refusal. Where an unattended agent must not be able to drive the hardware at
all, the debugger and serial tooling must not be on its `PATH` — the permission
model decides what Agentic HIL will do, not what the agent can do without it.
