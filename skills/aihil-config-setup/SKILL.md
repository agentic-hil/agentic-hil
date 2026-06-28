---
name: aihil-config-setup
description: Create and validate a project-local .aihil/config.yaml for AI-HIL without weakening hardware safety policy.
metadata:
  origin: AI-HIL
---

# AI-HIL Config Setup

Use this skill when a user asks to set up AI-HIL for a firmware project, create `.aihil/config.yaml`, fix AI-HIL configuration errors, or prepare a project for the local AI-HIL MCP server.

## Core Rule

Do not hand-write `.aihil/config.yaml` from memory. Use `aihil init` to create the starter config, then make the smallest project-specific edits and validate with `aihil doctor`.

The schema bundled with the installed `aihil` Node.js package is authoritative. A project-local schema copy is not required for runtime validation.

## Supported First Path

Use STM32 Nucleo-F446RE, ST-Link, OpenOCD, Node.js 16.16 or newer with npm, `interface/stlink.cfg`, `target/stm32f4x.cfg`, and firmware artifacts under `build/` as the reference setup unless project files or the user clearly specify a different target. Current Node.js LTS is recommended.

If the board, MCU family, debugger interface, target config, COM port, or artifact root cannot be inferred from project files, ask one concise question instead of guessing.

If `aihil` is not installed because Node.js is missing or too old, install or activate a supported Node.js/npm runtime before running `aihil init`. Current Node.js LTS is fine, but do not pin a specific Node.js patch version unless asked; any runtime accepted by `package.json` is fine. Do not refuse the setup for an old Node.js runtime, and do not bypass `engines` with `--force`, `--ignore-engines`, or an older AI-HIL version.

## Safety Boundaries

- Never use raw OpenOCD or debugger commands for hardware actions.
- Never enable `permissions.allow_raw_debugger_commands`.
- Never enable `permissions.allow_mass_erase`.
- Never add arbitrary COM devices for convenience; COM access must use named `com_ports` entries approved for this project.
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
