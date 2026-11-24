# Raspberry Pi Pico H demo

This bare-metal RP2040 example prints one line, `Hello World`, on UART0 at boot.
The test plan flashes the image through a Raspberry Pi Debug Probe, resets the
Pico H, and checks the line on the probe's UART at 115200 baud, 8N1.

## Prerequisites and wiring

- Raspberry Pi Pico H, Raspberry Pi Debug Probe, and their SWD and UART leads.
- CMake 3.24 or newer, Ninja, and the GNU Arm Embedded toolchain
  (`arm-none-eabi-gcc` and `arm-none-eabi-objcopy`) on your PATH.
- Agentic HIL and pyOCD installed on the host. Use a pyOCD version with the
  built-in `rp2040` target, such as 0.45.1.

Connect the probe's SWD lead to the Pico H's three-pin debug connector.
Connect the probe's UART RX to GP0 (Pico TX), UART TX to GP1 (Pico RX), and
GND to GND. Plug the probe and the Pico H into USB separately so the Pico
gets power from its own USB connector.

## Build

From the repository root:

```sh
cmake -S examples/pico-rp2040_demo --preset Debug --fresh
cmake --build examples/pico-rp2040_demo/build/Debug
```

The image is `build/Debug/pico-rp2040_demo.elf` relative to this demo directory.
The build computes the boot checksum locally and needs no SDK or downloads.

## Configure the probe

Copy [`config.example.yaml`](config.example.yaml) to a location you control
outside the checkout. Set `workspace_root` to the demo directory's absolute
path and `state_root` to an absolute state directory outside that workspace.
Set `AGENTIC_HIL_CONFIG` to the copy's absolute path in your environment.
Use this version 3 configuration directly. The probe discovery used by
`agentic-hil init` and `setup` is for ST-Link.

With the Debug Probe plugged in and pyOCD on your PATH, run the following
from this demo directory. The `dut` entry must already specify `type: pyocd`.
You can leave the serial and UART device placeholders in place for this listing.

```sh
agentic-hil debugger-probes
```

Copy the full ID for the Raspberry Pi Debug Probe into both
`debuggers.dut.probe_id` and `com_ports.dut_uart.serial_number`. Set
`com_ports.dut_uart.device` to the serial device for the Debug Probe's UART,
such as its `/dev/serial/by-id/` entry on Linux or its assigned COM port
in Windows Device Manager.

## Run the loop

From this demo directory, with the external configuration selected:

```sh
agentic-hil test-reactor --test-config testconfig.yaml
```

The plan flashes without resetting, opens and clears the UART, then resets
the Pico in `run` mode. It passes when it reads `Hello World` from the UART.
