from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from io import StringIO
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
import yaml
from conftest import FAKE_OPENOCD, write_authoritative_config, write_config

from agentic_hil.artifacts import ArtifactManager
from agentic_hil.backends.common import spawn_command
from agentic_hil.backends.gdbdebug import GdbDebugSessions
from agentic_hil.bridge import BridgeCleanupError, ProcessBridgeSession
from agentic_hil.can import CAN_DRAIN_TIMEOUT_S, CanBusService, CanBusSession, parse_can_id, payload_frame
from agentic_hil.cli import debugger_probes, doctor, init_config
from agentic_hil.comports import ComPortService, ComPortSession
from agentic_hil.comstdio import run_com_stdio
from agentic_hil.config import (
    _PATH_LOCKS,
    OPENOCD_SCRIPT_SEARCH_NAME,
    ConfigError,
    _close_windows_handles,
    _windows_hold_directory_chain,
    config_schema_text,
    debugger_drives_hardware,
    debugger_is_placeholder,
    debugger_is_starter_entry,
    derived_state_directory,
    executable_is_disabled,
    load_authoritative_config,
    load_config,
    openocd_script_kind,
    project_config_path,
    project_state_directory,
    temporary_roots,
)
from agentic_hil.contracts import validate_tool_arguments
from agentic_hil.devices import uart_device
from agentic_hil.gdbmi import GdbMiClient
from agentic_hil.knowledge import CONFIG_WORKED_EXAMPLE
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.report import (
    ContactMarker,
    append_canonical_audit_log,
    append_jsonl,
    append_jsonl_audited,
    attach_canonical_audit_evidence,
    canonical_audit_evidence,
    canonical_audit_log_path,
    ensure_audit_ready,
    logs_directory,
    overall_success,
    read_last_failure,
    read_last_report,
    report_lock_path,
    report_state_path,
    write_report,
)
from agentic_hil.stdio import run_stdio_server
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import CanBusConfig, DebuggerConfig
from tests.test_virtualized_user_paths import virtualize

WAIT_TIMEOUT_S = 5.0
POLL_INTERVAL_S = 0.01

COM_PORT_YAML = 'com_ports:\n  dut:\n    device: "/dev/ttyAGENTIC_HILTEST"\n'
CAN_BUS_YAML = 'can_buses:\n  bench:\n    adapter: "process"\n    channel: "vcan0"\n    executable: "python"\n'


def load_test_config(tmp_path: Path, **kwargs):
    return load_config(str(write_config(tmp_path, **kwargs)))


def wait_until(predicate, timeout_s: float = WAIT_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL_INTERVAL_S)
    return predicate()


def test_state_root_is_pinned_and_outside_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    original = project_state_directory(config)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "changed"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "changed"))

    assert project_state_directory(config) == original
    assert not original.is_relative_to(tmp_path)


def test_state_root_inside_workspace_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, state_root=tmp_path / "state")
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(path))
    assert excinfo.value.details["field"] == "state_root"


def test_relative_state_root_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^state_root:.*$", "state_root: relative-state", text, flags=re.MULTILINE)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(path))
    assert excinfo.value.details["field"] == "state_root"


# --- What a configured path is still checked for, and what it is not --------
#
# The Windows ACL walk and the POSIX mode/sticky-bit walk are gone:
# they only ever defended against a different account on the same machine, which
# was never a requirement here, and they could not defend against the operator's
# own processes, which own these objects. What survives is structural — a path
# must be what it claims to be — plus detection, which is the digest.


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode semantics")
def test_a_group_writable_state_root_is_no_longer_a_refusal(tmp_path: Path) -> None:
    """The measured cost of the removed rule, from the other side.

    A umask-002 / private-group home produced a `unsafe_configured_path` refusal
    on a directory the operator owns and can rewrite regardless — 509 failures in
    one suite run on one developer machine, and `agentic-hil init` refusing in an
    empty directory. The mode says nothing this project needs, so it is not read.
    """
    path = write_config(tmp_path)
    root = Path(load_config(str(path)).state_root)
    root.chmod(0o777)
    try:
        assert Path(load_config(str(path)).state_root) == root
        assert derived_state_directory(root, "coordination").is_dir()
    finally:
        root.chmod(0o700)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL semantics")
def test_a_broadly_writable_windows_state_root_is_no_longer_a_refusal(tmp_path: Path) -> None:
    """The same on Windows, against a real ACL rather than a synthetic finding.

    Everyone (S-1-1-0) with Modify is the sharpest case the check ever refused,
    and it is now accepted for the reason the check could never answer: the
    operator holds FullControl on this directory either way.
    """
    path = write_config(tmp_path)
    root = Path(load_config(str(path)).state_root)
    grant = subprocess.run(["icacls", str(root), "/grant", "*S-1-1-0:(OI)(CI)M"], capture_output=True, text=True, check=False)
    if grant.returncode != 0:
        pytest.skip(f"could not set temporary test ACL: {grant.stderr}")
    try:
        assert Path(load_config(str(path)).state_root) == root
        assert derived_state_directory(root, "coordination").is_dir()
    finally:
        subprocess.run(["icacls", str(root), "/remove:g", "*S-1-1-0"], capture_output=True, check=False)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics; Windows refuses the same chain by handle")
def test_a_symlinked_state_root_component_is_still_refused(tmp_path: Path) -> None:
    """What the path check is still about: the object, not its ACL.

    A symlinked component means the directory that gets written is not the one
    the configuration names, and that is decidable from the path itself on every
    platform. Removing the trust walk must not take this with it.
    """
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ConfigError) as refused:
        derived_state_directory(link, "coordination")

    assert refused.value.error_type == "unsafe_configured_path"


def test_state_root_validation_leaves_no_probe_entry_behind(tmp_path: Path) -> None:
    """Writability is established by creating and removing a private entry, so
    the directory a normal load leaves behind has to be exactly as clean as
    before. The name is checked too: a probe a killed process left is
    recognisable as this and not as project state."""
    from agentic_hil.config import WRITE_PROBE_PREFIX

    path = write_config(tmp_path)
    root = Path(load_config(str(path)).state_root)

    assert root.is_dir()
    assert [entry.name for entry in root.iterdir() if entry.name.startswith(WRITE_PROBE_PREFIX)] == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode semantics; the Windows case is the deny-ACE test below")
@pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root ignores the mode bits this test sets")
def test_an_existing_unwritable_state_root_is_refused_at_load(tmp_path: Path) -> None:
    """The half of `state_root` validation the trust walk's removal left open.

    `safe_directory` opens read-only and suppresses `FileExistsError`, so an
    existing directory that cannot be written loaded clean and failed at the
    first lease, report or log write — somewhere with far less context than
    here. This is function, not trust: it asks whether *this* process can create
    in the directory, not who else could."""
    path = write_config(tmp_path)
    root = Path(load_config(str(path)).state_root)
    root.chmod(0o500)
    try:
        with pytest.raises(ConfigError) as refused:
            load_config(str(path))
        assert refused.value.error_type == "unsafe_configured_path"
        assert refused.value.details["field"] == "state_root"
    finally:
        root.chmod(0o700)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL semantics")
def test_an_existing_undeniably_unwritable_windows_state_root_is_refused_at_load(tmp_path: Path) -> None:
    """The same on Windows, against a real deny ACE rather than a mode.

    A POSIX mode is not what Windows answers with, and neither is the ACL the
    removed walk used to read: what decides this is whether the create succeeds,
    which is what the probe asks."""
    path = write_config(tmp_path)
    root = Path(load_config(str(path)).state_root)
    # Only "add subdirectory". A broad write deny also stops the no-symlink walk
    # from opening the chain at all, which is a different refusal on a different
    # line; this leaves listing and traversal intact so the create is what fails.
    denied = subprocess.run(["icacls", str(root), "/deny", "*S-1-1-0:(OI)(CI)(AD)"], capture_output=True, text=True, check=False)
    if denied.returncode != 0:
        pytest.skip(f"could not set temporary test ACL: {denied.stderr}")
    try:
        try:
            (root / "probe-check").mkdir()
        except OSError:
            pass
        else:
            (root / "probe-check").rmdir()
            pytest.skip("the deny ACE did not take effect on this host")
        with pytest.raises(ConfigError) as refused:
            load_config(str(path))
        assert refused.value.error_type == "unsafe_configured_path"
        assert refused.value.details["field"] == "state_root"
    finally:
        subprocess.run(["icacls", str(root), "/remove:d", "*S-1-1-0"], capture_output=True, check=False)


def test_a_configuration_that_still_carries_the_removed_key_is_refused_by_name(tmp_path: Path) -> None:
    """Silently ignoring it would leave the key looking like it still selects a policy.

    0.8.0 was never released, so no file in the field carries `windows_path_trust`
    — which is why removing it cost no migration. A file that does carry it was
    written against a pre-release, and the refusal has to say what went and what
    took its place rather than report an unknown field.
    """
    path = write_config(tmp_path)
    path.write_text("windows_path_trust: permissive\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(ConfigError) as refused:
        load_config(str(path))

    details = refused.value.to_dict()
    assert details["error_type"] == "config_invalid"
    assert details["field"] == "windows_path_trust"
    assert "removed" in details["migration"]
    assert "config_status" in json.dumps(details["migration"]), "the refusal names what replaced it"


def test_the_loaded_configuration_carries_no_trust_mode(tmp_path: Path) -> None:
    """The key is gone from the object too, not only from the parser.

    A field left on `AgenticHILConfig` with nothing reading it is how a removed
    policy comes back as a default nobody chose.
    """
    config = load_config(str(write_config(tmp_path)))

    assert not hasattr(config, "windows_path_trust")
    assert "windows_path_trust" not in json.loads(config_schema_text())["properties"]


def test_the_suite_does_not_divert_its_own_scaffolding_into_the_real_profile(isolated_config_environment: Path) -> None:
    """Where the suite writes, which is not the operator's own configuration.

    `tests/conftest.py` and `tests/support.py` once diverted into the real Local
    AppData because the removed trust check refused the standard per-user Temp —
    the strongest single argument against that check was that the tool's own tests
    had to evade its rule. The launcher stays under the real home, because the
    MCP executable check still walks its parent chain.

    The sandbox itself sits in the system temp root and not beside `tmp_path`,
    which is the one thing `--basetemp` can move: pointed inside this clone, it
    used to put the isolated HOME under the working directory, and a home inside
    a project is what `agent-install` refuses. Where the suite writes is the
    suite's decision, not the command line's.
    """
    from conftest import ROOT, SANDBOX_ROOT
    from support import LAUNCHER_ROOT, REAL_HOME

    assert isolated_config_environment.parent.parent == SANDBOX_ROOT
    assert ROOT not in isolated_config_environment.parents
    assert LAUNCHER_ROOT.parent == REAL_HOME


def test_each_test_gets_temporary_storage_of_its_own(isolated_config_environment: Path) -> None:
    """The one directory every suite on this machine would otherwise share.

    Redirecting HOME is not isolation while temporary storage stays machine-wide.
    `tempfile` answers out of TMPDIR/TEMP/TMP, so a tool that asks the system for
    scratch space gets a directory two parallel suites both reach: the review
    loop's scratch root is named after the repository and the current second, and
    two runs that started together deleted each other's rounds out of it.

    Both halves are checked, because they fail apart: the variables are what a
    child process reads, and the module attribute is what this process reads,
    since `gettempdir` caches its first answer.
    """
    temporary = isolated_config_environment.parent / "tmp"

    assert [os.environ[name] for name in ("TMPDIR", "TEMP", "TMP")] == [str(temporary)] * 3
    assert Path(tempfile.gettempdir()) == temporary
    assert Path(tempfile.mkdtemp()).parent == temporary


def test_a_sandbox_root_is_swept_only_once_nobody_is_using_it() -> None:
    """The residue nothing else collects, and the suite beside it that must survive.

    A per-test sandbox Windows would not delete, because a detached child still
    held a lock file in it, outlives both finalizers, and the system temp root
    has no garbage collection of its own. Age is what tells residue from a live
    suite: a running session creates and removes a sandbox inside its root on
    every test, so only a root nothing has written to for hours is free.

    The two roots this plants carry the pid, because the directory they are
    planted in is the one place in this suite that is genuinely shared: the sweep
    is about the whole temp root, so it has to be asked about the whole temp
    root, and fixed names would have meant two suites on one machine -- an xdist
    worker and a second clone's run -- planting, ageing and deleting each other's
    fixtures. Every assertion below names one of this process's own entries, so
    what a neighbour leaves in `removed` is none of this test's business.
    """
    from conftest import SANDBOX_PREFIX, SANDBOX_ROOT, STALE_SANDBOX_AGE_S, sweep_stale_sandbox_roots

    stale = SANDBOX_ROOT.parent / f"{SANDBOX_PREFIX}test-stale-{os.getpid()}"
    fresh = SANDBOX_ROOT.parent / f"{SANDBOX_PREFIX}test-fresh-{os.getpid()}"
    for root in (stale, fresh):
        (root / "held").mkdir(parents=True, exist_ok=True)
    os.utime(stale, (time.time() - STALE_SANDBOX_AGE_S - 60,) * 2)
    try:
        removed = sweep_stale_sandbox_roots()

        assert stale in removed and not stale.exists()
        assert fresh not in removed and fresh.is_dir()
        # This session's own root is never a candidate, however long it has run.
        assert SANDBOX_ROOT not in removed
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
        shutil.rmtree(stale, ignore_errors=True)


def test_a_test_that_undoes_its_own_monkeypatch_keeps_the_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one line that once aimed the whole suite at the operator's profile.

    `monkeypatch.undo()` reverts everything recorded on the test's own instance,
    and the HOME redirect used to be recorded there: a scratch test called it
    mid-run, the three CLI calls that followed resolved their user-level paths
    out of the real environment, and the `uninstall` among them removed all three
    installed agent skills and their MCP registrations from a live machine. The
    fixture keeps an instance of its own now, so a test's undo has nothing here
    to reach.

    That this test is reported as passing rather than as an escape is the other
    half of the proof: the guard wrapped around every test body asks the same
    question after this one returns.
    """
    from conftest import SANDBOX_ROOT
    from support import REAL_HOME

    monkeypatch.undo()

    assert Path.home() != REAL_HOME
    assert SANDBOX_ROOT in Path.home().parents


def test_a_test_pointed_back_at_the_real_home_is_failed_by_name(request: pytest.FixtureRequest) -> None:
    """What the guard says when a redirect is gone, and about which test.

    A suite that quietly writes into the real profile is worse than a suite that
    fails, so the failure has to name the test that arrived there: whoever reads
    it hours later has nothing else to go on. Reverting HOME is enough to reach
    `~/.claude.json` and the two skill directories beside it, and both variables
    are set because `Path.home()` reads USERPROFILE on Windows and HOME
    everywhere else.

    The redirect is put back inside the test body, not at teardown, because the
    guard checks this test too: a `monkeypatch` fixture would restore it after
    the check had already run and this test would report the escape it is here to
    describe.
    """
    from conftest import SandboxEscaped, assert_still_sandboxed, pytest_runtest_call
    from support import REAL_HOME

    with pytest.MonkeyPatch.context() as reverted:
        reverted.setenv("HOME", str(REAL_HOME))
        reverted.setenv("USERPROFILE", str(REAL_HOME))

        with pytest.raises(SandboxEscaped) as refused:
            next(pytest_runtest_call(request.node))

    assert request.node.nodeid in str(refused.value)
    assert str(REAL_HOME) in str(refused.value)
    assert "uninstalls from" in str(refused.value), "the message says what is at stake, not that an assertion failed"
    assert_still_sandboxed("with the redirect back in place")


def test_the_guard_watches_the_configuration_root_and_not_only_the_home() -> None:
    """HOME is not the only variable that decides where user-level files land.

    The authoritative configurations `agentic-hil init` writes, and the state
    root under them, are read out of `%APPDATA%` and `%LOCALAPPDATA%` on Windows
    and out of `$XDG_CONFIG_HOME` and `$XDG_STATE_HOME` on POSIX. A redirect that
    lost one of those while HOME stayed sandboxed would leave every home-shaped
    check looking correct while a project's policy file landed in the operator's
    own configuration directory.

    The value written here is the fallback the tool derives from the real home
    rather than whatever this machine's variable happens to say, because that
    fallback is a real user path on any profile and is therefore the one spelling
    this assertion can make on every machine the suite runs on.
    """
    from conftest import SandboxEscaped, assert_still_sandboxed
    from support import REAL_HOME

    variable, real_root = ("APPDATA", REAL_HOME / "AppData" / "Roaming") if os.name == "nt" else ("XDG_CONFIG_HOME", REAL_HOME / ".config")

    with pytest.MonkeyPatch.context() as reverted:
        reverted.setenv(variable, str(real_root))

        with pytest.raises(SandboxEscaped) as refused:
            assert_still_sandboxed("in this test")

    assert str(real_root / "agentic-hil") in str(refused.value)


def test_stdio_rejects_oversized_message_and_keeps_serving(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    oversized = '{"jsonrpc": "2.0", "id": 1, "method": "ping", "pad": "' + "x" * 5000 + '"}'
    ping = '{"jsonrpc": "2.0", "id": 2, "method": "ping"}'
    output = StringIO()

    exit_code = run_stdio_server(
        config,
        input_stream=StringIO(oversized + "\n" + ping + "\n"),
        output_stream=output,
        max_message_chars=1000,
    )

    assert exit_code == 0
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(responses) == 2
    assert responses[0]["error"]["code"] == -32600
    assert responses[1]["id"] == 2
    assert "result" in responses[1]


def test_empty_jsonrpc_batch_returns_invalid_request(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_test_config(tmp_path))

    response = handle_mcp_message([], service)

    assert isinstance(response, dict)
    assert response["error"]["code"] == -32600


class FailingSerialHandle:
    """Serial handle whose reads fail like an unplugged device; is_open stays True (pyserial behavior)."""

    is_open = True
    in_waiting = 0

    def read(self, size: int) -> bytes:
        raise OSError("device disconnected")

    def close(self) -> None:
        pass


class RetryCloseSerialHandle:
    is_open = True
    in_waiting = 0

    def __init__(self) -> None:
        self.close_attempts = 0

    def read(self, size: int) -> bytes:
        return b""

    def close(self) -> None:
        self.close_attempts += 1
        if self.close_attempts == 1:
            raise OSError("port busy during close")
        self.is_open = False


class FailingCancelSerialHandle:
    is_open = True
    in_waiting = 0

    def __init__(self) -> None:
        self.close_attempts = 0

    def read(self, size: int) -> bytes:
        return b""

    def cancel_read(self) -> None:
        raise OSError("cancel failed")

    def close(self) -> None:
        self.close_attempts += 1
        self.is_open = False


def test_com_session_with_dead_reader_reports_not_active(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    log_path = tmp_path / ".agentic-hil" / "logs" / "test-com.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    session = ComPortSession("dut", config.com_ports["dut"], FailingSerialHandle(), str(log_path))
    service.sessions["dut"] = session

    assert wait_until(lambda: session.reader_error is not None), "reader thread never recorded its error"

    result = service.read_bytes("dut", 16, 0.0)
    assert result["ok"] is False
    assert result["error_type"] == "session_not_active"
    assert result["reader_error"]["error_type"] == "serial_read_failed"
    assert service._session_is_active(session) is False


class ShortThenCompleteSerialHandle:
    """First write() call confirms fewer bytes than requested, pyserial's own
    documented behavior for a short write under a finite write_timeout,
    and the second call, made against exactly the remainder, finishes it."""

    is_open = True
    in_waiting = 0

    def __init__(self, short_by: int = 3) -> None:
        self.short_by = short_by
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        if len(self.writes) == 1:
            return max(0, len(data) - self.short_by)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


class ChronicallyShortSerialHandle:
    """Confirms only a few bytes per call, however many are asked for: a link
    that genuinely cannot keep up, as opposed to a one-off short return a
    retry recovers from."""

    is_open = True
    in_waiting = 0

    def __init__(self, chunk: int = 2) -> None:
        self.chunk = chunk
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return min(self.chunk, len(data))

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


def test_com_write_retries_a_short_write_and_completes(tmp_path: Path) -> None:
    """pyserial's return value is read now, not discarded: a first call that
    confirms fewer bytes than asked for is retried against exactly its own
    remainder, and a write that recovers within the retry budget reports the
    honest, complete count.

    This fails before the fix in the one way that matters here: the old code
    called ``write()`` exactly once and never looked at what it returned, so
    ``handle.writes`` would hold a single, full-length entry instead of the
    short first attempt followed by its remainder.
    """
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    log_path = tmp_path / ".agentic-hil" / "logs" / "test-com-short-write-recovers.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = ShortThenCompleteSerialHandle(short_by=3)
    session = ComPortSession("dut", config.com_ports["dut"], handle, str(log_path), start_reader=False)
    service.sessions["dut"] = session

    result = service.write_bytes("dut", b"12345678")

    assert result["ok"] is True, result
    assert result["bytes_written"] == 8
    assert result["data"]["text"] == "12345678"
    # The short first return was retried against exactly its own remainder,
    # not resent from the top.
    assert handle.writes == [b"12345678", b"678"]


def test_com_write_that_stays_short_after_retry_records_event_without_quarantine(tmp_path: Path) -> None:
    """The bug this pins: the write path used to discard pyserial's return
    value and report ``bytes_written = len(data)`` regardless, so a write a
    finite ``write_timeout_s`` cut short still came back ``ok: true`` and
    claiming a whole write. A write that is still short after the bounded
    retry now fails honestly with the confirmed count, and, because the
    count is confirmed rather than unknown, the lease is treated the way
    quarantine narrowing (#216) treats a peripheral failure the next call
    proves nothing more about: the reason is recorded and the device goes
    back, rather than a standing quarantine an operator has to clear.
    """
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    log_path = tmp_path / ".agentic-hil" / "logs" / "test-com-short-write-exhausted.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = ChronicallyShortSerialHandle(chunk=2)
    lease = service.coordinator.acquire(uart_device(config, "dut"))
    session = ComPortSession("dut", config.com_ports["dut"], handle, str(log_path), lease, start_reader=False)
    service.sessions["dut"] = session

    result = service.write_bytes("dut", b"0123456789")

    assert result["ok"] is False, result
    assert result["error_type"] == "serial_write_incomplete"
    assert result["bytes_written"] == 8
    assert result["bytes_requested"] == 10
    assert result["data"]["text"] == "01234567"
    # Bounded: the initial attempt plus SHORT_WRITE_RETRY_LIMIT retries, and
    # not one call more even though the handle never stops confirming
    # progress.
    assert len(handle.writes) == 4
    # Confirmed, not unknown, so this is recorded rather than quarantined:
    # the lease is not held for it and the bench is not blocked.
    assert session.lease.state == "active"
    assert session.lease.cleanup_reasons() == []
    assert session.lease.reported_cleanup_reasons() == ["serial_write_incomplete"]
    assert service.coordinator.blocked is False


class DyingMidReadSerialHandle:
    """Answers a few empty reads, proving the session was genuinely alive
    when the wait started, then fails like a device unplugged while
    `com_read` was still waiting on it."""

    is_open = True
    in_waiting = 0

    def __init__(self, healthy_reads: int = 5) -> None:
        self.healthy_reads = healthy_reads
        self.calls = 0

    def read(self, size: int) -> bytes:
        self.calls += 1
        if self.calls > self.healthy_reads:
            raise OSError("device disconnected mid-read")
        return b""

    def close(self) -> None:
        self.is_open = False


class DataThenDiesSerialHandle:
    """Delivers one chunk of real data, then fails like a device that was
    unplugged right after: used to prove a read that got real bytes stays a
    success even though the session goes on to record a reader_error."""

    is_open = True
    in_waiting = 0

    def __init__(self) -> None:
        self.calls = 0

    def read(self, size: int) -> bytes:
        self.calls += 1
        if self.calls == 1:
            return b"ok\n"
        raise OSError("device disconnected after delivering data")

    def close(self) -> None:
        self.is_open = False


def test_com_read_reports_failure_when_reader_dies_mid_wait(tmp_path: Path) -> None:
    """The bug this pins: a reader that dies while `com_read` is inside its
    wait loop used to exit that loop into the same result as no feedback at
    all, ``{"ok": true, ..., "summary": "No COM port feedback was
    available."}``, with the reader's own error present only as a nested
    ``reader_error`` that ``overall_success`` never inspects. A reader that
    died partway through the wait is a failed read, not an empty successful
    one, and it now refuses the same way a read against an already-dead
    session does.
    """
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    log_path = tmp_path / ".agentic-hil" / "logs" / "test-com-mid-wait-death.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = DyingMidReadSerialHandle(healthy_reads=5)
    session = ComPortSession("dut", config.com_ports["dut"], handle, str(log_path))
    service.sessions["dut"] = session

    # The reader must still be alive when the call is made, or this would
    # only exercise the pre-existing pre-check in `_active_session` (see
    # test_com_session_with_dead_reader_reports_not_active above) instead of
    # the wait loop this test targets.
    assert session.reader_error is None

    result = service.read_bytes("dut", 16, 2.0)

    assert result["ok"] is False, result
    assert overall_success(result) is False
    assert result["error_type"] == "session_not_active"
    assert result["reader_error"]["error_type"] == "serial_read_failed"
    assert result["summary"] != "No COM port feedback was available."
    assert wait_until(lambda: session.reader_error is not None)


def test_com_read_keeps_real_data_a_success_even_if_the_reader_later_dies(tmp_path: Path) -> None:
    """The fix is scoped to an empty read: a call that did retrieve real
    bytes before the reader went on to fail stays a success, with the
    reader's error still nested for the next call to see."""
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    log_path = tmp_path / ".agentic-hil" / "logs" / "test-com-data-then-dies.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = DataThenDiesSerialHandle()
    session = ComPortSession("dut", config.com_ports["dut"], handle, str(log_path))
    service.sessions["dut"] = session

    result = service.read_bytes("dut", 16, 2.0)

    assert result["ok"] is True, result
    assert overall_success(result) is True
    assert result["bytes_read"] == 3
    assert result["data"]["text"] == "ok\n"


def test_com_session_stop_retains_session_when_close_fails_for_retry(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    log_path = tmp_path / ".agentic-hil" / "logs" / "test-com-close.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = RetryCloseSerialHandle()
    session = ComPortSession("dut", config.com_ports["dut"], handle, str(log_path))
    service.sessions["dut"] = session

    first = service.session_stop("dut")
    second = service.session_stop("dut")

    assert first["ok"] is False
    assert first["error_type"] == "com_port_close_failed"
    assert second["ok"] is True
    assert handle.close_attempts == 2
    assert "dut" not in service.sessions


def test_com_session_stop_attempts_close_after_cancel_failure(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    log_path = tmp_path / ".agentic-hil" / "logs" / "test-com-cancel.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = FailingCancelSerialHandle()
    session = ComPortSession("dut", config.com_ports["dut"], handle, str(log_path), start_reader=False)
    service.sessions["dut"] = session

    result = service.session_stop("dut")

    assert result["error_type"] == "com_port_close_failed"
    # The stop failed and the failure is the answer. It records the reason,
    # gives the port back, and holds nothing: a handle that is really stuck is
    # refused by the operating system at the next open, and one that is not
    # simply opens, which is the proof this reason was waiting for.
    assert service.sessions["dut"].lease.state == "active"
    assert handle.close_attempts == 1


def test_com_open_failure_without_cleanup_confirmation_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An open that got far enough to leave something behind still quarantines.

    The stub says so the way the backend does: it marks contact as unproven,
    which is what an open that may have left a handle on the device records. A
    stub that touched the marker not at all would be describing an open that
    never happened, and would rightly be refused instead."""
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)

    def partial_open(port_id, port_config, log_path, lease, contact):
        contact.record_unproven("test_partial_open")
        return {"ok": False, "tool": "com_session_start", "port_id": "dut", "error_type": "com_port_open_failed", "summary": "partial open unknown"}

    monkeypatch.setattr(service, "_open_serial", partial_open)
    try:
        result = service.session_start("dut")

        # Recorded and released rather than held: the open failed and its own
        # teardown could not be confirmed, which the next open settles either
        # way. The reason and its remediation still travel with the refusal.
        assert result["cleanup_reasons"] == ["com_open_cleanup_unconfirmed"]
        assert result["lease_state"] == "released"
        assert service.coordinator.blocked is False
    finally:
        service.close()


def test_com_open_failure_with_confirmed_cleanup_releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    monkeypatch.setattr(service, "_open_serial", lambda *args: {"ok": False, "tool": "com_session_start", "port_id": "dut", "error_type": "com_port_open_failed", "summary": "open rolled back", "cleanup_confirmed": True})

    result = service.session_start("dut")

    assert result["cleanup_confirmed"] is True
    assert result["lease_state"] == "released"
    assert result.get("cleanup_required") is not True


def test_com_reader_start_failure_reports_before_lease_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    handle = SimpleNamespace(is_open=True, in_waiting=0, close=lambda: setattr(handle, "is_open", False))

    def opened(port_id, port_config, log_path, lease, contact):
        session = ComPortSession(port_id, port_config, handle, log_path, lease, start_reader=False, contact=contact)
        session.start_reader = lambda: (_ for _ in ()).throw(RuntimeError("thread failed"))
        return {"ok": True, "session": session}

    monkeypatch.setattr(service, "_open_serial", opened)
    result = service.session_start("dut")

    assert result["error_type"] == "com_reader_start_failed"
    assert result["lease_state"] == "released"
    # The persisted report must prove the SAME terminal lease_state it returned;
    # the action-time snapshot is no longer left behind under report_path.
    assert read_last_report(config)["lease_state"] == "released"
    assert result["report_path"] == read_last_report(config)["report_path"]
    assert service.sessions == {}


def test_com_reader_started_then_start_raises_is_joined_before_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)

    class BlockingHandle:
        is_open = True
        in_waiting = 0

        def __init__(self) -> None:
            self.closed = threading.Event()

        def read(self, size: int) -> bytes:
            self.closed.wait()
            return b""

        def close(self) -> None:
            self.is_open = False
            self.closed.set()

    handle = BlockingHandle()
    captured: list[ComPortSession] = []

    def opened(port_id, port_config, log_path, lease, contact):
        session = ComPortSession(port_id, port_config, handle, log_path, lease, start_reader=False, contact=contact)
        captured.append(session)
        return {"ok": True, "session": session}

    original_start = threading.Thread.start

    def start_then_raise(thread: threading.Thread) -> None:
        original_start(thread)
        raise RuntimeError("start failed after launch")

    monkeypatch.setattr(service, "_open_serial", opened)
    monkeypatch.setattr(threading.Thread, "start", start_then_raise)

    result = service.session_start("dut")

    assert result["error_type"] == "com_reader_start_failed"
    assert result["lease_state"] == "released"
    assert captured[0].reader is not None
    assert not captured[0].reader.is_alive()
    assert handle.is_open is False


def test_new_com_session_clears_os_and_memory_buffers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    reset_calls = 0

    class BufferedHandle:
        is_open = True

        def reset_input_buffer(self) -> None:
            nonlocal reset_calls
            reset_calls += 1

        def close(self) -> None:
            self.is_open = False

    def opened(port_id, port_config, log_path, lease, contact):
        session = ComPortSession(port_id, port_config, BufferedHandle(), log_path, lease, start_reader=False, contact=contact)
        session.buffer.extend(b"stale")
        session.overflow_bytes = 3
        session.start_reader = lambda: None
        return {"ok": True, "session": session}

    monkeypatch.setattr(service, "_open_serial", opened)
    try:
        result = service.session_start("dut", clear_buffer=True)

        assert result["ok"] is True
        assert reset_calls == 1
        assert service.sessions["dut"].buffer == b""
        assert service.sessions["dut"].overflow_bytes == 0
    finally:
        service.close()


def test_com_session_constructor_failure_closes_raw_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    # open() is a no-op here: the port is now configured before it is opened,
    # so the modem lines are decided rather than left to pyserial's defaults.
    handle = SimpleNamespace(closed=False, close=lambda: setattr(handle, "closed", True), open=lambda: None)
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: handle))
    monkeypatch.setattr("agentic_hil.comports.ComPortSession", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("construct failed")))

    result = service._open_serial("dut", config.com_ports["dut"], str(tmp_path / "com.jsonl"), SimpleNamespace(), ContactMarker())

    assert result["ok"] is False
    assert handle.closed is True


def test_can_session_constructor_failure_shuts_down_raw_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.can import open_python_can_adapter

    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n')
    bus = SimpleNamespace(closed=False, shutdown=lambda: setattr(bus, "closed", True))
    monkeypatch.setitem(sys.modules, "can", SimpleNamespace(Bus=lambda **kwargs: bus))
    monkeypatch.setattr("agentic_hil.can.PythonCanAdapterSession", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("construct failed")))

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert bus.closed is True


def test_com_double_failure_keeps_raw_handle_reachable_for_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.provisional import provisional_handle_count

    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    close_calls = {"count": 0}

    def flaky_close() -> None:
        close_calls["count"] += 1
        if close_calls["count"] == 1:
            raise OSError("first close failed")

    handle = SimpleNamespace(close=flaky_close, open=lambda: None)
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: handle))
    monkeypatch.setattr("agentic_hil.comports.ComPortSession", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("construct failed")))

    result = service._open_serial("dut", config.com_ports["dut"], str(tmp_path / "com.jsonl"), SimpleNamespace(), ContactMarker())

    assert result["ok"] is False
    assert provisional_handle_count(service.coordinator.owner_marker) == 1
    service.close()
    assert close_calls["count"] == 2
    assert provisional_handle_count(service.coordinator.owner_marker) == 0


def test_can_inner_double_failure_keeps_raw_bus_reachable_for_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.can import open_python_can_adapter
    from agentic_hil.process import managed_process_owner
    from agentic_hil.provisional import cleanup_provisional_handles, provisional_handle_count

    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n')
    shutdown_calls = {"count": 0}

    def flaky_shutdown() -> None:
        shutdown_calls["count"] += 1
        if shutdown_calls["count"] == 1:
            raise OSError("first shutdown failed")

    bus = SimpleNamespace(shutdown=flaky_shutdown)
    monkeypatch.setitem(sys.modules, "can", SimpleNamespace(Bus=lambda **kwargs: bus))
    monkeypatch.setattr("agentic_hil.can.PythonCanAdapterSession", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("construct failed")))

    owner = "owner-token-under-test"
    with managed_process_owner(owner):
        result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)
    assert result["ok"] is False
    assert provisional_handle_count(owner) == 1

    assert cleanup_provisional_handles(owner) == []
    assert shutdown_calls["count"] == 2
    assert provisional_handle_count(owner) == 0


def test_can_outer_session_constructor_failure_closes_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.provisional import provisional_handle_count

    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n')
    service = CanBusService(config)
    adapter = SimpleNamespace(closed=False, adapter_name="fake", status=lambda: {"active": True})
    adapter.close = lambda: setattr(adapter, "closed", True)
    monkeypatch.setattr("agentic_hil.can.open_adapter", lambda *args, **kwargs: {"ok": True, "session": adapter})
    monkeypatch.setattr("agentic_hil.can.CanBusSession", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("outer construct failed")))

    with pytest.raises(RuntimeError, match="outer construct failed"):
        service.session_start("bench", clear_rx_queue=False)

    assert adapter.closed is True
    assert provisional_handle_count(service.coordinator.owner_marker) == 0
    assert service.coordinator.blocked is False
    service.close()


def test_active_can_session_drains_more_than_one_buffer_batch(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n    max_buffer_frames: 2\n')
    service = CanBusService(config)

    class QueuedAdapter:
        adapter_name = "fake"

        def __init__(self) -> None:
            self.frames = [1, 2, 3, 4, 5]
            self.reads = 0

        def read(self, max_frames: int, wait_timeout_s: float) -> dict:
            self.reads += 1
            batch = self.frames[:max_frames]
            del self.frames[:max_frames]
            return {"ok": True, "frames": [{"id": item} for item in batch]}

        def status(self) -> dict:
            return {"active": True}

        def close(self) -> dict:
            return {"safe_state_confirmed": True, "process_reaped": True}

    adapter = QueuedAdapter()
    session = CanBusSession("bench", config.can_buses["bench"], adapter, str(tmp_path / "can.jsonl"))
    service.sessions["bench"] = session

    result = service.session_start("bench", clear_rx_queue=True)

    assert result["ok"] is True
    assert result["already_active"] is True
    assert adapter.frames == []
    assert adapter.reads == 4


def test_active_can_session_quarantines_when_queue_never_drains(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n    max_buffer_frames: 2\n')
    service = CanBusService(config)

    class BusyAdapter:
        adapter_name = "fake"

        def read(self, max_frames: int, wait_timeout_s: float) -> dict:
            return {"ok": True, "frames": [{"id": 1}, {"id": 2}]}

        def status(self) -> dict:
            return {"active": True}

    session = CanBusSession("bench", config.can_buses["bench"], BusyAdapter(), str(tmp_path / "can-limit.jsonl"))
    service.sessions["bench"] = session

    result = service.session_start("bench", clear_rx_queue=True)

    assert result["error_type"] == "can_queue_clear_limit"
    assert result["frames_drained"] > config.can_buses["bench"].max_buffer_frames
    assert result["side_effect_committed"] is True
    assert result["side_effect_status"] == "partial"
    assert result["lease_state"] == "cleanup_required"


def test_can_queue_drain_budget_excludes_the_pre_drain_audit_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n    max_buffer_frames: 2\n')
    service = CanBusService(config)
    clock = {"now": 1000.0}

    def slow_append(config_arg, log_path, record):
        if record.get("event") == "queue_clear_start":
            clock["now"] += CAN_DRAIN_TIMEOUT_S * 5
        return append_jsonl_audited(config_arg, log_path, record)

    monkeypatch.setattr("agentic_hil.can.append_jsonl_audited", slow_append)
    monkeypatch.setattr("agentic_hil.can.time", SimpleNamespace(monotonic=lambda: clock["now"]))

    class QueuedAdapter:
        adapter_name = "fake"

        def __init__(self) -> None:
            self.frames = [1, 2, 3, 4, 5]
            self.reads = 0

        def read(self, max_frames: int, wait_timeout_s: float) -> dict:
            self.reads += 1
            batch = self.frames[:max_frames]
            del self.frames[:max_frames]
            return {"ok": True, "frames": [{"id": item} for item in batch]}

        def status(self) -> dict:
            return {"active": True}

        def close(self) -> dict:
            return {"safe_state_confirmed": True, "process_reaped": True}

    adapter = QueuedAdapter()
    session = CanBusSession("bench", config.can_buses["bench"], adapter, str(tmp_path / "can-slow-audit.jsonl"))
    service.sessions["bench"] = session

    result = service.session_start("bench", clear_rx_queue=True)

    assert result["ok"] is True
    assert adapter.frames == []
    assert adapter.reads == 4


def test_can_queue_drain_budget_still_bounds_adapter_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n    max_buffer_frames: 2\n')
    service = CanBusService(config)
    clock = {"now": 1000.0}
    monkeypatch.setattr("agentic_hil.can.time", SimpleNamespace(monotonic=lambda: clock["now"]))

    class SlowBusyAdapter:
        adapter_name = "fake"

        def __init__(self) -> None:
            self.reads = 0

        def read(self, max_frames: int, wait_timeout_s: float) -> dict:
            self.reads += 1
            clock["now"] += CAN_DRAIN_TIMEOUT_S * 0.6
            return {"ok": True, "frames": [{"id": 1}, {"id": 2}]}

        def status(self) -> dict:
            return {"active": True}

    adapter = SlowBusyAdapter()
    session = CanBusSession("bench", config.can_buses["bench"], adapter, str(tmp_path / "can-slow-adapter.jsonl"))
    service.sessions["bench"] = session

    result = service.session_start("bench", clear_rx_queue=True)

    assert result["error_type"] == "can_queue_clear_limit"
    assert adapter.reads == 2
    assert result["lease_state"] == "cleanup_required"


class BlockingStdin:
    """Stdin stub that blocks like a cooked-mode TTY until released, then reports EOF."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def read1(self, size: int) -> bytes:
        self.release.wait()
        return b""


class StubComPortService:
    """ComPortService substitute: one banner chunk is available immediately, then silence."""

    def __init__(self, config) -> None:
        self.banner_sent = False

    def session_start(self, port_id: str, clear_buffer: bool) -> dict:
        return {"ok": True}

    def write_bytes(self, port_id: str, data: bytes, tool: str) -> dict:
        return {"ok": True, "bytes_written": len(data)}

    def read_bytes(self, port_id: str, max_bytes: int, wait_timeout_s: float, tool: str) -> dict:
        if not self.banner_sent:
            self.banner_sent = True
            return {"ok": True, "bytes_read": 6, "data": {"text": "banner"}}
        return {"ok": True, "bytes_read": 0, "data": {"text": ""}}

    def session_stop(self, port_id: str) -> dict:
        return {"ok": True}

    def close(self) -> None:
        pass


def test_com_stdio_relays_device_output_while_stdin_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    monkeypatch.setattr("agentic_hil.comstdio.ComPortService", StubComPortService)
    stdin = BlockingStdin()
    output = StringIO()
    worker = threading.Thread(
        target=run_com_stdio,
        kwargs={"config": config, "port_id": "dut", "input_stream": stdin, "output_stream": output, "error_stream": StringIO()},
        daemon=True,
    )

    worker.start()
    relayed = wait_until(lambda: "banner" in output.getvalue(), timeout_s=2.0)
    stdin.release.set()
    worker.join(timeout=WAIT_TIMEOUT_S)

    assert relayed, "device output was not relayed while stdin was still blocked"
    assert not worker.is_alive(), "com-stdio loop did not exit after stdin EOF"


@pytest.mark.skipif(os.name == "nt", reason="POSIX select-based reader poll")
def test_stdin_reader_stops_without_external_input_on_posix(tmp_path: Path) -> None:
    from agentic_hil.comstdio import start_stdin_reader, stop_stdin_reader

    read_fd, write_fd = os.pipe()

    class PipeStream:
        def fileno(self) -> int:
            return read_fd

        def read(self, size: int) -> bytes:  # pragma: no cover - fallback path only
            return os.read(read_fd, size)

    reader = start_stdin_reader(PipeStream())
    try:
        # No byte is ever written to the pipe; the reader must still observe the
        # stop event via its poll timeout rather than staying parked in read().
        errors = stop_stdin_reader(reader, 0.5)
        assert errors == []
        assert not reader.thread.is_alive()
    finally:
        os.close(write_fd)
        with suppress(OSError):
            os.close(read_fd)


def test_stdin_reader_start_failure_closes_dup_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil import comstdio

    closed: list[int] = []
    real_close = os.close
    monkeypatch.setattr(comstdio.os, "dup", lambda fd: 4242)
    monkeypatch.setattr(comstdio.os, "close", lambda fd: closed.append(fd))

    def failing_start(self) -> None:
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(comstdio.threading.Thread, "start", failing_start)

    class FdStream:
        def fileno(self) -> int:
            return 0

    with pytest.raises(RuntimeError, match="thread start failed"):
        comstdio.start_stdin_reader(FdStream())

    assert 4242 in closed, "the dup'd stdin fd must be closed when the reader thread fails to start"
    real_close  # noqa: B018 - keep reference so monkeypatch teardown restores cleanly


def test_com_stdio_completes_cleanup_when_reader_teardown_interrupts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    events: list[str] = []

    class TrackingService(StubComPortService):
        def read_bytes(self, port_id: str, max_bytes: int, wait_timeout_s: float, tool: str) -> dict:
            # Break the relay loop immediately so the cleanup path is reached.
            return {"ok": False, "error_type": "serial_read_failed"}

        def session_stop(self, port_id: str) -> dict:
            events.append("session_stop")
            return {"ok": True}

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr("agentic_hil.comstdio.ComPortService", TrackingService)

    def interrupting_stop(reader, timeout_s):
        raise KeyboardInterrupt("reader teardown interrupted")

    monkeypatch.setattr("agentic_hil.comstdio.stop_stdin_reader", interrupting_stop)

    with pytest.raises(KeyboardInterrupt):
        run_com_stdio(config, "dut", input_stream=BlockingStdin(), output_stream=StringIO(), error_stream=StringIO())

    assert events == ["session_stop", "close"], "session/service cleanup must still run after an interrupt in reader teardown"


def test_com_stdio_propagates_stdin_reader_error_after_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    monkeypatch.setattr("agentic_hil.comstdio.ComPortService", StubComPortService)

    class ErrorStdin:
        def read(self, size: int) -> bytes:
            raise OSError("stdin failed")

    with pytest.raises(OSError, match="stdin failed"):
        run_com_stdio(config, "dut", input_stream=ErrorStdin(), output_stream=StringIO(), error_stream=StringIO())


def test_com_stdio_cancels_and_joins_blocked_stdin_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)

    class FailingReadService(StubComPortService):
        def read_bytes(self, port_id: str, max_bytes: int, wait_timeout_s: float, tool: str) -> dict:
            return {"ok": False, "error_type": "serial_read_failed"}

    class CancelStdin:
        def __init__(self) -> None:
            self.cancelled = threading.Event()
            self.finished = threading.Event()

        def read(self, size: int) -> bytes:
            self.cancelled.wait()
            self.finished.set()
            return b""

        def cancel_read(self) -> None:
            self.cancelled.set()

    stdin = CancelStdin()
    monkeypatch.setattr("agentic_hil.comstdio.ComPortService", FailingReadService)

    result = run_com_stdio(config, "dut", input_stream=stdin, output_stream=StringIO(), error_stream=StringIO())

    assert result == 1
    assert stdin.cancelled.is_set()
    assert stdin.finished.is_set()


def test_spawn_command_preserves_interrupt_when_cleanup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    child = SimpleNamespace(communicate=lambda timeout: (_ for _ in ()).throw(KeyboardInterrupt("stop")))
    monkeypatch.setattr("agentic_hil.backends.common.spawn_managed_process", lambda *args, **kwargs: child)
    monkeypatch.setattr("agentic_hil.backends.common.terminate_process_tree", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("reap failed")))

    with pytest.raises(KeyboardInterrupt, match="stop") as excinfo:
        spawn_command(["tool"], str(Path.cwd()), 1)

    assert "Cleanup error: reap failed" in str(excinfo.value.args)


def test_artifact_upload_rejects_oversized_local_file(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    build_dir = tmp_path / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    oversized = build_dir / "big.bin"
    oversized.write_bytes(b"\0" * (config.artifacts.max_upload_size_mb * 1024 * 1024 + 1))
    service = AgenticHILToolService(config)

    result = service.call("artifact_upload", {"image_path": "build/big.bin"})

    assert result["ok"] is False
    assert result["error_type"] == "artifact_too_large"
    assert result["side_effect_committed"] is False


def test_com_ports_list_hides_host_ports_without_read_permission(tmp_path: Path) -> None:
    config = load_test_config(
        tmp_path,
        com_ports_yaml=COM_PORT_YAML,
        permissions={"allow_com_read": False},
    )
    service = ComPortService(config)

    result = service.list_ports()

    assert result["ok"] is True
    assert "dut" in result["ports"]
    assert result["available_com_ports"]["ok"] is False
    assert result["available_com_ports"]["error_type"] == "permission_denied"


def test_parse_can_id_rejects_booleans() -> None:
    assert parse_can_id(True) is None
    assert parse_can_id(False) is None
    assert parse_can_id(1) == 1


def test_payload_frame_rejects_boolean_frame_id() -> None:
    bus_config = CanBusConfig(
        adapter="process", channel="vcan0", bitrate=500000, fd=False, data_bitrate=None,
        pcanbasic_dll=None, executable=None, args=[], timeout_s=1.0, poll_interval_ms=10,
        receive_own_messages=False, listen_only=False, max_buffer_frames=16, max_frame_data_bytes=8,
    )

    result = payload_frame(bus_config, {"frame_id": True, "data_hex": "00"})

    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"


def test_bridge_stderr_is_capped_and_surfaced_in_errors() -> None:
    noisy_lines = ["x" * 1024 + "\n"] * 256
    child = SimpleNamespace(stdout=[], stderr=noisy_lines, stdin=None, poll=lambda: None)
    session = ProcessBridgeSession(child)

    assert wait_until(lambda: len(session.stderr) > 0)
    assert wait_until(lambda: session.stderr.endswith("x" * 1024 + "\n"))
    assert len(session.stderr) <= 65536

    error = session._bridge_error("timeout", "Bridge request timed out.")
    assert error["stderr_tail"]
    assert len(error["stderr_tail"]) <= 2000


def test_bridge_close_propagates_reap_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    child = SimpleNamespace(stdout=[], stderr=[], poll=lambda: 0)
    session = ProcessBridgeSession(child)
    monkeypatch.setattr(
        "agentic_hil.bridge.terminate_process_tree",
        lambda process, timeout: (_ for _ in ()).throw(subprocess.TimeoutExpired("bridge", timeout)),
    )

    with pytest.raises(BridgeCleanupError) as excinfo:
        session.close()

    assert session.closed is False
    assert excinfo.value.result["process_reaped"] is False


def test_debug_set_breakpoint_requires_location(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_test_config(tmp_path))

    result = service.call("debug_set_breakpoint", {})

    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"


def test_flash_rejects_non_boolean_reset_after_flash(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_test_config(tmp_path))

    result = service.call("flash_firmware", {"image_path": "build/app.elf", "reset_after_flash": "false"})

    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"


def test_flash_requires_reset_permission_only_when_requested(tmp_path: Path) -> None:
    service = AgenticHILToolService(
        load_test_config(tmp_path, permissions={"allow_flash": True, "allow_reset": False})
    )

    without_reset = service.call("flash_firmware", {"image_path": "build/app.elf", "reset_after_flash": False})
    with_reset = service.call("flash_firmware", {"image_path": "build/app.elf", "reset_after_flash": True})

    assert without_reset["error_type"] != "permission_denied"
    assert with_reset["error_type"] == "permission_denied"


PERMISSION_GATE_CASES = [
    ("allow_probe", "probe_target", {}),
    ("allow_probe", "debugger_info", {}),
    ("allow_flash", "flash_firmware", {"image_path": "build/app.elf"}),
    ("allow_reset", "reset_target", {"mode": "run"}),
    ("allow_com_read", "com_session_start", {"port_id": "dut"}),
    ("allow_com_write", "com_write", {"port_id": "dut", "text": "hi"}),
    ("allow_can_read", "can_read", {"bus_id": "bench"}),
    ("allow_can_write", "can_send", {"bus_id": "bench", "frame_id": 1, "data_hex": "00"}),
]


@pytest.mark.parametrize(("flag", "tool", "arguments"), PERMISSION_GATE_CASES)
def test_disabled_permission_blocks_tool(tmp_path: Path, flag: str, tool: str, arguments: dict) -> None:
    config = load_test_config(
        tmp_path,
        com_ports_yaml=COM_PORT_YAML,
        can_buses_yaml=CAN_BUS_YAML,
        permissions={flag: False},
    )
    service = AgenticHILToolService(config)

    result = service.call(tool, arguments)

    assert result["ok"] is False, f"{tool} must be blocked when {flag} is false"
    assert result["error_type"] == "permission_denied"


def test_startup_config_is_frozen_when_file_permissions_change(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, permissions={"allow_probe": False})
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        config_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(config_text.replace("allow_probe: false", "allow_probe: true"), encoding="utf-8")

        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "permission_denied"


def test_startup_config_is_frozen_when_file_resources_change(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        config_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(config_text.replace("com_ports: {}", COM_PORT_YAML.rstrip()), encoding="utf-8")

        result = service.call("com_ports_list")
    finally:
        service.close()

    assert result["ok"] is True
    assert "dut" not in result["ports"]


def test_authoritative_config_falls_back_to_canonical_project_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(workspace, monkeypatch, permissions={})
    monkeypatch.delenv("AGENTIC_HIL_CONFIG")

    config = load_authoritative_config(workspace)

    assert config.config_path == str(config_path)


def test_authoritative_config_env_must_be_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", "relative/config.yaml")

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(tmp_path)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["environment_variable"] == "AGENTIC_HIL_CONFIG"


def test_authoritative_config_accepts_absolute_external_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    config_path = write_config(
        workspace,
        config_path=tmp_path / "managed" / "bench.yaml",
        permissions={},
    )
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(config_path.resolve()))

    config = load_authoritative_config(workspace)

    assert config.config_path == str(config_path.resolve())


def test_authoritative_config_must_be_outside_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_root = tmp_path / "user-config"
    monkeypatch.setenv("APPDATA", str(config_root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    workspace = config_root / "agentic-hil" / "projects" / "inside" / "workspace"
    config_path = write_config(workspace, config_path=workspace / "config.yaml")
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(config_path.resolve()))

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["workspace_root"] == str(workspace.resolve())


def test_authoritative_config_requires_exact_workspace_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    other.mkdir()
    write_authoritative_config(workspace, monkeypatch, permissions={})

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(other)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["workspace_root"] == str(workspace.resolve())
    assert rejected.value.details["expected_workspace"] == str(other.resolve())


def test_authoritative_config_override_need_not_use_canonical_workspace_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    canonical = write_authoritative_config(workspace, monkeypatch, permissions={})
    alternate = canonical.parent.parent / "alternate" / "config.yaml"
    alternate.parent.mkdir()
    alternate.write_text(canonical.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(alternate))

    config = load_authoritative_config(workspace)

    assert config.config_path == str(alternate.resolve())


def test_raw_config_requires_workspace_root(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("target: {}\n", encoding="utf-8")

    with pytest.raises(ConfigError) as rejected:
        load_config(str(config_path))

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["validator"] == "required"


def test_raw_config_rejects_duplicate_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"workspace_root: {str(tmp_path.resolve())!r}\npermissions: {{}}\npermissions: {{}}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as rejected:
        load_config(str(config_path))

    assert rejected.value.error_type == "config_invalid"
    assert "valid YAML" in rejected.value.summary


@pytest.mark.parametrize("section", ["com_ports", "can_buses"])
@pytest.mark.parametrize("non_string_key", ["1", "true", "2026-07-18"])
def test_raw_config_rejects_non_string_mapping_keys(tmp_path: Path, section: str, non_string_key: str) -> None:
    config_path = write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace(f"{section}: {{}}", f"{section}:\n  {non_string_key}: {{}}"), encoding="utf-8")

    with pytest.raises(ConfigError) as rejected:
        load_config(str(config_path))

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["path"] == str(config_path.resolve())
    assert rejected.value.details["line"] > 0
    assert rejected.value.details["column"] > 0
    assert "backend_error" not in rejected.value.details


def test_authoritative_config_pins_external_can_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        can_buses_yaml=(
            'can_buses:\n  bench:\n    adapter: "process"\n    channel: "test"\n'
            f'    executable: "{FAKE_OPENOCD.as_posix()}"\n'
        ),
        permissions={},
    )

    config = load_authoritative_config(workspace)

    assert config.can_buses["bench"].executable == str(FAKE_OPENOCD.resolve())


def test_authoritative_config_rejects_workspace_can_bridge_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    bridge = workspace / "bridge.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("print('untrusted')\n", encoding="utf-8")
    write_authoritative_config(
        workspace,
        monkeypatch,
        can_buses_yaml=f'can_buses:\n  injected:\n    adapter: "process"\n    channel: "test"\n    executable: "{bridge.as_posix()}"\n',
        permissions={},
    )

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "can_buses.injected.executable"


def test_authoritative_config_rejects_process_bridge_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    bridge = workspace / "bridge.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("print('untrusted')\n", encoding="utf-8")
    write_authoritative_config(
        workspace,
        monkeypatch,
        can_buses_yaml=(
            'can_buses:\n  injected:\n    adapter: "process"\n    channel: "test"\n'
            f'    executable: "{FAKE_OPENOCD.as_posix()}"\n    args: ["{bridge.as_posix()}"]\n'
        ),
        permissions={},
    )

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "can_buses.injected.args"


def test_authoritative_config_pins_debugger_and_openocd_scripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    host = tmp_path / "tools"
    interface_cfg = host / "trusted-interface.cfg"
    target_cfg = host / "trusted-target.cfg"
    interface_cfg.parent.mkdir(parents=True)
    interface_cfg.write_text("# trusted interface\n", encoding="utf-8")
    target_cfg.write_text("# trusted target\n", encoding="utf-8")
    config_path = write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_probe": True},
    )
    config_text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        config_text.replace((config_path.parent / "interface.cfg").as_posix(), interface_cfg.as_posix()).replace(
            (config_path.parent / "target.cfg").as_posix(), target_cfg.as_posix()
        ),
        encoding="utf-8",
    )

    config = load_authoritative_config(workspace)

    assert config.debugger.executable == str(FAKE_OPENOCD.resolve())
    assert config.debugger.interface_cfg == str(interface_cfg.resolve())
    assert config.debugger.target_cfg == str(target_cfg.resolve())


def test_authoritative_config_rejects_debuggers_that_pin_onto_one_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two pyocd entries reached through different spellings of one binary (an
    # absolute path and a bare PATH name), both naming the same probe serial.
    # Distinct executables never made these distinct boards, and pinning
    # collapses them onto one file, so the config is refused.
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    shim = toolchain / "pyocd-shim.bat"
    shim.write_text("@echo off\n", encoding="utf-8")
    os.chmod(shim, 0o755)
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_type="pyocd",
        debugger_executable=shim,
        debugger_name="dut_a",
        probe_id="PROBE-A",
        auto_probe_ids=False,
        debuggers_yaml='debuggers:\n  probe_b:\n    type: pyocd\n    probe_id: "PROBE-A"\n    executable: "pyocd-shim.bat"\n',
        permissions={"allow_probe": True},
    )
    monkeypatch.setenv("PATH", str(toolchain) + os.pathsep + os.environ.get("PATH", ""))

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "debuggers.probe_b.probe_id"
    assert rejected.value.details["other_debugger"] == "dut_a"


def test_authoritative_config_rejects_a_relative_openocd_script_path_when_debugger_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value that navigates is a path, and a path has to say where it is.

    `./interface/stlink.cfg` is not what OpenOCD resolves out of its script
    tree; it is this configuration claiming a file, relative to a working
    directory nothing here names. The search-name spelling is the one exempted
    from the file rule, and it is exempt by being a name, not by being short."""
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_probe": True},
    )
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        text.replace((config_path.parent / "interface.cfg").as_posix(), "./interface/stlink.cfg"), encoding="utf-8"
    )

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "debuggers.dut.interface_cfg"
    assert "absolute" in rejected.value.summary


def test_an_openocd_search_name_loads_on_a_host_with_no_script_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The generated value, on the machine the install eval actually ran on.

    `interface/stlink.cfg` is what OpenOCD resolves against its own script path,
    and which tree that is belongs to the OpenOCD that runs. Held to the file
    rule it was refused on every host without a script tree, and the first
    answer an install found for that was to write `/tmp/openocd-scripts/...` and
    point the authoritative configuration there. It is accepted as what it is,
    and kept exactly as written: resolving it here would put this process's
    guess about OpenOCD's script path into the config."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_probe": True, "allow_flash": True, "allow_reset": True},
        interface_cfg="interface/stlink.cfg",
        target_cfg="target/stm32f4x.cfg",
    )

    config = load_authoritative_config(workspace)

    entry = config.debuggers["dut"]
    assert debugger_drives_hardware(config, entry), "the entry has a toolchain, so it is the checked kind"
    assert entry.interface_cfg == "interface/stlink.cfg"
    assert entry.target_cfg == "target/stm32f4x.cfg"
    assert openocd_script_kind(entry.interface_cfg) == OPENOCD_SCRIPT_SEARCH_NAME


def test_connect_mode_defaults_to_hot_plug_and_stlink_accepts_under_reset(tmp_path: Path) -> None:
    """The key is optional, and its default is what every older file already did.

    Both halves matter here. A configuration that never heard of `connect_mode`
    has to keep flashing exactly as it did, which is what the default states, and
    a configuration that asks for the fix has to come out of the loader carrying
    it rather than being normalised away."""
    unset = load_config(str(write_config(tmp_path / "unset", debugger_type="stlink", probe_id="STLINK123")))
    asked = load_config(str(write_config(tmp_path / "asked", debugger_type="stlink", probe_id="STLINK123", connect_mode="under_reset")))
    spelled_out = load_config(str(write_config(tmp_path / "spelled", debugger_type="stlink", probe_id="STLINK123", connect_mode="hotplug")))

    assert unset.debuggers["dut"].connect_mode == "hotplug"
    assert asked.debuggers["dut"].connect_mode == "under_reset"
    assert spelled_out.debuggers["dut"].connect_mode == "hotplug"


@pytest.mark.parametrize("debugger_type", ["openocd", "pyocd"])
def test_connect_mode_under_reset_is_refused_by_the_backends_that_would_ignore_it(tmp_path: Path, debugger_type: str) -> None:
    """A value a backend cannot carry out is a refusal, not a silent no-op.

    `target_type` on OpenOCD is read and ignored, and that is tolerable: it is a
    field belonging to another backend, sitting in an entry that names its own.
    An ignored `under_reset` is a different thing. It is a bench saying its first
    flash keeps failing at the erase under a running core, and accepting the word
    while changing nothing would leave that bench believing it had been fixed.
    The refusal names the field, what this backend accepts, and where the same
    effect lives for it."""
    with pytest.raises(ConfigError) as refused:
        load_config(str(write_config(tmp_path, debugger_type=debugger_type, target_type="stm32f446retx", connect_mode="under_reset")))

    assert refused.value.error_type == "config_invalid"
    assert refused.value.details["field"] == "debuggers.dut.connect_mode"
    assert refused.value.details["value"] == "under_reset"
    assert refused.value.details["allowed_values"] == ["hotplug"]
    assert "stlink" in refused.value.summary
    assert "interface_cfg" in refused.value.summary, "OpenOCD's own way to the same effect is named"

    # And the value every backend does have still loads, so the refusal is about
    # the one mode rather than about the key.
    accepted = load_config(str(write_config(tmp_path / "hotplug", debugger_type=debugger_type, target_type="stm32f446retx", connect_mode="hotplug")))
    assert accepted.debuggers["dut"].connect_mode == "hotplug"


def test_a_connect_mode_the_schema_does_not_spell_is_refused_with_both_values(tmp_path: Path) -> None:
    """`mode=UR` is the programmer's spelling, not this configuration's.

    A person who read the STM32CubeProgrammer documentation will try `UR` here,
    and the answer has to be the enum rather than a backend message, because the
    fix is to write the other word rather than to change backend."""
    with pytest.raises(ConfigError) as refused:
        load_config(str(write_config(tmp_path, debugger_type="stlink", probe_id="STLINK123", connect_mode="UR")))

    assert refused.value.error_type == "config_invalid"
    assert refused.value.details["field"] == "debuggers.dut.connect_mode"
    assert refused.value.details["allowed_values"] == ["hotplug", "under_reset"]
    assert refused.value.details["value"] == "UR"


def test_the_generated_configuration_adopts_a_probe_on_a_host_with_no_script_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole sequence the install eval ran, through the real writer and parser.

    `agentic-hil init` writes the skeleton with the two search names in it, and
    that file loads because the entry names no toolchain. Adoption then fills in
    the executable and the probe serial, which is all `setup` and
    `project_config_adopt_hardware` do to it, and the entry starts driving
    hardware. That transition is where `doctor` used to turn red on a bench
    nothing was wrong with, on any machine without an OpenOCD script tree, and
    this host is one: neither script exists here."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    written = init_config()
    assert written["ok"] is True, written
    config_file = Path(written["path"])
    document = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert document["debuggers"]["dut"]["interface_cfg"] == "interface/stlink.cfg"
    document["debuggers"]["dut"]["executable"] = FAKE_OPENOCD.as_posix()
    document["debuggers"]["dut"]["probe_id"] = "066AFF495451885087171450"
    config_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    config = load_authoritative_config(workspace.resolve())

    entry = config.debuggers["dut"]
    assert debugger_drives_hardware(config, entry)
    assert entry.interface_cfg == "interface/stlink.cfg"
    assert entry.target_cfg == "target/stm32f4x.cfg"
    assert not Path(entry.interface_cfg).exists(), "this host has no OpenOCD script tree, which is the point"
    report = doctor(str(config_file))
    assert report["ok"] is True, report
    assert report["debuggers"]["dut"]["scripts"]["interface_cfg"]["kind"] == OPENOCD_SCRIPT_SEARCH_NAME


def test_doctor_labels_a_search_name_rather_than_calling_it_a_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What `doctor` says about the two spellings, which is the whole difference.

    A search name reported as an absent file reads as a broken bench and invites
    somebody to manufacture the file; the report names the kind instead, and
    asks whether the file is there only of a value that claims to be one."""
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_probe": True},
        interface_cfg="interface/stlink.cfg",
    )
    monkeypatch.chdir(workspace)

    report = doctor(str(config_path))

    assert report.get("debuggers") is not None, report
    scripts = report["debuggers"]["dut"]["scripts"]
    assert scripts["interface_cfg"] == {
        "value": "interface/stlink.cfg",
        "kind": OPENOCD_SCRIPT_SEARCH_NAME,
        "resolved_by": "openocd",
        "note": scripts["interface_cfg"]["note"],
    }
    assert "does not have to exist here" in scripts["interface_cfg"]["note"]
    # The other field kept the conftest's absolute path, and a path is answered
    # about as a path: it is one, and it is there.
    assert scripts["target_cfg"]["kind"] == "path"
    assert scripts["target_cfg"]["exists"] is True


def test_an_openocd_script_under_temporary_storage_is_refused_by_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The answer the install eval invented, refused with its own sentence.

    A fabricated `/tmp/openocd-scripts/interface/stlink.cfg` passes every check
    the file rule makes (it exists, it is a single-link regular file, it is
    outside the workspace) and describes the bench until the next reboot. So it
    is refused for where it is, and the refusal says that rather than talking
    about a missing file.

    The temporary root is injected because the suite's own `tmp_path` is under
    the real one; `test_temporary_roots_name_this_platforms_temporary_storage`
    is what holds the real set."""
    temporary_root = tmp_path / "system-temp"
    fabricated = temporary_root / "openocd-scripts" / "interface" / "stlink.cfg"
    fabricated.parent.mkdir(parents=True)
    fabricated.write_text("# fabricated\n", encoding="utf-8")
    monkeypatch.setattr("agentic_hil.config.temporary_roots", lambda: (temporary_root,))
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_probe": True},
        interface_cfg=fabricated.as_posix(),
    )

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "debuggers.dut.interface_cfg"
    assert rejected.value.details["temporary_root"] == str(temporary_root)
    assert "temporary storage" in rejected.value.summary
    assert "single-link regular file" not in rejected.value.summary


def test_a_configuration_in_temporary_storage_is_not_refused_for_its_own_scripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one case the temporary refusal has nothing true to say about.

    The rule is about a configuration that outlives what it points at. A
    configuration that lives in temporary storage itself is already exactly as
    ephemeral as this field could make it, so refusing the field would name the
    wrong thing about that bench. It is also the shape every test in this suite
    has, since `tmp_path` is under the platform's temporary root, so it is
    written down rather than left to be discovered."""
    temporary_root = tmp_path / "system-temp"
    config_root = temporary_root / "user-config"
    monkeypatch.setattr("agentic_hil.config.temporary_roots", lambda: (temporary_root,))
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(
        workspace,
        monkeypatch,
        config_root=config_root,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_probe": True},
    )
    assert config_path.is_relative_to(temporary_root)

    config = load_authoritative_config(workspace)

    assert Path(config.debuggers["dut"].interface_cfg).is_relative_to(temporary_root)


def test_a_windows_style_temporary_script_is_refused_in_either_spelling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Backslashes and forward slashes name one file, and one is not a way past this.

    Windows accepts both separators for the same path, so a check that read the
    string rather than the path would refuse `C:\\...\\Temp\\x.cfg` and let
    `C:/.../Temp/x.cfg` through."""
    if os.name != "nt":
        pytest.skip("both separators name one path only on Windows")
    temporary_root = tmp_path / "system-temp"
    fabricated = temporary_root / "openocd-scripts" / "stlink.cfg"
    fabricated.parent.mkdir(parents=True)
    fabricated.write_text("# fabricated\n", encoding="utf-8")
    monkeypatch.setattr("agentic_hil.config.temporary_roots", lambda: (temporary_root,))
    for spelling in (fabricated.as_posix(), str(fabricated)):
        workspace = tmp_path / f"workspace-{spelling.count(chr(92))}"
        write_authoritative_config(
            workspace,
            monkeypatch,
            debugger_executable=FAKE_OPENOCD,
            permissions={"allow_probe": True},
            interface_cfg=spelling,
        )

        with pytest.raises(ConfigError) as rejected:
            load_authoritative_config(workspace)

        assert "temporary storage" in rejected.value.summary, spelling


def test_temporary_roots_name_this_platforms_temporary_storage() -> None:
    """The set the refusal is measured against, per platform.

    `tempfile.gettempdir()` alone is the process's inherited answer from
    TMPDIR, TEMP or TMP, so a bench whose config was loaded under one
    environment and written under another would be measured against a root it
    never used. The
    platform's own fixed locations are named beside it."""
    roots = {Path(root) for root in temporary_roots()}

    assert Path(tempfile.gettempdir()) in roots
    if os.name == "nt":
        assert Path(os.environ["LOCALAPPDATA"]) / "Temp" in roots
        assert Path(os.environ.get("SYSTEMROOT") or "C:/Windows") / "Temp" in roots
    else:
        assert Path("/tmp") in roots
        assert Path("/var/tmp") in roots


def test_a_search_name_is_never_read_as_temporary_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The temporary check is asked of both spellings and answers no to this one.

    A search name is resolved by OpenOCD against its script path and never
    against a working directory, so it names nothing under the temporary root
    even when the process that loads the config is running there. Reading it as
    a relative path would have refused the shipped default on any bench started
    from a temporary directory."""
    temporary_root = tmp_path / "system-temp"
    temporary_root.mkdir()
    monkeypatch.setattr("agentic_hil.config.temporary_roots", lambda: (temporary_root,))
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_probe": True},
        interface_cfg="interface/stlink.cfg",
        target_cfg="target/stm32f4x.cfg",
    )
    # Started inside the temporary root: a check that resolved the name against
    # the process working directory would land squarely under it.
    monkeypatch.chdir(temporary_root)

    config = load_authoritative_config(workspace)

    assert config.debuggers["dut"].interface_cfg == "interface/stlink.cfg"


def test_an_openocd_entry_with_a_real_toolchain_is_validated_whatever_its_scripts_say(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both skeleton script names plus a real executable is a driving entry.

    The placeholder exemption used to key on exactly this shape — an OpenOCD
    entry still holding `interface/stlink.cfg` and `target/stm32f4x.cfg` — and so
    let an entry with a real program behind it and every permission granted skip
    script validation and `doctor` alike. What the entry can drive is what
    decides now, and this one can: the two search names are fine, and the script
    field that claims a path is held to the path rule in the same entry."""
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_flash": True},
        interface_cfg="interface/stlink.cfg",
        target_cfg=(tmp_path / "no-such-script-tree" / "stm32f4x.cfg").as_posix(),
    )
    assert "interface/stlink.cfg" in config_path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "debuggers.dut.target_cfg"


def test_the_starter_entry_does_not_pick_up_a_toolchain_from_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry nobody configured stays inert, on a host that has OpenOCD.

    This is the other half of the rule above. Autodetection used to resolve
    `openocd` off PATH for the starter entry too, so on such a host `agentic-hil
    init` left behind an entry that granted everything, had a real
    program behind it, and drove a board with the skeleton's two relative script
    names — exempt from validation and skipped by `doctor`, because both keyed on
    it being the starter entry. It names no toolchain and nothing finds it one."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_executable=None,
        permissions={"allow_flash": True, "allow_reset": True},
        interface_cfg="interface/stlink.cfg",
        target_cfg="target/stm32f4x.cfg",
    )
    # The shipped skeleton names no executable; the conftest writer always does.
    config_file = project_config_path(workspace)
    document = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    document["debuggers"]["dut"]["executable"] = None
    document["debuggers"]["dut"]["probe_id"] = None
    config_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda name, *args, **kwargs: str(FAKE_OPENOCD) if "openocd" in str(name).lower() else None)

    config = load_authoritative_config(workspace)

    entry = config.debuggers["dut"]
    assert debugger_is_placeholder(entry), "an entry nobody configured must not acquire a toolchain at load"
    assert not debugger_drives_hardware(config, entry)
    assert entry.executable is not None and executable_is_disabled(entry.executable)
    # And the relative script names it still carries reach no program: the entry
    # is inert, which is the only reason it may keep them.
    assert entry.interface_cfg == "interface/stlink.cfg"


def test_an_openocd_entry_with_no_mutation_grant_is_still_validated_under_version_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading is a way in, so script validation may not key on the write grants.

    `allow_probe`, `allow_flash` and `allow_reset` all false, and under version 2
    that stops nothing: reading needs no grant, so `probe_target` on this entry
    starts the real OpenOCD behind it. Validation used to be conditional on those
    three, so this exact configuration loaded with two unchecked script paths in
    it and `doctor` skipped the entry: a debugger run on files no part of this
    configuration had looked at."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        config_version=2,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_probe": False, "allow_flash": False, "allow_reset": False, "allow_raw_debugger_commands": False, "allow_mass_erase": False},
        interface_cfg=(tmp_path / "no-such-script-tree" / "stlink.cfg").as_posix(),
    )

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "debuggers.dut.interface_cfg"


def test_a_read_free_entry_without_mutation_grants_is_checked_by_doctor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The set `doctor` checks is the set config load validated, read model included.

    Same entry as above with its scripts made absolute: it loads, and it must not
    then be skipped. An entry nothing validated and nothing checks, that a probe
    read still reaches, is the pair of holes this predicate closes at once."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        config_version=2,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_probe": False, "allow_flash": False, "allow_reset": False, "allow_raw_debugger_commands": False, "allow_mass_erase": False},
    )

    config = load_authoritative_config(workspace)

    entry = config.debuggers["dut"]
    assert not debugger_is_placeholder(entry)
    assert debugger_drives_hardware(config, entry), "a read-free entry with a toolchain is reachable and must be checked"


def test_a_configured_openocd_entry_resolves_from_path_and_is_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`executable: null` on an identified bench is a PATH lookup, not a starter.

    The starter exemption used to ask only about the type, the two skeleton
    script names and the absent executable, so a bench somebody had identified
    with a probe serial — the shape the served worked example shows — was called
    untouched on a host that has OpenOCD: no PATH candidate was tried, the entry
    took the disabled marker, and `doctor` skipped a fully configured bench. An
    entry that names a board is not the shipped skeleton."""
    skeleton = DebuggerConfig(
        type="openocd",
        executable=None,
        probe_id=None,
        target_type=None,
        interface="SWD",
        interface_cfg="interface/stlink.cfg",
        target_cfg="target/stm32f4x.cfg",
        flash_address=None,
        timeout_s=60,
    )
    assert debugger_is_starter_entry(skeleton), "the untouched skeleton entry is the one exemption"
    for named in (
        replace(skeleton, probe_id="066AFF495451885087171450"),
        replace(skeleton, resource_id="bench-a"),
        replace(skeleton, target_type="stm32f4x"),
        replace(skeleton, flash_address="0x08000000"),
    ):
        assert not debugger_is_starter_entry(named), "an entry that names a board is not untouched"

    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        config_version=2,
        debugger_executable=None,
        probe_id="066AFF495451885087171450",
    )
    config_file = project_config_path(workspace)
    document = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    document["debuggers"]["dut"]["executable"] = None
    config_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda name, *args, **kwargs: str(FAKE_OPENOCD) if "openocd" in str(name).lower() else None)

    config = load_authoritative_config(workspace)

    entry = config.debuggers["dut"]
    assert entry.executable == str(FAKE_OPENOCD.resolve())
    assert not debugger_is_placeholder(entry)
    assert debugger_drives_hardware(config, entry)


def test_a_configured_openocd_entry_from_path_keeps_no_unchecked_script_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the same rule: resolved from PATH means validated too.

    The identified entry above with a script path that is nowhere on this host.
    It is not the starter entry, so it acquires the toolchain, and having one,
    every path it states is checked."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        config_version=2,
        debugger_executable=None,
        probe_id="066AFF495451885087171450",
        interface_cfg=(tmp_path / "no-such-script-tree" / "stlink.cfg").as_posix(),
    )
    config_file = project_config_path(workspace)
    document = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    document["debuggers"]["dut"]["executable"] = None
    config_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda name, *args, **kwargs: str(FAKE_OPENOCD) if "openocd" in str(name).lower() else None)

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "debuggers.dut.interface_cfg"


def test_the_served_worked_example_is_not_an_inert_starter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The example a caller copies has to satisfy the rules a caller is held to.

    It shows an identified OpenOCD bench with `executable: null`, so under the
    corrected starter predicate it resolves from PATH and its scripts are
    validated — which means the example's own scripts must be absolute paths
    outside the workspace, or the file it teaches people to write is one config
    load refuses."""
    example = yaml.safe_load(CONFIG_WORKED_EXAMPLE)
    entry = example["debuggers"]["dut"]

    assert entry["executable"] is None
    assert entry["probe_id"]
    for field in ("interface_cfg", "target_cfg"):
        script = PurePosixPath(entry[field])
        assert script.is_absolute() or entry[field][1:3] == ":/", f"{field} must be absolute"
        assert not entry[field].startswith(example["workspace_root"]), f"{field} must be outside the workspace"

    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        config_version=2,
        debugger_executable=None,
        probe_id=entry["probe_id"],
        interface_cfg=entry["interface_cfg"],
        target_cfg=entry["target_cfg"],
    )
    config_file = project_config_path(workspace)
    document = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    document["debuggers"]["dut"]["executable"] = None
    config_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda name, *args, **kwargs: str(FAKE_OPENOCD) if "openocd" in str(name).lower() else None)

    # The example's own scripts do not exist on this host, so the failure it must
    # not produce is the starter one — a disabled marker and a skipped bench.
    # Whatever load says, it says it about a resolved toolchain.
    try:
        config = load_authoritative_config(workspace)
    except ConfigError as error:
        assert error.error_type == "config_invalid"
        assert error.details["field"].startswith("debuggers.dut.")
    else:
        assert not debugger_is_placeholder(config.debuggers["dut"])


def test_artifact_root_symlink_cannot_pivot_after_startup(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    service = AgenticHILToolService(config)
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    (outside / "firmware.elf").write_bytes(b"\x7fELFfake")
    try:
        (tmp_path / "build").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        service.close()
        pytest.skip(f"directory symlinks unavailable: {error}")

    try:
        result = service.call("flash_firmware", {"image_path": "build/firmware.elf"})
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "artifact_validation_failed"
    assert result["validation"]["allowed_root"] is True
    assert result["validation"]["regular_file"] is False


def test_report_directory_symlink_is_rejected(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    service = AgenticHILToolService(config)
    outside = tmp_path / "outside-reports"
    outside.mkdir()
    try:
        (tmp_path / ".agentic-hil" / "reports").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        service.close()
        pytest.skip(f"directory symlinks unavailable: {error}")

    try:
        result = service.call("get_last_report")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "report_not_found"


def test_last_report_file_symlink_is_rejected_without_reading_target(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    reports = tmp_path / ".agentic-hil" / "reports"
    reports.mkdir(parents=True)
    outside = tmp_path / "outside-report.json"
    outside.write_text('{"secret": "must-not-be-read"}\n', encoding="utf-8")
    report_path = reports / "last-report.json"
    try:
        report_path.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")
    service = AgenticHILToolService(config)
    try:
        result = service.call("get_last_report")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "report_not_found"
    assert "must-not-be-read" not in json.dumps(result)


def test_parallel_jsonl_appends_keep_every_event_exactly_once(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"

    with ThreadPoolExecutor(max_workers=32) as executor:
        list(executor.map(lambda event_id: append_jsonl(str(log_path), {"event_id": event_id}), range(1000)))

    events = [json.loads(line)["event_id"] for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 1000
    assert sorted(events) == list(range(1000))


def test_report_path_is_not_claimed_when_canonical_state_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    monkeypatch.setattr("agentic_hil.report.write_report_state", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("state denied")))

    result = write_report(config, {"ok": True, "tool": "probe_target"})

    assert result["audit_ok"] is False
    assert "report_path" not in result


def test_report_path_is_not_claimed_when_workspace_snapshot_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil import report as report_module

    config = load_test_config(tmp_path)
    original_write = report_module.safe_write_text

    def fail_snapshot(config, path, text, **kwargs):
        if Path(path).name == "last-report.json":
            raise OSError("snapshot denied")
        return original_write(config, path, text, **kwargs)

    monkeypatch.setattr("agentic_hil.report.safe_write_text", fail_snapshot)

    result = write_report(config, {"ok": True, "tool": "probe_target"})

    assert result["audit_ok"] is False
    assert "report_path" not in result


def test_path_lock_registry_does_not_keep_short_lived_paths(tmp_path: Path) -> None:
    _PATH_LOCKS.clear()

    for index in range(100):
        assert append_jsonl(str(tmp_path / f"event-{index}.jsonl"), {"event_id": index}) is None

    assert _PATH_LOCKS == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows directory-handle behavior")
def test_windows_secure_io_handles_block_parent_rename(tmp_path: Path) -> None:
    parent = tmp_path / "audit"
    parent.mkdir()
    handles = _windows_hold_directory_chain(parent)
    try:
        with pytest.raises(OSError):
            os.replace(parent, tmp_path / "replacement")
    finally:
        _close_windows_handles(handles)


def test_can_close_failure_retains_session_for_retry(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, can_buses_yaml=CAN_BUS_YAML)

    class RetryAdapter:
        adapter_name = "process"

        def __init__(self) -> None:
            self.active = True
            self.attempts = 0

        def close(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("bridge busy")
            self.active = False

        def status(self) -> dict:
            return {"active": self.active}

    adapter = RetryAdapter()
    log_path = tmp_path / ".agentic-hil" / "logs" / "can.jsonl"
    log_path.parent.mkdir(parents=True)
    session = CanBusSession("bench", config.can_buses["bench"], adapter, str(log_path))  # type: ignore[arg-type]
    service = CanBusService(config)
    service.sessions["bench"] = session

    first = service.session_stop("bench")
    second = service.session_stop("bench")

    assert first["error_type"] == "can_adapter_close_failed"
    assert second["ok"] is True
    assert adapter.attempts == 2
    assert "bench" not in service.sessions


def test_unavailable_audit_prevents_hardware_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    backend = SimpleNamespace(
        probe_target=lambda: calls.append("probe") or {"ok": True, "tool": "probe_target"},
        close=lambda: None,
    )
    service = AgenticHILToolService(load_test_config(tmp_path), backend=backend)
    monkeypatch.setattr("agentic_hil.report.safe_write_text", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "audit_unavailable"
    assert result["side_effect_committed"] is False
    assert calls == []


def test_com_log_path_failure_does_not_open_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    def serial_open(*args, **kwargs):
        opened.append("serial")
        return SimpleNamespace(is_open=True, close=lambda: None)

    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=serial_open))
    monkeypatch.setattr("agentic_hil.comports.logs_directory", lambda config: (_ for _ in ()).throw(ConfigError("unsafe_configured_path", "bad log path")))
    service = ComPortService(load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML))

    result = service.session_start("dut")

    assert result["error_type"] == "audit_unavailable"
    assert opened == []
    assert service.sessions == {}


def test_can_log_path_failure_does_not_open_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    monkeypatch.setattr("agentic_hil.can.logs_directory", lambda config: (_ for _ in ()).throw(ConfigError("unsafe_configured_path", "bad log path")))
    monkeypatch.setattr("agentic_hil.can.open_adapter", lambda *args, **kwargs: opened.append("adapter") or {"ok": True})
    service = CanBusService(load_test_config(tmp_path, can_buses_yaml=CAN_BUS_YAML))

    result = service.session_start("bench")

    assert result["error_type"] == "audit_unavailable"
    assert opened == []
    assert service.sessions == {}


def test_post_action_report_failure_preserves_committed_flash_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFfake")
    config = load_test_config(tmp_path, debugger_type="stlink")
    service = AgenticHILToolService(config)
    from agentic_hil import report as report_module

    original_write = report_module.safe_write_text

    def fail_last_report(config, path, text, **kwargs):
        if Path(path).name == "last-report.json":
            raise OSError("disk full")
        return original_write(config, path, text, **kwargs)

    monkeypatch.setattr(report_module, "safe_write_text", fail_last_report)
    try:
        result = service.call("flash_firmware", {"image_path": "build/firmware.elf"})
    finally:
        service.close()

    assert result["ok"] is True
    assert result["side_effect_committed"] is True
    assert result["audit_ok"] is False
    assert result["retry_safe"] is False
    assert result["audit_error"]["backend_error"] == "disk full"


def test_gdbmi_close_always_runs_process_tree_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    child = SimpleNamespace(poll=lambda: 0, wait=lambda timeout: 0)
    client = object.__new__(GdbMiClient)
    client.child = child
    client.exited = threading.Event()
    client.exited.set()
    calls: list[object] = []
    monkeypatch.setattr("agentic_hil.gdbmi.terminate_process_tree", lambda process, timeout: calls.append(process))

    client.close(0.1)

    assert calls == [child]


def test_debug_server_cleanup_runs_after_leader_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    child = SimpleNamespace(poll=lambda: 0)
    sessions = object.__new__(GdbDebugSessions)
    sessions._audit_broken = None
    sessions._write_session_log = lambda session: None
    session = SimpleNamespace(gdb=None, server=child)
    calls: list[object] = []
    monkeypatch.setattr("agentic_hil.backends.gdbdebug.terminate_process_tree", lambda process, timeout: calls.append(process))

    result = sessions._cleanup_session(session, 0.1)

    assert result is None
    assert calls == [child]


def test_audit_ready_rejects_last_failure_symlink(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    reports = tmp_path / ".agentic-hil" / "reports"
    reports.mkdir(parents=True)
    outside = tmp_path / "outside-last-failure.json"
    outside.write_text("{}\n", encoding="utf-8")
    target = reports / "last-failure.json"
    try:
        target.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")

    with pytest.raises(ConfigError) as rejected:
        ensure_audit_ready(config)

    assert rejected.value.error_type == "unsafe_configured_path"


def test_audit_ready_rejects_last_failure_hardlink(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    reports = tmp_path / ".agentic-hil" / "reports"
    reports.mkdir(parents=True)
    outside = tmp_path / "outside-last-failure.json"
    outside.write_text("{}\n", encoding="utf-8")
    target = reports / "last-failure.json"
    try:
        os.link(outside, target)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")

    with pytest.raises(ConfigError) as rejected:
        ensure_audit_ready(config)

    assert rejected.value.error_type == "unsafe_configured_path"


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO regression")
def test_audit_ready_rejects_fifo_before_hardware_runs(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    reports = tmp_path / ".agentic-hil" / "reports"
    reports.mkdir(parents=True)
    os.mkfifo(reports / "last-failure.json")
    calls: list[str] = []
    backend = SimpleNamespace(probe_target=lambda: calls.append("hardware") or {"ok": True}, close=lambda: None)
    service = AgenticHILToolService(config, backend=backend)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["error_type"] == "audit_unavailable"
    assert result["audit_ok"] is False
    assert calls == []


def test_report_pair_lock_keeps_last_files_on_same_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    from agentic_hil import report as report_module

    original_write = report_module.safe_write_text
    first_failure_started = threading.Event()
    release_first = threading.Event()
    second_write_started = threading.Event()

    def controlled_write(config, path, text, **kwargs):
        payload = json.loads(text)
        if payload["operation"] == "A" and Path(path).name == "last-failure.json":
            first_failure_started.set()
            assert release_first.wait(5)
        if payload["operation"] == "B":
            second_write_started.set()
        return original_write(config, path, text, **kwargs)

    monkeypatch.setattr(report_module, "safe_write_text", controlled_write)
    failures: list[Exception] = []

    def write(operation: str) -> None:
        try:
            write_report(config, {"ok": False, "operation": operation, "error_type": "test_failure"})
        except Exception as error:
            failures.append(error)

    first = threading.Thread(target=write, args=("A",))
    second = threading.Thread(target=write, args=("B",))
    first.start()
    assert first_failure_started.wait(5)
    second.start()
    assert not second_write_started.wait(0.1)
    release_first.set()
    first.join(5)
    second.join(5)

    assert failures == []
    assert not first.is_alive() and not second.is_alive()
    reports = tmp_path / ".agentic-hil" / "reports"
    assert json.loads((reports / "last-report.json").read_text(encoding="utf-8"))["operation"] == "B"
    assert json.loads((reports / "last-failure.json").read_text(encoding="utf-8"))["operation"] == "B"


def test_canonical_report_state_and_lock_are_outside_workspace(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)

    result = write_report(config, {"ok": False, "operation": "A", "error_type": "test_failure"})

    assert result["audit_ok"] is True
    assert not Path(report_state_path(config)).is_relative_to(tmp_path)
    assert not Path(report_lock_path(config)).is_relative_to(tmp_path)


def test_canonical_report_state_ignores_mismatched_compatibility_snapshots(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    write_report(config, {"ok": False, "operation": "A", "error_type": "test_failure"})
    reports = tmp_path / ".agentic-hil" / "reports"
    reports.joinpath("last-report.json").write_text('{"ok": false, "operation": "B"}\n', encoding="utf-8")
    reports.joinpath("last-failure.json").write_text('{"ok": false, "operation": "B"}\n', encoding="utf-8")

    assert read_last_report(config)["operation"] == "A"
    assert read_last_failure(config)["operation"] == "A"


def test_second_snapshot_failure_is_recorded_atomically_in_canonical_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    from agentic_hil import report as report_module

    original_write = report_module.safe_write_text

    def fail_last_report(config, path, text, **kwargs):
        if Path(path).name == "last-report.json":
            raise OSError("disk full")
        return original_write(config, path, text, **kwargs)

    monkeypatch.setattr(report_module, "safe_write_text", fail_last_report)

    result = write_report(config, {"ok": False, "operation": "B", "error_type": "test_failure"})

    assert result["audit_ok"] is False
    assert read_last_report(config)["operation"] == "B"
    assert read_last_report(config)["audit_ok"] is False
    assert read_last_failure(config)["operation"] == "B"
    assert read_last_failure(config)["audit_ok"] is False


def test_last_failure_write_failure_does_not_persist_successful_last_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    from agentic_hil import report as report_module

    original_write = report_module.safe_write_text

    def fail_last_failure(config, path, text, **kwargs):
        if Path(path).name == "last-failure.json":
            raise OSError("fsync failed")
        return original_write(config, path, text, **kwargs)

    monkeypatch.setattr(report_module, "safe_write_text", fail_last_failure)

    result = write_report(config, {"ok": False, "tool": "probe_target", "error_type": "probe_failed"})

    assert result["audit_ok"] is False
    assert not (tmp_path / ".agentic-hil" / "reports" / "last-report.json").exists()


def test_hardlinked_artifact_is_rejected(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    outside = tmp_path / "outside" / "firmware.elf"
    outside.parent.mkdir()
    outside.write_bytes(b"\x7fELFoutside")
    linked = tmp_path / "build" / "firmware.elf"
    linked.parent.mkdir()
    try:
        os.link(outside, linked)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")

    manager = ArtifactManager(config)
    try:
        result = manager.validate_local_path("build/firmware.elf")
    finally:
        manager.close()

    assert result["ok"] is False
    assert result["error_type"] == "artifact_validation_failed"
    assert result["validation"]["single_link"] is False


def test_artifact_is_rechecked_and_staged_before_backend_use(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELForiginal")
    manager = ArtifactManager(config)
    try:
        validation = manager.validate_local_path("build/firmware.elf")
        assert validation["ok"] is True
        firmware.write_bytes(b"\x7fELFchanged")

        changed = manager.stage_for_backend(validation["artifact"], "flash_firmware")

        assert changed["ok"] is False
        assert changed["error_type"] == "artifact_changed"
        assert list(Path(manager._staging.name).iterdir()) == []

        validation = manager.validate_local_path("build/firmware.elf")
        staged = manager.stage_for_backend(validation["artifact"], "flash_firmware")
        assert staged["ok"] is True
        staged_path = Path(staged["artifact"]["resolved_path"])
        assert not staged_path.is_relative_to(tmp_path)
        assert staged_path.read_bytes() == firmware.read_bytes()
    finally:
        manager.close()


def test_backend_staging_rejects_artifact_above_limit(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "firmware.bin"
    firmware.parent.mkdir()
    firmware.write_bytes(b"x" * (1024 * 1024 + 1))
    manager = ArtifactManager(config)
    try:
        validation = manager.validate_local_path("build/firmware.bin")
    finally:
        manager.close()

    assert validation["ok"] is False
    assert validation["tool"] == "flash_firmware"
    assert validation["error_type"] == "artifact_too_large"


def test_backend_staging_io_error_is_structured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "firmware.bin"
    firmware.parent.mkdir()
    firmware.write_bytes(b"approved")
    manager = ArtifactManager(config)
    validation = manager.validate_local_path("build/firmware.bin")
    monkeypatch.setattr("agentic_hil.artifacts.tempfile.mkdtemp", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    try:
        result = manager.stage_for_backend(validation["artifact"], "debug_start_session")
    finally:
        manager.close()

    assert result["ok"] is False
    assert result["tool"] == "debug_start_session"
    assert result["error_type"] == "artifact_staging_failed"
    assert result["backend_error"] == "disk full"


def test_local_artifact_upload_rechecks_source_before_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "firmware.bin"
    firmware.parent.mkdir()
    firmware.write_bytes(b"approved")
    manager = ArtifactManager(config)
    validate = manager.validate_local_path

    def replace_after_validation(path: str):
        result = validate(path)
        firmware.write_bytes(b"replacement")
        return result

    monkeypatch.setattr(manager, "validate_local_path", replace_after_validation)
    try:
        result = manager._upload_local_path("build/firmware.bin")
    finally:
        manager.close()

    assert result["ok"] is False
    assert result["error_type"] == "artifact_changed"


def test_upload_does_not_remove_existing_backend_stage(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "firmware.bin"
    firmware.parent.mkdir()
    firmware.write_bytes(b"approved")
    manager = ArtifactManager(config)
    try:
        validation = manager.validate_local_path("build/firmware.bin")
        staged = manager.stage_for_backend(validation["artifact"], "debug_start_session")
        staged_path = Path(staged["artifact"]["resolved_path"])

        uploaded = manager._upload_local_path("build/firmware.bin")

        assert uploaded["ok"] is True
        assert staged_path.read_bytes() == b"approved"
    finally:
        manager.close()


def test_artifact_stage_rejects_ancestor_symlink_pivot(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFsame-content")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / firmware.name).write_bytes(firmware.read_bytes())
    manager = ArtifactManager(config)
    try:
        validation = manager.validate_local_path("build/firmware.elf")
        original_build = tmp_path / "original-build"
        firmware.parent.rename(original_build)
        try:
            (tmp_path / "build").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"directory symlinks unavailable: {error}")

        staged = manager.stage_for_backend(validation["artifact"], "flash_firmware")

        assert staged["ok"] is False
        # The pivoted ancestor resolves elsewhere, so the guarded read answers
        # with the same sentence a redirected workspace gets. This one is still
        # the mid-swap the retryable answer was written for: the workspace the
        # configuration names resolves to itself, and what moved was a directory
        # under it, so re-validating really is the way out.
        assert staged["error_type"] == "artifact_changed"
        assert staged["retry_safe"] is True
    finally:
        manager.close()


def test_artifact_stage_carries_a_redirected_workspace_refusal_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal no retry can lift must not be dressed up as one that can.

    The profile is the packaged agent host: the project's own tree names one
    place and resolves into the package's private cache, so the guarded read
    refuses every artifact under it for as long as the configuration names that
    spelling. Answered as `artifact_changed` with `retry_safe: true`, that sends
    a caller around a loop with no way out, because there is nothing about the
    file to rebuild or re-validate.

    Validated first and redirected afterwards, because validation goes through
    the same guarded read and turns a workspace that is already redirected down
    on its own. What reaches staging is therefore a redirection that begins after
    validation, or an artifact a caller staged without validating it under this
    configuration, and both of those are as permanent as the static case.
    """
    config = load_test_config(tmp_path)
    workspace = Path(config.work_dir)
    firmware = workspace / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFapproved")
    backing = workspace.parent / "package-local-cache"
    manager = ArtifactManager(config)
    try:
        validation = manager.validate_local_path("build/firmware.elf")
        assert validation["ok"] is True
        virtualize(monkeypatch, workspace, backing)

        staged = manager.stage_for_backend(validation["artifact"], "flash_firmware")
    finally:
        manager.close()

    assert staged["ok"] is False
    assert staged["tool"] == "flash_firmware"
    assert staged["error_type"] == "unsafe_configured_path"
    assert staged["retry_safe"] is False
    # Both spellings, because either one alone leaves the reader walking a chain
    # that shows nothing: no reparse point, no symlink, link count one. The
    # configured path is what the file names and the resolved one is the answer.
    assert staged["path"] == str(firmware)
    assert staged["resolved_parent"] == str(backing / "build")
    # Carried as the refusal itself rather than restated here, so the fix the
    # error catalogue serves for this type comes with it and cannot drift from
    # what the refusal says.
    assert staged["remediation"] == ConfigError("unsafe_configured_path", staged["summary"]).to_dict()["remediation"]
    # Still a refusal that happened before the backend ran, so it must not be
    # recorded as an unconfirmed hardware effect over a file that never reached
    # the board.
    assert staged["side_effect_committed"] is False
    assert staged["side_effect_status"] == "not_started"


def test_artifact_stage_still_answers_a_swapped_file_as_retryable(tmp_path: Path) -> None:
    """The case the retryable answer was written for keeps it.

    Holds both ways. A file replaced between validation and staging is refused by
    the same guarded read and by the same error type the redirected workspace
    raises, and here the artifact really did change: build it again, validate it
    again, and the next attempt goes through. Narrowing the permanent answer to
    the configured workspace is what keeps these two apart.
    """
    config = load_test_config(tmp_path)
    workspace = Path(config.work_dir)
    firmware = workspace / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFapproved")
    manager = ArtifactManager(config)
    try:
        validation = manager.validate_local_path("build/firmware.elf")
        assert validation["ok"] is True
        firmware.unlink()
        firmware.mkdir()

        staged = manager.stage_for_backend(validation["artifact"], "flash_firmware")
    finally:
        manager.close()

    assert staged["ok"] is False
    assert staged["error_type"] == "artifact_changed"
    assert staged["retry_safe"] is True
    assert staged["side_effect_committed"] is False
    assert Path(config.work_dir).resolve() == Path(config.work_dir), "the workspace is the healthy one"


def test_flash_releases_private_backend_stage(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFfake")
    service = AgenticHILToolService(config)
    staging_root = Path(service.artifacts._staging.name)
    try:
        result = service.call("flash_firmware", {"image_path": "build/firmware.elf"})

        assert result["ok"] is True
        assert list(staging_root.iterdir()) == []
    finally:
        service.close()


def test_canonical_audit_log_written_under_state_root(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    workspace_log = str(Path(logs_directory(config)) / "com-effect.jsonl")

    assert append_jsonl(workspace_log, {"direction": "tx", "hex": "01"}, config) is None

    canonical = canonical_audit_log_path(config, workspace_log)
    assert canonical.exists()
    workspace_root = Path(config.workspace_root).resolve()
    assert workspace_root not in canonical.resolve().parents
    evidence = canonical_audit_evidence(config, workspace_log)
    assert evidence["canonical_log_present"] is True
    assert evidence["log_sequence"] == 1
    assert evidence["workspace_log_verified"] is True


def test_workspace_audit_log_tampering_is_detected(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    workspace_log = str(Path(logs_directory(config)) / "com-effect.jsonl")
    assert append_jsonl(workspace_log, {"direction": "tx", "hex": "0a0b"}, config) is None
    canonical = canonical_audit_log_path(config, workspace_log)
    canonical_before = canonical.read_bytes()

    # The agent is assumed able to edit any workspace file; rewrite the log.
    Path(workspace_log).write_text('{"direction": "tx", "hex": "ffff"}\n', encoding="utf-8")
    tampered = canonical_audit_evidence(config, workspace_log)
    assert tampered["canonical_log_present"] is True
    assert tampered["workspace_log_verified"] is False
    assert canonical.read_bytes() == canonical_before

    # Deletion is likewise detected, canonical evidence still intact.
    Path(workspace_log).unlink()
    deleted = canonical_audit_evidence(config, workspace_log)
    assert deleted["canonical_log_present"] is True
    assert deleted["workspace_log_verified"] is False
    assert canonical.read_bytes() == canonical_before


def test_canonical_evidence_tolerates_extra_passive_workspace_lines(tmp_path: Path) -> None:
    # Effect events are mirrored canonically; passive COM RX feedback is written
    # to the workspace log only. Verification must treat the canonical log as an
    # ordered subset of the workspace log, not require byte equality.
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    workspace_log = str(Path(logs_directory(config)) / "com-mixed.jsonl")
    assert append_jsonl(workspace_log, {"event": "start"}, config) is None  # mirrored
    assert append_jsonl(workspace_log, {"direction": "rx", "hex": "aa"}) is None  # workspace only
    assert append_jsonl(workspace_log, {"direction": "tx", "hex": "bb"}, config) is None  # mirrored
    assert append_jsonl(workspace_log, {"direction": "rx", "hex": "cc"}) is None  # workspace only

    evidence = canonical_audit_evidence(config, workspace_log)
    assert evidence["canonical_log_present"] is True
    assert evidence["workspace_log_verified"] is True

    # Deleting a mirrored effect line is still detected despite the RX lines.
    lines = [line for line in Path(workspace_log).read_text(encoding="utf-8").splitlines() if '"tx"' not in line]
    Path(workspace_log).write_text("\n".join(lines) + "\n", encoding="utf-8")
    tampered = canonical_audit_evidence(config, workspace_log)
    assert tampered["workspace_log_verified"] is False


def test_canonical_evidence_detects_reordered_effect_lines(tmp_path: Path) -> None:
    # Reordering effect records breaks the ordered-subsequence check, so an
    # attacker cannot hide an effect by shuffling the workspace log.
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    workspace_log = str(Path(logs_directory(config)) / "com-reorder.jsonl")
    assert append_jsonl(workspace_log, {"event": "start", "seq": 1}, config) is None
    assert append_jsonl(workspace_log, {"direction": "tx", "seq": 2}, config) is None
    assert append_jsonl(workspace_log, {"event": "stop", "seq": 3}, config) is None

    original = [line for line in Path(workspace_log).read_text(encoding="utf-8").splitlines() if line]
    reordered = [original[2], original[0], original[1]]
    Path(workspace_log).write_text("\n".join(reordered) + "\n", encoding="utf-8")

    assert canonical_audit_evidence(config, workspace_log)["workspace_log_verified"] is False


def test_canonical_audit_sequence_is_monotonic_under_parallel_appends(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    workspace_log = str(Path(logs_directory(config)) / "com-parallel.jsonl")
    total = 40

    def append(index: int) -> Exception | None:
        return append_canonical_audit_log(config, workspace_log, json.dumps({"seq": index}) + "\n")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(append, range(total)))

    assert all(result is None for result in results)
    evidence = canonical_audit_evidence(config, workspace_log)
    assert evidence["log_sequence"] == total
    canonical = canonical_audit_log_path(config, workspace_log)
    lines = [line for line in canonical.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == total


def test_report_api_surfaces_canonical_audit_evidence(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    workspace_log = str(Path(logs_directory(config)) / "com-report.jsonl")
    assert append_jsonl(workspace_log, {"direction": "tx", "hex": "01"}, config) is None
    from agentic_hil.config import display_path

    result = {"ok": True, "tool": "com_write", "log_path": display_path(config, workspace_log)}
    enriched = attach_canonical_audit_evidence(config, result)
    assert enriched["canonical_audit"]["workspace_log_verified"] is True

    Path(workspace_log).write_text("tampered\n", encoding="utf-8")
    retampered = attach_canonical_audit_evidence(config, result)
    assert retampered["canonical_audit"]["workspace_log_verified"] is False


def test_unbound_debugger_refusal_does_not_demand_an_impossible_argument(tmp_path: Path) -> None:
    # No MCP tool schema carries a probe name, so a refusal telling the caller to
    # "name the debugger" would ask for something the protocol cannot express —
    # the shape of refusal this codebase records as having driven a model to
    # reach for st-flash instead.
    config = load_test_config(
        tmp_path,
        probe_id="PROBE-A",
        debuggers_yaml='debuggers:\n  probe_b:\n    type: openocd\n    probe_id: "PROBE-B"\n',
    )
    assert config.debugger is None
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "not_supported"
    assert result["retry_safe"] is False
    assert result["side_effect_committed"] is False
    assert "test-reactor" in result["summary"]
    assert sorted(result["configured_debuggers"]) == ["dut", "probe_b"]
    assert validate_tool_arguments("probe_target", {"debugger": "dut"}) is not None


def test_zero_debugger_refusal_does_not_claim_several_are_configured(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(re.sub(r"(?ms)^debuggers:\n(?:  .*\n)+", "debuggers: {}\n", text), encoding="utf-8")
    config = load_config(str(config_path))
    assert config.debuggers == {}
    service = AgenticHILToolService(config)
    try:
        result = service.call("debugger_info")
    finally:
        service.close()

    assert result["error_type"] == "not_supported"
    assert result["configured_debuggers"] == []
    assert "several" not in result["summary"]
    assert result["retry_safe"] is False


def test_staging_refusal_before_any_backend_call_does_not_quarantine_the_lease(tmp_path: Path) -> None:
    # require_existing_file: false lets validation pass on a file that is not
    # there yet, so nothing about those bytes was checked. Staging must refuse —
    # but the refusal happened before the backend ran, so it must not be
    # recorded as an unconfirmed hardware effect and poison the bench.
    config_path = write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace("logs:\n", "validation:\n  require_existing_file: false\nlogs:\n", 1), encoding="utf-8")
    config = load_config(str(config_path))
    service = AgenticHILToolService(config)
    try:
        (tmp_path / "build").mkdir(parents=True, exist_ok=True)
        validated = service.artifacts.validate_local_path("build/app.elf")
        assert validated["ok"] is True
        assert validated["artifact"]["integrity_sha256"] is None

        (tmp_path / "build" / "app.elf").write_bytes(b"\x7fELF" + b"\x00" * 12)
        staged = service.artifacts.stage_for_backend(validated["artifact"], "flash_firmware")

        assert staged["ok"] is False
        assert staged["error_type"] == "artifact_changed"
        assert staged["side_effect_committed"] is False
        assert service._result_requires_quarantine(staged) is False
    finally:
        service.close()


def test_dump_output_path_outside_the_workspace_is_refused_up_front(tmp_path: Path) -> None:
    # The mirror of the artifact read check: without it a symlinked directory
    # inside the workspace only failed after the plan had already flashed and
    # halted the board.
    config = load_test_config(tmp_path)
    service = AgenticHILToolService(config)
    try:
        outside = tmp_path.parent / "escaped.hex"
        result = service.artifacts.validate_output_path(str(outside), "debug_dump_symbol_ihex")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "output_validation_failed"
    assert result["validation"]["within_workspace"] is False
    assert "workspace_root" in result["summary"]


def test_two_debuggers_naming_one_probe_id_are_rejected_despite_distinct_resource_ids(tmp_path: Path) -> None:
    # resource_id only renames the coordination lease. probe_id is what the
    # backends turn into `adapter serial` / `sn=` / `--uid`, so two entries on
    # one serial are one board however their leases are named.
    with pytest.raises(ConfigError) as rejected:
        load_config(
            str(
                write_config(
                    tmp_path,
                    debugger_name="board_a",
                    probe_id="SN-001",
                    debuggers_yaml='debuggers:\n  board_b:\n    type: openocd\n    probe_id: "SN-001"\n    resource_id: "bench-b"\n',
                )
            )
        )

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "debuggers.board_b.probe_id"
    assert rejected.value.details["other_debugger"] == "board_a"


def test_classify_last_error_works_without_any_debugger(tmp_path: Path) -> None:
    # The failure record is written by whichever tool failed, so a project with
    # no probe must still be able to ask what went wrong.
    config_path = write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(re.sub(r"(?ms)^debuggers:\n(?:  .*\n)+", "debuggers: {}\n", text), encoding="utf-8")
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        service.call("com_session_start", {"port_id": "dut_uart"})
        result = service.call("classify_last_error")
    finally:
        service.close()

    assert result["error_type"] != "not_supported"
    assert result["tool"] == "classify_last_error"


def test_write_only_com_port_can_actually_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # allow_write without allow_read used to be a grant that could never be
    # used: the session is the only way to reach the port, and opening it was
    # gated on allow_read alone.
    config = load_test_config(
        tmp_path,
        com_ports_yaml='com_ports:\n  reset_line:\n    device: "COM_TEST"\n    permissions:\n      allow_read: false\n      allow_write: true\n',
    )
    service = ComPortService(config)
    written: list[bytes] = []
    monkeypatch.setattr(
        service,
        "_open_serial",
        lambda port_id, port_config, log_path, lease, contact: {
            "ok": True,
            "session": ComPortSession(port_id, port_config, _RecordingSerial(written), log_path, lease, start_reader=False, contact=contact),
        },
    )
    try:
        started = service.session_start("reset_line")
        assert started["ok"] is True, started
        assert service.sessions["reset_line"].reader is None

        result = service.write("reset_line", {"text": "reset\n"})
        assert result["ok"] is True, result
        assert written == [b"reset\n"]

        denied = service.read("reset_line")
        assert denied["error_type"] == "permission_denied"
    finally:
        service.close()


class _RecordingSerial:
    is_open = True
    in_waiting = 0

    def __init__(self, sink: list[bytes]) -> None:
        self._sink = sink

    def write(self, data: bytes) -> int:
        self._sink.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def read(self, size: int) -> bytes:
        return b""

    def close(self) -> None:
        self.is_open = False


def test_artifact_that_never_existed_is_reported_as_not_found(tmp_path: Path) -> None:
    # With require_existing_file: false the file may be absent at validation.
    # Reporting that as "changed after validation" sends the caller back to
    # re-validate, which keeps succeeding; the artifact has to be built.
    config_path = write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace("logs:\n", "validation:\n  require_existing_file: false\nlogs:\n", 1), encoding="utf-8")
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        (tmp_path / "build").mkdir(parents=True, exist_ok=True)
        validated = service.artifacts.validate_local_path("build/app.elf")
        assert validated["ok"] is True

        staged = service.artifacts.stage_for_backend(validated["artifact"], "flash_firmware")
    finally:
        service.close()

    assert staged["error_type"] == "artifact_not_found"
    assert "Build it first" in staged["summary"]
    assert staged["side_effect_committed"] is False


def test_probe_discovery_without_any_debugger_does_not_blame_a_permission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing is switched off when nothing is configured, and the CLI must not
    # contradict the MCP surface for the same config.
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch)
    config_path = Path(os.environ["AGENTIC_HIL_CONFIG"])
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(re.sub(r"(?ms)^debuggers:\n(?:  .*\n)+", "debuggers: {}\n", text), encoding="utf-8")
    monkeypatch.chdir(workspace)

    result = debugger_probes()

    assert result["ok"] is False
    assert result["error_type"] == "not_supported"
    assert result["configured_debuggers"] == []
    assert "permission" not in result["summary"].lower()


def test_multi_probe_discovery_fails_when_any_probe_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # One probe answering must not report the run as healthy while another
    # failed: the CLI exit code is derived from this verdict alone, and a
    # containment marker on any probe has to reach the top level.
    from agentic_hil import cli as cli_module

    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_name="probe_a",
        probe_id="PROBE-A",
        debuggers_yaml=(
            'debuggers:\n  probe_b:\n    type: openocd\n    probe_id: "PROBE-B"\n'
            f'    executable: "{FAKE_OPENOCD.as_posix()}"\n'
        ),
    )
    monkeypatch.chdir(workspace)

    answers = {
        "probe_a": {"ok": True, "tool": "debugger_probes_list", "probes": []},
        "probe_b": {"ok": False, "tool": "debugger_probes_list", "error_type": "debugger_not_found", "cleanup_required": True, "quarantined": True, "quarantine_id": "q-1"},
    }

    class FakeService:
        def __init__(self, config, *args, **kwargs) -> None:
            self._name = config.debugger_id

        def call(self, name: str, arguments: dict | None = None) -> dict:
            return answers[self._name]

        def close(self) -> None:
            return None

    monkeypatch.setattr(cli_module, "AgenticHILToolService", FakeService)

    result = cli_module.debugger_probes()

    assert result["ok"] is False
    assert "probe_b" in result["summary"]
    assert result["cleanup_required"] is True
    assert result["quarantined"] is True
    assert result["quarantine_id"] == "q-1"
    assert overall_success(result) is False


def test_debug_load_refusal_names_the_permission_that_fired(tmp_path: Path) -> None:
    # Preflight is the only diagnosis on this path — the backend's per-flag
    # messages are never reached — so it must not point at a flag the operator
    # can see is false.
    from agentic_hil.test_reactor import TestConfig, TestReactor, TestStep

    config = load_test_config(tmp_path, permissions={"allow_probe": True, "allow_reset": True, "allow_flash": True, "allow_mass_erase": True})
    firmware = tmp_path / "build" / "app.elf"
    firmware.parent.mkdir(parents=True, exist_ok=True)
    firmware.write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = AgenticHILToolService(config)
    try:
        plan = TestConfig(path=str(tmp_path / "plan.yaml"), name="load", steps=[TestStep("debug_start", {"image_path": "build/app.elf", "mode": "load"}, debugger="dut")])
        result = TestReactor(config, service).run(plan)
    finally:
        service.close()

    assert result["ok"] is False
    assert result["validation_error"]["permission"] == "allow_mass_erase"
    assert "mass erase" in result["validation_error"]["summary"]


def test_artifact_outside_the_workspace_is_refused_with_the_real_reason(tmp_path: Path) -> None:
    # With require_allowed_root off, the allowed-roots refusal no longer fires
    # first and this is the path that used to answer "must be a single-link
    # regular file" with a backend_error about an *output* file. No permission
    # relaxes workspace containment, so a caller told "regular file" keeps
    # retrying a path that can never work.
    config_path = write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace("logs:\n", "validation:\n  require_allowed_root: false\nlogs:\n", 1), encoding="utf-8")
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        outside = tmp_path.parent / "elsewhere.elf"
        outside.write_bytes(b"\x7fELF" + b"\x00" * 12)
        result = service.artifacts.validate_local_path(str(outside))
    finally:
        service.close()

    assert result["ok"] is False
    assert result["validation"]["within_workspace"] is False
    assert "workspace_root" in result["summary"]
    assert "single-link regular file" not in result["summary"]


def test_artifact_validation_names_a_redirected_workspace_rather_than_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal a caller can act on, instead of one about the wrong thing.

    The profile is the packaged agent host: the project's own tree names one
    place and resolves into the package's private cache, so the guarded read
    refuses every artifact under it for as long as the configuration names that
    spelling. Folded into "must be a single-link regular file that can be opened
    safely", that sends an operator to inspect a file whose type is perfectly
    fine, while the sentence naming the tree sits in `backend_error` where the
    summary contradicts it. There is nothing about the file to fix, and no
    permission and no rebuild lifts it.
    """
    config = load_test_config(tmp_path)
    workspace = Path(config.work_dir)
    firmware = workspace / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFapproved")
    backing = workspace.parent / "package-local-cache"
    manager = ArtifactManager(config)
    try:
        virtualize(monkeypatch, workspace, backing)
        result = manager.validate_local_path("build/firmware.elf")
    finally:
        manager.close()

    assert result["ok"] is False
    assert result["tool"] == "flash_firmware"
    assert result["error_type"] == "unsafe_configured_path"
    assert "single-link regular file" not in result["summary"]
    assert result["retry_safe"] is False
    # Both spellings, because either one alone leaves the reader walking a chain
    # that shows nothing: no reparse point, no symlink, link count one. The
    # configured path is what the file is named by and the resolved one is the
    # answer, being the spelling that works.
    assert result["path"] == str(firmware)
    assert result["resolved_parent"] == str(backing / "build")
    # Carried as the refusal itself rather than restated here, so the fix the
    # error catalogue serves for this type comes with it and cannot drift from
    # what the refusal says.
    assert result["remediation"] == ConfigError("unsafe_configured_path", result["summary"]).to_dict()["remediation"]
    # The read was refused before it reached the object, so nothing was learned
    # about the file and nothing is claimed about it.
    assert "regular_file" not in result["validation"]
    assert "single_link" not in result["validation"]
    # Nothing was started, which is what keeps this out of quarantine now that
    # the type is no longer one of the exempt ones.
    assert result["side_effect_committed"] is False
    assert result["side_effect_status"] == "not_started"


def test_artifact_validation_still_answers_a_file_that_is_not_one_as_before(tmp_path: Path) -> None:
    """The case the folded summary was written for keeps it.

    Holds both ways. Under a workspace that resolves to itself, a directory
    standing where the firmware should be is refused by the same guarded read
    with the same error type a redirected workspace raises, and here the summary
    is right: the object at that path is not the object it has to be, and that is
    what a caller has to go and change. Narrowing the permanent answer to the
    configured workspace is what keeps these two apart.
    """
    config = load_test_config(tmp_path)
    workspace = Path(config.work_dir)
    (workspace / "build" / "firmware.elf").mkdir(parents=True)
    assert Path(config.work_dir).resolve() == Path(config.work_dir), "the workspace is the healthy one"
    service = AgenticHILToolService(config)
    try:
        result = service.artifacts.validate_local_path("build/firmware.elf")

        assert result["ok"] is False
        assert result["error_type"] == "artifact_validation_failed"
        assert "single-link regular file" in result["summary"]
        assert result["validation"]["regular_file"] is False
        assert result["validation"]["single_link"] is False
        assert result["validation"]["backend_error"]
        # The exemption this type has always had, and the reason it stays: this
        # refusal carries no side-effect markers of its own.
        assert "side_effect_committed" not in result
        assert service._result_requires_quarantine(result) is False
    finally:
        service.close()


def test_a_redirected_workspace_refusal_does_not_quarantine_the_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The entanglement the changed error type walks into, settled.

    `artifact_validation_failed` is in `_result_requires_quarantine`'s exempt
    set, so every refusal validation gave used to be spared by name.
    `unsafe_configured_path` is not in that set, and must not be added to it: the
    set outranks even `side_effect_status: unknown`, and that type is raised by
    output writes and ledger appends too, where a side effect may well be in
    flight. So this refusal earns its exemption the narrow way, by saying what is
    true of it: it happens before the backend is invoked and nothing was started.
    Without that, a firmware path under a virtualized `%LOCALAPPDATA%` would put
    the bench into recovery over an image that never reached the board.
    """
    config = load_test_config(tmp_path)
    workspace = Path(config.work_dir)
    firmware = workspace / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFapproved")
    service = AgenticHILToolService(config)
    try:
        virtualize(monkeypatch, workspace, workspace.parent / "package-local-cache")
        result = service.artifacts.validate_local_path("build/firmware.elf")
        monkeypatch.undo()

        assert result["error_type"] == "unsafe_configured_path"
        assert service._result_requires_quarantine(result) is False
        # The markers are what does it, and this is the shape the exempt set
        # would have had to cover instead: the same refusal with nothing said
        # about what was started quarantines on its type alone.
        assert service._result_requires_quarantine({key: value for key, value in result.items() if not key.startswith("side_effect")}) is True
    finally:
        service.close()


def test_the_validation_quarantine_exemption_stayed_with_the_marker(tmp_path: Path) -> None:
    """Why the exempt set did not move, stated as the two answers it gives.

    Holds both ways, and pins the choice. Exempting `unsafe_configured_path` by
    name would have been the smaller diff and the wrong claim: membership of that
    set is asserted of every result that ever carries the type, and it is checked
    before `side_effect_status`, so an unknown hardware effect answered with that
    type would stop quarantining. The type is general, raised wherever a
    configured path is refused, including writes that happen after a target has
    been contacted. `artifact_validation_failed` keeps its place because nothing
    but artifact validation produces it and validation runs before any backend
    call.
    """
    service = AgenticHILToolService(load_test_config(tmp_path))
    try:
        assert service._result_requires_quarantine({"error_type": "unsafe_configured_path"}) is True
        assert service._result_requires_quarantine({"error_type": "unsafe_configured_path", "side_effect_status": "unknown"}) is True
        assert (
            service._result_requires_quarantine(
                {"error_type": "unsafe_configured_path", "side_effect_committed": False, "side_effect_status": "not_started"}
            )
            is False
        )
        assert service._result_requires_quarantine({"error_type": "artifact_validation_failed", "side_effect_status": "unknown"}) is False
    finally:
        service.close()


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are a Windows construct")
def test_artifact_behind_a_link_that_leaves_the_workspace_is_named_too(tmp_path: Path) -> None:
    # A lexical containment check reports this path as contained, so the
    # refusal fell back to the misleading "single-link regular file" wording on
    # exactly the case where naming the reason matters most.
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    (outside / "app.elf").write_bytes(b"\x7fELF" + b"\x00" * 12)
    config_path = write_config(workspace)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace("logs:\n", "validation:\n  require_allowed_root: false\nlogs:\n", 1), encoding="utf-8")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(workspace / "build"), str(outside)], check=True, capture_output=True)

    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        result = service.artifacts.validate_local_path("build/app.elf")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["validation"]["within_workspace"] is False
    assert "workspace_root" in result["summary"]


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are a Windows construct")
def test_dump_into_an_allowed_root_that_is_a_link_is_not_falsely_refused(tmp_path: Path) -> None:
    # validate_output_path resolves symlinks while the allowed roots are built
    # lexically, so an allowed root that is itself a link inside the workspace
    # refused every legitimate dump into it.
    workspace = tmp_path / "ws"
    config_path = write_config(workspace)
    (workspace / "real").mkdir(parents=True, exist_ok=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(workspace / "build"), str(workspace / "real")], check=True, capture_output=True)

    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        result = service.artifacts.validate_output_path("build/dump.hex", "debug_dump_symbol_ihex")
    finally:
        service.close()

    assert result["ok"] is True, result
    assert result["validation"]["allowed_root"] is True
    assert result["validation"]["within_workspace"] is True


def _without_the_root_check(config_path: Path) -> Path:
    """Switch `validation.require_allowed_root` off in a written config.

    The widest a configuration can be. What still refuses after this is what was
    never the root list's job in the first place."""
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace("logs:\n", "validation:\n  require_allowed_root: false\nlogs:\n", 1), encoding="utf-8")
    return config_path


def _link_directory(link: Path, target: Path) -> None:
    """A directory link at `link` pointing at `target`, on either platform."""
    if os.name == "nt":
        # A junction needs no privilege; a Windows symlink does.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_an_artifact_anywhere_in_the_workspace_is_accepted(tmp_path: Path) -> None:
    # The case the whole change is for: CLion builds into cmake-build-debug,
    # PlatformIO into .pio/build/<env>. Neither is "build", and with "." neither
    # has to be named before the first flash.
    config_path = write_config(tmp_path, allowed_roots=["."])
    firmware = tmp_path / "cmake-build-debug" / "nested" / "app.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        result = service.artifacts.validate_local_path("cmake-build-debug/nested/app.elf")
    finally:
        service.close()

    assert result["ok"] is True, result
    assert result["validation"]["allowed_root"] is True
    assert result["validation"]["within_workspace"] is True
    # Nothing else was skipped on the way: the content was still read and hashed.
    assert result["validation"]["sha256_computed"] is True
    assert result["validation"]["elf_header"] is True


def test_the_whole_workspace_is_still_only_the_workspace(tmp_path: Path) -> None:
    # With the root list at its widest *and* switched off, containment is the
    # only thing left holding — which is the claim this change rests on.
    workspace = tmp_path / "ws"
    config_path = _without_the_root_check(write_config(workspace, allowed_roots=["."]))
    outside = tmp_path / "elsewhere.elf"
    outside.write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        result = service.artifacts.validate_local_path(str(outside))
        traversed = service.artifacts.validate_local_path("../elsewhere.elf")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["validation"]["within_workspace"] is False
    assert "workspace_root" in result["summary"]
    assert traversed["ok"] is False
    assert traversed["validation"]["path_traversal_safe"] is False


def test_a_link_out_of_the_whole_workspace_is_still_refused(tmp_path: Path) -> None:
    # A build directory that is a link to somewhere else is contained lexically
    # and not in fact. Widening the roots must not make that the accepted case.
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    (outside / "app.elf").write_bytes(b"\x7fELF" + b"\x00" * 12)
    config_path = _without_the_root_check(write_config(workspace, allowed_roots=["."]))
    _link_directory(workspace / "cmake-build-debug", outside)

    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        result = service.artifacts.validate_local_path("cmake-build-debug/app.elf")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["validation"]["within_workspace"] is False
    assert "workspace_root" in result["summary"]


def test_the_whole_workspace_still_refuses_a_foreign_extension(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, allowed_roots=["."])
    artifact = tmp_path / "cmake-build-debug" / "notes.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        result = service.artifacts.validate_local_path("cmake-build-debug/notes.txt")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["validation"]["allowed_extension"] is False
    assert "extension" in result["summary"]


def test_the_whole_workspace_still_inspects_the_format(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, allowed_roots=["."])
    artifact = tmp_path / "cmake-build-debug" / "app.elf"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"this is not an ELF file")
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        result = service.artifacts.validate_local_path("cmake-build-debug/app.elf")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["validation"]["elf_header"] is False
    assert "plausibility" in result["summary"]


def test_a_configuration_that_names_its_roots_keeps_exactly_those(tmp_path: Path) -> None:
    # The anti-widening rule, from the operator's side: a file saying "build"
    # still means build, and a build directory of another shape is still refused.
    config_path = write_config(tmp_path, allowed_roots=["build"])
    config = load_config(str(config_path))
    assert config.artifacts.allowed_roots == ["build"]

    for name in ("build", "cmake-build-debug"):
        artifact = tmp_path / name / "app.elf"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = AgenticHILToolService(config)
    try:
        named = service.artifacts.validate_local_path("build/app.elf")
        unnamed = service.artifacts.validate_local_path("cmake-build-debug/app.elf")
    finally:
        service.close()

    assert named["ok"] is True, named
    assert unnamed["ok"] is False
    assert unnamed["validation"]["allowed_root"] is False


def test_a_configuration_that_never_named_the_roots_still_means_build(tmp_path: Path) -> None:
    # The other half of the same rule: what the *absence* of the key means may
    # not change either, or every file written before this release widens on
    # update without anyone editing it.
    config = load_config(str(write_config(tmp_path, omit_allowed_roots=True)))
    assert config.artifacts.allowed_roots == ["build"]


def test_an_empty_allowed_roots_list_is_refused_by_name(tmp_path: Path) -> None:
    # An empty list is the other plausible spelling for "the whole project", and
    # here it has always meant the opposite. It is refused rather than left to
    # mean whichever of the two the reader assumed.
    config_path = write_config(tmp_path, allowed_roots=[])

    with pytest.raises(ConfigError) as excinfo:
        load_config(str(config_path))

    assert excinfo.value.error_type == "config_invalid"
    assert excinfo.value.details["field"] == "artifacts.allowed_roots"
    assert '["."]' in excinfo.value.details["migration"]["whole_workspace"]


def test_widening_the_roots_does_not_move_where_uploads_are_stored(tmp_path: Path) -> None:
    # allowed_roots also governs the upload path. Widening it must not relocate
    # the store: that is artifacts.upload_directory and nothing else.
    config_path = write_config(tmp_path, allowed_roots=["."])
    firmware = tmp_path / "cmake-build-debug" / "app.bin"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x01\x02\x03\x04")
    config = load_config(str(config_path))
    service = AgenticHILToolService(config)
    try:
        result = service.artifacts.upload({"image_path": "cmake-build-debug/app.bin"})
    finally:
        service.close()

    assert result["ok"] is True, result
    assert Path(result["artifact"]["resolved_path"]).parent == Path(config.work_dir) / ".agentic-hil" / "artifacts"


def test_a_dump_into_the_whole_workspace_still_obeys_the_extension(tmp_path: Path) -> None:
    # The same list decides where a debug dump may land. It moves with the
    # artifact roots by design; the extension check on that path does not.
    config_path = write_config(tmp_path, allowed_roots=["."])
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        accepted = service.artifacts.validate_output_path("cmake-build-debug/memory.hex", "debug_dump_symbol_ihex")
        refused = service.artifacts.validate_output_path("cmake-build-debug/memory.elf", "debug_dump_symbol_ihex")
    finally:
        service.close()

    assert accepted["ok"] is True, accepted
    assert accepted["validation"]["allowed_root"] is True
    assert refused["ok"] is False
    assert refused["validation"]["allowed_extension"] is False


def _pyocd_service(tmp_path: Path, probe_id: str | None):
    config = load_test_config(tmp_path, debugger_type="pyocd", probe_id=probe_id)
    return AgenticHILToolService(config)


def test_pyocd_probe_selector_is_canonicalized_to_a_full_uid(tmp_path: Path) -> None:
    # pyOCD matches --uid as a case-insensitive SUBSTRING, so a partial value
    # must be resolved against the enumerated probes and the full UID passed on,
    # or the configured name does not mean one board.
    service = _pyocd_service(tmp_path, "pyocd123")
    try:
        result = service.call("probe_target")
        assert result["ok"] is True, result
        assert service.backend._resolved_probe_uid == "PYOCD123"
        assert service.backend._connection_args()[:2] == ["--uid", "PYOCD123"]
    finally:
        service.close()


def test_pyocd_ambiguous_probe_selector_is_refused_before_touching_a_board(tmp_path: Path) -> None:
    # "PYOCD" is a substring of both connected probes, so pyOCD would silently
    # take the first one.
    service = _pyocd_service(tmp_path, "PYOCD")
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "adapter_not_found"
    assert result["side_effect_committed"] is False
    assert sorted(result["candidate_probe_ids"]) == ["PYOCD123", "PYOCD456"]


def test_pyocd_probe_selector_that_matches_nothing_is_refused(tmp_path: Path) -> None:
    service = _pyocd_service(tmp_path, "NOT-CONNECTED")
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "adapter_not_found"
    assert result["side_effect_committed"] is False
    assert result["configured_probe_id"] == "NOT-CONNECTED"


def test_pyocd_without_a_probe_id_passes_no_uid(tmp_path: Path) -> None:
    service = _pyocd_service(tmp_path, None)
    try:
        assert service.call("probe_target")["ok"] is True
        assert "--uid" not in service.backend._connection_args()
    finally:
        service.close()


def test_pyocd_refuses_two_debuggers_that_resolve_to_one_physical_probe(tmp_path: Path) -> None:
    # "OCD1" and "123" share no substring relationship with each other, so
    # config.validate_debuggers accepts both at load time (it only rejects a
    # selector contained in another). Enumerated against the fake's two
    # boards they both land on PYOCD123 alone and nothing else, which only
    # the hardware boundary can see (#278).
    from agentic_hil.config import bind_debugger

    config = bind_debugger(
        load_test_config(
            tmp_path,
            debugger_type="pyocd",
            debugger_name="probe_x",
            probe_id="OCD1",
            auto_probe_ids=False,
            debuggers_yaml='debuggers:\n  probe_y:\n    type: pyocd\n    probe_id: "123"\n',
        ),
        "probe_x",
    )
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "adapter_not_found"
    assert result["side_effect_committed"] is False
    assert result["other_debugger"] == "probe_y"
    assert result["other_probe_id"] == "123"
    assert "probe_x" in result["summary"]
    assert "probe_y" in result["summary"]
    # Not cached as if the resolution had succeeded: a later call must repeat
    # the same refusal rather than silently reuse a collided identity.
    assert service.backend._resolved_probe_uid is None


def test_pyocd_probe_naming_no_other_debugger_is_unaffected_by_the_collision_check(tmp_path: Path) -> None:
    # A control alongside the collision test above: two debuggers configured,
    # each resolving to its own distinct probe, must still both work.
    from agentic_hil.config import bind_debugger

    config = bind_debugger(
        load_test_config(
            tmp_path,
            debugger_type="pyocd",
            debugger_name="probe_x",
            probe_id="PYOCD123",
            auto_probe_ids=False,
            debuggers_yaml='debuggers:\n  probe_y:\n    type: pyocd\n    probe_id: "PYOCD456"\n',
        ),
        "probe_x",
    )
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is True, result
    assert service.backend._resolved_probe_uid == "PYOCD123"


def test_pyocd_collision_check_does_not_apply_pyocds_substring_rule_to_an_openocd_peer(tmp_path: Path) -> None:
    # An OpenOCD entry passes its configured probe_id straight to OpenOCD as an
    # exact serial ("adapter serial 123"), never as a pyOCD-style substring.
    # "123" is a substring of "PYOCD123" but is not an OpenOCD serial that
    # resolves to it, so this must not be reported as a collision -- unlike
    # test_pyocd_refuses_two_debuggers_that_resolve_to_one_physical_probe,
    # where the peer is itself type pyocd and the same "123" genuinely does
    # collide (#review finding 7).
    from agentic_hil.config import bind_debugger

    config = bind_debugger(
        load_test_config(
            tmp_path,
            debugger_type="pyocd",
            debugger_name="probe_x",
            probe_id="OCD1",
            auto_probe_ids=False,
            debuggers_yaml='debuggers:\n  probe_y:\n    type: openocd\n    probe_id: "123"\n',
        ),
        "probe_x",
    )
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is True, result
    assert service.backend._resolved_probe_uid == "PYOCD123"


def test_pyocd_collision_check_still_catches_an_openocd_peer_with_the_exact_same_serial(tmp_path: Path) -> None:
    # The other half: an OpenOCD (or ST-Link) peer configured with the *exact*
    # resolved UID as its own serial would still resolve to the same physical
    # probe under its own backend's exact-match semantics, so this must still
    # be refused as a collision.
    #
    # Built by replacing the loaded config's debuggers directly rather than
    # through YAML: any peer whose exact probe_id equals what a pyocd entry
    # resolves to necessarily contains that entry's own (substring-matched)
    # needle, so config-load's own static guard (validate_debuggers) already
    # refuses that combination as text before any hardware is enumerated --
    # correctly, since it cannot yet know the peer's type-based matching rule
    # differs at the hardware boundary. This is the runtime check
    # (`_cross_debugger_identity_collision`) in isolation, on the exact-match
    # rule this fix adds for a non-pyocd peer.
    import dataclasses

    from agentic_hil.config import bind_debugger

    config = bind_debugger(
        load_test_config(tmp_path, debugger_type="pyocd", debugger_name="probe_x", probe_id="OCD1", auto_probe_ids=False),
        "probe_x",
    )
    peer = dataclasses.replace(config.debuggers["probe_x"], type="openocd", probe_id="PYOCD123", executable=None, resource_id=None)
    config = dataclasses.replace(config, debuggers={**config.debuggers, "probe_y": peer})
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "adapter_not_found"
    assert result["other_debugger"] == "probe_y"
    assert result["other_probe_id"] == "PYOCD123"
    assert service.backend._resolved_probe_uid is None


def test_pyocd_reconfigure_re_checks_collisions_after_only_a_peers_selector_changes(tmp_path: Path) -> None:
    # Review finding 6: the cache used to be invalidated only when the *bound*
    # debugger's own probe_id changed. A project config reload that changes
    # only the *other* debugger's selector into one that now collides has to
    # invalidate it too, or a resolution cached before the reload keeps being
    # reused as if the collision check had already cleared it against the new
    # configuration.
    from agentic_hil.config import bind_debugger

    first = bind_debugger(
        load_test_config(
            tmp_path,
            debugger_type="pyocd",
            debugger_name="probe_x",
            probe_id="OCD1",
            auto_probe_ids=False,
            debuggers_yaml='debuggers:\n  probe_y:\n    type: pyocd\n    probe_id: "NOT-CONNECTED"\n',
        ),
        "probe_x",
    )
    service = AgenticHILToolService(first)
    try:
        clean = service.call("probe_target")
        assert clean["ok"] is True, clean
        assert service.backend._resolved_probe_uid == "PYOCD123"

        second = bind_debugger(
            load_test_config(
                tmp_path,
                debugger_type="pyocd",
                debugger_name="probe_x",
                probe_id="OCD1",
                auto_probe_ids=False,
                debuggers_yaml='debuggers:\n  probe_y:\n    type: pyocd\n    probe_id: "123"\n',
            ),
            "probe_x",
        )
        service.backend.reconfigure(second)
        assert service.backend._resolved_probe_uid is None

        collided = service.call("probe_target")
    finally:
        service.close()

    assert collided["ok"] is False
    assert collided["error_type"] == "adapter_not_found"
    assert collided["other_debugger"] == "probe_y"
    assert service.backend._resolved_probe_uid is None


def test_pyocd_reconfigure_re_checks_collisions_after_a_peers_type_changes(tmp_path: Path) -> None:
    # Review round 1, finding 2: `_probe_selector_map` used to record only
    # `probe_selector_key(debugger)`, and that key carries no trace of `type`
    # once computed -- an OpenOCD peer with probe_id "123" and a pyOCD peer
    # with the same probe_id both fold to the key "123". So reconfiguring only
    # a peer's *type*, keeping its probe_id byte-for-byte the same, produced an
    # identical selector map and never invalidated the cache, even though
    # `_cross_debugger_identity_collision` matches an OpenOCD peer by exact
    # serial and a pyOCD peer by substring -- two different rules over the same
    # key. Here "123" is not an OpenOCD serial that resolves to PYOCD123 (no
    # collision, exact-match rule) but is a substring that does (collision,
    # pyOCD's own rule), so retyping probe_y from openocd to pyocd must flip
    # the outcome, and the cached resolution from before the reload must not
    # paper over that.
    from agentic_hil.config import bind_debugger

    first = bind_debugger(
        load_test_config(
            tmp_path,
            debugger_type="pyocd",
            debugger_name="probe_x",
            probe_id="OCD1",
            auto_probe_ids=False,
            debuggers_yaml='debuggers:\n  probe_y:\n    type: openocd\n    probe_id: "123"\n',
        ),
        "probe_x",
    )
    service = AgenticHILToolService(first)
    try:
        clean = service.call("probe_target")
        assert clean["ok"] is True, clean
        assert service.backend._resolved_probe_uid == "PYOCD123"

        second = bind_debugger(
            load_test_config(
                tmp_path,
                debugger_type="pyocd",
                debugger_name="probe_x",
                probe_id="OCD1",
                auto_probe_ids=False,
                debuggers_yaml='debuggers:\n  probe_y:\n    type: pyocd\n    probe_id: "123"\n',
            ),
            "probe_x",
        )
        service.backend.reconfigure(second)
        assert service.backend._resolved_probe_uid is None

        collided = service.call("probe_target")
    finally:
        service.close()

    assert collided["ok"] is False
    assert collided["error_type"] == "adapter_not_found"
    assert collided["other_debugger"] == "probe_y"
    assert service.backend._resolved_probe_uid is None


def test_unnamed_probe_refusal_no_longer_reasons_from_a_debugger_count(tmp_path: Path) -> None:
    # The refusal exists because this call cannot tell the bound, unnamed
    # debugger apart from another configured one - not because some number of
    # debuggers happen to be configured. The old text recited that count as
    # its own justification ("...and the project configures 2 debuggers...");
    # this asserts that reasoning is gone from what a caller reads (#278).
    from agentic_hil.config import bind_debugger

    config = bind_debugger(
        load_test_config(
            tmp_path,
            debugger_name="probe_x",
            auto_probe_ids=False,
            debuggers_yaml="debuggers:\n  probe_y:\n    type: openocd\n",
        ),
        "probe_x",
    )
    service = AgenticHILToolService(config)
    try:
        result = service.call("reset_target", {"mode": "run"})
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "not_supported"
    assert "the project configures" not in result["summary"]
    assert f"{len(config.debuggers)} debuggers" not in result["summary"]
    assert "debugger-probes" in result["summary"]
    assert result["configured_debuggers"] == ["probe_x", "probe_y"]


def test_single_debugger_stays_exempt_from_naming_its_probe(tmp_path: Path) -> None:
    # The other half of the same decision: with nothing else configured, a
    # bound debugger cannot be confused with another entry, so it is not
    # forced to pin a probe_id it may have no self-service way to discover
    # (OpenOCD cannot enumerate probes) or that may not exist at all (#278).
    config = load_test_config(tmp_path, debugger_name="dut")
    assert config.debugger.probe_id is None
    service = AgenticHILToolService(config)
    try:
        result = service.call("reset_target", {"mode": "run"})
    finally:
        service.close()

    assert result["ok"] is True, result
