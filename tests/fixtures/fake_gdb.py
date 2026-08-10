#!/usr/bin/env python3
"""Fake GDB/MI process for tests: token-numbered replies, delayed async *stopped records."""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

COMMAND_PATTERN = re.compile(r"^(\d+)(.*)$")
MEMORY_READ_PATTERN = re.compile(r"^-data-read-memory-bytes\s+(0x[0-9a-fA-F]+|\d+)\s+(\d+)$")
ASYNC_STOP_DELAY_S = 0.02

CTC_ARRAY_ADDRESS = 0x200006F0
CTC_ARRAY_SIZE = 408
BATCH_ADDRESS_PATTERN = re.compile(r'^printf\s+"(?P<marker>[A-Za-z_]+=)%lu\\n",\s*\(unsigned long\)&(?P<symbol>\w+)$')
BATCH_SIZE_PATTERN = re.compile(r'^printf\s+"(?P<marker>[A-Za-z_]+=)%lu\\n",\s*\(unsigned long\)sizeof\((?P<symbol>\w+)\)$')
# What the ELF's symbol table holds, for the offline query. Address and size
# match the values this fake answers over MI, so a dump resolves to the same
# place whether it went through a session or through the batch query.
BATCH_SYMBOLS = {"CTC_array": (CTC_ARRAY_ADDRESS, CTC_ARRAY_SIZE), "big_buffer": (0x20001000, 4096)}
BEHAVIOR_MARKER = b"FAKE_GDB_BEHAVIOR="
behavior_override = ""
EXPECTED_BREAKPOINT_STOP = '*stopped,reason="breakpoint-hit",disp="keep",bkptno="1",frame={addr="0x08000200",func="test_done",args=[],file="tests.c",fullname="/work/tests.c",line="123"},thread-id="1",stopped-threads="all"'
UNEXPECTED_BREAKPOINT_STOP = '*stopped,reason="breakpoint-hit",disp="keep",bkptno="99",frame={addr="0x08000300",func="assert_failed",args=[],file="assert.c",fullname="/work/assert.c",line="7"},thread-id="1",stopped-threads="all"'
HARDFAULT_STOP = '*stopped,reason="signal-received",signal-name="SIGINT",signal-meaning="Interrupt",frame={addr="0x08000400",func="HardFault_Handler",args=[],file="startup.c",fullname="/work/startup.c",line="88"},thread-id="1",stopped-threads="all"'


def emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def behavior() -> str:
    return behavior_override


def emit_delayed_stop(stop_line: str) -> None:
    time.sleep(ASYNC_STOP_DELAY_S)
    emit(stop_line)


def continue_stop_line() -> str:
    current = behavior()
    if current == "unexpected_breakpoint":
        return UNEXPECTED_BREAKPOINT_STOP
    if current == "hardfault":
        return HARDFAULT_STOP
    return EXPECTED_BREAKPOINT_STOP


def evaluate_expression(token: str, expression: str) -> None:
    if expression == "(unsigned long)&CTC_array":
        emit(f'{token}^done,value="{hex(CTC_ARRAY_ADDRESS)}"')
    elif expression == "sizeof(CTC_array)":
        emit(f'{token}^done,value="{CTC_ARRAY_SIZE}"')
    elif "missing_symbol" in expression:
        emit(f'{token}^error,msg="No symbol \\"missing_symbol\\" in current context."')
    else:
        emit(f'{token}^done,value="0"')


def read_memory(token: str, address_text: str, length_text: str) -> None:
    address = int(address_text, 16 if address_text.lower().startswith("0x") else 10)
    length = int(length_text)
    contents = "".join(f"{(address + index) & 0xFF:02x}" for index in range(length))
    emit(f'{token}^done,memory=[{{begin="{hex(address)}",offset="0x0",end="{hex(address + length)}",contents="{contents}"}}]')


def batch_query(args: list[str]) -> int:
    """`--batch -nx -q -ex ... <elf>`: read the ELF's symbol table and exit.

    No stdin is read and no target is selected, because the real invocation has
    no way to reach one: this branch exists to prove the offline query answers
    from the file alone. A symbol the table does not hold produces GDB's own
    words on stderr and no marker line, which is the only signal the caller
    parses.
    """
    if "-ex" not in args:
        print("No commands were given.", file=sys.stderr)
        return 1
    for index, argument in enumerate(args[:-1]):
        if argument != "-ex":
            continue
        command = args[index + 1]
        match = BATCH_ADDRESS_PATTERN.match(command) or BATCH_SIZE_PATTERN.match(command)
        if match is None:
            print(f'Undefined command: "{command}".', file=sys.stderr)
            continue
        entry = BATCH_SYMBOLS.get(match.group("symbol"))
        if entry is None:
            print(f'No symbol "{match.group("symbol")}" in current context.', file=sys.stderr)
            continue
        value = entry[0] if BATCH_ADDRESS_PATTERN.match(command) else entry[1]
        emit(f"{match.group('marker')}{value}")
    return 0


def main() -> int:
    global behavior_override

    if "--batch" in sys.argv[1:]:
        return batch_query(sys.argv[1:])

    emit('=thread-group-added,id="i1"')
    emit("(gdb)")
    next_breakpoint = 1
    live_breakpoints: set[int] = set()
    reset_count = 0
    for raw_line in sys.stdin:
        match = COMMAND_PATTERN.match(raw_line.strip())
        if match is None:
            continue
        token, command = match.group(1), match.group(2)
        if command == "-gdb-exit":
            emit(f"{token}^exit")
            return 0
        if command.startswith("-target-select"):
            if behavior() == "target_select_timeout":
                continue
            emit(f"{token}^done")
            if behavior() == "stopped_on_attach_hardfault":
                emit(HARDFAULT_STOP)
        elif command.startswith("-file-exec-and-symbols"):
            artifact_path = command[len("-file-exec-and-symbols") :].strip().strip('"').replace("\\\\", "\\")
            try:
                artifact_data = Path(artifact_path).read_bytes()
                if BEHAVIOR_MARKER in artifact_data:
                    behavior_override = artifact_data.split(BEHAVIOR_MARKER, 1)[1].splitlines()[0].decode()
            except OSError:
                pass
            emit(f"{token}^done")
        elif command.startswith("-target-download"):
            if behavior() == "download_timeout":
                continue
            if behavior() == "download_error":
                emit(f'{token}^error,msg="Download failed"')
                continue
            emit(f"{token}^done")
        elif command.startswith("-interpreter-exec"):
            reset_count += 1
            if reset_count == 2 and behavior() == "post_load_reset_timeout":
                continue
            if reset_count == 2 and behavior() == "post_load_reset_error":
                emit(f'{token}^error,msg="Reset failed"')
                continue
            emit(f"{token}^done")
        elif command.startswith("-gdb-set"):
            emit(f"{token}^done")
        elif command.startswith("-break-delete"):
            for number_text in command[len("-break-delete") :].split():
                if number_text.isdigit():
                    live_breakpoints.discard(int(number_text))
            emit(f"{token}^done")
        elif command.startswith("-break-list"):
            body = ",".join(f'bkpt={{number="{number}",type="breakpoint",disp="keep",enabled="y",addr="0x08000200",func="test_done",file="tests.c",line="123"}}' for number in sorted(live_breakpoints))
            emit(f'{token}^done,BreakpointTable={{nr_rows="{len(live_breakpoints)}",nr_cols="6",body=[{body}]}}')
        elif command.startswith("-break-insert"):
            live_breakpoints.add(next_breakpoint)
            emit(f'{token}^done,bkpt={{number="{next_breakpoint}",type="breakpoint",disp="keep",enabled="y",addr="0x08000200",func="test_done",file="tests.c",line="123"}}')
            next_breakpoint += 1
        elif command.startswith("-exec-continue"):
            emit(f"{token}^running")
            emit("*running,thread-id=\"all\"")
            threading.Thread(target=emit_delayed_stop, args=(continue_stop_line(),), daemon=True).start()
        elif command.startswith("-exec-interrupt"):
            emit(f"{token}^done")
            if behavior() != "halt_timeout":
                emit('*stopped,reason="signal-received",signal-name="SIGINT",frame={addr="0x08000100",func="main",file="main.c",line="42"}')
        elif command.startswith("-data-evaluate-expression"):
            expression = command[len("-data-evaluate-expression") :].strip().strip('"')
            evaluate_expression(token, expression)
        elif MEMORY_READ_PATTERN.match(command):
            memory_match = MEMORY_READ_PATTERN.match(command)
            assert memory_match is not None
            read_memory(token, memory_match.group(1), memory_match.group(2))
        else:
            emit(f'{token}^error,msg="Undefined MI command: {command}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
