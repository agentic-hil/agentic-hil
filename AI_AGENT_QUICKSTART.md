# AI Agent Quickstart

Agentic Hardware-in-the-Loop (Agentic HIL) is the local MCP server for embedded firmware work. Reference hardware: STM32 Nucleo-F446RE, ST-Link, OpenOCD, Python 3.10+.

This file is for agents. Humans start with `README.md`; `TROUBLESHOOTING.md` is for operator diagnostics.

Fetching this guide over HTTP? Take the raw file, not the repository's web view around it: `https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/AI_AGENT_QUICKSTART.md`.

Two steps: install the package, then run `setup` in the firmware project. `setup` does the rest: skill, MCP registration, config, doctor.

`setup` is two commands in one: `agent-install` (user scope, once per user and agent) and `init` (project scope, once per project). First time, run `setup`. Every later firmware repository under the same user, run `init` alone.

## What this changes on the machine

Not project-local. The whole of it, before you start:

- A user-local package and console script in your home directory. No admin rights, ever.
- An `agentic-hil` MCP entry in the agent CLI's **user-level** config, so every project sees it. It starts only where `workspace_root` matches. *(user-wide, `agent-install`)*
- A skill file in your agent's skill directory. *(user-wide, `agent-install`)*
- One external config per project, outside its repository. It grants **every** permission it declares (flashing, reset, serial and CAN writes, artifact upload, unrestricted symbol access, and the three `permissions.allow_config_*` grants) except `allow_raw_debugger_commands` and `allow_mass_erase`, which are `false` because either one being true refuses flashing on that probe and neither has a tool behind it. So the bench is workable without anybody editing YAML, flashing included. Report that and ask which of it the operator wants taken away. A shipped `agentic-hil.config.example.yaml` may narrow individual flags. *(per project, `init`)*
- Nothing inside the firmware project, nothing committed: no `.mcp.json`, no config, no checkout. `setup` needs none of them, and writing one puts the hardware gate in a file anyone with repository access can change.

Larger than what the operator asked for? Say so and let them decide.

## Install

Handed a source tree, a tag, or a commit instead of a link? Install from that (`uv tool install --upgrade <path or git+URL@ref>`) and skip to `setup`. What you were given may have no release yet. A link to this guide is not that case; it means the current release.

Otherwise take these in order and stop at the first that works. The list is complete; the last line needs nothing preinstalled, so reaching the end of it without an installation means you skipped a line, not that you are out of options:

```bash
agentic-hil --version && agentic-hil setup --help   # already installed and new enough? done
python -m pip install --user --upgrade agentic-hil  # wherever pip works
uv tool install --upgrade agentic-hil               # wherever uv is present
pipx install agentic-hil                            # wherever pipx is present
curl -LsSf https://astral.sh/uv/install.sh | sh     # nothing present: this installs uv, then rerun the uv line
```

On Windows the last line is `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`. That download regularly exceeds the 60-second timeout many agent shells default to. A timeout there is not a failed command; rerun it with a longer one. Do not switch methods because of it.

**0.4.0 or newer is required**; earlier releases have no `setup`. Never lower that floor, and dropping the constraint is lowering it: installing `agentic-hil` unpinned after `"agentic-hil>=0.4.0"` failed takes the release you just established is too old. If nothing satisfies it, stop and report what you tried.

Rules that hold whichever line you use:

- **No `sudo`, no admin rights, ever.** Every step is user-local.
- **Never editable** (`pip install -e`, `--editable`). That copies nothing: it points the MCP server at a tree that can change under the operator without a reinstall.
- **Never `--break-system-packages`**, and never into the system Python. PEP 668 refuses for good reason.
- **Never your own virtual environment.** `agentic-hil` would exist only inside it, on nobody's `PATH`, and the MCP registration would name a path only that environment resolves.
- **`uvx` and `pipx run` are not installations.** They leave nothing to register as a long-lived server command.
- **Missing `pip`, `uv` or `pipx` is not a reason to install one system-wide.** No `ensurepip`, no `get-pip`, no `apt install python3-pip`, no package manager at all: the `uv` line above needs none of them and is the answer to a machine that has nothing.

Two Linux failures look like a broken machine and are not. Recognise them by what the terminal printed:

- **`error: externally-managed-environment`** is the pip line refusing under PEP 668, which Debian and Ubuntu ship. `--user` does not get around it. Three of the four remedies the message itself proposes are ruled out above (an `apt` package, a virtual environment, `--break-system-packages`) and the fourth is `pipx`, already the fourth line here. Go to the next line; `uv` above it needs nothing preinstalled either.
- **`invalid peer certificate: UnknownIssuer`** from `uv`, or `self-signed certificate in certificate chain`, means a TLS-intercepting proxy: the machine trusts its CA, uv's bundled roots do not. Rerun *the same* line with `--system-certs`, which makes uv read the operating system's trust store: `uv tool install --system-certs --upgrade agentic-hil`. Older uv releases spell it `--native-tls`. This is the one failure to retry rather than move past.

`--system-certs` belongs against a certificate error and nowhere else. It changes nothing for `Could not connect, are you offline?` with no certificate cause under it, for a proxy demanding authentication, or for a timeout, and it buries the real one. The check is that `curl` and `apt` reach the network from the same shell while `uv` alone fails: they read the system trust store, uv does not until told to. If `--system-certs` still reports `UnknownIssuer`, that store does not have the proxy's CA either; installing it there is the operator's job, and never `--allow-insecure-host` or anything else that switches verification off.

If every line fails, stop and report which one failed and what it printed. An honest report of a blocked prerequisite beats an installation nobody can reach.

Then confirm, opening a new shell if asked:

```bash
agentic-hil --version
agentic-hil setup --help
```

Not on `PATH`? `uv tool update-shell` or `pipx ensurepath`, then a new shell; never admin rights.

Those lines are for a machine that has no Agentic HIL yet. **Once one exists, upgrade it with `agentic-hil upgrade`, never by rerunning an install line.** It upgrades through the manager that owns the installation, keeps `[can]` and `[pyocd]`, and refuses before it removes anything when the MCP server is still running. `uv tool install --upgrade` has no such check: on Windows it removes the tool environment first, the delete fails on the running server, and the rebuild never happens, leaving neither the old installation nor the new one. Adding an extra later is not an upgrade and does need the environment rewritten; [TROUBLESHOOTING.md](TROUBLESHOOTING.md) has that sequence.

**None of the install lines above pins an exact version, and that is deliberate.** `uv tool install "agentic-hil==X.Y.Z"` records `==X.Y.Z` as the requirement, and `uv tool upgrade` (what `agentic-hil upgrade` runs) cannot move an installation off an exact pin. It exits successfully and writes the reason on stderr, so the pin makes every later upgrade a no-op. Never add `==` to one of those lines to "get the version you tested against"; use `>=` if you need a floor. Read `agentic-hil upgrade`'s result by its version fields and not by its exit code: `ok: true` with `previous_version` and `version` differing is the only outcome that upgraded anything, `ok: true` with `already_current: true` means there was nothing newer and nothing is wrong, and `ok: false` with `upgrade_blocked_by_pin` means a pin stopped it and `reinstall_command` on that result is the line that clears it while keeping the extras. Both of the unchanged outcomes set `restart_required: false`; do not ask the operator to restart on either, because the same release would be reloaded. Do not run `reinstall_command` yourself: it rewrites the operator's installation, and a pin can be deliberate. Report it and let them decide.

## Set up the project

Expect the host to stop you here. `setup` writes the agent's own skill file and its user-level MCP registration, and a permission classifier is built to catch exactly that, and rightly, because a program that can edit an agent's configuration can change what the agent may do. Say so before you run it and ask the operator for the grant, instead of letting them find out from a refusal: an approval of the prompt when it appears, or a standing rule for the command prefix, which in Claude Code is `Bash(agentic-hil setup:*)` in user or project settings (other hosts take the same prefix in whatever allowlist they keep), or the operator types the one line themselves; have `agentic-hil setup --agent <agent>` ready to paste when you ask. The same applies to `agent-install`, the half that does the writing.

This is the host's permission system, not Agentic HIL's: it produces no `permission_denied` result, no report and no log, and it refuses before the command runs. That refusal stands until the operator lifts it. Do not reach for another shell, another user, or a flag that skips permission checks, and do not write the rule into the host's settings yourself: that file is what the rule protects.

When the refusal has landed anyway, do not stop at a menu of ways the operator could unblock you while the bench sits unconfigured. Finish what you are allowed to finish: run `agentic-hil init` with no `--agent`. The project half writes the authoritative config outside the repository and touches no file of the agent's own, so it is not what the classifier catches, and it ends in its own `doctor`. Then hand the operator the one line that stays theirs, `agentic-hil agent-install --agent <agent>`, and when they have run it, confirm with `agentic-hil doctor` and report the bench. One thing this order skips, and you must say so: on Claude Code, `setup` and `init --agent` also write the agent's own refusal of its write tools on the config and the state root (SECURITY.md describes that lock), and after a plain `init` the config exists, so that write has no route left. The operator typing `agentic-hil setup --agent <agent>` before your `init` is the complete path; the split is the fallback that leaves a working bench instead of a menu.

From the firmware project root:

```bash
agentic-hil setup --agent <agent>
```

Agent names: `claude-code`/`claude`, `codex`, `opencode`. For another skill-capable agent add `--target <its user-level skill directory>`.

One command instead of `agent-install`, `init` and `doctor` separately. It returns one JSON result with a per-step breakdown under `steps`, and each half's own outcome under `scopes.user` and `scopes.project`; healthy is `ok: true` throughout. It registers the server in the agent's **user-level** config, outside the repository, so an untrusted repo cannot control how the agent launches tools. It writes no project `.mcp.json`.

## The two halves

| | `agentic-hil agent-install --agent <agent>` | `agentic-hil init [--agent <agent>]` |
|---|---|---|
| Scope | user: per user, per machine; every process of that OS user on this host, and no other OS user | one workspace |
| Frequency | once per user and agent | once per project |
| Writes | the agent's skill, the user-level MCP registration | the authoritative config, with every permission granted except the two flashing is interlocked against; with `--agent claude-code`, that agent's refusal of its own write tools on the config and state root. For opencode nothing is written and the step reports that, see SECURITY.md; Codex needs nothing |
| Also | checks that a persistent trusted executable exists to register | runs `doctor` |
| Cwd | anywhere; needs and creates no workspace and no config | the firmware project root |

Each half rolls back only its own writes; **a failing project half never removes an installed skill or MCP registration.** Where the config location cannot be used (a symlinked or non-directory component in the chain, a redirected profile the tool cannot create under; MCP resource `agentic-hil://reference/platform-paths` has where each file lives), `setup` returns `ok: false` with `scopes.user.ok: true`; the agent is installed and working. Fix the location `steps.config` names, run `init` alone, do not rerun `agent-install`, and do not read the refusal as "nothing was installed".

`--force` on `setup` and `agent-install` repairs a managed skill or MCP entry. It never rewrites an authoritative config; only `agentic-hil init --force` does, and that is operator policy. Ask first.

The external config binds `workspace_root` and is written at `version: 3`: reading a device needs no permission, because a run locks the devices its test plan names for its whole duration and that exclusivity is what protects a read, and every `com_ports` entry has to say which hardware it is, whether a `serial_number`, a `resource_id`, a `/dev/serial/by-id/...` device name, or an explicit `identity_source` for an adapter that publishes no serial of its own. Versions 1 and 2 still load exactly as before; `agentic-hil adopt-hardware --apply` is what fills the identity in before a bench is raised to 3. Everything that writes or changes state is **granted**: the owner's decision, because the closed default cost a working day of hand-edited YAML and stopped nothing an agent with a shell was actually stopped by. The two exceptions are `allow_raw_debugger_commands` and `allow_mass_erase`, written `false`: flashing is refused while either is true, and neither has a tool behind it, so granting them subtracted flashing and added nothing. Bootstrap runs either way: setup locates STM32CubeProgrammer, requires one ST-Link, identifies it via HOTPLUG, and matches its virtual COM port, whether or not the workspace ships an `agentic-hil.config.example.yaml`. A flag that profile sets to `false` is honoured, so a project can ship a narrower bench than the skeleton; without a profile the same fixed default `project_config_create` uses fills the template, so `init` and the server describe one bench. Multiple probes, ambiguous COM matches, or existing configs cause refusal, never guessing or replacement.

Bootstrap uses fixed read-only setup commands, not MCP or `probe_target`. No profile means the skeleton, which grants everything but the two flashing is interlocked against.

**`mcp_config_conflict` or `skill_conflict` is the finished answer.** Something under this name is already there and Agentic HIL did not write it. Do not hand-edit the config, delete the entry, or rerun with `--force`: `--force` does not apply to a foreign entry, and replacing one hands the hardware gate to a program the operator did not choose. Report the conflict, name the file, stop.

Write a project `.mcp.json` only if the operator asks (`agentic-hil mcp-config --output .mcp.json`). Unprompted it is a second registration inside the repository, naming the program that answers as the hardware gate in a file anyone with write access can change.

If the board, debugger, COM port or artifact path cannot be inferred, ask one concise question instead of guessing.

## Then use the tools

Discover them with `tools/list`. Build, `probe_target`, `flash_firmware`, then `com_session_*` or `can_session_*` for stimulus and feedback, and `get_last_report` plus `classify_last_error` to diagnose.

Each entry carries MCP `annotations` saying what that tool does to the bench: `project_config_reload_description` and the reads are `readOnlyHint: true`, `flash_firmware` is `destructiveHint: true`, `probe_target` is neither because an SWD attach halts the core. A host may use them to decide what it prompts for. They grant nothing: the permissions in the authoritative config still decide, and `permission_denied` is still the answer.

A sequence of hardware calls is one run and has to say so: `bench_run_start` with `devices: [{"kind": "debugger"|"uart"|"can", "id": "<config entry>"}]` holds those devices until `bench_run_stop`. Without it each call holds its device only for its own duration and the board is free between two steps; inside a run only the declared devices may be touched.

Never substitute raw `openocd`, `pyocd`, `st-flash`, `gdb`, `screen` or `candump`, a Makefile target that runs one, or direct `/dev/tty*` access. `permission_denied` is the answer to the request, not an obstacle: report it, name the permission, stop. Never edit the config to grant yourself one.

Names: the distribution, CLI, repository and MCP server are `agentic-hil`; Python imports and fixtures are `agentic_hil`.
