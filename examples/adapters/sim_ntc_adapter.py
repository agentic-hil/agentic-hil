#!/usr/bin/env python3
"""Simulated NTC temperature-sensor test adapter for the Agentic HIL adapter bridge protocol.

The bridge speaks JSON per line on stdin/stdout:

    request:  {"id": 1, "method": "set_value", "params": {"channel": "temperature", "value": 85}}
    response: {"id": 1, "result": {"ok": true, "channel": "temperature", "value": 85.0, "unit": "degC"}}

Channels: temperature (degC, settable and measurable), resistance (ohm, measurable)
Faults:   open, short_to_gnd, short_to_vcc

Physical test-adapter hardware implements the same protocol; this simulator
lets agents and CI pipelines exercise the full flash -> stimulate -> assert
loop without hardware attached.
"""
from __future__ import annotations

import json
import math
import sys

R25_OHM = 10_000.0
BETA_K = 3950.0
T25_K = 298.15
KELVIN_OFFSET = 273.15
OPEN_RESISTANCE_OHM = 1e9

CHANNELS = ["temperature", "resistance"]
FAULTS = ["open", "short_to_gnd", "short_to_vcc"]

state = {"temperature_c": 25.0, "fault": None}


def ntc_resistance_ohm(temperature_c: float) -> float:
    temperature_k = temperature_c + KELVIN_OFFSET
    return R25_OHM * math.exp(BETA_K * (1.0 / temperature_k - 1.0 / T25_K))


def measured_resistance_ohm() -> float:
    if state["fault"] == "open":
        return OPEN_RESISTANCE_OHM
    if state["fault"] in {"short_to_gnd", "short_to_vcc"}:
        return 0.0
    return ntc_resistance_ohm(state["temperature_c"])


def ok(**payload: object) -> dict:
    return {"ok": True, **payload}


def error(error_type: str, summary: str) -> dict:
    return {"ok": False, "error_type": error_type, "summary": summary}


def handle(method: str, params: dict) -> dict:
    if method == "open":
        return ok(backend="sim-ntc", channels=CHANNELS, faults=FAULTS)
    if method == "set_value":
        if params.get("channel") != "temperature":
            return error("channel_not_supported", "sim-ntc can only set the temperature channel.")
        try:
            state["temperature_c"] = float(params.get("value"))
        except (TypeError, ValueError):
            return error("invalid_argument", "value must be a number.")
        return ok(channel="temperature", value=state["temperature_c"], unit="degC")
    if method == "inject_fault":
        fault = params.get("fault")
        if fault not in FAULTS:
            return error("fault_not_supported", f"sim-ntc supports: {', '.join(FAULTS)}.")
        state["fault"] = fault
        return ok(fault=fault)
    if method == "clear_fault":
        state["fault"] = None
        return ok(fault=None)
    if method == "measure":
        channel = params.get("channel")
        if channel == "temperature":
            return ok(channel=channel, value=state["temperature_c"], unit="degC", fault=state["fault"])
        if channel == "resistance":
            return ok(channel=channel, value=measured_resistance_ohm(), unit="ohm", fault=state["fault"])
        return error("channel_not_supported", "sim-ntc can only measure temperature and resistance.")
    if method == "close":
        state["fault"] = None
        return ok(safe_state_confirmed=True)
    return error("unknown_method", f"unknown method: {method}")


def main() -> int:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = {"id": request.get("id"), "result": handle(str(request.get("method")), request.get("params") or {})}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
        if request.get("method") == "close":
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
