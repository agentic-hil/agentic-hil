# Troubleshooting

This page covers the most common AI-HIL setup and hardware-loop failures. Start with the supported first path from the README: STM32 Nucleo-F446RE, ST-Link, OpenOCD, and Node.js 16.16 or newer with npm. Current Node.js LTS is recommended.

Always inspect structured JSON first. The most useful fields are `ok`, `error_type`, `backend_error_type`, `summary`, `likely_causes`, `report_path`, and `log_path`.

## Windows Quick Notes

- If OpenOCD is installed but not on `PATH`, set `debugger.executable` explicitly, for example `C:/Program Files/OpenOCD/bin/openocd.exe`.
- Use forward slashes in YAML paths to avoid accidental escape sequences.
- Run `aihil com-ports` after reconnecting USB serial hardware.
- Configure Windows COM devices such as `COM5` under named `com_ports` ids, then use those ids from MCP COM tools.
- Configure PEAK CAN devices under named `can_buses` ids, for example `adapter: "peak"` and `channel: "PCAN_USBBUS1"` on Windows.
- Do not bypass AI-HIL with raw OpenOCD commands, arbitrary serial tools, or direct CAN adapter tools in agent workflows.

## 1. `aihil` Command Not Found

Symptom: the shell or MCP client cannot start AI-HIL.

Likely cause: `npm` is not available to the shell or MCP client, package download is blocked, or a direct `aihil` command is used even though `aihil` is not on `PATH`.

Fix:

```bash
npm exec --yes --package aihil -- aihil --version
npm exec --yes --package aihil -- aihil doctor
```

If npm reports an old Node.js version or an `engines` error, install or activate a supported Node.js/npm runtime, open a fresh shell if needed, and rerun the `npm exec` command. Current Node.js LTS is fine, but you do not need to install a specific Node.js patch version; any runtime accepted by `package.json` is fine. On Windows, `winget install OpenJS.NodeJS.LTS` is the usual direct path when `winget` is available. Do not use `--force`, `--ignore-engines`, or an older AI-HIL version to bypass the runtime requirement.

A persistent `aihil` CLI command is optional. Use it only when npm is configured to install into a user-owned location.

If developing from this checkout, run:

```bash
npm install
npm run build
node dist/main.js --version
```

## 2. `config_file_not_found` / `config_invalid`

Symptom: `aihil doctor` returns `error_type: "config_file_not_found"` or `error_type: "config_invalid"`.

Likely cause: `.aihil/config.yaml` is missing, the command is running from the wrong directory, the YAML syntax is invalid, or the file contains an unknown field or unsupported value.

Fix: run `aihil init` from the firmware project directory, edit only project-specific fields, then run `aihil doctor` again. Use the structured fields such as `field`, `allowed_fields`, `allowed_values`, and `expected_type` to fix schema errors.

## 3. `debugger_not_found` / `openocd_not_found`

Symptom: `aihil doctor` returns `ok: false`, `error_type: "debugger_not_found"`, or `backend_error_type: "openocd_not_found"`.

Likely cause: OpenOCD is not installed, not on `PATH`, or `debugger.executable` points to a missing file.

Fix: install OpenOCD, restart the shell or MCP client, and either leave `debugger.executable: null` or set it to the actual OpenOCD executable path.

Windows example:

```yaml
debugger:
  type: "openocd"
  executable: "C:/Program Files/OpenOCD/bin/openocd.exe"
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
```

## 4. `debugger_config_not_found`

Symptom: `backend_error_type` is `interface_config_not_found`, `target_config_not_found`, or `config_file_not_found`.

Likely cause: the OpenOCD package cannot find `interface/stlink.cfg` or `target/stm32f4x.cfg`, or the target config does not match the installed OpenOCD layout.

Fix: verify OpenOCD's script directory, keep the supported first path values for Nucleo-F446RE, and avoid replacing them unless the board or probe is actually different.

Reference values:

```yaml
debugger:
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
```

## 5. `adapter_not_found`

Symptom: OpenOCD starts but AI-HIL reports `error_type: "adapter_not_found"`.

Likely cause: ST-Link is not connected, the USB cable is charge-only, a driver is missing, or another process owns the probe.

Fix: reconnect the Nucleo board with a data-capable USB cable, close other debugger sessions, check OS driver or udev rules, then run `aihil doctor` and probe again.

## 6. `target_not_detected`

Symptom: `aihil_probe_target` returns `ok: false` with `error_type: "target_not_detected"`.

Likely cause: target power is missing, SWD is disabled by firmware, jumpers are wrong, the board is held in reset, or the config is for the wrong target family.

Fix: confirm board power LEDs, keep `target/stm32f4x.cfg` for Nucleo-F446RE, disconnect other debug tools, power-cycle the board, and probe again before flashing.

## 7. `permission_denied`

Symptom: an MCP tool returns `error_type: "permission_denied"`.

Likely cause: the local `.aihil/config.yaml` policy intentionally disables that action.

Fix: stop and ask the human operator. Do not work around the policy with raw OpenOCD, direct COM-port tools, direct CAN adapter tools, mass erase, or shell commands. The local AI-HIL config is authoritative.

## 8. Artifact Not Found Or Fails Validation

Symptom: `aihil_flash_firmware` returns `artifact_not_found` or `artifact_validation_failed` with fields such as `allowed_root: false`, `allowed_extension: false`, `elf_header: false`, `hex_parseable: false`, or `bin_size_plausible: false`.

Likely cause: the firmware was not built, the path is wrong, the artifact is outside configured `artifacts.allowed_roots`, the extension is not allowed, or the selected file is not a valid firmware artifact.

Fix: build firmware first and flash `.elf`, `.hex`, or `.bin` from an allowed root, usually `build/firmware.elf`. Only add another extension to `.aihil/config.yaml` if the project intentionally produces that format.

Reference values:

```yaml
artifacts:
  allowed_roots:
    - "build"
```

## 9. `flash_failed`, `verify_failed`, `reset_failed`, Or `timeout`

Symptom: probe works, but flashing, verification, reset, or a debugger action times out.

Likely cause: the firmware image does not match the target memory layout, flash is locked, the target is unstable, reset wiring is wrong, the wrong OpenOCD target config is used, or `debugger.timeout_s` is too low for this operation.

Fix: inspect `log_path`, confirm the artifact is for STM32F446RE, keep `target/stm32f4x.cfg` for the supported first path, power-cycle the board, then retry probe before retrying flash. Increase `debugger.timeout_s` only when the operation is valid but consistently too slow.

## 10. COM Port Does Not Work

Symptom: COM tools cannot start a session, return permission errors, or read no expected serial text.

Likely cause: the port is not configured under `com_ports`, the wrong device name is used, the baud rate is wrong, another program owns the port, or serial access permissions are missing.

Fix: run `aihil com-ports`, add only the approved project port to `.aihil/config.yaml`, close other serial monitors, and use MCP COM tools with the configured `port_id` instead of opening host COM devices directly. On Windows, the configured device can be a value such as `COM5`; the MCP calls should still use the AI-HIL id such as `dut_uart`.

## 11. CAN Bus Does Not Work

Symptom: CAN tools cannot start a session, return `can_bus_not_configured`, `can_adapter_backend_not_available`, `config_invalid`, permission errors, or read no expected frames.

Likely cause: the bus is not configured under `can_buses`, the wrong `bus_id` is used, `allow_can_read` or `allow_can_write` is disabled, the adapter backend is unavailable on the host, another program owns the adapter, or the `channel` value is for a different backend.

Fix: add only the approved project bus to `.aihil/config.yaml`, close other CAN tools, and use MCP CAN tools with the configured `bus_id` instead of opening host CAN adapters directly. On Windows with PEAK, start with `adapter: "peak"` and `channel: "PCAN_USBBUS1"`. On Linux SocketCAN, use `adapter: "socketcan"` and a network interface such as `can0`; `PCAN_USBBUS*` and numeric PCAN handles are Windows PCANBasic channels, not SocketCAN interface names.
