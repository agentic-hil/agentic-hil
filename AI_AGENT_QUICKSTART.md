# AI Agent Quickstart

Use Agentic Hardware-in-the-Loop (Agentic HIL) as the local MCP server for embedded firmware development and embedded hardware actions. An STM32 Nucleo-F446RE with on-board ST-Link and OpenOCD is the reference hardware.

This file is for agents. Humans should start with `README.md` and use `TROUBLESHOOTING.md` for operator-facing diagnostics.

Canonical copy/paste user request:

```text
Install from https://github.com/agentic-hil/agentic-hil and set it up for this project.
```

If you were given only the Agentic HIL repository URL and asked to set it up: run the fast path below, install the Agentic HIL skill into your own skill directory, configure the firmware project, then return to the firmware project. Do not clone, checkout, or vendor the Agentic HIL source tree into the firmware project for normal setup.

## Ground Rules

- Never use `sudo` or any administrator privileges for the Agentic HIL installation. Every step below works user-local.
- Never use `pip install --break-system-packages`, and do not install into the system Python (PEP 668 environments will refuse, and they are right).
- Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.
- If the board, debugger, COM port, or artifact path cannot be inferred, ask one concise question instead of guessing.

## Reference Setup

Prefer the supported first path unless the firmware project or user clearly says otherwise:

- STM32 Nucleo-F446RE (a complete demo lives in `examples/nucleo-f446re_demo/`).
- ST-Link with OpenOCD (`interface/stlink.cfg`, `target/stm32f4x.cfg`).
- Python 3.10 or newer.
- Firmware artifacts under `build/`.

## Start Agentic HIL

Fast path, in order — stop at the first persistent installation that works:

Agentic HIL 0.4.0 or newer is required because earlier releases do not provide `setup`. A transient `uvx` or `pipx run` invocation is useful for evaluation but is not an installation and must not be stored as the long-lived MCP server command. Agentic HIL is a Python package, so a plain user-local `pip install` is fine where `pip` works. On current systems (Ubuntu 24.04+/PEP-668, minimal images) the system `pip` is often absent or externally managed. If `pip install` fails for that reason, do **not** hand-roll `ensurepip`/`get-pip`/`apt install python3-pip`; use `uv` or `pipx` as below.

1. Reuse an existing installation only when `agentic-hil --version` reports 0.4.0 or newer and the setup command exists:

```bash
agentic-hil --version
agentic-hil setup --help
```

An older version must be upgraded; a successful `--version` call by itself is not sufficient.

2. Try the normal user-local pip installation first:

```bash
python -m pip install --user --upgrade "agentic-hil>=0.4.0"
```

3. If Python is externally managed, the console script is not reliably on `PATH`, or `pip` is unavailable, use an isolated persistent tool installation:

```bash
uv tool install --upgrade "agentic-hil>=0.4.0"
```

If PyPI lookup is unavailable but the release tag is reachable, use the repository as the persistent package source (this does not create a checkout):

```bash
uv tool install --upgrade "git+https://github.com/agentic-hil/agentic-hil@v0.4.0"
```

4. If `uv` is missing but `pipx` is available, use `pipx install "agentic-hil>=0.4.0"` (or `pipx upgrade agentic-hil` for an older pipx-managed installation). The repository-source equivalent is `pipx install "git+https://github.com/agentic-hil/agentic-hil@v0.4.0"`.
5. If neither `uv` nor `pipx` is available, install `uv` user-locally (no admin rights; installs to `~/.local/bin`):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then rerun step 3. A missing installer is a remediable setup prerequisite, not a reason to refuse the Agentic HIL setup.

After installation, open a new shell if requested and verify both the minimum version and the setup command:

```bash
agentic-hil --version
agentic-hil setup --help
```

`uv tool` and `pipx` place `agentic-hil` into their user-level executable directory. If it is not on `PATH`, use `uv tool update-shell` or `pipx ensurepath` and start a new shell — never use administrator rights.

## Install Agent Skill

Agent-driven Agentic HIL installation includes installing the bundled `agentic-hil-config-setup` skill into the active agent's user-level skill directory after the CLI is available:

```bash
agentic-hil skill-install --agent <agent>
```

Supported agent names and aliases: `opencode`/`open-code`, `claude-code`/`claude`, `codex`/`codex-cli`/`openai-codex`. For other skill-capable agents use `--agent <name> --target <path>` with that agent's documented user-level skill directory. The installed `agentic-hil` distribution is authoritative: if the installed skill's front-matter version differs from `agentic-hil --version`, rerun `skill-install`.

`agentic-hil setup --agent <agent>` (see below) already installs this skill as part of one-shot project setup; run `skill-install` on its own only for a skill-only reinstall or version bump.

**Cross-agent alternative (no Python pre-install):** this repo is also discoverable by the Vercel [`skills`](https://github.com/vercel-labs/skills) CLI, so the skill can be dropped into every agent at once without installing `agentic-hil` first:

```bash
npx skills add -g https://github.com/agentic-hil/agentic-hil --skill agentic-hil-config-setup -a claude-code -a codex -a opencode
```

It installs into `~/.agents/skills/` (read by Codex and OpenCode, symlinked for Claude Code). This distributes only the guidance skill — the MCP server and the deny-by-default project config still come from `agentic-hil setup --agent <agent>`.

## Configure Each Project

In every firmware project that should use Agentic HIL:

```bash
agentic-hil setup --agent <agent>
```

`setup` is the one-shot path: it prepares a safe external `state_root`, writes the deny-by-default authoritative config, installs the agent skill, registers the MCP server in the selected agent's user-level configuration, and runs `doctor` — one command instead of running `init`, `skill-install`, agent-specific MCP registration, and `doctor` yourself. It returns one JSON result with a per-step breakdown. It does not write a project `.mcp.json`; that remains an explicit `mcp-config` operation.

The config it writes is exactly one automatically discovered authoritative file outside the repository, at `%APPDATA%/agentic-hil/projects/<project-id>/config.yaml` on Windows or `${XDG_CONFIG_HOME:-~/.config}/agentic-hil/projects/<project-id>/config.yaml` on POSIX. It sets mandatory `workspace_root` to the current absolute project root and leaves hardware permissions denied. Ask the human operator to review resource and permission changes. Use `AGENTIC_HIL_CONFIG` only for an explicit operator-controlled absolute-path override. Do not create a repository hardware config.

Run the granular steps yourself only if you need to (`agentic-hil init`, then `agentic-hil doctor`). `doctor` validates the authoritative file and checks the debugger only when `allow_probe` permits execution.

Expected healthy result: `ok: true` overall with each step ok, and — once `allow_probe` is enabled — a nested debugger result with `ok: true`.

## Configure MCP

`agentic-hil setup --agent <agent>` (above) already registers the server for that agent in the agent's **user-level** config — outside the firmware repo, so the untrusted repo cannot control how the agent launches tools (the same trust boundary as the authoritative config):

- Claude Code → `~/.claude.json` user scope (secure direct merge)
- Codex → `~/.codex/config.toml` (`[mcp_servers.agentic-hil]`)
- opencode → `~/.config/opencode/opencode.json` (`mcp.agentic-hil`)

No `cwd` is baked in. `setup` resolves the installed console script, rejects transient, workspace-local, unsafe-symlink, or otherwise untrusted candidates, and stores its verified absolute persistent user-bin path. A user-owned pipx/uv-tool link is accepted only when both the link path and its fully resolved target chain pass the ownership, permission, and location checks. The host starts that executable with `mcp-stdio` from the firmware project root; config discovery then refuses to start unless `workspace_root` matches. See [MCP host configuration](docs/mcp-hosts.md) for the exact per-host rendering and for hosts `setup` does not cover.

If you deliberately want a **project-scoped, machine-local** entry instead, `agentic-hil mcp-config --output .mcp.json` writes the Claude-compatible `mcpServers` form with the same verified absolute executable. That file is not portable or team-shared: keep it uncommitted. Do not translate host syntax into new server or tool semantics, and never commit a machine-specific `AGENTIC_HIL_CONFIG` override.

**Claude Code, optional:** a native plugin under `plugins/agentic-hil/` distributes the setup skill but deliberately stores no MCP server command. A portable plugin cannot know the verified absolute path of a persistent user-local executable, and `uvx` is not a durable installation boundary. Install the plugin with `/plugin marketplace add https://github.com/agentic-hil/agentic-hil` then `/plugin install agentic-hil@agentic-hil` (or `claude --plugin-dir ./plugins/agentic-hil` to test), install the exact package version required by its skill, and run `agentic-hil setup --agent claude`. `setup` performs the trusted user-level registration; the plugin does not replace that step.

`mcp-stdio` is project-scoped and JSON-RPC only. COM tool calls pass `port_id`, and CAN tool calls pass `bus_id` as tool arguments. For a continuous plain-text serial channel use a separate `agentic-hil com-stdio --port <port_id>` process from the same project root; never mix plain text into `mcp-stdio`.

## Use The Tools

Use `tools/list` to discover available MCP tools, then follow this loop:

1. Build firmware.
2. Check debugger availability with `debugger_info` if setup is unclear.
3. If multiple probes are attached, use `debugger_probes_list` to discover IDs before asking the operator to select one in the authoritative config. STM32CubeProgrammer and pyOCD support enumeration; OpenOCD does not.
4. Probe with `probe_target`.
5. Flash with `flash_firmware` using `image_path` (usually `build/firmware.elf`), or first call `artifact_upload` and flash the returned `artifact_id`. Pass `reset_after_flash: true` only when a post-flash reset is explicitly needed.
6. For serial feedback: `com_session_start`, stimulate with `com_write`, read with `com_read`, stop with `com_session_stop`.
7. For CAN: `can_session_start`, `can_send`, `can_read`, `can_session_stop`.
8. Read the tool result and `get_last_report`; diagnose failures with `classify_last_error`.

Healthy probe and flash signals: `target_detected: true`, `success_confirmed: true`, `verify: true`, an intentional `reset_after_flash` value, plus `report_path` and `log_path` for auditability.

Do not use raw OpenOCD commands, arbitrary COM-port shell tools, or direct CAN adapter tools when an Agentic HIL MCP tool is available. Treat `permission_denied` as authoritative and stop; ask the operator to review the authoritative config rather than bypassing it.

## pytest Suites

For CI regression suites the installed package registers a pytest plugin: the `agentic_hil` fixture drives the same tools via `agentic_hil.call(name, arguments)`. It uses the same discovered config or absolute-path override as `doctor`, MCP, `com-stdio`, and the test reactor. Tests skip when no config exists and fail loudly when an available config is invalid or bound to another workspace. A repository `.agentic-hil/testconfig.yaml` or `--test-config` is only a test plan for `test-reactor`; it contains no hardware resources or permissions. See `examples/nucleo-f446re_demo/tests/`.
