# The Authoritative Configuration

Every project has exactly one authoritative configuration, and it lives outside
the repository. This page is the reference for that file: what it declares, who
may change which part of it, and what happens when it moves while a server is
running.

`agentic-hil setup` already created this file; `agentic-hil init` creates it on its own, without the user-wide skill and MCP registration that `agentic-hil agent-install` owns. Either way it lands outside the repository with `workspace_root` bound to the current absolute project path. It defines the target, artifact roots, and the project's named devices: debug probes, serial ports, and CAN buses.

## Where it is found

`mcp-stdio` discovers the authoritative file from its project working directory, and discovery has two roots it tries in that order. The platform default first: `%APPDATA%/agentic-hil/projects/<name>-<digest>/config.yaml` on Windows or `${XDG_CONFIG_HOME:-~/.config}/agentic-hil/projects/<name>-<digest>/config.yaml` on POSIX. Then `%USERPROFILE%\.agentic-hil\projects\<name>-<digest>\config.yaml` or `~/.agentic-hil/projects/<name>-<digest>/config.yaml`, which is taken when the first root cannot be written. Writability is what decides that, not whether the directory happens to exist, which is how a redirected profile directory or a packaged host lands under the second root. `<name>-<digest>` is the same directory under either root: the workspace directory's own name, a hyphen, and a short digest of its absolute path, so two checkouts of one repository get one configuration each. One rule outranks the order: an existing configuration wins over it. A file already written under the fallback is this workspace's authoritative one, every later load finds it there, and `agentic-hil init --force` rewrites it where it stands instead of generating a second beside the default. Set `AGENTIC_HIL_CONFIG` when an operator-controlled absolute-path override is needed; never commit a machine-specific override in repository-controlled MCP configuration.

Started in a directory that has no configuration, as a user-wide VS Code registration is started in the home directory, `mcp-stdio` asks a host that declared MCP `roots` which folder it has open and serves that folder as if it had been started there. One local folder is that folder, configured or not, so a configuration `project_config_create` generates is generated for it; several are narrowed to the one whose configuration loads, and any other answer leaves the server where it started. A tool call that arrives before the answer waits for it, and the answer moves the server only until a configuration is bound. A working directory with a configuration is served without asking.

A configured path is checked for what it *is*: every component is opened without following a link and held open for the duration, so a symlinked component or a file where a directory belongs is refused (`unsafe_configured_path`) rather than traversed. Who else on the machine holds rights on it is not asked. A Windows ACL walk and a POSIX mode walk over every ancestor used to ask exactly that, and both were removed in 0.8.0: they could only defend against a different account on the same host (never a requirement here), and they could not defend against your own processes, which own these files and can rewrite them whatever an ACL says. What answers the question that does matter (is this still the configuration this server enforces) is the digest in `config_status`, under [Changing it while the server runs](#changing-it-while-the-server-runs).

`%APPDATA%` and `%LOCALAPPDATA%` are the discovered defaults, and `%USERPROFILE%\.agentic-hil` is the second root discovery falls to on its own: `agentic-hil init` writes both the configuration and the generated `state_root` there when the default cannot be written, and nothing has to be set for that to happen. A redirected profile directory is already covered by that. `AGENTIC_HIL_CONFIG` is the separate fact, an operator's manual binding, and what it is for is a location neither root reaches (a roaming share, a volume chosen to keep this bench's files off the system disk); set the `state_root` inside that file to match. It is a supported binding and not a workaround: a configuration reached that way is read exactly like a discovered one, with the same schema, validation and permissions. Set it in the host's user-level or managed environment, never in a repository-controlled file. Where each file lives: `agentic-hil://reference/platform-paths`. Per-host registration syntax is in [MCP host configuration](mcp-hosts.md).

## What it declares

Reading a device needs no permission. What protects a read is exclusivity: every device a test plan names is locked machine-wide for the duration of the run, a device the plan does not name is refused, and a second session is turned away with the holder named. That covers opening a debug session and inspecting it while the target sits halted, the same way it covers a probe attach: both halt the core the way any read on this bench can, and exclusivity rather than a grant answers for it. Everything that writes or changes state, or that lifts a halted target back into running (flash, reset, serial and CAN sends, resuming a debug session, mass erase, raw debugger commands), is declared per device, and a generated configuration grants all of it except `allow_raw_debugger_commands` and `allow_mass_erase`. Those two are false because of what they can reach: both act on flash outside the path this server validates (a raw debugger command writes whatever it is given, a mass erase clears whatever a flash has just written), so once either is allowed, a flash report's claim about what is on the device is no longer one this server can stand behind. That is the mutual exclusion between validated flashing and unrestricted debugger access: while either is true, `flash_firmware` on that probe is refused, and so is a debug session. Neither has a tool behind it here to trade for that, so setting one true withholds flashing rather than granting anything. The example below is what a generation writes:

```yaml
version: 3                   # reading needs no permission, and every com_ports
                             # entry says which hardware it is; see Migration below
workspace_root: "/absolute/path/to/firmware-project"
state_root: "/absolute/operator-controlled/user-state/agentic-hil"

target:
  name: "sensor-board"
  controller: "stm32f4"

# Every device is named, and every device carries its own write permissions.
# Test plan steps address a device by that name. The MCP tool surface drives the
# single configured probe, so a project with several probes runs multi-board
# work through `agentic-hil test-reactor`.
debuggers:
  dut:
    type: "openocd"          # or "pyocd" (most Cortex-M targets), "stlink" (STM32CubeProgrammer CLI),
                             # or "esptool" (an ESP32 through its USB-UART bridge; see below)
    probe_id: "0668FF383036" # required once several probes are configured: it is
                             # the only field that selects a physical probe
    # OpenOCD search names: OpenOCD resolves them against its own script path,
    # so they name no file on this host. Give an absolute path instead to pin
    # the exact scripts, e.g. "/usr/share/openocd/scripts/target/stm32f4x.cfg"
    interface_cfg: "interface/stlink.cfg"
    target_cfg: "target/stm32f4x.cfg"
    timeout_s: 60
    permissions:
      allow_flash: true
      allow_reset: true
      allow_debug_execution: true  # resume a halted target in a debug session
      allow_raw_debugger_commands: false
      allow_mass_erase: false
  probe_b:                   # a second, independently controlled board
    type: "openocd"
    probe_id: "0669FF505153" # pin the physical probe so boards cannot swap silently
    interface_cfg: "/absolute/path/to/openocd/scripts/interface/stlink.cfg"
    target_cfg: "/absolute/path/to/openocd/scripts/target/stm32f4x.cfg"  # the path spelling
    target:                  # optional per-probe override of the project target
      name: "sensor-node"
    permissions:
      allow_flash: false     # this board is read-only for now

debug:
  # Which GDB reads this bench's images. Null autodetects one on the server's
  # PATH (arm-none-eabi-gdb, gdb-multiarch, gdb, in that order); name it when
  # the GDB that speaks this target is not on that PATH. `adopt-hardware`
  # fills it in, and so does `project_config_set` behind the description grant.
  gdb_executable: null
  # Every symbol resolves. To narrow that, set allow_all_symbols false and list
  # the names in allowed_symbols, for example ["main", "sensor_state"].
  allowed_symbols: []
  allow_all_symbols: true

artifacts:
  allowed_roots: ["."]       # the whole workspace, recursively; see Artifact Roots below
  allowed_extensions: [".elf", ".hex", ".bin"]

com_ports:
  dut_uart:
    # How the port is opened. On Linux prefer the /dev/serial/by-id/... symlink:
    # udev builds it from the vendor, the product and the serial, so it follows
    # the board. /dev/ttyACM0 and COM5 are an enumeration order and can come to
    # mean a different board once a second adapter is attached.
    device: "/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_0669FF303435-if02"
    # Which board it is: the adapter's own USB serial, which on a Nucleo is the
    # serial `probe_id` also carries. This is the machine-wide lock identity, and
    # it is compared against the attached hardware when the port is opened.
    # `adopt-hardware` fills both in; on Windows `device` stays "COM5" and this
    # is what carries the identity. From `version: 3` on an entry has to say
    # which hardware it is: this key, a `resource_id`, or a
    # `/dev/serial/by-id/...` device name. An adapter that publishes no serial
    # number of its own (a cheap USB-serial bridge) says so instead, with
    # `identity_source: vid_pid` beside `vid`/`pid`, or `identity_source: device`
    # when it publishes nothing at all.
    serial_number: "0669FF303435"
    baudrate: 115200
    assert_dtr: false        # this board wires DTR to reset: keep it released
                             # for the session. On Linux the open itself was
                             # measured to pulse it once, for 239 to 943
                             # microseconds on an FT232R.
    assert_rts: false
    permissions:
      allow_write: true

can_buses:
  dut_can:
    adapter: "socketcan"     # or "peak", or "process" for a custom bridge
    channel: "can0"
    bitrate: 500000
    listen_only: true        # receive without sending dominant ACK bits. A
                             # transmit on this bus is refused as
                             # can_listen_only_mode before any driver call,
                             # whatever allow_write says. Enforcement and
                             # verification reach as far as the driver's or the
                             # kernel's own report of the mode and no further;
                             # see docs/safety-model.md
    permissions:
      allow_write: false
```

## Connecting Under Reset

Some boards refuse their first flash after every power-up and take the immediate retry without complaint. The failure is at the erase, and the cause is the core: it is executing from flash while the probe attaches to it and interferes with the erase of the memory it is running out of. The retry works because by then the board has been reset by the failed attempt.

`debuggers.<name>.connect_mode` is how a bench says so once instead of retrying forever:

```yaml
debuggers:
  dut:
    type: "stlink"
    probe_id: "066AFF495451885087171450"
    connect_mode: "under_reset"   # hold the target in reset while the probe attaches
```

| Value | What the flash does |
|---|---|
| `hotplug` | attaches to the running core. The default, and what every configuration written before this key existed does, so nothing changes for a file that omits it. |
| `under_reset` | holds the target in reset while the connection is made, so the core never runs during the erase. STM32CubeProgrammer's `mode=UR`. |

Three things bound it:

- **Only `type: stlink` carries it.** OpenOCD reaches the target through the scripts named by `interface_cfg` and `target_cfg`, and connecting under reset is a `reset_config` decision inside them that depends on how SRST is wired for that adapter and part; ask for it there. Nothing this key could say reaches pyOCD: the one connect option this server passes there belongs to the typed memory reads and is fixed at `--connect attach`, with the target pack's DebugCoreStart sequence disabled so the connect leaves a halted core halted. `probe_target` keeps pyOCD's default connect, which is attach as well, and disables the same sequence. Both backends *refuse* `under_reset` when the file is loaded, with `config_invalid` naming the field, rather than reading a value they would then ignore.
- **Only `flash_firmware` reads it.** `probe_target` keeps connecting hot plug, because it is the least intrusive call this server makes and it erases nothing. `reset_target` and the typed memory reads keep their own connect: what they do to the target is decided by the operation, not by this key. The reads connect hot plug for a reason of their own, and it is the opposite of this key's: `mode=UR` holds the target in reset, and RAM read through a held reset line is not a measurement of anything the firmware did. The pyOCD reads connect with `attach` for exactly that reason, and pyOCD's other three connect modes all halt or reset.
- **The reset line has to be wired.** STM32CubeProgrammer pairs `UR` with a hardware reset, so the probe's reset pin must reach the target's NRST. On a Nucleo with its on-board ST-Link it already does; on a board wired with SWDIO, SWCLK and ground alone it does not, and the connect will fail rather than fall back.

`connect_mode` sits with the other things the bench *is*, under `allow_config_description_write`: it grants nothing, and both of its values are a flash the configuration already allows.

## An ESP32 through esptool

An ESP32 development board has no debug probe. `type: esptool` reaches the chip the way esptool always does: it pulls the chip into its ROM bootloader through the DTR and RTS lines of the board's USB-UART bridge (a CP210x, CH340 or FTDI part), flashes it over the same serial line, and lets it go again. So the entry names the `com_ports` entry of that bridge where other entries name a probe, and that entry says which board it is:

```yaml
com_ports:
  esp:
    device: /dev/ttyUSB0        # COM5 on Windows
    vid: "1a86"                 # a CH340 bridge, which publishes no serial number
    pid: "7523"
    identity_source: vid_pid
    baudrate: 115200
    assert_dtr: false           # DTR and RTS drive the chip's reset and boot pins:
    assert_rts: false           # released, the application runs while a session listens
debuggers:
  esp32:
    type: esptool
    com_port: esp               # the com_ports entry above, by name
    target_type: esp32          # optional; unset, esptool detects the chip
    flash_address: "0x10000"    # where an ESP-IDF application goes
    permissions:
      allow_flash: true
      allow_reset: true         # probe_target resets the chip as well
```

What follows from there being no probe:

- **The port is the identity and the lock.** `com_port` names the `com_ports` entry, not a device path, and that entry's `device` is passed to esptool as `--port` on every call, so esptool never scans the host's serial ports: its scan resets every board it reaches. The entry's `serial_number`, or `vid`/`pid` for a bridge that publishes no serial number, is checked against the adapter behind the name before esptool starts, as `com_session_start` checks it. `probe_id` and `resource_id` are refused on the debugger entry, and two esptool entries naming one `com_ports` entry are refused at load.
- **Flash, reset and probe; no debug session.** `flash_firmware` writes a `.bin` at `flash_address`, which has to start a 4 KiB sector, or a `.hex` at the addresses its records carry, and esptool verifies every write against the image. An `.elf` is refused: `esptool elf2image` turns one into an image, and `esptool merge-bin` combines bootloader, partition table and application into one. A flash without `reset_after_flash` leaves the chip in its ROM bootloader until `reset_target` runs it. `reset_target` mode `run` resets into the application, mode `halt` holds the chip in its ROM bootloader, the nearest thing to a halted core this interface has, and mode `init` is refused. `probe_target` resets the chip into its bootloader to read it, so it needs `allow_reset` as well. Every typed debug tool and `debugger_probes_list` answer `not_supported`, because esptool has no debug interface to the CPU.
- **The board needs an auto-reset circuit.** Every one of those calls starts with esptool restarting the chip into its bootloader through DTR and RTS, so the bridge's lines have to reach EN and IO0, as the two-transistor circuit on Espressif's ESP32-DevKitC wires them. A board whose bootloader is entered by holding a FLASH or BOOT button while RST is pressed has nothing those lines move. Every call ends on `target_not_detected`, with esptool's `No serial data received.` or, while the application is printing, `Invalid head of packet`; the board can be flashed only while somebody presses the buttons, and no `reset_target` starts its application.
- **Recovery resets only under `reset_halt`.** The default `recovery.auto_recover: reset_halt` settles an unconfirmed flash or reset as it does on a probe: a reset into the ROM bootloader, read back by a probe that leaves the chip there. Reading the chip is a reset here, so nothing is settled by a re-read alone: under `readonly` a failed run's `recovery` block names `probe_resets_target` and nothing reaches the chip, and under either policy an incident only a re-read would settle stands down when its call ends.
- **The console shares the line.** The bridge's serial line is the board's console too, so esptool and a COM session take turns on it: a flash or a reset while this server holds a session on the port is refused with `device_busy` and `held_by_com_session`, and `com_session_stop` clears the way. With `assert_dtr` and `assert_rts` false the application keeps running while a session listens. An entry that omits them asserts both, and depending on the order the driver moves the lines in, the auto-reset circuit can then reset the chip on open or hold it in its bootloader.
- **esptool 5 only.** The executable is found beside the Python this server runs on, which is where `pip install agentic-hil[esptool]` puts it, then on PATH; `executable` names another one. `esptool version`, which opens no port, is read first, and an esptool other than 5.x is refused as `debugger_not_found` with `backend_error_type: esptool_version_unsupported`. Each run gets a private working directory, an empty esptool configuration file and none of the `ESPTOOL_*`, `ESP_*` or `IDF_*` variables of the server's environment, so nothing left in an operator's shell adds a port, a chip or an option. esptool prints the chip's MAC address on every connect; reports and logs carry it redacted.
- **Written by hand.** Neither `project_config_create` nor `agentic-hil adopt-hardware` writes these entries: finding the board would mean opening serial ports, and opening an ESP32 board's bridge can reset the chip. Adoption refuses an esptool entry before it reads anything, and a regeneration does not carry one over: `project_config_create` lists it under `dropped_entries`, and `agentic-hil init --force` rewrites the whole file without it. `project_config_set` does not write `com_port`, so it cannot move an entry onto esptool or off it; such a switch is refused with the key under `operator_keys`, and it is an edit in the file.

`agentic-hil doctor` checks `target_type` without opening the port: it puts the name to esptool as `--chip=<name> version`, and a family this esptool does not know is red, with the close matches and the list esptool accepts. The MCP resource `agentic-hil://reference/debugger-backends/esptool` has the field matrix.

## Artifact Roots

`artifacts.allowed_roots` lists the directories under `workspace_root` that firmware may be flashed from and debug dumps written to, each including its subdirectories. There is exactly one spelling for the whole project:

| Written | Means |
|---|---|
| `["."]` | every directory under `workspace_root`, recursively. What `agentic-hil init` and `project_config_create` generate. |
| `["build", "cmake-build-debug"]` | those directories and their subdirectories, and nothing else. |
| key omitted | `["build"]`. What a configuration written before 0.8.0 means, and it keeps meaning that. |
| `[]` | refused at load, naming both replacements. It reads as "no restriction" to one reader and as "nothing at all" to another, and here it always meant the second. |

Build layouts differ per toolchain (CLion writes to `cmake-build-debug`, PlatformIO to `.pio/build/<env>`, others to `out`, `Debug`, or `zephyr/build`), so a list of directories has to be curated before the first flash while protecting nothing an attacker was stopped by. What refuses a foreign artifact is elsewhere and unchanged: the path must resolve inside `workspace_root`, which no configuration key relaxes and which a symlink or junction leaving the workspace does not satisfy; the extension must be in `allowed_extensions`; and the content is inspected for its format and hashed before a backend sees it. Narrowing the roots restricts a project against its own operator, so it is available and no longer the way in.

An update never widens this. What is generated changed; what an existing file means did not, whether it names its roots or omits the key. `project_config_create` carries the `allowed_roots` of the configuration that server loaded over into the file it regenerates, together with `allowed_extensions` (the check the wide roots lean on) the same way it carries the permissions of the entries that are still there.

## Changing it from an agent

An agent reaches this file only through the MCP tools; `agentic-hil setup` writes host deny rules so its own file tools cannot. Project-scoped permissions decide how far it gets, and they are deliberately separate:

```yaml
permissions:
  allow_config_write: true                  # regenerate the whole file from hardware discovery
  allow_config_description_write: true      # what the bench IS
  allow_config_permissions_write: true      # take back what the bench MAY do
  allow_upgrade: true                       # lift this installation to the newest release
```

`allow_config_description_write` opens `target.*`, `debuggers.<name>.type` / `probe_id` / `executable` / `gdb_server_executable` / `interface_cfg` / `target_cfg` / `connect_mode`, `com_ports.<name>.device` / `baudrate` / `serial_number`, every `can_buses.<name>` field except its permissions, and `debug.gdb_executable`. `allow_config_permissions_write` opens every permission key in the file: each `permissions:` block, and the two grants that sit directly on a section rather than inside one, `artifacts.allow_upload` and `debug.allow_all_symbols`. One permission for both would be a master key: set to let an agent enter a 24-character probe serial, it would in the same motion have handed over the permissions block.

`allow_upgrade` is the one that is not about this file. It opens `server_upgrade`, which replaces the installed package with the newest release, because on an MCP host without a shell there is otherwise no way for the main surface to perform the basic maintenance of its own server. It takes no version, so it cannot be used to install a release that reads the rest of this block differently, and it is subject to the same ratchet as everything else here: an agent can set it false and never true.

### Switching a probe to another debug stack

`debuggers.<name>.type` is in the description half, beside the three fields that already decide which binary runs with which scripts. An operator who granted description-write has handed over `executable`, `interface_cfg` and `target_cfg`; which debug stack the bench runs is the same kind of fact about the same bench, and holding it back only meant the file had to be opened by hand for it.

It is the one key here whose value decides which *other* fields its entry needs, so it is the one that is checked as a whole rather than field by field. A change to it is refused unless the entry that comes out of the call carries everything the backend it names requires and this surface can write, and the refusal names the missing keys:

```json
{"changes": [{"key": "debuggers.dut.type", "value": "openocd"}]}
```

```text
invalid_argument   debuggers.dut.interface_cfg
`debuggers.dut.type` would put this entry on the openocd backend, and the entry
does not carry `interface_cfg`, `target_cfg`, which openocd requires. A backend
switch lands whole or not at all: send the missing keys in the same call.
Nothing was written.
```

The whole switch is one call:

```json
{"changes": [
  {"key": "debuggers.dut.type", "value": "openocd"},
  {"key": "debuggers.dut.executable", "value": null},
  {"key": "debuggers.dut.interface_cfg", "value": "C:/tools/openocd/share/openocd/scripts/interface/stlink.cfg"},
  {"key": "debuggers.dut.target_cfg", "value": "C:/tools/openocd/share/openocd/scripts/target/stm32f4x.cfg"}
]}
```

Three notes on that call:

- **Send `executable` too.** It is not demanded, because an entry may legitimately have none and let the backend be discovered, but an executable *already* in the entry was chosen for the backend the entry is leaving. `null` is how you ask for the new backend's binary to be found on PATH.
- **Only what this surface can write is demanded.** `interface` on stlink and `target_type` on pyocd are required by those backends and are not keys `project_config_set` sets; demanding them would make a switch impossible rather than atomic. Both work unset, and the MCP resource `agentic-hil://reference/debugger-backends` states per backend what it requires, discovers, ignores or refuses.
- **A field the new backend refuses is caught by the loader.** Nothing on this surface knows what OpenOCD can carry out; what it knows is that the changed file has to load as authoritative before it may replace the one that did. So switching an entry that carries `connect_mode: under_reset` to `openocd` is refused as `config_invalid` naming `debuggers.<name>.connect_mode`, and the file that was there stands untouched. Send `connect_mode: hotplug` in the same call to make that switch land.
- **esptool is the one switch no call lands.** An esptool entry needs `com_port`, and every other backend refuses it, and `project_config_set` writes it in neither direction. A switch onto esptool or off it is refused with `operator_keys` naming that key, rather than `missing_keys` sending you round a loop: it is the operator's edit in the file. [An ESP32 through esptool](#an-esp32-through-esptool) has the entry.

A changed `type` is a device description like any other, so the running server keeps answering out of the configuration it loaded until `project_config_reload_description` or a restart.

What the switch keeps: `openocd` and `pyocd` both open typed debug sessions, each through its own GDB server (OpenOCD's, and `pyocd gdbserver` on a port the server reserves for the session), so moving between those two keeps every session tool with the same payloads; only `reset_target` mode `init` stays with OpenOCD. One difference shows on a core that is already halted when a session opens: pyOCD runs the part's CMSIS-Pack `DebugCoreStart` sequence at every connect, and on the reference board that sequence lets the core run for a few tens of milliseconds before pyOCD halts it again, so an `attach` session finds it slightly further on, where OpenOCD's finds it where it was. `stlink` opens them too, through ST-LINK_gdbserver, the GDB server STM32CubeCLT installs beside STM32_Programmer_CLI: named as `debuggers.<name>.gdb_server_executable`, or found by itself in the same STM32CubeCLT tree as the configured CLI, so a switch to `stlink` whose `executable` is STM32CubeCLT's CLI keeps every session tool as well. The server is started with the CLI's directory as `-cp`, the probe's serial as `-i`, `-d` for SWD and `-g`, which halts the core where it runs without resetting it; `reset_halt` then resets it with `monitor reset`, which leaves it halted. While the session lasts the server listens on every interface of the host, on the session's port and, where it is free, on the one after it. It also gets `-t` wherever stlink-server, which STM32CubeCLT installs to share one in-circuit debugger or programmer between its tools, is already listening or can be started from PATH: the server then reaches the probe through stlink-server rather than holding its USB itself, which is what a session stop needs, because the stop kills the server (every end the server runs itself resumed the core) and a killed server that held the USB left the probe refusing its next opener in part of the recorded stops. Every session result says which way it went in `probe_server`, and the direct way carries what its stop risks and that putting stlink-server on PATH is the way out. Where the entry names no server and none is found, moving to `stlink` keeps flashing, resets, probing and the three symbol reads, and the session tools refuse with `not_supported`, naming both ways out: name or install the server, or keep the probe and run it under `type: openocd`.

### Naming the GDB this bench reads with

`debug.gdb_executable` says which GDB reads this bench's images: the one every typed debug session connects to the backend's GDB server with, on openocd, pyocd and stlink alike, and the offline symbol resolution the stlink and pyocd memory reads run against the flashed ELF before they touch the target. It is in the description half beside `debuggers.<name>.executable`, for the same reason: it is a toolchain on this host, at a location only this host can state, and it grants nothing that the debug permissions beside it do not already decide.

Left `null` it is not a hole. Every caller that needs GDB then resolves one on the PATH the server was started with, trying `arm-none-eabi-gdb`, `gdb-multiarch` and `gdb` in that order, which is what a generated configuration relies on, because generation writes `null` whenever the generating shell had no GDB on its own PATH. Name it when that fallback cannot answer or answers with the wrong one:

```json
{"changes": [{"key": "debug.gdb_executable", "value": "C:/ST/STM32CubeCLT/GNU-tools-for-STM32/bin/arm-none-eabi-gdb.exe"}]}
```

The value is held to the rule every configured executable is held to, and it is the loader that holds it: an absolute path (or a bare name this host resolves on PATH) to an existing single-link file, stored outside `workspace_root`. A value that is none of those is refused as `config_invalid` naming `debug.gdb_executable` in the loader's own words, and the file that was there stands untouched, because the changed document has to load as authoritative before it may replace the one that did.

`agentic-hil adopt-hardware` fills the key in too, from the same three candidates in the same order, so a bench set up with the toolchain installed says which GDB it uses without anybody typing a path. A key that already names one comes back under `kept`: which GDB reads these images is a choice, and adoption does not overrule one.

### Permissions move one way

A generated configuration grants everything it can, and the one MCP call that writes a permission field-wise can only ever take grants away. `project_config_set` writes `false` into a permission and no other value: not `true` into one it never touched, and not into one it set to `false` itself a moment earlier. Such a call is refused as `permission_widening_denied`, whatever key it named: once on the value it carried, and again on a comparison of the permissions present in the document before and after the write, so a permission that came on some way the key model did not anticipate is caught too.

```text
Generation              every permission true, except the two flashing is interlocked against
project_config_set may  write false into a permission
             may not    write anything else into one, ever, not even into one it just closed
Last move               permissions.allow_config_permissions_write: false
After that              only you, with `agentic-hil grant` or `agentic-hil init --force`
```

So "the agent may set permissions" does not mean "the agent may set itself anything". It means you can say *narrow this bench* in prose and have it done, with a `provenance` record, and no editor. Closing `allow_config_permissions_write` is terminal for that call: it cannot move a permission in the file again, and the call that does it says so in its own result (which permissions stand frozen, that the agent cannot undo it, and which command reopens it).

### Opening one again: `agentic-hil grant`

The other direction is yours, and it no longer costs you the file:

```bash
agentic-hil grant can_buses.dut.allow_write          # open one permission
agentic-hil revoke debuggers.dut.allow_mass_erase    # and close one again
```

One named permission moves and nothing else in the configuration is touched: the baudrate, the `resource_id`, the `state_root`, the artifact roots and everything else you ever set stay exactly as they are. That is the whole point of it. Before this, a bench whose `can_buses.<n>.allow_write` was `false` could be reopened only by regenerating the file from attached hardware or by deleting it, and both rewrite far more than the one flag you meant.

Both directions ship together, so this command line is not a one-way street either. What is worth knowing:

* **Single keys, several per command, no wildcard and no whole-entry form.** `agentic-hil grant debuggers.dut.allow_flash debuggers.dut.allow_reset` opens exactly those two, together or not at all. There is deliberately no `debuggers.dut` form: it would mean whatever permissions that entry's schema happens to have *at the moment you type it*, so a flag added in a later release would silently join a command you learned years ago.
* **Both spellings work.** `can_buses.dut.allow_write` and the long `can_buses.dut.permissions.allow_write` name the same key. A name that is neither is refused with a list of every permission key your configuration actually has, entry names and all. That is how you find one, and why there is no wildcard to need.
* **It says what it changed**, including the value it replaced, and names anything already in the asked-for state as a no-op rather than as a change. A pure no-op writes nothing at all: no `provenance` entry, no change marker in the header.
* **A restart is what makes it bind.** A running MCP server parses permissions once, at startup, and `project_config_reload_description` deliberately re-reads none of them. The result says so rather than leaving you to wonder why a granted permission is still refused.
* **It is refused while anything holds the bench**: a run, a COM or CAN session, another terminal. Those holds were taken under the permissions in this file. `agentic-hil lease-status` names the holder.
* **It is not an MCP tool and never will be.** It appears in no tool list, so an agent cannot call it, and the ratchet over MCP is exactly what it was. This is your shell, which already has `agentic-hil init --force`.

**Regenerating is yours, and is not covered by that rule.** `project_config_create` refreshes the hardware facts and carries the permissions of the configuration **that server loaded at startup** over for the entries that are still there; but a workspace whose configuration is gone gets the skeleton at its generated defaults, anything a regeneration discovers for the first time arrives at those defaults, and `agentic-hil init --force` in your own terminal rewrites the whole file back to them. A server does not reload, so a permission narrowed with `project_config_set` in the same session is on disk and not in what that server holds, and a regeneration before it restarts writes the wider loaded value back. Regeneration is creation rather than a permissions write, and it belongs to you and your command line.

With the first set, an agent calls `project_config_describe` (which keys are open right now, which are not, and which permission would open a locked one) and then `project_config_set`:

```json
{"changes": [{"key": "debuggers.dut.probe_id", "value": "066AFF495451885087171450"}]}
```

Named keys with scalar values, each checked against the shipped schema. No document and no subtree can be sent, so the file stays something the agent did not author. A write is refused while a run or a session holds hardware, the changed document is validated before it replaces the working one, and `provenance` records what moved, when, and through which call. The MCP resource `agentic-hil://reference/config-shape` is the full description, with a worked example and what deliberately cannot be done.

## A board attached after the configuration was written

`agentic-hil setup` discovers hardware once. Run it with nothing plugged in and it writes placeholders (`probe_id: null`, `executable: null`, `controller: "unknown-controller"`), which is the common case, because installing the tool and connecting the board are two separate moments. Where the workspace has an `agentic-hil.config.example.yaml`, the placeholder carries what that profile already states: the board's name and controller, and each `com_ports` entry by name, baudrate and permissions with no `device`, since which of this host's serial devices a port is, is exactly what discovery did not find out. Nothing opens a port in that state; a call or a plan step that names one is refused `com_port_not_bound`, which says the bench is not filled in rather than that the port is unconfigured.

### What discovery looks for

Two enumerations, and which one runs is decided by one fact about the host. With STM32CubeProgrammer's `STM32_Programmer_CLI` installed, the probes come from its ST-Link listing and the target names itself over a HOTPLUG connect, as they always have. Without it, the ST-Link is enumerated from this host's own USB serial inventory (a port whose vendor is `0483` and whose product is an ST-Link id publishes the probe serial in its descriptor, which is the string OpenOCD selects the probe by), the toolchain is the `openocd` on `PATH`, and the generated `debuggers` entry is `type: openocd` with its two scripts. That is the ordinary Linux workstation, where `apt install openocd` is one command and STM32CubeProgrammer is a registration wall, and it is the Nucleo + ST-Link + OpenOCD bench this project supports first. With neither tool installed the refusal names both and says OpenOCD alone is enough.

That USB serial inventory is not an authoritative count of what is attached: it reaches an ST-Link only through the virtual COM port a V2-1 or a V3 publishes, so a standalone ST-LINK/V2, or any probe with no VCP, can be plugged in and never appear (`complete: false`), which means an empty reading is not proof no probe is connected and a sole visible one is not proof it is the only one. Exactly one visible ST-Link is bound anyway, because that is what makes `agentic-hil init` from the project root reach a working bench on a host that has OpenOCD and nothing from ST, and the caveat travels with it instead of blocking: the discovery result and the `type: openocd` entry it writes both carry `discovered_by: usb_serial_inventory`, `probe_inventory: incomplete` and the sentence naming what that reading cannot see, and `init` and `project_config_create` say it in a step of their own. Where a probe this inventory cannot reach is attached beside the visible one, name the intended board with `agentic-hil adopt-hardware --probe-id <serial>` (the serials the host is showing are under `hardware_discovery.probes` and from `agentic-hil debugger-probes`); where an authoritative count is what matters, install STM32CubeProgrammer, whose listing reads the serial off the probe itself. Two or more visible probes are `ambiguous_hardware` as always, and a reading with no ST-Link in it at all is `probe_inventory_incomplete`: it has nothing to bind and cannot report an absent bench either, so `init` writes the placeholder and `project_config_create` writes nothing.

On that second path the target is named by the workspace profile's `target.controller` when it states one, which is more exact than any read of the board and says nothing to it; otherwise by a read-only OpenOCD `init`, `targets`, `shutdown` against the probe selected by that serial, which reports the target script's family rather than the part number. A probe whose target nobody could name is still written down, because the probe serial, the toolchain path and the board's own COM port are the values nobody retypes correctly. Every result says which enumeration answered under `discovered_by`, and `agentic-hil init` reports which binaries it looked for, where each resolved, and which ST-Link serials the host is showing on which ports.

```bash
agentic-hil adopt-hardware          # --dry-run to see the plan first
```

It reads the attached probe and fills in what is still unset: the probe serial, the backend's executable, the GDB this host answers with (`debug.gdb_executable`), the detected controller, and the COM device the probe itself exposes. Nothing else: it supplies no value of its own, and its arguments only select (`--probe-id` names which probe this is about, `--debugger` and `--com-port` name which configured entries to fill). A lone attached board is adopted without naming it on either path; `--probe-id <serial>` is what disambiguates among several attached boards, and on an OpenOCD-only host it is also how the intended board is named where a probe that publishes no virtual COM port is attached beside the visible one, which that USB serial inventory would not list. A key that already holds a value nobody generated is reported as a disagreement and left alone, so a bench somebody set up is never repointed because something else happens to be plugged in; and an entry that already names a probe with a *different* probe attached is refused whole rather than partly carried, because the identity keys describe one board between them. A `com_ports` entry it creates arrives with every permission `false`, written by the server: a generation grants, a write only ever takes away, and adding an entry is a write. `agentic-hil init --force` is what opens a new entry.

Reading a probe is a hardware call like any other here: it takes the same machine-wide lock, so a board another MCP server, test-reactor run or terminal is holding answers `device_busy` instead of being connected to behind its owner's back, and the read is written into the same audit trail. On a configuration written before `version: 2`, where reading still needs `allow_probe`, an entry that denies it denies this too.

An agent does the same over MCP with `project_config_adopt_hardware`, which returns the plan and writes only with `{"apply": true}` and `permissions.allow_config_description_write`. Without that permission it still returns the exact keys and values, so a refusal ends with a person copying one command rather than transcribing a 24-character serial. Both paths go through `project_config_set`, so both are recorded in `provenance` and neither names a `permissions:` key at all.

## Changing it while the server runs

The MCP server parses this file once, at startup. Its **permissions** stay that version's for the life of the process: a policy that changed under a held board would move the rules during the run they govern, and a server bound to a policy must not be able to widen its own grants from a file that moved underneath it.

Its **description** of the bench can be re-read on request, because that argument was never about probe serials and COM devices; see [Picking up a board without a restart](#picking-up-a-board-without-a-restart). Nothing re-reads anything on its own, and no call adopts a permission.

What the answers do say is whether the two still match. Every result carries a `config_status` block once they do not, plus a flat `config_stale: true`; `debugger_info`, `project_config_describe` and `agentic-hil doctor` carry it in every case, so "this is the configuration in force" is a positive statement and not an absence. The comparison is a SHA-256 of the exact bytes the running server parsed against the bytes on disk now, not a modification time, which calls a `git checkout` that restored identical content a change and misses two writes inside one timestamp tick. It is a comparison and nothing more: it reports that the two differ, never what a restart onto the new file would produce. Establishing that would take a second validation congruent with startup, and the value of a promise like that is exactly the congruence of two code paths nobody can keep aligned. `state` is one of five, and they do not share a remedy:

| `state` | What it says | What fixes it | `config_stale` |
|---|---|---|---|
| `unchanged` | `current_digest` equals `loaded_digest`; the file on disk is byte-for-byte the one in force | nothing | absent |
| `changed` | the two digests differ; the file on disk is not the one this server loaded. That is the whole claim: nothing is said about what the file now contains | if what moved is the bench's description, `project_config_reload_description`. Otherwise restart the MCP server; if it does not come back, the startup refusal names what is wrong with the file, so repair it and restart again. `agentic-hil doctor` produces the same refusal without stopping anything | `true` |
| `missing` | the file is gone, so `current_digest` is `null` and there is nothing to restart onto | restore the file, *then* restart | `true` |
| `unreadable` | it is there and will not open: a permission on the file or a directory above it, a path component that is no longer a directory, bytes that are no longer UTF-8. `current_digest` is `null` and `backend_error` names the failure | make it readable, *then* restart | `true` |
| `unknown` | this configuration did not come from a file this process read, so no comparison was made | nothing; `reload_required` is `false` and there is no claim to act on | absent |

While the file is `missing`, `project_config_describe` answers out of the loaded policy rather than refusing: `permissions_in_force` is what this server is still enforcing and `document_source: loaded_policy` says the values came from memory, not from a document that could be compared. `agentic-hil doctor` re-reads the file on every invocation and is therefore always current, which is why its `config_status.loaded_digest` is the value to compare a running server's against; if they differ, that server is answering out of the older file.

One thing does follow the file while the server runs, and it can only ever narrow: `project_config_describe` and `project_config_set` gate on the narrower of the permissions this server loaded and the ones on disk. A permission an operator revokes therefore binds on the next call, and one they add needs the restart.

## Picking up a board without a restart

Installing the tool and plugging the board in are two separate moments, and the file is usually written in the first one. Until now a board written down in the second one was invisible to the running MCP server, and the only way across was the operator restarting it from their agent host. `project_config_reload_description` is the way across, and `agentic-hil config-reload` is the shell-side check of what it would take from this file before anyone makes the call.

It takes no arguments and re-reads exactly four sections (`target`, `debuggers`, `com_ports`, `can_buses`) minus every `permissions:` block inside them.

**It re-reads no permission at all.** Not narrowed, not compared, not adopted, and there is no flag that changes that. The grants in force after a reload are byte-for-byte the ones parsed at startup, and a device this server has never seen arrives holding **nothing**: from `version: 2` on it can be probed and read, because reading needs no grant and exclusivity is what protects it, and flashing, reset, mass erase and COM/CAN writes are denied on it until an operator restarts the server onto the file that grants them. Renaming an entry is the same case: the new name is a device this server has not seen, so it arrives closed.

What still needs a restart is refused by name rather than quietly skipped, because each of these is either a permission wearing a description key's clothes or a section that mixes the two: `permissions`, `version` (it decides whether reading needs a grant at all), `workspace_root`, `state_root`, `debug` (`allow_all_symbols` is a grant, `allowed_symbols` an allowlist), `artifacts` (`allow_upload` is a grant, `allowed_roots` decides what may be flashed), `validation`, `recovery` (it decides whether the machine may drive a physical reset on its own), `reports` and `logs`. The result lists them with the reason.

It is refused while a run, a COM/CAN session or a debug session holds this bench (`config_reload_in_open_run`), while an incident on it is unresolved (`resource_quarantined`), and when the file is missing, unreadable or does not load: the same three states `config_status` reports, with nothing changed on any of them. It writes nothing, so no grant gates it: a bench whose `allow_config_description_write` is false can still pick up a board somebody plugged in.

Afterwards `config_status` compares the file against the description now in force, so a reload that just took the file's description does not leave `config_stale: true` behind for it. The other half stays visible: when the file's permissions differ from the ones being enforced, the status carries `permissions_source`, which says the grants came from the document parsed at startup and that a restart is what adopts the file's. The reload's own result lists the differences under `permission_differences`, taken while both documents were in hand.

## Migration

Versions 1, 2 and 3 are all read, so no file already on disk has to move; what changes is what a file that opts into a version promises. Nothing is inferred from the absence of a key, so an update never widens what a bench already allows and never adds a requirement to a file that did not ask for one.

A configuration written before 0.8.0 has no `version:` key and is read under version 1, where reading still needs `allow_probe` on a debugger and `allow_read` on a COM port or CAN bus. Migrating to version 2 is one edit: remove those keys and set `version: 2`. A version 2 file that still carries one is refused by name rather than silently ignoring it.

Version 3 requires every `com_ports` entry to say which hardware it is. A `serial_number`, a `resource_id` or a `/dev/serial/by-id/...` device name is such a statement; a bare `device: COM7` is not. It is an enumeration order, so attaching a second adapter can hand one entry the other's board. An adapter that publishes no serial number has a deliberate exception, and it is visible in the file rather than reported as a warning: `identity_source: vid_pid` beside `vid`/`pid` for one that publishes USB ids, `identity_source: device` for one that publishes nothing at all. A version 3 file with an entry that carries none of these is refused at load, naming the entry and `agentic-hil adopt-hardware`.

Migrating is one edit again, in this order: run `agentic-hil adopt-hardware` with the boards attached while the file still says `version: 2` (it fills in `serial_number`, `vid` and `pid` from each adapter, and records `identity_source` where the adapter turns out to publish no serial), then set `version: 3`. That order matters because the command loads this configuration, so it has to be able to. A bench that stays at `version: 2` keeps working exactly as it does today, with `agentic-hil doctor` reporting the same identity as a warning. `agentic-hil init` and `project_config_create` write version 3.

The operator reviews this file and takes away the permissions this bench should not have; a generated one grants all of them except `allow_raw_debugger_commands` and `allow_mass_erase`, which it writes false so that flashing works, and adding a resource is still an operator's edit. `workspace_root` is mandatory and must exactly match the project root used to launch Agentic HIL. `state_root` is also mandatory: it must be an absolute, operator-controlled directory outside and non-overlapping with the workspace. Every trusted launcher for the same host resources must use this pinned root; changing `LOCALAPPDATA` or `XDG_STATE_HOME` after initialization does not change a running service's coordination namespace. Configured debugger/GDB/process-bridge executables must resolve to existing host-owned files outside the workspace, and so must an OpenOCD script named by path. An OpenOCD script named the way OpenOCD names its own (`interface/stlink.cfg`, `target/stm32f4x.cfg`: forward slashes, no leading `.` or `~`) is a search name, resolved by the OpenOCD that runs and accepted here without a file on this host; `agentic-hil doctor` reports which of the two each value is. Either spelling is refused under the system temporary directory, which is cleared without warning, and so is a configured executable, measured where the value resolves to (a bare name through `PATH`, a link at its target, an autodetected candidate where it was found). A configured executable is also refused under a package manager cache (`~/.cache`, `~/Library/Caches`, `UV_CACHE_DIR`, `XDG_CACHE_HOME`, `LOCALAPPDATA/uv/cache`), the same list the MCP launcher is measured against, because a cache is not an installation boundary. A configuration that itself lives under one of those roots is not refused for that root, since it is already as ephemeral as the field could make it. Empty symbol allowlists deny all symbols; unrestricted symbol access requires `allow_all_symbols: true`. Set optional `resource_id` on a debugger, COM, or CAN entry when different host paths/wrappers address the same physical resource; matching IDs share one cross-process lease. A `resource_id` names hardware rather than a file, so it is matched case-insensitively on every platform, and two entries that spell one id in two cases are refused rather than merged: make them identical if they are one unit, or different by more than case if they are two. Two `debuggers` entries that resolve to the same physical probe are rejected outright, so a plan naming one board can never drive another.

All hardware entry points use this same file: `doctor`, `mcp-stdio`, `com-stdio`, the pytest plugin, and `test-reactor`. Deprecated configuration-path options are still parsed, and parsing is the whole of what they still do: one that names anything other than the discovered external file stops the run instead of being tolerated. They cannot redirect authority, and they do not degrade to it either, because a suite that had its policy quietly replaced by the one it did not ask for is the worse of the two outcomes. The pytest plugin's `--agentic-hil-config` and its `agentic_hil_config` ini key are the surviving pair; [Running Hardware Tests](testing.md#pytest-plugin) says what they do today and what to use instead.

Export the full JSON schema with `agentic-hil schema --output agentic-hil-config.schema.json`.
