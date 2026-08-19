# Installing and Running Agentic HIL

Everything installs user-local, **no admin rights required, ever** (whether an
agent does it or you do). [AI_AGENT_QUICKSTART.md](https://github.com/agentic-hil/agentic-hil/blob/master/AI_AGENT_QUICKSTART.md) is
the agent's copy of this and carries the complete fallback chain;
[TROUBLESHOOTING.md](https://github.com/agentic-hil/agentic-hil/blob/master/TROUBLESHOOTING.md) covers what to do when something
does not start.

## One line

Linux and macOS, in any shell:

```bash
curl -LsSf https://agentic-hil.github.io/install.sh | sh
```

Windows, in PowerShell:

```powershell
irm https://agentic-hil.github.io/install.ps1 | iex
```

Windows, from `cmd.exe` or the Run box, where the line has to start the
interpreter it needs first:

```cmd
powershell -c "irm https://agentic-hil.github.io/install.ps1|iex"
```

No execution policy stands in the way of either spelling: a policy governs
script *files*, and a command read from a pipe is not one.

That host serves nothing but these two scripts, mirrored from this repository's
default branch by a workflow that copies them and commits only what changed, so
what it serves is byte for byte what is here. The section below takes the
canonical copy straight from the repository and checks it.

The script installs the package user-local (`uv tool install` where `uv` exists,
`python -m pip install --user` otherwise, and it fetches one pinned release of
Astral's uv installer where there is neither `uv` nor a Python 3.10 or newer) and
then runs
`agentic-hil agent-install` for every agent CLI it finds on `PATH`. That is the
machine half and nothing else: it writes no project configuration, edits no
shell profile, and asks for no admin rights. After one restart of your agent,
the agent itself creates this project's configuration over MCP at the first
hardware question, which is the same file `init` would have written.

Its flags are the same in both scripts: `--agent <claude-code|codex|opencode>`
registers one agent instead of every one it finds, `--no-agent-install` stops
after the package, `--version <x.y.z>` installs exactly that release (later
upgrades go through `agentic-hil upgrade`, not through a second run with a new
pin), `--no-can` drops the `[can]` extra that is on by default, and `--help`
prints all of them. Piped, `sh` takes them after `-s --`:

```bash
curl -LsSf https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/install.sh | sh -s -- --agent claude-code
```

A PowerShell script read from a pipe takes no arguments at all, so on Windows a
run that needs a flag downloads the file first and passes it there. PowerShell
also spells the same flags `-Agent`, `-NoAgentInstall`, `-Version`, `-Can`,
`-NoCan` and `-Help`; both spellings bind to the same options:

```powershell
irm https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/install.ps1 -OutFile install.ps1
powershell -NoProfile -File .\install.ps1 --agent claude-code
```

### Reading the script before you run it

PyPI is the trust anchor for the package itself: everything the script installs
comes from the index under the `agentic-hil` name. The uv installer it may fetch
is Astral's own, and it is taken from a versioned URL naming one uv release,
checked against a SHA-256 the script carries as a constant, and executed only if
those bytes match; a mismatch prints both digests and stops the install rather
than running what arrived. Astral's POSIX installer then verifies the uv archive
it downloads against its own published checksums, which is the layer below ours
and the one covering the binary rather than the script. Their PowerShell
installer does not, so on Windows the pin in `install.ps1` is the only check
between `astral.sh` and an executed script. Refreshing that pin is a release
chore, described in [Release Strategy](release-strategy.md). For the script
itself, each release carries `install.sh` and `install.ps1` themselves as release
assets beside their `install.sh.sha256` and `install.ps1.sha256`, so the
verify-first variant takes both halves from the same release, checks one against
the other, and runs the file it checked:

```bash
curl -LsSfO https://github.com/agentic-hil/agentic-hil/releases/latest/download/install.sh
curl -LsSfO https://github.com/agentic-hil/agentic-hil/releases/latest/download/install.sh.sha256
sha256sum -c install.sh.sha256 && sh install.sh
```

```powershell
irm https://github.com/agentic-hil/agentic-hil/releases/latest/download/install.ps1 -OutFile install.ps1
irm https://github.com/agentic-hil/agentic-hil/releases/latest/download/install.ps1.sha256 -OutFile install.ps1.sha256
(Get-FileHash install.ps1 -Algorithm SHA256).Hash -eq (Get-Content install.ps1.sha256).Split()[0]
```

Both halves come from the release on purpose. The one-line form installs the
default branch, where a fix lands first and where the script can therefore have
moved since the last release; a checksum published with a release can only speak
for the file published with it, and pairing the two across that gap is a check
that fails for a reason that is not an attack. The default branch is also what
[the short host](https://agentic-hil.github.io) mirrors, so the two convenient
routes agree with each other and this one is the one that is verifiable.

The raw file on the default branch stays readable at
`https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/install.sh`
and `.../install.ps1` for anyone who wants to read what the one-liner runs
before running it.

## The two commands

Driving it by hand is two commands: install the package user-locally, then set
up the project from its root.

```bash
pip install --user agentic-hil
agentic-hil setup                 # add --agent codex or --agent opencode for those
```

`setup` installs the agent skill, registers the MCP server with a verified
absolute executable path, creates the policy file outside the repository, and
runs `doctor`. The bench works from that file as written, flashing included:
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

`agent-install` writes only under the invoking user's home (user-wide, per user
and per machine, not shared with other OS users) and needs no project and no
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

For direct PEAK/SocketCAN adapters add the CAN extra (`uv tool install
'agentic-hil[can]'`), and for the pyOCD backend `agentic-hil[pyocd]`. Both are
optional because they carry platform-specific drivers that flashing and UART do
not need; without them those tools refuse with `can_backend_not_available`
rather than failing at import.

Adding one of them to an installation that already exists means rewriting that
environment, so stop the agent host first (it runs the MCP server out of that
environment) and name every extra you want on the command line, because `uv`
records the requirement literally and drops the extras it is not told about.
[TROUBLESHOOTING.md](https://github.com/agentic-hil/agentic-hil/blob/master/TROUBLESHOOTING.md) has the sequence and the way to add
an extra without stopping the host.

## Upgrading

To upgrade later, run `agentic-hil upgrade`: it upgrades through the manager
that owns the installation running it, keeps the extras, and refuses before it
removes anything if the MCP server is still running. It reports success only
when the version actually moved, and names both numbers when it did. An
installation that is already current and one the package manager holds at an
exact version pin are two separate refusals, and neither asks for a restart:
there would be nothing new to load. Installing without an exact pin, as the
lines above do, keeps the second one from arising at all; the Claude Code plugin
pins on purpose and states the consequence where it does.

## Uninstalling

Removal is two lines, and the first one has to come first. `agentic-hil
uninstall` takes back the user-wide half while there is still a command left to
run it with: the agent skill files it wrote, the user-level MCP registrations it
made, the Claude Code write refusals `init --agent` added, and the lock sidecars
it left in those agents' directories, for every agent it set up or for the one
`--agent` names. Each is taken back only where it is recognisably Agentic HIL's
own, by the same checks that refuse to overwrite an operator's file, so an entry
somebody else wrote is named on the result and left where it is. Then run the
line the result ends on, which removes the package through the manager that owns
this installation. It has to be a line rather than a step, because a process
cannot delete the files it is executing out of.

Doing it the other way round leaves everything above standing with no command
left to clear it, which is what makes a leftover MCP registration point at a
launcher that is gone.

Two trees are left alone, and there is no flag that purges them. A state root
holds the audit trail of every hardware action and any incident still standing
over a board, which removing a package does not put back. A project
configuration is operator policy, and its permissions only ever narrow, so
deleting it would put every one of them back to the template's on the next
install. Both are named with their paths on the result, so `rm -rf` at your own
shell is one copy away once you have read what is in them.

## Platforms and debugger backends

Linux, macOS, and Windows (CI-tested on Python 3.10–3.13). Debugger backends: OpenOCD, pyOCD (`agentic-hil[pyocd]`: covers most ARM Cortex-M targets via CMSIS packs and CMSIS-DAP/ST-Link/J-Link probes, set `debuggers.<name>.target_type`), and STM32CubeProgrammer CLI (auto-discovered on Windows). Direct CAN requires `agentic-hil[can]` (python-can); CAN also supports a configured `process` bridge backend.

Installing pyOCD is not enough to reach an STM32 part. Most vendor target types, the whole STM32F4 family included, come from a CMSIS device-family pack rather than pyOCD's built-in list, so they need a second, deliberate step:

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
agentic-hil upgrade [--agent <agent>]                          # upgrade the installation and refresh what it wrote
agentic-hil uninstall [--agent <agent>]                        # take the user-wide half back, then run the removal line it names
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

Every one of them takes `--json`, before or after the subcommand.

## What a command prints

Each command answers with one result document, and by default you get it
rendered for a person: the summary as prose, the steps and warnings as lines,
permission changes named, and the next step at the end. A refusal renders its
`error_type`, what happened, and the numbered remediation the tool already
carries for that error. That is what a pipe, a redirect and a terminal all get,
because a wrapper that captures this command and then shows somebody what came
back is what almost every caller is.

`--json` prints the document itself instead, unchanged and field for field. Every
caller that parses this command asks for it, and that is the whole of the
machine contract:

```bash
agentic-hil doctor                  # the health report, for a person
agentic-hil doctor | less           # the same report, through a pipe
agentic-hil doctor --json           # the document, for something that parses it
agentic-hil doctor --json > out.json
```

`mcp-stdio` and `com-stdio` are untouched by any of this: their stdout carries a
protocol, not a result. So is every `--output` file, which is written the same
either way.

`agentic-hil grant` and `agentic-hil revoke` move one named permission in the
authoritative configuration and are described with it, in
[the authoritative configuration](configuration.md#opening-one-again-agentic-hil-grant).
