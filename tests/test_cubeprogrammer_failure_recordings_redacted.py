"""Hold the committed STM32CubeProgrammer failure recordings to their redacted probe serial.

The fixture holds what the product's own action log printed, with the probe's
serial replaced by ``[redacted]``. The ordinary suite replays those recordings
but nothing checks them for a serial, so a later recording could commit one.
These tests read every recording in the fixture, so one added later is covered
too, and show that the check fires on a raw serial planted in each field.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import support

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "stm32cubeprogrammer_2_23_0_failure_recordings.json"
RECORDINGS = json.loads(FIXTURE.read_text(encoding="utf-8"))["recordings"]
MISSING_CHECK = (
    "tests/support.py offers no unredacted_serials, so nothing holds the committed CubeProgrammer recordings "
    "to a redacted probe serial"
)
PLANTED_FORMS = (
    "sn=0028FACE0000BEEF",
    "sn=SECRET-SERIAL",
    "ST-LINK SN  : 0028FACE0000BEEF",
    # The exact-equality boundaries: an empty sn= or banner value, a value that
    # only starts with [redacted], and a banner value with text after [redacted].
    "sn=",
    "sn=[redacted]X",
    "ST-LINK SN  :",
    "ST-LINK SN  : [redacted] 0028FACE0000BEEF",
)


def unredacted_serials_check() -> Callable[[dict], list[str]]:
    """The check in tests/support.py, or the declared gap while that module does not offer it."""
    check = getattr(support, "unredacted_serials", None)
    if not callable(check):
        raise AssertionError(MISSING_CHECK)
    return check


def planted_copies(recording: dict, form: str) -> Iterator[tuple[str, dict]]:
    """A copy of the recording for each field, with the raw serial form put into that one field alone."""
    for index in range(len(recording["argv"])):
        planted = copy.deepcopy(recording)
        planted["argv"][index] = form
        yield f"argv[{index}]", planted
    for field in ("stdout", "stderr"):
        planted = copy.deepcopy(recording)
        planted[field] += f"\n{form}\n"
        yield field, planted


def test_every_committed_cubeprogrammer_failure_recording_keeps_its_serial_redacted() -> None:
    check = unredacted_serials_check()
    assert RECORDINGS, "the fixture holds no recordings under \"recordings\" to check"
    findings: dict[str, list[str]] = {}
    for name, recording in RECORDINGS.items():
        found = check(recording)
        if found:
            findings[name] = found
    assert not findings, f"unredacted probe serial in the committed CubeProgrammer recordings: {findings}"


def test_a_raw_serial_planted_in_any_recording_field_is_reported() -> None:
    check = unredacted_serials_check()
    for name, recording in RECORDINGS.items():
        assert check(recording) == [], f"{name} has findings before anything is planted: {check(recording)}"
        for form in PLANTED_FORMS:
            for label, planted in planted_copies(recording, form):
                found = check(planted)
                assert any(finding.startswith(f"{label}: ") for finding in found), (
                    f"{name}: {form!r} planted in {label} was not reported, got {found}"
                )
