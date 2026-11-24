"""Offline acceptance checks for the Nucleo-F446RE shared-bench HardFault plan."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import yaml

from agentic_hil.test_reactor import load_test_config

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "tests/hil/f446re_hardfault_shared_bench.yaml"
FIRMWARE = ROOT / "tests/bench/firmware/hardfault_capture.c"
TEST_PATH = Path(__file__).resolve().relative_to(ROOT).as_posix()
IMAGE = "build/hil/f446re_hardfault.elf"
BENCH_RELEASE = "0.28.0"
DEBUGGER = {"debugger": "f446re"}
UART = {"port_id": "f446re_vcp"}

sys.path.insert(0, str(ROOT / "tools"))

from check_version_consistency import uncovered_files  # noqa: E402


def test_typed_plan() -> None:
    text = PLAN.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    assert set(raw) == {"version", "name", "steps"}
    assert type(raw["version"]) is int and raw["version"] == 5

    plan = load_test_config(str(PLAN), str(ROOT))
    actual_steps = [
        {"action": step.action, **{key: getattr(step, key) for key in step.route_keys}, **step.arguments}
        for step in plan.steps
    ]
    assert actual_steps == [
        {"action": "flash", **DEBUGGER, "image_path": IMAGE, "reset_after_flash": False},
        {"action": "uart_open", **UART, "clear_buffer": True},
        {"action": "reset", **DEBUGGER, "mode": "run"},
        {"action": "uart_expect", **UART, "text": "hardfault capture image booted\n", "timeout_s": 5},
        {"action": "uart_close", **UART},
        {"action": "debug_start", **DEBUGGER, "image_path": IMAGE, "mode": "reset_halt"},
        {"action": "run_until_breakpoint", **DEBUGGER, "location": "HardFault_Handler", "timeout_s": 5},
        {
            "action": "read_symbol", **DEBUGGER, "symbol": "scb_cfsr", "size_bytes": 4,
            "comparator": {"mask": 0x00010000, "equals": 0x00010000},
        },
        {
            "action": "read_symbol", **DEBUGGER, "symbol": "scb_hfsr", "size_bytes": 4,
            "comparator": {"mask": 0x40000000, "equals": 0x40000000},
        },
        {"action": "read_symbol", **DEBUGGER, "symbol": "scb_mmfar", "size_bytes": 4},
        {"action": "read_symbol", **DEBUGGER, "symbol": "scb_bfar", "size_bytes": 4},
        {"action": "run_until_breakpoint", **DEBUGGER, "location": "fault_captured", "timeout_s": 5},
        {"action": "read_symbol", **DEBUGGER, "symbol": "fault_frame", "size_bytes": 32},
        {"action": "debug_stop", **DEBUGGER},
    ]

    header = " ".join(line.lstrip("# ").strip() for line in text.split("version:", 1)[0].splitlines())
    assert re.search(r"copy\b.*examples/nucleo-f446re_demo\b.*\b(?:without|excluding)\b.*\bbuild/", header, re.I)
    assert re.search(r"replace\b.*Src/main\.c\b.*" + re.escape(FIRMWARE.relative_to(ROOT).as_posix()), header, re.I)
    assert re.search(r"cmake --preset Debug --fresh\b.*cmake --build --preset Debug\b", header)
    assert re.search(r"copy\b.*build/Debug/nucleo-f446re_demo\.elf\b.*" + re.escape(IMAGE), header, re.I)


def assembly_lines(source: str) -> list[str]:
    blocks = re.findall(r'\b__asm__\s*(?:volatile\s*)?\(\s*((?:"(?:\\.|[^"\\])*"\s*)+)\)', source)
    return [
        " ".join(line.replace(",", ", ").split())
        for block in blocks
        for line in ast.literal_eval(f"({block})").splitlines()
        if line.strip()
    ]


def test_capture_image() -> None:
    source = FIRMWARE.read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
    compact = re.sub(r"\s+", "", code)
    plan = load_test_config(str(PLAN), str(ROOT))
    (boot_expectation,) = [step.arguments["text"] for step in plan.steps if step.action == "uart_expect"]
    sends = re.findall(r'\busart2_send\s*\(\s*("(?:\\.|[^"\\])*")\s*\)\s*;', code)
    assert [ast.literal_eval(literal) for literal in sends] == [boot_expectation]
    assert re.search(
        r'\bmain\s*\(\s*void\s*\)\s*\{\s*usart2_init\s*\(\s*\)\s*;\s*'
        r'usart2_send\s*\([^;]+\)\s*;\s*__asm__\s+volatile\s*\(\s*"udf\s+#0"\s*\)',
        code,
    ), "main must initialize the UART, send the boot line, then fault"

    # Keep the clock and fault configuration at reset defaults, and check the UART setup.
    assert "voidSystemInit(void){}" in compact
    assert re.search(r"^#define\s+USART2_TX_PIN\s+2[uU]?\s*$", code, re.M)
    assert re.search(r"^#define\s+USART2_RX_PIN\s+3[uU]?\s*$", code, re.M)
    assert re.search(r"^#define\s+USART2_BRR_115200_PCLK16MHZ\s+139[uU]?\s*$", code, re.M)
    for operation in (
        "RCC_AHB1ENR|=GPIOAEN;", "RCC_APB1ENR|=USART2EN;",
        "GPIO_MODER_AF(USART2_TX_PIN)|GPIO_MODER_AF(USART2_RX_PIN)",
        "GPIO_AFRL_AF7(USART2_TX_PIN)|GPIO_AFRL_AF7(USART2_RX_PIN)",
        "USART2_CR1=0U;", "USART2_CR2=0U;", "USART2_CR3=0U;",
        "USART2_BRR=USART2_BRR_115200_PCLK16MHZ;",
        "USART2_CR1=USART_CR1_RE|USART_CR1_TE|USART_CR1_UE;",
    ):
        assert operation in compact, f"Missing UART setup operation {operation}"
    assert re.search(r"USART2_DR\s*=[^;]+;.*?\bwhile\s*\(\s*\(\s*USART2_SR\s*&\s*USART_SR_TC", code, re.S)
    mmio_addresses = {
        int(address, 16)
        for address in re.findall(r"\*\(\s*volatile\s+uint32_t\s*\*\s*\)\s*(0x[0-9a-f]+)", code, re.I)
    }
    assert mmio_addresses == {
        0x40023830, 0x40023840, 0x40020000, 0x40020020,
        0x40004400, 0x40004404, 0x40004408, 0x4000440C, 0x40004410, 0x40004414,
    }

    assert re.search(r"\bvolatile\s+uint32_t\s+fault_frame\s*\[\s*8\s*\]\s*;", code)
    assert re.search(
        r"\bhard_fault_capture\s*\(\s*const\s+uint32_t\s*\*\s*(?P<stack>\w+)\s*\)\s*\{\s*"
        r"for\s*\(\s*unsigned\s+(?:int\s+)?(?P<index>\w+)\s*=\s*0[uU]?\s*;\s*"
        r"(?P=index)\s*<\s*8[uU]?\s*;\s*\+\+\s*(?P=index)\s*\)\s*\{\s*"
        r"fault_frame\s*\[\s*(?P=index)\s*\]\s*=\s*(?P=stack)\s*\[\s*(?P=index)\s*\]\s*;\s*"
        r"\}\s*fault_captured\s*\(\s*\)\s*;",
        code,
    ), "Copy all eight words in stack order before calling fault_captured"
    assert re.search(
        r"__attribute__\s*\(\([^)]*\bnoinline\b[^)]*\)\)\s+void\s+fault_captured\s*\(\s*void\s*\)"
        r"\s*\{\s*for\s*\(\s*;\s*;\s*\)\s*\{\s*\}\s*\}",
        code,
    )
    handler = re.search(
        r"__attribute__\s*\(\(\s*naked\s*\)\)\s+void\s+HardFault_Handler\s*\(\s*void\s*\)\s*\{([^}]+)\}",
        code,
    )
    assert handler, "HardFault_Handler must be naked"
    assert assembly_lines(handler[1]) == [
        "tst lr, #4", "ite eq", "mrseq r0, msp", "mrsne r0, psp", "b hard_fault_capture",
    ]

    assembly = "\n".join(assembly_lines(code))
    for symbol, address in {
        "scb_cfsr": 0xE000ED28,
        "scb_hfsr": 0xE000ED2C,
        "scb_mmfar": 0xE000ED34,
        "scb_bfar": 0xE000ED38,
    }.items():
        assert re.search(rf"^\.glob(?:al|l) {symbol}$", assembly, re.M)
        assert re.search(rf"^\.type {symbol}, %object$", assembly, re.M)
        absolute = re.search(rf"^\.(?:set|equ) {symbol}, (0x[0-9a-f]+)$", assembly, re.I | re.M)
        assert absolute and int(absolute[1], 16) == address, f"Wrong absolute address for {symbol}"
        assert re.search(rf"^\.size {symbol}, 4$", assembly, re.M)


def test_checkout_only_exclusion() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    if f"exclude {TEST_PATH}" not in {line.strip() for line in manifest.splitlines()}:
        raise AssertionError("MANIFEST.in must exclude tests/test_f446re_hardfault_plan.py.")


def test_recorded_bench_release() -> None:
    findings = uncovered_files(ROOT, version=BENCH_RELEASE)
    if any(finding.startswith(f"{TEST_PATH} carries version ") for finding in findings):
        raise AssertionError("The offline test's fixed bench release is missing from the version inventory.")

    check_hardfault_changelog((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))


def check_hardfault_changelog(changelog: str) -> None:
    releases = re.findall(
        r"^##[ \t]+(?:\[Unreleased\]|\[\d+\.\d+\.\d+\] - \d{4}-\d{2}-\d{2})[ \t]*\n(.*?)(?=^##[ \t]|\Z)",
        changelog, re.M | re.S,
    )
    entries = (
        entry
        for release in releases
        for added in re.findall(r"^### Added[ \t]*\n(.*?)(?=^###[ \t]|\Z)", release, re.M | re.S)
        for entry in re.findall(r"(?m)^[-*+][ \t]+[^\n]*(?:\n[ \t]+[^\n]*)*", added)
    )
    entry = next((entry for entry in entries if re.search(r"\bHardFault_Handler\b", entry)), "")
    entry = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", entry)
    entry = " ".join(entry.replace("`", "").replace("**", "").split())

    assert re.search(r"\b(?:stop\w*|breakpoint)\b[^.!?]*\bHardFault_Handler\b", entry, re.I), "Describe the handler stop"
    assert re.search(r"\bCFSR\.UNDEFINSTR\s*[(, ]*bit 16\b", entry, re.I), "Name CFSR.UNDEFINSTR (bit 16)"
    assert re.search(r"\bHFSR\.FORCED\s*[(, ]*bit 30\b", entry, re.I), "Name HFSR.FORCED (bit 30)"
    assert re.search(r"\b(?:assert|check|confirm|verif)\w*", entry, re.I), "Describe the fault bits as checked"
    sentences = re.split(r"(?<=[.!?])\s+", entry)
    register_sentences = [sentence for sentence in sentences if "MMFAR" in sentence and "BFAR" in sentence]
    assert any(
        re.search(r"\bread\w*\b", sentence, re.I) and re.search(r"\brecord\w*\b", sentence, re.I)
        for sentence in register_sentences
    ), "Describe reading and recording MMFAR and BFAR"
    assert re.search(
        r"\bMMARVALID\b[^.!?]*\bBFARVALID\b[^.!?]*\bclear\b|\bclear\b[^.!?]*\bMMARVALID\b[^.!?]*\bBFARVALID\b",
        entry, re.I,
    ), "Explain that the address-valid bits were clear"
    assert re.search(
        r"\b(?:neither|without|no|cannot|does not|do not)\b[^.!?]*\bfault address(?:es)?\b",
        entry, re.I,
    ), "Avoid claiming either reading is a fault address"
    assert re.search(
        r"\b(?:read\w*|record\w*)\b[^.!?]*\b(?:stacked\b[^.!?]*\bframe\b|frame\b[^.!?]*\b(?:as stacked|in stack order))"
        r"|\b(?:stacked\b[^.!?]*\bframe\b|frame\b[^.!?]*\b(?:as stacked|in stack order))[^.!?]*\b(?:read\w*|record\w*)\b",
        entry, re.I,
    ), "Describe reading the frame as stacked"
    assert re.search(r"\br0,\s*r1,\s*r2,\s*r3,\s*r12,\s*lr,\s*pc,?\s*(?:and\s+)?xPSR\b", entry, re.I), (
        "List the eight words in stack order"
    )
    assert not re.search(r"\b(?:0x[0-9a-f]+|[0-9]{5,})\b", entry, re.I), "Omit register readings and build-specific addresses"
    assert not re.search(r"\b(?:MMFAR|BFAR|r[0-3]|r12|lr|pc|xPSR)\b\s*(?:=|:)\s*[0-9]", entry, re.I)
    assert "Nucleo-F446RE" in entry, "Name the bench board"
    assert "OpenOCD 0.12.0" in entry, "Name the bench's OpenOCD version"
    assert re.search(rf"\bagentic-hil\s+(?:serve\s+)?(?:v(?:ersion)?\s*)?{re.escape(BENCH_RELEASE)}\b", entry, re.I), (
        "Name the released Agentic HIL version"
    )
    assert re.search(
        r"\bserve\b[^.!?]*\b(?:does not|cannot|leaves)\b[^.!?]*\bGDB build\b"
        r"|\bGDB build\b[^.!?]*\b(?:unidentified|unknown|not reported)\b",
        entry, re.I,
    ), "State that serve leaves the GDB build unidentified"
    assert re.search(r"#756\b[^.!?]*\b(?:open|pending)\b", entry, re.I) and "arm-none-eabi-gdb" in entry, (
        "Keep #756 open for an arm-none-eabi-gdb recording"
    )
