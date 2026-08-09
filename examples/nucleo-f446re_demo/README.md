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

The project half reads the profile: where `agentic-hil agent-install` has already run for this user, `agentic-hil init` alone does the same discovery.

Authoritative config stays outside the repository: `%APPDATA%/agentic-hil/projects/<project-id>/config.yaml` on Windows; `${XDG_CONFIG_HOME:-~/.config}/agentic-hil/projects/<project-id>/config.yaml` on POSIX. The profile declares requirements; the project half resolves probe, CubeProgrammer, and COM bindings without replacing existing config. Never commit host-specific copies.

## Run the loop (start here)

The loop is a declared test plan, and running it is one command:

```bash
agentic-hil test-reactor --test-config testconfig.yaml
```

[testconfig.yaml](testconfig.yaml) is the whole loop in five steps — flash, open the port with a clean buffer, reset, wait for `Hello World`, close. This is the path to reach for, including from an agent: the reactor validates every device name, permission and session order *before* the first hardware action, and closes the UART session even when a step fails. A banner that never arrives fails the run and reports the tail of what the port did say, so a silent board and a wrong banner read differently.

Waiting on a fixed substring is `text:`; when the check is that the board answered with the *right* value — a counter, a measurement, a version string — a step writes `pattern:` instead and gets a Python regular expression (`re.search`, so it is anchored only where the pattern says). Exactly one of the two per step.

## Drive the loop tool by tool (MCP)

The same steps, driven rather than declared. With the MCP host started from this project root as described in the [top-level README](../../README.md), `agentic-hil mcp-stdio` exposes the individual tools the plan above calls:

```text
flash_firmware     {"image_path": "build/Debug/nucleo-f446re_demo.elf"}
com_session_start  {"port_id": "dut_uart", "clear_buffer": true}
reset_target       {"mode": "run"}
com_read           {"port_id": "dut_uart", "wait_timeout_s": 5}
→ feedback contains "Hello World"
```

Reach for this when a step needs something the plan vocabulary has no word for; otherwise the plan says the same thing in one command and checks it before touching the board.

## Embed the loop in a pytest suite

```bash
pytest tests/
```

[tests/test_firmware.py](tests/test_firmware.py) is the variant for CI suites that already run pytest: it drives the same tools by hand — flash the ELF, reset the target, read until the boot banner appears — so the hardware check sits alongside a project's other tests and reports through the same runner. The `agentic_hil` fixture uses the same discovered config or `AGENTIC_HIL_CONFIG` override as `doctor`, MCP and the reactor. Without an available config the test skips; with a config but no board attached it fails — that is the point of a hardware-in-the-loop regression test.

A suite that only needs the loop itself can call the reactor instead of restating it in Python; the plan is the shorter and better-checked way to say the same thing.

## Adapting to another board

Change `debuggers.dut.target_cfg` (OpenOCD target script), the `com_ports` device, and rebuild for your MCU (`CMakePresets.json` carries the CMSIS device settings; `stm32f446xe_flash.ld` and `Src/startup_stm32f446xx.S` are device-specific).
