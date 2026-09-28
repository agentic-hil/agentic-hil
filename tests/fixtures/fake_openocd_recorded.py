#!/usr/bin/env python3
"""An OpenOCD that answers each run with what the real one answered on the bench.

The OpenOCD backend can start OpenOCD more than once for one call: once for
`--version`, once more on some releases to ask which adapter driver the
interface script loads, and then the call itself. A test that holds the backend
to one release's own words needs each of those runs answered with that
release's recording of exactly that run, so this fake takes one recording per
kind of run and plays the one whose kind this run is:

* ``AGENTIC_HIL_FAKE_OPENOCD_VERSION`` for a run with `--version`,
* ``AGENTIC_HIL_FAKE_OPENOCD_ADAPTER_DRIVER`` for a run whose `-c` script asks
  for `[adapter name]`,
* ``AGENTIC_HIL_FAKE_OPENOCD_CALL`` for any other run.

Each names an entry of ``openocd_0_11_bench_recordings.json`` beside this file,
which says how and where each entry was recorded. The entry is played only when
this run's arguments are the recorded run's arguments after the program name,
word for word, so a backend that starts OpenOCD with anything else than what the
bench ran is answered with a refusal from this fake rather than with a
recording of some other command line. `<scratch>` in a recorded argument is the
recorder's temporary directory, and stands here for
``AGENTIC_HIL_FAKE_OPENOCD_SCRATCH``.

A run of a kind no recording is named for, or with arguments no recording was
made with, exits 97 and says which, on stderr: nothing here invents an answer.

Every run appends its arguments to the file ``AGENTIC_HIL_FAKE_OPENOCD_ARGV_LOG``
names, as one JSON array per line, whatever it then answers, so a test can read
back which runs were started and in which order.

The streams are written with a line feed after each line on every platform, so
the bytes the backend reads are the recorded ones on either host.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

RECORDINGS_FILE = "openocd_0_11_bench_recordings.json"
UNRECORDED_RUN = 97
ADAPTER_DRIVER_QUERY = "[adapter name]"


def kind_of_run(args: list[str]) -> str:
    if "--version" in args:
        return "VERSION"
    if any(ADAPTER_DRIVER_QUERY in value for flag, value in zip(args, args[1:], strict=False) if flag == "-c"):
        return "ADAPTER_DRIVER"
    return "CALL"


def refuse(message: str) -> int:
    sys.stderr.write(f"fake_openocd_recorded: {message}\n")
    sys.stderr.flush()
    return UNRECORDED_RUN


def main() -> int:
    sys.stdout.reconfigure(newline="\n")
    sys.stderr.reconfigure(newline="\n")
    args = sys.argv[1:]
    argv_log = os.environ.get("AGENTIC_HIL_FAKE_OPENOCD_ARGV_LOG")
    if argv_log:
        with open(argv_log, "a", encoding="utf-8", newline="\n") as log:
            log.write(json.dumps(args) + "\n")
    kind = kind_of_run(args)
    name = os.environ.get(f"AGENTIC_HIL_FAKE_OPENOCD_{kind}")
    if not name:
        return refuse(f"no recording is named for a {kind.lower()} run, and this run's arguments are {args!r}")
    recorded = json.loads(Path(__file__).with_name(RECORDINGS_FILE).read_text(encoding="utf-8"))["recordings"][name]
    if "argv" not in recorded:
        return refuse(f"the recording {name!r} does not say which arguments it was made with, so no run can be matched to it")
    scratch = os.environ.get("AGENTIC_HIL_FAKE_OPENOCD_SCRATCH", "<scratch>")
    expected = [argument.replace("<scratch>", scratch) for argument in recorded["argv"][1:]]
    if args != expected:
        return refuse(f"the recording {name!r} was made with the arguments {expected!r}, and this run's are {args!r}")
    sys.stdout.write(recorded["stdout"])
    sys.stdout.flush()
    sys.stderr.write(recorded["stderr"])
    sys.stderr.flush()
    return int(recorded["returncode"])


if __name__ == "__main__":
    raise SystemExit(main())
