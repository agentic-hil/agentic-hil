# Troubleshooting

This page covers the most common Agentic Hardware-in-the-Loop (Agentic HIL) setup and hardware-loop failures. Start with the supported first path from the README: STM32 Nucleo-F446RE, ST-Link, OpenOCD, and Python 3.10 or newer.

Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

Always inspect structured JSON first. The most useful fields are `ok`, `error_type`, `backend_error_type`, `summary`, `likely_causes`, `report_path`, and `log_path`.

## Windows Quick Notes

- If OpenOCD is installed but not on `PATH`, set `debuggers.<name>.executable` explicitly, for example `C:/Program Files/OpenOCD/bin/openocd.exe`.
- Use forward slashes in YAML paths to avoid accidental escape sequences.
- Run `agentic-hil com-ports` after reconnecting USB serial hardware.
- Configure Windows COM devices such as `COM5` under named `com_ports` ids, then use those ids from MCP COM tools.
- Configure PEAK CAN devices under named `can_buses` ids, for example `adapter: "peak"` and `channel: "PCAN_USBBUS1"` on Windows.

## 1. `agentic-hil` Command Not Found

Symptom: the shell or MCP client cannot start Agentic HIL.

Likely cause: Agentic HIL is not installed, `~/.local/bin` is not on `PATH`, or the MCP client starts with a minimal environment.

Fix — all user-local, never with admin rights. Start with the PyPI/pip package:

```bash
python -m pip install --user --upgrade "agentic-hil>=0.6.0"
agentic-hil --version
agentic-hil setup --help
```

If that fails, use `uv` or `pipx` instead:

```bash
uvx --from "agentic-hil>=0.6.0" agentic-hil --version                           # transient diagnostic only
uvx --from git+https://github.com/agentic-hil/agentic-hil@v0.6.0 agentic-hil --version # transient repository check
uv tool install --upgrade "agentic-hil>=0.6.0"                                  # persistent user-local install
```

`pipx run --spec "agentic-hil>=0.6.0" agentic-hil --version` is also only a transient diagnostic; use `pipx install "agentic-hil>=0.6.0"` for a persistent installation. If `agentic-hil` is installed but not found, add the user-level executable directory to `PATH` with `uv tool update-shell` or `pipx ensurepath` and open a fresh shell. If neither `uv` nor `pipx` exists, install `uv` user-locally first (`curl -LsSf https://astral.sh/uv/install.sh | sh`). Never use `sudo pip` or `pip install --break-system-packages`.

Do not store `uvx`, a workspace virtual environment, or a bare PATH command in `.mcp.json` or a user-level MCP registration. Run `agentic-hil agent-install --agent <agent>` for Claude Code, Codex, or OpenCode; it verifies the persistent executable and stores its absolute user-bin path. It is user-wide and needs no project, so it answers this symptom even where the project configuration cannot be written yet; `agentic-hil setup --agent <agent>` runs it first, then binds the project. For another host, review that same persistent executable and configure its absolute path as described in [MCP host configuration](docs/mcp-hosts.md).

The MCP host must start in the firmware project root so Agentic HIL can discover its external authoritative config. If an operator-controlled registration or parent environment sets `AGENTIC_HIL_CONFIG`, it must be an absolute path; do not commit its machine-specific value in repository-controlled `.mcp.json`.

## 2. `config_file_not_found` / `config_invalid` / `config_unreadable`

Symptom: `agentic-hil doctor` returns one of these `error_type` values.

Likely cause: the automatically discovered config is missing, or `AGENTIC_HIL_CONFIG` is relative, points to a missing file, or selects invalid YAML.

Fix: run `agentic-hil init`, review the deny-by-default file outside the repository, then run `agentic-hil doctor` again. Set `AGENTIC_HIL_CONFIG` only if an explicit absolute-path override is required. Use structured fields such as `field`, `allowed_fields`, `allowed_values`, and `expected_type` to fix schema errors.

`init` is the project half of `setup`. After a `setup` that reported `ok: false` here, read `scopes.user.ok` first: `true` means the agent skill and the user-level MCP registration are installed and stay installed, so only `init` is left. A configuration refusal never undoes them, and `agent-install` need not run again for this user.

If the refusal is `unsafe_configured_path` on the location rather than the file contents — a stock Windows profile rejects `%APPDATA%` and `%LOCALAPPDATA%` — move the configuration as `agentic-hil://reference/platform-paths` describes and point `AGENTIC_HIL_CONFIG` at the new absolute path. Never relax an ACL or take ownership to pass the check.

## 3. `config_invalid` / Workspace Binding Failure

Symptom: `mcp-stdio` exits before serving tools with one of these `error_type` values.

Likely cause: the MCP host started from the wrong project directory, `workspace_root` does not match the project root, or the config authorizes a debugger/process-bridge executable inside the workspace.

Fix: stop and ask the human operator. The operator reviews the external config created by `agentic-hil init`, configures the MCP host's project working directory, and restarts the MCP process. If an absolute-path override is needed, set `AGENTIC_HIL_CONFIG` outside repository configuration. Never replace it with a repository path.

## 4. `debugger_not_found`

Symptom: `agentic-hil doctor` returns `ok: false` with `error_type: "debugger_not_found"`.

Likely cause: OpenOCD (or pyOCD for `type: "pyocd"`, or STM32CubeProgrammer CLI for `type: "stlink"`) is not installed, not on `PATH`, or the configured `debuggers.<name>.executable` could not be pinned.

Fix: install the debugger tool (`pyocd` comes with the `agentic-hil[pyocd]` extra), then have the operator set `debuggers.<name>.executable` in the authoritative config to an existing host-owned executable outside the workspace. For pyOCD targets beyond the built-ins, install the CMSIS pack first (`pyocd pack install <target_type>`).

## 5. `debugger_config_not_found`

Symptom: `backend_error_type` is `interface_config_not_found`, `target_config_not_found`, or `config_file_not_found`.

Likely cause: OpenOCD cannot find `interface/stlink.cfg` or `target/stm32f4x.cfg`, or the target config does not match the installed OpenOCD layout.

Fix: verify OpenOCD's script directory. In the authoritative config, use absolute paths to host-owned interface and target scripts outside the workspace.

## 6. `adapter_not_found`

Symptom: OpenOCD starts but Agentic HIL reports `error_type: "adapter_not_found"`.

Likely cause: the debug probe is not connected, the USB cable is charge-only, a driver/udev rule is missing, or another process owns the probe.

Fix: reconnect with a data-capable USB cable, close other debugger sessions, check OS drivers or udev rules, then run `agentic-hil doctor` and probe again.

## 7. `target_not_detected`

Symptom: `probe_target` returns `ok: false` with `error_type: "target_not_detected"`.

Likely cause: target power is missing, SWD is disabled by firmware, jumpers are wrong, the board is held in reset, or the config is for the wrong target family.

Fix: confirm board power, keep `target/stm32f4x.cfg` for Nucleo-F446RE, disconnect other debug tools, power-cycle the board, and probe again before flashing.

## 8. `permission_denied`

Symptom: an MCP tool returns `error_type: "permission_denied"`.

Likely cause: the authoritative config disables that action or does not name the resource.

Fix: stop and ask the human operator to review the authoritative config. Do not work around the result with raw OpenOCD, direct COM-port tools, direct CAN access, or shell commands.

## 9. Artifact Not Found Or Fails Validation

Symptom: `flash_firmware` returns `artifact_not_found` or `artifact_validation_failed` with fields such as `allowed_root: false`, `allowed_extension: false`, `elf_header: false`, `hex_parseable: false`, or `bin_size_plausible: false`.

Likely cause: the firmware was not built, the path is wrong, the artifact is outside configured `artifacts.allowed_roots`, the extension is not allowed, or the file is not a valid firmware artifact.

Fix: build first and flash `.elf`, `.hex`, or `.bin` from an allowed root, usually `build/firmware.elf`. Only extend `allowed_extensions` if the project intentionally produces that format.

## 10. `flash_failed`, `verify_failed`, `reset_failed`, Or `timeout`

Symptom: probe works, but flashing, verification, reset, or a debugger action times out.

Likely cause: the image does not match the target memory layout, flash is locked, the target is unstable, reset wiring is wrong, the wrong OpenOCD target config is used, or `debuggers.<name>.timeout_s` is too low.

Fix: inspect `log_path`, confirm the artifact matches the target, power-cycle the board, then retry probe before retrying flash. Increase `debuggers.<name>.timeout_s` only when the operation is valid but consistently slow.

## 11. COM Port Does Not Work

Symptom: COM tools cannot start a session, return permission errors, or read no expected serial text.

Likely cause: the port is not configured under `com_ports`, the device name is wrong, the baud rate is wrong, another program owns the port, or serial access permissions are missing.

Fix: run `agentic-hil com-ports`, have the operator add only the approved project port to the authoritative config, close other serial monitors, and use MCP COM tools with the configured `port_id`.

Linux permission note: if opening the device fails with a permission error, the user typically needs membership in the `dialout` (Debian/Ubuntu) or `uucp` (Arch) group, or a udev rule for the adapter. This is the one setup step that may genuinely need an administrator once; Agentic HIL itself never needs admin rights.

## 12. CAN Bus Does Not Work

Symptom: CAN tools cannot start a session, return `can_bus_not_configured`, `can_backend_not_available`, `config_invalid`, permission errors, or read no expected frames.

Likely cause: the bus is not configured under `can_buses`, the wrong `bus_id` is used, sending is refused because `permissions.allow_write` is disabled (reading needs no permission, and on a version 1 file — one with no `version:` key — it still needs `permissions.allow_read`), `python-can` is not installed (`can_backend_not_available` -> install `agentic-hil[can]`), another program owns the adapter, or the `channel` value is for a different backend.

Fix: have the operator add only the approved project bus to the authoritative config and use MCP CAN tools with the configured `bus_id`. On Windows with PEAK, use `adapter: "peak"` and `channel: "PCAN_USBBUS1"`. On Linux SocketCAN, use `adapter: "socketcan"` and an interface such as `can0` — `PCAN_USBBUS*` values are Windows PCANBasic channels, not SocketCAN interface names.

## 13. `device_busy`, `resource_busy` Or Quarantined Hardware

Symptom: a call returns `device_busy` and names a `holder`, or tools return `resource_busy`, `cleanup_required`, or `quarantined` with a `quarantine_id`.

`device_busy` is not a fault. Another run holds that physical device for its whole duration, which is the exclusivity that replaced the read permission. The refusal carries `holder` (pid, host, frontend, and the run label when there is one) and `held_since`; nothing was touched. Wait for that run, or ask its owner to finish. If waiting is right, ask for it explicitly and bounded: `agentic-hil test-reactor --wait-s <seconds>`. Never delete a lock file under `~/.agentic-hil/device-locks` — the hold belongs to a live process, and removing it lets two runs drive one board.

A holder reported with `holder_heartbeat_stale: true` is hung rather than busy; stop that process. A crashed one needs nothing at all: the operating system drops the lock when the process dies, and the next run reports what it reclaimed.

`undeclared_device` is separate: a run reached for a device its test plan does not name. Add the device to the plan and rerun — the plan is what the mutex locks before the first step, so a device it does not name was never locked.

Likely cause: another live process owns the project or resource lease, or a previous owner crashed / left an unknown hardware effect, which quarantines the resource instead of silently releasing it.

Fix: inspect ownership with `agentic-hil lease-status`. If a live owner exists, wait or stop it.

For a quarantine, read `cleanup_reasons` and `auto_recoverable` from `lease-status` before doing anything physical. They name what is actually unresolved and whether an operator is needed at all:

- `auto_recoverable: true` — the running owner clears this incident itself on the next hardware tool call, then runs it. It reaps this owner's leftover debugger processes, verifies the safe state, records the attestation as `recovery: machine_attested`, and proceeds. No operator step. If verification fails the call returns `resource_quarantined` with `auto_recovery_attempted: true` and the quarantine stands; after `recovery.max_attempts` failures for the same incident it stops retrying.
- `auto_recoverable: false` — a broken audit (`*_audit_broken`), an incident adopted from an owner that died, or an unconfirmed physical effect on a bench whose policy does not allow the stronger predicate. This needs a person.

Which reasons are machine-recoverable is the bench's `recovery.auto_recover` policy, reported as `auto_recover_policy`:

| Policy | Verification it may run | Settles |
| --- | --- | --- |
| `off` | none | nothing — operator only |
| `readonly` | reap this owner's debugger processes, re-read the probe (connects without resetting) | toolchain faults: `debug_session_cleanup_unconfirmed`, `debug_target_state_unconfirmed`, `debugger_readonly_result_unconfirmed`, … |
| `reset_halt` (default) | the above, plus a reset-into-halt that establishes a defined state | additionally `debugger_result_unconfirmed` (unconfirmed flash/reset) and `debug_session_start_unconfirmed` |

`reset_halt` drives the board. Set `recovery.auto_recover: "readonly"` if anything on the bench reacts to a target reset. The weakest predicate that can settle the open reason is the one that runs, so a toolchain fault never triggers a reset. Recovery halts the target and never runs it, so control is not handed back to a partially written image. `reset_halt` also degrades to `readonly` when the bound probe lacks `allow_reset`, and a config that never names `recovery.auto_recover` gets the default plus a one-time warning in the report the first time recovery resets the target.

For the operator case, physically confirm the bench is in a safe state, then release it with `agentic-hil recover --confirm-safe-state --quarantine-id <id>` using the current `quarantine_id` (an old incident ID cannot release a newer quarantine). If the authoritative config changed since the incident was recorded, recovery refuses with `config_changed` showing both hashes; after verifying the config delta, rerun with the explicit `--accept-config-change` override.
