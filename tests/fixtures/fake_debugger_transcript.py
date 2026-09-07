#!/usr/bin/env python3
"""A debugger that answers every invocation with one recorded transcript.

The classifiers in the three backends read the tool's own words, so a test
that wants a particular bucket needs a process that says those words, and the
words have to be the tool's rather than a paraphrase somebody remembered. This
fake plays a transcript back and does nothing else: no argument is read, no
file is written, no stage marker and no success marker is echoed, because the
product's own `-c` script never runs here. What comes out is exactly what the
test put in.

Two ways to say what to play:

* ``AGENTIC_HIL_FAKE_TRANSCRIPT_RECORDING=<name>`` replays one entry of
  ``debugger_refusal_recordings.json`` beside this file, byte for byte: the
  stdout, the stderr and the exit status the real program produced in the
  container test image. The recording names the tool versions and the date.
* ``AGENTIC_HIL_FAKE_TRANSCRIPT_STDOUT``, ``_STDERR`` and ``_EXIT`` play a
  transcript the test wrote, for the phrases of a tool nobody here can run
  (STM32CubeProgrammer) and for the ones only a probe can make a tool print.
  The test that uses them says where each phrase comes from.

The recording, when named, wins over the three variables. The exit status
defaults to 1: a transcript with no status is a failure, never a success that
happened to say nothing.

The streams are written with a line feed after each line on every platform,
not the carriage return and line feed a Windows text stream would substitute,
so a byte comparison against the recording holds on either host.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

RECORDINGS = Path(__file__).with_name("debugger_refusal_recordings.json")


def main() -> int:
    sys.stdout.reconfigure(newline="\n")
    sys.stderr.reconfigure(newline="\n")
    name = os.environ.get("AGENTIC_HIL_FAKE_TRANSCRIPT_RECORDING")
    if name:
        recording = json.loads(RECORDINGS.read_text(encoding="utf-8"))["recordings"][name]
        stdout, stderr, status = recording["stdout"], recording["stderr"], int(recording["returncode"])
    else:
        stdout = os.environ.get("AGENTIC_HIL_FAKE_TRANSCRIPT_STDOUT", "")
        stderr = os.environ.get("AGENTIC_HIL_FAKE_TRANSCRIPT_STDERR", "")
        status = int(os.environ.get("AGENTIC_HIL_FAKE_TRANSCRIPT_EXIT", "1"))
    sys.stdout.write(stdout)
    sys.stdout.flush()
    sys.stderr.write(stderr)
    sys.stderr.flush()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
