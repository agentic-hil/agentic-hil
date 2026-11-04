"""What a defmt firmware puts on the wire, written in Python for the suite.

The decoder in `agentic_hil.defmt` is checked against frames a board would
send. No board runs in the suite, and nothing emulates one, so the frames are
encoded here from the same rules the `defmt` crate's encoder follows: the
16-bit index, the timestamp, each argument in the format string's wire order,
and the rzCOBS framing with the separator the encoder writes before its first
frame. `tests/fixtures/defmt/` holds a real cross-compiled image whose table
these frames index into, and the frames this module produced for it decoded
with the official `defmt-decoder` to the lines recorded beside it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from agentic_hil.defmt import INTEGER_WIDTHS, DefmtTable, _decode_plan


@dataclass
class Nested:
    """A value formatted through `{}` / `{=?}`: its format string, and for an enum the variant."""

    format: str
    args: list = field(default_factory=list)
    variant: int | None = None


@dataclass
class Slice:
    """A `{=[?]}` slice or `{=[?; N]}` array: one format string, one element per entry.

    An element is its argument list, or `(variant, args)` for an enum."""

    format: str
    elements: list = field(default_factory=list)


@dataclass
class Sequence:
    """What a hand-written `Format` impl writes: `(format, args)` pairs, ended by index 0."""

    parts: list = field(default_factory=list)


def index_of(table: DefmtTable, format_string: str, tags: tuple[str, ...] | None = None) -> int:
    found = [index for index, (tag, string) in table.entries.items() if string == format_string and (tags is None or tag in tags)]
    if len(found) != 1:
        raise KeyError(f"{format_string!r} is in the table {len(found)} times")
    return found[0]


def encode_frame(table: DefmtTable, format_string: str, args: list, *, timestamp: list | None = None, tags: tuple[str, ...] | None = None) -> bytes:
    """The raw bytes of one log frame: index, timestamp, arguments."""
    out = bytearray(struct.pack("<H", index_of(table, format_string, tags)))
    if table.timestamp is not None:
        _encode_args(table, out, table.timestamp, timestamp if timestamp is not None else [0])
    _encode_args(table, out, format_string, args)
    return bytes(out)


def _encode_args(table: DefmtTable, out: bytearray, format_string: str, args: list) -> None:
    for param in _decode_plan(format_string):
        _encode_value(table, out, param, args[param.index])


def _encode_value(table: DefmtTable, out: bytearray, param, value) -> None:
    ty = param.ty
    if ty in INTEGER_WIDTHS:
        out += int(value).to_bytes(INTEGER_WIDTHS[ty], "little", signed=ty.startswith("i"))
    elif ty == "f32":
        out += struct.pack("<f", value)
    elif ty == "f64":
        out += struct.pack("<d", value)
    elif ty == "bool":
        out.append(1 if value else 0)
    elif ty == "char":
        out += struct.pack("<I", ord(value))
    elif ty == "str":
        data = value.encode("utf-8")
        out += struct.pack("<I", len(data)) + data
    elif ty == "istr":
        out += struct.pack("<H", index_of(table, value, ("defmt_str",)))
    elif ty == "u8_slice":
        out += struct.pack("<I", len(value)) + bytes(value)
    elif ty == "u8_array":
        out += bytes(value)
    elif ty in ("debug", "display"):
        out += value.encode("utf-8") + b"\xff"
    elif ty == "bitfield":
        low_byte, high_byte = param.bits[0] // 8, (param.bits[1] - 1) // 8
        span = high_byte - low_byte + 1
        width = 1 if span == 1 else 2 if span == 2 else 4 if span <= 4 else 8 if span <= 8 else 16
        out += ((int(value) >> (low_byte * 8)) & ((1 << (8 * width)) - 1)).to_bytes(width, "little")
    elif ty == "format":
        out += struct.pack("<H", index_of(table, value.format))
        element_format = _write_variant(out, value.format, value.variant)
        _encode_args(table, out, element_format, value.args)
    elif ty in ("format_slice", "format_array"):
        if ty == "format_slice":
            out += struct.pack("<I", len(value.elements))
        out += struct.pack("<H", index_of(table, value.format))
        for element in value.elements:
            variant, element_args = element if isinstance(element, tuple) else (None, element)
            element_format = _write_variant(out, value.format, variant)
            _encode_args(table, out, element_format, element_args)
    elif ty == "format_sequence":
        for part_format, part_args in value.parts:
            out += struct.pack("<H", index_of(table, part_format))
            _encode_args(table, out, part_format, part_args)
        out += struct.pack("<H", 0)
    else:
        raise ValueError(f"cannot encode {ty}")


def _write_variant(out: bytearray, format_string: str, variant: int | None) -> str:
    if "|" not in format_string:
        return format_string
    variants = format_string.split("|")
    separators = len(variants) - 1
    width = 1 if separators < 1 << 8 else 2 if separators < 1 << 16 else 4
    out += int(variant or 0).to_bytes(width, "little")
    return variants[int(variant or 0)]


class RzcobsEncoder:
    """The `defmt` crate's rzCOBS encoder, separator before the first frame included."""

    def __init__(self) -> None:
        self.started = False

    def frame(self, data: bytes) -> bytes:
        out = bytearray()
        if not self.started:
            self.started = True
            out.append(0)
        run = zeros = 0
        for byte in data:
            if run < 7:
                if byte == 0:
                    zeros |= 1 << run
                else:
                    out.append(byte)
                run += 1
                if run == 7 and zeros != 0:
                    out.append(zeros)
                    run = zeros = 0
            elif byte == 0:
                out.append((run - 7) | 0x80)
                run = zeros = 0
            else:
                out.append(byte)
                run += 1
                if run == 134:
                    out.append(0xFF)
                    run = zeros = 0
        if 1 <= run <= 6:
            out.append((zeros | (0xFF << run)) & 0x7F)
        elif run >= 7:
            out.append((run - 7) | 0x80)
        out.append(0)
        return bytes(out)


def rzcobs_stream(frames: list[bytes]) -> bytes:
    encoder = RzcobsEncoder()
    return b"".join(encoder.frame(frame) for frame in frames)


def build_elf(symbols: list[tuple[str, int, int, str | None]], sections: dict[str, bytes] | None = None) -> bytes:
    """A minimal little-endian ELF32 holding `sections` and a symbol table.

    Each symbol is `(name, value, size, section)`, with `section` one of the
    names in `sections` or None for an absolute symbol. Enough for the table
    reader, which looks at section names, symbols and section bytes and at
    nothing a loader would need."""
    sections = dict(sections or {})
    names = ["", *sections, ".symtab", ".strtab", ".shstrtab"]
    shstrtab = bytearray(b"\x00")
    name_offsets = {}
    for name in names[1:]:
        name_offsets[name] = len(shstrtab)
        shstrtab += name.encode() + b"\x00"
    strtab = bytearray(b"\x00")
    symtab = bytearray(bytes(16))
    section_index = {name: number for number, name in enumerate(names)}
    for name, value, size, section in symbols:
        offset = len(strtab)
        strtab += name.encode() + b"\x00"
        shndx = 0xFFF1 if section is None else section_index[section]
        symtab += struct.pack("<IIIBBH", offset, value, size, 0x11, 0, shndx)
    bodies = {**sections, ".symtab": bytes(symtab), ".strtab": bytes(strtab), ".shstrtab": bytes(shstrtab)}
    data = bytearray(52)
    placed = {}
    for name in names[1:]:
        placed[name] = len(data)
        data += bodies[name]
    while len(data) % 4:
        data.append(0)
    table_offset = len(data)
    data += bytes(40)
    for name in names[1:]:
        kind = {".symtab": 2, ".strtab": 3, ".shstrtab": 3}.get(name, 1)
        link = section_index[".strtab"] if name == ".symtab" else 0
        entry_size = 16 if name == ".symtab" else 0
        data += struct.pack("<IIIIIIIIII", name_offsets[name], kind, 0, 0, placed[name], len(bodies[name]), link, 0, 1, entry_size)
    header = b"\x7fELF\x01\x01\x01" + bytes(9) + struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, 0, table_offset, 0, 52, 0, 0, 40, len(names), section_index[".shstrtab"])
    data[:52] = header
    return bytes(data)


def defmt_symbol(tag: str, data: str, package: str = "app", disambiguator: str = "1") -> str:
    import json

    return json.dumps({"package": package, "tag": tag, "data": data, "disambiguator": disambiguator, "crate_name": package}, separators=(",", ":"))
