"""End-to-end HIL regression test for the Nucleo-F446RE demo firmware.

Build the firmware first, then run pytest from this demo directory with a
.hardci/config.yaml (see hardci.config.example.yaml) and the board connected:

    cmake --preset Debug && cmake --build --preset Debug
    pytest tests/

Without a HardCI configuration the test is skipped; with a configuration but
no board attached it fails — that is the point of a hardware-in-the-loop test.
"""
from __future__ import annotations

import time

FIRMWARE_ELF = "build/Debug/nucleo-f446re_demo.elf"
UART_ID = "dut_uart"
BOOT_BANNER = "Hello World"


def read_uart_until(hardci, needle: str, timeout_s: float = 5.0) -> str:
    collected = ""
    deadline = time.monotonic() + timeout_s
    while needle not in collected and time.monotonic() < deadline:
        feedback = hardci.call("hardci_com_read", {"port_id": UART_ID, "wait_timeout_s": 0.5})
        assert feedback["ok"] is True, feedback["summary"]
        collected += feedback["data"]["text"]
    return collected


def test_firmware_boots_and_prints_banner(hardci) -> None:
    flashed = hardci.call("hardci_flash_firmware", {"image_path": FIRMWARE_ELF})
    assert flashed["ok"] is True, flashed["summary"]

    started = hardci.call("hardci_com_session_start", {"port_id": UART_ID, "clear_buffer": True})
    assert started["ok"] is True, started["summary"]

    reset = hardci.call("hardci_reset_target", {"mode": "run"})
    assert reset["ok"] is True, reset["summary"]

    output = read_uart_until(hardci, BOOT_BANNER)
    assert BOOT_BANNER in output, f"boot banner not seen on {UART_ID}; got: {output!r}"
