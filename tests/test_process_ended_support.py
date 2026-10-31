"""When a process the suite ended counts as ended (#746).

`test_process_cleanup_handles_exited_group_leader` failed once in a full run
under load. The run was stopped before pytest printed its failures, so the
exception it failed on has not been read, and what follows is the working
hypothesis rather than an observed cause. Its last line asked `os.kill(pid, 0)`
about a descendant `terminate_process_tree` had just ended. The product returns
as soon as every member of the group is a zombie, which is the #485 decision,
and this descendant was orphaned when its leader exited first, so it is init's
or a subreaper's to collect, not this process's. Until that reaper runs the pid
still answers the signal probe, so an assertion on a loaded host can land
inside that moment.

The two POSIX nodes now ask `support.wait_until_process_ended` instead, which
counts a zombie as ended and, where no `/proc` can say so, waits for the reaper.
Those nodes are skipped on Windows and their window is a race, so the decision
itself is pinned here, on every platform, with every probe injected: no
process is started, nothing is signalled and no real time passes. The first
case below shows that the window can occur, a pid that answers the probe and
is a zombie, and how it is now read; it does not show that run's failure was
that window. The clock moves only when the helper sleeps, so a state read
wrongly fails at the deadline instead of hanging.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import support


def the_wait() -> Callable[..., None]:
    """`support.wait_until_process_ended`, looked up when a test runs rather than imported.

    A `from support import` of a name the module does not define fails the
    collection of the whole file; looked up here, its absence fails each test
    by name instead.
    """
    helper = getattr(support, "wait_until_process_ended", None)
    if helper is None:
        raise AssertionError("tests/support.py defines no wait_until_process_ended")
    return helper


class FakeTime:
    """A clock that moves only when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        # A loop that never reaches its deadline would otherwise spin forever on
        # a clock that only this moves.
        assert len(self.sleeps) < 100_000, "the wait slept a hundred thousand times without deciding"
        self.sleeps.append(seconds)
        self.now += max(seconds, 0.001)


def stat_line(state: str) -> str:
    """One `/proc/<pid>/stat` line for pid 4321 in `state`.

    The command name carries a parenthesis and a space of its own, as procfs
    prints whatever name the process set: a reader that splits the whole line,
    or splits at the first `)`, reads `thon)` where the state is.
    """
    return f"4321 (py) thon) {state} 1 4321 4321 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 12345"


def no_stat(pid: int) -> str:
    raise FileNotFoundError(f"/proc/{pid}/stat")


def test_a_zombie_counts_as_ended_reading_the_state_after_the_last_parenthesis() -> None:
    """The window #746 is suspected of, forced: the pid answers the probe and is a zombie."""
    wait = the_wait()
    fake = FakeTime()
    probed: list[tuple[int, int]] = []

    def probe(pid: int, sig: int) -> None:
        probed.append((pid, sig))

    wait(4321, 5.0, signal_probe=probe, read_stat=lambda pid: stat_line("Z"), sleep=fake.sleep, clock=fake.clock)

    assert probed == [(4321, 0)]
    assert fake.sleeps == [], "a zombie was waited on as though it were still running"


def test_a_process_still_sleeping_at_the_deadline_fails_naming_its_pid_and_state() -> None:
    """What must keep failing: a descendant the teardown did not end."""
    wait = the_wait()
    fake = FakeTime()
    started = fake.now

    with pytest.raises(AssertionError) as excinfo:
        wait(4321, 5.0, signal_probe=lambda pid, sig: None, read_stat=lambda pid: stat_line("S"), sleep=fake.sleep, clock=fake.clock)

    assert "4321" in str(excinfo.value)
    assert "state S" in str(excinfo.value)
    # The whole bound was waited out, not a fraction of it: a wait that gave up
    # early would fail a descendant the reaper was about to collect.
    assert fake.now - started >= 5.0, f"the wait gave up after {fake.now - started:g}s of a 5s bound"
    assert "5s" in str(excinfo.value), "the failure does not name the bound it waited out"


def test_a_pid_the_kernel_no_longer_knows_has_ended() -> None:
    """Reaped already: the probe says so, and that settles it."""
    wait = the_wait()
    fake = FakeTime()

    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError()

    wait(4321, 5.0, signal_probe=gone, read_stat=no_stat, sleep=fake.sleep, clock=fake.clock)

    assert fake.sleeps == []


def test_without_proc_the_wait_lasts_until_the_reaper_collects_the_pid() -> None:
    """macOS: no stat to read, so a pid that answers the probe may be a zombie or not.

    It is not counted as ended on that answer. The wait goes on until the reaper
    has been past and the probe says the pid is gone.
    """
    wait = the_wait()
    fake = FakeTime()
    probes: list[int] = []

    def reaped_after_the_first_probe(pid: int, sig: int) -> None:
        probes.append(pid)
        if len(probes) > 1:
            raise ProcessLookupError()

    wait(4321, 5.0, signal_probe=reaped_after_the_first_probe, read_stat=no_stat, sleep=fake.sleep, clock=fake.clock)

    assert len(fake.sleeps) >= 1, "a pid that still answered the probe was taken as ended"
