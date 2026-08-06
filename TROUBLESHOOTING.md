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
python -m pip install --user --upgrade "agentic-hil>=0.8.0"
agentic-hil --version
agentic-hil setup --help
```

If that fails, use `uv` or `pipx` instead:

```bash
uvx --from "agentic-hil>=0.8.0" agentic-hil --version                           # transient diagnostic only
uvx --from git+https://github.com/agentic-hil/agentic-hil@v0.8.0 agentic-hil --version # transient repository check
uv tool install --upgrade "agentic-hil>=0.8.0"                                  # persistent user-local install
```

`pipx run --spec "agentic-hil>=0.8.0" agentic-hil --version` is also only a transient diagnostic; use `pipx install "agentic-hil>=0.8.0"` for a persistent installation. If `agentic-hil` is installed but not found, add the user-level executable directory to `PATH` with `uv tool update-shell` or `pipx ensurepath` and open a fresh shell. If neither `uv` nor `pipx` exists, install `uv` user-locally first (`curl -LsSf https://astral.sh/uv/install.sh | sh`). Never use `sudo pip` or `pip install --break-system-packages`.

Two Linux failures leave no installation behind and are not a broken machine. `error: externally-managed-environment` is PEP 668: Debian and Ubuntu mark the system Python as owned by the distribution and `pip` refuses it, `--user` included, so the pip block above does not apply on those hosts — `uv` and `pipx` install into environments of their own and need no exception. `invalid peer certificate: UnknownIssuer` from `uv`, or `self-signed certificate in certificate chain`, is a TLS-intercepting proxy: `uv` validates against roots bundled in its binary, while `curl` and `apt` on the same host read the system trust store and keep working, which is what makes the difference recognisable. `uv tool install --system-certs` (older releases: `--native-tls`) points `uv` at that same store. Prefer `UV_SYSTEM_CERTS=1` in the operator's environment over the flag on a single command line: `agentic-hil upgrade` shells out to `uv tool upgrade` and passes no TLS flags of its own, so a proxied host that fixed only the install command loses the upgrade path. If `--system-certs` does not help, the proxy's CA is missing from the system trust store itself; installing it there is the fix, and `--allow-insecure-host` or any other switch that disables verification is not a fallback.

Do not store `uvx`, a workspace virtual environment, or a bare PATH command in `.mcp.json` or a user-level MCP registration. Run `agentic-hil agent-install --agent <agent>` for Claude Code, Codex, or OpenCode; it verifies the persistent executable and stores its absolute user-bin path. It is user-wide and needs no project, so it answers this symptom even where the project configuration cannot be written yet; `agentic-hil setup --agent <agent>` runs it first, then binds the project. For another host, review that same persistent executable and configure its absolute path as described in [MCP host configuration](docs/mcp-hosts.md).

The MCP host must start in the firmware project root so Agentic HIL can discover its external authoritative config. If an operator-controlled registration or parent environment sets `AGENTIC_HIL_CONFIG`, it must be an absolute path; do not commit its machine-specific value in repository-controlled `.mcp.json`.

## 2. `config_file_not_found` / `config_invalid` / `config_unreadable`

Symptom: `agentic-hil doctor` returns one of these `error_type` values.

Likely cause: the automatically discovered config is missing, or `AGENTIC_HIL_CONFIG` is relative, points to a missing file, or selects invalid YAML.

Fix: run `agentic-hil init`, review the file it writes outside the repository — every permission in it is granted — then run `agentic-hil doctor` again. Set `AGENTIC_HIL_CONFIG` only if an explicit absolute-path override is required. Use structured fields such as `field`, `allowed_fields`, `allowed_values`, and `expected_type` to fix schema errors.

`init` is the project half of `setup`. After a `setup` that reported `ok: false` here, read `scopes.user.ok` first: `true` means the agent skill and the user-level MCP registration are installed and stay installed, so only `init` is left. A configuration refusal never undoes them, and `agent-install` need not run again for this user.

If the refusal is `unsafe_configured_path` on the location rather than the file contents, a component of the path is not what the path claims: a symlink where a real directory is needed, or a file where a directory belongs. `path` names the path and `component` the part of the chain that stopped the walk. Replace it, or move the configuration as `agentic-hil://reference/platform-paths` describes and point `AGENTIC_HIL_CONFIG` at the new absolute path.

**A path is no longer refused for who else could write it.** Until 0.8.0 a Windows ACL walk and a POSIX ownership/mode walk went over every ancestor and refused a path some other principal could write, delete or re-permission. Both were removed (hardci-hq#95): the guarantee they were reaching for — that no *other account on the same machine* can rewrite the policy — was never a requirement of this project, and no ACL could have held it anyway, because the operator owns these files and every ordinary process of that user can rewrite them. `windows_path_trust` went with the check; a configuration that still carries the key is refused by name, and the refusal says what replaced it.

What replaced it is detection rather than prevention. `config_status`, on every tool result, compares the SHA-256 of the configuration this server loaded against the bytes on disk, so an unexpected change is announced instead of prevented; the audit trail records the digest each hardware action ran under; and the file-level deny rules `agentic-hil setup` writes into the agent's own configuration still keep the agent's file tools off the authoritative file. If a refusal on `%APPDATA%` or `%LOCALAPPDATA%` is what sent you here from an older release, upgrade: those folders are no longer inspected at all.

`AGENTIC_HIL_CONFIG` and a chosen `state_root` are the supported way to bind a project to another location, not a workaround. Nothing about a project is second class for using them: a configuration selected by `AGENTIC_HIL_CONFIG` is read exactly like a discovered one — same schema, same validation, same permissions — and `state_root` may be any absolute directory that does not overlap `workspace_root`.

```text
AGENTIC_HIL_CONFIG=C:\Users\<user>\.agentic-hil\projects\<workspace-name>\config.yaml
state_root:        C:\Users\<user>\.agentic-hil\state
```

Set `AGENTIC_HIL_CONFIG` in the host's user-level, managed, or parent-process environment — never in a repository-controlled file, because an agent that can edit the file selecting the configuration can select a configuration it wrote.

## 3. `config_invalid` / Workspace Binding Failure

Symptom: `mcp-stdio` exits before serving tools with one of these `error_type` values.

Likely cause: the MCP host started from the wrong project directory, `workspace_root` does not match the project root, or the config authorizes a debugger/process-bridge executable inside the workspace.

Fix: stop and ask the human operator. The operator reviews the external config created by `agentic-hil init`, configures the MCP host's project working directory, and restarts the MCP process. If an absolute-path override is needed, set `AGENTIC_HIL_CONFIG` outside repository configuration. Never replace it with a repository path.

## 4. `debugger_not_found`

Symptom: `agentic-hil doctor` returns `ok: false` with `error_type: "debugger_not_found"`.

Likely cause: OpenOCD (or pyOCD for `type: "pyocd"`, or STM32CubeProgrammer CLI for `type: "stlink"`) is not installed, not on `PATH`, or the configured `debuggers.<name>.executable` could not be pinned.

Fix: install the debugger tool (`pyocd` comes with the `agentic-hil[pyocd]` extra), then have the operator set `debuggers.<name>.executable` in the authoritative config to an existing host-owned executable outside the workspace. For pyOCD targets beyond the built-ins the CMSIS pack is a second, separate step — see 5a below.

## 5. `debugger_config_not_found`

Symptom: `backend_error_type` is `interface_config_not_found`, `target_config_not_found`, or `config_file_not_found`.

Likely cause: OpenOCD cannot find `interface/stlink.cfg` or `target/stm32f4x.cfg`, or the target config does not match the installed OpenOCD layout.

Fix: verify OpenOCD's script directory. In the authoritative config, use absolute paths to host-owned interface and target scripts outside the workspace.

## 5a. `target_type_invalid` (pyOCD) — the CMSIS pack is missing

Symptom: a pyOCD call returns `error_type: "target_type_invalid"`, or `agentic-hil doctor` reports `debuggers.<name>.target_support.status: "unsupported"`. pyOCD's own message is `Target type <name> not recognized`.

Likely cause: the configured `target_type` comes from a CMSIS device-family pack that is not installed on this host. Most vendor parts — including the whole STM32F4 family — are not built into pyOCD; `pyocd list --targets` shows `pack` in its Source column for them.

Fix: run the commands the result carries in `install_commands`, as a deliberate host setup step:

```bash
pyocd pack find stm32f446         # GLOB; shorten it to widen the search
pyocd pack install stm32f446retx  # downloads the pack from the vendor index
pyocd pack show                   # what is installed now
```

Agentic HIL never runs these. `pyocd pack install` fetches over the network, and a background download nobody asked for is precisely what this documentation gap once caused. Do not reconstruct a pack from hand-downloaded `.pdsc` files.

Installed packs live in `cmsis-pack-manager`'s data directory (`%LOCALAPPDATA%\cmsis-pack-manager\cmsis-pack-manager` on Windows, `~/.local/share/cmsis-pack-manager` on Linux). `CMSIS_PACK_ROOT` is a different thing — pyOCD reads it only for CMSIS-Toolbox `cbuild-run` projects — and does not relocate that cache.

`doctor` distinguishes three answers, and only one is a failure: `unsupported` means the backend enumerated its target types and this one is absent; `undetermined` means this host could not answer at all — no toolchain, or the enumeration failed — and stays green with `undetermined_reason` saying why. Full detail: `agentic-hil://reference/target-support`.

## 5b. The board was plugged in after `setup` ran

Symptom: the configuration holds `probe_id: null`, `executable: null`, `target.controller: "unknown-controller"` and no `com_ports` entry, and `doctor` skips the debugger check.

Likely cause: `setup` discovers hardware once. It ran while nothing was attached, so `init` wrote the skeleton with placeholders — the ordinary case, because installing the tool and connecting the board are two separate moments.

Fix: attach the board and carry it in. Do not retype the values, and do not reach for `init --force`, which discards whatever a person has set since.

```bash
agentic-hil adopt-hardware --dry-run   # what it would fill in
agentic-hil adopt-hardware
agentic-hil doctor
```

It fills in the probe serial, the backend's executable, the detected controller and the COM device the probe exposes — only where the file has nothing there. A key that already holds a value comes back under `kept` with what the hardware says beside it, never overwritten, and no permission is touched. With more than one probe attached it refuses and lists the serials; name one with `--probe-id`. With more than one debugger or COM port configured, `--debugger` and `--com-port` say which entry this is about.

Three things it will not do. `debuggers.<name>.type` decides which program drives the board, so a discovered ST-Link toolchain is not written into an entry whose type is `openocd`; the result says so and names the entry — that one line is the only edit the workflow above can need, and it is the one field that is not a board's identity. A `com_ports` entry it creates arrives with every permission `false` — writing to a port stays yours to grant. And an entry that already names a probe is never repointed at a different attached one: that answers `hardware_mismatch` and writes nothing, because the controller, the COM device and the toolchain path all describe the attached board and carrying them beside another board's serial would give you a configuration pointing at two boards at once.

It reads the probe the way every other command here reaches hardware, so `device_busy` means a board an MCP server, a `test-reactor` run or another terminal is holding: close that first rather than retrying. `resource_quarantined` means this bench has unresolved cleanup — `agentic-hil recover`, once you know the bench is in a safe state.

An agent does the same over MCP with `project_config_adopt_hardware`, which needs `permissions.allow_config_description_write`. Without it the call is refused and still returns the exact keys and values, so what you get is one command to run rather than a serial to transcribe.

A running MCP server does not see the change yet: it parsed the file at startup. It no longer has to be restarted for this. Ask the agent to call `project_config_reload_description` — no arguments, and it re-reads `target`, `debuggers`, `com_ports` and `can_buses` from the file. It re-reads **no permission at all**, so a device that server has never seen arrives with none: it can be probed and read, and flashing, reset, mass erase and COM/CAN writes on it wait for a restart. `agentic-hil config-reload` shows what that call would take from this file and refuses the same way if the file no longer loads. A permission you changed, and anything outside those four sections, still needs the server restarted.

## 6. `adapter_not_found`

Symptom: OpenOCD starts but Agentic HIL reports `error_type: "adapter_not_found"`.

Likely cause: the debug probe is not connected, the USB cable is charge-only, a driver/udev rule is missing, or another process owns the probe.

Fix: reconnect with a data-capable USB cable, close other debugger sessions, check OS drivers or udev rules, then run `agentic-hil doctor` and probe again.

## 7. `target_not_detected`

Symptom: `probe_target` returns `ok: false` with `error_type: "target_not_detected"`.

Likely cause: target power is missing, SWD is disabled by firmware, jumpers are wrong, the board is held in reset, or the config is for the wrong target family.

Fix: confirm board power, keep `target/stm32f4x.cfg` for Nucleo-F446RE, disconnect other debug tools, power-cycle the board, and probe again before flashing.

Not the same as `target_state_unconfirmed`, which is what a toolchain that exited without confirming what it did produces — `operation_result` in that result names which confirmation lines arrived and which did not. This one is the backend's own report that it reached the adapter and nothing answered, so it refuses retry-safe and the bench stays in service; that one places the abort point nowhere, quarantines, and is settled by the physical check in its `quarantine_guidance` rather than by another probe.

## 8. `permission_denied`

Symptom: an MCP tool returns `error_type: "permission_denied"`.

Likely cause: the authoritative config disables that action or does not name the resource.

Fix: stop and ask the human operator to review the authoritative config. Do not work around the result with raw OpenOCD, direct COM-port tools, direct CAN access, or shell commands.

## 9. Artifact Not Found Or Fails Validation

Symptom: `flash_firmware` returns `artifact_not_found` or `artifact_validation_failed` with fields such as `allowed_root: false`, `allowed_extension: false`, `elf_header: false`, `hex_parseable: false`, or `bin_size_plausible: false`.

Likely cause: the firmware was not built, the path is wrong, the artifact is outside configured `artifacts.allowed_roots`, the extension is not allowed, or the file is not a valid firmware artifact.

Fix: build first and flash `.elf`, `.hex`, or `.bin` from an allowed root. On `allowed_root: false`, read what the configuration lists: a generated one says `["."]` and permits the whole workspace recursively, while a configuration written before 0.8.0, or one that omits the key, permits `build` alone — which no CLion, PlatformIO, or Zephyr tree builds into. Setting `allowed_roots: ["."]` is the one edit that covers every build layout; it is not a permission and it does not reach outside `workspace_root`. Only extend `allowed_extensions` if the project intentionally produces that format.

A refusal with `allowed_root: true` and `within_workspace: false` is a different fault: the build directory is a symlink or junction pointing outside `workspace_root`, so the path only looks contained. Build inside the workspace, or move `workspace_root` to where the build really lands. No configuration key relaxes containment, so widening `allowed_roots` will not help here.

## 10. `flash_failed`, `verify_failed`, `reset_failed`, Or `timeout`

Symptom: probe works, but flashing, verification, reset, or a debugger action times out.

Likely cause: the image does not match the target memory layout, flash is locked, the target is unstable, reset wiring is wrong, the wrong OpenOCD target config is used, or `debuggers.<name>.timeout_s` is too low.

Fix: inspect `log_path`, confirm the artifact matches the target, power-cycle the board, then retry probe before retrying flash. Increase `debuggers.<name>.timeout_s` only when the operation is valid but consistently slow.

Not this: `error_type: "debugger_command_rejected"`, which carries `target_contacted: false` and the `rejected_commands` OpenOCD would not run. OpenOCD stopped inside its own interpreter, before it opened the probe, so the board was never driven and there is nothing on it to inspect, power-cycle or recover. The named commands are either unknown to the installed OpenOCD or belong to its run stage and were reached before `init`; check the version `debugger_info` reports, and the scripts named by `interface_cfg` and `target_cfg`.

## 11. COM Port Does Not Work

Symptom: COM tools cannot start a session, return permission errors, or read no expected serial text.

Likely cause: the port is not configured under `com_ports`, the device name is wrong, the baud rate is wrong, another program owns the port, or serial access permissions are missing.

Fix: run `agentic-hil com-ports`, have the operator add only the approved project port to the authoritative config, close other serial monitors, and use MCP COM tools with the configured `port_id`.

Linux permission note: if opening the device fails with a permission error, the user typically needs membership in the `dialout` (Debian/Ubuntu) or `uucp` (Arch) group, or a udev rule for the adapter. This is the one setup step that may genuinely need an administrator once; Agentic HIL itself never needs admin rights.

## 11a. The COM Port Moved To Another Board

Symptom: `com_session_start` refuses with `error_type: "com_port_identity_mismatch"` — usually right after a second adapter was attached, after a replug, or after a reboot with both boards in.

What it means: the entry is fine and the *name* is not. `/dev/ttyACM0` and `COM7` are an enumeration order — which device the host saw first — so attaching a second ST-Link can move a board from `ttyACM0` to `ttyACM1` and give the vacated name to the other one. The refusal says `expected_serial_number` (what the configuration names, and `expected_from` says where), and `found_serial_number` (the adapter actually behind that name now). Nothing was opened and nothing was written to either board.

The refusal is the good outcome. Without it an unchanged configuration keeps working against a board nobody meant, and a flash, a reset or a `com_write` goes there with nothing in the result saying so.

Fix: read `expected_device`. When it is present, the board this entry names is still attached under that name and the entry is simply out of date — run `agentic-hil adopt-hardware` (add `--com-port <id>` on a bench with several ports) and it rewrites the entry from the attached hardware. When it is absent, the named board is not attached at all: plug it in, or work on the board that is there by naming its own entry.

Do not point `serial_number` at the serial that was found, and do not delete the key. That turns the one check that noticed into agreement with whatever happens to be plugged in.

Preventing it: name ports so the name cannot move.

- On Linux, set `device` to the udev symlink — `/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_<serial>-if02`. It is built from the vendor, the product and the device's own serial, so it follows the board. `agentic-hil com-ports` lists it as `stable_device` next to each port, and `adopt-hardware` writes it.
- On both platforms, set `serial_number` to the adapter's USB serial — on a Nucleo the same serial `probe_id` carries. This is what makes the machine-wide lock follow the board rather than the name, and it is what the check at open time compares. Windows has no openable stable device name, so there `device` stays `COM5` and this key carries the identity by itself.
- `agentic-hil doctor` names every port that has neither, under `warnings`, and every `com_ports` entry reports its `identity_source`. Such an entry keeps working exactly as before — it is simply identified by something the host can reassign.

An entry that names no hardware is not checked at all, and neither is one whose port the host does not enumerate (a pseudo-terminal, a URL handler) or one whose adapter reports no serial. Those say so in the result's `identity` block rather than guessing.

## 12. CAN Bus Does Not Work

Symptom: CAN tools cannot start a session, return `can_bus_not_configured`, `can_backend_not_available`, `config_invalid`, permission errors, or read no expected frames.

Likely cause: the bus is not configured under `can_buses`, the wrong `bus_id` is used, sending is refused because `permissions.allow_write` is disabled (reading needs no permission, and on a version 1 file — one with no `version:` key — it still needs `permissions.allow_read`), `python-can` is not installed (`can_backend_not_available` -> install `agentic-hil[can]`), another program owns the adapter, or the `channel` value is for a different backend.

Fix: have the operator add only the approved project bus to the authoritative config and use MCP CAN tools with the configured `bus_id`. On Windows with PEAK, use `adapter: "peak"` and `channel: "PCAN_USBBUS1"`. On Linux SocketCAN, use `adapter: "socketcan"` and an interface such as `can0` — `PCAN_USBBUS*` values are Windows PCANBasic channels, not SocketCAN interface names.

### `can_listen_only_unsupported` / `can_listen_only_unconfirmed`

Symptom: a bus with `listen_only: true` refuses `can_session_start` with one of these two, instead of starting.

This is the flag working. `listen_only: true` is the claim that observing this bus sends nothing — a controller outside listen-only sends dominant ACK bits, and on a bus with one other participant that ACK decides whether the sender considers its frame delivered. So the mode is enforced per adapter rather than assumed, and where it cannot be backed the session is refused rather than silently downgraded to listening anyway.

| adapter | how the mode is obtained | what backs the claim |
|---|---|---|
| `peak` | set through PCAN's `BusState.PASSIVE`, re-asserted once the channel is initialized | `PCAN_LISTEN_ONLY` is read back from the driver |
| `socketcan` | not obtainable from this process: the mode belongs to the kernel's CAN device, and only `ip link` sets it | the control mode is read before the socket is created |
| `process` | sent to the bridge in its `open` request | the bridge's `open` result must answer `listen_only: true` |

`can_listen_only_unsupported` is settled before the bus is touched, so it carries `retry_safe: true` and quarantines nothing. On SocketCAN it means the interface is not in listen-only, or its control mode could not be read; `link_state` on the result says which. Put a real interface into the mode outside Agentic HIL:

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 listen-only on
sudo ip link set can0 up
ip -details link show dev can0     # prints <LISTEN-ONLY> when it took
```

A `vcan` interface has no CAN controller and therefore no listen-only mode — and no physical bus to disturb. Set `listen_only: false` on a virtual bus. If `link_state` says the control mode could not be read, install iproute2: `ip` is read from `/sbin`, `/usr/sbin`, `/bin` or `/usr/bin` and deliberately not from `PATH`, because a proof read from a PATH-resolved binary is not a proof.

`can_listen_only_unconfirmed` is settled after the adapter was opened: it was asked for the mode, it did not confirm, and it was closed again. The exposure is the open rather than the session, and the result says so instead of implying otherwise. `driver_state` names what came back. On PEAK, check that the channel supports listen-only at all and that PCANBasic and python-can are current. On a `process` bridge, make the bridge answer `"listen_only": true` from `open` once it has actually put its controller into the mode — the bridge protocol version does not change, and a bridge that is never asked for the mode is unaffected.

If the bus may be ACKed — a bench nobody else is driving, a rig where this is the only participant — set `listen_only: false` on that entry. That is an honest configuration and the refusal goes away, because nothing is being claimed any more. Do not instead drop the flag and keep calling the reading passive.

None of this proves what the silicon does; it reaches as far as the driver's or the kernel's own report of its mode. The bench proof stays with the operator: a single sender with no other participant reports its frame unacknowledged.

## 13. `device_busy`, `resource_busy` Or Quarantined Hardware

Symptom: a call returns `device_busy` and names a `holder`, or tools return `resource_busy`, `cleanup_required`, or `quarantined` with a `quarantine_id`.

`device_busy` is not a fault. Another run holds that physical device for its whole duration, which is the exclusivity that replaced the read permission. The refusal carries `holder` (pid, host, frontend, and the run label when there is one) and `held_since`; nothing was touched. Wait for that run, or ask its owner to finish. If waiting is right, ask for it explicitly and bounded: `agentic-hil test-reactor --wait-s <seconds>`. Never delete a lock file under `~/.agentic-hil/device-locks` — the hold belongs to a live process, and removing it lets two runs drive one board.

A holder reported with `holder_heartbeat_stale: true` is hung rather than busy; stop that process. A crashed one needs nothing at all: the operating system drops the lock when the process dies, and the next run reports what it reclaimed.

`undeclared_device` is separate: a run reached for a device its test plan does not name. Add the device to the plan and rerun — the plan is what the mutex locks before the first step, so a device it does not name was never locked.

Likely cause: another live process owns the project or resource lease, or a previous owner crashed / left an unknown hardware effect, which quarantines the resource instead of silently releasing it.

Fix: inspect ownership with `agentic-hil lease-status`. If a live owner exists, wait or stop it.

First check whether the failure quarantined at all. A failure that proves it never reached the hardware does not: a missing toolchain executable, an OpenOCD script or adapter that failed before `init` completed, a pyOCD probe or `target_type` refused before any connect, an absent ST-Link, a COM port that could not be opened (handle verifiably closed), a SocketCAN adapter that never initialized, and a read (`probe_target` / `debugger_probes_list`) whose backend reports that no target answered all refuse with a named `error_type`, `target_contacted: false` and `retry_safe: true`, write no quarantine record, and need no recovery — fix the named cause and retry. If such a result also said `quarantined: true`, that is a bug worth reporting; releases before 0.8.0 quarantined several of these.

What decides it is the backend's claim, not that the call was a read. A read that names no abort point — killed at its deadline (`timeout`), or a toolchain that exited without a complete confirmation (`target_state_unconfirmed`) — the result names which markers were expected and which arrived, and a partial set still names no safe abort point — quarantines as `debugger_readonly_target_state_unconfirmed`: an SWD attach halts the core, a killed process never ran the `shutdown` in its own command string, and a re-read that finds the probe again does not resume a halted core. `target_state_unconfirmed` is its own `error_type` for that reason: `target_not_detected` beside it is the backend's positive report that it reached the adapter and nothing answered, which is why that one refuses and this one does not. That reason is settled by a verified reset-into-halt or by an operator, never by the read-only predicate. A PCAN `CanInitializationError` likewise quarantines, because python-can raises it from `SetValue` calls that run after `PCANBasic.Initialize` has already put the channel on the bus; only SocketCAN's, where the controller is brought up out of band, proves the adapter never joined.

For a quarantine, read `cleanup_reasons`, `quarantine_guidance`, and `auto_recoverable` from `lease-status` before doing anything physical. They name what is actually unresolved and whether an operator is needed at all:

- `auto_recoverable: true` — the running owner clears this incident itself on the next hardware tool call, then runs it. It reaps this owner's leftover debugger processes, verifies the safe state, records the attestation as `recovery: machine_attested`, and proceeds. No operator step. If verification fails the call returns `resource_quarantined` with `auto_recovery_attempted: true` and the quarantine stands; after `recovery.max_attempts` failures for the same incident it stops retrying.
- `auto_recoverable: false` — a broken audit (`*_audit_broken`), an incident adopted from an owner that died, or an unconfirmed physical effect on a bench whose policy does not allow the stronger predicate. This needs a person.

Which reasons are machine-recoverable is the bench's `recovery.auto_recover` policy, reported as `auto_recover_policy`:

| Policy | Verification it may run | Settles |
| --- | --- | --- |
| `off` | none | nothing — operator only |
| `readonly` | reap this owner's debugger processes, re-read the probe (connects without resetting) | toolchain faults: `debug_session_cleanup_unconfirmed`, `debug_target_state_unconfirmed`, `debugger_readonly_result_unconfirmed`, … |
| `reset_halt` (default) | the above, plus a reset-into-halt that establishes a defined state | additionally `debugger_result_unconfirmed` (unconfirmed flash/reset), `debugger_readonly_target_state_unconfirmed` (a read that named no abort point) and `debug_session_start_unconfirmed` |

`reset_halt` drives the board. Set `recovery.auto_recover: "readonly"` if anything on the bench reacts to a target reset. The weakest predicate that can settle the open reason is the one that runs, so a toolchain fault never triggers a reset. Recovery halts the target and never runs it, so control is not handed back to a partially written image. `reset_halt` also degrades to `readonly` when the bound probe lacks `allow_reset`, and a config that never names `recovery.auto_recover` gets the default plus a one-time warning in the report the first time recovery resets the target.

For the operator case, `lease-status` carries a `quarantine_guidance` entry per reason: `attempted` (what was being done when confirmation was lost), `confirmed` (what still holds), `unknown` (the gap that makes a machine answer impossible), and `physical_check` (what to verify on the physical board). Perform the named check, confirm the bench matches, then release it with `agentic-hil recover --confirm-safe-state --quarantine-id <id>` using the current `quarantine_id` (an old incident ID cannot release a newer quarantine) — the signature attests exactly that check. An incident recorded by a different Agentic HIL version gets an explicit fallback entry pointing at its own records. If the authoritative config changed since the incident was recorded, recovery refuses with `config_changed` showing both hashes; after verifying the config delta, rerun with the explicit `--accept-config-change` override.

## 14. Upgrading, And Adding `[pyocd]` Or `[can]` Later

Use `agentic-hil upgrade` to move an existing installation forward. It upgrades through the manager that owns the interpreter running it — `uv tool`, `pipx`, `uv pip`, or `pip` — instead of whichever `agentic-hil` `PATH` resolves first, so a second copy is never upgraded by mistake. Restart the agent hosts afterwards; they load the MCP server at startup and keep running the old one until they do.

**Read the version, not the exit code.** The result distinguishes three outcomes, because the package manager does not: `uv tool upgrade` exits 0 for "upgraded" and for "nothing to do" alike, and reading that code alone once made every no-op report success and send the operator to a restart that reloaded the same release (hardci-hq#99).

| Result | What happened | What to do |
|---|---|---|
| `ok: true`, `restart_required: true` | `previous_version` and `version` differ; the summary names both | Restart the agent hosts |
| `ok: true`, `already_current: true` | Nothing newer to install; both version fields are the same number | Nothing. Do not restart — the same release would be reloaded |
| `ok: false`, `error_type: upgrade_blocked_by_pin` | The manager records an exact version pin, so the upgrade could not move it | Run the line in `reinstall_command`, with the host stopped |

Only the pinned case is a refusal, and only it exits non-zero: a machine that is already at the newest release has nothing wrong with it, so a provisioning script may run `agentic-hil upgrade` unconditionally. Read `restart_required` rather than assuming one: it is true on exactly the outcome that replaced something.

An exact pin comes from the requirement the installation was created with: `uv tool install "agentic-hil==X.Y.Z"` records `==X.Y.Z`, and every later `uv tool upgrade` honours it and reports `hint: agentic-hil is pinned to ... (installed with an exact version pin)` on stderr. The Claude Code plugin's skill installs that way on purpose — it ships guidance for one release and says so — so a bench set up from the plugin is the case this refusal names. `reinstall_command` is the fix, and it carries the extras `installed_extras` found on the installation; `uv`'s own hint suggests `agentic-hil@latest`, which re-resolves the bare distribution and uninstalls whatever `[can]` or `[pyocd]` brought in. Stop the agent host before running it: it reinstalls the environment, and none of `uv tool install`'s forms have the `installation_in_use` check. Agentic HIL never runs that reinstall for you — which version a machine runs is the operator's decision, and a pin can be deliberate.

**Stop the agent host before upgrading.** The host starts the Agentic HIL MCP server itself, so the server runs for as long as the host does — a working setup is exactly the state an upgrade fails in. On Windows a file that is mapped as a running image cannot be deleted, and `uv` removes a tool environment before it rebuilds it. When that removal fails, the rebuild never happens, and neither the old installation nor the new one is left:

```text
error: failed to remove directory ...\uv\tools\agentic-hil\Scripts: Zugriff verweigert (os error 5)
Failed find package agentic-hil in tool environment
ModuleNotFoundError: No module named 'agentic_hil'
```

`agentic-hil upgrade` refuses with `installation_in_use` before anything is removed, naming each holding process by pid and image path, and leaves the installation working. Close the host and run it again. Running `uv tool install --upgrade` or `uv tool install --force` by hand is what the refusal is protecting against — those have no such check.

If an upgrade already destroyed the installation, nothing is recoverable in place; reinstall it, naming the extras, and take the environment name from the error message:

```bash
uv tool install --force "agentic-hil[pyocd]"     # or pipx install --force "agentic-hil[pyocd]"
```

Adding an extra is a different operation from upgrading, and there are two ways to do it:

- **Permanently, with the host stopped.** Close the agent host, then `uv tool install --upgrade "agentic-hil[pyocd]"` (or `pipx install --force "agentic-hil[pyocd]"`), then start the host. Name every extra you want on that command line: `uv` records the requirement literally, so `uv tool install --upgrade agentic-hil` on an installation made with `agentic-hil[can]` re-resolves without the extra and uninstalls `python-can`. `agentic-hil upgrade` does not have this problem — `uv tool upgrade` and `pipx upgrade` both reinstall from the requirement the manager already recorded, extras included.
- **Without stopping the host, `uv` only.** This adds the extra's dependencies to the existing environment and removes nothing, so no locked file is in the way:

  ```bash
  uv pip install --python "$(uv tool dir)/agentic-hil/Scripts/python.exe" "agentic-hil[pyocd]"   # bin/python3 on Linux and macOS
  ```

  The MCP server picks the new packages up when the host restarts. This does not update `uv`'s record of the tool, so name the extra again the next time the tool itself is reinstalled or upgraded.
