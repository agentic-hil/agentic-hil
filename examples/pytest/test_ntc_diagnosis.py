"""Example: temperature-sensor diagnosis loop driven by the Agentic Hardware-in-the-Loop (Agentic HIL) pytest plugin.

Run from a firmware project after copying examples/adapters/sim_ntc_adapter.py to
an operator-controlled location outside that workspace. Point the external
authoritative config's adapters section at the copied bridge, as shown in
examples/adapters/README.md:

    pip install agentic-hil
    pytest test_ntc_diagnosis.py

The `agentic_hil` fixture skips these tests when no Agentic HIL configuration file
exists, so the suite stays green in code-only environments, and it stops
adapter sessions after each test so fault state cannot leak between tests.

These tests only exercise the stimulus side: they assert what the adapter
presents to the device under test. In a real project each fault injection is
paired with assertions on the firmware's reaction, e.g. its diagnosis output
read via com_read.
"""
from __future__ import annotations

import pytest

from agentic_hil.report import overall_success

ADAPTER_ID = "ntc_sim"


@pytest.fixture()
def ntc(agentic_hil):
    started = agentic_hil.call("adapter_session_start", {"adapter_id": ADAPTER_ID})
    # overall_success() is the full contract: ok alone can hide audit_ok: false,
    # side_effect_status: unknown, or a non-active lease_state such as stale.
    assert overall_success(started), started["summary"]
    return agentic_hil  # the plugin stops adapter sessions after each test


def test_nominal_temperature_reading(ntc) -> None:
    set_result = ntc.call("adapter_set_value", {"adapter_id": ADAPTER_ID, "channel": "temperature", "value": 25})
    assert overall_success(set_result)
    resistance = ntc.call("adapter_measure", {"adapter_id": ADAPTER_ID, "channel": "resistance"})
    assert overall_success(resistance)
    assert 9000 < resistance["value"] < 11000  # 10k NTC at 25 degC


def test_open_sensor_fault_is_injectable(ntc) -> None:
    injected = ntc.call("adapter_inject_fault", {"adapter_id": ADAPTER_ID, "fault": "open"})
    assert overall_success(injected)
    resistance = ntc.call("adapter_measure", {"adapter_id": ADAPTER_ID, "channel": "resistance"})
    assert overall_success(resistance)
    assert resistance["value"] >= 1e9  # the adapter now presents an open circuit to the firmware


def test_short_to_gnd_fault_is_injectable(ntc) -> None:
    injected = ntc.call("adapter_inject_fault", {"adapter_id": ADAPTER_ID, "fault": "short_to_gnd"})
    assert overall_success(injected)
    resistance = ntc.call("adapter_measure", {"adapter_id": ADAPTER_ID, "channel": "resistance"})
    assert overall_success(resistance)
    assert resistance["value"] == 0.0  # the adapter now presents a short to the firmware
