"""End-to-end HIL regression test for the Nucleo-F446RE demo firmware.

Build the firmware first, then run pytest from this demo directory with a
.agentic-hil/config.yaml (see agentic-hil.config.example.yaml) and the board connected:

    cmake --preset Debug && cmake --build --preset Debug
    pytest tests/

Without an Agentic HIL configuration the test is skipped; with a configuration but
no board attached it fails — that is the point of a hardware-in-the-loop test.
"""
from __future__ import annotations

import time

FIRMWARE_ELF = "build/Debug/nucleo-f446re_demo.elf"
UART_ID = "dut_uart"
BOOT_BANNER = "Hello World"


def read_uart_until(agentic_hil, needle: str, timeout_s: float = 5.0) -> str:
    collected = ""
    deadline = time.monotonic() + timeout_s
    while needle not in collected and time.monotonic() < deadline:
        feedback = agentic_hil.call("com_read", {"port_id": UART_ID, "wait_timeout_s": 0.5})
        assert feedback["ok"] is True, feedback["summary"]
        collected += feedback["data"]["text"]
    return collected


def test_firmware_boots_and_prints_banner(agentic_hil) -> None:
    flashed = agentic_hil.call("flash_firmware", {"image_path": FIRMWARE_ELF})
    assert flashed["ok"] is True, flashed["summary"]

    started = agentic_hil.call("com_session_start", {"port_id": UART_ID, "clear_buffer": True})
    assert started["ok"] is True, started["summary"]

    reset = agentic_hil.call("reset_target", {"mode": "run"})
    assert reset["ok"] is True, reset["summary"]

    output = read_uart_until(agentic_hil, BOOT_BANNER)
    assert BOOT_BANNER in output, f"boot banner not seen on {UART_ID}; got: {output!r}"
