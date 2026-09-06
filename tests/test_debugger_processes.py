"""What the backends do with the process behind a configured debugger.

Two conditions from #485, each reachable on any bench and each unpinned until
now: a debugger that starts and never finishes, and an executable configured
the way Linux documentation spells it, as a bare name on PATH or as nothing at
all.

A hung tool has a contract. The process is terminated with its process tree
when `debuggers.<name>.timeout_s` runs out, the log is written with `timed_out`
true, and the tool answers `error_type` `timeout` with `likely_causes` and a
`log_path` an operator can open. Probe discovery, which never addresses a
board, answers its own sentence. Until this module the only `timeout` results
the suite had seen were dicts typed by hand; nothing had driven a process that
hangs through `spawn_command`.

A bare name has two rules, decided by where the lookup happens. `executable:
openocd` is resolved through PATH when the configuration is loaded, and a PATH
that lacks it is a load failure: `config_invalid`, naming the field. An omitted
executable is the entry `init` writes before a toolchain is installed, and it
must not stop the configuration from loading; the debugger call refuses it with
the `debugger_not_found` refusal the absolute-path case has always carried.
Every configuration the suite wrote before this used an absolute fake path, so
neither branch had been taken.

The non-executable file (present, and this host will not run it) is #479, is
already fixed on this branch, and is pinned in `test_executable_refusals.py`;
nothing here repeats it.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import pytest
from conftest import FAKE_OPENOCD, write_authoritative_config, write_config

from agentic_hil.config import ConfigError, debugger_is_placeholder, load_authoritative_config, load_config
from agentic_hil.tools import AgenticHILToolService

ROOT = Path(__file__).resolve().parents[1]
FAKE_HUNG_DEBUGGER = ROOT / "tests" / "fixtures" / "fake_openocd_hung.py"

# The deadline the hung fake is given, and the longest the whole call may take
# before the test says the deadline was not enforced. The fake sleeps for 30 s,
# so a call that only ends when the fake does ends well outside this.
HANG_TIMEOUT_S = 1
CALL_CEILING_S = 15.0
# How long the reaped processes are given to disappear from the process table
# after the call returned: the backend's own reap waits up to CHILD_REAP_TIMEOUT_S.
REAP_CEILING_S = 5.0

WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Arrangements.


def config_with_debugger(directory: Path, tool: Path, *, debugger_type: str = "openocd", timeout_s: int = HANG_TIMEOUT_S) -> Path:
    """A configuration whose one debugger is `tool`, allowed `timeout_s` seconds.

    `write_config` writes every entry with `timeout_s: 5`; the hung-tool tests
    need a deadline the call is measured against, so the number is rewritten in
    the file exactly as an operator would edit it.
    """
    path = write_config(directory, debugger_type=debugger_type, debugger_executable=tool)
    text = path.read_text(encoding="utf-8")
    assert text.count("timeout_s: 5") == 1, text
    path.write_text(text.replace("timeout_s: 5", f"timeout_s: {timeout_s}"), encoding="utf-8")
    return path


def authoritative_config_with_executable_spelling(workspace: Path, monkeypatch: pytest.MonkeyPatch, spelling: str) -> Path:
    """The authoritative configuration of a bench, with `debuggers.dut.executable` written as `spelling`.

    `spelling` is the YAML value verbatim: `openocd` for the bare name, `null`
    for the omitted executable. The written entry is the only line in the file
    whose key is `executable`; the GDB line is `gdb_executable` and is left as
    it is.

    Authoritative, because pinning is what `load_authoritative_config` does on
    the way to every production entry point and what `load_config` on a bare
    path never does. And a bench that names its probe, because the entry with
    no executable, the skeleton's two script names and nothing naming a board
    is the shipped starter, which pinning deliberately leaves inert; the case
    #485 describes is an operator's entry.
    """
    path = write_authoritative_config(workspace, monkeypatch, probe_id="ST-LINK-1")
    text = path.read_text(encoding="utf-8")
    rewritten, replaced = re.subn(r"(?m)^(\s+)executable: .*$", rf"\1executable: {spelling}", text)
    assert replaced == 1, text
    path.write_text(rewritten, encoding="utf-8")
    return path


def bare_openocd_on_path(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory holding the fake OpenOCD under the bare name `openocd`, on PATH.

    What the name resolves to is the platform's business. On POSIX `openocd` is
    a shell wrapper that runs the fake through this interpreter, so the file
    PATH finds is a program the operating system executes directly, the way a
    packaged OpenOCD is. On Windows a bare name is found through PATHEXT, so the
    fake is placed as `openocd.py` and `.PY` is added to the extensions a lookup
    tries; `invocation` then runs it through this interpreter as it does every
    `.py` executable.

    PATH is replaced, not extended: the point of the test is which directory
    answered, and a developer's bench may have a real OpenOCD on PATH.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        launcher = directory / "openocd.py"
        launcher.write_bytes(FAKE_OPENOCD.read_bytes())
        monkeypatch.setenv("PATHEXT", ".PY;" + os.environ.get("PATHEXT", ".EXE;.BAT;.CMD"))
    else:
        launcher = directory / "openocd"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_OPENOCD.as_posix()}" "$@"\n', encoding="utf-8")
        os.chmod(launcher, 0o755)
    monkeypatch.setenv("PATH", str(directory))
    return launcher


def path_without_openocd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    assert shutil.which("openocd") is None


def recorded_pids(pid_file: Path) -> list[int]:
    return [int(line) for line in pid_file.read_text(encoding="utf-8").split() if line.strip()]


def process_is_gone(pid: int) -> bool:
    """Whether the operating system no longer runs `pid`.

    On Linux a process the reap has already killed can still be a zombie when
    nobody has waited for it (the grandchild's parent died first), and a zombie
    runs nothing, so the state is read out of procfs rather than inferred from
    `kill(pid, 0)`, which succeeds on one. On Windows an open handle with an exit
    code other than STILL_ACTIVE is a process that has ended.
    """
    if WINDOWS:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return True
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value != still_active
        finally:
            kernel32.CloseHandle(handle)
    status = Path(f"/proc/{pid}/status")
    if status.is_dir() or status.is_file():
        try:
            state = next(line for line in status.read_text(encoding="utf-8").splitlines() if line.startswith("State:"))
        except (OSError, StopIteration):
            return True
        return "Z" in state.split(":", 1)[1]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def wait_until_gone(pids: list[int], timeout_s: float) -> list[int]:
    """The pids of `pids` still running after `timeout_s`, ideally none."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if all(process_is_gone(pid) for pid in pids):
            return []
        time.sleep(0.05)
    return [pid for pid in pids if not process_is_gone(pid)]


def written_log(config, result: dict) -> dict:
    log_path = Path(result["log_path"])
    if not log_path.is_absolute():
        log_path = Path(config.work_dir) / log_path
    return json.loads(log_path.read_text(encoding="utf-8"))


@pytest.fixture
def pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    recorded = tmp_path / "hung-debugger.pids"
    monkeypatch.setenv("AGENTIC_HIL_TEST_PID_FILE", str(recorded))
    return recorded


# ---------------------------------------------------------------------------
# A hung tool is reaped and reported as a timeout.


@pytest.mark.parametrize("debugger_type", ["openocd", "pyocd", "stlink"])
def test_a_hanging_debugger_is_reaped_and_answers_timeout(tmp_path: Path, pid_file: Path, debugger_type: str) -> None:
    """The whole of the timeout contract, driven through a process that hangs.

    The answer, the log, and the process table are three separate claims, and a
    reap that got two of them right is the one that leaves a debugger holding
    the probe open for the next call to find.
    """
    config = load_config(str(config_with_debugger(tmp_path, FAKE_HUNG_DEBUGGER, debugger_type=debugger_type)))
    service = AgenticHILToolService(config)
    started = time.monotonic()
    try:
        result = service.call("probe_target")
    finally:
        service.close()
    elapsed = time.monotonic() - started

    assert result["ok"] is False, json.dumps(result)
    assert result["error_type"] == "timeout"
    assert result["summary"] == "Debugger command timed out."
    assert result["backend"] == debugger_type
    assert result["likely_causes"], result
    assert result["log_path"], result
    # The deadline was the deadline: the call ended because timeout_s ran out,
    # not because the fake's own 30 s sleep did.
    assert elapsed < CALL_CEILING_S, f"the call took {elapsed:.1f} s against a {HANG_TIMEOUT_S} s deadline"

    log = written_log(config, result)
    assert log["timed_out"] is True, log
    assert log["returncode"] != 0, log

    # At least the tool and its child; the service re-reads the probe to settle
    # the incident a timeout raises, so the hung tool may have been started more
    # than once, and every one of those processes has to be gone as well.
    pids = recorded_pids(pid_file)
    assert len(pids) >= 2 and len(pids) % 2 == 0, f"the fake did not record itself and its child: {pids}"
    assert wait_until_gone(pids, REAP_CEILING_S) == [], "the debugger or its child survived the reap"


def test_a_timed_out_probe_keeps_the_target_state_unconfirmed(tmp_path: Path, pid_file: Path) -> None:
    """The service-layer reading of a real timeout, not of a dict typed by hand.

    `test_quarantine_triggers` pins what the service does with a `timeout`
    result by substituting one; this is the same rule reached through a process
    that actually hung, so the two cannot drift apart without one of them going
    red.
    """
    config = load_config(str(config_with_debugger(tmp_path, FAKE_HUNG_DEBUGGER)))
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["error_type"] == "timeout"
    assert result["quarantined"] is False
    assert result["cleanup_reasons"] == ["debugger_readonly_target_state_unconfirmed"]


@pytest.mark.parametrize("debugger_type", ["pyocd", "stlink"])
def test_a_hanging_probe_discovery_answers_its_own_timeout_sentence(tmp_path: Path, pid_file: Path, debugger_type: str) -> None:
    """Discovery never addresses a board, and its timeout says what timed out."""
    config = load_config(str(config_with_debugger(tmp_path, FAKE_HUNG_DEBUGGER, debugger_type=debugger_type)))
    service = AgenticHILToolService(config)
    started = time.monotonic()
    try:
        result = service.call("debugger_probes_list")
    finally:
        service.close()
    elapsed = time.monotonic() - started

    assert result["ok"] is False, json.dumps(result)
    assert result["error_type"] == "timeout"
    assert result["summary"] == "Debugger probe discovery timed out."
    assert result["target_contacted"] is False
    assert result["side_effect_status"] == "not_started"
    assert elapsed < CALL_CEILING_S, f"the call took {elapsed:.1f} s against a {HANG_TIMEOUT_S} s deadline"
    assert wait_until_gone(recorded_pids(pid_file), REAP_CEILING_S) == [], "the debugger or its child survived the reap"


def test_a_debugger_that_finishes_inside_the_deadline_is_untouched(tmp_path: Path) -> None:
    """The neighbour: a tool that answers in time is not a timeout and says so in its log."""
    config = load_config(str(config_with_debugger(tmp_path, FAKE_OPENOCD, timeout_s=5)))
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is True, json.dumps(result)
    assert written_log(config, result)["timed_out"] is False


# ---------------------------------------------------------------------------
# A bare name resolves through PATH at load; an omitted one is looked up for the call.


def test_a_bare_openocd_name_resolves_through_path_at_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`executable: openocd`, the common Linux spelling, pins the program PATH names."""
    workspace = tmp_path / "workspace"
    launcher = bare_openocd_on_path(tmp_path / "on-path", monkeypatch)
    authoritative_config_with_executable_spelling(workspace, monkeypatch, "openocd")

    config = load_authoritative_config(workspace)

    pinned = Path(config.debugger.executable)
    assert pinned.is_absolute(), config.debugger.executable
    assert pinned.samefile(launcher), (pinned, launcher)
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()
    assert result["ok"] is True, json.dumps(result)
    assert result["target_detected"] is True


def test_a_bare_name_that_path_lacks_is_refused_when_the_configuration_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A name that resolves to nothing is a configuration that names nothing."""
    workspace = tmp_path / "workspace"
    authoritative_config_with_executable_spelling(workspace, monkeypatch, "openocd")
    path_without_openocd(monkeypatch)

    with pytest.raises(ConfigError) as raised:
        load_authoritative_config(workspace)

    assert raised.value.error_type == "config_invalid"
    assert raised.value.summary == "Configured executable could not be resolved at startup."
    assert raised.value.details["field"] == "debuggers.dut.executable"
    assert raised.value.details["value"] == "openocd"


def test_an_omitted_executable_is_looked_up_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`executable: null` runs the `openocd` PATH names, without anybody writing the path."""
    workspace = tmp_path / "workspace"
    launcher = bare_openocd_on_path(tmp_path / "on-path", monkeypatch)
    authoritative_config_with_executable_spelling(workspace, monkeypatch, "null")

    config = load_authoritative_config(workspace)
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is True, json.dumps(result)
    assert Path(config.debugger.executable).samefile(launcher)


def test_an_omitted_executable_that_path_lacks_loads_and_refuses_the_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bench whose toolchain is not installed yet: the file loads, the call refuses.

    The refusal is the one the absolute-path case has always carried, so a
    caller that reads `debugger_not_found` reads the same next step whichever
    way the executable was left unresolved.
    """
    workspace = tmp_path / "workspace"
    authoritative_config_with_executable_spelling(workspace, monkeypatch, "null")
    path_without_openocd(monkeypatch)

    config = load_authoritative_config(workspace)
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False, json.dumps(result)
    assert result["error_type"] == "debugger_not_found"
    assert result["backend_error_type"] == "openocd_not_found"
    assert result["summary"] == "Debugger executable could not be found."
    assert result["target_contacted"] is False


def test_an_absolute_path_is_pinned_without_consulting_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour every other test in the suite relies on: an absolute path is not looked up."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, probe_id="ST-LINK-1")
    path_without_openocd(monkeypatch)

    config = load_authoritative_config(workspace)

    assert Path(config.debugger.executable).samefile(FAKE_OPENOCD)


def test_the_shipped_starter_entry_stays_inert_whatever_path_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour the omitted-executable rule must not swallow.

    The entry `init` writes, with no executable, the skeleton's two script names
    and nothing naming a board, is the one entry pinning does not go looking
    for a toolchain for: on a host with OpenOCD on PATH it would otherwise
    become an entry that grants everything, has a real program behind it and is
    checked by nobody.
    """
    bare_openocd_on_path(tmp_path / "on-path", monkeypatch)
    workspace = tmp_path / "workspace"
    path = write_authoritative_config(workspace, monkeypatch, interface_cfg="interface/stlink.cfg", target_cfg="target/stm32f4x.cfg")
    text = path.read_text(encoding="utf-8")
    rewritten, replaced = re.subn(r"(?m)^(\s+)executable: .*$", r"\1executable: null", text)
    assert replaced == 1, text
    path.write_text(rewritten, encoding="utf-8")

    config = load_authoritative_config(workspace)

    assert debugger_is_placeholder(config.debugger), config.debugger.executable
    assert not Path(config.debugger.executable).exists(), config.debugger.executable
