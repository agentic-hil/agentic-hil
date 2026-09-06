#!/usr/bin/env python3
"""A pyOCD with no probe attached, answering the way pyOCD 0.45.1 answered.

Recorded on 2026-09-06 in the container test image (tools/container/Dockerfile,
python:3.12-slim, `pip install pyocd` giving 0.45.1, nothing on USB), one command
at a time with stdout and stderr kept apart:

    $ pyocd commander -W --command status
    No connected debug probes                                        (stdout)
    exit 0
    $ pyocd commander -W --uid NOSUCHPROBE0001 --command status
    No connected debug probe matches unique ID 'NOSUCHPROBE0001'     (stdout)
    exit 0
    $ pyocd reset -W
    No connected debug probes                                        (stdout)
    0000212 E No target device available to reset [reset_cmd]        (stderr)
    exit 1
    $ pyocd flash -W --no-reset --base-address 0x08000000 /tmp/x.bin
    No connected debug probes                                        (stdout)
    0000223 E No target device available [load_cmd]                  (stderr)
    exit 1
    $ pyocd flash -W --uid NOSUCHPROBE0001 --no-reset --base-address 0x08000000 /tmp/x.bin
    No connected debug probe matches unique ID 'NOSUCHPROBE0001'     (stdout)
    0000207 E No target device available [load_cmd]                  (stderr)
    exit 1
    $ timeout 4 pyocd commander --command status                     (no -W)
    Waiting for a debug probe to be connected...                     (stdout)
    killed by the timeout, exit 124
    $ pyocd json --probes --no-config
    {"pyocd_version": "0.45.1", "version": {"major": 1, "minor": 1}, "status": 0, "boards": []}
    exit 0

Two facts in that recording decide the shape of this fixture. Without `-W`
pyOCD never exits on its own: it prints the waiting line and polls for a probe,
so a backend that spawns it without the flag has nothing but its own timeout to
end the call with. And with `-W` the commander exits 0 over the refusal, so the
sentence on stdout is the only evidence that nothing was contacted; only `reset`
and `flash` exit 1, each with a second line of its own on stderr.

The probes the `json` enumeration lists come from AGENTIC_HIL_FAKE_PYOCD_PROBES,
a JSON list of unique ids, so a test can have enumeration find a probe that the
connect afterwards cannot: a probe unplugged between the two calls.

AGENTIC_HIL_FAKE_PYOCD_HANGS_DESPITE_NO_WAIT is the other bench: a probe that is
there and a target that never answers. With it set the fixture prints nothing
and never exits, `-W` or not, which is what a stuck core looks like from the
backend and what its timeout exists for.
"""

from __future__ import annotations

import json
import os
import sys
import time

PYOCD_VERSION = "0.45.1"
NO_PROBE = "No connected debug probes"
NO_PROBE_FOR_UID = "No connected debug probe matches unique ID '{uid}'"
WAITING = "Waiting for a debug probe to be connected..."
RESET_NO_TARGET = "0000212 E No target device available to reset [reset_cmd]"
FLASH_NO_TARGET = "0000223 E No target device available [load_cmd]"
NO_WAIT_FLAGS = ("-W", "--no-wait")


def listed_probes() -> list[str]:
    raw = os.environ.get("AGENTIC_HIL_FAKE_PYOCD_PROBES", "[]")
    return [str(uid) for uid in json.loads(raw)]


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print(PYOCD_VERSION)
        return 0
    if args and args[0] == "json":
        if args[1:] == ["--probes", "--no-config"]:
            boards = [{"unique_id": uid} for uid in listed_probes()]
            print(json.dumps({"pyocd_version": PYOCD_VERSION, "version": {"major": 1, "minor": 1}, "status": 0, "boards": boards}, indent=4))
            return 0
        if args[1:] == ["--targets", "--no-config"]:
            # Unavailable on purpose: the backend cross-checks an unexplained
            # failure against this list, and a list it cannot read reclassifies
            # nothing, so what a test sees is the phrase match alone.
            print("`pyocd json --targets` is unavailable in this fixture", file=sys.stderr)
            return 2
        print("unsafe probe discovery arguments", file=sys.stderr)
        return 2
    if os.environ.get("AGENTIC_HIL_FAKE_PYOCD_HANGS_DESPITE_NO_WAIT") == "1":
        # A probe found and a target that never answers: no sentence, no exit.
        time.sleep(300)
        return 0
    if not any(flag in args for flag in NO_WAIT_FLAGS):
        # What 0.45.1 does with no `-W`: says so once and polls for a probe
        # until somebody ends the process. Long enough that only the backend's
        # own timeout can end this run.
        print(WAITING, flush=True)
        time.sleep(300)
        return 0
    uid = args[args.index("--uid") + 1] if "--uid" in args and args.index("--uid") + 1 < len(args) else None
    print(NO_PROBE_FOR_UID.format(uid=uid) if uid else NO_PROBE)
    if args[0] == "reset":
        print(RESET_NO_TARGET, file=sys.stderr)
        return 1
    if args[0] in {"flash", "load"}:
        print(FLASH_NO_TARGET, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
