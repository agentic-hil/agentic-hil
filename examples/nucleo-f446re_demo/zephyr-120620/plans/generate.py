"""Write this validation's three test plans from one description.

One plan per image. Each flashes its image, resets the board with the port
already open, waits for the firmware to finish its Stop mode cycles, and then
asks for every value twice: once to put the answer on record, once to judge it.

    python3 generate.py

writes pr-180.testconfig.yaml, pr-168.testconfig.yaml and
base-168.testconfig.yaml beside this file. Change this file and run it again
rather than editing the plans. Standard library only.

SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

CYCLES = 6
CYCLES_WORD = "six"
# Bounds for the clocks counted against the LSE crystal, either side of nominal.
PPM = 500
READ_TIMEOUT_S = 5
# The firmware says `ready` about sixteen seconds after the reset.
READY_TIMEOUT_S = 60
# VDDA bounds, in millivolts: the supply range five flash wait states assume.
SUPPLY_MV = (2700, 3600)
WRAP = 96

VARIANTS: dict[str, dict[str, Any]] = {
    "pr-180": {
        "zephyr": "4e25f4f8d6a7",
        "mhz": 180,
        "od": 1,
        "title": "the pull request at 180 MHz, where it switches over-drive on",
    },
    "pr-168": {
        "zephyr": "4e25f4f8d6a7",
        "mhz": 168,
        "od": 0,
        "title": "the pull request at 168 MHz, where over-drive stays off (control)",
    },
    "base-168": {
        "zephyr": "9f0253dcc66c",
        "mhz": 168,
        "od": 0,
        "title": "the pull request's base at 168 MHz, without the change (control)",
    },
}

HEX8 = r"0x[0-9a-f]{8}"
RAW = " ".join(f"{name}={HEX8}" for name in ("PWR_CR", "PWR_CSR", "RCC_CR", "RCC_PLLCFGR", "RCC_CFGR", "FLASH_ACR"))

# At a Stop exit, before the clock driver runs: what the hardware itself did.
EXIT_REGS = (
    r"ODEN=0 ODSWEN=0 ODRDY=[01] ODSWRDY=[01] VOS=\d+ VOSRDY=[01] SWS=0 PLLRDY=0 "
    r"HSEBYP=[01] PLLSRC=[01] PLLM=\d+ PLLN=\d+ PLLP=\d+ LATENCY=\d+ AHB_DIV=\d+ "
    r"APB1_DIV=\d+ APB2_DIV=\d+ " + RAW
)


def running_regs(od: int, plln: int) -> str:
    """The state this image must be in whenever its PLL runs: after boot,
    after each restore, and from the main thread."""
    return (
        f"ODEN={od} ODSWEN={od} ODRDY={od} ODSWRDY={od} VOS=3 VOSRDY=1 SWS=2 PLLRDY=1 "
        f"HSEBYP=1 PLLSRC=1 PLLM=4 PLLN={plln} PLLP=2 LATENCY=5 AHB_DIV=1 APB1_DIV=4 "
        f"APB2_DIV=2 " + RAW
    )


def bounds(nominal: int) -> dict[str, int]:
    return {"min": nominal - nominal * PPM // 1_000_000, "max": nominal + nominal * PPM // 1_000_000}


def request(command: str, pattern: str, value_range: dict[str, int] | None = None) -> list[dict[str, Any]]:
    comparator: dict[str, Any] = {"pattern": pattern}
    if value_range is not None:
        comparator["range"] = value_range
    return [
        {"device": "dut_uart", "action": "uart_write", "text": f"{command}\n"},
        {"device": "dut_uart", "action": "uart_read", "timeout_s": READ_TIMEOUT_S, "comparator": comparator},
    ]


def on_record(command: str, head: str) -> list[dict[str, Any]]:
    """Ask once and keep whatever the board answers: any one whole line."""
    return request(command, rf"Z120620 {head} [^\r\n]* end")


def plan(variant: str) -> list[Any]:
    """The plan's steps, with comment strings between them."""
    v = VARIANTS[variant]
    hz = v["mhz"] * 1_000_000
    od, od_hex = v["od"], "0xf" if v["od"] else "0x0"
    running = running_regs(od, v["mhz"])
    core, tim5 = bounds(hz), bounds(hz // 2)
    ks = range(1, CYCLES + 1)

    record: list[Any] = ["hello, boot, supply, and the clocks measured after boot"]
    for command, head in (("hello", "hello"), ("boot", "boot"), ("supply", "supply"), ("clock 0", "clock=0")):
        record += on_record(command, head)
    for k in ks:
        record.append(f"Stop cycle {k}")
        for command, head in (("cycle", "cycle"), ("exit", "exit"), ("after", "after"), ("clock", "clock")):
            record += on_record(f"{command} {k}", f"{head}={k}")
    record.append("the summary over every Stop entry and exit since boot")
    record += on_record("done", "done")

    judged: list[Any] = ["the image, its build and a clean boot"]
    judged += request(
        "hello",
        rf"Z120620 hello variant={variant} zephyr={v['zephyr']} board=nucleo_f446re "
        rf"sysclk_dt_hz={hz} rcc_csr={HEX8} iwdg_reset=0 dbgmcu_at_main={HEX8} lse=[a-z-]+ end",
    )
    judged.append("the state after boot")
    judged += request("boot", f"Z120620 boot {running} end")
    judged.append("the state right after the clock driver restored the clocks, at each Stop exit")
    for k in ks:
        judged += request(f"after {k}", f"Z120620 after={k} {running} end")
    judged.append(
        "each cycle: entered Stop mode with RTC alarm A as the only wake-up source and "
        "the debug Stop clock off, woken by that alarm while still on HSI, restored "
        "without an error, and the main thread back on the PLL"
    )
    for k in ks:
        judged += request(
            f"cycle {k}",
            rf"Z120620 cycle={k} entries=([1-9]) restores=\1 rtc_irqs=\1 entry_exti_imr=0x00020000 "
            r"entry_exti_emr=0x00000000 entry_alarm_a=1 entry_rtc_alarm_nvic=1 "
            r"entry_dbgmcu=0x[0-9a-f]{7}[0189] irq_exti_pr17=1 irq_SWS=0 restore_ret=0 "
            rf"main_od={od_hex} main_SWS=2 end",
        )
    judged.append("the state each Stop exit left, before the clock driver ran: ODEN and ODSWEN cleared, HSI, PLL off")
    for k in ks:
        judged += request(f"exit {k}", f"Z120620 exit={k} {EXIT_REGS} end")
    judged.append("the same over every Stop entry and exit since boot, and none outside the cycles")
    judged += request(
        "done",
        rf"Z120620 done variant={variant} cycles={CYCLES} entries=([6-9]|[1-9][0-9]+) exits=\1 "
        r"rtc_irqs=\1 other_entries=0 outside_cycles=0 exit_od_or=0x[048c] "
        rf"after_od_and={od_hex} after_od_or={od_hex} every_exit_on_hsi=1 "
        r"every_exit_after_rtc_irq=1 every_after_on_pll=1 every_entry_wake_rtc_alarm_only=1 "
        r"every_entry_debug_stop_off=1 every_rtc_irq_pr17_on_hsi=1 watchdog=on end",
    )
    judged.append("VDDA, from the internal reference and its factory calibration")
    judged += request(
        "supply",
        r"Z120620 supply vdda_mv=(\d+) vrefint_cal=\d+ vrefint_avg=\d+ end",
        {"min": SUPPLY_MV[0], "max": SUPPLY_MV[1]},
    )
    judged.append(f"the core clock after boot and after every cycle, within {PPM} ppm of {v['mhz']} MHz")
    for k in range(0, CYCLES + 1):
        judged += request(f"clock {k}", rf"Z120620 clock={k} core_hz=(\d+) tim5_hz=\d+ status=0 end", core)
    judged.append(f"TIM5, twice the APB1 clock, within {PPM} ppm of {v['mhz'] // 2} MHz")
    for k in range(0, CYCLES + 1):
        judged += request(f"clock {k}", rf"Z120620 clock={k} core_hz=\d+ tim5_hz=(\d+) status=0 end", tim5)

    return [
        "Flashed without a reset, so the port is open before the image starts.",
        {"device": "dut", "action": "flash", "image_path": f"build/zephyr-120620/{variant}.elf", "reset_after_flash": False},
        "The cleared buffer makes every answer below one from this boot.",
        {"device": "dut_uart", "action": "uart_open", "clear_buffer": True},
        {"device": "dut", "action": "reset", "mode": "run"},
        "Five seconds after the reset the image starts its Stop cycles, 1.5 s asleep "
        "each, and says `ready` once they are done. An image the watchdog reset "
        "skips them and says `cycles=0 iwdg_reset=1`, which fails here.",
        {
            "device": "dut_uart",
            "action": "uart_read",
            "timeout_s": READY_TIMEOUT_S,
            "comparator": {"pattern": f"Z120620 ready variant={variant} cycles={CYCLES} iwdg_reset=0 end"},
        },
        "Block 1, on record: every answer once, with any one whole line accepted.",
        {"action": "repeat", "count": 1, "steps": record},
        "Block 2, judged: the same answers against what this image must show.",
        {"action": "repeat", "count": 1, "steps": judged},
    ]


def steps_only(items: list[Any]) -> list[dict[str, Any]]:
    """The plan as the reactor reads it, with the comments left out."""
    out = []
    for item in items:
        if isinstance(item, dict):
            item = dict(item)
            if "steps" in item:
                item["steps"] = steps_only(item["steps"])
            out.append(item)
    return out


PLAIN = re.compile(r"^[A-Za-z0-9_./-]+$")


def scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if PLAIN.match(value) and value not in {"y", "n", "yes", "no", "on", "off", "true", "false", "null"}:
        return value
    return quoted(value)


def quoted(value: str) -> str:
    assert "'" not in value and "\n" not in value
    return f"'{value}'"


def folded(prefix: str, value: str, indent: int) -> list[str]:
    """`value` as a single-quoted scalar after `prefix`, folded at spaces.

    A line break inside a single-quoted YAML scalar reads back as one space, so
    a long pattern stays one value however many lines it takes here."""
    assert "'" not in value and "  " not in value and not value.startswith(" ") and not value.endswith(" ")
    words = value.split(" ")
    lines, line = [], f"{prefix}'{words[0]}"
    for word in words[1:]:
        if len(line) + 1 + len(word) > WRAP:
            lines.append(line)
            line = " " * indent + word
        else:
            line += " " + word
    lines.append(line + "'")
    return lines


def comment(text: str, indent: int) -> list[str]:
    pad = " " * indent + "# "
    lines, line = [], ""
    for word in text.split(" "):
        if line and len(pad) + len(line) + 1 + len(word) > WRAP:
            lines.append(pad + line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    lines.append(pad + line)
    return lines


def render(items: list[Any], indent: int) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    for item in items:
        if isinstance(item, str):
            lines += comment(item, indent)
            continue
        if item["action"] == "uart_write":
            text = item["text"].replace("\n", "\\n")
            lines.append(f'{pad}- {{device: {item["device"]}, action: uart_write, text: "{text}"}}')
            continue
        for index, (key, value) in enumerate(item.items()):
            lead = f"{pad}- " if index == 0 else f"{pad}  "
            if key == "steps":
                lines.append(f"{lead}steps:")
                lines += render(value, indent + 4)
            elif key == "comparator":
                lines.append(f"{lead}comparator:")
                for name, part in value.items():
                    if name == "pattern":
                        lines += folded(f"{pad}    pattern: ", part, indent + 6)
                    else:
                        lines.append(f"{pad}    {name}: {{min: {part['min']}, max: {part['max']}}}")
            else:
                lines.append(f"{lead}{key}: {scalar(value)}")
    return lines


def header(variant: str) -> list[str]:
    v = VARIANTS[variant]
    mhz = v["mhz"]
    on = v["od"] == 1
    state = "over-drive on (ODEN, ODSWEN, ODRDY and ODSWRDY all 1)" if on else "over-drive off (all four bits 0)"
    paragraphs = [
        f"zephyrproject-rtos/zephyr#120620 on a NUCLEO-F446RE: {v['title']}.",
        "Written by generate.py beside this file, which describes all three plans; "
        "change that and run it again rather than editing this file.",
        "The image boots at the clock configuration of its overlay and records the "
        "over-drive, regulator and clock tree state. It then enters Stop mode "
        f"{CYCLES_WORD} times, woken each time by the RTC alarm, and records the same state "
        "when the core leaves Stop mode, right after the clock driver restored the "
        "clocks, and once the main thread runs again. Then it says `ready` and "
        "answers one line per request; Stop mode stays locked from there on.",
        "Every value is asked for twice. The first block puts every answer on "
        "record with a lenient pattern, so the report quotes what the board said "
        "whatever the second block then finds. The second block judges the same "
        "answers against what this image must show and stops at the first that "
        "does not. The format caps a list of steps at 128, so each pass is a "
        "`repeat` block that runs once: nothing is reset or run again between them.",
        f"What this image must show: after boot and after every Stop exit, {state}, "
        "regulator Scale 1 (VOS 3, VOSRDY 1), the PLL as system clock from the 8 MHz "
        f"HSE bypass at M 4, N {mhz}, P 2, five flash wait states, AHB /1, APB1 /4 and "
        "APB2 /2. At every Stop exit, before the clock driver runs, ODEN and ODSWEN "
        "are clear and the core runs on HSI with the PLL off. Every Stop entry has "
        "RTC alarm A armed as the only wake-up source and the debug Stop clock off, "
        "and every exit follows that alarm's interrupt. VDDA is between 2.7 and 3.6 V. "
        f"The core clock is within {PPM} ppm of {mhz} MHz and TIM5 within {PPM} ppm of "
        f"{mhz // 2} MHz, after boot and after every cycle, both counted against the "
        "board's 32.768 kHz LSE crystal: a second crystal on the same board, not an "
        "independent instrument.",
    ]
    if not on:
        paragraphs.append(
            "The change only acts above 168 MHz, and at 168 MHz the pull request's "
            "image and its base's run the same instructions: they differ only in the "
            "variant and commit strings they report, and the longer variant name moves "
            "the constants after it by two bytes."
        )
    lines: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        if index:
            lines.append("#")
        lines += comment(paragraph, 0)
    return lines


def document(variant: str) -> str:
    lines = header(variant)
    lines += ["version: 4", f"name: zephyr-120620-{variant}", "steps:"]
    lines += render(plan(variant), 2)
    return "\n".join(lines) + "\n"


def main() -> None:
    for variant in VARIANTS:
        (HERE / f"{variant}.testconfig.yaml").write_text(document(variant), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
