# AI Agent Quickstart

Agentic Hardware-in-the-Loop (Agentic HIL) is the local MCP server for embedded firmware work. Reference hardware: STM32 Nucleo-F446RE, ST-Link, OpenOCD, Python 3.10+.

This file is for agents. Humans start with `README.md`; `TROUBLESHOOTING.md` is for operator diagnostics.

Two steps: install the package, then run `setup` in the firmware project. `setup` does the rest — skill, MCP registration, config, doctor.

`setup` is two commands in one: `agent-install` (user scope, once per user and agent) and `init` (project scope, once per project). First time, run `setup`. Every later firmware repository under the same user, run `init` alone.

## What this changes on the machine

Not project-local. The whole of it, before you start:

- A user-local package and console script in your home directory. No admin rights, ever.
- An `agentic-hil` MCP entry in the agent CLI's **user-level** config, so every project sees it. It starts only where `workspace_root` matches. *(user-wide, `agent-install`)*
- A skill file in your agent's skill directory. *(user-wide, `agent-install`)*
- One external config per project, outside its repository: reading a device needs no permission, and every permission that writes or changes state is denied — unless `agentic-hil.config.example.yaml` declares bootstrap hardware requirements. *(per project, `init`)*
- Nothing inside the firmware project, nothing committed — no `.mcp.json`, no config, no checkout. `setup` needs none of them, and writing one puts the hardware gate in a file anyone with repository access can change.

Larger than what the operator asked for? Say so and let them decide.

## Install

Handed a source tree, a tag, or a commit instead of a link? Install from that — `uv tool install --upgrade <path or git+URL@ref>` — and skip to `setup`. What you were given may have no release yet. A link to this guide is not that case; it means the current release.

Otherwise take these in order and stop at the first that works. The list is complete — the last line needs nothing preinstalled, so reaching the end of it without an installation means you skipped a line, not that you are out of options:

```bash
agentic-hil --version && agentic-hil setup --help   # already installed and new enough? done
python -m pip install --user --upgrade agentic-hil  # wherever pip works
uv tool install --upgrade agentic-hil               # wherever uv is present
pipx install agentic-hil                            # wherever pipx is present
curl -LsSf https://astral.sh/uv/install.sh | sh     # nothing present: this installs uv, then rerun the uv line
```

On Windows the last line is `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`. That download regularly exceeds the 60-second timeout many agent shells default to. A timeout there is not a failed command — rerun it with a longer one. Do not switch methods because of it.

**0.4.0 or newer is required**; earlier releases have no `setup`. Never lower that floor, and dropping the constraint is lowering it: installing `agentic-hil` unpinned after `"agentic-hil>=0.4.0"` failed takes the release you just established is too old. If nothing satisfies it, stop and report what you tried.

Rules that hold whichever line you use:

- **No `sudo`, no admin rights, ever.** Every step is user-local.
- **Never editable** (`pip install -e`, `--editable`). That copies nothing: it points the MCP server at a tree that can change under the operator without a reinstall.
- **Never `--break-system-packages`**, and never into the system Python. PEP 668 refuses for good reason.
- **Never your own virtual environment.** `agentic-hil` would exist only inside it, on nobody's `PATH`, and the MCP registration would name a path only that environment resolves.
- **`uvx` and `pipx run` are not installations.** They leave nothing to register as a long-lived server command.
- **Missing `pip`, `uv` or `pipx` is not a reason to install one system-wide.** No `ensurepip`, no `get-pip`, no `apt install python3-pip`, no package manager at all — the `uv` line above needs none of them and is the answer to a machine that has nothing.

If every line fails, stop and report which one failed and what it printed. An honest report of a blocked prerequisite beats an installation nobody can reach.

Then confirm, opening a new shell if asked:

```bash
agentic-hil --version
agentic-hil setup --help
```

Not on `PATH`? `uv tool update-shell` or `pipx ensurepath`, then a new shell — never admin rights.

Those lines are for a machine that has no Agentic HIL yet. **Once one exists, upgrade it with `agentic-hil upgrade`, never by rerunning an install line.** It upgrades through the manager that owns the installation, keeps `[can]` and `[pyocd]`, and refuses before it removes anything when the MCP server is still running. `uv tool install --upgrade` has no such check: on Windows it removes the tool environment first, the delete fails on the running server, and the rebuild never happens — leaving neither the old installation nor the new one. Adding an extra later is not an upgrade and does need the environment rewritten; [TROUBLESHOOTING.md](TROUBLESHOOTING.md) has that sequence.

## Set up the project

From the firmware project root:

```bash
agentic-hil setup --agent <agent>
```

Agent names: `claude-code`/`claude`, `codex`, `opencode`. For another skill-capable agent add `--target <its user-level skill directory>`.

One command instead of `agent-install`, `init` and `doctor` separately. It returns one JSON result with a per-step breakdown under `steps`, and each half's own outcome under `scopes.user` and `scopes.project`; healthy is `ok: true` throughout. It registers the server in the agent's **user-level** config — outside the repository, so an untrusted repo cannot control how the agent launches tools. It writes no project `.mcp.json`.

## The two halves

| | `agentic-hil agent-install --agent <agent>` | `agentic-hil init [--agent <agent>]` |
|---|---|---|
| Scope | user: per user, per machine — every process of that OS user on this host, and no other OS user | one workspace |
| Frequency | once per user and agent | once per project |
| Writes | the agent's skill, the user-level MCP registration | the deny-by-default authoritative config; with `--agent`, that agent's refusal of its own write tools on the config and state root |
| Also | checks that a persistent trusted executable exists to register | runs `doctor` |
| Cwd | anywhere; needs and creates no workspace and no config | the firmware project root |

Each half rolls back only its own writes; **a failing project half never removes an installed skill or MCP registration.** Where the config location is refused (a stock Windows profile rejects `%APPDATA%`; MCP resource `agentic-hil://reference/platform-paths`), `setup` returns `ok: false` with `scopes.user.ok: true` — the agent is installed and working. Fix the location `steps.config` names, run `init` alone, do not rerun `agent-install`, and do not read the refusal as "nothing was installed".

`--force` on `setup` and `agent-install` repairs a managed skill or MCP entry. It never rewrites an authoritative config; only `agentic-hil init --force` does, and that is operator policy — ask first.

The external config binds `workspace_root` and is written at `version: 2`: reading a device needs no permission, because a run locks the devices its test plan names for its whole duration and that exclusivity is what protects a read. Everything that writes or changes state is denied. A shipped `agentic-hil.config.example.yaml` instead authorizes bootstrap: setup locates STM32CubeProgrammer, requires one ST-Link, identifies it via HOTPLUG, matches its virtual COM port, then writes requested flash/reset/UART grants. Raw debugger commands and mass erase stay denied. Multiple probes, ambiguous COM matches, or existing configs cause refusal, never guessing or replacement.

Bootstrap uses fixed read-only setup commands, not MCP or `probe_target`. No profile means everything that writes or changes state is denied.

**`mcp_config_conflict` or `skill_conflict` is the finished answer.** Something under this name is already there and Agentic HIL did not write it. Do not hand-edit the config, delete the entry, or rerun with `--force` — `--force` does not apply to a foreign entry, and replacing one hands the hardware gate to a program the operator did not choose. Report the conflict, name the file, stop.

Write a project `.mcp.json` only if the operator asks (`agentic-hil mcp-config --output .mcp.json`). Unprompted it is a second registration inside the repository, naming the program that answers as the hardware gate in a file anyone with write access can change.

If the board, debugger, COM port or artifact path cannot be inferred, ask one concise question instead of guessing.

## Then use the tools

Discover them with `tools/list`. Build, `probe_target`, `flash_firmware`, then `com_session_*` or `can_session_*` for stimulus and feedback, and `get_last_report` plus `classify_last_error` to diagnose.

A sequence of hardware calls is one run and has to say so: `bench_run_start` with `devices: [{"kind": "debugger"|"uart"|"can", "id": "<config entry>"}]` holds those devices until `bench_run_stop`. Without it each call holds its device only for its own duration and the board is free between two steps; inside a run only the declared devices may be touched.

Never substitute raw `openocd`, `pyocd`, `st-flash`, `gdb`, `screen` or `candump`, a Makefile target that runs one, or direct `/dev/tty*` access. `permission_denied` is the answer to the request, not an obstacle: report it, name the permission, stop. Never edit the config to grant yourself one.

Names: the distribution, CLI, repository and MCP server are `agentic-hil`; Python imports and fixtures are `agentic_hil`.
