# Arduino Nano ESP32 A/B demo

This example uses the Nano's native USB-Serial/JTAG port for both esptool and
the console. The two ESP32-S3 images print `agentic-hil esp32s3 image A tick 0`
or the equivalent for B, then advance the counter every 100 ms.

**Flashing either image at `0x0` replaces the factory bootloader.** Keep a
copy of the correct factory bootloader or a factory recovery image before
flashing. Restore the factory bootloader at `0x0` with an Arduino Nano ESP32
recovery image or procedure before trying to run the original firmware again.

## Set up the native USB port

1. Install Agentic HIL with its `esptool` extra. Connect the Nano over its own USB port.
2. Copy `config.example.yaml` to the authoritative Agentic HIL configuration
   location. Set `workspace_root` to this demo directory and `state_root` to a
   trusted directory outside the workspace. Replace the console device and
   serial placeholders with the Nano's actual Linux `/dev/serial/by-id` path
   and USB serial number. Both `esp32s3` entries refer to this same port.
3. From this directory, run `agentic-hil test-reactor --test-config testconfig.yaml`.
   The demo flashes A and B in order, verifies each write, resets to run, and
   opens the read-only 115200 baud console with its buffer cleared. It checks a
   complete banner and then ticks 50 and 51 before closing the console. The
   later pair leaves time for a slow reset. A passing run leaves B running.

The Nano's MAC address is its USB adapter serial. Keep raw port identities,
lock names, esptool command lines and job artifacts private. Publish only the
sanitized JSON and record the tested checkout revision alongside it.

## Rebuild the images

From the repository root, use a Python environment with esptool 5 and the
Espressif crosstool-NG release `esp-16.1.0_20260609`.

```text
python tools/build_esp32s3_demo_images.py --toolchain <directory-containing-xtensa-esp32s3-elf-gcc>
```

The command produces both DIO images and `manifest.json`, which records the
tool versions, flags, source hashes, image hashes and sizes, and flash address.

## Shared bench evidence

`tests/hil/nano_esp32s3_shared_bench.yaml` is the exclusive qualification plan.
It checks 20 consecutive A/B cycles, for 40 verified flashes, and leaves B
running on success. Use the job's evidence to report console observations and
any reconnects visible in its steps. Physical disconnect is outside this plan.

```text
python tools/esp32s3_demo_results.py --report <report.json> --evidence <job-id-evidence.zip> --manifest examples/nano-esp32s3_demo/manifest.json
```

The supplemental plans `nano_esp32s3_console_refuses_flash.yaml` and
`nano_esp32s3_console_refuses_reset.yaml` exercise flash and reset while the
same console is open. Either step is refused with `device_busy` before esptool
runs. The refusal carries `held_by_com_session: esp32s3` only where the
configuration has exactly one debugger. With several debuggers it carries
`holder_is_this_process: true` instead, and the refused resource must match
the lock recorded by the console open step. Use `--refusal` with the job
artifacts to print the receipt without the private lock key.

An image must be running before each of these plans. With reset-to-halt
recovery, the failed job leaves the chip in its ROM bootloader, so flash A or B
and reset to run before starting another refusal plan. The plans are not part
of the qualification, and no receipt of them from the shared bench is
published. The pull request says what was evidenced on the board.
Probe exclusion and two port owners are covered offline.
