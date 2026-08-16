"""Address and size for one symbol, read out of an ELF's own symbol table.

The fallback behind both symbol-resolution paths. GDB's expression evaluator is
asked first everywhere, because it is the one route that understands types,
scopes and C++ names; it answers `&symbol` and `sizeof(symbol)` out of the
image's DWARF. An object the assembler defined with `.global` / `.type` /
`.size` and no debug information has no type for either expression to work
with, so GDB refuses both, and the symbol is nonetheless completely described:
`st_value` and `st_size` sit in the ELF symbol table, which is the same file
GDB already loaded (#187).

Read here rather than asked of GDB because GDB has no command that prints a
minimal symbol's size. `info address` and `info symbol` answer the address in
prose that varies with the symbol's storage class, and `maint print msymbols`
prints the address and the section and not the size at all. The size is in the
file, in a fixed-width record whose layout is the ABI, so it is read from
there: 60 lines of `struct` against a format that cannot change under us, and
no dependency on how a particular GDB build words an answer.

Deliberately narrow. This reads the symbol table and nothing else: no
relocations, no DWARF, no program headers, no section contents. Anything it
cannot parse is `unreadable` rather than a guess, and every refusal leaves the
caller's own GDB-based refusal standing, so a symbol that is genuinely absent
is still reported absent.

The one other answer here is `read_elf_byte_order`, which is the identification
byte this module already has to read before it can unpack a single field. It is
public because a caller that turns a symbol's bytes into a number needs exactly
that byte and has nowhere else to get it (#249).
"""

from __future__ import annotations

import struct
from pathlib import Path

from agentic_hil.types import JsonObject

ELF_MAGIC = b"\x7fELF"
ELF_CLASS_32 = 1
ELF_CLASS_64 = 2
ELF_DATA_LSB = 1
ELF_DATA_MSB = 2
# What EI_DATA, the sixth identification byte, means, in the words
# `int.from_bytes` takes. One table for the two readers below: the struct
# prefixes this module unpacks with are derived from it, so an encoding that is
# neither LSB nor MSB is the same "no answer" to both, and neither can start
# recognising a third one without the other.
ELF_DATA_BYTE_ORDER = {ELF_DATA_LSB: "little", ELF_DATA_MSB: "big"}
STRUCT_BYTE_ORDER = {"little": "<", "big": ">"}
SHT_SYMTAB = 2
SHN_UNDEF = 0
# Section header sizes, symbol record sizes and the offsets inside them, per
# ELF class. The layout is the ABI's, not a choice: ELF32 orders a symbol
# record name/value/size/info/other/shndx and ELF64 orders it
# name/info/other/shndx/value/size, which is why the two need separate formats
# rather than one with different widths.
SECTION_HEADER_FORMAT = {ELF_CLASS_32: "IIIIIIIIII", ELF_CLASS_64: "IIQQQQIIQQ"}
SYMBOL_FORMAT = {ELF_CLASS_32: "IIIBBH", ELF_CLASS_64: "IBBHQQ"}
# Which unpacked field is what, per class, so the two record orders are read by
# name below instead of by a magic index.
SYMBOL_FIELDS = {ELF_CLASS_32: {"name": 0, "value": 1, "size": 2, "shndx": 5}, ELF_CLASS_64: {"name": 0, "shndx": 3, "value": 4, "size": 5}}
ELF_HEADER_SIZE = {ELF_CLASS_32: 52, ELF_CLASS_64: 64}
# Where the section table is described in the ELF header, per class: the offset
# and width of `e_shoff`, then the offset of `e_shentsize`, with `e_shnum`
# immediately after it. Only these three fields are read; everything else in the
# header belongs to loading an image, which this module does not do.
SECTION_TABLE_FIELDS = {ELF_CLASS_32: (32, 4, 46), ELF_CLASS_64: (40, 8, 58)}
# A corrupt or hostile file must not turn a symbol lookup into an unbounded
# read. Both caps sit far above any firmware ELF: a Cortex-M image has tens of
# sections and thousands of symbols, and a debug build of a large C++ target
# stays well inside these.
MAX_SECTIONS = 65536
MAX_SYMBOLS = 4_000_000


def read_elf_symbol(elf_path: str | Path, symbol: str) -> JsonObject:
    """What the symbol table says about one symbol, or why it says nothing.

    `ok` is true only for a defined symbol with a recorded non-zero size,
    because that is the whole of what the callers need: an address to read from
    and a length to read. A symbol the table carries with `st_size` zero (a bare
    assembler label, with no `.size` directive behind it) is reported
    `no_size` rather than answered with a zero-byte read, which would be a dump
    of nothing presented as a successful one.

    Two different definitions of the same name are `ambiguous` rather than a
    silent first-match: picking one would be picking a board's address by file
    order.
    """
    try:
        with Path(elf_path).open("rb") as handle:
            return _read_symbol(handle, symbol)
    except (OSError, struct.error, ValueError, MemoryError):
        # Anything this file is that an ELF is not. Never raised onward: the
        # caller is already holding a GDB refusal it will report if this
        # fallback declines, and a broken image must not turn that structured
        # refusal into a traceback.
        return {"ok": False, "reason": "unreadable"}


def _read_symbol(handle, symbol: str) -> JsonObject:
    identification = handle.read(16)
    if len(identification) < 16 or identification[:4] != ELF_MAGIC:
        return {"ok": False, "reason": "unreadable"}
    elf_class = identification[4]
    byte_order = _byte_order(identification[5])
    if elf_class not in SECTION_HEADER_FORMAT or byte_order is None:
        return {"ok": False, "reason": "unreadable"}
    sections = _sections(handle, elf_class, byte_order)
    if sections is None:
        return {"ok": False, "reason": "unreadable"}
    wanted = symbol.encode("utf-8")
    found: set[tuple[int, int]] = set()
    defined = False
    for section in sections:
        if section["type"] != SHT_SYMTAB:
            continue
        strings = _section_bytes(handle, sections, section["link"])
        if strings is None:
            return {"ok": False, "reason": "unreadable"}
        for entry in _symbols(handle, section, elf_class, byte_order):
            if entry["shndx"] == SHN_UNDEF or _symbol_name(strings, entry["name"]) != wanted:
                continue
            defined = True
            if entry["size"] > 0:
                found.add((entry["value"], entry["size"]))
    if not found:
        # `defined` separates the two silences: a name the table does not carry
        # at all is `not_found`, and a name it carries without a size is
        # `no_size`. The caller reports which, because the fix differs: add the
        # symbol, or add the `.size` directive behind it.
        return {"ok": False, "reason": "no_size" if defined else "not_found"}
    if len(found) > 1:
        return {"ok": False, "reason": "ambiguous"}
    address, size_bytes = found.pop()
    return {"ok": True, "address": address, "size_bytes": size_bytes}


def read_elf_byte_order(elf_path: str | Path) -> str | None:
    """The byte order the image declares about itself, or None where it declares none.

    EI_DATA is the file's own statement about how its multi-byte fields are
    ordered, and for a firmware image that is the target's order: an Arm
    Cortex-M build says LSB, a big-endian part's build says MSB. Answered as
    "little" or "big" so it goes straight into `int.from_bytes`, which is the
    one thing a caller wants it for.

    None where the file cannot be read, is not an ELF, or declares an encoding
    that is neither of the two, which is the same file `read_elf_symbol` calls
    `unreadable`, refused here for the same reason: an image that states no
    order cannot be made to state one.
    """
    try:
        with Path(elf_path).open("rb") as handle:
            identification = handle.read(16)
    except OSError:
        return None
    if len(identification) < 16 or identification[:4] != ELF_MAGIC:
        return None
    return ELF_DATA_BYTE_ORDER.get(identification[5])


def _byte_order(data_encoding: int) -> str | None:
    name = ELF_DATA_BYTE_ORDER.get(data_encoding)
    return None if name is None else STRUCT_BYTE_ORDER[name]


def _sections(handle, elf_class: int, byte_order: str) -> list[JsonObject] | None:
    offset_position, offset_width, entry_size_position = SECTION_TABLE_FIELDS[elf_class]
    header = _read_at(handle, 0, ELF_HEADER_SIZE[elf_class])
    if header is None:
        return None
    offset_format = "I" if offset_width == 4 else "Q"
    table_offset = struct.unpack_from(byte_order + offset_format, header, offset_position)[0]
    entry_size, count = struct.unpack_from(byte_order + "HH", header, entry_size_position)
    fixed_size = struct.calcsize(byte_order + SECTION_HEADER_FORMAT[elf_class])
    if table_offset == 0 or entry_size < fixed_size:
        return None
    if count == 0:
        # Extended numbering: with more than 0xff00 sections the header's 16-bit
        # count is zero and the real count lives in section 0's sh_size. Rare in
        # firmware and cheap to honour; ignoring it would silently find no
        # symbol table in exactly the images that have the most symbols.
        first = _section_at(handle, table_offset, entry_size, 0, elf_class, byte_order)
        if first is None:
            return None
        count = first["size"]
    if count > MAX_SECTIONS:
        return None
    sections = []
    for index in range(count):
        section = _section_at(handle, table_offset, entry_size, index, elf_class, byte_order)
        if section is None:
            return None
        sections.append(section)
    return sections


def _section_at(handle, table_offset: int, entry_size: int, index: int, elf_class: int, byte_order: str) -> JsonObject | None:
    raw = _read_at(handle, table_offset + index * entry_size, struct.calcsize(byte_order + SECTION_HEADER_FORMAT[elf_class]))
    if raw is None:
        return None
    fields = struct.unpack(byte_order + SECTION_HEADER_FORMAT[elf_class], raw)
    return {"type": fields[1], "offset": fields[4], "size": fields[5], "link": fields[6], "entry_size": fields[9]}


def _symbols(handle, section: JsonObject, elf_class: int, byte_order: str):
    record_size = struct.calcsize(byte_order + SYMBOL_FORMAT[elf_class])
    entry_size = int(section["entry_size"]) or record_size
    if entry_size < record_size:
        return
    count = int(section["size"]) // entry_size
    if count > MAX_SYMBOLS:
        return
    fields = SYMBOL_FIELDS[elf_class]
    for index in range(count):
        raw = _read_at(handle, int(section["offset"]) + index * entry_size, record_size)
        if raw is None:
            return
        unpacked = struct.unpack(byte_order + SYMBOL_FORMAT[elf_class], raw)
        yield {name: unpacked[position] for name, position in fields.items()}


def _section_bytes(handle, sections: list[JsonObject], index: int) -> bytes | None:
    if index >= len(sections):
        return None
    section = sections[index]
    return _read_at(handle, int(section["offset"]), int(section["size"]))


def _symbol_name(strings: bytes, offset: int) -> bytes:
    if offset >= len(strings):
        return b""
    end = strings.find(b"\x00", offset)
    return strings[offset:] if end < 0 else strings[offset:end]


def _read_at(handle, offset: int, size: int) -> bytes | None:
    if offset < 0 or size < 0:
        return None
    handle.seek(offset)
    data = handle.read(size)
    return data if len(data) == size else None
