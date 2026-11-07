"""The frames the defmt fixture image's log statements are exercised with.

One entry per statement in `firmware/src/main.rs`: its format string, the
arguments a board running that image would send, and for some statements a
second set that reaches another branch of the same rule (a negative value, an
enum's other variant, a value that crosses a unit boundary). `streams()` turns
them into the bytes on the wire for each fixture image; `expected-*.jsonl` is
what the official `defmt-decoder` 1.2 printed for exactly those bytes.
"""

from __future__ import annotations

from defmt_wire import Nested, Sequence, Slice, encode_frame, rzcobs_stream

from agentic_hil.defmt import DefmtTable

STATE = "Idle|Running {{ speed: {=u16:?} }}|Fault({=u8})"
READING = "Reading {{ channel: {=u8:?}, millivolts: {=i32:?} }}"
OPTION = "None|Some({=?})"


def statements(table: DefmtTable) -> list[tuple[str, list]]:
    flags = next(string for tag, string in table.entries.values() if tag == "defmt_bitflags")
    return [
        ("Hello, world!", []),
        ("unsigned {=u8} {=u16} {=u32} {=u64} {=u128}", [1, 2, 3, 4, 5]),
        ("unsigned {=u8} {=u16} {=u32} {=u64} {=u128}", [255, 65535, 4294967295, 2**64 - 1, 2**128 - 1]),
        ("signed {=i8} {=i16} {=i32} {=i64} {=i128}", [-1, -2, -3, -4, -5]),
        ("signed {=i8} {=i16} {=i32} {=i64} {=i128}", [-128, 32767, -(2**31), 2**63 - 1, -(2**127)]),
        ("sizes {=usize} {=isize}", [7, -8]),
        ("floats {=f32} {=f64}", [1.5, -0.25]),
        ("floats {=f32} {=f64}", [-0.0, 0.0]),
        ("bool {=bool} char {=char}", [True, "x"]),
        ("bool {=bool} char {=char}", [False, "\n"]),
        ("str {=str} istr {=istr}", ["dynamic", "interned"]),
        ("str {=str} istr {=istr}", ["", "interned"]),
        ("bytes {=[u8]} array {=[u8; 4]}", [[1, 2, 3], [9, 8, 7, 6]]),
        ("hex {=u32:x} HEX {=u32:#X} bin {=u8:#010b} oct {=u16:o} pad {=u16:05} sx {=i8:x}", [0xBEEF, 0xBEEF, 5, 8, 42, -1]),
        ("hex {=u32:x} HEX {=u32:#X} bin {=u8:#010b} oct {=u16:o} pad {=u16:05} sx {=i8:x}", [0, 0xFFFFFFFF, 255, 65535, 123456 % 65536, 127]),
        ("ascii {=[u8]:a}", [list(b'hi\n\x01 "\\\t\r\xff~')]),
        ("time {=u64:tus} secs {=u32:ms} tms {=u32:tms} ts {=u32:ts}", [3_723_000_001, 1234, 90_061_001, 90_061]),
        ("time {=u64:tus} secs {=u32:ms} tms {=u32:tms} ts {=u32:ts}", [0, 0, 0, 59]),
        ("dbg {=str:?}", ['q"uote\\\ttab\x01é']),
        ("struct {} enum {} {} {}", [Nested(READING, [2, -1200]), Nested(STATE, [], 0), Nested(STATE, [300], 1), Nested(STATE, [4], 2)]),
        ("option {} {} result {}", [Nested(OPTION, [Nested("{=u8}", [5])], 1), Nested(OPTION, [], 0), Nested("Err({=?})|Ok({=?})", [Nested("{=u16}", [9])], 1)]),
        ("option {} {} result {}", [Nested(OPTION, [], 0), Nested(OPTION, [Nested("{=u8}", [0])], 1), Nested("Err({=?})|Ok({=?})", [Nested("{=i8}", [-7])], 0)]),
        ("slice {=[?]} array {=[?; 3]}", [Slice("{=u16}", [[1], [2]]), Slice(STATE, [(0, []), (2, [1]), (1, [2])])]),
        ("manual {}", [Nested("{=__internal_FormatSequence}", [Sequence([("Manual(", []), ("{=u8}, {=u8:x}", [3, 255]), (")", [])])])]),
        ("flags {}", [Nested(flags, [0x81])]),
        ("flags {}", [Nested(flags, [0])]),
        ("flags {}", [Nested(flags, [0x02])]),
        ("bitfields {0=0..4} {0=4..8} {0=8..16:x} {0=12..16:a} next {1=u16:#x}", [0xAB5C, 0x4142]),
        ("wide bits {0=20..24} {0=1..2}", [0x00F0_0002]),
        ("display {} debug {}", [Nested("{=__internal_Display}", ["17"]), Nested("{=__internal_Debug}", ['"dbg"'])]),
        ("indexed {1=u8} {0=u8} {1=u8:x}", [10, 11]),
        ("prim format {} {} {}", [Nested("{=u8}", [42]), Nested("{=i32}", [-3]), Nested("{=f32}", [2.5])]),
        ("floats {=f32} {=f32} {=f32} {=f32} {=f64} {=f64}", [1.0e-7, float("nan"), float("inf"), 123456789.0, 1.0e300, 0.1]),
        ("floats {=f32} {=f32} {=f32} {=f32} {=f64} {=f64}", [0.1, -float("inf"), 3.4028234663852886e38, 1.0e13, 1.0e16, 1.0e-5]),
        ("floats {=f32} {=f32} {=f32} {=f32} {=f64} {=f64}", [1.0e-5, 1.0e-6, 1.401298464324817e-45, 16777216.0, 123.456, 1.0e15]),
        ("floats {=f32} {=f32} {=f32} {=f32} {=f64} {=f64}", [9.999999e12, 1.0e12, 0.3, 100.0, 5e-324, 1.7976931348623157e308]),
        ("escaped {{braces}} {=u8}", [1]),
        ("iso {=u64:iso8601ms}", [1_700_000_000_123]),
        ("iso {=u64:iso8601ms}", [0]),
        ("char {=char} {=char}", ["ä", "🦀"]),
        ("tuple {}", [Nested("({=?}, {=?})", [Nested("{=u8}", [1]), Nested("{=i16}", [-2])])]),
        ("println {=u8}", [99]),
        ("nested {}", [Nested(OPTION, [Nested(OPTION, [Nested(STATE, [2], 2)], 1)], 1)]),
        ("empty slice {=[?]} empty bytes {=[u8]}", [Slice("{=u8}", []), []]),
        ("str array {=[?]}", [Slice("{=str}", [["a"], ["bc"]])]),
        ("hexbytes {=[u8]:x} {=[u8; 2]:#04x}", [[0x0A, 0xFF], [1, 0x20]]),
    ]


def frames(table: DefmtTable) -> list[bytes]:
    out = []
    for number, (format_string, args) in enumerate(statements(table)):
        tags = ("defmt_println",) if format_string.startswith("println") else None
        out.append(encode_frame(table, format_string, args, timestamp=[number * 1_000_003], tags=tags))
    return out


def stream(table: DefmtTable) -> bytes:
    encoded = frames(table)
    if table.encoding == "rzcobs":
        return rzcobs_stream(encoded)
    return b"".join(encoded)


def error_stream(table: DefmtTable) -> bytes:
    """A stream with one of each broken frame between good ones, for an rzCOBS image.

    In order: text a bootloader printed before the firmware's first frame, a
    good frame, an index the table does not carry, a `bool` that carries 2, a
    frame cut short inside its arguments, bytes that are not rzCOBS, a nested
    format index naming a log statement, a good frame followed by bytes its
    format string does not account for, a good frame, and two bytes of a frame
    the stream ends inside of."""
    from defmt_wire import RzcobsEncoder, index_of

    encoder = RzcobsEncoder()
    good = encode_frame(table, "Hello, world!", [], timestamp=[7])
    bad_bool = bytearray(encode_frame(table, "bool {=bool} char {=char}", [True, "x"], timestamp=[1]))
    bad_bool[10] = 2
    truncated = encode_frame(table, "unsigned {=u8} {=u16} {=u32} {=u64} {=u128}", [1, 2, 3, 4, 5], timestamp=[1])[:-9]
    nested_log = (
        index_of(table, "prim format {} {} {}").to_bytes(2, "little") + bytes(8) + index_of(table, "Hello, world!").to_bytes(2, "little") + bytes(12)
    )
    return b"".join(
        [
            b"boot banner\r\n",
            encoder.frame(good),
            encoder.frame((0x7777).to_bytes(2, "little") + bytes(8)),
            encoder.frame(bytes(bad_bool)),
            encoder.frame(truncated),
            b"\x05\x00",
            encoder.frame(nested_log),
            encoder.frame(good + b"\x01\x02"),
            encoder.frame(good),
            b"\x12\x34",
        ]
    )
