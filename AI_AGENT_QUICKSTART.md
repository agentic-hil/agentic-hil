# AI Agent Quickstart

Use Agentic HIL as the local MCP server for embedded firmware development and embedded hardware actions. HardCI adapters are the reference hardware.

This file is for agents. Humans should start with `README.md` and use `TROUBLESHOOTING.md` for operator-facing diagnostics.

Canonical copy/paste user request:

```text
Install from https://github.com/hp-8472/agentic-hil and set it up for this project.
```

If you were given only the Agentic HIL repository URL and asked to set it up: run the fast path below, install the Agentic HIL skill into your own skill directory, configure the firmware project, then return to the firmware project. Do not clone, checkout, or vendor the Agentic HIL source tree into the firmware project for normal setup.

## Ground Rules

- Never use `sudo` or any administrator privileges for the Agentic HIL installation. Every step below works user-local.
- Never use `pip install --break-system-packages`, and do not install into the system Python (PEP 668 environments will refuse, and they are right).
- Names: the Python package/install target and Python-facing identifiers such as imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`. The CLI command, repository URL, and MCP server name use `agentic-hil`.
- If the board, debugger, COM port, or artifact path cannot be inferred, ask one concise question instead of guessing.

## Reference Setup

Prefer the supported first path unless the firmware project or user clearly says otherwise:

- STM32 Nucleo-F446RE (a complete demo lives in `examples/nucleo-f446re_demo/`).
- ST-Link with OpenOCD (`interface/stlink.cfg`, `target/stm32f4x.cfg`).
- Python 3.10 or newer.
- Firmware artifacts under `build/`.

## Start Agentic HIL

Fast path, in order — stop at the first step that works:

1. If `agentic-hil --version` works, do not reinstall.
2. Try the normal user-local pip install first:

```bash
python -m pip install --user agentic_hil
agentic-hil --version
```

If that fails because Python is externally managed, `agentic-hil` is not on `PATH`, or the package is unavailable through that interpreter, continue with the runner paths below.

3. If `uv` is available, run Agentic HIL without installing anything (no admin rights, no `PATH` changes):

```bash
uvx --from agentic_hil agentic-hil --version
```

4. If the PyPI package lookup fails, use the repository as the package source (this is a package source only — it does not create a checkout):

```bash
uvx --from git+https://github.com/hp-8472/agentic-hil agentic-hil --version
```

5. If `uv` is missing but `pipx` is available, the equivalents are `pipx run --spec agentic_hil agentic-hil --version` and `pipx run --spec git+https://github.com/hp-8472/agentic-hil agentic-hil --version`.
6. If neither `uv` nor `pipx` is available, install `uv` user-locally (no admin rights; installs to `~/.local/bin`):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

then rerun step 3. A missing runner is a remediable setup prerequisite, not a reason to refuse the Agentic HIL setup.

If the `pip --user` command is not reliably on `PATH`, use an isolated persistent install for the MCP server entry (still user-local, still no admin rights):

```bash
uv tool install agentic_hil        # or from the repository: uv tool install git+https://github.com/hp-8472/agentic-hil
```

`pipx install agentic_hil` is the equivalent. Both place `agentic-hil` into `~/.local/bin`; if that is not on `PATH`, fix it with `uv tool update-shell` or `pipx ensurepath` — never with admin rights.

## Install Agent Skill

Agent-driven Agentic HIL installation includes installing the bundled `hardci-config-setup` skill into the active agent's user-level skill directory after the CLI is available:

```bash
agentic-hil skill-install --agent <agent>          # or: uvx --from agentic_hil agentic-hil skill-install --agent <agent>
```

Supported agent names and aliases: `opencode`/`open-code`, `claude-code`/`claude`, `codex`/`codex-cli`/`openai-codex`. For other skill-capable agents use `--agent <name> --target <path>` with that agent's documented user-level skill directory. The installed `agentic_hil` package is authoritative: if the installed skill's front-matter version differs from `agentic-hil --version`, rerun `skill-install`.

## Configure Each Project

In every firmware project that should use Agentic HIL:

```bash
agentic-hil init            # writes the starter .hardci/config.yaml
# edit .hardci/config.yaml: target, debugger configs, allowed artifact roots,
# named com_ports / can_buses / adapters — keep the safety policy restrictive
agentic-hil doctor          # validates config and checks the debugger
agentic-hil mcp-config --output .mcp.json
```

Keep `.hardci/` with the project: it defines that project's hardware policy, reports, logs, and allowed artifact locations. Do not reinstall Agentic HIL inside every project.

Expected healthy `agentic-hil doctor` result: `ok: true`, `summary: "HardCI configuration loaded and debugger checked."`, and a nested debugger result with `ok: true`.

## Configure MCP

`.mcp.json` is only the MCP launch entry. The default written by `agentic-hil mcp-config` assumes `agentic-hil` is on `PATH`:

```json
{
  "mcpServers": {
    "agentic-hil": {
      "command": "agentic-hil",
      "args": ["mcp-stdio", "--config", ".hardci/config.yaml"]
    }
  }
}
```

If `agentic-hil` is not on `PATH`, use the runner form instead: `"command": "uvx", "args": ["--from", "agentic_hil", "agentic-hil", "mcp-stdio", "--config", ".hardci/config.yaml"]`.

`mcp-stdio` is project-scoped and JSON-RPC only. COM tool calls pass `port_id`, CAN tool calls pass `bus_id`, and test-adapter tool calls pass `adapter_id` as tool arguments. For a continuous plain-text serial channel use a separate `agentic-hil com-stdio --config .hardci/config.yaml --port <port_id>` process — never mix plain text into `mcp-stdio`.

## Use The Tools

Use `tools/list` to discover available MCP tools, then follow this loop:

1. Build firmware.
2. Check debugger availability with `hardci_debugger_info` if setup is unclear.
3. Probe with `hardci_probe_target`.
4. Flash with `hardci_flash_firmware` using `image_path` (usually `build/firmware.elf`), or first call `hardci_artifact_upload` and flash the returned `artifact_id`. Pass `reset_after_flash: true` only when a post-flash reset is explicitly needed.
5. For serial feedback: `hardci_com_session_start`, stimulate with `hardci_com_write`, read with `hardci_com_read`, stop with `hardci_com_session_stop`.
6. For CAN: `hardci_can_session_start`, `hardci_can_send`, `hardci_can_read`, `hardci_can_session_stop`.
7. For simulated sensors, loads, and fault states: `hardci_adapter_session_start`, `hardci_adapter_set_value`, `hardci_adapter_inject_fault`, `hardci_adapter_measure`, `hardci_adapter_clear_fault`, `hardci_adapter_session_stop`.
8. Read the tool result and `hardci_get_last_report`; diagnose failures with `hardci_classify_last_error`.

Healthy probe and flash signals: `target_detected: true`, `success_confirmed: true`, `verify: true`, an intentional `reset_after_flash` value, plus `report_path` and `log_path` for auditability.

Do not use raw OpenOCD commands, arbitrary COM-port shell tools, direct CAN adapter tools, or direct test-adapter access when an Agentic HIL MCP tool is available. Treat `permission_denied` as authoritative and stop.

## pytest Suites

For CI regression suites the installed package registers a pytest plugin: the `agentic_hil` fixture drives the same tools via `agentic_hil.call(name, arguments)`. Tests skip when no `.hardci/config.yaml` exists and fail loudly when the config is invalid. See `examples/pytest/` and `examples/nucleo-f446re_demo/tests/`.
