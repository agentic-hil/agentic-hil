# hq172: STM32 UART device-PM suspend while CTS holds the transmitter (NUCLEO-F446RE)

A hardware report on an existing upstream change, not a competing fix:
[zephyrproject-rtos/zephyr#121031](https://github.com/zephyrproject-rtos/zephyr/pull/121031).
That pull request bounds the idle waits in `uart_stm32_suspend_setup()` with
`WAIT_FOR(..., CONFIG_UART_STM32_SUSPEND_TIMEOUT_US, NULL)` (a new Kconfig int,
default 100000 us, `depends on PM_DEVICE`), logs an error and returns `-EBUSY`;
`uart_stm32_pm_action()` returns that error before the peripheral clock is
switched off. Without it the wait spins without a limit and blocks whichever
thread or work queue runs the suspend.

Two images, built from the one application in `zephyr/`:

| Image | Zephyr tree | Revision |
|---|---|---|
| `head` | the pull request's head | `90ccb4f9d4efc6a2886a8c910f7129856667ae04` |
| `base` | the commit it is based on | `3c61a12bdd8b2975b9180abcb80779ad3724a40d` |

Both are built against hal_stm32 `c9550199186236d610092f4005f8c9bae2a6bfcf`,
hal/cmsis_6 `1c1840af7a7e757d6e2fec3ddb0e5ce0dfcc93c8` and hal/cmsis
`512cc7e895e8491696b61f7ba8066b4a182569b8`. The application source, the
devicetree overlay and `prj.conf` are the same for both, so the Zephyr tree is
the only difference and any difference in behaviour is the pull request's.

The images print raw measurements and never a verdict. The verdicts are computed
by the gate in `tests/hq172/` of the hil-validations repository, from the lines
in the serial log.

The only path from this source to the board is
[Agentic Hardware-in-the-Loop (Agentic HIL)](https://github.com/agentic-hil/agentic-hil):
it flashes each image, resets the board with the serial port open and records
what the image prints, and that is how the images published beside this file ran.

## Coverage, and what is not claimed

The F4 USART has no `USART_ISR_BUSY` and no `USART_ISR_REACK` flag, so of the
three waits the pull request bounds only the transmission-complete (TC) wait is
compiled for this SoC and only that wait is exercised here; the BUSY and REACK
waits are not. The build record prints `has_busy=0 has_reack=0` as the
compiled-in proof of this.

CTS is driven by the pin's own internal pull resistor. Nothing is connected to
the board and nothing is measured outside the chip: no modem, no external
flow-control partner and no logic analyser is involved, and no claim is made
about any of them. Time is measured in the MCU with `k_cycle_get_32()`, never on
a host.

## The setup, all inside the chip

* Test UART: `usart1` with `hw-flow-control`, TX on PB6, RX on PB7, CTS on PA11
  and RTS on PA12 (`usart1_cts_pa11`, `usart1_rts_pa12` of the F446 pin control
  file in hal_stm32), all alternate function 7. The console stays on `usart2`
  (PA2/PA3), which is what the ST-LINK's virtual COM port reads.
* PA11 and PA12 reach only the morpho header of the board; no other function on
  the board drives them.
* CTS is active low and the pin control node gives PA11 a pull-up, so an
  unconnected CTS reads deasserted and the USART holds the transmitter. The
  in-chip equivalent of a jumper is PA11's pull: `01` (pull-up) in
  `GPIOA->PUPDR` bits 23:22 holds, `10` (pull-down) releases. `GPIOA->IDR` bit
  11 reads the level the pin really has, which every record reports.
* A pending frame is exactly one byte, written while CTS is held: it stays in
  the data register with TXE and TC both clear. A second `uart_poll_out()` would
  spin on TXE, which is a different wait from the one the pull request bounds,
  so the images never write a second byte while CTS is held.
* The release is a `k_timer` expiry function, so it runs in interrupt context at
  its time whether or not the thread that called the suspend is stuck.

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
5. The release timer is started for 300 ms and the rescue timer for 1500 ms. Two
   work items are then submitted to the system work queue under
   `k_sched_lock()`, the suspend first and a progress item behind it, and `t0` is
   stamped between the second submit and the unlock.
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
11. The main thread prints the record. It is the only context that prints, and it
    prints after the repetition is over, so no line can interleave.

## The lines on USART2

Every line is `HQ172 <kind> key=value ...` at 115200 baud, 8N1, and the last line
is `HQ172 done`. Integers are decimal and never zero-padded, registers are
`0x`-prefixed hexadecimal. Untagged output (the Zephyr boot banner, for example)
carries no meaning here.

`HQ172 build` appears once, before anything else happens:

| Field | Meaning |
|---|---|
| `zephyr` | Zephyr's generated `BUILD_VERSION`, `git describe --abbrev=12 --always` of the tree built, or `none` when it is empty. Expected to end in `g90ccb4f9d4ef` (head) or `g3c61a12bdd8b` (base). |
| `timeout_us` | `CONFIG_UART_STM32_SUSPEND_TIMEOUT_US`, or 0 where the symbol does not exist: 100000 for head, 0 for base. |
| `limit_cycles` | that limit in cycles of the counter the wait polls, `k_us_to_cyc_ceil32()`: 9 600 000 for head, 0 for base. |
| `rate` | `sys_clock_hw_cycles_per_sec()`, expected 96 000 000. |
| `ticks_per_sec` | `CONFIG_SYS_CLOCK_TICKS_PER_SEC`, expected 10 000; the release window below assumes it. |
| `has_busy`, `has_reack` | whether the BUSY and REACK waits are compiled for this SoC; both 0 on the F446. |
| `cr1`, `cr3` | `USART_CR1` and `USART_CR3` after initialisation: TE (bit 3) and UE (bit 13), CTSE (bit 9) and RTSE (bit 8) must be set. |
| `pa11_mode`, `pa11_af`, `pa11_pull` | `GPIOA` MODER, AFRH and PUPDR for PA11: 2 (alternate function), 7 and 1 (pull-up). Together with CTSE and RTSE this is the evidence that the overlay took effect. |
| `n_control`, `n_held`, `release_ms`, `rescue_ms` | the plan this image ran: 5, 100, 300, 1500. |

`HQ172 cts` appears three times, before any power-management action, and is the
proof that the measurement technique works in the same run: `pin_held` (1),
`held_tc` and `held_txe` (0, with `held_sr`), `released_tc` (1, with
`released_sr`), `pin_released` (0) and `release_cycles`, the cycles from the pull
flip to TC, which must be about one frame time. A failure here is a failure of
the measurement, not of the driver.

`HQ172 rep` appears 105 times, `n` running from 0 to 104 (`cts=released` for
`n < 5`, `cts=held` above). Besides the fields named in the sequence above it
carries `ctse`, `sent1` and `sent2` (whether each byte was really written),
`dur1` and `dur2` (the two suspend durations), `resumed1`/`resume1_rc` and
`resumed2`/`resume2_rc`, `pending_tc`/`pending` (the held frame's completion),
`progressed`/`progress` (the second work item) and `rescued`/`rescue`.

Two rules hold for every time field: `t0` is the raw `k_cycle_get_32()` value,
and `start1`, `end1`, `release`, `progress`, `pending`, `start2`, `end2` and
`rescue` are unsigned 32-bit cycle offsets from it, so the counter's wrap every
44.7 s never enters a difference (an event before `t0` would print as a value
near 2^32). `dur1` and `dur2` are `end - start` in the same arithmetic, and
`regular_cycles` is the interval from the second write to TC. Every stamp and
flag is zeroed at the start of a repetition, so a stamp of 0 means its event did
not happen; in particular `rescue` is 0 whenever `rescued` is 0.

## The numbers these records are expected to show, and where they come from

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
the power-management core's bookkeeping, each far below 10 us; interrupts that
run during the wait do not add to it, because they are inside the window the
wait itself measures. The base has no limit at all, so its suspend can only end
when the release interrupt lets the frame out: about 300 ms, one frame plus the
clock-off epilogue after the release.

The release lands within about one tick (100 us at a 10 kHz tick) plus interrupt
latency of 300 ms after `t0`, because the timer is started a few microseconds
before `t0` is stamped and a relative `k_timer` fires on the first tick boundary
at or after its duration.

## Every wait is bounded, and what happens if one is not

Each wait in the image has a limit: 10 ms for the TXE and TC polls, 2 ms for the
window in which a held frame must not complete, 350 ms after `t0` for the frame
the release lets out, and 2 s for each semaphore. The one wait that cannot be
bounded from the thread that performs it is the base tree's spin inside the
driver, which is the subject of the report. The rescue timer at 1500 ms is the
last resort against it: from interrupt context it clears CTSE, so the
transmitter stops asking CTS, finishes its frame, sets TC and lets the spin end.
The repetition then carries `rescued=1` and the gate rejects it; 1500 ms is five
times the release offset, far beyond a correct base suspend, so a rescue can
never shorten one. If the rescue cannot free the transmitter either, the image
prints no `HQ172 done` line and the gate fails the image as a whole. There is no
path on which a stuck run looks like a passing one.

## Why the configuration is this small

`zephyr/prj.conf` holds exactly four assignments: `CONFIG_PRINTK=y` (the
records), `CONFIG_PM_DEVICE=y` (the device-PM action under test),
`CONFIG_LOG=n` so that the driver's `LOG_ERR` on a timeout compiles to nothing
and no untagged text can interleave with a tagged line, and
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

## Running the images without our tools

For each revision, with a Zephyr workspace whose `zephyr` tree is at that commit
and whose modules are at the revisions named above:

```
west build -b nucleo_f446re -d build/head <this directory>/zephyr
west flash -d build/head
```

`app.overlay` is picked up by name from the application directory; our build
passes it explicitly as `-DDTC_OVERLAY_FILE=<path>/app.overlay` and passes no
other extra CMake argument, in particular never `-DBUILD_VERSION` (which would
let an image claim any tree) and never
`-DCONFIG_UART_STM32_SUSPEND_TIMEOUT_US`.

Then read the ST-LINK's virtual COM port at 115200 baud, 8N1 until
`HQ172 done`, and check the first tagged line's `zephyr` field against the
revision checked out. The whole run is under a minute per image.

## How each image's tree is established

The image's own `zephyr` field is a consistency check, not the proof: the gate
freezes both revisions as its own constants, replaces the build helper with a
stand-in and asserts that the build passes exactly those two tree names, this
application directory and the single overlay argument, and reads
`git rev-parse HEAD` and `git status --porcelain` in the workspace's two trees
and three module checkouts in the session that built the images. It also
compares the build script and the three configuration files with texts it owns
before the build, and checks that the application directory held nothing else
and did not change while the images were built. The one thing this does not
exclude is a workspace changed between the build and that check within the same
session.

## Assumptions

Stated because they are not verifiable from this directory:

* The workspace trees are git checkouts, which is what makes Zephyr's
  `BUILD_VERSION` the `git describe` of the tree built.
* `CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC` is 96 000 000 and `k_cycle_get_32()`
  counts those cycles monotonically; `CONFIG_SYS_CLOCK_TICKS_PER_SEC` is 10 000.
  The build record prints both so that a drift fails loudly instead of quietly
  moving a window.
* `WAIT_FOR` converts its timeout once with `k_us_to_cyc_ceil32()`, stamps
  `k_cycle_get_32()` and polls until the expression holds or the elapsed cycles
  reach that budget. Only the "plus a small overhead" reasoning above depends on
  this; the bounds do not.
* Zephyr's generated `version.h` defines `BUILD_VERSION` as an unquoted token,
  which `STRINGIFY()` turns into a string, and leaves it empty for a tree that
  is not a git checkout (the image then prints `zephyr=none`).
* With CTSE set and nCTS deasserted the F446 USART keeps a byte written to the
  data register there, with TXE and TC clear, and sends it when nCTS falls
  (RM0390); in alternate-function mode `GPIOA->IDR` still reads the pin level.
  The three `cts` records measure exactly this in every run, before any
  power-management action, so a board that behaves otherwise fails the
  measurement's own control first.
