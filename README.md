# AI-HIL

[![Node CI](https://github.com/hp-8472/aihil/actions/workflows/ci.yml/badge.svg)](https://github.com/hp-8472/aihil/actions/workflows/ci.yml)

**AI-HIL accelerates embedded development by putting your real hardware in the coding-agent loop and making hardware tests reproducible.**

It turns firmware work into a hardware-in-the-loop cycle: edit, build, probe, flash, reset, read logs, diagnose, improve, repeat. AI-HIL is the safe MCP stdio control layer that lets agents run and repeat that loop on real boards through configured tools instead of raw debugger or COM-port access.

## Quick Start

```bash
npm i -g aihil
git clone https://github.com/hp-8472/aihil.git
cd aihil/examples/nucleo-f446re_demo
aihil init
aihil doctor
```

From a local checkout of this repository, use `npm install --global .` instead of `npm i -g aihil`.

The demo includes a prebuilt first-run ELF at `build/Debug/nucleo-f446re_demo.elf`, so you can validate the hardware loop before installing an ARM toolchain. The demo `.aihil/config.yaml` is intentionally local machine state. Create it with `aihil init`, then edit only host-specific fields such as a non-`PATH` OpenOCD executable or configured COM ports. Keep the firmware artifact root as `build/`.

## 60-Second Nucleo Loop

With a NUCLEO-F446RE connected over USB/ST-LINK and a local `.aihil/config.yaml` created by `aihil init`:

```bash
aihil doctor
aihil mcp-config > .mcp.json
```

Run `cmake --preset Debug` and then `cmake --build --preset Debug` only when you want to rebuild the demo firmware locally.

If OpenOCD is not on `PATH` or serial feedback is needed, edit the local `.aihil/config.yaml` before running `aihil doctor`. Do not commit machine-specific `.aihil/` files from the demo project.

Open Claude Code, opencode, Codex, or another MCP-capable coding agent in `examples/nucleo-f446re_demo` and ask:

```text
Use AI-HIL to probe the target, flash build/Debug/nucleo-f446re_demo.elf, reset it in run mode, read the last report, and read the configured COM port if one is available.
```

Expected firmware-in-the-loop path:

```text
change firmware
build firmware
aihil_probe_target
aihil_flash_firmware
aihil_reset_target
aihil_get_last_report and optional COM read
use real hardware feedback for the next firmware patch
repeat
```

## Reproducible Hardware Tests

AI-HIL treats every hardware run as something that should be repeatable. The local `.aihil/` setup captures the target, debugger, permissions, allowed firmware artifacts, reports, and logs so the next agent, developer, or CI job can inspect the same hardware-in-the-loop test under the same constraints.

Today, `.aihil/config.yaml` contains both portable project choices and machine-specific values such as OpenOCD executable paths and COM devices. For checked-in examples, keep `.aihil/` ignored and document the stable values instead of committing a host-specific config file.

Reproducibility is part of the value: a passing or failing firmware result should be explainable from structured JSON, raw OpenOCD logs, COM logs, the flashed artifact path, and the project configuration.

## Why It Exists

AI agents can edit firmware quickly, but embedded development only speeds up when the agent can run the firmware on the actual board, learn from the result, and reproduce the same test later. AI-HIL closes that gap by making real hardware part of the development loop while keeping hardware access bounded, configured, reproducible, and auditable.

## Audience

This README is for human developers and hardware operators.

Agent-facing instructions live in:

- [`AGENTS.md`](AGENTS.md)
- [`AI_AGENT_QUICKSTART.md`](AI_AGENT_QUICKSTART.md)
- [`skills/aihil-config-setup/SKILL.md`](skills/aihil-config-setup/SKILL.md)

Troubleshooting lives in [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).

## Supported First Path

The official reference setup is deliberately narrow:

- Board: STM32 Nucleo-F446RE.
- Debug probe: ST-Link, including the onboard Nucleo ST-Link.
- Debug backend: OpenOCD.
- Host runtime: Node.js LTS with npm.
- OpenOCD interface config: `interface/stlink.cfg`.
- OpenOCD target config: `target/stm32f4x.cfg`.
- Firmware artifact root: `build/`.
- Firmware artifact formats: `.elf`, `.hex`, or `.bin`.

Other OpenOCD-supported boards, probes, and targets may work when represented in `.aihil/config.yaml`, but the supported first path is the baseline for documentation, examples, and issue reproduction.

## How It Works

```text
AI agent edits firmware
  -> build artifact
AI-HIL MCP stdio
  -> .aihil/config.yaml policy
OpenOCD / ST-Link / configured COM ports
  -> real target board
AI-HIL reports and logs
  -> AI agent improves firmware
  -> repeat
```

The agent does not receive a generic OpenOCD shell. It receives narrow tools such as `aihil_probe_target`, `aihil_flash_firmware`, `aihil_reset_target`, and configured COM-port tools.

## Install

Install the `aihil` command once on the local machine:

```bash
npm i -g aihil
```

From this repository checkout, install the local version with:

```bash
npm install --global .
```

For local AI-HIL development:

```bash
npm install
npm test
```

AI-HIL is a Node.js CLI. The npm package builds TypeScript during installation and installs the `aihil` executable on `PATH` when installed globally.

## Distribution

npm is the primary distribution channel for AI-HIL. The repository is a native Node.js CLI with `package.json`, TypeScript builds, and a package `bin` entry for the `aihil` command.

PyPI is intentionally not a primary target. Publish a Python package only if AI-HIL grows a deliberate Python wrapper for Python-heavy embedded teams.

When publishing to npm, use trusted publishing from GitHub Actions with OIDC and npm provenance. This avoids long-lived npm tokens in repository secrets and records the build provenance for the published package.

AI-HIL is local-first because real hardware access depends on host USB, ST-Link, OpenOCD, and serial/COM devices. Keep the first-run path on the host through the npm CLI and MCP stdio.

Later packaging candidates are Homebrew, Scoop or WinGet, and optional single-file binaries.

## Per-Project Setup

Run setup from the firmware project directory, not from the AI-HIL source repository:

```bash
aihil init
aihil doctor
aihil mcp-config > .mcp.json
```

Each firmware project owns its own `.aihil/` directory. That directory contains the local target configuration, hardware permissions, allowed artifact roots, reports, logs, and uploaded artifacts. Treat it as local machine state unless the project has an explicit policy for sharing sanitized AI-HIL config.

For the supported first path, start from `aihil init` and set the important fields like this:

```yaml
target:
  name: "nucleo-f446re"
  controller: "stm32f446re"

debugger:
  type: "openocd"
  executable: null
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
  timeout_s: 60

artifacts:
  allowed_roots:
    - "build"
  upload_directory: ".aihil/artifacts"
  allowed_extensions:
    - ".elf"
    - ".hex"
    - ".bin"
  max_upload_size_mb: 64
  allow_upload: true

permissions:
  allow_probe: true
  allow_flash: true
  allow_reset: true
  allow_com_read: true
  allow_com_write: true
  allow_raw_debugger_commands: false
  allow_mass_erase: false
```

Add `com_ports` only for serial ports that are intentionally part of the project setup.

## Expected Output

The exact paths, timestamps, OpenOCD version, elapsed times, COM device names, and SHA-256 values will differ by machine. The shape and key fields should match these examples.

### `aihil doctor`

```json
{
  "ok": true,
  "tool": "aihil_doctor",
  "summary": "AI-HIL configuration loaded and debugger checked.",
  "config_path": ".aihil/config.yaml",
  "mcp": {
    "transport": "stdio",
    "command": "aihil",
    "args": [
      "mcp-stdio",
      "--config",
      ".aihil/config.yaml"
    ]
  },
  "target": {
    "name": "nucleo-f446re",
    "controller": "stm32f446re"
  },
  "com_ports": {
    "dut_uart": {
      "device": "COM5",
      "baudrate": 115200,
      "encoding": "utf-8"
    }
  },
  "debugger": {
    "ok": true,
    "tool": "aihil_debugger_info",
    "backend": "openocd",
    "executable": "C:/Program Files/OpenOCD/bin/openocd.exe",
    "version": "Open On-Chip Debugger 0.12.0",
    "summary": "OpenOCD is available."
  }
}
```

If no serial port is configured, `com_ports` is `{}`.

### Successful Probe Report

After an agent calls `aihil_probe_target`, the MCP tool result and `.aihil/reports/last-report.json` should look like this:

```json
{
  "ok": true,
  "tool": "aihil_probe_target",
  "backend": "openocd",
  "started_at": "2026-06-26T10:14:23.121Z",
  "finished_at": "2026-06-26T10:14:24.088Z",
  "elapsed_ms": 967,
  "summary": "Target detected through OpenOCD.",
  "log_path": ".aihil/logs/openocd-20260626T101423121Z-aihil_probe_target.log",
  "success_confirmed": true,
  "target_detected": true,
  "report_path": ".aihil/reports/last-report.json"
}
```

### Successful Flash Report

After an agent calls `aihil_flash_firmware` with `image_path: "build/firmware.elf"`, a successful report should look like this:

```json
{
  "ok": true,
  "tool": "aihil_flash_firmware",
  "backend": "openocd",
  "started_at": "2026-06-26T10:15:02.442Z",
  "finished_at": "2026-06-26T10:15:06.659Z",
  "elapsed_ms": 4217,
  "summary": "Firmware flashed, verified, and target reset.",
  "log_path": ".aihil/logs/openocd-20260626T101502442Z-aihil_flash_firmware.log",
  "success_confirmed": true,
  "artifact": {
    "source": "path",
    "path": "build/firmware.elf",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  },
  "verify": true,
  "reset_after_flash": true,
  "report_path": ".aihil/reports/last-report.json"
}
```

For failures, inspect `ok`, `error_type`, `backend_error_type`, `summary`, `likely_causes`, `report_path`, and `log_path` before changing configuration or firmware.

### `aihil_get_last_report`

`aihil_get_last_report` wraps the most recent report from `.aihil/reports/last-report.json`:

```json
{
  "ok": true,
  "tool": "aihil_get_last_report",
  "report": {
    "ok": true,
    "tool": "aihil_flash_firmware",
    "backend": "openocd",
    "summary": "Firmware flashed, verified, and target reset.",
    "artifact": {
      "source": "path",
      "path": "build/Debug/nucleo-f446re_demo.elf",
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    },
    "verify": true,
    "reset_after_flash": true,
    "report_path": ".aihil/reports/last-report.json",
    "log_path": ".aihil/logs/openocd-20260626T101502442Z-aihil_flash_firmware.log"
  }
}
```

If no hardware action has written a report yet, the result is:

```json
{
  "ok": false,
  "tool": "aihil_get_last_report",
  "error_type": "report_not_found",
  "summary": "No AI-HIL report has been written yet."
}
```

### `aihil_classify_last_error`

After a failed hardware action, `aihil_classify_last_error` returns a compact diagnosis from the most recent report:

```json
{
  "ok": true,
  "tool": "aihil_classify_last_error",
  "error_type": "target_not_detected",
  "backend_error_type": "target_not_detected",
  "summary": "OpenOCD could not detect the target.",
  "likely_causes": [
    "target board is not powered",
    "debug probe is disconnected or already in use",
    "wrong OpenOCD interface or target config",
    "SWD/JTAG wiring or boot-mode issue"
  ],
  "report_path": ".aihil/reports/last-report.json",
  "log_path": ".aihil/logs/openocd-20260626T101423121Z-aihil_probe_target.log"
}
```

If the last report succeeded, the classifier returns `ok: true`, `error_type: null`, and `summary: "Last AI-HIL report did not contain an error."`.

## MCP Client Configuration

AI-HIL uses MCP over stdio. Generate project-local MCP discovery config with:

```bash
aihil mcp-config > .mcp.json
```

Example `.mcp.json`:

```json
{
  "mcpServers": {
    "aihil": {
      "command": "aihil",
      "args": [
        "mcp-stdio",
        "--config",
        ".aihil/config.yaml"
      ]
    }
  }
}
```

`mcp-stdio` is project-scoped. Do not add `--port` to it. COM MCP tool calls provide configured `port_id` values when needed.

## Plain COM Text Stdio

Use the MCP COM tools for normal agent workflows. If a separate plain-text serial channel is explicitly needed, start:

```bash
aihil com-stdio --config .aihil/config.yaml --port dut_uart
```

`com-stdio` is not MCP. It binds one plain text stream to one configured COM port. Do not mix COM text into `mcp-stdio`; MCP stdout must remain JSON-RPC only.

## Safety Model

AI-HIL's safety boundary is the project-local `.aihil/config.yaml` file.

The default model is:

- Probe, flash, reset, and COM actions require explicit permissions.
- Raw debugger commands are not exposed.
- Mass erase is disabled.
- Firmware paths must be under configured artifact roots.
- COM access is limited to named `com_ports` entries.
- Every hardware action returns structured JSON and writes raw logs for human inspection.

## Repository Layout

```text
.
|-- AGENTS.md
|-- AI_AGENT_QUICKSTART.md
|-- CLAUDE.md
|-- README.md
|-- TROUBLESHOOTING.md
|-- skills/
|-- src/aihil/
|-- tests-ts/
`-- package.json
```

## Agent Entry Point

If you want an AI coding agent to set up a firmware project with AI-HIL, open the firmware project and say:

```text
Install https://github.com/hp-8472/aihil and use it for this firmware project.
```

The agent should install `aihil`, return to the firmware project, and follow `AGENTS.md`, `AI_AGENT_QUICKSTART.md`, and `skills/aihil-config-setup/SKILL.md`. It should not vendor the AI-HIL source tree into the firmware project unless you explicitly ask for that.

## License

Copyright 2026 Hannes Pauli.

This project is licensed under the Apache License 2.0. See [LICENSE](LICENSE).
