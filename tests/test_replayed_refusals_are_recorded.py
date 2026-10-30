"""The refusals the suite replays are ones the tools print (#740).

#516 named two refusals that the recordings since have shown no tool prints:
STM32CubeProgrammer refusing `--version` with exit 2, and a pyOCD line about a
failed verify. These read the row tables and the parametrization of
``test_debug_backend_refusals.py`` against the recordings, so a row that plays
words no recording or cited source holds fails here instead of standing in for
the tool. The phrases are read out of the fixtures and not repeated in this
file, which would otherwise carry the words it refuses.
"""

from __future__ import annotations

import json
import re

import test_debug_backend_refusals as refusals

from agentic_hil.knowledge import catalogue_entry


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
