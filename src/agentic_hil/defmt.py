"""Native defmt decoding: from the bytes a board sends to structured log frames.

defmt ("deferred formatting") keeps every format string out of the firmware
image. The board sends a 16-bit index into a table plus the raw arguments, and
the table lives in the ELF the firmware was linked into: one symbol per
interned string in the `.defmt` section, whose name is a JSON object carrying
the string, its tag (log level, `println`, derived `Format` impl, ...) and its
crate. The host joins the two back together.

The usual route is a host tool (`defmt-print`, `probe-rs`) that turns the
frames into text lines, which a test then greps. This module skips the text
detour: a frame decodes straight into its level, timestamp, format string and
typed arguments, and a claim about it is made against those fields. The text a
`defmt-print` would have printed is still produced, from the same rules, for a
person reading the result.

What is implemented, and checked against the official `defmt-decoder` 1.2 on a
real cross-compiled image (`tests/fixtures/defmt/`):

* the wire format of defmt 0.3 and 1.x (wire versions 3 and 4): the 16-bit
  little-endian index, the optional global timestamp, every parameter type of
  the format grammar (integers, floats, `bool`, `char`, `str`, `istr`, byte
  slices and arrays, nested `Format` values including derived enums, `[?]`
  slices and arrays, bitfields, `Display2Format` / `Debug2Format` and the
  format sequences hand-written `Format` impls produce), and `bitflags!`;
* both stream encodings, `rzcobs` (the default, self-synchronising) and `raw`;
* the display hints, rendered as `defmt-decoder` renders them.

What is not: the `:cbor` hint renders the bytes rather than CBOR diagnostic
notation, and source locations are not read (they come from DWARF, which a
release build with `strip = "debuginfo"` does not carry either).

Nothing here guesses. A frame whose index the table does not carry, an rzCOBS
frame that does not decode, an argument that runs past the end of its frame,
a `bool` that is neither 0 nor 1: each is reported as an error record carrying
the bytes it was made of, never skipped and never rendered as if it had
decoded. An index that is unknown is the usual sign of a table that does not
describe the image on the board, which is why the callers take the table from
the ELF this service flashed.
"""

from __future__ import annotations

import datetime
import decimal
import functools
import io
import json
import math
import re
import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from agentic_hil import elfsymbols
from agentic_hil.types import JsonObject

# The wire versions this decoder reads, as the firmware states them in its
# `_defmt_version_` symbol. 3 and 4 share one frame layout; 4 added the crate
# name to the bitflags hint, which the hint parser below accepts either way.
SUPPORTED_WIRE_VERSIONS = ("3", "4")
ENCODINGS = ("rzcobs", "raw")
LEVEL_TAGS = {"defmt_trace": "trace", "defmt_debug": "debug", "defmt_info": "info", "defmt_warn": "warn", "defmt_error": "error"}
LEVELS = ("trace", "debug", "info", "warn", "error")
PRINTLN_TAG = "defmt_println"
TIMESTAMP_TAG = "defmt_timestamp"
BITFLAGS_VALUE_TAG = "defmt_bitflags_value"
KNOWN_TAGS = frozenset({*LEVEL_TAGS, PRINTLN_TAG, TIMESTAMP_TAG, BITFLAGS_VALUE_TAG, "defmt_prim", "defmt_derived", "defmt_bitflags", "defmt_write", "defmt_str"})

# Bounds a hostile or corrupt stream cannot talk this decoder past. Every byte
# of a frame is read at most once, so the work a frame costs is bounded by its
# length, except for two shapes that consume nothing per step: nesting, where
# each level costs two bytes but a Python frame on the stack, and a slice of
# zero-sized elements, whose count is a 32-bit field. The first is capped by
# depth, the second by the bytes left in the frame plus a margin a real slice
# of unit values never reaches.
MAX_NESTING_DEPTH = 64
ZERO_SIZED_ELEMENT_ALLOWANCE = 4096
MAX_ARGUMENTS = 256
MAX_ARRAY_LENGTH = 65536
MAX_FORMAT_LENGTH = 65536
MAX_PADDING_WIDTH = 4096
MAX_RENDERED_MESSAGE_LENGTH = 1048576
MAX_DIAGNOSTIC_DETAIL_LENGTH = 512
# How many bytes of a broken frame an error record carries. The whole frame is
# in the session log either way; this is what a reader of the result sees.
ERROR_HEX_BYTES = 64
DEFMT_SECTION = ".defmt"


class DefmtTableError(Exception):
    """An ELF that cannot serve as a defmt table, with the reason a caller reports."""

    def __init__(self, reason: str, summary: str):
        super().__init__(summary)
        self.reason = reason
        self.summary = summary


class _Eof(Exception):
    """The frame ended before the format string's arguments did."""


class _Malformed(Exception):
    """The frame holds a value its format string cannot have produced."""

    def __init__(self, error: str, detail: str):
        super().__init__(detail)
        self.error = error
        self.detail = detail


@dataclass(frozen=True)
class DefmtTable:
    """The format strings of one firmware image, keyed by the index on the wire."""

    entries: dict[int, tuple[str, str]]
    timestamp: str | None
    encoding: str
    version: str
    bitflags: dict[tuple[str, str, str, str | None], list[tuple[str, int]]] = field(default_factory=dict)

    def describe(self) -> JsonObject:
        """What a result says about the table it decoded with."""
        levels = {level: 0 for level in LEVELS}
        println = 0
        for tag, _ in self.entries.values():
            if tag in LEVEL_TAGS:
                levels[LEVEL_TAGS[tag]] += 1
            elif tag == PRINTLN_TAG:
                println += 1
        return {
            "wire_version": self.version,
            "encoding": self.encoding,
            "timestamp_format": self.timestamp,
            "log_statements": {**levels, "println": println},
        }

    def lookup(self, index: int) -> tuple[str, str]:
        found = self.entries.get(index)
        if found is None:
            raise _Malformed("unknown_index", f"index {index} is not in this image's defmt table")
        return found

    def nested(self, index: int) -> str:
        """A format string a frame refers to from inside, which is never a log statement."""
        tag, string = self.lookup(index)
        if tag in LEVEL_TAGS:
            raise _Malformed("malformed", f"index {index} names a log statement where a nested format string belongs")
        return string


# ---------------------------------------------------------------------------
# Reading the table out of an ELF.


def load_table(elf_path: str | Path) -> DefmtTable:
    """The defmt table of one ELF, or `DefmtTableError` naming why there is none."""
    try:
        with Path(elf_path).open("rb") as handle:
            return _load_table(handle)
    except DefmtTableError:
        raise
    except (OSError, struct.error, ValueError, MemoryError, OverflowError) as error:
        raise DefmtTableError("elf_unreadable", f"The ELF could not be read as an ELF image: {error}") from error


def load_table_bytes(data: bytes) -> DefmtTable:
    """`load_table` over bytes already in memory.

    The callers hash an image and read its table from one copy of its bytes, so
    a rebuild that lands on the path in between cannot hand them a table the
    digest never covered."""
    try:
        return _load_table(io.BytesIO(data))
    except DefmtTableError:
        raise
    except (struct.error, ValueError, MemoryError, OverflowError) as error:
        raise DefmtTableError("elf_unreadable", f"The ELF could not be read as an ELF image: {error}") from error


@dataclass(frozen=True)
class DefmtBinding:
    """A table, and where it came from, as one COM session decodes with it.

    `source` says which ELF the table was read from, its digest, and whether
    that ELF is proven to be the image on the board: `proven` is true only for
    the ELF this service itself flashed, unchanged since."""

    table: DefmtTable
    source: JsonObject

    def describe(self) -> JsonObject:
        return {**self.source, **self.table.describe()}


def _load_table(handle) -> DefmtTable:
    file_size = elfsymbols._file_size(handle)
    identification = elfsymbols._read_at(handle, 0, 16, file_size) if file_size is not None else None
    if identification is None or identification[:4] != elfsymbols.ELF_MAGIC:
        raise DefmtTableError("elf_unreadable", "The file is not an ELF image, so it carries no defmt table. Pass the ELF the firmware was linked into, not a .bin or .hex.")
    elf_class = identification[4]
    byte_order = elfsymbols._byte_order(identification[5])
    if elf_class not in elfsymbols.SECTION_HEADER_FORMAT or byte_order is None or file_size is None:
        raise DefmtTableError("elf_unreadable", "The ELF header declares a class or byte order this reader does not know.")
    sections = elfsymbols._sections(handle, elf_class, byte_order, file_size)
    if sections is None:
        raise DefmtTableError("elf_unreadable", "The ELF's section table could not be read.")
    names = _section_names(handle, elf_class, byte_order, file_size, sections)
    defmt_sections = {index for index, name in enumerate(names) if name == DEFMT_SECTION or name.startswith(DEFMT_SECTION + ".")}
    if any(int(sections[index]["offset"]) + int(sections[index]["size"]) > file_size for index in defmt_sections):
        raise DefmtTableError("elf_unreadable", "The ELF's defmt section lies outside the file.")

    version: str | None = None
    encoding: str | None = None
    timestamp: str | None = None
    entries: dict[int, tuple[str, str]] = {}
    bitflag_values: dict[tuple[str, str, str, str | None], list[tuple[int, str, int]]] = {}
    for section in sections:
        if section["type"] != elfsymbols.SHT_SYMTAB:
            continue
        record_size = struct.calcsize(byte_order + elfsymbols.SYMBOL_FORMAT[elf_class])
        entry_size = int(section["entry_size"])
        size = int(section["size"])
        link = int(section["link"])
        if (entry_size < record_size or size % entry_size or size // entry_size > elfsymbols.MAX_SYMBOLS
                or int(section["offset"]) + size > file_size
                or not 0 < link < len(sections) or sections[link]["type"] != 3):
            raise DefmtTableError("elf_unreadable", "The ELF's symbol table has invalid bounds, entries or string-table link.")
        strings = elfsymbols._section_bytes(handle, sections, section["link"], file_size)
        if strings is None:
            raise DefmtTableError("elf_unreadable", "The ELF's symbol string table could not be read.")
        for entry in elfsymbols._symbols(handle, section, elf_class, byte_order, file_size):
            name = _table_string(strings, entry["name"])
            if not name:
                continue
            found_version = _marker(name, "_defmt_version_ = ")
            if found_version is not None:
                if version is not None and version != found_version:
                    raise DefmtTableError("table_invalid", f"The ELF carries two defmt wire versions, {version} and {found_version}; only one can describe a stream.")
                version = found_version
            found_encoding = _marker(name, "_defmt_encoding_ = ")
            if found_encoding is not None:
                if encoding is not None and encoding != found_encoding:
                    raise DefmtTableError("table_invalid", f"The ELF carries two defmt encodings, {encoding} and {found_encoding}; only one can describe a stream.")
                encoding = found_encoding
            if name.startswith(("_defmt", "__DEFMT_MARKER", '"_defmt')) or name == "$d" or name.startswith("$d."):
                continue
            if entry["shndx"] not in defmt_sections:
                continue
            symbol = _demangle(name)
            if symbol is None:
                raise DefmtTableError("table_invalid", f"A symbol in the .defmt section is not the JSON defmt writes there: {name[:120]}")
            tag, data = symbol["tag"], symbol["data"]
            if tag == TIMESTAMP_TAG:
                if timestamp is not None and timestamp != data:
                    raise DefmtTableError("table_invalid", "The ELF defines the defmt timestamp twice.")
                timestamp = data
            elif tag == BITFLAGS_VALUE_TAG:
                value = _bitflags_value(handle, sections[entry["shndx"]], entry, file_size)
                segments = data.split("::")
                if len(segments) != 3 or not segments[1].isascii() or not segments[1].isdigit() or len(segments[1]) > 10:
                    raise DefmtTableError("table_invalid", f"A defmt bitflags value has an unexpected name: {data}")
                key = (segments[0], symbol["package"], symbol["disambiguator"], symbol.get("crate_name"))
                bitflag_values.setdefault(key, []).append((int(segments[1]), segments[2], value))
            elif tag in KNOWN_TAGS:
                entries[int(entry["value"])] = (tag, data)
            # A tag outside `defmt_*` belongs to third-party tooling and is not
            # part of the stream this decoder reads.

    if not defmt_sections or (not entries and version is None):
        raise DefmtTableError(
            "table_missing",
            "The ELF has no .defmt section, so the firmware does not log with defmt, or it was linked without `-Tdefmt.x`.",
        )
    if version is None:
        raise DefmtTableError("table_invalid", "The ELF has a .defmt section but no `_defmt_version_` symbol.")
    if version not in SUPPORTED_WIRE_VERSIONS:
        raise DefmtTableError(
            "version_unsupported",
            f"The firmware uses defmt wire format version {version}; this decoder reads versions {', '.join(SUPPORTED_WIRE_VERSIONS)} (defmt 0.3 and 1.x).",
        )
    if encoding not in ENCODINGS:
        raise DefmtTableError("table_invalid", f"The ELF names defmt encoding {encoding!r}; this decoder reads {' and '.join(ENCODINGS)}.")
    bitflags = {key: [(name, value) for _, name, value in sorted(values)] for key, values in bitflag_values.items()}
    return DefmtTable(entries=entries, timestamp=timestamp, encoding=str(encoding), version=version, bitflags=bitflags)


def _section_names(handle, elf_class: int, byte_order: str, file_size: int, sections: list[JsonObject]) -> list[str]:
    header = elfsymbols._read_at(handle, 0, elfsymbols.ELF_HEADER_SIZE[elf_class], file_size)
    if header is None:
        raise DefmtTableError("elf_unreadable", "The ELF header could not be read.")
    # e_shstrndx sits right after e_shnum, which sits right after e_shentsize.
    _, _, entry_size_position = elfsymbols.SECTION_TABLE_FIELDS[elf_class]
    string_index = struct.unpack_from(byte_order + "H", header, entry_size_position + 4)[0]
    if not 0 < string_index < len(sections) or sections[string_index]["type"] != 3:
        raise DefmtTableError("elf_unreadable", "The ELF has an invalid section-name table index.")
    strings = elfsymbols._section_bytes(handle, sections, string_index, file_size)
    if strings is None:
        raise DefmtTableError("elf_unreadable", "The ELF's section-name string table could not be read.")
    return [_table_string(strings, int(section["name"])) for section in sections]


def _table_string(strings: bytes, offset: int) -> str:
    if not 0 <= offset < len(strings):
        raise DefmtTableError("elf_unreadable", "An ELF string reference lies outside its table.")
    end = strings.find(b"\x00", offset)
    if end < 0:
        raise DefmtTableError("elf_unreadable", "An ELF string has no terminator inside its table.")
    return strings[offset:end].decode("utf-8", errors="replace")


def _bitflags_value(handle, section: JsonObject, entry: JsonObject, file_size: int) -> int:
    # A `bitflags!` value is a 16-byte u128 stored at the symbol, inside the
    # section's own bytes rather than in a loadable segment.
    if entry["size"] != 16:
        raise DefmtTableError("table_invalid", "A defmt bitflags value does not occupy 16 bytes.")
    offset = int(entry["value"]) - int(section["address"])
    if offset < 0 or offset + 16 > int(section["size"]):
        raise DefmtTableError("table_invalid", "A defmt bitflags value lies outside its section.")
    raw = elfsymbols._read_at(handle, int(section["offset"]) + offset, 16, file_size)
    if raw is None:
        raise DefmtTableError("table_invalid", "A defmt bitflags value could not be read.")
    return int.from_bytes(raw, "little")


def _marker(name: str, prefix: str) -> str | None:
    # LLD keeps the quotes a linker script put around this symbol; GNU ld does not.
    unquoted = name.strip('"')
    return unquoted[len(prefix) :] if unquoted.startswith(prefix) else None


def _demangle(name: str) -> JsonObject | None:
    try:
        symbol = json.loads(name)
    except (ValueError, RecursionError):
        return None
    if not isinstance(symbol, dict) or not all(isinstance(symbol.get(key), str) for key in ("package", "tag", "data", "disambiguator")):
        return None
    if symbol.get("crate_name") is not None and not isinstance(symbol["crate_name"], str):
        return None
    return symbol


# ---------------------------------------------------------------------------
# The format string grammar, as `defmt-parser` reads it in its
# forwards-compatible mode.


@dataclass(frozen=True)
class Hint:
    kind: str  # none, hex, octal, binary, ascii, debug, seconds, time, iso8601, bitflags, cbor, unknown
    alternate: bool = False
    uppercase: bool = False
    zero_pad: int = 0
    precision: str | None = None  # micros, millis, seconds
    bitflags: tuple[str, str, str, str | None] | None = None


@dataclass(frozen=True)
class Param:
    index: int
    ty: str
    size: int = 0  # array length, for u8_array and format_array
    bits: tuple[int, int] = (0, 0)  # [start, end) for a bitfield
    hint: Hint | None = None


PRIMITIVE_TYPES = {
    "u8", "u16", "u32", "u64", "u128", "usize", "i8", "i16", "i32", "i64", "i128", "isize",
    "f32", "f64", "bool", "str", "istr", "char",
}
TYPE_ALIASES = {"__internal_Debug": "debug", "__internal_Display": "display", "__internal_FormatSequence": "format_sequence", "[u8]": "u8_slice", "?": "format", "[?]": "format_slice"}
INTEGER_WIDTHS = {"u8": 1, "u16": 2, "u32": 4, "u64": 8, "u128": 16, "usize": 4, "i8": 1, "i16": 2, "i32": 4, "i64": 8, "i128": 16, "isize": 4}


class FormatStringError(ValueError):
    pass


@functools.lru_cache(maxsize=4096)
def parse_format(format_string: str) -> tuple[str | Param, ...]:
    """The literal and parameter fragments of one format string."""
    if len(format_string) > MAX_FORMAT_LENGTH:
        raise FormatStringError("format string exceeds the length limit")
    fragments: list[str | Param] = []
    end = 0
    next_index = 0
    position = 0
    length = len(format_string)
    while position < length:
        if format_string[position] != "{":
            position += 1
            continue
        if format_string.startswith("{{", position):
            position += 2
            continue
        if position > end:
            fragments.append(_literal(format_string[end:position]))
        close = format_string.find("}", position + 1)
        if close < 0:
            raise FormatStringError("unmatched `{` in format string")
        param = _param(format_string[position + 1 : close])
        if param.index < 0:
            param = Param(next_index, param.ty, param.size, param.bits, param.hint)
            next_index += 1
        if param.index >= MAX_ARGUMENTS:
            raise FormatStringError("argument index exceeds the argument limit")
        fragments.append(param)
        end = position = close + 1
    if end != length:
        fragments.append(_literal(format_string[end:]))
    types: dict[int, str] = {}
    for fragment in fragments:
        if isinstance(fragment, Param):
            seen = types.setdefault(fragment.index, fragment.ty)
            if seen != fragment.ty and not (seen == "bitfield" and fragment.ty == "bitfield"):
                raise FormatStringError(f"conflicting types for argument {fragment.index}")
    if types and max(types) + 1 != len(types):
        raise FormatStringError("an argument is not used in this format string")
    return tuple(fragments)


def _literal(text: str) -> str:
    open_run = close_run = False
    for character in text:
        if character == "{":
            open_run = not open_run
        elif character == "}":
            close_run = not close_run
        elif open_run or close_run:
            raise FormatStringError("unmatched brace in format string")
    if open_run or close_run:
        raise FormatStringError("unmatched brace in format string")
    return text.replace("{{", "{").replace("}}", "}")


def _param(text: str) -> Param:
    digits = len(text) - len(text.lstrip("0123456789"))
    index = _bounded_decimal(text[:digits], MAX_ARGUMENTS - 1) if digits else -1
    rest = text[digits:]
    ty, size, bits = "format", 0, (0, 0)
    if rest.startswith("="):
        rest = rest[1:]
        type_end = rest.find(":")
        fragment = rest if type_end < 0 else rest[:type_end]
        rest = "" if type_end < 0 else rest[type_end:]
        if fragment in PRIMITIVE_TYPES:
            ty = fragment
        elif fragment in TYPE_ALIASES:
            ty = TYPE_ALIASES[fragment]
        elif fragment.startswith("[u8;"):
            ty, size = "u8_array", _array_length(fragment[4:])
        elif fragment.startswith("[?;"):
            ty, size = "format_array", _array_length(fragment[3:])
        else:
            match = re.fullmatch(r"(\d+)\.\.(\d+)", fragment)
            if match is None:
                raise FormatStringError("invalid type specifier")
            start, stop = (_bounded_decimal(match.group(i), 128) for i in (1, 2))
            if stop <= start or start >= 128 or stop > 128:
                raise FormatStringError("invalid bitfield range")
            ty, bits = "bitfield", (start, stop)
    hint = None
    if rest.startswith(":"):
        if len(rest) == 1:
            raise FormatStringError("missing display hint after ':'")
        hint = _hint(rest[1:])
    elif rest:
        raise FormatStringError("unexpected content in format string")
    return Param(index, ty, size, bits, hint)


def _array_length(text: str) -> int:
    match = re.fullmatch(r" *(\d+)\]", text)
    if match is None:
        raise FormatStringError("invalid array specifier")
    return _bounded_decimal(match.group(1), MAX_ARRAY_LENGTH)


def _bounded_decimal(text: str, maximum: int) -> int:
    # Bound the spelling too: leading zeros must not bypass the work limit.
    if not text.isascii() or not text.isdigit() or len(text) > len(str(maximum)):
        raise FormatStringError("numeric specifier exceeds the limit")
    value = int(text)
    if value > maximum:
        raise FormatStringError("numeric specifier exceeds the limit")
    return value


def _hint(text: str) -> Hint:
    alternate = text.startswith("#")
    if alternate:
        text = text[1:]
    zero_pad = 0
    if text.startswith("0"):
        digits = len(text[1:]) - len(text[1:].lstrip("0123456789"))
        if digits == 0:
            return Hint("unknown")
        zero_pad, text = _bounded_decimal(text[1 : 1 + digits], MAX_PADDING_WIDTH), text[1 + digits :]
    if text.startswith("__internal_bitflags_"):
        parts = text[len("__internal_bitflags_") :].split("@")
        if not 3 <= len(parts) <= 4:
            return Hint("unknown")
        return Hint("bitflags", bitflags=(parts[0], parts[1], parts[2], parts[3] if len(parts) == 4 else None))
    simple = {
        "": Hint("none", zero_pad=zero_pad),
        "us": Hint("seconds", precision="micros"),
        "ms": Hint("seconds", precision="millis"),
        "tus": Hint("time", precision="micros"),
        "tms": Hint("time", precision="millis"),
        "ts": Hint("time", precision="seconds"),
        "a": Hint("ascii"),
        "b": Hint("binary", alternate=alternate, zero_pad=zero_pad),
        "o": Hint("octal", alternate=alternate, zero_pad=zero_pad),
        "x": Hint("hex", alternate=alternate, zero_pad=zero_pad),
        "X": Hint("hex", alternate=alternate, uppercase=True, zero_pad=zero_pad),
        "iso8601ms": Hint("iso8601", precision="millis"),
        "iso8601s": Hint("iso8601", precision="seconds"),
        "cbor": Hint("cbor"),
        "?": Hint("debug"),
    }
    # A hint this decoder does not know renders as no hint, as
    # `defmt-decoder` does in its forwards-compatible mode.
    return simple.get(text, Hint("unknown"))


# ---------------------------------------------------------------------------
# Decoding one frame.

# Decoded arguments, as tagged tuples:
#   ("bool", b) ("f32", x) ("f64", x) ("uint", n) ("int", n) ("str", s)
#   ("char", c) ("bytes", bytes) ("format", format, args)
#   ("format_slice", [(format, args), ...]) ("format_sequence", [(format, args), ...])
Arg = tuple


class _Reader:
    __slots__ = ("data", "position")

    def __init__(self, data: bytes):
        self.data = data
        self.position = 0

    def take(self, count: int) -> bytes:
        end = self.position + count
        if end > len(self.data):
            raise _Eof()
        chunk = self.data[self.position : end]
        self.position = end
        return chunk

    def uint(self, width: int) -> int:
        return int.from_bytes(self.take(width), "little")

    def sint(self, width: int) -> int:
        return int.from_bytes(self.take(width), "little", signed=True)

    @property
    def remaining(self) -> int:
        return len(self.data) - self.position


@dataclass
class DecodedFrame:
    index: int
    tag: str
    format: str
    args: list
    timestamp_args: list
    table: DefmtTable
    unconsumed: bytes = b""

    @property
    def level(self) -> str | None:
        return LEVEL_TAGS.get(self.tag)

    def message(self) -> str:
        return _format_args(self.table, self.format, self.args, None)

    def timestamp_text(self) -> str | None:
        if self.table.timestamp is None:
            return None
        return _format_args(self.table, self.table.timestamp, self.timestamp_args, None)

    def line(self) -> str:
        """What `defmt-print` prints for this frame, without colour or location."""
        parts = []
        timestamp = self.timestamp_text()
        if timestamp is not None:
            parts.append(timestamp)
        if self.level is not None:
            parts.append(self.level.upper())
        return " ".join([*parts, self.message()])

    def record(self) -> JsonObject:
        record: JsonObject = {"index": self.index, "level": self.level}
        timestamp = self.timestamp_text()
        remaining = MAX_RENDERED_MESSAGE_LENGTH - len(timestamp or "")
        message = _format_args(self.table, self.format, self.args, None, remaining)
        parts = [timestamp] if timestamp is not None else []
        if self.level is not None:
            parts.append(self.level.upper())
        text = " ".join([*parts, message])
        if self.tag == PRINTLN_TAG:
            record["println"] = True
        if self.table.timestamp is not None:
            record["timestamp"] = timestamp
            value = _timestamp_value(self.timestamp_args)
            if value is not None:
                record["timestamp_value"] = value
        record.update({"format": self.format, "args": [_json_arg(self.table, arg) for arg in self.args], "message": message, "text": text})
        if self.unconsumed.strip(b"\x00"):
            # rzCOBS pads a frame with zeros, never with anything else. Bytes the
            # format string did not account for mean the table and the image
            # disagree about this statement; the frame is shown, and says so.
            record["unconsumed_hex"] = self.unconsumed.hex()
        return record


def decode_frame(table: DefmtTable, data: bytes) -> tuple[DecodedFrame, int]:
    """One frame from the start of `data`, and how many bytes it took.

    Raises `_Eof` when `data` ends inside the frame and `_Malformed` when the
    frame cannot be what the table says it is."""
    reader = _Reader(data)
    return _decode_frame(table, reader), reader.position


def _decode_frame(table: DefmtTable, reader: _Reader) -> DecodedFrame:
    index = reader.uint(2)
    tag, format_string = table.lookup(index)
    if tag not in LEVEL_TAGS and tag != PRINTLN_TAG:
        raise _Malformed("malformed", f"index {index} does not name a log statement")
    timestamp_args = _decode_args(table, reader, table.timestamp, 0) if table.timestamp is not None else []
    args = _decode_args(table, reader, format_string, 0)
    return DecodedFrame(index, tag, format_string, args, timestamp_args, table)


@functools.lru_cache(maxsize=4096)
def _decode_plan(format_string: str) -> tuple[Param, ...]:
    """The parameters in the order the wire carries them, as `defmt-decoder` orders them."""
    params = [fragment for fragment in parse_format(format_string) if isinstance(fragment, Param)]
    merged: dict[int, tuple[int, int]] = {}
    others: list[Param] = []
    for param in params:
        if param.ty == "bitfield":
            start, stop = merged.get(param.index, (param.bits[0], param.bits[1]))
            merged[param.index] = (min(start, param.bits[0]), max(stop, param.bits[1]))
        else:
            others.append(param)
    ordered = sorted([*others, *(Param(index, "bitfield", bits=bits) for index, bits in merged.items())], key=lambda param: param.index)
    plan: list[Param] = []
    for param in ordered:
        if not plan or plan[-1].index != param.index:
            plan.append(param)
    return tuple(plan)


def _decode_args(table: DefmtTable, reader: _Reader, format_string: str, depth: int) -> list:
    if depth > MAX_NESTING_DEPTH:
        raise _Malformed("malformed", f"nested format values go deeper than {MAX_NESTING_DEPTH} levels")
    try:
        plan = _decode_plan(format_string)
    except FormatStringError as error:
        raise _Malformed("malformed", f"the table's format string does not parse: {error}") from error
    return [_decode_param(table, reader, param, depth) for param in plan]


def _nested_format(table: DefmtTable, index: int) -> str:
    format_string = table.nested(index)
    if len(format_string) > MAX_FORMAT_LENGTH:
        raise _Malformed("malformed", "nested format string exceeds the length limit")
    return format_string


def _decode_param(table: DefmtTable, reader: _Reader, param: Param, depth: int) -> Arg:
    ty = param.ty
    if ty in INTEGER_WIDTHS:
        width = INTEGER_WIDTHS[ty]
        return ("int", reader.sint(width)) if ty.startswith("i") else ("uint", reader.uint(width))
    if ty == "f32":
        return ("f32", struct.unpack("<f", reader.take(4))[0])
    if ty == "f64":
        return ("f64", struct.unpack("<d", reader.take(8))[0])
    if ty == "bool":
        value = reader.uint(1)
        if value > 1:
            raise _Malformed("malformed", f"a bool carries {value}, which is neither 0 nor 1")
        return ("bool", value == 1)
    if ty == "char":
        code = reader.uint(4)
        if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
            raise _Malformed("malformed", f"a char carries {code:#x}, which is not a Unicode scalar value")
        return ("char", chr(code))
    if ty == "str":
        return ("str", _utf8(reader.take(reader.uint(4))))
    if ty == "istr":
        return ("str", table.nested(reader.uint(2)))
    if ty == "u8_slice":
        return ("bytes", reader.take(reader.uint(4)))
    if ty == "u8_array":
        return ("bytes", reader.take(param.size))
    if ty in ("debug", "display"):
        end = reader.data.find(b"\xff", reader.position)
        if end < 0:
            raise _Eof()
        text = _utf8(reader.data[reader.position : end])
        reader.position = end + 1
        return ("str", text)
    if ty == "bitfield":
        low_byte, high_byte = param.bits[0] // 8, (param.bits[1] - 1) // 8
        span = high_byte - low_byte + 1
        width = 1 if span == 1 else 2 if span == 2 else 4 if span <= 4 else 8 if span <= 8 else 16
        return ("uint", reader.uint(width) << (low_byte * 8))
    if ty == "format":
        format_string = _nested_format(table, reader.uint(2))
        if "|" in format_string:
            format_string = _variant(reader, format_string)
        return ("format", format_string, _decode_args(table, reader, format_string, depth + 1))
    if ty in ("format_slice", "format_array"):
        count = reader.uint(4) if ty == "format_slice" else param.size
        if count > reader.remaining + ZERO_SIZED_ELEMENT_ALLOWANCE:
            raise _Malformed("malformed", f"a slice claims {count} elements, more than its frame can hold")
        format_string = _nested_format(table, reader.uint(2))
        is_enum = "|" in format_string
        elements = []
        for _ in range(count):
            element_format = _variant(reader, format_string) if is_enum else format_string
            elements.append((element_format, _decode_args(table, reader, element_format, depth + 1)))
        return ("format_slice", elements)
    if ty == "format_sequence":
        sequence = []
        while True:
            index = reader.uint(2)
            if index == 0:
                break
            format_string = _nested_format(table, index)
            sequence.append((format_string, _decode_args(table, reader, format_string, depth + 1)))
        return ("format_sequence", sequence)
    raise _Malformed("malformed", f"parameter type {ty} is not decodable")


def _variant(reader: _Reader, format_string: str) -> str:
    variants = format_string.split("|")
    separators = len(variants) - 1
    width = 1 if separators < 1 << 8 else 2 if separators < 1 << 16 else 4 if separators < 1 << 32 else 8
    discriminant = reader.uint(width)
    if discriminant >= len(variants):
        raise _Malformed("malformed", f"enum discriminant {discriminant} names none of the {len(variants)} variants")
    return variants[discriminant]


def _utf8(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _Malformed("malformed", "a string argument is not valid UTF-8") from error


# ---------------------------------------------------------------------------
# Rendering, as `defmt-decoder`'s `Frame::display` renders.


def _checked_join(parts: Iterable[str], limit: int, separator: str = "") -> str:
    out: list[str] = []
    size = 0
    for part in parts:
        size += len(part) + (len(separator) if out else 0)
        if size > limit:
            raise _Malformed("malformed", "rendered output exceeds the frame limit")
        out.append(part)
    return separator.join(out)


def _format_args(table: DefmtTable, format_string: str, args: list, parent_hint: Hint | None, limit: int = MAX_RENDERED_MESSAGE_LENGTH) -> str:
    out: list[str] = []
    remaining = limit
    for fragment in parse_format(format_string):
        text = fragment if isinstance(fragment, str) else _format_param(table, fragment, args, parent_hint, remaining)
        remaining -= len(text)
        if remaining < 0:
            raise _Malformed("malformed", "rendered output exceeds the frame limit")
        out.append(text)
    return "".join(out)


def _format_param(table: DefmtTable, param: Param, args: list, parent_hint: Hint | None, limit: int) -> str:
    hint = param.hint or parent_hint
    arg = args[param.index]
    kind = arg[0]
    if kind in ("uint", "int") and hint is not None and hint.zero_pad > limit:
        raise _Malformed("malformed", "rendered output exceeds the frame limit")
    if kind == "bool":
        return "true" if arg[1] else "false"
    if kind == "f32":
        return format_float(arg[1], single=True)
    if kind == "f64":
        return format_float(arg[1], single=False)
    if kind == "uint":
        value = arg[1]
        if param.ty == "bitfield":
            start, stop = param.bits
            bits = (value >> start) & ((1 << (stop - start)) - 1)
            if hint is not None and hint.kind == "ascii":
                skip = (128 - stop + start) // 8
                return _format_bytes(table, bits.to_bytes(16, "big")[skip:], hint, limit)
            return _format_uint(table, bits, hint, limit)
        if hint is not None and hint.kind == "iso8601":
            return _format_iso8601(value & 0xFFFF_FFFF_FFFF_FFFF, hint.precision or "millis")
        if hint is not None and hint.kind == "debug":
            return _format_uint(table, value, parent_hint, limit)
        return _format_uint(table, value, hint, limit)
    if kind == "int":
        return _format_int(arg[1], param.ty, hint)
    if kind == "str":
        return _debug_str(arg[1], limit) if hint is not None and hint.kind == "debug" else arg[1]
    if kind == "char":
        return arg[1]
    if kind == "bytes":
        return _format_bytes(table, arg[1], hint, limit)
    if kind == "format":
        inherited = parent_hint if parent_hint is not None and parent_hint.kind == "ascii" else hint
        return _format_args(table, arg[1], arg[2], inherited, limit)
    if kind == "format_sequence":
        return _checked_join((_format_args(table, element_format, element_args, hint, limit) for element_format, element_args in arg[1]), limit)
    if kind == "format_slice":
        elements = arg[1]
        if hint is not None and hint.kind == "ascii" and any(element_format == "{=u8}" for element_format, _ in elements):
            values = []
            for _, element_args in elements:
                if len(element_args) != 1 or element_args[0][0] != "uint":
                    return _format_elements(table, elements, hint, limit)
                values.append(element_args[0][1] & 0xFF)
            return _format_bytes(table, bytes(values), hint, limit)
        return _format_elements(table, elements, hint, limit)
    return str(arg[1])


def _format_elements(table: DefmtTable, elements: list, hint: Hint | None, limit: int) -> str:
    body = _checked_join((_format_args(table, fmt, args, hint, max(0, limit - 2)) for fmt, args in elements), limit - 2, ", ")
    if limit < 2:
        raise _Malformed("malformed", "rendered output exceeds the frame limit")
    return "[" + body + "]"


def _format_uint(table: DefmtTable, value: int, hint: Hint | None, limit: int) -> str:
    if hint is None:
        return str(value)
    if hint.kind == "none":
        return format(value, f"0{hint.zero_pad}d") if hint.zero_pad else str(value)
    if hint.kind in ("binary", "octal", "hex"):
        return _radix(value, hint)
    if hint.kind == "seconds":
        if hint.precision == "micros":
            return f"{value // 1_000_000}.{value % 1_000_000:06d}"
        return f"{value // 1_000}.{value % 1_000:03d}"
    if hint.kind == "time":
        return _format_time(value, hint.precision or "seconds")
    if hint.kind == "bitflags" and hint.bitflags is not None:
        flags = table.bitflags.get(hint.bitflags)
        if flags is None:
            return str(value)
        names = [name for name, flag in flags if not (flag == 0 and value != 0) and value & flag == flag]
        return _checked_join(names, limit, " | ") if names else "(empty)"
    return str(value)


def _radix(value: int, hint: Hint) -> str:
    """Rust's `{:#0N$x}` and friends: the prefix counts toward the width."""
    code = {"binary": "b", "octal": "o", "hex": "X" if hint.uppercase else "x"}[hint.kind]
    digits = format(value, code)
    prefix = {"binary": "0b", "octal": "0o", "hex": "0x"}[hint.kind] if hint.alternate else ""
    return prefix + digits.rjust(max(0, hint.zero_pad - len(prefix)), "0")


def _format_int(value: int, ty: str, hint: Hint | None) -> str:
    if hint is None:
        return str(value)
    if hint.kind == "none":
        return format(value, f"0{hint.zero_pad}d") if hint.zero_pad else str(value)
    if hint.kind in ("binary", "octal"):
        # `defmt-decoder` formats the sign-extended i128 here, so a negative
        # value shows all 128 bits of its two's complement.
        return _radix(value & ((1 << 128) - 1), hint)
    if hint.kind == "hex":
        # Here it narrows to the argument's own width first. `isize` has no arm
        # in `defmt-decoder` (it panics); its 32-bit wire width is used.
        bits = 8 * INTEGER_WIDTHS.get(ty, 16)
        return _radix(value & ((1 << bits) - 1), hint)
    return str(value)


def _format_bytes(table: DefmtTable, data: bytes, hint: Hint | None, limit: int) -> str:
    if hint is not None and hint.kind == "ascii":
        def fragments() -> Iterator[str]:
            yield 'b"'
            for byte in data:
                escaped = {0x09: "\\t", 0x0A: "\\n", 0x0D: "\\r", 0x20: " ", 0x22: '\\"', 0x5C: "\\\\"}.get(byte)
                yield escaped if escaped is not None else chr(byte) if 0x21 <= byte <= 0x7E else f"\\x{byte:02x}"
            yield '"'
        return _checked_join(fragments(), limit)
    if hint is not None and hint.kind in ("hex", "octal", "binary"):
        body = _checked_join((_format_uint(table, byte, hint, limit - 2) for byte in data), limit - 2, ", ")
        return _checked_join(("[", body, "]"), limit)
    if hint is not None and hint.kind == "cbor":
        # Not rendered as CBOR diagnostic notation; the bytes are shown as
        # they arrived and are in the frame's structured arguments either way.
        if len(data) * 2 + 8 > limit:
            raise _Malformed("malformed", "rendered output exceeds the frame limit")
        return f"cbor h'{data.hex()}'"
    body = _checked_join((str(byte) for byte in data), limit - 2, ", ")
    return _checked_join(("[", body, "]"), limit)


def _format_time(value: int, precision: str) -> str:
    if precision == "micros":
        value, decimals, width = value // 1_000_000, value % 1_000_000, 6
    elif precision == "millis":
        value, decimals, width = value // 1_000, value % 1_000, 3
    else:
        decimals, width = 0, 0
    value, seconds = divmod(value, 60)
    value, minutes = divmod(value, 60)
    days, hours = divmod(value, 24)
    clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    if width:
        clock += f".{decimals:0{width}d}"
    return clock if days == 0 else f"{days}:{clock}"


def _format_iso8601(value: int, precision: str) -> str:
    seconds, fraction = divmod(value, 1000) if precision == "millis" else (value, 0)
    try:
        moment = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    except OverflowError:
        return str(value)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{text}.{fraction:03d}Z" if precision == "millis" else f"{text}Z"


def _debug_str(text: str, limit: int) -> str:
    """Rust's `{:?}` for a `str`, for the characters a firmware log realistically holds."""
    def fragments() -> Iterator[str]:
        yield '"'
        for character in text:
            escaped = {"\t": "\\t", "\r": "\\r", "\n": "\\n", "\\": "\\\\", '"': '\\"', "\0": "\\0"}.get(character)
            yield escaped if escaped is not None else character if character.isprintable() or character == " " else f"\\u{{{ord(character):x}}}"
        yield '"'
    return _checked_join(fragments(), limit)


def format_float(value: float, *, single: bool) -> str:
    """The shortest round-trip rendering the `ryu` crate gives, which is what `defmt-decoder` prints."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    sign = "-" if math.copysign(1.0, value) < 0 else ""
    if value == 0:
        return f"{sign}0.0"
    digits, exponent = _shortest_digits(abs(value), single)
    length = len(digits)
    total = length + exponent  # 10**(total-1) <= value < 10**total
    upper, lower = (13, -6) if single else (16, -5)
    if exponent >= 0 and total <= upper:
        body = digits + "0" * exponent + ".0"
    elif 0 < total <= upper:
        body = digits[:total] + "." + digits[total:]
    elif lower < total <= 0:
        body = "0." + "0" * (-total) + digits
    elif length == 1:
        body = f"{digits}e{total - 1}"
    else:
        body = f"{digits[0]}.{digits[1:]}e{total - 1}"
    return sign + body


def _shortest_digits(value: float, single: bool) -> tuple[str, int]:
    """Digits and exponent of the shortest decimal that reads back as `value`."""
    if not single:
        # Python's repr is the shortest round-trip form for a double, chosen
        # closest to the exact value, which is the same contract ryu keeps.
        mantissa, _, exponent_text = repr(value).partition("e")
        whole, _, fraction = mantissa.partition(".")
        digits = (whole + fraction).lstrip("0")
        exponent = int(exponent_text or 0) - len(fraction)
        stripped = digits.rstrip("0")
        return stripped, exponent + len(digits) - len(stripped)
    exact = decimal.Decimal(value)
    target = struct.pack("<f", value)
    context = decimal.Context(prec=60)
    for precision in range(1, 10):
        rounded = context.create_decimal(exact).quantize(decimal.Decimal(1).scaleb(exact.adjusted() - precision + 1), rounding=decimal.ROUND_HALF_EVEN)
        unit = decimal.Decimal(1).scaleb(rounded.adjusted() - precision + 1)
        candidates = [rounded, rounded - unit, rounded + unit]
        fitting = [candidate for candidate in candidates if candidate > 0 and _f32_bits(candidate) == target]
        if fitting:
            best = min(fitting, key=lambda candidate: (abs(candidate - exact), candidate != rounded))
            sign, digit_tuple, decimal_exponent = best.normalize().as_tuple()
            return "".join(map(str, digit_tuple)), int(decimal_exponent)
    sign, digit_tuple, decimal_exponent = exact.normalize().as_tuple()  # pragma: no cover - nine digits always suffice
    return "".join(map(str, digit_tuple)), int(decimal_exponent)


def _f32_bits(candidate: decimal.Decimal) -> bytes | None:
    try:
        return struct.pack("<f", float(candidate))
    except OverflowError:
        return None


# ---------------------------------------------------------------------------
# Arguments as JSON values a plan can compare against.


def _json_arg(table: DefmtTable, arg: Arg):
    kind = arg[0]
    if kind in ("uint", "int", "bool", "str", "char"):
        return arg[1]
    if kind in ("f32", "f64"):
        value = arg[1]
        if not math.isfinite(value):
            return format_float(value, single=kind == "f32")
        # The value as it reads, so 0.1f32 compares equal to 0.1 rather than
        # to the 0.10000000149011612 the same bits are as a double.
        return float(format_float(value, single=kind == "f32"))
    if kind == "bytes":
        return list(arg[1])
    if kind == "format":
        return _json_nested(table, arg[1], arg[2])
    if kind == "format_slice":
        return [_json_nested(table, element_format, element_args) for element_format, element_args in arg[1]]
    if kind == "format_sequence":
        return {"sequence": [_json_nested(table, element_format, element_args) for element_format, element_args in arg[1]], "text": _format_args(table, "{=__internal_FormatSequence}", [arg], None)}
    return None


def _json_nested(table: DefmtTable, format_string: str, args: list):
    fragments = parse_format(format_string)
    if len(fragments) == 1 and isinstance(fragments[0], Param) and len(args) == 1:
        # A value formatted through its own primitive `Format` impl ("{=u8}",
        # "{=str}", ...) is that value; wrapping it would only bury it.
        return _json_arg(table, args[0])
    return {"format": format_string, "args": [_json_arg(table, arg) for arg in args], "text": _format_args(table, format_string, args, None)}


def _timestamp_value(args: list) -> int | float | None:
    if len(args) == 1 and args[0][0] in ("uint", "int"):
        return args[0][1]
    return None


# ---------------------------------------------------------------------------
# Streams.


def rzcobs_decode(data: bytes) -> bytes:
    """One rzCOBS frame, without its 0x00 separator, back to the bytes it encodes."""
    out = bytearray()
    source = reversed(data)
    for code in source:
        if code == 0:
            raise ValueError("a zero byte inside an rzCOBS frame")
        if code < 0x80:
            for bit in range(7):
                if code & (1 << (6 - bit)):
                    out.append(0)
                else:
                    out.append(_next(source))
        elif code < 0xFF:
            out.append(0)
            for _ in range((code & 0x7F) + 7):
                out.append(_next(source))
        else:
            for _ in range(134):
                out.append(_next(source))
    out.reverse()
    return bytes(out)


def _next(source: Iterator[int]) -> int:
    try:
        return next(source)
    except StopIteration:
        raise ValueError("an rzCOBS frame ends inside a run") from None


@dataclass
class DecodeBatch:
    """What one pass over a stream's bytes produced."""

    records: list[JsonObject]
    consumed: int
    desynchronized: bool = False
    # Where in the input each record ends, so a reader can stop after any one
    # of them and leave the rest for later.
    ends: list[int] = field(default_factory=list)


def error_record(error: str, detail: str, raw: bytes) -> JsonObject:
    record: JsonObject = {"error": error, "detail": detail[:MAX_DIAGNOSTIC_DETAIL_LENGTH], "size_bytes": len(raw), "raw_hex": raw[:ERROR_HEX_BYTES].hex()}
    if len(raw) > ERROR_HEX_BYTES:
        record["raw_truncated"] = True
    if raw and all(0x20 <= byte <= 0x7E or byte in (0x09, 0x0A, 0x0D) for byte in raw):
        # Plain text where a frame belongs is almost always something other
        # than defmt sharing the line, a bootloader banner or a `printf` left
        # in: shown as text, so a reader recognises it without decoding hex.
        record["raw_text"] = raw[:ERROR_HEX_BYTES].decode("ascii")
    return record


def decode_stream(table: DefmtTable, data: bytes, *, final: bool = False) -> DecodeBatch:
    """Every complete frame at the start of `data`, in order.

    `consumed` is how many bytes those frames took; what follows is an
    incomplete frame that waits for more bytes, unless `final` says no more are
    coming, in which case it is reported as `incomplete_frame` and consumed.
    """
    if table.encoding == "rzcobs":
        return _decode_rzcobs(table, data, final)
    return _decode_raw(table, data, final)


def _decode_rzcobs(table: DefmtTable, data: bytes, final: bool) -> DecodeBatch:
    records: list[JsonObject] = []
    ends: list[int] = []
    position = 0
    length = len(data)
    while True:
        while position < length and data[position] == 0:
            position += 1
        if position >= length:
            break
        separator = data.find(b"\x00", position)
        if separator < 0:
            if final:
                records.append(error_record("incomplete_frame", "the stream ended inside a frame, before its 0x00 separator", data[position:]))
                position = length
                ends.append(position)
            break
        records.append(_decode_rzcobs_frame(table, data[position:separator]))
        position = separator + 1
        ends.append(position)
    return DecodeBatch(records, position, ends=ends)


def _decode_rzcobs_frame(table: DefmtTable, raw: bytes) -> JsonObject:
    try:
        payload = rzcobs_decode(raw)
    except ValueError as error:
        return error_record("rzcobs_invalid", f"the bytes between two separators are not an rzCOBS frame: {error}", raw)
    try:
        frame, consumed = decode_frame(table, payload)
        frame.unconsumed = payload[consumed:]
        return frame.record()
    except _Eof:
        return error_record("truncated", "the frame ended before its format string's arguments did", raw)
    except _Malformed as error:
        return error_record(error.error, error.detail, raw)


def _decode_raw(table: DefmtTable, data: bytes, final: bool) -> DecodeBatch:
    records: list[JsonObject] = []
    ends: list[int] = []
    position = 0
    reader = _Reader(data)
    while position < len(data):
        try:
            frame = _decode_frame(table, reader)
            record = frame.record()
        except _Eof:
            if final:
                records.append(error_record("incomplete_frame", "the stream ended inside a frame", data[position:]))
                position = len(data)
                ends.append(position)
            break
        except _Malformed as error:
            # Without framing there is no next frame boundary to resume at, so
            # everything from here on is unreadable. Said once, not guessed at.
            records.append(error_record(error.error, f"{error.detail}; the raw encoding has no frame boundaries, so the rest of this stream cannot be decoded", data[position:]))
            ends.append(len(data))
            return DecodeBatch(records, len(data), desynchronized=True, ends=ends)
        records.append(record)
        position = reader.position
        ends.append(position)
    return DecodeBatch(records, position, ends=ends)


def describe_records(records: list[JsonObject]) -> JsonObject:
    """Counts of what a list of records holds, for a result's summary line."""
    frames = sum(1 for record in records if "error" not in record)
    errors: dict[str, int] = {}
    for record in records:
        if "error" in record:
            errors[record["error"]] = errors.get(record["error"], 0) + 1
    return {"frames": frames, "errors": errors}
