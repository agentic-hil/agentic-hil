# hq176: how long does `UART.sendbreak()` hold the bus low on an STM32F4?

MicroPython issues [#8794](https://github.com/micropython/micropython/issues/8794)
and [#8646](https://github.com/micropython/micropython/issues/8646) report that
the UART break of the stm32 port is shorter than the documents claim, and that
the two documents disagree with each other: `docs/library/pyb.UART.rst` says the
break drives the bus low "for a duration of 13 bits", while
`docs/library/machine.UART.rst` says it is low "longer than required for a
normal transmission of a character". The original report was made on an STM32F405;
this job measures an STM32F446.

This directory holds everything that was put on the board, plus the change the
measurement supports. **The decision is a documentation change, not an
implementation change.**

## The two images

`baseline` is MicroPython at commit
23f0e61b47f0adf9a8f1259ec603892f27a69eb7 unchanged, the reproduction; `patched`
is the same commit with `uart-sendbreak-docs.patch` applied; both carry the same
frozen `main.py`, so their binaries differ at most in the version string.

They ran on the board through
[Agentic Hardware-in-the-Loop (Agentic HIL)](https://github.com/agentic-hil/agentic-hil),
which flashed each image, reset the board and recorded USART2.

The board is a NUCLEO-F446RE: an STM32F446RE running at 168 MHz from the 8 MHz
HSE bypass of its ST-LINK, with APB1 at 42 MHz and APB2 at 84 MHz. USART2
(PA2/PA3, 115200 baud 8N1) is the REPL and the only line the recording reads.

Because the patch changes two `.rst` files and no code, the two images are the
same firmware. A documentation change therefore cannot be seen in a binary, and
the evidence that the patched image really carries the patch is of two other
kinds: the build applies this directory's one `*.patch` file to the copy it then
builds the `patched` image from, and that image's version string carries no
commit hash, because the patched copy is a checkout without `.git` and
`makeversionhdr.py` falls back to the version in `py/mpconfig.h`. The
unmodified `baseline` image carries the abbreviated hash of the commit above.

## How the break was measured, without touching the board

Nothing was wired to the board's headers: where the reports imply a logic
analyser or an oscilloscope on the TX pin, this job samples the TX pin's own
GPIO input data register by DMA on a timer at 32 samples per bit, inside the
chip, and no external measurement is claimed.

In detail, as the frozen `main.py` does it:

* The line under test is a UART that is not the REPL's. UART4 with TX on PA0 is
  the first choice and UART3 with TX on PB10 the fallback; both hang on APB1,
  which is what lets them reach 1200 baud at this board's clocks (on APB2 at
  84 MHz the 12-bit BRR mantissa stops at about 1282 baud). The `boot` record
  names the UART, the GPIO port and the pin that were used.
* An STM32F4 GPIO updates its input data register every AHB cycle whatever the
  pin's mode, so the register shows what the UART drives out of the pin.
* TIM8 raises an update event every `period` cycles and DMA2 stream 1, channel 7
  (TIM8_UP) copies the 16 low bits of that GPIO input data register into a
  4096-entry buffer, once per update. 4096 samples at 32 samples per bit are 128
  bit times at every baud, more than twice what a repetition needs.
* TIM8 and DMA2 stream 1 were chosen because the port leaves them alone on this
  board: the port's timer code claims TIM3 (LED PWM), TIM5 (servo) and TIM6
  (ADC/DAC), and its DMA table gives DMA2 stream 1 to the DCMI only, which this
  board does not enable, while streams 2 to 6 belong to SPI, SDIO and I2S. TIM8
  is reachable only through `pyb.Timer(8)`, which the script never creates.
* A sample index is turned into a time with the sample period and the timer clock
  of the same run, never with a nominal constant. TIM8 runs at 2 x APB2 = 168 MHz,
  the CPU clock, so one sample period is also that many CPU cycles and the DWT
  cycle counter can be cross-checked against the sample axis.
* The moment the method returned is placed on the same axis by reading the DMA
  stream's remaining-transfer count: `4096 - NDTR` is the number of samples
  stored so far. The cost of that read, and of a trivial bound method call on the
  same UART object, are measured in the same run and in the same conditions, so
  the placement of the return inside the break is bounded by measured overheads
  and not by an assumption.

Each repetition sends, on the test UART, a `0x55` frame (ten edges: a
known-answer control that yields the bit time), a `0x00` frame (low for its start
and data bits: a known-answer control for a low run of known length), then the
break, then another `0x55`. The script writes the byte after the break only once
it has seen the line actually go low, so that byte never races the start of the
break; nothing here is measured about a break requested while a character is
still being sent.

Five configurations were run, 24 repetitions each plus 5 more that poll
`CR1.SBK` instead of sending a byte after the break: 1200, 9600 and 19200 baud
at 8N1, and 9600 baud at 8E1 and at 8N2. The port counts the parity bit in the
word length, so 8E1 is a nine-bit USART word.

## What the board showed

Three readings of the F4 reference manual's break character were admissible
before the run, as bit times at 8N1, 8E1 and 8N2: A, one frame including all stop
bits (10, 11, 11); B, one frame including one stop bit (10, 11, 10); C, the
start, data and parity bits only (9, 10, 9).

The board confirmed hypothesis A: the break is low for 10, 11 and 11 bit times at
8N1, 8E1 and 8N2, in both images and in every repetition.

| Baud | Format | Break, bit times | Break, us at the nominal baud |
|---|---|---|---|
| 1200 | 8N1 | 10 | 8333.3 |
| 9600 | 8N1 | 10 | 1041.7 |
| 19200 | 8N1 | 10 | 520.7 |
| 9600 | 8E1 | 11 | 1145.8 |
| 9600 | 8N2 | 11 | 1145.8 |

The microsecond column is the bit count times the bit time that the run's own
BRR and APB1 clock give, which is why 19200 baud shows 520.7 and not 520.8: the
divider the HAL programs produces 19204.4 baud. The per-repetition low times,
the marks and the raw edge indices are in the serial log of the run; the figures
above are recomputed from those registers rather than reported by the image.

Two further facts the same records carry: `sendbreak()` returned while the line
was still low, tens of microseconds after the call and a whole break before the
line went high again (8.3 ms of it at 1200 baud); and the byte written during
the break came out whole and in the configured format afterwards, with the pin
still in its alternate function.

So the break is one character frame of logic 0, not 13 bit times, and it is not
"longer than required for a normal transmission of a character" either: a normal
8N1 character occupies 10 bit times from the start bit to the end of the stop
bit, exactly as much as the break.

## The change

`uart-sendbreak-docs.patch` replaces the body of the `UART.sendbreak()` entry in
the two documents and nothing else. It applies to commit
23f0e61b47f0adf9a8f1259ec603892f27a69eb7 with `git apply`. Each of its two hunks
is anchored on the directive above the entry it rewrites and on the next
directive or heading below it, and rewrites the blank line that separates the
entry from each anchor as itself, so that the only context lines in the file are
the two anchors and no line of a hunk body is a lone space.

The new entry of `docs/library/pyb.UART.rst` reads:

```rst
   Send a break condition on the bus.  On STM32F4 MCUs the method returns at
   once, before the break has been sent, and the break drives the bus low for
   the duration of one character frame (start bit, data bits, parity bit if
   any and stop bits), followed by at least one bit time of logic 1 before the
   next character: with 8 data bits the bus is low for 10 bit times with no
   parity and 1 stop bit, and for 11 bit times with a parity bit and 1 stop
   bit or with no parity and 2 stop bits.
   Return value: ``None``.
```

The second document keeps its generic sentence and puts the measured behaviour in
a per-port note, the form it already uses for `flush()` and `txdone()`.

The new entry of `docs/library/machine.UART.rst` reads:

```rst
   Send a break condition on the bus.

   .. note::

       On the stm32 port with an STM32F4 MCU the method returns at once, before
       the break has been sent, and the break drives the bus low for the
       duration of one character frame (start bit, data bits, parity bit if any
       and stop bits), followed by at least one bit time of logic 1 before the
       next character: with 8 data bits the bus is low for 10 bit times with no
       parity and 1 stop bit, and for 11 bit times with a parity bit and 1 stop
       bit or with no parity and 2 stop bits.
```

Every clause of those texts is held against the board: the numbers and the
frame description by the measured bit count, "returns at once, before the break
has been sent" by the placement of the return mark inside the break, and "at
least one bit time of logic 1 before the next character" by the gap between the
break and the frame that follows it.

### Why not change the code

A blocking `sendbreak()` was the alternative and was rejected. The classic
STM32 USART of the F1, F2, F4 and L1 has no break-length field, so there is no
register setting that would turn the break into the documented 13 bits; LIN mode
would, but it also changes the receiver, and bit-banging a break would have to
take the pin away from the UART and race its transmitter. Making the method wait
for the break to finish would change the timing of every stm32 user, would need
a second implementation for the families that use `RQR`/`ISR.SBKF`, and can hang
under CTS flow control. None of that is what the two reports ask for, and no
configurable break length has been agreed upstream. The documents, which are
simply wrong about this port, are the smaller and more reviewable change. A
blocking implementation would also be caught rather than hidden: the measurement
asserts that the return happens a whole break before the line goes high.

## What this does and does not say

The F405 of the original report has the same F4 USART and the same CR1.SBK path
as the F446, so the break length in bit times and the immediate return carry
over; its clock tree differs, so bit times in microseconds derive from its own
PCLK and are not those measured here.

The result speaks for the F4 CR1.SBK path only: the RQR/SBKRQ families named in
machine_uart.c have a different USART and were not measured; the break length was
measured only with 8 data bits, none or even parity and 1 or 2 stop bits; nothing
was measured about a break requested while a character is still being sent; and
no LIN or SDI-12 conformance is claimed.

Under MicroPython's generative-AI policy nothing from this job goes upstream by
itself: a human reviews the patch and this evidence, does the review
communication and submits.

## Reading the serial log

Every line the images print is `H176 <kind> key=value ...`, decimal or `0x` hex,
and the last line is `H176 done`. The images print measurements only, never a
verdict.

```text
boot   the clock tree, the RCC_CFGR word, the window size (ns) and the target
       samples per bit (spb), the chosen UART, GPIO port and pin, and the
       version string of the firmware
cfg    one per configuration: the requested baud, bits, parity and stop bits,
       the USART BRR/CR1/CR2/CR3 words that resulted, the sample period and
       timer clock, the TX pin's MODER and alternate function, and the measured
       cost of a mark (rd, rd_cyc) and of a trivial call (cl, cl_cyc)
rep    one per repetition: the marks t0, tc (before the call), tr (after the
       return), tb (line seen low) or tsbk (CR1.SBK seen clear), te (end), the
       DWT cycle counts cyc and cyc_all, the return values of the writes, the
       DMA flag group and remaining count, the edge count n, the level of the
       first sample, and the pin's MODER and alternate function read back
edges  the sample indices at which the pin changed level, at most 16 per line,
       in seq order
fault  any exception the script raised, which fails the image
```

A `rep` line and its `edges` lines are one repetition; `phase=main` is the
24-repetition sequence with a byte written during the break, `phase=sbk` the
5-repetition sequence that polls `CR1.SBK` instead.

## Files

* `README.md`, this file.
* `uart-sendbreak-docs.patch`, the change, against the commit named above.
* `micropython/main.py`, the measurement script, frozen into both images as
  source text and run at boot. Its `print` output is the serial log above.

The build that produced the two ELFs and the tests that judge these records live
outside this directory, in `hilval/hq176/build.py` and `tests/hq176/`.
