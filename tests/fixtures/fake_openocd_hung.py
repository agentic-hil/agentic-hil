#!/usr/bin/env python3
"""A debugger that starts, forks a helper, and never finishes.

The shape of a hung OpenOCD, pyOCD or STM32CubeProgrammer: a process that has
been started, that has a child of its own (OpenOCD's own helpers, a wrapper
script's interpreter), and that will not exit inside any deadline a
configuration names. Nothing is answered on stdout and nothing is faked about a
board: the only thing this file is about is what the backend does with a process
that is still there when `debuggers.<name>.timeout_s` runs out.

Both processes write their pid to the file named by
`AGENTIC_HIL_TEST_PID_FILE`, one per line, so the test that started this can
ask the operating system afterwards whether either of them survived. Whatever
arguments the backend passes are ignored: a hung tool does not read its command
line any differently from one that works.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

HANG_S = 30.0


def record_pid() -> None:
    pid_file = os.environ.get("AGENTIC_HIL_TEST_PID_FILE")
    if pid_file:
        with open(pid_file, "a", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")


def main() -> int:
    record_pid()
    if "--child" not in sys.argv:
        # The grandchild of the backend: the process a termination that only
        # reaches the direct child leaves behind.
        child = subprocess.Popen([sys.executable, __file__, "--child"])
        try:
            time.sleep(HANG_S)
        finally:
            child.kill()
        return 0
    time.sleep(HANG_S)
    return 0


if __name__ == "__main__":
    sys.exit(main())
