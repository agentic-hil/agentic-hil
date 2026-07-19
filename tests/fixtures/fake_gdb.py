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


def main() -> int:
    global behavior_override

    emit('=thread-group-added,id="i1"')
    emit("(gdb)")
    next_breakpoint = 1
    for raw_line in sys.stdin:
        match = COMMAND_PATTERN.match(raw_line.strip())
        if match is None:
            continue
        token, command = match.group(1), match.group(2)
        if command == "-gdb-exit":
            emit(f"{token}^exit")
            return 0
        if command.startswith("-target-select"):
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
        elif command.startswith(("-gdb-set", "-interpreter-exec", "-target-download", "-break-delete")):
            emit(f"{token}^done")
        elif command.startswith("-break-insert"):
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
