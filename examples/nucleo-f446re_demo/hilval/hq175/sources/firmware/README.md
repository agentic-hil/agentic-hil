# hq175: independent main and complementary PWM outputs on TIM1 (Embassy #3710)

Two validation images for a NUCLEO-F446RE (STM32F446RE) and the upstream change
they check. They ran on the board through
[Agentic Hardware-in-the-Loop (Agentic HIL)](https://github.com/agentic-hil/agentic-hil),
which flashed each image, reset the board with the serial port open and recorded
everything the image printed on USART2.

The images print measurements only: counts, durations, register words and raw
sample buffers. Nothing here decides whether a measurement is right; the tests in
`tests/hq175/` do that, and they are what a reader should read next to this file.

## What is checked

[embassy-rs/embassy#3710](https://github.com/embassy-rs/embassy/issues/3710) asks
for a way to enable a timer channel's main output (`CCxE`) and its complementary
output (`CCxNE`) independently. In Embassy revision
`27d398592b8a8c415c54eb04a0893aa9ef20316d`, `ComplementaryPwm::enable(channel)`
and `disable(channel)` always switch both.

`embassy-independent-outputs.patch` adds four methods to `ComplementaryPwm` and
changes nothing that exists:

| Method | What it does |
|---|---|
| `set_main_output_enable(channel, enable)` | Writes `CCxE` only; `CCxNE` is left as it is |
| `set_complementary_output_enable(channel, enable)` | Writes `CCxNE` only; `CCxE` is left as it is |
| `get_main_output_enable(channel)` | Reads `CCxE` back |
| `get_complementary_output_enable(channel)` | Reads `CCxNE` back |

The last one needs a read-back primitive `timer/low_level.rs` did not have (it had
one for `CCxE` only), so the patch adds `get_complementary_channel_enable_state`
there as well. `enable()`, `disable()` and the `embedded_hal_02::Pwm` impl are
untouched and still switch both outputs of a channel together.

The method names are this job's choice; the names sketched in the upstream issue
are not an agreed API. "main"/"complementary" is the vocabulary the same file
already uses for `set_main_polarity` / `set_complementary_polarity`.

What the hardware does when only one of the two outputs is enabled (RM0390's
output-control table, with `MOE = 1` and `OSSR = 0`) is part of the API's meaning,
so the doc comments state it and the `baseline` image measures it:

| `CC1E` | `CC1NE` | CH1 (PA8) | CH1N (PA7) |
|---|---|---|---|
| 1 | 1 | `OC1REF` with dead time | inverse of `OC1REF`, with dead time |
| 1 | 0 | `OC1REF`, no dead time | released by the timer |
| 0 | 1 | released by the timer | `OC1REF` itself, not inverted, no dead time |
| 0 | 0 | released by the timer | released by the timer |

## The two images

| Image | Built against | Shows |
|---|---|---|
| `baseline` | the unmodified Embassy checkout | today's behaviour, and the hardware's own answer |
| `patched` | a copy with `embassy-independent-outputs.patch` applied | the four new methods |

Both are binaries of one crate (`embassy/`) and share `embassy/src/lib.rs`, the
measurement rig, so they print the same records in the same order and differ only
in which call they make for each step. `cargo build --bin baseline` never compiles
`src/bin/patched.rs`, which is what lets `baseline` build against the untouched
upstream revision: it could not compile the calls to the new methods.

Both images set up TIM1 the same way:

* TIM1_CH1 on PA8 and TIM1_CH1N on PA7, both alternate function 1, push-pull, with
  an internal pull.
* `PSC = 0`, `ARR = 158`: a period of **159 timer ticks**. `CCR1 = 40`, PWM mode 1:
  `OC1REF` is high for **40 of the 159 ticks**. Dead time **8 ticks**
  (`set_dead_time(8)`, which maps to `CKD = Div1` with `DTG = 8`) or **0 ticks**.
* `MOE = 1`, `OSSR = OSSI = 0`, `OIS1 = OIS1N = 0`, so an output the timer does not
  drive is released rather than held at an off-state level.

The period is written to `PSC` and `ARR` directly after the driver is constructed,
because every expectation is a ratio of ticks of a 159-tick period while
`set_frequency` can only land on a period the registers happen to represent for
the clock the timer runs at. Both registers are printed back in the `setup` record
and the tests assert them, so a wrong period fails as a set-up error and not as
the API's.

Each image runs three sequences of 100 repetitions:

| Image | Sequence | Dead time | Each step is |
|---|---|---|---|
| `baseline` | `raw8` | 8 ticks | one bit of `TIM1_CCER` written directly |
| `baseline` | `raw0` | 0 ticks | one bit of `TIM1_CCER` written directly |
| `baseline` | `legacy` | 8 ticks | `enable(Ch1)` / `disable(Ch1)` |
| `patched` | `api8` | 8 ticks | one of the two new setters, with both getters read back |
| `patched` | `api0` | 0 ticks | one of the two new setters, with both getters read back |
| `patched` | `legacy` | 8 ticks | `enable(Ch1)` / `disable(Ch1)` |

The four-step sequences switch one output per step, `main_on`, `comp_on`,
`main_off`, `comp_off`, so each step has an output that must not change and the
four states (main only, both on, complementary only, both off) are all reached.
`raw8` and `raw0` are the controls: no driver code sits between the register write
and the pins, so what those sequences measure is the board's own behaviour, which
is what every expectation about the new API is compared against.

The pins' pull alternates between pull-down (even repetitions) and pull-up (odd
ones). An output the timer has released follows its pull, so it reads 0 under one
and 1 under the other; an output that is still driven, or one the timer holds low,
cannot do that. One pull alone could not tell the two apart.

## How a pin is measured, all inside the chip

Nothing is wired to the board and nothing outside it measures anything:

* **Instead of a logic analyser or an oscilloscope on PA8 and PA7**: TIM8 is set to
  one update event every **16 timer ticks** and DMA2 stream 1 (channel 7, TIM8_UP)
  copies `GPIOA_IDR` into RAM on each of them, **1908 samples** per measurement.
  PA7 and PA8 are both in GPIOA, so one read catches both outputs at the same
  instant. The image then counts high samples, samples with both pins high, and
  sample-to-sample changes.
* **Instead of external pull resistors**: the pins' own pull-up and pull-down, set
  in `GPIOA_PUPDR` beside the alternate function.
* **Instead of a debugger reading registers**: `TIM1_CCER` is read immediately
  before and immediately after every call and `TIM1_BDTR` after it, and both words
  are printed with the fields decoded beside them.
* **Instead of host timestamps**: the core's cycle counter (DWT CYCCNT) times every
  transfer, and HCLK is measured against the timer clock in-chip by counting TIM8
  ticks and HCLK cycles over one interval.

Why 159 ticks and 16: `gcd(16, 159) = 1`, so 159 consecutive samples visit every
tick offset of the PWM period exactly once, and 1908 = 12 x 159 samples sweep the
whole period twelve times. A high fraction is then a duty cycle, with the expected
count an exact multiple: 12 x 40 = 480 samples high for a lone output, 12 x 32 =
384 for the main output with 8 ticks of dead time. 1908 samples 16 ticks apart span
exactly 192 whole periods, so a driven pin must show exactly 384 edges. No sampled
interval is shorter than the 16-tick interval, so no edge can be missed or counted
twice.

No motor, power stage, load or break input was connected, and none was simulated.
Nothing here says anything about shoot-through in a half bridge, about break
inputs or about a power stage: the only claim is what the two pins did, as the
samples show them, and what the registers read back.

## The records

Every line is `HQ175 <kind> key=value ...` on USART2 (PA2/PA3, 115200 baud 8N1),
and the last line is `HQ175 done`. Untagged lines are ignored by the reader.

| Kind | When | Fields |
|---|---|---|
| `boot` | once, first | `image`, `tim_hz`, `hclk_hz`, `rcc_cfgr`, `reps`, `n`, `tick` |
| `setup` | once per sequence | `seq`, `psc`, `arr`, `ccr1`, `cr1`, `cr2`, `ccer`, `bdtr`, `dtg`, `ckd`, `moe`, `ossr`, `ossi`, `ois1`, `oisn1`, `moder7`, `moder8`, `af7`, `af8`, `tim8_arr` |
| `pwm` | after every call | `seq`, `rep`, `step`, `pull`, `ccer0`, `ccer`, `bdtr`, `cc1e`, `cc1ne`, `cc1p`, `cc1np`, `moe`, `ossr`, `ossi`, `dtg`, `n`, `dur`, `left`, `flags`, `h1`, `h1n`, `both`, `e1`, `e1n`, and `g1`, `g1n` in `api8`/`api0` |
| `trace` | the traced steps | `seq`, `rep`, `step`, `part`, `data` |
| `done` | once, last | none |
| `fault` | only after a panic | `kind` |

In a `pwm` record: `pull` is 0 for pull-down and 1 for pull-up; `ccer0` is
`TIM1_CCER` before the call and `ccer` after it; `n` is the number of samples;
`dur` is the transfer's length in HCLK cycles; `left` is what was still
outstanding in the DMA stream's `NDTR` (0 after a complete transfer); `flags` is
`(DMA2_LISR >> 6) & 0x3F`, stream 1's five flags moved down to bit 0, so bit 5 is
transfer-complete, bit 4 half-transfer and bits 3, 2 and 0 the transfer, direct
mode and FIFO errors; `h1`/`h1n` are the samples in which CH1/CH1N read high,
`both` those in which both did, and `e1`/`e1n` the sample-to-sample changes of each
pin. `g1`/`g1n` are what the two new getters returned.

`trace` records carry the raw sample buffer of one measurement so a reader (and
the tests) can count it again instead of trusting the image's own counting. The
1908 samples are packed two bits each, bit 0 for PA8 and bit 1 for PA7, four
samples per byte with sample `4j+i` in bits `2i..2i+1` of byte `j`: 477 bytes, sent
as 8 parts of up to 64 bytes, two lowercase hex digits per byte, high nibble first.
The first two repetitions of `raw8`, `raw0` and `api8` are traced (so both pulls
are covered) and the first repetition of `api0` and of `legacy`.

## Rebuilding without our tools

The images are plain Cargo builds; `hilval/hq175/build.py` only arranges the
checkouts and keeps the output out of the repository.

1. Check out `embassy-rs/embassy` at `27d398592b8a8c415c54eb04a0893aa9ef20316d`
   and `embassy-rs/stm32-data-generated` at
   `b99bf17d58e6a8d385faf4b87b5f310ca2c057a0`.
2. For the patched image, copy the Embassy checkout and apply the patch in the
   copy: `git apply embassy-independent-outputs.patch` (it is a unified diff with
   `a/` and `b/` prefixes, relative to the checkout's root).
3. In a copy of `embassy/Cargo.toml`, replace every `@EMBASSY@` with the path of
   the Embassy checkout to build against (the unmodified one for `baseline`, the
   patched copy for `patched`) and every `@STM32_DATA@` with the path of the
   `stm32-data-generated` checkout.
4. `cargo build --release --target thumbv7em-none-eabihf --bin baseline` and, with
   `@EMBASSY@` pointing at the patched copy,
   `cargo build --release --target thumbv7em-none-eabihf --bin patched`.

Flash either ELF to a NUCLEO-F446RE and read USART2 at 115200 baud 8N1 (the
ST-LINK's virtual COM port). The image prints its records and ends with
`HQ175 done`; it needs nothing from the host and no pin connected.

## Licences

Embassy is MIT OR Apache-2.0, and the patch in this directory is offered under the
same terms. `THIRD_PARTY_NOTICES.md` names every third-party component the images
contain.
