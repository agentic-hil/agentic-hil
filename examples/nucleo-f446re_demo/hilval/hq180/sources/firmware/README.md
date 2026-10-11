# hq180: `Serial.write()` and `Serial.flush()` with interrupts masked or from an ISR

Two STM32duino images for a NUCLEO-F446RE, for upstream issue
[stm32duino/Arduino_Core_STM32 #1834](https://github.com/stm32duino/Arduino_Core_STM32/issues/1834):
`Serial.write()` and `Serial.flush()` loop forever when they are called from an
interrupt handler, or with interrupts disabled, while a transmission is in
progress. They ran on the board through
[Agentic Hardware-in-the-Loop (Agentic HIL)](https://github.com/agentic-hil/agentic-hil),
which flashed each image, reset the board with the serial port open and recorded
USART2.

* `before` is core revision `53560ef` (with `ArduinoCore-API` at `bce6000` and
  CMSIS 6.3.0), unmodified.
* `after` is the same sketch, `stm32duino/sketch.cpp`, built against a copy of
  that core with `serial-poll-when-irq-blocked.patch` in this directory applied.
  Nothing else differs between the two images.

The board runs the variant's own clock setup: HSI 16 MHz through the PLL to
180 MHz, AHB /1, APB1 /4 (45 MHz, timer clock 90 MHz), APB2 /2 (90 MHz).
`Serial` is USART2 on PA2/PA3 at 115200 8N1, which is the port the ST-LINK
exposes, so the port under test is also the port the measurements are read
from. Each image prints the clock configuration it measured with.

## Why the calls never return

Only the USART2 interrupt handler moves `tx_tail`, the read index of the
64-byte transmit ring, and it runs at NVIC preemption priority 1
(`UART_IRQ_PRIO`). `write()` with a full ring spins in
`while (!availableForWrite())` and `flush()` in `while (tx_head != tx_tail)`,
and only that handler ends either loop. `flush(timeout)` measures its timeout
with `HAL_GetTick()`, which the SysTick exception advances at priority 0.

So, from the contexts this image measures in:

| context | UART interrupt (1) | SysTick (0) |
|---|---|---|
| thread mode, interrupts enabled | runs | runs |
| thread mode, `PRIMASK` set | blocked | blocked |
| a TIM3 ISR at preemption priority 1 | blocked (equal priority does not preempt) | runs |
| a TIM3 ISR at preemption priority 0 | blocked | blocked |

`write()` with a full ring and `flush()` therefore cannot return from any of the
three masked contexts, and `flush(timeout)` cannot even time out where SysTick
is blocked as well. Where it does time out it sets `tx_head = tx_tail` while a
hardware transfer is still in flight; when that transfer completes, the
transfer-complete handler moves `tx_tail` past `tx_head`, finds the ring
apparently almost full, and sends 64 stale ring bytes again.

## The 17 cases

Each case runs in a boot of its own, 20 repetitions each, and ends with a reset
the image causes itself, so that one hang cannot disturb the next case. `write`
queues 63 bytes and then measures `write()` of one more byte, which is the call
that meets a full ring; the other steps queue 40 bytes and measure the flush.

| case | context | measured call | expected on `before` |
|---|---|---|---|
| `ctl-write`, `ctl-flush` | thread mode, interrupts enabled | `write()`, `flush()` | returns (control) |
| `w-a`, `w-b`, `w-c` | `PRIMASK` set / ISR at 1 / ISR at 0 | `write()` with a full ring | hangs |
| `f-a`, `f-b`, `f-c` | the same three | `flush()` | hangs |
| `t-a`, `t-c` | `PRIMASK` set / ISR at 0 | `flush(20)` | hangs: `HAL_GetTick()` stands still |
| `t-b` | ISR at priority 1 | `flush(20)` | returns after 20 ms, then re-sends 24 stale bytes |
| `s-a`, `s-c` | `PRIMASK` set / ISR at 0 | `flush(1)` | hangs |
| `s-b`, `s-n` | ISR at priority 1 / thread mode | `flush(1)` | returns, then re-sends 24 stale bytes |
| `ctl-write-2`, `ctl-flush-2` | thread mode, interrupts enabled | `write()`, `flush()` | returns, after the whole chain of resets |

The timer for the ISR contexts is TIM3: it has an interrupt line of its own and
is neither `TIMER_TONE` (TIM6) nor `TIMER_SERVO` (TIM7) of this variant. The
library's default interrupt priority is 14, which the UART interrupt would
preempt, so the image sets preemption priority 1 and 0 explicitly.

Everything is done inside the chip. Where the issue text would suggest an
external measurement, the in-chip equivalent is used and nothing is claimed
beyond it: the data bytes are checked against the bench's own capture of USART2
instead of against a second wire, a HardwareTimer interrupt stands in for a
user interrupt, and time is measured with the Cortex-M4 DWT cycle counter
rather than from host timestamps. No debugger reads the program counter at a
hang; the breadcrumbs the image stores in `.noinit` RAM before each call and
prints after the watchdog reset take its place.

## What the images print

Every line that starts with `H180 ` is a record, `H180 <kind> key=value ...`;
every other line is data a case sent. Each start prints one empty line first,
and the last record of a run is `H180 done`.

| kind | what it says |
|---|---|
| `boot` | `boot` number, `resumed`, the `rcc_csr` reset flags as read before they were cleared, the case and repetition this start continues from, `sysclk`/`hclk`/`pclk1`/`pclk2`/`cfgr`/`brr`, the NVIC `grouping` and the priorities of the UART (`prio_uart`), SysTick (`prio_tick`) and TIM3, `timclk`, the watchdog's `iwdg_pr` and `iwdg_rlr`, whether the DWT counter runs, and the `ring` size |
| `wdgarm`, `wdgctl` | the watchdog control: `wdgarm` before a spin that feeds no watchdog, `wdgctl` after the reset, with the `cycles` the spin lasted |
| `pre` | the case, repetition, name, context, step, payload size `n` and timeout `tmo` of the repetition that follows; its own last 24 bytes are what a stale re-send of the ring shows |
| `post` | the call returned: `cycles` from the DWT reads around it, the bytes `acc1` and `acc2` the two writes accepted, the ring index `hp` the payload starts at, `head0`/`tail0`/`size0`/`sr0`/`cr1_0` before and `head1`/`tail1`/`size1`/`sr1`/`cr1_1` after, `pm0`/`bp0` and `pm1`/`bp1` for PRIMASK and BASEPRI, `en0`/`en1` for the USART2 NVIC enable bit, and `ipsr`/`prio_tim` for the context |
| `wdg` | the call never returned: the same breadcrumbs, as stored before the call, printed after the watchdog reset, with the `boot` the call was made in |

Data lines start with `d ` and never with the tag. Each is
`d c=<case> r=<rep> k=<half> ` (16 bytes) followed by letters of
`abcdefghijklmnopqrstuvwxyz` at index `(5*case + 3*rep + 7*half + i) % 26` and
then CR LF, 32 bytes per line for a `write` case and 20 otherwise.

### Recomputing a verdict by hand

* One frame is 10 bits. `BRR` holds `USARTDIV` as mantissa times 16 plus
  fraction, so with oversampling by 16 a frame is
  `10 * ((BRR >> 4) * 16 + (BRR & 0xF))` `PCLK1` periods, which at the clocks
  above is `10 * 391 * 180/45 = 15640` CPU cycles, or 86.9 us.
* A `write()` that meets a full ring has to wait for the first hardware chunk,
  `size0` bytes, so it may take up to `(size0 + 1)` frames; a `flush()` has to
  wait for all `n` queued bytes, so up to `(n + 1)` frames. The extra frame is
  the transfer-complete event after the last byte left the data register.
* A `flush(timeout)` that fires may send between 1 and 14 of the 40 bytes: 1 ms
  is 11 frames, plus the two-byte pipeline of data and shift register.
  `(tail1 - hp) mod 64` is how many bytes it reported as sent, and the capture
  between the `pre` and `post` lines must be exactly those bytes.
* On the unmodified core a `flush(timeout)` that fires sends, after whatever
  chunks completed before it, the whole 64-byte ring from `tail1` on: the 40
  payload bytes and then the last 24 bytes of the `pre` line again.
* The watchdog is armed for 250 ms nominal (prescaler /4, reload 1999, so 8000
  LSI cycles); with the datasheet's LSI tolerance that is 170 to 471 ms, and
  `wdgctl`'s `cycles` divided by `sysclk` says what it really was.

## The patch

`serial-poll-when-irq-blocked.patch` is plain `git diff` output against core
revision `53560ef` and touches three files and nothing else:

* `libraries/SrcWrapper/src/stm32/uart.c` and
  `libraries/SrcWrapper/inc/uart.h` gain four functions: `uart_irq_blocked()`
  decides, from `PRIMASK`, `FAULTMASK`, `BASEPRI`, `IPSR` and the NVIC, whether
  the UART interrupt can run in the caller's context at all;
  `uart_poll_irq()` runs `HAL_UART_IRQHandler()` by hand where it cannot;
  `uart_abort_tx()` stops a transfer and reports how many of its bytes already
  reached the data register, which sends them whatever happens next; and
  `uart_tick_elapsed()` counts SysTick reloads, which happen whether or not the
  SysTick exception can run.
* `cores/arduino/Serial.cpp` uses them. Where the wait loops of `write()` and
  `flush()` would never end, they now run the handler themselves, the way the
  AVR core polls `UDRE` when interrupts are off -- the precedent the issue
  cites. When a `flush(timeout)` fires it stops the transfer, moves `tx_tail`
  past the bytes the hardware already has and only then sets
  `tx_head = tx_tail`, so no later transfer-complete event can carry the tail
  beyond the head and send the ring a second time.

Where the interrupt can run, both wait loops are the upstream loops unchanged;
only the timeout branch of `flush(timeout)` differs.

What the patch does not claim:

* The HAL v2 branch of `uart_abort_tx()` is not verified. No HAL v2 part was
  available, so on those series it reports that nothing left the buffer, which
  leaves a flush timeout behaving exactly as it does today.
* A `write()` or `flush()` from inside the UART's *own* handler would re-enter
  `HAL_UART_IRQHandler()`. That is not tested and not fixed.
* Where the SysTick counter is stopped altogether, a blocked `flush(timeout)`
  does not time out; it returns once the queued bytes have been sent, which is
  bounded by the baud rate but is not the timeout the caller asked for.
* Contexts masked with `BASEPRI`, and interrupt handlers at the library's
  default priorities, are handled by the same code but were not measured.

These results hold for the NUCLEO-F446RE's USART2 (`Serial`, PA2/PA3) at 115200
baud in the contexts listed above and make no general claim about interrupt
safety.

## Building and rerunning

`hilval/hq180/build.py` builds both images: `before` straight from the cached
checkout `stm32duino-53560ef`, `after` from a patched copy of it, both with the
same sketch directory and the core's `IWatchdog` library. The sketch is plain
C++ that includes `<Arduino.h>`; there is no `.ino` file and no
`hal_conf_extra.h`, because the HAL's IWDG and TIM modules are enabled by
default on this series. `tests/hq180/test_hq180.py` is the test that judges a
run: it recomputes every bound from the values the images print and compares
every byte against the serial capture.

AStyle formatting of the patch, and anything posted upstream, is a human
submitter's step. No agent opened an issue, comment or pull request for this
work; what is published here is the firmware, the build, the test and the
evidence, for human review and an independent rerun.
