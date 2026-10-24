# Bench power supplies

A bench power supply that feeds the board is part of the bench, like the debugger and the ports. Agentic HIL drives it through four tools: read what it is set to and what the board draws, set the voltage and the current limit the board gets, switch the output, and power-cycle the board for a cold boot. Every one of them is bounded by limits that only the board's owner writes, and every write is read back exactly before the call reports success.

The supply is declared in the authoritative configuration under `power_supplies`, with its port, the debuggers whose boards it powers, its limits and its two grants: [Configuration](configuration.md#bench-power-supplies) has the entry.

## The tools

| Tool | Arguments | Grant | What it does |
|---|---|---|---|
| `power_supply_status` | `supply_id`, `channel` | none | Reads every channel, or the one named: the set points, the measured voltage and current, the output state, the effective limits and what protection the supply offers. Writes nothing. |
| `power_supply_set` | `supply_id`, `channel`, `voltage_v`, `current_a` | `allow_set` | Writes the voltage, the current or both, and reads each one back. |
| `power_supply_output` | `supply_id`, `channel`, `on` | `allow_output` | Switches the output on or off and reads the state back. |
| `power_supply_cycle` | `supply_id`, `channel`, `off_s` | `allow_output` | Switches the output off, waits `off_s` seconds (0.5 to 30), switches it on again, and reads the state back after each switch. |

`channel` is numbered from 1 and may be left out on a supply with one channel; `power_supply_status` without it reads them all. Values are exact decimals: a value that is not a whole number of the supply's steps is refused as `invalid_argument` and nothing is rounded, and every value a result carries is written as decimal text, so `3.3` reads back as `3.3` and never as a float near it.

## Limits

The owner sets `voltage_limit_v` and `current_limit_a` for the supply and, under `channels.<n>`, for each output channel. They are written by hand in the authoritative configuration and adopted at the next restart of the server. No tool writes a limit.

- **The effective limit** of a channel is the lower of the supply value and the channel value, for voltage and current separately. The supply's own hardware range is a further bound and never a stand-in: a limit is never defaulted, never taken from that range and never taken from the present set point.
- **A missing limit refuses writes.** When any of the four values for the addressed channel is missing, every write on that channel is refused before contact, output off and power cycle included. The refusal is `permission_denied` with `missing_limits`, the dotted keys the owner has to write. `power_supply_status` still reads the channel and says the same in `writes_refused_reason`.
- **A request above the limit is refused before contact** as `power_supply_limit_exceeded`, with `field`, `requested` and `limit`.
- **The stored set point counts.** `power_supply_output` with `on: true` and `power_supply_cycle` first read the set points already stored on the supply, because they reach the board the moment the output switches on. `power_supply_set` reads them too, and holds every quantity it does not write to the limit, because that value stays in place beside the one it writes. A stored value above the effective limit, or one the supply does not report, refuses the call the same way before anything is written or switched, and names the stored value and the limit. A stored value is never lowered unasked: a set has to write that quantity too, within the limit.
- **Lower before raise.** When `power_supply_set` writes both quantities, every one that falls below its stored value is written before any that rises, so the supply never holds the higher voltage and the higher current together between the two writes.

## Protection

A call that may put power on the board arms the supply's own over-voltage and over-current protection at the effective limits before any other write: `power_supply_set`, `power_supply_output` with `on: true`, and `power_supply_cycle`, which arms once before it switches the output off. Switching the output off and reading the status never arm.

Each supply type declares, per protection, how it works:

- `threshold`: a settable trip level, armed at the effective limit;
- `at_setpoint`: the supply trips at its own set point, so there is no level to place at the limit; reported and never armed;
- `unsupported`: the type has no such protection this server can drive; reported and never armed.

Results and `power_supply_status` carry a `protection` block per channel, `{ovp|ocp: {support, armed, threshold, threshold_read}}`. `armed` is `confirmed` when the threshold reads back exactly as armed, `unconfirmed` when the type cannot read it back, and null when the call did not arm. When the protection cannot be armed, the call answers `power_supply_protection_not_armed`, naming the `protection`, the `requested` threshold, the `read_back` and the `cause` (`backend_fault`, `readback_mismatch` or `readback_missing`), and writes no set point and switches no output.

## What a call that changes the supply does

1. **Every check that needs no contact**, in this order: the supply and the arguments, a backend for its type, the grant, the owner's limits, whether the type can read its output state back (for `power_supply_output` and `power_supply_cycle`), and the requested values against the limits. A refusal here sends nothing to the supply.
2. **This server's own sessions.** A debug session this server holds on a debugger the supply powers refuses the call as `device_busy` with `held_by_debug_session: true`, and a COM session on a port that shares a lock key with the supply or a powered debugger refuses it as `device_busy` with `held_by_com_session`, naming the port. Nothing is sent to the supply, and `next_step` names the call that ends the session ([Watching a cold boot](#watching-a-cold-boot)).
3. **The port's USB identity**, checked before the port is opened, as for every configured COM port: an adapter with another VID, PID or serial is never opened.
4. **The lock**, on the supply and on every debugger it powers (`powers`), all or nothing, so no flash, reset or session reaches a board whose power is being changed. `power_supply_status` locks the supply alone. Inside a run declared with `bench_run_start`, the supply and every powered debugger have to be declared (`{"kind": "power_supply", "id": "<entry>"}`); outside one, a call that passes steps 1 and 2 runs as a run of its own over the same devices.
5. **The unit's identity.** The identity query is the first command sent and the only one until the model, the firmware and, where the entry names one, the `idn_serial` match. A model or firmware this server has no verified record for is refused as `power_supply_identity_unknown`, another unit of the same model as `power_supply_identity_mismatch`.
6. **The stored set points**, for output on and power cycle, and for every quantity a set leaves as it is.
7. **The protection**, armed at the limits.
8. **The writes**, each followed by an exact read-back, every lowered set point before any raised one.
9. **Close and report.** The connection closes whatever happened, and the report keeps the transcript of the exchange.

Each call opens one connection to the supply and closes it. The port of a supply belongs to that supply alone: `com_session_start` and a `flash_firmware` capture on it are refused as `com_port_reserved_for_power_supply`, before the port is touched.

## Watching a cold boot

`power_supply_cycle` restarts the board from cold. To read its boot from the first byte, open the console before the cycle:

1. `com_session_start` on the console's port, with `clear_buffer: true`;
2. `power_supply_cycle`;
3. `com_read`, which returns what the board printed as it came up.

This works whenever the console's port shares no lock key with the supply or with any debugger the supply powers. The session and the cycle then lock different devices, and because this server runs one call at a time, the session stays open across the cycle. Inside a run declared with `bench_run_start`, declare the console, the supply and every powered debugger.

When the keys overlap, the cycle is refused before anything is sent to the supply, as `device_busy` with `held_by_com_session` naming the port, and its `next_step` says to stop the session with `com_session_stop` first. They overlap when:

- the console's entry shares its `resource_id` with a debugger the supply powers, so both lock as one physical device;
- the console is the `com_port` of an esptool debugger the supply powers, as on an ESP32 board whose USB-UART bridge is both the flashing line and the console.

On such a bench the boot cannot be watched on that console, and only the owner can change that, in the authoritative configuration. A debug session this server holds on a powered debugger refuses the same calls with `held_by_debug_session`, until `debug_stop_session` ends it.

## When a write does not hold

- **`power_supply_readback_mismatch`**: the supply answered, and reads back something other than what was written. `requested` and `read_back` say both; the state is known, it is the read-back.
- **`power_supply_no_reply`**: the supply could not be reached. `cause` says why: `no_reply` (it did not answer in time, or answered something unreadable), `port_open_failed` (its port did not open, often because another program holds it), `write_timeout` (a write on its serial line timed out), `serial_error` (the serial line failed in another way) or `not_sent` (the backend refused the command before sending it), and `backend_error` carries the error itself. Before any write nothing changed, and `retry_safe` is true. After a write, whether it took effect is unconfirmed: `retry_safe` is false and the call carries the cleanup reason `power_supply_state_unconfirmed`, whose `quarantine_guidance` names what was attempted, what is confirmed, what is unknown and what to check on the unit itself.

Every failure carries `last_known_output`: the channel, its output state and the time it was last read in this call, all null when nothing was read.

## Serials

A supply's serial number names one physical unit. Every serial this server reads or is configured with, the adapter's USB serial and the serial the supply reports for itself, is masked in every result, report, lock record and log it writes ([Safety model](safety-model.md#bench-power-supply-serials)).

## What a backend provides

All the rules above live in one class layer, the same for every supply type, so a backend cannot skip any of them. A backend speaks its supply's protocol through a device-neutral interface, and nothing protocol-specific crosses it:

- `identity()`: model, firmware, whether this server has a verified record for them, and the unit's serial, used only for the comparison;
- `capabilities()`: the static hardware bounds of the type, read without I/O: the channel count, per channel the voltage and current maximum and resolution, what it measures, whether the output state reads back, and per protection `threshold`, `at_setpoint` or `unsupported` and whether the threshold reads back;
- `channel_status(channel)`: set points, measured values, output state, regulation, a tripped protection and the armed thresholds, each one null where the type does not report it, with the time it was read;
- `set_voltage`, `set_current`, `set_output` and `arm_protection(channel, ovp_v, ocp_a)`, where a threshold of null leaves that protection alone;
- `evidence()`: the transcript of the connection, every serial masked;
- `classify_last_error()` and `close()`.

What a backend cannot do it raises as a typed fault naming a catalogue error type, never as a guess. Behaviour nobody has recorded on a real unit is declared as unsupported until a recording shows it, so the class layer refuses what it cannot confirm instead of assuming it. A configured supply type without a backend in this build answers `not_supported` and touches nothing.

## Two points to read closely

- **`unsupported` is a value, `not_supported` the refusal.** A capability the type lacks is reported with the status value `unsupported` in `protection` and in the type's record. A tool that needs it answers with the error type `not_supported` and names the missing capability in `capability`, the same split the debugger backends use, so a host matches one error type for every capability a backend lacks.
- **A port without a USB serial is bound by its unit serial.** The port of a supply is bound by its VID and PID, a `resource_id` no other device shares and, where the adapter publishes one, its USB serial. Where the adapter publishes none, the port check can only confirm the model of adapter, so the entry has to name `idn_serial`, and the supply's answer to the identity query is compared with it before any other command is sent. On such a port the identity query is the one command a different unit of the same model can receive.

## RS-3005P

Agentic HIL drives an RS-3005P bench power supply over its USB serial port, with
the KA3005P command set at 9600 baud, 8N1 and no flow control. The type is
`rs3005p`: one channel, with a capability range of 30 V and 5 A at a resolution
of 0.01 V and 0.001 A. That range is the hardware bound only. The limits a bench
runs at are the ones the owner writes into the authoritative configuration, and
the range never stands in for them.

On channel 1 it reads the set and the measured voltage and current and the
output state, sets the voltage and the current limit, and switches the output.
Every set point is written exactly as asked, never rounded, and read back with
its own query before the write counts; every switch is read back from the
STATUS byte.

### Recorded

These replies were recorded on 2026-10-22 from a unit on firmware V6.9, with
queries only, and are what the backend decodes:

| Command | Reply | Read as |
|---|---|---|
| `*IDN?` | `RS-3005P V6.9 SN:<serial>` | model, firmware and serial |
| `VSET1?` | `24.00` | set voltage |
| `ISET1?` | `2.000` | set current |
| `VOUT1?` | `24.00` | measured voltage |
| `IOUT1?` | `0.000` | measured current |
| `STATUS?` | one raw byte, `0x53` | kept as evidence, no meaning |

On 2026-10-24 the same firmware was recorded through the tools themselves, with
nothing attached to the output:

| Command | Then read | Read as |
|---|---|---|
| `VSET1:3.30` | `VSET1?` `03.30` | the voltage setter as drafted |
| `ISET1:0.100` | `ISET1?` `0.100` | the current setter as drafted |
| `OUT0` | `STATUS?` `0x12`, `VOUT1?` `00.00` | output off |
| `OUT1` | `STATUS?` `0x53`, `VOUT1?` `03.03` | output on |

Neither a setter nor `OUT0`/`OUT1` answers anything. Bit 6 of the STATUS byte
(`0x40`) is the only bit that follows the output in both directions, and it is
the only bit the backend reads: set means the output is on. Bit 0 changed too
and is given no meaning, because the off state cannot tell regulation apart.
`VOUT1?` read 0.1 s after `OUT1` was still rising, so the switch is confirmed by
the STATUS bit and never by the measured voltage. After the supply's own power
key cycled it, the output came back on at the stored set point: the unit does
not power up with its output off.

A command ending in a newline is answered with a trailing newline, and the
first reply byte arrives 13 to 44 ms after the write. Two commands written back
to back get only the first one answered, so every command waits for the reply
to the one before it.

### Draft

Not yet recorded, and used as drafts until a recording settles them:

- the setter format beyond the two values recorded, `VSET1:12.34` and
  `ISET1:1.234`, with as many places as the query replies carry;
- the wait after a setter or an output switch, 0.1 s, before the read-back;
- the reply timeout, the entry's `timeout_s` (1 s unless the owner sets one);
- the framing of `STATUS?`, read as one byte and an optional newline;
- the capability range of 30 V and 5 A, taken from the datasheet and the model
  name.

### Fails closed

What has not been recorded is refused rather than guessed:

- **Identity.** Only an RS-3005P on firmware V6.9 is driven. Another model or
  firmware is read and named, and nothing after `*IDN?` is sent to it.
- **Regulation and protection trips.** No bit of the STATUS byte is recorded
  for them, so they read as `null`. Output switching is sent only to a firmware
  whose STATUS table names the output bit, which today is V6.9 alone.
- **OVP and OCP.** Both are `unsupported` and neither threshold reads back. No
  OVP or OCP command is ever sent, and a request to arm one fails with
  `power_supply_protection_not_armed` before contact.
- **No reply.** A reply that does not arrive, arrives without its newline, or
  is not a decimal number is `power_supply_no_reply`, and the result says
  whether a set point had already been written. The conversation ends there, so
  a late reply can never be taken as the answer to a later command.
- **A failing serial line.** A port that does not open, a write that times out
  and any other error on the serial line are `power_supply_no_reply` too, with
  `cause` naming which and the error in `backend_error`. A port that did not
  open sent nothing; a setter whose write failed may have reached the supply,
  so its state is unconfirmed. The conversation ends the same way.
- **STATUS framing.** A STATUS reply longer than one byte and a newline is
  `power_supply_no_reply` too, and ends the conversation for the same reason, so
  the read that met it fails and nothing is written after it.
- **Refused before sending.** A command the backend will not send, on an ended
  or closed conversation or to switch an output it cannot read back, is `power_supply_no_reply`
  with the cause `not_sent`. Nothing reached the supply, so its state stays
  confirmed unless an earlier write in the same call changed it.

### Evidence

Every exchange is kept: the command, the full reply as hex with its terminator,
its latency and the time. The `SN:` span of an identity reply is masked with
`*`, keeping its length, before anything is kept; a reply that does not parse as
an identity is masked whole.

### Checking it on a bench

`tests/bench/test_bench_power_supply.py` drives the supply through the MCP
server of a bench workspace, on a bench run (`AGENTIC_HIL_BENCH=1`) where
`AGENTIC_HIL_BENCH_POWER_SUPPLY` names that workspace. It reads the status,
sets half of the owner's effective limit (rounded down to the resolution) and
reads it back exactly, and has a request one step above the limit refused with
`power_supply_limit_exceeded` and nothing changed. A bench run that names no
workspace leaves the test out and says so; a skipped run qualifies nothing.
