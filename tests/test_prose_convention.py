"""No em-dash and no en-dash anywhere in the tracked tree.

The prose in this repository is punctuated with commas, colons, parentheses and
full stops. U+2014 and U+2013 are not used, in code, in comments, in docstrings,
in string literals, in JSON schema descriptions, in Markdown or in YAML. The
convention had no gate until now, and a tree with no gate drifts: the sweep that
removed the last of 2072 em-dashes and 4 en-dashes across 98 files only stays
swept if something fails the next one on the way in.

The check is deliberately whole-tree rather than per-directory. A dash reaches
the repository through whatever file somebody happened to be editing, so a gate
that guarded a subset would be a gate that names the places nobody was going to
edit anyway. Reading every tracked file costs a few hundredths of a second, and
that buys a rule with no corners in it.

The one thing a dash may still be is somebody else's. Where a string exists to
match the output of an external tool and that tool's real output carries the
dash, the dash is not this repository's to change, and rewriting it would break
the match while looking like a tidy-up. Those are enumerated in
``EXTERNAL_OUTPUT`` below, and nowhere else.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# Named by codepoint rather than written out, so this file, which the gate below
# reads along with every other, carries none of what it refuses.
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

# The complete list of dashes this repository keeps, and the only exception the
# gate below admits. An entry needs three things:
#
# * the tracked path it lives in, relative to the repository root,
# * the exact string carrying the dash, so a wording change stops matching and
#   the gate speaks up again rather than quietly widening,
# * why the dash is external, meaning which tool prints it and why reproducing
#   it verbatim is the point.
#
# A dash in prose this repository writes is never an entry here. It is a defect,
# and the fix is the punctuation the sentence already meant.
EXTERNAL_OUTPUT: tuple[tuple[str, str, str], ...] = (
    (
        "tests/test_install_eval.py",
        f"Warning: Unknown --effort value 'bogus' {EM_DASH} ignoring it and using the default effort.",
        "the literal warning the Claude Code CLI prints when it is handed an effort it does not know. "
        "The test writes it into a transcript so `reasoning_effort_evidence` is exercised against the "
        "real sentinel; paraphrasing it would leave the test passing while the wording it stands for "
        "moved under a CLI bump.",
    ),
)


def tracked_files() -> list[str]:
    """Every path git holds, or a skip where git cannot be asked.

    An unpacked sdist and a checkout with no git binary can answer nothing about
    what is tracked, and a gate that guessed at the answer would be reading
    build output and virtualenvs. Every place this suite is meant to run is a
    clone: CI, the Linux container in `tools/ci_linux.py`, and the review loop's
    checkouts all have git and a repository behind them.
    """
    try:
        listing = subprocess.run(
            ["git", "-C", str(REPOSITORY_ROOT), "ls-files", "-z"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover - needs a host without git
        pytest.skip(f"git could not list the tracked tree: {error}")
    if listing.returncode != 0:  # pragma: no cover - needs a directory that is not a repository
        pytest.skip(f"git could not list the tracked tree: {listing.stderr.strip() or listing.returncode}")
    return [name for name in listing.stdout.split("\0") if name]


def external_spans(relative: str, text: str) -> list[range]:
    """The character ranges in one file that an allowlist entry covers."""
    spans = []
    for path, string, _ in EXTERNAL_OUTPUT:
        if path != relative:
            continue
        start = text.find(string)
        while start != -1:
            spans.append(range(start, start + len(string)))
            start = text.find(string, start + 1)
    return spans


def offending_lines(relative: str, text: str) -> list[tuple[int, str]]:
    """Every line of one file that carries a dash no allowlist entry covers."""
    allowed = external_spans(relative, text)
    found = []
    offset = 0
    for number, line in enumerate(text.split("\n"), 1):
        for column, character in enumerate(line):
            if character in (EM_DASH, EN_DASH) and not any(offset + column in span for span in allowed):
                found.append((number, line))
                break
        offset += len(line) + 1
    return found


def test_the_tracked_tree_carries_no_em_dash_and_no_en_dash() -> None:
    offences: dict[str, list[tuple[int, str]]] = {}
    for relative in tracked_files():
        try:
            text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # A binary, or a path this checkout does not hold. Neither carries
            # prose, and neither is this gate's to judge.
            continue
        if EM_DASH not in text and EN_DASH not in text:
            continue
        lines = offending_lines(relative, text)
        if lines:
            offences[relative] = lines

    if not offences:
        return

    total = sum(len(lines) for lines in offences.values())
    report = [f"{total} line(s) carry an em-dash (U+2014) or an en-dash (U+2013):", ""]
    for relative, lines in sorted(offences.items()):
        report.append(f"{relative}, line(s) {', '.join(str(number) for number, _ in lines)}:")
        report.extend(f"    {number}: {line.strip()}" for number, line in lines)
        report.append("")
    report.append(
        "Replace each one with the punctuation the sentence already means: a comma where the clause "
        "continues, a colon where what follows explains or lists, parentheses for a pair, a full stop "
        "where two sentences read better apart, and the word 'to' or a plain hyphen in a range. Do not "
        "reword. A dash that reproduces an external tool's own output belongs in EXTERNAL_OUTPUT in "
        f"{Path(__file__).name} instead, with the exact string and why it is not ours to change."
    )
    pytest.fail("\n".join(report), pytrace=False)


def test_every_allowlisted_string_is_still_there_and_still_carries_a_dash() -> None:
    """An exemption nothing matches is an exemption that has stopped being read.

    The gate is only as narrow as this list is honest. An entry whose string has
    been edited, moved or deleted exempts nothing and says so here, rather than
    sitting in the file as a standing permission for a dash somebody may add to
    that path later.
    """
    for relative, string, reason in EXTERNAL_OUTPUT:
        path = REPOSITORY_ROOT / relative
        assert path.is_file(), f"{relative} is allowlisted and is not in the tree"
        assert EM_DASH in string or EN_DASH in string, f"{relative}: the allowlisted string carries no dash"
        assert string in path.read_text(encoding="utf-8"), f"{relative} no longer contains its allowlisted string"
        assert reason.strip(), f"{relative}: an allowlist entry has to say why the dash is external"
