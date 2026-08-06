# Agentic HIL

<!-- mcp-name: io.github.agentic-hil/agentic-hil -->

**Your AI agent can develop firmware on its own — because Agentic HIL closes the loop with real hardware.**

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/hero-loop-dark.svg">
  <img alt="The Agentic HIL loop: build, flash, stimulate, observe, then diagnose and fix, closing back onto build. Flash and stimulate write to the real board on your bench; observe reads back from it. Your agent runs the loop unattended and you review the pull request." src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/hero-loop.svg">
</picture>

Agentic Hardware-in-the-Loop (Agentic HIL) is a Python package that exposes bounded MCP tools for probing, flashing, resetting, artifact validation, serial and CAN stimulus/feedback, reports, and logs — without giving an agent arbitrary host or debugger access. Each project has exactly one authoritative configuration stored outside the repository. Agentic HIL discovers it from the project root, while `AGENTIC_HIL_CONFIG` can select an explicit absolute-path override. The file defines the workspace binding, devices, actions, paths, and limits.

Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

## Install

The easiest path: copy/paste this prompt to your AI agent:

```text
Read and follow the complete guide at https://github.com/agentic-hil/agentic-hil/blob/master/AI_AGENT_QUICKSTART.md to install Agentic HIL and set it up for this project.
```

Agents follow [AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) — everything installs user-local, **no admin rights required, ever**. The same is true doing it by hand.

Your agent's host will ask you to approve `agentic-hil setup`, because it writes
the agent's own skill file and MCP registration. That prompt is expected:
approve it once, or add a standing rule for the command prefix
(`Bash(agentic-hil setup:*)` in Claude Code). Nothing asks when you install by
hand in your own terminal.

### Installing it yourself

Two commands: install the package user-locally, then set up the project from its
root.

```bash
pip install --user agentic-hil
agentic-hil setup                 # add --agent codex or --agent opencode for those
```

`setup` installs the agent skill, registers the MCP server with a verified
absolute executable path, creates the policy file outside the repository, and
runs `doctor`. The bench works from that file as written — flashing included:
every permission in it is granted except `allow_raw_debugger_commands` and
`allow_mass_erase`, which are false because either one being true refuses
flashing. It prints where the file landed: review it, and take back whatever this
bench should not have.

Those are two scopes, and `setup` composes the commands that own them:

```bash
agentic-hil agent-install --agent <agent>   # skill + user-level MCP registration; once per user and agent
agentic-hil init --agent <agent>            # this project's policy + doctor; once per project, from its root
```

`agent-install` writes only under the invoking user's home — user-wide, per user
and per machine, not shared with other OS users — and needs no project and no
configuration. Each half rolls back only its own writes, so a project that will
not configure still leaves a working agent, and the next repository for this
user needs `init` alone.

If `pip` is missing, Python is externally managed, or `agentic-hil` does not end
up on `PATH`, install it persistently with a tool installer instead and rerun
`setup`:

```bash
uv tool install agentic-hil       # or: pipx install agentic-hil
```

Never use `pip install --break-system-packages`. Never persist a `uvx`
invocation, a workspace virtual environment, or a bare `PATH` name as the MCP
launcher: each resolves anew later, so the program behind the hardware gate
could change without anyone editing anything.
[AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) has the complete fallback chain,
and [TROUBLESHOOTING.md](TROUBLESHOOTING.md) covers what to do when something
does not start.

For direct PEAK/SocketCAN adapters add the CAN extra — `uv tool install
'agentic-hil[can]'` — and for the pyOCD backend `agentic-hil[pyocd]`. Both are
optional because they carry platform-specific drivers that flashing and UART do
not need; without them those tools refuse with `can_backend_not_available`
rather than failing at import.

Adding one of them to an installation that already exists means rewriting that
environment, so stop the agent host first — it runs the MCP server out of that
environment — and name every extra you want on the command line, because `uv`
records the requirement literally and drops the extras it is not told about.
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) has the sequence and the way to add an
extra without stopping the host.

To upgrade later, run `agentic-hil upgrade`: it upgrades through the manager
that owns the installation running it, keeps the extras, and refuses before it
removes anything if the MCP server is still running. It reports success only
when the version actually moved, and names both numbers when it did. An
installation that is already current and one the package manager holds at an
exact version pin are two separate refusals, and neither asks for a restart —
there would be nothing new to load. Installing without an exact pin, as the
lines above do, keeps the second one from arising at all; the Claude Code plugin
pins on purpose and states the consequence where it does.

To check a version without installing anything, `uvx --from agentic-hil
agentic-hil --version` is a diagnostic only.

## Why

A green build is not enough in embedded development: firmware has to behave correctly on the real board. Classic tools automate single steps — flash here, read a log there — but the moment real hardware has to respond, a human is back in the loop. Handing an agent a raw debugger shell or direct serial access instead is neither safe nor reproducible. Agentic HIL closes the gap with a small, auditable gate:

```
AI agent / CI  ──MCP (stdio)──▶  Agentic HIL  ──authoritative config──▶  OpenOCD / pyOCD / STM32CubeProgrammer
                                    │                        serial ports (pyserial)
                                    │                        CAN (PEAK / SocketCAN / bridge)
                                    ▼
                       structured results, reports, logs
```

Every hardware action is validated against the selected authoritative configuration, executed with timeouts, logged to `.agentic-hil/logs/`, and answered with a structured JSON result (`ok`, `error_type`, `summary`, `likely_causes`, `report_path`, `log_path`) that an agent can act on.

## MCP Entry

Every MCP host starts the same local stdio server from the firmware project root, using the reviewed absolute path of its persistent installation:

```text
/absolute/path/to/persistent/agentic-hil mcp-stdio
```

Host configuration schemas are not portable: VS Code uses `servers`, Claude Code uses `mcpServers`, Codex uses TOML, and OpenCode uses a command array. `agentic-hil agent-install --agent <agent>` — which `agentic-hil setup --agent <agent>` runs first — performs secure user-level registration for Claude Code, Codex, and OpenCode. See [MCP host configuration](docs/mcp-hosts.md) for the remaining hosts. `agentic-hil mcp-config --output .mcp.json` generates only a machine-local Claude-compatible form with an absolute executable path; keep it uncommitted.

`mcp-stdio` discovers the authoritative file from its project working directory: `%APPDATA%/agentic-hil/projects/<project-id>/config.yaml` on Windows or `${XDG_CONFIG_HOME:-~/.config}/agentic-hil/projects/<project-id>/config.yaml` on POSIX. Set `AGENTIC_HIL_CONFIG` when an operator-controlled absolute-path override is needed; never commit a machine-specific override in repository-controlled MCP configuration.

A configured path is checked for what it *is*: every component is opened without following a link and held open for the duration, so a symlinked component or a file where a directory belongs is refused (`unsafe_configured_path`) rather than traversed. Who else on the machine holds rights on it is not asked. A Windows ACL walk and a POSIX mode walk over every ancestor used to ask exactly that, and both were removed in 0.8.0: they could only defend against a different account on the same host — never a requirement here — and they could not defend against your own processes, which own these files and can rewrite them whatever an ACL says. What answers the question that does matter — is this still the configuration this server enforces — is the digest in `config_status`, described under [Changing it while the server runs](#changing-it-while-the-server-runs).

`%APPDATA%` and `%LOCALAPPDATA%` are the discovered defaults. To put the configuration and `state_root` somewhere else — a redirected profile directory, a roaming share — write them under `%USERPROFILE%\.agentic-hil` and select the configuration with `AGENTIC_HIL_CONFIG`. That is a supported binding and not a workaround: a configuration reached that way is read exactly like a discovered one, with the same schema, validation and permissions. Set `AGENTIC_HIL_CONFIG` in the host's user-level or managed environment, never in a repository-controlled file. Where each file lives: `agentic-hil://reference/platform-paths`.

## Configuration

`agentic-hil setup` already created this file; `agentic-hil init` creates it on its own, without the user-wide skill and MCP registration that `agentic-hil agent-install` owns. Either way it lands outside the repository with `workspace_root` bound to the current absolute project path. It defines the target, artifact roots, and the project's named devices — debug probes, serial ports, and CAN buses.

Reading a device needs no permission. What protects a read is exclusivity: every device a test plan names is locked machine-wide for the duration of the run, a device the plan does not name is refused, and a second session is turned away with the holder named. Everything that writes or changes state — flash, reset, serial and CAN sends, mass erase, raw debugger commands — is declared per device, and a generated configuration grants all of it except `allow_raw_debugger_commands` and `allow_mass_erase`. Those two are false because validated flashing and unrestricted debugger access are mutually exclusive: while either is true, `flash_firmware` on that probe is refused, and neither has a tool behind it here to trade for that. The example below is what a generation writes:

```yaml
version: 2                   # reading needs no permission; see Migration below
workspace_root: "/absolute/path/to/firmware-project"
state_root: "/absolute/operator-controlled/user-state/agentic-hil"

target:
  name: "sensor-board"
  controller: "stm32f4"

# Every device is named, and every device carries its own write permissions.
# Test plan steps address a device by that name. The MCP tool surface drives the
# single configured probe, so a project with several probes runs multi-board
# work through `agentic-hil test-reactor`.
debuggers:
  dut:
    type: "openocd"          # or "pyocd" (most Cortex-M targets), or "stlink" (STM32CubeProgrammer CLI)
    probe_id: "0668FF383036" # required once several probes are configured: it is
                             # the only field that selects a physical probe
    interface_cfg: "/absolute/path/to/openocd/scripts/interface/stlink.cfg"
    target_cfg: "/absolute/path/to/openocd/scripts/target/stm32f4x.cfg"
    timeout_s: 60
    permissions:
      allow_flash: true
      allow_reset: true
      allow_raw_debugger_commands: false
      allow_mass_erase: false
  probe_b:                   # a second, independently controlled board
    type: "openocd"
    probe_id: "0669FF505153" # pin the physical probe so boards cannot swap silently
    interface_cfg: "/absolute/path/to/openocd/scripts/interface/stlink.cfg"
    target_cfg: "/absolute/path/to/openocd/scripts/target/stm32f4x.cfg"
    target:                  # optional per-probe override of the project target
      name: "sensor-node"
    permissions:
      allow_flash: false     # this board is read-only for now

debug:
  allowed_symbols: ["main", "sensor_state", "capture_done", "capture_buffer"]
  allow_all_symbols: false

artifacts:
  allowed_roots: ["."]       # the whole workspace, recursively; see Artifact Roots below
  allowed_extensions: [".elf", ".hex", ".bin"]

com_ports:
  dut_uart:
    # How the port is opened. On Linux prefer the /dev/serial/by-id/... symlink:
    # udev builds it from the vendor, the product and the serial, so it follows
    # the board. /dev/ttyACM0 and COM5 are an enumeration order and can come to
    # mean a different board once a second adapter is attached.
    device: "/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_0669FF303435-if02"
    # Which board it is: the adapter's own USB serial, which on a Nucleo is the
    # serial `probe_id` also carries. This is the machine-wide lock identity, and
    # it is compared against the attached hardware when the port is opened.
    # `adopt-hardware` fills both in; on Windows `device` stays "COM5" and this
    # is what carries the identity.
    serial_number: "0669FF303435"
    baudrate: 115200
    assert_dtr: false        # opening the port must not reset this board
    assert_rts: false
    permissions:
      allow_write: true

can_buses:
  dut_can:
    adapter: "socketcan"     # or "peak", or "process" for a custom bridge
    channel: "can0"
    bitrate: 500000
    listen_only: true        # receive without sending dominant ACK bits; enforced per adapter (see Security Model)
    permissions:
      allow_write: false
```

### Artifact Roots

`artifacts.allowed_roots` lists the directories under `workspace_root` that firmware may be flashed from and debug dumps written to, each including its subdirectories. There is exactly one spelling for the whole project:

| Written | Means |
|---|---|
| `["."]` | every directory under `workspace_root`, recursively. What `agentic-hil init` and `project_config_create` generate. |
| `["build", "cmake-build-debug"]` | those directories and their subdirectories, and nothing else. |
| key omitted | `["build"]`. What a configuration written before 0.8.0 means, and it keeps meaning that. |
| `[]` | refused at load, naming both replacements. It reads as "no restriction" to one reader and as "nothing at all" to another, and here it always meant the second. |

Build layouts differ per toolchain — CLion writes to `cmake-build-debug`, PlatformIO to `.pio/build/<env>`, others to `out`, `Debug`, or `zephyr/build` — so a list of directories has to be curated before the first flash while protecting nothing an attacker was stopped by. What refuses a foreign artifact is elsewhere and unchanged: the path must resolve inside `workspace_root`, which no configuration key relaxes and which a symlink or junction leaving the workspace does not satisfy; the extension must be in `allowed_extensions`; and the content is inspected for its format and hashed before a backend sees it. Narrowing the roots restricts a project against its own operator, so it is available and no longer the way in.

An update never widens this. What is generated changed; what an existing file means did not, whether it names its roots or omits the key. `project_config_create` carries the `allowed_roots` of the configuration that server loaded over into the file it regenerates, together with `allowed_extensions` — the check the wide roots lean on — the same way it carries the permissions of the entries that are still there.

### Changing it from an agent

An agent reaches this file only through the MCP tools; `agentic-hil setup` writes host deny rules so its own file tools cannot. Two project-scoped permissions decide how far it gets, and they are deliberately separate:

```yaml
permissions:
  allow_config_write: true                  # regenerate the whole file from hardware discovery
  allow_config_description_write: true      # what the bench IS
  allow_config_permissions_write: true      # take back what the bench MAY do
```

`allow_config_description_write` opens `target.*`, `debuggers.<name>.probe_id` / `executable` / `interface_cfg` / `target_cfg`, `com_ports.<name>.device` / `baudrate` / `serial_number`, and every `can_buses.<name>` field except its permissions. `allow_config_permissions_write` opens every permission key in the file: each `permissions:` block, and the two grants that sit directly on a section rather than inside one, `artifacts.allow_upload` and `debug.allow_all_symbols`. One permission for both would be a master key: set to let an agent enter a 24-character probe serial, it would in the same motion have handed over the permissions block.

#### Permissions move one way

A generated configuration grants everything it can, and the one MCP call that writes a permission field-wise can only ever take grants away. `project_config_set` writes `false` into a permission and no other value — not `true` into one it never touched, and not into one it set to `false` itself a moment earlier. Such a call is refused as `permission_widening_denied`, whatever key it named: once on the value it carried, and again on a comparison of the permissions present in the document before and after the write, so a permission that came on some way the key model did not anticipate is caught too.

```text
Generation              every permission true, except the two flashing is interlocked against
project_config_set may  write false into a permission
             may not    write anything else into one — ever, not even into one it just closed
Last move               permissions.allow_config_permissions_write: false
After that              only you, with `agentic-hil grant` or `agentic-hil init --force`
```

So "the agent may set permissions" does not mean "the agent may set itself anything". It means you can say *narrow this bench* in prose and have it done, with a `provenance` record, and no editor. Closing `allow_config_permissions_write` is terminal for that call: it cannot move a permission in the file again, and the call that does it says so in its own result — which permissions stand frozen, that the agent cannot undo it, and which command reopens it.

#### Opening one again: `agentic-hil grant`

The other direction is yours, and it no longer costs you the file:

```bash
agentic-hil grant can_buses.dut.allow_write          # open one permission
agentic-hil revoke debuggers.dut.allow_mass_erase    # and close one again
```

One named permission moves and nothing else in the configuration is touched — the baudrate, the `resource_id`, the `state_root`, the artifact roots and everything else you ever set stay exactly as they are. That is the whole point of it. Before this, a bench whose `can_buses.<n>.allow_write` was `false` could be reopened only by regenerating the file from attached hardware or by deleting it, and both rewrite far more than the one flag you meant.

Both directions ship together, so this command line is not a one-way street either. What is worth knowing:

* **Single keys, several per command, no wildcard and no whole-entry form.** `agentic-hil grant debuggers.dut.allow_flash debuggers.dut.allow_reset` opens exactly those two, together or not at all. There is deliberately no `debuggers.dut` form: it would mean whatever permissions that entry's schema happens to have *at the moment you type it*, so a flag added in a later release would silently join a command you learned years ago.
* **Both spellings work.** `can_buses.dut.allow_write` and the long `can_buses.dut.permissions.allow_write` name the same key. A name that is neither is refused with a list of every permission key your configuration actually has, entry names and all — that is how you find one, and why there is no wildcard to need.
* **It says what it changed**, including the value it replaced, and names anything already in the asked-for state as a no-op rather than as a change. A pure no-op writes nothing at all: no `provenance` entry, no change marker in the header.
* **A restart is what makes it bind.** A running MCP server parses permissions once, at startup, and `project_config_reload_description` deliberately re-reads none of them. The result says so rather than leaving you to wonder why a granted permission is still refused.
* **It is refused while anything holds the bench** — a run, a COM or CAN session, another terminal. Those holds were taken under the permissions in this file. `agentic-hil lease-status` names the holder.
* **It is not an MCP tool and never will be.** It appears in no tool list, so an agent cannot call it, and the ratchet over MCP is exactly what it was. This is your shell, which already has `agentic-hil init --force`.

**Regenerating is yours, and is not covered by that rule.** `project_config_create` refreshes the hardware facts and carries the permissions of the configuration **that server loaded at startup** over for the entries that are still there — but a workspace whose configuration is gone gets the skeleton at its generated defaults, anything a regeneration discovers for the first time arrives at those defaults, and `agentic-hil init --force` in your own terminal rewrites the whole file back to them. A server does not reload, so a permission narrowed with `project_config_set` in the same session is on disk and not in what that server holds, and a regeneration before it restarts writes the wider loaded value back. Regeneration is creation rather than a permissions write, and it belongs to you and your command line.

With the first set, an agent calls `project_config_describe` — which keys are open right now, which are not, and which permission would open a locked one — and then `project_config_set`:

```json
{"changes": [{"key": "debuggers.dut.probe_id", "value": "066AFF495451885087171450"}]}
```

Named keys with scalar values, each checked against the shipped schema. No document and no subtree can be sent, so the file stays something the agent did not author. A write is refused while a run or a session holds hardware, the changed document is validated before it replaces the working one, and `provenance` records what moved, when, and through which call. The MCP resource `agentic-hil://reference/config-shape` is the full description, with a worked example and what deliberately cannot be done.

### A board attached after the configuration was written

`agentic-hil setup` discovers hardware once. Run it with nothing plugged in and it writes placeholders — `probe_id: null`, `executable: null`, `controller: "unknown-controller"` — which is the common case, because installing the tool and connecting the board are two separate moments.

```bash
agentic-hil adopt-hardware          # --dry-run to see the plan first
```

It reads the attached probe and fills in what is still unset: the probe serial, the backend's executable, the detected controller, and the COM device the probe itself exposes. Nothing else — it supplies no value of its own, and its arguments only select (`--probe-id` among several attached boards, `--debugger` and `--com-port` among configured entries). A key that already holds a value nobody generated is reported as a disagreement and left alone, so a bench somebody set up is never repointed because something else happens to be plugged in — and an entry that already names a probe with a *different* probe attached is refused whole rather than partly carried, because the identity keys describe one board between them. A `com_ports` entry it creates arrives with every permission `false`, written by the server: a generation grants, a write only ever takes away, and adding an entry is a write. `agentic-hil init --force` is what opens a new entry.

Reading a probe is a hardware call like any other here: it takes the same machine-wide lock, so a board another MCP server, test-reactor run or terminal is holding answers `device_busy` instead of being connected to behind its owner's back, and the read is written into the same audit trail. On a configuration written before `version: 2`, where reading still needs `allow_probe`, an entry that denies it denies this too.

An agent does the same over MCP with `project_config_adopt_hardware`, which returns the plan and writes only with `{"apply": true}` and `permissions.allow_config_description_write`. Without that permission it still returns the exact keys and values, so a refusal ends with a person copying one command rather than transcribing a 24-character serial. Both paths go through `project_config_set`, so both are recorded in `provenance` and neither names a `permissions:` key at all.

### Changing it while the server runs

The MCP server parses this file once, at startup. Its **permissions** stay that version's for the life of the process: a policy that changed under a held board would move the rules during the run they govern, and a server bound to a policy must not be able to widen its own grants from a file that moved underneath it.

Its **description** of the bench can be re-read on request, because that argument was never about probe serials and COM devices — see [Picking up a board without a restart](#picking-up-a-board-without-a-restart). Nothing re-reads anything on its own, and no call adopts a permission.

What the answers do say is whether the two still match. Every result carries a `config_status` block once they do not, plus a flat `config_stale: true`; `debugger_info`, `project_config_describe` and `agentic-hil doctor` carry it in every case, so "this is the configuration in force" is a positive statement and not an absence. The comparison is a SHA-256 of the exact bytes the running server parsed against the bytes on disk now — not a modification time, which calls a `git checkout` that restored identical content a change and misses two writes inside one timestamp tick. It is a comparison and nothing more: it reports that the two differ, never what a restart onto the new file would produce. Establishing that would take a second validation congruent with startup, and the value of a promise like that is exactly the congruence of two code paths nobody can keep aligned. `state` is one of five, and they do not share a remedy:

| `state` | What it says | What fixes it | `config_stale` |
|---|---|---|---|
| `unchanged` | `current_digest` equals `loaded_digest`; the file on disk is byte-for-byte the one in force | nothing | absent |
| `changed` | the two digests differ — the file on disk is not the one this server loaded. That is the whole claim: nothing is said about what the file now contains | if what moved is the bench's description, `project_config_reload_description`. Otherwise restart the MCP server; if it does not come back, the startup refusal names what is wrong with the file, so repair it and restart again. `agentic-hil doctor` produces the same refusal without stopping anything | `true` |
| `missing` | the file is gone, so `current_digest` is `null` and there is nothing to restart onto | restore the file, *then* restart | `true` |
| `unreadable` | it is there and will not open — a permission on the file or a directory above it, a path component that is no longer a directory, bytes that are no longer UTF-8. `current_digest` is `null` and `backend_error` names the failure | make it readable, *then* restart | `true` |
| `unknown` | this configuration did not come from a file this process read, so no comparison was made | nothing; `reload_required` is `false` and there is no claim to act on | absent |

While the file is `missing`, `project_config_describe` answers out of the loaded policy rather than refusing: `permissions_in_force` is what this server is still enforcing and `document_source: loaded_policy` says the values came from memory, not from a document that could be compared. `agentic-hil doctor` re-reads the file on every invocation and is therefore always current, which is why its `config_status.loaded_digest` is the value to compare a running server's against — if they differ, that server is answering out of the older file.

One thing does follow the file while the server runs, and it can only ever narrow: `project_config_describe` and `project_config_set` gate on the narrower of the permissions this server loaded and the ones on disk. A permission an operator revokes therefore binds on the next call, and one they add needs the restart.

### Picking up a board without a restart

Installing the tool and plugging the board in are two separate moments, and the file is usually written in the first one. Until now a board written down in the second one was invisible to the running MCP server, and the only way across was the operator restarting it from their agent host. `project_config_reload_description` is the way across, and `agentic-hil config-reload` is the shell-side check of what it would take from this file before anyone makes the call.

It takes no arguments and re-reads exactly four sections — `target`, `debuggers`, `com_ports`, `can_buses` — minus every `permissions:` block inside them.

**It re-reads no permission at all.** Not narrowed, not compared, not adopted, and there is no flag that changes that. The grants in force after a reload are byte-for-byte the ones parsed at startup, and a device this server has never seen arrives holding **nothing**: from `version: 2` on it can be probed and read, because reading needs no grant and exclusivity is what protects it, and flashing, reset, mass erase and COM/CAN writes are denied on it until an operator restarts the server onto the file that grants them. Renaming an entry is the same case — the new name is a device this server has not seen, so it arrives closed.

What still needs a restart is refused by name rather than quietly skipped, because each of these is either a permission wearing a description key's clothes or a section that mixes the two: `permissions`, `version` (it decides whether reading needs a grant at all), `workspace_root`, `state_root`, `debug` (`allow_all_symbols` is a grant, `allowed_symbols` an allowlist), `artifacts` (`allow_upload` is a grant, `allowed_roots` decides what may be flashed), `validation`, `recovery` (it decides whether the machine may drive a physical reset on its own), `reports` and `logs`. The result lists them with the reason.

It is refused while a run, a COM/CAN session or a debug session holds this bench (`config_reload_in_open_run`), while an incident on it is unresolved (`resource_quarantined`), and when the file is missing, unreadable or does not load — the same three states `config_status` reports, with nothing changed on any of them. It writes nothing, so no grant gates it: a bench whose `allow_config_description_write` is false can still pick up a board somebody plugged in.

Afterwards `config_status` compares the file against the description now in force, so a reload that just took the file's description does not leave `config_stale: true` behind for it. The other half stays visible: when the file's permissions differ from the ones being enforced, the status carries `permissions_source`, which says the grants came from the document parsed at startup and that a restart is what adopts the file's. The reload's own result lists the differences under `permission_differences`, taken while both documents were in hand.

### Migration

A configuration written before this release has no `version:` key and is read under version 1, where reading still needs `allow_probe` on a debugger and `allow_read` on a COM port or CAN bus. Nothing infers the new model from a missing key, so an update never widens what a bench already allows. Migrating is one edit: remove those keys and set `version: 2`. A version 2 file that still carries one is refused by name rather than silently ignoring it.

The operator reviews this file and takes away the permissions this bench should not have; a generated one grants all of them except `allow_raw_debugger_commands` and `allow_mass_erase`, which it writes false so that flashing works, and adding a resource is still an operator's edit. `workspace_root` is mandatory and must exactly match the project root used to launch Agentic HIL. `state_root` is also mandatory: it must be an absolute, operator-controlled directory outside and non-overlapping with the workspace. Every trusted launcher for the same host resources must use this pinned root; changing `LOCALAPPDATA` or `XDG_STATE_HOME` after initialization does not change a running service's coordination namespace. Configured debugger/GDB/process-bridge executables and OpenOCD scripts must resolve to existing host-owned files outside the workspace. Empty symbol allowlists deny all symbols; unrestricted symbol access requires `allow_all_symbols: true`. Set optional `resource_id` on a debugger, COM, or CAN entry when different host paths/wrappers address the same physical resource; matching IDs share one cross-process lease. A `resource_id` names hardware rather than a file, so it is matched case-insensitively on every platform, and two entries that spell one id in two cases are refused rather than merged — make them identical if they are one unit, or different by more than case if they are two. Two `debuggers` entries that resolve to the same physical probe are rejected outright, so a plan naming one board can never drive another.

All hardware entry points use this same file: `doctor`, `mcp-stdio`, `com-stdio`, the pytest plugin, and `test-reactor`. Deprecated configuration-path options remain parseable for patch-release compatibility but cannot redirect authority away from the discovered external file.

Export the full JSON schema with `agentic-hil schema --output agentic-hil-config.schema.json`.

## MCP Tools

| Group | Tools | Notes |
|-------|-------|-------|
| Debugger | `debugger_info`, `debugger_probes_list`, `probe_target`, `reset_target` | Probe-ID listing uses pyOCD or STM32CubeProgrammer; OpenOCD cannot enumerate all attached probes |
| Firmware | `flash_firmware`, `artifact_upload` | artifacts are validated, rechecked, and copied to private process staging before flashing; `allow_reset` is additionally required when `reset_after_flash` is requested |
| Serial | `com_ports_list`, `com_session_start`, `com_session_stop`, `com_write`, `com_read` | named ports only, buffered background reader |
| CAN | `can_buses_list`, `can_session_start`, `can_session_stop`, `can_send`, `can_read` | PEAK, SocketCAN, or a process bridge |
| Diagnostics | `get_last_report`, `classify_last_error` | structured error classification with likely causes |
| Project setup | `project_config_create` | generates this workspace's configuration from attached hardware when it has none; takes no arguments and writes every permission `true` except `allow_raw_debugger_commands` and `allow_mass_erase`, which are false so that flashing works, so the bench is workable from the file it produces. Regenerating an existing one carries that file's permissions onto the entries still in it, gives an entry discovered for the first time those same defaults, and reads a probe under the same locks and audit trail as every other hardware call |
| Project config | `project_config_describe`, `project_config_set`, `project_config_adopt_hardware`, `project_config_reload_description` | field-wise changes to an existing configuration, gated by `allow_config_description_write` (what the bench is) and `allow_config_permissions_write` (every permission key: the `permissions:` blocks plus `artifacts.allow_upload` and `debug.allow_all_symbols`). `describe` needs no permission and says which keys are open in this state; `set` takes named keys with scalar values; `adopt_hardware` reads the attached probe and fills in the identity keys that are still unset, through `set`; `reload_description` makes a changed description the one this running server answers out of, re-reading no permission at all and needing no grant because it writes nothing |
| Debug sessions | `debug_start_session`, `debug_stop_session`, `debug_get_session_status`, `debug_set_breakpoint`, `debug_list_breakpoints`, `debug_clear_breakpoints`, `debug_continue`, `debug_halt`, `debug_get_stop_reason`, `debug_symbol_info`, `debug_dump_symbol_ihex` | typed GDB/MI sessions via the OpenOCD backend's gdbserver; unexpected breakpoints and target exceptions are returned as structured stop reasons; symbol allowlist and dump-size limits come from the `debug:` config section |
| Run boundary | `bench_run_start`, `bench_run_stop`, `bench_run_status` | declares the devices of a multi-call run and holds them for its whole duration; without it each call holds its device only for its own duration |

A typical loop: build firmware → `bench_run_start` naming the probe and the port → `flash_firmware` with `reset_after_flash: true` when a fresh boot is required → `com_session_start` → stimulate via `com_write`/`can_send` → assert on `com_read`/`can_read` → `bench_run_stop` → on failure, `classify_last_error`.

Devices are named as `{"kind": "debugger" | "uart" | "can", "id": "<config entry>"}`. The lock is keyed on the hardware behind the entry, so two entries describing one physical unit — a probe and its virtual COM port under a shared `resource_id` — collapse to one lock. The DUT is not a device: it is what the devices drive.

## Test Reactor

Write a hardware test as a plan, not a script: one reviewable YAML file describes flash → stimulate → break → dump, and the reactor guarantees it is either executed exactly as written or rejected before the first hardware action. No half-run plans, no leftover breakpoints, no orphaned debug or UART sessions — the same file behaves identically on your bench and in CI, and it diffs like code in a pull request.

The test reactor executes a strict, sequential YAML or JSON test plan against the named devices in the authoritative config. A debugger step names a `debuggers` entry (`debugger: <name>`); a UART step names a `com_ports` entry (`port_id: <name>`). `debugger` may be omitted while the config declares exactly one probe — with several, the plan must name one, because picking a board for the author is how the wrong board gets flashed. Every probe carries its own permissions, so a step is judged by the grants of the board it names. Probes other than the bound one run on their own service under one shared project lease. Typed debug actions currently require OpenOCD; flash/UART-only plans can use the other backends.

Before the first hardware action, the reactor validates every device name, permission, session order, artifact, breakpoint symbol, and dump path. Execution is fail-fast, each reactor-created breakpoint is removed after use, and debug/UART sessions opened by the runner are closed even when a step raises an exception. Breakpoint and dump symbols must be present in `debug.allowed_symbols` unless `allow_all_symbols: true` is explicitly set.

The run pipeline is deliberately simple — validate everything, then execute, then always clean up:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/reactor-pipeline-dark.svg">
  <img alt="Test reactor pipeline: plan → preflight (no hardware touched; any finding rejects the whole plan) → sequential fail-fast execution → guaranteed cleanup → structured report" src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/reactor-pipeline.svg">
</picture>

```yaml
# .agentic-hil/testconfig.yaml
version: 2
name: capture-state
steps:
  - {debugger: dut, action: flash, image_path: build/app.elf}
  - {port_id: dut_uart, action: uart_open}
  - {debugger: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {debugger: dut, action: run_until_breakpoint, location: capture_done, timeout_s: 5}
  - {debugger: dut, action: dump_memory, symbol: capture_buffer, output_path: build/capture.hex}
  - {debugger: dut, action: debug_stop}
  - {port_id: dut_uart, action: uart_close}
```

`.agentic-hil/testconfig.yaml` and `--test-config` select only this test plan: ordered test steps and the device names they run on. They contain no hardware resources or permissions. The reactor gets all hardware settings from the discovered authoritative config or its `AGENTIC_HIL_CONFIG` override:

```text
agentic-hil test-reactor --test-config .agentic-hil/testconfig.yaml
```

See [`examples/testconfig.example.yaml`](examples/testconfig.example.yaml) for the expanded form.

## Safety Model

Leave an agent alone with real bench hardware and still trust the board, the host, and the logs afterwards. The agent can edit every file inside the workspace, so nothing there is trusted — all authority sits outside, beyond its reach:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/trusted-boundary-dark.svg">
  <img alt="Trusted policy boundary: the agent-writable workspace talks to Agentic HIL over MCP stdio only; the authoritative config and state_root (leases, quarantine, audit chain) live on the operator-controlled host outside the workspace" src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/trusted-boundary.svg">
</picture>

Every hardware action, from every entry point, walks the same gate:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/action-gate-dark.svg">
  <img alt="Action gate: tool call → per-device permission gate → validation → owner lease → execution with pinned executable and timeout → SHA-256 audit chain → structured JSON result; a crash or unknown effect quarantines the resource until operator recovery" src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/action-gate.svg">
</picture>

- Deny-by-default permission switches for everything that writes or changes state, with deliberate interlocks — flashing is refused while `allow_raw_debugger_commands` or `allow_mass_erase` is enabled. `permission_denied` results are authoritative and agents are instructed to stop (see [AGENTS.md](AGENTS.md)).
- Reading needs no permission, and exclusivity carries that load instead. The risk in a HIL tool is not damage but a false green: a test disturbed unnoticed produces a result nobody should trust. Whoever holds a board cannot be disturbed, and whoever reads while no run holds it disturbs nobody. `listen_only` for CAN and a DTR/RTS-free port open remain the way to observe a target provably undisturbed.
- `listen_only` is enforced per adapter rather than assumed, because the three adapters obtain the mode in three different ways. `peak` sets it through PCAN's `BusState.PASSIVE`, re-asserts it once the channel is initialized, and reads `PCAN_LISTEN_ONLY` back from the driver. `socketcan` reads the kernel's control mode and never sets it: the mode belongs to the CAN netdev and only `ip link` puts it there, so an interface that is up without listen-only refuses the session rather than being joined by an ACKing node. A `process` bridge is sent the flag and must answer `listen_only: true` from its `open` result — forwarding confirms nothing. An adapter that cannot be held to it refuses with `can_listen_only_unsupported` (before the bus is touched) or `can_listen_only_unconfirmed` (asked, not confirmed, adapter closed again), never with a silent downgrade; `can_buses_list` reports `listen_only` and `listen_only_enforcement` per bus. That reaches as far as the driver's or the kernel's own report of its mode. Whether the silicon then stops ACKing is proved at the bench, by a single sender with no other participant reporting its frame unacknowledged.
- Configured executables and OpenOCD scripts are pinned at startup and must resolve outside the workspace; firmware artifacts are validated, hashed, and staged in a private process directory before any backend consumes them.
- Serial/CAN writes and reads are size- and buffer-capped; debugger calls run with timeouts and OpenOCD's TCP servers disabled.
- One live owner per project and physical probe/port/bus across all entry points; a second process gets `resource_busy`, and crashes or unknown effects quarantine the resource until explicit operator recovery ([TROUBLESHOOTING.md](TROUBLESHOOTING.md#13-device_busy-resource_busy-or-quarantined-hardware)). Quarantine is reserved for a genuinely unknown physical state: a failure that proves it never reached the hardware — a missing toolchain, an unplugged probe or adapter, an unopenable port, a script refused before the adapter opened, a read whose backend reports that no target answered — refuses with a named error, `target_contacted: false` and `retry_safe: true` instead. The proof has to be the backend's, not the tool's category: a read killed at its deadline still quarantines, because an SWD attach halts the core and a killed process never ran its own shutdown. A quarantine that is justified carries `quarantine_guidance` naming what was attempted, what is confirmed, what remains unknown, and the physical check to perform before signing `recover --confirm-safe-state`.
- Exclusivity per physical device is machine-wide and held for the whole run, kept in one agreed place (`~/.agentic-hil/device-locks`) rather than beside a configuration — two sessions with different `state_root` values are still one bench. A contender is refused with `device_busy` naming the holder; waiting happens only when asked for (`--wait-s`) and is bounded. Ownership is the operating system's lock, so a crashed run frees its devices immediately, with no quarantine and no manual recovery.
- Canonical reports and the tamper-evident audit chain live under the operator-pinned `state_root`; workspace logs and reports are untrusted mirrors, verified against the chain on read.

The complete threat model and design rationale: [docs/security-design.md](docs/security-design.md).

## pytest Plugin

Installing `agentic_hil` registers the `agentic_hil` pytest plugin, so CI regression suites can drive the same permission-gated tools without an MCP client.

The `agentic_hil` fixture uses the same discovered config or absolute-path override as every other entry point and verifies that its `workspace_root` matches the pytest rootdir. Tests using the fixture skip when no config exists and fail loudly when an available config is invalid. Pytest executes project code and is therefore not a sandbox or security boundary; real unattended hardware runners must still use OS isolation and host-managed invocation. COM and CAN sessions opened during a test are stopped afterwards so stimulus state cannot leak between tests. See [examples/nucleo-f446re_demo/](examples/nucleo-f446re_demo/) for the complete loop on real hardware.

## Common Commands

```text
agentic-hil setup --agent <claude-code|codex|opencode>         # both halves, first run
agentic-hil agent-install --agent <claude-code|codex|opencode> # user-wide half
agentic-hil init [--agent <agent>]                             # project half
agentic-hil adopt-hardware [--dry-run]                         # board plugged in after init: fill in what is unset
agentic-hil doctor
agentic-hil config-reload                                      # what a running server's description reload would take from this file
agentic-hil debugger-probes
agentic-hil com-ports
agentic-hil mcp-config --output .mcp.json
agentic-hil mcp-stdio
agentic-hil test-reactor --test-config .agentic-hil/testconfig.yaml
agentic-hil com-stdio --port dut_uart
agentic-hil schema --output agentic-hil-config.schema.json
agentic-hil test-schema --output testconfig.schema.json
agentic-hil skill-install --agent opencode
```

## Platform Support

Linux, macOS, and Windows (CI-tested on Python 3.10–3.13). Debugger backends: OpenOCD, pyOCD (`agentic-hil[pyocd]` — covers most ARM Cortex-M targets via CMSIS packs and CMSIS-DAP/ST-Link/J-Link probes, set `debuggers.<name>.target_type`), and STM32CubeProgrammer CLI (auto-discovered on Windows). Direct CAN requires `agentic-hil[can]` (python-can); CAN also supports a configured `process` bridge backend.

Installing pyOCD is not enough to reach an STM32 part. Most vendor target types — the whole STM32F4 family included — come from a CMSIS device-family pack rather than pyOCD's built-in list, so they need a second, deliberate step:

```bash
pyocd pack find stm32f446         # what packs offer this part
pyocd pack install stm32f446retx  # downloads it from the vendor index
```

`agentic-hil doctor` reports this as `debuggers.<name>.target_support` before anything is flashed, and separates "this host cannot resolve that target type" (red, with the install command) from "this host cannot answer the question" (green, with the reason). Agentic HIL never installs a pack itself: `pyocd pack install` fetches over the network, and that is a step a person takes knowingly. Details: `agentic-hil://reference/target-support`.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check src tests evals tools
pytest
python -m build
twine check dist/*
```

The package is configured for PyPI publishing through GitHub trusted publishing in `.github/workflows/workflow.yml`.

## Security

Policy bypasses are treated as vulnerabilities — see [SECURITY.md](SECURITY.md).

## License

Apache-2.0 — see [LICENSE](LICENSE).
