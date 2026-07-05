# Troubleshooting

This page covers the most common HardCI setup and hardware-loop failures. Start with the supported first path from the README: STM32 Nucleo-F446RE, ST-Link, OpenOCD, and Python 3.10 or newer.

Always inspect structured JSON first. The most useful fields are `ok`, `error_type`, `backend_error_type`, `summary`, `likely_causes`, `report_path`, and `log_path`.

## Windows Quick Notes

- If OpenOCD is installed but not on `PATH`, set `debugger.executable` explicitly, for example `C:/Program Files/OpenOCD/bin/openocd.exe`.
- Use forward slashes in YAML paths to avoid accidental escape sequences.
- Run `hardci com-ports` after reconnecting USB serial hardware.
- Configure Windows COM devices such as `COM5` under named `com_ports` ids, then use those ids from MCP COM tools.
- Configure PEAK CAN devices under named `can_buses` ids, for example `adapter: "peak"` and `channel: "PCAN_USBBUS1"` on Windows.

## 1. `hardci` Command Not Found

Symptom: the shell or MCP client cannot start HardCI.

Likely cause: HardCI is not installed, `~/.local/bin` is not on `PATH`, or the MCP client starts with a minimal environment.

Fix — all user-local, never with admin rights:

```bash
uvx hardci --version                                          # run without installing
uvx --from git+https://github.com/hp-8472/hardci hardci --version   # repository as package source
uv tool install hardci                                        # persistent user-local install
```

`pipx run hardci` / `pipx install hardci` are equivalents. If `hardci` is installed but not found, add `~/.local/bin` to `PATH` with `uv tool update-shell` or `pipx ensurepath` and open a fresh shell. If neither `uv` nor `pipx` exists, install `uv` user-locally first (`curl -LsSf https://astral.sh/uv/install.sh | sh`). Never use `sudo pip` or `pip install --break-system-packages`.

In `.mcp.json`, a runner form avoids the `PATH` question entirely: `"command": "uvx", "args": ["hardci", "mcp-stdio", "--config", ".hardci/config.yaml"]`.

## 2. `config_file_not_found` / `config_invalid` / `config_unreadable`

Symptom: `hardci doctor` returns one of these `error_type` values.

Likely cause: `.hardci/config.yaml` is missing, the command runs from the wrong directory, the YAML is invalid or not UTF-8, or the file contains an unknown field or unsupported value.

Fix: run `hardci init` from the firmware project directory, edit only project-specific fields, then run `hardci doctor` again. Use the structured fields such as `field`, `allowed_fields`, `allowed_values`, and `expected_type` to fix schema errors.

## 3. `debugger_not_found`

Symptom: `hardci doctor` returns `ok: false` with `error_type: "debugger_not_found"`.

Likely cause: OpenOCD (or STM32CubeProgrammer CLI for `type: "stlink"`) is not installed, not on `PATH`, or `debugger.executable` points to a missing file.

Fix: install the debugger tool, restart the shell or MCP client, and either leave `debugger.executable: null` or set it to the actual executable path.

## 4. `debugger_config_not_found`

Symptom: `backend_error_type` is `interface_config_not_found`, `target_config_not_found`, or `config_file_not_found`.

Likely cause: OpenOCD cannot find `interface/stlink.cfg` or `target/stm32f4x.cfg`, or the target config does not match the installed OpenOCD layout.

Fix: verify OpenOCD's script directory and keep the supported first-path values for Nucleo-F446RE unless the board or probe is actually different.

## 5. `adapter_not_found`

Symptom: OpenOCD starts but HardCI reports `error_type: "adapter_not_found"`.

Likely cause: the debug probe is not connected, the USB cable is charge-only, a driver/udev rule is missing, or another process owns the probe.

Fix: reconnect with a data-capable USB cable, close other debugger sessions, check OS drivers or udev rules, then run `hardci doctor` and probe again.

## 6. `target_not_detected`

Symptom: `hardci_probe_target` returns `ok: false` with `error_type: "target_not_detected"`.

Likely cause: target power is missing, SWD is disabled by firmware, jumpers are wrong, the board is held in reset, or the config is for the wrong target family.

Fix: confirm board power, keep `target/stm32f4x.cfg` for Nucleo-F446RE, disconnect other debug tools, power-cycle the board, and probe again before flashing.

## 7. `permission_denied`

Symptom: an MCP tool returns `error_type: "permission_denied"`.

Likely cause: the local `.hardci/config.yaml` policy intentionally disables that action.

Fix: stop and ask the human operator. Do not work around the policy with raw OpenOCD, direct COM-port tools, direct CAN or test-adapter access, or shell commands. The local HardCI config is authoritative.

## 8. Artifact Not Found Or Fails Validation

Symptom: `hardci_flash_firmware` returns `artifact_not_found` or `artifact_validation_failed` with fields such as `allowed_root: false`, `allowed_extension: false`, `elf_header: false`, `hex_parseable: false`, or `bin_size_plausible: false`.

Likely cause: the firmware was not built, the path is wrong, the artifact is outside configured `artifacts.allowed_roots`, the extension is not allowed, or the file is not a valid firmware artifact.

Fix: build first and flash `.elf`, `.hex`, or `.bin` from an allowed root, usually `build/firmware.elf`. Only extend `allowed_extensions` if the project intentionally produces that format.

## 9. `flash_failed`, `verify_failed`, `reset_failed`, Or `timeout`

Symptom: probe works, but flashing, verification, reset, or a debugger action times out.

Likely cause: the image does not match the target memory layout, flash is locked, the target is unstable, reset wiring is wrong, the wrong OpenOCD target config is used, or `debugger.timeout_s` is too low.

Fix: inspect `log_path`, confirm the artifact matches the target, power-cycle the board, then retry probe before retrying flash. Increase `debugger.timeout_s` only when the operation is valid but consistently slow.

## 10. COM Port Does Not Work

Symptom: COM tools cannot start a session, return permission errors, or read no expected serial text.

Likely cause: the port is not configured under `com_ports`, the device name is wrong, the baud rate is wrong, another program owns the port, or serial access permissions are missing.

Fix: run `hardci com-ports`, add only the approved project port to `.hardci/config.yaml`, close other serial monitors, and use MCP COM tools with the configured `port_id`.

Linux permission note: if opening the device fails with a permission error, the user typically needs membership in the `dialout` (Debian/Ubuntu) or `uucp` (Arch) group, or a udev rule for the adapter. This is the one setup step that may genuinely need an administrator once; HardCI itself never needs admin rights.

## 11. CAN Bus Does Not Work

Symptom: CAN tools cannot start a session, return `can_bus_not_configured`, `can_backend_not_available`, `config_invalid`, permission errors, or read no expected frames.

Likely cause: the bus is not configured under `can_buses`, the wrong `bus_id` is used, `allow_can_read`/`allow_can_write` is disabled, `python-can` is not installed (`can_backend_not_available` → install `hardci[can]`), another program owns the adapter, or the `channel` value is for a different backend.

Fix: add only the approved project bus to `.hardci/config.yaml` and use MCP CAN tools with the configured `bus_id`. On Windows with PEAK, use `adapter: "peak"` and `channel: "PCAN_USBBUS1"`. On Linux SocketCAN, use `adapter: "socketcan"` and an interface such as `can0` — `PCAN_USBBUS*` values are Windows PCANBasic channels, not SocketCAN interface names.

## 12. Test Adapter Does Not Work

Symptom: adapter tools return `adapter_not_configured`, `session_not_active`, `channel_not_configured`, `fault_not_configured`, or `adapter_bridge_*` errors.

Likely cause: the adapter is not configured under `adapters`, the session was not started, the channel or fault name is not in the config allowlist, or the bridge executable is missing or crashed.

Fix: configure the adapter with explicit `channels` and `faults` allowlists, start with `hardci_adapter_session_start`, and use only allowlisted names. For `adapter_bridge_process_exited`/`adapter_bridge_timeout`, check the bridge executable path and test it standalone — the bundled simulator `examples/adapters/sim_ntc_adapter.py` is a working reference for the bridge protocol.
