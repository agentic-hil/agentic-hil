# The embassy-stm32 SPI clock selection on a NUCLEO-F446RE

A hardware report on how `embassy-stm32` turns `spi::Config::frequency` into an
SPI clock divider. It measures the SCK pin of an STM32F446RE with the
unmodified crate at Embassy `c70760de42e2c8103138da6ff91626ff5a2688ba` and with
the change that the accompanying pull request to embassy-rs/embassy proposes,
applied as a patch to the same revision. Both images run the same firmware;
only the Embassy tree they are built against differs.

The runs are driven by [Agentic Hardware-in-the-Loop (Agentic HIL)](https://github.com/agentic-hil/agentic-hil):
it uploaded each image, ran the plan in [plans/](plans/) that flashes the board
and resets it, and carried every byte between the board's serial port and the
records in [bundles/](bundles/), where the results of those tool calls are kept
as receipts. Apart from one jumper wire fitted by hand before the runs, nothing
reached the board any other way.

## What the change does

`compute_baud_rate(kernel_clock, freq)` in `embassy-stm32/src/spi/mod.rs`
selects the divider 2, 4, ..., 256 (the `BR` field) from the integer quotient
of the kernel clock `K` and the request `f`. At the pinned revision it is a
table:

| `K / f` (integer) | Divider |
|---|---|
| 0 | `Err(FrequencyTooHigh)` |
| 1 to 2 | 2 |
| 3 to 5 | 4 |
| 6 to 11 | 8 |
| 12 to 23 | 16 |
| 24 to 39 | 32 |
| 40 to 95 | 64 |
| 96 to 191 | 128 |
| 192 and above | 256 |

Six of its seven upper bounds are 3 x 2^n - 1, the last quotient below the
midpoint between two dividers, so the table rounds to the nearest divider; the
bound between /32 and /64 is 39 where that pattern gives 47. The constructors
unwrap the result (`unwrap!(compute_baud_rate(...))`), `set_config` returns it
with `?`. What follows from that, all of it visible in the results below:

- SCK runs above the request for 7 of the 28 requests measured here that a
  divider can meet, by up to 49.2 %: 20 942 Hz runs at 31 250 Hz, 42 105 Hz at 62 500 Hz, 173 913 Hz at
  250 000 Hz (+43.8 %), 363 636 Hz at 500 000 Hz (+37.5 %), 1 500 000 Hz at
  2 000 000 Hz (+33.3 %), 800 000 Hz at 1 000 000 Hz (+25 %) and 102 564 Hz at
  125 000 Hz (+21.9 %). An SPI device specifies a maximum clock, and a bus that
  runs half as fast again as the configured value can exceed it.
- Below the slowest divider a request still runs at `K / 256`: 15 000 Hz runs
  at 15 625 Hz (+4.2 %) without an error.
- Above `K` the constructors panic, while `set_config` returns
  `FrequencyTooHigh` and keeps the previous divider, so the same `Config` is a
  panic on one path and an error on the other.
- A request of 0 Hz divides by zero (from the code; not measured).

The patch,
[patches/hq356-embassy-stm32-spi-clock-contract.diff](patches/hq356-embassy-stm32-spi-clock-contract.diff),
changes `embassy-stm32/src/spi/mod.rs` and `embassy-stm32/CHANGELOG.md` and
nothing else. `Config::frequency` becomes the highest SCK frequency the bus may
run at: `compute_baud_rate` returns the smallest divider whose SCK does not
exceed the request, compared without rounding (`K <= f << (BR + 1)` in 64
bits). A request at or above `K / 2` gets `K / 2`. A request below `K / 256`
cannot be met and returns the new `ConfigError::FrequencyTooLow`. `set_config`
now checks the frequency before it changes anything, so a rejected config
leaves the driver unchanged; the constructors are not changed and, unwrapping
the result as before, panic on such a request. `FrequencyTooHigh` stays in the
`#[non_exhaustive]` enum, documented as no longer returned. The doc comment of
`Config::frequency` and the CHANGELOG say all of this. The patch applies to
`c70760de` with `git apply`: its pre-image blobs (`769c2dd8e` for the
CHANGELOG, `9eb8bd01a` for `spi/mod.rs`) are the files at that commit.

Two smaller changes were considered. Correcting only the bound 39 to 47 keeps
rounding to the nearest divider, which still runs up to almost 50 % above a
request. Rounding to the nearest frequency instead runs up to a third above.
Rounding the divider up is the only one of the three that keeps SCK at or below
the request; embassy-rp's SPI driver rounds its prescaler divisions up in the
same way (`calc_prescs`).

## Coverage, and what is not claimed

- Clock ratios only. The SPI kernel clock and TIM8, which timestamps the SCK
  edges, run from the same PLL, so every measurement is a ratio of the two. The
  absolute frequency of the internal oscillator, and with it of SCK in hertz,
  is not measured; the hertz figures assume the nominal 16 MHz.
- One chip and one register path: an STM32F446RE, whose SPI takes the divider
  in `CR1.BR`. `spi_v4` to `spi_v6` (for example the STM32H7) take it in
  `CFG1.MBR` through the same function; they are not measured.
- One kernel clock, `K` = 4 MHz, chosen so that all eight dividers, 2 MHz down
  to 15 625 Hz, can be timed, and 29 requests: each exact divider, both sides
  of every bound of the old table, 15 000 Hz below `K / 256`, and `K` and
  `K + 1` at the top. [cases.json](cases.json) names the class of each.
- The 88 `set_config` rows are the rounding evidence for the change. The
  constructors unwrap the same `compute_baud_rate`, which the patch leaves
  them calling, so they select the same divider wherever `set_config` accepts;
  that is from the code. On the board, the patched constructor ran for the
  three starting constructions at 1 MHz, the recovery construction at 1 MHz
  and `construct/0`, which asks for 15 000 Hz, below `K / 256`, and panicked
  as the patch intends. That panic ends the boot in the frozen order, so
  `construct/1` to `construct/28` were not reached after the change; they are
  listed as not reached and count neither for nor against it.
- After a rejected `set_config` only the divider is shown to be unchanged, in
  `CR1` and on the pin. That the rejected call also leaves the GPIO speed and
  the CRC setting alone is from the code, not measured.
- A request of 0 Hz is not measured.
- SPI2 only, blocking, transmit only, mode 0, through `blocking_write`; the
  asynchronous and DMA paths are not exercised. MOSI is driven but not
  checked: the capture path sees SCK alone, so `data_bit_errors` stays empty
  in the tables.
- Each image ran once.

## The two images

| Image | Embassy tree | SHA-256 of the ELF | Size (bytes) | `build_id` | `embassy_rev` in the BOOT record |
|---|---|---|---|---|---|
| before | `c70760de42e2c8103138da6ff91626ff5a2688ba`, unmodified | `9216c2e50d111b365b84830e7923c74fa20a9a6f81ec5ddbb056437baf9cd4b3` | 520 700 | `00ee5edf` | `c70760de42e2c8103138da6ff91626ff5a2688ba` |
| after | the same revision with the patch applied | `81dd772b8570440f4dc5bb1676bf255efc9c3b313c9c083f5116f607444b4b69` | 520 068 | `c780d976` | `c70760de42e2c8103138da6ff91626ff5a2688ba+patch.5f485e98008fdf27` |

Both are built from the firmware sources in [firmware/](firmware/);
[variants/after/](variants/after/) only points the same sources at an Embassy
checkout with the patch applied. The `+patch.` suffix is the first 16 hex digits
of the patch's SHA-256, which the build script writes into the image, so the
after image names the patch it was built with. The images are not included in
this directory; each bundle records their SHA-256 and size as built, uploaded
and flashed.

`hq184-fw` (the crate), `HQ184` (the tag of every console line) and `hq356`
(in the patch's file name) are tags of the validation jobs that produced this
report, and nothing more. Comments in the firmware, the build script,
analyze.py, measurement.json and the bundle indexes cite those jobs' internal
issue numbers and files of the pipeline that ran them, such as
`finalize_bundle.py`. Those files are not part of this directory, and the files
here are byte for byte the ones that were built, run and evaluated, so the
comments are left as they are. Where the firmware's comments call the SPI
driver unmodified, that holds for the before image; the after image is the same
sources built against the patched tree.

## The setup, all inside the chip

- Board: a NUCLEO-F446RE (STM32F446RET6). Its on-board ST-LINK/V2-1 flashes it
  (Agentic HIL's OpenOCD backend) and carries the console on its virtual COM
  port. One jumper wire joins CN10-25 (PB10) to CN10-19 (PC7); nothing else is
  connected.
- Clocks: the 16 MHz internal oscillator (HSI) feeds the PLL with M 16, N 256
  and P 4, so SYSCLK and HCLK are 64 MHz; APB1 is divided by 16 and APB2 by 1.
  The SPI2 kernel clock `K` is PCLK1, 4 MHz; TIM8 on APB2 runs at 64 MHz; TIM2
  on APB1 runs at 8 MHz (`TIMPRE` 0). Each boot prints the raw registers,
  `cfgr=0x00001c0a pllcfgr=0x00014010 dckcfgr=0x00000000`, and analyze.py
  derives every clock from them rather than from the hertz values the firmware
  prints alongside.
- SPI: SPI2 through `Spi::new_blocking_txonly` with SCK on PB10 (AF5) and MOSI
  on PB15, `Config::default()` except `frequency`: mode 0, MSB first. `CR1` is
  0x0344 (`MSTR`, `SPE`, `SSI`, `SSM`) plus `BR` in bits 5:3.
- Capture path: TIM8 runs free at 64 MHz (`PSC` 0, `ARR` 0xFFFF). Its channel 2
  on PC7 (AF3) latches the counter on every rising edge, with an input filter of
  8 timer clocks (`IC2F` 0011), and DMA2 stream 3 channel 7 copies the 32
  `CCR2` timestamps of one frame (4 bytes of 8 rising edges each) to RAM. It is
  armed around each frame the driver sends; a frame whose timestamps have not
  all arrived within 100 ms counts as a timeout.
- Qualification of the capture path, in every boot: TIM2 channel 3 drives PB10
  (AF1) with a PWM of `K / 2^(N+1)` for N = 0 to 7, the SCK frequency of each
  divider, and the capture path records 64 rising edges of each (`ICQUAL`).
  Every interval has to be within 1 tick of 32 x 2^N TIM8 ticks; in both boots
  of both runs every interval was exactly that. A row counts only on the
  capture configuration of its divider that qualified in its own boot.
- Pad sampler: the setup block also calibrates and qualifies a GPIO sampler
  from an earlier stage of this work (`CAL`, `CALISR`, `QUAL`: TIM8 update DMA
  of `GPIOB_IDR`). No row uses it, and no claim rests on it. analyze.py
  evaluates its records into the `qualification` list of the evaluation files:
  in both boots of both runs, its configurations `cfg0` and `cfg1` passed no
  step and `cfg2` to `cfg7` none beyond step 3 (`highest_passing_step`).
- Console: USART2 on PA2 (TX) and PA3 (RX), the ST-LINK's virtual COM port,
  115200 baud, 8N1.

## What one row is

A row is one applied configuration, identified by sequence, position and case.
The firmware applies the request through the constructor or `set_config`, reads
back `get_current_config().frequency` and the raw `CR1`, and prints an `OCC`
record with the path, the outcome (`accepted`, or `rejected` with the error's
name) and both readbacks. It then sends 20 frames of 4 bytes through
`blocking_write`, with the capture path armed around each frame, and prints one
`XSUM` record that summarises the 20.

- Payload: the seed of a frame is the FNV-1a-32 hash of
  `184|<sequence>|<position>|<case>|<transfer>` (a hash of 0 becomes 1), and
  its 4 bytes are the low bytes of four xorshift32 steps (13, 17, 5) from that
  seed. `XSUM` carries the FNV-1a-32 hash of the 20 seeds, written as eight
  lowercase hex digits each and joined by `|` (`seeds`), which analyze.py
  recomputes.
- A frame is clean when the DMA copied every timestamp (`ndtr` 0), there was no
  DMA error, overcapture or timeout, the driver returned `Ok`, and `BR` is still
  the divider of the row's first frame. `XSUM` counts each condition.
- Byte span: for each byte of a clean frame, the sum of the seven differences
  between its eight rising-edge timestamps, each taken mod 2^16. That is exactly
  seven SCK periods, whatever gaps the driver leaves between bytes and frames.
  `XSUM` lists the spans as a multiset (`spans=<ticks>:<count>`), 80 per row.
- Interval: each span `s` admits the SCK frequencies
  `7 x 64 MHz / (s + 1)` to `7 x 64 MHz / (s - 1)`; the row's interval is the
  intersection over all its spans.
- A row is `observed_pass` when its capture configuration qualified in its own
  boot, at least 20 frames are valid, the interval contains `K / 2^(BR+1)` for
  the `BR` in the reported `CR1`, and `get_current_config()` reports exactly that
  frequency. A row whose case panicked is `observed_panic`; a row the firmware
  never got to is `not_reached`.

## The lines on USART2

Every line is `HQ184 <KIND> seq=<n> key=value ... crc32=<8 hex digits>`, where
`crc32` is the CRC-32 (as in zlib) of the line up to the space before `crc32`.
`seq` counts the lines of one boot. The console prints nothing else, and both
runs received no untagged line.

| Kind | Before | After | Fields |
|---|---|---|---|
| `BOOT` | 1 | 1 | `build_id`, `embassy_rev`, `cases_sha256`, `measurement_sha256`, `rustc` |
| `RCC` | 2 | 2 | `cfgr`, `pllcfgr`, `dckcfgr`, `sysclk_hz`, `hclk_hz`, `pclk1_hz`, `pclk2_hz` |
| `CAL` | 2 | 2 | `cfg`, `runs`, `failed`, `overhead_cycles`, `min_cycles`, `max_cycles` (pad sampler) |
| `CALISR` | 2 | 2 | `entries`, `spurious`, `sr`, `cnt` (pad sampler) |
| `QUAL` | 80 | 80 | `cfg`, `step`, `psc`, `arr`, `cycles`, `samples`, `ndtr`, `dma_err`, `timeout`, `data` (pad sampler) |
| `ICQUAL` | 16 | 16 | `cfg`, `pwm_psc`, `pwm_arr`, `captures`, `ndtr`, `dma_err`, `overcapture`, `timeout`, `min_ticks`, `max_ticks`, `data` (the 64 timestamps, base64 of little-endian 16-bit words) |
| `BLOCK-END` | 30 | 23 | `n` |
| `OCC` | 120 | 92 | `sequence`, `position`, `case`, `path`, `f_req`, `outcome`, `cr1`, `reported_hz`, `error` |
| `XSUM` | 120 | 92 | `sequence`, `position`, `case`, `cfg`, `transfers`, `clean`, `ndtr`, `dma_err`, `overcapture`, `timeout`, `drv_err`, `cfg_changes`, `seeds`, `spans`, `spans_dropped` |
| `PANIC` | 1 | 1 | `sequence`, `position`, `case` |
| `RECOVERY-BOOT` | 1 | 1 | `build_id` |
| `END` | 1 | 1 | none |
| all | 376 | 313 | |

The order is frozen in [cases.json](cases.json):

1. After every start the firmware stays silent for about 45 s, so that the
   serial session can be opened after the plan's reset without missing a line.
2. The setup block: `BOOT`, `RCC`, the pad sampler's `CAL`, `CALISR` and 40
   `QUAL`, and 8 `ICQUAL`, closed by `BLOCK-END n=1`. The firmware then waits
   for the exact line `GO\n` on USART2 RX.
3. `set_config_ascending`, `set_config_descending` and `set_config_revisited`:
   each constructs the driver at 1 MHz (position -1) and applies its 29 or 30
   requests with `set_config`, in ascending order, descending order, and
   alternating between low and high requests.
4. `construct`: a fresh construction for each of the 29 requests, in ascending
   order.
5. After every fourth occurrence the firmware prints a `BLOCK-END` and waits for
   `GO\n` again, so the host never has more than one block to read.
6. A panic prints `PANIC` with the row it happened in, sets a marker in RAM
   that survives a reset and resets the chip. The next boot holds for 45 s,
   prints `RECOVERY-BOOT`, repeats the setup block without `BLOCK-END` and
   without waiting, constructs the driver once at 1 MHz (`recovery/0`) and
   prints `END`.

The before image panics in the last construct case, 4 000 001 Hz, above `K`.
The after image panics in the first, 15 000 Hz, below `K / 256`, so its first
boot ends there.

## What the records were expected to show

For the divider 2^(BR+1), SCK is `K / 2^(BR+1)` and a byte span is
`7 x 64 MHz / SCK` TIM8 ticks. The accepted interval, with the 1-tick tolerance,
is far narrower than the factor of two between neighbouring dividers, so a row
can only pass at the divider its `CR1` names:

| BR | Divider | SCK (Hz) | CR1 | Byte span (TIM8 ticks) | Accepted interval (Hz) | Rows before | Rows after |
|---|---|---|---|---|---|---|---|
| 0 | 2 | 2 000 000 | 0x0344 | 224 | 1991111.1 .. 2008968.6 | 17 | 13 |
| 1 | 4 | 1 000 000 | 0x034c | 448 | 997772.8 .. 1002237.1 | 17 | 15 |
| 2 | 8 | 500 000 | 0x0354 | 896 | 499442.6 .. 500558.7 | 12 | 9 |
| 3 | 16 | 250 000 | 0x035c | 1792 | 249860.6 .. 250139.6 | 12 | 9 |
| 4 | 32 | 125 000 | 0x0364 | 3584 | 124965.1 .. 125034.9 | 13 | 9 |
| 5 | 64 | 62 500 | 0x036c | 7168 | 62491.3 .. 62508.7 | 20 | 15 |
| 6 | 128 | 31 250 | 0x0374 | 14336 | 31247.8 .. 31252.2 | 12 | 9 |
| 7 | 256 | 15 625 | 0x037c | 28672 | 15624.5 .. 15625.5 | 17 | 13 |
| | | | | | total | 120 | 92 |

The last two columns are the `observed_pass` rows of each run at each divider.

Which divider a row should get: before the change, the table above for every
path, a panic for 4 000 001 Hz in the constructor and `FrequencyTooHigh` with
the previous divider kept in `set_config`. After the change, the fastest SCK at
or below the request, which is 2 MHz for 4 000 000 and 4 000 001 Hz, and for
15 000 Hz `FrequencyTooLow` with the previous divider kept in `set_config` and
a panic in the constructor. In every row, `get_current_config()` and `CR1`
have to name the divider that SCK runs at.

## Results

| | before | after |
|---|---|---|
| Bundle | [bundles/matrix-5dbccd03/](bundles/matrix-5dbccd03/) | [bundles/matrix-2277d528/](bundles/matrix-2277d528/) |
| Run handle | `run-5dbccd03e31c2867` | `run-2277d528ecfff7f9` |
| Plan started, finished (UTC) | 2026-10-27 22:54:14.810, 22:54:19.380 | 2026-10-28 09:07:53.415, 09:07:57.895 |
| Serial session opened (UTC) | 22:54:23.336 | 09:08:01.306 |
| `com_read` calls, bytes received, overflow | 68, 99 353, 0 | 54, 88 420, 0 |
| `GO` lines written | 30 | 23 |
| Console lines | 376, all tagged | 313, all tagged |
| Rows | 120 `observed_pass`, 1 `observed_panic` | 92 `observed_pass`, 1 `observed_panic`, 28 `not_reached` |
| Evaluation errors | none | none |

Both plans finished and passed. The receipts do not record which Agentic HIL
version the bench ran. In every `XSUM` record of both runs all 20 frames were
clean and every counter was 0, and every one of the 9 600 byte spans before and
7 360 after was exactly the nominal span of its divider. Every capture
configuration qualified in both boots of both runs. The panic rows are
`construct/28` (4 000 001 Hz) before and `construct/0` (15 000 Hz) after; the
recovery boot and `END` followed in both runs.

Per request, the SCK each path ran at (from `CR1`, confirmed on the pin) and
its distance from the request. The three `set_config` sequences agree for every
request, before and after. `K div request` is the integer quotient the old
table uses. The after constructor is left out: it was reached only at 15 000 Hz,
where it panicked.

| Request (Hz) | K div request | Before: constructor | Before: `set_config` | After: `set_config` |
|---|---|---|---|---|
| 15 000 | 266 | 15 625 (+4.2 %) | 15 625 (+4.2 %) | `FrequencyTooLow` |
| 15 625 | 256 | 15 625 (0 %) | 15 625 (0 %) | 15 625 (0 %) |
| 15 686 | 255 | 15 625 (-0.4 %) | 15 625 (-0.4 %) | 15 625 (-0.4 %) |
| 20 833 | 192 | 15 625 (-25.0 %) | 15 625 (-25.0 %) | 15 625 (-25.0 %) |
| 20 942 | 191 | 31 250 (+49.2 %) | 31 250 (+49.2 %) | 15 625 (-25.4 %) |
| 31 250 | 128 | 31 250 (0 %) | 31 250 (0 %) | 31 250 (0 %) |
| 41 666 | 96 | 31 250 (-25.0 %) | 31 250 (-25.0 %) | 31 250 (-25.0 %) |
| 42 105 | 95 | 62 500 (+48.4 %) | 62 500 (+48.4 %) | 31 250 (-25.8 %) |
| 62 500 | 64 | 62 500 (0 %) | 62 500 (0 %) | 62 500 (0 %) |
| 83 333 | 48 | 62 500 (-25.0 %) | 62 500 (-25.0 %) | 62 500 (-25.0 %) |
| 85 106 | 47 | 62 500 (-26.6 %) | 62 500 (-26.6 %) | 62 500 (-26.6 %) |
| 100 000 | 40 | 62 500 (-37.5 %) | 62 500 (-37.5 %) | 62 500 (-37.5 %) |
| 102 564 | 39 | 125 000 (+21.9 %) | 125 000 (+21.9 %) | 62 500 (-39.1 %) |
| 125 000 | 32 | 125 000 (0 %) | 125 000 (0 %) | 125 000 (0 %) |
| 166 666 | 24 | 125 000 (-25.0 %) | 125 000 (-25.0 %) | 125 000 (-25.0 %) |
| 173 913 | 23 | 250 000 (+43.8 %) | 250 000 (+43.8 %) | 125 000 (-28.1 %) |
| 250 000 | 16 | 250 000 (0 %) | 250 000 (0 %) | 250 000 (0 %) |
| 333 333 | 12 | 250 000 (-25.0 %) | 250 000 (-25.0 %) | 250 000 (-25.0 %) |
| 363 636 | 11 | 500 000 (+37.5 %) | 500 000 (+37.5 %) | 250 000 (-31.2 %) |
| 500 000 | 8 | 500 000 (0 %) | 500 000 (0 %) | 500 000 (0 %) |
| 666 666 | 6 | 500 000 (-25.0 %) | 500 000 (-25.0 %) | 500 000 (-25.0 %) |
| 800 000 | 5 | 1 000 000 (+25.0 %) | 1 000 000 (+25.0 %) | 500 000 (-37.5 %) |
| 1 000 000 | 4 | 1 000 000 (0 %) | 1 000 000 (0 %) | 1 000 000 (0 %) |
| 1 333 333 | 3 | 1 000 000 (-25.0 %) | 1 000 000 (-25.0 %) | 1 000 000 (-25.0 %) |
| 1 500 000 | 2 | 2 000 000 (+33.3 %) | 2 000 000 (+33.3 %) | 1 000 000 (-33.3 %) |
| 2 000 000 | 2 | 2 000 000 (0 %) | 2 000 000 (0 %) | 2 000 000 (0 %) |
| 3 000 000 | 1 | 2 000 000 (-33.3 %) | 2 000 000 (-33.3 %) | 2 000 000 (-33.3 %) |
| 4 000 000 | 1 | 2 000 000 (-50.0 %) | 2 000 000 (-50.0 %) | 2 000 000 (-50.0 %) |
| 4 000 001 | 0 | panic | `FrequencyTooHigh` | 2 000 000 (-50.0 %) |

All 121 rows, in the order of [before-after.csv](before-after.csv). Before
and after give the SCK in Hz, or for a rejected `set_config` the error and the
SCK the driver kept, which is what the pin showed. 29 rows are marked
`changed`; 64 rows are the same before and after, and 28
were not reached after the change.

| Row | Request (Hz) | Path | Before | After | |
|---|---|---|---|---|---|
| `construct/0` | 15 000 | constructor | 15 625 | panic | changed |
| `construct/1` | 15 625 | constructor | 15 625 | not reached |  |
| `construct/2` | 15 686 | constructor | 15 625 | not reached |  |
| `construct/3` | 20 833 | constructor | 15 625 | not reached |  |
| `construct/4` | 20 942 | constructor | 31 250 | not reached |  |
| `construct/5` | 31 250 | constructor | 31 250 | not reached |  |
| `construct/6` | 41 666 | constructor | 31 250 | not reached |  |
| `construct/7` | 42 105 | constructor | 62 500 | not reached |  |
| `construct/8` | 62 500 | constructor | 62 500 | not reached |  |
| `construct/9` | 83 333 | constructor | 62 500 | not reached |  |
| `construct/10` | 85 106 | constructor | 62 500 | not reached |  |
| `construct/11` | 100 000 | constructor | 62 500 | not reached |  |
| `construct/12` | 102 564 | constructor | 125 000 | not reached |  |
| `construct/13` | 125 000 | constructor | 125 000 | not reached |  |
| `construct/14` | 166 666 | constructor | 125 000 | not reached |  |
| `construct/15` | 173 913 | constructor | 250 000 | not reached |  |
| `construct/16` | 250 000 | constructor | 250 000 | not reached |  |
| `construct/17` | 333 333 | constructor | 250 000 | not reached |  |
| `construct/18` | 363 636 | constructor | 500 000 | not reached |  |
| `construct/19` | 500 000 | constructor | 500 000 | not reached |  |
| `construct/20` | 666 666 | constructor | 500 000 | not reached |  |
| `construct/21` | 800 000 | constructor | 1 000 000 | not reached |  |
| `construct/22` | 1 000 000 | constructor | 1 000 000 | not reached |  |
| `construct/23` | 1 333 333 | constructor | 1 000 000 | not reached |  |
| `construct/24` | 1 500 000 | constructor | 2 000 000 | not reached |  |
| `construct/25` | 2 000 000 | constructor | 2 000 000 | not reached |  |
| `construct/26` | 3 000 000 | constructor | 2 000 000 | not reached |  |
| `construct/27` | 4 000 000 | constructor | 2 000 000 | not reached |  |
| `construct/28` | 4 000 001 | constructor | panic | not reached |  |
| `set_config_ascending/-1` | 1 000 000 | constructor | 1 000 000 | 1 000 000 |  |
| `set_config_ascending/0` | 15 000 | `set_config` | 15 625 | `FrequencyTooLow`, keeps 1 000 000 | changed |
| `set_config_ascending/1` | 15 625 | `set_config` | 15 625 | 15 625 |  |
| `set_config_ascending/2` | 15 686 | `set_config` | 15 625 | 15 625 |  |
| `set_config_ascending/3` | 20 833 | `set_config` | 15 625 | 15 625 |  |
| `set_config_ascending/4` | 20 942 | `set_config` | 31 250 | 15 625 | changed |
| `set_config_ascending/5` | 31 250 | `set_config` | 31 250 | 31 250 |  |
| `set_config_ascending/6` | 41 666 | `set_config` | 31 250 | 31 250 |  |
| `set_config_ascending/7` | 42 105 | `set_config` | 62 500 | 31 250 | changed |
| `set_config_ascending/8` | 62 500 | `set_config` | 62 500 | 62 500 |  |
| `set_config_ascending/9` | 83 333 | `set_config` | 62 500 | 62 500 |  |
| `set_config_ascending/10` | 85 106 | `set_config` | 62 500 | 62 500 |  |
| `set_config_ascending/11` | 100 000 | `set_config` | 62 500 | 62 500 |  |
| `set_config_ascending/12` | 102 564 | `set_config` | 125 000 | 62 500 | changed |
| `set_config_ascending/13` | 125 000 | `set_config` | 125 000 | 125 000 |  |
| `set_config_ascending/14` | 166 666 | `set_config` | 125 000 | 125 000 |  |
| `set_config_ascending/15` | 173 913 | `set_config` | 250 000 | 125 000 | changed |
| `set_config_ascending/16` | 250 000 | `set_config` | 250 000 | 250 000 |  |
| `set_config_ascending/17` | 333 333 | `set_config` | 250 000 | 250 000 |  |
| `set_config_ascending/18` | 363 636 | `set_config` | 500 000 | 250 000 | changed |
| `set_config_ascending/19` | 500 000 | `set_config` | 500 000 | 500 000 |  |
| `set_config_ascending/20` | 666 666 | `set_config` | 500 000 | 500 000 |  |
| `set_config_ascending/21` | 800 000 | `set_config` | 1 000 000 | 500 000 | changed |
| `set_config_ascending/22` | 1 000 000 | `set_config` | 1 000 000 | 1 000 000 |  |
| `set_config_ascending/23` | 1 333 333 | `set_config` | 1 000 000 | 1 000 000 |  |
| `set_config_ascending/24` | 1 500 000 | `set_config` | 2 000 000 | 1 000 000 | changed |
| `set_config_ascending/25` | 2 000 000 | `set_config` | 2 000 000 | 2 000 000 |  |
| `set_config_ascending/26` | 3 000 000 | `set_config` | 2 000 000 | 2 000 000 |  |
| `set_config_ascending/27` | 4 000 000 | `set_config` | 2 000 000 | 2 000 000 |  |
| `set_config_ascending/28` | 4 000 001 | `set_config` | `FrequencyTooHigh`, keeps 2 000 000 | 2 000 000 | changed |
| `set_config_descending/-1` | 1 000 000 | constructor | 1 000 000 | 1 000 000 |  |
| `set_config_descending/0` | 4 000 001 | `set_config` | `FrequencyTooHigh`, keeps 1 000 000 | 2 000 000 | changed |
| `set_config_descending/1` | 4 000 000 | `set_config` | 2 000 000 | 2 000 000 |  |
| `set_config_descending/2` | 3 000 000 | `set_config` | 2 000 000 | 2 000 000 |  |
| `set_config_descending/3` | 2 000 000 | `set_config` | 2 000 000 | 2 000 000 |  |
| `set_config_descending/4` | 1 500 000 | `set_config` | 2 000 000 | 1 000 000 | changed |
| `set_config_descending/5` | 1 333 333 | `set_config` | 1 000 000 | 1 000 000 |  |
| `set_config_descending/6` | 1 000 000 | `set_config` | 1 000 000 | 1 000 000 |  |
| `set_config_descending/7` | 800 000 | `set_config` | 1 000 000 | 500 000 | changed |
| `set_config_descending/8` | 666 666 | `set_config` | 500 000 | 500 000 |  |
| `set_config_descending/9` | 500 000 | `set_config` | 500 000 | 500 000 |  |
| `set_config_descending/10` | 363 636 | `set_config` | 500 000 | 250 000 | changed |
| `set_config_descending/11` | 333 333 | `set_config` | 250 000 | 250 000 |  |
| `set_config_descending/12` | 250 000 | `set_config` | 250 000 | 250 000 |  |
| `set_config_descending/13` | 173 913 | `set_config` | 250 000 | 125 000 | changed |
| `set_config_descending/14` | 166 666 | `set_config` | 125 000 | 125 000 |  |
| `set_config_descending/15` | 125 000 | `set_config` | 125 000 | 125 000 |  |
| `set_config_descending/16` | 102 564 | `set_config` | 125 000 | 62 500 | changed |
| `set_config_descending/17` | 100 000 | `set_config` | 62 500 | 62 500 |  |
| `set_config_descending/18` | 85 106 | `set_config` | 62 500 | 62 500 |  |
| `set_config_descending/19` | 83 333 | `set_config` | 62 500 | 62 500 |  |
| `set_config_descending/20` | 62 500 | `set_config` | 62 500 | 62 500 |  |
| `set_config_descending/21` | 42 105 | `set_config` | 62 500 | 31 250 | changed |
| `set_config_descending/22` | 41 666 | `set_config` | 31 250 | 31 250 |  |
| `set_config_descending/23` | 31 250 | `set_config` | 31 250 | 31 250 |  |
| `set_config_descending/24` | 20 942 | `set_config` | 31 250 | 15 625 | changed |
| `set_config_descending/25` | 20 833 | `set_config` | 15 625 | 15 625 |  |
| `set_config_descending/26` | 15 686 | `set_config` | 15 625 | 15 625 |  |
| `set_config_descending/27` | 15 625 | `set_config` | 15 625 | 15 625 |  |
| `set_config_descending/28` | 15 000 | `set_config` | 15 625 | `FrequencyTooLow`, keeps 15 625 | changed |
| `set_config_revisited/-1` | 1 000 000 | constructor | 1 000 000 | 1 000 000 |  |
| `set_config_revisited/0` | 15 000 | `set_config` | 15 625 | `FrequencyTooLow`, keeps 1 000 000 | changed |
| `set_config_revisited/1` | 4 000 000 | `set_config` | 2 000 000 | 2 000 000 |  |
| `set_config_revisited/2` | 15 625 | `set_config` | 15 625 | 15 625 |  |
| `set_config_revisited/3` | 3 000 000 | `set_config` | 2 000 000 | 2 000 000 |  |
| `set_config_revisited/4` | 15 686 | `set_config` | 15 625 | 15 625 |  |
| `set_config_revisited/5` | 2 000 000 | `set_config` | 2 000 000 | 2 000 000 |  |
| `set_config_revisited/6` | 20 833 | `set_config` | 15 625 | 15 625 |  |
| `set_config_revisited/7` | 1 500 000 | `set_config` | 2 000 000 | 1 000 000 | changed |
| `set_config_revisited/8` | 20 942 | `set_config` | 31 250 | 15 625 | changed |
| `set_config_revisited/9` | 1 333 333 | `set_config` | 1 000 000 | 1 000 000 |  |
| `set_config_revisited/10` | 31 250 | `set_config` | 31 250 | 31 250 |  |
| `set_config_revisited/11` | 1 000 000 | `set_config` | 1 000 000 | 1 000 000 |  |
| `set_config_revisited/12` | 41 666 | `set_config` | 31 250 | 31 250 |  |
| `set_config_revisited/13` | 800 000 | `set_config` | 1 000 000 | 500 000 | changed |
| `set_config_revisited/14` | 42 105 | `set_config` | 62 500 | 31 250 | changed |
| `set_config_revisited/15` | 666 666 | `set_config` | 500 000 | 500 000 |  |
| `set_config_revisited/16` | 62 500 | `set_config` | 62 500 | 62 500 |  |
| `set_config_revisited/17` | 500 000 | `set_config` | 500 000 | 500 000 |  |
| `set_config_revisited/18` | 83 333 | `set_config` | 62 500 | 62 500 |  |
| `set_config_revisited/19` | 363 636 | `set_config` | 500 000 | 250 000 | changed |
| `set_config_revisited/20` | 85 106 | `set_config` | 62 500 | 62 500 |  |
| `set_config_revisited/21` | 333 333 | `set_config` | 250 000 | 250 000 |  |
| `set_config_revisited/22` | 100 000 | `set_config` | 62 500 | 62 500 |  |
| `set_config_revisited/23` | 250 000 | `set_config` | 250 000 | 250 000 |  |
| `set_config_revisited/24` | 102 564 | `set_config` | 125 000 | 62 500 | changed |
| `set_config_revisited/25` | 173 913 | `set_config` | 250 000 | 125 000 | changed |
| `set_config_revisited/26` | 125 000 | `set_config` | 125 000 | 125 000 |  |
| `set_config_revisited/27` | 166 666 | `set_config` | 125 000 | 125 000 |  |
| `set_config_revisited/28` | 4 000 001 | `set_config` | `FrequencyTooHigh`, keeps 125 000 | 2 000 000 | changed |
| `set_config_revisited/29` | 15 000 | `set_config` | 15 625 | `FrequencyTooLow`, keeps 2 000 000 | changed |
| `recovery/0` | 1 000 000 | constructor | 1 000 000 | 1 000 000 |  |

The result files hold the same rows with every field:

- [before-after.csv](before-after.csv): per row, the outcome, SCK and status
  before and after, how each relates to the request (`before_reading_ceiling`,
  `after_reading_ceiling`) and the verdict on the after row (`conforms` or
  `not_reached`).
- [tables.csv](tables.csv) and [tables-after.csv](tables-after.csv): the full
  evaluation of each run, per row: `CR1`, `BR`, the frequency it gives, the
  reported frequency, the measured interval, the valid frames, both readings
  below and the status.
- [evaluation.json](evaluation.json) and
  [evaluation-after.json](evaluation-after.json): the same as JSON, with the
  capture path qualification, the pad sampler, the lifecycle (panic, recovery
  boot, `END`) and the list of errors, empty in both.
- [matrix.txt](matrix.txt) and [matrix-after.txt](matrix-after.txt): the rows
  grouped by status, as text.

The evaluation reads each row two ways, as properties and not as pass or fail.
The ceiling reading takes the request as a maximum: before the change 84 rows
conform, 28 run above the request (`exceeds_request`, 7 requests on four paths
each), 5 accept 15 000 Hz although no divider meets it, and 4 reject 4 000 001
Hz, or panic on it, although 2 MHz would meet it. The nearest reading takes the request as a target and
expects the nearest frequency. Before the change, the `matches` column of
tables.csv has 80 rows that agree with both readings, 17 with the nearest
reading only, 4 with the ceiling reading only and 20 with neither: the old table
follows neither reading. After the change, all 93 reached rows conform to the
ceiling reading, which is the patch's contract.

In words. Before the change, SCK ran above the request for 7 of the 28
requests that a divider can meet, by up to 49.2 %, through the constructor and `set_config` alike;
15 000 Hz ran at 15 625 Hz without an error; and 4 000 001 Hz panicked in the
constructor while `set_config` rejected it and kept the previous divider. After
the change, each of the 88 `set_config` rows ran at the fastest divider at or
below its request, or, for 15 000 Hz, returned `FrequencyTooLow` and kept the
previous divider on the pin; 4 000 001 Hz ran at 2 MHz. The constructor
panicked at 15 000 Hz as the patch intends, which ended that boot before the
other construct rows, and the four constructions at 1 MHz ran at 1 MHz. In
every row of both runs, the SCK measured on the pin agreed with the divider in
`CR1` and with `get_current_config()`.

### Reproducing the tables

From this directory, with Python 3 and its standard library only:

```bash
OUT=$(mktemp -d)
python -I -B - "$OUT" <<'EOF'
import importlib.util, pathlib, sys

spec = importlib.util.spec_from_file_location("analyze", "analyze.py")
analyze = importlib.util.module_from_spec(spec)
sys.modules["analyze"] = analyze
spec.loader.exec_module(analyze)

before = analyze.evaluate_bundle("bundles/matrix-5dbccd03", "hardware")
after = analyze.evaluate_bundle("bundles/matrix-2277d528", "hardware")
tables_before = analyze.render_tables(before)
tables_after = analyze.render_tables(after)
out = pathlib.Path(sys.argv[1])
for name, text in [
    ("tables.csv", tables_before["tables_csv"]),
    ("evaluation.json", tables_before["evaluation_json"]),
    ("matrix.txt", analyze.render_matrix(before)),
    ("tables-after.csv", tables_after["tables_csv"]),
    ("evaluation-after.json", tables_after["evaluation_json"]),
    ("matrix-after.txt", analyze.render_matrix(after)),
    ("before-after.csv", analyze.render_before_after(before, after)["csv"]),
]:
    (out / name).write_bytes(text.encode("utf-8"))
EOF
for f in tables.csv evaluation.json matrix.txt tables-after.csv evaluation-after.json matrix-after.txt before-after.csv; do cmp "$OUT/$f" "$f" && echo "identical $f"; done
```

analyze.py has no command line of its own; the snippet imports it, evaluates
both bundles, renders the seven result files into a temporary directory and
compares them byte for byte with the files here. With Python 3.13.14 it prints
`identical` for all seven. Before it reads a record, analyze.py checks every
file of a bundle against the SHA-256 and size in its `index.json`, each plan
against the canonical digest and plan path its index records, and the console
file against the text of the `com_read` receipts it was joined from.

## The test plans

[plans/matrix.json](plans/matrix.json) is the whole plan: flash `hq184-fw.elf`
onto the device `dut`, then reset it. The serial session is not part of the
plan; it was run with separate tool calls after the plan had finished (see
below), which the firmware's 45 s hold allows for. The two files in
[plans/instantiated/](plans/instantiated/) are the plan of each run with
`image_path` set to the uploaded image, `.agentic-hil/artifacts/<sha256>.elf`;
each is byte for byte the `plans/matrix.json` of its bundle. The run receipt
names the copy of the plan the bench ran (`test_config_path`) by its canonical
digest, the SHA-256 of `json.dumps(plan, sort_keys=True)`; the bundle index
records the same path, and analyze.py recomputes the digest from the plan.

## Running the plans

As run, through Agentic HIL's MCP tools, against a configuration whose debugger
`dut` is the board's ST-LINK and whose COM port `dut` is its virtual COM port:

1. `artifact_upload` of the image with its SHA-256.
2. `test_reactor_run` of the instantiated plan with `detach: true`, then
   `test_reactor_status` until the run has finished and passed.
3. `com_session_start` on `dut`, within the 45 s hold after the reset.
4. `com_read` with `max_bytes: 12000` up to the next `BLOCK-END` record
   (`until: "n=<k> crc32="`, read again while a read stopped at 12 000 bytes),
   then up to the end of that line (`until: "\n"`), then `com_write` of `GO\n`;
   once per `BLOCK-END`.
5. Reading on through `PANIC` and the 45 s hold up to `RECOVERY-BOOT seq=0 `,
   then through the recovery setup block and `recovery/0` up to `HQ184 END seq=`
   and the end of that line, and one last read without `until` that returns
   nothing.
6. `com_session_stop`.

The bundles hold the results of these calls as receipts, with `receipt_seq`
giving their order, and `hardware_lease_status` before and after. Each receipt
names in `redacted` what was removed from it: the probe's USB serial number and
device path, other device identities, lease identifiers and owner tokens, and
paths and host names on the bench host. The `com_read` receipts
were filled in from our pipeline's own record of each tool response, checked
against the receipt's request, `bytes_read` and `data.hex`, and `uart/matrix.txt` is their
text joined. `build.json` is the result of our pipeline's build step, which is
not an Agentic HIL tool. The script that assembled the bundles belongs to the
pipeline and is not included. The `agentic-hil` command line was not used.

Without Agentic HIL: flash an image with the tool of your choice, reset the
board, open the virtual COM port at 115200 baud, 8N1, within 45 s, and send
`GO\n` after every `BLOCK-END` line until `END`.

## Building the images

Both images need rustc 1.97.0 (2d8144b78 2026-07-07) with the
`thumbv7em-none-eabihf` target, and take every crate at the version the
respective `Cargo.lock` records. `build_id` is derived from the SHA-256 of the
two frozen inputs, the compiler version and the Embassy revision (with the
patch digest), so the same `build_id` needs that compiler.

The before image, from [firmware/](firmware/):

```bash
cargo build --release --target thumbv7em-none-eabihf
```

The after image, from [variants/after/](variants/after/), with a checkout of
Embassy at the same revision and the patch applied in `variants/after/embassy`
(which `variants/after/.gitignore` ignores):

```bash
git clone https://github.com/embassy-rs/embassy embassy
git -C embassy checkout c70760de42e2c8103138da6ff91626ff5a2688ba
git -C embassy apply ../../../patches/hq356-embassy-stm32-spi-clock-contract.diff
cargo build --release --target thumbv7em-none-eabihf
```

Each image is `target/thumbv7em-none-eabihf/release/hq184-fw` in the directory
it was built from. [variants/after/Cargo.toml](variants/after/Cargo.toml) takes
the sources and the build script from `../../firmware` and patches every
Embassy git crate to the checkout, so exactly one Embassy tree is in the build;
its `[package.metadata.hq184]` names the patch whose digest goes into the BOOT
record. The build script reads `../cases.json` and `../measurement.json`
relative to the firmware directory, which is why both sit at the top of this
directory, and compiles them into the image. The release profile keeps limited
debug information (`debug = 1`), which records paths of the build machine. The bundles record the
build arguments as not observable; the commands above are the plain release
build that the manifests describe, and a byte-for-byte rebuild has not been
checked.

## Where things are

| Path | What it is |
|---|---|
| [patches/hq356-embassy-stm32-spi-clock-contract.diff](patches/hq356-embassy-stm32-spi-clock-contract.diff) | the patch, as the after image was built with it |
| [firmware/](firmware/) | the firmware: sources, build script, `memory.x`, manifest and lock file of the before build |
| [variants/after/](variants/after/) | manifest and lock file of the after build |
| [cases.json](cases.json) | the 29 requests and the frozen order |
| [measurement.json](measurement.json) | clocks, SPI pins, capture path, frames per row, seed, console |
| [plans/](plans/) | the plan and the two instantiated plans |
| [bundles/matrix-5dbccd03/](bundles/matrix-5dbccd03/), [bundles/matrix-2277d528/](bundles/matrix-2277d528/) | the evidence of each run: `index.json` (every file with its SHA-256 and size, the run, provenance), `inputs/` (cases.json and measurement.json as run, and in the after bundle the patch as `patch.diff`, all byte for byte the files at the top), `plans/matrix.json`, `receipts/` and `uart/matrix.txt`, the console as received |
| [analyze.py](analyze.py) | the analyzer |
| [tables.csv](tables.csv), [evaluation.json](evaluation.json), [matrix.txt](matrix.txt) | the evaluation of the before run |
| [tables-after.csv](tables-after.csv), [evaluation-after.json](evaluation-after.json), [matrix-after.txt](matrix-after.txt) | the evaluation of the after run |
| [before-after.csv](before-after.csv) | the two runs side by side |
| [.gitattributes](.gitattributes) | keeps the bundles, the plans and the patch byte for byte as their digests record them |

## How this was made

This report is part of the Agentic HIL project's upstream work, and we maintain
Agentic HIL. AI coding agents in our development pipeline wrote the patch, the
campaign firmware, the analyzer, the plans and this text, and the pipeline built
the images. Agentic HIL carried out every step on the board: it flashed and
reset the board and carried every byte to and from its serial port, and no step
reached the board any other way; the one jumper wire was fitted by hand. Our
maintainer approved submitting this report and the pull request. No person has
reviewed the patch, the firmware, the analyzer or the analysis beyond that.

## Licensing

The firmware, the plans, analyze.py and the result files are Apache-2.0, like
this repository. The patch is a change to Embassy and is offered under
Embassy's terms, MIT OR Apache-2.0. Building the images pulls in Embassy,
`cortex-m`, `cortex-m-rt` and further crates, each under its own terms; none of
them is included here.
