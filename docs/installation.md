# Installing and Running Agentic HIL

Everything installs user-local, **no admin rights required, ever** — whether an
agent does it or you do. [AI_AGENT_QUICKSTART.md](https://github.com/agentic-hil/agentic-hil/blob/master/AI_AGENT_QUICKSTART.md) is
the agent's copy of this and carries the complete fallback chain;
[TROUBLESHOOTING.md](https://github.com/agentic-hil/agentic-hil/blob/master/TROUBLESHOOTING.md) covers what to do when something
does not start.

## The two commands

Install the package user-locally, then set up the project from its root.

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
bench should not have. [The authoritative configuration](configuration.md) is
what that file says and how to change it.

Your agent's host will ask you to approve `agentic-hil setup`, because it writes
the agent's own skill file and MCP registration. That prompt is expected:
approve it once, or add a standing rule for the command prefix
(`Bash(agentic-hil setup:*)` in Claude Code). Nothing asks when you install by
hand in your own terminal.

## The two halves

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

## When `pip` is not the way

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

To check a version without installing anything, `uvx --from agentic-hil
agentic-hil --version` is a diagnostic only.

## Optional extras

For direct PEAK/SocketCAN adapters add the CAN extra — `uv tool install
'agentic-hil[can]'` — and for the pyOCD backend `agentic-hil[pyocd]`. Both are
optional because they carry platform-specific drivers that flashing and UART do
not need; without them those tools refuse with `can_backend_not_available`
rather than failing at import.

Adding one of them to an installation that already exists means rewriting that
environment, so stop the agent host first — it runs the MCP server out of that
environment — and name every extra you want on the command line, because `uv`
records the requirement literally and drops the extras it is not told about.
[TROUBLESHOOTING.md](https://github.com/agentic-hil/agentic-hil/blob/master/TROUBLESHOOTING.md) has the sequence and the way to add
an extra without stopping the host.

## Upgrading

To upgrade later, run `agentic-hil upgrade`: it upgrades through the manager
that owns the installation running it, keeps the extras, and refuses before it
removes anything if the MCP server is still running. It reports success only
when the version actually moved, and names both numbers when it did. An
installation that is already current and one the package manager holds at an
exact version pin are two separate refusals, and neither asks for a restart —
there would be nothing new to load. Installing without an exact pin, as the
lines above do, keeps the second one from arising at all; the Claude Code plugin
pins on purpose and states the consequence where it does.

## Platforms and debugger backends

Linux, macOS, and Windows (CI-tested on Python 3.10–3.13). Debugger backends: OpenOCD, pyOCD (`agentic-hil[pyocd]` — covers most ARM Cortex-M targets via CMSIS packs and CMSIS-DAP/ST-Link/J-Link probes, set `debuggers.<name>.target_type`), and STM32CubeProgrammer CLI (auto-discovered on Windows). Direct CAN requires `agentic-hil[can]` (python-can); CAN also supports a configured `process` bridge backend.

Installing pyOCD is not enough to reach an STM32 part. Most vendor target types — the whole STM32F4 family included — come from a CMSIS device-family pack rather than pyOCD's built-in list, so they need a second, deliberate step:

```bash
pyocd pack find stm32f446         # what packs offer this part
pyocd pack install stm32f446retx  # downloads it from the vendor index
```

`agentic-hil doctor` reports this as `debuggers.<name>.target_support` before anything is flashed, and separates "this host cannot resolve that target type" (red, with the install command) from "this host cannot answer the question" (green, with the reason). Agentic HIL never installs a pack itself: `pyocd pack install` fetches over the network, and that is a step a person takes knowingly. Details: `agentic-hil://reference/target-support`.

## Command reference

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

`agentic-hil grant` and `agentic-hil revoke` move one named permission in the
authoritative configuration and are described with it, in
[the authoritative configuration](configuration.md#opening-one-again-agentic-hil-grant).
