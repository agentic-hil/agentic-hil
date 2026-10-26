"""The machine directory every account of a group keeps its device locks in (#777).

Device locks under each home split two accounts on one board: neither sees the
other's hold, so a run as one account and a run as another both take the same
probe. A fixed directory, created once for a group, is the one place every
account's processes look. When it is there and cannot be used, a device lock is
refused rather than kept in a home where no other account looks.
"""
from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml
from support import scaled_time_bound
from test_device_access import a_bench_bound_to_the_recorded_probe_and_port

from agentic_hil import bench as bench_module
from agentic_hil.backends import stlink_server
from agentic_hil.bench import BenchMutex, DeviceBusyError, device_lock_root, resource_digest
from agentic_hil.cli import DOCTOR_DEVICE_LOCKS_FINDING, DOCTOR_FINDINGS_SETUP_KEEPS, doctor
from agentic_hil.config import ConfigError
from agentic_hil.knowledge import remediation_fields

BOARD = "physical:shared-bench-board"
UNUSABLE = "device_lock_root_unusable"

ON_LINUX = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the machine directory is a Linux feature; elsewhere the home directory stays")
NOT_ROOT = pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="root may enter a directory whatever its mode says")


@pytest.fixture
def machine_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the machine directory would be, with nothing there yet."""
    parent = tmp_path / "var-lib" / "agentic-hil"
    parent.mkdir(parents=True)
    root = parent / "device-locks"
    monkeypatch.setattr(bench_module, "MACHINE_DEVICE_LOCK_ROOT", root)
    return root


@pytest.fixture
def mutex() -> Iterator[BenchMutex]:
    held = BenchMutex(frontend="test")
    try:
        yield held
    finally:
        held.release_all()


def shared(root: Path, mode: int = 0o2770) -> Path:
    """The directory as the setup creates it: setgid, the group's, nobody else's."""
    root.mkdir()
    os.chmod(root, mode)
    return root


def home_locks() -> Path:
    return Path(os.path.expanduser("~")) / ".agentic-hil" / "device-locks"


def lock_files(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_without_the_machine_directory_the_home_directory_stays(machine_root: Path) -> None:
    assert device_lock_root() == home_locks()
    assert not machine_root.exists(), "the product never creates the machine directory: an administrator does, for a group"


def test_off_linux_the_machine_directory_is_not_consulted(machine_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    machine_root.mkdir()
    monkeypatch.setattr(bench_module, "machine_lock_root_applies", lambda: False)

    assert device_lock_root() == home_locks()


@ON_LINUX
def test_the_machine_directory_is_the_root_when_it_is_there(machine_root: Path, mutex: BenchMutex) -> None:
    shared(machine_root)

    assert device_lock_root() == machine_root
    mutex.acquire([BOARD])

    digest = resource_digest(BOARD)
    assert lock_files(machine_root) == [f"{digest}.holder.json", f"{digest}.lock"]
    assert lock_files(home_locks()) == []


def a_link(root: Path) -> None:
    target = shared(root.parent / "elsewhere")
    root.symlink_to(target, target_is_directory=True)


def a_parent_link(root: Path) -> None:
    real_parent = root.parent.parent / "real-parent"
    real_parent.mkdir()
    shared(real_parent / root.name)
    root.parent.rmdir()
    root.parent.symlink_to(real_parent, target_is_directory=True)


def a_file(root: Path) -> None:
    root.write_text("", encoding="utf-8")


def with_mode(mode: int) -> Callable[[Path], None]:
    def make(root: Path) -> None:
        shared(root, mode)

    return make


UNUSABLE_DIRECTORIES = [
    pytest.param(a_link, "link", id="a-link"),
    pytest.param(a_parent_link, "link", id="its-parent-a-link"),
    pytest.param(a_file, "not a directory", id="a-file"),
    pytest.param(with_mode(0o770), "setgid", id="no-setgid"),
    pytest.param(with_mode(0o2775), "others", id="others-may-read"),
    pytest.param(with_mode(0o2771), "others", id="others-may-enter"),
    pytest.param(with_mode(0o2700), "group", id="the-group-may-not-write"),
    pytest.param(with_mode(0o2750), "group", id="the-group-may-only-read"),
]


@ON_LINUX
@pytest.mark.parametrize(("make", "problem"), UNUSABLE_DIRECTORIES)
def test_a_machine_directory_that_cannot_be_used_is_refused_by_name(machine_root: Path, make: Callable[[Path], None], problem: str) -> None:
    make(machine_root)

    with pytest.raises(ConfigError) as caught:
        device_lock_root()

    assert caught.value.error_type == UNUSABLE
    assert caught.value.details["path"] == str(machine_root)
    assert problem in caught.value.details["problem"], caught.value.details
    assert str(machine_root) in caught.value.summary
    assert not home_locks().exists(), "an unusable machine directory never sends the locks home"


@ON_LINUX
@NOT_ROOT
def test_a_machine_directory_this_account_cannot_enter_is_refused_by_name(machine_root: Path) -> None:
    """Group read, write and enter, setgid, nothing for others: a directory set up
    right, which this account still cannot use. Its owner is refused here by its
    own empty bits, the way an account outside the group is refused by the
    directory's."""
    shared(machine_root, 0o2070)
    try:
        with pytest.raises(ConfigError) as caught:
            device_lock_root()
    finally:
        os.chmod(machine_root, 0o2770)

    assert caught.value.error_type == UNUSABLE
    assert "this account" in caught.value.details["problem"], caught.value.details


@ON_LINUX
def test_a_device_lock_on_an_unusable_machine_directory_is_refused_and_kept_nowhere(machine_root: Path, mutex: BenchMutex) -> None:
    shared(machine_root, 0o770)

    with pytest.raises(ConfigError) as caught:
        mutex.acquire([BOARD])

    refusal = caught.value.to_dict()
    assert refusal["error_type"] == UNUSABLE
    assert refusal["remediation"], refusal
    assert mutex.held_resources() == frozenset()
    assert lock_files(machine_root) == []
    assert lock_files(home_locks()) == []


def test_the_refusal_has_its_fix_in_the_catalogue() -> None:
    fields = remediation_fields(UNUSABLE)

    assert fields.get("remediation"), fields
    assert fields.get("do_not"), fields


@ON_LINUX
def test_files_in_the_machine_directory_are_the_groups_whatever_the_umask(machine_root: Path, mutex: BenchMutex) -> None:
    shared(machine_root)
    previous = os.umask(0o077)
    try:
        mutex.acquire([BOARD])
        digest = resource_digest(BOARD)
        assert mode_of(machine_root / f"{digest}.lock") == 0o660
        assert mode_of(machine_root / f"{digest}.holder.json") == 0o660
        mutex.release([BOARD])
        assert mode_of(machine_root / f"{digest}.holder.json") == 0o660
    finally:
        os.umask(previous)


@ON_LINUX
def test_the_st_link_server_record_in_the_machine_directory_is_the_groups(machine_root: Path) -> None:
    shared(machine_root)
    previous = os.umask(0o077)
    try:
        stlink_server._write_record(7184, {"version": stlink_server.RECORD_VERSION, "port": 7184, "pid": os.getpid()})
    finally:
        os.umask(previous)

    assert mode_of(machine_root / "stlink-server-7184.json") == 0o660


@ON_LINUX
def test_the_root_moves_over_at_the_next_acquire_while_nothing_is_held(machine_root: Path, mutex: BenchMutex) -> None:
    """A server started before the directory existed is not left on its home for good."""
    mutex.acquire([BOARD])
    mutex.release([BOARD])
    assert lock_files(home_locks()) != []

    shared(machine_root)
    mutex.acquire([BOARD])

    assert f"{resource_digest(BOARD)}.lock" in lock_files(machine_root)


@ON_LINUX
def test_the_root_stays_while_a_device_is_held(machine_root: Path, mutex: BenchMutex) -> None:
    """One owner's holds stay in one place: a second device taken after the switch
    goes beside the first, and the move waits for the owner to hold nothing."""
    second = "physical:second-board"
    mutex.acquire([BOARD])
    shared(machine_root)

    mutex.acquire([second])

    assert f"{resource_digest(second)}.lock" in lock_files(home_locks())
    assert lock_files(machine_root) == []


HOLD_IN_ANOTHER_HOME = """
import sys
from pathlib import Path

from agentic_hil import bench

bench.MACHINE_DEVICE_LOCK_ROOT = Path(sys.argv[1])
mutex = bench.BenchMutex(frontend="other-account")
mutex.acquire([sys.argv[2]])
print("held", flush=True)
sys.stdin.readline()
"""


def another_home(tmp_path: Path) -> dict[str, str]:
    """A process of this machine with a home of its own, as another account's is."""
    home = tmp_path / "other-home"
    home.mkdir()
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    dependency_root = str(Path(yaml.__file__).resolve().parents[1])
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join([dependency_root, source_root, environment.get("PYTHONPATH", "")])
    return environment


@ON_LINUX
def test_two_homes_meet_in_the_machine_directory(machine_root: Path, mutex: BenchMutex, tmp_path: Path) -> None:
    """The case the directory exists for. Under their homes these two never meet."""
    shared(machine_root)
    other = subprocess.Popen(
        [sys.executable, "-c", HOLD_IN_ANOTHER_HOME, str(machine_root), BOARD],
        env=another_home(tmp_path),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert other.stdout is not None
        assert other.stdout.readline().strip() == "held"

        with pytest.raises(DeviceBusyError) as caught:
            mutex.acquire([BOARD])
        assert caught.value.result["holder"]["pid"] == other.pid

        other.send_signal(signal.SIGKILL)
        other.wait(timeout=scaled_time_bound(30))
        mutex.acquire([BOARD])
        reclaimed = mutex.reclaimed(BOARD)
        assert reclaimed is not None and reclaimed["owner"]["pid"] == other.pid
    finally:
        if other.poll() is None:
            other.kill()
            other.wait(timeout=scaled_time_bound(30))


def test_doctor_names_the_home_directory_where_there_is_no_machine_directory(machine_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a_bench_bound_to_the_recorded_probe_and_port(tmp_path, monkeypatch)

    report = doctor()

    assert report["device_locks"]["ok"] is True, report["device_locks"]
    assert report["device_locks"]["root"] == "home"
    assert DOCTOR_DEVICE_LOCKS_FINDING not in report["unhealthy"]


@ON_LINUX
def test_doctor_names_the_machine_directory_in_use(machine_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared(machine_root)
    a_bench_bound_to_the_recorded_probe_and_port(tmp_path, monkeypatch)

    report = doctor()

    check = report["device_locks"]
    assert (check["ok"], check["root"], check["path"]) == (True, "machine", str(machine_root))
    assert DOCTOR_DEVICE_LOCKS_FINDING not in report["unhealthy"]


@ON_LINUX
def test_doctor_fails_over_a_machine_directory_that_cannot_be_used(machine_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared(machine_root, 0o770)
    a_bench_bound_to_the_recorded_probe_and_port(tmp_path, monkeypatch)

    report = doctor()

    assert report["ok"] is False
    assert report["unhealthy"] == [DOCTOR_DEVICE_LOCKS_FINDING], report["unhealthy"]
    check = report["device_locks"]
    assert (check["ok"], check["error_type"], check["path"]) == (False, UNUSABLE, str(machine_root))
    assert check["remediation"], check
    assert check["summary"] in report["summary"]


@ON_LINUX
def test_doctor_without_a_configuration_still_names_a_machine_directory_that_cannot_be_used(machine_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`init` takes device locks, so an account the machine directory refuses is
    refused before it has a configuration at all, and that refusal sends it to
    `doctor`. Found on a bench: an account outside the group heard only that it
    had no configuration."""
    shared(machine_root, 0o770)
    monkeypatch.chdir(tmp_path)

    report = doctor()

    assert report["ok"] is False
    assert report["error_type"] == "config_file_not_found", report
    check = report["device_locks"]
    assert (check["ok"], check["error_type"], check["path"]) == (False, UNUSABLE, str(machine_root))
    assert check["summary"] in report["summary"]


def test_doctor_without_a_configuration_is_unchanged_over_a_usable_lock_directory(machine_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    report = doctor()

    assert report["error_type"] == "config_file_not_found", report
    assert "device_locks" not in report


def test_setup_keeps_the_file_over_the_finding() -> None:
    """The finding is about this account and this machine, never about the document."""
    assert DOCTOR_DEVICE_LOCKS_FINDING in DOCTOR_FINDINGS_SETUP_KEEPS
