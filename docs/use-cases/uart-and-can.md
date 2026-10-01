# Use case: drive UART and CAN and read the board's answer back

**The problem.** A verified flash says the image is on the chip, not that the
firmware works. The check is to send the board a command over its serial line,
or a frame on its CAN bus, and judge what comes back. Done with a terminal
program, `cansend` or `candump`, the agent reads free text, holds a port nobody
knows is held, and can transmit on a bus it was only meant to watch.

**What Agentic Hardware-in-the-Loop (Agentic HIL) does instead.** Serial ports
and CAN buses are named entries in the operator's configuration, and the agent
reaches them only through tools (`com_session_start`, `com_write`, `com_read`,
and `can_session_start`, `can_send`, `can_read`) or the matching plan steps
(`uart_open`, `uart_write`, `uart_read`, `can_open`, `can_send`, `can_read`).
A read in a plan carries a comparator, which is a claim about the answer. The
run judges the claim, and the report keeps the claim next to what the board
actually said, whether it matched or not.

## Serial: a claim, and what the board said

The [STM32 starter](https://github.com/agentic-hil/stm32-starter)'s firmware
speaks a small line protocol on the ST-LINK's virtual COM port. Its nominal
plan, with the comments removed:

```yaml
version: 3
name: nucleo-f446re-nominal-status
steps:
  - device: dut
    action: flash
    image_path: build/Debug/stm32-starter.elf
    reset_after_flash: false
  - device: dut_uart
    action: uart_open
    clear_buffer: true
  - device: dut
    action: reset
    mode: run
  - device: dut_uart
    action: uart_read
    comparator:
      pattern: "\"event\":\"boot\""
    timeout_s: 5
  - device: dut_uart
    action: uart_write
    text: "STATUS\n"
  - device: dut_uart
    action: uart_read
    comparator:
      pattern: "\"state\":\"READY\",\"diagnostic\":\"NONE\""
    timeout_s: 5
```

The port is opened before the reset and its buffer cleared, so the boot line is
captured and every claim after it is a claim about this boot. The last read, as
the run on Linux reported it (Agentic HIL 0.21.5, OpenOCD, 2026-09-15,
[record](https://github.com/agentic-hil/stm32-starter/blob/main/validation/2026-09-15-newcomer-linux/README.md)),
abridged:

```text
  - 6
      route       dut_uart
      action      uart_read
      result
        Expected pattern matched the COM port output.
        port_id                 dut_uart
        timeout_s               5.0
        bytes_received          39
        reads                   1
        matched_text_truncated  no
        comparator
          pattern  "state":"READY","diagnostic":"NONE"
        matched_text
          text      "state":"READY","diagnostic":"NONE"
          encoding  utf-8
```

### When the answer is wrong

The starter's diagnostic plan makes the same start, then sends `DIAG ON` and
claims the board reports its self-test fault:

```yaml
  - device: dut_uart
    action: uart_write
    text: "DIAG ON\n"
  - device: dut_uart
    action: uart_read
    comparator:
      pattern: "\"state\":\"DEGRADED\",\"diagnostic\":\"E_SELF_TEST\""
    timeout_s: 5
```

The firmware ships with a planted defect, and the same run reported, abridged:

```text
Failed: comparator_unmet
...
    - 6
        route       dut_uart
        action      uart_read
        result
          Expected pattern did not match the COM port output before this step's
          timeout.
          error_type  comparator_unmet
          timeout_s                5.0
          bytes_received           39
          reads                    2
          comparator
            pattern  "state":"DEGRADED","diagnostic":"E_SELF_TEST"
          received_tail
            text      {"state":"READY","diagnostic":"NONE"}
```

The claim and the answer sit side by side: the board took `DIAG ON` and went on
reporting itself healthy. That points at the command parser, which compared
against `DIAG ENABLE`, a command the protocol never sends. One string literal
changed, no plan was touched, and the three plans went green on the rebuilt
image. The run had stopped at the failing step, closed the serial session in its
cleanup, and recovered the probe on its own (`reap_processes, reset_halt,
probe_target`, `outcome recovered`). The run on Windows (0.21.1, STM32CubeProgrammer
CLI, 2026-09-03,
[record](https://github.com/agentic-hil/stm32-starter/blob/main/validation/2026-09-03-bench-windows/README.md))
reported the same failure the same way, in text and in hex.

### What holds the serial line

- A port is bound by its USB serial number as well as its device path, and the
  session confirms that identity when it opens. A port name that has moved to
  another board, after a replug or with a second adapter attached, is refused
  as `com_port_identity_mismatch`, with nothing opened and nothing written to
  either board.
- An entry can leave DTR and RTS unasserted on open (`assert_dtr: false`,
  `assert_rts: false`), so opening the port does not reset a board that wires
  them to its reset line.
- Writing needs `allow_write` on the port. Reading needs no permission, because
  exclusivity carries that load: a run holds the port for its whole duration,
  so no second reader consumes the answer a claim was waiting for.
- Writes are size-capped and reads are buffer-capped.
- `agentic-hil test-reactor --junit-xml <path>` (from 0.21.2) writes one JUnit
  test case per step beside the JSON report, so a CI dashboard shows the read
  above as a failure of type `comparator_unmet` and the steps after it as
  skipped. [Testing](../testing.md#a-junit-report-for-ci) has the mapping.

## CAN: what is enforced, and what this page cannot show yet

A bus is declared the same way as a port:

```yaml
can_buses:
  dut_can:
    adapter: "socketcan"     # or "peak", or "process" for a custom bridge
    channel: "can0"
    bitrate: 500000
    listen_only: true
    permissions:
      allow_write: false
```

- `socketcan` and `peak` run through python-can, installed with
  `agentic-hil[can]`; a `process` bridge is a program you provide that speaks
  the bridge protocol.
- `listen_only: true` is enforced, not recorded. `peak` sets PCAN's passive
  state and reads the mode back from the driver. `socketcan` reads the kernel's
  control mode and never sets it, so an interface that is up without listen-only
  refuses the session rather than joining the bus as an acknowledging node. A
  `process` bridge has to confirm the mode in its answer to `open`.
- An adapter that cannot be held to it refuses with `can_listen_only_unsupported`
  before the bus is touched, or with `can_listen_only_unconfirmed` after asking,
  and closes again. There is no silent downgrade.
- A send on a listen-only bus is refused as `can_listen_only_mode` before any
  driver call, whatever `allow_write` says. A plan with a `can_send` on such a
  bus is refused before its first hardware action.
- `can_read` without `max_frames` reads the bus's configured
  `max_buffer_frames`, which is also its ceiling. A read's comparator can name a
  frame id, claim a pattern over the data bytes and a numeric range for a value
  it captures; [Testing](../testing.md) has a plan that does.

None of the runs on this page opened a CAN bus. The starter's bench declares
none, and `agentic-hil doctor` said so:

```text
CAN buses
  None configured.
```

No published record shows the CAN tools driving a board yet. The rules above are
specified in the [Safety model](../safety-model.md) and held by the
repository's tests; this page does not show them on hardware.

## Limits

- No expected failure. A plan cannot assert that a send is refused or that a
  line stays silent, because the first failing step ends the run.
- No waiting on a condition beyond one read. A read waits for its claim up to
  its `timeout_s` and no longer, and there is no branch, retry or until; from
  plan version 4, `repeat` runs a block a fixed number of times.
- No per-step description that reaches the report, where a step is identified
  by its index, route and action.
- The rest is in [what a plan cannot express yet](../test-plan-contract.md#open).

## Try it

The [STM32 starter](https://github.com/agentic-hil/stm32-starter) runs this loop
end to end: three plans, one planted defect in the firmware, and the failing
report as the agent's starting point. [MCP tools](../mcp-tools.md) has the
serial and CAN tools an agent calls outside a plan, and
[Configuration](../configuration.md) the full port and bus entries.
