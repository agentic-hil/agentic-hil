# Zephyr #120620 on a NUCLEO-F446RE

A hardware validation of the open Zephyr pull request
[zephyrproject-rtos/zephyr#120620](https://github.com/zephyrproject-rtos/zephyr/pull/120620),
"drivers: clock_control: stm32f4: enable over-drive above 168 MHz", which
addresses [zephyrproject-rtos/zephyr#120619](https://github.com/zephyrproject-rtos/zephyr/issues/120619).
It tests the change as it stands; it is not a fix of its own.

The runs are driven by [Agentic Hardware-in-the-Loop (Agentic HIL)](https://github.com/agentic-hil/agentic-hil):
its test reactor flashes each image, resets the board with the serial port
already open, and judges the firmware's answers on that port against the plans
in [plans/](plans/), and `agentic-hil run-evidence` turns each run into a
report a reader without the board can check.

## What the change does

On STM32F4, `drivers/clock_control/clock_stm32f2_f4_f7.c` now enables the
over-drive regulator whenever `CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC` is above
168 MHz: after enabling the PLL it sets `PWR_CR.ODEN` and waits for
`PWR_CSR.ODRDY`, then sets `PWR_CR.ODSWEN` and waits for `PWR_CSR.ODSWRDY`.
The same `stm32_clock_control_init()` runs at boot and again from
`pm_state_exit_post_ops()` after every Stop mode exit, so the change has to
hold in both places.

## The three images

| Image | Zephyr | SYSCLK | Must show |
|---|---|---|---|
| `pr-180` | PR head `4e25f4f8d6a7` | 180 MHz | over-drive on: ODEN, ODSWEN, ODRDY, ODSWRDY all 1 |
| `pr-168` | PR head `4e25f4f8d6a7` | 168 MHz | over-drive off: all four 0 (control) |
| `base-168` | PR base `9f0253dcc66c` | 168 MHz | over-drive off: all four 0 (control) |

At 168 MHz the change does not act, and the two control images run the same
instructions: they differ only in the variant and commit strings they report,
and the longer variant name moves the constants after it by two bytes. The base
is not run at 180 MHz: without over-drive that clocks the core outside the
F446's operating conditions, and the change can be validated without it.

## Board configuration and F446 edge cases

The settings are the F446's own, from its reference manual (RM0390) and
datasheet, not copied from an F429 configuration.

- **Clock source.** HSE in bypass mode, 8 MHz from the on-board ST-LINK, as the
  board's devicetree configures it.
- **PLL.** M 4 for a 2 MHz VCO input; N 180 (VCO 360 MHz) or 168 (VCO
  336 MHz); P 2 for 180 or 168 MHz. Q 8 or 7 (45 or 48 MHz); nothing in these
  images is clocked from Q. See [app/clock-180.overlay](app/clock-180.overlay)
  and [app/clock-168.overlay](app/clock-168.overlay).
- **Buses.** AHB /1; APB1 /4 for 45 or 42 MHz; APB2 /2 for 90 or 84 MHz. With
  over-drive the F446 allows 45 and 90 MHz, without it 42 and 84 MHz, so each
  image stays inside its own limits.
- **Regulator.** 180 MHz needs over-drive and voltage scale 1. `PWR_CR.VOS`
  resets to scale 1 on the F446 and Zephyr's STM32F4 clock code does not change
  it. The plans check VOS 3 and VOSRDY 1 whenever the PLL runs rather than
  assume it.
- **Flash.** Five wait states at both 168 and 180 MHz for a supply of 2.7 to
  3.6 V. The plans check `FLASH_ACR.LATENCY` and that VDDA, measured against the
  internal reference and its factory calibration, lies in that range. VDD itself
  is not measured.
- **Stop mode exit.** The core leaves Stop mode on HSI with the PLL off, and the
  hardware clears ODEN and ODSWEN, so the driver has to run the over-drive
  sequence again after every exit. ODRDY and ODSWRDY are left unjudged at that
  moment.
- **Wake-up source.** Stop mode is entered by Zephyr's own power management
  (`PM_STATE_SUSPEND_TO_IDLE`), with the RTC as system timer companion
  ([app/pm.overlay](app/pm.overlay)). The RTC runs on LSI, as the board
  configures it, and its alarm A on EXTI line 17 wakes the core. At every Stop
  entry the firmware checks that line 17 is the only EXTI line unmasked, that
  no event line is, and that alarm A and its interrupt are armed; Stop mode
  wakes on EXTI lines only.
- **Debug.** `DBGMCU_CR.DBG_STOP` keeps clocks running in Stop mode for a
  debugger, and OpenOCD's STM32F4 target script sets it when it attaches. The
  firmware clears it before the cycles, so the core enters real Stop mode,
  checks that at every entry, and sets it again once the cycles are over.
- **Watchdog.** During the cycles the probe cannot reach the core, so the
  firmware runs the independent watchdog (LSI, about 6 s). A core that stays in
  Stop mode is reset, the next boot skips the cycles and reports
  `iwdg_reset=1`, and the plan fails at its first read.
- **Reference clock.** The firmware starts the board's 32.768 kHz LSE crystal
  for the clock count below, retrying with high drive if it does not start
  within 2 s. The RTC stays on LSI.

## How the firmware observes the exit path

[app/src/main.c](app/src/main.c) changes nothing on the path under test. Two
functions are wrapped with the linker's `--wrap`
([app/CMakeLists.txt](app/CMakeLists.txt)), and each wrapper reads the
registers and calls the real function:

- `stm32_clock_control_init()`: only the call from `pm_state_exit_post_ops()`
  reaches the wrapper, since the boot-time call is made inside the driver's own
  translation unit. It records the registers as Stop mode left them and right
  after the restore, the exception it runs in (`IPSR`), whether EXTI line 17 is
  still pending, and how many RTC alarm handlers ran since the Stop entry.
- `stm32_exti_clear_pending()`: called by the RTC alarm's handler for EXTI
  line 17. It records whether the line is pending, the system clock source and
  how many restores ran since the Stop entry.

The order of the two is Zephyr's. STM32F4 selects
`CONFIG_PM_STATE_SET_IRQ_UNLOCKED`, so `_kernel.idle` is still set while
`pm_state_set()` sleeps, and `k_cpu_idle()` unmasks interrupts as soon as the
core is awake. The RTC alarm interrupt that woke it is taken at once, and
`_isr_wrapper()` calls `pm_system_resume()` before the alarm's handler: the
restore runs inside that interrupt, on HSI, and the handler after it, on the
PLL. The plans judge that order at every exit: the restore with `IPSR` 57
(`RTC_Alarm_IRQn` 41 plus 16), line 17 pending and no handler run yet, and the
handler with line 17 pending, one restore run and the PLL as system clock.

A `pm_notifier` records the wake-up configuration at each Stop entry, and the
main thread records the registers again once it runs. The registers recorded
are `PWR_CR`, `PWR_CSR`, `RCC_CR`, `RCC_PLLCFGR`, `RCC_CFGR` and `FLASH_ACR`,
each answer giving the decoded fields and the raw values.

From five seconds after reset the firmware sleeps six times for 1.5 s, and the
idle thread enters Stop mode each time. It then prints `Z120620 ready ... end`, locks Stop mode out and answers
one line per request on the console UART (USART2, the ST-LINK's virtual COM
port, 115200 baud), so a plan reads every value without racing the output:

| Request | Answer |
|---|---|
| `hello` | variant, Zephyr commit, devicetree SYSCLK, reset flags, DBGMCU at `main()`, LSE state |
| `boot` | registers at the start of `main()`, after the clock driver configured the clocks |
| `cycle k` | Stop entry checks, the exception the restore ran in and its return value, the RTC alarm handler after it, and the main thread's state for cycle `k` |
| `exit k` | registers as Stop exit `k` left them, before the driver ran |
| `after k` | registers right after the driver restored the clocks at exit `k` |
| `done` | totals and every-cycle checks since boot |
| `supply` | VDDA in mV and the reference readings behind it |
| `clock k` | core and TIM5 clocks after boot (`k` 0) or after cycle `k` |

Every answer is one line of at most 512 bytes, starting `Z120620` and ending
`end`. Lines starting `Z120620 log` go out during boot and the cycles and are
not judged.

## Register finding and measured frequency

The plans judge two separate things, and a report keeps them separate:

- **Registers.** The over-drive bits, regulator, clock source, PLL settings,
  wait states and prescalers above, as the firmware reads them back.
- **Frequency.** The core clock (DWT cycle counter) and TIM5 (twice the APB1
  clock) are counted over 2048 periods of the LSE crystal, and must lie within
  500 ppm of nominal after boot and after every cycle. The LSE is a separate
  crystal from the ST-LINK's 8 MHz, so a wrong source or PLL setting shows; but
  it is a second crystal on the same board, not an independent instrument. No
  external frequency measurement was made.

## The test plans

[plans/generate.py](plans/generate.py) writes all three plans from one
description; change it and run it again rather than editing a plan. Each plan
flashes its image without a reset, opens the port with a cleared buffer, resets
the board, waits for `ready`, and then asks for every value twice. The first
block puts every answer on record with a lenient pattern, so the report quotes
what the board said whatever the second block finds. The second block judges
the same answers and stops at the first that does not hold. The plan format
caps a list at 128 steps, so each block is a `repeat` that runs once. The plans were
written for and checked with Agentic HIL 0.21.4.

## Running the plans

On this branch, [.github/workflows/hardware-bench.yml](../../../.github/workflows/hardware-bench.yml)
runs the three plans one after another on the Agentic HIL project's own
NUCLEO-F446RE, collects each run's evidence, and puts the demo firmware back.

With your own NUCLEO-F446RE and an Agentic HIL configuration that
`agentic-hil init` wrote in [examples/nucleo-f446re_demo](..), binding `dut` to
the ST-LINK and `dut_uart` to its virtual COM port, run from that directory:

```bash
mkdir -p build/zephyr-120620
cp zephyr-120620/firmware/*.elf build/zephyr-120620/
(cd build/zephyr-120620 && sha256sum -c ../../zephyr-120620/firmware/SHA256SUMS)
agentic-hil test-reactor --test-config zephyr-120620/plans/pr-180.testconfig.yaml
```

The plans flash from `build/zephyr-120620/`, which lies inside the artifact
roots Agentic HIL allows by default.

## Building the images

[firmware/](firmware/) holds the three images as built, stripped of symbols,
with their [SHA-256 sums](firmware/SHA256SUMS). They were built from:

- Zephyr `4e25f4f8d6a724ce4dc8fa35618adbe3443b6a4b` (the pull request's head)
  for `pr-180` and `pr-168`, and `9f0253dcc66ccecc92a699dd81cb1034c3f6875b`
  (its base) for `base-168`, both Zephyr 4.4.99.
- The modules both commits' `west.yml` name: hal_stm32
  `c9550199186236d610092f4005f8c9bae2a6bfcf` (STM32CubeF4 1.28.3), cmsis_6
  `1c1840af7a7e757d6e2fec3ddb0e5ce0dfcc93c8` and cmsis
  `512cc7e895e8491696b61f7ba8066b4a182569b8`.
- GNU Tools for STM32 14.3.rel1 (arm-none-eabi-gcc 14.3.1, from STM32CubeCLT
  1.22.0) as `gnuarmemb`, CMake 4.3.1 and Ninja 1.13.2.

For each image, with `ZEPHYR_BASE` at the matching Zephyr checkout, from this
directory:

```bash
cmake -S app -B build/pr-180 -GNinja -DBOARD=nucleo_f446re \
  -DZEPHYR_TOOLCHAIN_VARIANT=gnuarmemb -DGNUARMEMB_TOOLCHAIN_PATH=<toolchain> \
  -DZEPHYR_MODULES="<hal_stm32>;<cmsis_6>;<cmsis>" \
  -DEXTRA_DTC_OVERLAY_FILE="pm.overlay;clock-180.overlay" \
  -DZ120620_VARIANT=pr-180 -DZ120620_ZEPHYR=4e25f4f8d6a7
cmake --build build/pr-180
arm-none-eabi-strip --strip-all -o firmware/pr-180.elf build/pr-180/zephyr/zephyr.elf
```

`pr-168` uses `clock-168.overlay` and the same commit; `base-168` uses
`clock-168.overlay`, the base checkout and `-DZ120620_ZEPHYR=9f0253dcc66c`.
Byte-for-byte reproducibility of a rebuild has not been checked.

## Licensing

The test firmware sources and the plans are Apache-2.0, like this repository.
The images also contain Zephyr, CMSIS and STMicroelectronics code under their
own terms; see [firmware/THIRD_PARTY_NOTICES.md](firmware/THIRD_PARTY_NOTICES.md).
