"""`flash_firmware` with `capture`: flash, reset and read the boot output in one call.

The inner loop of firmware work on a board is flash, reset, read what the
firmware prints. With the session tools that is `com_session_start`,
`flash_firmware` with `reset_after_flash`, one or more `com_read` and
`com_session_stop`, inside a declared run: six calls or more, each a full round
trip. `capture` does the same job in one call, under the same locks, the same
permissions and the same reports.

The teeth here are in four places:

* everything that can refuse the capture refuses before the flash, and a
  refusal flashes nothing and says so;
* the port is opened, its input cleared, before the flash, and the session is
  stopped after the read, with the probe and the port held from before the open
  until after the stop;
* each way the call can fail keeps what it knows: a flash that failed keeps the
  bytes the board had already sent, and a read or a stop that failed after a
  good flash keeps the flash;
* without `capture`, nothing changes.

The board is faked at the hardware boundary, the probe and the serial line
both, so the order of events is read off the board's own record rather than
inferred from the results. The boot text below is invented firmware output, not
a recording of any board.
"""

from __future__ import annotations

import errno
import json
import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, write_config
from support import scaled_time_bound

from agentic_hil.bench import BenchMutex
from agentic_hil.comports import data_result
from agentic_hil.config import load_config
from agentic_hil.contracts import MCP_TOOLS
from agentic_hil.coordination import com_resource
from agentic_hil.knowledge import permission_key
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.report import canonical_audit_log_path, overall_success
from agentic_hil.tools import AgenticHILToolService

# Device locks are machine-wide, so a port id and a device name shared with
# another checkout's tests would contend across sibling clones. These are this
# file's alone.
PORT_ID = "flash_capture_uart"
DEVICE = "/dev/ttyFLASHCAPTURE0"

# What the firmware prints after its reset, and two lines of it a caller would
# wait for.
BANNER = b"\r\nboot: demo firmware 1.0\r\nclock: 180 MHz\r\nself-test: pass\r\nREADY\r\n"
READY = "READY\r\n"
CLOCK = "clock: 180 MHz\r\n"

# The keys a flash result carried before `capture` existed, beyond the ones the
# backend answers with itself: the lease, the report, the run.
TODAY_FLASH_ENVELOPE = frozenset(
    {
        "audit_ok",
        "cleanup_reasons",
        "cleanup_required",
        "config_in_force",
        "config_status",
        "lease_id",
        "lease_state",
        "processes_reaped",
        "quarantine_id",
        "quarantined",
        "report_path",
        "resources",
        "run",
        "safe_state_confirmed",
    }
)

# The moments at which the board notes what this server holds on the bench.
WATCHED_EVENTS = frozenset({"open", "flash", "close"})


class Board:
    """One board on the bench: its UART line, and its own record of what reached it.

    `events` is the order things happened to the board in: the port opened, its
    input cleared, the flash, the reset, each read that returned bytes, the port
    closed. `held_at` is what this server held on the bench at the moment the
    port opened, the flash began and the port closed, which is how "held from
    before the open until after the stop" is read off the hardware rather than
    off a result."""

    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.line = bytearray()
        self.events: list[str] = []
        self.held_at: dict[str, frozenset[str]] = {}
        self.watch: Callable[[], frozenset[str]] = frozenset
        self.constructed = 0
        self.opens = 0
        self.busy = False
        self.close_failures = 0
        self.fail_reads = False
        self.read_calls = 0
        self.last_data_read = 0
        self.handle: BoardUart | None = None
        self.timers: list[threading.Timer] = []

    def construct(self, *args, **kwargs) -> BoardUart:
        self.constructed += 1
        self.handle = BoardUart(self)
        return self.handle

    def record(self, event: str) -> None:
        if event in WATCHED_EVENTS and event not in self.held_at:
            self.held_at[event] = frozenset(self.watch())
        with self.guard:
            self.events.append(event)

    def forget(self) -> None:
        with self.guard:
            self.events.clear()
        self.held_at.clear()

    def transmit(self, data: bytes) -> None:
        """Put `data` on the line and wait until the port's reader has taken it.

        The wait makes the order a fact rather than a race: bytes the board
        sends at its reset are in the session's buffer before the flash
        returns, so a test that says they were captured does not lean on the
        scheduler. A line nobody has open is not waited on."""
        with self.guard:
            self.line.extend(data)
        self.wait_until_buffered()

    def transmit_later(self, delay_s: float, data: bytes) -> None:
        timer = threading.Timer(delay_s, self.transmit, args=(data,))
        timer.daemon = True
        self.timers.append(timer)
        timer.start()

    def wait_until_buffered(self) -> None:
        deadline = time.monotonic() + scaled_time_bound(5.0)
        while time.monotonic() < deadline:
            with self.guard:
                handle = self.handle
                if handle is None or not handle.is_open:
                    return
                if not self.line and self.read_calls > self.last_data_read:
                    return
            time.sleep(0.005)
        self.record("reader_stalled")

    def finish(self) -> None:
        for timer in self.timers:
            timer.cancel()
        for timer in self.timers:
            timer.join()


class BoardUart:
    """The serial handle the port opens: this board's end of the line."""

    def __init__(self, board: Board) -> None:
        self.board = board
        self.is_open = False
        self.exclusive = None

    @property
    def in_waiting(self) -> int:
        with self.board.guard:
            return len(self.board.line)

    def open(self) -> None:
        self.board.opens += 1
        if self.board.busy:
            raise OSError(errno.EWOULDBLOCK, f"Could not exclusively lock port {DEVICE}: [Errno 11] Resource temporarily unavailable")
        self.is_open = True
        self.board.record("open")

    def reset_input_buffer(self) -> None:
        with self.board.guard:
            self.board.line.clear()
        self.board.record("clear")

    def read(self, size: int) -> bytes:
        board = self.board
        with board.guard:
            board.read_calls += 1
            if not self.is_open:
                raise OSError("port is closed")
            if board.fail_reads and not board.line:
                raise OSError("device disconnected")
            data = bytes(board.line[:size])
            del board.line[:size]
            if data:
                board.last_data_read = board.read_calls
                board.events.append("rx")
            return data

    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        return None

    def cancel_read(self) -> None:
        return None

    def close(self) -> None:
        if self.board.close_failures > 0:
            self.board.close_failures -= 1
            self.board.record("close_failed")
            raise OSError("port busy during close")
        self.is_open = False
        self.board.record("close")


class BoardBackend:
    """The probe: flashes and resets the board, and records what it drove.

    The same hardware-boundary double the implicit-run tests use, with one
    addition: a reset into run makes the firmware print, and what it prints goes
    out on the board's UART."""

    def __init__(
        self,
        board: Board,
        *,
        banner: bytes = BANNER,
        during_flash: bytes = b"",
        flash_s: float = 0.0,
        banner_delay_s: float = 0.0,
        unconfirmed: bool = False,
        late: bytes = b"",
        die_after_banner: bool = False,
    ) -> None:
        self.board = board
        self.banner = banner
        self.during_flash = during_flash
        self.flash_s = flash_s
        self.banner_delay_s = banner_delay_s
        self.unconfirmed = unconfirmed
        self.late = late
        self.die_after_banner = die_after_banner
        self.calls: list[str] = []

    def flash_firmware(self, artifact, reset_after_flash=False, *args, **kwargs) -> dict:
        self.calls.append("flash_firmware")
        self.board.record("flash")
        if self.during_flash:
            # The old firmware is still running until the probe takes the core.
            self.board.transmit(self.during_flash)
        if self.flash_s:
            time.sleep(self.flash_s)
        if self.unconfirmed:
            if self.late:
                self.board.transmit_later(0.5, self.late)
            # What a backend says when it cannot tell whether the image landed:
            # the one answer that raises an incident rather than merely failing.
            return {"ok": False, "tool": "flash_firmware", "error_type": "flash_failed", "side_effect_status": "unknown", "cleanup_required": True}
        if reset_after_flash:
            self._reset_into_run()
        return {
            "ok": True,
            "tool": "flash_firmware",
            "reset_after_flash": reset_after_flash,
            "success_confirmed": True,
            "side_effect_committed": True,
            "side_effect_status": "committed",
            "retry_safe": False,
        }

    def _reset_into_run(self) -> None:
        self.board.record("reset")
        if self.banner:
            if self.banner_delay_s:
                self.board.transmit_later(self.banner_delay_s, self.banner)
            else:
                self.board.transmit(self.banner)
        if self.die_after_banner:
            self.board.fail_reads = True

    def reset_target(self, mode: str = "run") -> dict:
        self.calls.append(f"reset_target:{mode}")
        if mode == "run":
            self._reset_into_run()
        return {"ok": True, "tool": "reset_target", "mode": mode}

    def probe_target(self) -> dict:
        self.calls.append("probe_target")
        return {"ok": True, "tool": "probe_target", "target_detected": True}

    def close(self) -> None:
        return None

    def sessionless_debug_tools(self) -> frozenset[str]:
        return frozenset()


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch):
    fake = Board()
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=fake.construct))
    yield fake
    fake.finish()


def config_for(workspace: Path, *, permissions: dict[str, bool] | None = None, port_yaml: str = ""):
    written = write_config(
        workspace,
        com_ports_yaml=f'com_ports:\n  {PORT_ID}:\n    device: "{DEVICE}"\n{port_yaml}',
        permissions=permissions,
    )
    return load_config(str(written))


def firmware(workspace: Path) -> dict:
    image = workspace / "build" / "app.elf"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"\x7fELF" + b"\x00" * 12)
    return {"image_path": "build/app.elf"}


def flash_args(workspace: Path, **capture: object) -> dict:
    return {**firmware(workspace), "reset_after_flash": True, "capture": {"port_id": PORT_ID, **capture}}


def service_for(config, board: Board, **options) -> tuple[AgenticHILToolService, BoardBackend]:
    backend = BoardBackend(board, **options)
    service = AgenticHILToolService(config, backend=backend)
    board.watch = service.coordinator.bench.held_resources
    return service, backend


def openocd_service(config, board: Board) -> AgenticHILToolService:
    """The configured OpenOCD backend, driving the suite's fake OpenOCD."""
    service = AgenticHILToolService(config)
    board.watch = service.coordinator.bench.held_resources
    return service


def capture_of(result: dict) -> dict:
    capture = result.get("capture")
    assert isinstance(capture, dict), result
    return capture


def named(result: dict) -> str:
    """Where a refusal names an argument: its field, and the sentence a caller reads."""
    return f"{result.get('field', '')} {result.get('summary', '')}"


def session_log(config, log_path: str) -> list[dict]:
    path = Path(config.work_dir) / log_path
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def received(config, log_path: str) -> bytes:
    """Every byte the session's log says the port received."""
    return b"".join(bytes.fromhex(entry["hex"]) for entry in session_log(config, log_path) if entry.get("direction") == "rx")


def ledger_events(config, log_path: str) -> list[str | None]:
    """The events the trusted canonical copy of a session log holds."""
    path = canonical_audit_log_path(config, log_path)
    if not path.is_file():
        return []
    return [json.loads(line).get("event") for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def without_time(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if key != "time"}


def assert_bench_free(service: AgenticHILToolService) -> None:
    """No run, nothing held, no session and no lease: what every path leaves behind."""
    status = service.call("bench_run_status")
    assert status["run_active"] is False, status
    assert status["held_devices"] == [], status
    assert service.com_ports.sessions == {}, sorted(service.com_ports.sessions)
    assert not service.coordinator.leases, sorted(service.coordinator.leases)


def end_run_if_open(service: AgenticHILToolService) -> None:
    if service.call("bench_run_status").get("run_active") is True:
        service.call("bench_run_stop")


def flash_tool() -> dict:
    return next(tool for tool in MCP_TOOLS if tool["name"] == "flash_firmware")


def without_descriptions(schema: object) -> object:
    if isinstance(schema, dict):
        return {key: without_descriptions(value) for key, value in schema.items() if key != "description"}
    if isinstance(schema, list):
        return [without_descriptions(item) for item in schema]
    return schema


# ---------------------------------------------------------------------------
# A. The argument: its shape, and the reset it needs.


@pytest.mark.parametrize(
    ("capture", "field", "validators"),
    [
        ({}, "capture.port_id", {"required"}),
        ({"port_id": ""}, "capture.port_id", {"minLength"}),
        ({"port_id": PORT_ID, "baudrate": 9600}, "capture.baudrate", {"additionalProperties"}),
        ("uart", "capture", {"type"}),
        ({"port_id": PORT_ID, "wait_timeout_s": -1}, "capture.wait_timeout_s", {"minimum", "exclusiveMinimum"}),
        ({"port_id": PORT_ID, "max_bytes": 0}, "capture.max_bytes", {"minimum"}),
    ],
    ids=["no-port", "empty-port", "unknown-key", "not-an-object", "negative-wait", "zero-max-bytes"],
)
def test_a_capture_outside_its_shape_is_refused_by_the_schema_before_anything_is_touched(tmp_path: Path, board: Board, capture: object, field: str, validators: set[str]) -> None:
    """Four keys and no others, refused where a caller can read which one was
    wrong: `field` and `validator` name the key inside `capture`, not the whole
    argument."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    try:
        result = service.call("flash_firmware", {**firmware(tmp_path), "reset_after_flash": True, "capture": capture})

        assert result["ok"] is False, result
        assert result["error_type"] == "invalid_argument", result
        assert result["field"] == field, result
        assert result["validator"] in validators, result
        assert backend.calls == [], backend.calls
        assert board.constructed == 0
        assert_bench_free(service)
    finally:
        service.close()


@pytest.mark.parametrize(
    "until",
    ["", [], [READY] * 9, [""], ["x" * 257], 7, [7]],
    ids=["empty-string", "empty-list", "nine-entries", "empty-entry", "entry-over-256", "number", "list-of-number"],
)
def test_an_until_outside_the_com_read_rules_is_refused_before_the_flash(tmp_path: Path, board: Board, until: object) -> None:
    """`until` takes exactly `com_read`'s rules: a non-empty string, or 1 to 8 of
    them, each at most 256 characters. Anything else is a malformed call, and a
    malformed call flashes nothing."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, until=until))

        assert result["ok"] is False, result
        assert result["error_type"] == "invalid_argument", result
        assert "until" in named(result), result
        assert backend.calls == [], backend.calls
        assert board.constructed == 0
        assert_bench_free(service)
    finally:
        service.close()


def test_an_until_entry_the_port_cannot_encode_is_refused_by_name_and_nothing_is_flashed(tmp_path: Path, board: Board) -> None:
    """Entries are matched as bytes in the port's encoding, so one the encoding
    cannot express could never match. Refused naming that entry, before the
    board is flashed for a wait that could only time out."""
    config = config_for(tmp_path, port_yaml='    encoding: "ascii"\n')
    service, backend = service_for(config, board)
    entry = "READY\u00e9"
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, until=[READY, entry]))

        assert result["ok"] is False, result
        assert result["error_type"] == "invalid_argument", result
        assert entry in json.dumps(result, ensure_ascii=False), result
        assert backend.calls == [], backend.calls
        assert board.constructed == 0
        assert_bench_free(service)
    finally:
        service.close()


def test_a_wait_longer_than_the_cap_is_capped_rather_than_refused(tmp_path: Path, board: Board) -> None:
    """`com_read` caps its wait at 60 s instead of refusing a longer one, and the
    capture takes the same rule: two minutes asked for is the cap, not an error
    to reword. The report records the wait in force, so the cap is read there
    without waiting it out."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, until=READY, wait_timeout_s=120))

        assert result["ok"] is True, result
        assert capture_of(result)["until_matched"] is True, result
        assert backend.calls == ["flash_firmware"], backend.calls
        assert service.call("get_last_report")["report"]["capture"]["until_wait_s"] == 60.0
        assert_bench_free(service)
    finally:
        service.close()


@pytest.mark.parametrize("reset", [None, False], ids=["reset-absent", "reset-false"])
def test_a_capture_without_the_reset_is_refused_naming_both_arguments(tmp_path: Path, board: Board, reset: bool | None) -> None:
    """Without the reset there is no boot to capture: the port would open onto a
    board still running whatever it ran before. Refused before anything is
    touched, naming both arguments, so the fix is in the refusal."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    arguments = {**firmware(tmp_path), "capture": {"port_id": PORT_ID}}
    if reset is not None:
        arguments["reset_after_flash"] = reset
    try:
        result = service.call("flash_firmware", arguments)

        assert result["ok"] is False, result
        assert result["error_type"] == "invalid_argument", result
        assert "capture" in named(result), result
        assert "reset_after_flash" in named(result), result
        assert backend.calls == [], backend.calls
        assert board.constructed == 0
        assert_bench_free(service)
    finally:
        service.close()


# ---------------------------------------------------------------------------
# B. Everything that can refuse the capture refuses before the flash.


def test_a_port_that_is_not_configured_is_refused_as_the_session_start_refuses_it(tmp_path: Path, board: Board) -> None:
    """The same answer `com_session_start` gives for the same port id, and a
    board left exactly as it was: nothing flashed, nothing opened, and a result
    that says the effect never started."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    try:
        reference = service.call("com_session_start", {"port_id": "no_such_port"})
        assert reference["ok"] is False, reference

        result = service.call("flash_firmware", {**flash_args(tmp_path), "capture": {"port_id": "no_such_port"}})

        assert result["ok"] is False, result
        assert result["error_type"] == reference["error_type"], (result, reference)
        assert result["side_effect_status"] == "not_started", result
        assert backend.calls == [], backend.calls
        assert board.opens == 0
        assert_bench_free(service)
    finally:
        service.close()


def test_a_port_whose_read_permission_is_off_is_refused_as_com_read_refuses_it(tmp_path: Path, board: Board) -> None:
    """A capture is a read of the port, so it answers to the port's read
    permission, with the refusal `com_read` gives and the key it names, before
    the port is opened or the board flashed."""
    config = config_for(tmp_path, permissions={**DEFAULT_TEST_PERMISSIONS, "allow_com_read": False})
    service, backend = service_for(config, board)
    try:
        reference = service.call("com_read", {"port_id": PORT_ID})
        assert reference["ok"] is False, reference

        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is False, result
        assert result["error_type"] == reference["error_type"] == "permission_denied", (result, reference)
        assert result["permission"] == reference["permission"] == permission_key("com_ports", PORT_ID, "allow_read"), result
        assert result["side_effect_status"] == "not_started", result
        assert backend.calls == [], backend.calls
        assert board.opens == 0
        assert_bench_free(service)
    finally:
        service.close()


def test_a_port_held_by_another_owner_is_refused_before_the_flash_naming_the_holder(tmp_path: Path, board: Board) -> None:
    """Another owner on this machine holds the port. The session start would be
    refused naming them, and so is the capture, before the board is flashed:
    finding the port taken after a flash would leave a new image running with
    its boot output lost."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    port = com_resource(config, PORT_ID)
    stranger = BenchMutex(frontend="cli", label="alice-run")
    stranger.acquire([port])
    try:
        reference = service.call("com_session_start", {"port_id": PORT_ID})
        assert reference["ok"] is False, reference
        assert reference.get("resource") == port, reference

        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is False, result
        assert result["error_type"] == reference["error_type"], (result, reference)
        assert result.get("resource") == port, result
        assert result["holder"]["label"] == "alice-run", result
        assert result["side_effect_status"] == "not_started", result
        assert backend.calls == [], backend.calls
        assert board.opens == 0
        assert_bench_free(service)
    finally:
        service.close()
        stranger.release_all()


def test_a_port_another_process_has_open_is_refused_before_the_flash(tmp_path: Path, board: Board) -> None:
    """A terminal program holding the device is the everyday form of the same
    thing. The open fails exactly as the session start's does, and the board is
    still not flashed."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    board.busy = True
    try:
        reference = service.call("com_session_start", {"port_id": PORT_ID})
        assert reference["ok"] is False, reference

        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is False, result
        assert result["error_type"] == reference["error_type"], (result, reference)
        assert result["side_effect_status"] == "not_started", result
        assert backend.calls == [], backend.calls
        assert_bench_free(service)
    finally:
        service.close()


def test_a_port_with_a_session_already_open_is_refused_and_that_session_is_left_alone(tmp_path: Path, board: Board) -> None:
    """The capture owns its session from open to stop. A session the caller
    opened holds bytes the caller has not read yet, so the capture neither
    clears it nor reads it: it refuses, and says how to get the port back."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    pending = b"caller's unread line\r\n"
    try:
        assert service.call("com_session_start", {"port_id": PORT_ID})["ok"] is True
        board.transmit(pending)
        clears = board.events.count("clear")

        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is False, result
        assert result["error_type"] == "invalid_argument", result
        assert "com_read" in result["summary"], result
        assert "stop" in result["summary"], result
        assert backend.calls == [], backend.calls
        assert board.events.count("clear") == clears, board.events
        read = service.call("com_read", {"port_id": PORT_ID})
        assert read["ok"] is True, read
        assert read["data"] == data_result(pending, "utf-8"), read
        assert service.call("com_session_stop", {"port_id": PORT_ID})["ok"] is True
        assert_bench_free(service)
    finally:
        service.close()


@pytest.mark.parametrize("key", ["allow_flash", "allow_reset"])
def test_the_flash_and_reset_permissions_still_gate_a_flash_with_capture(tmp_path: Path, board: Board, key: str) -> None:
    """The capture adds a read and takes nothing away: a flash, or the reset the
    capture needs, that the configuration disables is refused as it is today,
    before the port is so much as constructed."""
    config = config_for(tmp_path, permissions={**DEFAULT_TEST_PERMISSIONS, key: False})
    service, backend = service_for(config, board)
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is False, result
        assert result["error_type"] == "permission_denied", result
        assert result["permission"] == service.debugger_permission_key(key), result
        assert backend.calls == [], backend.calls
        assert board.constructed == 0
        assert_bench_free(service)
    finally:
        service.close()


def test_the_capture_needs_the_port_read_permission_and_no_other_grant(tmp_path: Path, board: Board) -> None:
    """No new permission key: the default grants carry none, and a port that may
    be read but not written is enough."""
    config = config_for(tmp_path, permissions={**DEFAULT_TEST_PERMISSIONS, "allow_com_write": False})
    service, backend = service_for(config, board)
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is True, result
        assert capture_of(result)["until_matched"] is True, result
        assert_bench_free(service)
    finally:
        service.close()


# ---------------------------------------------------------------------------
# C. Open, flash, read, stop, with the bench held around all four.


def test_the_port_opens_and_clears_before_the_flash_and_closes_after_the_read(tmp_path: Path, board: Board) -> None:
    """Opened before the flash, so the first bytes after the reset land in a
    buffer rather than on a port nobody has open; cleared at the open, so what
    is captured is this boot; closed after the read. The bytes here arrive only
    after the board's reset, and they are the capture."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is True, result
        assert capture_of(result)["data"] == data_result(BANNER, "utf-8"), result
        events = list(board.events)
        for event in ("open", "clear", "flash", "reset", "rx", "close"):
            assert event in events, (event, events)
        last_rx = len(events) - 1 - events[::-1].index("rx")
        assert events.index("open") < events.index("clear") < events.index("flash") < events.index("reset") < events.index("rx"), events
        assert last_rx < events.index("close"), events
        assert_bench_free(service)
    finally:
        service.close()


def test_the_same_order_holds_through_the_openocd_backend(tmp_path: Path, board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """The configured OpenOCD backend, flashing through the suite's fake
    OpenOCD. The fake cannot drive a serial line, so the board's boot output is
    sent the moment the backend's flash returns, which is when a real reset
    would have released the core."""
    config = config_for(tmp_path)
    service = openocd_service(config, board)
    flash = service.backend.flash_firmware

    def flash_and_boot(artifact, reset_after_flash=False, *args, **kwargs):
        board.record("flash")
        answer = flash(artifact, reset_after_flash, *args, **kwargs)
        board.record("flashed")
        if reset_after_flash and answer.get("ok") is True:
            board.transmit(BANNER)
        return answer

    monkeypatch.setattr(service.backend, "flash_firmware", flash_and_boot)
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is True, result
        assert result["success_confirmed"] is True, result
        assert capture_of(result)["data"] == data_result(BANNER, "utf-8"), result
        events = list(board.events)
        for event in ("open", "clear", "flash", "flashed", "rx", "close"):
            assert event in events, (event, events)
        last_rx = len(events) - 1 - events[::-1].index("rx")
        assert events.index("open") < events.index("clear") < events.index("flash") < events.index("flashed") < events.index("rx"), events
        assert last_rx < events.index("close"), events
        assert_bench_free(service)
    finally:
        service.close()


def test_the_capture_starts_when_the_port_opens_so_bytes_sent_during_the_flash_are_in_it(tmp_path: Path, board: Board) -> None:
    """What was on the line before the call is not this boot and is cleared;
    what the board sends between the open and the reset is feedback the board
    really produced, and it is kept, ahead of the boot output."""
    config = config_for(tmp_path)
    during = b"old firmware: tick\r\n"
    service, backend = service_for(config, board, during_flash=during)
    try:
        with board.guard:
            board.line.extend(b"old firmware: stale\r\n")

        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is True, result
        assert capture_of(result)["data"] == data_result(during + BANNER, "utf-8"), result
        assert_bench_free(service)
    finally:
        service.close()


def test_the_wait_counts_from_the_end_of_the_flash_not_from_the_open(tmp_path: Path, board: Board) -> None:
    """A flash takes as long as it takes. A wait counted from the open would be
    spent by a slow flash before the board had printed a byte; counted from the
    end of the flash it is the time the firmware is given to boot."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board, flash_s=1.5, banner_delay_s=0.2)
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, until=READY, wait_timeout_s=1.2))

        assert result["ok"] is True, result
        capture = capture_of(result)
        assert capture["until_matched"] is True, capture
        assert capture["data"] == data_result(BANNER, "utf-8"), capture
    finally:
        service.close()


def test_without_a_wait_the_capture_waits_the_default_rather_than_not_at_all(tmp_path: Path, board: Board) -> None:
    """No `wait_timeout_s` is 10 s, not zero: a board that takes half a second
    to print after its reset is still captured by a caller that named nothing
    but the port and the line it wants."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board, banner_delay_s=0.5)
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is True, result
        assert capture_of(result)["until_matched"] is True, result
        assert service.call("get_last_report")["report"]["capture"]["until_wait_s"] == 10.0
    finally:
        service.close()


def test_without_until_the_capture_reads_until_max_bytes_and_the_report_keeps_the_default_wait(tmp_path: Path, board: Board) -> None:
    """With nothing to match, the capture answers when `max_bytes` are buffered
    or the wait is over, not at the first fragment of a banner. The wait in
    force is in the report for every capture, `until` or not; the answer does
    not repeat it."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    try:
        started = time.monotonic()
        result = service.call("flash_firmware", flash_args(tmp_path, max_bytes=len(BANNER)))
        elapsed = time.monotonic() - started

        assert result["ok"] is True, result
        capture = capture_of(result)
        assert capture["data"] == data_result(BANNER, "utf-8"), capture
        assert "until_wait_s" not in capture, capture
        assert elapsed < scaled_time_bound(4.0), elapsed
        report = service.call("get_last_report")["report"]
        assert report["capture"]["until_wait_s"] == 10.0, report["capture"]
        assert "until" not in report["capture"], report["capture"]
        assert_bench_free(service)
    finally:
        service.close()


def test_outside_a_run_the_call_holds_the_probe_and_the_port_from_before_the_open_until_after_the_stop(tmp_path: Path, board: Board) -> None:
    """The call's own single-action run declares the port beside the probe, and
    the board's record shows both held at the open, through the flash and at
    the close: no other owner can take the port between the reset and the read,
    and no `bench_run_start` is needed for that."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    port = com_resource(config, PORT_ID)
    try:
        plain = service.call("flash_firmware", firmware(tmp_path))
        assert plain["ok"] is True, plain
        probe = set(plain["run"]["declared_devices"])
        assert port not in probe, plain["run"]
        board.forget()

        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is True, result
        run = result["run"]
        assert run["implicit"] is True, run
        assert run["aborted"] is False, run
        assert set(run["declared_devices"]) == probe | {port}, run
        for event in ("open", "flash", "close"):
            assert event in board.held_at, (event, board.events)
            assert board.held_at[event] >= probe | {port}, (event, board.held_at[event])
        assert "recovery" not in result, result
        assert_bench_free(service)
    finally:
        service.close()


def test_inside_a_run_that_declared_the_port_the_capture_runs_in_it_and_leaves_the_run_open(tmp_path: Path, board: Board) -> None:
    """A declared run keeps its own boundary: the capture's session ends with
    the call, and the run, with the port it declared, stays the caller's until
    `bench_run_stop`."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    port = com_resource(config, PORT_ID)
    try:
        started = service.call("bench_run_start", {"devices": [{"kind": "debugger"}, {"kind": "uart", "id": PORT_ID}]})
        assert started["ok"] is True, started

        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is True, result
        assert capture_of(result)["until_matched"] is True, result
        assert service.com_ports.sessions == {}, sorted(service.com_ports.sessions)
        status = service.call("bench_run_status")
        assert status["run_active"] is True, status
        assert port in status["held_devices"], status
        assert service.call("bench_run_stop")["ok"] is True
        assert_bench_free(service)
    finally:
        end_run_if_open(service)
        service.close()


def test_inside_a_run_that_did_not_declare_the_port_the_capture_is_refused_as_the_session_start_is(tmp_path: Path, board: Board) -> None:
    """Inside a declared run the port must be one of the run's devices, exactly
    as `com_session_start` requires, and the refusal comes before the flash."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    try:
        assert service.call("bench_run_start", {"devices": [{"kind": "debugger"}]})["ok"] is True
        reference = service.call("com_session_start", {"port_id": PORT_ID})
        assert reference["ok"] is False, reference

        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is False, result
        assert result["error_type"] == reference["error_type"], (result, reference)
        assert result["side_effect_status"] == "not_started", result
        assert backend.calls == [], backend.calls
        assert board.constructed == 0
        assert service.call("bench_run_status")["run_active"] is True
        assert service.call("bench_run_stop")["ok"] is True
        assert_bench_free(service)
    finally:
        end_run_if_open(service)
        service.close()


# ---------------------------------------------------------------------------
# D. What the capture returns.


def test_a_match_returns_the_boot_output_up_to_the_match_and_the_log_that_holds_it(tmp_path: Path, board: Board) -> None:
    """The flash result as it is, and beside it the capture: the port, the bytes
    in `com_read`'s shape, the entry that matched, and the session log."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is True, result
        assert result["side_effect_status"] == "committed", result
        capture = capture_of(result)
        assert capture["port_id"] == PORT_ID, capture
        assert capture["bytes_read"] == len(BANNER), capture
        assert capture["data"] == data_result(BANNER, "utf-8"), capture
        assert capture["until_matched"] is True, capture
        assert capture["matched"] == READY, capture
        assert set(capture) == {"port_id", "bytes_read", "data", "until_matched", "matched", "overflow_bytes", "log_path"}, capture
        assert capture["overflow_bytes"] == 0, capture
        assert received(config, capture["log_path"]) == BANNER, capture
        assert backend.calls == ["flash_firmware"], backend.calls
        assert_bench_free(service)
    finally:
        service.close()


def test_the_earliest_ending_entry_wins_and_what_was_left_unread_is_flagged_and_logged(tmp_path: Path, board: Board) -> None:
    """The capture stops at the first line the caller named, and the lines after
    it are not lost: `truncated` says bytes were left at the stop, and the
    session log has every one of them."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    through_clock = BANNER[: BANNER.index(CLOCK.encode("utf-8")) + len(CLOCK)]
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, until=[READY, CLOCK]))

        assert result["ok"] is True, result
        capture = capture_of(result)
        assert capture["until_matched"] is True, capture
        assert capture["matched"] == CLOCK, capture
        assert capture["bytes_read"] == len(through_clock), capture
        assert capture["data"] == data_result(through_clock, "utf-8"), capture
        assert capture["truncated"] is True, capture
        assert received(config, capture["log_path"]) == BANNER, capture
    finally:
        service.close()


def test_max_bytes_bounds_the_capture_and_the_rest_is_flagged_as_left_unread(tmp_path: Path, board: Board) -> None:
    """Without `until` the read is `com_read`'s, bounded by `max_bytes`. What it
    did not take is flagged rather than silently dropped, and there is no match
    to report because none was asked for."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, max_bytes=16, wait_timeout_s=0.3))

        assert result["ok"] is True, result
        capture = capture_of(result)
        assert capture["bytes_read"] == 16, capture
        assert capture["data"] == data_result(BANNER[:16], "utf-8"), capture
        assert capture["truncated"] is True, capture
        assert "until_matched" not in capture, capture
        assert "matched" not in capture, capture
    finally:
        service.close()


def test_max_bytes_ending_before_the_match_is_no_match_and_flags_the_rest(tmp_path: Path, board: Board) -> None:
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, until=READY, max_bytes=16, wait_timeout_s=0.5))

        assert result["ok"] is True, result
        capture = capture_of(result)
        assert capture["bytes_read"] == 16, capture
        assert capture["data"] == data_result(BANNER[:16], "utf-8"), capture
        assert capture["until_matched"] is False, capture
        assert capture["truncated"] is True, capture
    finally:
        service.close()


def test_bytes_the_buffer_had_to_drop_are_counted_and_still_in_the_log(tmp_path: Path, board: Board) -> None:
    """A board that prints more than the port buffers loses the oldest bytes
    from the buffer, never from the log: the capture counts them, and the log
    still holds them."""
    config = config_for(tmp_path, port_yaml="    max_buffer_bytes: 64\n")
    payload = b"".join(f"tick {index:03d}\r\n".encode("ascii") for index in range(10))
    assert len(payload) == 100
    service, backend = service_for(config, board, banner=b"", during_flash=payload)
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, wait_timeout_s=0.3))

        assert result["ok"] is True, result
        capture = capture_of(result)
        assert capture["overflow_bytes"] == 36, capture
        assert capture["bytes_read"] == 64, capture
        assert capture["data"] == data_result(payload[36:], "utf-8"), capture
        # Dropped is not left unread: every byte still buffered was returned.
        assert "truncated" not in capture, capture
        assert received(config, capture["log_path"]) == payload, capture
    finally:
        service.close()


def test_an_until_not_seen_by_the_deadline_is_not_a_failure(tmp_path: Path, board: Board) -> None:
    """The flash worked and the board printed; it did not print the line asked
    for in time. That is an answer about the firmware, not a failed call: `ok`
    stays true, the bytes are there, and `until_matched` says the rest."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    try:
        started = time.monotonic()
        result = service.call("flash_firmware", flash_args(tmp_path, until="NEVER-SEEN", wait_timeout_s=0.5))
        elapsed = time.monotonic() - started

        assert result["ok"] is True, result
        assert overall_success(result) is True, result
        capture = capture_of(result)
        assert capture["until_matched"] is False, capture
        assert "matched" not in capture, capture
        assert capture["data"] == data_result(BANNER, "utf-8"), capture
        assert elapsed >= 0.5
        assert_bench_free(service)
    finally:
        service.close()


# ---------------------------------------------------------------------------
# E. Each way to fail keeps what it knows.


def test_a_flash_that_fails_stops_the_session_keeps_what_the_board_sent_and_recovers(tmp_path: Path, board: Board) -> None:
    """The result is the flash failure it is today, and the implicit run aborts
    into the same recovery. Nothing is read after the failure: the session is
    stopped at once rather than waiting out a deadline for a boot that did not
    happen. What the board had sent by then is kept, because the board really
    sent it."""
    config = config_for(tmp_path)
    ticks = b"old firmware: tick\r\n"
    service, backend = service_for(config, board, during_flash=ticks, unconfirmed=True, late=b"LATE\r\n")
    try:
        started = time.monotonic()
        result = service.call("flash_firmware", flash_args(tmp_path, until="NEVER-SEEN", wait_timeout_s=12))
        elapsed = time.monotonic() - started

        assert result["ok"] is False, result
        assert result["error_type"] == "flash_failed", result
        assert result["side_effect_status"] == "unknown", result
        capture = capture_of(result)
        assert capture["data"] == data_result(ticks, "utf-8"), capture
        assert "close" in board.events, board.events
        assert service.com_ports.sessions == {}, sorted(service.com_ports.sessions)
        assert elapsed < scaled_time_bound(4.0), elapsed
        assert result["run"]["aborted"] is True, result
        recovery = result["recovery"]
        assert recovery["attempted"] is True, recovery
        assert recovery["outcome"] == "recovered", recovery
        assert recovery["actions"] == ["reap_processes", "reset_halt", "probe_target"], recovery
        assert recovery["incident_resolved"] is True, recovery
        assert recovery["resolved_reason"] == "debugger_result_unconfirmed", recovery
        assert "reset_target:halt" in backend.calls, backend.calls
        assert service.coordinator.status()["blocked"] is False
        assert_bench_free(service)
    finally:
        service.close()


def test_a_read_that_fails_after_a_good_flash_keeps_every_field_of_the_flash(tmp_path: Path, board: Board) -> None:
    """The board was flashed and reset, and then the port died. `ok` is false
    because the capture failed, and the result still says the flash committed,
    with every field the same flash has without a capture, so nobody reads it as
    "nothing was flashed" and flashes again to be sure."""
    config = config_for(tmp_path)
    partial = b"\r\nboot: demo firmware 1.0\r\n"
    service, backend = service_for(config, board)
    try:
        plain = service.call("flash_firmware", {**firmware(tmp_path), "reset_after_flash": True})
        assert plain["ok"] is True, plain
        with board.guard:
            board.line.clear()
        backend.banner = partial
        backend.die_after_banner = True

        started = time.monotonic()
        result = service.call("flash_firmware", flash_args(tmp_path, until=READY, wait_timeout_s=12))
        elapsed = time.monotonic() - started

        assert result["ok"] is False, result
        assert result["error_type"] == "serial_read_failed", result
        assert sorted(set(plain) - set(result)) == [], sorted(set(plain) - set(result))
        for key in ("side_effect_status", "side_effect_committed", "success_confirmed", "reset_after_flash"):
            assert result[key] == plain[key], (key, result, plain)
        assert result["side_effect_status"] == "committed", result
        capture = capture_of(result)
        assert capture["data"] == data_result(partial, "utf-8"), capture
        assert capture["reader_error"]["error_type"] == "serial_read_failed", capture
        assert "device disconnected" in capture["reader_error"]["backend_error"], capture
        assert "device disconnected" in result["summary"], result
        assert elapsed < scaled_time_bound(4.0), elapsed
        assert_bench_free(service)
    finally:
        service.close()


def test_a_reader_that_dies_before_any_byte_still_keeps_the_flash_and_the_reason(tmp_path: Path, board: Board) -> None:
    """The reader's own error, not `session_not_active`: that answer tells a
    caller to start a session, and this session was the call's own."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board, banner=b"", die_after_banner=True)
    try:
        started = time.monotonic()
        result = service.call("flash_firmware", flash_args(tmp_path, until=READY, wait_timeout_s=12))
        elapsed = time.monotonic() - started

        assert result["ok"] is False, result
        assert result["error_type"] == "serial_read_failed", result
        assert result["side_effect_status"] == "committed", result
        assert result["success_confirmed"] is True, result
        capture = capture_of(result)
        assert capture["bytes_read"] == 0, capture
        assert capture["reader_error"]["error_type"] == "serial_read_failed", capture
        assert "device disconnected" in capture["reader_error"]["backend_error"], capture
        assert "device disconnected" in result["summary"], result
        assert elapsed < scaled_time_bound(4.0), elapsed
        assert_bench_free(service)
    finally:
        service.close()


def test_through_openocd_a_read_that_fails_after_the_flash_keeps_the_flash_and_its_log(tmp_path: Path, board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same promise on the configured backend: the verify, the artifact and
    the OpenOCD log of the flash stand in the failed result exactly as a plain
    flash reports them, and the capture's session log is a second log beside
    it, not in its place."""
    config = config_for(tmp_path)
    partial = b"\r\nboot: demo firmware 1.0\r\n"
    service = openocd_service(config, board)
    try:
        plain = service.call("flash_firmware", {**firmware(tmp_path), "reset_after_flash": True})
        assert plain["ok"] is True, plain
        assert plain["backend"] == "openocd", plain
        flash = service.backend.flash_firmware

        def flash_and_die(artifact, reset_after_flash=False, *args, **kwargs):
            answer = flash(artifact, reset_after_flash, *args, **kwargs)
            if reset_after_flash and answer.get("ok") is True:
                board.transmit(partial)
                board.fail_reads = True
            return answer

        monkeypatch.setattr(service.backend, "flash_firmware", flash_and_die)
        result = service.call("flash_firmware", flash_args(tmp_path, until=READY, wait_timeout_s=12))

        assert result["ok"] is False, result
        assert result["error_type"] == "serial_read_failed", result
        for key in ("backend", "verify", "success_confirmed", "side_effect_status", "reset_after_flash"):
            assert result.get(key) == plain[key], (key, result, plain)
        assert result["artifact"]["sha256"] == plain["artifact"]["sha256"], result
        assert "flash_firmware" in result["log_path"], result
        capture = capture_of(result)
        assert capture["log_path"] != result["log_path"], capture
        assert capture["data"] == data_result(partial, "utf-8"), capture
        assert_bench_free(service)
    finally:
        service.close()


def test_a_session_stop_that_fails_is_reported_as_com_session_stop_reports_it(tmp_path: Path, board: Board) -> None:
    """A port that will not close is a port still held. The call says so the way
    `com_session_stop` does, with its reasons and the decisive error line, and
    without unsaying the flash or the capture that did happen."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    port = com_resource(config, PORT_ID)
    try:
        assert service.call("com_session_start", {"port_id": PORT_ID})["ok"] is True
        board.close_failures = 1
        reference = service.call("com_session_stop", {"port_id": PORT_ID})
        assert reference["ok"] is False, reference
        assert service.call("com_session_stop", {"port_id": PORT_ID})["ok"] is True
        board.close_failures = 1

        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is False, result
        assert overall_success(result) is False, result
        assert result["error_type"] == reference["error_type"], (result, reference)
        # What the stop reports, merged as it reports it: the probe settled
        # cleanly, so the port's answer is the whole of it.
        assert result["cleanup_required"] == reference["cleanup_required"], (result, reference)
        assert result["quarantined"] == reference["quarantined"], (result, reference)
        assert set(reference["cleanup_reasons"]) <= set(result["cleanup_reasons"]), (result, reference)
        assert "port busy during close" in result["summary"], result
        assert result["side_effect_status"] == "committed", result
        assert result["success_confirmed"] is True, result
        assert capture_of(result)["data"] == data_result(BANNER, "utf-8"), result
        # Where the call leaves the bench: the run is over and the probe given
        # back, and the session stays registered, its lease holding the port,
        # until a stop confirms, exactly as after a failed `com_session_stop`.
        assert PORT_ID in service.com_ports.sessions, sorted(service.com_ports.sessions)
        status = service.call("bench_run_status")
        assert status["run_active"] is False, status
        assert status["held_devices"] == [port], status
        assert service.coordinator.status()["blocked"] is False
        assert service.call("com_session_stop", {"port_id": PORT_ID})["ok"] is True
        assert_bench_free(service)
    finally:
        service.close()


@pytest.mark.parametrize("first", ["flash", "read"])
def test_a_stop_that_fails_after_another_failure_keeps_the_first_error_and_the_stops_line(tmp_path: Path, board: Board, first: str) -> None:
    """Two failures in one call: the first one's error stands, and the stop's
    reasons and its error line are merged beside it, never dropped."""
    config = config_for(tmp_path)
    options: dict[str, object] = {"unconfirmed": True} if first == "flash" else {"banner": b"", "die_after_banner": True}
    service, backend = service_for(config, board, **options)
    board.close_failures = 1
    try:
        result = service.call("flash_firmware", flash_args(tmp_path, until=READY, wait_timeout_s=12))

        assert result["ok"] is False, result
        assert result["error_type"] == ("flash_failed" if first == "flash" else "serial_read_failed"), result
        assert "port busy during close" in result["cleanup_error"], result
        assert "com_cleanup_unconfirmed" in result["cleanup_reasons"], result
    finally:
        service.close()


# ---------------------------------------------------------------------------
# F. Reports, audit, and the flash without a capture.


def test_the_report_carries_the_capture_and_the_session_is_audited_as_a_started_one_is(tmp_path: Path, board: Board) -> None:
    """One call, one report, and it is the flash's with the capture in it. The
    session underneath leaves the same audit trail a `com_session_start` and
    `com_session_stop` pair leaves, in the workspace log and in the trusted
    ledger both."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    try:
        started = service.call("com_session_start", {"port_id": PORT_ID})
        assert started["ok"] is True, started
        assert service.call("com_session_stop", {"port_id": PORT_ID})["ok"] is True
        pair_log = started["session"]["log_path"]

        result = service.call("flash_firmware", flash_args(tmp_path, until=READY))

        assert result["ok"] is True, result
        capture = capture_of(result)
        report = service.call("get_last_report")["report"]
        assert report["tool"] == "flash_firmware", report
        # The report also keeps what the call waited for and for how long; the
        # answer does not repeat either, as `com_read`'s does not.
        assert report["capture"] == {**capture, "until": [READY], "until_wait_s": 10.0}, report
        assert capture["log_path"] != pair_log, capture
        pair_entries = session_log(config, pair_log)
        capture_entries = session_log(config, capture["log_path"])
        assert without_time(capture_entries[0]) == without_time(pair_entries[0]), (capture_entries[0], pair_entries[0])
        assert capture_entries[-1]["event"] == "stop", capture_entries[-1]
        assert set(capture_entries[-1]) == set(pair_entries[-1]), (capture_entries[-1], pair_entries[-1])
        assert ledger_events(config, pair_log), pair_log
        assert ledger_events(config, capture["log_path"]) == ledger_events(config, pair_log)
    finally:
        service.close()


@pytest.mark.parametrize("reset", [False, True], ids=["no-reset", "reset"])
def test_without_capture_a_flash_is_exactly_the_flash_it_was(tmp_path: Path, board: Board, reset: bool) -> None:
    """A configured port is not a reason to touch it: without `capture` nothing
    is opened, nothing about the port is declared, and the result has no key it
    did not have before."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    backend_keys = {"ok", "tool", "reset_after_flash", "success_confirmed", "side_effect_committed", "side_effect_status", "retry_safe"}
    try:
        result = service.call("flash_firmware", {**firmware(tmp_path), "reset_after_flash": reset})

        assert result["ok"] is True, result
        assert "capture" not in result, result
        assert set(result) <= backend_keys | TODAY_FLASH_ENVELOPE, sorted(set(result) - backend_keys - TODAY_FLASH_ENVELOPE)
        assert com_resource(config, PORT_ID) not in result["run"]["declared_devices"], result["run"]
        assert board.constructed == 0
        assert backend.calls == ["flash_firmware"], backend.calls
        assert_bench_free(service)
    finally:
        service.close()


# ---------------------------------------------------------------------------
# G. What a client is told.


def test_the_capture_argument_is_the_com_read_shape_and_nothing_more() -> None:
    """`until` and `max_bytes` are `com_read`'s own, so a caller who knows one
    knows the other, and the object refuses any key it does not name."""
    properties = flash_tool()["inputSchema"]["properties"]
    capture = properties.get("capture")
    assert isinstance(capture, dict), sorted(properties)
    assert capture["type"] == "object", capture
    assert set(capture["properties"]) == {"port_id", "until", "wait_timeout_s", "max_bytes"}, capture
    assert capture["required"] == ["port_id"], capture
    assert capture["additionalProperties"] is False, capture
    com_read = next(tool for tool in MCP_TOOLS if tool["name"] == "com_read")["inputSchema"]["properties"]
    assert "until" in com_read, sorted(com_read)
    for key in ("until", "max_bytes"):
        assert without_descriptions(capture["properties"][key]) == without_descriptions(com_read[key]), key


def test_the_description_says_one_call_does_it_without_a_declared_run() -> None:
    """At most two sentences more than the three it had, and they carry the two
    facts a caller chooses by: flash, reset and the UART output come back from
    one call, and it needs no `bench_run_start`. Where the capture begins, at
    the open, is said on the tool or on the argument."""
    tool = flash_tool()
    description = tool["description"]
    for word in ("capture", "reset_after_flash", "bench_run_start"):
        assert word in description, description
    sentences = [sentence for sentence in re.split(r"(?<=[.!?])\s+", description.strip()) if sentence]
    assert len(sentences) <= 5, sentences
    capture_description = tool["inputSchema"]["properties"].get("capture", {}).get("description", "")
    assert re.search(r"\bopen(?:ed|s)?\b", f"{description} {capture_description}", re.IGNORECASE), (description, capture_description)


def test_one_tools_call_flashes_resets_and_returns_the_boot_output(tmp_path: Path, board: Board) -> None:
    """The whole loop through the MCP surface a client uses: the argument is
    listed, one `tools/call` returns the boot output, and a malformed capture is
    an error result naming the key."""
    config = config_for(tmp_path)
    service, backend = service_for(config, board)
    try:
        listed = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, service)
        flash = next(tool for tool in listed["result"]["tools"] if tool["name"] == "flash_firmware")
        assert "capture" in flash["inputSchema"]["properties"], flash["inputSchema"]

        response = handle_mcp_message(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "flash_firmware", "arguments": flash_args(tmp_path, until=READY)}},
            service,
        )
        answer = response["result"]
        assert answer["isError"] is False, answer
        structured = answer["structuredContent"]
        assert structured["ok"] is True, structured
        assert set(structured["capture"]) == {"port_id", "bytes_read", "data", "until_matched", "matched", "overflow_bytes", "log_path"}, structured
        assert structured["capture"]["until_matched"] is True, structured
        assert structured["capture"]["data"] == data_result(BANNER, "utf-8"), structured
        assert backend.calls == ["flash_firmware"], backend.calls

        refused = handle_mcp_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "flash_firmware", "arguments": {**flash_args(tmp_path), "capture": {"port_id": PORT_ID, "baudrate": 115200}}},
            },
            service,
        )["result"]
        assert refused["isError"] is True, refused
        assert refused["structuredContent"]["error_type"] == "invalid_argument", refused
        assert refused["structuredContent"]["field"] == "capture.baudrate", refused
        assert backend.calls == ["flash_firmware"], backend.calls
        assert_bench_free(service)
    finally:
        service.close()
