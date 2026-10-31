"""The refusals the suite replays are ones the tools print (#740).

#516 named two refusals that the recordings since have shown no tool prints:
STM32CubeProgrammer refusing `--version` with exit 2, and a pyOCD line about a
failed verify. These read the row tables and the parametrization of
``test_debug_backend_refusals.py`` against the recordings, so a row that plays
words no recording or cited source holds fails here instead of standing in for
the tool. The phrases are read out of the fixtures and not repeated in this
file, which would otherwise carry the words it refuses.

#738 found the same for STM32CubeProgrammer's failed verify: the
`verify_failed:stlink` entry quoted a line the CLI never prints. The two lines
it does print are written here as shapes, because they are the tool's own
words, and one test holds them to the recording while another holds the entry
to them.
"""

from __future__ import annotations

import ast
import json
import re

import test_debug_backend_refusals as refusals

from agentic_hil.knowledge import catalogue_entry

KNOWLEDGE = refusals.ROOT.parent / "src" / "agentic_hil" / "knowledge.py"
# STM32CubeProgrammer 2.23.0's two lines about a failed verify on its legacy path,
# as the recording prints them, with each value and each run of spaces left open.
# The first byte is the one read back and the second the one the file holds.
CUBE_VERIFY_LINES = (
    r"Error: Data mismatch found at address{gap}{address}{gap}\(byte = {read} instead of {expected}\)",
    r"Error: Download verification failed",
)
# A value as the CLI printed it.
RECORDED_VALUE = r"0x[0-9A-F]+"
# What limits the recorded words to the path they were recorded on: the CLI's own
# announcement, or a sentence that ties the legacy verify path to files under 32KB.
LEGACY_PATH_LIMITS = (
    re.compile(re.escape("File size < 32KB legacy verify will be used")),
    re.compile(r"legacy verify path\b[^.]*?\b(?:files|images) (?:under|below|smaller than) 32 ?KB\b", re.IGNORECASE),
)


def cube_verify_line_shapes(address: str, read: str, expected: str) -> list[re.Pattern[str]]:
    """The two verify lines, with each pattern standing for the value in its own slot."""
    return [re.compile(line.format(gap=r"\s+", address=address, read=read, expected=expected)) for line in CUBE_VERIFY_LINES]


def catalogue_slot(role: str) -> str:
    """A slot as the catalogue may write it: its own placeholder, such as `<read>`, or a value the CLI printed."""
    return f"(?:<{role}>|{RECORDED_VALUE})"


def written_entry(key: str) -> dict:
    """The fields of one catalogue entry as knowledge.py writes them, read from the source."""
    tree = ast.parse(KNOWLEDGE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for name, value in zip(node.keys, node.values, strict=True):
            if isinstance(name, ast.Constant) and name.value == key and isinstance(value, ast.Call):
                return {keyword.arg: ast.literal_eval(keyword.value) for keyword in value.keywords}
    raise AssertionError(f"knowledge.py writes no {key} entry")


def pyocd_transcripts() -> list[str]:
    """Every transcript a pyOCD row of the three tables plays."""
    transcripts = [row[2] for row in refusals.CLASSIFIER_ROWS if row[0] == "pyocd"]
    transcripts += [row[3] for row in refusals.TOOL_RESULT_ROWS if row[0] == "pyocd"]
    for row in refusals.SCOPED_REMEDIATION_ROWS:
        if row[0] == "pyocd":
            transcripts += [row[1], row[2]]
    return transcripts


def test_no_pyocd_row_plays_a_phrase_pyocd_does_not_print() -> None:
    """A phrase the pyOCD 0.45.1 package does not contain is in no pyOCD transcript.

    The list is the one ``fixtures/pyocd_failure_recordings.json`` keeps beside
    the lines read off the installed package, under ``not_in_the_package``.
    """
    absent = refusals.PYOCD_FAILURES["phrases_in_the_pyocd_source"]["not_in_the_package"]
    transcripts = pyocd_transcripts()
    if any(phrase in transcript for phrase in absent for transcript in transcripts):
        raise AssertionError("a pyOCD row plays a phrase pyOCD 0.45.1 does not print")
    assert absent, refusals.PYOCD_FAILURES["phrases_in_the_pyocd_source"]
    assert transcripts, "the three tables hold no pyOCD row"


def version_check_backends() -> list[str]:
    """The backends `test_debugger_info_reports_a_failing_version_check` runs for."""
    test = refusals.test_debugger_info_reports_a_failing_version_check
    (mark,) = [mark for mark in test.pytestmark if mark.name == "parametrize" and mark.args[0] == "backend_name"]
    return list(mark.args[1])


def refuses(recording: dict | None) -> bool:
    return recording is not None and recording.get("returncode", 0) != 0


def recorded_unknown_option(backend: str) -> bool:
    """Whether a recording holds this backend refusing an option it does not know.

    OpenOCD's and pyOCD's are in ``fixtures/debugger_refusal_recordings.json``.
    STM32CubeProgrammer's would be an ``unknown_option`` entry among its 2.23.0
    USB-free recordings, which the licensed container stage reruns.
    """
    if refuses(refusals.RECORDINGS["recordings"].get(f"{backend}_unknown_option")):
        return True
    if backend != "stlink":
        return False
    cube = json.loads((refusals.FIXTURES / "stm32cubeprogrammer_2_23_0_recordings.json").read_text(encoding="utf-8"))
    return refuses(cube["recordings"].get("unknown_option"))


def test_the_failing_version_check_replays_only_recorded_refusals() -> None:
    """Every backend the failing version check runs for plays a recorded refusal.

    STM32CubeProgrammer 2.23.0's `--version` exits 0 and prints its version, so
    a non-zero `--version` of its own is a refusal nobody has recorded, and the
    test may not run for it until one is.
    """
    backends = version_check_backends()
    unrecorded = [backend for backend in backends if not recorded_unknown_option(backend)]
    if unrecorded or hasattr(refusals, "STLINK_VERSION_REFUSED"):
        raise AssertionError("the failing version check plays a refusal no recording holds")
    assert {"openocd", "pyocd"} <= set(backends), backends


def test_the_pyocd_verify_entry_says_pyocd_does_not_verify() -> None:
    """The entry stays as a guard, and says why no pyOCD run of this backend reaches it.

    The flash this backend runs reports `verify: false`, and the `flash_firmware`
    definition already says "pyOCD does not verify (verify false)". With the
    rows that played an invented verify line gone, the entry is what tells a
    reader that the bucket is a guard and not a failure pyOCD reports.
    """
    entry = catalogue_entry("verify_failed:pyocd")
    if entry is None or not re.search(r"\bpyOCD does not verify\b", str(entry["meaning"]), re.IGNORECASE):
        raise AssertionError("verify_failed:pyocd does not say that pyOCD does not verify")


def test_the_stlink_verify_entry_quotes_the_lines_cubeprogrammer_prints() -> None:
    """Every `Error:` line the entry quotes is one the CLI prints, and it quotes both (#738).

    The address and the two bytes are on the Data mismatch line, so the entry
    may write them as placeholders or as recorded values, but each placeholder
    only in its own slot: `<address>`, then `<read>`, then `<expected>`, the
    order the CLI prints them in. Swapping the two bytes would send the operator
    looking for the file's byte on the board. The words were recorded on the
    legacy verify path the CLI announces for files under 32KB, and the entry has
    to tie them to that limit rather than claim them for a larger image.
    """
    entry = written_entry("verify_failed:stlink")
    text = "\n".join([entry["meaning"], *entry["remediation"], *entry.get("do_not", ())])
    shapes = cube_verify_line_shapes(catalogue_slot("address"), catalogue_slot("read"), catalogue_slot("expected"))
    quoted = re.findall(r"`(Error:[^`]*)`", text)
    if (
        any(not any(shape.fullmatch(span) for shape in shapes) for span in quoted)
        or any(not any(shape.fullmatch(span) for span in quoted) for shape in shapes)
        or text.count("Error:") != len(quoted)
        or not any(limit.search(text) for limit in LEGACY_PATH_LIMITS)
    ):
        raise AssertionError("verify_failed:stlink quotes a line STM32CubeProgrammer 2.23.0 does not print")
    # What the source writes is what the catalogue serves.
    assert catalogue_entry("verify_failed:stlink")["meaning"] == entry["meaning"]


def test_the_lines_the_stlink_verify_entry_must_quote_are_the_recorded_ones() -> None:
    """The shapes the entry is held to are the recording's own `Error:` lines, in order and none besides."""
    recording = refusals.cube_verify_failure_recording()
    stdout = recording["stdout"]
    errors = [line for line in stdout.splitlines() if line.startswith("Error:")]

    shapes = cube_verify_line_shapes(RECORDED_VALUE, RECORDED_VALUE, RECORDED_VALUE)
    assert [[shape.fullmatch(line) is not None for shape in shapes] for line in errors] == [[True, False], [False, True]], errors
    assert "\nFile size < 32KB legacy verify will be used\n" in stdout, stdout
    # The CLI's announcement, which the entry may quote as its limit, is the recording's own line.
    assert LEGACY_PATH_LIMITS[0].search(stdout), stdout
    assert recording["returncode"] == 1, recording["returncode"]
