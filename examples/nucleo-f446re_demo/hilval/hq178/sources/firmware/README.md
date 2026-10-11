# hq178: what each Embassy USART reader returns with parity enabled (NUCLEO-F446RE)

Embassy issue [#6850](https://github.com/embassy-rs/embassy/issues/6850) reports
that with parity enabled the received parity bit comes back as part of the data,
seen on an STM32WL55. Pull request
[#7224](https://github.com/embassy-rs/embassy/pull/7224) masks it off. Its author
states that they have no STM32 hardware of their own, so this is a hardware
report on that fix, not a competing fix: two images, one built from the pull
request's merge base and one from its head, run the same matrix on a
NUCLEO-F446RE and print what every reader returned.

These images ran on the NUCLEO-F446RE through [Agentic Hardware-in-the-Loop (Agentic HIL)](https://github.com/agentic-hil/agentic-hil), which flashed them, reset the board with the serial port open and recorded USART2.

Every statement below about what a reader returned is one that `tests/hq178` in
this repository asserts against the recorded lines, value by value and
repetition by repetition. The images themselves print raw measurements and never
a verdict, and nothing from this run is posted upstream.

## The pull request and the two images

| Image | Revision | What it contains |
|---|---|---|
| `base` | `d6825485e038f4553417c0950c15f459765d4973` | the pull request's merge base |
| `head` | `3dc12e8dd5f80d22e75124b131def830a77a43a0` | the pull request, one commit on top of it |

Both are built from the one crate in `embassy/` next to this file, so the only
difference between them is Embassy itself. The pull request adds
`rx_data_mask()` to `embassy-stm32/src/usart/mod.rs` and applies it in the four
readers that hand received words to a caller: the async DMA reads including
`read_until_idle`, `blocking_read_helper()`, `on_interrupt()` in `buffered.rs`
and `RingBufferedUartRx::read()` in `ringbuffered.rs`. The mask is `None`
without `CR1.PCE`, `0x3F` for a 7-bit word, `0xFF` for the 9-bit word and `0x7F`
otherwise.

## What ran on the board

The F446 has `usart_v2`. That version has no `M1` bit, so of `rx_data_mask()`
only the `M0` branches are compiled into these images: `0xFF` where `M0` selects
the 9-bit word and `0x7F` otherwise. `configure()` maps the line settings to
that word as RM0390 lays it out:

| Configuration | Word | Where the hardware puts the parity bit |
|---|---|---|
| 7E1, 7O1 | 8 bits (`M0` = Bit8, `PCE`) | bit 7, which a `u8` reader can see |
| 8E1 | 9 bits (`M0` = Bit9, `PCE`) | bit 8, which only a `u16` reader can see |
| 8N1 | 8 bits, no `PCE` | there is none |

The device under test is USART1 at 19200 baud in each of the four
configurations above, in single-wire half-duplex mode on PA9 (AF7). USART2 on
PA2/PA3 is the console and is never reconfigured. PA10, which is USART1_RX, is
never configured at all, and that is a finding of its own: an earlier image of
this job put its synthesized frames on PA10 and received nothing through any of
them, while measuring PA10's own input level as high under its own pull-down.
Something outside the chip holds that pin on this board, so no frame of this run
is put on it and no measurement below depends on it. (That earlier measurement
is not part of the records below; what the records carry is the same measurement
of the line this run does use.) Half duplex puts the receiver on PA9, which is
the line every value of every pattern below travels over.

There is no wiring on this bench, so the frames are made inside the chip in two
ways, and each one proves itself with a control in the same image before any of
its measurements is believed.

The first source is the USART's own half-duplex readback:
`Uart::new_half_duplex()`, `Uart::new_blocking_half_duplex()` and
`BufferedUart::new_half_duplex()` with `HalfDuplexReadback::Readback` keep the
receiver enabled while the transmitter shifts a frame out, so the receiver sees
the frame the hardware built, parity bit included. These carry the whole
pattern: every value of the configuration's width (`0x00`..`0x7F` for 7 data
bits, `0x00`..`0xFF` for 8) followed by `0x01` and `0x03`, the two values the
pull request's own test uses.

How the word reaches that transmitter matters only for the time the sequence
takes, and for one reader it had to change. `blocking_read` reads one word per
call, so its sequence alternates a write with a read, and a half-duplex
`blocking_write()` flushes and clears `TE` at the end of every call while the
reader clears it again on every call of its own. Each time `TE` goes from 0 to
1, the transmitter sends a whole idle frame before the word (RM0390 26.3.2), so
one word read cost two frames of line time instead of one. In that sequence the transmitter
is therefore enabled once and the word is written into `USART_DR` itself, and
half-duplex mode is switched off for the few cycles the read call takes so that
the reader leaves `TE` alone; the word is already in the data register then and
the next frame cannot start until the next write, so nothing can arrive in that
window, and the pin the receiver listens to meanwhile is the PA10 this board
holds at mark. The parity bit is still the one the USART computes from its own
`CR1`, the frame still travels the single wire, and the reader under test still
takes the word out of the data register through its own mask.

RM0390 asks for `CR1.UE` to be clear when `CR3.HDSEL` is written, and
`configure()` writes it that way, so the image does not assume that the bit can
be written while the USART runs: it reads the bit back after writing it. Where
it did not take, the read clears `TE` as it always would and the transmitter is
enabled again for the next word, so that word costs the idle frame as well; the
values are the same either way, and only the time the sequence takes differs.
The `ticks` field of a `blk`/`rb` sequence says which of the two happened: about
one frame of line time per word, or about two. The other three readback
sequences write through Embassy's own call, because one call carries their whole
pattern.

The second source is needed because a transmitter cannot send a frame with the
wrong parity bit, and because `RingBufferedUartRx::read()` switches the
transmitter off in half-duplex mode and so can never read anything back. Its
frames are written bit by bit, one word per bit, by DMA2 on every TIM8 update
into one register of GPIOA. TIM8_UP is DMA2 stream 1, channel 7, and DMA2 is the
only controller whose peripheral port reaches the AHB1 GPIO ports; neither the
transmit DMA (stream 7) nor the receive DMA (stream 2) of USART1 uses stream 1.

Which register that is, and so what drives the line, is the one thing these
images had to find out on the board. The pin is the transmit pin of half-duplex
mode, configured open drain (`OutputConfig::OpenDrainPullUp`), which is what
`new_half_duplex()` asks for anyway: the transmitter can pull that line low but
never drives it high, so a released line rests wherever the pull bits put it,
whether the transmitter is enabled or not. That leaves two ways to put a level
on it:

| `drive` | The register and the pin | What it needs |
|---|---|---|
| `pull` | PA9's two pull bits in `GPIOA_PUPDR`, the pin left the open-drain alternate function | a line the internal pull of about 40 kOhm can move |
| `open` | PA9's two bits in `GPIOA_BSRR`, PA9 an open-drain output while `AFRH` keeps USART1 selected on it | a pin whose input path still reaches the receiver in output mode |

The first is the register this job names, and every image measures whether it
works in every `ctl` record: `pin_lo` and `pin_hi` are PA9's own input level
with its pull bits set to pull-down and to pull-up, beside `pin_mode`, `pin_af`
and `pin_ot`, which say that the pin was the open-drain alternate function of
USART1 while that was measured. Each image then sends one trial frame per drive
before its controls, reads it straight out of `USART_DR`, and keeps the first
drive whose frame arrives as RM0390 lays it out. Those trial frames are not
printed: the `drive` field of every `ctl` record names the drive the whole phase
then used, and the nine control frames that follow are the measurement of it. A
sequence reports `synth=ok` only when that selection had succeeded and its own
word list ran out with the line back at mark. Only the two bits of the driven
pin are ever written in `GPIOA_BSRR`, and the pull-bit word keeps every other
pin's pull setting, so no other pin of the port changes either way.

At 19200 baud one bit is 52 microseconds; an output driver settles in far less
than one microsecond and the internal pull in about one, so an edge has settled
long before the receiver's three samples in the middle of the bit. Frames are
separated by two idle bits beyond the stop bit: more than any reader needs to
take a word or to re-arm after an error, and less than the full frame of idle
that the hardware needs to report an idle line, so no overrun can hide a parity
error and no idle line falls between two frames.

The third thing in every image is a register-level control that shares no code
with the readers: it writes `CR1` straight from RM0390, reads one synthesized
frame out of `USART_DR` through its address, and records `USART_SR`, `USART_DR`
and `pe_lead`, the number of core cycles between the first read of SR that
showed `PE` and the first that showed `RXNE`. It runs for 7E1, 7O1 and 8E1 with
`0x01`, with `0x03` and with `0x01` carrying an inverted parity bit.

Every image measures its own clock configuration and prints it; `Config::default()`
leaves this part on HSI at 16 MHz without the PLL, which makes TIM8's 833-cycle
period exactly one bit of 19200 baud. Every read has a timeout, every sequence
ends with one call that must find nothing, each of the twenty repetitions per
configuration and reader is recorded on its own, and the whole matrix runs a
second time after the image resets itself with SYSRESETREQ.

## What each reader returned

Five readers are exercised: `blocking_read` (`blk`), `UartRx::read` (`asy`),
`read_until_idle` (`idl`), `BufferedUart` through `BufferedUartRx::read`
(`buf`) and `RingBufferedUartRx` through its `read()` (`rng`). At 8E1 the first
three and the last are used through their `u16` calls, because the parity bit
lives in bit 8 of the 9-bit word and a `u8` call can never carry it.

With correct parity and no error, for every value of the pattern:

| Configuration | `base` returns | `head` returns |
|---|---|---|
| 7E1 | the data bits with the parity bit in bit 7 (`0x01` comes back as `0x81`, `0x03` as `0x03`) | the data bits (`0x01`, `0x03`) |
| 7O1 | the data bits with the parity bit in bit 7, the other way round (`0x01` as `0x01`, `0x03` as `0x83`) | the data bits |
| 8E1 | the data bits with the parity bit in bit 8 (`0x01` as `0x101`, `0x03` as `0x003`) | the data bits |
| 8N1 | the byte as sent, bit 7 included | the byte as sent, bit 7 included |

That is the bug of issue #6850 on this part, and the pull request removes it on
every one of the four code paths it touches. Two entries of the table have no
reader behind them and the report says so rather than leaving a gap: at 8E1 the
`u8` store in `buffered.rs` drops bit 8 before any caller sees it, so
`BufferedUart` returns the data bits in `base` as well, and `blocking_read` on
`u8` reads the data register through a byte pointer, which cannot carry bit 8
either. At 8N1 `rx_data_mask()` is `None`, so both revisions return the byte
unchanged and no value loses bit 7 to a mask that should not be there.

## Wrong parity

A frame whose parity bit is inverted is synthesized as the third of six, and
every call after it is recorded until one of them finds nothing. What the async
readers do with such a frame depends on whether the hardware raises `PE`
together with `RXNE` or a part of a bit earlier, because the interrupt handler
in `mod.rs` clears the DMA request when it runs. RM0390 asks software to wait
for `RXNE` before it clears `PE`, which only matters if `PE` can be set first,
so both orders are allowed and the control of each run measures which one this
part has from its `pe_lead` field.

Measured PE timing: together

That line is the class the control of this run measured, and it is checked:
`tests/hq178` derives the class from the `ctl` records of both images and fails
the run if the line names another one, so the outcomes below are read against
the timing this part actually has rather than a chosen one.

Read with that answer, the two code-derived outcomes are:

* `blocking_read` reports `Error::Parity` and then returns the erroneous word on
  the next call, in both timings. `check_rx_flags()` reads the status register
  only once `RXNE` is set and returns the error without reading the data
  register, so the word is still pending; the second status snapshot of that
  call shows `RXNE` for exactly that reason.
* `UartRx::read` on one word returns `Error::Parity` under the early timing: the
  handler clears the DMA request before the word arrives, the read's abort
  future reports the error and the buffer still holds the sentinel the image put
  there. Under the together timing this part has, the DMA has already moved the
  word when the task runs, so the read returns it as `Ok` and no error is
  reported at all.
* `read_until_idle` always reports `Error::Parity` first, because its transfer
  cannot complete at the third of six frames. `inner_read` masks only on
  success, so the prefix the DMA had already written is raw in both images: the
  two frames before the error and, under the together timing, the erroneous word
  as the hardware stored it. One further call then returns the three frames
  after the error.
* `RingBufferedUartRx` reports the error as `check_idle_and_errors()` does, and
  `read()` stops the background reception. Nothing can wake that reader earlier:
  `start()` enables no `RXNE` interrupt without `eager_reads`, six words cannot
  half-fill a ring of 1024, and there is no idle line before the first
  character. One thing could, and the image takes it away first: that reader
  reports the transmitter's `TC` flag to the transmit waker on every poll of its
  own future, and `TC` is set out of reset, so a waker left registered by an
  earlier sequence of the same task would re-poll the reader and it would hand
  over the words that had arrived so far. The image clears `TC` before every
  synthesized sequence, and nothing in one transmits, so it stays clear.

Three of those findings a reader cannot derive from the records alone, so each
stands here as its own statement.

RingBufferedUartRx restart: the next `read()` starts the ring again from its beginning, so the words received before the error are discarded and only the frames after it are returned.

BufferedUart limit: `on_interrupt()` stores the erroneous byte and only calls `warn!`, which no feature of these images enables, so the API reports nothing at all about the error and every call of that reader returns `Ok`.

Consequence of the together timing: the one-word async read returns the erroneous frame as `Ok`, and at `head` the mask then takes the parity bit off it, which was the last trace in the data of an error that the doc comment of `rx_data_mask()` says `Error::Parity` reports.

In every case the frames that follow the erroneous one arrive as they were sent,
and in `head` they are masked to their data bits exactly as the frames before
it.

## Paths not covered

* `usart_v3` and `usart_v4`, where the `M1` bit exists. The `0x3F` branch of
  `rx_data_mask()` is for a 7-bit word selected by `M1` and is not compiled for
  this part, so nothing here says anything about it, and issue #6850 was
  reported on `usart_v4`.
* `new_half_duplex_on_rx` and `BufferedUart::new_half_duplex_on_rx`, which exist
  only on `usart_v3` and `usart_v4`.
* `eager_reads`, left at its default, so the buffered and ring-buffered readers
  wake on a half-full buffer or an idle line and never per word.
* `detect_previous_overrun`, left at `false`, so a pending error is cleared
  before a read rather than reported by it.
* 9 data bits with parity, and 7 data bits without parity: `configure()` returns
  `ConfigError::DataParityNotSupported` for both on this part, so neither can be
  measured here.
* `BufferedUart` at 8E1 and `blocking_read` on `u8` at 8E1: bit 8 of the 9-bit
  word never reaches either, so on this path the two revisions cannot be told
  apart by any API the reader offers.
* Full-duplex reception, that is a reader listening on the USART's own RX pin.
  Every sequence here runs in single-wire half-duplex mode on PA9, because PA10
  is held high from outside the chip on this board. The masking the pull request
  changes is the same code either way (`rx_data_mask()` is applied where the word
  leaves the data register, not where the line enters the pin), but no frame of
  this run entered through PA10.
* The receive FIFO, the `swap_rx_tx` and inversion settings and the `u16`
  buffered API, none of which this part or this driver has.

## Reading the records without our tools

Open the ST-LINK's virtual COM port at 115200 baud, 8N1, no flow control, and
reset the board. Every line the image prints is
`HQ178 <kind> key=value key=value ...`; untagged lines are not part of the
record. The image prints one empty line at every start and `HQ178 done` once,
after which it stops and never prints again. Numbers are decimal or `0x`
hexadecimal, and a word list is fixed-width lower-case hex without separators,
two digits per word or four where a `u16` reader is used; `-` means none.

The record kinds are:

* `boot`: the reset flags of `RCC_CSR` as they were at this start, the phase and
  whether the marker in uninitialised RAM was found. The marker is read and
  zeroed at every start and honoured only together with the software-reset flag,
  so a reset from outside always starts the first phase. The second `boot` line
  is the image's own restart between its two runs of the matrix.
* `clk`: the kernel clocks of USART1, USART2 and TIM8 in hertz, `RCC_CFGR`,
  USART1's `BRR` at 19200 baud, and the cycles the core counted during a
  100 ms delay.
* `ctl`: one register-level control frame. `kind` is `ok` or `bad`, `val` what
  was sent, `sr` and `dr` the status and data registers as they were read, and
  `pe_lead` the cycles between `PE` and `RXNE` (`-1` when `PE` never appeared)
  beside `poll`, the longest single iteration of that measurement. The same
  record carries the control of the stimulus itself, taken with the receiver
  switched off so no level of it can be mistaken for a frame: `pin_lo` and
  `pin_hi` are PA9's own input level with its pull bits set to pull-down and to
  pull-up (a line the pull can move reads `0x0` and then `0x200`), `pin_mode`,
  `pin_af` and `pin_ot` are PA9's mode, alternate function and output type as
  the driver left them (`2`, `7` and `1` are the open-drain alternate function
  of USART1), `prate` is the core cycles DMA2 took for `pwords` timer updates,
  which is one bit time per word when the stimulus runs at the baud rate, and
  `drive` names which of the two ways of driving the line this phase used. A
  sequence
  reports `synth=ok` only when a trial frame had arrived through that drive,
  because a word list that ran out and a register that ended where it started
  read the same whether or not the line ever moved.
* `cfg`: the line settings of one repetition and configuration, with `CR1` and
  `BRR` as the driver left them.
* `run`: one sequence. `rd` is the reader, `src` is `rb` for the half-duplex
  readback and `sy` for the synthesized frames, `kind` is `full` for the whole
  pattern, `ctl` for the synthesized control and `bad` for the sequence with the
  inverted parity bit. `n` is how many words the successful calls returned,
  `calls` how many calls were made and `res` their results in order,
  run-length encoded over `o` (Ok), `p` `f` `n` `v` `b` (the five error
  variants, parity first) and `t` (a call that timed out): `o2p1o4t1` is two Ok,
  one parity error, four Ok and the final call that found nothing. `ticks` is
  the duration in 32768 Hz ticks measured in the chip, and `synth` says whether
  the synthesizer drove the line as asked (`na` where it was not used).
* `val`: the words of a `full` sequence, in order, split over as many records as
  the field length needs; `part` numbers them from zero.
* `call`: one call of a `ctl` or `bad` sequence, numbered by `i`. `res` is that
  call's result, `n` the words it returned, `hex` its buffer or chunk, and `sr0`
  and `sr1` two snapshots of `USART_SR` taken after a call that reported an
  error, two bit times apart. Reading the status register alone clears nothing
  on this part, so the second snapshot says whether the erroneous word had
  reached the data register. The image issues the next read two bit times after
  an error for the same reason, which is far less than the twelve bit times
  between two frames.
* `matrix`: the end of one phase, with the repetitions and sequences it ran.
* `done`: the end of the image. Nothing follows it.
