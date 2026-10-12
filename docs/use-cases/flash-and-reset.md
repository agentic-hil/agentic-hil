# Use case: let a coding agent flash and reset the board

**The problem.** A coding agent that writes firmware should check its work on
the board: build, flash, restart, and see what the board does. The usual way to
give it that is a shell with `openocd`, `pyocd` or `STM32_Programmer_CLI` on the
`PATH`. That hands over every probe the host can see and every command the
debugger accepts, a mass erase included, and it leaves nothing a reviewer can
read afterwards except a scrollback.

**What Agentic Hardware-in-the-Loop (Agentic HIL) does instead.** The agent gets
narrow MCP tools, `flash_firmware`, `reset_target` and `probe_target`, or a
whole test plan through `test_reactor_run`, and never the debugger's command
line. Every call is checked against one configuration file the operator owns,
which lives outside the repository and out of reach of the agent's own file
tools: a probe it does not declare does not exist for the agent, and an action
it does not grant is refused rather than performed. Debugger calls run with
timeouts, and every call answers with a structured result that says what ran,
on which probe, under which configuration.

The rest of this page is two recorded runs on a real board, what they reported,
and where this stops.

## Which probes

The supported unit is a debug probe with its backend, not a board:

- ST-Link through OpenOCD (`type: openocd`) or through the STM32CubeProgrammer
  CLI (`type: stlink`).
- CMSIS-DAP probes through pyOCD (`type: pyocd`), which reaches most Arm
  Cortex-M targets through CMSIS packs.
- An ESP32 board's own USB-UART bridge through esptool (`type: esptool`),
  which flashes, resets and probes the chip through its ROM bootloader and
  opens no debug session.

Any board behind such a probe runs the same software. Both runs below used an
STM32F446 behind its onboard ST-LINK, the reference board of the
[STM32 starter](https://github.com/agentic-hil/stm32-starter), once through
OpenOCD and once through the STM32CubeProgrammer CLI. Neither used pyOCD.
[Platforms and debugger backends](../installation.md#platforms-and-debugger-backends)
has what each backend needs.

## What the operator grants

`agentic-hil setup`, or `agentic-hil init` on its own, writes the configuration
from the attached hardware, and `agentic-hil doctor` shows what it holds. From
the Linux run below, with the probe's serial number left out:

```text
Debuggers
  dut (openocd, bound)
    interface_cfg  interface/stlink.cfg (search_name) resolved by openocd
    target_cfg     target/stm32f4x.cfg (search_name) resolved by openocd
    permissions    granted: allow_debug_execution, allow_flash, allow_reset; closed:
                   allow_mass_erase, allow_raw_debugger_commands
    check           ok              OpenOCD is available.
```

- `allow_flash`, `allow_reset` and `allow_debug_execution` are granted per
  probe. A flash with `reset_after_flash` needs `allow_reset` as well.
- `allow_raw_debugger_commands` and `allow_mass_erase` stay closed. They are an
  interlocked pair with no tool behind either, and flashing is refused while
  either one is open.
- Over MCP an agent can close a permission and never open one. The operator
  reopens one with `agentic-hil grant <key>` from their own shell.
- A `permission_denied` result is the answer to the request. The agent is
  instructed to report it, name the permission, and stop, rather than reach the
  board another way.
- The firmware image has to sit under the configured artifact roots with an
  allowed extension. It is validated, rechecked and copied to private staging
  before it is flashed, and the result records its SHA-256.

## The plan

The starter's nominal plan flashes the image, opens the serial line, resets the
board and then reads the board's answers. Its first three steps, with the
comments removed:

```yaml
version: 3
name: nucleo-f446re-nominal-status
steps:
  - device: dut
    action: flash
    image_path: build/Debug/stm32-starter.elf
    reset_after_flash: false
  - device: dut_uart
    action: uart_open
    clear_buffer: true
  - device: dut
    action: reset
    mode: run
```

`dut` and `dut_uart` are names the configuration binds to the probe and the
serial port, so the plan carries no probe serial and no port. An operator runs
it with:

```bash
agentic-hil test-reactor --test-config tests/hil/nominal.testconfig.yaml
```

An agent runs the same plan through `test_reactor_run`. Both reach the same
code: the same checks of every device name, permission and artifact before the
first hardware action, the same devices held for the whole run, the same report.
The serial half of the plan is on [UART and CAN feedback](uart-and-can.md).

## OpenOCD on Linux

Agentic HIL 0.21.5 from the one-line installer, Ubuntu 24.04, OpenOCD 0.12.0
over SWD, 2026-09-15. The run followed the starter's public README and was made
by an automated agent, not by a person; the
[record](https://github.com/agentic-hil/stm32-starter/blob/main/validation/2026-09-15-newcomer-linux/README.md)
has every command, timestamp and report. The flash step, abridged:

```text
  - 1
      route       dut
      action      flash
      result
        Firmware flashed and verified. Target was not reset.
        backend                openocd
        success_confirmed      yes
        verify                 yes
        reset_after_flash      no
        lease_state            released
        safe_state_confirmed   yes
        processes_reaped       yes
        quarantined            no
        artifact
          source  path
          path    build/Debug/stm32-starter.elf
          sha256  0c219181f7ca595b95178ee76ac10fda82085c0a1fab31e3ec8acf033280bbb3
```

The probe was held for the step and released after it, and the backend's
processes were reaped. The reset step:

```text
  - 3
      route       dut
      action      reset
      result
        Target reset with mode 'run'. OpenOCD printed 1 failure-worded line in a run
        its own success marker confirmed; they are carried verbatim in
        backend_warnings.
        backend                openocd
        success_confirmed      yes
        backend_warnings       Error: Error setting register pc
        mode                   run
```

The verdict comes from OpenOCD's own success marker. A failure-worded line
printed in a run that marker confirmed is kept verbatim under
`backend_warnings`, rather than dropped or read as a failure, so a reviewer sees
everything the debugger said and still gets one verdict.

## STM32CubeProgrammer CLI on Windows, and a flash that failed

Agentic HIL 0.21.1, Windows 11, STM32CubeProgrammer 2.23.0 over SWD,
2026-09-03, [record](https://github.com/agentic-hil/stm32-starter/blob/main/validation/2026-09-03-bench-windows/README.md).
`doctor` showed the probe as `dut (stlink, bound)`. An earlier attempt at the
nominal plan was refused with `flash_erase_failed`, and the programmer's own
transcript was in the report:

```text
Connect mode: Hot Plug
...
Erasing memory corresponding to segment 0:
Erasing internal memory sector 0
Error: failed to erase memory
```

The refusal's guidance said to retry the flash once and that nothing had to be
recovered first, and its recovery block reported `outcome recovered`. The same
command, run once more, programmed and verified the board, and the refusal did
not come back in the eight plan runs after it. A failed flash reaches the agent
as a typed refusal with the backend's transcript and a next step, not as a page
of debugger output to interpret.

## Limits

- Reset modes `run` and `halt` work on every backend. `init`, which also runs
  the target's reset-init event script, is OpenOCD-only: the stlink, pyocd and
  esptool backends refuse it with `not_supported` rather than halting instead.
- A typed debug session (breakpoints, running to a breakpoint, symbol reads in a
  halted session) needs a probe of type `openocd`. The two memory reads,
  `read_symbol` and `dump_memory`, also run without a session on stlink and
  pyocd.
- There is no tool for a raw debugger command or a mass erase.
- The two records cover one board family and two of the four backends; pyOCD
  and esptool have no record here. Both were made on 0.21 releases. The project's own bench
  repeats a flash, a reset and a serial read of its demo every night on the
  current tree, and [CI examples](../ci-examples.md#the-bench-this-project-runs-itself)
  says what a green night claims and what it does not.

## Try it

The [STM32 starter](https://github.com/agentic-hil/stm32-starter) has the
firmware, three plans and one planted defect, and its README runs the loop on a
Nucleo-F446RE with either OpenOCD or the STM32CubeProgrammer CLI installed.
[Installation](../installation.md) covers every other bench;
[Configuration](../configuration.md) and the [Safety model](../safety-model.md)
have the rules above in full.
