#!/usr/bin/env python3
"""An OpenOCD whose probe is attached and closed to this user, printing what the real one printed.

The transcript is a recording, not a paraphrase: OpenOCD 0.12.0, the Debian
package tools/bench/Dockerfile installs, run on 2026-09-27 by `probe_target` in
that image, with an ST-Link V2-1 and its board attached and the group that owns
the probe's USB device withheld from the container. libusb enumerated the probe
and was refused opening it, and OpenOCD exited 1 before `init` completed, so
neither the stage marker nor a result marker ever printed. The bench stage that
withholds the group drives the real binary for the same transcript
(tests/bench/test_bench_without_device_group.py); this fake is what the unit
tier runs on a host that has no probe, and it has to stay word for word what
the recording says.

The line after the libusb one is the whole of what the same OpenOCD prints with
nothing on USB (fake_openocd_no_probe.py), which is why the libusb line is the
one that says what happened.
"""

from __future__ import annotations

import sys

# OpenOCD 0.12.0, 2026-09-27, in the bench image, probe attached, its group withheld.
RECORDED_ACCESS_DENIED_STDERR = (
    "Open On-Chip Debugger 0.12.0\n"
    "Licensed under GNU GPL v2\n"
    "For bug reports, read\n"
    "\thttp://openocd.org/doc/doxygen/bugs.html\n"
    "Info : auto-selecting first available session transport \"hla_swd\". To override use 'transport select <transport>'.\n"
    "Info : The selected transport took over low-level target control. The results might differ compared to plain JTAG/SWD\n"
    "Info : clock speed 2000 kHz\n"
    "Error: libusb_open() failed with LIBUSB_ERROR_ACCESS\n"
    "Error: open failed\n"
    "\n"
    "\n"
)
RECORDED_ACCESS_DENIED_RETURNCODE = 1
THE_LINE_THE_CLASSIFIER_READS = "Error: libusb_open() failed with LIBUSB_ERROR_ACCESS"


def main() -> int:
    if "--version" in sys.argv[1:]:
        print("Open On-Chip Debugger 0.12.0")
        return 0
    sys.stderr.write(RECORDED_ACCESS_DENIED_STDERR)
    return RECORDED_ACCESS_DENIED_RETURNCODE


if __name__ == "__main__":
    raise SystemExit(main())
