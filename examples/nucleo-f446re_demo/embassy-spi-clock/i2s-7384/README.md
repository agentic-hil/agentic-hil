# The embassy-stm32 I2S constructor on a NUCLEO-F446RE

A hardware check for
[embassy-rs/embassy#7384](https://github.com/embassy-rs/embassy/pull/7384), the
pull request behind the SPI clock report in the parent folder. The pull request makes `compute_baud_rate` in
`embassy-stm32/src/spi/mod.rs` reject a frequency below kernel clock / 256. The
I2S driver passed its sample rate to its internal SPI as the SPI frequency, so
with the pull request the I2S constructors panic whenever the kernel clock
exceeds 256 times the sample rate, as in the `i2s_dma` test in `tests/stm32`
(48 kHz at a 45 MHz APB1 clock). Commit `c8db3be` makes the I2S driver request
the SPI kernel clock instead.

This folder runs the sequence of `tests/stm32/src/bin/i2s_dma.rs` on a
NUCLEO-F446RE, built against three Embassy revisions:

| Variant | Embassy commit | What it is |
|---|---|---|
| `base` | `c2413d5994bbd8b7e6d5975fd49e6427cb903a02` | base of the pull request |
| `before` | `5623d7b56c2c6e1afeafe0a431ddff8e3264db2a` | the SPI change, without the I2S fix |
| `after` | `c8db3be841e271609a2bd3928996d0c6ba188f3a` | the SPI change with the I2S fix |

[Agentic Hardware-in-the-Loop (Agentic HIL)](https://github.com/agentic-hil/agentic-hil)
drove the board. Each run held the board's ST-LINK probe and its virtual COM
port from `bench_run_start` to `bench_run_stop`, and for each variant called
`artifact_upload` with the image and then `flash_firmware`, which wrote and
verified the image, reset the board and captured the serial output from the
reset until a `RESULT` line. A run made no other calls. [receipts/](receipts/)
holds every call and its result.

## The firmware

[firmware/src/main.rs](firmware/src/main.rs) keeps the test's constructor call
(`I2S::new_txonly_nomck` on `SPI3` with `PB5`, `PA15`, `PB3` and `DMA1_CH7`),
wavetable, DMA buffer and configuration (`Format::Data16Channel32`, no master
clock, the default 48 kHz), and its three rounds of `start`, ten `write`s and
`stop`. It differs from the test in four ways:

- The clocks come from HSI 16 MHz / 8 instead of HSE 8 MHz bypass / 4. Both PLLs
  see the same 2 MHz input, so SYSCLK (180 MHz), APB1 (45 MHz), APB2 (90 MHz)
  and the PLLI2S R output (60 MHz) are the test's.
- It reports on USART2 at 115 200 Bd, the ST-LINK virtual COM port, instead of
  defmt-rtt, panic-probe and `bkpt`. Its panic handler prints the panic message
  and location.
- Each write and stop runs under a 500 ms timeout. The test discards each
  write's result with `.ok()`; this firmware reports every outcome by write
  index.
- Then it starts the stream once more, makes ten warm-up writes and times the
  next 400 against the DWT cycle counter. Once the ring buffer is full, each
  write waits until the I2S peripheral has drained as much as it adds, so the
  timed series measures the frame rate the peripheral runs at. It also reads
  back the SPI3 and I2S registers after construction and while the stream runs.

`RESULT pass` needs what the upstream test needs: the sequence runs to its end
without a panic and without a write or stop that hangs. It also needs a frame
rate within 1 % of the rate the I2S registers program. Write errors are
reported, not judged, as in the upstream test.

## Results (run 3)

| | `base` | `before` | `after` |
|---|---|---|---|
| Constructor | returns | panics | returns |
| `SPI3_CR1` after construction | `0x037c` (BR = 7, /256) | – | `0x0344` (BR = 0, /2) |
| `SPI3_I2SCFGR` / `SPI3_I2SPR` | `0x0a01` / `0x000a` | – | `0x0a01` / `0x000a` |
| Writes / stops completed | 30 of 30 / 3 of 3 | – | 30 of 30 / 3 of 3 |
| `Overrun` returned by write | writes 0 and 1 of each round | – | writes 0 and 1 of each round |
| Timed series: cycles for 240 000 frames | 921 599 951 | – | 921 599 945 |
| Frame rate | 46 875.002 Hz | – | 46 875.002 Hz |
| `RESULT` | `pass` | `panic` | `pass` |

`before` stops in the constructor:

```
I2S7384 PANIC message="unwrap of `compute_baud_rate(self.kernel_clock, config.frequency)` failed: FrequencyTooLow" location=/embassy/embassy-stm32/src/spi/mod.rs:408
```

On the F429, `i2s_dma` calls the same constructor with the same 45 MHz kernel
clock and 48 kHz, so it panics at the same place.

`after` writes the same `I2SCFGR` and `I2SPR` as `base`: I2S master transmit,
Philips standard, 16-bit data in 32-bit channels, `I2SDIV` = 10, `ODD` = 0, no
master clock output. The `SPI3_CR1` values differ only in the `BR` field
(`0x037c ^ 0x0344 = 0x0038`), which plays no part in I2S mode: the bit clock
comes from `I2SDIV` and `ODD`. While the stream runs, `CR1` reads `0x0b7c` and
`0x0b44`, again differing only in `BR`. `CR2` (`0x0004` after construction,
`0x0006` while running) and the running `I2SCFGR` (`0x0e01`) are the same on
both.

The rate the registers program is 60 MHz / (64 × (2 × 10 + 0)) = 46 875 Hz,
which takes 921 600 000 cycles at 180 MHz for 240 000 frames. Both variants
measure that to within 55 cycles (0.06 ppm). The core clock and the PLLI2S both
come from HSI, so this checks the divider chain, not the oscillator.

On `base` and `after` alike, the first two writes after every `start()` return
`Error::Overrun` and the other eight succeed, in all three rounds and in the
warm-up before the timed series. The test discards these results, and the pull
request does not change them. This folder does not look into their cause.

The serial output of each flash is in
`response.result.structuredContent.capture.data.text` of its
`*-flash_firmware.json` receipt (`004` is `base`, `006` is `before`, `008` is
`after`).

## Runs 1 and 2

All three runs took place on 2026-11-02 between 23:19 and 23:28 UTC on the same
board, and each flashed all three variants. Their sources are in
[firmware/runs/](firmware/runs/) (run 3 is `firmware/src/main.rs`), and their
receipts are kept as well. The constructor outcomes and the registers are the
same in all three runs.

- **Run 1** reported write errors only as a count per round (1, 2 and 2, where
  runs 2 and 3 saw 2 in every round), counted the failed writes of the warm-up
  and of the timed series as one number (2), and failed on any write error, so
  `base` and `after` read `RESULT fail`. Its timed series took 921 599 999 and
  921 600 001 cycles, 0 ppm. It printed the rate in millihertz under the name
  `measured_mhz`.
- **Run 2** reported each write's outcome by index and judged as run 3 does,
  but printed the warm-up line between the warm-up and the timed series. The
  line has 75 characters, about 6.5 ms at 115 200 Bd, and the DMA kept draining
  meanwhile, so the timed series came out 6.4 ms short: 46 933.7 Hz, +1252 ppm
  on `base` and +1250 ppm on `after`, still within the 1 % limit. Run 3 prints
  the line after the timed series.

## Reproducing the images

The images are not included: they rebuild bit for bit from this folder.
[firmware/rebuild_runs.sh](firmware/rebuild_runs.sh) rebuilt all nine from it,
and each SHA-256 matched its build record in [builds/](builds/), which is also
the `artifact.sha256` that `flash_firmware` reports in the receipts. The build
needs:

- Windows with Git Bash and Python, and rustc 1.97.0 (`cargo +1.97`) with the
  `thumbv7em-none-eabi` target.
- The Embassy revision of each variant checked out at `C:/t/e7384-<variant>`.
  `5623d7b` and `c8db3be` are commits of the pull request
  (`git fetch https://github.com/embassy-rs/embassy pull/7384/head`).

`firmware/build.sh <variant>` builds one image into `C:/t/i2s-<variant>`, maps
the source paths to `/embassy`, `/cargo` and `/fw`, and writes
`build-<variant>.json` with the image's SHA-256. The fixed paths matter: the
target directory ends up in the image. For runs 1 and 2, copy
`firmware/runs/run-<n>/main.rs` over `firmware/src/main.rs` first;
`rebuild_runs.sh` does that and puts run 3 back.

| Run | `base` | `before` | `after` |
|---|---|---|---|
| 1 | `cb91b69e…` | `d4547b3b…` | `f14fd5c9…` |
| 2 | `8869671c…` | `4492572c…` | `0fa6da5f…` |
| 3 | `a89d8673…` | `10062b13…` | `050643c7…` |

[tools/](tools/) has the MCP client and the script that drove the runs
(`run_variants.py <receipt dir> <image dir> base before after`). For
publication, the client reads its ssh profile from the environment variable
`AGENTIC_HIL_BENCH_PROFILE` instead of a local path, and `run_variants.py` takes
the image folder as an argument. The calls they make are unchanged.

In the receipts, bench host paths, the host name, the probe serial number, lease
ids and the owner id are redacted, and each receipt lists what was redacted
under `redacted`. Uploads appear as their size and SHA-256 instead of the image
data.

## Limits

- One board, an STM32F446RE, where the test targets the STM32F429ZI. SPI3, the
  pins, the DMA stream and the clocks are the same, but the F429 test binary
  itself did not run here.
- Nothing measured the pins. The frame rate is how fast the DMA drains the
  buffer, timed against the core clock.

## How this was made

This check is part of the Agentic HIL project's upstream work, and we maintain
Agentic HIL. An AI coding agent wrote the firmware, the scripts and this text,
built the images and drove the runs; it reached the board only through Agentic
HIL. Our maintainer approved publishing this folder. No person has reviewed the
firmware or the analysis beyond that.

## Licensing

The firmware and the scripts are Apache-2.0, like this repository. Building the
images pulls in Embassy, `cortex-m`, `cortex-m-rt` and further crates, each
under its own terms; none of them is included here.
