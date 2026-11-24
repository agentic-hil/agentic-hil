"""Keep the opt-in F446RE plans, firmware fixture and qualification claims in agreement."""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest
import yaml

from agentic_hil.test_reactor import TestStep as Step
from agentic_hil.test_reactor import load_test_config

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "tests/bench/firmware/defmt_serial"
IMAGE = "tests/bench/firmware/defmt_serial/target/thumbv7em-none-eabihf/release/defmt-serial-f446re"
PLANS = {
    "session": "tests/bench/plans/defmt_serial_1_session.yaml",
    "read": "tests/bench/plans/defmt_serial_2_read.yaml",
    "expect": "tests/bench/plans/defmt_serial_3_expect.yaml",
}
BOOT_FORMAT = "F446RE boot"
PERIODIC_FORMAT = "F446RE sample {=u8} value {=u16}"
PERIODIC_TEXT = "F446RE sample 2 value 37"


def load_steps(plan: str) -> list[Step]:
    return load_test_config(PLANS[plan], str(ROOT)).steps


@pytest.mark.parametrize("plan", PLANS, ids=list(PLANS))
def test_plan_contract(plan: str) -> None:
    steps = load_steps(plan)
    header = yaml.safe_load((ROOT / PLANS[plan]).read_text(encoding="utf-8"))
    assert set(header) == {"version", "name", "steps"}
    assert header["version"] == 7

    flash, open_port, reset, *body, close = steps
    assert [flash.action, open_port.action, reset.action, close.action] == [
        "flash", "uart_open", "reset", "uart_close",
    ]
    assert all(step.device is None for step in steps)
    assert flash.debugger == reset.debugger == "f446re"
    assert all(step.port_id == "f446re_vcp" for step in [open_port, *body, close])
    assert flash.arguments["image_path"] == IMAGE
    assert flash.arguments["reset_after_flash"] is False
    assert open_port.arguments["defmt"] == {"image_path": flash.arguments["image_path"]}
    assert open_port.arguments["clear_buffer"] is True
    assert reset.arguments["mode"] == "run"

    if plan == "read":
        if body[0].action == "delay":
            delay, *body = body
            assert delay.arguments["duration_ms"] > 0
        read, *body = body
        assert read.action == "uart_read"
        assert {"comparator", "until"}.isdisjoint(read.arguments)

    selectors = []
    for step in body:
        assert step.action == "defmt_expect"
        timeout = step.arguments["timeout_s"]
        assert math.isfinite(timeout) and timeout > 0
        selectors.append({key: value for key, value in step.arguments.items() if key != "timeout_s"})

    assert selectors == {
        "session": [{"format": BOOT_FORMAT}],
        "read": [{"format": PERIODIC_FORMAT}],
        "expect": [
            {"level": "info"},
            {"format": PERIODIC_FORMAT},
            {"args": [{"equals": 2}]},
            {"args": [None, {"range": {"min": 35, "max": 37}}]},
            {"text": PERIODIC_TEXT},
        ],
    }[plan]


def test_firmware_matches_plans() -> None:
    manifest = tomllib.loads((FIRMWARE / "Cargo.toml").read_text(encoding="utf-8"))
    assert manifest["package"]["name"] == "defmt-serial-f446re"
    assert re.fullmatch(r"[~^]?1(?:\.\d+){0,2}", manifest["dependencies"]["defmt"])
    assert "defmt-serial" in manifest["dependencies"]

    source = (FIRMWARE / "src/main.rs").read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
    assert re.search(r"impl\s+embedded_io::Write\s+for\s+Usart2Tx", source)
    uart_init = source.split("fn new() -> Self {", 1)[1].split("impl embedded_io", 1)[0]
    for register in ("RCC_AHB1ENR", "RCC_APB1ENR", "GPIOA_MODER", "GPIOA_AFRL", "USART2_BRR"):
        assert f"write_volatile({register}," in re.sub(r"\(\s+", "(", uart_init)
    for name, value in {"HSI_HZ": 16_000_000, "BAUD": 115_200, "TX_PIN": 2, "USART2_AF": 7}.items():
        declaration = re.search(rf"\bconst\s+{name}\s*:\s*u32\s*=\s*([\d_]+)\s*;", source)
        assert declaration and int(declaration[1].replace("_", "")) == value
    assert re.search(r"USART2_BRR,\s*\(HSI_HZ\s*\+\s*BAUD\s*/\s*2\)\s*/\s*BAUD", uart_init)
    assert re.search(r"GPIOA_AFRL,.*USART2_AF\s*<<\s*af_shift", uart_init, flags=re.S)
    assert re.search(r"GPIOA_MODER,.*0x2\s*<<\s*mode_shift", uart_init, flags=re.S)
    assert re.search(r"write_volatile\(\s*USART2_DR,\s*u32::from\(byte\)\)", source)

    main = source.split("fn main()", 1)[1]
    logs = re.findall(r'defmt::(\w+)!\(\s*"([^"]+)"\s*(?:,\s*([^;]*?))?\s*\);', main)
    [(boot_level, boot_format, boot_args), (level, periodic_format, args)] = logs
    assert boot_level == "info"
    assert not boot_args
    setup = main.split("defmt::", 1)[0]
    assert re.search(
        r"let\s+tx\s*=\s*unsafe\s*\{\s*\(\*addr_of_mut!\(USART2_TX\)\)\.write\(Usart2Tx::new\(\)\)\s*\};"
        r"\s*defmt_serial::defmt_serial\(\s*tx\s*\);",
        setup,
    )
    before_loop, loop_body = main.split("loop {", 1)
    assert boot_format in before_loop and periodic_format in loop_body
    assert "wait_for_next_sample();" in loop_body

    [first, second] = [arg.strip() for arg in args.split(",")]
    first_arg = re.fullmatch(r"(\d+)u8", first)
    second_arg = re.fullmatch(r"(\d+)u16", second)
    assert first_arg and second_arg
    values = int(first_arg[1]), int(second_arg[1])
    rendered = periodic_format.replace("{=u8}", str(values[0])).replace("{=u16}", str(values[1]))

    assert load_steps("session")[-2].arguments["format"] == boot_format
    assert load_steps("read")[-2].arguments["format"] == periodic_format
    level_step, format_step, equal_step, range_step, text_step = load_steps("expect")[3:-1]
    assert level_step.arguments["level"] == level
    assert format_step.arguments["format"] == periodic_format
    assert equal_step.arguments["args"] == [{"equals": values[0]}]
    bound = range_step.arguments["args"][1]["range"]
    assert bound["min"] <= values[1] <= bound["max"]
    assert text_step.arguments["text"] == rendered


def explains_explicit_elf_source(passage: str, *, require_version: bool = True) -> bool:
    text = passage.replace("`", "").casefold()
    mapping = r"\bdefmt\s*:\s*\{\s*image_path\s*:\s*[^{}\s][^{}]*\}"
    # Keep placeholder dots and filename punctuation inside the example.
    example = "defmt: {image_path: <elf>}"
    text = re.sub(mapping, example, text)
    paragraphs = re.split(r"\n[ \t]*\n|(?m:^[ \t]*(?:[-+*]|\d+[.)])[ \t]+)", text)
    required = (
        r"\b(?:plan|job)s?\b",
        r"\b(?:flash(?:es|ed|ing)?|program(?:s|med|ming)?)\b",
        r"\b(?:multi[- ]debugger|(?:multiple|several|more than one|two or more)\s+(?:configured\s+)?debuggers?)\b",
        r"\buart_open\b",
        r"\bdefmt\s*:\s*true\b",
        r"\b(?:refus(?:e|es|ed|al)|reject(?:s|ed|ion)?|fail(?:s|ed|ure)?|return(?:s|ed)?)\b",
        r"\bdefmt_table_not_available\b",
        r"\b(?:so|therefore|thus|hence|as a result|which is why|because|instead|accordingly|requiring|prompting)\b",
        re.escape(example),
    )
    if require_version:
        required += (r"\bv?0\.28\.0\b",)
    for paragraph in paragraphs:
        sentences = re.split(r"(?<=[.!?])(?:[\"')\]]+)?\s+", " ".join(paragraph.split()))
        if any(all(re.search(pattern, sentence) for pattern in required) for sentence in sentences):
            return True
    return False


def describes_observations(passage: str, *, require_version: bool = True) -> bool:
    text = " ".join(passage.replace("`", "").casefold().split())
    boot = str(load_steps("session")[-2].arguments["format"]).casefold()
    rendered = str(load_steps("expect")[-2].arguments["text"]).casefold()
    read = r"(?:uart_read|com_read|(?:plain|comparator-free)\s+(?:uart\s+|serial\s+)?read)"
    periodic = r"\b(?:later|subsequent|periodic|another)\b"
    match = r"\b(?:defmt_expect|match(?:ed|es)?|assert(?:ed|s)?|decod(?:ed|es|ing))\b"
    periodic_match = rf"(?:{match}[^.]*?{periodic}|{periodic}[^.]*?{match})"
    read_then_expect = (
        rf"{read}[^.]*?(?:\bthen\b|\bfollowed by\b|\bafterward\b|\.\s+)[^.]*?{periodic_match}"
        rf"|{periodic_match}[^.]*?\bafter\b[^.]*?{read}"
    )
    serve = r"(?:agentic-hil\s+)?serve"
    release = (
        rf"\b{serve}\s+(?:version\s+)?v?0\.28\.0\b"
        if require_version
        else rf"\b(?:released\s+(?:version\s+of\s+)?{serve}|{serve}\s+release)\b"
    )
    return (
        "nucleo-f446re" in text
        and re.search(r"\bst-link\s+(?:vcp|virtual com port)\b", text) is not None
        and re.search(release, text) is not None
        and boot in text
        and rendered in text
        and re.search(r"\b(?:reset|reboot)\b", text) is not None
        and re.search(read_then_expect, text) is not None
        and all(re.search(selector, text) for selector in (
            r"\b(?:log\s+)?level\b",
            r"\b(?:exact\s+)?format(?:\s+string)?\b",
            r"\b(?:argument\s+(?:value\s+)?equal(?:ity|s)?|(?:positional|first)\s+argument\s+(?:value|equal(?:s)?))\b",
            r"\b(?:argument|numeric|inclusive)\s+range\b",
            r"\brendered\s+(?:text|message)\b",
        ))
        and explains_explicit_elf_source(passage, require_version=require_version)
    )


def test_guide_describes_board_observations() -> None:
    guide = (ROOT / "docs/use-cases/uart-and-can.md").read_text(encoding="utf-8")
    section = re.search(r"^### [^\n]*\bdefmt\b[^\n]*\n(.*?)(?=^#{1,3} |\Z)", guide, re.I | re.M | re.S)
    if (
        section is None
        or "No published record shows it on a board yet." in " ".join(section[1].split())
        or not describes_observations(section[1], require_version=False)
    ):
        raise AssertionError("The serial guide must describe the three Nucleo-F446RE defmt plan observations.")


def test_unreleased_describes_board_observations() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = re.search(r"^## \[Unreleased\]\s*\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    if section is None or not any(describes_observations(entry) for entry in re.split(r"(?m)^- ", section[1])):
        raise AssertionError("The Unreleased changelog must describe the three Nucleo-F446RE defmt plan observations.")
