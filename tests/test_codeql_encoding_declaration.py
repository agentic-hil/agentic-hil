"""No Python file opens with words CodeQL reads as an encoding declaration.

PEP 263 lets the first two lines of a source file name its encoding, and
CPython only listens to a comment: `# -*- coding: utf-8 -*-`. CodeQL's Python
extractor is looser. `src/agentic_hil/defmt.py` opened with a docstring whose
first words were `Native defmt decoding: from the bytes`, and the extractor logged
`defmt.py has encoding 'from'`, failed to read the file and reported it as a
parse error. Python compiled it without a word, so nothing else noticed, and
for as long as that line stood CodeQL analyzed none of the module: the decoder
that parses what a board sends over a serial line.

So the first two lines of every Python file are held to the stricter reading:
wherever CodeQL would find `coding:` or `coding=` followed by a name, the line
has to be a comment CPython reads the same way. The predicate is exercised on
synthetic inputs that must fail before the real files are put through it, so a
check that could no longer catch anything says so.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# The directories CodeQL's Python analysis extracts from. `tools/`, `evals/` and
# `examples/` are repository content a source distribution does not carry; what
# is absent here is skipped rather than reported.
SCANNED = ("src", "tests", "tools", "evals", "examples")

# CPython's reading: only a comment line declares an encoding (PEP 263).
PYTHON_DECLARATION = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
# The reading CodeQL's extractor showed: the same words anywhere on the line.
LOOSE_DECLARATION = re.compile(r"coding[:=][ \t]*([-\w.]+)")


def misread_declarations(text: str) -> list[tuple[int, str]]:
    """The lines among the first two where CodeQL finds an encoding CPython does not."""
    found = []
    for number, line in enumerate(text.splitlines()[:2], start=1):
        loose = LOOSE_DECLARATION.search(line)
        if loose is not None and PYTHON_DECLARATION.match(line) is None:
            found.append((number, loose.group(1)))
    return found


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('"""Native defmt decoding: from the bytes a board sends."""\n', [(1, "from")]),
        ('#!/usr/bin/env python3\n"""Transcoding=utf-8 is not a comment."""\n', [(2, "utf-8")]),
        ('x = "decoding:raw"\n', [(1, "raw")]),
    ],
)
def test_a_declaration_outside_a_comment_is_caught(source: str, expected: list[tuple[int, str]]) -> None:
    assert misread_declarations(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "# -*- coding: utf-8 -*-\n",
        "#!/usr/bin/env python3\n# vim: set fileencoding=utf-8 :\n",
        '"""Native defmt decoding, from the bytes a board sends."""\n',
        '"""A module."""\n\nDECODING: str = "only the first two lines are read"\n',
    ],
)
def test_a_comment_declaration_or_none_at_all_is_accepted(source: str) -> None:
    assert misread_declarations(source) == []


def test_no_python_file_opens_with_an_encoding_codeql_misreads() -> None:
    files = sorted(path for directory in SCANNED if (REPOSITORY_ROOT / directory).is_dir() for path in (REPOSITORY_ROOT / directory).rglob("*.py"))
    assert any(path.name == "defmt.py" for path in files), "the scan no longer reaches src/agentic_hil/defmt.py; it is not reading the tree"
    misread = {}
    for path in files:
        with path.open(encoding="utf-8", errors="replace") as handle:
            head = handle.readline() + handle.readline()
        if found := misread_declarations(head):
            misread[path.relative_to(REPOSITORY_ROOT).as_posix()] = found
    assert misread == {}, f"CodeQL would read these as encoding declarations and skip the file: {misread}"
