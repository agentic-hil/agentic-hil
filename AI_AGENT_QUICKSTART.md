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

<!-- TEMP feature/smooth-installation: PyPI install paths removed so agents install from the branch. REVERT before merge. -->
Fast path, in order — stop at the first step that works:

Agentic HIL is a Python package, so a plain `pip install` is fine where `pip` works. Just don't fight a missing one: on current systems (Ubuntu 24.04+/PEP-668, minimal images) the system `pip` is often absent or externally managed. If `pip install` fails for that reason, do **not** hand-roll `ensurepip`/`get-pip`/`apt install python3-pip` — use `uv` as below (bootstrap it in step 4 if missing), or a throwaway venv's pip (`python3 -m venv .venv && .venv/bin/pip install …`).

1. If `agentic-hil --version` works, do not reinstall.
2. If `uv` is available, run Agentic HIL without installing anything (no admin rights, no `PATH` changes):

```bash
uvx --from git+https://github.com/agentic-hil/agentic-hil@feature/smooth-installation agentic-hil --version
```

3. If `uv` is missing but `pipx` is available, the equivalent is `pipx run --spec git+https://github.com/agentic-hil/agentic-hil@feature/smooth-installation agentic-hil --version`.
4. If neither `uv` nor `pipx` is available, install `uv` user-locally (no admin rights; installs to `~/.local/bin`):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

then rerun step 2. A missing runner is a remediable setup prerequisite, not a reason to refuse the Agentic HIL setup.

For a stable command on `PATH` (the MCP server entry), use an isolated persistent install (still user-local, still no admin rights):

```bash
uv tool install git+https://github.com/agentic-hil/agentic-hil@feature/smooth-installation
```

`pipx install "git+https://github.com/agentic-hil/agentic-hil@feature/smooth-installation"` is the equivalent. Both place `agentic-hil` into `~/.local/bin`; if that is not on `PATH`, fix it with `uv tool update-shell` or `pipx ensurepath` — never with admin rights.

## Install Agent Skill

Agent-driven Agentic HIL installation includes installing the bundled `agentic-hil-config-setup` skill into the active agent's user-level skill directory after the CLI is available:

```bash
agentic-hil skill-install --agent <agent>          # or: uvx --from git+https://github.com/agentic-hil/agentic-hil@feature/smooth-installation agentic-hil skill-install --agent <agent>
```

Supported agent names and aliases: `opencode`/`open-code`, `claude-code`/`claude`, `codex`/`codex-cli`/`openai-codex`. For other skill-capable agents use `--agent <name> --target <path>` with that agent's documented user-level skill directory. The installed `agentic-hil` distribution is authoritative: if the installed skill's front-matter version differs from `agentic-hil --version`, rerun `skill-install`.

`agentic-hil setup --agent <agent>` (see below) already installs this skill as part of one-shot project setup; run `skill-install` on its own only for a skill-only reinstall or version bump.

## Configure Each Project

In every firmware project that should use Agentic HIL:

```bash
agentic-hil setup --agent <agent>
```

`setup` is the one-shot path: it prepares a safe external `state_root`, writes the deny-by-default authoritative config, installs the agent skill, writes `.mcp.json`, and runs `doctor` — one command instead of running `init`, `skill-install`, `mcp-config`, and `doctor` yourself, and it fixes the common group-writable `state_root` ancestor snag automatically. It returns one JSON result with a per-step breakdown.

The config it writes is exactly one automatically discovered authoritative file outside the repository, at `%APPDATA%/agentic-hil/projects/<project-id>/config.yaml` on Windows or `${XDG_CONFIG_HOME:-~/.config}/agentic-hil/projects/<project-id>/config.yaml` on POSIX. It sets mandatory `workspace_root` to the current absolute project root and leaves hardware permissions denied. Ask the human operator to review resource and permission changes. Use `AGENTIC_HIL_CONFIG` only for an explicit operator-controlled absolute-path override. Do not create a repository hardware config.

Run the granular steps yourself only if you need to (`agentic-hil init`, then `agentic-hil doctor`). `doctor` validates the authoritative file and checks the debugger only when `allow_probe` permits execution.

Expected healthy result: `ok: true` overall with each step ok, and — once `allow_probe` is enabled — a nested debugger result with `ok: true`.

## Configure MCP

`agentic-hil setup --agent <agent>` (above) already registers the server for that agent in the agent's **user-level** config — outside the firmware repo, so the untrusted repo cannot control how the agent launches tools (the same trust boundary as the authoritative config):

- Claude Code → user scope (`claude mcp add --scope user`, i.e. `~/.claude.json`)
- Codex → `~/.codex/config.toml` (`[mcp_servers.agentic-hil]`)
- opencode → `~/.config/opencode/opencode.json` (`mcp.agentic-hil`)

No `cwd` or absolute path is baked in: every host starts the same command (`agentic-hil mcp-stdio`) from the firmware project root, and `mcp-stdio` discovers the external config from that working directory — refusing to start unless `workspace_root` matches. If `agentic-hil` is not on `PATH`, the generated command uses the resolvable console-script path automatically. See [MCP host configuration](docs/mcp-hosts.md) for the exact per-host rendering and for hosts `setup` does not cover.

If you deliberately want a **project-scoped, team-shared** entry checked into the repo instead, `agentic-hil mcp-config --output .mcp.json` writes the Claude-compatible `mcpServers` form. Do not translate host syntax into new server or tool semantics, and never commit a machine-specific `AGENTIC_HIL_CONFIG` override.

**Claude Code, optional:** a native plugin under `plugins/agentic-hil/` bundles this skill and the MCP server. Install it with `/plugin marketplace add https://github.com/agentic-hil/agentic-hil` then `/plugin install agentic-hil@agentic-hil` (or `claude --plugin-dir ./plugins/agentic-hil` to test). It is an alternative to the skill+MCP part of `agentic-hil setup --agent claude` — use one or the other for the MCP registration, not both. Either way, still run `agentic-hil setup --agent claude` (or at least `agentic-hil init`) once per firmware project for the deny-by-default authoritative config.

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
