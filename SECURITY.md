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

Mutable canonical report and hardware-lease state is stored under the pinned `state_root`; `agentic-hil init` selects `%LOCALAPPDATA%/agentic-hil` on Windows or `${XDG_STATE_HOME:-~/.local/state}/agentic-hil` on POSIX as its initial value. Agentic HIL rejects relative roots, workspace overlap, unsafe POSIX ownership/modes, and path redirection. On Windows it rejects a path that a principal outside the operator's own identity — a nameable account other than the current user, `SYSTEM` or `BUILTIN\Administrators` — can write, delete or re-permission, on the object or any ancestor. It does not reject an application-package identity, nor a SID the local security authority answered about by reporting that nothing on this machine maps to it; those two are reported under `doctor`'s `path_trust`, and `windows_path_trust: strict` in the configuration turns both back into a refusal. It does reject what it could not establish: a principal lookup that failed for any other reason, and an ACL it could not read. `windows_path_trust` reaches that configuration's own `state_root` and the directories derived from it; the user configuration directory, the MCP server executable and the machine-wide device-lock directory are always checked under `standard`, so a permissive project cannot weaken the mutex another project's run depends on. None of this defends against code running as the operator, which owns these objects and can rewrite them regardless of any ACL — see the paragraph below. Do not delete or edit quarantine records to bypass recovery. Inspect `agentic-hil lease-status`, confirm physical safe state for its current incident, then use `agentic-hil recover --confirm-safe-state --quarantine-id <id>`.

This boundary assumes the agent cannot modify the authoritative config, parent-process environment, host MCP registration, installed executables, or Agentic HIL process itself. If the agent has arbitrary shell access as the same OS identity, run Agentic HIL as a separate service account or isolated process and restrict the IPC boundary; an external YAML config cannot sandbox an already equivalent host principal.

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
