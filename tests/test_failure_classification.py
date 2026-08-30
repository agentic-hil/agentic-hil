"""Every backend failure is classified by its own line.

#327 fixed the one transcript that had bench evidence behind it. While fixing it
the classification tables of all three backends were measured by calling
``_classify_output`` directly, and the defect turned out to be a family rather
than an instance (#333): seven transcripts, three backends, every one of them
answered with a classification the tool had not reported.

The seven literals below are those seven rows. They are literals on purpose, the
way ``STLINK_ERASE_REFUSED_TRANSCRIPT`` in ``test_diagnosable_failures`` is: each
one holds an ordinary line about a reset that the tool prints while it works,
lines away from the failure it actually reported, and paraphrasing them would
throw away the only thing that made the old rules answer ``reset_failed``.

The mirror-image tests matter as much. A rule that stopped answering
``reset_failed`` for a refused erase by never answering ``reset_failed`` at all
would pass every row above and lose the failure the reset rule exists for, so
each backend also proves a genuine reset failure still classifies as one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    FAKE_OPENOCD_ERASE_REFUSED,
    FAKE_PYOCD_ERASE_REFUSED,
    write_config,
)

from agentic_hil.backends.openocd import OpenOCDBackend
from agentic_hil.backends.pyocd import PyOCDBackend
from agentic_hil.backends.stlink import STLinkBackend
from agentic_hil.config import load_config
from agentic_hil.tools import AgenticHILToolService

# STM32CubeProgrammer v2.22.0 opening any action, transcribed from the bench
# report behind #327. `Reset mode  : Software reset` is the only occurrence of
# the word "reset" in a run that never resets anything, and it is printed before
# the CLI has done a thing, so it precedes every failure this backend can report.
STLINK_CONNECT_BANNER = """      -------------------------------------------------------------------
                       STM32CubeProgrammer v2.22.0
      -------------------------------------------------------------------

ST-LINK SN  : 002E00073431511834333935
Board       : NUCLEO-F446RE
Voltage     : 3.27V
SWD freq    : 8000 KHz
Connect mode: Hot Plug
Reset mode  : Software reset
Device ID   : 0x421
Device name : STM32F446

Memory Programming ...
Opening and parsing file: firmware.elf
"""

# OpenOCD prints this on nearly every reset of a Cortex-M core with no srst
# wired. It is a statement about how the reset it just performed was carried out,
# not a report that one failed.
OPENOCD_RESET_WARNING = (
    "Warn : Only resetting the Cortex-M core, use a reset-init event handler to reset any peripherals "
    "or configure hardware srst support.\n"
)
# pyOCD logs what it is doing as it does it, and a flash run resets the target on
# the way in. The line reports a reset that worked.
PYOCD_RESET_LINE = "0000684 I Resetting target with default reset type [board]\n"

OPENOCD_ERASE_REFUSAL = "Error: failed erasing sectors 0 to 5\n"
PYOCD_ERASE_REFUSAL = "0000902 E Failed to erase sector at 0x08000000 [flash]\n"

# The measured table from #333, row for row, in the order the issue lists it. The
# third column is what each row classified as before the fix; the fourth is the
# classification the tool's own words support.
MEASURED_TABLE = [
    ("stlink", "download refused", STLINK_CONNECT_BANNER + "Error: failed to download the file\n", "reset_failed", "flash_failed"),
    ("stlink", "download failed", STLINK_CONNECT_BANNER + "Error: download failed\n", "reset_failed", "flash_failed"),
    ("stlink", "internal command error", STLINK_CONNECT_BANNER + "Error: internal command error\n", "reset_failed", "flash_failed"),
    ("openocd", "erase refused", OPENOCD_ERASE_REFUSAL, "flash_failed", "flash_erase_failed"),
    ("openocd", "erase refused after a reset warning", OPENOCD_RESET_WARNING + OPENOCD_ERASE_REFUSAL, "reset_failed", "flash_erase_failed"),
    ("pyocd", "erase refused", PYOCD_ERASE_REFUSAL, "flash_failed", "flash_erase_failed"),
    ("pyocd", "erase refused after a reset log line", PYOCD_RESET_LINE + PYOCD_ERASE_REFUSAL, "reset_failed", "flash_erase_failed"),
]

# One genuine reset failure per backend, in each tool's own error style. Unlike
# the table above, which is measured, these are representative: the property
# under test is structural rather than a particular release's wording, and it is
# that a tool reporting a failed operation names the operation on the line it
# reports the failure on. Wording that no rule has seen is covered separately, by
# the operation-anchored bucket at the bottom of each table.
GENUINE_RESET_FAILURES = [
    ("stlink", STLINK_CONNECT_BANNER + "Error: failed to reset the target\n"),
    ("openocd", "Info : device id = 0x10006421\nError: timed out while waiting for target halted\nError: Error executing event reset-assert on target stm32f4x.cpu\n"),
    ("pyocd", "0000684 E Error attempting to reset target: reset failed [board]\n"),
]


def backend_for(name: str, workspace: Path):
    config = load_config(str(write_config(workspace, debugger_type=name)))
    return {"openocd": OpenOCDBackend, "pyocd": PyOCDBackend, "stlink": STLinkBackend}[name](config)


@pytest.mark.parametrize(
    ("backend_name", "label", "transcript", "was", "expected"),
    MEASURED_TABLE,
    ids=[f"{row[0]}-{row[1].replace(' ', '-')}" for row in MEASURED_TABLE],
)
def test_the_measured_table_is_classified_by_the_line_that_reported_the_failure(
    backend_name: str, label: str, transcript: str, was: str, expected: str, tmp_path: Path
) -> None:
    backend = backend_for(backend_name, tmp_path)

    classified = backend._classify_output(transcript, "flash_firmware")

    assert classified == expected, f"{backend_name} {label}"
    # Named rather than merely implied: every row of the table came back as one
    # of these before the fix, and a future rule that reintroduced either would
    # be reintroducing the defect this test exists for.
    assert classified != was


@pytest.mark.parametrize(("backend_name", "transcript"), GENUINE_RESET_FAILURES, ids=[row[0] for row in GENUINE_RESET_FAILURES])
def test_a_genuine_reset_failure_is_still_a_reset_failure(backend_name: str, transcript: str, tmp_path: Path) -> None:
    """The mirror image, per backend.

    Anchoring the reset rule to the failing line narrows where it looks, not what
    it looks for. A tool that reports a failed reset names the reset on the line
    it reports the failure on, so every one of these still classifies.

    Asked as `flash_firmware` on purpose: a reset can fail during a flash, and
    the tool this asks for is the one thing that cannot be answering here, so
    only the anchored rule can produce the classification.
    """
    backend = backend_for(backend_name, tmp_path)

    assert backend._classify_output(transcript, "flash_firmware") == "reset_failed"


@pytest.mark.parametrize("backend_name", ["stlink", "openocd", "pyocd"])
def test_a_reset_that_failed_in_words_no_rule_knows_is_still_a_reset_failure(backend_name: str, tmp_path: Path) -> None:
    """The other half of keeping the reset failure: the operation, not the word.

    A line-anchored rule can only classify wording somebody has seen, and the
    three tools here word a failed reset differently and across releases. When
    the tool is `reset_target` the operation that reported a failure is a reset
    whatever it was called, which is exactly the reasoning behind the
    `flash_firmware` bucket that has sat at the bottom of every one of these
    tables all along.
    """
    backend = backend_for(backend_name, tmp_path)
    unfamiliar = "Info : device id = 0x10006421\nError: timed out while waiting for target halted\n"

    assert backend._classify_output(unfamiliar, "reset_target") == "reset_failed"
    # And the same transcript under any other tool is not silently called one:
    # nothing in it says a reset failed, and this rule claims only what the tool
    # it was asked for justifies.
    assert backend._classify_output(unfamiliar, "probe_target") != "reset_failed"


def test_an_informational_reset_line_does_not_make_an_unrelated_failure_a_reset_failure(tmp_path: Path) -> None:
    """The rule stated directly, apart from any one backend's vocabulary.

    Two lines: one says a reset happened, the other says something else failed.
    Neither line says a reset failed, so neither does the classification.
    """
    backend = backend_for("openocd", tmp_path)
    transcript = "Info : Resetting target\nError: failed erasing sectors 0 to 5\n"
    with_the_reset_line_removed = "Error: failed erasing sectors 0 to 5\n"

    assert backend._classify_output(transcript, "flash_firmware") == backend._classify_output(with_the_reset_line_removed, "flash_firmware")


def flash_through_the_service(workspace: Path, backend_name: str, executable: Path) -> dict:
    firmware = workspace / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True, exist_ok=True)
    firmware.write_bytes(b"\x7fELFfake")
    probe_id = {"openocd": "STLINK123", "pyocd": "PYOCD123"}[backend_name]
    target_type = "stm32f446retx" if backend_name == "pyocd" else None
    config = load_config(str(write_config(workspace, debugger_type=backend_name, debugger_executable=executable, probe_id=probe_id, target_type=target_type)))
    service = AgenticHILToolService(config)
    try:
        return service.call("flash_firmware", {"image_path": "build/firmware.elf"})
    finally:
        service.close()


def test_the_openocd_erase_refusal_reaches_the_operator_in_openocds_own_terms(tmp_path: Path) -> None:
    """Driven through the real backend and the real CLI path, not asserted in isolation.

    A classification with no catalogue entry behind it is a new word for the same
    silence, so this proves the entry is reachable: the summary says erase, the
    causes name protection and the flash bank rather than the reset line, and the
    remedy names the option bytes and `target_cfg`.
    """
    refused = flash_through_the_service(tmp_path, "openocd", FAKE_OPENOCD_ERASE_REFUSED)
    causes = " ".join(refused["likely_causes"])
    steps = " ".join(refused["remediation"])

    assert refused["ok"] is False
    assert refused["error_type"] == "flash_erase_failed"
    assert refused["backend_error_type"] == "flash_erase_failed"
    assert "erase" in refused["summary"] and "unconfirmed" in refused["summary"]
    assert "protect" in causes and "target_cfg" in causes
    assert "reset line wiring issue" not in refused["likely_causes"]
    assert "option bytes" in steps and "target_cfg" in steps
    assert "reset problem" in " ".join(refused["do_not"])


def test_the_pyocd_erase_refusal_reaches_the_operator_in_pyocds_own_terms(tmp_path: Path) -> None:
    refused = flash_through_the_service(tmp_path, "pyocd", FAKE_PYOCD_ERASE_REFUSED)
    causes = " ".join(refused["likely_causes"])
    steps = " ".join(refused["remediation"])

    assert refused["ok"] is False
    assert refused["error_type"] == "flash_erase_failed"
    assert refused["backend_error_type"] == "flash_erase_failed"
    assert "erase" in refused["summary"] and "unconfirmed" in refused["summary"]
    assert "protect" in causes and "target_type" in causes
    assert "reset line wiring issue" not in refused["likely_causes"]
    assert "option bytes" in steps and "target_type" in steps
    assert "reset problem" in " ".join(refused["do_not"])
