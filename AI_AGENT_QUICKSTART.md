# AI Agent Quickstart

Agentic Hardware-in-the-Loop (Agentic HIL) is the local MCP server for embedded firmware work. Reference hardware: STM32 Nucleo-F446RE, ST-Link, OpenOCD, Python 3.10+.

This file is for agents. Humans start with `README.md`; `TROUBLESHOOTING.md` is for operator diagnostics.

Two steps: install the package, then run `setup` in the firmware project. `setup` does the rest — skill, MCP registration, config, doctor.

`setup` is two commands in one, split by scope. `agent-install` is machine-wide and runs once per machine and agent; `init` binds one project and runs once per project. First time, run `setup` and get both. Second firmware repository on the same machine, run `init` alone.

## What this changes on the machine

Not project-local. The whole of it, before you start:

- A user-local package and console script in your home directory. No admin rights, ever.
- An `agentic-hil` MCP entry in the agent CLI's **user-level** config, so every project sees it. It starts only where `workspace_root` matches. *(machine-wide, `agent-install`)*
- A skill file in your agent's skill directory. *(machine-wide, `agent-install`)*
- One external config per project: deny-by-default unless `agentic-hil.config.example.yaml` declares bootstrap hardware requirements. *(per project, `init`)*
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

## Set up the project

From the firmware project root:

```bash
agentic-hil setup --agent <agent>
```

Agent names: `claude-code`/`claude`, `codex`, `opencode`. For another skill-capable agent add `--target <its user-level skill directory>`.

One command instead of `agent-install`, `init` and `doctor` separately. It returns one JSON result with a per-step breakdown under `steps`, and each half's own outcome under `scopes.machine` and `scopes.project`; healthy is `ok: true` throughout. It registers the server in the agent's **user-level** config — outside the repository, so an untrusted repo cannot control how the agent launches tools. It writes no project `.mcp.json`.

## The two halves, and when to run which

```bash
agentic-hil agent-install --agent <agent>   # machine-wide, once per machine and agent
agentic-hil init --agent <agent>            # one project, from its root
```

`agent-install` installs the agent's skill and registers the MCP server at user level, and checks that there is a persistent trusted executable to register. It needs no project: no workspace, no configuration, and it creates neither. Run it from anywhere.

`init` writes this workspace's deny-by-default authoritative config and verifies it with `doctor`. With `--agent` it also asks that agent to refuse its own write tools on the config and the state root. Run it from the firmware project root.

Each half rolls back only what it wrote. **A failing project half never removes an installed skill or MCP registration** — so where the configuration location is refused (a stock Windows profile rejects `%APPDATA%`; see MCP resource `agentic-hil://reference/platform-paths`), `setup` reports `ok: false` while `scopes.machine.ok` is `true`, and the agent stays installed and working. Fix the location that `steps.config` names, then run `init` alone. Do not rerun `agent-install`, and do not read the refusal as "nothing was installed".

`--force` on `setup` and `agent-install` repairs a managed skill or MCP entry. It never rewrites an authoritative config; only `agentic-hil init --force` does, and that is operator policy — ask first.

The external config binds `workspace_root` and normally denies all hardware access. A shipped `agentic-hil.config.example.yaml` instead authorizes bootstrap: setup locates STM32CubeProgrammer, requires one ST-Link, identifies it via HOTPLUG, matches its virtual COM port, then writes requested probe/flash/reset/UART grants. Raw debugger commands and mass erase stay denied. Multiple probes, ambiguous COM matches, or existing configs cause refusal, never guessing or replacement.

Bootstrap uses fixed read-only setup commands, not MCP or `probe_target`; `allow_probe` belongs to the config being created. No profile means deny-by-default.

**`mcp_config_conflict` or `skill_conflict` is the finished answer.** Something under this name is already there and Agentic HIL did not write it. Do not hand-edit the config, delete the entry, or rerun with `--force` — `--force` does not apply to a foreign entry, and replacing one hands the hardware gate to a program the operator did not choose. Report the conflict, name the file, stop.

Write a project `.mcp.json` only if the operator asks (`agentic-hil mcp-config --output .mcp.json`). Unprompted it is a second registration inside the repository, naming the program that answers as the hardware gate in a file anyone with write access can change.

If the board, debugger, COM port or artifact path cannot be inferred, ask one concise question instead of guessing.

## Then use the tools

Discover them with `tools/list`. Build, `probe_target`, `flash_firmware`, then `com_session_*` or `can_session_*` for stimulus and feedback, and `get_last_report` plus `classify_last_error` to diagnose.

Never substitute raw `openocd`, `pyocd`, `st-flash`, `gdb`, `screen` or `candump`, a Makefile target that runs one, or direct `/dev/tty*` access. `permission_denied` is the answer to the request, not an obstacle: report it, name the permission, stop. Never edit the config to grant yourself one.

Names: the distribution, CLI, repository and MCP server are `agentic-hil`; Python imports and fixtures are `agentic_hil`.
