"""`bench_run_stop` describes the bench its own teardown leaves behind.

A call inside a declared run that fails into an incident keeps its lease
registered until the run ends, because the run's teardown is where that
incident is settled or stood down and the lease given back. The answer
`bench_run_stop` gives was built before that teardown ran, so after a failed
flash it named the flash's lease as still open and the probe as still held,
beside a `recovery` block saying the incident had ended, and told the caller to
stop a session nobody had opened; `lease-status` right after showed nothing
held. What the answer names now is what holds the bench when it returns, and
only a session still holding a lease is something it tells the caller to stop.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_implicit_single_action_run import (
    PORT_ID,
    FakeBackend,
    config_for,
    fail_every_release,
    firmware,
    install_fake_serial,
)

from agentic_hil.tools import AgenticHILToolService


def a_run_whose_flash_failed(service: AgenticHILToolService, workspace: Path, devices: list[dict]) -> None:
    """Open a declared run over `devices` and fail a flash inside it into an incident."""
    started = service.call("bench_run_start", {"devices": devices})
    assert started["ok"] is True, started
    flashed = service.call("flash_firmware", firmware(workspace))
    assert flashed["ok"] is False, flashed
    # Inside a declared run the incident waits for the run's teardown, and so
    # does the lease the flash took.
    assert service.coordinator.blocked is True
    assert service.coordinator.leases, "the failed flash left no lease for the teardown to give back"


def the_bench_now(service: AgenticHILToolService) -> tuple[list[str], list[str]]:
    """The leases this server still has registered, and the devices it still holds."""
    return sorted(service.coordinator.leases), sorted(service.coordinator.bench.held_resources())


@pytest.mark.parametrize("policy", [None, "readonly", "off"])
def test_a_run_whose_flash_failed_ends_with_nothing_said_to_be_open(tmp_path: Path, policy: str | None) -> None:
    """The case in the issue, under every policy: the default settles the
    incident with a reset into halt, `readonly` and `off` leave it to be stood
    down at the end of the call. Either way nothing holds the bench when the
    call returns, so the answer names no open lease, no held device, and no
    session to stop."""
    config = config_for(tmp_path, auto_recover=policy)
    service = AgenticHILToolService(config, backend=FakeBackend(flash_unconfirmed=True))
    try:
        a_run_whose_flash_failed(service, tmp_path, [{"kind": "debugger"}])

        stopped = service.call("bench_run_stop")
        leases, held = the_bench_now(service)

        assert stopped["ok"] is True, stopped
        assert "recovery" in stopped, stopped
        assert service.coordinator.status()["blocked"] is False
        assert (leases, held) == ([], []), (leases, held)
        assert "open_leases" not in stopped, stopped
        assert "still_held_devices" not in stopped, stopped
        assert stopped["summary"] == f"{len(stopped['released_devices'])} device(s) were released.", stopped["summary"]
        run = service.call("bench_run_status")
        assert run["run_active"] is False and run["held_devices"] == [], run
    finally:
        service.close()


def test_a_session_the_run_left_running_is_all_the_answer_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A COM session outlives the run that opened it by design, and keeps its
    port held. After the same failed flash the answer names that session's
    lease and the device it holds, and tells the caller to stop the session;
    the flash's lease, which the teardown gave back, is not among them."""
    config = config_for(tmp_path, com_port=True)
    service = AgenticHILToolService(config, backend=FakeBackend(flash_unconfirmed=True))
    install_fake_serial(monkeypatch)
    try:
        started = service.call("bench_run_start", {"devices": [{"kind": "debugger"}, {"kind": "uart", "id": PORT_ID}]})
        assert started["ok"] is True, started
        assert service.call("com_session_start", {"port_id": PORT_ID})["ok"] is True
        session_lease = service.com_ports.sessions[PORT_ID].lease.lease_id
        flashed = service.call("flash_firmware", firmware(tmp_path))
        assert flashed["ok"] is False, flashed
        assert sorted(service.coordinator.leases) != [session_lease], "the failed flash left no lease of its own"

        stopped = service.call("bench_run_stop")
        leases, held = the_bench_now(service)

        assert stopped["ok"] is True, stopped
        assert leases == [session_lease], leases
        assert stopped["open_leases"] == [session_lease], stopped
        assert stopped["still_held_devices"] == held, (stopped, held)
        assert held, "the session's port is no longer held"
        assert stopped["summary"] == "The run ended, but 1 lease(s) are still open and keep their devices held; stop those sessions to free them.", stopped["summary"]
    finally:
        service.call("com_session_stop", {"port_id": PORT_ID})
        service.close()


def test_a_lease_the_teardown_could_not_give_back_is_named_without_a_session_to_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one lease the teardown cannot end: a release that cannot persist its
    own record fails closed and keeps the lease registered under an incident of
    its own. The answer names it, because the bench is held, and does not send
    the caller to stop a session, because no session holds it."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend(flash_unconfirmed=True))
    try:
        a_run_whose_flash_failed(service, tmp_path, [{"kind": "debugger"}])
        fail_every_release(service, monkeypatch)

        stopped = service.call("bench_run_stop")
        leases, held = the_bench_now(service)

        assert stopped["ok"] is True, stopped
        assert service.coordinator.status()["blocked"] is True
        assert leases, "the lease whose release failed is no longer registered"
        assert stopped["open_leases"] == leases, (stopped, leases)
        assert stopped["still_held_devices"] == held, (stopped, held)
        assert "session" not in stopped["summary"], stopped["summary"]
    finally:
        monkeypatch.undo()
        service.close()


def test_a_session_and_a_lease_the_teardown_could_not_give_back_are_told_apart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both at once: the session the run left running, and the flash's lease
    whose release failed. The answer names both leases, and its advice to stop
    sessions counts only the one a session holds, because stopping a session
    frees nothing the incident holds."""
    config = config_for(tmp_path, com_port=True)
    service = AgenticHILToolService(config, backend=FakeBackend(flash_unconfirmed=True))
    install_fake_serial(monkeypatch)
    try:
        started = service.call("bench_run_start", {"devices": [{"kind": "debugger"}, {"kind": "uart", "id": PORT_ID}]})
        assert started["ok"] is True, started
        assert service.call("com_session_start", {"port_id": PORT_ID})["ok"] is True
        session_lease = service.com_ports.sessions[PORT_ID].lease.lease_id
        assert service.call("flash_firmware", firmware(tmp_path))["ok"] is False
        fail_every_release(service, monkeypatch)

        stopped = service.call("bench_run_stop")
        leases, held = the_bench_now(service)

        assert stopped["ok"] is True, stopped
        assert len(leases) == 2 and session_lease in leases, leases
        assert stopped["open_leases"] == leases, (stopped, leases)
        assert stopped["still_held_devices"] == held, (stopped, held)
        assert stopped["summary"] == (
            "The run ended, but 2 lease(s) are still open and keep their devices held; 1 of them belong to live sessions, and stopping those sessions frees their devices."
        ), stopped["summary"]
    finally:
        monkeypatch.undo()
        service.call("com_session_stop", {"port_id": PORT_ID})
        service.close()
