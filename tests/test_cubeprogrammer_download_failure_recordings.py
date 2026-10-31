"""STM32CubeProgrammer's failed download, recorded for each of its two causes (#743).

`flash_bin_outside_flash` (#717) is a raw binary written past the end of a
NUCLEO-F446RE's flash, under reset. The pair added here is a NUCLEO-H533RE
whose first `flash_firmware` of a valid image failed under hot plug while the
previous firmware was running from flash, and whose retry of the same call 18 s
later programmed and verified. Both are the product's own action logs, with the
probe's serial redacted and the staging directory replaced, as every recording
in the fixture is.

The two failures end in the same `Error:` lines, which is why the
`flash_failed:stlink` steps cannot be written from the words alone: what tells
the causes apart is the connect banner above them, and the retry is the
recording that says a hot-plug failure of this shape can program on the next
attempt.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import write_config

from agentic_hil.backends.stlink import STLINK_SUCCESS_CONFIRMATION, STLinkBackend
from agentic_hil.config import load_config

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "stm32cubeprogrammer_2_23_0_failure_recordings.json"
RECORDINGS = json.loads(FIXTURE.read_text(encoding="utf-8"))["recordings"]
FAILED = "flash_elf_hotplug_core_running_from_flash"
RETRY = "flash_elf_hotplug_retry_succeeded"

# The argv both action logs carry, the issue's command split on its spaces, with
# the serial and the staging directory replaced as the fixture's `redaction` says.
H533RE_ARGV = [
    "/opt/st/stm32cubeclt_1.22.0/STM32CubeProgrammer/bin/STM32_Programmer_CLI",
    "-q",
    "-c",
    "port=SWD",
    "mode=HOTPLUG",
    "sn=[redacted]",
    "-w",
    "[staging]/hil-smoke-h533re.elf",
    "-v",
]
H533RE_TOOL_VERSIONS = {"stm32cubeprogrammer": "2.23.0", "stm32cubeclt": "1.22.0", "agentic_hil": "0.24.1.dev0"}
# Each action log's `stdout` as the issue prints it, character for character,
# with the probe's serial in the banner replaced by `[redacted]`.
FAILED_STDOUT = "      -------------------------------------------------------------------\n                        STM32CubeProgrammer v2.23.0                  \n      -------------------------------------------------------------------\n\nST-LINK SN  : [redacted]\nST-LINK FW  : V3J13M4\nBoard       : NUCLEO-H533RE\nVoltage     : 3.29V\nSWD freq    : 8000 KHz\nConnect mode: Hot Plug\nReset mode  : Software reset\nDevice ID   : 0x478\nRevision ID : --\nDevice name : STM32H533/523\nNVM size    : 512 KBytes\nDevice type : MCU\nDevice CPU  : Cortex-M33\nBL Version  : 0xE2\nSFSP Version: v1.2.0\nDebug in Low Power mode enabled\n\n\n\nOpening and parsing file: hil-smoke-h533re.elf\n\n\nMemory Programming ...\n  File          : hil-smoke-h533re.elf\n  Size          : 10.69 KB \n  Address       : 0x08000000\n\n\nErasing memory corresponding to segment 0:\nErasing internal memory sectors [0 1]\nDownload in Progress:\n\n\nError: failed to download  Sector[0]\nError: failed to download the File\n"
RETRY_STDOUT = "      -------------------------------------------------------------------\n                        STM32CubeProgrammer v2.23.0                  \n      -------------------------------------------------------------------\n\nST-LINK SN  : [redacted]\nST-LINK FW  : V3J13M4\nBoard       : NUCLEO-H533RE\nVoltage     : 3.29V\nSWD freq    : 8000 KHz\nConnect mode: Hot Plug\nReset mode  : Software reset\nDevice ID   : 0x478\nRevision ID : --\nDevice name : STM32H533/523\nNVM size    : 512 KBytes\nDevice type : MCU\nDevice CPU  : Cortex-M33\nBL Version  : 0xE2\nSFSP Version: v1.2.0\nDebug in Low Power mode enabled\n\n\n\nOpening and parsing file: hil-smoke-h533re.elf\n\n\nMemory Programming ...\n  File          : hil-smoke-h533re.elf\n  Size          : 10.69 KB \n  Address       : 0x08000000\n\n\nErasing memory corresponding to segment 0:\nErasing internal memory sectors [0 1]\nDownload in Progress:\n\n\nFile download complete\nTime elapsed during download operation: 00:00:00.168\n\n\n\nVerifying...\n\n\n\n\nTime elapsed during verifying operation: 00:00:00.063\n\n\nDownload verified successfully\n\n\n"
# What the bench and the capture have to be named by, in the recording's own
# `bench` and `how`: the board, the probe and its firmware, the host, the commit
# the product ran at, the connect, and the state the board was in.
PROVENANCE_TOKENS = ("NUCLEO-H533RE", "STLINK-V3", "V3J13M4", "Linux", "1395351c", "hot plug", "running from flash")


def recording(name: str) -> dict:
    """One recording out of the fixture, or a failure that names the one it does not hold."""
    if name not in RECORDINGS:
        raise AssertionError(f"the CubeProgrammer failure recordings hold no {name}")
    return RECORDINGS[name]


def error_lines(transcript: str) -> list[str]:
    """The lines STM32CubeProgrammer begins with `Error:`, the ones it says a run stopped on."""
    return [line.strip() for line in transcript.splitlines() if line.strip().startswith("Error:")]


def connect_mode_lines(transcript: str) -> list[str]:
    return [line.strip() for line in transcript.splitlines() if line.strip().startswith("Connect mode:")]


def test_the_h533re_hot_plug_pair_joins_the_recordings_with_its_provenance() -> None:
    failed = recording(FAILED)
    retry = recording(RETRY)

    assert failed["argv"] == H533RE_ARGV, failed["argv"]
    assert retry["argv"] == H533RE_ARGV, retry["argv"]
    assert failed["returncode"] == 1, failed["returncode"]
    assert retry["returncode"] == 0, retry["returncode"]
    assert failed["stderr"] == "", failed["stderr"]
    assert retry["stderr"] == "", retry["stderr"]
    assert failed["stdout"] == FAILED_STDOUT, failed["stdout"]
    assert retry["stdout"] == RETRY_STDOUT, retry["stdout"]
    for name, recorded in ((FAILED, failed), (RETRY, retry)):
        assert recorded["recorded_on"] == "2026-10-23", (name, recorded["recorded_on"])
        assert recorded["tool_versions"] == H533RE_TOOL_VERSIONS, (name, recorded["tool_versions"])
        provenance = recorded["bench"] + " " + recorded["how"]
        for token in PROVENANCE_TOKENS:
            assert token in provenance, (name, token, provenance)
    retry_provenance = retry["bench"] + " " + retry["how"]
    assert "18 s" in retry_provenance, retry_provenance
    assert FAILED in retry_provenance, retry_provenance


def test_the_two_recorded_causes_print_the_same_error_lines() -> None:
    """Past the end of flash under reset, and a core running from flash under hot plug: one set of words."""
    outside = recording("flash_bin_outside_flash")
    hot_plug = recording(FAILED)

    assert error_lines(outside["stdout"] + outside["stderr"]) == error_lines(hot_plug["stdout"] + hot_plug["stderr"])
    assert connect_mode_lines(outside["stdout"]) == ["Connect mode: Under Reset"], outside["stdout"]
    assert connect_mode_lines(hot_plug["stdout"]) == ["Connect mode: Hot Plug"], hot_plug["stdout"]


def test_the_recorded_retry_reads_as_a_confirmed_flash(tmp_path: Path) -> None:
    """The retry is a flash the backend accepts: no failure read out of it, and the verify line it waits for."""
    retry = recording(RETRY)
    backend = STLinkBackend(load_config(str(write_config(tmp_path, debugger_type="stlink"))))

    assert backend._backend_error_from_output(retry["stdout"] + retry["stderr"], "flash_firmware") is None
    assert backend._confirm_operation_success(retry["stdout"], STLINK_SUCCESS_CONFIRMATION["flash_firmware"])["confirmed"] is True
