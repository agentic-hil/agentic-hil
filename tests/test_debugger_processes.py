"""What the backends do with the process behind a configured debugger.

Two conditions from #485, each reachable on any bench and each unpinned until
now: a debugger that starts and never finishes, and an executable configured
the way Linux documentation spells it, as a bare name on PATH or as nothing at
all.

A hung tool has a contract. The process is terminated with its process tree
when `debuggers.<name>.timeout_s` runs out, the log is written with `timed_out`
true, and the tool answers `error_type` `timeout` with `likely_causes` and a
`log_path` an operator can open. Probe discovery, which never addresses a
board, answers its own sentence, and so does the version check `doctor` runs.
Until this module the only `timeout` results the suite had seen were dicts
typed by hand; nothing had driven a process that hangs through
`spawn_command`.

Where that contract stood before any change, so a red here is read for what it
is. The deadline, the answer and the log already held: every hung-fake test in
this module is green on the current code wherever the hosted matrix runs it,
and each is a pin whose mutation check is recorded beside it. What did not hold
is the last question of the reap, whether the process group has emptied. The
group is asked with `killpg(pgid, 0)`, which counts a zombie as a member, and a
descendant the SIGKILL reached becomes a zombie of whoever adopted it. Under an
init that reaps orphans it disappears at once; under a parent that adopts them
and never waits for them, which is any process running as PID 1 of a container
(`agentic-hil mcp-stdio` as an entrypoint, or the container tier's pytest), it
stays, the group never reads empty, and the reap raises "Process group remained
active after SIGKILL" out of a teardown that had already ended everything. The
decision these tests hold is that a group whose remaining members are all
zombies is an emptied group, and that the product reaps the orphans it adopted
itself. The container tier meets the shape as PID 1 on purpose; here the same
shape is produced on Linux with `PR_SET_CHILD_SUBREAPER`, which makes this test
process the adoptive parent without being PID 1.

A bare name has two rules, decided by where the lookup happens. `executable:
openocd` (or `pyocd`, or `STM32_Programmer_CLI`) is resolved through PATH when
the configuration is loaded, and a PATH that lacks it is a load failure:
`config_invalid`, naming the field. An omitted executable is the entry `init`
writes before a toolchain is installed, and it must not stop the configuration
from loading; the debugger call refuses it with the `debugger_not_found`
refusal the absolute-path case has always carried. Every configuration the
suite wrote before this used an absolute fake path, so neither branch had been
taken, for any of the three backends.

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
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import FAKE_OPENOCD, FAKE_PYOCD, FAKE_STLINK, write_authoritative_config, write_config

from agentic_hil.config import ConfigError, debugger_is_placeholder, load_authoritative_config, load_config
from agentic_hil.tools import AgenticHILToolService

ROOT = Path(__file__).resolve().parents[1]
FAKE_HUNG_DEBUGGER = ROOT / "tests" / "fixtures" / "fake_openocd_hung.py"

# The deadline the hung fake is given, and the longest the whole call may take
# before the test says the deadline was not enforced. The fake sleeps for 30 s,
# so a call that only ends when the fake does ends well outside this. Nine
# hangs run in this module (three probes, one settle, two discoveries, three
# version checks), each costing the deadline plus the reap, so the deadline is
# the smallest number the contract can be measured against.
HANG_TIMEOUT_S = 1
CALL_CEILING_S = 15.0
# How long the reaped processes are given to disappear from the process table
# after the call returned: the backend's own reap waits up to CHILD_REAP_TIMEOUT_S.
REAP_CEILING_S = 5.0

WINDOWS = os.name == "nt"
LINUX = sys.platform.startswith("linux")

# What each backend calls its program when nothing but the name is written, the
# fake that answers to it, and the refusal it carries when nothing does.
BARE_NAME = {"openocd": "openocd", "pyocd": "pyocd", "stlink": "STM32_Programmer_CLI"}
FAKE_BY_TYPE = {"openocd": FAKE_OPENOCD, "pyocd": FAKE_PYOCD, "stlink": FAKE_STLINK}
NOT_FOUND_BACKEND_ERROR = {"openocd": "openocd_not_found", "pyocd": "pyocd_not_found", "stlink": "stm32_programmer_cli_not_found"}
NOT_FOUND_SUMMARY = {
    "openocd": "Debugger executable could not be found.",
    "pyocd": "pyOCD executable could not be found.",
    "stlink": "STM32CubeProgrammer CLI executable could not be found.",
}
# What names the board in each backend's entry, so the entry is an operator's
# and not the shipped starter that pinning deliberately leaves inert. The ids
# are the ones the fakes list.
BOARD_IDENTITY = {
    "openocd": {"probe_id": "ST-LINK-1"},
    "pyocd": {"probe_id": "PYOCD123", "target_type": "stm32f446retx"},
    "stlink": {"probe_id": "STLINK123"},
}
BACKENDS = ["openocd", "pyocd", "stlink"]


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


def authoritative_config_with_executable_spelling(workspace: Path, monkeypatch: pytest.MonkeyPatch, spelling: str, *, debugger_type: str = "openocd") -> Path:
    """The authoritative configuration of a bench, with `debuggers.dut.executable` written as `spelling`.

    `spelling` is the YAML value verbatim: the bare name for that spelling,
    `null` for the omitted executable. The written entry is the only line in
    the file whose key is `executable`; the GDB line is `gdb_executable` and is
    left as it is.

    Authoritative, because pinning is what `load_authoritative_config` does on
    the way to every production entry point and what `load_config` on a bare
    path never does. And a bench that names its board, because the entry with
    no executable, the skeleton's two script names and nothing naming a board
    is the shipped starter, which pinning deliberately leaves inert; the case
    #485 describes is an operator's entry.
    """
    path = write_authoritative_config(workspace, monkeypatch, debugger_type=debugger_type, **BOARD_IDENTITY[debugger_type])
    text = path.read_text(encoding="utf-8")
    rewritten, replaced = re.subn(r"(?m)^(\s+)executable: .*$", rf"\1executable: {spelling}", text)
    assert replaced == 1, text
    path.write_text(rewritten, encoding="utf-8")
    return path


def fake_on_path(directory: Path, monkeypatch: pytest.MonkeyPatch, debugger_type: str) -> Path:
    """A directory holding the backend's fake under the backend's bare name, on PATH.

    What the name resolves to is the platform's business. On POSIX the name is
    a shell wrapper that runs the fake through this interpreter, so the file
    PATH finds is a program the operating system executes directly, the way a
    packaged OpenOCD is. On Windows a bare name is found through PATHEXT, so the
    fake is placed as `<name>.py` and `.PY` is added to the extensions a lookup
    tries; `invocation` then runs it through this interpreter as it does every
    `.py` executable.

    PATH is replaced, not extended: the point of the test is which directory
    answered, and a developer's bench may have a real toolchain on PATH.
    """
    name = BARE_NAME[debugger_type]
    fake = FAKE_BY_TYPE[debugger_type]
    directory.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        launcher = directory / f"{name}.py"
        launcher.write_bytes(fake.read_bytes())
        monkeypatch.setenv("PATHEXT", ".PY;" + os.environ.get("PATHEXT", ".EXE;.BAT;.CMD"))
    else:
        launcher = directory / name
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake.as_posix()}" "$@"\n', encoding="utf-8")
        os.chmod(launcher, 0o755)
    monkeypatch.setenv("PATH", str(directory))
    return launcher


def bare_openocd_on_path(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return fake_on_path(directory, monkeypatch, "openocd")


def path_without_a_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host with no debugger anywhere a lookup would find one.

    PATH is emptied for every backend. STM32CubeProgrammer has a second place
    an omitted executable is looked for, the installer's usual directories
    under Program Files and C:/ST, and a developer's bench may have the real
    CLI there; the question these tests ask is what PATH answers, so that list
    is emptied too.
    """
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("agentic_hil.backends.common.common_stm32_programmer_paths", lambda: [])
    for name in BARE_NAME.values():
        assert shutil.which(name) is None, name


def path_without_openocd(monkeypatch: pytest.MonkeyPatch) -> None:
    path_without_a_toolchain(monkeypatch)


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


def call_with_a_hung_debugger(tmp_path: Path, tool: str, debugger_type: str) -> tuple[object, dict, float]:
    """One tool call against the hung fake: the configuration, the result and the seconds it took."""
    config = load_config(str(config_with_debugger(tmp_path, FAKE_HUNG_DEBUGGER, debugger_type=debugger_type)))
    service = AgenticHILToolService(config)
    started = time.monotonic()
    try:
        result = service.call(tool)
    finally:
        service.close()
    return config, result, time.monotonic() - started


@pytest.fixture
def pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    recorded = tmp_path / "hung-debugger.pids"
    monkeypatch.setenv("AGENTIC_HIL_TEST_PID_FILE", str(recorded))
    return recorded


# ---------------------------------------------------------------------------
# A hung tool is reaped and reported as a timeout.
#
# Green on the current code wherever the hosted matrix runs them, red as PID 1
# of a container for the zombie verdict the module docstring describes; the
# process rule itself is pinned on its own below. Mutation checks for the pins:
# a `communicate` without the timeout trips the 15 s ceiling, a reap that skips
# the tree kill leaves the fake's child in the pid file's survivors, and a log
# written without `timed_out` trips the log assertion.


@pytest.mark.parametrize("debugger_type", BACKENDS)
def test_a_hanging_debugger_is_reaped_and_answers_timeout(tmp_path: Path, pid_file: Path, debugger_type: str) -> None:
    """The whole of the timeout contract, driven through a process that hangs.

    The answer, the log, and the process table are three separate claims, and a
    reap that got two of them right is the one that leaves a debugger holding
    the probe open for the next call to find.
    """
    config, result, elapsed = call_with_a_hung_debugger(tmp_path, "probe_target", debugger_type)

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
    # than once, and every one of those processes has to be gone as well. The
    # survivors check is the contract; how many lines the file holds is not,
    # since the kill can land between the parent's line and the child's.
    pids = recorded_pids(pid_file)
    assert len(pids) >= 2, f"the fake did not record itself and its child: {pids}"
    assert wait_until_gone(pids, REAP_CEILING_S) == [], "the debugger or its child survived the reap"


def test_a_timed_out_probe_keeps_the_target_state_unconfirmed(tmp_path: Path, pid_file: Path) -> None:
    """The service-layer reading of a real timeout, not of a dict typed by hand.

    `test_quarantine_triggers` pins what the service does with a `timeout`
    result by substituting one; this is the same rule reached through a process
    that actually hung, so the two cannot drift apart without one of them going
    red.
    """
    _, result, _ = call_with_a_hung_debugger(tmp_path, "probe_target", "openocd")

    assert result["error_type"] == "timeout"
    assert result["quarantined"] is False
    assert result["cleanup_reasons"] == ["debugger_readonly_target_state_unconfirmed"]


@pytest.mark.parametrize("debugger_type", ["pyocd", "stlink"])
def test_a_hanging_probe_discovery_answers_its_own_timeout_sentence(tmp_path: Path, pid_file: Path, debugger_type: str) -> None:
    """Discovery never addresses a board, and its timeout says what timed out."""
    _, result, elapsed = call_with_a_hung_debugger(tmp_path, "debugger_probes_list", debugger_type)

    assert result["ok"] is False, json.dumps(result)
    assert result["error_type"] == "timeout"
    assert result["summary"] == "Debugger probe discovery timed out."
    assert result["target_contacted"] is False
    assert result["side_effect_status"] == "not_started"
    assert elapsed < CALL_CEILING_S, f"the call took {elapsed:.1f} s against a {HANG_TIMEOUT_S} s deadline"
    assert wait_until_gone(recorded_pids(pid_file), REAP_CEILING_S) == [], "the debugger or its child survived the reap"


@pytest.mark.parametrize("debugger_type", BACKENDS)
def test_a_hanging_version_check_answers_its_own_timeout_sentence(tmp_path: Path, pid_file: Path, debugger_type: str) -> None:
    """The check `doctor` runs first on a new bench, against a tool that never answers.

    `debugger_info` asks the program for its version under the entry's own
    deadline, so a hung tool is met on the first command a bench runs, and
    what the operator reads has to say that the version check timed out, not
    that a command did or that the tool is missing.
    """
    _, result, elapsed = call_with_a_hung_debugger(tmp_path, "debugger_info", debugger_type)

    assert result["ok"] is False, json.dumps(result)
    assert result["error_type"] == "timeout"
    assert result["summary"] == "Debugger version check timed out."
    assert result["backend"] == debugger_type
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
# The reap's last question, under a parent that adopts the orphans it leaves.


PR_SET_CHILD_SUBREAPER = 36
# What the reap has to finish inside once the group has really emptied: the
# reap's own waits are CHILD_REAP_TIMEOUT_S each, and the verdict this pins was
# reached only after two of them had run out.
EMPTIED_GROUP_CEILING_S = 4.0


@pytest.mark.skipif(not LINUX, reason="PR_SET_CHILD_SUBREAPER and the procfs state are Linux; the orphan shape needs both")
def test_a_group_whose_remaining_members_are_zombies_reads_as_emptied(tmp_path: Path) -> None:
    """The process rule behind every hang in the container tier, without PID 1.

    A shell with a `sleep` behind it is the two-deep tree every wrapper script
    around a debugger is. The group is signalled, both die, and the `sleep`,
    whose parent died in the same signal, is adopted by this process, which as a
    child subreaper stands where PID 1 of a container stands: it inherits the
    orphan and, unless the product collects it, never waits for it. The kernel
    keeps that zombie in its group, `killpg(pgid, 0)` keeps answering that the
    group has a member, and a reap that reads the group off that answer alone
    waits out both of its deadlines and then reports a tree it had already
    ended as still active. Measured before the fix: `RuntimeError: Process
    group remained active after SIGKILL.` after 10 s, with the orphan in state
    Z and nothing else of the tree left.

    What is pinned: the reap returns, it returns at once, and the orphan is
    collected rather than left as a zombie for the life of the server.
    """
    from agentic_hil.process import spawn_managed_process, terminate_process_tree

    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0, os.strerror(ctypes.get_errno())
    try:
        child = spawn_managed_process(["sh", "-c", "sleep 60 & echo $!; wait"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            assert child.stdout is not None
            grandchild = int(child.stdout.readline().strip())
            assert Path(f"/proc/{grandchild}").is_dir()

            started = time.monotonic()
            terminate_process_tree(child, 5.0)
            elapsed = time.monotonic() - started
        finally:
            if child.stdout is not None:
                child.stdout.close()
    finally:
        libc.prctl(PR_SET_CHILD_SUBREAPER, 0, 0, 0, 0)

    assert elapsed < EMPTIED_GROUP_CEILING_S, f"the reap waited {elapsed:.1f} s on a group that held nothing but a zombie"
    assert child.poll() is not None
    assert not Path(f"/proc/{grandchild}").exists(), "the orphan the reap left behind was never collected"


# ---------------------------------------------------------------------------
# A bare name resolves through PATH at load; an omitted one is looked up for the call.
#
# Green pins on the current code. Mutation checks: pinning that keeps the bare
# name instead of what PATH answers trips `is_absolute` and `samefile`; a load
# that tolerates an unresolvable bare name trips the `ConfigError` expectation;
# an omitted executable that autodetection no longer fills trips `samefile`; a
# call that reports a placeholder as anything but `debugger_not_found` trips the
# refusal assertions.


@pytest.mark.parametrize("debugger_type", BACKENDS)
def test_a_bare_name_resolves_through_path_at_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debugger_type: str) -> None:
    """`executable: openocd`, the common Linux spelling, pins the program PATH names; the same for each backend's name."""
    workspace = tmp_path / "workspace"
    launcher = fake_on_path(tmp_path / "on-path", monkeypatch, debugger_type)
    authoritative_config_with_executable_spelling(workspace, monkeypatch, BARE_NAME[debugger_type], debugger_type=debugger_type)

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


@pytest.mark.parametrize("debugger_type", BACKENDS)
def test_a_bare_name_that_path_lacks_is_refused_when_the_configuration_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debugger_type: str) -> None:
    """A name that resolves to nothing is a configuration that names nothing."""
    workspace = tmp_path / "workspace"
    authoritative_config_with_executable_spelling(workspace, monkeypatch, BARE_NAME[debugger_type], debugger_type=debugger_type)
    path_without_a_toolchain(monkeypatch)

    with pytest.raises(ConfigError) as raised:
        load_authoritative_config(workspace)

    assert raised.value.error_type == "config_invalid"
    assert raised.value.summary == "Configured executable could not be resolved at startup."
    assert raised.value.details["field"] == "debuggers.dut.executable"
    assert raised.value.details["value"] == BARE_NAME[debugger_type]


@pytest.mark.parametrize("debugger_type", BACKENDS)
def test_an_omitted_executable_is_looked_up_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debugger_type: str) -> None:
    """`executable: null` runs the program PATH names, without anybody writing the path.

    The outcome is the issue's: the program on PATH is what runs. Where the
    lookup happens is the code's: pinning autodetects the name when the
    configuration loads, which is why the pinned entry already carries the
    launcher here. #485 words the omitted case as a lookup "at call time"; an
    implementation that moved the lookup there would change this test's second
    assertion and nothing the operator sees.
    """
    workspace = tmp_path / "workspace"
    launcher = fake_on_path(tmp_path / "on-path", monkeypatch, debugger_type)
    authoritative_config_with_executable_spelling(workspace, monkeypatch, "null", debugger_type=debugger_type)

    config = load_authoritative_config(workspace)
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is True, json.dumps(result)
    assert Path(config.debugger.executable).samefile(launcher)


@pytest.mark.parametrize("debugger_type", BACKENDS)
def test_an_omitted_executable_that_path_lacks_loads_and_refuses_the_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debugger_type: str) -> None:
    """A bench whose toolchain is not installed yet: the file loads, the call refuses.

    The refusal is the one the absolute-path case has always carried, so a
    caller that reads `debugger_not_found` reads the same next step whichever
    way the executable was left unresolved.
    """
    workspace = tmp_path / "workspace"
    authoritative_config_with_executable_spelling(workspace, monkeypatch, "null", debugger_type=debugger_type)
    path_without_a_toolchain(monkeypatch)

    config = load_authoritative_config(workspace)
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False, json.dumps(result)
    assert result["error_type"] == "debugger_not_found"
    assert result["backend_error_type"] == NOT_FOUND_BACKEND_ERROR[debugger_type]
    assert result["summary"] == NOT_FOUND_SUMMARY[debugger_type]
    assert result["target_contacted"] is False


def test_an_absolute_path_is_pinned_without_consulting_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour every other test in the suite relies on: an absolute path is not looked up."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, probe_id="ST-LINK-1")
    path_without_a_toolchain(monkeypatch)

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
