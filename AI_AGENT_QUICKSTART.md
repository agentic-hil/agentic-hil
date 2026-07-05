# AI Agent Quickstart

Use HardCI as the local MCP server for embedded firmware development and embedded hardware actions.

This file is for agents. Humans should start with `README.md` and use `TROUBLESHOOTING.md` for operator-facing diagnostics.

If you were given only the HardCI repository URL and asked to set it up: run the fast path below, install the HardCI skill into your own skill directory, configure the firmware project, then return to the firmware project. Do not clone, checkout, or vendor the HardCI source tree into the firmware project for normal setup.

## Ground Rules

- Never use `sudo` or any administrator privileges for the HardCI installation. Every step below works user-local.
- Never use `pip install --break-system-packages`, and do not install into the system Python (PEP 668 environments will refuse, and they are right).
- If the board, debugger, COM port, or artifact path cannot be inferred, ask one concise question instead of guessing.

## Reference Setup

Prefer the supported first path unless the firmware project or user clearly says otherwise:

- STM32 Nucleo-F446RE (a complete demo lives in `examples/nucleo-f446re_demo/`).
- ST-Link with OpenOCD (`interface/stlink.cfg`, `target/stm32f4x.cfg`).
- Python 3.10 or newer.
- Firmware artifacts under `build/`.

## Start HardCI

Fast path, in order — stop at the first step that works:

1. If `hardci --version` works, do not reinstall.
2. If `uv` is available, run HardCI without installing anything (no admin rights, no `PATH` changes):

```bash
uvx hardci --version
```

3. If the PyPI package lookup fails, use the repository as the package source (this is a package source only — it does not create a checkout):

```bash
uvx --from git+https://github.com/hp-8472/hardci hardci --version
```

4. If `uv` is missing but `pipx` is available, the equivalents are `pipx run hardci --version` and `pipx run --spec git+https://github.com/hp-8472/hardci hardci --version`.
5. If neither `uv` nor `pipx` is available, install `uv` user-locally (no admin rights; installs to `~/.local/bin`):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

then rerun step 2. A missing runner is a remediable setup prerequisite, not a reason to refuse the HardCI setup.

For the MCP server entry it is usually better to install the `hardci` command persistently (still user-local, still no admin rights):

```bash
uv tool install hardci        # or from the repository: uv tool install git+https://github.com/hp-8472/hardci
```

`pipx install hardci` is the equivalent. Both place `hardci` into `~/.local/bin`; if that is not on `PATH`, fix it with `uv tool update-shell` or `pipx ensurepath` — never with admin rights.

## Install Agent Skill

Agent-driven HardCI installation includes installing the bundled `hardci-config-setup` skill into the active agent's user-level skill directory after the CLI is available:

```bash
hardci skill-install --agent <agent>          # or: uvx hardci skill-install --agent <agent>
```

Supported agent names and aliases: `opencode`/`open-code`, `claude-code`/`claude`, `codex`/`codex-cli`/`openai-codex`. For other skill-capable agents use `--agent <name> --target <path>` with that agent's documented user-level skill directory. The CLI package is authoritative: if the installed skill's front-matter version differs from `hardci --version`, rerun `skill-install`.

## Configure Each Project

In every firmware project that should use HardCI:

```bash
hardci init                 # writes the starter .hardci/config.yaml
# edit .hardci/config.yaml: target, debugger configs, allowed artifact roots,
# named com_ports / can_buses / adapters — keep the safety policy restrictive
hardci doctor               # validates config and checks the debugger
hardci mcp-config --output .mcp.json
```

Keep `.hardci/` with the project: it defines that project's hardware policy, reports, logs, and allowed artifact locations. Do not reinstall HardCI inside every project.

Expected healthy `hardci doctor` result: `ok: true`, `summary: "HardCI configuration loaded and debugger checked."`, and a nested debugger result with `ok: true`.

## Configure MCP

`.mcp.json` is only the MCP launch entry. The default written by `hardci mcp-config` assumes `hardci` is on `PATH`:

```json
{
  "mcpServers": {
    "hardci": {
      "command": "hardci",
      "args": ["mcp-stdio", "--config", ".hardci/config.yaml"]
    }
  }
}
```

If `hardci` is not on `PATH`, use the runner form instead: `"command": "uvx", "args": ["hardci", "mcp-stdio", "--config", ".hardci/config.yaml"]`.

`mcp-stdio` is project-scoped and JSON-RPC only. COM tool calls pass `port_id`, CAN tool calls pass `bus_id`, and test-adapter tool calls pass `adapter_id` as tool arguments. For a continuous plain-text serial channel use a separate `hardci com-stdio --config .hardci/config.yaml --port <port_id>` process — never mix plain text into `mcp-stdio`.

## Use The Tools

Use `tools/list` to discover available MCP tools, then follow this loop:

1. Build firmware.
2. Check debugger availability with `hardci_debugger_info` if setup is unclear.
3. Probe with `hardci_probe_target`.
4. Flash with `hardci_flash_firmware` using `image_path` (usually `build/firmware.elf`), or first call `hardci_artifact_upload` and flash the returned `artifact_id`.
5. For serial feedback: `hardci_com_session_start`, stimulate with `hardci_com_write`, read with `hardci_com_read`, stop with `hardci_com_session_stop`.
6. For CAN: `hardci_can_session_start`, `hardci_can_send`, `hardci_can_read`, `hardci_can_session_stop`.
7. For simulated sensors, loads, and fault states: `hardci_adapter_session_start`, `hardci_adapter_set_value`, `hardci_adapter_inject_fault`, `hardci_adapter_measure`, `hardci_adapter_clear_fault`, `hardci_adapter_session_stop`.
8. Read the tool result and `hardci_get_last_report`; diagnose failures with `hardci_classify_last_error`.

Healthy probe and flash signals: `target_detected: true`, `success_confirmed: true`, `verify: true`, `reset_after_flash: true`, plus `report_path` and `log_path` for auditability.

Do not use raw OpenOCD commands, arbitrary COM-port shell tools, direct CAN adapter tools, or direct test-adapter access when a HardCI MCP tool is available. Treat `permission_denied` as authoritative and stop.

## pytest Suites

For CI regression suites the installed package registers a pytest plugin: the `hardci` fixture drives the same tools via `hardci.call(name, arguments)`. Tests skip when no `.hardci/config.yaml` exists and fail loudly when the config is invalid. See `examples/pytest/` and `examples/nucleo-f446re_demo/tests/`.
