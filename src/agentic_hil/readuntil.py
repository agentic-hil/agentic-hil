"""Waiting for what the caller is waiting for, instead of for the first thing.

`com_read` answers on the first bytes that arrive and `can_read` on the first
frame. A test verdict or a boot banner arrives over many reader chunks, so a
caller waiting for `PASS` had to poll. `until` (one string, or a list of them)
and `until_id` (one arbitration id, or a list of them) let one call wait for
exactly that.

The checks, the wait and the byte matcher live here rather than in either
manager, because anything else that reads a COM port for a pattern takes the
same `until` under the same rules: the caller passes its own tool and field
names for the refusals, its port's encoding and its own bytes buffer.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentic_hil.types import JsonObject

UNTIL_MAX_ENTRIES = 8
UNTIL_MAX_CHARACTERS = 256
CAN_ID_MAX = 0x1FFFFFFF
UNTIL_DEFAULT_WAIT_S = 10.0
UNTIL_MAX_WAIT_S = 60.0


def until_wait_s(wait_timeout_s: float | None) -> float:
    """The wait in force for `until` or `until_id`, in seconds.

    Ten seconds when the caller gave none, because a call that was given
    something to wait for and then did not wait would answer nothing useful;
    never more than sixty, the cap a plain read has always had."""
    if wait_timeout_s is None:
        return UNTIL_DEFAULT_WAIT_S
    return max(0.0, min(float(wait_timeout_s), UNTIL_MAX_WAIT_S))


def until_patterns(until: object, encoding: str, *, tool: str = "com_read", field: str = "until") -> JsonObject:
    """Check `until` and encode each entry as the bytes it is looked for as.

    `until` is a non-empty string, or a list of 1 to 8 of them, each at most
    256 characters. Each entry is encoded with `encoding`, the port's own,
    because the match is made on the bytes the port carries: a pattern split
    across two reads, or a character split between its bytes, is then found
    like any other. An entry the encoding cannot carry could never match, so it
    is refused by name rather than waited for.

    Answers `{"ok": True, "entries": [...], "patterns": [...]}`, the entries as
    a list whatever shape they were given in, or the refusal under the caller's
    own `tool` and `field`."""
    if isinstance(until, str):
        entries: list[tuple[str, object]] = [(field, until)]
    elif isinstance(until, list) and 1 <= len(until) <= UNTIL_MAX_ENTRIES:
        entries = [(f"{field}[{index}]", entry) for index, entry in enumerate(until)]
    else:
        return _refusal(tool, field, f"{field} must be a non-empty string or a list of 1 to {UNTIL_MAX_ENTRIES} of them.")
    texts: list[tuple[str, str]] = []
    for path, entry in entries:
        if not isinstance(entry, str) or not entry:
            return _refusal(tool, path, f"{path} must be a non-empty string.")
        if len(entry) > UNTIL_MAX_CHARACTERS:
            return _refusal(tool, path, f"{path} must be at most {UNTIL_MAX_CHARACTERS} characters.")
        texts.append((path, entry))
    patterns: list[bytes] = []
    for path, text in texts:
        try:
            patterns.append(text.encode(encoding))
        except LookupError:
            return {"ok": False, "tool": tool, "error_type": "config_invalid", "summary": "COM port encoding is not supported by Python.", "encoding": encoding}
        except UnicodeEncodeError:
            return {**_refusal(tool, path, f"{path} {text!r} cannot be encoded with the port's encoding {encoding!r}, so it could never match."), "encoding": encoding}
    return {"ok": True, "entries": [text for _, text in texts], "patterns": patterns}


def find_until(buffer: bytes | bytearray, patterns: Sequence[bytes]) -> tuple[int, int] | None:
    """Where the first match in `buffer` ends, and whose it is.

    Answers `(end, index)`: `buffer[:end]` runs through the end of the match
    that ends first, which is as little as answers the caller, and
    `patterns[index]` is its pattern, the first listed of those ending on the
    same byte. None when no pattern occurs in `buffer`. All occurrences of one
    pattern have its length, so its first occurrence is also the one that ends
    first, and one search per pattern is enough."""
    first: tuple[int, int] | None = None
    for index, pattern in enumerate(patterns):
        start = buffer.find(pattern)
        if start < 0:
            continue
        end = start + len(pattern)
        if first is None or end < first[0]:
            first = (end, index)
    return first


def until_ids(until_id: object, *, tool: str = "can_read", field: str = "until_id") -> JsonObject:
    """Check `until_id`: an arbitration id from 0 to 0x1FFFFFFF, or 1 to 8 of them.

    Answers `{"ok": True, "ids": [...]}`, the ids as a list whatever shape they
    were given in, or the refusal naming the id at fault. A number JSON carries
    as a float is taken when it is whole, as the input schema takes it."""
    if isinstance(until_id, list):
        if not 1 <= len(until_id) <= UNTIL_MAX_ENTRIES:
            return _refusal(tool, field, f"{field} must be an id or a list of 1 to {UNTIL_MAX_ENTRIES} of them.")
        entries = [(f"{field}[{index}]", entry) for index, entry in enumerate(until_id)]
    else:
        entries = [(field, until_id)]
    ids: list[int] = []
    for path, entry in entries:
        if isinstance(entry, float) and entry.is_integer():
            entry = int(entry)
        if isinstance(entry, bool) or not isinstance(entry, int) or not 0 <= entry <= CAN_ID_MAX:
            return _refusal(tool, path, f"{path} must be an integer from 0 to 0x{CAN_ID_MAX:X}.")
        ids.append(entry)
    return {"ok": True, "ids": ids}


def _refusal(tool: str, field: str, summary: str) -> JsonObject:
    return {"ok": False, "tool": tool, "error_type": "invalid_argument", "field": field, "summary": summary}
