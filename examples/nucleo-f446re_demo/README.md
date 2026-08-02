# Nucleo-F446RE Demo: Real Firmware in the Agentic HIL Loop

A minimal bare-metal STM32F446RE firmware for the [ST Nucleo-F446RE](https://www.st.com/en/evaluation-tools/nucleo-f446re.html) board that demonstrates the complete Agentic Hardware-in-the-Loop (Agentic HIL) loop on real hardware: build → flash → reset → assert on serial output.

The firmware prints `Hello World` on USART2 (PA2/PA3, routed to the ST-LINK virtual COM port, 115200 baud) at boot and then blinks the LD2 user LED. No HAL, no external dependencies — the whole program is [Src/main.c](Src/main.c).

## Prerequisites

- `arm-none-eabi-gcc` (GNU Arm Embedded Toolchain), CMake ≥ 3.20, Ninja
- OpenOCD (default) or STM32CubeProgrammer CLI
- A Nucleo-F446RE connected via USB (ST-LINK provides debug and the virtual COM port over the same cable)

## Build

```bash
cmake --preset Debug
cmake --build --preset Debug
```

This produces `build/Debug/nucleo-f446re_demo.elf` (plus `.hex`/`.bin`) — inside the `build` artifact root that the Agentic HIL config allows for flashing.

## Configure Agentic HIL

Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

Install Agentic HIL as [AI_AGENT_QUICKSTART.md](../../AI_AGENT_QUICKSTART.md)
describes, then from this directory:

```bash
agentic-hil setup --agent claude-code   # or: codex / opencode
# The included profile resolves one ST-Link, target, and matching COM port, then
# externally grants only requested probe, flash, reset, and UART-read access.
agentic-hil doctor
```

Authoritative config stays outside the repository: `%APPDATA%/agentic-hil/projects/<project-id>/config.yaml` on Windows; `${XDG_CONFIG_HOME:-~/.config}/agentic-hil/projects/<project-id>/config.yaml` on POSIX. The profile declares requirements; setup resolves probe, CubeProgrammer, and COM bindings without replacing existing config. Never commit host-specific copies.

## Run the loop from an agent (MCP)

With the MCP host started from this project root as described in the [top-level README](../../README.md), `agentic-hil mcp-stdio` lets an agent drive:

```text
flash_firmware     {"image_path": "build/Debug/nucleo-f446re_demo.elf"}
com_session_start  {"port_id": "dut_uart"}
reset_target       {"mode": "run"}
com_read           {"port_id": "dut_uart", "wait_timeout_s": 5}
→ feedback contains "Hello World"
```

## Run the loop from pytest

```bash
pytest tests/
```

[tests/test_firmware.py](tests/test_firmware.py) flashes the ELF, resets the target, and asserts the boot banner on the UART. The pytest plugin uses the same discovered config or `AGENTIC_HIL_CONFIG` override as `doctor` and MCP. Without an available config the test skips; with a config but no board attached it fails — that is the point of a hardware-in-the-loop regression test.

## Adapting to another board

Change `debuggers.dut.target_cfg` (OpenOCD target script), the `com_ports` device, and rebuild for your MCU (`CMakePresets.json` carries the CMSIS device settings; `stm32f446xe_flash.ld` and `Src/startup_stm32f446xx.S` are device-specific).
