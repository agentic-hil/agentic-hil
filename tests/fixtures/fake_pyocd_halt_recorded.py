#!/usr/bin/env python3
"""A pyOCD whose core keeps its run state from one process to the next, as recorded on the board (#631).

Without a debug session every product call is its own `pyocd commander`
process, so whether a halt holds is a question about what each process leaves
behind for the next one. `fake_pyocd.py` keeps no state between processes and
cannot answer it. This one keeps the core's state in the JSON file named by
`AGENTIC_HIL_FAKE_PYOCD_CORE_STATE` and moves it the way pyOCD 0.45.1 moved the
reference board's core in `pyocd_0_45_1_halt_recordings.json` beside it. Every
line it prints and every number it moves the counter by is taken from that
recording:

* `reset halt` halts the core at its reset vector and leaves RAM, so the
  counter keeps the value it had (OpenOCD read the same frozen counter twice,
  one second apart, after the product's reset into halt).
* `reset` starts the image again from its startup code, which zeroes the
  counter.
* Every commander connect runs the target pack's `DebugCoreStart` sequence,
  unless `pack.debug_sequences.disabled_sequences` names it. Its DHCSR write is
  the recorded one, and a write that sets `C_DEBUGEN` without `C_HALT` lets a
  halted core run. A core let run from its reset vector runs its startup code
  first, so the first read after it returns the recorded connect window, and a
  core that runs between two processes moves the counter by what the recorded
  product reads moved it.
* The commander disconnects without resuming, so the state a process leaves is
  the state its connect and its commands made.

Without the state variable the fake refuses every commander call, so a test
that forgot to name the file fails loudly instead of reading a fresh core.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

RECORDING = Path(__file__).with_name("pyocd_0_45_1_halt_recordings.json")
STATE_VARIABLE = "AGENTIC_HIL_FAKE_PYOCD_CORE_STATE"
OPTION_FLAGS = ("-O", "--option")
DISABLED_SEQUENCES_OPTION = "pack.debug_sequences.disabled_sequences"
DEBUG_CORE_START = "DebugCoreStart"
C_DEBUGEN = 1 << 0
C_HALT = 1 << 1
WRITE32 = re.compile(r"Write32\(\s*0xE000EDF0\s*,\s*(0x[0-9A-Fa-f]+)\s*\)")


def recording() -> dict:
    return json.loads(RECORDING.read_text(encoding="utf-8"))


def debug_core_start_lets_a_halted_core_run(recorded: dict) -> bool:
    """Whether the pack's DebugCoreStart, as recorded, clears a halt it finds."""
    for line in recorded["cmsis_pack"]["debug_core_start_dhcsr_writes"]:
        match = WRITE32.search(line)
        if match is None:
            continue
        value = int(match.group(1), 16)
        if value & C_DEBUGEN and not value & C_HALT:
            return True
    return False


def recorded_numbers(recorded: dict) -> tuple[int, int]:
    """The connect window and the movement between two processes, in counter ticks.

    Both come from the product's own two reads of a core its reset had halted:
    the first read returned the ticks the core counted from its startup code to
    the read, and the second the ticks it had counted by the next process.
    """
    reads = recorded["scenarios"]["product_read_of_a_halted_core"]
    first = int(reads["first_read"]["value_unsigned"])
    second = int(reads["second_read"]["value_unsigned"])
    return first, second - first


def recorded_output(recorded: dict, scenario: str, call: str) -> list[str]:
    return list(recorded["scenarios"][scenario][call]["output"])


def disabled_sequences(args: list[str]) -> set[str]:
    disabled: set[str] = set()
    for index, item in enumerate(args):
        if item in OPTION_FLAGS and index + 1 < len(args):
            name, _, value = args[index + 1].partition("=")
            if name == DISABLED_SEQUENCES_OPTION:
                disabled.update(part.strip() for part in value.split(",") if part.strip())
    return disabled


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"halted": False, "at_reset_vector": False, "counter": 0}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == "'" and token[-1] == "'":
        return token[1:-1]
    return token


def save_memory(command: str, counter: int) -> None:
    _, _address, size_text, file_text = command.split(None, 3)
    size = int(size_text)
    path = Path(unquote(file_text.strip()))
    path.write_bytes((counter & ((1 << (8 * size)) - 1)).to_bytes(size, "little"))
    print(f"Saved {size} bytes to {path}")


def commander(args: list[str]) -> int:
    variable = os.environ.get(STATE_VARIABLE)
    if not variable:
        print(f"{STATE_VARIABLE} names no core state file", file=sys.stderr)
        return 2
    state_path = Path(variable)
    state = load_state(state_path)
    recorded = recording()
    connect_window, between_processes = recorded_numbers(recorded)

    if not state["halted"]:
        state["counter"] += between_processes
    if DEBUG_CORE_START not in disabled_sequences(args) and debug_core_start_lets_a_halted_core_run(recorded) and state["halted"]:
        state["halted"] = False
        if state["at_reset_vector"]:
            state["counter"] = 0
            state["at_reset_vector"] = False
    if not state["halted"]:
        state["counter"] += connect_window

    commands = [args[index + 1] for index, item in enumerate(args) if item in {"-c", "--command"} and index + 1 < len(args)]
    for command in commands:
        words = shlex.split(command, posix=False)
        if words == ["reset", "halt"]:
            state.update(halted=True, at_reset_vector=True)
            print("\n".join(recorded_output(recorded, "product_reset_halt", "reset_halt")))
        elif words == ["reset"]:
            state.update(halted=False, at_reset_vector=False, counter=0)
            print("\n".join(recorded_output(recorded, "pyocd_attach_without_debug_core_start_on_a_running_core", "reset_run")))
        elif words and words[0] == "savemem":
            save_memory(command, state["counter"])
        else:
            print(f"command {command!r} is not one this recording holds", file=sys.stderr)
            save_state(state_path, state)
            return 2
    save_state(state_path, state)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print(recording()["pyocd_version"])
        return 0
    if args and args[0] == "json":
        if args[1:] == ["--targets", "--no-config"]:
            print(json.dumps({"pyocd_version": recording()["pyocd_version"], "status": 0, "targets": [{"name": "stm32f446re", "vendor": "STMicroelectronics", "part_number": "STM32F446RE", "source": "pack"}]}))
            return 0
        print(json.dumps({"status": 0, "boards": [{"unique_id": "PYOCD123"}]}))
        return 0
    if args and args[0] in {"commander", "cmd"}:
        return commander(args)
    if args and args[0] == "flash":
        print("[==================================] 100%")
        print("Programmed 8192 bytes @ 0x08000000")
        return 0
    print(f"pyocd {' '.join(args)} is not a call this recording holds", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
