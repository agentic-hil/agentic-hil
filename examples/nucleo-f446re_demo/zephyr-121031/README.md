# Zephyr #121031 on a NUCLEO-F446RE

A hardware report on the open Zephyr pull request
[zephyrproject-rtos/zephyr#121031](https://github.com/zephyrproject-rtos/zephyr/pull/121031),
"drivers: serial: stm32: bound the wait before suspending". It measures the
change as the pull request's head has it, against the commit the pull request is
based on; it is not a fix of its own.

The runs are driven by [Agentic Hardware-in-the-Loop (Agentic HIL)](https://github.com/agentic-hil/agentic-hil):
its test reactor flashes each image, resets the board with the serial port
already open and records what the image prints until its last line, following
the plans in [plans/](plans/). Nothing reached the board any other way.

## What the change does

`uart_stm32_suspend_setup()` waits for the USART to go idle before the driver
suspends it: for reception to end (`BUSY`), for the transmission to complete
(`TC`) and for the receiver to acknowledge its enable (`REACK`). Without the
change those waits spin without a limit, so a transmitter that cannot finish its
frame, because the peer holds CTS, blocks whichever thread or work queue runs the
suspend for as long as CTS is held. The change bounds each wait with
`WAIT_FOR(..., CONFIG_UART_STM32_SUSPEND_TIMEOUT_US, NULL)`, a new Kconfig
integer that defaults to 100000 us and depends on `PM_DEVICE`, logs an error and
returns `-EBUSY` when a wait runs out, and `uart_stm32_pm_action()` returns that
error before it switches the peripheral clock off.

## Coverage, and what is not claimed

- The F4 USART has neither `USART_ISR_BUSY` nor `USART_ISR_REACK`, so of the
  three waits only the transmission-complete wait is compiled for this SoC, and
  only that one is exercised here. On the F446 one timeout is therefore the
  whole bound; what three consecutive waits add up to on a SoC that compiles all
  of them is not something this board can show. The build record prints
  `has_busy=0 has_reack=0` as the compiled-in proof.
- The images are built with `CONFIG_LOG=n`, so the driver's `LOG_ERR()` on a
  timeout compiles to nothing; the logging path is not exercised. The return
  code is.
- The suspend is requested directly with `pm_device_action_run()` from a work
  item. `CONFIG_PM` and `CONFIG_PM_DEVICE_RUNTIME` are not set, so neither
  system power management nor the runtime-PM path is covered.
- CTS is driven by the pin's own internal pull resistor. Nothing is connected
  to the board and nothing is measured outside the chip: no modem, no external
  flow-control partner and no logic analyser is involved, and no claim is made
  about any of them. Time is measured in the MCU with `k_cycle_get_32()`, never
  on a host.

## The two images

Both are built from the one Zephyr application in [zephyr/](zephyr/), with the
same source, overlay and `prj.conf`, so the Zephyr tree is the only difference
between them and any difference in behaviour is the pull request's.

| Image | Zephyr tree | Revision | Reports itself as |
|---|---|---|---|
| `head` | the pull request's head | `90ccb4f9d4efc6a2886a8c910f7129856667ae04` | `zephyr=v4.5.0-rc1-2-g90ccb4f9d4ef timeout_us=100000 limit_cycles=9600000` |
| `base` | the commit it is based on | `3c61a12bdd8b2975b9180abcb80779ad3724a40d` | `zephyr=v4.5.0-rc1-1-g3c61a12bdd8b timeout_us=0 limit_cycles=0` |

Both use hal_stm32 `c9550199186236d610092f4005f8c9bae2a6bfcf`, cmsis_6
`1c1840af7a7e757d6e2fec3ddb0e5ce0dfcc93c8` and cmsis
`512cc7e895e8491696b61f7ba8066b4a182569b8`, the modules both commits' `west.yml`
name. The images print raw measurements and never a verdict.

## The setup, all inside the chip

- Test UART: `usart1` with `hw-flow-control`, TX on PB6, RX on PB7, CTS on PA11
  and RTS on PA12 (`usart1_cts_pa11`, `usart1_rts_pa12` of the F446 pin control
  file in hal_stm32), all alternate function 7. The console stays on `usart2`
  (PA2/PA3), which is what the ST-LINK's virtual COM port reads.
- PA11 and PA12 reach only the morpho header of the board; no other function on
  the board drives them.
- CTS is active low and the pin control node gives PA11 a pull-up, so an
  unconnected CTS reads deasserted and the USART holds the transmitter. The
  in-chip equivalent of a jumper is PA11's pull: `01` (pull-up) in
  `GPIOA->PUPDR` bits 23:22 holds, `10` (pull-down) releases. `GPIOA->IDR` bit
  11 reads the level the pin really has, which every record reports.
- A pending frame is exactly one byte, written while CTS is held: it stays in
  the data register with TXE and TC both clear. A second `uart_poll_out()` would
  spin on TXE, which is a different wait from the one the pull request bounds,
  so the images never write a second byte while CTS is held.
- The release is a `k_timer` expiry function, so it runs in interrupt context
  at its time whether or not the thread that called the suspend is stuck.

## What one repetition does

Each image runs three `cts` control measurements, then 105 repetitions of one
identical sequence: 5 with CTS released before the suspend (the in-run
baseline), then 100 with CTS held.

1. Both timers are stopped and every stamp and flag of the repetition is zeroed.
2. If the device is suspended (it should not be), it is resumed; if an earlier
   rescue cleared CTSE, hardware flow control is switched on again.
3. PA11's pull is set: pull-up for a held repetition, pull-down for a control
   one. After 1 ms the pin level is read (`pin_held`).
4. TC is cleared and one byte (0x55) is written through `uart_poll_out()`, but
   only after a bounded wait saw TXE. 1 ms later `USART_SR` is sampled
   (`sr_pre`, `tc_pre`, `txe_pre`): held, the frame is still in the data
   register; released, it has gone out.
5. The release timer is started for 300 ms and the rescue timer for 1500 ms.
   Two work items are then submitted to the system work queue under
   `k_sched_lock()`, the suspend first and a progress item behind it, and `t0`
   is stamped between the second submit and the unlock.
6. The suspend item stamps `start1`, calls
   `pm_device_action_run(PM_DEVICE_ACTION_SUSPEND)`, stamps `end1`, records the
   return code and the device state, and reads `USART_SR` when the call failed
   (the device is then still active and its clock on). The progress item stamps
   the time it ran. Neither prints.
7. The release interrupt stamps `release`, switches PA11 to pull-down and sets
   `released`.
8. The main thread takes both semaphores (2 s each, `waited` says whether one
   timed out) and then, depending on the state the suspend left behind, either
   waits for the held frame to leave and samples CTS (device still active), or
   samples CTS, resumes the device and only then reads any USART register
   (device suspended, clock off).
9. One more byte (0xAA) is written with CTS asserted and its frame time measured
   (`regular_tc`, `regular_cycles`).
10. The suspend is run once more on the same queue (`start2`, `end2`, `rc2`,
    `state2`) and the device resumed, which re-applies the pin control state and
    with it PA11's pull-up, so the next repetition starts with CTS held.
11. The main thread prints the record. It is the only context that prints, and
    it prints after the repetition is over, so no line can interleave.

## The lines on USART2

Every line is `HQ172 <kind> key=value ...` at 115200 baud, 8N1, and the last
line is `HQ172 done`. `HQ172` is the tag of the validation job that produced
these images, and nothing more. Integers are decimal and never zero-padded,
registers are `0x`-prefixed hexadecimal. Untagged output (the Zephyr boot
banner) carries no meaning here.

`HQ172 build` appears once, before anything else happens:

| Field | Meaning |
|---|---|
| `zephyr` | Zephyr's generated `BUILD_VERSION`, `git describe --abbrev=12 --always` of the tree built. |
| `timeout_us` | `CONFIG_UART_STM32_SUSPEND_TIMEOUT_US`, or 0 where the symbol does not exist: 100000 for head, 0 for base. |
| `limit_cycles` | that limit in cycles of the counter the wait polls, `k_us_to_cyc_ceil32()`: 9 600 000 for head, 0 for base. |
| `rate` | `sys_clock_hw_cycles_per_sec()`, 96 000 000. |
| `ticks_per_sec` | `CONFIG_SYS_CLOCK_TICKS_PER_SEC`, 10 000; the release window below assumes it. |
| `has_busy`, `has_reack` | whether the BUSY and REACK waits are compiled for this SoC; both 0 on the F446. |
| `cr1`, `cr3` | `USART_CR1` and `USART_CR3` after initialisation: TE (bit 3) and UE (bit 13), CTSE (bit 9) and RTSE (bit 8) must be set. |
| `pa11_mode`, `pa11_af`, `pa11_pull` | `GPIOA` MODER, AFRH and PUPDR for PA11: 2 (alternate function), 7 and 1 (pull-up). Together with CTSE and RTSE this is the evidence that the overlay took effect. |
| `n_control`, `n_held`, `release_ms`, `rescue_ms` | the plan this image ran: 5, 100, 300, 1500. |

`HQ172 cts` appears three times, before any power-management action, and is the
proof that the measurement technique works in the same run: `pin_held` (1),
`held_tc` and `held_txe` (0, with `held_sr`), `released_tc` (1, with
`released_sr`), `pin_released` (0) and `release_cycles`, the cycles from the
pull flip to TC, which must be about one frame time. A failure here is a failure
of the measurement, not of the driver.

`HQ172 rep` appears 105 times, `n` running from 0 to 104 (`cts=released` for
`n < 5`, `cts=held` above). Besides the fields named in the sequence above it
carries `ctse`, `sent1` and `sent2` (whether each byte was really written),
`dur1` and `dur2` (the two suspend durations), `sr1` (`USART_SR` right after a
failed suspend, or after the resume when the suspend succeeded),
`resumed1`/`resume1_rc` and `resumed2`/`resume2_rc`, `pending_tc`/`pending`
(the held frame's completion), `progressed`/`progress` (the second work item)
and `rescued`/`rescue`.

Two rules hold for every time field: `t0` is the raw `k_cycle_get_32()` value,
and `start1`, `end1`, `release`, `progress`, `pending`, `start2`, `end2` and
`rescue` are unsigned 32-bit cycle offsets from it, so the counter's wrap every
44.7 s never enters a difference. `dur1` and `dur2` are `end - start` in the
same arithmetic, and `regular_cycles` is the interval from the second write to
TC. Every stamp and flag is zeroed at the start of a repetition, so a stamp of 0
means its event did not happen; in particular `rescue` is 0 whenever `rescued`
is 0.

## What the records were expected to show

The board runs at 96 MHz (HSE 8 MHz bypass from the ST-LINK, PLL M 8, N 384,
P 4, AHB /1), so one cycle is 10.4 ns and one 10-bit frame at 115200 baud is
8333 cycles (87 us).

| Quantity | head, CTS held | base, CTS held | both, CTS released |
|---|---|---|---|
| `rc1` | -16 (`-EBUSY`) | 0 | 0 |
| `dur1` | 9 600 000 to 9 696 000 cycles (the limit, and at most 1 ms more) | about 28 800 000, above that window | at most 960 000 (10 ms) |
| `state1` | 0 (active: the failed action leaves the state alone) | 1 (suspended) | 1 (suspended) |
| `progress` | right after `end1`, and before `release` | right after `end1`, which is after `release` | right after `end1` |
| the held frame | `pending_tc=1`, about one frame after `release` | goes out during the suspend, which is why it ends | already out before the suspend |
| `rc2`, `dur2` | 0, at most 960 000 | the same | the same |

The lower bound on the head's `dur1` is exact rather than approximate because
`WAIT_FOR` polls `k_cycle_get_32()`, the very counter these stamps come from,
and gives up once the elapsed cycles reach `k_us_to_cyc_ceil32(100000)` =
9 600 000. What is added on top is the call and return path around the wait and
the power-management core's bookkeeping, each far below 10 us. The base has no
limit at all, so its suspend can only end when the release interrupt lets the
frame out: about 300 ms, one frame plus the clock-off epilogue after the
release.

Each wait in the image has a limit: 10 ms for the TXE and TC polls, 2 ms for the
window in which a held frame must not complete, 350 ms after `t0` for the frame
the release lets out, and 2 s for each semaphore. The one wait that cannot be
bounded from the thread that performs it is the base tree's spin inside the
driver, which is the subject of the report. The rescue timer at 1500 ms is the
last resort against it: from interrupt context it clears CTSE, so the
transmitter stops asking CTS, finishes its frame, sets TC and lets the spin end.
The repetition then carries `rescued=1`, and the run is judged a failure; 1500 ms
is five times the release offset, far beyond a correct base suspend, so a rescue
can never shorten one. If the rescue cannot free the transmitter either, the
image prints no `HQ172 done` line and the plan's read times out. There is no
path on which a stuck run looks like a passing one.

## Why the configuration is this small

[zephyr/prj.conf](zephyr/prj.conf) holds exactly four assignments:
`CONFIG_PRINTK=y` (the records), `CONFIG_PM_DEVICE=y` (the device-PM action
under test), `CONFIG_LOG=n` so that the driver's `LOG_ERR` on a timeout
compiles to nothing and no untagged text can interleave with a tagged line, and
`CONFIG_MAIN_STACK_SIZE=2048` for the main thread's records and polling loops.
Not set, deliberately: `CONFIG_PM` (it would change `uart_poll_out` and connect
the USART interrupt), `CONFIG_PM_DEVICE_RUNTIME` (its asynchronous path hides
the return code behind an `__ASSERT`, while a direct action from an application
work item makes the code observable) and
`CONFIG_UART_STM32_SUSPEND_TIMEOUT_US` (the head's default is what this report
is about, and the base tree has no such symbol, so assigning it would abort that
build).

`zephyr/` holds nothing besides `CMakeLists.txt`, `app.overlay`, `prj.conf` and
`src/main.c`: Zephyr would load a `Kconfig`, a `boards/*.conf`, a `socs/*.conf`
or a `VERSION` file from an application directory on its own, and nothing is
configured here that is not in those four assignments or in `src/main.c`.

## Results

[Run 38163838076](https://github.com/agentic-hil/agentic-hil/actions/runs/38163838076)
of the hardware-bench workflow on the branch `bench/hilval-hq172`, at commit
`efd4280f`, ran both plans on the Agentic HIL project's NUCLEO-F446RE with
Agentic HIL 0.21.4 and the bench configuration
`sha256:9e0b4615276e15b64107b9a0e09044c5c202265bbe5e337424c9b915ae67652c`, and
each image reached its `HQ172 done` line. Its evidence is in
[results/run-38163838076/](results/run-38163838076/), as the run uploaded it
(artifact `hilval-hq172-evidence`,
`sha256:1a5f30bcd696a286246024a036b5464da232024d6c98415488a6a732d13841ec`),
with the probe's serial number and the machine's names withheld:
`reports/<image>.json` is the plan's report, every step with what Agentic HIL
did and saw, and `evidence/<image>/` holds the run summary and the OpenOCD and
serial logs the report names. `demo` is the demo firmware put back afterwards.
The reports record the SHA-256 of the plan they followed (`test_config_sha256`)
and of the file they flashed; those are the sums of [plans/](plans/) and
[firmware/SHA256SUMS](firmware/SHA256SUMS), so the plans and images in this
directory are byte for byte the ones the run used.

Each serial log holds 110 tagged lines: one `build`, three `cts`, 105 `rep`
with `n` 0 to 104 in order, and `HQ172 done`. The tables below are what
[results/summarize.py](results/summarize.py) prints from those two logs; every
value is in cycles of the 96 MHz counter unless it says otherwise, and a range
is the minimum and maximum over the repetitions of that group.

**The build records:**

| | `head` | `base` |
|---|---|---|
| `zephyr` | `v4.5.0-rc1-2-g90ccb4f9d4ef` | `v4.5.0-rc1-1-g3c61a12bdd8b` |
| `timeout_us`, `limit_cycles` | 100000, 9 600 000 | 0, 0 |
| `rate`, `ticks_per_sec` | 96 000 000, 10 000 | the same |
| `has_busy`, `has_reack` | 0, 0 | 0, 0 |
| `cr1`, `cr3` | 0x200c, 0x300 (TE, UE; CTSE, RTSE) | the same |
| `pa11_mode`, `pa11_af`, `pa11_pull` | 2, 7, 1 | the same |
| `n_control`, `n_held`, `release_ms`, `rescue_ms` | 5, 100, 300, 1500 | the same |

**The controls**, three per image, before any power-management action:
`pin_held` 1, `held_tc` 0 and `held_txe` 0 (`held_sr` 0x0 in the first control
and 0x200, the CTS toggle flag, in the two after it), `released_tc` 1
(`released_sr` 0x2c0), `pin_released` 0, in all six. `release_cycles`, the
pull flip to TC: 8 669, 8 561 and 9 317 in `head`, 8 777, 8 669 and 8 561 in
`base`, each about one frame.

**The repetitions:**

| | `head`, CTS held (100) | `base`, CTS held (100) | `head`, released (5) | `base`, released (5) |
|---|---|---|---|---|
| `rc1` | -16 (`-EBUSY`) in all 100 | 0 in all 100 | 0 | 0 |
| `dur1`, the suspend call | 9 600 406 in all 100 (100.004 ms) | 28 807 705 to 28 817 105 (300.08 to 300.18 ms) | 541 (5.6 us) | 420 (4.4 us) |
| `state1` after the call | 0 (active) in all 100 | 1 (suspended) in all 100 | 1 | 1 |
| `release`, CTS released, from `t0` | 28 799 443 to 28 808 974 (300.0 to 300.1 ms) | 28 799 540 to 28 809 033 | 28 801 355 to 28 805 210 | 28 801 724 to 28 803 905 |
| `end1` relative to `release` | 19.2 million cycles (200 ms) before it | 8 755 to 9 571 after it (one frame) | before it | before it |
| `progress` minus `end1`: the next work item ran | 716 after the suspend returned, in all 100 | 715, in all 100 | 718 | 715 |
| the held frame | `pending_tc` 1; `pending` 8 606 to 10 767 after `release` (one frame plus latency) | went out during the suspend; `pending` is stamped after the resume | sent before the suspend | sent before the suspend |
| `sr1` | 0x200 in all 100: TXE and TC clear, the frame still pending when the call returned | 0x2c0 after the resume: TXE and TC set | 0x2c0 | 0x2c0 |
| `regular_cycles`, the 0xAA byte | 8 522 to 9 386 | 9 153 in all 100 | 8 630 to 9 170 | 8 829 to 9 369 |
| `rc2`, `dur2`, `state2`, the second suspend | 0, 541, 1 in all 100 | 0, 420, 1 in all 100 | 0, 541, 1 | 0, 420, 1 |
| `rescued`, `waited`, `resumed1` | 0, 0, 0 | 0, 0, 1 | 0, 0, 0 | 0, 0, 0 |
| `sr_pre`, `tc_pre`, `txe_pre` (1 ms after the write) | 0x200, 0, 0 | 0x200, 0, 0 | 0x2c0, 1, 1 | 0x2c0, 1, 1 |
| `pin_held`, `pin_released` | 1, 0 | 1, 0 | 0, 0 | 0, 0 |

In words. With CTS held, the head's suspend returned `-EBUSY` after exactly the
configured limit plus 406 cycles (4.2 us) in every one of the 100 repetitions,
left the device active with the frame still pending, and the work queue ran its
next item 7.5 us later, 200 ms before CTS was released. The held frame then went
out one frame after the release, the next byte took one frame, and the second
suspend, made with the line idle, succeeded in 5.6 us. The base's suspend did
not return until the release interrupt let the frame out: it held the system
work queue for 300 ms in every held repetition, ended one frame after the
release, and left the device suspended, so the next item on the queue ran 300 ms
late. No repetition in either image needed the rescue, no semaphore wait timed
out, and the suspend of a released line took 5.6 us (head) or 4.4 us (base) in
every control repetition, in both images alike. Every value is within the
windows of the table above.

### Reproducing the tables

```bash
python3 results/summarize.py \
  head=results/run-38163838076/evidence/head/logs/com-20261011T182655395Z-dut_uart.jsonl \
  base=results/run-38163838076/evidence/base/logs/com-20261011T182733705Z-dut_uart.jsonl
```

The script joins the received bytes of each log, keeps the tagged lines and
prints every field per group as a count per value or as min/median/max, plus
the differences the table uses.

## The test plans

Each plan in [plans/](plans/) flashes its image without a reset, opens the
ST-LINK's virtual COM port with a cleared buffer, resets the board and records
the port until a line is exactly `HQ172 done`, with a 240 s limit, and closes
the port. The plans judge only that the image ran to its last line; every value
above was judged from the recorded lines afterwards. The plans were written for
and checked with Agentic HIL 0.21.4.

## Running the plans

With your own NUCLEO-F446RE and an Agentic HIL configuration that
`agentic-hil init` wrote in [examples/nucleo-f446re_demo](..), binding `dut` to
the ST-LINK and `dut_uart` to its virtual COM port, run from that directory:

```bash
mkdir -p build/hilval-hq172
cp zephyr-121031/firmware/*.elf build/hilval-hq172/
(cd build/hilval-hq172 && sha256sum -c ../../zephyr-121031/firmware/SHA256SUMS)
agentic-hil test-reactor --test-config zephyr-121031/plans/head.testconfig.yaml
agentic-hil test-reactor --test-config zephyr-121031/plans/base.testconfig.yaml
```

The plans flash from `build/hilval-hq172/`, which lies inside the artifact
roots Agentic HIL allows by default. Each image runs for about 40 s. The serial
log of each run is the `com-*-dut_uart.jsonl` its report names, and
`results/summarize.py` reads it as above.

Without Agentic HIL: flash an image with the tool of your choice, open the
virtual COM port at 115200 baud, 8N1, reset the board and read until
`HQ172 done`.

## Building the images

[firmware/](firmware/) holds the two images as built, stripped of debug
information with their symbol tables kept, and their
[SHA-256 sums](firmware/SHA256SUMS). They were built from:

- Zephyr `90ccb4f9d4efc6a2886a8c910f7129856667ae04` (the pull request's head)
  for `head` and `3c61a12bdd8b2975b9180abcb80779ad3724a40d` (its base) for
  `base`, both Zephyr 4.5.0-rc1 plus the commits named above.
- The modules both commits' `west.yml` name: hal_stm32
  `c9550199186236d610092f4005f8c9bae2a6bfcf`, cmsis_6
  `1c1840af7a7e757d6e2fec3ddb0e5ce0dfcc93c8` and cmsis
  `512cc7e895e8491696b61f7ba8066b4a182569b8`.
- GNU Tools for STM32 14.3.rel1 (arm-none-eabi-gcc 14.3.1, from STM32CubeCLT
  1.22.0) as `gnuarmemb`, CMake 4.3.1 and Ninja 1.13.2.

For each image, with `ZEPHYR_BASE` at the matching Zephyr checkout, from this
directory:

```bash
cmake -S zephyr -B build/head -GNinja -DBOARD=nucleo_f446re \
  -DZEPHYR_TOOLCHAIN_VARIANT=gnuarmemb -DGNUARMEMB_TOOLCHAIN_PATH=<toolchain> \
  -DZEPHYR_MODULES="<hal_stm32>;<cmsis_6>;<cmsis>" \
  -DDTC_OVERLAY_FILE=<this directory>/zephyr/app.overlay
cmake --build build/head
arm-none-eabi-objcopy --strip-debug build/head/zephyr/zephyr.elf firmware/head.elf
```

The overlay is the one `west build` would pick up by name from the application
directory; the build passes it explicitly and passes no other extra CMake
argument apart from a `-ffile-prefix-map` flag that rewrites the source paths
embedded in the image, in particular never `-DBUILD_VERSION` (which would let
an image claim any tree) and never `-DCONFIG_UART_STM32_SUSPEND_TIMEOUT_US`.
The session that built the images recorded `git rev-parse HEAD` and
`git status --porcelain` of the two Zephyr trees and the three module
checkouts, and each image's own `zephyr` field agrees with its tree.
Byte-for-byte reproducibility of a rebuild has not been checked.

## How this was made

This report is part of the Agentic HIL project's upstream work, and we maintain
Agentic HIL. AI coding agents, run by our development pipeline, wrote the
validation firmware, the test suite that judges the recorded lines, this README
and the comment for the pull request, and built the images; that suite also
generated the plans. Agentic HIL carried out every step on the board: it
flashed each image, reset the board and recorded the serial port until the
firmware's last line, and no step reached the board any other way. The suite
then asserted every value of the tables above against the recorded lines, and
`results/summarize.py` reproduces the tables from the logs. Our maintainer
approved posting the comment; the check that the run passed and that the rows
above match the logs was made by the agent that operates the pipeline, and no
person has reviewed the firmware, the test suite or the analysis.

## Licensing

The test firmware sources and the plans are Apache-2.0, like this repository.
The images also contain Zephyr, CMSIS and STMicroelectronics code under their
own terms; see [firmware/THIRD_PARTY_NOTICES.md](firmware/THIRD_PARTY_NOTICES.md).
