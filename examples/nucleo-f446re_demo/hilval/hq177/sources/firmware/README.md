# hq177: dual-channel DAC with DMA on an STM32F446, read back by the ADC

One firmware image and one documentation patch for
[libopencm3 issue #528](https://github.com/libopencm3/libopencm3/issues/528):
the dual-DAC DMA example in the shared Doxygen text at the top of
`lib/stm32/common/dac_common_all.c` is written against the channel DMA API of
the F0/F1/F3/L1 families. It names `DMA2, DMA_CHANNEL3` and `DMA_CCR_*` fields
the STM32F2/F4 do not have, takes the address of `DAC_DHR8RD`, which is now a
macro with a `dac` argument, and never says how the two channels' values are
packed into one dual data register. The F2/F4 need the stream DMA API.

These images ran on the board through
[Agentic Hardware-in-the-Loop (Agentic HIL)](https://github.com/agentic-hil/agentic-hil).

**What is claimed, and nothing more:** functional dual-channel output. Each
channel outputs the 12-bit code packed for it, in the buffer's order, over
twelve complete buffer cycles, with no DMA error flag and no DAC underrun. The
analog levels are only ever judged against the error budgets stated below;
none of them is a precision figure, and no high-precision analog value is
claimed. Nothing is claimed about how simultaneous the two channels are: ADC1
converts PA4 and PA5 one after the other, about 120 us apart, so a skew below
that is invisible here and is not looked for.

## Nothing is wired to the board

The bench is one NUCLEO-F446RE with its ST-LINK and no wiring at all, so every
stimulus and every measurement is in-chip. Where a bench instrument or a jumper
would normally be used, this is the substitute:

| What would be external | What is used instead |
|---|---|
| A voltmeter or scope on the DAC outputs | ADC1 converts the DAC's own pins: PA4 is `DAC_OUT1` and `ADC1_IN4`, PA5 is `DAC_OUT2` and `ADC1_IN5` |
| A reference voltage to scale the readings | The internal reference on `ADC1_IN17` against its factory calibration word, which gives VDDA |
| A signal generator for the DAC trigger | TIM7's update event as its TRGO, which never leaves the chip |
| A logic analyser on the DMA | The DAC's own output registers `DOR1`/`DOR2`, the stream's `NDTR` and its interrupt status flags, read by the firmware |
| A frequency counter | TIM2, 32 bits, free running at the APB1 timer clock; every time in the evidence is a TIM2 count, never a host timestamp |

Nothing external was measured, and no statement in the evidence depends on
anything outside the chip.

### PA5 also drives the board's LED

On a NUCLEO-F446RE, PA5 carries the user LED LD2 through a series resistor to
ground as well as `DAC_OUT2`. Once PA5's level nears the green LED's forward
voltage, about 2 V, the LED starts to conduct, and from there `DAC_OUT2` drives
a load far below the 5 kiloohm minimum the F446 datasheet gives for the output
buffer; PA4 carries no load at all. Outside that specified load the datasheet
promises nothing about the buffered level, and neither does this image: it
writes the same code to both channels, reads each pin back eight times and
prints the numbers.

The effect is not a simple sag. On the bench board, code 3000 with the buffer
on read 3019.8 on PA4 and 3147.5 on PA5, so the loaded channel sat about 128
LSB, roughly 0.1 V, *above* the unloaded one; PA5's eight conversions spanned
2 LSB and PA4's 8 LSB, and three further runs of the same image on the same
board read 3146.6 against 3018.8, 3146.0 against 3020.4 and 3147.9 against
3019.9, so these are the board's figures and not noise. One code lower, at 2500, just under the LED's forward
voltage, the two pins still agreed inside two channel error budgets, and every
channel 2 level the DMA run uses, all at or below 1800, was within 40 LSB of
its own code. The deviation follows the LED's current.

Three controls in the same run say the readback itself is sound, so those
figures are the board's behaviour and not the measurement's: the internal
reference against its factory calibration word puts VDDA between 3.0 V and
3.6 V; PA4 is within 40 LSB of every code it is given, buffered and unbuffered
up to 4000; and PA5 is within 40 LSB of every code below the LED's forward
voltage, through the same ADC channel that reads 3147 at code 3000.

What the sign rules out is a passive load. A resistor and an LED to ground can
only take current out of the pin, and taking current out of a node cannot raise
it. While the buffer is on, it is the buffer that sets the level at the pin, and
this load is far below the 5 kiloohm minimum the datasheet specifies for it;
what a buffer does outside its specified load is not something this image
measures, and the datasheet does not promise it either. With the buffer off the
output is driven through the DAC's own resistor string, tens of kiloohms against
the LED's few hundred ohms, so there the LED has to dominate and pull the pin
down; that is what the `static buf=0` codes 500, 1500, 2500, 3500 and 4000 are
measured for, and their numbers are in the evidence of every run, although the
gate stops at the buffered comparison before it reaches them. The gate in
`tests/hq177/` expects the loaded pin never to read more than two channel error
budgets above the unloaded one; the measurement above contradicts that
expectation at code 3000, and it is an expectation about a loaded output buffer,
not about the DMA path this example documents.

Two consequences for the design:

* The image measures both pins at codes 2500, 3000, 3300 and 3700 with the
  output buffer on and at 500, 1500, 2500, 3500 and 4000 with it off, so the
  effect is in the evidence as numbers rather than as an assertion.
* The DMA sequence for channel 2 stays at or below code 1800, about 1.45 V,
  below the LED's forward voltage, and every one of its levels has its own
  static control in the same run.

### The output buffer's range

The F446 datasheet specifies the DAC's buffered output only from 0.2 V to
VDDA - 0.2 V, which is about code 3820 at VDDA = 3.0 V. Every code this image
drives with the buffer on therefore stops at 3700, about 120 LSB inside that
limit; code 4000 is measured with the buffer off only, where the question is
the LD2 load and not the absolute level.

## The image `dual-dac`

Built from `main.c`, `dual_dac_dma.c`, `console.c` and `faults.c` against
libopencm3, it measures and prints and never decides: the tests in
`tests/hq177/` compute every verdict from the raw values. The system clock
stays at the reset HSI, 16 MHz, on every bus, which is what libopencm3 assumes
until told otherwise, so one TIM7 period is exactly 160000 TIM2 counts whatever
the HSI's own error is.

Phases, in order:

1. **boot.** USART2 up, TIM2 started, then the device identity, `RCC_CFGR`,
   the constants of the run and the SHA-256 of every source the build
   compiled.
2. **adc.** ADC1 on with 480 sampling cycles, 12 bits, single conversion,
   software start. The ADC clock keeps its reset prescaler, APB2/2 = 8 MHz,
   well inside the F446's 36 MHz limit; 480 cycles are then about 60 us of
   sampling. Ten conversions of the internal reference follow: against the
   factory calibration word they give VDDA, and that is the control which says
   the ADC converts sanely before any DAC reading is believed.
3. **static, buffered.** Both channels are enabled with no trigger and no DMA
   request, so a write to a holding register reaches the output register one
   APB cycle later. Each of the twenty codes is written to both channels and
   read back eight times on each pin: both output registers are read 200 us
   after the write, then PA4's eight conversions and then PA5's, about 62 us
   each, so a record's `min` and `max` show whether its own pin held still over
   the half millisecond its mean comes from.
4. **static, unbuffered.** The same with `BOFF1` and `BOFF2` set, for the five
   codes that include the rail.
5. **dma.** `DAC_CR` is cleared, then `dual_dac_dma_start()` arms everything
   (see below) and the firmware records 97 plateaus of 10 ms: one per trigger,
   each sampled eight times per pin after a 1 ms settle, with both output
   registers, the stream's remaining count and flags and the DAC's status.
6. **udr.** The stream is disabled while the triggers keep coming, so nobody
   serves the requests and `DMAUDR1` must appear; then it is written back to 1
   and must go away. Without this positive control, the zeros of phase 5 would
   say nothing about the run and only something about the flag read.
7. **done.**

### Nothing is printed while something is measured

Between the `console_flush()` before `dual_dac_dma_start()` and the end of the
plateau loop the image prints nothing at all. The console blocks: at 115200
baud a character costs about 87 us, so one plateau record of about 190
characters takes about 17 ms, longer than the 10 ms plateau it belongs to, and
the eight `word` records together take about 31 ms. TIM7's update flag is
sticky, so a late poll would still see every update, but the output registers
would already hold a later pair and the timing would be nonsense. The records
are held in RAM (about 5 KiB of the 128 KiB) and printed after the loop, which
is why the `dma phase=armed` line appears after the plateaus although its
values were read before the first trigger.

### What the lines look like

Every line is `HQ177 <kind> key=value ...` on USART2 (PA2/PA3, 115200 8N1, the
ST-LINK virtual COM port), and the last line is `HQ177 done`. Untagged lines
are ignored; a tagged line that does not parse fails the run.

| kind | fields | count |
|---|---|---|
| `boot` | `image` | 1 |
| `id` | `dev` `rev` (from the debug identity register) | 1 |
| `clock` | `cfgr` `sysclk` | 1 |
| `cfg` | `n` `cycles` `plateaus` `psc` `arr` `settle` `samples` | 1 |
| `src` | `file` `sha` (SHA-256 of that source, as the build computed it) | 4 |
| `cal` | `vrefint` (the factory calibration word) | 1 |
| `adc` | `ch` `n` `min` `max` `sum` `failed` | 1 |
| `static` | `buf` `code` `pin` `ch` `dor` `cr` `n` `min` `max` `sum` `failed` | 40 buffered, 10 unbuffered |
| `dma` | `phase=armed`: `ndtr` `isr` `sr` `cr` `par` `m0ar` `words` `fcr` `dac_cr` `t7_psc` `t7_arr` `t7_cr1` `t7_cr2`; `phase=end`: `ndtr` `isr` `tc` `sr` `cr` `dac_cr` | 2 |
| `word` | `i` `ch1` `ch2` `w` | 8 |
| `plateau` | `p` `t` `timeout` `dor1` `dor2` `ndtr` `isr` `sr` and, per channel, `n1 min1 max1 sum1 fail1` / `n2 min2 max2 sum2 fail2` | 97 |
| `udr` | `phase` `sr`, and `dor1` `dor2` for the control | 2 |
| `fault` | `kind` `cfsr` `hfsr` `bfar` | none expected |
| `done` | none | 1 |

Print order: everything of phases 1 to 4 as it is measured, then after the
plateau loop `dma phase=armed`, the eight `word` lines, the 97 `plateau` lines
and `dma phase=end`, then the two `udr` lines and `done`. `isr` carries the
stream's five flags shifted down to bit 0, so bit 5 is transfer complete, bit 4
half transfer, bit 3 transfer error, bit 2 direct-mode error and bit 0 FIFO
error. `n` and `n1`/`n2` count the conversions that reached `min`, `max` and
`sum`, and `failed`/`fail1`/`fail2` how many of the eight (ten for the internal
reference) timed out instead, so `sum` divided by `n` is always the mean of the
readings those fields describe, and a conversion that failed is visible in both
numbers.

## The example, and what it is tied to

The body of `dual_dac_dma_start()` in `dual_dac_dma.c`, between the lines
`/* hq177-example-begin */` and `/* hq177-example-end */`, is the code the
documentation patch inserts into libopencm3, line for line. The region
therefore carries no comment of any kind: it lands inside the Doxygen comment
block at the top of `dac_common_all.c`, and a `*/` anywhere in it would close
that comment and leave the file uncompilable. The explanation of the five
steps sits in the comment above the function instead, outside the markers.

Things that bite, and are the reason the example looks as it does:

* **The packing.** `DAC_DHR12RD` holds channel 1 in bits 11:0 and channel 2 in
  bits 27:16. The example packs both samples itself, which is the part issue
  #528 says the old text never explains. The field macros of
  `dac_common_all.h` are spelled inconsistently upstream:
  `DAC_DHR12RD_DACC1DHR_MSK` for channel 1 but `DAC_DHR12RD_DACC2DHR_MASK` for
  channel 2. The example uses both spellings as they are.
* **One request per trigger.** DMA is enabled for channel 1 only, so a trigger
  raises exactly one request and that one 32-bit word carries both channels.
  `DMAUDR2` cannot be set at all then, which is why the tests assert it as zero
  and say so.
* **`dac_set_trigger_source()` only ORs bits into `DAC_CR`.** It cannot clear a
  trigger selection that is already there, so `main` writes `DAC_CR = 0` before
  the example runs. The same function would be a no-op for TIM6, whose `TSEL`
  value is 0: a missing call would then pass unnoticed. This example uses TIM7,
  `TSEL = 010`, so a missing or wrong call shows up at once as a DAC that waits
  for a trigger that never comes.
* **The UG event before TRGO.** `timer_generate_event(TIM7, TIM_EGR_UG)` loads
  the prescaler into its shadow register; without it the first period would run
  with the reset prescaler, 0.625 ms instead of 10 ms. It is generated before
  the master mode is set to the update event and long before the DAC is
  enabled, so no stray trigger reaches the DAC.
* **The output lags the buffer by one trigger.** On a trigger the DAC moves the
  holding register into the output register and only then asks the DMA for the
  next word. So the first trigger outputs the pair
  `dac_load_data_buffer_dual()` preloaded, and plateau `p` shows
  `words[(p - 2) mod 8]`. The preload is the last entry of the buffer, which
  also makes the library's own packing agree with the explicit one in the same
  run.
* **`dma_set_peripheral_address()` and `dma_set_memory_address()` do nothing
  while the stream is enabled**, so both come before `dma_enable_stream()`.

### How the documentation and the binary are tied together

A patch that matches a file in a repository only shows that two files in a
repository agree. The chain to the board is closed in five links a reader can
check in the evidence of a run:

1. The image prints one `src` record per source the build compiled, with the
   SHA-256 the build computed.
2. Each digest is passed into the compiler as a string literal and printed from
   that literal, so the example's digest is in the flashed binary as ASCII and
   not only in the serial output.
3. The bench records the SHA-256 of the binary it flashed and refuses a run
   whose flash report names another one.
4. `dual_dac_dma_start` is one global symbol in that binary, which `main` calls
   exactly once with a plain `bl`. The image is built without link-time
   optimisation and each source is its own translation unit, so the compiler
   cannot inline the documented function into its caller.
5. Right after that call the image reads back every register the example
   configured (`DMA1` stream 5 `CR`, `PAR`, `M0AR`, `FCR`, `DAC_CR`, TIM7's
   `PSC`, `ARR`, `CR1` and `CR2`) and reads `CR` and `DAC_CR` again after the
   last plateau, so neither a different piece of code nor a later change can
   have configured the run.

## The documentation patch

`0001-dac-doc-split-dual-dma-example-by-dma-family.patch` applies to
`lib/stm32/common/dac_common_all.c` at libopencm3
`ddcbb2889fa018626c604e27ea50e66db5755b78` and touches nothing else. It splits
`@section dac_api_dma_ex` into two subsections:

* `@subsection dac_api_dma_ex_channel` holds the existing channel-API example
  byte for byte, under a heading that says which devices it is for. No global
  replacement is used, so the other families' text cannot change by accident.
  A `@note` adds the one correction that example needs today: the dual data
  registers take the DAC base address as an argument.
* `@subsection dac_api_dma_ex_stream` is new and holds the example above, with
  the F446's request mapping (DMA1 stream 5 on channel 7 for DAC channel 1,
  stream 6 for channel 2) and the layout of `DAC_DHR12RD` spelled out.

An introduction between the section heading and the first subsection tells the
reader which of the two applies to their device.

Left alone on purpose: the F1-flavoured wording in `dac_dma_enable()`'s own
documentation ("DMA2 channel 3 for DAC channel 1 and DMA2 channel 4 for DAC
channel 2") is the same kind of problem one function further down, and belongs
in its own change rather than in this one.

Only one revision of libopencm3 is available to this check, the one named
above, so nothing is said here about how the text looked before it or how it
will look after.

## Rebuilding and rerunning without our tools

The build script `hilval/hq177/build.py` only arranges the commands below; a
reader with an Arm toolchain and a NUCLEO-F446RE needs nothing else.

1. Clone libopencm3 and check out `ddcbb2889fa018626c604e27ea50e66db5755b78`,
   then build the F4 target soft-float, the way this image is linked:

   ```
   python3 ./scripts/irq2nvic_h ./include/libopencm3/stm32/f4/irq.json
   make TARGETS=stm32/f4 FP_FLAGS=-mfloat-abi=soft
   ```

2. Compile and link the image against that tree, from this directory.
   `LIBOPENCM3` is its root, and each digest define has to reach the compiler
   as a C string literal, so the double quotes are themselves quoted for the
   shell (`sha256sum main.c` prints the digest to put inside them):

   ```
   arm-none-eabi-gcc -mcpu=cortex-m4 -mthumb -mfloat-abi=soft \
     -std=c11 -O2 -g0 -ffreestanding -fno-common \
     -ffunction-sections -fdata-sections -Wall -Wextra -Werror \
     -fno-tree-loop-distribute-patterns \
     -DSTM32F4 \
     -DHQ177_SHA_MAIN_C='"<SHA of main.c>"' \
     -DHQ177_SOURCE_SHA='"<SHA of dual_dac_dma.c>"' \
     -DHQ177_SHA_CONSOLE_C='"<SHA of console.c>"' \
     -DHQ177_SHA_FAULTS_C='"<SHA of faults.c>"' \
     -I. -isystem $LIBOPENCM3/include \
     main.c dual_dac_dma.c console.c faults.c \
     -nostdlib -nostartfiles -L$LIBOPENCM3/lib -Tlink.ld \
     -Wl,--gc-sections -Wl,--build-id=none -Wl,--strip-debug \
     -o dual-dac.elf -lopencm3_stm32f4 -lgcc
   ```

   The digests are only provenance: the image prints them, and a reader who
   changes a source and recomputes them gets a different binary, which is the
   point.

3. Flash `dual-dac.elf` with any ST-LINK tool and open the board's virtual COM
   port at 115200 baud, 8 data bits, no parity, one stop bit. The image prints
   its lines once after reset and then stops; it takes about five seconds.

4. To apply the documentation patch to a libopencm3 checkout:

   ```
   git apply --whitespace=nowarn 0001-dac-doc-split-dual-dma-example-by-dma-family.patch
   ```

## Licensing

libopencm3 is LGPL-3.0-or-later. The patch changes an LGPL-3.0-or-later file
and is offered under the same terms; the file keeps its own header.
`THIRD_PARTY_NOTICES.md` beside this file names what the image links against.
The firmware in this directory is part of this repository and carries its
license.
