# Agentic HIL Test Adapter Bridges

Agentic Hardware-in-the-Loop (Agentic HIL) talks to test adapters through a small bridge protocol so that hardware adapters and pure-software simulators are interchangeable. A test adapter simulates what standard lab equipment cannot: realistic sensors, actuator loads, and fault states (open sensor, short to GND/VCC, contact bounce, blocked motor).

## Configure an adapter

```yaml
# Operator-controlled authoritative config outside the workspace
adapters:
  ntc_sim:
    executable: "/operator-controlled/agentic-hil-bridges/sim_ntc_adapter.py"
    channels: ["temperature", "resistance"]   # allowlist enforced by Agentic HIL
    faults: ["open", "short_to_gnd", "short_to_vcc"]
```

Copy the example bridge to an operator-controlled location outside the firmware workspace before adding it to the authoritative config. `executable` is the bridge entry point; generic interpreter arguments are not accepted. `channels` and `faults` are policy: Agentic HIL rejects any unlisted name before the bridge sees the request.

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
response: {"id": <int>, "result": {"ok": true, "protocol_version": 2, ...}}
          {"id": <int>, "result": {"ok": false, "error_type": "...", "summary": "..."}}
```

| Method | Params | Purpose |
|--------|--------|---------|
| `open` | `channels`, `faults` (configured allowlists) | initialize the adapter; return `protocol_version: 2` and `backend` info |
| `set_value` | `channel`, `value`, optional `unit` | drive a simulated sensor/stimulus channel |
| `inject_fault` | `fault`, optional `channel` | enter a fault state |
| `clear_fault` | optional `fault`, optional `channel` | leave fault state(s) |
| `measure` | `channel` | return `value` (+ optional `unit`) for a channel |
| `close` | — | enter device-specific safe state and return `ok: true`, `protocol_version: 2`, and `safe_state_confirmed: true` |

Agentic HIL releases resource ownership only after both an explicit safe-state acknowledgement and verified process-tree reap. Missing/negative acknowledgements quarantine the resource for operator recovery. Agentic HIL enforces permissions (`allow_adapter_read`, `allow_adapter_write`), validates channel/fault names against the config, and logs every action to `.agentic-hil/logs/adapter-*.jsonl`.

## Included example

- `sim_ntc_adapter.py` — simulated 10 kΩ NTC (B=3950): set a temperature, measure the resulting resistance, inject open/short faults. Works without any hardware; used by the Agentic HIL test suite.
