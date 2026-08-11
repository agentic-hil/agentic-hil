"""A CAN backend that could not exist refuses; it does not quarantine.

Two bench reports, an hour apart on the same rig. First: a PCAN dongle attached
but the PCAN-Basic API not installed, so `can_session_start` failed with
`backend_error: "PCANBasic library not found."`, carried `side_effect_status:
"unknown"`, and left a dead-owner quarantine on session exit, twice in a row,
each needing a manual `recover`. Then the dongle gone from enumeration entirely,
so `PCANBasic.Initialize` answered PCAN_ERROR_ILLHANDLE, and the same quarantine
followed once per attempt.

Neither of those can have touched a bus. A library that is not installed leaves
no driver object for a call to happen through, and a handle the driver calls
invalid names no channel that could have been opened. Both are the provable
innocence `can_interface_not_found` already established on the SocketCAN side,
on the two places a PCAN bus has it.

The split of this file follows the one `test_can_interface_missing.py`
established, and for the reason that file gives: the classifier that missed the
SocketCAN case was written against an expected exception class rather than
against what the library throws. So the library facts below are asserted against
the real installed python-can, and the behaviour is driven through doubles that
raise the real exception classes. None of it needs a CAN adapter, which is also
why it runs on a host with no PCAN driver at all.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from conftest import write_config

from agentic_hil.can import (
    can_adapter_library_missing,
    open_python_can_adapter,
    pcan_available_channels,
    pcan_basic_library_error,
    pcan_illegal_handle_text,
    peak_channel_not_available,
)
from agentic_hil.config import load_config
from agentic_hil.knowledge import (
    CAN_ADAPTER_LIBRARY_MISSING_ERROR,
    CAN_CHANNEL_NOT_AVAILABLE_ERROR,
    catalogue_entry,
)
from agentic_hil.tools import AgenticHILToolService

# Distinctive by design: device locks are machine-wide, so a bus id and channel
# shared with another clone's tests would contend across checkouts.
BUS_ID = "pcan_absent_probe_bus"
# A PCAN channel name, so the Linux channel-shape refusal above the open does not
# fire and the classifier under test is the one that answers.
CHANNEL = "PCAN_USBBUS16"
# What the PEAK driver's own GetErrorText answers for PCAN_ERROR_ILLHANDLE in
# English. Written out here because this file's doubles stand in for the driver,
# never because the classifier knows it: the classifier asks the installed
# library for this string, and the test below compares the two on any host that
# has the library.
ILLEGAL_HANDLE_TEXT = "The value of a handle (PCAN-Channel, PCAN-Hardware, PCAN-Net, PCAN-Client) is invalid"


def can_config(tmp_path: Path, *, channel: str = CHANNEL, adapter: str = "peak"):
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


def raising_bus(error: BaseException):
    def factory(**kwargs: object):
        raise error

    return factory


class AbsentPcanBasic:
    """A PCAN-Basic API that will not load, as the real one reports it."""

    def __init__(self) -> None:
        raise OSError("PCANBasic library not found.")


class LoadedPcanBasic:
    """The two driver calls this classifier makes, and nothing else.

    A double for the *driver*, not for the classifier: the production path asks
    the installed PCAN-Basic API for the text of PCAN_ERROR_ILLHANDLE, and this
    stands in for that API on a host that has no PEAK driver installed. What it
    answers is checked against the real library, where there is one, by
    `test_the_illegal_handle_text_comes_from_the_installed_driver`.
    """

    def GetErrorText(self, error, language):  # noqa: N802 - the vendor API's spelling
        from can.interfaces.pcan.basic import PCAN_ERROR_ILLHANDLE, PCAN_ERROR_OK

        if error == PCAN_ERROR_ILLHANDLE and language == 0x9:
            return (PCAN_ERROR_OK, ILLEGAL_HANDLE_TEXT.encode("utf-8"))
        return (PCAN_ERROR_ILLHANDLE, b"")


def with_loaded_driver(monkeypatch: pytest.MonkeyPatch, *, channels: list[str] | None = None) -> None:
    """A host where the PCAN-Basic API loads and enumerates what it is told to."""
    from can.interfaces.pcan import basic, pcan

    monkeypatch.setattr(basic, "PCANBasic", LoadedPcanBasic)
    monkeypatch.setattr(
        pcan.PcanBus,
        "_detect_available_configs",
        staticmethod(lambda: [{"interface": "pcan", "channel": name} for name in (channels or [])]),
    )


def initialization_error(can_module, message: str) -> BaseException:
    from can.interfaces.pcan.pcan import PcanCanInitializationError

    return PcanCanInitializationError(message)


# ---------------------------------------------------------------------------
# Library facts. Asserted against what is installed, because assuming them is
# what built the defect this file removes.


def test_python_can_names_an_interface_it_cannot_import_with_its_own_class() -> None:
    """The first of the two shapes, driven rather than described: python-can
    answers an interface it cannot provide with `CanInterfaceNotImplementedError`
    and nothing else, which is why the classifier tests for that class."""
    can = real_can_module()

    with pytest.raises(can.CanInterfaceNotImplementedError):
        can.Bus(interface="not_a_real_interface_for_this_test", channel="x")

    assert issubclass(can.CanInterfaceNotImplementedError, can.CanError)


def test_the_pcan_bus_reaches_for_the_driver_before_it_does_anything_else() -> None:
    """Why a host with no PCAN-Basic API proves no contact for any peak failure.

    `PcanBus.__init__` constructs `PCANBasic()` as its first statement, and that
    constructor raises when the library cannot be found or cannot be loaded. So
    on such a host nothing after that line ran, whatever exception came back.
    """
    real_can_module()
    from can.interfaces.pcan import basic, pcan

    body = [line.strip() for line in inspect.getsource(pcan.PcanBus.__init__).splitlines() if line.strip()]
    first_statement = next(line for line in body if line.startswith("self."))
    assert first_statement == "self.m_objPCANBasic = PCANBasic()"

    loader = inspect.getsource(basic.PCANBasic.__init__)
    assert "find_library(" in loader
    assert "library not found" in loader
    assert "could not be loaded" in loader


def test_the_initialize_refusal_comes_before_every_setvalue_call() -> None:
    """The boundary the channel classifier depends on, read off the library.

    `Initialize` is checked and raises before the four `SetValue` calls, and only
    those four run on a channel that is already on the bus. That ordering is what
    makes "ILLHANDLE from Initialize" a claim about a channel that was never
    opened rather than about one that was.
    """
    real_can_module()
    from can.interfaces.pcan import basic, pcan

    source = inspect.getsource(pcan.PcanBus.__init__)
    assert source.index("raise PcanCanInitializationError") < source.index("SetValue(")
    assert hasattr(basic, "PCAN_ERROR_ILLHANDLE")


def test_the_initialization_error_carries_the_text_and_not_the_status_code() -> None:
    """Why the classifier compares a string at all.

    `PcanBus` raises `PcanCanInitializationError(self._get_formatted_error(result))`,
    which passes the driver's text and drops the status code, so there is no
    number left on the exception to read. The classifier therefore asks the same
    `GetErrorText` for the same code rather than hard-coding the sentence.
    """
    can = real_can_module()
    error = initialization_error(can, "whatever the driver said")

    assert error.error_code is None
    assert "PcanCanInitializationError(self._get_formatted_error(result))" in inspect.getsource(can.interfaces.pcan.pcan.PcanBus.__init__)
    assert "GetErrorText" in inspect.getsource(can.interfaces.pcan.pcan.PcanBus._get_formatted_error)


def test_the_illegal_handle_text_comes_from_the_installed_driver() -> None:
    """Closes the loop on a bench that has the PEAK driver: what this file's
    double answers is what the real library answers. Skipped where the driver is
    not installed, which is exactly the case the other classifier covers."""
    real_can_module()
    text = pcan_illegal_handle_text()
    if text is None:
        pytest.skip("the PCAN-Basic API is not installed on this host")

    assert text.strip() == ILLEGAL_HANDLE_TEXT


# ---------------------------------------------------------------------------
# Classification: the missing library.


def test_an_interface_python_can_cannot_provide_is_no_contact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    can = real_can_module()
    config = can_config(tmp_path, channel="can0", adapter="socketcan")
    monkeypatch.setattr(can, "Bus", raising_bus(can.CanInterfaceNotImplementedError("Unknown interface type")))

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["error_type"] == CAN_ADAPTER_LIBRARY_MISSING_ERROR, result
    assert result["side_effect_committed"] is False
    assert result["side_effect_status"] == "not_started"
    assert result["retry_safe"] is True
    assert result["target_contacted"] is False
    assert result["remediation"]


def test_an_absent_pcan_basic_api_is_no_contact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first bench report. Fails on the state before this change: the open
    ran into the markerless branch, the lease was quarantined
    `can_open_cleanup_unconfirmed`, and an operator had to sign it off."""
    can = real_can_module()
    from can.interfaces.pcan import basic

    config = can_config(tmp_path)
    monkeypatch.setattr(basic, "PCANBasic", AbsentPcanBasic)
    monkeypatch.setattr(can, "Bus", raising_bus(OSError("PCANBasic library not found.")))

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["error_type"] == CAN_ADAPTER_LIBRARY_MISSING_ERROR, result
    assert result["missing_library"] == "PCAN-Basic API"
    assert result["side_effect_status"] == "not_started"
    assert result["retry_safe"] is True
    assert any("PCAN-Basic" in step for step in result["remediation"])


def test_a_host_with_the_driver_keeps_the_markerless_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Narrow by construction. The four `SetValue` calls run after `Initialize`
    has already put the channel on the bus, so on a host where the API loads and
    the driver did not call the handle invalid, the failure stays the unknown it
    has always been and keeps earning its quarantine."""
    can = real_can_module()
    config = can_config(tmp_path)
    with_loaded_driver(monkeypatch, channels=[CHANNEL])
    monkeypatch.setattr(can, "Bus", raising_bus(initialization_error(can, "An error occurred while setting a value")))

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["error_type"] == "can_adapter_open_failed", result
    assert "side_effect_status" not in result


# ---------------------------------------------------------------------------
# Classification: the channel the driver does not have.


def test_an_illegal_handle_from_initialize_is_no_contact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The second bench report, with the driver's own words for the status code
    it answered."""
    can = real_can_module()
    config = can_config(tmp_path)
    with_loaded_driver(monkeypatch)
    monkeypatch.setattr(can, "Bus", raising_bus(initialization_error(can, ILLEGAL_HANDLE_TEXT)))

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["error_type"] == CAN_CHANNEL_NOT_AVAILABLE_ERROR, result
    assert result["channel"] == CHANNEL
    assert result["field"] == f"can_buses.{BUS_ID}.channel"
    assert result["side_effect_committed"] is False
    assert result["side_effect_status"] == "not_started"
    assert result["retry_safe"] is True
    assert result["target_contacted"] is False


def test_a_channel_missing_from_a_real_enumeration_is_no_contact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The second question, for a driver that words its refusal differently: a
    channel absent from a list of channels the driver does have was not there to
    be opened, and the result names what is."""
    can = real_can_module()
    config = can_config(tmp_path)
    with_loaded_driver(monkeypatch, channels=["PCAN_USBBUS1", "PCAN_USBBUS2"])
    monkeypatch.setattr(can, "Bus", raising_bus(initialization_error(can, "Ein Handle ist ungueltig")))

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["error_type"] == CAN_CHANNEL_NOT_AVAILABLE_ERROR, result
    assert result["available_channels"] == ["PCAN_USBBUS1", "PCAN_USBBUS2"]


def test_an_empty_enumeration_alone_proves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-answer that must not become evidence. `_detect_available_configs`
    returns an empty list both when nothing is attached and when the driver would
    not answer the attached-channel query at all, so emptiness on its own leaves
    the failure exactly as unproven as it was."""
    can = real_can_module()
    config = can_config(tmp_path)
    with_loaded_driver(monkeypatch, channels=[])
    monkeypatch.setattr(can, "Bus", raising_bus(initialization_error(can, "Ein Handle ist ungueltig")))

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["error_type"] == "can_adapter_open_failed", result
    assert "side_effect_status" not in result


def test_the_channel_classifier_leaves_socketcan_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A SocketCAN bus has its own answer and this one does not reach it."""
    can = real_can_module()
    config = can_config(tmp_path, channel="can0", adapter="socketcan")
    with_loaded_driver(monkeypatch)

    assert peak_channel_not_available(initialization_error(can, ILLEGAL_HANDLE_TEXT), BUS_ID, config.can_buses[BUS_ID]) is None


def test_an_operation_error_is_not_an_initialize_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the initialization class, because only it can be raised from the one
    place that runs before the channel is on the bus."""
    can = real_can_module()
    config = can_config(tmp_path)
    with_loaded_driver(monkeypatch)

    assert peak_channel_not_available(can.CanOperationError(ILLEGAL_HANDLE_TEXT), BUS_ID, config.can_buses[BUS_ID]) is None


def test_a_library_that_will_not_load_answers_before_the_channel_question(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Order matters and is asserted: with no driver object there is no channel
    question to ask, so the refusal names the library the host is missing rather
    than sending somebody to look for a dongle that is plugged in."""
    can = real_can_module()
    from can.interfaces.pcan import basic

    config = can_config(tmp_path)
    monkeypatch.setattr(basic, "PCANBasic", AbsentPcanBasic)
    error = initialization_error(can, ILLEGAL_HANDLE_TEXT)

    assert can_adapter_library_missing(error, BUS_ID, config.can_buses[BUS_ID])["error_type"] == CAN_ADAPTER_LIBRARY_MISSING_ERROR
    assert pcan_basic_library_error() == "PCANBasic library not found."
    assert pcan_available_channels() == []


# ---------------------------------------------------------------------------
# Through the service: the bench case end to end, twice.


@pytest.mark.parametrize("shape", ["missing_library", "absent_channel"])
def test_neither_failure_quarantines_the_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    """The whole report. Each of these left a dead-owner quarantine on session
    exit, one per attempt, each needing a manual `recover`. The lease is released
    cleanly now and no coordination record survives the session."""
    can = real_can_module()
    from can.interfaces.pcan import basic

    config = can_config(tmp_path)
    if shape == "missing_library":
        expected = CAN_ADAPTER_LIBRARY_MISSING_ERROR
        monkeypatch.setattr(basic, "PCANBasic", AbsentPcanBasic)
        monkeypatch.setattr(can, "Bus", raising_bus(OSError("PCANBasic library not found.")))
    else:
        expected = CAN_CHANNEL_NOT_AVAILABLE_ERROR
        with_loaded_driver(monkeypatch)
        monkeypatch.setattr(can, "Bus", raising_bus(initialization_error(can, ILLEGAL_HANDLE_TEXT)))
    service = AgenticHILToolService(config)
    try:
        result = service.call("can_session_start", {"bus_id": BUS_ID})

        assert result["ok"] is False
        assert result["error_type"] == expected, result
        assert result.get("quarantined") is not True, result
        assert result.get("cleanup_required") is not True, result
        assert "quarantine_guidance" not in result
        assert result["lease_state"] == "released"
        assert result["retry_safe"] is True
        assert service.coordinator.blocked is False
    finally:
        service.close()

    assert not coordination_record_states(config) & {"cleanup_required", "quarantined", "recovery_pending"}


@pytest.mark.parametrize("key", [CAN_ADAPTER_LIBRARY_MISSING_ERROR, CAN_CHANNEL_NOT_AVAILABLE_ERROR])
def test_each_refusal_carries_its_own_fix(key: str) -> None:
    entry = catalogue_entry(key)

    assert entry is not None
    assert entry["remediation"]
    assert any("recover --confirm-safe-state" in step for step in entry["do_not"])
