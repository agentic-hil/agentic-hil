"""defmt frames decode natively, and the way the official decoder decodes them.

`agentic_hil.defmt` turns the bytes a defmt firmware sends into frames with a
level, a timestamp, a format string and typed arguments, without a host tool
printing text first. Its rendering has to agree with `defmt-decoder`, the
crate behind `defmt-print` and `probe-rs`, or a plan written against what a
developer sees in their terminal would not match what the bench sees.

The firmware source and the recorded streams and expectations are under
`tests/fixtures/defmt/`. The tests build each firmware variant from that source,
then hold the Python decoder to the records `defmt-decoder` 1.2 printed for
those bytes, without using a board.
"""

from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

import pytest
from defmt_wire import Nested, RzcobsEncoder, build_elf, defmt_symbol, encode_frame, rzcobs_stream

from agentic_hil import defmt
from agentic_hil.defmt import DefmtTable, DefmtTableError, decode_stream, format_float, load_table, rzcobs_decode

FIXTURES = Path(__file__).parent / "fixtures" / "defmt"
sys.path.insert(0, str(FIXTURES))
import scenario  # noqa: E402

STREAMS = [pytest.param("rzcobs", id="rzcobs"), pytest.param("raw", id="raw")]


def expected(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / f"expected-{name}.jsonl").read_text(encoding="utf-8").splitlines()]


def as_reference(record: dict) -> dict:
    """A record in the shape the reference harness prints it."""
    if "error" in record:
        return {"error": "malformed"}
    return {"index": record["index"], "level": record["level"], "timestamp": record.get("timestamp"), "message": record["message"], "line": record["text"]}


def write_elf(tmp_path: Path, symbols: list, sections: dict | None = None) -> Path:
    path = tmp_path / "image.elf"
    path.write_bytes(build_elf(symbols, sections))
    return path


def synthetic(entries: dict[int, tuple[str, str]], *, timestamp: str | None = None, encoding: str = "rzcobs") -> DefmtTable:
    return DefmtTable(entries=entries, timestamp=timestamp, encoding=encoding, version="4")


# ---------------------------------------------------------------------------
# The table.


def test_the_table_of_a_real_image_names_its_encoding_timestamp_and_statements(defmt_images: dict[str, Path]):
    table = load_table(defmt_images["rzcobs"])
    assert table.describe() == {
        "wire_version": "4",
        "encoding": "rzcobs",
        "timestamp_format": "{=u64:us}",
        "log_statements": {"trace": 1, "debug": 1, "info": 27, "warn": 1, "error": 1, "println": 1},
    }
    raw = load_table(defmt_images["raw"])
    assert (raw.encoding, raw.timestamp) == ("raw", None)
    # `bitflags!` values are read out of the section's own bytes, in definition order.
    assert [flags for flags in table.bitflags.values()] == [[("READY", 1), ("BUSY", 2), ("ERROR", 128)]]


def test_a_file_that_is_not_an_elf_is_refused_by_name(tmp_path):
    path = tmp_path / "firmware.bin"
    path.write_bytes(b"\x00\x10\x00\x20" * 64)
    with pytest.raises(DefmtTableError) as refused:
        load_table(path)
    assert refused.value.reason == "elf_unreadable"
    assert ".bin" in refused.value.summary


def test_a_missing_file_is_unreadable(tmp_path):
    with pytest.raises(DefmtTableError) as refused:
        load_table(tmp_path / "absent.elf")
    assert refused.value.reason == "elf_unreadable"


def test_an_elf_without_a_defmt_section_says_defmt_is_not_linked(tmp_path):
    path = write_elf(tmp_path, [("main", 0x08000400, 4, ".text")], {".text": bytes(8)})
    with pytest.raises(DefmtTableError) as refused:
        load_table(path)
    assert refused.value.reason == "table_missing"
    assert "-Tdefmt.x" in refused.value.summary


def test_a_wire_version_this_decoder_does_not_read_is_refused(tmp_path):
    path = write_elf(
        tmp_path,
        [("_defmt_version_ = 5", 1, 0, ".defmt"), ("_defmt_encoding_ = rzcobs", 2, 0, ".defmt"), (defmt_symbol("defmt_info", "x"), 3, 1, ".defmt")],
        {".defmt": bytes(4)},
    )
    with pytest.raises(DefmtTableError) as refused:
        load_table(path)
    assert refused.value.reason == "version_unsupported"


def test_a_defmt_section_whose_symbols_are_not_defmt_json_is_refused(tmp_path):
    path = write_elf(tmp_path, [("_defmt_version_ = 4", 1, 0, ".defmt"), ("_defmt_encoding_ = rzcobs", 2, 0, ".defmt"), ("not json", 3, 1, ".defmt")], {".defmt": bytes(4)})
    with pytest.raises(DefmtTableError) as refused:
        load_table(path)
    assert refused.value.reason == "table_invalid"


def test_the_quoted_version_symbol_lld_leaves_is_read(tmp_path):
    path = write_elf(
        tmp_path,
        [('"_defmt_version_ = 3"', 1, 0, ".defmt"), ("_defmt_encoding_ = raw", 2, 0, ".defmt"), (defmt_symbol("defmt_warn", "w {=u8}"), 3, 1, ".defmt")],
        {".defmt": bytes(4)},
    )
    table = load_table(path)
    assert (table.version, table.encoding, table.entries) == ("3", "raw", {3: ("defmt_warn", "w {=u8}")})


# ---------------------------------------------------------------------------
# Agreement with defmt-decoder on a real image.


@pytest.mark.parametrize("name", STREAMS)
def test_every_frame_decodes_to_the_line_defmt_decoder_printed(defmt_images: dict[str, Path], name: str):
    table = load_table(defmt_images[name])
    data = (FIXTURES / f"stream-{name}.bin").read_bytes()
    batch = decode_stream(table, data)
    assert batch.consumed == len(data)
    assert [as_reference(record) for record in batch.records] == expected(name)


@pytest.mark.parametrize("name", STREAMS)
def test_the_suite_encoder_writes_the_recorded_stream(defmt_images: dict[str, Path], name: str):
    """The frames the integration tests feed a fake port are the recorded ones."""
    assert scenario.stream(load_table(defmt_images[name])) == (FIXTURES / f"stream-{name}.bin").read_bytes()


def test_broken_frames_are_errors_exactly_where_defmt_decoder_found_them(defmt_images: dict[str, Path]):
    table = load_table(defmt_images["rzcobs"])
    data = (FIXTURES / "stream-errors.bin").read_bytes()
    batch = decode_stream(table, data)
    assert [as_reference(record) for record in batch.records] == expected("errors")
    # Where defmt-decoder only says "malformed", each error here names its cause.
    assert [record.get("error") for record in batch.records] == [
        "unknown_index",
        None,
        "unknown_index",
        "malformed",
        "truncated",
        "rzcobs_invalid",
        "malformed",
        None,
        None,
    ]
    assert batch.records[0]["raw_text"] == "boot banner\r\n"
    assert batch.records[7]["unconsumed_hex"] == "01020000"
    # The last two bytes are a frame still arriving: left for the next pass.
    assert batch.consumed == len(data) - 2
    final = decode_stream(table, data, final=True)
    assert final.consumed == len(data)
    assert final.records[-1] == {"error": "incomplete_frame", "detail": "the stream ended inside a frame, before its 0x00 separator", "size_bytes": 2, "raw_hex": "1234"}


@pytest.mark.parametrize(
    ("name", "chunk"),
    [pytest.param(name, chunk, id=f"{name}-{chunk}") for chunk in (1, 3, 7, 64) for name in ("rzcobs", "raw")],
)
def test_a_stream_that_arrives_in_pieces_decodes_to_the_same_frames(defmt_images: dict[str, Path], name: str, chunk: int):
    table = load_table(defmt_images[name])
    data = (FIXTURES / f"stream-{name}.bin").read_bytes()
    pending = b""
    records: list[dict] = []
    for start in range(0, len(data), chunk):
        pending += data[start : start + chunk]
        batch = decode_stream(table, pending)
        records.extend(batch.records)
        pending = pending[batch.consumed :]
    assert pending == b""
    assert records == decode_stream(table, data).records


# ---------------------------------------------------------------------------
# Structured arguments.


def frame_for(format_string: str, images: dict[str, Path]) -> dict:
    table = load_table(images["rzcobs"])
    for record in decode_stream(table, (FIXTURES / "stream-rzcobs.bin").read_bytes()).records:
        if record.get("format") == format_string:
            return record
    raise KeyError(format_string)


def test_arguments_are_json_values_in_the_order_the_firmware_passed_them(defmt_images: dict[str, Path]):
    record = frame_for("unsigned {=u8} {=u16} {=u32} {=u64} {=u128}", defmt_images)
    assert record["args"] == [1, 2, 3, 4, 5]
    assert (record["level"], record["timestamp"], record["timestamp_value"]) == ("debug", "1.000003", 1_000_003)
    assert frame_for("bytes {=[u8]} array {=[u8; 4]}", defmt_images)["args"] == [[1, 2, 3], [9, 8, 7, 6]]
    assert frame_for("bool {=bool} char {=char}", defmt_images)["args"] == [True, "x"]
    assert frame_for("str {=str} istr {=istr}", defmt_images)["args"] == ["dynamic", "interned"]
    # A bitfield's argument is the whole value the firmware passed, once.
    assert frame_for("bitfields {0=0..4} {0=4..8} {0=8..16:x} {0=12..16:a} next {1=u16:#x}", defmt_images)["args"] == [0xAB5C, 0x4142]


def test_a_float_reads_as_the_number_it_prints_and_a_non_finite_one_as_its_name(defmt_images: dict[str, Path]):
    record = frame_for("floats {=f32} {=f32} {=f32} {=f32} {=f64} {=f64}", defmt_images)
    assert record["args"] == [1e-7, "NaN", "inf", 123456790.0, 1e300, 0.1]
    assert json.dumps(record, allow_nan=False)


def test_nested_values_keep_their_format_and_primitives_unwrap(defmt_images: dict[str, Path]):
    record = frame_for("struct {} enum {} {} {}", defmt_images)
    assert record["args"] == [
        {"format": "Reading {{ channel: {=u8:?}, millivolts: {=i32:?} }}", "args": [2, -1200], "text": "Reading { channel: 2, millivolts: -1200 }"},
        {"format": "Idle", "args": [], "text": "Idle"},
        {"format": "Running {{ speed: {=u16:?} }}", "args": [300], "text": "Running { speed: 300 }"},
        {"format": "Fault({=u8})", "args": [4], "text": "Fault(4)"},
    ]
    assert frame_for("prim format {} {} {}", defmt_images)["args"] == [42, -3, 2.5]
    assert frame_for("slice {=[?]} array {=[?; 3]}", defmt_images)["args"][0] == [1, 2]
    assert frame_for("println {=u8}", defmt_images)["println"] is True
    assert frame_for("println {=u8}", defmt_images)["level"] is None


# ---------------------------------------------------------------------------
# Encodings and framing.

# The `defmt` crate's own rzCOBS encoder vectors (src/encoding/rzcobs.rs).
RZCOBS_VECTORS = [
    (b"", b"\x00"),
    (b"\x00", b"\x7f\x00"),
    (b"\x00" * 7, b"\x7f\x00"),
    (b"\x00" * 8, b"\x7f\x7f\x00"),
    (b"\x01", b"\x01\x7e\x00"),
    (b"\x00\x01", b"\x01\x7d\x00"),
    (b"\x01\x02", b"\x01\x02\x7c\x00"),
    (b"\x11\x22\x33\x44\x55\x66\x00", b"\x11\x22\x33\x44\x55\x66\x40\x00"),
    (b"\x11\x22\x33\x44\x55\x66\x77", b"\x11\x22\x33\x44\x55\x66\x77\x80\x00"),
    (b"\x11\x22\x33\x44\x55\x66\x77\x88", b"\x11\x22\x33\x44\x55\x66\x77\x88\x81\x00"),
    (b"\x00" * 13 + b"\xff", b"\x7f\xff\x3f\x00"),
    (b"\x00\x00\x00\x00\x00\x44" + b"\x00" * 7 + b"\xff", b"\x44\x5f\xff\x3f\x00"),
    (bytes(range(1, 0x86)), bytes(range(1, 0x86)) + b"\xfe\x00"),
    (bytes(range(1, 0x86)) + b"\x00", bytes(range(1, 0x86)) + b"\xfe\x00"),
    (bytes(range(1, 0x87)), bytes(range(1, 0x87)) + b"\xff\x00"),
]


@pytest.mark.parametrize(("plain", "encoded"), RZCOBS_VECTORS, ids=[f"v{index:02d}" for index in range(15)])
def test_rzcobs_matches_the_defmt_crates_own_vectors(plain, encoded):
    encoder = RzcobsEncoder()
    encoder.started = True
    assert encoder.frame(plain) == encoded
    # Decoding pads a frame's last group with zeros, never with anything else.
    decoded = rzcobs_decode(encoded[:-1])
    assert decoded[: len(plain)] == plain and not decoded[len(plain) :].strip(b"\x00")


def test_a_raw_stream_that_loses_sync_says_so_once_and_consumes_the_rest(defmt_images: dict[str, Path]):
    table = load_table(defmt_images["raw"])
    data = (FIXTURES / "stream-raw.bin").read_bytes()
    corrupted = data[:2] + b"\xee\xee" + data[4:]
    batch = decode_stream(table, corrupted)
    assert batch.desynchronized is True
    assert batch.consumed == len(corrupted)
    assert batch.records[-1]["error"] == "unknown_index"
    assert "no frame boundaries" in batch.records[-1]["detail"]


def test_a_raw_stream_waits_for_the_rest_of_a_frame(defmt_images: dict[str, Path]):
    table = load_table(defmt_images["raw"])
    data = (FIXTURES / "stream-raw.bin").read_bytes()
    # "Hello, world!" is its two index bytes alone; the next frame needs 33.
    first = decode_stream(table, data[:5])
    assert ([record["message"] for record in first.records], first.consumed) == (["Hello, world!"], 2)
    assert decode_stream(table, data[:-1], final=True).records[-1]["error"] == "incomplete_frame"


# ---------------------------------------------------------------------------
# Inputs nothing well-formed produces.


def test_nesting_deeper_than_the_cap_is_malformed_not_a_recursion_error():
    table = synthetic({1: ("defmt_prim", "{=?}"), 2: ("defmt_info", "{}")})
    frame = struct.pack("<H", 2) + struct.pack("<H", 1) * 500
    record = decode_stream(table, rzcobs_stream([frame])).records[0]
    assert record["error"] == "malformed" and "deeper" in record["detail"]


def test_a_slice_claiming_more_zero_sized_elements_than_a_frame_can_hold_is_malformed():
    table = synthetic({1: ("defmt_derived", "Unit"), 2: ("defmt_info", "{=[?]}")})
    frame = struct.pack("<H", 2) + struct.pack("<I", 0xFFFFFFFF) + struct.pack("<H", 1)
    record = decode_stream(table, rzcobs_stream([frame])).records[0]
    assert record["error"] == "malformed" and "elements" in record["detail"]


def test_a_slice_of_zero_sized_elements_within_the_allowance_decodes():
    table = synthetic({1: ("defmt_derived", "Unit"), 2: ("defmt_info", "{=[?]}")})
    frame = struct.pack("<H", 2) + struct.pack("<I", 3) + struct.pack("<H", 1)
    assert decode_stream(table, rzcobs_stream([frame])).records[0]["message"] == "[Unit, Unit, Unit]"


def test_a_string_length_past_the_end_of_its_frame_is_truncated_not_read_beyond():
    table = synthetic({1: ("defmt_info", "{=str}")})
    frame = struct.pack("<H", 1) + struct.pack("<I", 1_000_000) + b"abc"
    assert decode_stream(table, rzcobs_stream([frame])).records[0]["error"] == "truncated"


@pytest.mark.parametrize(
    ("format_string", "payload", "error"),
    [
        ("{=char}", struct.pack("<I", 0xD800), "malformed"),
        ("{=str}", struct.pack("<I", 2) + b"\xff\xfe", "malformed"),
        ("{=?}", struct.pack("<H", 9), "unknown_index"),
    ],
    ids=["char", "str", "nested"],
)
def test_values_no_firmware_sends_are_named(format_string, payload, error):
    table = synthetic({1: ("defmt_info", format_string)})
    assert decode_stream(table, rzcobs_stream([struct.pack("<H", 1) + payload])).records[0]["error"] == error


def test_an_enum_discriminant_past_its_variants_is_malformed():
    table = synthetic({1: ("defmt_derived", "A|B"), 2: ("defmt_info", "{}")})
    record = decode_stream(table, rzcobs_stream([struct.pack("<H", 2) + struct.pack("<H", 1) + b"\x05"])).records[0]
    assert record["error"] == "malformed" and "discriminant" in record["detail"]


def test_an_error_record_carries_a_bounded_slice_of_its_bytes():
    table = synthetic({1: ("defmt_info", "x")})
    record = decode_stream(table, rzcobs_stream([b"\x09\x09" + bytes(range(1, 200))])).records[0]
    assert record["error"] == "unknown_index"
    assert len(bytes.fromhex(record["raw_hex"])) == defmt.ERROR_HEX_BYTES and record["raw_truncated"] is True


# ---------------------------------------------------------------------------
# Rendering details the reference stream does not reach.


@pytest.mark.parametrize(
    ("value", "single", "text"),
    [
        (1.0, True, "1.0"),
        (0.0001, False, "0.0001"),
        (-2.5e-8, False, "-2.5e-8"),
        (1.25e17, False, "1.25e17"),
        (3.0e38, True, "3e38"),
        (1.17549435e-38, True, "1.1754944e-38"),
        (float("-nan"), True, "NaN"),
    ],
    ids=[f"f{index}" for index in range(7)],
)
def test_floats_render_the_way_ryu_renders_them(value, single, text):
    if single:
        value = struct.unpack("<f", struct.pack("<f", value))[0]
    assert format_float(value, single=single) == text


def test_every_shortest_f32_round_trips_through_single_precision():
    for bits in range(0x00000001, 0x7F800000, 0x00F3A7C1):
        value = struct.unpack("<f", struct.pack("<I", bits))[0]
        text = format_float(value, single=True)
        assert struct.pack("<f", float(text)) == struct.pack("<I", bits), (bits, text)
        assert math.isfinite(float(text))


def test_a_format_string_that_does_not_parse_makes_its_frame_malformed():
    table = synthetic({1: ("defmt_info", "{=nonsense}")})
    record = decode_stream(table, rzcobs_stream([struct.pack("<H", 1)])).records[0]
    assert record["error"] == "malformed" and "does not parse" in record["detail"]


def test_a_frame_names_its_unknown_display_hint_and_still_renders():
    table = synthetic({1: ("defmt_info", "{=u8:future}")})
    assert decode_stream(table, rzcobs_stream([struct.pack("<H", 1) + b"\x07"])).records[0]["message"] == "7"


def test_counts_describe_what_a_batch_holds(defmt_images: dict[str, Path]):
    table = load_table(defmt_images["rzcobs"])
    batch = decode_stream(table, (FIXTURES / "stream-errors.bin").read_bytes())
    assert defmt.describe_records(batch.records) == {"frames": 3, "errors": {"unknown_index": 2, "malformed": 2, "truncated": 1, "rzcobs_invalid": 1}}


def test_the_suite_encoder_round_trips_a_nested_enum(defmt_images: dict[str, Path]):
    table = load_table(defmt_images["rzcobs"])
    frame = encode_frame(table, "nested {}", [Nested(scenario.OPTION, [Nested(scenario.OPTION, [], 0)], 1)], timestamp=[5])
    assert decode_stream(table, rzcobs_stream([frame])).records[0]["text"] == "0.000005 INFO nested Some(None)"
