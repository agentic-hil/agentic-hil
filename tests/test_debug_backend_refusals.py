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
import tempfile
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
    FAKE_STLINK_SHORT_READ,
    elf_with_symbols,
    write_config,
)
from support import scaled_time_bound

# What the fake GDB answers for a refused read, which is what the product puts in
# the summary. Imported rather than repeated: a placeholder that drifted between
# the fake and the assertion would read like a product regression. The fixture
# says why it is a placeholder and names the recording that is owed.
from fixtures.fake_gdb import MEMORY_READ_REFUSAL as GDB_MEMORY_READ_REFUSAL

from agentic_hil.backends.common import NOT_CONTACTED
from agentic_hil.backends.gdbdebug import GdbDebugSession
from agentic_hil.backends.openocd import OpenOCDBackend
from agentic_hil.backends.pyocd import PyOCDBackend
from agentic_hil.backends.stlink import STLinkBackend
from agentic_hil.config import load_config
from agentic_hil.gdbmi import GdbMiClient, stop_result_from_line
from agentic_hil.knowledge import ERROR_CATALOGUE, ErrorRemedy, catalogue_entry, remediation_fields
from agentic_hil.tools import AgenticHILToolService

T = TypeVar("T")

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
FAKE_TRANSCRIPT = FIXTURES / "fake_debugger_transcript.py"
FAKE_PYOCD_RESET_REFUSED = FIXTURES / "fake_pyocd_reset_refused.py"
FAKE_PYOCD_READ_FAILED = FIXTURES / "fake_pyocd_read_failed.py"
FAKE_PYOCD_SILENT_READ = FIXTURES / "fake_pyocd_silent_read.py"
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


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_debugger_info_refuses_a_tool_that_went_between_the_resolve_and_the_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_name: str) -> None:
    """The same refusal from the other side of a window nothing can close.

    `info()` resolves the executable and then spawns it, and nothing holds the
    file between the two: an upgrade, a cleanup or an operator can take it away
    in that window, and what the spawn reports then is a program that is not
    there. That report has a branch of its own, and it has to answer the same
    not-found refusal the resolve does rather than let a spawn failure out. The
    resolve here answers what it answered while the file was there, which is
    exactly what it would have answered a moment before the file went.
    """
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    # No `.py` suffix: a fake with one is spawned as an argument to this
    # interpreter, and the interpreter is what the operating system would then
    # find. The path under test is the one where the tool itself is the program.
    executable = toolchain / f"{backend_name}-tool"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    config = config_for(tmp_path, backend_name, executable)
    backend = BACKEND_CLASS[backend_name](config)
    resolved = backend._resolve_executable()
    assert resolved["ok"] is True, resolved
    executable.unlink()
    monkeypatch.setattr(backend, "_resolve_executable", lambda: dict(resolved))

    result = backend.info()

    assert result["ok"] is False, result
    assert result["tool"] == "debugger_info"
    assert result["error_type"] == "debugger_not_found", result
    assert result["backend_error_type"] == NOT_FOUND_BACKEND_ERROR[backend_name], result
    assert "version" not in result, result


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_failure_whose_words_match_no_bucket_is_the_same_public_error_everywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_name: str) -> None:
    """The neighbour of the version check: one public name for "the debugger failed and said no more".

    The version check above publishes `debugger_error` for a failure no bucket
    matched, which is the name the debug-session backend has always given an
    unclassified server failure. This pins that the name is the backend's answer
    for that classification wherever it is reached, not a special case of the
    version check: the same unmatched words through `probe_target` publish it
    too, with the backend's own `unknown_debugger_error` still carried beside it
    for a caller that reads the backend layer.
    """
    play_transcript(monkeypatch, stderr="Error: the tool gave up and said nothing about why\n", returncode=1)
    config = config_for(tmp_path, backend_name, FAKE_TRANSCRIPT)

    result = call(config, "probe_target")

    assert result["ok"] is False, result
    assert result["backend_error_type"] == "unknown_debugger_error", result
    assert result["error_type"] == "debugger_error", result


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
#
# The remediation is compared against `remediation_fields` rather than spelled
# out, so a row follows its bucket's entry wherever the catalogue puts it. The
# five entries these rows once pinned as absent are written at the bottom of
# this file (#516).
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

    #506 lists `config_file_not_found` among the public error_types these
    phrases map to, so the name stays and the catalogue is what has to tell the
    two apart: the backend-scoped entry, the same mechanism the running-server
    variant of this error_type already uses. What the operator gets instead is
    asserted here rather than only what they must not get, so no fix can be had
    by publishing an undocumented error_type or by leaving the result silent.
    """
    play_transcript(monkeypatch, stderr="ST-LINK SN  : STLINK123\nError: File build/firmware.elf not found\n", returncode=1)
    config = config_for(tmp_path, "stlink", FAKE_TRANSCRIPT)

    result = call(config, "flash_firmware", {"image_path": "build/firmware.elf"})

    assert result["ok"] is False, result
    assert result["backend_error_type"] == "config_file_not_found", result
    assert result["error_type"] == "config_file_not_found", result
    assert result["summary"] == "Debugger input file could not be found.", result
    scoped = remediation_fields("config_file_not_found", "stlink")
    assert result.get("remediation") == scoped["remediation"], result.get("remediation")
    assert scoped["remediation"] != remediation_fields("config_file_not_found")["remediation"], scoped
    steps = json.dumps(scoped["remediation"])
    assert "agentic-hil init" not in steps, steps
    # The argument that was wrong, named, so the steps are about this refusal
    # and not a second copy of the configuration route under another name.
    assert "image_path" in steps, steps


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

    Raising out of `close` in a `finally` replaces every assertion the test came
    to make with the cleanup's own complaint, so the error is returned and
    asserted on with the rest.
    """
    try:
        service.close()
    except BaseException as error:  # noqa: BLE001 - asserted on by the caller
        return error
    return None


# What closing a session whose GDB died has to keep doing. The target was last
# seen resumed and the debugger then died, so whether the core is halted is
# genuinely unknown, and the close contract already answers that: the session is
# left `cleanup_required` with `hardware_state_unconfirmed` set and the close
# raises rather than reporting a tidy end (`GdbDebugSessions.close`, pinned by
# tests/test_debug_sessions.py and tests/test_debug_session_run_state.py). #506
# asks nothing about `close`, so it is pinned here as unchanged: no fix for a
# GDB that dies may buy its green by skipping the halt reconfirmation for a
# board that may still be running.
UNCONFIRMED_CLOSE_SENTENCE = "reconfirming the target was halted"


def assert_close_refused_to_call_the_target_settled(closing: BaseException | None, service: AgenticHILToolService) -> None:
    assert isinstance(closing, RuntimeError), f"closing a session whose GDB exited answered {closing!r}"
    assert UNCONFIRMED_CLOSE_SENTENCE in str(closing), closing
    session = service.backend._debug.session
    assert session is not None, "the unsettled session was cleared by the close that could not settle it"
    assert session.status == "cleanup_required", session.status
    assert session.hardware_state_unconfirmed is True, session


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
    assert started["elapsed_ms"] < scaled_time_bound(START_TIMEOUT_S * 1000 / 2), started
    assert started["cleanup_confirmed"] is True, started
    assert started["side_effect_status"] == "not_started", started
    assert started["retry_safe"] is True, started
    assert decisive_line in Path(tmp_path / started["log_path"]).read_text(encoding="utf-8"), started["log_path"]


def test_a_gdb_that_dies_after_acknowledging_the_resume_ends_the_session_in_error(tmp_path: Path) -> None:
    """`^running`, then the pipe closes: a `debugger_error` stop naming the status GDB left with.

    This is the timing #506 names. The stop the product waited for never comes;
    what comes is the exit, and it is read as a debugger failure rather than as
    a target that did not stop before the timeout, so the caller is not told to
    wait longer. The pipe closed before the wait began, which is a fact about
    this run's timing and not about what the caller has to be told: a stop that
    never comes because the debugger died names the code it died with, here as
    in the two timings below, or a log reader cannot tell a GDB that crashed
    from one that was never there. The session is over: its status is `error`
    and a halt asked for afterwards is refused instead of being sent into a
    closed pipe.
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
    assert continued["stop"]["backend_error"] == "GDB process exited with code 0.", continued["stop"]
    assert continued["session"]["status"] == "error", continued
    assert status["status"] == "error", status
    assert halted["ok"] is False, halted
    # The session layer answers first: a session already in `error` takes no
    # further commands, so the transport's own "GDB process is not running." is
    # what a caller reaching past the session meets, and that is pinned on the
    # transport itself in test_the_transport_answers_an_exited_gdb_in_its_own_words.
    assert halted["error_type"] == "session_not_active", halted
    assert_close_refused_to_call_the_target_settled(closing, service)


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
    assert_close_refused_to_call_the_target_settled(closing, service)


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
    assert_close_refused_to_call_the_target_settled(closing, service)


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


# ---------------------------------------------------------------------------
# The five backend-scoped remediation entries the classifier buckets are owed.
#
# `verify_failed`, `flash_failed` and `memory_read_failed` had no catalogue
# entry at all, scoped or unscoped, so the row table above could pin only the
# bucket and the absence of steps: an operator whose flash failed to verify was
# told which bucket the failure fell in and nothing about what to do next.
# What each of them has to say is the tool's own account of that operation, so
# the entries are scoped per backend the way `flash_erase_failed` already is. A
# failed verify under STM32CubeProgrammer is a different investigation from a
# failed verify under pyOCD (one has a connect mode and option bytes, the other
# has an erase, a program and a flash algorithm out of a pack), and one generic
# entry that fitted both would name neither tool's options (#516).
#
# The lookup is the one the existing scoped entries use: `remediation_fields`
# forms `<error_type>:<scope>` and falls back to the unscoped key, so nothing
# on the result path changes for these to arrive.

# (bucket, backend, the tool's name as the catalogue writes it, the operation
# the steps have to be about, the subjects the decided entry covers).
OWED_SCOPED_ENTRIES = [
    ("verify_failed", "stlink", "STM32CubeProgrammer", "verify", ("connect_mode", "option bytes")),
    ("verify_failed", "pyocd", "pyOCD", "verify", ("erase", "probe", "target_type")),
    ("flash_failed", "pyocd", "pyOCD", "flash", ("target_type", "frequenc")),
    # `` `program` `` with its backticks and not the bare word: every
    # neighbouring entry opens by sending the reader to `programmer_output`, so
    # a bare `program` is satisfied by an entry that never names the command.
    ("flash_failed", "openocd", "OpenOCD", "flash", ("target_cfg", "bank", "`program`")),
    ("memory_read_failed", "pyocd", "pyOCD", "read", ("probe", "halt", "address", "memory")),
]


@pytest.mark.parametrize(
    ("bucket", "backend_name", "tool_name", "operation", "subjects"),
    OWED_SCOPED_ENTRIES,
    ids=[f"{row[0]}-{row[1]}" for row in OWED_SCOPED_ENTRIES],
)
def test_each_bucket_the_catalogue_left_unanswered_has_its_own_backend_scoped_entry(bucket: str, backend_name: str, tool_name: str, operation: str, subjects: tuple[str, ...]) -> None:
    """A refusal that names a bucket and hands over no next step is the gap this closes.

    Non-empty is the first half; the second is that the steps are about this
    tool and this operation, because the reason the entries are scoped at all
    is that a generic answer for `verify_failed` could name neither tool's own
    verify. The subjects each entry has to cover are the ones the issue's
    decision names for it, so an entry that reads well and answers a different
    question fails here rather than shipping.

    The prose is what those checks read, and the key fields are excluded from
    it on purpose: `verify_failed` contains `verify` and `memory_read_failed`
    contains `read`, so an operation checked against the serialized entry would
    be satisfied by the `error_type` the line above already pinned and could
    never fail.
    """
    key = f"{bucket}:{backend_name}"

    assert key in ERROR_CATALOGUE, sorted(ERROR_CATALOGUE)
    fields = remediation_fields(bucket, backend_name)
    assert fields.get("remediation"), key
    entry = catalogue_entry(key)
    assert entry["error_type"] == bucket, entry
    assert entry["scope"] == backend_name, entry
    assert entry["meaning"].strip(), entry
    said = json.dumps([entry["meaning"], entry["remediation"], entry.get("do_not", [])])
    assert tool_name in said, said
    assert operation in said.lower(), (operation, said)
    for subject in subjects:
        assert subject in said.lower(), (subject, said)
    # The nearest neighbour in the other direction: the same backend's
    # `flash_erase_failed`, which is the entry an implementer would most
    # plausibly copy. Its steps are about an erase the device refused, so an
    # entry that repeated them would answer a different failure under this name.
    erase = remediation_fields("flash_erase_failed", backend_name)
    assert erase.get("remediation"), backend_name
    assert fields["remediation"] != erase["remediation"], key


SHARED_BUCKETS = {"verify_failed": ("stlink", "pyocd"), "flash_failed": ("pyocd", "openocd")}


@pytest.mark.parametrize("bucket", sorted(SHARED_BUCKETS))
def test_the_two_backends_that_share_a_bucket_do_not_share_its_steps(bucket: str) -> None:
    """Scoping is the point: two entries under one bucket that said the same thing would be the generic entry again.

    The steps are what has to differ, and only the steps. Nothing decides that
    two backends may not warn against the same thing, and the three
    `flash_erase_failed` entries show `do_not` lists across backends legitimately
    opening with one sentence, so a check over `do_not` would be a gate this
    design never asked for.
    """
    first_backend, second_backend = SHARED_BUCKETS[bucket]
    first = remediation_fields(bucket, first_backend)
    second = remediation_fields(bucket, second_backend)

    assert first.get("remediation"), (bucket, first_backend)
    assert second.get("remediation"), (bucket, second_backend)
    assert first["remediation"] != second["remediation"], bucket


SILENT_PAIRS = [
    ("verify_failed", "openocd"),
    ("flash_failed", "stlink"),
    ("memory_read_failed", "stlink"),
    ("memory_read_failed", "openocd"),
]


@pytest.mark.parametrize(("bucket", "backend_name"), SILENT_PAIRS, ids=[f"{row[0]}-{row[1]}" for row in SILENT_PAIRS])
def test_a_bucket_on_a_backend_the_catalogue_does_not_name_stays_silent(bucket: str, backend_name: str) -> None:
    """The neighbour that must not move: writing five entries is not writing fifteen.

    Each of these pairs is a bucket a backend can produce and for which nobody
    has written the tool's own steps. Silence is what a result carries there
    today, and silence is better than another backend's advice arriving under a
    generic name: an operator whose OpenOCD verify failed must not be told about
    STM32CubeProgrammer's connect mode.
    """
    assert f"{bucket}:{backend_name}" not in ERROR_CATALOGUE, sorted(ERROR_CATALOGUE)
    assert remediation_fields(bucket, backend_name) == {}, (bucket, backend_name)


@pytest.mark.parametrize("bucket", ["verify_failed", "flash_failed", "memory_read_failed"])
def test_the_three_buckets_grow_no_unscoped_entry(bucket: str) -> None:
    """`flash_erase_failed`'s convention, which these follow: scoped entries and no generic one.

    An unscoped entry would be reached by every backend that has no scoped one,
    which is the fallback `lookup_remedy` performs, so adding one would answer
    the silent pairs above with advice nobody wrote for them.
    """
    assert {key for key in ERROR_CATALOGUE if key.partition(":")[0] == "flash_erase_failed"} == {
        "flash_erase_failed:stlink",
        "flash_erase_failed:openocd",
        "flash_erase_failed:pyocd",
    }
    assert bucket not in ERROR_CATALOGUE, bucket
    assert remediation_fields(bucket) == {}, bucket


# The four transcripts the row table already plays for these pairs, driven here
# for the one thing the row table cannot assert while the catalogue is silent:
# that the refusal reaching the operator carries this backend's steps and not an
# empty field.
#
# The OpenOCD row replays the transcript
# `test_a_flash_that_failed_without_reaching_its_marker_is_flash_failed` owns, so
# a change to `** Programming Failed **` or to the rule that reads it has two
# call sites in this file and not one.
SCOPED_REMEDIATION_ROWS = [
    ("stlink", "", "ST-LINK SN  : STLINK123\nMemory Programming ...\nError: Verify failed at address 0x08000000\n", "verify_failed", "STM32CubeProgrammer"),
    ("pyocd", "", "0000900 E Verify failed at 0x08000000 [load_cmd]\n", "verify_failed", "pyOCD"),
    ("pyocd", "", "0000900 C Flash programming failed [load_cmd]\n", "flash_failed", "pyOCD"),
    ("openocd", f"{PROGRAMMING_FAILED}\n", "Error: failed to write memory at 0x08000000\n", "flash_failed", "OpenOCD"),
]


@pytest.mark.parametrize(
    ("backend_name", "stdout", "stderr", "error_type", "tool_name"),
    SCOPED_REMEDIATION_ROWS,
    ids=[f"{row[0]}-{row[3]}" for row in SCOPED_REMEDIATION_ROWS],
)
def test_a_failed_verify_or_flash_hands_the_operator_that_tools_own_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_name: str, stdout: str, stderr: str, error_type: str, tool_name: str) -> None:
    """The whole point of the entries, seen where an operator meets them: on the refusal itself."""
    play_transcript(monkeypatch, stdout=stdout, stderr=stderr, returncode=1)
    config = config_for(tmp_path, backend_name, FAKE_TRANSCRIPT)

    result = call(config, "flash_firmware", {"image_path": "build/firmware.elf"})

    assert result["ok"] is False, result
    assert result["error_type"] == error_type, result
    assert result.get("remediation"), result
    assert result["remediation"] == remediation_fields(error_type, backend_name)["remediation"], result["remediation"]
    assert any(tool_name in step for step in result["remediation"]), result["remediation"]
    # The scoped entry is what arrives, not the fallback: there is no unscoped
    # entry for these buckets, and a result carrying one would mean somebody
    # wrote the generic entry this design decided against.
    assert remediation_fields(error_type) == {}, error_type


# The two shapes a pyOCD read fails in, both classified `memory_read_failed` and
# both reaching for one entry. The first is the commander reporting the read
# failed; the second is a run that exited 0 and left no file holding the window
# that was asked for, which is a failed read that did reach the target. One
# entry answers both, so the steps have to be about a read that could not be
# taken rather than about the words of one transcript.
PYOCD_READ_FAILURE_SHAPES = [
    (FAKE_PYOCD_READ_FAILED, "Transfer error while reading"),
    (FAKE_PYOCD_SILENT_READ, "pyOCD reported a completed run but left no file holding the requested bytes."),
]


@pytest.mark.parametrize(("executable", "evidence"), PYOCD_READ_FAILURE_SHAPES, ids=["commander-reported-failed", "completed-run-left-no-bytes"])
def test_a_failed_pyocd_read_hands_the_operator_the_read_steps(tmp_path: Path, executable: Path, evidence: str) -> None:
    """The third bucket, through the two paths that can produce it: a read against a flashed ELF.

    The first row drives the flow
    `test_a_pyocd_read_the_commander_reported_failed_is_memory_read_failed`
    already owns, for the assertions that test cannot make while the catalogue is
    silent; the second is the shape `tests/test_debug_sessions.py` pins, and it
    is here because it receives the same steps the moment the entry exists and
    nothing else says the wording fits it.
    """
    service = read_service(tmp_path, executable)
    try:
        assert service.call("flash_firmware", {"image_path": "build/app.elf"})["ok"] is True
        value = service.call("debug_symbol_value", {"symbol": "boot_counter"})
    finally:
        service.close()

    assert value["ok"] is False, value
    assert value["error_type"] == "memory_read_failed", value
    assert evidence in json.dumps(value), value
    assert value.get("remediation"), value
    assert value["remediation"] == remediation_fields("memory_read_failed", "pyocd")["remediation"], value["remediation"]
    assert any("pyOCD" in step for step in value["remediation"]), value["remediation"]


def test_a_read_that_never_left_this_host_carries_the_same_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shape of this bucket the entry opens by naming, and the one that answered with nothing.

    A read whose private staging file cannot be created is `memory_read_failed`
    like the two above, and it is the shape the entry's first step is written
    for: it tells the reader that this summary is about the host's temporary
    directory rather than about the board, so that a caller does not go looking
    at a probe that was never opened. That step is unreachable on the one result
    it is about unless this branch merges the bucket's steps as the branches
    that go through the classifier do.
    """
    import tempfile as tempfile_module

    real_mkdtemp = tempfile_module.mkdtemp

    def refuse_the_read_staging_directory(*args: object, **kwargs: object):
        if kwargs.get("prefix") == "agentic-hil-pyocd-read-":
            raise OSError("no space left on device")
        return real_mkdtemp(*args, **kwargs)

    service = read_service(tmp_path, FAKE_PYOCD_READ_FAILED)
    try:
        assert service.call("flash_firmware", {"image_path": "build/app.elf"})["ok"] is True
        monkeypatch.setattr(tempfile_module, "mkdtemp", refuse_the_read_staging_directory)
        value = service.call("debug_symbol_value", {"symbol": "boot_counter"})
    finally:
        service.close()

    assert value["ok"] is False, value
    assert value["error_type"] == "memory_read_failed", value
    assert value["summary"] == "The private file this read needs could not be created.", value
    # The half that must not move with it: nothing was sent, so the promise that
    # the board is exactly as the flash left it stands.
    assert value["target_contacted"] is False, value
    assert value["remediation"] == remediation_fields("memory_read_failed", "pyocd")["remediation"], value.get("remediation")
    assert any(value["summary"] in step for step in value["remediation"]), value["remediation"]


# ---------------------------------------------------------------------------
# The five `memory_read_failed` results the other two backends build by hand.
#
# #516 wrote the read bucket its first entry, `memory_read_failed:pyocd`, and
# that backend merges it where each result is built, including the three it
# assembles by hand rather than reads out of a transcript. The GDB session
# backend and the ST-Link backend build the same bucket in five more places and
# none of them asks the catalogue: the response GDB refuses, the contents that
# do not parse, the read that comes back short, the read whose private staging
# file could not be created, and the run STM32CubeProgrammer confirmed that left
# no parseable Intel HEX covering the requested bytes. Nothing is missing on
# screen today only because `memory_read_failed:openocd` and
# `memory_read_failed:stlink` have no entry; the day either is written it would
# arrive on every result the classifier builds and on none of these five (#521).
#
# Whether those two entries get written is a separate decision about each tool's
# own investigation, so none is added here and the silence #516 pinned stands.
# Each test plants an entry for the duration of the test instead and reads it
# back off the result, which is exactly what the result path has to do the
# moment a real entry exists, and the silence without a planted entry is pinned
# beside it so writing this merge cannot invent advice nobody wrote.

# The planted entries. Their words say nothing about a read on purpose: what is
# under test is that whatever the catalogue holds for this bucket on this
# backend reaches the result, and a step that read like real advice would let a
# hand-copied sentence pass for the lookup.
PLANTED_READ_ENTRY = ErrorRemedy(
    meaning="Planted for one test: what this backend's failed read would mean.",
    remediation=("Planted first step for the read bucket.", "Planted second step for the read bucket."),
    do_not=("Planted step this backend's failed read must not be answered with.",),
)
# The timeout keeps its own error_type, so it has to reach its own entry: a
# merge that handed every shape of this branch the read bucket's steps would
# tell an operator whose command never came back what to do about a read the
# target refused. No `do_not`, so a result carrying one is the read entry.
PLANTED_TIMEOUT_ENTRY = ErrorRemedy(
    meaning="Planted for one test: what a GDB/MI command that did not answer would mean.",
    remediation=("Planted first step for the timeout bucket.",),
)
# The fake GDB answers a hang by never replying, so the read waits out this cap
# rather than the configured `timeout_s`. Two seconds for the reason
# test_debug_sessions gives its own cap: on a loaded machine a tighter one times
# out a healthy round trip and the test reports the wrong shape.
MEMORY_READ_TIMEOUT_CAP_S = 2.0
# The fake GDB answer each session shape is driven by, and the tool the read is
# driven through. `_read_memory_bytes` has two callers, and the merge belongs
# where the result is built rather than in either of them, so one shape is driven
# through the dump as well: an implementation that merged in `symbol_value` alone
# would leave `debug_dump_symbol_ihex` with exactly the gap this pins.
GDB_READ_CASES = {
    "gdb-refused": ("memory_read_refused", "debug_symbol_value"),
    "gdb-refused-dump": ("memory_read_refused", "debug_dump_symbol_ihex"),
    "gdb-unparsable-contents": ("memory_read_without_contents", "debug_symbol_value"),
    "gdb-short-read": ("memory_read_short", "debug_symbol_value"),
}
# The output the dump would have written, had the read it needs come back. The
# file is asserted absent: a read that failed writes nothing.
DUMP_OUTPUT_PATH = "build/symbol.hex"


def gdb_read_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, behavior: str, tool: str = "debug_symbol_value") -> dict:
    """One failed read over a session, on the fake GDB's answer named by `behavior`."""
    if behavior == "memory_read_hangs":
        # Only the hang waits this cap out. The other answers come straight back,
        # and shortening their margin on a loaded machine would buy a flake for
        # nothing.
        monkeypatch.setattr("agentic_hil.backends.gdbdebug.GDB_COMMAND_TIMEOUT_CAP_S", MEMORY_READ_TIMEOUT_CAP_S)
    arguments: dict = {"symbol": "boot_counter"}
    if tool == "debug_dump_symbol_ihex":
        arguments["output_path"] = DUMP_OUTPUT_PATH
    service = debug_service(tmp_path, fake_gdb_behavior=behavior)
    try:
        assert service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "load", "timeout_s": START_TIMEOUT_S})["ok"] is True
        value = answered_within(60.0, lambda: service.call(tool, arguments))
        if tool == "debug_dump_symbol_ihex":
            assert not (tmp_path / DUMP_OUTPUT_PATH).exists(), "the dump wrote a file for a read that failed"
    finally:
        closing = answered_within(60.0, lambda: closed_reporting_its_own_failure(service))
    # Unchanged by any of this: a session command that failed leaves where the
    # target stopped unproven, so the close refuses to call it settled.
    assert isinstance(closing, RuntimeError), f"closing a session whose read failed answered {closing!r}"
    return value


def stlink_read_service(tmp_path: Path, executable: Path = FAKE_STLINK) -> AgenticHILToolService:
    """An ST-Link bench whose reads resolve their symbol against the ELF a flash put on the board."""
    config_path = write_config(tmp_path, debugger_type="stlink", debugger_executable=executable, gdb_executable=FAKE_GDB)
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12)
    return AgenticHILToolService(load_config(str(config_path)))


def stlink_read_without_a_staging_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """The read this host would not give a private file to, so nothing was sent."""
    real_mkdtemp = tempfile.mkdtemp

    def refuse_the_read_staging_directory(*args: object, **kwargs: object):
        if kwargs.get("prefix") == "agentic-hil-symbol-value-":
            raise OSError("no space left on device")
        return real_mkdtemp(*args, **kwargs)

    service = stlink_read_service(tmp_path)
    try:
        assert service.call("flash_firmware", {"image_path": "build/app.elf"})["ok"] is True
        monkeypatch.setattr(tempfile, "mkdtemp", refuse_the_read_staging_directory)
        return service.call("debug_symbol_value", {"symbol": "boot_counter"})
    finally:
        service.close()


def stlink_read_without_parseable_hex(tmp_path: Path) -> dict:
    """The confirmed read whose file does not cover the window that was asked for."""
    service = stlink_read_service(tmp_path, FAKE_STLINK_SHORT_READ)
    try:
        assert service.call("flash_firmware", {"image_path": "build/app.elf"})["ok"] is True
        return service.call("debug_symbol_value", {"symbol": "boot_counter"})
    finally:
        service.close()


def drive_hand_built_read_failure(case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    if case == "stlink-staging-file":
        return stlink_read_without_a_staging_file(tmp_path, monkeypatch)
    if case == "stlink-no-parseable-hex":
        return stlink_read_without_parseable_hex(tmp_path)
    behavior, tool = GDB_READ_CASES[case]
    return gdb_read_failure(tmp_path, monkeypatch, behavior, tool)


# (case, the backend the result names, its summary, its `target_contacted`). The
# summary is the result's own and is asserted unchanged in both directions,
# because the merge may add fields to these results and may move nothing else.
# `target_contacted` is None where the result does not carry the field: the
# session backend's reads answer that question through the session and the
# incident they open, and this must not start answering it a second way.
HAND_BUILT_READ_FAILURES = [
    ("gdb-refused", "openocd", GDB_MEMORY_READ_REFUSAL, None),
    ("gdb-refused-dump", "openocd", GDB_MEMORY_READ_REFUSAL, None),
    ("gdb-unparsable-contents", "openocd", "GDB returned unparsable memory contents.", None),
    ("gdb-short-read", "openocd", "GDB returned fewer memory bytes than requested.", None),
    ("stlink-staging-file", "stlink", "The private file this read needs could not be created.", False),
    ("stlink-no-parseable-hex", "stlink", "STM32CubeProgrammer confirmed the read but left no parseable Intel HEX covering the requested bytes.", True),
]


@pytest.mark.parametrize(
    ("case", "backend_name", "summary", "target_contacted"),
    HAND_BUILT_READ_FAILURES,
    ids=[row[0] for row in HAND_BUILT_READ_FAILURES],
)
def test_a_hand_built_read_failure_carries_the_entry_its_backend_has(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, backend_name: str, summary: str, target_contacted: bool | None) -> None:
    """The gap: an entry for this backend's read bucket reaches every other result and none of these.

    The entry is planted for the duration of the test rather than written into
    the catalogue, because whether these two backends get real steps is a
    separate decision about each tool's own investigation. What is decided is
    that the lookup happens where the result is built, so an entry arrives the
    moment one exists.

    `do_not` is asserted with the steps: the merge is the catalogue's whole
    answer for this bucket, and a merge that carried only `remediation` would
    drop the wrong fix an entry names on purpose.
    """
    monkeypatch.setitem(ERROR_CATALOGUE, f"memory_read_failed:{backend_name}", PLANTED_READ_ENTRY)

    result = drive_hand_built_read_failure(case, tmp_path, monkeypatch)

    assert result["ok"] is False, result
    assert result["error_type"] == "memory_read_failed", result
    assert result["summary"] == summary, result
    assert result.get("target_contacted") is target_contacted, result
    assert result.get("remediation") == list(PLANTED_READ_ENTRY.remediation), result.get("remediation")
    assert result.get("do_not") == list(PLANTED_READ_ENTRY.do_not), result.get("do_not")
    # The lookup and not a copy: what arrives is what the catalogue answers for
    # this bucket on this backend, through the same scoped lookup every other
    # result on this path already goes through.
    assert result["remediation"] == remediation_fields("memory_read_failed", backend_name)["remediation"], result["remediation"]


@pytest.mark.parametrize(
    ("case", "backend_name", "summary", "target_contacted"),
    HAND_BUILT_READ_FAILURES,
    ids=[row[0] for row in HAND_BUILT_READ_FAILURES],
)
def test_a_hand_built_read_failure_stays_silent_while_its_backend_has_no_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, backend_name: str, summary: str, target_contacted: bool | None) -> None:
    """The neighbour that must not move, on the result rather than on the lookup.

    #516 pins that `remediation_fields` answers nothing for a bucket on a
    backend nobody has written for. This is the same silence where an operator
    would meet it: writing the merge must not put another tool's advice on these
    five under a generic name, and it must add no empty field either.
    """
    assert f"memory_read_failed:{backend_name}" not in ERROR_CATALOGUE, sorted(ERROR_CATALOGUE)

    result = drive_hand_built_read_failure(case, tmp_path, monkeypatch)

    assert result["ok"] is False, result
    assert result["error_type"] == "memory_read_failed", result
    assert result["summary"] == summary, result
    assert result.get("target_contacted") is target_contacted, result
    assert "remediation" not in result, result
    assert "do_not" not in result, result


def test_a_read_that_timed_out_carries_the_timeout_entry_and_not_the_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one shape of this branch that is not the read bucket, and keeps its own entry.

    `_read_memory_bytes` answers a GDB that did not reply at all with `timeout`
    and everything else it refused with `memory_read_failed`, and that split is
    what a caller reads to know whether waiting longer is the answer. So the
    merge is over the error_type the result actually carries: both entries are
    planted here, and the timeout result has to carry the timeout's steps and
    not the read's.
    """
    monkeypatch.setitem(ERROR_CATALOGUE, "memory_read_failed:openocd", PLANTED_READ_ENTRY)
    monkeypatch.setitem(ERROR_CATALOGUE, "timeout:openocd", PLANTED_TIMEOUT_ENTRY)

    value = gdb_read_failure(tmp_path, monkeypatch, "memory_read_hangs")

    assert value["ok"] is False, value
    assert value["error_type"] == "timeout", value
    assert value["summary"] == "GDB/MI command timed out.", value
    assert value.get("remediation") == list(PLANTED_TIMEOUT_ENTRY.remediation), value.get("remediation")
    # The read entry is the one carrying a `do_not`, so its absence is the
    # second half of the claim: the read bucket's advice did not arrive here.
    assert "do_not" not in value, value


def test_a_read_that_timed_out_stays_silent_while_the_catalogue_names_no_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The timeout's own silence, unchanged: nothing has written that bucket either."""
    assert "timeout" not in ERROR_CATALOGUE, sorted(ERROR_CATALOGUE)
    assert "timeout:openocd" not in ERROR_CATALOGUE, sorted(ERROR_CATALOGUE)

    value = gdb_read_failure(tmp_path, monkeypatch, "memory_read_hangs")

    assert value["ok"] is False, value
    assert value["error_type"] == "timeout", value
    assert value["summary"] == "GDB/MI command timed out.", value
    assert "remediation" not in value, value
    assert "do_not" not in value, value
