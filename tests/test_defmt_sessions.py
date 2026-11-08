"""`com_session_start` with `defmt`: a serial line decoded with a validated table.

A COM session can decode the board's defmt frames instead of handing out text
and hex. The table comes from one of two places:

* `defmt: true`, the ELF the last confirmed `flash_firmware` of this service
  wrote, checked again through the artifact policy and by its digest;
* `defmt: {"image_path": ...}`, an ELF named outright, validated by the same
  artifact policy a flash uses. It decodes with no flash in the server session
  at all, and it is `proven` only when it is the confirmed flashed file,
  identity and digest both.

The teeth here:

* every table refusal comes before the port is opened, and an active session
  whose change is refused keeps decoding as it did, with what it had buffered;
* the stream keeps complete and partial frames across a start that does not
  clear, keeps a raw desynchronization until a clearing start, decodes a burst
  a bounded number of frames per read, counts what overflowed, and leaves what
  follows an `until` match for the next read;
* a session without `defmt` answers exactly as it always has.

The board is faked at the serial boundary with the `Board` of
`tests/test_flash_capture.py`. The tables are the cross-compiled fixture images
under `tests/fixtures/defmt/`, copied into each test's own workspace; the
frames are encoded for them by `tests/defmt_wire.py` and are invented for
these tests, not recorded from any board. What a frame decodes to is asked of
`agentic_hil.defmt.decode_stream` on the same bytes.

Every check raises `AssertionError` with its own message, so the first one a
missing behaviour reaches names that behaviour.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, write_config
from defmt_wire import RzcobsEncoder, build_elf, defmt_symbol, encode_frame, rzcobs_stream
from test_flash_capture import Board, BoardBackend

from agentic_hil import comports, defmt
from agentic_hil.comports import COM_PORT_IDENTITY_MISMATCH, data_result
from agentic_hil.config import load_config
from agentic_hil.defmt import DefmtTableError
from agentic_hil.knowledge import ERROR_URI_PREFIX, catalogue_entry, permission_key
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

# Device locks are machine-wide, so this file's port id and device are its own.
PORT_ID = "defmt_sessions_uart"
DEVICE = "/dev/ttyDEFMTSESSIONS0"

FIXTURES = Path(__file__).parent / "fixtures" / "defmt"
RZCOBS_ELF = FIXTURES / "defmt-fixture-rzcobs.elf"
RAW_ELF = FIXTURES / "defmt-fixture-raw.elf"
IMAGE = "build/app.elf"

TABLE_ERRORS = ("defmt_table_not_available", "defmt_table_changed", "defmt_table_invalid")

# One ELF per table the decoder refuses, each written the way the decoder's own
# tests write it, and each confirmed against the decoder before it is used.
INVALID_TABLES = {
    "table_missing": lambda: build_elf([("main", 0x08000400, 4, ".text")], {".text": bytes(8)}),
    "table_invalid": lambda: build_elf(
        [("_defmt_version_ = 4", 1, 0, ".defmt"), ("_defmt_encoding_ = rzcobs", 2, 0, ".defmt"), ("not json", 3, 1, ".defmt")],
        {".defmt": bytes(4)},
    ),
    "version_unsupported": lambda: build_elf(
        [("_defmt_version_ = 5", 1, 0, ".defmt"), ("_defmt_encoding_ = rzcobs", 2, 0, ".defmt"), (defmt_symbol("defmt_info", "x"), 3, 1, ".defmt")],
        {".defmt": bytes(4)},
    ),
}


def check(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# ---------------------------------------------------------------------------
# The bench.


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch):
    fake = Board()
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=fake.construct))
    yield fake
    fake.finish()


def config_for(workspace: Path, *, port_yaml: str = "", permissions: dict[str, bool] | None = None):
    written = write_config(workspace, com_ports_yaml=f'com_ports:\n  {PORT_ID}:\n    device: "{DEVICE}"\n{port_yaml}', permissions=permissions)
    return load_config(str(written))


def service_for(config, board: Board) -> tuple[AgenticHILToolService, BoardBackend]:
    backend = BoardBackend(board, banner=b"")
    service = AgenticHILToolService(config, backend=backend)
    board.watch = service.coordinator.bench.held_resources
    return service, backend


def place(workspace: Path, content: Path | bytes, relative: str = IMAGE) -> bytes:
    data = content.read_bytes() if isinstance(content, Path) else content
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def table_of(image: Path) -> defmt.DefmtTable:
    return defmt.load_table_bytes(image.read_bytes())


def refusal_reason(data: bytes) -> str:
    try:
        defmt.load_table_bytes(data)
    except DefmtTableError as error:
        return error.reason
    raise AssertionError("the planted ELF carries a table the decoder reads")


def statement(table: defmt.DefmtTable, number: int) -> bytes:
    return encode_frame(table, "escaped {{braces}} {=u8}", [number], timestamp=[number * 1_000])


def hello(table: defmt.DefmtTable, number: int = 0) -> bytes:
    return encode_frame(table, "Hello, world!", [], timestamp=[number])


def records(table: defmt.DefmtTable, data: bytes) -> list[dict]:
    return defmt.decode_stream(table, data).records


def start(service: AgenticHILToolService, **arguments: object) -> dict:
    return service.call("com_session_start", {"port_id": PORT_ID, **arguments})


def read(service: AgenticHILToolService, **arguments: object) -> dict:
    return service.call("com_read", {"port_id": PORT_ID, **arguments})


def flash(service: AgenticHILToolService, image: str = IMAGE) -> dict:
    return service.call("flash_firmware", {"image_path": image})


def decoding(result: dict) -> bool:
    return result.get("ok") is True and isinstance(result.get("defmt"), dict)


def frames_of(result: dict) -> list:
    frames = result.get("frames")
    check(isinstance(frames, list), f"a read on a decoding session handed out no frames: {result}")
    check("data" not in result, f"a read on a decoding session handed out text and hex as well: {result}")
    return frames


def check_all_errors(result: dict, delivered: int, what: str) -> None:
    """Undecodable bytes become error records and are never held back or skipped."""
    frames = frames_of(result)
    check(frames, f"{what} handed out no records for the bytes it could not decode: {result}")
    check(all("error" in record for record in frames), f"{what} decoded a frame: {result}")
    check(result.get("bytes_read") == delivered and result.get("buffer_remaining_bytes") == 0, f"{what} did not account for the {delivered} bytes it was sent: {result}")


def names_image(source: dict, relative: str) -> bool:
    path = source.get("path")
    return isinstance(path, str) and Path(path).as_posix().endswith(relative)


def logged_rx(config, log_path: str) -> bytes:
    """Every byte the session's log says the port received."""
    path = Path(config.work_dir) / log_path
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return b"".join(bytes.fromhex(entry["hex"]) for entry in entries if entry.get("direction") == "rx")


def check_raw_log(service: AgenticHILToolService, config, answered: dict, sent: bytes) -> None:
    """Stop the session, which joins its reader, and read its log: the raw bytes, decoded or not."""
    stopped = service.call("com_session_stop", {"port_id": PORT_ID})
    check(stopped.get("ok") is True, f"the session could not be stopped: {stopped}")
    check(logged_rx(config, answered["log_path"]) == sent, "the session log does not hold the raw bytes the port received")


def check_nothing_opened(service: AgenticHILToolService, board: Board, result: dict, what: str, *, says_not_started: bool = True) -> None:
    check(result.get("ok") is False, f"{what} answered ok: {result}")
    if says_not_started:
        check(result.get("side_effect_committed") is False, f"{what} did not say nothing was started: {result}")
    check(result.get("quarantined") is not True, f"{what} quarantined a bench it never touched: {result}")
    check(board.constructed == 0 and board.opens == 0, f"{what} reached the serial port: constructed {board.constructed}, opened {board.opens}")
    check(service.com_ports.sessions == {}, f"{what} left a session registered: {sorted(service.com_ports.sessions)}")
    check(not service.coordinator.leases, f"{what} left a lease held: {sorted(service.coordinator.leases)}")


# ---------------------------------------------------------------------------
# Provenance: where the table comes from, and what `proven` claims.


def test_defmt_true_after_a_confirmed_flash_decodes_frames_and_is_proven(tmp_path: Path, board: Board) -> None:
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    image = place(workspace, RZCOBS_ELF)
    table = table_of(RZCOBS_ELF)
    stream = rzcobs_stream([statement(table, 1), hello(table, 2), statement(table, 3)])
    service, backend = service_for(config, board)
    try:
        flashed = flash(service)
        check(flashed.get("ok") is True, f"the plain flash before the session failed: {flashed}")

        started = start(service, defmt=True)

        check(decoding(started), "com_session_start with defmt: true after a confirmed flash did not start a decoding session")
        source = started["defmt"]
        check(source.get("proven") is True, f"the table of the confirmed flashed ELF is not proven: {source}")
        check(source.get("origin") == "flashed", f"the table of defmt: true does not say it came from the flash: {source}")
        check(source.get("sha256") == digest(image), f"the session names another digest than the flashed ELF's: {source}")
        check(names_image(source, IMAGE), f"the session does not name the flashed ELF: {source}")
        board.transmit(stream)
        answered = read(service)
        check(answered.get("ok") is True, f"the read failed: {answered}")
        check(frames_of(answered) == records(table, stream), f"the frames are not what the table decodes: {answered}")
        check(answered["defmt"].get("proven") is True, f"the read does not carry the proof: {answered}")
        check(backend.calls == ["flash_firmware"], f"the session made a call on the probe: {backend.calls}")
        check_raw_log(service, config, answered, stream)
    finally:
        service.close()


def test_defmt_true_with_no_confirmed_flash_is_refused_before_the_port_opens(tmp_path: Path, board: Board) -> None:
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, RZCOBS_ELF)
    service, backend = service_for(config, board)
    try:
        refused = start(service, defmt=True)

        check(refused.get("error_type") == "defmt_table_not_available", "defmt: true with no confirmed flash was not refused as defmt_table_not_available")
        check_nothing_opened(service, board, refused, "defmt: true with no confirmed flash")
        check(backend.calls == [], f"the refusal made a call on the probe: {backend.calls}")
    finally:
        service.close()


def test_defmt_true_after_the_flashed_elf_changed_is_refused_before_the_port_opens(tmp_path: Path, board: Board) -> None:
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, RZCOBS_ELF)
    service, backend = service_for(config, board)
    try:
        flashed = flash(service)
        check(flashed.get("ok") is True, f"the plain flash before the rebuild failed: {flashed}")
        # A rebuild lands on the flashed path: the file is a valid ELF with a
        # valid table, and it is not the image on the board.
        place(workspace, RAW_ELF)

        refused = start(service, defmt=True)

        check(refused.get("error_type") == "defmt_table_changed", "defmt: true after the flashed ELF changed was not refused as defmt_table_changed")
        check_nothing_opened(service, board, refused, "defmt: true after the flashed ELF changed")
    finally:
        service.close()


def test_defmt_true_after_a_hex_flash_has_no_table(tmp_path: Path, board: Board) -> None:
    """A .hex carries no table, so the ELF flashed before it no longer describes the board."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, RZCOBS_ELF)
    place(workspace, b":00000001FF\n", "build/app.hex")
    service, backend = service_for(config, board)
    try:
        check(flash(service).get("ok") is True, "the ELF flash before the .hex flash failed")
        check(flash(service, "build/app.hex").get("ok") is True, "the .hex flash failed")

        refused = start(service, defmt=True)

        check(refused.get("error_type") == "defmt_table_not_available", "defmt: true after a .hex flash was not refused as defmt_table_not_available")
        check_nothing_opened(service, board, refused, "defmt: true after a .hex flash")
    finally:
        service.close()


def test_an_explicit_elf_decodes_with_no_flash_in_the_server_session(tmp_path: Path, board: Board) -> None:
    """The owner's case: a board flashed some other way, and an ELF named for its table."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    image = place(workspace, RZCOBS_ELF)
    table = table_of(RZCOBS_ELF)
    stream = rzcobs_stream([hello(table, 1), statement(table, 2)])
    service, backend = service_for(config, board)
    try:
        started = start(service, defmt={"image_path": IMAGE})

        check(decoding(started), "an explicit ELF with no flash in this server session did not start a decoding session")
        source = started["defmt"]
        check(source.get("proven") is False, f"an ELF nobody flashed through this service is proven: {source}")
        check(source.get("sha256") == digest(image), f"the session names another digest than the named ELF's: {source}")
        check(names_image(source, IMAGE), f"the session does not name the ELF it was given: {source}")
        board.transmit(stream)
        answered = read(service)
        check(frames_of(answered) == records(table, stream), f"the frames are not what the named ELF's table decodes: {answered}")
        check(answered["defmt"].get("proven") is False, f"the read claims a proof the session does not have: {answered}")
        check(backend.calls == [], f"the session reached the probe: {backend.calls}")
    finally:
        service.close()


def test_an_explicit_elf_that_is_the_flashed_file_is_proven(tmp_path: Path, board: Board) -> None:
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    image = place(workspace, RZCOBS_ELF)
    table = table_of(RZCOBS_ELF)
    stream = rzcobs_stream([statement(table, 4)])
    service, backend = service_for(config, board)
    try:
        check(flash(service).get("ok") is True, "the plain flash before the session failed")

        started = start(service, defmt={"image_path": IMAGE})

        check(decoding(started), "an explicit ELF that is the confirmed flashed file did not start a decoding session")
        check(started["defmt"].get("proven") is True, f"the flashed file, unchanged, is not proven: {started['defmt']}")
        check(started["defmt"].get("sha256") == digest(image), f"the session names another digest: {started['defmt']}")
        board.transmit(stream)
        check(frames_of(read(service)) == records(table, stream), "the frames are not what the flashed table decodes")
    finally:
        service.close()


@pytest.mark.parametrize(
    ("named", "source_image"),
    [pytest.param(IMAGE, RAW_ELF, id="other_bytes"), pytest.param("build/copy.elf", RZCOBS_ELF, id="same_bytes_other_path")],
)
def test_an_explicit_elf_other_than_the_flashed_file_is_not_proven(tmp_path: Path, board: Board, named: str, source_image: Path) -> None:
    """Identity and digest both: the flashed path holding other bytes is not the
    flashed image, and a copy of the flashed bytes elsewhere is not the flashed
    file. Either still decodes, with the table of the file it names."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, RZCOBS_ELF)
    case = f"{named} holding {source_image.name}"
    service, backend = service_for(config, board)
    try:
        check(flash(service).get("ok") is True, "the plain flash before the session failed")
        image = place(workspace, source_image, named)
        table = table_of(source_image)
        frame = hello(table, 5)
        stream = rzcobs_stream([frame]) if table.encoding == "rzcobs" else frame

        started = start(service, defmt={"image_path": named})

        check(decoding(started), "an explicit ELF other than the flashed file did not start a decoding session")
        check(started["defmt"].get("proven") is False, f"{case}: a file that is not the confirmed flashed file is proven: {started['defmt']}")
        check(started["defmt"].get("sha256") == digest(image), f"{case}: the session names another digest: {started['defmt']}")
        board.transmit(stream)
        answered = read(service)
        check(frames_of(answered) == records(table, stream), f"{case}: the frames are not what the named file's table decodes: {answered}")
        check(answered["defmt"].get("proven") is False, f"{case}: the read claims a proof: {answered}")
    finally:
        service.close()


def test_an_explicit_elf_outside_the_artifact_policy_is_refused_by_that_policy(tmp_path: Path, board: Board) -> None:
    """The artifact roots are the flash's own rule, and an ELF read for its table meets the same one."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, RZCOBS_ELF, "elsewhere/app.elf")
    service, backend = service_for(config, board)
    try:
        refused = start(service, defmt={"image_path": "elsewhere/app.elf"})

        check(refused.get("error_type") == "artifact_validation_failed", "an explicit ELF outside the artifact policy was not refused by that policy as artifact_validation_failed")
        validation = refused.get("validation")
        check(isinstance(validation, dict) and validation.get("allowed_root") is False, f"the refusal is not the artifact policy's own: {refused}")
        check(refused.get("tool") == "com_session_start", f"the refusal names another tool: {refused}")
        # The policy's refusal is its own, as a flash receives it.
        check_nothing_opened(service, board, refused, "an ELF outside the artifact roots", says_not_started=False)
    finally:
        service.close()


@pytest.mark.parametrize("reason", sorted(INVALID_TABLES))
def test_an_invalid_table_is_refused_before_the_port_opens(tmp_path: Path, board: Board, reason: str) -> None:
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    data = INVALID_TABLES[reason]()
    check(refusal_reason(data) == reason, f"the planted ELF is refused by the decoder as {refusal_reason(data)}, not {reason}")
    place(workspace, data, "build/broken.elf")
    service, backend = service_for(config, board)
    try:
        refused = start(service, defmt={"image_path": "build/broken.elf"})

        check(refused.get("error_type") == "defmt_table_invalid", f"an explicit ELF whose table is {reason} was not refused as defmt_table_invalid")
        check(refused.get("table_error") == reason, f"the refusal does not name the decoder's reason {reason}: {refused}")
        check_nothing_opened(service, board, refused, f"a {reason} table")
    finally:
        service.close()


def test_a_defmt_start_on_a_port_whose_read_permission_is_off_is_refused_before_the_port_opens(tmp_path: Path, board: Board) -> None:
    """Decoding is reading, so a port that may only be written answers to
    allow_read, with the key `com_read` names, before anything is opened."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace, permissions={**DEFAULT_TEST_PERMISSIONS, "allow_com_read": False})
    place(workspace, RZCOBS_ELF)
    service, backend = service_for(config, board)
    try:
        refused = start(service, defmt={"image_path": IMAGE})

        check(refused.get("error_type") == "permission_denied", "a defmt start on a port whose read permission is off was not refused as permission_denied")
        check(refused.get("permission") == permission_key("com_ports", PORT_ID, "allow_read"), f"the refusal does not name the port's read permission: {refused}")
        check_nothing_opened(service, board, refused, "a defmt start without the read permission", says_not_started=False)
        check(backend.calls == [], f"the refusal made a call on the probe: {backend.calls}")
    finally:
        service.close()


def test_a_defmt_start_on_a_port_whose_identity_does_not_match_is_refused_before_the_port_opens(tmp_path: Path, board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid table does not let a session past the identity check: the name
    leads to another board, and the refusal is the one a plain start gets."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace, port_yaml='    serial_number: "DEFMT-SESSIONS-A"\n')
    host = {"ok": True, "tool": "com_ports_available", "ports": [{"device": DEVICE, "serial_number": "DEFMT-SESSIONS-B"}], "summary": "1 available COM port(s)."}
    monkeypatch.setattr(comports, "list_available_com_ports", lambda tool="com_ports_available": host)
    place(workspace, RZCOBS_ELF)
    service, backend = service_for(config, board)
    try:
        reference = start(service)
        check(reference.get("error_type") == COM_PORT_IDENTITY_MISMATCH, f"the planted host does not make a plain start a mismatch: {reference}")
        check_nothing_opened(service, board, reference, "the plain start on the mismatched port")

        refused = start(service, defmt={"image_path": IMAGE})

        check(refused.get("error_type") == COM_PORT_IDENTITY_MISMATCH, "a defmt start on a port whose identity does not match was not refused as com_port_identity_mismatch")
        check_nothing_opened(service, board, refused, "a defmt start on the mismatched port")
        check(backend.calls == [], f"the refusal made a call on the probe: {backend.calls}")
    finally:
        service.close()


MALFORMED_DEFMT = {
    "a_string": "yes",
    "an_empty_object": {},
    "a_number_path": {"image_path": 1},
    "an_extra_key": {"image_path": IMAGE, "extra": 1},
}


@pytest.mark.parametrize("shape", sorted(MALFORMED_DEFMT))
def test_a_malformed_defmt_argument_is_refused_by_the_schema_before_anything_is_touched(tmp_path: Path, board: Board, shape: str) -> None:
    """`defmt` is `true`, `false` or `{"image_path": ...}` and nothing else, and
    the published schema says so before any table, port or probe is reached."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, RZCOBS_ELF)
    service, backend = service_for(config, board)
    try:
        refused = start(service, defmt=MALFORMED_DEFMT[shape])

        check(refused.get("error_type") == "invalid_argument", f"defmt as {shape} was not refused as invalid_argument: {refused}")
        check("defmt" in str(refused.get("field", "")), f"the refusal of defmt as {shape} does not name defmt: {refused}")
        check_nothing_opened(service, board, refused, f"defmt as {shape}", says_not_started=False)
        check(backend.calls == [], f"defmt as {shape} reached the probe: {backend.calls}")
    finally:
        service.close()


# ---------------------------------------------------------------------------
# An active session: enabling, replacing and disabling, and a change refused.


def test_defmt_can_be_enabled_replaced_and_disabled_on_an_active_session(tmp_path: Path, board: Board) -> None:
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    rzcobs_image = place(workspace, RZCOBS_ELF)
    raw_image = place(workspace, RAW_ELF, "build/raw.elf")
    rzcobs_table, raw_table = table_of(RZCOBS_ELF), table_of(RAW_ELF)
    service, backend = service_for(config, board)
    try:
        plain = start(service)
        check(plain.get("ok") is True and "defmt" not in plain, f"the plain session could not be started: {plain}")

        enabled = start(service, clear_buffer=False, defmt={"image_path": IMAGE})

        check(decoding(enabled) and enabled.get("already_active") is True, "defmt could not be enabled on an active session")
        check(enabled["defmt"].get("sha256") == digest(rzcobs_image), f"the enabled table is not the named ELF's: {enabled['defmt']}")
        stream = rzcobs_stream([statement(rzcobs_table, 6)])
        board.transmit(stream)
        check(frames_of(read(service)) == records(rzcobs_table, stream), "the enabled session does not decode with the named table")

        replaced = start(service, clear_buffer=False, defmt={"image_path": "build/raw.elf"})
        check(decoding(replaced) and replaced.get("already_active") is True, f"the table could not be replaced on the active session: {replaced}")
        check(replaced["defmt"].get("sha256") == digest(raw_image), f"the replaced table is not the new ELF's: {replaced['defmt']}")
        frame = hello(raw_table)
        board.transmit(frame)
        check(frames_of(read(service)) == records(raw_table, frame), "the session does not decode with the replacing table")

        disabled = start(service, clear_buffer=False, defmt=False)
        check(disabled.get("ok") is True and disabled.get("already_active") is True, f"defmt could not be disabled on the active session: {disabled}")
        check("defmt" in disabled and disabled["defmt"] is None, f"the disabling start does not say decoding is off: {disabled}")
        text = b"plain line\r\n"
        board.transmit(text)
        answered = read(service)
        check(answered.get("data") == data_result(text, "utf-8"), f"the disabled session does not hand out text and hex again: {answered}")
        check("frames" not in answered and "defmt" not in answered, f"the disabled session still decodes: {answered}")
        check(board.opens == 1, f"a change of mode reopened the port: {board.opens} opens")
    finally:
        service.close()


def test_a_failed_change_keeps_the_working_mode(tmp_path: Path, board: Board) -> None:
    """A refused replacement leaves the table, the session and what it had buffered."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    image = place(workspace, RZCOBS_ELF)
    place(workspace, INVALID_TABLES["table_missing"](), "build/broken.elf")
    table = table_of(RZCOBS_ELF)
    stream = rzcobs_stream([statement(table, 7), hello(table, 8)])
    service, backend = service_for(config, board)
    try:
        started = start(service, defmt={"image_path": IMAGE})

        check(decoding(started), "the decoding session whose change fails could not be started")
        session = service.com_ports.sessions[PORT_ID]
        board.transmit(stream)
        invalid = start(service, clear_buffer=False, defmt={"image_path": "build/broken.elf"})
        check(invalid.get("error_type") == "defmt_table_invalid", f"the replacement by a table-less ELF was not refused: {invalid}")
        # A refused change clears nothing, whatever clear_buffer says.
        unavailable = start(service, defmt=True)
        check(unavailable.get("error_type") == "defmt_table_not_available", f"the replacement by an unflashed table was not refused: {unavailable}")
        check(service.com_ports.sessions.get(PORT_ID) is session, "a refused change replaced the session")
        check(board.opens == 1, f"a refused change reopened the port: {board.opens} opens")
        answered = read(service)
        check(answered.get("ok") is True, f"the session did not survive the refused changes: {answered}")
        check(frames_of(answered) == records(table, stream), f"the frames buffered before the refused changes were not kept: {answered}")
        check(answered["defmt"].get("sha256") == digest(image), f"the session decodes with another table: {answered['defmt']}")
    finally:
        service.close()


def test_a_failed_enable_keeps_a_plain_session_plain(tmp_path: Path, board: Board) -> None:
    """A refused enable leaves a plain session handing out text and hex, with
    what it had buffered, on the port it already had open."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, INVALID_TABLES["table_missing"](), "build/broken.elf")
    text = b"plain before the refused enable\r\n"
    service, backend = service_for(config, board)
    try:
        plain = start(service)
        check(plain.get("ok") is True and "defmt" not in plain, f"the plain session could not be started: {plain}")
        session = service.com_ports.sessions[PORT_ID]
        board.transmit(text)

        invalid = start(service, clear_buffer=False, defmt={"image_path": "build/broken.elf"})

        check(invalid.get("error_type") == "defmt_table_invalid", "a failed enable on a plain session was not refused as defmt_table_invalid")
        # A refused change clears nothing, whatever clear_buffer says.
        unavailable = start(service, defmt=True)
        check(unavailable.get("error_type") == "defmt_table_not_available", f"the enable by an unflashed table was not refused: {unavailable}")
        check(service.com_ports.sessions.get(PORT_ID) is session, "a refused enable replaced the session")
        check(board.opens == 1, f"a refused enable reopened the port: {board.opens} opens")
        answered = read(service)
        check(answered.get("data") == data_result(text, "utf-8"), f"the plain session does not hand out its buffered text and hex after the refused enable: {answered}")
        check("frames" not in answered and "defmt" not in answered, f"the plain session answers with defmt fields after the refused enable: {answered}")
        check(answered.get("bytes_read") == len(text), f"the plain session counts other bytes after the refused enable: {answered}")
    finally:
        service.close()


# ---------------------------------------------------------------------------
# The stream.


def test_complete_and_partial_frames_survive_a_non_clearing_start(tmp_path: Path, board: Board) -> None:
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, RZCOBS_ELF)
    table = table_of(RZCOBS_ELF)
    encoder = RzcobsEncoder()
    chunks = [encoder.frame(statement(table, number)) for number in (10, 11, 12)]
    stream = b"".join(chunks)
    expected = records(table, stream)
    check(len(expected) == 3 and len(chunks[2]) > 2, "the planted stream is not three frames")
    service, backend = service_for(config, board)
    try:
        started = start(service, defmt={"image_path": IMAGE})

        check(decoding(started), "the decoding session could not be started")
        # Two whole frames and the start of a third, none of them read yet.
        board.transmit(chunks[0] + chunks[1] + chunks[2][:2])
        kept = start(service, clear_buffer=False)
        check(kept.get("ok") is True and kept.get("already_active") is True, f"the non-clearing start failed: {kept}")
        first = read(service)
        check(frames_of(first) == expected[:2], f"the complete frames did not survive the non-clearing start: {first}")
        again = start(service, clear_buffer=False)
        check(again.get("ok") is True, f"the second non-clearing start failed: {again}")
        board.transmit(chunks[2][2:])
        second = read(service)
        check(frames_of(second) == expected[2:], f"the partial frame did not survive the non-clearing start: {second}")
    finally:
        service.close()


def test_raw_desynchronization_persists_until_a_clearing_start(tmp_path: Path, board: Board) -> None:
    """The raw encoding has no frame boundary to resume at, so a frame that does
    not decode leaves every later byte unreadable until the stream starts over."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, RAW_ELF)
    table = table_of(RAW_ELF)
    check(table.encoding == "raw", "the raw fixture is not raw-encoded")
    good = hello(table)
    unknown = (0x7777).to_bytes(2, "little") + bytes(4)
    expected_good = records(table, good)
    service, backend = service_for(config, board)
    try:
        started = start(service, defmt={"image_path": IMAGE})

        check(decoding(started), "the raw decoding session could not be started")
        board.transmit(good + unknown)
        first = read(service)
        check(frames_of(first) == records(table, good + unknown), f"the frame before the bad one is not decoded as the table decodes it: {first}")
        check(first["defmt"].get("desynchronized") is True, f"a raw frame that does not decode does not desynchronize: {first}")

        board.transmit(good)
        second = read(service)
        check_all_errors(second, len(good), "a desynchronized raw stream")
        check(second["defmt"].get("desynchronized") is True, f"the desynchronization did not persist: {second}")

        kept = start(service, clear_buffer=False)
        check(kept.get("ok") is True, f"the non-clearing start failed: {kept}")
        board.transmit(good)
        third = read(service)
        check_all_errors(third, len(good), "a desynchronized raw stream after a non-clearing start")
        check(third["defmt"].get("desynchronized") is True, f"the desynchronization did not persist across a non-clearing start: {third}")

        cleared = start(service, clear_buffer=True)
        check(cleared.get("ok") is True, f"the clearing start failed: {cleared}")
        board.transmit(good)
        fourth = read(service)
        check(frames_of(fourth) == expected_good, f"a clearing start did not recover the stream: {fourth}")
        check(fourth["defmt"].get("desynchronized") is False, f"the recovered stream still says it is desynchronized: {fourth}")
    finally:
        service.close()


def test_a_burst_is_decoded_in_bounded_reads(tmp_path: Path, board: Board) -> None:
    """More frames than one read decodes: each read hands out at most the bound,
    and the rest stays buffered for the next, in order and complete."""
    workspace = tmp_path / "workspace"
    buffer_bytes = 1048576
    config = config_for(workspace, port_yaml=f"    max_buffer_bytes: {buffer_bytes}\n")
    place(workspace, RZCOBS_ELF)
    table = table_of(RZCOBS_ELF)
    service, backend = service_for(config, board)
    try:
        started = start(service, defmt={"image_path": IMAGE})

        check(decoding(started), "the decoding session for the burst could not be started")
        bound = comports.DEFMT_MAX_FRAMES_PER_READ
        check(isinstance(bound, int) and bound >= 1, f"the burst bound is not a positive frame count: {bound!r}")
        stream = rzcobs_stream([hello(table, number) for number in range(2 * bound + 1)])
        check(len(stream) <= buffer_bytes, f"a burst of {2 * bound + 1} frames does not fit the port's buffer")
        expected = records(table, stream)
        board.transmit(stream)
        reads = [read(service) for _ in range(3)]
        counts = [len(frames_of(answered)) for answered in reads]
        check(counts == [bound, bound, 1], f"the burst was not handed out {bound} frames at a time: {counts}")
        check(reads[0].get("buffer_remaining_bytes", 0) > 0, f"the frames past the bound were not left buffered: {reads[0]}")
        check([record for answered in reads for record in answered["frames"]] == expected, "the bounded reads lost, reordered or changed frames")
        check(reads[2].get("buffer_remaining_bytes") == 0, f"bytes were left after the last frame: {reads[2]}")
    finally:
        service.close()


def test_overflow_on_a_raw_decoding_session_is_counted_and_desynchronizes(tmp_path: Path, board: Board) -> None:
    """Bytes the buffer had to drop are counted, and on the raw encoding they take
    the frame boundary with them. The log still holds every byte."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace, port_yaml="    max_buffer_bytes: 64\n")
    place(workspace, RAW_ELF)
    table = table_of(RAW_ELF)
    payload = b"".join(hello(table) for _ in range(100))
    check(len(payload) == 200, "the planted raw payload is not 200 bytes")
    service, backend = service_for(config, board)
    try:
        started = start(service, defmt={"image_path": IMAGE})

        check(decoding(started), "the raw decoding session for the overflow could not be started")
        board.transmit(payload)
        answered = read(service)
        check(answered.get("overflow_bytes") == 136, f"the dropped bytes were not counted: {answered}")
        # Desynchronizing on any raw overflow is the design's conservative rule:
        # these 2-byte frames happen to stay aligned, and a raw stream in
        # general cannot tell where its next frame starts.
        check(answered["defmt"].get("desynchronized") is True, f"an overflow on a raw stream did not desynchronize it: {answered}")
        check_all_errors(answered, 64, "a raw stream that lost bytes to an overflow")
        check_raw_log(service, config, answered, payload)
    finally:
        service.close()


def test_until_returns_through_the_matching_frame_and_keeps_the_tail(tmp_path: Path, board: Board) -> None:
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, RZCOBS_ELF)
    table = table_of(RZCOBS_ELF)
    stream = rzcobs_stream([statement(table, number) for number in (1, 2, 3, 4)])
    expected = records(table, stream)
    until = expected[2]["message"]
    check(sum(until in record["text"] for record in expected) == 1, "the until text names more than one planted frame")
    service, backend = service_for(config, board)
    try:
        started = start(service, defmt={"image_path": IMAGE})

        check(decoding(started), "the decoding session for until could not be started")
        board.transmit(stream)
        matched = read(service, until=until, wait_timeout_s=5)
        check(matched.get("until_matched") is True, f"the until text did not match a frame: {matched}")
        check(frames_of(matched) == expected[:3], f"the read did not end at the matching frame: {matched}")
        tail = read(service)
        check(frames_of(tail) == expected[3:], f"the frame after the match was not kept for the next read: {tail}")
    finally:
        service.close()


def test_a_later_flash_reproves_an_active_session(tmp_path: Path, board: Board) -> None:
    """`proven` follows the confirmed programming: a flash of the named file
    proves the session, and a flash of anything else takes the proof back."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, RZCOBS_ELF)
    place(workspace, RAW_ELF, "build/other.elf")
    table = table_of(RZCOBS_ELF)
    service, backend = service_for(config, board)
    try:
        started = start(service, defmt={"image_path": IMAGE})

        check(decoding(started), "the explicit-ELF session to reprove could not be started")
        check(started["defmt"].get("proven") is False, f"the unflashed ELF is proven: {started['defmt']}")
        check(flash(service).get("ok") is True, "the flash of the named ELF failed")
        stream = rzcobs_stream([hello(table, 1)])
        board.transmit(stream)
        proven = read(service)
        check(frames_of(proven) == records(table, stream), f"the session stopped decoding after the flash: {proven}")
        check(proven["defmt"].get("proven") is True, f"the flash of the named ELF did not prove the session: {proven}")
        check(flash(service, "build/other.elf").get("ok") is True, "the flash of the other ELF failed")
        board.transmit(stream)
        unproven = read(service)
        check(frames_of(unproven) == records(table, stream), f"the session stopped decoding after the second flash: {unproven}")
        check(unproven["defmt"].get("proven") is False, f"a flash of another image left the session proven: {unproven}")
    finally:
        service.close()


def test_an_unconfirmed_flash_takes_the_proof_back(tmp_path: Path, board: Board) -> None:
    """A flash the backend cannot confirm may have written part of another
    image, so nothing proven before it stays proven. The named ELF still
    decodes; it claims nothing."""
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, RZCOBS_ELF)
    table = table_of(RZCOBS_ELF)
    service, backend = service_for(config, board)
    try:
        check(flash(service).get("ok") is True, "the confirmed flash before the session failed")

        started = start(service, defmt=True)

        check(decoding(started) and started["defmt"].get("proven") is True, "the proven session an unconfirmed flash unproves could not be started")
        backend.unconfirmed = True
        unconfirmed = flash(service)
        check(unconfirmed.get("ok") is False, f"the planted unconfirmed flash answered ok: {unconfirmed}")
        if PORT_ID in service.com_ports.sessions:
            # The session the proof was given to is still open, so it is the
            # one that has to stop claiming it, before anything restarts it.
            survived = rzcobs_stream([hello(table, 8)])
            board.transmit(survived)
            live = read(service)
            check(frames_of(live) == records(table, survived), f"the session that survived the unconfirmed flash does not decode: {live}")
            check(live["defmt"].get("proven") is False, f"the session that survived the unconfirmed flash still claims its proof: {live}")
        # Whether or not the flash's recovery stopped the session, a start with
        # the same ELF answers for the board as the unconfirmed flash left it.
        restarted = start(service, clear_buffer=False, defmt={"image_path": IMAGE})
        check(decoding(restarted), f"the named ELF does not decode after the unconfirmed flash: {restarted}")
        check(restarted["defmt"].get("proven") is False, f"an unconfirmed flash left the session proven: {restarted['defmt']}")
        stream = rzcobs_stream([hello(table, 9)])
        board.transmit(stream)
        answered = read(service)
        check(frames_of(answered) == records(table, stream), f"the session does not decode after the unconfirmed flash: {answered}")
        check(answered["defmt"].get("proven") is False, f"a read after the unconfirmed flash claims a proof: {answered}")
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Advice, and the session without defmt.


@pytest.mark.parametrize("error_type", TABLE_ERRORS)
def test_each_table_error_has_a_remedy(tmp_path: Path, error_type: str) -> None:
    entry = catalogue_entry(error_type)
    check(entry is not None, f"no catalogue entry for {error_type}")
    check(isinstance(entry.get("meaning"), str) and entry["meaning"].strip(), f"{error_type} says nothing about what it means: {entry}")
    steps = entry.get("remediation")
    check(isinstance(steps, list) and steps and all(isinstance(step, str) and step.strip() for step in steps), f"{error_type} names no fix: {entry}")
    service = AgenticHILToolService(load_config(str(write_config(tmp_path / "reference"))), frontend="mcp")
    try:
        uri = ERROR_URI_PREFIX + error_type
        response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}}, service)
        check(isinstance(response, dict) and "error" not in response, f"{uri} does not resolve: {response}")
        contents = response["result"]["contents"]
        check([content["uri"] for content in contents] == [uri], f"{uri} answers another resource: {contents}")
        check(json.loads(contents[0]["text"]) == entry, f"{uri} serves another entry than the catalogue holds")
    finally:
        service.close()


def test_a_session_without_defmt_answers_as_before(tmp_path: Path, board: Board) -> None:
    workspace = tmp_path / "workspace"
    config = config_for(workspace)
    place(workspace, RZCOBS_ELF)
    service, backend = service_for(config, board)
    try:
        started = start(service)
        check(started.get("ok") is True and started.get("already_active") is False, f"the plain session could not be started: {started}")
        check("defmt" not in started and "defmt" not in started["session"], f"a plain session answers with defmt: {started}")
        text = b"boot: plain firmware\r\n"
        board.transmit(text)
        answered = read(service)
        check(answered.get("data") == data_result(text, "utf-8"), f"a plain read does not hand out text and hex: {answered}")
        check("frames" not in answered and "defmt" not in answered, f"a plain read answers with defmt fields: {answered}")
        check(answered.get("bytes_read") == len(text), f"a plain read counts other bytes: {answered}")
        repeated = start(service, clear_buffer=False)
        check(repeated.get("ok") is True and repeated.get("already_active") is True, f"the repeated plain start failed: {repeated}")
        check("defmt" not in repeated, f"a repeated plain start answers with defmt: {repeated}")
        check_raw_log(service, config, answered, text)
    finally:
        service.close()
