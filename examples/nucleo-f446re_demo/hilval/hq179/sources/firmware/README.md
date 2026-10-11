# hq179: what Zephyr PR #119508's Pulse IO STM32 timer-DMA backend puts on a pin

Two firmware images for the ST NUCLEO-F446RE that measure, inside the chip
itself, the edges the STM32 Pulse IO backend of
[zephyrproject-rtos/zephyr#119508](https://github.com/zephyrproject-rtos/zephyr/pull/119508)
produces. The pull request adds a vendor-neutral Pulse IO API
(`include/zephyr/drivers/pulse_io.h`) and an STM32 backend
(`drivers/pulse_io/pulse_io_stm32_timer_dma.c`) in which a timer's update event
requests one DMA word per tick, written to a GPIO port's `BSRR`, so the pin
needs no alternate function.

These images ran on the NUCLEO-F446RE through [Agentic Hardware-in-the-Loop (Agentic HIL)](https://github.com/agentic-hil/agentic-hil), which flashed them, reset the board with the serial port open and recorded USART2.

Revisions, all pinned:

| What | Revision |
|---|---|
| Zephyr, PR head | `f7165025ba97992bd7367d6873f986b4cdca407f` |
| Zephyr, PR base | `3e8f38faf93704249745dbc475052848ce39fdaf` |
| hal_stm32 | `c9afc5e80f977e48f9de14c7f6cce1d193ec7181` |
| cmsis_6 | `1c1840af7a7e757d6e2fec3ddb0e5ce0dfcc93c8` |
| cmsis | `512cc7e895e8491696b61f7ba8066b4a182569b8` |

## The two images

The driver is new, so there is no upstream "before" version of it. Both images
are the same Zephyr application and differ only by one patch to that one C file:

* **`before`** carries the pull request's driver file as it is.
* **`after`** carries the same file with `pulse_io_stm32_timer_dma.patch`
  applied.

`prj.conf` sets `CONFIG_PULSE_IO_STM32_TIMER_DMA=n`, which keeps the in-tree
copy of the backend out of the build, and the driver file enters as application
source instead (`-DHQ179_DRIVER_SRC=<copy of the driver>`), bound to the same
devicetree compatible. Nothing else differs between the two images, so every
difference the records show is the patch's.

## What the patch changes, and why

Each change answers one deviation read out of the driver's source. The images
measure; they do not judge. The reading is the reason the case exists, not the
result.

1. **One tick is one whole update period.** `channel_configure()` programmed
   `ARR = tick_freq / resolution_hz` while the update event fires every
   `(PSC+1)·(ARR+1)` timer clocks, as the driver's own comment says, so every
   tick came out one timer clock longer than the stored rate. The patch computes
   the period in clocks and programs `ARR = period - 1`, and stores
   `resolution_hz = tick_freq / period`. The lower clamp becomes "at least two
   clocks", which is the same floor the old `ARR == 0 → 1` gave. One value does
   change outcome: a period of exactly 65536 clocks is rejected today
   (`ARR = 65536`) and accepted after the change (`ARR = 65535`). That is
   inherent to counting the period correctly, and no case of this job is near
   it.
2. **An idle-level helper**, `pulse_io_stm32_set_idle()`, which writes the
   configured resting level to `BSRR`. It is placed just after the driver's
   `pulse_io_stm32_data` definition, because it reads `idle_high` out of it; the
   `dma_callback()` is left exactly as it is.
3. **The idle level at configure time.** `init()` parks the line low and nothing
   wrote the configured level again, so a channel configured with
   `idle_high` rested low until its first frame had finished. The patch writes
   the configured level at the end of `channel_configure()` when no transfer is
   in flight. The API documents `idle_high` as "the level the line rests at
   between and after frames".
4. **A whole last tick.** The last cell's word stayed on the line only until the
   DMA transfer-complete interrupt ran, where the callback stops the timer and
   writes the idle level, so the last tick was as long as that interrupt's
   latency rather than a tick. `encode()` now appends one more word, the idle
   level, which the DMA writes one tick after the last cell word, on a timer
   event; the interrupt then only stops the timer and writes the same level
   again, which is no edge. Room for that word comes from change 6, so the
   capacity check and the documented meaning of `buffer-size` (the words one
   submission may carry) are unchanged.
5. **The idle level after an abort.** Neither the timeout path of
   `transmit_sync()` nor `stop()` wrote the idle level, so the line kept the
   last word that had gone out. Both now write it; in `stop()` before the
   semaphore reset, so no waiter can return while the line is still at the last
   word.
6. **A `buffer-size` of 0 no longer means a zero-length buffer.** The
   devicetree binding gives `buffer-size` a default of 0, so the property always
   has a value and `DT_INST_PROP_OR(inst, buffer_size, 4096)` yields 0, not
   4096: a node that leaves the property out gets a zero-length buffer and every
   request fails with `-ENOMEM`. A `PULSE_IO_STM32_BUFSZ()` macro now selects the
   build-time default when the property is 0, as the binding describes, and
   allocates one word more for change 4's trailing word.

Changes 2 and 5 are one fix; 4 and 6 share that one extra word. The patch
touches no other file: not the binding, not the Kconfig, not the sample.

## What runs on the board

Nothing is wired to the board and no LED is attached, so every stimulus and
every measurement is inside the MCU. Where a bench procedure would ask for a
probe or a jumper, the in-chip equivalent is used, and that is what the numbers
below are: a pin in output mode is sampled back into the port's input data
register every bus clock, and a DMA stream copies that register into RAM. No
external measurement is made or claimed.

* **Board and clocks.** NUCLEO-F446RE, 96 MHz from the ST-LINK's 8 MHz clock in
  bypass (PLL M 8, N 384, P 4), AHB /1, APB1 /2, APB2 /1, so every timer clock
  and the cycle counter run at 96 MHz. Console on USART2 (PA2/PA3) at
  115200 baud, 8N1.
* **The channel under test.** `app.overlay` gives the driver TIM1, DMA2 stream 5
  channel 6 (TIM1_UP, the only free update request that reaches a GPIO port),
  prescaler 0, `buffer-size = <512>` and pin **PC0**. A second instance on
  **PC1** has the same timer and stream and no `buffer-size` at all: that is
  change 6's case. The two are never used at the same time.
* **The measurement.** DMA2 stream 7 channel 7 (TIM8_CH4) copies the 16-bit
  `GPIOC` input data register into a 20000-entry array on every TIM8 compare
  event, one sample every **8 timer clocks** (12 MS/s), so one window covers
  160000 clocks (1.667 ms): the longest frame of this job plus a tail of more
  than 60000 clocks. TIM8 has no devicetree node on the F446 under Zephyr, so it
  is driven through its registers; the stream raises no interrupt, so Zephyr's
  own DMA driver never sees an interrupt for a stream it did not configure.
  TIM8's counter is reinitialised by TIM1's trigger output, which carries the
  driver's counter-enable signal (`MMS = 001` in TIM1, reset mode on internal
  trigger 0 in TIM8), so every frame starts at count 0 of the sampling counter
  and the samples fall `CCR4` clocks after it. That is where the per-repetition
  phase of the grid sits (see the tolerances below). Selecting a trigger output
  changes no count, no update event, no DMA request and nothing on the pin, the
  driver never writes that register, and both images print the two registers
  back in their `init` record.
* **The control.** TIM3 channel 1 drives **PC6**, a pin of the same port, with a
  PWM of 159 timer clocks per period (`ARR = 158`), 40 of them high
  (`CCR1 = 40`). 159 is prime, so the 8-clock samples sweep every phase of it.
  It runs from the start until the done line, and because it is a bit of the
  same port it is captured in the very same samples as the pin under test. Every
  window is qualified by it before anything is said about the driver.
* **DMA2 arbitration.** The measurement yields to the product. The driver fills
  its DMA configuration without a channel priority, and Zephyr's STM32 DMA
  driver maps that to the lowest priority level, so the priority bits in the
  overlay's `dmas` cell never reach the hardware; the overlay keeps the upstream
  value unchanged and this is an observation from the source, not a deviation
  the patch addresses. The sampling stream is programmed at that same lowest
  level and is the higher-numbered of the two, so the driver's stream 5 wins
  every arbitration tie: a driver word waits at most for one sample already in
  flight. Both images read both priority fields back and print them, and the
  tests assert them, because the tolerances below rest on that premise.
* **Pad slew rate.** After the driver's own initialisation the image sets the
  fast slew rate on PC0, PC1 and PC6 and prints the field it set. How fast a pad
  rises is the integrator's setting, not the driver's.

## Two runs per image

Everything runs twice. After the first set of 20 repetitions of all twelve
cases, the image waits for the last console byte to leave the transmitter, arms
a marker in uninitialised RAM and asks for a software reset
(`sys_reboot(SYS_REBOOT_COLD)`, which is `SYSRESETREQ`). The second start runs
exactly the same cases again and only then prints the done line; it never
restarts after that.

State carried across a reset is used **only** after a reset the image caused
itself: the second set runs only when `RCC_CSR` shows the software-reset flag
with no power-on, brown-out or watchdog flag, *and* the marker is armed. The
marker is armed in the last instructions before the reset and invalidated at
every start before anything else runs, so a reset the bench caused can never
find it armed. Every start prints `rcc_csr` and whether the marker was `hit`,
`stale` or `none`.

## The records

Every line is `HQ179 <kind> key=value ...` on USART2; untagged lines do not
exist in these images (the boot banner and the logging subsystem are off). Each
start prints an empty line first, then:

```
HQ179 boot  phase rcc_csr marker zephyr sysclk ticks enomem etimedout enotsup
            einval samples sample_clocks pwm_arr pwm_ccr nrep
HQ179 init  phase ready0 ready1 idr0 idr1 modes min_tick_ns max_ticks loop_max
            channels pc0_mode pc0_ospeed chan_rc tim1_cr2 tim8_smcr
HQ179 case  phase rep name res psc arr rc rc2 rc_cfg ret done_rc idle_cfg
            idle_end idle_stop stop_at s5_pl s7_pl flags ndtr dur nruns first
            first_len last last_len pwm_edges pwm_first pwm_last pwm_hi_min
            pwm_hi_max pwm_lo_min pwm_lo_max pwm_per_min pwm_per_max pc0_set
            pc0_rst
HQ179 case2 the same keys, for the follow-up window of the abort cases
HQ179 runs  phase rep name win part v=<run>,<run>,...
HQ179 done
```

* `rc`, `rc2`, `rc_cfg`, `done_rc` are what the API returned; `0` where the case
  has no such call.
* `ret`, `stop_at` and `dur` are cycle counts taken in the MCU, measured from
  the moment the window's sampling started. No host clock is used anywhere.
* `idle_cfg`, `idle_end` and `idle_stop` are the pin's level read out of the
  input register after configuring, after the transmit returned and right after
  `stop()` returned.
* `psc` and `arr` are read back out of the timer the driver was given.
* `nruns` is the number of runs of the measured bit over the whole window,
  `first`/`last` their levels at the two ends and `first_len`/`last_len` their
  lengths; the `runs` lines carry the lengths of the runs in between, in order,
  in **samples** of 8 clocks each, 16 to a line.
* `flags` and `ndtr` are the sampling stream's own report, and `s5_pl`/`s7_pl`
  the two priority fields.
* `tim1_cr2` and `tim8_smcr` are read back out of the two timers: the trigger
  output the driver's timer carries (`0x10` is its counter-enable signal) and
  the slave mode of the sampling timer (`0x4` is reset on internal trigger 0).
  Together they are what puts every frame at count 0 of the sampling counter.
* A value of `-1` means the case has no such measurement.

## The cases

Twelve cases, 20 repetitions each, in this order, per start:

| Case | Rate | What it does |
|---|---|---|
| `qual` | — | Drives the pin high and low through `BSRR` and reads it back, then records an undriven window: the measurement path proving itself |
| `dur500k` `dur2m` `dur6m` `dur8m` | 0.5, 2, 6, 8 MHz | One frame with duty 0, 1, period-1, period and above period (clamped) |
| `full` | 2 MHz | A frame that fills the buffer exactly, then one cell more |
| `timeout` | 0.5 MHz | A frame aborted by its own 300 µs timeout, then a normal frame |
| `stop` | 0.5 MHz | A frame stopped from another thread, then a normal frame |
| `twice` | 6 MHz | Two frames in one window |
| `node1` | 2 MHz | The second instance, the one with no `buffer-size`, on PC1 |
| `loop` | 2 MHz | A request with `loop_count = 1` |
| `idlehigh` | 2 MHz | A frame with a high resting level, beginning and ending with a duty-0 cell |

The cell period is 64 ticks and a frame is 8 cells, so one frame is 512 words,
exactly the buffer the overlay asks for. The frames are
`32, 1, 63, 0, 56, 8, 80, 64` (low resting level), `64` eight times (the full
buffer) and `0, 32, 1, 63, 56, 8, 80, 0` (high resting level).

`idlehigh` runs last, because before the fix it leaves the line high until a
frame has finished; it first sends a short frame whose own resting level is low,
so both images start it from a line that rests low.

In `stop`, a second thread of lower priority waits 300 µs from the moment the
main thread blocks inside the transmit, then stamps the time, calls `stop()` and
reads the pin, all with the scheduler locked, so the level it reports is the one
`stop()` alone left on the line and not what the transmit's own cleanup did
afterwards.

## The numbers to expect

One tick is `96000000 / rate` timer clocks: **192, 48, 16 and 12** clocks at
0.5, 2, 6 and 8 MHz. Each of those rates divides 96 MHz exactly, so the
"nearest achievable" rate the API promises is the requested rate and the
expectation needs nothing the driver reports.

| | `before` | `after` |
|---|---|---|
| `arr` at 0.5 / 2 / 6 / 8 MHz | 192 / 48 / 16 / 12 | 191 / 47 / 15 / 11 |
| a run of *d* ticks | *d* × (tick + 1) clocks | *d* × tick clocks |
| a frame's last run of *n* ticks | (*n* − 1) × (tick + 1) + the interrupt's latency | *n* × tick clocks |
| the resting level after an abort | the last word that went out | the configured idle level |
| a channel configured idle-high, before its first frame | low | high |
| a request on the instance without `buffer-size` | `-ENOMEM` | accepted |

Two resolutions are stated, because they answer different questions:

* **±16 clocks for a single repetition**: one sample of quantisation (8 clocks)
  plus one sample for the arbitration delay of an edge. At 6 and 8 MHz one tick
  (16 or 12 clocks) is inside that, so a single repetition cannot resolve a
  one-tick error there.
* **±4 clocks for the mean of the 20 repetitions**: every window measures the
  frame at its own phase of the 8-clock sampling grid, so the quantisation of a
  run averages out over the repetitions. That is what resolves a
  one-clock-per-tick error at every rate measured. The phase is the compare
  value the samples are taken at, `1 + rep % 7` clocks after the sampling
  counter is reinitialised, and the driver's own counter enable reinitialises it
  at the start of every frame. Seven values cover seven of the eight phases,
  three repetitions each and two for the seventh, which leaves at most 1.5
  clocks of quantisation in the mean.

  The phase has to be tied to the frame by the hardware, because a shift
  applied before the frame does not survive. Two earlier versions of this image
  shifted the sampling counter before it started, and then its compare value
  plus a wait on that counter, and both measured every run of a frame as the
  same number of samples in all 20 repetitions of both reset phases - a grid
  that never moves relative to the frame. The sampling stream writes into the
  same SRAM that the driver's encoder writes its 512 words into, and a loop
  that is held up by one clock whenever the two coincide settles at a fixed
  position in the grid and takes the frame start with it. The counter is still
  preset to `2 * rep % 8` before it starts: that shift moves the whole grid
  rather than its offset from a frame, so it cannot cancel the compare value,
  and in the qualification window, where no frame runs and the counter is never
  reinitialised, it is the only phase there is.

For `before`, the shortfall of the last run is reported as
`measured − (n − 1) × (tick + 1)`, which is the transfer-complete interrupt's
latency. At 0.5 MHz one tick is 192 clocks, which that latency could plausibly
equal, so the last cell is reported there but the deviation is only asserted at
2, 6 and 8 MHz, where one tick is 48 clocks or less.

A measurement that lost samples shows up in the sampling stream's own flags (a
FIFO-error overrun in direct mode), in a non-zero `ndtr`, or in the control's
high, low and period lengths and the span its rising edges cover. All of those
are the measurement path's failure, not the driver's, and the tests say so. A
console line that never reached the reader is the same kind of failure: the
images number every repetition and the tests require all 20 of them, so a
reading that lost one is refused instead of being judged as if it were the whole
set. The images themselves cannot lose a line — `printk` here waits for the
transmitter on every character — but the link to the reader can, and in two
earlier runs of these images it did: a few kilobytes vanished in the middle of a
case while the port was carrying its ~300 kB with no gap in it, once leaving a
line cut in two and once whole repetitions missing between two intact lines.
Every record line is therefore followed by 75 ms of silence (`PACE_MS` in
`src/main.c`), which holds the port at about a quarter of its capacity: a reader
that stalls for a whole second then has under 3 kB to catch up with instead of
11 kB. The images still print their done line after about 110 seconds, and no
measurement waits for those gaps, because no sampling window is armed while a
record is printed.

## Coverage

These results hold for the **STM32F446RE at 96 MHz**, for the **four measured
rates** (500 kHz, 2 MHz, 6 MHz and 8 MHz), as **timing relative to the MCU's own
clock**. Nothing here says anything about LEDs or LED strips, about absolute
frequency, about the API's 10 MHz capability floor, or about any rate not
measured. No WS2812 or other device is attached.

## Running them again without our tools

With a Zephyr workspace whose `zephyr` tree is at the PR head above and whose
`hal_stm32` module is at the revision above:

```
west build -b nucleo_f446re firmware/hq179/zephyr -- \
    -DDTC_OVERLAY_FILE=<absolute path to firmware/hq179/zephyr/app.overlay> \
    -DHQ179_DRIVER_SRC=<a copy of drivers/pulse_io/pulse_io_stm32_timer_dma.c>
west flash
```

Use the driver file as it is for `before` and with
`pulse_io_stm32_timer_dma.patch` applied for `after`:

```
git apply --whitespace=nowarn pulse_io_stm32_timer_dma.patch
```

Then read the ST-LINK's virtual serial port at 115200 baud, 8N1, until the
`HQ179 done` line. The board resets itself once in the middle, so keep the port
open from the first reset to that line.

## Files

| File | What it is |
|---|---|
| `pulse_io_stm32_timer_dma.patch` | the one patch, a plain `git diff` of the one driver file |
| `zephyr/CMakeLists.txt` | the application: `src/main.c`, `src/sampler.c` and the driver copy named on the command line |
| `zephyr/prj.conf` | the Kconfig symbols, including `CONFIG_PULSE_IO_STM32_TIMER_DMA=n` |
| `zephyr/app.overlay` | the timer, DMA stream, pins and buffer size the driver is given |
| `zephyr/src/main.c` | the cases, the two runs and the printing |
| `zephyr/src/sampler.c`, `zephyr/src/sampler.h` | the measurement path and the control PWM |

## Licences

The driver under test, the Pulse IO API and everything else from Zephyr are
Apache-2.0 (`reference/hq179/zephyr/LICENSE`); each file keeps its own header.
The STM32 low-level drivers reached through `hal_stm32` are BSD-3-Clause and
CMSIS is Apache-2.0. The application sources here carry Apache-2.0 SPDX
headers.

## Assumptions

Stated because they are not verified inside this repository, and each one is
printed or asserted where these images depend on it:

* The Zephyr tree is a git checkout at the PR head, so `BUILD_VERSION` ends in
  `gf7165025ba97`; each start prints it.
* The DMA2 request mapping of RM0390: TIM1_UP on stream 5 channel 6 and
  TIM8_CH4/TRIG/COM on stream 7 channel 7; a pin in output mode is sampled into
  the input data register every bus clock; streams arbitrate by priority level
  and then by the lower stream number.
* Zephyr's STM32 DMA driver maps a channel priority of 0 to the lowest level and
  configures only the streams it is asked to; `k_sem_reset()` wakes a waiter
  with `-EAGAIN`; the default tick rate is the 10000 `prj.conf` states; a
  software reset also drives NRST on this part, so the pin-reset flag says
  nothing about who asked for the reset.
* The transfer-complete interrupt takes more than about 100 clocks to reach the
  driver's callback, which is what makes the last-cell deviation separable from
  the one-clock-per-tick deviation at 2 MHz and above.

## Nothing is submitted upstream

Nothing from this job is posted to the pull request or anywhere else upstream.
Zephyr's contribution rules (a `Signed-off-by:` for the responsible human, one
`Assisted-by:` trailer where a tool helped, and human review and testing of
every change) apply to any human who later chooses to submit this patch.
