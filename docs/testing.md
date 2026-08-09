# Running Hardware Tests

Two entry points drive the same permission-gated tools without an MCP client:
the test reactor executes a reviewable YAML plan, and the pytest plugin puts the
bench behind a fixture.

## Test Reactor

Write a hardware test as a plan, not a script: one reviewable YAML file describes flash → stimulate → break → dump, and the reactor guarantees it is either executed exactly as written or rejected before the first hardware action. No half-run plans, no leftover breakpoints, no orphaned debug or UART sessions — the same file behaves identically on your bench and in CI, and it diffs like code in a pull request.

The test reactor executes a strict, sequential YAML or JSON test plan against the named devices in the authoritative config. Every step names the configured entry it drives with one key — `device: <name>` — and the configuration is what knows whether that name is a `debuggers`, a `com_ports` or a `can_buses` entry. The version 2 keys `debugger:`, `port_id:` and `bus_id:` stay valid as readable aliases; a step writes one or the other, never both, and a step that names its device twice is refused rather than resolved by precedence. Every device kind the config models can take part in a plan, and each kind answers for its own actions, permissions, session order and cleanup. The name may be omitted on a debugger step while the config declares exactly one probe — with several, the plan must name one, because picking a board for the author is how the wrong board gets flashed. Every probe carries its own permissions, so a step is judged by the grants of the board it names. Probes other than the bound one run on their own service under one shared project lease. Typed debug actions currently require OpenOCD; flash/UART-only plans can use the other backends.

A step action is a string a device class declares on the method that implements it, so a capability is one decorated method and a device kind is a class of them. A string a kind does not declare is answered `not_supported` naming the kind, by construction rather than by a special case. `delay` is declared once on the base class and is therefore served by every kind: it drives nothing, but it is still a step and appears in the run's result, because a wait that left no trace would be a gap in the record of what a test did.

Feedback steps say what they are claiming, not merely that they read something. `uart_read` with a `comparator:` reads until the claim is met or its timeout passes: `equals:` for a whole value rather than a substring — any one complete line of what was received matches as soon as its terminator arrives, and the whole of what was received matches on the last pass, once the timeout has run out and nothing further is coming, so a value the board never terminates cannot pass while it is still being printed — `pattern:` for a Python regular expression under `re.search`, and `pattern:` with `range:` for a number captured by the pattern's one capture group and held to inclusive bounds. A claim that goes unmet fails with the tail of what the port did say and, for a range, with the value it did capture, so a reading that was merely out of range and a board that said nothing read differently. Without a comparator the step is a plain read. The comparator is an object so that preprocessing a bench eventually needs can be added as further keys without invalidating plans written today.

Close steps are optional: end-of-run cleanup closes every session the run opened, so a plan that never closes is complete. Write an explicit close where the plan means to close, reconfigure and reopen a line or a bus mid-run.

Before the first hardware action, the reactor validates every device name, permission, session order, artifact, breakpoint symbol, and dump path. A plan that contradicts the bus it declared is refused there as well: `can_send` on a bus configured `listen_only: true` can never work, because that flag is the claim that observing the bus sends nothing. `uart_write` on a port whose entry does not grant `permissions.allow_write` is refused by name in the same pass, as is a `range:` whose pattern captures nothing to put in it. Execution is fail-fast, each reactor-created breakpoint is removed after use, and debug, UART and CAN sessions opened by the runner are closed even when a step raises an exception. Breakpoint and dump symbols must be present in `debug.allowed_symbols` unless `allow_all_symbols: true` is explicitly set.

The run pipeline is deliberately simple — validate everything, then execute, then always clean up:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/reactor-pipeline-dark.svg">
  <img alt="Test reactor pipeline: plan → preflight (no hardware touched; any finding rejects the whole plan) → sequential fail-fast execution → guaranteed cleanup → structured report" src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/reactor-pipeline.svg">
</picture>

```yaml
# .agentic-hil/testconfig.yaml
version: 3
name: capture-state
steps:
  - {device: dut, action: flash, image_path: build/app.elf}
  - {device: dut_uart, action: uart_open}
  - {device: dut_can, action: can_open}
  - {device: dut_uart, action: uart_write, text: "capture\n"}
  - {device: dut_uart, action: uart_read, comparator: {pattern: "temp=(\\d+)C", range: {min: 20, max: 30}}, timeout_s: 5}
  - {device: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {device: dut, action: run_until_breakpoint, location: capture_done, timeout_s: 5}
  - {device: dut_can, action: can_read, max_frames: 64, wait_timeout_s: 1}
  - {device: dut, action: dump_memory, symbol: capture_buffer, output_path: build/capture.hex}
  - {device: dut, action: debug_stop}
```

`dut_can` above is the `listen_only: true` bus from the [configuration example](configuration.md#what-it-declares), so the plan only reads it. A `can_send` step belongs on a bus whose entry sets `listen_only: false` and grants `allow_write`. The plan closes neither the port nor the bus, because it does not have to — cleanup does.

A plan already written against `version: 2` keeps loading and behaving exactly as it did; the older `uart_expect` action stays valid too. A plan is held to what its own `version:` contains, so a `version: 2` plan reaching for a version 3 action or key is refused by name rather than working on one install and failing on an older one for no stated reason.

`.agentic-hil/testconfig.yaml` and `--test-config` select only this test plan: ordered test steps and the device names they run on. They contain no hardware resources or permissions. The reactor gets all hardware settings from the discovered authoritative config or its `AGENTIC_HIL_CONFIG` override:

```text
agentic-hil test-reactor --test-config .agentic-hil/testconfig.yaml
```

See [`examples/testconfig.example.yaml`](https://github.com/agentic-hil/agentic-hil/blob/master/examples/testconfig.example.yaml) for the expanded form.

## pytest Plugin

Installing `agentic_hil` registers the `agentic_hil` pytest plugin, so CI regression suites can drive the same permission-gated tools without an MCP client.

The `agentic_hil` fixture uses the same discovered config or absolute-path override as every other entry point and verifies that its `workspace_root` matches the pytest rootdir. Tests using the fixture skip when no config exists and fail loudly when an available config is invalid. Pytest executes project code and is therefore not a sandbox or security boundary; real unattended hardware runners must still use OS isolation and host-managed invocation. COM and CAN sessions opened during a test are stopped afterwards so stimulus state cannot leak between tests. See [examples/nucleo-f446re_demo/](../examples/nucleo-f446re_demo/) for the complete loop on real hardware.
