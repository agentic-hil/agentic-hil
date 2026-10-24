"""The power-supply backend interface and its value types (#733, architecture section 6).

What crosses the interface is device-neutral: an identity, a channel's status
and a fault. No command string and no raw byte. The types are pinned here
field by field, because every backend and the class layer above them are
written against exactly these names, and a field that quietly appears or goes
would reshape every one of them.
"""

from __future__ import annotations

import dataclasses
import inspect
from decimal import Decimal

import pytest

STATUS_FIELDS = (
    "set_voltage_v",
    "set_current_a",
    "measured_voltage_v",
    "measured_current_a",
    "output_on",
    "regulation",
    "protection_tripped",
    "ovp_threshold_v",
    "ocp_threshold_a",
    "read_at",
)

BACKEND_OPERATIONS = (
    "identity",
    "capabilities",
    "channel_status",
    "set_voltage",
    "set_current",
    "set_output",
    "arm_protection",
    "evidence",
    "classify_last_error",
    "close",
)


@pytest.mark.parametrize("error_type", ["power_supply_no_reply", "power_supply_protection_not_armed"])
def test_a_power_supply_fault_carries_its_catalogue_type_summary_and_whether_it_wrote(error_type: str) -> None:
    from agentic_hil.types import PowerSupplyFault

    fault = PowerSupplyFault(error_type, "The supply did not answer VSET1? within 0.5 s.", wrote=True)

    assert isinstance(fault, Exception)
    assert (fault.error_type, fault.summary, fault.wrote) == (error_type, "The supply did not answer VSET1? within 0.5 s.", True)
    assert str(fault) == "The supply did not answer VSET1? within 0.5 s."


@pytest.mark.parametrize("error_type", ["power_supply_readback_mismatch", "power_supply_identity_unknown", "not_supported", "timeout", ""])
def test_a_power_supply_fault_refuses_every_type_but_the_two_a_backend_may_raise(error_type: str) -> None:
    from agentic_hil.types import PowerSupplyFault

    with pytest.raises(ValueError) as refused:
        PowerSupplyFault(error_type, "anything", wrote=False)

    assert "power_supply_no_reply" in str(refused.value) and "power_supply_protection_not_armed" in str(refused.value)


def test_whether_a_fault_wrote_is_said_by_name() -> None:
    from agentic_hil.types import PowerSupplyFault

    with pytest.raises(TypeError):
        PowerSupplyFault("power_supply_no_reply", "anything", True)  # type: ignore[misc]
    with pytest.raises(TypeError):
        PowerSupplyFault("power_supply_no_reply", "anything")  # type: ignore[call-arg]


def test_a_supply_identity_is_model_firmware_acceptance_and_a_serial_its_repr_never_shows() -> None:
    from agentic_hil.types import SupplyIdentity

    serial = "psu-serial-6f1c2a"
    identity = SupplyIdentity(model="RS-3005P", firmware="V6.9", accepted=True, serial=serial)

    assert [field.name for field in dataclasses.fields(SupplyIdentity)] == ["model", "firmware", "accepted", "serial"]
    assert (identity.model, identity.firmware, identity.accepted, identity.serial) == ("RS-3005P", "V6.9", True, serial)
    assert serial not in repr(identity) and serial not in str(identity)
    assert SupplyIdentity(model=None, firmware=None, accepted=False).serial is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.serial = "other"  # type: ignore[misc]


def test_a_channel_status_names_every_reading_and_none_is_what_cannot_be_read() -> None:
    from agentic_hil.types import ChannelStatus

    assert tuple(field.name for field in dataclasses.fields(ChannelStatus)) == STATUS_FIELDS
    unread = ChannelStatus(**{name: None for name in STATUS_FIELDS if name != "read_at"}, read_at="2026-10-22T12:00:00Z")
    assert all(getattr(unread, name) is None for name in STATUS_FIELDS if name != "read_at")
    with pytest.raises(TypeError):
        ChannelStatus(Decimal("24.00"))  # type: ignore[misc]
    with pytest.raises(TypeError):
        ChannelStatus(read_at="2026-10-22T12:00:00Z")  # type: ignore[call-arg]
    with pytest.raises(dataclasses.FrozenInstanceError):
        unread.output_on = True  # type: ignore[misc]


def test_the_backend_protocol_has_exactly_the_device_neutral_operations() -> None:
    from agentic_hil.debugger import PowerSupplyBackend

    declared = tuple(name for name, value in vars(PowerSupplyBackend).items() if inspect.isfunction(value) and not name.startswith("_"))

    assert declared == BACKEND_OPERATIONS


def test_arm_protection_takes_a_threshold_or_none_for_each_protection() -> None:
    from agentic_hil.debugger import PowerSupplyBackend

    signature = inspect.signature(PowerSupplyBackend.arm_protection)

    assert list(signature.parameters) == ["self", "channel", "ovp_v", "ocp_a"]
    assert signature.parameters["ovp_v"].annotation == "Decimal | None"
    assert signature.parameters["ocp_a"].annotation == "Decimal | None"


@pytest.mark.parametrize(("operation", "parameters"), [("set_voltage", ["self", "channel", "volts"]), ("set_current", ["self", "channel", "amps"]), ("set_output", ["self", "channel", "on"]), ("channel_status", ["self", "channel"])])
def test_each_channel_operation_names_its_channel(operation: str, parameters: list[str]) -> None:
    from agentic_hil.debugger import PowerSupplyBackend

    assert list(inspect.signature(getattr(PowerSupplyBackend, operation)).parameters) == parameters
