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

Names: the Python distribution/install target and CLI command use `agentic-hil`. Python-facing identifiers such as pytest fixtures and Python examples use `agentic_hil`.

```bash
pipx install agentic-hil
agentic-hil init
# Copy agentic-hil.config.example.yaml's contents into that external file.
# Replace workspace_root with this directory's absolute path, keep state_root
# absolute and outside this workspace, and adjust com_ports.dut_uart.device
# (for example /dev/ttyACM0 or COM5), then:
agentic-hil doctor
```

The authoritative file belongs outside the repository at `%APPDATA%/agentic-hil/projects/<project-id>/config.yaml` on Windows or `${XDG_CONFIG_HOME:-~/.config}/agentic-hil/projects/<project-id>/config.yaml` on POSIX. Do not commit a machine-specific copy. The included template enables only the probe, flash, reset, and UART-read permissions required by this demo; review it before use.

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

Change `debugger.target_cfg` (OpenOCD target script), the `com_ports` device, and rebuild for your MCU (`CMakePresets.json` carries the CMSIS device settings; `stm32f446xe_flash.ld` and `Src/startup_stm32f446xx.S` are device-specific).
