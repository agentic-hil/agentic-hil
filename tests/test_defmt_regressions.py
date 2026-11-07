"""Decoder-only hostile input and boundary acceptance, without hardware or Rust."""

from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from defmt_wire import RzcobsEncoder, build_elf, defmt_symbol, rzcobs_stream
from support import scaled_time_bound

from agentic_hil import defmt, elfsymbols
from agentic_hil.defmt import DefmtTable, DefmtTableError, decode_stream, load_table, load_table_bytes

FIXTURES = Path(__file__).parent / "fixtures/defmt"


def _table(fmt, encoding="rzcobs", version="4", *, timestamp=None, extra=None):
    return DefmtTable({1: ("defmt_info", fmt), 2: ("defmt_prim", "{=u8}"), 3: ("defmt_info", "ok {=u8}"), **(extra or {})}, timestamp, encoding, version)


def _wire(frames, encoding):
    return rzcobs_stream(frames) if encoding == "rzcobs" else b"".join(frames)


def _encoded(frame):
    encoder = RzcobsEncoder()
    encoder.started = True
    return encoder.frame(frame)


def _decode_in_child(fmt, encoding, wire, version="4"):
    """Keep hostile allocation/shift regressions out of the pytest worker."""
    script = "\n".join([
        "import json, os, sys", "from pathlib import Path", "from agentic_hil import defmt", "from agentic_hil.defmt import DefmtTable, decode_stream",
        "assert Path(defmt.__file__).resolve().is_relative_to(Path(os.environ['PYTHONPATH']).resolve())",
        "fmt, encoding, wire, version = json.loads(sys.stdin.read())",
        "table = DefmtTable({1: ('defmt_info', fmt), 2: ('defmt_prim', '{=u8}'), 3: ('defmt_info', 'ok {=u8}')}, None, encoding, version)",
        "batch = decode_stream(table, bytes.fromhex(wire))",
        "print(json.dumps([batch.records, batch.consumed, batch.desynchronized, batch.ends]))",
    ])
    child = subprocess.run([sys.executable, "-c", script], input=json.dumps([fmt, encoding, wire.hex(), version]), text=True, capture_output=True, check=True, timeout=scaled_time_bound(15), env={**os.environ, "PYTHONPATH": str(Path(defmt.__file__).resolve().parents[1])})
    records, consumed, desynchronized, ends = json.loads(child.stdout)
    return defmt.DecodeBatch(records, consumed, desynchronized, ends)


def _assert_good(batch, wire, messages):
    assert batch.consumed == len(wire) and not batch.desynchronized, batch
    assert [record.get("message") for record in batch.records] == messages, batch
    assert all("error" not in record for record in batch.records), batch
    assert len(batch.ends) == len(messages) and batch.ends[-1] == len(wire)


def _assert_malformed(batch, wire, encoding, rejected):
    assert batch.consumed == len(wire), batch
    record = batch.records[0]
    assert record["error"] == "malformed", batch
    assert 0 < len(record["detail"]) <= 512, record
    received = rejected if encoding == "rzcobs" else wire
    assert record["size_bytes"] == len(received)
    assert record["raw_hex"] == received[:64].hex()
    assert record.get("raw_truncated", False) == (len(received) > 64)
    if encoding == "rzcobs":
        assert len(batch.records) == 2 and not batch.desynchronized, batch
        assert batch.records[1]["message"] == "ok 42", batch
        assert batch.ends == [len(rejected) + 2, len(wire)]
    else:
        assert batch.desynchronized and len(batch.records) == 1, batch
        assert batch.ends == [len(wire)]


def _image(version="4", encoding="rzcobs", address=0):
    suffix = "@app" if version == "4" else ""
    def symbol(tag, data):
        value = json.loads(defmt_symbol(tag, data))
        if version == "3":
            del value["crate_name"]
        return json.dumps(value, separators=(",", ":"))
    symbols = [
        (f'"_defmt_version_ = {version}"' if version == "3" else f"_defmt_version_ = {version}", 0, 0, None),
        (f"_defmt_encoding_ = {encoding}", 0, 0, None),
        (symbol("defmt_info", "flags {=u8:__internal_bitflags_Flags@app@1" + suffix + "}"), 3, 1, ".defmt"),
        (symbol("defmt_bitflags_value", "Flags::0::READY"), address + 16, 16, ".defmt"),
        (symbol("defmt_bitflags_value", "Flags::1::BUSY"), address + 32, 16, ".defmt"),
        ("main", 0x08000400, 4, ".text"),
    ]
    data = bytearray(build_elf(symbols, {".defmt": bytes(16) + (1).to_bytes(16, "little") + (2).to_bytes(16, "little"), ".text": bytes(8)}))
    shoff = struct.unpack_from("<I", data, 32)[0]
    struct.pack_into("<I", data, shoff + 40 + 12, address)
    return bytes(data)


def _section(data, index):
    shoff = struct.unpack_from("<I", data, 32)[0]
    return struct.unpack_from("<10I", data, shoff + index * 40)


def test_load_table_bytes_agrees_with_path_and_is_path_independent(tmp_path):
    retained = load_table_bytes((FIXTURES / "defmt-fixture-rzcobs.elf").read_bytes())
    for encoding in ("rzcobs", "raw"):
        source = FIXTURES / f"defmt-fixture-{encoding}.elf"
        from_bytes = retained if encoding == "rzcobs" else load_table_bytes(source.read_bytes())
        from_path = load_table(source)
        assert from_bytes == from_path
        assert from_bytes.describe() == from_path.describe()
        assert (from_bytes.timestamp, from_bytes.encoding, from_bytes.version, from_bytes.bitflags) == (from_path.timestamp, encoding, from_path.version, from_path.bitflags)
        temporary = tmp_path / "retained.elf"
        temporary.write_bytes(source.read_bytes())
        data = temporary.read_bytes()
        temporary.unlink()
        assert load_table_bytes(data) == from_bytes
        for version in ("3", "4"):
            table = load_table_bytes(_image(version, encoding))
            assert (table.version, table.encoding, table.timestamp) == (version, encoding, None)
            assert table.bitflags == {("Flags", "app", "1", "app" if version == "4" else None): [("READY", 1), ("BUSY", 2)]}
            wire = _wire([struct.pack("<HB", 3, 3)], encoding)
            batch = decode_stream(table, wire)
            _assert_good(batch, wire, ["flags READY | BUSY"])
            assert batch.records[0]["args"] == [3]


def test_table_sections_links_strings_and_bitflags_are_bounded(tmp_path):
    original = _image()
    table = load_table_bytes(original)
    assert table.encoding == "rzcobs" and table.entries[3][0] == "defmt_info"
    assert list(table.bitflags.values()) == [[("READY", 1), ("BUSY", 2)]]
    shoff = struct.unpack_from("<I", original, 32)[0]
    extended_valid = bytearray(original)
    struct.pack_into("<H", extended_valid, 48, 0)
    struct.pack_into("<I", extended_valid, shoff + 20, 6)
    assert load_table_bytes(bytes(extended_valid)) == table
    # Sections: null, .defmt, .text, .symtab, .strtab, .shstrtab.
    symtab, strings, names = (_section(original, index) for index in (3, 4, 5))
    mutations = [
        (32, "I", len(original) + 1), (48, "H", 65535), (46, "H", 39),
        (50, "H", 65535), (shoff + 5 * 40 + 16, "I", len(original) + 1),
        (shoff + 5 * 40 + 20, "I", 0xFFFFFFFF), (shoff + 40, "I", names[5]),
        (shoff + 3 * 40 + 16, "I", len(original) + 1),
        (shoff + 3 * 40 + 20, "I", 0xFFFFFFFF),
        (shoff + 3 * 40 + 24, "I", 65535), (shoff + 3 * 40 + 36, "I", 15),
        (shoff + 3 * 40 + 24, "I", 1),
        (shoff + 3 * 40 + 36, "I", 0xFFFFFFFF),
        (shoff + 3 * 40 + 20, "I", symtab[5] - 1),
        (symtab[4] + 3 * 16, "I", strings[5]),
        (symtab[4] + 4 * 16 + 8, "I", 15),
        (symtab[4] + 4 * 16 + 4, "I", 0xFFFFFFFF),
        (symtab[4] + 4 * 16 + 4, "I", 40),
        (shoff + 40 + 12, "I", 17),
    ]
    corruptions = []
    for offset, fmt, value in mutations:
        data = bytearray(original)
        struct.pack_into("<" + fmt, data, offset, value)
        corruptions.append(bytes(data))
    extended = bytearray(original)
    struct.pack_into("<H", extended, 48, 0)
    struct.pack_into("<I", extended, shoff + 20, 65537)
    corruptions.append(bytes(extended))
    # A required symbol's terminating NUL is outside its declared string table.
    unterminated = bytearray(original)
    log_name = struct.unpack_from("<I", original, symtab[4] + 3 * 16)[0]
    end = original.index(0, strings[4] + log_name)
    struct.pack_into("<I", unterminated, shoff + 4 * 40 + 20, end - strings[4])
    corruptions.append(bytes(unterminated))
    class BoundedReader(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.calls = self.bytes_read = 0

        def read(self, size=-1):
            assert 0 <= size <= 65536
            assert self.tell() + size <= len(self.getvalue())
            self.calls += 1
            self.bytes_read += size
            assert self.calls <= 256 and self.bytes_read <= len(self.getvalue()) * 16
            return super().read(size)
    assert defmt._load_table(BoundedReader(original)) == table
    for number, data in enumerate(corruptions):
        path = tmp_path / f"corrupt-{number}.elf"
        path.write_bytes(data)
        for operation in (lambda data=data: load_table_bytes(data), lambda path=path: load_table(path), lambda data=data: defmt._load_table(BoundedReader(data))):
            with pytest.raises(DefmtTableError) as refusal:
                operation()
            assert refusal.value.reason in {"elf_unreadable", "table_invalid"}, (number, refusal.value.reason)


def test_nonzero_section_address_preserves_bitflags_and_symbols(tmp_path):
    data = _image(address=0x20001000)
    table = load_table_bytes(data)
    assert table.bitflags == {("Flags", "app", "1", "app"): [("READY", 1), ("BUSY", 2)]}
    path = tmp_path / "nonzero.elf"
    path.write_bytes(data)
    assert load_table(path) == table
    wire = rzcobs_stream([struct.pack("<HB", 3, 3)])
    batch = decode_stream(table, wire)
    _assert_good(batch, wire, ["flags READY | BUSY"])
    shoff = struct.unpack_from("<I", data, 32)[0]
    fields = _section(data, 1)
    section = elfsymbols._section_at(io.BytesIO(data), shoff, 40, 1, 1, "<", len(data))
    assert section == {"name": fields[0], "address": 0x20001000, "type": fields[1], "offset": fields[4], "size": fields[5], "link": fields[6], "entry_size": fields[9]}
    symbol = elfsymbols.read_elf_symbol(path, "main")
    assert symbol["ok"] is True and symbol["address"] == 0x08000400 and symbol["size_bytes"] == 4


def test_non_log_indices_are_rejected_as_frame_headers():
    entries = {
        1: ("defmt_str", "interned"), 2: ("defmt_derived", "Wrapped({=u8})"),
        3: ("defmt_prim", "{=u8}"), 4: ("defmt_timestamp", "{=u32:us}"),
        5: ("defmt_info", "ok {=u8}"), 6: ("defmt_println", "printed {=u8}"),
        7: ("defmt_info", "nested {} {} {=istr}"), 8: ("defmt_trace", "trace"),
        9: ("defmt_debug", "debug"), 10: ("defmt_warn", "warn"), 11: ("defmt_error", "error"),
        12: ("defmt_write", "write"), 13: ("defmt_bitflags", "flags"),
    }
    good = struct.pack("<HB", 5, 42)
    for encoding in ("rzcobs", "raw"):
        table = DefmtTable(entries, None, encoding, "4")
        for index, payload in ((1, b""), (2, b"\x07"), (3, b"\x09"), (4, struct.pack("<I", 123)), (12, b""), (13, b"")):
            rejected = struct.pack("<H", index) + payload
            wire = _wire([rejected, good], encoding)
            batch = decode_stream(table, wire)
            _assert_malformed(batch, wire, encoding, _encoded(rejected)[:-1])
        controls = [good, struct.pack("<HB", 6, 9), struct.pack("<HHBHBH", 7, 2, 7, 3, 9, 1), *(struct.pack("<H", index) for index in (8, 9, 10, 11))]
        wire = _wire(controls, encoding)
        batch = decode_stream(table, wire)
        _assert_good(batch, wire, ["ok 42", "printed 9", "nested Wrapped(7) 9 interned", "trace", "debug", "warn", "error"])
        assert [record["index"] for record in batch.records] == [5, 6, 7, 8, 9, 10, 11]
        assert [record["level"] for record in batch.records] == ["info", None, "info", "trace", "debug", "warn", "error"]
        assert batch.records[1]["println"] is True
        assert batch.records[2]["args"] == [{"format": "Wrapped({=u8})", "args": [7], "text": "Wrapped(7)"}, 9, "interned"]
        timestamped = DefmtTable(entries, "{=u64:us}", encoding, "4")
        following = struct.pack("<HQB", 5, 0, 42)
        for index in (1, 2, 3, 4, 12, 13):
            rejected = struct.pack("<H", index)
            wire = _wire([rejected, following], encoding)
            _assert_malformed(decode_stream(timestamped, wire), wire, encoding, _encoded(rejected)[:-1])


def test_excessive_numeric_specifiers_are_bounded_malformed_records():
    control = _table("{=u8:04x}")
    valid = rzcobs_stream([b"\x01\x00\x07"])
    batch = decode_stream(control, valid)
    _assert_good(batch, valid, ["0007"])
    for encoding in ("rzcobs", "raw"):
        controls = [("{0=u8}", b"\x07", "7"), ("{=u8:04x}", b"\x07", "0007"), ("{=[u8; 3]}", b"\x01\x02\x03", "[1, 2, 3]"), ("{=[?; 3]}", b"\x02\x00\x01\x02\x03", "[1, 2, 3]"), ("{0=0..4} {0=4..8:x}", b"\x5c", "12 5")]
        for fmt, payload, expected in controls:
            wire = _wire([b"\x01\x00" + payload], encoding)
            _assert_good(decode_stream(_table(fmt, encoding), wire), wire, [expected])
        for digits in ("9" * 5000, "0" * 5000 + "1", "4294967295"):
            cases = [("{" + digits + "=u8}", b"\x07"), ("{=u8:0" + digits + "x}", b"\x07"), ("{=[u8; " + digits + "]}", b"\x01\x02\x03"), ("{=[?; " + digits + "]}", b"\x02\x00\x01\x02\x03"), ("{=0.." + digits + "}", b"\x5c"), ("{=" + digits + "..128}", bytes(16))]
            for fmt, payload in cases:
                invalid = b"\x01\x00" + payload
                wire = _wire([invalid, b"\x03\x00\x2a"], encoding)
                if digits == "4294967295":
                    batch = _decode_in_child(fmt, encoding, wire)
                else:
                    batch = decode_stream(_table(fmt, encoding), wire)
                _assert_malformed(batch, wire, encoding, _encoded(invalid)[:-1])
                assert fmt not in batch.records[0]["detail"] or len(fmt) <= 512


def test_numeric_boundaries_preserve_valid_formats():
    wire = rzcobs_stream([b"\x01\x00\x07"])
    batch = decode_stream(_table("{=u8:04096x}"), wire)
    _assert_good(batch, wire, ["0" * 4095 + "7"])
    for version in ("3", "4"):
        for encoding in ("raw", "rzcobs"):
            cases = [
                (" ".join("{" + str(i) + "=u8}" for i in range(256)), bytes(range(256)), " ".join(str(i) for i in range(256))),
                ("{=u8:04096x}", b"\x07", "0" * 4095 + "7"),
                ("{=[u8; 65536]:a}", b"a" * 65536, 'b"' + "a" * 65536 + '"'),
                ("{=[?; 65536]}", b"\x02\x00" + b"\x01" * 65536, "[" + ", ".join(["1"] * 65536) + "]"),
                ("x" * 65536, b"", "x" * 65536),
                ("{=127..128}", b"\x80", "1"),
                ("{=0..128}", b"\xff" * 16, "340282366920938463463374607431768211455"),
            ]
            for fmt, payload, expected in cases:
                wire = _wire([b"\x01\x00" + payload], encoding)
                result = decode_stream(_table(fmt, encoding, version), wire)
                _assert_good(result, wire, [expected])
                if fmt.startswith("{=[u8"):
                    assert result.records[0]["args"] == [[97] * 65536]
                if fmt.startswith("{0=u8}"):
                    assert result.records[0]["args"] == list(range(256))
            bad = [(" ".join("{" + str(i) + "=u8}" for i in range(257)), bytes(257)), ("{4294967295=u8}", b"\x07"), ("{=u8:04097x}", b"\x07"), ("{=[u8; 65537]}", b""), ("{=[?; 65537]}", b"\x02\x00"), ("x" * 65537, b""), ("{=0..129}", bytes(17)), ("{=8..4}", b"\x01"), ("{=8..8}", b"\x01")]
            for fmt, payload in bad:
                rejected = b"\x01\x00" + payload
                wire = _wire([rejected, b"\x03\x00\x2a"], encoding)
                result = _decode_in_child(fmt, encoding, wire, version) if "4294967295" in fmt else decode_stream(_table(fmt, encoding, version), wire)
                _assert_malformed(result, wire, encoding, _encoded(rejected)[:-1])
            for fmt in ("{=str}", "{=[u8]}"):
                truncated = b"\x01\x00" + struct.pack("<I", 0xFFFFFFFF) + b"abc"
                wire = _wire([truncated], encoding)
                result = decode_stream(_table(fmt, encoding, version), wire)
                if encoding == "raw":
                    assert (result.records, result.consumed, result.ends, result.desynchronized) == ([], 0, [], False)
                    assert decode_stream(_table(fmt, encoding, version), wire, final=True).records[0]["error"] == "incomplete_frame"
                else:
                    assert result.records[0]["error"] == "truncated" and result.consumed == len(wire)


def test_recursion_and_zero_sized_element_boundaries():
    table = _table("{}", extra={4: ("defmt_derived", "Wrap({})"), 5: ("defmt_derived", "Unit")})
    wire = rzcobs_stream([struct.pack("<HHH", 1, 4, 5)])
    batch = decode_stream(table, wire)
    _assert_good(batch, wire, ["Wrap(Unit)"])
    for encoding in ("raw", "rzcobs"):
        for depth in (64, 65):
            table = _table("{}", encoding, extra={4: ("defmt_derived", "Wrap({})"), 5: ("defmt_derived", "Unit")})
            frame = struct.pack("<H", 1) + struct.pack("<H", 4) * (depth - 1) + struct.pack("<H", 5)
            wire = _wire([frame, b"\x03\x00\x2a"], encoding)
            result = decode_stream(table, wire)
            if depth == 64:
                _assert_good(result, wire, ["Wrap(" * 63 + "Unit" + ")" * 63, "ok 42"])
            else:
                _assert_malformed(result, wire, encoding, _encoded(frame)[:-1])
        for nested in (False, True):
            prefix = struct.pack("<H", 1) + (struct.pack("<H", 4) if nested else b"")
            # At the count check the shared element index and explicit trailing
            # bytes remain unread. These frames have 14 bytes when padded by
            # rzCOBS; the trailing nonzero byte fixes that length independently.
            padding = b"\x01" * (4 if nested else 6)
            # Total plain size is 14 for either shape, so rzCOBS adds no zeros.
            remaining = 2 + len(padding)
            for count in (3, remaining + 4096, remaining + 4097, 0xFFFFFFFF):
                fmt = "{}" if nested else "{=[?]}"
                table = _table(fmt, encoding, extra={4: ("defmt_derived", "{=[?]}"), 5: ("defmt_derived", "Unit")})
                frame = prefix + struct.pack("<I", count) + struct.pack("<H", 5) + padding
                assert len(frame) == 14
                wire = _wire([frame], encoding)
                result = decode_stream(table, wire)
                if count <= remaining + 4096:
                    # Raw trailing bytes cannot be scanned as a valid frame.
                    first = decode_stream(table, _wire([frame[:-len(padding)]], encoding)) if count == 3 else result
                    assert first.records[0]["message"] == "[" + ", ".join(["Unit"] * count) + "]"
                    assert "error" not in first.records[0]
                else:
                    error = result.records[0]
                    assert error["error"] == "malformed" and len(error["detail"]) <= 512
                    assert result.consumed == len(wire) and result.ends == [len(wire)]
                    assert result.desynchronized == (encoding == "raw")
        allowed = 4101 if encoding == "rzcobs" else 4098
        for count in (3, allowed, allowed + 1):
            # Explicit arrays use the same pre-index budget, with two bytes left.
            table = _table("{=[?; " + str(count) + "]}", encoding, extra={5: ("defmt_derived", "Unit")})
            frame = b"\x01\x00\x05\x00"
            # rzCOBS pads this four-byte frame to seven bytes, leaving five.
            wire = _wire([frame], encoding)
            result = decode_stream(table, wire)
            if count <= allowed:
                _assert_good(result, wire, ["[" + ", ".join(["Unit"] * count) + "]"])
            else:
                assert result.records[0]["error"] == "malformed"


def test_stream_accounting_and_bounded_encoded_diagnostics():
    table = _table("ok {=u8}")
    frame = b"\x01\x00\x2a"
    wire = _encoded(frame)
    batch = decode_stream(table, wire)
    _assert_good(batch, wire, ["ok 42"])
    assert batch.records[0]["args"] == [42]
    for encoding in ("raw", "rzcobs"):
        table = _table("ok {=u8}", encoding)
        empty = decode_stream(table, b"")
        assert (empty.records, empty.consumed, empty.ends, empty.desynchronized) == ([], 0, [], False)
        wire = _encoded(frame) if encoding == "rzcobs" else frame
        for split in range(len(wire) + 1):
            first = decode_stream(table, wire[:split])
            if split < len(wire):
                assert (first.records, first.consumed, first.ends, first.desynchronized) == ([], 0, [], False)
            tail = wire[:split][first.consumed:] + wire[split:]
            second = decode_stream(table, tail)
            assert [r["message"] for r in first.records + second.records] == ["ok 42"]
            assert first.consumed + second.consumed == len(wire)
        for length in (63, 64, 65, 4096):
            rejected = b"A" * length
            good = _encoded(frame) if encoding == "rzcobs" else frame
            wire = rejected + b"\x00" + good if encoding == "rzcobs" else rejected
            result = decode_stream(table, wire)
            span = rejected if encoding == "rzcobs" else wire
            error = result.records[0]
            assert error["size_bytes"] == len(span) and error["raw_hex"] == span[:64].hex()
            assert error.get("raw_truncated", False) == (len(span) > 64)
            if encoding == "rzcobs":
                assert error["raw_text"] == "A" * min(length, 64)
                assert result.ends == [length + 1, len(wire)] and not result.desynchronized
                assert result.records[1]["message"] == "ok 42"
            else:
                assert result.ends == [len(wire)] and result.desynchronized and len(result.records) == 1
                assert error["raw_text"] == "A" * min(length, 64)
            assert result.consumed == len(wire)
        # Entirely printable raw remainder gets the same bounded text prefix.
        raw = b"A" * 65
        result = decode_stream(_table("ok", "raw"), raw)
        assert result.records[0]["raw_text"] == "A" * 64
        assert result.records[0]["size_bytes"] == 65
    plausible = b"\xff\xff" + frame
    result = decode_stream(_table("ok {=u8}", "raw"), plausible)
    assert len(result.records) == 1 and result.records[0]["error"] == "unknown_index"
    assert result.records[0]["size_bytes"] == len(plausible) and result.records[0]["raw_hex"] == plausible.hex()
    assert (result.consumed, result.ends, result.desynchronized) == (len(plausible), [len(plausible)], True)
    separators = decode_stream(table := _table("ok {=u8}"), b"\0\0" + _encoded(frame) + b"\0\0")
    assert separators.consumed == len(_encoded(frame)) + 4
    assert separators.ends == [len(_encoded(frame)) + 2]
    assert [r["message"] for r in separators.records] == ["ok 42"]
    missing = _encoded(frame)[:-1]
    waiting = decode_stream(table, missing)
    assert (waiting.records, waiting.consumed, waiting.ends, waiting.desynchronized) == ([], 0, [], False)
    final = decode_stream(table, missing, final=True)
    assert final.records[0]["error"] == "incomplete_frame"
    assert final.records[0]["raw_hex"] == missing.hex() and final.ends == [len(missing)] and final.consumed == len(missing)
    truncated = _encoded(b"\x01\x00")
    # u8 would be satisfied by rzCOBS zero padding; a u64 cannot be.
    result = decode_stream(_table("{=u64}"), truncated)
    assert result.records[0]["error"] == "truncated" and result.ends == [len(truncated)] and result.consumed == len(truncated)
    invalid = b"\xff\x00" + _encoded(frame)
    result = decode_stream(table, invalid)
    assert [r.get("error") for r in result.records] == ["rzcobs_invalid", None]
    assert result.records[0]["raw_hex"] == "ff" and result.records[0]["size_bytes"] == 1
    assert result.consumed == len(invalid) and result.ends == [2, len(invalid)] and not result.desynchronized
    extra = _encoded(frame + b"\x01\x02")
    result = decode_stream(table, extra)
    assert result.records[0]["unconsumed_hex"].startswith("0102")
    pending = _encoded(frame) + b"\x12\x34"
    result = decode_stream(table, pending)
    assert result.consumed == len(pending) - 2 and result.ends == [len(pending) - 2]
    final = decode_stream(table, pending, final=True)
    assert final.records[-1]["error"] == "incomplete_frame" and final.records[-1]["raw_hex"] == "1234"
    assert final.consumed == len(pending) and final.ends == [len(pending) - 2, len(pending)]
    raw_missing = decode_stream(_table("{=u64}", "raw"), b"\x01\x00\x01")
    assert (raw_missing.records, raw_missing.consumed, raw_missing.ends, raw_missing.desynchronized) == ([], 0, [], False)
    raw_final = decode_stream(_table("{=u64}", "raw"), b"\x01\x00\x01", final=True)
    assert raw_final.records[0]["error"] == "incomplete_frame" and raw_final.consumed == 3 and raw_final.ends == [3]


def test_rendering_budget_is_cumulative():
    fmt = "{0=str}{0=str}"
    wire = rzcobs_stream([b"\x01\x00" + struct.pack("<I", 2) + b"ab"])
    batch = decode_stream(_table(fmt), wire)
    _assert_good(batch, wire, ["abab"])
    assert batch.records[0]["args"] == ["ab"]
    limit = 1048576
    # Repeated placeholders, nested formats, padding, and byte arrays all have
    # individually legal fragments. Their cumulative output crosses the budget.
    cases = [
        ("{0=str}" * 32, struct.pack("<I", 32768) + b"a" * 32768, "a" * limit, {}),
        ("{0=?}" * 32, b"\x04\x00" + struct.pack("<I", 32768) + b"a" * 32768, "a" * limit, {4: ("defmt_derived", "{=str}")}),
        ("{0=u8:04096x}" * 256, b"\x07", ("0" * 4095 + "7") * 256, {}),
        ("{0=[u8; 65536]:a}" * 15 + "{1=str}", b"a" * 65536 + struct.pack("<I", 65491) + b"b" * 65491, ('b"' + "a" * 65536 + '"') * 15 + "b" * 65491, {}),
    ]
    for fmt, payload, expected, extra in cases:
        assert len(expected) == limit
        for excess in (False, True):
            table = _table(fmt + ("!" if excess else ""), extra=extra)
            frame = b"\x01\x00" + payload
            wire = rzcobs_stream([frame, b"\x03\x00\x2a"])
            result = decode_stream(table, wire)
            if excess:
                _assert_malformed(result, wire, "rzcobs", _encoded(frame)[:-1])
            else:
                _assert_good(result, wire, [expected, "ok 42"])
    # Timestamp and message must share the frame budget, then reset on the next.
    for excess in (False, True):
        table = _table("{0=str}" * 16, timestamp="{0=str}" * 16 + ("!" if excess else ""))
        chunk = struct.pack("<I", 32768) + b"a" * 32768
        frame = b"\x01\x00" + chunk + chunk
        following = b"\x03\x00" + struct.pack("<I", 0) + b"\x2a"
        wire = rzcobs_stream([frame, following])
        result = decode_stream(table, wire)
        if excess:
            _assert_malformed(result, wire, "rzcobs", _encoded(frame)[:-1])
        else:
            assert len(result.records[0]["message"]) == limit // 2
            assert len(result.records[0]["timestamp"]) == limit // 2
            _assert_good(result, wire, ["a" * (limit // 2), "ok 42"])


def test_parser_caches_remain_bounded():
    plan = defmt._decode_plan("value {=u8}")
    assert len(plan) == 1 and (plan[0].index, plan[0].ty) == (0, "u8")
    for index in range(4200):
        fmt = f"value-{index} {{=u8}}"
        assert defmt._decode_plan(fmt) == plan
        assert any(isinstance(fragment, defmt.Param) for fragment in defmt.parse_format(fmt))
    for cache in (defmt._decode_plan, defmt.parse_format):
        info = cache.cache_info()
        assert info.maxsize == 4096 and 0 < info.currsize <= 4096
    assert defmt._decode_plan("value {=u8}") == plan
    for fmt in ("{4294967295=u8}", "{256=u8}", "{=[u8; 65537]}"):
        frame = b"\x01\x00\x07"
        wire = rzcobs_stream([frame, b"\x03\x00\x2a"])
        result = _decode_in_child(fmt, "rzcobs", wire) if "4294967295" in fmt else decode_stream(_table(fmt), wire)
        _assert_malformed(result, wire, "rzcobs", _encoded(frame)[:-1])
