---
name: aihil-config-setup
description: Create and validate a project-local .aihil/config.yaml for AI-HIL embedded firmware hardware-in-the-loop workflows without weakening hardware safety policy.
metadata:
  origin: AI-HIL
  aihil_version: "0.4.0"
---

# AI-HIL Config Setup

Use this skill when a user asks to set up AI-HIL for an embedded firmware project, create `.aihil/config.yaml`, fix AI-HIL configuration errors, or prepare a project for the local AI-HIL MCP server.

## Core Rule

Do not hand-write `.aihil/config.yaml` from memory. Use `aihil init` to create the starter config, then make the smallest project-specific edits and validate with `aihil doctor`.

The schema bundled with the installed `aihil` Node.js package is authoritative. A project-local schema copy is not required for runtime validation.

## Supported First Path

Use STM32 Nucleo-F446RE, ST-Link, OpenOCD, Node.js 16.16 or newer with npm, `interface/stlink.cfg`, `target/stm32f4x.cfg`, and firmware artifacts under `build/` as the reference setup unless project files or the user clearly specify a different target. Current Node.js LTS is recommended.

If the board, MCU family, debugger interface, target config, COM port, CAN bus, or artifact root cannot be inferred from project files, ask one concise question instead of guessing.

## Fast Nucleo-F446RE Path

When the project and user match the supported first path, skip broad discovery and do this directly:

1. Run `aihil init` if `.aihil/config.yaml` is missing.
2. Set `target.name: "NUCLEO-F446RE"`, `target.controller: "STM32F446RET6"`, `debugger.interface_cfg: "interface/stlink.cfg"`, and `debugger.target_cfg: "target/stm32f4x.cfg"`.
3. For ST/STM32 targets, check existing environment variables before hard-coded paths. Prefer `PATH`, `OPENOCD`, `OPENOCD_HOME`, `OPENOCD_SCRIPTS`, `STM32_PROGRAMMER_CLI`, `STM32CUBEIDE_PATH`, and `STLINK_PATH` when they point to an existing OpenOCD/ST-Link toolchain. On Windows, also check `%LOCALAPPDATA%/stm32cube/bundles` for STM32Cube-managed tools such as `programmer/*/bin/STM32_Programmer_CLI.exe`, `stlink-gdbserver/*/bin/ST-LINK_gdbserver.exe`, and `stlink-server/*/bin/stlinkserver.exe`. Derive only supported config values from them, usually `debugger.executable`, `debugger.interface_cfg`, and `debugger.target_cfg`.
4. If the user gives a COM device, add it directly as `com_ports.dut_uart.device`; set `com_ports.dut_uart.baudrate` to the baud rate configured in the firmware code (`HAL_UART_Init`, LL init structs, register setup, or project UART constants). Use `encoding: "ascii"` unless the project or firmware output requires a different encoding.
5. If the user gives a CAN adapter, add it directly as `can_buses.dut_can`; on Windows with PEAK use `adapter: "peak"`, `channel: "PCAN_USBBUS1"`, and the intended `bitrate`, while Linux SocketCAN uses `adapter: "socketcan"` and a network interface such as `can0`.
6. Run `aihil doctor`. `.mcp.json` is only the MCP launch configuration, not the tool list. Prefer the stable portable entry shipped as `dist/templates/mcp.json`, which runs `aihil mcp-stdio --config .aihil/config.yaml` when `aihil` is on `PATH`. If the MCP client cannot resolve `aihil`, edit `.mcp.json` for that machine instead of changing reusable project instructions.

For UART smoke tests, start the AI-HIL COM session before the reset or flash that should emit text. Accumulate short reads until the expected substring appears, then stop immediately; avoid fixed multi-second waits unless no data arrives. Once the expected text is observed, do not inspect COM logs or reports unless a failure needs diagnosis.

For AI-HIL 0.3.x, `mcp-stdio` expects newline-delimited JSON-RPC on stdio. Do not use `Content-Length` framing for quick smoke clients.

For tiny STM32 projects, check `ninja` early. If a preset requires Ninja and `ninja` is missing, skip the failing CMake build attempt. When the source set is obvious, a direct `arm-none-eabi-gcc` build into `build/` is an acceptable firmware-build fallback before AI-HIL probe/flash.

Before installing, check `aihil --version`. On Windows, also try `aihil.cmd --version`; if that works, do not reinstall. If `aihil` is not installed because Node.js is missing or too old, install or activate a supported Node.js/npm runtime before running `aihil init`. Current Node.js LTS is fine, but do not pin a specific Node.js patch version unless asked; any runtime accepted by `package.json` is fine. Do not refuse the setup for an old Node.js runtime, and do not bypass `engines` with `--force`, `--ignore-engines`, or an older AI-HIL version.

## Version Contract

This skill is tied to the AI-HIL version in its front matter. The installed `aihil` CLI is authoritative. If the skill version differs from `aihil --version`, update and register the skill from the installed CLI with `aihil skill-install --agent <agent>`; do not downgrade the CLI to match an older skill. Supported defaults include `opencode`, `claude-code`, and `codex`; use `--target` for other skill-capable agents.

Use `tools/list` as the runtime source of truth for MCP tools. This setup skill should not probe, flash, reset, or open COM sessions unless the user asks for hardware validation.

## Safety Boundaries

- Never use raw OpenOCD or debugger commands for hardware actions.
- Never enable `permissions.allow_raw_debugger_commands`.
- Never enable `permissions.allow_mass_erase`.
- Never add arbitrary COM devices for convenience; COM access must use named `com_ports` entries approved for this project.
- Never add arbitrary CAN adapters for convenience; CAN access must use named `can_buses` entries approved for this project.
- AI-HIL uses MCP over stdio; do not add listener configuration.
- Do not flash, reset, probe, or otherwise touch hardware while only setting up config unless the user asks for the hardware workflow.
- Stop if `aihil doctor` or an MCP tool reports `permission_denied`; the local config is authoritative.

## Workflow

1. Confirm you are in the firmware project directory, not the AI-HIL source repo unless the task is AI-HIL development.
2. Check whether `.aihil/config.yaml` already exists.
3. If it exists, read it, preserve existing policy decisions, and do not overwrite it with `aihil init --force` unless the user explicitly asks.
4. If it is missing, run `aihil init` from the firmware project directory.
5. Edit only project-specific fields.
6. Run `aihil doctor` and inspect the JSON result.
7. If `error_type` is `config_invalid`, fix the config using `summary`, `field`, `allowed_fields`, and `allowed_values`.
8. If the config is valid but debugger detection fails, report the debugger issue separately instead of loosening config policy.

## Fields To Customize

Only change these fields unless the user explicitly asks for a broader policy change:

- `target.name`: board or project name, for example `fan-controller-v1`.
- `target.controller`: MCU or family, for example `stm32f4`.
- `debugger.executable`: usually `openocd` or `null` to use `PATH`.
- `debugger.probe_id`: optional debug probe serial number when multiple probes are connected.
- `debugger.interface`: direct debugger interface such as `SWD` when `debugger.type` is `stlink`.
- `debugger.flash_address`: required for `.bin` artifacts when `debugger.type` is `stlink`; not needed for `.elf` or `.hex`.
- `debugger.interface_cfg`: OpenOCD interface config, for example `interface/stlink.cfg`.
- `debugger.target_cfg`: OpenOCD target config, for example `target/stm32f4x.cfg`.
- `artifacts.allowed_roots`: firmware build output directories, usually `build`.
- `artifacts.allowed_extensions`: keep `.elf`, `.hex`, and `.bin` unless the project actually emits another firmware artifact format.
- `com_ports.<port_id>.device`: approved serial device for this project, for example `COM5` or `/dev/ttyUSB0`.
- `com_ports.<port_id>.baudrate`: serial baud rate for the target or fixture.
- `com_ports.<port_id>.encoding`: text encoding for decoded feedback, usually `utf-8`.
- `can_buses.<bus_id>.adapter`: approved CAN adapter backend, usually `peak`, `socketcan`, or `process`.
- `can_buses.<bus_id>.channel`: adapter channel, for example `PCAN_USBBUS1` on Windows PEAK or `can0` on Linux SocketCAN.
- `can_buses.<bus_id>.bitrate`: CAN bus bitrate, for example `500000`.
- `can_buses.<bus_id>.fd`: whether CAN FD frames are allowed for this bus.
- `can_buses.<bus_id>.data_bitrate`: optional CAN FD data phase bitrate.
- `can_buses.<bus_id>.pcanbasic_dll`: optional PEAK PCANBasic DLL path when it is not discoverable through `PATH`.
- `can_buses.<bus_id>.executable`: bridge executable for `adapter: "process"`.
- `can_buses.<bus_id>.args`: extra bridge arguments for `adapter: "process"`.

If the supported first path does not match the project, change only the fields needed for the actual board and debugger.

## Validation Loop

After each edit run:

```bash
aihil doctor
```

Interpret the result as structured data:

- `ok: true`: config loaded and debugger check succeeded.
- `error_type: config_invalid`: fix YAML, field names, field types, or enum values.
- `error_type: config_file_not_found`: run `aihil init` or verify the working directory.
- debugger failures: keep the config policy intact and report missing OpenOCD, missing probe, missing OpenOCD config files, or target availability separately.

## Optional Editor Schema

Do not create `.aihil/config.schema.json` by default. If the user specifically wants a schema file for an editor or external tooling, export the bundled schema:

```bash
aihil schema --output config.schema.json
```

This exported file is only for tooling. Runtime validation still uses the schema bundled with the installed AI-HIL package.

## Completion Report

Finish with a concise summary:

- Config path created or updated.
- Project-specific fields changed.
- `aihil doctor` result.
- Remaining operator action, if any, such as installing OpenOCD, connecting a probe, or selecting the correct target config.
