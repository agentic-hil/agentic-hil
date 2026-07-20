# Agentic HIL Test Adapter Bridges

Agentic Hardware-in-the-Loop (Agentic HIL) talks to test adapters through a small bridge protocol so that hardware adapters and pure-software simulators are interchangeable. A test adapter simulates what standard lab equipment cannot: realistic sensors, actuator loads, and fault states (open sensor, short to GND/VCC, contact bounce, blocked motor).

## Configure an adapter

```yaml
# .agentic-hil/config.yaml
adapters:
  ntc_sim:
    executable: "examples/adapters/sim_ntc_adapter.py"
    channels: ["temperature", "resistance"]   # allowlist enforced by Agentic HIL
    faults: ["open", "short_to_gnd", "short_to_vcc"]
```

`channels` and `faults` are policy: Agentic HIL rejects any channel or fault name that is not listed, before the bridge ever sees the request.

## Use from an agent (MCP)

```text
adapter_session_start  {"adapter_id": "ntc_sim"}
adapter_set_value      {"adapter_id": "ntc_sim", "channel": "temperature", "value": 85}
adapter_inject_fault   {"adapter_id": "ntc_sim", "fault": "open"}
adapter_measure        {"adapter_id": "ntc_sim", "channel": "resistance"}
adapter_clear_fault    {"adapter_id": "ntc_sim"}
adapter_session_stop   {"adapter_id": "ntc_sim"}
```

Typical diagnosis loop: flash firmware → set 25 °C → assert nominal readings over UART → inject `open` → assert the firmware reports the sensor fault → clear → assert recovery.

## Bridge protocol

The bridge is any executable (Python scripts run via the current interpreter) reading JSON requests line-by-line from stdin and writing JSON responses to stdout:

```text
request:  {"id": <int>, "method": <str>, "params": <object>}
response: {"id": <int>, "result": {"ok": true, ...}}
          {"id": <int>, "result": {"ok": false, "error_type": "...", "summary": "..."}}
```

| Method | Params | Purpose |
|--------|--------|---------|
| `open` | `channels`, `faults` (configured allowlists) | initialize the adapter; return `backend` info |
| `set_value` | `channel`, `value`, optional `unit` | drive a simulated sensor/stimulus channel |
| `inject_fault` | `fault`, optional `channel` | enter a fault state |
| `clear_fault` | optional `fault`, optional `channel` | leave fault state(s) |
| `measure` | `channel` | return `value` (+ optional `unit`) for a channel |
| `close` | — | reset hardware to a safe state, return `safe_state_confirmed: true`, then exit |

Agentic HIL enforces permissions (`allow_adapter_read`, `allow_adapter_write`), validates channel/fault names against the config, logs every action to `.agentic-hil/logs/adapter-*.jsonl`, and kills the bridge process if it does not exit on close. Bridge process exit alone is not considered safe; `close` must return `{"ok": true, "safe_state_confirmed": true}` after relays, loads, and injected faults are reset.

## Included example

- `sim_ntc_adapter.py` — simulated 10 kΩ NTC (B=3950): set a temperature, measure the resulting resistance, inject open/short faults. Works without any hardware; used by the Agentic HIL test suite.
