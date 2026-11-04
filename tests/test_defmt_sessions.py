"""A COM session reads defmt natively: decoded frames, from the table of the flashed ELF.

A defmt firmware sends indices and raw arguments, so the line is unreadable as
text and a host tool usually decodes it into text that is then grepped.
`com_session_start` with `defmt` makes the session decode it itself:
`com_read` hands out whole frames, with level, timestamp, format string and
typed arguments, and `until` waits for a frame whose rendered line contains
what it names. `flash_firmware` with `capture.defmt` flashes, resets and hands
back the decoded boot output in one call.

The table comes from the ELF this service flashed, while it is still the file
that was flashed, because a table from any other build decodes into messages
the firmware never sent. Each refusal for a missing, changed or unusable table
is asked for here, before any port is opened.

The board is faked at the hardware boundary (`test_flash_capture.Board`), and
the bytes it sends are the recorded frames of the real fixture image under
`tests/fixtures/defmt/`. No board, port or probe is touched.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config
from defmt_wire import RzcobsEncoder, encode_frame
from test_flash_capture import Board, capture_of, service_for

from agentic_hil.config import load_config
from agentic_hil.defmt import load_table
from agentic_hil.report import overall_success

# Device locks are machine-wide: this port and device are this file's alone.
PORT_ID = "defmt_uart"
DEVICE = "/dev/ttyDEFMTSESSION0"

FIXTURES = Path(__file__).parent / "fixtures" / "defmt"
RZCOBS_ELF = FIXTURES / "defmt-fixture-rzcobs.elf"
RAW_ELF = FIXTURES / "defmt-fixture-raw.elf"
STREAM = (FIXTURES / "stream-rzcobs.bin").read_bytes()
EXPECTED = [json.loads(line) for line in (FIXTURES / "expected-rzcobs.jsonl").read_text(encoding="utf-8").splitlines()]
IMAGE = "build/app.elf"


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch):
    fake = Board()
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=fake.construct))
    yield fake
    fake.finish()


def config_for(workspace: Path):
    return load_config(str(write_config(workspace, com_ports_yaml=f'com_ports:\n  {PORT_ID}:\n    device: "{DEVICE}"\n')))


def place_image(workspace: Path, source: Path = RZCOBS_ELF, name: str = IMAGE) -> str:
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return name


def texts(frames: list[dict]) -> list[str]:
    return [frame.get("text", f"<{frame.get('error')}>") for frame in frames]


def flashed(tmp_path: Path, board: Board, banner: bytes = b"", **options):
    """A service that has flashed the fixture image, and the board it flashed."""
    image = place_image(tmp_path)
    service, backend = service_for(config_for(tmp_path), board, banner=banner, **options)
    result = service.call("flash_firmware", {"image_path": image, "reset_after_flash": True})
    assert overall_success(result), result
    return service


def read_all(service, **arguments) -> list[dict]:
    result = service.call("com_read", {"port_id": PORT_ID, **arguments})
    assert result["ok"] is True, result
    return result["frames"]


# ---------------------------------------------------------------------------
# A session decodes with the flashed ELF's table.


def test_a_defmt_session_reads_the_frames_defmt_print_would_print(tmp_path: Path, board: Board) -> None:
    service = flashed(tmp_path, board)
    try:
        started = service.call("com_session_start", {"port_id": PORT_ID, "defmt": True})
        assert started["ok"] is True, started
        described = started["session"]["defmt"]
        assert described["table_source"] == "flashed_image" and described["proven"] is True
        assert described["image_path"] == IMAGE and described["encoding"] == "rzcobs"
        board.transmit(STREAM)
        frames = read_all(service, wait_timeout_s=1)
        assert texts(frames) == [record["line"] for record in EXPECTED]
        assert frames[1]["args"] == [1, 2, 3, 4, 5]
    finally:
        service.close()


def test_a_frame_split_across_two_reads_is_decoded_once_by_the_read_that_sees_its_end(tmp_path: Path, board: Board) -> None:
    service = flashed(tmp_path, board)
    try:
        assert service.call("com_session_start", {"port_id": PORT_ID, "defmt": True})["ok"] is True
        table = load_table(RZCOBS_ELF)
        encoder = RzcobsEncoder()
        first = encoder.frame(encode_frame(table, "Hello, world!", [], timestamp=[1]))
        second = encoder.frame(encode_frame(table, "sizes {=usize} {=isize}", [7, -8], timestamp=[2]))
        board.transmit(first + second[:3])
        result = service.call("com_read", {"port_id": PORT_ID})
        assert texts(result["frames"]) == ["0.000001 INFO Hello, world!"]
        assert result["buffer_remaining_bytes"] == 3
        board.transmit(second[3:])
        assert texts(read_all(service)) == ["0.000002 WARN sizes 7 -8"]
    finally:
        service.close()


def test_until_waits_for_a_frame_and_leaves_the_ones_after_it_buffered(tmp_path: Path, board: Board) -> None:
    service = flashed(tmp_path, board)
    try:
        assert service.call("com_session_start", {"port_id": PORT_ID, "defmt": True})["ok"] is True
        board.transmit(STREAM)
        result = service.call("com_read", {"port_id": PORT_ID, "until": "WARN sizes", "wait_timeout_s": 2})
        assert result["until_matched"] is True and result["matched"] == "WARN sizes"
        assert texts(result["frames"])[-1] == "5.000015 WARN sizes 7 -8"
        assert len(result["frames"]) == 6
        assert "until" not in result and "until_wait_s" not in result
        rest = read_all(service)
        assert texts(rest)[0] == "6.000018 ERROR floats 1.5 -0.25"
    finally:
        service.close()


def test_a_session_without_defmt_still_reads_text(tmp_path: Path, board: Board) -> None:
    service = flashed(tmp_path, board)
    try:
        assert "defmt" not in service.call("com_session_start", {"port_id": PORT_ID})["session"]
        board.transmit(b"plain\r\n")
        result = service.call("com_read", {"port_id": PORT_ID})
        assert result["data"]["text"] == "plain\r\n" and "frames" not in result
    finally:
        service.close()


def test_a_repeat_start_replaces_how_the_line_is_read(tmp_path: Path, board: Board) -> None:
    service = flashed(tmp_path, board)
    try:
        assert service.call("com_session_start", {"port_id": PORT_ID})["ok"] is True
        again = service.call("com_session_start", {"port_id": PORT_ID, "defmt": True})
        assert again["already_active"] is True and again["session"]["defmt"]["proven"] is True
        board.transmit(STREAM[:40])
        assert read_all(service)[0]["text"] == "0.000000 INFO Hello, world!"
    finally:
        service.close()


# ---------------------------------------------------------------------------
# What a table has to be, asked before any port is opened.


def test_without_a_flashed_elf_the_session_is_refused_and_no_port_is_opened(tmp_path: Path, board: Board) -> None:
    service, _ = service_for(config_for(tmp_path), board)
    try:
        refused = service.call("com_session_start", {"port_id": PORT_ID, "defmt": True})
        assert refused["error_type"] == "defmt_table_not_available", refused
        assert refused["remediation"] and board.opens == 0
    finally:
        service.close()


def test_an_elf_rebuilt_after_the_flash_is_refused_as_changed(tmp_path: Path, board: Board) -> None:
    service = flashed(tmp_path, board)
    try:
        place_image(tmp_path, RAW_ELF)
        refused = service.call("com_session_start", {"port_id": PORT_ID, "defmt": True})
        assert (refused["error_type"], refused["reason"]) == ("defmt_table_changed", "changed"), refused
        assert board.opens == 0
    finally:
        service.close()


def test_a_named_elf_decodes_and_says_it_is_not_proven(tmp_path: Path, board: Board) -> None:
    service, _ = service_for(config_for(tmp_path), board)
    try:
        image = place_image(tmp_path, RZCOBS_ELF, "build/other.elf")
        started = service.call("com_session_start", {"port_id": PORT_ID, "defmt": {"image_path": image}})
        assert started["ok"] is True, started
        assert (started["session"]["defmt"]["table_source"], started["session"]["defmt"]["proven"]) == ("image_path", False)
        board.transmit(STREAM[:40])
        frames = read_all(service)
        assert frames[0]["text"] == "0.000000 INFO Hello, world!"
    finally:
        service.close()


def test_the_flashed_file_named_by_path_is_proven(tmp_path: Path, board: Board) -> None:
    service = flashed(tmp_path, board)
    try:
        started = service.call("com_session_start", {"port_id": PORT_ID, "defmt": {"image_path": IMAGE}})
        assert started["session"]["defmt"]["proven"] is True, started
    finally:
        service.close()


def test_an_elf_without_a_defmt_section_is_refused_with_the_reason(tmp_path: Path, board: Board) -> None:
    from defmt_wire import build_elf

    service, _ = service_for(config_for(tmp_path), board)
    try:
        target = tmp_path / "build" / "plain.elf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(build_elf([("main", 0x08000400, 4, ".text")], {".text": bytes(8)}))
        refused = service.call("com_session_start", {"port_id": PORT_ID, "defmt": {"image_path": "build/plain.elf"}})
        assert (refused["error_type"], refused["reason"]) == ("defmt_table_invalid", "table_missing"), refused
        assert "-Tdefmt.x" in refused["summary"] and board.opens == 0
    finally:
        service.close()


def test_a_named_file_that_is_no_elf_is_refused_as_a_flash_image_would_be(tmp_path: Path, board: Board) -> None:
    service, _ = service_for(config_for(tmp_path), board)
    try:
        target = tmp_path / "build" / "fake.elf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00\x10\x00\x20" * 64)
        refused = service.call("com_session_start", {"port_id": PORT_ID, "defmt": {"image_path": "build/fake.elf"}})
        assert (refused["error_type"], refused["field"]) == ("artifact_validation_failed", "defmt.image_path"), refused
        assert board.opens == 0
    finally:
        service.close()


def test_a_named_elf_outside_the_workspace_is_refused_as_a_flash_image_would_be(tmp_path: Path, board: Board) -> None:
    service, _ = service_for(config_for(tmp_path / "workspace"), board)
    try:
        outside = tmp_path / "elsewhere.elf"
        shutil.copyfile(RZCOBS_ELF, outside)
        refused = service.call("com_session_start", {"port_id": PORT_ID, "defmt": {"image_path": str(outside)}})
        assert refused["ok"] is False and refused["error_type"] == "artifact_validation_failed", refused
        assert refused["field"] == "defmt.image_path" and board.opens == 0
    finally:
        service.close()


@pytest.mark.parametrize("defmt", ["yes", {"path": "x.elf"}, {"image_path": ""}, 1])
def test_a_defmt_argument_outside_its_shape_is_refused_by_the_schema(tmp_path: Path, board: Board, defmt: object) -> None:
    service, _ = service_for(config_for(tmp_path), board)
    try:
        refused = service.call("com_session_start", {"port_id": PORT_ID, "defmt": defmt})
        assert refused["error_type"] == "invalid_argument", refused
        assert board.opens == 0
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Lines that do not carry what the table describes.


def test_a_session_opened_mid_frame_says_its_first_broken_record_may_be_why(tmp_path: Path, board: Board) -> None:
    service = flashed(tmp_path, board)
    try:
        assert service.call("com_session_start", {"port_id": PORT_ID, "defmt": True})["ok"] is True
        table = load_table(RZCOBS_ELF)
        encoder = RzcobsEncoder()
        cut = encoder.frame(encode_frame(table, "unsigned {=u8} {=u16} {=u32} {=u64} {=u128}", [255, 65535, 2**32 - 1, 2**64 - 1, 2**128 - 1], timestamp=[1]))
        whole = encoder.frame(encode_frame(table, "Hello, world!", [], timestamp=[2]))
        board.transmit(cut[len(cut) // 2 :] + whole)
        result = service.call("com_read", {"port_id": PORT_ID})
        assert "error" in result["frames"][0] and result["frames"][0]["at_session_start"] is True
        assert texts(result["frames"])[1:] == ["0.000002 INFO Hello, world!"]
        # Only the first record a session hands out can have started mid-frame.
        board.transmit(cut[len(cut) // 2 :])
        assert "at_session_start" not in read_all(service)[0]
    finally:
        service.close()


def test_text_on_a_defmt_line_comes_back_as_text_and_never_stalls_the_reads(tmp_path: Path, board: Board) -> None:
    service = flashed(tmp_path, board)
    try:
        assert service.call("com_session_start", {"port_id": PORT_ID, "defmt": True})["ok"] is True
        board.transmit(b"bootloader v2 waiting...\r\n")
        result = service.call("com_read", {"port_id": PORT_ID, "max_bytes": 16})
        assert result["frames"][0]["error"] == "oversized_frame"
        assert result["frames"][0]["raw_text"] == "bootloader v2 wa"
        assert result["bytes_read"] == 16
    finally:
        service.close()


def test_frames_from_another_build_say_how_that_usually_happens(tmp_path: Path, board: Board) -> None:
    service = flashed(tmp_path, board)
    try:
        assert service.call("com_session_start", {"port_id": PORT_ID, "defmt": True})["ok"] is True
        board.transmit(RzcobsEncoder().frame(b"\xee\xee" + bytes(8)))
        result = service.call("com_read", {"port_id": PORT_ID})
        assert result["frames"][0]["error"] == "unknown_index"
        assert "another build" in result["defmt"]["advice"]
        assert result["defmt"]["errors"] == {"unknown_index": 1}
    finally:
        service.close()


def test_one_read_hands_out_a_bounded_number_of_frames(tmp_path: Path, board: Board) -> None:
    from agentic_hil.comports import DEFMT_MAX_RECORDS_PER_READ

    service = flashed(tmp_path, board)
    try:
        assert service.call("com_session_start", {"port_id": PORT_ID, "defmt": True})["ok"] is True
        table = load_table(RZCOBS_ELF)
        encoder = RzcobsEncoder()
        board.transmit(b"".join(encoder.frame(encode_frame(table, "Hello, world!", [], timestamp=[number])) for number in range(DEFMT_MAX_RECORDS_PER_READ + 10)))
        first = service.call("com_read", {"port_id": PORT_ID})
        assert len(first["frames"]) == DEFMT_MAX_RECORDS_PER_READ and first["defmt"]["more_frames_buffered"] is True
        assert len(read_all(service)) == 10
    finally:
        service.close()


# ---------------------------------------------------------------------------
# flash_firmware with capture.defmt.


def test_a_capture_decodes_the_boot_output_with_the_table_of_the_image_it_flashed(tmp_path: Path, board: Board) -> None:
    image = place_image(tmp_path)
    service, _ = service_for(config_for(tmp_path), board, banner=STREAM)
    try:
        result = service.call("flash_firmware", {"image_path": image, "reset_after_flash": True, "capture": {"port_id": PORT_ID, "defmt": True, "until": "println 99", "wait_timeout_s": 2}})
        assert overall_success(result), result
        capture = capture_of(result)
        assert capture["until_matched"] is True
        assert texts(capture["frames"])[-1] == "41.000123 println 99"
        assert capture["defmt"]["table"]["proven"] is True and "data" not in capture
    finally:
        service.close()


def test_a_capture_of_an_image_without_a_table_flashes_nothing(tmp_path: Path, board: Board) -> None:
    target = tmp_path / "build" / "app.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00\x10\x00\x20" * 64)
    service, backend = service_for(config_for(tmp_path), board)
    try:
        result = service.call("flash_firmware", {"image_path": "build/app.bin", "reset_after_flash": True, "capture": {"port_id": PORT_ID, "defmt": True}})
        assert (result["error_type"], result["reason"], result["field"]) == ("defmt_table_invalid", "elf_unreadable", "capture.defmt"), result
        assert result["side_effect_status"] == "not_started"
        assert backend.calls == [] and board.opens == 0
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Test plans: `uart_open` with `defmt`, and `defmt_expect`.


def write_plan(workspace: Path, text: str) -> Path:
    path = workspace / ".agentic-hil" / "testconfig.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def run_plan(service, workspace: Path, text: str) -> dict:
    from agentic_hil.test_reactor import TestReactor, load_test_config

    return TestReactor(service.config, service).run(load_test_config(str(write_plan(workspace, text)), str(workspace)))


BOOT_PLAN = f"""version: 7
name: defmt-boot
steps:
  - {{device: dut, action: flash, image_path: {IMAGE}}}
  - {{device: {PORT_ID}, action: uart_open, defmt: true}}
  - {{device: dut, action: reset}}
  - {{device: {PORT_ID}, action: defmt_expect, level: info, text: "Hello", timeout_s: 5}}
  - {{device: {PORT_ID}, action: defmt_expect, format: "sizes {{=usize}} {{=isize}}", args: [{{equals: 7}}, {{min: -10, max: 0}}], timeout_s: 5}}
  - {{device: {PORT_ID}, action: defmt_expect, level: error, args: [{{min: 1.0, max: 2.0}}, null], timeout_s: 5}}
  - {{device: {PORT_ID}, action: defmt_expect, format: "bool {{=bool}} char {{=char}}", args: [{{equals: false}}], timeout_s: 5}}
"""


def test_a_plan_flashes_opens_the_line_as_defmt_and_waits_for_frames_by_what_they_are(tmp_path: Path, board: Board) -> None:
    place_image(tmp_path)
    service, _ = service_for(config_for(tmp_path), board, banner=STREAM)
    try:
        result = run_plan(service, tmp_path, BOOT_PLAN)
        assert result["ok"] is True, result
        steps = [step["result"] for step in result["steps"]]
        assert steps[3]["frame"]["text"] == "0.000000 INFO Hello, world!"
        assert steps[4]["frame"]["args"] == [7, -8]
        assert steps[5]["frame"]["text"] == "6.000018 ERROR floats 1.5 -0.25"
        # Passed over: the second `floats` error, then `bool true`, which holds
        # `true` and not `false` (and is not 0 either); the next frame with
        # that format is the one that matches.
        assert steps[6]["frame"]["args"] == [False, "\n"] and steps[6]["frames_passed_over"] == 2
        # One reset, one burst: every step after the first matched frames the
        # first step's read had already taken off the line.
        assert [step["reads"] for step in steps[3:]] == [1, 0, 0, 0]
    finally:
        service.close()


def test_a_frame_that_never_comes_fails_with_the_frames_that_did(tmp_path: Path, board: Board) -> None:
    place_image(tmp_path)
    service, _ = service_for(config_for(tmp_path), board, banner=STREAM[:60])
    try:
        result = run_plan(
            service,
            tmp_path,
            f"""version: 7
steps:
  - {{device: dut, action: flash, image_path: {IMAGE}}}
  - {{device: {PORT_ID}, action: uart_open, defmt: true}}
  - {{device: dut, action: reset}}
  - {{device: {PORT_ID}, action: defmt_expect, level: warn, pattern: "sizes \\\\d+", timeout_s: 0.3}}
""",
        )
        assert result["ok"] is False
        failed = result["steps"][-1]["result"]
        assert failed["error_type"] == "defmt_expect_timeout", failed
        assert failed["last_frames"][0] == "0.000000 INFO Hello, world!"
        assert failed["expected"] == {"level": "warn", "pattern": "sizes \\d+"}
    finally:
        service.close()


@pytest.mark.parametrize(
    ("steps", "refused_action"),
    [
        (f"  - {{device: {PORT_ID}, action: uart_open}}\n  - {{device: {PORT_ID}, action: defmt_expect, level: info, timeout_s: 1}}\n", "defmt_expect"),
        (f"  - {{device: {PORT_ID}, action: uart_open, defmt: true}}\n  - {{device: {PORT_ID}, action: uart_expect, text: x, timeout_s: 1}}\n", "uart_expect"),
        (f"  - {{device: {PORT_ID}, action: uart_open, defmt: true}}\n  - {{device: {PORT_ID}, action: uart_read, comparator: {{equals: x}}, timeout_s: 1}}\n", "uart_read"),
    ],
    ids=["defmt-on-a-text-line", "text-on-a-defmt-line", "comparator-on-a-defmt-line"],
)
def test_a_line_read_the_wrong_way_is_refused_before_the_run(tmp_path: Path, board: Board, steps: str, refused_action: str) -> None:
    service, _ = service_for(config_for(tmp_path), board)
    try:
        result = run_plan(service, tmp_path, "version: 7\nsteps:\n" + steps)
        assert result["ok"] is False and result["steps"] == [], result
        assert result["error_type"] == "test_config_invalid", result
        assert result["validation_error"]["action"] == refused_action
        assert board.opens == 0
    finally:
        service.close()


@pytest.mark.parametrize(
    ("step", "field"),
    [
        ("{action: defmt_expect, timeout_s: 1}", "steps[1]"),
        ('{action: defmt_expect, text: a, pattern: b, timeout_s: 1}', "steps[1]"),
        ("{action: defmt_expect, args: [{equals: 1, min: 0}], timeout_s: 1}", "steps[1].args[0]"),
        ('{action: defmt_expect, pattern: "(", timeout_s: 1}', "steps[1].pattern"),
    ],
    ids=["no-claim", "text-and-pattern", "equals-and-range", "pattern-that-does-not-compile"],
)
def test_a_defmt_expect_outside_its_shape_is_refused_before_the_run(tmp_path: Path, board: Board, step: str, field: str) -> None:
    from agentic_hil.config import ConfigError
    from agentic_hil.test_reactor import TestReactor, load_test_config

    service, _ = service_for(config_for(tmp_path), board)
    try:
        text = f"version: 7\nsteps:\n  - {{device: {PORT_ID}, action: uart_open, defmt: true}}\n  - {{device: {PORT_ID}, " + step[1:] + "\n"
        try:
            result = TestReactor(service.config, service).run(load_test_config(str(write_plan(tmp_path, text)), str(tmp_path)))
        except ConfigError as refused:
            result = {"ok": False, **refused.to_dict()}
        named = result.get("field") or (result.get("validation_error") or {}).get("field", "")
        assert result["ok"] is False and str(named).startswith(field), result
        assert board.opens == 0
    finally:
        service.close()


def test_a_version_6_plan_cannot_reach_for_defmt(tmp_path: Path) -> None:
    from agentic_hil.config import ConfigError
    from agentic_hil.test_reactor import load_test_config

    text = f"version: 6\nsteps:\n  - {{device: {PORT_ID}, action: uart_open, defmt: true}}\n"
    with pytest.raises(ConfigError) as refused:
        load_test_config(str(write_plan(tmp_path, text)), str(tmp_path))
    assert refused.value.error_type == "test_config_invalid"
    assert "7" in json.dumps(refused.value.to_dict())
