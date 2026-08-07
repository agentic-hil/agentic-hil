"""Quarantine is the answer to "I do not know what state the hardware is in".

It is not the answer to "something went wrong". Quarantine is
the one state a human must physically resolve — walk to the bench, inspect,
type `agentic-hil recover --confirm-safe-state` — so every trigger has to pass
one test: can the failure prove it never reached the hardware (or that the
hardware provably ended where a normal call would leave it)?

* Provably-not-contacted failures refuse with a named error and `retry_safe`,
  and write no quarantine record.
* Failures that cannot prove their abort point keep quarantining. That is the
  feature, and the tests here pin that direction too.
* A justified quarantine now tells the signer what was attempted, what is
  confirmed, what remains unknown, and what to check on the physical board —
  out of the one catalogue in knowledge.py.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import (
    FAKE_GDB,
    FAKE_OPENOCD,
    FAKE_OPENOCD_MISSING_CFG,
    FAKE_OPENOCD_NO_TARGET,
    FAKE_OPENOCD_UNCONFIRMED,
    FAKE_STLINK_NO_PROBE,
    FAKE_STLINK_UNCONFIRMED,
    write_config,
)

from agentic_hil.config import load_config
from agentic_hil.coordination import HardwareCoordinator
from agentic_hil.knowledge import (
    QUARANTINE_REASON_GUIDES,
    attach_quarantine_guidance,
    quarantine_reason_details,
)
from agentic_hil.tools import AgenticHILToolService

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "agentic_hil"

DETECTED_PROBE = {"ok": True, "tool": "probe_target", "target_detected": True, "summary": "Target detected."}


def config_for(workspace: Path, **kwargs):
    return load_config(str(write_config(workspace, **kwargs)))


def coordination_record_states(config) -> set[str]:
    records = Path(config.state_root) / "coordination" / "records"
    if not records.is_dir():
        return set()
    states = set()
    for path in records.glob("*.json"):
        state = json.loads(path.read_text(encoding="utf-8")).get("state")
        if isinstance(state, str):
            states.add(state)
    return states


def assert_no_quarantine_record(config) -> None:
    """The DoD's own check: a provably-not-contacted failure writes no
    quarantine record — nothing an operator would ever have to recover."""
    blocking = coordination_record_states(config) & {"cleanup_required", "quarantined", "recovery_pending"}
    assert not blocking, blocking


def assert_refused_without_quarantine(result: dict, config) -> None:
    assert result["ok"] is False
    assert result.get("quarantined") is not True, result
    assert result.get("cleanup_required") is not True, result
    assert "quarantine_guidance" not in result
    assert_no_quarantine_record(config)


# ---------------------------------------------------------------------------
# Read-only one-shots: a failed read is a failed call, not an unconfirmed board.


def test_a_probe_read_that_fails_refuses_instead_of_quarantining(tmp_path: Path) -> None:
    """The owner's report in one test: probe an unreachable target, and the
    bench used to need a physical `recover --confirm-safe-state`."""
    config = config_for(tmp_path, debugger_executable=FAKE_OPENOCD_NO_TARGET)
    service = AgenticHILToolService(config)
    try:
        refused = service.call("probe_target")
        second = service.call("probe_target")

        assert refused["error_type"] == "target_not_detected"
        assert refused["retry_safe"] is True
        assert refused["hardware_state"] == "unchanged"
        assert refused["lease_state"] == "released"
        assert_refused_without_quarantine(refused, config)
        assert service.coordinator.blocked is False
        assert service.coordinator.leases == {}
        # The bench stays in service: the second call is the same refusal, not
        # resource_quarantined.
        assert second["error_type"] == "target_not_detected"
    finally:
        service.close()
    assert_no_quarantine_record(config)


def test_a_probe_listing_that_fails_refuses_instead_of_quarantining(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path)
    service = AgenticHILToolService(config)
    monkeypatch.setattr(
        service.backend,
        "list_probes",
        lambda: {"ok": False, "tool": "debugger_probes_list", "error_type": "probe_discovery_failed", "summary": "enumeration failed"},
    )
    try:
        refused = service.call("debugger_probes_list")

        assert refused["error_type"] == "probe_discovery_failed"
        assert refused["retry_safe"] is True
        assert_refused_without_quarantine(refused, config)
        assert service.coordinator.blocked is False
    finally:
        service.close()


def test_an_stlink_probe_that_enumerates_nothing_never_touched_the_bench(tmp_path: Path) -> None:
    config = config_for(tmp_path, debugger_type="stlink", debugger_executable=FAKE_STLINK_NO_PROBE)
    service = AgenticHILToolService(config)
    try:
        refused = service.call("probe_target")

        assert refused["error_type"] == "adapter_not_found"
        assert refused["target_contacted"] is False
        assert refused["side_effect_status"] == "not_started"
        assert_refused_without_quarantine(refused, config)
    finally:
        service.close()


def test_a_readonly_result_claiming_an_unknown_effect_still_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The self-resolving middle class stays, and it is the one the backend's own
    claim defines: the target was never contacted, so its run state is not in
    question, but the read still reports an effect nobody can account for. Only
    the host's bookkeeping is unsettled, so a read-only re-read is the predicate
    that settles it and the reason is the machine-recoverable one."""
    service = AgenticHILToolService(config_for(tmp_path))
    monkeypatch.setattr(
        service.backend,
        "probe_target",
        lambda: {"ok": False, "tool": "probe_target", "error_type": "probe_failed", "target_contacted": False, "side_effect_status": "unknown", "summary": "unconfirmed"},
    )
    try:
        result = service.call("probe_target")

        assert result["quarantined"] is True
        assert result["cleanup_reasons"] == ["debugger_readonly_result_unconfirmed"]
        assert service.coordinator.blocked is True
    finally:
        service.close()


def test_a_readonly_result_that_names_no_abort_point_needs_the_stronger_predicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same unknown effect without the backend's claim. Nothing here places
    the abort point before the target, so the run state is in question too and
    the reason may not be one a bare re-read clears."""
    service = AgenticHILToolService(config_for(tmp_path))
    monkeypatch.setattr(
        service.backend,
        "probe_target",
        lambda: {"ok": False, "tool": "probe_target", "error_type": "probe_failed", "side_effect_status": "unknown", "summary": "unconfirmed"},
    )
    try:
        result = service.call("probe_target")

        assert result["quarantined"] is True
        assert result["cleanup_reasons"] == ["debugger_readonly_target_state_unconfirmed"]
        assert service.coordinator.blocked is True
    finally:
        service.close()


def test_a_readonly_result_with_a_broken_audit_trail_still_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = AgenticHILToolService(config_for(tmp_path))
    monkeypatch.setattr(
        service.backend,
        "probe_target",
        lambda: {"ok": False, "tool": "probe_target", "error_type": "target_not_detected", "audit_ok": False, "summary": "audit broke"},
    )
    try:
        result = service.call("probe_target")

        assert result["quarantined"] is True
        assert service.coordinator.blocked is True
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Effectful one-shots: only a proven abort point may skip the quarantine.


def test_a_missing_pyocd_is_a_call_that_never_started(tmp_path: Path) -> None:
    config = config_for(tmp_path, debugger_type="pyocd", debugger_executable=tmp_path / "missing-pyocd.exe")
    service = AgenticHILToolService(config)
    try:
        result = service.call("reset_target", {"mode": "run"})

        assert result["error_type"] == "debugger_not_found"
        assert result["target_contacted"] is False
        assert result["side_effect_status"] == "not_started"
        assert result["retry_safe"] is True
        assert_refused_without_quarantine(result, config)
    finally:
        service.close()


def test_a_missing_stlink_cli_is_a_call_that_never_started(tmp_path: Path) -> None:
    config = config_for(tmp_path, debugger_type="stlink", debugger_executable=tmp_path / "missing-stlink.exe")
    service = AgenticHILToolService(config)
    try:
        result = service.call("reset_target", {"mode": "run"})

        assert result["error_type"] == "debugger_not_found"
        assert result["target_contacted"] is False
        assert_refused_without_quarantine(result, config)
    finally:
        service.close()


def test_an_openocd_script_that_cannot_load_never_reaches_the_bench(tmp_path: Path) -> None:
    """OpenOCD loads -f scripts in its configuration stage and exits before
    `init` when one is missing; the absent init-stage marker is the proof."""
    config = config_for(tmp_path, debugger_executable=FAKE_OPENOCD_MISSING_CFG)
    service = AgenticHILToolService(config)
    try:
        result = service.call("reset_target", {"mode": "halt"})

        assert result["error_type"] == "debugger_config_not_found"
        assert result["backend_error_type"] == "interface_config_not_found"
        assert result["target_contacted"] is False
        assert result["side_effect_status"] == "not_started"
        assert result["retry_safe"] is True
        assert_refused_without_quarantine(result, config)
    finally:
        service.close()


def test_a_flash_whose_scripts_cannot_load_refuses_without_quarantine(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    config = config_for(tmp_path, debugger_executable=FAKE_OPENOCD_MISSING_CFG)
    service = AgenticHILToolService(config)
    try:
        result = service.call("flash_firmware", {"image_path": "build/firmware.elf"})

        assert result["error_type"] == "debugger_config_not_found"
        assert result["target_contacted"] is False
        assert_refused_without_quarantine(result, config)
    finally:
        service.close()


def test_openocd_flash_now_carries_the_init_stage_marker(tmp_path: Path) -> None:
    """The proof channel for the flash path: the init-stage echo rides in front
    of `program`, which runs `init` itself and guards against running twice, so
    the command's effect is unchanged and its abort point becomes visible."""
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    config = config_for(tmp_path, debugger_executable=FAKE_OPENOCD)
    service = AgenticHILToolService(config)
    try:
        result = service.call("flash_firmware", {"image_path": "build/firmware.elf"})
    finally:
        service.close()

    assert result["ok"] is True, result
    logged = json.loads((tmp_path / result["log_path"]).read_text(encoding="utf-8"))["command"]
    assert "AGENTIC_HIL_STAGE:init:ok" in logged
    assert logged.index("AGENTIC_HIL_STAGE:init:ok") < logged.index("program")


def test_a_reset_the_openocd_fake_aborts_after_init_still_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction: once the init marker printed, the adapter was open,
    and no error classification may talk the quarantine away."""
    service = AgenticHILToolService(config_for(tmp_path))
    monkeypatch.setattr(
        service.backend,
        "reset_target",
        lambda mode="run": {
            "ok": False,
            "tool": "reset_target",
            "error_type": "reset_failed",
            "side_effect_status": "unknown",
            "summary": "reset unconfirmed",
        },
    )
    try:
        result = service.call("reset_target", {"mode": "halt"})

        assert result["quarantined"] is True
        assert result["cleanup_reasons"] == ["debugger_result_unconfirmed"]
        assert service.coordinator.blocked is True
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Debug sessions: failures before any process exists refuse, later ones keep
# quarantining.


def test_a_debug_server_that_cannot_spawn_refuses_without_quarantine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    config = config_for(tmp_path, gdb_executable=FAKE_GDB)
    service = AgenticHILToolService(config)
    monkeypatch.setattr("agentic_hil.backends.gdbdebug.spawn_managed_process", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn refused")))
    try:
        result = service.call("debug_start_session", {"image_path": "build/firmware.elf"})

        assert result["error_type"] == "debugger_not_found"
        assert result["target_contacted"] is False
        assert result["side_effect_status"] == "not_started"
        assert_refused_without_quarantine(result, config)
        assert service.coordinator.blocked is False
        assert service._debug_lease is None
    finally:
        service.close()


def test_a_missing_gdb_refuses_a_debug_session_without_quarantine(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    config = config_for(tmp_path, gdb_executable=tmp_path / "missing-gdb.exe")
    service = AgenticHILToolService(config)
    try:
        result = service.call("debug_start_session", {"image_path": "build/firmware.elf"})

        assert result["error_type"] == "gdb_not_found"
        assert result["target_contacted"] is False
        assert_refused_without_quarantine(result, config)
    finally:
        service.close()


# ---------------------------------------------------------------------------
# COM ports: an open that provably rolled back refuses; an unconfirmed handle
# keeps quarantining.


def com_config(tmp_path: Path):
    return config_for(tmp_path, com_ports_yaml='com_ports:\n  dut:\n    device: "/dev/ttyAGENTIC_HILTEST"\n')


def failing_serial_module(handle) -> SimpleNamespace:
    return SimpleNamespace(Serial=lambda *args, **kwargs: handle)


def test_an_unopenable_com_port_refuses_and_frees_the_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dominant serial failure — device absent, port busy, adapter
    unplugged — used to hold the bench for a physical recovery."""
    config = com_config(tmp_path)
    service = AgenticHILToolService(config)
    handle = SimpleNamespace(is_open=False)
    handle.open = lambda: (_ for _ in ()).throw(OSError("could not open port"))
    monkeypatch.setitem(sys.modules, "serial", failing_serial_module(handle))
    try:
        result = service.call("com_session_start", {"port_id": "dut"})

        assert result["error_type"] == "com_port_open_failed"
        assert result["cleanup_confirmed"] is True
        assert result["retry_safe"] is True
        assert result["lease_state"] == "released"
        assert_refused_without_quarantine(result, config)
        assert service.coordinator.blocked is False
        assert service.coordinator.leases == {}
    finally:
        service.close()
    assert_no_quarantine_record(config)


def test_a_com_open_whose_rollback_fails_still_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = com_config(tmp_path)
    service = AgenticHILToolService(config)
    handle = SimpleNamespace(is_open=True)
    handle.open = lambda: (_ for _ in ()).throw(OSError("open failed late"))
    handle.close = lambda: (_ for _ in ()).throw(OSError("close failed too"))
    monkeypatch.setitem(sys.modules, "serial", failing_serial_module(handle))
    try:
        result = service.call("com_session_start", {"port_id": "dut"})

        assert result["error_type"] == "com_port_open_failed"
        assert result["quarantined"] is True
        assert result["cleanup_reasons"] == ["com_open_cleanup_unconfirmed"]
        assert service.coordinator.blocked is True
        # The justified quarantine tells the signer what to verify.
        assert result["quarantine_guidance"][0]["reason"] == "com_open_cleanup_unconfirmed"
    finally:
        service.close()


def test_a_com_open_that_never_built_a_handle_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = com_config(tmp_path)
    service = AgenticHILToolService(config)
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("constructor refused"))))
    try:
        result = service.call("com_session_start", {"port_id": "dut"})

        assert result["error_type"] == "com_port_open_failed"
        assert result["side_effect_status"] == "not_started"
        assert_refused_without_quarantine(result, config)
    finally:
        service.close()


# ---------------------------------------------------------------------------
# CAN buses.


def can_config(tmp_path: Path):
    return config_for(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n')


class FakeCanInitializationError(Exception):
    pass


def test_a_can_adapter_that_never_initialized_refuses_and_frees_the_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path)
    service = AgenticHILToolService(config)
    fake_can = SimpleNamespace(
        Bus=lambda **kwargs: (_ for _ in ()).throw(FakeCanInitializationError("channel vcan0 does not exist")),
        CanInitializationError=FakeCanInitializationError,
    )
    monkeypatch.setitem(sys.modules, "can", fake_can)
    try:
        result = service.call("can_session_start", {"bus_id": "bench"})

        assert result["error_type"] == "can_adapter_open_failed"
        assert result["side_effect_status"] == "not_started"
        assert result["retry_safe"] is True
        assert result["lease_state"] == "released"
        assert_refused_without_quarantine(result, config)
        assert service.coordinator.blocked is False
    finally:
        service.close()
    assert_no_quarantine_record(config)


def test_a_can_open_failure_python_can_cannot_classify_still_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A partially initialized channel participates on the bus, and a generic
    exception cannot disprove one was left behind."""
    config = can_config(tmp_path)
    service = AgenticHILToolService(config)
    fake_can = SimpleNamespace(
        Bus=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("driver fault mid-initialization")),
        CanInitializationError=FakeCanInitializationError,
    )
    monkeypatch.setitem(sys.modules, "can", fake_can)
    try:
        result = service.call("can_session_start", {"bus_id": "bench"})

        assert result["quarantined"] is True
        assert result["cleanup_reasons"] == ["can_open_cleanup_unconfirmed"]
        assert service.coordinator.blocked is True
    finally:
        service.close()


class FlakyRecvBus:
    def __init__(self) -> None:
        self.fail = False

    def recv(self, timeout=0):
        if self.fail:
            raise RuntimeError("device I/O error")
        return None

    def shutdown(self) -> None:
        pass


def test_a_failed_direct_can_read_refuses_without_quarantining_the_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """recv() transmits nothing, so a read failure proves the bus was not
    stimulated by this call."""
    config = can_config(tmp_path)
    service = AgenticHILToolService(config)
    bus = FlakyRecvBus()
    monkeypatch.setitem(sys.modules, "can", SimpleNamespace(Bus=lambda **kwargs: bus, CanInitializationError=FakeCanInitializationError))
    try:
        started = service.call("can_session_start", {"bus_id": "bench"})
        assert started["ok"] is True, started
        bus.fail = True

        result = service.call("can_read", {"bus_id": "bench"})

        assert result["ok"] is False
        assert result["error_type"] == "can_read_failed"
        assert result["retry_safe"] is True
        assert result.get("quarantined") is not True
        assert result.get("cleanup_required") is not True
        assert service.coordinator.blocked is False
        bus.fail = False
    finally:
        service.close()


# ---------------------------------------------------------------------------
# The signature: what a justified quarantine tells the operator.


def test_a_justified_quarantine_names_what_the_signer_must_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = AgenticHILToolService(config_for(tmp_path))
    monkeypatch.setattr(
        service.backend,
        "reset_target",
        lambda mode="run": {"ok": False, "tool": "reset_target", "error_type": "reset_failed", "side_effect_status": "unknown", "summary": "unconfirmed"},
    )
    try:
        result = service.call("reset_target", {"mode": "run"})
        status = service.hardware_lease_status()

        for carrier in (result, status):
            guidance = carrier["quarantine_guidance"]
            assert [entry["reason"] for entry in guidance] == ["debugger_result_unconfirmed"]
            for field in ("attempted", "confirmed", "unknown", "physical_check"):
                assert isinstance(guidance[0][field], str) and guidance[0][field], field
        # The physical check is the criterion the signature attests.
        assert "sign" in status["quarantine_guidance"][0]["physical_check"]
    finally:
        service.close()


def test_lease_status_of_a_second_process_carries_the_same_guidance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator usually reads the incident from another process (the CLI),
    off the persisted record; the guidance must arrive there too."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config)
    monkeypatch.setattr(
        service.backend,
        "reset_target",
        lambda mode="run": {"ok": False, "tool": "reset_target", "error_type": "reset_failed", "side_effect_status": "unknown", "summary": "unconfirmed"},
    )
    try:
        service.call("reset_target", {"mode": "run"})
        observer = HardwareCoordinator(config, "observer")
        status = observer.status()

        assert status["blocked"] is True
        assert [entry["reason"] for entry in status["quarantine_guidance"]] == ["debugger_result_unconfirmed"]
    finally:
        service.close()


def test_a_healthy_status_carries_no_guidance(tmp_path: Path) -> None:
    coordinator = HardwareCoordinator(config_for(tmp_path), "healthy")
    lease = coordinator.acquire("physical:healthy")
    try:
        status = coordinator.status()
        assert status["blocked"] is False
        assert "quarantine_guidance" not in status
    finally:
        lease.release()
        coordinator.close()


def test_an_unknown_reason_gets_the_explicit_fallback_guidance() -> None:
    """An incident persisted by another Agentic HIL version must still be
    resolvable at this one's CLI."""
    details = quarantine_reason_details(["a_reason_from_the_future"])

    assert details[0]["reason"] == "a_reason_from_the_future"
    for field in ("attempted", "confirmed", "unknown", "physical_check"):
        assert details[0][field]
    assert "catalogue" in details[0]["attempted"]


def test_guidance_attaches_only_to_quarantined_results() -> None:
    clean = attach_quarantine_guidance({"ok": True, "cleanup_reasons": ["audit_broken"]})
    quarantined = attach_quarantine_guidance({"ok": False, "quarantined": True, "cleanup_reasons": ["audit_broken"]})

    assert "quarantine_guidance" not in clean
    assert quarantined["quarantine_guidance"][0]["reason"] == "audit_broken"


QUARANTINE_LITERAL = re.compile(
    r"(?:\.quarantine\(|_poison_quietly\(|_quarantine_registered_lease\(lease, )\s*\"([a-z0-9_]+)\""
)
PREFIXED_REASON = re.compile(r"f\"\{reason_prefix\}_([a-z0-9_]+)\"")
# Reasons reaching quarantine calls through variables or ternaries; kept
# explicit so a rename in the source fails this list loudly.
INDIRECT_REASONS = {
    "safe_state_unconfirmed",
    "process_reap_unconfirmed",
    "audit_broken",
    "lease_release_unconfirmed",
    "owner_process_exited_without_release",
    "owner_closed_with_active_lease",
    "debugger_readonly_result_unconfirmed",
    "debugger_readonly_target_state_unconfirmed",
    "debugger_result_unconfirmed",
    "debug_breakpoint_cleanup_unconfirmed",
    "debug_target_state_unconfirmed",
    "debug_session_result_unconfirmed",
}
REASON_PREFIXES = ("config_adopt", "config_create")


def test_every_quarantine_trigger_in_the_source_has_signer_guidance() -> None:
    """The inventory the reclassification demanded, kept honest going forward: a
    new trigger without catalogue guidance fails here by name."""
    reasons: set[str] = set(INDIRECT_REASONS)
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        reasons.update(QUARANTINE_LITERAL.findall(text))
        for suffix in PREFIXED_REASON.findall(text):
            reasons.update(f"{prefix}_{suffix}" for prefix in REASON_PREFIXES)

    missing = sorted(reasons - set(QUARANTINE_REASON_GUIDES))
    assert not missing, f"quarantine reasons without signer guidance: {missing}"
    # And the inventory is not empty by accident of the regexes.
    assert len(reasons) >= 30


# ---------------------------------------------------------------------------
# The abort point has to be the backend's claim, not this layer's inference.


def test_a_probe_read_killed_at_the_deadline_still_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout names no abort point at all.

    OpenOCD's and ST-Link's timeout branches report `timeout` and nothing about
    where the call stopped, and the process was killed rather than allowed to run
    the `shutdown` in its own command string — so a run that had got through
    `init` leaves the core halted with nothing to resume it. Reaping the child
    proves no *future* action; it is not a statement about the target, and
    treating the absent side-effect fields as "unchanged" released a board that
    may be sitting halted. The reason has to be one a re-read cannot settle,
    because a re-read attests reachability and this is about run state."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config)
    monkeypatch.setattr(
        service.backend,
        "probe_target",
        lambda: {"ok": False, "tool": "probe_target", "backend": "openocd", "error_type": "timeout", "summary": "Debugger command timed out."},
    )
    try:
        result = service.call("probe_target")

        assert result["quarantined"] is True
        assert result["cleanup_reasons"] == ["debugger_readonly_target_state_unconfirmed"]
        assert service.coordinator.blocked is True
    finally:
        service.close()


def test_a_timed_out_probe_is_not_cleared_by_a_successful_re_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The incident the timeout raises must outlive a bare read-only re-probe.

    A probe that answers proves the board is reachable. It does not resume a core
    the killed backend may have left halted, so on a bench whose policy permits
    only the read-only predicate the incident stands, `auto_recoverable` is false
    and the operator is the way out."""
    config = config_for(tmp_path, auto_recover="readonly")
    service = AgenticHILToolService(config)
    monkeypatch.setattr(
        service.backend,
        "probe_target",
        lambda: {"ok": False, "tool": "probe_target", "backend": "openocd", "error_type": "timeout", "summary": "Debugger command timed out."},
    )
    try:
        first = service.call("probe_target")
        assert first["cleanup_reasons"] == ["debugger_readonly_target_state_unconfirmed"]
        assert service.coordinator.status()["auto_recoverable"] is False

        # The re-read succeeds, and that is exactly what must not be enough.
        monkeypatch.setattr(service.backend, "probe_target", lambda: dict(DETECTED_PROBE))
        second = service.call("probe_target")

        # Was: the re-read is refused with `resource_quarantined`. A probe is
        # the recovery class now and runs during an incident — but running is
        # not settling, and this test's tooth is the second half. The read-only
        # re-read still cannot answer a reason about the core's run state, so
        # the incident is exactly where it was.
        assert second.get("error_type") != "resource_quarantined"
        assert "incident_resolved" not in second
        assert service.coordinator.status()["cleanup_reasons"] == ["debugger_readonly_target_state_unconfirmed"]
        assert service.coordinator.blocked is True
        assert service._attempt_machine_recovery() is None
    finally:
        service.close()


def test_a_timed_out_probe_is_cleared_only_by_a_verified_reset_into_halt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same incident under the stronger predicate: the bench's policy allows
    driving the target, so recovery establishes the run state it lost rather than
    re-reading around it, and says which predicate it used."""
    config = config_for(tmp_path, auto_recover="reset_halt")
    service = AgenticHILToolService(config)
    monkeypatch.setattr(
        service.backend,
        "probe_target",
        lambda: {"ok": False, "tool": "probe_target", "backend": "openocd", "error_type": "timeout", "summary": "Debugger command timed out."},
    )
    try:
        assert service.call("probe_target")["cleanup_reasons"] == ["debugger_readonly_target_state_unconfirmed"]

        resets: list[str] = []
        monkeypatch.setattr(service.backend, "probe_target", lambda: dict(DETECTED_PROBE))
        monkeypatch.setattr(
            service.backend,
            "reset_target",
            lambda mode: resets.append(mode) or {"ok": True, "tool": "reset_target", "summary": "Target reset."},
        )
        report = service._attempt_machine_recovery()

        assert report is not None
        assert report["reason"] == "debugger_readonly_target_state_unconfirmed"
        assert report["safe_state_predicate"] == "reset_halt"
        assert resets == ["halt"]
        assert service.coordinator.blocked is False
    finally:
        service.close()


def test_an_stlink_probe_that_confirms_nothing_quarantines(tmp_path: Path) -> None:
    """The post-`init` case that the timeout rule alone did not cover.

    STM32CubeProgrammer exits 0 with output that confirms no connection: the run
    is over, its child is reaped, and none of that says whether the ST-Link
    attached to the target on the way. `probe_unconfirmed` names no abort point,
    so the board's run state is unknown and containment is the answer — where
    `No STM32 target found` from the same CLI is a claim, and refuses."""
    config = config_for(tmp_path, debugger_type="stlink", debugger_executable=FAKE_STLINK_UNCONFIRMED)
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")

        assert result["backend_error_type"] == "probe_unconfirmed"
        assert result["error_type"] == "target_state_unconfirmed"
        assert result["quarantined"] is True
        assert result["cleanup_reasons"] == ["debugger_readonly_target_state_unconfirmed"]
        assert service.coordinator.blocked is True
    finally:
        service.close()


def test_an_openocd_probe_that_confirms_nothing_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same case on the default backend, which is where it was missed.

    OpenOCD exits 0 with neither its init-stage marker nor the tool's success
    marker in the output. That is the absence of a report, not a report that no
    target answered: `init` may have completed, examined the core and halted it,
    and then lost both echoes. The backend used to answer this branch with the
    classification `target_not_detected`, which is exactly the string the
    read-only pre-contact rule accepts — so a run that confirmed nothing was
    released with `hardware_state: unchanged` on the strength of a verdict
    OpenOCD never gave. It now answers `probe_unconfirmed`, which no evidence
    rule accepts, and the reason is one a re-read may not settle."""
    config = config_for(tmp_path, debugger_executable=FAKE_OPENOCD_UNCONFIRMED, auto_recover="readonly")
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")

        assert result["backend_error_type"] == "probe_unconfirmed"
        # And the public error_type withholds the claim too. `target_not_detected`
        # is OpenOCD's report that it reached the adapter and nothing answered —
        # its catalogue entry says exactly that — so a caller reading only the
        # public field off this branch would have been handed the abort point the
        # backend field refuses to give it.
        assert result["error_type"] == "target_state_unconfirmed"
        assert result.get("target_contacted") is not False
        assert result.get("hardware_state") != "unchanged"
        assert result["quarantined"] is True
        assert result["cleanup_reasons"] == ["debugger_readonly_target_state_unconfirmed"]
        assert service.coordinator.blocked is True
        assert service.coordinator.status()["auto_recoverable"] is False

        # And a read-only re-probe that succeeds does not clear it: it attests
        # the board is reachable, never that a core this run may have halted is
        # running again.
        monkeypatch.setattr(service.backend, "probe_target", lambda: dict(DETECTED_PROBE))
        second = service.call("probe_target")

        # Was: the re-read is refused with `resource_quarantined`. A probe is
        # the recovery class now and runs during an incident — but running is
        # not settling, and this test's tooth is the second half. The read-only
        # re-read still cannot answer a reason about the core's run state, so
        # the incident is exactly where it was.
        assert second.get("error_type") != "resource_quarantined"
        assert "incident_resolved" not in second
        assert service.coordinator.status()["cleanup_reasons"] == ["debugger_readonly_target_state_unconfirmed"]
        assert service.coordinator.blocked is True
        assert service._attempt_machine_recovery() is None
    finally:
        service.close()


def test_a_probe_enumeration_timeout_that_names_its_abort_point_still_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The paired case, so the rule above is about evidence and not about the
    word `timeout`: pyOCD's and ST-Link's probe *enumeration* never addresses a
    target, they say so with `target_contacted: false`, and that explicit claim
    settles the call however it ended."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config)
    monkeypatch.setattr(
        service.backend,
        "list_probes",
        lambda: {
            "ok": False,
            "tool": "debugger_probes_list",
            "error_type": "timeout",
            "summary": "Debugger probe discovery timed out.",
            "target_contacted": False,
            "side_effect_committed": False,
            "side_effect_status": "not_started",
            "retry_safe": True,
        },
    )
    try:
        result = service.call("debugger_probes_list")

        assert result["error_type"] == "timeout"
        assert result["target_contacted"] is False
        assert result["lease_state"] == "released"
        assert_refused_without_quarantine(result, config)
        assert service.coordinator.blocked is False
        # The rule itself, on the fields that carry it: the backend's claim
        # settles the call, and half a claim — or none — does not.
        timed_out = {"ok": False, "error_type": "timeout", "summary": "Debugger command timed out."}
        named = {**timed_out, "target_contacted": False, "side_effect_status": "not_started"}
        assert service._readonly_failure_is_settled(named) is True
        assert service._readonly_failure_is_settled({**timed_out, "target_contacted": False}) is False
        assert service._readonly_failure_is_settled(timed_out) is False
    finally:
        service.close()
    assert_no_quarantine_record(config)


def peak_config(tmp_path: Path):
    return config_for(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "peak"\n    channel: "can0"\n')


def test_a_peak_initialization_error_cannot_prove_it_never_joined_the_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`CanInitializationError` is a class, not a phase.

    python-can's PCAN backend raises `PcanCanInitializationError` — a subclass —
    from four `SetValue` calls that run *after* `PCANBasic.Initialize` has
    already succeeded, and every one of them raises before `BusABC.__init__`, so
    no bus object comes back here to shut down. An initialized PCAN channel is on
    the bus: it ACKs, and at a wrong bitrate it emits error frames. SocketCAN can
    make the claim because its controller is brought up out of band; PCAN cannot,
    so this path keeps the lease contained."""
    config = peak_config(tmp_path)
    service = AgenticHILToolService(config)
    fake_can = SimpleNamespace(
        Bus=lambda **kwargs: (_ for _ in ()).throw(FakeCanInitializationError("SetValue(PCAN_ALLOW_ERROR_FRAMES) failed")),
        CanInitializationError=FakeCanInitializationError,
    )
    monkeypatch.setitem(sys.modules, "can", fake_can)
    try:
        result = service.call("can_session_start", {"bus_id": "bench"})

        assert result["error_type"] == "can_adapter_open_failed"
        assert result.get("side_effect_status") != "not_started"
        assert result["quarantined"] is True
        assert result["cleanup_reasons"] == ["can_open_cleanup_unconfirmed"]
        assert service.coordinator.blocked is True
    finally:
        service.close()
