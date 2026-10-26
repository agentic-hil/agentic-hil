#!/usr/bin/env python3
"""A pyOCD with no probe attached, answering the way pyOCD 0.46.0 or 0.45.1 answered.

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

Recorded again on 2026-10-26 in the same image with 0.46.0: every stream above
byte for byte the same, `--version` and the `json` listing naming 0.46.0, and
one status changed. The commander now exits 3 over both refusals (`failed to
create session`, pyocd/commands/commander.py), where 0.45.1 exited 0; the
`--command reset` and `--command savemem` forms the backend also spawns
answer the same way. `reset` and `flash` still exit 1.

Two facts in those recordings decide the shape of this fixture. Without `-W`
pyOCD never exits on its own: it prints the waiting line and polls for a probe,
so a backend that spawns it without the flag has nothing but its own timeout to
end the call with. And with `-W` the commander's status has changed between
releases while its sentence has not, so the sentence on stdout is the evidence
that nothing was contacted; `reset` and `flash` exit 1, each with a second line
of its own on stderr.

AGENTIC_HIL_FAKE_PYOCD_RELEASE picks the release to answer as, `0.46.0` (the
default, the one the image installs) or `0.45.1`, so both of the commander's
statuses stay under test: an installed pyOCD may be either.

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

RELEASES = ("0.46.0", "0.45.1")
# The status the commander exits with over the refusal, per release.
COMMANDER_NO_PROBE_EXIT = {"0.46.0": 3, "0.45.1": 0}
NO_PROBE = "No connected debug probes"
NO_PROBE_FOR_UID = "No connected debug probe matches unique ID '{uid}'"
WAITING = "Waiting for a debug probe to be connected..."
RESET_NO_TARGET = "0000212 E No target device available to reset [reset_cmd]"
FLASH_NO_TARGET = "0000223 E No target device available [load_cmd]"
NO_WAIT_FLAGS = ("-W", "--no-wait")


def listed_probes() -> list[str]:
    raw = os.environ.get("AGENTIC_HIL_FAKE_PYOCD_PROBES", "[]")
    return [str(uid) for uid in json.loads(raw)]


def release() -> str:
    chosen = os.environ.get("AGENTIC_HIL_FAKE_PYOCD_RELEASE", RELEASES[0])
    if chosen not in RELEASES:
        raise SystemExit(f"AGENTIC_HIL_FAKE_PYOCD_RELEASE={chosen!r} is not one of the recorded releases {RELEASES}")
    return chosen


def main() -> int:
    # The recording's bytes on every platform: a line feed after each line, not
    # the carriage return and line feed a Windows text stream would substitute.
    sys.stdout.reconfigure(newline="\n")
    sys.stderr.reconfigure(newline="\n")
    args = sys.argv[1:]
    version = release()
    if "--version" in args:
        print(version)
        return 0
    if args and args[0] == "json":
        if args[1:] == ["--probes", "--no-config"]:
            boards = [{"unique_id": uid} for uid in listed_probes()]
            print(json.dumps({"pyocd_version": version, "version": {"major": 1, "minor": 1}, "status": 0, "boards": boards}, indent=4))
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
        # What both releases do with no `-W`: say so once and poll for a probe
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
    return COMMANDER_NO_PROBE_EXIT[version]


if __name__ == "__main__":
    raise SystemExit(main())
