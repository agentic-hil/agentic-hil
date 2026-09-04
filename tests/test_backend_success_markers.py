"""The backend's own success marker outranks the words on the way to it.

Every command this backend sends ends with an `echo` of a marker placed after the
one operation the tool exists for, and OpenOCD's interpreter stops evaluating a
`-c` script at the first command that fails. So a marker in the output is
OpenOCD's own statement that the operation's command returned success, and a
failure word printed while it succeeded is not a second, contradicting verdict.

Until #425 it was read as one. On Ubuntu 24.04 with the distribution's OpenOCD
0.12.0-1build2 and a Nucleo-F446RE, `reset run` over hla_swd writes `Error: Error
setting register pc` after the core has already restarted; the exit code was 0,
the marker was printed, the firmware's boot line arrived on the UART 350 ms
later, and the step was refused as `reset_failed` on that one word. The Windows
bench's OpenOCD build never writes the line, so the same plan passed there.

The transcript below is that run, verbatim from the issue, and it is used three
ways: as it stands, with the marker taken out, and with the exit code changed.
Only the first is a success, which is the whole of the change: the marker and the
exit code decide, and everything else is evidence carried alongside them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_config

from agentic_hil.backends import openocd as openocd_backend
from agentic_hil.backends.common import CompletedCommand
from agentic_hil.config import load_config
from agentic_hil.tools import AgenticHILToolService

# The #425 transcript, verbatim. On stderr as one block because that is where
# OpenOCD writes it: its log stream carries the `echo` markers and the error
# lines together, in the order it wrote them.
UBUNTU_HLA_RESET_TRANSCRIPT = """[stm32f4x.cpu] halted due to breakpoint, current mode: Thread
xPSR: 0x61000000 pc: 0x2000002e msp: 0x20020000
AGENTIC_HIL_STAGE:init:ok
Info : Unable to match requested speed 2000 kHz, using 1800 kHz
Error: Error setting register pc
AGENTIC_HIL_RESULT:reset_target:ok
shutdown command invoked
"""
THE_ERROR_LINE = "Error: Error setting register pc"
RESET_MARKER = "AGENTIC_HIL_RESULT:reset_target:ok"
WITHOUT_THE_MARKER = UBUNTU_HLA_RESET_TRANSCRIPT.replace(f"{RESET_MARKER}\n", "")

# The same shape on the other two tools, whose markers sit after their own
# operation in exactly the same way. `libusb_open` is the line OpenOCD writes for
# every USB device it may not open while enumerating, including on a run that
# goes on to open the right one; `checksum mismatch - attempting binary compare`
# is what `verify_image` writes before the byte compare that then succeeds. Each
# was a classification before #425: `adapter_not_found` and `verify_failed`.
PROBE_TRANSCRIPT = """Info : clock speed 2000 kHz
Error: libusb_open() failed with LIBUSB_ERROR_ACCESS
AGENTIC_HIL_STAGE:init:ok
    TargetName         Type       Endian TapName            State
--  ------------------ ---------- ------ ------------------ ------------
 0* stm32f4x.cpu       hla_target little stm32f4x.cpu       halted
AGENTIC_HIL_RESULT:probe_target:ok
"""
FLASH_TRANSCRIPT = """AGENTIC_HIL_STAGE:init:ok
** Programming Started **
** Programming Finished **
** Verify Started **
Error: checksum mismatch - attempting binary compare
** Verified OK **
AGENTIC_HIL_RESULT:flash_firmware:ok
"""
# The one classification that still decides with the marker printed: OpenOCD's
# own report that the sectors this image covers were not erased, which leaves the
# flash contents unstated whatever the rest of the run says.
FLASH_ERASE_REFUSED_TRANSCRIPT = """AGENTIC_HIL_STAGE:init:ok
** Programming Started **
Error: failed erasing sectors 0 to 5
AGENTIC_HIL_RESULT:flash_firmware:ok
"""


def openocd_answering(monkeypatch: pytest.MonkeyPatch, transcript: str, returncode: int) -> None:
    """One OpenOCD run, with the capture and the exit code named exactly.

    Patched at the spawn rather than faked as an executable so that the three
    directions share one literal transcript: a copy of it in a fixture program
    per direction is a copy that can drift, and what is under test is which of
    the two facts around the same words the result is read off.
    """
    monkeypatch.setattr(
        openocd_backend,
        "spawn_command",
        lambda *args, **kwargs: CompletedCommand(stdout="", stderr=transcript, returncode=returncode, timed_out=False, not_found=False),
    )


def call_through_the_service(workspace: Path, tool: str, arguments: dict | None = None) -> dict:
    config = load_config(str(write_config(workspace)))
    service = AgenticHILToolService(config)
    try:
        return service.call(tool, arguments or {})
    finally:
        service.close()


def flash_through_the_service(workspace: Path) -> dict:
    firmware = workspace / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True, exist_ok=True)
    firmware.write_bytes(b"\x7fELFfake")
    return call_through_the_service(workspace, "flash_firmware", {"image_path": "build/firmware.elf"})


def test_the_ubuntu_reset_is_a_success_that_carries_the_error_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#425 itself: the board reset, OpenOCD said so, and the step was refused.

    The evidence does not disappear into the log file: the line that used to
    decide the outcome is on the result, verbatim, and the summary says it came
    along, so a caller reading only the summary still learns that this run was
    not a quiet one.
    """
    openocd_answering(monkeypatch, UBUNTU_HLA_RESET_TRANSCRIPT, 0)

    reset = call_through_the_service(tmp_path, "reset_target", {"mode": "run"})

    assert reset["ok"] is True, reset
    assert reset["mode"] == "run"
    assert reset["success_confirmed"] is True
    assert "error_type" not in reset
    assert reset["backend_warnings"] == [THE_ERROR_LINE]
    assert reset["summary"].startswith("Target reset with mode 'run'.")
    assert "backend_warnings" in reset["summary"]
    # Unchanged by all of this, and named because the log is where the whole
    # capture stays: the result carries the decisive line, not a substitute for
    # the file.
    assert THE_ERROR_LINE in (tmp_path / reset["log_path"]).read_text(encoding="utf-8")


def test_the_same_transcript_without_the_marker_is_still_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction, one line different.

    Without the marker OpenOCD never stated that `reset run` returned success, so
    the words are all there is and they are read exactly as they were before: the
    operation-anchored bucket answers `reset_failed` for a `reset_target` run that
    reported a failure in words no rule knows (#333).
    """
    openocd_answering(monkeypatch, WITHOUT_THE_MARKER, 0)

    reset = call_through_the_service(tmp_path, "reset_target", {"mode": "run"})

    assert reset["ok"] is False
    assert reset["backend_error_type"] == "reset_failed"
    assert reset["error_type"] == "reset_failed"
    assert "backend_warnings" not in reset
    assert reset["programmer_output"]["stderr"] == WITHOUT_THE_MARKER


def test_a_silent_run_without_the_marker_is_still_unconfirmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbouring markerless branch, which this change must not move.

    A run that exits 0 saying nothing at all reported no outcome, and that is the
    absence of a verdict rather than a verdict: `reset_unconfirmed` under the
    hood, `reset_failed` to the caller, with the markers it did print named.
    """
    openocd_answering(monkeypatch, "Info : clock speed 2000 kHz\nAGENTIC_HIL_STAGE:init:ok\n", 0)

    reset = call_through_the_service(tmp_path, "reset_target", {"mode": "run"})

    assert reset["ok"] is False
    assert reset["backend_error_type"] == "reset_unconfirmed"
    assert reset["error_type"] == "reset_failed"
    assert reset["operation_result"]["matched_success_text"] == ["AGENTIC_HIL_STAGE:init:ok"]


def test_the_marker_does_not_rescue_a_run_that_exited_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both facts are required, and this is the one that is not the marker.

    OpenOCD exits non-zero when a command in its `-c` script failed, so a marker
    beside a non-zero exit is not the pair that means success: something after
    the operation went wrong, or this build reported the run itself as failed.
    Classification is untouched on that path.
    """
    openocd_answering(monkeypatch, UBUNTU_HLA_RESET_TRANSCRIPT, 1)

    reset = call_through_the_service(tmp_path, "reset_target", {"mode": "run"})

    assert reset["ok"] is False
    assert reset["error_type"] == "reset_failed"
    assert "backend_warnings" not in reset


def test_a_probe_is_not_refused_for_a_libusb_line_beside_its_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same rule on the read-only tool, whose marker follows `targets`."""
    openocd_answering(monkeypatch, PROBE_TRANSCRIPT, 0)

    probed = call_through_the_service(tmp_path, "probe_target")

    assert probed["ok"] is True, probed
    assert probed["target_detected"] is True
    assert probed["backend_warnings"] == ["Error: libusb_open() failed with LIBUSB_ERROR_ACCESS"]
    assert probed["summary"].startswith("Target detected through OpenOCD.")


def test_a_flash_is_not_refused_for_a_checksum_line_beside_its_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """And on the tool whose marker follows `program ... verify`.

    Every verified flash prints the word the old rule matched on, because OpenOCD
    announces the phase with `** Verify Started **`, so before #425 any `Error:`
    line in a flash transcript was a verify mismatch. The CRC compare that logs a
    mismatch and then passes the byte compare is the case that costs.
    """
    openocd_answering(monkeypatch, FLASH_TRANSCRIPT, 0)

    flashed = flash_through_the_service(tmp_path)

    assert flashed["ok"] is True, flashed
    assert flashed["verify"] is True
    assert flashed["backend_warnings"] == ["Error: checksum mismatch - attempting binary compare"]
    assert flashed["summary"].startswith("Firmware flashed and verified.")


def test_a_refused_erase_still_decides_over_the_flash_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one rule kept ahead of the marker, and why it is kept.

    `failed erasing sectors` is OpenOCD's own report that the operation it names
    did not happen, and it leaves a flash nobody can state the contents of. A
    marker printed over it would say the image is on the board.
    """
    openocd_answering(monkeypatch, FLASH_ERASE_REFUSED_TRANSCRIPT, 0)

    flashed = flash_through_the_service(tmp_path)

    assert flashed["ok"] is False
    assert flashed["backend_error_type"] == "flash_erase_failed"
    assert flashed["error_type"] == "flash_erase_failed"
    assert "backend_warnings" not in flashed


def test_a_quiet_success_carries_no_warnings_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The field is evidence, so it exists only where there is evidence."""
    openocd_answering(monkeypatch, "AGENTIC_HIL_STAGE:init:ok\nAGENTIC_HIL_RESULT:reset_target:ok\n", 0)

    reset = call_through_the_service(tmp_path, "reset_target", {"mode": "run"})

    assert reset["ok"] is True, reset
    assert "backend_warnings" not in reset
    assert reset["summary"] == "Target reset with mode 'run'."
