# MicroPython `UART.sendbreak()` on a NUCLEO-F446RE

A hardware report on two open MicroPython issues,
[micropython/micropython#8794](https://github.com/micropython/micropython/issues/8794),
"UART sendbreak bug", and
[micropython/micropython#8646](https://github.com/micropython/micropython/issues/8646),
"Pyboard UART.sendbreak": how long `UART.sendbreak()` holds the bus low on an
STM32F4, and when the method returns. It measures both on MicroPython's stm32
port, once as the documentation stands and once with
[uart-sendbreak-docs.patch](uart-sendbreak-docs.patch), a change to the two
documentation entries that says what the port does.

The runs are driven by [Agentic Hardware-in-the-Loop (Agentic HIL)](https://github.com/agentic-hil/agentic-hil):
its test reactor flashes each image, resets the board with the serial port
already open and records what the image prints until its last line, following
the plans in [plans/](plans/). Nothing reached the board any other way.

## What the change does

`docs/library/pyb.UART.rst` says that `sendbreak()` drives the bus low "for a
duration of 13 bits", and `docs/library/machine.UART.rst` that it drives it low
"longer than required for a normal transmission of a character". On an
STM32F4, `mp_machine_uart_sendbreak()` in `ports/stm32/machine_uart.c` sets
`USART_CR1_SBK` and returns; the USART then sends the break on its own. The
classic STM32 USART has no setting for the length of a break: outside LIN mode,
which the port does not use, RM0390, the STM32F446 reference manual, describes
the break frame as one frame of zeros whose length follows the `M` bit, and a
logic 1 that the USART inserts after it. Nothing in the port asks for 13 bits;
the number appears earlier in the same file, as the floor of `timeout_char`
("13 bits to be safe").

The patch rewrites the two entries to say what the port does on an STM32F4: the
method returns at once, before the break has been sent, and the break holds the
bus low for one character frame with a single stop bit, followed by at least one
bit time of logic 1; with 8 data bits that is 10 bit times without parity, with
1 or 2 stop bits, and 11 with a parity bit and 1 stop bit. `pyb.UART.rst` gets
this in its entry; `machine.UART.rst` keeps its generic sentence and gets a note
for the stm32 port, the form it already uses for `flush()` and `txdone()`. The
patch changes no source file.

The description at the top of the patch is the one the `patched` image was
built with. Two of its sentences are wrong, and the pull request's commit
message corrects them: the `timeout_char` floor is earlier in `machine_uart.c`,
not further down, and the method returns before the line goes low, not while it
is low (see Results). `git apply` reads only the diff below the description.

## Coverage, and what is not claimed

- One USART of one chip is measured: UART4 of an STM32F446RE, at the clocks
  MicroPython sets up for the NUCLEO-F446RE. The other STM32F4 parts, the
  pyboard's STM32F405 of #8646 among them, have the same USART and take the
  same `CR1.SBK` path in `machine_uart.c`, so the bit counts carry over; their
  microsecond figures follow from their own PCLK. Nothing else is claimed for
  them.
- The families that `machine_uart.c` sends a break for through
  `RQR = USART_RQR_SBKRQ` (F0, F7, G0, G4, H5, H7, L0, L4, N6, U5, WB, WL) are
  not measured, and the patch says nothing about them. L1 takes the `CR1.SBK`
  path as well but is not measured.
- 8 data bits only, with no parity or even parity and 1 or 2 stop bits. 7 and 9
  data bits, odd parity, a break requested while a character is still being
  sent, hardware flow control, LIN mode and SDI-12 conformance are not measured.
- Nothing is connected to the board, and nothing outside the chip is measured.
  The sampler reads the TX pin's input stage (`GPIOA_IDR`) at 32 samples per bit,
  so every length below is good to one sample, a thirty-second of a bit, and says
  nothing about the analog shape of the edges.

## The two images

Both are MicroPython for the `NUCLEO_F446RE` board with the same measurement
script, [micropython/main.py](micropython/main.py), frozen in as source text and
run at boot.

| Image | MicroPython tree | Reports itself as |
|---|---|---|
| `baseline` | `23f0e61b47f0adf9a8f1259ec603892f27a69eb7`, unmodified | `H176 boot mpy=23f0e61b47 ...`, banner `MicroPython 23f0e61b47 on 2026-10-11; NUCLEO-F446RE with STM32F446xx` |
| `patched` | the same revision with the patch applied | `H176 boot mpy=v1.30.0-preview ...`, banner `MicroPython v1.30.0-preview on 2026-10-11; NUCLEO-F446RE with STM32F446xx` |

The patch touches only `docs/`, so both images are compiled from the same
source code, and the documentation change cannot show in a binary. `patched`
is there as the control it then has to be: built from a copy of the tree
without its `.git` directory, it takes its version from `py/mpconfig.h` instead
of from Git, which makes its `.text` section four bytes longer and moves what
lies behind the version string; it has to measure the same as `baseline` in
every repetition. Both images
link the same object files with the same sizes, read from the linker map of each
build.

The comments in `main.py` name files of the validation job that produced the
images (`tests/hq176/`, `firmware/smoke/board.h`, and a `README.md` whose record
descriptions this file repeats). Those belong to the pipeline that built and
judged them and are not part of this directory; the script here is exactly the
one the images froze, so the comments are left as they are.

## The setup, all inside the chip

- Clocks: MicroPython's board configuration runs the F446 at 168 MHz from its
  PLL, with APB1 at 42 MHz and APB2 at 84 MHz (`sysclk=168000000`,
  `pclk1=42000000`, `pclk2=84000000`, `cfgr=0x0000940a` in the boot record).
  UART4 sits on APB1, and TIM8 on APB2 runs at twice PCLK2, 168 MHz.
- Test UART: UART4 with TX on PA0 (`MODER` 2, alternate function 8 in every
  record), a UART the REPL does not use. PA0 reaches only the board's headers,
  and nothing drives it but the USART. The script opens it with
  `machine.UART(4, 9600, ...)` and reconfigures it with `UART.init()` for each
  configuration; the `cfg` record carries the resulting `BRR`, `CR1`, `CR2` and
  `CR3`.
- Sampler: TIM8 requests one DMA transfer per update, and DMA2 stream 1 on
  channel 7 (`TIM8_UP`) copies `GPIOA_IDR` as 16-bit words into a buffer of 4096
  samples, in direct mode and without interrupts. The TIM8 period is the whole
  number of 168 MHz ticks nearest to 32 samples per bit: 4375 ticks at 1200
  baud, 547 at 9600 and 273 at 19200, so a window is 128 bit times. The port
  uses neither TIM8 nor that stream on this board.
- Marks: the sample index of a moment is read from the stream's remaining
  transfer count (`NDTR`), so the script can place its own actions on the same
  axis as the pin's edges. The call to `sendbreak()` and the marks around it run
  with interrupts disabled, and the DWT cycle counter times the call.
- Controls: before each configuration, the script measures 16 times what one
  mark and one trivial call on the same UART object (`u.any()`) cost with the
  sampler running, in samples and in cycles (`rd`, `cl`, `rd_cyc`, `cl_cyc`).
- Console: the REPL's USART2 on PA2/PA3, the ST-LINK's virtual COM port, 115200
  baud, 8N1. Each record is printed after its window has been sampled and the
  sampler stopped, and every line is followed by four times its own length of
  silence, because the ST-LINK's USB-serial bridge drops what its buffer cannot
  hold. An image takes about 30 s.

## What one repetition does

For each of five configurations, 1200, 9600 and 19200 baud at 8N1, and 9600 baud
at 8E1 and 8N2, the script runs 24 repetitions of the main phase and 5 of the
`sbk` phase.

Main phase:

1. The sampler starts.
2. `u.write(b"\x55")`: ten edges, from which the bit time is read.
3. `u.write(b"\x00")`: a low run of nine bit times, a control of known length.
4. With interrupts off: mark `tc`, `u.sendbreak()`, mark `tr`; then the script
   waits, for at most 3 bit times, until the pin reads low and marks `tb`.
5. With interrupts on again: `u.write(b"\x55")`, written during the break, and
   mark `te`.
6. Once the window is full, the sampler stops and the script prints a `rep`
   record and the indices of every edge as `edges` records.

The `sbk` phase sends one `0x55` and then, in place of steps 3 to 5, polls
`CR1.SBK` after the call until it reads clear, for at most 16 bit times, and
marks when it did (`tsbk`).

## The lines on USART2

Every line of the script is `H176 <kind> key=value ...`, and its last line is
`H176 done`. `H176` is the tag of the validation job that produced these images,
and nothing more. Integers are decimal, registers `0x`-prefixed hexadecimal.
Before the script runs, MicroPython prints its banner, `Type "help()" for more
information.` and the `>>> ` prompt; those are the only untagged lines.

| Kind | Per image | Fields |
|---|---|---|
| `boot` | 1 | `mpy`, `sysclk`, `hclk`, `pclk1`, `pclk2`, `cfgr`, `ahb1enr`, `apb2enr`, `uart`, `port`, `pin`, `ns`, `spb` |
| `cfg` | 5 | `id`, `baud`, `bits`, `parity`, `stop`, `brr`, `cr1`, `cr2`, `cr3`, `period`, `timclk`, `moder`, `afr`, `rd`, `cl`, `rd_cyc`, `cl_cyc` |
| `rep` | 145, 29 per `cfg` | `id`, `rep`, `phase`, `t0`, `tc`, `tr`, then `tb`, `low`, `te`, `cyc`, `cyc_all`, `w1`, `w2`, `w3` (`phase=main`) or `tsbk`, `sbk`, `cyc`, `w1` (`phase=sbk`), then `flags`, `rem`, `n`, `first`, `moder`, `afr`, `ahb1enr` |
| `edges` | 265 | `id`, `rep`, `phase`, `seq`, `i`: up to 16 edge indices per line |
| `done` | 1 | none |
| `fault` | 0 | `err`; printed only if the script raises |

In a `rep` record, `t0`, `tc`, `tr`, `tb`, `te` and `tsbk` are sample indices;
`cyc` is the DWT cycles from just before the call to just after it, marks
included; `n` is the number of edges in the window, `first` the pin's level in
its first sample, `rem` the stream's remaining transfers after the window (0
when every sample was written) and `flags` the stream's status bits in `LISR`
(0x30 is transfer complete and half transfer, with no error); `low` and `sbk`
are 1 when the wait for the low pin or for the clear bit ended because it was
seen. An edge index is the first sample that shows the new level.

## What the records were expected to show

If the documentation of `pyb.UART` were right, the bus would be low for 13 bit
times. From the reference manual, it was expected to be low for one frame: 10 bit
times at 8N1, 11 at 8E1, where the port sets `M` because it counts the parity bit
in the word length (`cr1=0x0000342c` against `0x0000202c`), and at 8N2 either 10
or 11, which is what that configuration decides. After the break, the USART was
expected to insert at least one bit time of logic 1 before the byte written
during it, and that byte to come out whole. Where the return falls against the
break was an open question, and `tr` answers it.

The script's own checks have to hold in every repetition: 24 edges in a main
window and 12 in an `sbk` one, `rem` 0, `flags` 0x30, `first` 1, PA0 in
alternate function 8, and the `0x55` and `0x00` frames decoding as such.

## Results

[Run 38167699110](https://github.com/agentic-hil/agentic-hil/actions/runs/38167699110)
of the hardware-bench workflow on the branch `bench/hilval-hq176`, at commit
`ebd3eeaf`, ran both plans on the Agentic HIL project's NUCLEO-F446RE with
Agentic HIL 0.21.4 and the bench configuration
`sha256:9e0b4615276e15b64107b9a0e09044c5c202265bbe5e337424c9b915ae67652c`, and
each image reached its `H176 done` line, `baseline` after 29.1 s and `patched`
after 27.1 s. Its evidence is in
[results/run-38167699110/](results/run-38167699110/), as the run uploaded it
(artifact `hilval-hq176-evidence`,
`sha256:2eac62b9952a31d97d5c34d5775ac7619bae52d0b53a7dfd9f17ec4222245479`),
with the probe's serial number and the machine's names withheld:
`reports/<image>.json` is the plan's report, every step with what Agentic HIL
did and saw, and `evidence/<image>/` holds the run summary and the OpenOCD and
serial logs the report names. `demo` is the demo firmware put back afterwards.
The reports record the SHA-256 of the plan they followed (`test_config_sha256`)
and of the file they flashed; those are the sums of [plans/](plans/) and
[firmware/SHA256SUMS](firmware/SHA256SUMS), so the plans and images in this
directory are byte for byte the ones the run used.

Each log holds 417 tagged lines, one `boot`, five `cfg`, 145 `rep`, 265 `edges`
and `H176 done`, no `fault`, and the three untagged lines of MicroPython's
banner and prompt. The tables below are what
[results/summarize.py](results/summarize.py) prints from the two logs, with the
two images side by side.

**The configurations**, the same in both images (`BRR` is `PCLK1 / baud` with
`OVER8` 0; 0x088b gives 19204 baud):

| id | baud | format | `BRR` | `CR1`, `CR2` | TIM8 period, ticks | repetitions (main + `sbk`) |
|---|---|---|---|---|---|---|
| 0 | 1200 | 8N1 | 0x88b8 | 0x202c, 0x0 | 4375 | 24 + 5 |
| 1 | 9600 | 8N1 | 0x1117 | 0x202c, 0x0 | 547 | 24 + 5 |
| 2 | 19200 | 8N1 | 0x088b | 0x202c, 0x0 | 273 | 24 + 5 |
| 3 | 9600 | 8E1 | 0x1117 | 0x342c, 0x0 | 547 | 24 + 5 |
| 4 | 9600 | 8N2 | 0x1117 | 0x202c, 0x2000 | 547 | 24 + 5 |

**The break**, over all 29 repetitions of a configuration. The bit time is the
8 bit times from the start edge of the first `0x55` to the falling edge of its
last data bit; the break is the low run that the call produced; the calculated
length is the whole number of bit times the board showed, from `BRR` and PCLK1:

| id | format | bit, samples | break, samples | break, bit times | calculated, us | observed, us, `baseline` (min / mean / max) | observed, us, `patched` |
|---|---|---|---|---|---|---|---|
| 0 | 1200 8N1 | 32.000 | 320 | 10.000 | 8333.3 | 8333.3 / 8333.3 / 8333.3 | 8333.3 / 8333.3 / 8333.3 |
| 1 | 9600 8N1 | 31.875 to 32.000 | 319 to 320 | 9.969 to 10.039 | 1041.7 | 1038.6 / 1041.8 / 1041.9 | 1041.9 / 1041.9 / 1041.9 |
| 2 | 19200 8N1 | 32.000 to 32.125 | 320 to 321 | 9.961 to 10.031 | 520.7 | 520.0 / 520.7 / 521.6 | 520.0 / 520.7 / 521.6 |
| 3 | 9600 8E1 | 31.875 to 32.000 | 351 to 352 | 10.969 to 11.043 | 1145.8 | 1146.1 / 1146.1 / 1146.1 | 1142.8 / 1146.0 / 1146.1 |
| 4 | 9600 8N2 | 31.875 to 32.000 | 319 to 320 | 9.969 to 10.039 | 1041.7 | 1038.6 / 1041.8 / 1041.9 | 1041.9 / 1041.9 / 1041.9 |

Rounded to whole bit times, the break is 10 in every repetition of
configurations 0, 1, 2 and 4 and 11 in every repetition of configuration 3, in
both images; `baseline` and `patched` agree repetition by repetition.

**Around the break**, ranges over both images:

| id | break starts after the return, bit times | break ends after the return, bit times | `cyc`, us | logic 1 after the break, bit times | `CR1.SBK` read clear, bit times after the break's end |
|---|---|---|---|---|---|
| 0 | 0.97 to 1.00 | 10.97 to 11.00 | 14.64 to 16.13 | 1.00 | 0.00 |
| 1 | 0.75 to 0.78 | 10.75 to 10.82 | 14.66 to 14.77 | 1.00 | 0.00 to 0.03 |
| 2 | 0.53 to 0.59 | 10.49 to 10.59 | 14.66 to 14.83 | 1.00 to 1.03 | 0.00 to 0.03 |
| 3 | 0.75 to 0.78 | 11.75 to 11.80 | 14.66 to 14.78 | 1.00 | 0.00 to 0.03 |
| 4 | 0.75 to 0.78 | 10.75 to 10.82 | 14.65 to 14.78 | 2.00 to 2.01 | 0.00 to 0.03 |

In every one of the 290 repetitions: the edge count was as expected, `rem` 0,
`flags` 0x30, `first` 1, PA0 in alternate function 8; every main window decoded
as `0x55`, `0x00`, `0x55` with correct start, parity and stop bits, every `sbk`
window as `0x55`, and every `sbk` poll saw the bit clear. In the controls, a mark
took 0 to 2 samples and 1273 to 1283 cycles, and `u.any()` 1 to 5 samples and
1243 to 1253 cycles.

In words. The break lasted one frame with a single stop bit: 10 bit times at 8N1
and at 8N2, 11 at 8E1, never 13. The second stop bit did not lengthen it; the
parity bit did. `sendbreak()` returned before the line went low: the return mark
lies 0.53 to 1.00 bit time ahead of the falling edge, and the call with its two
marks took about 15 us. `CR1.SBK` read clear again at the end of the low run.
After the break the line was high for one bit time, two at 8N2, and the byte
written during the break followed whole, in the configured format. The image
with the documentation change measured the same in every repetition.

Against the issues: #8646 measured 520 us with an oscilloscope on the pyboard's
UART1 at 19200 baud, where 13 bits would be 677 us; the 10 bit times measured
here at 19200 baud 8N1 are 520.7 us. #8794 reports 11 bit times, 9.167 ms, at
1200 baud for SDI-12, without naming its board or frame format; 11 is what this
board shows with a parity bit in the word (8E1). SDI-12's 7 data bits are not
measured here.

### Reproducing the tables

```bash
python3 results/summarize.py \
  baseline=results/run-38167699110/evidence/baseline/logs/com-20261011T192213636Z-dut_uart.jsonl \
  patched=results/run-38167699110/evidence/patched/logs/com-20261011T192254589Z-dut_uart.jsonl
```

The script joins the received bytes of each log, keeps the tagged lines and
lists the untagged ones, prints the `boot`, `cfg` and `done` lines as they are,
and one row per configuration and image with every value as a single number or
as `min..max`. It decodes every frame of every window at the middle of its bits.
It prints no verdict.

## The test plans

Each plan in [plans/](plans/) flashes its image without a reset, opens the
ST-LINK's virtual COM port with a cleared buffer, resets the board and records
the port until a line is exactly `H176 done`, with a 180 s limit, and closes the
port. The plans judge only that the image ran to its last line; every value
above was judged from the recorded lines afterwards. The plans were generated by
the pipeline, which their header comment names, and were written for and checked
with Agentic HIL 0.21.4.

## Running the plans

With your own NUCLEO-F446RE and an Agentic HIL configuration that
`agentic-hil init` wrote in [examples/nucleo-f446re_demo](..), binding `dut` to
the ST-LINK and `dut_uart` to its virtual COM port, run from that directory:

```bash
mkdir -p build/hilval-hq176
cp micropython-sendbreak/firmware/*.elf build/hilval-hq176/
(cd build/hilval-hq176 && sha256sum -c ../../micropython-sendbreak/firmware/SHA256SUMS)
agentic-hil test-reactor --test-config micropython-sendbreak/plans/baseline.testconfig.yaml
agentic-hil test-reactor --test-config micropython-sendbreak/plans/patched.testconfig.yaml
```

The plans flash from `build/hilval-hq176/`, which lies inside the artifact
roots Agentic HIL allows by default. Each image runs for about 30 s. The serial
log of each run is the `com-*-dut_uart.jsonl` its report names, and
`results/summarize.py` reads it as above.

Without Agentic HIL: flash an image with the tool of your choice, open the
virtual COM port at 115200 baud, 8N1, reset the board and read until
`H176 done`.

## Building the images

[firmware/](firmware/) holds the two images as built, stripped of debug
information with their symbol tables kept, and their
[SHA-256 sums](firmware/SHA256SUMS). They were built from:

- MicroPython `23f0e61b47f0adf9a8f1259ec603892f27a69eb7` with the submodules
  `lib/stm32lib` at `07619754261b571fb2f7c9f36ab94c203880d9fb` (STM32F4 HAL
  1.7.1, CMSIS device files 2.6.1) and `lib/CMSIS_6` at
  `45dab712ad84f8cbbf2b7bfc089c19088507df6f`, unmodified for `baseline`; for
  `patched`, a copy of that tree without `.git` with the patch applied by
  `git apply`.
- GNU Tools for STM32 14.3.rel1 (GCC 14.3.1 20250623), whose `objcopy` also did
  the strip.

For each image, from the MicroPython tree, with a manifest that holds the one
line `freeze_as_str('<path of micropython/>')`:

```bash
make -C ports/stm32 BOARD=NUCLEO_F446RE BUILD=<out>/build \
  FROZEN_MANIFEST=<out>/manifest.py MICROPY_MPYCROSS_DEPENDENCY= \
  CROSS_COMPILE=arm-none-eabi- \
  CFLAGS_EXTRA="-DMICROPY_MODULE_FROZEN_STR=1 <prefix maps>"
arm-none-eabi-objcopy --strip-debug <out>/build/firmware.elf firmware/baseline.elf
```

`freeze_as_str` freezes `main.py` as source text, so no `mpy-cross` is built;
the build only needs `MICROPY_MPYCROSS` to name an existing file, and an empty
one does. Nothing else is frozen. The prefix maps are `-ffile-prefix-map` and
`-fmacro-prefix-map` for the directory of the validation job's repository (to
`.`), the MicroPython tree (to `/micropython`) and the output directory (to
`/build/0`), so the images carry no path of the machine that built them.
Byte-for-byte reproducibility of a rebuild has not been checked; MicroPython
also records the build date in its banner.

## How this was made

This report is part of the Agentic HIL project's upstream work, and we maintain
Agentic HIL. AI coding agents, run by our development pipeline, wrote the
measurement script, the patch and its text, the test suite that judges the
recorded lines, this README and `results/summarize.py`, and built the images;
the pipeline also generated the plans. Agentic HIL carried out every step on
the board: it flashed each image, reset the board and recorded the serial port
until the script's last line, and no step reached the board any other way. The
suite then judged the recorded lines, and `results/summarize.py` reproduces the
tables above from the logs. When this report was published, no person had
reviewed the script, the patch or the analysis. MicroPython takes pull requests
only from a person who has read and validated them; our maintainer submits this
change in person after reading the patch and this evidence, and answers the
review.

## Licensing

[micropython/main.py](micropython/main.py), the plans and the scripts are
Apache-2.0, like this repository. The patch is a change to MicroPython's
documentation and is offered under MicroPython's terms, MIT. The images also
contain MicroPython, STMicroelectronics' HAL and further components under
their own terms; see
[firmware/THIRD_PARTY_NOTICES.md](firmware/THIRD_PARTY_NOTICES.md).
