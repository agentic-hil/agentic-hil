"""`flash_firmware` with `capture.defmt`: the decoded boot output of the image it flashed.

A capture with `defmt: true` decodes the boot output with the table of the
image the same call flashes. The table is read from the validated image before
anything is staged, before the port is opened and before the flash, so an
image that carries no table flashes nothing and opens nothing. Each way the
call can fail after the flash keeps what it knows: the programming that
happened, the frames the board had sent, and the session's cleanup.

The board is faked at the hardware boundary with the `Board` and
`BoardBackend` of `tests/test_flash_capture.py`: the probe flashes and resets,
and the reset puts the boot output on the board's UART. The image is built from
`tests/fixtures/defmt/firmware/` and copied into each test's own workspace; the
boot frames are encoded for its table by
`tests/defmt_wire.py`, invented for these tests and not recorded from any
board. What they decode to is asked of `agentic_hil.defmt.decode_stream` on
the same bytes.

Every check raises `AssertionError` with its own message, so the first one a
missing behaviour reaches names that behaviour.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config
from defmt_wire import build_elf, encode_frame, rzcobs_stream
from test_flash_capture import Board, BoardBackend

from agentic_hil import defmt
from agentic_hil.comports import data_result
from agentic_hil.config import load_config
from agentic_hil.defmt import DefmtTableError
from agentic_hil.tools import AgenticHILToolService

# Device locks are machine-wide, so this file's port id and device are its own.
PORT_ID = "defmt_capture_uart"
DEVICE = "/dev/ttyDEFMTCAPTURE0"

FIXTURES = Path(__file__).parent / "fixtures" / "defmt"
IMAGE = "build/app.elf"

# Images the decoder reads no table from, each confirmed against the decoder
# before it is used. The second is the four-byte magic the rest of the suite
# flashes, which the artifact policy accepts as an ELF.
UNREADABLE_TABLES = {
    "table_missing": lambda: build_elf([("main", 0x08000400, 4, ".text")], {".text": bytes(8)}),
    "elf_unreadable": lambda: b"\x7fELF" + b"\x00" * 12,
}


def check(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch):
    fake = Board()
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=fake.construct))
    yield fake
    fake.finish()


def config_for(workspace: Path):
    written = write_config(workspace, com_ports_yaml=f'com_ports:\n  {PORT_ID}:\n    device: "{DEVICE}"\n')
    return load_config(str(written))


def service_for(config, board: Board, **options) -> tuple[AgenticHILToolService, BoardBackend]:
    backend = BoardBackend(board, **options)
    service = AgenticHILToolService(config, backend=backend)
    board.watch = service.coordinator.bench.held_resources
    return service, backend


def place(workspace: Path, data: bytes, relative: str = IMAGE) -> None:
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def refusal_reason(data: bytes) -> str:
    try:
        defmt.load_table_bytes(data)
    except DefmtTableError as error:
        return error.reason
    raise AssertionError("the planted image carries a table the decoder reads")


def boot_frames(table: defmt.DefmtTable) -> list[bytes]:
    return [
        encode_frame(table, "Hello, world!", [], timestamp=[1_000]),
        encode_frame(table, "escaped {{braces}} {=u8}", [42], timestamp=[2_000]),
        encode_frame(table, "println {=u8}", [99], timestamp=[3_000], tags=("defmt_println",)),
    ]


def capture_args(**capture: object) -> dict:
    return {"image_path": IMAGE, "reset_after_flash": True, "capture": {"port_id": PORT_ID, **capture}}


def logged_rx(config, log_path: str) -> bytes:
    """Every byte the session's log says the port received."""
    path = Path(config.work_dir) / log_path
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return b"".join(bytes.fromhex(entry["hex"]) for entry in entries if entry.get("direction") == "rx")


def logged_events(config, log_path: str) -> list:
    path = Path(config.work_dir) / log_path
    return [json.loads(line).get("event") for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def check_bench_free(service: AgenticHILToolService, what: str) -> None:
    status = service.call("bench_run_status")
    check(status.get("run_active") is False and status.get("held_devices") == [], f"{what} left a run or a device held: {status}")
    check(service.com_ports.sessions == {}, f"{what} left a session registered: {sorted(service.com_ports.sessions)}")
    check(not service.coordinator.leases, f"{what} left a lease held: {sorted(service.coordinator.leases)}")


def test_a_capture_with_defmt_returns_the_decoded_boot_frames_of_the_flashed_image(tmp_path: Path, board: Board, defmt_images: dict[str, Path]) -> None:
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    image = defmt_images["rzcobs"].read_bytes()
    place(workspace, image)
    table = defmt.load_table_bytes(image)
    banner = rzcobs_stream(boot_frames(table))
    service, backend = service_for(config, board, banner=banner)
    try:
        result = service.call("flash_firmware", capture_args(defmt=True, wait_timeout_s=0.5))

        capture = result.get("capture")
        check(result.get("ok") is True and isinstance(capture, dict) and isinstance(capture.get("frames"), list), "a flash with capture.defmt did not flash and decode its boot output")
        check(capture["frames"] == defmt.decode_stream(table, banner).records, f"the capture's frames are not what the flashed table decodes: {capture}")
        check("data" not in capture, f"a decoding capture handed out text and hex as well: {capture}")
        check(isinstance(capture.get("defmt"), dict) and capture["defmt"].get("proven") is True, f"the capture of the image this call flashed is not proven: {capture}")
        check(backend.calls == ["flash_firmware"], f"the probe was driven other than by one flash: {backend.calls}")
        check(board.events.index("open") < board.events.index("flash"), f"the port did not open before the flash: {board.events}")
        check(logged_rx(config, capture["log_path"]) == banner, "the session log does not hold the raw boot bytes")
        check_bench_free(service, "a decoding capture")
    finally:
        service.close()


@pytest.mark.parametrize("reason", sorted(UNREADABLE_TABLES))
def test_an_invalid_table_flashes_nothing_and_opens_no_port(tmp_path: Path, board: Board, reason: str) -> None:
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    data = UNREADABLE_TABLES[reason]()
    check(refusal_reason(data) == reason, f"the planted image is refused by the decoder as {refusal_reason(data)}, not {reason}")
    place(workspace, data)
    service, backend = service_for(config, board)
    try:
        result = service.call("flash_firmware", capture_args(defmt=True))

        check(result.get("error_type") == "defmt_table_invalid", f"a capture.defmt flash with a {reason} image was not refused as defmt_table_invalid")
        check(result.get("table_error") == reason, f"the refusal does not name the decoder's reason {reason}: {result}")
        check(result.get("ok") is False and result.get("side_effect_committed") is False, f"the refusal does not say nothing was flashed: {result}")
        check(result.get("side_effect_status") == "not_started", f"the refusal does not say the flash never started: {result}")
        check(backend.calls == [], f"an image without a table reached the probe: {backend.calls}")
        check(board.constructed == 0 and board.opens == 0, f"an image without a table opened the port: constructed {board.constructed}, opened {board.opens}")
        check_bench_free(service, "a refused capture")
    finally:
        service.close()


def test_a_read_failure_after_a_good_flash_keeps_the_flash_and_its_cleanup(tmp_path: Path, board: Board, defmt_images: dict[str, Path]) -> None:
    """The board was flashed and reset, and then the port died. The failed call
    still says the flash committed and proves the image, keeps the frames the
    board had sent, and leaves the session stopped and logged."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    image = defmt_images["rzcobs"].read_bytes()
    place(workspace, image)
    table = defmt.load_table_bytes(image)
    partial = rzcobs_stream(boot_frames(table)[:2])
    service, backend = service_for(config, board, banner=b"")
    try:
        plain = service.call("flash_firmware", {"image_path": IMAGE})
        check(plain.get("ok") is True, f"the plain flash before the capture failed: {plain}")
        backend.banner = partial
        backend.die_after_banner = True

        result = service.call("flash_firmware", capture_args(defmt=True, wait_timeout_s=12))

        check(
            result.get("success_confirmed") is True and result.get("side_effect_status") == "committed" and isinstance(result.get("capture"), dict),
            "a defmt capture whose read failed did not report the flash it made",
        )
        check(result.get("ok") is False and result.get("error_type") == "serial_read_failed", f"the failed read is not the call's error: {result}")
        missing = sorted(set(plain) - set(result))
        check(missing == [], f"the failed capture dropped fields the flash reports: {missing}")
        capture = result["capture"]
        check(capture.get("frames") == defmt.decode_stream(table, partial).records, f"the frames the board sent before the port died were not kept: {capture}")
        check(isinstance(capture.get("reader_error"), dict) and capture["reader_error"].get("error_type") == "serial_read_failed", f"the reader's own error is not kept: {capture}")
        check(isinstance(capture.get("defmt"), dict) and capture["defmt"].get("proven") is True, f"the confirmed programming is not kept as the proof: {capture}")
        check(logged_rx(config, capture["log_path"]) == partial, "the session log does not hold the bytes the board sent")
        check(logged_events(config, capture["log_path"])[-1] == "stop", "the session log does not end with the session's stop")
        check_bench_free(service, "a capture whose read failed")
    finally:
        service.close()


def test_an_unconfirmed_flash_with_a_defmt_capture_keeps_its_incident(tmp_path: Path, board: Board, defmt_images: dict[str, Path]) -> None:
    """A flash the backend cannot confirm is the incident it is without a
    capture: the run aborts into recovery, the session is stopped, the frames
    the board had sent are kept, and nothing claims the image is proven."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    image = defmt_images["rzcobs"].read_bytes()
    place(workspace, image)
    table = defmt.load_table_bytes(image)
    during = rzcobs_stream(boot_frames(table)[:1])
    service, backend = service_for(config, board, during_flash=during, unconfirmed=True)
    try:
        result = service.call("flash_firmware", capture_args(defmt=True, wait_timeout_s=12))

        check(result.get("error_type") == "flash_failed" and result.get("side_effect_status") == "unknown", "an unconfirmed flash with a defmt capture did not keep its incident")
        check(result.get("ok") is False, f"an unconfirmed flash answered ok: {result}")
        run = result.get("run") or {}
        check(run.get("aborted") is True, f"the implicit run did not abort: {result}")
        recovery = result.get("recovery") or {}
        check(recovery.get("attempted") is True and recovery.get("resolved_reason") == "debugger_result_unconfirmed", f"the incident did not go into its recovery: {recovery}")
        capture = result.get("capture")
        check(isinstance(capture, dict), f"the capture of an unconfirmed flash was dropped: {result}")
        check(capture.get("frames") == defmt.decode_stream(table, during).records, f"the frames the board sent during the flash were not kept: {capture}")
        check(isinstance(capture.get("defmt"), dict) and capture["defmt"].get("proven") is False, f"an unconfirmed flash proves its image: {capture}")
        check("close" in board.events, f"the session's port was not closed: {board.events}")
        check(service.com_ports.sessions == {}, f"the session was left registered: {sorted(service.com_ports.sessions)}")
    finally:
        service.close()


def test_a_capture_with_defmt_and_until_ends_at_the_matching_frame(tmp_path: Path, board: Board, defmt_images: dict[str, Path]) -> None:
    """`until` matches a decoded frame's text, and the capture ends with that
    frame. The log still holds every byte the board sent."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    image = defmt_images["rzcobs"].read_bytes()
    place(workspace, image)
    table = defmt.load_table_bytes(image)
    banner = rzcobs_stream(boot_frames(table))
    expected = defmt.decode_stream(table, banner).records
    until = expected[1]["message"]
    check(sum(until in record["text"] for record in expected) == 1, "the until text names more than one planted frame")
    service, backend = service_for(config, board, banner=banner)
    try:
        result = service.call("flash_firmware", capture_args(defmt=True, until=until, wait_timeout_s=12))

        capture = result.get("capture")
        check(result.get("ok") is True and isinstance(capture, dict) and capture.get("until_matched") is True, "a capture.defmt flash with until did not end at the matching frame")
        check(capture.get("frames") == expected[:2], f"the capture did not end at the matching frame: {capture}")
        check(logged_rx(config, capture["log_path"]) == banner, "the session log does not hold the raw boot bytes")
        check_bench_free(service, "a decoding capture with until")
    finally:
        service.close()


def test_a_malformed_capture_defmt_is_refused_by_the_schema_before_anything_is_touched(tmp_path: Path, board: Board, defmt_images: dict[str, Path]) -> None:
    """`capture.defmt` is a boolean, and the published schema says so before the
    image is read, the port opened or the probe driven."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, defmt_images["rzcobs"].read_bytes())
    service, backend = service_for(config, board)
    try:
        result = service.call("flash_firmware", capture_args(defmt="x"))

        check(result.get("error_type") == "invalid_argument", f"capture.defmt as a string was not refused as invalid_argument: {result}")
        check(result.get("field") == "capture.defmt", f"the refusal does not name capture.defmt: {result}")
        check(backend.calls == [], f"a malformed capture.defmt reached the probe: {backend.calls}")
        check(board.constructed == 0 and board.opens == 0, f"a malformed capture.defmt opened the port: constructed {board.constructed}, opened {board.opens}")
        check_bench_free(service, "a malformed capture.defmt")
    finally:
        service.close()


def test_a_capture_without_defmt_is_unchanged(tmp_path: Path, board: Board, defmt_images: dict[str, Path]) -> None:
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, defmt_images["rzcobs"].read_bytes())
    banner = b"\r\nboot: demo firmware 1.0\r\nREADY\r\n"
    service, backend = service_for(config, board, banner=banner)
    try:
        result = service.call("flash_firmware", capture_args(until="READY\r\n"))

        check(result.get("ok") is True, f"a plain capture failed: {result}")
        capture = result.get("capture")
        check(isinstance(capture, dict), f"a plain capture has no capture: {result}")
        check(capture.get("data") == data_result(banner, "utf-8"), f"a plain capture does not hand out text and hex: {capture}")
        check(capture.get("until_matched") is True, f"a plain capture's until did not match: {capture}")
        check("frames" not in capture and "defmt" not in capture, f"a plain capture answers with defmt fields: {capture}")
        check("defmt" not in result, f"a plain flash answers with defmt: {result}")
        check(backend.calls == ["flash_firmware"], f"the probe was driven other than by one flash: {backend.calls}")
        check_bench_free(service, "a plain capture")
    finally:
        service.close()
