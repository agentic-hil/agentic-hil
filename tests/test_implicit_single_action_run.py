"""A bare effect call is a run; a declared run is only the explicit spelling.

Since the abort seam existed, a run that failed put the bench back into a state
the next run could start from. An effect tool called with no run open sat
outside that entirely: a `com_write`, a `can_send`, a `flash_firmware` from an
agent doing one thing took its lease, failed into an incident, and left a
padlock whose only key was `hardware_recover` or a person at a shell — while
the identical call between `bench_run_start` and `bench_run_stop` recovered
itself.

So the call declares the run. Same `begin_run`, same lease record and audit
trail, same teardown, and the teardown is shared with `bench_run_stop` rather
than copied beside it. The teeth here are in four places:

* a bare effect call that fails aborts into a recovery action that reaches the
  board, and the incident it raised is resolved with its own ledger line;
* a bare effect call that succeeds leaves nothing held and no run open;
* a call inside a declared run is untouched — the declared run keeps its own
  boundary, and recovery still happens at its own teardown and not mid-run;
* the wrapped set is the effect class and nothing else: no read, no session
  start, no configuration tool.
"""

from __future__ import annotations

import json
import sys
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config

from agentic_hil.bench import fold_resource_name
from agentic_hil.config import load_config
from agentic_hil.contracts import TOOL_ANNOTATIONS
from agentic_hil.coordination import HardwareCoordinator
from agentic_hil.tools import (
    _READ_ONLY_HARDWARE_TOOLS,
    AgenticHILToolService,
    audited_hardware_tools,
    implicit_run_tools,
)

# Device locks are machine-wide, so a port id and a device name shared with
# another checkout's tests would contend across sibling clones. These are this
# file's alone.
PORT_ID = "implicit_run_uart"
DEVICE = "/dev/ttyIMPLICITRUN0"
# What a debugger one-shot's unconfirmed result is recorded as, and a reason a
# verified reset into halt settles.
FLASH_REASON = "debugger_result_unconfirmed"
# The same for a write that failed on a port that was already open.
WRITE_REASON = "com_write_effect_unconfirmed"


class FakeBackend:
    """A probe that records what was driven, and can be told to fail a flash.

    Deliberately at the hardware boundary rather than over the recovery seam:
    the question every test below asks is whether a reset actually reached a
    target, and a double any higher up could not answer it."""

    def __init__(self, *, flash_unconfirmed: bool = False) -> None:
        self.calls: list[str] = []
        self.flash_unconfirmed = flash_unconfirmed

    def probe_target(self) -> dict:
        self.calls.append("probe_target")
        return {"ok": True, "tool": "probe_target", "target_detected": True}

    def reset_target(self, mode: str = "run") -> dict:
        self.calls.append(f"reset_target:{mode}")
        return {"ok": True, "tool": "reset_target", "mode": mode}

    def flash_firmware(self, *args, **kwargs) -> dict:
        self.calls.append("flash_firmware")
        if self.flash_unconfirmed:
            # What a backend says when it cannot tell whether the image landed:
            # the one answer that raises an incident rather than merely failing.
            return {
                "ok": False,
                "tool": "flash_firmware",
                "error_type": "flash_failed",
                "side_effect_status": "unknown",
                "cleanup_required": True,
            }
        return {"ok": True, "tool": "flash_firmware", "reset_after_flash": True}

    def close(self) -> None:
        return None

    def sessionless_debug_tools(self) -> frozenset[str]:
        return frozenset()


class FakeSerialHandle:
    def __init__(self) -> None:
        self.is_open = False
        self.in_waiting = 0
        self.exclusive = None

    def open(self) -> None:
        self.is_open = True

    def read(self, size: int) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        return None

    def cancel_read(self) -> None:
        return None

    def close(self) -> None:
        self.is_open = False


def install_fake_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: FakeSerialHandle()))


def config_for(workspace: Path, *, com_port: bool = False, auto_recover: str | None = None):
    written = write_config(
        workspace,
        com_ports_yaml=(f'com_ports:\n  {PORT_ID}:\n    device: "{DEVICE}"\n' if com_port else "com_ports: {}\n"),
        auto_recover=auto_recover,
    )
    return load_config(str(written))


def firmware(workspace: Path) -> dict:
    image = workspace / "build" / "app.elf"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"\x7fELF" + b"\x00" * 12)
    return {"image_path": "build/app.elf"}


def ledger(config) -> list[dict]:
    path = Path(HardwareCoordinator(config, "ledger-reader").root) / "recovery.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# A. The call declares its own run, and the report says which kind it was.


def test_a_bare_effect_call_declares_an_implicit_run_and_says_so(tmp_path: Path) -> None:
    """"This call ran in a run" and "an agent declared a run around this call"
    are different facts, and an operator reading the report afterwards has to be
    able to tell them apart — so the run block names itself implicit rather than
    leaving the reader to infer it from an absence."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        result = service.call("flash_firmware", firmware(tmp_path))

        assert result["ok"] is True, result
        run = result["run"]
        assert run["implicit"] is True, run
        assert run["aborted"] is False, run
        assert run["run_label"] == "implicit:flash_firmware", run
        assert run["run_started_at"], run
        # The declaration is a real one over the resources the call leases, not
        # a label on an empty set.
        assert run["declared_devices"], run
        assert "recovery" not in result, result
    finally:
        service.close()


def test_a_bare_effect_call_that_succeeds_leaves_no_run_open(tmp_path: Path) -> None:
    """The run the call opened is the call's own, and it ends with the call.
    A dangling one would hold the bench against the next caller for the rest of
    this process's life — nothing times a run out, by design."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        assert service.call("flash_firmware", firmware(tmp_path))["ok"] is True

        status = service.call("bench_run_status")
        assert status["run_active"] is False, status
        assert status["declared_devices"] == [], status
        assert status["held_devices"] == [], status
        # The proof that matters to the next caller: a run can still be declared.
        assert service.call("bench_run_start", {"devices": [{"kind": "debugger"}]})["ok"] is True
        assert service.call("bench_run_stop")["ok"] is True
    finally:
        service.close()


# ---------------------------------------------------------------------------
# B. A failure aborts the implicit run and recovers the bench.


def test_a_bare_flash_that_fails_aborts_into_a_recovery_action(tmp_path: Path) -> None:
    """The whole point. One call, no run declared by anybody, an unconfirmed
    result — and a reset-into-halt reaches the board without a person being
    asked for anything.

    `ok` stays false in the same breath: recovering the bench does not un-fail
    the call."""
    config = config_for(tmp_path)
    backend = FakeBackend(flash_unconfirmed=True)
    service = AgenticHILToolService(config, backend=backend)
    try:
        result = service.call("flash_firmware", firmware(tmp_path))

        assert result["ok"] is False, result
        assert result["run"]["implicit"] is True, result
        assert result["run"]["aborted"] is True, result
        recovery = result["recovery"]
        assert recovery["attempted"] is True, recovery
        assert recovery["outcome"] == "recovered", recovery
        assert recovery["actions"] == ["reap_processes", "reset_halt", "probe_target"], recovery
        assert recovery["safe_state_predicate"] == "reset_halt", recovery
        assert recovery["incident_resolved"] is True, recovery
        assert recovery["resolved_reason"] == FLASH_REASON, recovery
        # The claim is about the board, so it is read off the board's own log.
        assert "reset_target:halt" in backend.calls, backend.calls
        assert backend.calls.index("flash_firmware") < backend.calls.index("reset_target:halt")
        assert backend.calls.index("reset_target:halt") < backend.calls.index("probe_target")
        # And the bench is usable again, which is what the recovery was for.
        assert service.coordinator.status()["blocked"] is False
        assert service.call("bench_run_start", {"devices": [{"kind": "debugger"}]})["ok"] is True
        assert service.call("bench_run_stop")["ok"] is True
    finally:
        service.close()

    lines = [line for line in ledger(config) if line.get("via") == "recovery_action"]
    assert len(lines) == 1, ledger(config)
    assert lines[0]["actor"] == "server"
    assert lines[0]["attestation"] == "recovery_action_verified"
    assert lines[0]["reason"] == FLASH_REASON


def test_a_bare_com_write_that_fails_aborts_and_drives_the_recovery_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The case named in the issue, end to end and on a second device kind.

    A write that died on an open port may have put bytes on the line and the
    board may have acted on them; nothing on this host can say, which is why it
    quarantines. What is new is that the single-action run the call declared for
    itself then aborts, the run ends, and the recovery action drives the target
    into a defined state — exactly what the same call gets between
    `bench_run_start` and `bench_run_stop`.

    The incident itself is *not* settled here, and that is unchanged rather than
    a shortfall: this failure names two reasons, and an incident made of a mix
    of them is one no predicate this process runs can speak for, so it keeps the
    operator's route whichever kind of run it happened in."""
    config = config_for(tmp_path, com_port=True)
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    install_fake_serial(monkeypatch)
    try:
        assert service.call("com_session_start", {"port_id": PORT_ID})["ok"] is True
        session = service.com_ports.sessions[PORT_ID]
        monkeypatch.setattr(session.serial_handle, "write", lambda data: (_ for _ in ()).throw(OSError("write died mid-line")))

        result = service.call("com_write", {"port_id": PORT_ID, "text": "ping\n"})

        assert result["ok"] is False, result
        assert WRITE_REASON in result["cleanup_reasons"], result
        assert result["run"]["implicit"] is True, result
        assert result["run"]["aborted"] is True, result
        # The run declared the port it was about to write to, and nothing else.
        # Folded by the same production rule the lock key uses: backslashed and
        # lowercased on Windows, verbatim on POSIX — the test must not re-implement it.
        assert result["run"]["declared_devices"] == [fold_resource_name(f"com:{DEVICE}")], result["run"]
        recovery = result["recovery"]
        assert recovery["attempted"] is True, recovery
        assert recovery["actions"] == ["reap_processes", "reset_halt", "probe_target"], recovery
        assert "reset_target:halt" in backend.calls, backend.calls
        # The run is over even though the incident is not: a run left open
        # because the incident could not be settled would be the padlock in a
        # different shape.
        assert service.call("bench_run_status")["run_active"] is False
    finally:
        # A quarantined session stays registered for a cleanup retry, so shutting
        # the service down over it is refused as well. That is the containment
        # working, not a teardown defect.
        with suppress(RuntimeError):
            service.close()


def test_the_implicit_run_ends_even_when_recovery_is_withheld(tmp_path: Path) -> None:
    """A bench whose policy takes no action still gets its devices back, and the
    result names the setting that made that choice. A run left open because the
    recovery was declined would be the padlock in a different shape."""
    config = config_for(tmp_path, auto_recover="off")
    backend = FakeBackend(flash_unconfirmed=True)
    service = AgenticHILToolService(config, backend=backend)
    try:
        result = service.call("flash_firmware", firmware(tmp_path))

        assert result["run"]["aborted"] is True, result
        assert result["recovery"]["attempted"] is False, result["recovery"]
        assert result["recovery"]["reason_not_attempted"] == "auto_recover_policy_off"
        assert backend.calls == ["flash_firmware"], backend.calls
        assert service.call("bench_run_status")["run_active"] is False
    finally:
        service.close()


# ---------------------------------------------------------------------------
# C. A declared run is untouched.


def test_a_call_inside_a_declared_run_declares_nothing_of_its_own(tmp_path: Path) -> None:
    """The declared run already holds everything and spans more than one call.
    A second declaration inside it would be refused, and a teardown inside it
    would end a run its owner had not finished with."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        assert service.call("bench_run_start", {"devices": [{"kind": "debugger"}]})["ok"] is True

        result = service.call("flash_firmware", firmware(tmp_path))

        assert result["ok"] is True, result
        assert "run" not in result, result
        status = service.call("bench_run_status")
        assert status["run_active"] is True, status
        assert status["run_label"] is None, status
        assert service.call("bench_run_stop")["ok"] is True
    finally:
        service.close()


def test_a_failure_inside_a_declared_run_still_recovers_at_that_run_s_teardown(tmp_path: Path) -> None:
    """The load-bearing half of "no behaviour change inside a declared run": the
    recovery does not move earlier. An agent driving several steps under one
    declaration must not have the board reset out from under step two because
    step one failed — the verdict is the run's, and so is the teardown."""
    config = config_for(tmp_path)
    backend = FakeBackend(flash_unconfirmed=True)
    service = AgenticHILToolService(config, backend=backend)
    try:
        assert service.call("bench_run_start", {"devices": [{"kind": "debugger"}]})["ok"] is True

        result = service.call("flash_firmware", firmware(tmp_path))

        assert result["ok"] is False, result
        assert "run" not in result, result
        assert "recovery" not in result, result
        # Nothing was driven: the incident stands and the run is still the
        # agent's to end.
        assert backend.calls == ["flash_firmware"], backend.calls
        assert service.coordinator.blocked is True

        stopped = service.call("bench_run_stop")

        assert stopped["recovery"]["outcome"] == "recovered", stopped
        assert "reset_target:halt" in backend.calls, backend.calls
    finally:
        service.close()


def test_an_open_incident_keeps_the_recovery_class_reaching_the_board(tmp_path: Path) -> None:
    """A new run is the stimulus class and waits for the incident; the recovery
    class must reach the board *through* it. Wrapping a recovery-class call in a
    run it could not open would rebuild the padlock the abort seam removed, so
    the wrap stands aside while an incident is standing."""
    config = config_for(tmp_path, auto_recover="readonly")
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        lease = service.coordinator.acquire("physical:implicit-run-incident")
        service.coordinator.quarantine_lease(lease, "debug_session_start_unconfirmed")
        assert service.coordinator.status()["blocked"] is True

        result = service.call("reset_target", {"mode": "halt"})

        assert result["ok"] is True, result
        assert result["incident_resolved"] is True, result
        # The wrap now happens, and the reason it may is what #216 settled: this
        # incident owes no gate, so the run it declares for itself opens. What
        # the wrap must never do is stand between the remedy and the board, and
        # the line above is the proof it did not.
        assert result["run"]["implicit"] is True, result
    finally:
        service.close()


# ---------------------------------------------------------------------------
# D. The wrapped set is the effect class and nothing else.


def test_the_wrapped_set_is_exactly_the_effect_class(tmp_path: Path) -> None:
    """Pinned by name as well as by derivation. The derivation is what keeps a
    tool added later from being silently left outside the run model; the names
    are what make a change to it a decision somebody has to write down."""
    assert implicit_run_tools() == {"com_write", "can_send", "flash_firmware", "probe_target", "reset_target"}
    # Every member raises incidents — that is the base the derivation subtracts
    # from, and a member outside it could never abort into anything.
    assert implicit_run_tools() <= audited_hardware_tools()
    # And the three subtracted groups are out, each for its own reason.
    assert not implicit_run_tools() & {"com_session_start", "can_session_start", "debug_start_session"}
    assert not implicit_run_tools() & {"project_config_create", "project_config_adopt_hardware"}
    assert not implicit_run_tools() & {"debug_continue", "debug_set_breakpoint", "debug_dump_symbol_ihex"}


def test_the_read_only_exclusion_matches_this_server_s_own_classification() -> None:
    """`tools.py` may not decide behaviour from the annotation table — contracts
    says so and its own test enforces it — so the read-only set is written out
    beside the code that uses it and the two are compared here instead. Note
    which tool is *not* excluded: `probe_target` connects, an SWD attach halts a
    running core, and this repository classifies that as an effect."""
    advertised = {name for name in audited_hardware_tools() if TOOL_ANNOTATIONS[name].get("readOnlyHint") is True}

    assert set(_READ_ONLY_HARDWARE_TOOLS) == advertised
    assert "probe_target" not in advertised
    assert "probe_target" in implicit_run_tools()


def test_a_read_only_call_declares_no_run(tmp_path: Path) -> None:
    """A read leaves nothing for a recovery action to put back, so it gets no
    run and no teardown — the behavioural half of the set above."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        for name in ("get_last_report", "bench_run_status", "debugger_probes_list"):
            assert "run" not in service.call(name), name
    finally:
        service.close()
