"""Opt-in recording of which pyOCD call lets a halted core run, on the bench's board (#631).

Without a debug session the pyOCD backend runs one `pyocd commander` process per
call: `reset_target` with mode `halt` runs `--command "reset halt"`, and every
symbol read runs `--connect attach --command "savemem ..."`. A reset that
reports a halt followed by reads of a counter that keep climbing means one of
the two lets the core run, and this module records which.

A look at the core must not be what it reports, so every look here is two
readings of the same things with a pause between them: DHCSR (whose `S_HALT`
says whether the core is halted, and whose sticky `S_RETIRE_ST`, cleared by the
first reading, says whether any instruction retired before the second) and the
demo's millisecond counter, whose movement is the core running by another
measure. Two observers take that look:

* the pyOCD process itself, after it has halted or connected, so what it reads
  is the state its own connect and command left, before it disconnects;
* OpenOCD, started with `init` and memory reads only, after the pyOCD process
  has exited, so what it reads is what that process's disconnect left.

The controls make the second observer sound: OpenOCD's look on a core known to
run must read it running, and two of its looks one after the other on a halted
core must read the same counter, or OpenOCD itself would be halting or resuming
what it reports.

The product's own calls are driven over `agentic-hil mcp-stdio` on a copy of
the tier's configuration with the probe on `type: pyocd`. The direct pyOCD and
OpenOCD processes read memory and DHCSR and nothing else, apart from the reset
a `reset halt` is; none of them writes flash. The demo is put back on the board
and started at the end.

Selected explicitly, like the other recorders: the file is not named `test_*`.
With `AGENTIC_HIL_RECORDING_OUT` set to a directory, the recording is written
there as `pyocd-halt-recording.json`; it is attached to the test report as a
property as well.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import yaml
from support import scaled_time_bound

from agentic_hil.backends.openocd import openocd_probe_selection
from agentic_hil.elfsymbols import read_elf_symbol

from .conftest import BENCH_ONLY, Bench, put_on_board
from .pyocd_gdbserver_recordings import debug_core_start_dhcsr_writes, write_recording
from .pyocd_recordings import PACK_ID, TARGET_TYPE, pyocd_bench_environment, pyocd_provenance, transcript
from .test_bench_debug_sessions import McpServer, workspace_image
from .test_bench_pyocd_sessions import PyocdBench

pytestmark = [pytest.mark.bench, BENCH_ONLY]

RECORDING_SCHEMA = "agentic-hil.pyocd-halt-recording/v1"
OUTPUT_NAME = "pyocd-halt-recording.json"
COUNTER = "uptime_ms"
DHCSR = 0xE000EDF0
S_HALT = 1 << 17
S_RETIRE_ST = 1 << 24
S_RESET_ST = 1 << 25
C_DEBUGEN = 1 << 0
C_HALT = 1 << 1
# The pause inside one look: a running core moves the demo's counter by about
# this many milliseconds, a halted one by none.
LOOK_PAUSE_MS = 300
# The pause between two looks, or between two of the product's reads.
SETTLE_S = 1.0
PROCESS_TIMEOUT_S = 60.0
# The read's connect with the pack's DebugCoreStart sequence left out, which is
# the sequence whose DHCSR write is under suspicion.
WITHOUT_DEBUG_CORE_START = ["-O", "pack.debug_sequences.disabled_sequences=DebugCoreStart"]
# A word as both observers print it: `e000edf0:  01030003` from pyOCD's
# `read32`, `0xe000edf0: 01030003` from OpenOCD's `mdw`.
WORD = re.compile(r"^\s*(?:0x)?([0-9a-fA-F]{8}):\s+([0-9a-fA-F]{8})\b")


def dhcsr_fields(value: int) -> dict:
    return {
        "value": f"0x{value:08x}",
        "c_debugen": bool(value & C_DEBUGEN),
        "c_halt": bool(value & C_HALT),
        "s_halt": bool(value & S_HALT),
        "s_retire_st": bool(value & S_RETIRE_ST),
        "s_reset_st": bool(value & S_RESET_ST),
    }


def words(lines: list[str], address: int) -> list[int]:
    """Every word the transcript printed for one address, in order."""
    found = []
    for line in lines:
        match = WORD.match(line)
        if match and int(match.group(1), 16) == address:
            found.append(int(match.group(2), 16))
    return found


def look_result(lines: list[str], counter_address: int) -> dict:
    """What one look read: DHCSR twice, the counter twice, and what the pair says.

    `halted` needs all three to agree: the counter did not move, no instruction
    retired between the two DHCSR readings, and the second reads `S_HALT`.
    `running` needs the counter to move and an instruction to have retired.
    Anything else is `inconclusive` and recorded as such."""
    registers = words(lines, DHCSR)[-2:]
    counters = words(lines, counter_address)[-2:]
    result: dict = {"dhcsr": [dhcsr_fields(value) for value in registers], "counter": counters}
    if len(registers) != 2 or len(counters) != 2:
        result["state"] = "not_read"
        return result
    moved = counters[1] - counters[0]
    second = registers[1]
    result["counter_moved"] = moved
    if moved == 0 and not second & S_RETIRE_ST and second & S_HALT:
        result["state"] = "halted"
    elif moved > 0 and second & S_RETIRE_ST and not second & S_HALT:
        result["state"] = "running"
    else:
        result["state"] = "inconclusive"
    return result


class Observers:
    """The two ways of looking at the core, and the product's calls between them."""

    def __init__(self, environment: dict[str, str], pyocd: str, uid: str, openocd_head: list[str], counter_address: int, project: Path) -> None:
        self.environment = environment
        self.project = project
        self.pyocd_executable = pyocd
        self.uid = uid
        self.openocd_head = openocd_head
        self.counter_address = counter_address

    def run(self, argv: list[str], cwd: str) -> dict:
        started = time.monotonic()
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=scaled_time_bound(PROCESS_TIMEOUT_S),
            check=False,
        )
        return {
            "argv_tail": argv[1:],
            "returncode": completed.returncode,
            "elapsed_s": round(time.monotonic() - started, 2),
            "output": completed.stdout.splitlines(),
        }

    def pyocd_look(self, before: list[str], extra: list[str] | None = None) -> dict:
        """A pyOCD commander that connects the way the read does, runs `before`, then looks, in one process."""
        counter = hex(self.counter_address)
        commands = [*before, f"read32 {hex(DHCSR)}", f"read32 {counter}", f"sleep {LOOK_PAUSE_MS}", f"read32 {counter}", f"read32 {hex(DHCSR)}"]
        argv = [self.pyocd_executable, "commander", "--connect", "attach", *(extra or [])]
        for command in commands:
            argv += ["--command", command]
        argv += ["--uid", self.uid, "--target", TARGET_TYPE, "-W"]
        record = self.run(argv, str(Path(self.pyocd_executable).parent))
        record["look"] = look_result(record["output"], self.counter_address)
        return record

    def openocd_look(self) -> dict:
        """OpenOCD's `init`, then the same look through memory reads, then `shutdown`."""
        counter = hex(self.counter_address)
        steps = ["init", f"mdw {hex(DHCSR)}", f"mdw {counter}", f"sleep {LOOK_PAUSE_MS}", f"mdw {counter}", f"mdw {hex(DHCSR)}", "shutdown"]
        argv = [*self.openocd_head]
        for step in steps:
            argv += ["-c", step]
        record = self.run(argv, str(self.project))
        record["look"] = look_result(record["output"], self.counter_address)
        return record


def product_call(server: McpServer, bench: Bench, tool: str, arguments: dict, private_values: tuple[str, ...]) -> dict:
    """One tool call over MCP, with the fields that matter here and the command the product ran."""
    answer = server.tool(tool, arguments)
    record = {
        key: answer.get(key)
        for key in ("ok", "error_type", "summary", "mode", "value_unsigned", "hex", "side_effect_status", "safe_state_confirmed", "hardware_state", "target_contacted")
        if key in answer
    }
    try:
        logged = transcript(bench, answer, private_values)["action_log"]
    except (OSError, ValueError):
        logged = None
    if isinstance(logged, dict):
        record["command"] = logged.get("command")
        record["output"] = f"{logged.get('stdout') or ''}{logged.get('stderr') or ''}".splitlines()
    return record


def operator_home() -> str:
    """The home directory of the account the bench runs as, which the test's own HOME no longer names."""
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, AttributeError):
        return ""


def openocd_head(debugger: dict) -> list[str]:
    """The tier's OpenOCD, its interface and target scripts and its probe selector, with every server port off."""
    named = str(debugger.get("executable") or "openocd")
    executable = named if Path(named).is_absolute() else shutil.which(named)
    assert executable, "the tier's OpenOCD is not on PATH, and the recording observes through it"
    selection = openocd_probe_selection(executable, debugger["interface_cfg"], debugger.get("probe_id"), 10.0)
    assert selection.supported, selection
    return [
        executable,
        "-f",
        debugger["interface_cfg"],
        *selection.commands,
        "-f",
        debugger["target_cfg"],
        "-c",
        "gdb_port disabled",
        "-c",
        "tcl_port disabled",
        "-c",
        "telnet_port disabled",
    ]


def test_record_which_pyocd_call_lets_a_halted_core_run(bench: Bench, firmware: Path, gdb: None, tmp_path: Path, record_property) -> None:
    environment = pyocd_bench_environment(bench)
    executable, pyocd_version, pack_version = pyocd_provenance(environment)
    document = bench.configuration()
    debugger = dict(document["debuggers"][bench.debugger_name()])
    uid = str(debugger.get("probe_id") or "")
    assert uid, "the bench configuration names no probe"
    symbol = read_elf_symbol(firmware, COUNTER)
    assert symbol["ok"] is True and symbol["size_bytes"] == 4, symbol
    observers = Observers(environment, executable, uid, openocd_head(debugger), int(symbol["address"]), bench.project)

    entry = document["debuggers"][bench.debugger_name()]
    entry["type"] = "pyocd"
    entry["executable"] = executable
    entry["target_type"] = TARGET_TYPE
    entry.pop("interface_cfg", None)
    entry.pop("target_cfg", None)
    variant = bench.config.parent / "bench-pyocd-halt-recording.yaml"
    variant.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    on_pyocd = PyocdBench(project=bench.project, config=variant, config_root=bench.config_root, state_root=bench.state_root)

    # The test's HOME is a sandbox, so the operator's own home, where the tools
    # are installed, and the sandbox's temporary root are named here as well.
    private_values = (
        uid,
        str(bench.project),
        str(bench.config_root),
        str(bench.state_root),
        str(tmp_path),
        tempfile.gettempdir(),
        str(Path.home()),
        operator_home(),
        str(Path(executable).parent),
        str(firmware.parent),
    )
    recording: dict = {
        "schema": RECORDING_SCHEMA,
        "recorded_on": time.strftime("%Y-%m-%d", time.gmtime()),
        "source_commit": os.environ.get("AGENTIC_HIL_BENCH_COMMIT"),
        "pyocd_version": pyocd_version,
        "openocd_version": subprocess.run([observers.openocd_head[0], "--version"], capture_output=True, text=True, timeout=scaled_time_bound(30), check=False).stderr.splitlines()[:1],
        "cmsis_pack": {"id": PACK_ID, "version": pack_version, "target_type": TARGET_TYPE, "debug_core_start_dhcsr_writes": debug_core_start_dhcsr_writes(environment, pack_version)},
        "probe": "the bench's in-circuit debugger, its unique ID redacted",
        "image": "the demo firmware the tier builds",
        "counter": {"symbol": COUNTER, "address": hex(int(symbol["address"])), "size_bytes": 4},
        "look_pause_ms": LOOK_PAUSE_MS,
        "settle_s": SETTLE_S,
        "controls": {},
        "scenarios": {},
    }
    controls = recording["controls"]
    scenarios = recording["scenarios"]
    server = None
    try:
        server = McpServer(on_pyocd)

        def call(tool: str, arguments: dict) -> dict:
            return product_call(server, on_pyocd, tool, arguments, private_values)

        flashed = call("flash_firmware", {"image_path": workspace_image(bench, firmware), "reset_after_flash": True})
        recording["flash"] = flashed
        assert flashed.get("ok") is True, flashed

        # Controls. OpenOCD's look on a core the product just started, twice:
        # both must read it running, or OpenOCD halts what it looks at.
        controls["running_core"] = {"reset_run": call("reset_target", {"mode": "run"})}
        time.sleep(SETTLE_S)
        controls["running_core"]["openocd_looks"] = [observers.openocd_look(), observers.openocd_look()]

        # The product's reset into halt, then OpenOCD twice with a pause between:
        # the state the reset's disconnect left, and whether OpenOCD's own look
        # resumed it.
        scenarios["product_reset_halt"] = {"reset_halt": call("reset_target", {"mode": "halt"})}
        scenarios["product_reset_halt"]["openocd_look"] = observers.openocd_look()
        time.sleep(SETTLE_S)
        scenarios["product_reset_halt"]["openocd_look_again"] = observers.openocd_look()

        # One product read of that halted core, then OpenOCD, then a second read.
        reads = scenarios["product_read_of_a_halted_core"] = {"first_read": call("debug_symbol_value", {"symbol": COUNTER})}
        reads["openocd_look"] = observers.openocd_look()
        time.sleep(SETTLE_S)
        reads["second_read"] = call("debug_symbol_value", {"symbol": COUNTER})

        # Inside one pyOCD process: `reset halt` and a look before it disconnects,
        # then OpenOCD after it has.
        inside = scenarios["pyocd_reset_halt_in_process"] = {"pyocd": observers.pyocd_look(["reset halt"])}
        inside["openocd_look"] = observers.openocd_look()

        # The read's connect on that halted core, looked at from inside the
        # same process before anything else runs, then OpenOCD after it.
        attach = scenarios["pyocd_attach_connect_on_a_halted_core"] = {"before": observers.openocd_look()}
        attach["pyocd"] = observers.pyocd_look([])
        attach["openocd_look"] = observers.openocd_look()

        # The same connect without the pack's DebugCoreStart, on a core the
        # product halted again.
        held = scenarios["pyocd_attach_without_debug_core_start_on_a_halted_core"] = {"reset_halt": call("reset_target", {"mode": "halt"})}
        held["before"] = observers.openocd_look()
        held["pyocd"] = observers.pyocd_look([], WITHOUT_DEBUG_CORE_START)
        held["openocd_look"] = observers.openocd_look()

        # And on a running core, where a read must not stop it.
        running = scenarios["pyocd_attach_without_debug_core_start_on_a_running_core"] = {"reset_run": call("reset_target", {"mode": "run"})}
        time.sleep(SETTLE_S)
        running["pyocd"] = observers.pyocd_look([], WITHOUT_DEBUG_CORE_START)
        running["openocd_look"] = observers.openocd_look()
    finally:
        if server is not None:
            recording["server_exit"] = server.shut_down(stop_session=False)
        variant.unlink(missing_ok=True)
        write_recording(recording, private_values, OUTPUT_NAME, "pyocd_halt_recording_v1", record_property)
        restored = put_on_board(bench, firmware)
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}"
