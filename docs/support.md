# Support

Agentic HIL exists so that a coding agent can develop firmware on the board in
front of you, with the run on that hardware, rather than a green build, deciding
whether the work is done. This page says what is supported when you take that
path, how quickly you can expect an answer, and what is deliberately not
promised.

## Platforms

Linux, macOS and Windows are supported equally. CI runs the full suite on all
three, and the reference path is proven on Linux and Windows benches, with a
board attached rather than only in a hosted runner. Python 3.10 or newer is the
floor on every one of them; [Installation](installation.md) has the per-platform
detail.

## What "supported" is attached to

The supported unit is the debug probe together with the backend that drives it,
not a board:

| Probe | Backend |
|---|---|
| ST-Link | OpenOCD, or the STM32CubeProgrammer CLI |
| CMSIS-DAP probes | pyOCD |
| an ESP32 board's USB-UART bridge (CP210x, CH340, FTDI) with an auto-reset circuit | esptool 5: flashing, reset and probing, no debug sessions |
| RP2040 and RP2350 boards using their own USB port | picotool 2: verified UF2, ELF and BIN flashing, reboot and probing, no typed debug. Hardware-unproven. |

Recognized picotool releases are 2.0.0, 2.1.0, 2.1.1, 2.2.0, 2.2.0-a4, 2.3.0
and 2.3.1. Picotool 2.0.0 leaves the chip family unknown while a board is
running. With picotool below 2.3.0 on Windows, use `reset_target` in `halt` mode
to enter BOOTSEL before flashing a running RP2040.

OpenOCD, STM32CubeProgrammer CLI, pyOCD and esptool have hardware support
evidence. The probe or USB interface, backend, and target settings determine
which boards the bench can reach. See [Configuration](configuration.md).
An ESP32 board has no probe:
esptool reaches the chip through the serial bridge on the board, whose DTR and
RTS lines have to restart the chip into its bootloader the way the auto-reset
circuit on Espressif's ESP32-DevKitC does, and
[An ESP32 through esptool](configuration.md#an-esp32-through-esptool) says what
that changes and what a board with only FLASH and RST buttons answers instead.

The Arduino Nano ESP32 demo uses the board's native USB-Serial/JTAG interface
for verified flash, reset to run and read-only console checks. The shared-bench
qualification plan checks 20 consecutive A/B cycles. Its job steps bound what
can be reported about reconnects. Physical disconnect is outside the plan.
The supplemental console refusal plans exercise flash and reset while the
console is open. On a shared bench with several debuggers, the `device_busy`
receipt identifies the lock as held by the current process. The demo matches
that lock to the open console before publishing the refusal. See the pull
request for the hardware receipts. Probe exclusion and competing port owners
are covered offline.

The Nucleo-F446RE remains the reference board for
[the STM32 starter](https://github.com/agentic-hil/stm32-starter) and its
worked example in this repository.

A recorded shared-bench run with Agentic HIL 0.28.0 demonstrated CMSIS-DAP
through pyOCD on a Raspberry Pi Pico H (RP2040) behind a Raspberry Pi Debug
Probe. The run flashed the [Pico H demo](https://github.com/agentic-hil/agentic-hil/tree/master/examples/pico-rp2040_demo),
reset the board in run mode, and received the complete `Hello World` line
on the probe's UART within the five-second timeout. The flash step reported
`verify: false`. The UART line after reset confirmed that the image ran.

## Response

- **Issues: within 24 hours on workdays.** That is a first answer, not a fix: a
  question answered, a reproduction asked for, or a defect confirmed and
  labelled.
- **Security reports: within seven days.**
  [SECURITY.md](https://github.com/agentic-hil/agentic-hil/blob/master/SECURITY.md)
  is the route and the acknowledgement window, and a policy or permission bypass
  is a vulnerability rather than a defect. Report one privately and never as a
  public issue.

## Releases

The last two minor releases are maintained. A report against either is one this
project acts on, and a fix ships forward as a new release rather than as a patch
onto an older line, so the answer to a defect you hit is an upgrade.
[Release strategy](release-strategy.md) has the versioning rules and the release
gate.

## Where to go

| What you have | Where it goes |
|---|---|
| a question, or something that is not clearly a defect yet | [Discussions Q&A](https://github.com/agentic-hil/agentic-hil/discussions/categories/q-a) |
| a first run on your own bench, green or red | [the first run report](https://github.com/agentic-hil/agentic-hil/issues/new?template=first-run.yml) |
| a run, a board or a bench worth showing | [Discussions, Show and tell](https://github.com/agentic-hil/agentic-hil/discussions/categories/show-and-tell) |
| a reproducible defect | [the bug report](https://github.com/agentic-hil/agentic-hil/issues/new?template=bug_report.yml) |
| a vulnerability | [SECURITY.md](https://github.com/agentic-hil/agentic-hil/blob/master/SECURITY.md) |

A first run report is wanted whichever way the run went. A red one says where
the path breaks for somebody who has not read the source, which is the only way
that gets found; a green one says which probe, backend and operating system
combination has actually been walked end to end by someone other than this
project.

## What is not promised

**No list of supported or unsupported boards.** The probe and its backend
decide, so a board is not something this project certifies. Where the probe and
backend are supported and your configuration binds the target, the board
follows; where they are not, no entry in a table would have made it work.

**No hosted bench.** Agentic HIL drives hardware you own and have plugged in.
There is no board to borrow here and no bench this project runs for you, so a
report that needs hardware to reproduce needs your hardware. A bench of your own
can be shared with the other machines on your network by running
`agentic-hil serve` on the machine it is plugged into; see
[Shared bench](shared-bench.md).
