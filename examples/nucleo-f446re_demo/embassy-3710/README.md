# Embassy #3710 on a NUCLEO-F446RE

A hardware report on the open Embassy issue
[embassy-rs/embassy#3710](https://github.com/embassy-rs/embassy/issues/3710),
"Add granular control for ComplementaryPwm (independent enable/disable of main
and complementary outputs)". It measures the change that the accompanying pull
request to embassy-rs/embassy proposes, as a patch on the Embassy revision the
images are built from, against the unmodified crate; and it measures what the
timer itself does when a channel's two enable bits are written one at a time,
which is what the patch's doc comments claim.

The runs are driven by [Agentic Hardware-in-the-Loop (Agentic HIL)](https://github.com/agentic-hil/agentic-hil):
its test reactor flashes each image, resets the board with the serial port
already open and records what the image prints until its last line, following
the plans in [plans/](plans/). Nothing reached the board any other way.

## What the change does

`ComplementaryPwm` in `embassy-stm32` drives the complementary output pairs
(`CHx` and `CHxN`) of an advanced-control timer. Its `enable(channel)` and
`disable(channel)` set and clear `CCxE` and `CCxNE` in `TIMx_CCER` together, so
a channel's main output cannot run while its complementary output stays off,
and the other way round. The issue asks for the two outputs to be switchable on
their own.

The patch, [embassy-independent-outputs.patch](embassy-independent-outputs.patch),
adds four methods beside `enable` and `disable` and changes nothing that exists:
`set_main_output_enable(channel, bool)` writes `CCxE` alone,
`set_complementary_output_enable(channel, bool)` writes `CCxNE` alone, and two
getters read the bits back. The complementary getter needs a read-back
primitive that `timer/low_level.rs` had only for `CCxE`, so
`get_complementary_channel_enable_state` is added there next to
`enable_complementary_channel`. `enable` and `disable` keep switching both
outputs together.

In the patch as the images were built with it, the getters are called
`get_main_output_enable` and `get_complementary_output_enable`. The pull request
names them `main_output_enabled` and `complementary_output_enabled`, following
Embassy's API guidelines (getters carry no `get_` prefix); their bodies are the
same. Below, the methods are referred to by what they do.

What the timer drives while only one output of a channel is enabled is in
RM0390, the STM32F446 reference manual, in its table of the output control
bits for complementary `OCx` and `OCxN` channels. Its rows for `MOE` 1 and
`OSSR` 0:

| `CCxE` | `CCxNE` | `OCx` | `OCxN` |
|---|---|---|---|
| 0 | 0 | output disabled, not driven by the timer | output disabled, not driven by the timer |
| 0 | 1 | output disabled | `OCxREF` xor `CCxNP`: `OCxREF` itself, no dead time |
| 1 | 0 | `OCxREF` xor `CCxP`: `OCxREF`, no dead time | output disabled |
| 1 | 1 | `OCxREF` with polarity and dead time | the complement of `OCxREF` with polarity and dead time |

So a channel with one output enabled drives that pin straight from `OCxREF`,
without dead time, and a lone complementary output is not inverted; the output
that is disabled is released by the timer, so its pin floats unless `OSSR` or
`OSSI` select an off-state level. Dead time is inserted again as soon as both
outputs are enabled. The doc comments of the new methods say this, and it is
what the images measure.

## Coverage, and what is not claimed

- One channel of one timer is exercised: TIM1 CH1 on PA8 and CH1N on PA7 of an
  STM32F446RE. The methods take the channel as a parameter and reach the
  register through the same path for every channel, but channels 2 to 4 and
  TIM8 are not measured.
- Dead time is 8 ticks or 0, with `CKD` 0. `OSSR`, `OSSI`, `OIS1` and `OIS1N`
  are 0 and the break input is off throughout, so the rows of the table above
  are the only ones covered. What a disabled output does with `OSSR` 1, where
  the timer drives it to its idle level instead of releasing it, is the
  reference manual's statement and is not measured here.
- `CC1P` and `CC1NP` stay 0; the polarity terms of the table are not exercised.
- Nothing is connected to the board. A released pin is held by its own internal
  pull resistor, and nothing is measured outside the chip: no power stage, load,
  motor or logic analyser is involved, and no claim is made about any of them.
- The sampler reads the pins' input stage (`GPIOA_IDR`) once per 16 ticks. The
  statement "no sample saw both outputs high" therefore holds at the
  granularity of one tick, 62.5 ns, over every tick position of the period, and
  says nothing about the analog shape of the edges.
- `set_frequency` is not under test: the period is written to `PSC` and `ARR`
  directly and read back. `set_duty` and `set_dead_time` are used for the setup
  and judged only through the registers the `setup` record reads back. The
  `embedded_hal_02::Pwm` implementation is untouched by the patch and not
  exercised.

## The two images

Both are built from the one crate in [embassy/](embassy/): two binaries of the
same library, with the same setup, sampler and console, against the same
Embassy revision. The patch is the only difference in what they link, and the
switching step is the only difference in what they run.

| Image | Embassy tree | Switching steps | Reports itself as |
|---|---|---|---|
| `baseline` | `27d398592b8a8c415c54eb04a0893aa9ef20316d`, unmodified | single-bit writes to `CCER` (`CC1E`, `CC1NE`), the register path the patch's methods take | `HQ175 boot image=baseline ...` |
| `patched` | the same revision with the patch applied | the patch's four methods | `HQ175 boot image=patched ...` |

The baseline's raw writes are the control: they show what the hardware does when
the bits are flipped, independently of any driver code, and the patched image
has to produce the same register transitions and the same pin behaviour through
the new methods, with getters that agree with `CCER`. Both images also run the
unchanged `enable`/`disable` pair. The images print raw measurements and never a
verdict.

The header of the crate's `Cargo.toml` and the module comment of `src/lib.rs`
name files of the validation job that produced the images
(`toolchain.cargo_image`, `reference/templates/README.md`, `tests/hq175/`,
`firmware/hq175/README.md`). Those belong to the pipeline that built and judged
them and are not part of this directory; the sources here are exactly the ones
the images were built from, so the comments are left as they are.

## The setup, all inside the chip

- Clocks: Embassy's default configuration leaves the F446 on its 16 MHz
  internal oscillator (HSI) without a PLL, and `rcc_cfgr` reads 0x0 (`SW` HSI,
  no AHB or APB prescaler), so the CPU, the DWT cycle counter, TIM1 and TIM8 all
  run at 16 MHz and one timer tick is 62.5 ns. The exact frequency enters no
  expected value: every count is a ratio of ticks of the same clock.
- Pins: TIM1 CH1 on PA8 and CH1N on PA7, alternate function 1, push-pull, very
  high speed, with the pins' internal pull resistors (`moder7=2 moder8=2 af7=1
  af8=1` in every `setup` record). Both pins reach only the headers of the
  board; nothing on the board drives them.
- PWM: `ComplementaryPwm::new` on TIM1 with CH1 and CH1N only. The constructor
  is asked for 100 kHz; the image then writes `PSC` 0 and `ARR` 158 directly,
  so the period is exactly 159 ticks (9.94 us, about 100.6 kHz), and reads both
  back. `set_duty(Ch1, 40)` makes `OC1REF` high for 40 of the 159 ticks
  (25.2 %), `set_dead_time(8)` inserts 8 ticks (500 ns) of dead time; the
  dead-time-0 sequences call `set_dead_time(0)`. The driver sets `MOE`;
  `OSSR`, `OSSI`, `OIS1` and `OIS1N` are 0 and the break input is off (`bdtr`
  0x8008: `MOE` and `DTG` 8; 0x8000 with dead time 0).
- Pull: before each repetition the pull of both pins is set, pull-down for an
  even repetition and pull-up for an odd one (`pull` 0 and 1 in the records). A
  pin the timer releases follows its pull, while a pin the timer holds low stays
  low under either; the difference between the two pulls is what tells a
  released output from one that is driven low, which no single pull could show.
- Sampler: TIM8 (`PSC` 0, `ARR` 15, `tim8_arr=15`) requests one DMA transfer
  per update, every 16 ticks, and DMA2 stream 1 on channel 7 (`TIM8_UP`) copies
  `GPIOA_IDR` as 16-bit words into a buffer of 1908 samples, in direct mode and
  without interrupts. 1908 x 16 = 30 528 ticks is exactly 192 PWM
  periods, and because 16 and 159 are coprime, 159 consecutive samples land on
  each of the 159 tick positions of the period once, so the 1908 samples cover
  every position exactly 12 times. The image counts the samples with PA8 high
  (`h1`), PA7 high (`h1n`) and both high (`both`), and the level changes between
  consecutive samples (`e1`, `e1n`). The buffer is filled with 0xFFFF before
  each measurement, so a sample the DMA did not write would count as both pins
  high, and the record carries the DMA's remaining transfer count (`left`) and
  its status flags (`flags`) as well.
- Self-checks in every record: `dur`, the DWT cycles from starting TIM8 to the
  transfer-complete flag, has to be 30 528 plus the few cycles around it; `e1`
  and `e1n` have to be 383 or 384 wherever a pin is driven, which is 192 periods
  of 159 ticks read off the pin itself. `hclk_hz` in the boot record is HCLK
  measured against 20 000 TIM8 ticks, the ratio of the two clocks the
  expectations assume.
- Registers: `CCER` is read before the call (`ccer0`) and after it (`ccer`),
  `BDTR` after it; `cc1e`, `cc1ne`, `cc1p`, `cc1np`, `moe`, `ossr`, `ossi` and
  `dtg` are their bits. After the call the image waits 2000 cycles (125 us,
  about 12 periods, far longer than the ~0.4 us a released pin needs to reach
  its pull's level) before the sampler starts.
- Console: USART2 on PA2/PA3, the ST-LINK's virtual COM port, 115200 baud, 8N1.
  Every byte is written to the data register after `TXE`, and every line ends
  with a wait for `TC` and 30 ms of silence, so the serial path never holds
  more than one line. A `pwm` line is about 215 bytes, 19 ms on the wire, and
  an image takes about 60 s. The driver is used only to set the port up. A panic
  prints `HQ175 fault kind=panic`.

## What one repetition does

Each image runs three sequences of 100 repetitions and prints a `setup` record
before each: `baseline` runs `raw8` (dead time 8), `raw0` (dead time 0) and
`legacy` (dead time 8); `patched` runs `api8`, `api0` and `legacy`. Every
sequence starts with both outputs off (`ccer=0x0` in its `setup` record).

1. The pull of PA7 and PA8 is set: down for an even repetition, up for an odd
   one.
2. For each step of the sequence, in order: `CCER` is read (`ccer0`); the
   step's call is made; `CCER` and `BDTR` are read; 2000 cycles pass; one
   measurement of 1908 samples is taken; the `pwm` record is printed; and in the
   first repetitions (two in `raw8`, `raw0` and `api8`, one in `api0` and
   `legacy`) the raw sample buffer follows as eight `trace` records.

| Sequences | Step | `baseline` does | `patched` does | `CCER` |
|---|---|---|---|---|
| `raw8`/`api8`, `raw0`/`api0` | `main_on` | sets `CC1E` | `set_main_output_enable(Ch1, true)` | 0x0 to 0x1 |
| | `comp_on` | sets `CC1NE` | `set_complementary_output_enable(Ch1, true)` | 0x1 to 0x5 |
| | `main_off` | clears `CC1E` | `set_main_output_enable(Ch1, false)` | 0x5 to 0x4 |
| | `comp_off` | clears `CC1NE` | `set_complementary_output_enable(Ch1, false)` | 0x4 to 0x0 |
| `legacy` | `enable` | `enable(Ch1)` | the same | 0x0 to 0x5 |
| | `disable` | `disable(Ch1)` | the same | 0x5 to 0x0 |

In `api8` and `api0` each record also carries the two getters, read after the
call (`g1`, `g1n`). `CCER` bit 0 is `CC1E` and bit 2 is `CC1NE`; bits 1 and 3
(`CC1P`, `CC1NP`) stay 0.

## The lines on USART2

Every line is `HQ175 <kind> key=value ...` at 115200 baud, 8N1, and the last
line is `HQ175 done`. `HQ175` is the tag of the validation job that produced
these images, and nothing more. Integers are decimal and never zero-padded,
registers are `0x`-prefixed hexadecimal. The console prints nothing else, so
there is no untagged output.

| Kind | Per image | Fields |
|---|---|---|
| `boot` | 1 | `image`, `tim_hz`, `hclk_hz`, `rcc_cfgr`, `reps`, `n`, `tick` |
| `setup` | 3, one per sequence | `seq`, `psc`, `arr`, `ccr1`, `cr1`, `cr2`, `ccer`, `bdtr`, `dtg`, `ckd`, `moe`, `ossr`, `ossi`, `ois1`, `oisn1`, `moder7`, `moder8`, `af7`, `af8`, `tim8_arr` |
| `pwm` | 1000 | `seq`, `rep`, `step`, `pull`, `ccer0`, `ccer`, `bdtr`, `cc1e`, `cc1ne`, `cc1p`, `cc1np`, `moe`, `ossr`, `ossi`, `dtg`, `n`, `dur`, `left`, `flags`, `h1`, `h1n`, `both`, `e1`, `e1n`, and in `api8`/`api0` `g1`, `g1n` |
| `trace` | 144 (`baseline`), 112 (`patched`) | `seq`, `rep`, `step`, `part`, `data` |
| `done` | 1 | none |
| `fault` | 0 | `kind` (`panic`); printed only by the panic handler |

In a `pwm` record every number is what the sampler counted or the register
held: `n` is the number of samples (1908), `dur` the DWT cycles
the measurement took, `left` the DMA's remaining transfers (0 when every sample
was written), `flags` DMA2's status bits for stream 1 (`LISR` bits 11:6 masked
to `TCIF`, `HTIF`, `TEIF`, `DMEIF` and `FEIF`: 0x30 is transfer complete and
half transfer with no transfer, direct-mode or FIFO error). `h1`, `h1n` and
`both` count samples with PA8 high, PA7 high and both high; `e1` and `e1n`
count the level changes of PA8 and PA7 between consecutive samples.

`tim_hz` in the boot record is the timer clock as the image reconstructs it from
the driver's integer `get_frequency()` (100 628 Hz) times the 159-tick period,
which is why it reads 15 999 852 and not 16 000 000; `hclk_hz` is HCLK measured
in the chip, 20 000 TIM8 ticks against the DWT counter.

`trace` carries the sample buffer as 477 bytes of hex in 8 parts of at most 64
bytes: four samples per byte from the least significant bit pair up, two bits
per sample, bit 0 the level of PA8 (CH1) and bit 1 the level of PA7 (CH1N).

## What the records were expected to show

The period is 159 ticks, `OC1REF` is high for 40 of them, and the 1908 samples
cover each tick position 12 times, so a pin that carries a waveform high for
`k` ticks of the period shows exactly `12 k` high samples, up to the alignment
of samples and edges. The window is 192 periods, so a driven pin shows 384
edges, of which the 1907 comparisons between consecutive samples (16 ticks
short of the window) see 383 or 384. A released pin follows its pull: 0 high
samples under pull-down, 1908 under pull-up, no edges either way.

| Step | `CC1E`/`CC1NE` | `OC1`, PA8 | `OC1N`, PA7 | `h1` | `h1n` | `both` | `e1` | `e1n` |
|---|---|---|---|---|---|---|---|---|
| `main_on` | 1/0 | `OC1REF`, no dead time: 40 ticks high | released | 480 | 0 (pull-down), 1908 (pull-up) | 0, 480 | 383 or 384 | 0 |
| `comp_on`, `enable` | 1/1, dead time 8 | `OC1REF` minus the dead time: 32 ticks high | its complement minus the dead time: 111 ticks high | 384 | 1332 | 0 | 383 or 384 | 383 or 384 |
| `comp_on` | 1/1, dead time 0 | `OC1REF`: 40 ticks | its complement: 119 ticks | 480 | 1428 | 0 | 383 or 384 | 383 or 384 |
| `main_off` | 0/1 | released | `OC1REF` itself, not inverted, no dead time: 40 ticks | 0, 1908 | 480 | 0, 480 | 0 | 383 or 384 |
| `comp_off`, `disable` | 0/0 | released | released | 0, 1908 | 0, 1908 | 0, 1908 | 0 | 0 |

The sampler reads the port a few bus cycles after the timer's request, with a
jitter of a cycle or two, and a cycle is a tick here; a sample that falls within
that jitter of an edge lands on either side of it, so a count may miss the exact
value by a few samples of 1908. In every record `ccer` has to be `ccer0` with
exactly the one bit of the step changed, `cc1e` and `cc1ne` have to be the bits
of `ccer`, and in `api8` and `api0` the getters `g1` and `g1n` have to equal
them; `n` 1908, `left` 0, `flags` 0x30 and `dur` a few cycles above 30 528 say
the measurement itself was complete.

## Results

[Run 38166475392](https://github.com/agentic-hil/agentic-hil/actions/runs/38166475392)
of the hardware-bench workflow on the branch `bench/hilval-hq175`, at commit
`5f5e0a77`, ran both plans on the Agentic HIL project's NUCLEO-F446RE with
Agentic HIL 0.21.4 and the bench configuration
`sha256:9e0b4615276e15b64107b9a0e09044c5c202265bbe5e337424c9b915ae67652c`, and
each image reached its `HQ175 done` line. Its evidence is in
[results/run-38166475392/](results/run-38166475392/), as the run uploaded it
(artifact `hilval-hq175-evidence`,
`sha256:19ace8dd6f4e6b0d06c63c0277bb02d69dae4a713ad6d4e8ade17f0225cf2992`),
with the probe's serial number and the machine's names withheld:
`reports/<image>.json` is the plan's report, every step with what Agentic HIL
did and saw, and `evidence/<image>/` holds the run summary and the OpenOCD and
serial logs the report names. `demo` is the demo firmware put back afterwards.
The reports record the SHA-256 of the plan they followed (`test_config_sha256`)
and of the file they flashed; those are the sums of [plans/](plans/) and
[firmware/SHA256SUMS](firmware/SHA256SUMS), so the plans and images in this
directory are byte for byte the ones the run used.

The `baseline` log holds 1149 tagged lines and the `patched` log 1117: one
`boot`, three `setup`, 1000 `pwm` (400 per four-step sequence, 200 for
`legacy`, `rep` 0 to 99 in order with every step of every repetition present),
144 or 112 `trace`, `HQ175 done`, and no untagged line. The tables below are
what [results/summarize.py](results/summarize.py) prints from those two logs; a
range is the minimum and maximum over the 50 records of a step under one pull.

**The boot and setup records**, identical in both images except for `image`:

| | value |
|---|---|
| `tim_hz`, `hclk_hz`, `rcc_cfgr` | 15 999 852, 15 994 259, 0x0 |
| `reps`, `n`, `tick` | 100, 1908, 16 |
| `psc`, `arr`, `ccr1` | 0, 158, 40 |
| `cr1`, `cr2`, `ccer` at the start of each sequence | 0x81 (`CEN`, `ARPE`), 0x0, 0x0 |
| `bdtr`, `dtg` | 0x8008, 8 in `raw8`, `api8` and `legacy`; 0x8000, 0 in `raw0` and `api0` |
| `ckd`, `moe`, `ossr`, `ossi`, `ois1`, `oisn1` | 0, 1, 0, 0, 0, 0 |
| `moder7`, `moder8`, `af7`, `af8` | 2, 2, 1, 1 |
| `tim8_arr` | 15 |

**The four-step sequences**, `raw8` and `raw0` of `baseline`, `api8` and `api0`
of `patched`, 100 repetitions each:

| Step | `ccer0` to `ccer` | `cc1e`, `cc1ne` (and `g1`, `g1n` in `api8`/`api0`) | pull-down: `h1`, `h1n`, `both` | pull-up: `h1`, `h1n`, `both` | `e1`, `e1n` |
|---|---|---|---|---|---|
| `main_on`, dead time 8 and 0 | 0x0 to 0x1 | 1, 0 | 479 to 481, 0, 0 | 479 to 481, 1908, 479 to 481 | 383 to 384, 0 |
| `comp_on`, dead time 8 | 0x1 to 0x5 | 1, 1 | 383 to 386, 1330 to 1333, 0 | 383 to 386, 1330 to 1333, 0 | 383 to 384, 383 to 384 |
| `comp_on`, dead time 0 | 0x1 to 0x5 | 1, 1 | 479 to 481, 1427 to 1429, 0 | 479 to 481, 1427 to 1429, 0 | 383 to 384, 383 to 384 |
| `main_off`, dead time 8 and 0 | 0x5 to 0x4 | 0, 1 | 0, 479 to 481, 0 | 1908, 479 to 481, 479 to 481 | 0, 383 to 384 |
| `comp_off`, dead time 8 and 0 | 0x4 to 0x0 | 0, 0 | 0, 0, 0 | 1908, 1908, 1908 | 0, 0 |

The ranges are the union over the sequences a row names. The per-sequence
ranges differ in two cells only, both in `comp_on` with dead time 8: `h1` under
pull-up is 383 to 385 in `raw8` and 383 to 386 in `api8`, and `h1n` under
pull-down is 1330 to 1333 in `raw8` and 1331 to 1333 in `api8`. Every other cell
is the same, range for range, between the raw register writes of `baseline` and
the methods of `patched`. In `api8` and `api0`, `g1` and `g1n` equal `cc1e` and
`cc1ne` in every one of the 800 records.

**The `legacy` sequence**, `enable`/`disable` with dead time 8, 100 repetitions
in each image:

| Step | `ccer0` to `ccer` | `cc1e`, `cc1ne` | pull-down: `h1`, `h1n`, `both` | pull-up: `h1`, `h1n`, `both` | `e1`, `e1n` |
|---|---|---|---|---|---|
| `enable`, `baseline` | 0x0 to 0x5 | 1, 1 | 383 to 385, 1330 to 1333, 0 | 383 to 386, 1331 to 1333, 0 | 383 to 384, 383 to 384 |
| `enable`, `patched` | 0x0 to 0x5 | 1, 1 | 383 to 386, 1331 to 1333, 0 | 383 to 386, 1331 to 1333, 0 | 383 to 384, 383 to 384 |
| `disable`, both images | 0x5 to 0x0 | 0, 0 | 0, 0, 0 | 1908, 1908, 1908 | 0, 0 |

In all 2000 `pwm` records: `n` 1908, `left` 0, `flags` 0x30, `cc1p` 0,
`cc1np` 0, `moe` 1, `ossr` 0, `ossi` 0, `dtg` as the sequence's `setup` says,
and `dur` 30 534 to 30 547, 30 536 in nearly all (1.9085 ms: the 30 528 ticks
of the window plus the cycles between starting the timer and reading the flag).
The 18 traced measurements of `baseline` and the 14 of `patched` each hold 477
bytes, 1908 samples; recounting `h1`, `h1n`, `both`, `e1` and `e1n` from the
samples gives the values the image printed, in all 32.

In words. With one output enabled, the timer drove that pin with `OC1REF`, 40 of
159 ticks high and without dead time, and released the other pin, which followed
its pull: 480 high samples of 1908 on the driven pin, 0 or 1908 on the released
one, through raw `CCER` writes in `baseline` and through the new methods in
`patched` alike. A lone complementary output carried `OC1REF` itself, not its
inverse. With both outputs enabled, the main output's high time shrank by the
dead time (384 samples with 8 ticks of dead time, 480 without) and the
complementary output carried the inverse minus the dead time (1332 and 1428),
and no sample saw both pins high. The register moved exactly one bit per step,
the getters agreed with the register in every record, and `enable`/`disable`
behaved the same in both images. Every value is within the windows of the table
above, and every self-check held in every record.

### Reproducing the tables

```bash
python3 results/summarize.py \
  baseline=results/run-38166475392/evidence/baseline/logs/com-20261011T190443725Z-dut_uart.jsonl \
  patched=results/run-38166475392/evidence/patched/logs/com-20261011T190546848Z-dut_uart.jsonl
```

The script joins the received bytes of each log, keeps the tagged lines, prints
the `boot`, `setup` and `done` lines as they are and one row per sequence, step
and pull with every field as a single value or as `min..max`, and recounts every
`trace` buffer against the record it belongs to.

## The test plans

Each plan in [plans/](plans/) flashes its image without a reset, opens the
ST-LINK's virtual COM port with a cleared buffer, resets the board and records
the port until a line is exactly `HQ175 done`, with a 120 s limit, and closes
the port. The plans judge only that the image ran to its last line; every value
above was judged from the recorded lines afterwards. The plans were written for
and checked with Agentic HIL 0.21.4.

## Running the plans

With your own NUCLEO-F446RE and an Agentic HIL configuration that
`agentic-hil init` wrote in [examples/nucleo-f446re_demo](..), binding `dut` to
the ST-LINK and `dut_uart` to its virtual COM port, run from that directory:

```bash
mkdir -p build/hilval-hq175
cp embassy-3710/firmware/*.elf build/hilval-hq175/
(cd build/hilval-hq175 && sha256sum -c ../../embassy-3710/firmware/SHA256SUMS)
agentic-hil test-reactor --test-config embassy-3710/plans/baseline.testconfig.yaml
agentic-hil test-reactor --test-config embassy-3710/plans/patched.testconfig.yaml
```

The plans flash from `build/hilval-hq175/`, which lies inside the artifact
roots Agentic HIL allows by default. Each image runs for about 60 s. The serial
log of each run is the `com-*-dut_uart.jsonl` its report names, and
`results/summarize.py` reads it as above.

Without Agentic HIL: flash an image with the tool of your choice, open the
virtual COM port at 115200 baud, 8N1, reset the board and read until
`HQ175 done`.

## Building the images

[firmware/](firmware/) holds the two images as built, stripped of debug
information with their symbol tables kept, and their
[SHA-256 sums](firmware/SHA256SUMS). They were built from:

- Embassy `27d398592b8a8c415c54eb04a0893aa9ef20316d` as path dependencies
  (`embassy-stm32` with the features `stm32f446re`, `unstable-pac`, `memory-x`,
  `time-driver-tim4`, `exti` and `rt`; `embassy-executor`; `embassy-time`),
  unmodified for `baseline` and with
  [embassy-independent-outputs.patch](embassy-independent-outputs.patch) applied
  for `patched`.
- `stm32-metapac` from stm32-data-generated
  `b99bf17d58e6a8d385faf4b87b5f310ca2c057a0` (tag
  `stm32-data-37a22f31552ba1fd29b3ef192c4578b84abee6e1`, the one that Embassy
  revision pins), supplied through the `[patch]` section of
  [embassy/Cargo.toml](embassy/Cargo.toml).
- `cortex-m` 0.7.9 and `cortex-m-rt` 0.7.7 from crates.io, and every other
  crate at the version [embassy/Cargo.lock](embassy/Cargo.lock) records.
- rustc 1.97.0 (2d8144b78 2026-07-07) for `thumbv7em-none-eabihf`, and GNU
  objcopy from GNU Tools for STM32 14.3.rel1 (binutils 2.44) for the strip.

For each image, from a copy of [embassy/](embassy/) in which `@EMBASSY@` and
`@STM32_DATA@` in `Cargo.toml` are replaced by the paths of the two checkouts
(the unmodified Embassy checkout for `baseline`, the patched one for `patched`;
the `patched` binary does not compile against the unmodified tree):

```bash
cargo build --release --offline --target thumbv7em-none-eabihf --bin baseline
arm-none-eabi-objcopy --strip-debug target/thumbv7em-none-eabihf/release/baseline firmware/baseline.elf
```

`--offline` presumes a `cargo fetch` with the lock file beforehand. The release
profile keeps full debug information in the build (`debug = 2`, which the strip
removes) and optimises for size (`opt-level = "s"`); `build.rs` passes
`--nmagic` and `-Tlink.x` to the linker. The build sets no rustflags other than
`--remap-path-prefix` for the crate copy, the two checkouts, Cargo's registry
and git caches and the target directory (to `/crate`, `/src/embassy`,
`/src/stm32_data`, `/cargo/registry`, `/cargo/git` and `/target`), so the images
carry no path of the machine that built them. `Cargo.lock` is the one the build
resolved, identical for both images. Byte-for-byte reproducibility of a rebuild
has not been checked.

## How this was made

This report is part of the Agentic HIL project's upstream work, and we maintain
Agentic HIL. AI coding agents, run by our development pipeline, wrote the
patch, the validation firmware, the test suite that judges the recorded lines,
this README and the pull request, and built the images; that suite also
generated the plans. Agentic HIL carried out every step on the board: it
flashed each image, reset the board and recorded the serial port until the
firmware's last line, and no step reached the board any other way. The suite
then asserted every value of the tables above against the recorded lines, and
`results/summarize.py` reproduces the tables from the logs. Our maintainer
approved submitting this report and the pull request; the check that the run
passed and that the rows above match the logs was made by the agent that
operates the pipeline, and no person has reviewed the patch, the firmware, the
test suite or the analysis.

## Licensing

The crate in [embassy/](embassy/), the plans and the scripts are Apache-2.0,
like this repository. The patch is a change to Embassy and is offered under
Embassy's terms, MIT OR Apache-2.0. The images also contain Embassy,
`stm32-metapac`, `cortex-m` and further crates under their own terms; see
[firmware/THIRD_PARTY_NOTICES.md](firmware/THIRD_PARTY_NOTICES.md).
