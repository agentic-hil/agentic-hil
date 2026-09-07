#!/usr/bin/env python3
"""Fake GDB/MI process for tests: token-numbered replies, delayed async *stopped records."""
from __future__ import annotations

import os
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
BOOT_COUNTER_ADDRESS = 0x20000080
BOOT_COUNTER_SIZE = 4
BATCH_ADDRESS_PATTERN = re.compile(r'^printf\s+"(?P<marker>[A-Za-z_]+=)%lu\\n",\s*\(unsigned long\)&(?P<symbol>\w+)$')
BATCH_SIZE_PATTERN = re.compile(r'^printf\s+"(?P<marker>[A-Za-z_]+=)%lu\\n",\s*\(unsigned long\)sizeof\((?P<symbol>\w+)\)$')
# What the ELF's debug information holds, for the offline query. Address and size
# match the values this fake answers over MI, so a dump resolves to the same
# place whether it went through a session or through the batch query.
BATCH_SYMBOLS = {
    "CTC_array": (CTC_ARRAY_ADDRESS, CTC_ARRAY_SIZE),
    "big_buffer": (0x20001000, 4096),
    "boot_counter": (BOOT_COUNTER_ADDRESS, BOOT_COUNTER_SIZE),
}
# Symbols the linker placed and the compiler never described: an assembly object
# with `.global`/`.type`/`.size` and no DWARF behind it. Real GDB answers both
# `&symbol` and `sizeof(symbol)` for one of these with this message, on stderr in
# batch mode and as an MI `^error` in a session, and it is the whole reason the
# resolution falls back to the ELF's symbol table (#187). This fake therefore
# refuses them exactly as GDB does and carries no address or size for them: what
# a test then resolves came out of the ELF the test built, not out of here.
UNTYPED_SYMBOLS = {"g_pfnVectors"}
UNKNOWN_TYPE_MESSAGE = "'{symbol}' has unknown type; cast it to its declared type"
BEHAVIOR_MARKER = b"FAKE_GDB_BEHAVIOR="
# The behaviour is learned twice. The artifact trailer is read when
# `-file-exec-and-symbols` arrives, which is the third command of a session, so
# anything the product sends before it is answered by the default fake; a test
# whose claim is about a startup command therefore seeds the same string through
# the environment, which the GDB child inherits from the test process and this
# fake reads before its first command. The trailer, when present, still wins.
BEHAVIOR_ENVIRONMENT_VARIABLE = "FAKE_GDB_BEHAVIOR"
behavior_override = os.environ.get(BEHAVIOR_ENVIRONMENT_VARIABLE, "")
# What the bench observed and the two issues recorded, 2026-09-05, a debug
# session over OpenOCD's GDB server (the GDB version itself was not recorded in
# either issue; the bench tier carries the hardware half of these tests):
#
# #495: GDB started without `-gdb-set mi-async on` does not read the next MI
#   command while `-exec-continue` is in flight. An `-exec-interrupt --all` sent
#   while the target runs is not processed until the target stops on its own,
#   and a firmware whose main loop never returns never does; the product saw
#   `halt_command_acknowledged: False` and then "GDB/MI command timed out." on
#   the explicit `debug_halt`. With asynchronous MI on, the interrupt is
#   processed while the target runs and answered `^done` followed by a
#   `*stopped` record carrying SIGINT.
# #492: `-exec-interrupt --all` on a target that is already stopped is
#   acknowledged `^done`, and no `*stopped` record follows, because the target
#   never resumed; the product saw `error_type: timeout` with "Target halt was
#   requested but not confirmed."
#
# The default fake answers every interrupt with a stop and every resume with a
# breakpoint hit, which is what the rest of the suite relies on. The behaviour
# below is opted into by the tests that need the bench's answers.
BENCH_RUN_STATE = "bench_run_state"
# Two more of the bench's run-state answers, each opted into on top of it:
# `halt_timeout` acknowledges an interrupt and never stops (the shape of a probe
# that lost the core), and `interrupt_lost_once` leaves the first interrupt
# unanswered altogether and the target running, so the product's containment
# fails once for a real reason and a later `debug_halt` meets a target that is
# still running.
HALT_TIMEOUT = "halt_timeout"
INTERRUPT_LOST_ONCE = "interrupt_lost_once"
# A GDB without the setting. The product must refuse at session start; the
# assertion behind this is that whatever the debugger answered is carried to
# the caller, and this is the text this fake answers. It is not a recording:
# no GDB without asynchronous MI has been driven on the bench, and a recording
# from one (older than 7.8, where the setting was still `target-async`, or built
# without async support, given `-gdb-set mi-async on` under `--interpreter=mi2`)
# is still owed.
MI_ASYNC_UNSUPPORTED = "mi_async_unsupported"
MI_ASYNC_REFUSAL = 'No symbol \\"mi\\" in current context.'
INTERRUPT_STOP = '*stopped,reason="signal-received",signal-name="SIGINT",frame={addr="0x08000100",func="main",file="main.c",line="42"}'
target_running = False
mi_async = False
run_state_lock = threading.Lock()
target_stopped = threading.Event()
target_stopped.set()
# Which resume the target is running from. A delayed breakpoint stop belongs to
# the resume that scheduled it; once an interrupt has stopped that resume, the
# delayed stop must not land on the next one, or the product reads a stale
# breakpoint stop and attributes it to a resume that never reached one.
resume_generation = 0
EXPECTED_BREAKPOINT_STOP = '*stopped,reason="breakpoint-hit",disp="keep",bkptno="1",frame={addr="0x08000200",func="test_done",args=[],file="tests.c",fullname="/work/tests.c",line="123"},thread-id="1",stopped-threads="all"'
UNEXPECTED_BREAKPOINT_STOP = '*stopped,reason="breakpoint-hit",disp="keep",bkptno="99",frame={addr="0x08000300",func="assert_failed",args=[],file="assert.c",fullname="/work/assert.c",line="7"},thread-id="1",stopped-threads="all"'
HARDFAULT_STOP = '*stopped,reason="signal-received",signal-name="SIGINT",signal-meaning="Interrupt",frame={addr="0x08000400",func="HardFault_Handler",args=[],file="startup.c",fullname="/work/startup.c",line="88"},thread-id="1",stopped-threads="all"'
# The stop records the public `stop_reason` vocabulary maps and the suite had
# never produced (#506): a target that ran off the end of main, a stop inside
# the reset vector, a breakpoint instruction nobody set, a fault delivered as a
# signal, a signal outside every named set, and the end of a step. The shapes
# are the GDB/MI `*stopped` records the GDB manual documents (Async Records:
# `exited-normally`, `signal-received` with `signal-name` and `signal-meaning`,
# `end-stepping-range`, each with the `frame` tuple OpenOCD's gdbserver
# populates); a bench recording of a real fault and a real reset over
# arm-none-eabi-gdb is still owed and would replace these. Each is opted into
# by name, so the default fake keeps answering the breakpoint hit the rest of
# the suite relies on.
STOP_EXITED_NORMALLY = "stop_exited_normally"
STOP_IN_RESET_HANDLER = "stop_in_reset_handler"
STOP_SIGTRAP = "stop_sigtrap"
STOP_SIGSEGV = "stop_sigsegv"
STOP_SIGUSR1 = "stop_sigusr1"
STOP_END_STEPPING_RANGE = "stop_end_stepping_range"
EXITED_NORMALLY_STOP = '*stopped,reason="exited-normally"'
RESET_HANDLER_STOP = '*stopped,reason="signal-received",signal-name="SIGINT",signal-meaning="Interrupt",frame={addr="0x080001c0",func="Reset_Handler",args=[],file="startup_stm32f446xx.s",fullname="/work/startup_stm32f446xx.s",line="65"},thread-id="1",stopped-threads="all"'
SIGTRAP_STOP = '*stopped,reason="signal-received",signal-name="SIGTRAP",signal-meaning="Trace/breakpoint trap",frame={addr="0x08000310",func="assert_failed",args=[],file="assert.c",fullname="/work/assert.c",line="9"},thread-id="1",stopped-threads="all"'
SIGSEGV_STOP = '*stopped,reason="signal-received",signal-name="SIGSEGV",signal-meaning="Segmentation fault",frame={addr="0x08000520",func="main",args=[],file="main.c",fullname="/work/main.c",line="77"},thread-id="1",stopped-threads="all"'
SIGUSR1_STOP = '*stopped,reason="signal-received",signal-name="SIGUSR1",signal-meaning="User defined signal 1",frame={addr="0x08000530",func="main",args=[],file="main.c",fullname="/work/main.c",line="80"},thread-id="1",stopped-threads="all"'
END_STEPPING_RANGE_STOP = '*stopped,reason="end-stepping-range",frame={addr="0x08000534",func="main",args=[],file="main.c",fullname="/work/main.c",line="81"},thread-id="1",stopped-threads="all"'
STOP_LINES_BY_BEHAVIOR = {
    STOP_EXITED_NORMALLY: EXITED_NORMALLY_STOP,
    STOP_IN_RESET_HANDLER: RESET_HANDLER_STOP,
    STOP_SIGTRAP: SIGTRAP_STOP,
    STOP_SIGSEGV: SIGSEGV_STOP,
    STOP_SIGUSR1: SIGUSR1_STOP,
    STOP_END_STEPPING_RANGE: END_STEPPING_RANGE_STOP,
}
# A GDB that dies under the session (#506), in the three timings the transport
# tells apart. `gdb_exits_after_running` answers `-exec-continue` with
# `^running` and the `*running` record and then exits at once, so the pipe is
# already closed when the product asks to wait for a stop.
# `gdb_exits_during_the_stop_wait` answers the same way and exits a moment
# later, so the exit lands while the stop wait is pending and it is the wait
# that has to report it. `gdb_exits_before_answering` exits with the command
# itself still pending, so the product's command wait is what sees the close.
GDB_EXITS_AFTER_RUNNING = "gdb_exits_after_running"
GDB_EXITS_DURING_THE_STOP_WAIT = "gdb_exits_during_the_stop_wait"
GDB_EXITS_BEFORE_ANSWERING = "gdb_exits_before_answering"
# Long enough for the product to have entered its stop wait, short enough that
# a test waiting on that wait is not slowed by it.
STOP_WAIT_EXIT_DELAY_S = 0.5
# The four answers to `-data-read-memory-bytes` that are not the bytes that were
# asked for, each opted into by name so the default fake keeps answering the
# full window the rest of the suite reads (#521). `memory_read_refused` is the
# refusal GDB gives for an address the target will not read, `memory_read_hangs`
# never answers at all, `memory_read_without_contents` answers `^done` with an
# empty memory list and therefore no `contents` field, and `memory_read_short`
# answers with half the bytes the command asked for.
#
# None of the four is a recording: no GDB refusing a read has been driven on the
# bench, and the issue quotes no GDB text either. The refusal message below is
# the one arm-none-eabi-gdb is documented to print for an unreadable address and
# a recording of a real one is owed; the other three shapes are answered by the
# structure of the reply rather than by any text, so what the product reads out
# of them does not depend on a tool's wording.
MEMORY_READ_REFUSED = "memory_read_refused"
MEMORY_READ_HANGS = "memory_read_hangs"
MEMORY_READ_WITHOUT_CONTENTS = "memory_read_without_contents"
MEMORY_READ_SHORT = "memory_read_short"
MEMORY_READ_REFUSAL = "Cannot access memory at address 0x20000080"


def emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def behavior() -> str:
    return behavior_override


def has_behavior(name: str) -> bool:
    """Whether `name` is among the behaviours the artifact asked for.

    The marker takes several joined by `+`, so a test can combine the bench's
    run-state answers with one of the stop lines below."""
    return name in behavior_override.split("+")


def mark_running() -> int:
    """Record the resume and return its generation, for the stop that ends it."""
    global target_running, resume_generation
    with run_state_lock:
        target_running = True
        resume_generation += 1
        target_stopped.clear()
        return resume_generation


def mark_stopped(stop_line: str, generation: int | None = None) -> None:
    """Emit the stop and record that the target is halted, atomically enough
    that a command read after the record sees a stopped target.

    With a generation, only if the target is still running from that resume:
    a stop scheduled for a resume that an interrupt already ended is dropped."""
    global target_running
    with run_state_lock:
        if generation is not None and (not target_running or generation != resume_generation):
            return
        target_running = False
        emit(stop_line)
        target_stopped.set()


def is_running() -> bool:
    with run_state_lock:
        return target_running


def emit_delayed_bench_stop(stop_line: str, generation: int) -> None:
    time.sleep(ASYNC_STOP_DELAY_S)
    mark_stopped(stop_line, generation)


def emit_delayed_stop(stop_line: str) -> None:
    time.sleep(ASYNC_STOP_DELAY_S)
    emit(stop_line)


def continue_stop_line() -> str:
    if has_behavior("unexpected_breakpoint"):
        return UNEXPECTED_BREAKPOINT_STOP
    if has_behavior("hardfault"):
        return HARDFAULT_STOP
    for name, line in STOP_LINES_BY_BEHAVIOR.items():
        if has_behavior(name):
            return line
    return EXPECTED_BREAKPOINT_STOP


def evaluate_expression(token: str, expression: str) -> None:
    untyped = next((symbol for symbol in UNTYPED_SYMBOLS if symbol in expression), None)
    if expression == "(unsigned long)&CTC_array":
        emit(f'{token}^done,value="{hex(CTC_ARRAY_ADDRESS)}"')
    elif expression == "sizeof(CTC_array)":
        emit(f'{token}^done,value="{CTC_ARRAY_SIZE}"')
    elif expression == "(unsigned long)&boot_counter":
        emit(f'{token}^done,value="{hex(BOOT_COUNTER_ADDRESS)}"')
    elif expression == "sizeof(boot_counter)":
        emit(f'{token}^done,value="{BOOT_COUNTER_SIZE}"')
    elif untyped is not None:
        emit(f'{token}^error,msg="{UNKNOWN_TYPE_MESSAGE.format(symbol=untyped)}"')
    elif "missing_symbol" in expression:
        emit(f'{token}^error,msg="No symbol \\"missing_symbol\\" in current context."')
    else:
        emit(f'{token}^done,value="0"')


def read_memory(token: str, address_text: str, length_text: str) -> None:
    address = int(address_text, 16 if address_text.lower().startswith("0x") else 10)
    length = int(length_text)
    if has_behavior(MEMORY_READ_HANGS):
        return
    if has_behavior(MEMORY_READ_REFUSED):
        emit(f'{token}^error,msg="{MEMORY_READ_REFUSAL}"')
        return
    if has_behavior(MEMORY_READ_WITHOUT_CONTENTS):
        emit(f"{token}^done,memory=[]")
        return
    if has_behavior(MEMORY_READ_SHORT):
        length = max(1, length // 2)
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
        symbol = match.group("symbol")
        if symbol in UNTYPED_SYMBOLS:
            print(UNKNOWN_TYPE_MESSAGE.format(symbol=symbol), file=sys.stderr)
            continue
        entry = BATCH_SYMBOLS.get(symbol)
        if entry is None:
            print(f'No symbol "{symbol}" in current context.', file=sys.stderr)
            continue
        value = entry[0] if BATCH_ADDRESS_PATTERN.match(command) else entry[1]
        emit(f"{match.group('marker')}{value}")
    return 0


def main() -> int:
    global behavior_override, mi_async

    if "--batch" in sys.argv[1:]:
        return batch_query(sys.argv[1:])

    emit('=thread-group-added,id="i1"')
    emit("(gdb)")
    next_breakpoint = 1
    live_breakpoints: set[int] = set()
    reset_count = 0
    interrupts_lost = 0
    for raw_line in sys.stdin:
        match = COMMAND_PATTERN.match(raw_line.strip())
        if match is None:
            continue
        token, command = match.group(1), match.group(2)
        # Synchronous MI: the command just read is not processed while the
        # target runs. It is processed once the target stops on its own; a target
        # that never stops leaves it unread until the fake is killed (#495).
        if has_behavior(BENCH_RUN_STATE) and not mi_async and is_running():
            target_stopped.wait()
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
            if "mi-async" in command and has_behavior(MI_ASYNC_UNSUPPORTED):
                emit(f'{token}^error,msg="{MI_ASYNC_REFUSAL}"')
                continue
            if "mi-async" in command:
                mi_async = command.split()[-1] == "on"
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
            if has_behavior(GDB_EXITS_BEFORE_ANSWERING):
                return 0
            emit(f"{token}^running")
            emit("*running,thread-id=\"all\"")
            if has_behavior(GDB_EXITS_AFTER_RUNNING):
                return 0
            if has_behavior(GDB_EXITS_DURING_THE_STOP_WAIT):
                time.sleep(STOP_WAIT_EXIT_DELAY_S)
                return 0
            if has_behavior(BENCH_RUN_STATE):
                # The demo's main loop never returns: only a live breakpoint
                # stops a resumed target, and nothing else ever does.
                generation = mark_running()
                if live_breakpoints:
                    threading.Thread(target=emit_delayed_bench_stop, args=(continue_stop_line(), generation), daemon=True).start()
                continue
            threading.Thread(target=emit_delayed_stop, args=(continue_stop_line(),), daemon=True).start()
        elif command.startswith("-exec-interrupt"):
            if has_behavior(INTERRUPT_LOST_ONCE) and interrupts_lost == 0:
                # Not answered at all, and the target keeps running.
                interrupts_lost += 1
                continue
            emit(f"{token}^done")
            if has_behavior(BENCH_RUN_STATE):
                # Acknowledged either way; a stop follows only from a target
                # that was running (#492), and from a probe that still has it.
                if is_running() and not has_behavior(HALT_TIMEOUT):
                    mark_stopped(INTERRUPT_STOP)
                continue
            if not has_behavior(HALT_TIMEOUT):
                emit(INTERRUPT_STOP)
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
