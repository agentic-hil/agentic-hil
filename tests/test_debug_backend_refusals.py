"""The debugger failures the suite had never produced, pinned from #506.

Each test here was written from the issue text before any implementation was
touched, against a process that says the tool's own words. Where the words are
OpenOCD's or pyOCD's they are the recording in
``fixtures/debugger_refusal_recordings.json``, taken in the container test
image with nothing on USB (the file names the versions and the date), played
back byte for byte by ``fixtures/fake_debugger_transcript.py``. Where the
words are STM32CubeProgrammer's, which does not run in the image, or are what
a tool prints only with a probe attached, the transcript is the phrase the
issue quotes or the phrase the classifier's own table names, each row says
which, and the recording that would replace it is listed as owed.

What the issue found unpinned, in its order:

* ``debugger_info`` (and ``doctor`` through it) on a version check that
  fails: the configured tool gone, a classified failure instead of ``ok``
  with an empty version. The timeout half already lives in
  ``test_debugger_processes`` and the container tier.
* OpenOCD's missing target script named as ``target_config_not_found`` and as
  the field, a generic missing script as ``config_file_not_found``, and a
  flash that failed without reaching its marker as ``flash_failed``.
* STM32CubeProgrammer exiting 0 while printing an error.
* every ST-Link and pyOCD phrase the classifier has a bucket for, driven
  through the tool result, the quarantine it opens or refuses, and the
  remediation it selects.
* pyOCD's post-flash reset failure as a partial, non-retry-safe result.
* a debug server that exits before its GDB port is ready, and a GDB that dies
  under a session.
* the GDB stop reasons the fake had never produced.
* the ``.hex`` and ``.bin`` plausibility refusals TROUBLESHOOTING promises.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar

import pytest
from conftest import (
    FAKE_GDB,
    FAKE_OPENOCD,
    FAKE_OPENOCD_MISSING_CFG,
    FAKE_PYOCD,
    FAKE_STLINK,
    elf_with_symbols,
    write_config,
)

from agentic_hil.backends.common import NOT_CONTACTED
from agentic_hil.backends.gdbdebug import GdbDebugSession
from agentic_hil.backends.openocd import OpenOCDBackend
from agentic_hil.backends.pyocd import PyOCDBackend
from agentic_hil.backends.stlink import STLinkBackend
from agentic_hil.config import load_config
from agentic_hil.gdbmi import GdbMiClient, stop_result_from_line
from agentic_hil.knowledge import remediation_fields
from agentic_hil.tools import AgenticHILToolService

T = TypeVar("T")

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
FAKE_TRANSCRIPT = FIXTURES / "fake_debugger_transcript.py"
FAKE_PYOCD_RESET_REFUSED = FIXTURES / "fake_pyocd_reset_refused.py"
FAKE_PYOCD_READ_FAILED = FIXTURES / "fake_pyocd_read_failed.py"
RECORDINGS_PATH = FIXTURES / "debugger_refusal_recordings.json"
RECORDINGS = json.loads(RECORDINGS_PATH.read_text(encoding="utf-8"))
TROUBLESHOOTING = ROOT.parent / "TROUBLESHOOTING.md"

BACKENDS = ["openocd", "pyocd", "stlink"]
BACKEND_CLASS = {"openocd": OpenOCDBackend, "pyocd": PyOCDBackend, "stlink": STLinkBackend}
FAKE_BY_TYPE = {"openocd": FAKE_OPENOCD, "pyocd": FAKE_PYOCD, "stlink": FAKE_STLINK}
NOT_FOUND_BACKEND_ERROR = {"openocd": "openocd_not_found", "pyocd": "pyocd_not_found", "stlink": "stm32_programmer_cli_not_found"}
AVAILABLE_SUMMARY = {"openocd": "OpenOCD is available.", "pyocd": "pyOCD is available.", "stlink": "STM32CubeProgrammer CLI is available."}
# pyOCD needs a target type before it drives anything; the other two take
# their target from a script or from the probe.
TARGET_TYPE = {"openocd": None, "pyocd": "stm32f446re", "stlink": None}

# The one STM32CubeProgrammer transcript the issue quotes, and the exit status
# it says the CLI returns for it. Not a recording: the CLI does not run in the
# container image, and the bench recording of `STM32_Programmer_CLI -c port=SWD`
# with no board attached is owed. The probe line beside it is the one the
# in-tree fake_stlink_no_target.py prints, because the real CLI names the
# ST-Link it opened before it gives up on the target.
STLINK_NO_TARGET_STDOUT = "ST-LINK SN  : STLINK123\n"
STLINK_NO_TARGET_STDERR = "Error: No STM32 target found!\n"
# What the issue's own test names for a `--version` that exits 2: the phrase
# and the status, for the tool that cannot be recorded here.
STLINK_VERSION_REFUSED = "Error: no such option\n"

# The two strings OpenOCD's embedded `program` proc echoes about a flash that
# stopped, present in the installed binary (the recording counts them). A
# probeless run cannot print them, so a transcript carrying one is the flash
# that reached the board and failed there.
PROGRAMMING_FAILED = "** Programming Failed **"
VERIFY_FAILED = "** Verify Failed **"
assert PROGRAMMING_FAILED in RECORDINGS["phrases_in_the_openocd_binary"]["phrases"]
assert VERIFY_FAILED in RECORDINGS["phrases_in_the_openocd_binary"]["phrases"]


def recording(name: str) -> dict:
    return RECORDINGS["recordings"][name]


def play_recording(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Have the transcript fake replay one recorded run, byte for byte."""
    monkeypatch.setenv("AGENTIC_HIL_FAKE_TRANSCRIPT_RECORDING", name)


def play_transcript(monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", stderr: str = "", returncode: int = 1) -> None:
    """Have the transcript fake play a transcript the test wrote."""
    monkeypatch.delenv("AGENTIC_HIL_FAKE_TRANSCRIPT_RECORDING", raising=False)
    monkeypatch.setenv("AGENTIC_HIL_FAKE_TRANSCRIPT_STDOUT", stdout)
    monkeypatch.setenv("AGENTIC_HIL_FAKE_TRANSCRIPT_STDERR", stderr)
    monkeypatch.setenv("AGENTIC_HIL_FAKE_TRANSCRIPT_EXIT", str(returncode))


def config_for(workspace: Path, backend_name: str, executable: Path, **kwargs):
    firmware = workspace / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True, exist_ok=True)
    firmware.write_bytes(b"\x7fELFfake")
    return load_config(str(write_config(workspace, debugger_type=backend_name, debugger_executable=executable, target_type=TARGET_TYPE[backend_name], **kwargs)))


def answered_within(seconds: float, call: Callable[[], T]) -> T:
    """`call()`'s answer, or a failure that names the hang; never a suite that stops.

    A transport that refuses a command by taking a lock it already holds does
    not return at all, and a test that waited for it would take the whole run
    with it. The call runs on a thread this waits for, so the wrong behaviour is
    reported as a red test with the time it was given."""
    outcome: dict = {}

    def run() -> None:
        try:
            outcome["answer"] = call()
        except BaseException as error:  # noqa: BLE001 - re-raised on the test thread below
            outcome["error"] = error

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        pytest.fail(f"the call did not return within {seconds} s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["answer"]


def call(config, tool: str, arguments: dict | None = None) -> dict:
    service = AgenticHILToolService(config)
    try:
        return service.call(tool, arguments or {})
    finally:
        service.close()


def blocking_record_states(config) -> set[str]:
    records = Path(config.state_root) / "coordination" / "records"
    if not records.is_dir():
        return set()
    states = {json.loads(path.read_text(encoding="utf-8")).get("state") for path in records.glob("*.json")}
    return {state for state in states if isinstance(state, str)} & {"cleanup_required", "quarantined", "recovery_pending"}


def assert_refused_before_contact(result: dict, config) -> None:
    """A failed call over a channel that never carried anything: refused, not quarantined."""
    assert result["ok"] is False, result
    for key, value in NOT_CONTACTED.items():
        assert result.get(key) == value, (key, result)
    assert result.get("cleanup_required") is not True, result
    assert result.get("quarantined") is not True, result
    assert "quarantine_guidance" not in result, result
    assert not blocking_record_states(config), blocking_record_states(config)


def assert_effect_unconfirmed(result: dict) -> None:
    """A failure after the tool reached the board: the effect is unknown and the incident stands.

    The same fields the mid-flash and the refused-erase failures carry
    (test_quarantine_triggers): no claim that the hardware is unchanged, no
    retry, and the cleanup reason the coordination layer keys on. What the
    recovery policy then makes of the incident is that layer's own contract
    and is not pinned here."""
    assert result["ok"] is False, result
    assert result["side_effect_status"] == "unknown", result
    assert result["retry_safe"] is False, result
    assert result.get("hardware_state") != "unchanged", result
    assert result["cleanup_required"] is True, result
    assert result["cleanup_reasons"] == ["debugger_result_unconfirmed"], result


def log_of(config, result: dict) -> dict:
    return json.loads((Path(config.workspace_root) / result["log_path"]).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# debugger_info and doctor: the version check that does not answer a version.


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_debugger_info_reports_a_failing_version_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_name: str) -> None:
    """A `--version` that exits non-zero is a classified failure, not `ok` with no version.

    OpenOCD and pyOCD refuse an option they do not know with the recorded
    texts (exit 1 and exit 2); STM32CubeProgrammer with the phrase and status
    the issue names. None of the three words matches any bucket, so the
    backend's own classification is `unknown_debugger_error`, and the public
    error_type is `debugger_error`, the name the debug-session backend already
    gives an unclassified server failure, so a caller reads one word for "the
    debugger failed and its output says no more" wherever it meets it.
    """
    if backend_name == "stlink":
        play_transcript(monkeypatch, stderr=STLINK_VERSION_REFUSED, returncode=2)
    else:
        play_recording(monkeypatch, f"{backend_name}_unknown_option")
    config = config_for(tmp_path, backend_name, FAKE_TRANSCRIPT)

    result = call(config, "debugger_info")

    assert result["ok"] is False, result
    assert result["tool"] == "debugger_info"
    assert result["backend"] == backend_name
    assert result["backend_error_type"] == "unknown_debugger_error", result
    assert result["error_type"] == "debugger_error", result
    assert "version" not in result, result
    assert result["summary"] != AVAILABLE_SUMMARY[backend_name], result


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_debugger_info_refuses_when_the_configured_tool_vanished(tmp_path: Path, backend_name: str) -> None:
    """The not-found refusal, on a tool the configuration resolved and that is gone since.

    Config load pins the executable while the file is there; the check that
    runs later has to notice it is not, and answer the same refusal a never-
    installed tool gets rather than reporting the tool as available.
    """
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    executable = toolchain / FAKE_BY_TYPE[backend_name].name
    shutil.copy(FAKE_BY_TYPE[backend_name], executable)
    config = config_for(tmp_path, backend_name, executable)
    executable.unlink()

    result = call(config, "debugger_info")

    assert result["ok"] is False, result
    assert result["tool"] == "debugger_info"
    assert result["error_type"] == "debugger_not_found", result
    assert result["backend_error_type"] == NOT_FOUND_BACKEND_ERROR[backend_name], result
    assert "version" not in result, result


@pytest.mark.parametrize("backend_name", ["openocd", "pyocd"])
def test_debugger_info_reads_the_version_the_real_tool_prints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_name: str) -> None:
    """The happy path, pinned to the world rather than to the fake's own idea of it.

    OpenOCD prints its four-line version banner on stderr and nothing on
    stdout; pyOCD prints one line on stdout. Both are in the recording, and the
    version the tool reports has to be the first line of either.
    """
    play_recording(monkeypatch, f"{backend_name}_version")
    config = config_for(tmp_path, backend_name, FAKE_TRANSCRIPT)

    result = call(config, "debugger_info")

    assert result["ok"] is True, result
    assert result["version"] == {"openocd": "Open On-Chip Debugger 0.12.0", "pyocd": "0.45.1"}[backend_name], result
    assert result["summary"] == AVAILABLE_SUMMARY[backend_name]


def test_doctor_reports_the_classified_version_failure_and_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's view of the same check: a document that carries the classification, and exit 1.

    `doctor` runs the configured debugger's version check first, so a tool that
    answers `--version` with a refusal is met on the first command a new bench
    runs, and what the operator reads must say the check failed and how, not
    that OpenOCD is available with an empty version.
    """
    play_recording(monkeypatch, "openocd_unknown_option")
    # The file lives outside the workspace, as the authoritative configuration
    # must; the service tests above load it in-process and never check that.
    workspace = tmp_path / "workspace"
    config = config_for(workspace, "openocd", FAKE_TRANSCRIPT, config_path=tmp_path / "config" / "config.yaml")
    environment = {**os.environ, "AGENTIC_HIL_CONFIG": str(config.config_path)}

    answered = subprocess.run([sys.executable, "-m", "agentic_hil", "doctor", "--json"], capture_output=True, text=True, cwd=str(workspace), env=environment, timeout=120, check=False)

    assert answered.returncode == 1, answered.stdout + answered.stderr
    assert answered.stdout.strip(), f"doctor --json wrote no document on stdout; stderr was:\n{answered.stderr}"
    document = json.loads(answered.stdout)
    check = document["debuggers"]["dut"]["check"]
    assert check["ok"] is False, check
    assert check["error_type"] == "debugger_error", check
    assert check["backend_error_type"] == "unknown_debugger_error", check
    assert "version" not in check, check


# ---------------------------------------------------------------------------
# OpenOCD's scripts: which one is missing, and a flash that stopped with no marker.


def test_a_missing_target_cfg_is_target_config_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`Can't find target/xyz.cfg` is `target_config_not_found`, and the summary says `target_cfg`.

    The interface case was the only one ever faked, because the fake reported
    the first `-f` it could not find and OpenOCD's own search names are never
    files on the host; with the fake resolving the search names the real
    OpenOCD's package installs, the target script is the one that fails, in
    the recorded wording. An operator sent to check `interface_cfg` for a
    target script that is not there checks the wrong key, so the summary has
    to name the field.
    """
    monkeypatch.setenv("AGENTIC_HIL_FAKE_OPENOCD_SCRIPTS", "interface/stlink.cfg")
    config = config_for(tmp_path, "openocd", FAKE_OPENOCD_MISSING_CFG, target_cfg="target/does-not-exist.cfg")

    result = call(config, "probe_target")

    assert result["backend_error_type"] == "target_config_not_found", result
    assert result["error_type"] == "debugger_config_not_found", result
    assert "Can't find target/does-not-exist.cfg" in result["programmer_output"]["stderr"], result["programmer_output"]
    assert_refused_before_contact(result, config)
    assert result["remediation"] == remediation_fields("debugger_config_not_found", "openocd")["remediation"]
    assert "target_cfg" in result["summary"], result["summary"]


def test_a_missing_interface_cfg_is_still_interface_config_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour: the one case the fake always produced keeps its bucket, and is never blamed on `target_cfg`."""
    monkeypatch.setenv("AGENTIC_HIL_FAKE_OPENOCD_SCRIPTS", "target/stm32f4x.cfg")
    config = config_for(tmp_path, "openocd", FAKE_OPENOCD_MISSING_CFG, interface_cfg="interface/does-not-exist.cfg")

    result = call(config, "probe_target")

    assert result["backend_error_type"] == "interface_config_not_found", result
    assert result["error_type"] == "debugger_config_not_found", result
    assert "Can't find interface/does-not-exist.cfg" in result["programmer_output"]["stderr"], result["programmer_output"]
    assert_refused_before_contact(result, config)
    assert "target_cfg" not in result["summary"], result["summary"]


def test_a_script_that_is_neither_configured_field_is_config_file_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file a configured script sources and OpenOCD cannot find, in OpenOCD's recorded words.

    Neither configured name is in the transcript, so neither field is blamed:
    the generic bucket answers, still as a configuration refusal, still before
    the adapter was opened.
    """
    play_recording(monkeypatch, "openocd_missing_include")
    config = config_for(tmp_path, "openocd", FAKE_TRANSCRIPT)

    result = call(config, "probe_target")

    assert result["backend_error_type"] == "config_file_not_found", result
    assert result["error_type"] == "debugger_config_not_found", result
    assert "Can't find nothing-here.tcl" in result["programmer_output"]["stderr"], result["programmer_output"]
    assert_refused_before_contact(result, config)


def test_a_flash_that_failed_without_reaching_its_marker_is_flash_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenOCD's own `** Programming Failed **`, with no erase, verify or reset word beside it.

    The `program` proc echoes it after `flash write_image` returned an error
    and before `shutdown error`, so the transcript carries a failure word and
    no marker. That is the operation-anchored bucket at the bottom of the
    classifier, and it was never driven: every failing flash fixture printed a
    more specific phrase. A flash that reached the board and stopped there is
    an unconfirmed effect, so the result quarantines rather than refuses.
    """
    play_transcript(monkeypatch, stdout=f"{PROGRAMMING_FAILED}\n", stderr="Error: failed to write memory at 0x08000000\n", returncode=1)
    config = config_for(tmp_path, "openocd", FAKE_TRANSCRIPT)

    result = call(config, "flash_firmware", {"image_path": "build/firmware.elf"})

    assert result["backend_error_type"] == "flash_failed", result
    assert result["error_type"] == "flash_failed", result
    assert "success_confirmed" not in result, result
    assert PROGRAMMING_FAILED in result["programmer_output"]["stdout"], result["programmer_output"]
    assert_effect_unconfirmed(result)
    assert result.get("remediation") == remediation_fields("flash_failed", "openocd").get("remediation"), result.get("remediation")


# ---------------------------------------------------------------------------
# A tool that exits 0 while printing an error.


def test_a_zero_exit_with_error_text_is_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """STM32CubeProgrammer returns 0 for some failures; the words decide, not the status.

    `Error: No STM32 target found!` with exit 0 is neither a confirmed success
    nor an unconfirmed one: it is the CLI's own report that a probe was opened
    and nothing answered behind it, classified as such, with no
    `success_confirmed` anywhere on the result. pyOCD's own exit-0 refusal
    (`No connected debug probes`) is pinned the same way in
    tests/test_pyocd_without_a_probe.py and is not repeated here.
    """
    play_transcript(monkeypatch, stdout=STLINK_NO_TARGET_STDOUT, stderr=STLINK_NO_TARGET_STDERR, returncode=0)
    config = config_for(tmp_path, "stlink", FAKE_TRANSCRIPT)

    result = call(config, "probe_target")

    assert result["ok"] is False, result
    assert result["backend_error_type"] == "target_not_detected", result
    assert result["error_type"] == "target_not_detected", result
    assert "success_confirmed" not in result, result
    assert result.get("target_detected") is not True, result
    assert result["programmer_output"]["returncode"] == 0, result["programmer_output"]
    assert log_of(config, result)["returncode"] == 0
    assert_refused_before_contact(result, config)


# ---------------------------------------------------------------------------
# Every phrase the two classifiers have a bucket for.

# (backend, tool, transcript, expected bucket, where the words come from).
# `recorded`: the container recording. `in tree`: a phrase an existing fixture
# already prints, typed from the tool by whoever wrote it. `issue`: the phrase
# #506 quotes. `rule`: the classifier's own table words, with no recording
# behind them; each of those rows is a recording owed.
CLASSIFIER_ROWS = [
    ("stlink", "probe_target", "Error: No ST-LINK detected!\n", "probe_not_found", "in tree"),
    ("stlink", "probe_target", STLINK_NO_TARGET_STDOUT + STLINK_NO_TARGET_STDERR, "target_not_detected", "issue"),
    ("stlink", "probe_target", "ST-LINK SN  : STLINK123\nError: no device found\n", "target_not_detected", "issue"),
    ("stlink", "probe_target", "ST-LINK SN  : STLINK123\nError: unable to connect to target\n", "target_not_detected", "issue"),
    ("stlink", "flash_firmware", "ST-LINK SN  : STLINK123\nMemory Programming ...\nError: Verify failed at address 0x08000000\n", "verify_failed", "rule"),
    ("stlink", "flash_firmware", "ST-LINK SN  : STLINK123\nError: File build/firmware.elf not found\n", "config_file_not_found", "rule"),
    ("stlink", "flash_firmware", "ST-LINK SN  : STLINK123\nMemory Programming ...\nError: Download failed\n", "flash_failed", "rule"),
    ("pyocd", "probe_target", recording("pyocd_commander_status_no_probe")["stdout"], "probe_not_found", "recorded"),
    ("pyocd", "probe_target", "0000000:ERROR:Error attempting to connect to target\nError: unable to connect to the target\n", "target_not_detected", "in tree"),
    ("pyocd", "probe_target", "0000512 E No ACK received [__main__]\n", "target_not_detected", "rule"),
    ("pyocd", "flash_firmware", "0001042 C Target type stm32f446re not recognized. Use 'pyocd list --targets' to see currently available target types. See <https://pyocd.io/docs/target_support.html> for how to install additional target support. [__main__]\n", "target_type_invalid", "in tree"),
    ("pyocd", "flash_firmware", "0000900 E Verify failed at 0x08000000 [load_cmd]\n", "verify_failed", "rule"),
    ("pyocd", "flash_firmware", "0000900 C Flash programming failed [load_cmd]\n", "flash_failed", "rule"),
    ("pyocd", "debug_symbol_value", "0000817 E Transfer error while reading 4 bytes @ 0x20000080 [savemem]\n", "memory_read_failed", "rule"),
]


@pytest.mark.parametrize(
    ("backend_name", "tool", "transcript", "expected", "source"),
    CLASSIFIER_ROWS,
    ids=[f"{row[0]}-{row[3]}-{index}" for index, row in enumerate(CLASSIFIER_ROWS)],
)
def test_classifier_buckets_from_recorded_tool_output(tmp_path: Path, backend_name: str, tool: str, transcript: str, expected: str, source: str) -> None:
    """Each phrase lands in its own bucket, and in no other."""
    config = load_config(str(write_config(tmp_path, debugger_type=backend_name, target_type=TARGET_TYPE[backend_name])))
    backend = BACKEND_CLASS[backend_name](config)

    assert backend._classify_output(transcript, tool) == expected, (source, transcript)


# The same rows through the service: the public error_type each bucket
# publishes, the remediation it selects out of the catalogue, and whether the
# failure refuses (the tool's own words say the board was never reached) or
# quarantines (the tool reached it and the effect is unconfirmed). The pyOCD
# memory read is driven below on its own, because a read needs a flashed ELF
# to resolve its symbol against, which no single transcript can provide.
TOOL_RESULT_ROWS = [
    ("stlink", "probe_target", {}, "Error: No ST-LINK detected!\n", "probe_not_found", "adapter_not_found", "refused"),
    ("stlink", "probe_target", {}, "ST-LINK SN  : STLINK123\nError: no device found\n", "target_not_detected", "target_not_detected", "refused"),
    ("stlink", "probe_target", {}, "ST-LINK SN  : STLINK123\nError: unable to connect to target\n", "target_not_detected", "target_not_detected", "refused"),
    ("stlink", "flash_firmware", {"image_path": "build/firmware.elf"}, "ST-LINK SN  : STLINK123\nMemory Programming ...\nError: Verify failed at address 0x08000000\n", "verify_failed", "verify_failed", "quarantined"),
    ("pyocd", "probe_target", {}, "0000000:ERROR:Error attempting to connect to target\nError: unable to connect to the target\n", "target_not_detected", "target_not_detected", "refused"),
    ("pyocd", "flash_firmware", {"image_path": "build/firmware.elf"}, "0001042 C Target type stm32f446re not recognized. Use 'pyocd list --targets' to see currently available target types. See <https://pyocd.io/docs/target_support.html> for how to install additional target support. [__main__]\n", "target_type_invalid", "target_type_invalid", "refused"),
    ("pyocd", "flash_firmware", {"image_path": "build/firmware.elf"}, "0000900 E Verify failed at 0x08000000 [load_cmd]\n", "verify_failed", "verify_failed", "quarantined"),
    ("pyocd", "flash_firmware", {"image_path": "build/firmware.elf"}, "0000900 C Flash programming failed [load_cmd]\n", "flash_failed", "flash_failed", "quarantined"),
]


@pytest.mark.parametrize(
    ("backend_name", "tool", "arguments", "transcript", "backend_error_type", "error_type", "contact"),
    TOOL_RESULT_ROWS,
    ids=[f"{row[0]}-{row[1]}-{row[4]}" for row in TOOL_RESULT_ROWS],
)
def test_each_bucket_drives_the_tool_result_its_quarantine_and_its_remediation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_name: str, tool: str, arguments: dict, transcript: str, backend_error_type: str, error_type: str, contact: str) -> None:
    """A wrong bucket sends the operator the wrong next step; this is each bucket's right one."""
    play_transcript(monkeypatch, stderr=transcript, returncode=1)
    config = config_for(tmp_path, backend_name, FAKE_TRANSCRIPT)

    result = call(config, tool, arguments)

    assert result["ok"] is False, result
    assert result["backend_error_type"] == backend_error_type, result
    assert result["error_type"] == error_type, result
    # Whatever the catalogue holds for this error, and nothing else: a wrong
    # bucket would carry another entry's steps. Where the catalogue is silent
    # both sides are absent, and the bucket above is what the row pins.
    assert result.get("remediation") == remediation_fields(error_type, backend_name).get("remediation"), result.get("remediation")
    assert transcript.strip().splitlines()[-1] in result["programmer_output"]["stderr"], result["programmer_output"]
    if contact == "refused":
        assert_refused_before_contact(result, config)
    else:
        assert_effect_unconfirmed(result)


def test_a_missing_input_file_on_stlink_is_not_answered_with_the_missing_configuration_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one bucket whose public name collides with a catalogue entry about something else.

    `config_file_not_found` in the catalogue means this workspace has no
    authoritative configuration, and its steps say to write one. The ST-Link
    classifier uses the same name for a file STM32CubeProgrammer could not open,
    and its own summary says so (`Debugger input file could not be found.`), so
    the result must not carry the steps of the other refusal: an operator whose
    firmware path was wrong must not be sent to `agentic-hil init`.
    """
    play_transcript(monkeypatch, stderr="ST-LINK SN  : STLINK123\nError: File build/firmware.elf not found\n", returncode=1)
    config = config_for(tmp_path, "stlink", FAKE_TRANSCRIPT)

    result = call(config, "flash_firmware", {"image_path": "build/firmware.elf"})

    assert result["ok"] is False, result
    assert result["backend_error_type"] == "config_file_not_found", result
    assert result["summary"] == "Debugger input file could not be found.", result
    assert result.get("remediation") != remediation_fields("config_file_not_found")["remediation"], result.get("remediation")
    assert "agentic-hil init" not in json.dumps(result.get("remediation", [])), result.get("remediation")


def read_service(tmp_path: Path, executable: Path) -> AgenticHILToolService:
    """A pyOCD bench whose reads resolve their symbol against the ELF a flash put on the board."""
    config_path = write_config(tmp_path, debugger_type="pyocd", debugger_executable=executable, target_type="stm32f446re", gdb_executable=FAKE_GDB)
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(elf_with_symbols([("boot_counter", 0x20000080, 4)]))
    return AgenticHILToolService(load_config(str(config_path)))


def test_a_pyocd_read_the_commander_reported_failed_is_memory_read_failed(tmp_path: Path) -> None:
    """The read bucket through the tool: the probe opened, the core answered, `savemem` failed.

    Not a refusal: the connect reached the target, so the words say nothing
    about the board being untouched, and the read reports contact. What it
    must not do is fall into the unknown bucket and tell the caller nothing
    about what was being attempted.
    """
    service = read_service(tmp_path, FAKE_PYOCD_READ_FAILED)
    try:
        assert service.call("flash_firmware", {"image_path": "build/app.elf"})["ok"] is True
        value = service.call("debug_symbol_value", {"symbol": "boot_counter"})
    finally:
        service.close()

    assert value["ok"] is False, value
    assert value["backend_error_type"] == "memory_read_failed", value
    assert value["error_type"] == "memory_read_failed", value
    assert "Transfer error" in value["programmer_output"]["stderr"], value["programmer_output"]
    assert "hex" not in value, value
    assert value.get("remediation") == remediation_fields("memory_read_failed", "pyocd").get("remediation"), value.get("remediation")


# ---------------------------------------------------------------------------
# pyOCD: the firmware is on the board and the target would not reset.


def test_pyocd_flash_then_failed_reset_is_partial(tmp_path: Path) -> None:
    """Flashed, not running: a partial effect nobody may retry blindly."""
    config = config_for(tmp_path, "pyocd", FAKE_PYOCD_RESET_REFUSED)

    result = call(config, "flash_firmware", {"image_path": "build/firmware.elf", "reset_after_flash": True})

    assert result["ok"] is False, result
    assert result["error_type"] == "reset_failed", result
    assert result["summary"] == "Firmware flashed, but the post-flash reset failed.", result
    assert result["side_effect_committed"] is True, result
    assert result["side_effect_status"] == "partial", result
    assert result["retry_safe"] is False, result
    assert result["reset_after_flash"] is False, result
    assert result["verify"] is True, result
    assert result["artifact"]["path"] == "build/firmware.elf", result
    assert "reset failed" in result["programmer_output"]["stderr"], result["programmer_output"]
    # The firmware is on the board, so the failure is not one of the refusals
    # that promise the hardware was never touched.
    assert result.get("target_contacted") is not False, result
    assert "success_confirmed" not in result, result


def test_pyocd_flash_without_a_reset_never_meets_the_failing_reset(tmp_path: Path) -> None:
    """The neighbour: with `reset_after_flash` false the reset is not run, so the same fake succeeds."""
    config = config_for(tmp_path, "pyocd", FAKE_PYOCD_RESET_REFUSED)

    result = call(config, "flash_firmware", {"image_path": "build/firmware.elf"})

    assert result["ok"] is True, result
    assert result["reset_after_flash"] is False, result
    assert result["summary"] == "Firmware flashed and verified. Target was not reset.", result


# ---------------------------------------------------------------------------
# A debug server that exits before its port, and a GDB that dies under a session.

START_TIMEOUT_S = 10.0


def debug_service(tmp_path: Path, *, server: Path = FAKE_OPENOCD, fake_gdb_behavior: str | None = None) -> AgenticHILToolService:
    config_path = write_config(tmp_path, debugger_executable=server, gdb_executable=FAKE_GDB)
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    trailer = b"" if fake_gdb_behavior is None else f"\nFAKE_GDB_BEHAVIOR={fake_gdb_behavior}\n".encode()
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12 + trailer)
    return AgenticHILToolService(load_config(str(config_path)))


def closed_reporting_its_own_failure(service: AgenticHILToolService) -> BaseException | None:
    """`service.close()`'s own error, if it has one, instead of an exception that hides the test.

    A session whose GDB died is still a session the caller has to be able to
    close. Raising out of `close` in a `finally` replaces every assertion the
    test came to make with the cleanup's own complaint, so the error is
    returned and asserted on with the rest.
    """
    try:
        service.close()
    except BaseException as error:  # noqa: BLE001 - asserted on by the caller
        return error
    return None


@pytest.mark.parametrize(
    ("name", "decisive_line"),
    [
        ("openocd_server_stlink_no_probe", "Error: open failed"),
        ("openocd_server_ftdi_no_device", "Error: unable to open ftdi device"),
    ],
    ids=["stlink-open-failed", "ftdi-no-device"],
)
def test_a_server_that_dies_at_startup_is_classified_from_its_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, decisive_line: str) -> None:
    """OpenOCD exiting during startup is read from what it printed, not waited out as a timeout.

    Both recordings are the real OpenOCD asked to open a GDB port with no
    adapter to open: the ST-Link interface says `open failed`, the FTDI one says
    `no device found` and `unable to open ftdi device`. Either is
    `adapter_not_found`, the summary says the server exited, and the call ends
    the moment the process does rather than at `timeout_s`. No target was
    reached, so the failed start refuses and leaves nothing to clean up.
    """
    play_recording(monkeypatch, name)
    service = debug_service(tmp_path, server=FAKE_TRANSCRIPT)
    try:
        started = service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "attach", "timeout_s": START_TIMEOUT_S})
    finally:
        service.close()

    assert started["ok"] is False, started
    assert started["error_type"] == "adapter_not_found", started
    assert started["backend_error_type"] == "adapter_not_found", started
    assert started["summary"] == "Debug server exited before the GDB port became ready.", started
    assert started["elapsed_ms"] < START_TIMEOUT_S * 1000 / 2, started
    assert started["cleanup_confirmed"] is True, started
    assert started["side_effect_status"] == "not_started", started
    assert started["retry_safe"] is True, started
    assert decisive_line in Path(tmp_path / started["log_path"]).read_text(encoding="utf-8"), started["log_path"]


def test_a_gdb_that_dies_after_acknowledging_the_resume_ends_the_session_in_error(tmp_path: Path) -> None:
    """`^running`, then the pipe closes: a `debugger_error` stop, and a session nobody may go on using.

    The stop the product waited for never comes; what comes is the exit, and it
    is read as a debugger failure rather than as a target that did not stop
    before the timeout, so the caller is not told to wait longer. The session
    is over: its status is `error`, a halt asked for afterwards is refused
    instead of being sent into a closed pipe, and closing the service that lost
    its GDB is an answer too, not an exception the caller cannot handle. The
    exit code itself belongs to the two timings below, where the transport has
    something pending to report it on.
    """
    service = debug_service(tmp_path, fake_gdb_behavior="gdb_exits_after_running")
    try:
        assert service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "load", "timeout_s": START_TIMEOUT_S})["ok"] is True
        continued = answered_within(30.0, lambda: service.call("debug_continue", {"timeout_s": 5}))
        status = answered_within(30.0, lambda: service.call("debug_get_session_status"))
        halted = answered_within(30.0, lambda: service.call("debug_halt", {"timeout_s": 1}))
    finally:
        closing = answered_within(60.0, lambda: closed_reporting_its_own_failure(service))

    assert continued["ok"] is False, continued
    assert continued["error_type"] == "debugger_error", continued
    assert continued["stop_reason"] == "debugger_error", continued
    assert continued["stop"]["backend_error"] == "GDB process is not running.", continued["stop"]
    assert continued["session"]["status"] == "error", continued
    assert status["status"] == "error", status
    assert halted["ok"] is False, halted
    assert halted["error_type"] == "session_not_active", halted
    assert closing is None, f"closing a session whose GDB exited raised {type(closing).__name__}: {closing}"


def test_a_gdb_that_dies_while_the_stop_wait_is_pending_reports_the_exit_code(tmp_path: Path) -> None:
    """The exit lands on a wait that is already waiting, and that wait names the status GDB left with.

    This is the timing the transport writes the sentence for: something is
    pending when the pipe closes, so the stop the caller is waiting on carries
    `GDB process exited with code N` rather than a bare "not running". A caller
    reading the log has to be able to tell a GDB that crashed from one that was
    never there.
    """
    service = debug_service(tmp_path, fake_gdb_behavior="gdb_exits_during_the_stop_wait")
    try:
        assert service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "load", "timeout_s": START_TIMEOUT_S})["ok"] is True
        continued = answered_within(30.0, lambda: service.call("debug_continue", {"timeout_s": 5}))
    finally:
        closing = answered_within(60.0, lambda: closed_reporting_its_own_failure(service))

    assert continued["ok"] is False, continued
    assert continued["error_type"] == "debugger_error", continued
    assert continued["stop_reason"] == "debugger_error", continued
    assert continued["stop"]["backend_error"] == "GDB process exited with code 0.", continued["stop"]
    assert closing is None, f"closing a session whose GDB exited raised {type(closing).__name__}: {closing}"


def test_a_gdb_that_dies_with_the_command_pending_answers_the_exit_code(tmp_path: Path) -> None:
    """The other timing: the pipe closes before `-exec-continue` was answered at all."""
    service = debug_service(tmp_path, fake_gdb_behavior="gdb_exits_before_answering")
    try:
        assert service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "load", "timeout_s": START_TIMEOUT_S})["ok"] is True
        continued = answered_within(30.0, lambda: service.call("debug_continue", {"timeout_s": 5}))
    finally:
        closing = answered_within(60.0, lambda: closed_reporting_its_own_failure(service))

    assert continued["ok"] is False, continued
    assert continued["error_type"] == "debugger_error", continued
    assert continued["backend_error_type"] == "gdb_error", continued
    assert continued["summary"] == "GDB process exited with code 0.", continued
    assert closing is None, f"closing a session whose GDB exited raised {type(closing).__name__}: {closing}"


def test_the_transport_answers_an_exited_gdb_in_its_own_words(tmp_path: Path) -> None:
    """The GDB/MI client alone, for the three sentences the issue names.

    A stop wait that is pending when GDB exits ends with the exit status; a
    command sent afterwards is refused with the sentence for a transport that
    has no process behind it any more. That is the transport's contract,
    without the session layer's reading of it. Every call is given its own
    wall-clock ceiling, because a refusal that never returns is one of the
    failures this pins against, and a test that simply waited for it would take
    the whole run with it.
    """
    client = GdbMiClient(str(FAKE_GDB), str(FAKE_GDB.parent))
    try:
        assert client.command("-gdb-set mi-async on", 5.0).ok
        with_behavior = tmp_path / "app.elf"
        with_behavior.write_bytes(b"\x7fELF" + b"\x00" * 12 + b"\nFAKE_GDB_BEHAVIOR=gdb_exits_during_the_stop_wait\n")
        assert client.command(f'-file-exec-and-symbols "{with_behavior.as_posix()}"', 5.0).ok
        resumed = answered_within(20.0, lambda: client.command("-exec-continue", 5.0))
        stop = answered_within(20.0, lambda: client.wait_for_stop(5.0))
        after = answered_within(20.0, lambda: client.command("-exec-interrupt --all", 5.0))
    finally:
        answered_within(20.0, lambda: client.close(5.0))

    assert resumed.ok, resumed
    assert stop.reason == "debugger_error", stop
    assert stop.error_message == "GDB process exited with code 0.", stop
    assert after.ok is False, after
    assert after.error_message == "GDB process is not running.", after
    assert client.is_running() is False


# ---------------------------------------------------------------------------
# The stop reasons the fake had never produced.

# (behaviour of the fake, public stop_reason, whether the stop is abnormal, the
# public error_type an abnormal stop carries, the exception_type a fault carries,
# the signal name the stop carries).
STOP_ROWS = [
    ("stop_exited_normally", "target_exit", False, None, None, None),
    ("stop_in_reset_handler", "reset", False, None, None, "SIGINT"),
    ("stop_sigtrap", "unexpected_breakpoint", True, "unexpected_breakpoint", None, "SIGTRAP"),
    ("stop_sigsegv", "exception", True, "target_exception", "sigsegv", "SIGSEGV"),
    ("stop_sigusr1", "signal", False, None, None, "SIGUSR1"),
]


@pytest.mark.parametrize(("behavior", "stop_reason", "abnormal", "error_type", "exception_type", "signal_name"), STOP_ROWS, ids=[row[1] for row in STOP_ROWS])
def test_the_stop_reasons_a_plan_branches_on_come_through_the_fake_gdb(tmp_path: Path, behavior: str, stop_reason: str, abnormal: bool, error_type: str | None, exception_type: str | None, signal_name: str | None) -> None:
    """`debug_continue` and `debug_get_stop_reason` publish the documented stop_reason for each record."""
    service = debug_service(tmp_path, fake_gdb_behavior=behavior)
    try:
        assert service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "load", "timeout_s": START_TIMEOUT_S})["ok"] is True
        continued = service.call("debug_continue", {"timeout_s": 5})
        asked = service.call("debug_get_stop_reason")
    finally:
        service.close()

    assert continued["stop_reason"] == stop_reason, continued
    assert continued["ok"] is not abnormal, continued
    assert continued["target_ok"] is not abnormal, continued
    assert asked["stop_reason"] == stop_reason, asked
    assert asked["target_ok"] is not abnormal, asked
    if abnormal:
        assert continued["error_type"] == error_type, continued
        assert continued["suggested_actions"], continued
    else:
        assert "error_type" not in continued, continued
    if exception_type is not None:
        assert continued["stop"]["exception_type"] == exception_type, continued["stop"]
    else:
        assert "exception_type" not in continued["stop"], continued["stop"]
    if signal_name is not None:
        assert continued["stop"]["signal"]["name"] == signal_name, continued["stop"]
    if stop_reason == "unexpected_breakpoint":
        assert continued["stop"]["breakpoint_expected"] is False, continued["stop"]
        assert "debug_clear_breakpoints" in " ".join(continued["suggested_actions"]), continued["suggested_actions"]


def stop_reason_of(tmp_path: Path, line: str) -> dict:
    """`_stop_reason_from_gdb` on one `*stopped` record, with no breakpoint set."""
    config = load_config(str(write_config(tmp_path, gdb_executable=FAKE_GDB)))
    sessions = OpenOCDBackend(config)._debug
    session = GdbDebugSession("debug-test", {"path": "build/app.elf"}, "load", 0, SimpleNamespace(poll=lambda: None), [], str(tmp_path / "log.json"))
    return sessions._stop_reason_from_gdb(session, stop_result_from_line(line))


MI_ROWS = [
    ('*stopped,reason="exited-normally"', "target_exit", None),
    ('*stopped,reason="exited",exit-code="01"', "target_exit", None),
    ('*stopped,reason="signal-received",signal-name="SIGTRAP",signal-meaning="Trace/breakpoint trap",frame={addr="0x08000310",func="assert_failed",file="assert.c",line="9"},thread-id="1",stopped-threads="all"', "unexpected_breakpoint", None),
    ('*stopped,reason="signal-received",signal-name="SIGSEGV",signal-meaning="Segmentation fault",frame={addr="0x08000520",func="main",file="main.c",line="77"},thread-id="1",stopped-threads="all"', "exception", "sigsegv"),
    ('*stopped,reason="signal-received",signal-name="SIGBUS",signal-meaning="Bus error",frame={addr="0x08000520",func="main",file="main.c",line="77"},thread-id="1",stopped-threads="all"', "exception", "sigbus"),
    ('*stopped,reason="signal-received",signal-name="SIGINT",signal-meaning="Interrupt",frame={addr="0x080001c0",func="Reset_Handler",file="startup_stm32f446xx.s",line="65"},thread-id="1",stopped-threads="all"', "reset", None),
    ('*stopped,reason="signal-received",signal-name="SIGUSR1",signal-meaning="User defined signal 1",frame={addr="0x08000530",func="main",file="main.c",line="80"},thread-id="1",stopped-threads="all"', "signal", None),
]


@pytest.mark.parametrize(("line", "stop_reason", "exception_type"), MI_ROWS, ids=[f"{row[1]}-{index}" for index, row in enumerate(MI_ROWS)])
def test_stop_reason_mapping_from_mi_stopped_records(tmp_path: Path, line: str, stop_reason: str, exception_type: str | None) -> None:
    """The mapping itself, one documented GDB/MI record at a time.

    The record shapes are the GDB manual's; a bench recording of a real fault
    and a real reset over arm-none-eabi-gdb is owed and would replace them.
    """
    stop = stop_reason_of(tmp_path, line)

    assert stop["stop_reason"] == stop_reason, stop
    assert stop["backend_stop_reason"] == stop_result_from_line(line).reason, stop
    if exception_type is None:
        assert "exception_type" not in stop, stop
    else:
        assert stop["exception_type"] == exception_type, stop
        assert stop["fault_type"] == exception_type, stop
    if stop_reason == "unexpected_breakpoint":
        assert stop["breakpoint_expected"] is False, stop


def test_the_end_of_a_step_is_not_read_as_an_error(tmp_path: Path) -> None:
    """`end-stepping-range` names no failure; the record's own reason is carried and nothing is quarantined.

    No tool here steps, so the product documents no name for this stop; what
    it must not do is answer it as an abnormal stop that blocks the session
    or loses what GDB said.
    """
    stop = stop_reason_of(tmp_path, '*stopped,reason="end-stepping-range",frame={addr="0x08000534",func="main",file="main.c",line="81"},thread-id="1",stopped-threads="all"')

    assert stop["backend_stop_reason"] == "end-stepping-range", stop
    assert stop["stop_reason"] not in {"debugger_error", "exception", "fault", "timeout", "unexpected_breakpoint"}, stop
    assert stop["frame"]["function"] == "main", stop


def test_a_gdb_error_is_a_debugger_error_stop_carrying_the_message(tmp_path: Path) -> None:
    """The last documented reason: not a record GDB wrote, but the transport's own report of a GDB that failed."""
    config = load_config(str(write_config(tmp_path, gdb_executable=FAKE_GDB)))
    sessions = OpenOCDBackend(config)._debug
    session = GdbDebugSession("debug-test", {"path": "build/app.elf"}, "load", 0, SimpleNamespace(poll=lambda: None), [], str(tmp_path / "log.json"))
    from agentic_hil.gdbmi import GdbMiStopResult

    stop = sessions._stop_reason_from_gdb(session, GdbMiStopResult(line="", reason="debugger_error", error_message="GDB process exited with code 1."))

    assert stop["stop_reason"] == "debugger_error", stop
    assert stop["backend_error"] == "GDB process exited with code 1.", stop


# ---------------------------------------------------------------------------
# The .hex and .bin plausibility refusals TROUBLESHOOTING promises.

OK_HEX = ":020000040800F2\n:00000001FF\n"


def test_hex_and_bin_artifacts_fail_their_documented_plausibility_checks(tmp_path: Path) -> None:
    """`hex_parseable: false` and `bin_size_plausible: false`, exactly as section 9 lists them.

    The check reads the file's bytes before any backend is spawned, so the
    fixture is the file itself: text that is not Intel HEX, a `.bin` of zero
    bytes, and a two-record Intel HEX that is accepted and flashed.
    """
    config = load_config(str(write_config(tmp_path)))
    build = tmp_path / "build"
    build.mkdir(parents=True, exist_ok=True)
    (build / "bad.hex").write_text("not hex\n", encoding="ascii")
    (build / "empty.bin").write_bytes(b"")
    (build / "ok.hex").write_text(OK_HEX, encoding="ascii")

    bad_hex = call(config, "flash_firmware", {"image_path": "build/bad.hex"})
    empty_bin = call(config, "flash_firmware", {"image_path": "build/empty.bin"})
    ok_hex = call(config, "flash_firmware", {"image_path": "build/ok.hex"})

    assert bad_hex["ok"] is False, bad_hex
    assert bad_hex["error_type"] == "artifact_validation_failed", bad_hex
    assert bad_hex["validation"]["hex_parseable"] is False, bad_hex["validation"]
    assert "plausibility" in bad_hex["summary"], bad_hex
    # Refused on the file's own bytes, so the flash never ran and there is no
    # artifact to point at.
    assert "artifact" not in bad_hex, bad_hex
    assert bad_hex.get("side_effect_committed") is not True, bad_hex

    assert empty_bin["ok"] is False, empty_bin
    assert empty_bin["error_type"] == "artifact_validation_failed", empty_bin
    assert empty_bin["validation"]["bin_size_plausible"] is False, empty_bin["validation"]
    assert "artifact" not in empty_bin, empty_bin

    assert ok_hex["ok"] is True, ok_hex
    assert ok_hex.get("validation", {}).get("hex_parseable") is not False, ok_hex.get("validation")
    assert ok_hex.get("validation", {}).get("bin_size_plausible") is not False, ok_hex.get("validation")
    assert ok_hex["artifact"]["path"] == "build/ok.hex", ok_hex
    assert ok_hex["success_confirmed"] is True, ok_hex


def test_troubleshooting_still_names_both_plausibility_keys() -> None:
    """The promise the test above holds the code to, held in the document that makes it."""
    text = TROUBLESHOOTING.read_text(encoding="utf-8")

    assert "`hex_parseable: false`" in text
    assert "`bin_size_plausible: false`" in text
