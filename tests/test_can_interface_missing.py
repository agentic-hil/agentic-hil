"""A SocketCAN channel that is not on this host refuses; it does not quarantine.

hardci-hq#127. The bench report was `[Errno 19] No such device` on a `can0` that
had vanished after a PCAN re-enumeration, and the result was a quarantine an
operator had to sign off with `recover --confirm-safe-state`. ENODEV at bind time
is the one open failure that is not merely unproven: there is no netdev, so the
socket had nothing to bind to and no controller was ever addressed.

The classifier that missed it was written against an expected exception class
rather than against what the library throws, so the tests below are deliberately
split: the library facts are asserted against the real installed python-can, and
the behaviour is driven through a `Bus` that raises the real exception classes.
None of it needs a CAN interface, which is also why it runs on Windows.
"""

from __future__ import annotations

import errno
import json
import sys
from pathlib import Path

import pytest
from conftest import write_config

from agentic_hil.can import open_python_can_adapter, socketcan_interface_missing
from agentic_hil.config import load_config
from agentic_hil.knowledge import CAN_INTERFACE_NOT_FOUND_ERROR, catalogue_entry
from agentic_hil.tools import AgenticHILToolService

# Distinctive per hardci-hq#116: device locks are machine-wide, so a bus id and
# channel shared with another clone's tests would contend across checkouts.
BUS_ID = "enodev_probe_bus"
MISSING_CHANNEL = "can127missing"


def can_config(tmp_path: Path, *, channel: str = MISSING_CHANNEL, adapter: str = "socketcan"):
    yaml = "".join(
        [
            "can_buses:\n",
            f"  {BUS_ID}:\n",
            f'    adapter: "{adapter}"\n',
            f'    channel: "{channel}"\n',
            "    listen_only: false\n",
        ]
    )
    return load_config(str(write_config(tmp_path, can_buses_yaml=yaml)))


def coordination_record_states(config) -> set[str]:
    records = Path(config.state_root) / "coordination" / "records"
    if not records.is_dir():
        return set()
    return {state for path in records.glob("*.json") if isinstance(state := json.loads(path.read_text(encoding="utf-8")).get("state"), str)}


def real_can_module():
    return pytest.importorskip("can", reason="python-can is an optional extra")


# ---------------------------------------------------------------------------
# Library facts. Asserted against what is installed, because assuming them is
# what built the defect.


def test_python_can_never_raises_an_initialization_error_for_a_failed_bind() -> None:
    """The premise correction, pinned.

    `CanOperationError` is not a subclass of `CanInitializationError`, so the
    SocketCAN branch's `isinstance` check against the initialization class cannot
    catch a bind failure however it is wrapped — and `SocketcanBus.__init__`
    re-raises the bare `OSError` from `bind_socket` in the first place. Either
    way the number is in the chain and the class is not the one being tested for.
    """
    can = real_can_module()

    assert not issubclass(can.CanOperationError, can.CanInitializationError)
    assert not issubclass(OSError, can.CanInitializationError)
    # python-can copies the OS number onto its own wrapper, which is the second
    # place `raised_errno` looks.
    assert can.CanOperationError("failed to bind", errno.ENODEV).error_code == errno.ENODEV


def test_the_socketcan_bind_is_where_a_missing_interface_is_found() -> None:
    """`bind_socket` is what raises for a nonexistent interface, and it runs
    inside the constructor — which is what makes "constructor time only" a real
    boundary and not a hopeful one."""
    real_can_module()
    import inspect

    from can.interfaces.socketcan import socketcan

    source = inspect.getsource(socketcan.SocketcanBus.__init__)

    assert "bind_socket(" in source
    assert "OSError" in source


# ---------------------------------------------------------------------------
# Classification.


def raising_bus(error: BaseException):
    def factory(**kwargs: object):
        raise error

    return factory


def operation_error_from_enodev(can, channel: str = MISSING_CHANNEL) -> BaseException:
    """The shape the bench met: the OS number reached through a wrapper's cause.

    Built by raising and catching so `__cause__` is set the way `raise ... from
    ...` sets it, rather than by assigning the attribute.
    """
    try:
        raise OSError(errno.ENODEV, "No such device")
    except OSError as os_error:
        try:
            raise can.CanOperationError(f"Failed to bind {channel}", errno.ENODEV) from os_error
        except can.CanOperationError as wrapped:
            return wrapped


def test_a_wrapped_enodev_is_classified_as_no_contact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    can = real_can_module()
    config = can_config(tmp_path)
    monkeypatch.setattr(can, "Bus", raising_bus(operation_error_from_enodev(can)))

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["ok"] is False
    assert result["error_type"] == CAN_INTERFACE_NOT_FOUND_ERROR
    assert result["channel"] == MISSING_CHANNEL
    assert result["side_effect_committed"] is False
    assert result["side_effect_status"] == "not_started"
    assert result["retry_safe"] is True
    assert result["target_contacted"] is False
    assert any("ip link" in step for step in result["remediation"])


def test_a_bare_oserror_carrying_enodev_is_classified_the_same_way(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """python-can 4.6.1's `SocketcanBus.__init__` re-raises the bind's `OSError`
    unwrapped, so the classifier has to read the number on the exception itself
    as well as through a cause chain."""
    can = real_can_module()
    config = can_config(tmp_path)
    monkeypatch.setattr(can, "Bus", raising_bus(OSError(errno.ENODEV, "No such device")))

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["error_type"] == CAN_INTERFACE_NOT_FOUND_ERROR


@pytest.mark.parametrize("number", [errno.ENETDOWN, errno.EPERM, errno.ENXIO])
def test_every_other_errno_keeps_the_markerless_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, number: int) -> None:
    """Narrow by construction. An interface that exists and is down was bound to
    by something, and a permission failure says nothing about the controller;
    both stay unknowns and keep earning their quarantine."""
    can = real_can_module()
    config = can_config(tmp_path)
    monkeypatch.setattr(can, "Bus", raising_bus(can.CanOperationError("failed", number)))

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["error_type"] == "can_adapter_open_failed"
    assert "side_effect_status" not in result


def test_the_peak_adapter_is_left_alone(tmp_path: Path) -> None:
    """PCAN's `Initialize` succeeds before the four `SetValue` calls that raise,
    so an initialized channel is already on the bus. ENODEV there is not the same
    claim and this classification does not reach it."""
    config = can_config(tmp_path, channel="PCAN_USBBUS1", adapter="peak")

    assert socketcan_interface_missing(OSError(errno.ENODEV, "No such device"), BUS_ID, config.can_buses[BUS_ID]) is None


def test_the_errno_is_read_from_the_chain_and_not_from_the_message(tmp_path: Path) -> None:
    """A message that merely says the words is not the number, and the number
    reached through a cause is."""
    config = can_config(tmp_path)
    bus_config = config.can_buses[BUS_ID]

    assert socketcan_interface_missing(RuntimeError("[Errno 19] No such device"), BUS_ID, bus_config) is None
    assert socketcan_interface_missing(OSError(errno.ENODEV, "Aucun peripherique de ce type"), BUS_ID, bus_config) is not None


def test_a_self_referential_exception_chain_terminates(tmp_path: Path) -> None:
    config = can_config(tmp_path)
    looping = RuntimeError("first")
    looping.__cause__ = looping

    assert socketcan_interface_missing(looping, BUS_ID, config.can_buses[BUS_ID]) is None


# ---------------------------------------------------------------------------
# Through the service: the bench case end to end.


def test_a_missing_socketcan_interface_refuses_and_frees_the_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bench case (hardci-hq#127). Fails on the state before this change:
    the open ran into the markerless branch, the lease was quarantined
    `can_open_cleanup_unconfirmed`, and an operator had to sign the incident off.
    """
    can = real_can_module()
    config = can_config(tmp_path)
    service = AgenticHILToolService(config)
    monkeypatch.setattr(can, "Bus", raising_bus(operation_error_from_enodev(can)))
    try:
        result = service.call("can_session_start", {"bus_id": BUS_ID})

        assert result["ok"] is False
        assert result["error_type"] == CAN_INTERFACE_NOT_FOUND_ERROR
        assert result.get("quarantined") is not True, result
        assert result.get("cleanup_required") is not True, result
        assert "quarantine_guidance" not in result
        assert result["lease_state"] == "released"
        assert result["retry_safe"] is True
        assert service.coordinator.blocked is False
    finally:
        service.close()
    assert not coordination_record_states(config) & {"cleanup_required", "quarantined", "recovery_pending"}


def test_the_refusal_carries_its_own_fix() -> None:
    entry = catalogue_entry(CAN_INTERFACE_NOT_FOUND_ERROR)

    assert entry is not None
    assert any("ip link show" in step for step in entry["remediation"])
    assert any("recover --confirm-safe-state" in step for step in entry["do_not"])


def test_the_classifier_is_reachable_without_a_can_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`socketcan_interface_missing` reads exception attributes and the bus
    entry only. A host with no python-can never gets here — the import failure
    refuses first — but the helper must not be the thing that needs it."""
    config = can_config(tmp_path)
    monkeypatch.setitem(sys.modules, "can", None)

    assert socketcan_interface_missing(OSError(errno.ENODEV, "gone"), BUS_ID, config.can_buses[BUS_ID]) is not None
