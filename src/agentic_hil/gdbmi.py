from __future__ import annotations

import re
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from agentic_hil.config import atomic_write_text
from agentic_hil.process import process_group_kwargs, terminate_process_tree
from agentic_hil.types import JsonObject

GDB_MI_ARGS = ["--nx", "--quiet", "--interpreter=mi2", "--init-eval-command=set auto-load off"]
RESULT_RECORD_PATTERN = re.compile(r"^(\d+)\^(done|running|connected|error|exit)(?:,.*)?$")
MI_FIELD_ESCAPE_PATTERN = re.compile(r"\\(.)")
STOP_RECORD_PREFIX = "*stopped,"
INTEL_HEX_BYTES_PER_RECORD = 16
INTEL_HEX_EOF_RECORD = ":00000001FF"
GDB_EXIT_COMMAND_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class GdbMiCommandResult:
    result_class: str
    line: str
    records: list[str] = field(default_factory=list)
    timed_out: bool = False
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.result_class in {"done", "running", "connected"}


@dataclass(frozen=True)
class GdbMiStopResult:
    line: str
    reason: str
    timed_out: bool = False
    error_message: str | None = None


class GdbMiClient:
    """Serialized GDB/MI transport: one in-flight command, token-matched replies, buffered async stops."""

    def __init__(self, executable: str, work_dir: str):
        from agentic_hil.backends.common import invocation

        self.child = subprocess.Popen(
            [*invocation(executable), *GDB_MI_ARGS],
            cwd=work_dir,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **process_group_kwargs(),
        )
        self.lock = threading.Lock()
        self.next_token = 0
        self.pending: JsonObject | None = None
        self.pending_stop: JsonObject | None = None
        self.last_stop_line: str | None = None
        self.command_history: list[JsonObject] = []
        self.exited = threading.Event()
        threading.Thread(target=self._stdout_reader, daemon=True).start()
        threading.Thread(target=self._stderr_reader, daemon=True).start()

    def command(self, mi_command: str, timeout_s: float) -> GdbMiCommandResult:
        if "\n" in mi_command or "\r" in mi_command:
            return self._command_error(mi_command, "GDB/MI command must be a single line.")
        with self.lock:
            if self.pending is not None:
                return self._command_error(mi_command, "Another GDB/MI command is still pending.")
            if self.exited.is_set() or self.child.stdin is None or self.child.stdin.closed:
                return self._command_error(mi_command, "GDB process is not running.")
            self.next_token += 1
            token = self.next_token
            pending: JsonObject = {"token": token, "records": [], "event": threading.Event(), "result": None}
            self.pending = pending
        try:
            self.child.stdin.write(f"{token}{mi_command}\n")
            self.child.stdin.flush()
        except (OSError, ValueError):
            with self.lock:
                if self.pending is pending:
                    self.pending = None
            return self._command_error(mi_command, "GDB process closed its input.")
        if not pending["event"].wait(timeout=max(0.0, timeout_s)):
            timed_out_result: GdbMiCommandResult | None = None
            with self.lock:
                if self.pending is pending:
                    self.pending = None
                    timed_out_result = GdbMiCommandResult(result_class="timeout", line="", records=list(pending["records"]), timed_out=True, error_message="GDB/MI command timed out.")
            if timed_out_result is not None:
                self._record_history(mi_command, timed_out_result)
                return timed_out_result
        result = pending["result"]
        if result is None:
            return self._command_error(mi_command, "GDB/MI command produced no result.")
        self._record_history(mi_command, result)
        return result

    def wait_for_stop(self, timeout_s: float) -> GdbMiStopResult:
        with self.lock:
            if self.last_stop_line is not None:
                line = self.last_stop_line
                self.last_stop_line = None
                return stop_result_from_line(line)
            if self.pending_stop is not None:
                return GdbMiStopResult(line="", reason="debugger_error", error_message="Another stop wait is already pending.")
            if self.exited.is_set():
                return GdbMiStopResult(line="", reason="debugger_error", error_message="GDB process is not running.")
            pending_stop: JsonObject = {"event": threading.Event(), "result": None}
            self.pending_stop = pending_stop
        if not pending_stop["event"].wait(timeout=max(0.0, timeout_s)):
            with self.lock:
                if self.pending_stop is pending_stop:
                    self.pending_stop = None
                    return GdbMiStopResult(line="", reason="timeout", timed_out=True)
        result = pending_stop["result"]
        if result is None:
            return GdbMiStopResult(line="", reason="timeout", timed_out=True)
        return result

    def poll_stop(self) -> GdbMiStopResult | None:
        with self.lock:
            if self.pending_stop is not None or self.last_stop_line is None:
                return None
            line = self.last_stop_line
            self.last_stop_line = None
        return stop_result_from_line(line)

    def is_running(self) -> bool:
        return not self.exited.is_set() and self.child.poll() is None

    def close(self, timeout_s: float) -> None:
        if self.is_running():
            self.command("-gdb-exit", min(GDB_EXIT_COMMAND_TIMEOUT_S, max(0.1, timeout_s)))
        with suppress(subprocess.TimeoutExpired):
            self.child.wait(timeout=max(0.1, timeout_s))
        if self.child.poll() is None:
            terminate_process_tree(self.child, timeout_s)
        if self.child.poll() is None:
            raise RuntimeError("GDB process remained active after kill.")

    def history(self) -> list[JsonObject]:
        with self.lock:
            return [dict(entry) for entry in self.command_history]

    def _command_error(self, mi_command: str, message: str) -> GdbMiCommandResult:
        result = GdbMiCommandResult(result_class="error", line="", error_message=message)
        self._record_history(mi_command, result)
        return result

    def _record_history(self, mi_command: str, result: GdbMiCommandResult) -> None:
        entry: JsonObject = {"command": mi_command, "result_class": result.result_class, "timed_out": result.timed_out}
        if result.error_message:
            entry["error"] = result.error_message
        with self.lock:
            self.command_history.append(entry)

    def _stdout_reader(self) -> None:
        stream = self.child.stdout
        if stream is not None:
            for raw_line in stream:
                self._handle_line(raw_line.rstrip("\r\n"))
        self._handle_exit()

    def _stderr_reader(self) -> None:
        stream = self.child.stderr
        if stream is None:
            return
        for raw_line in stream:
            with self.lock:
                if self.pending is not None:
                    self.pending["records"].append(f"stderr:{raw_line.rstrip()}")

    def _handle_line(self, line: str) -> None:
        with self.lock:
            if self.pending is not None:
                self.pending["records"].append(line)
            if line.startswith(STOP_RECORD_PREFIX):
                if self.pending_stop is not None:
                    self.pending_stop["result"] = stop_result_from_line(line)
                    self.pending_stop["event"].set()
                    self.pending_stop = None
                else:
                    self.last_stop_line = line
                return
            match = RESULT_RECORD_PATTERN.match(line)
            if match is None or self.pending is None or int(match.group(1)) != self.pending["token"]:
                return
            result_class = match.group(2)
            self.pending["result"] = GdbMiCommandResult(
                result_class=result_class,
                line=line,
                records=list(self.pending["records"]),
                error_message=mi_field(line, "msg") if result_class == "error" else None,
            )
            self.pending["event"].set()
            self.pending = None

    def _handle_exit(self) -> None:
        with self.lock:
            self.exited.set()
            returncode = self.child.poll()
            if self.pending is not None:
                self.pending["result"] = GdbMiCommandResult(result_class="error", line="", records=list(self.pending["records"]), error_message=f"GDB process exited with code {returncode}.")
                self.pending["event"].set()
                self.pending = None
            if self.pending_stop is not None:
                self.pending_stop["result"] = GdbMiStopResult(line="", reason="debugger_error", error_message=f"GDB process exited with code {returncode}.")
                self.pending_stop["event"].set()
                self.pending_stop = None


def stop_result_from_line(line: str) -> GdbMiStopResult:
    return GdbMiStopResult(line=line, reason=mi_field(line, "reason") or "unknown")


def mi_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def mi_field(line: str, name: str) -> str | None:
    match = re.search(rf'(?:^|[,{{}}]){re.escape(name)}="((?:\\.|[^"\\])*)"', line)
    if match is None:
        return None
    return unescape_mi_string(match.group(1))


def unescape_mi_string(value: str) -> str:
    replacements = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}
    return MI_FIELD_ESCAPE_PATTERN.sub(lambda match: replacements.get(match.group(1), match.group(1)), value)


def parse_gdb_integer(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    except ValueError:
        return None


def write_intel_hex_file(file_path: Path, start_address: int, data: bytes) -> None:
    lines: list[str] = []
    upper_address: int | None = None
    for offset in range(0, len(data), INTEL_HEX_BYTES_PER_RECORD):
        absolute = start_address + offset
        chunk_upper = (absolute >> 16) & 0xFFFF
        if chunk_upper != upper_address:
            lines.append(intel_hex_record(0, 0x04, bytes([(chunk_upper >> 8) & 0xFF, chunk_upper & 0xFF])))
            upper_address = chunk_upper
        lines.append(intel_hex_record(absolute & 0xFFFF, 0x00, data[offset : offset + INTEL_HEX_BYTES_PER_RECORD]))
    lines.append(INTEL_HEX_EOF_RECORD)
    atomic_write_text(file_path, "\n".join(lines) + "\n", encoding="ascii")


def intel_hex_record(address16: int, record_type: int, payload: bytes) -> str:
    record = bytes([len(payload), (address16 >> 8) & 0xFF, address16 & 0xFF, record_type, *payload])
    checksum = (-sum(record)) & 0xFF
    return f":{record.hex().upper()}{checksum:02X}"
