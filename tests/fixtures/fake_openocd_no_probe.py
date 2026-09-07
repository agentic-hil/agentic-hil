#!/usr/bin/env python3
"""An OpenOCD with nothing on USB, printing what the real one printed.

The transcript is a recording, not a paraphrase: OpenOCD 0.12.0, the Debian
package tools/container/Dockerfile installs, run on 2026-09-06 with
`-f interface/stlink.cfg -f target/stm32f4x.cfg -c init` and no probe attached.
It exits 1 before `init` completes, so neither the stage marker nor a result
marker ever prints, and the one line the classifier can read is `Error: open
failed`. The container tier drives the real binary for the same transcript
(tests/container/test_openocd_without_a_probe.py); this fake is what the unit
tier runs on a host that has no OpenOCD, and it has to stay word for word what
the recording says.

Not recorded here, because a container with no USB bus gives libusb nothing to
refuse: on a Linux host without a udev rule the same run prints `Error:
libusb_open() failed with LIBUSB_ERROR_ACCESS` ahead of the `open failed` line.
The classifier reads either.
"""

from __future__ import annotations

import sys

# OpenOCD 0.12.0, 2026-09-06, in the container image, with nothing on USB.
RECORDED_NO_PROBE_STDERR = (
    "Open On-Chip Debugger 0.12.0\n"
    "Licensed under GNU GPL v2\n"
    "For bug reports, read\n"
    "\thttp://openocd.org/doc/doxygen/bugs.html\n"
    "Info : auto-selecting first available session transport \"hla_swd\". To override use 'transport select <transport>'.\n"
    "Info : The selected transport took over low-level target control. The results might differ compared to plain JTAG/SWD\n"
    "Info : clock speed 2000 kHz\n"
    "Error: open failed\n"
    "\n"
    "\n"
)
RECORDED_NO_PROBE_RETURNCODE = 1
THE_LINE_THE_CLASSIFIER_READS = "Error: open failed"


def main() -> int:
    if "--version" in sys.argv[1:]:
        print("Open On-Chip Debugger 0.12.0")
        return 0
    sys.stderr.write(RECORDED_NO_PROBE_STDERR)
    return RECORDED_NO_PROBE_RETURNCODE


if __name__ == "__main__":
    raise SystemExit(main())
