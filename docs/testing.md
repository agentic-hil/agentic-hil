# Running Hardware Tests

Two entry points drive the same permission-gated tools without an MCP client:
the test reactor executes a reviewable YAML plan, and the pytest plugin puts the
bench behind a fixture.

## Test Reactor

Write a hardware test as a plan, not a script: one reviewable YAML file describes flash → stimulate → break → dump, and the reactor guarantees it is either executed exactly as written or rejected before the first hardware action. No half-run plans, no leftover breakpoints, no orphaned debug or UART sessions — the same file behaves identically on your bench and in CI, and it diffs like code in a pull request.

The test reactor executes a strict, sequential YAML or JSON test plan against the named devices in the authoritative config. A debugger step names a `debuggers` entry (`debugger: <name>`); a UART step names a `com_ports` entry (`port_id: <name>`); a CAN step names a `can_buses` entry (`bus_id: <name>`). Every device kind the config models can take part in a plan, and each kind answers for its own actions, permissions, session order and cleanup. `debugger` may be omitted while the config declares exactly one probe — with several, the plan must name one, because picking a board for the author is how the wrong board gets flashed. Every probe carries its own permissions, so a step is judged by the grants of the board it names. Probes other than the bound one run on their own service under one shared project lease. Typed debug actions currently require OpenOCD; flash/UART-only plans can use the other backends.

Before the first hardware action, the reactor validates every device name, permission, session order, artifact, breakpoint symbol, and dump path. A plan that contradicts the bus it declared is refused there as well: `can_send` on a bus configured `listen_only: true` can never work, because that flag is the claim that observing the bus sends nothing. Execution is fail-fast, each reactor-created breakpoint is removed after use, and debug, UART and CAN sessions opened by the runner are closed even when a step raises an exception. Breakpoint and dump symbols must be present in `debug.allowed_symbols` unless `allow_all_symbols: true` is explicitly set.

The run pipeline is deliberately simple — validate everything, then execute, then always clean up:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/reactor-pipeline-dark.svg">
  <img alt="Test reactor pipeline: plan → preflight (no hardware touched; any finding rejects the whole plan) → sequential fail-fast execution → guaranteed cleanup → structured report" src="https://raw.githubusercontent.com/agentic-hil/agentic-hil/master/docs/diagrams/reactor-pipeline.svg">
</picture>

```yaml
# .agentic-hil/testconfig.yaml
version: 2
name: capture-state
steps:
  - {debugger: dut, action: flash, image_path: build/app.elf}
  - {port_id: dut_uart, action: uart_open}
  - {bus_id: dut_can, action: can_open}
  - {debugger: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {debugger: dut, action: run_until_breakpoint, location: capture_done, timeout_s: 5}
  - {bus_id: dut_can, action: can_read, max_frames: 64, wait_timeout_s: 1}
  - {debugger: dut, action: dump_memory, symbol: capture_buffer, output_path: build/capture.hex}
  - {debugger: dut, action: debug_stop}
  - {bus_id: dut_can, action: can_close}
  - {port_id: dut_uart, action: uart_close}
```

`dut_can` above is the `listen_only: true` bus from the [configuration example](configuration.md#what-it-declares), so the plan only reads it. A `can_send` step belongs on a bus whose entry sets `listen_only: false` and grants `allow_write`.

`.agentic-hil/testconfig.yaml` and `--test-config` select only this test plan: ordered test steps and the device names they run on. They contain no hardware resources or permissions. The reactor gets all hardware settings from the discovered authoritative config or its `AGENTIC_HIL_CONFIG` override:

```text
agentic-hil test-reactor --test-config .agentic-hil/testconfig.yaml
```

See [`examples/testconfig.example.yaml`](../examples/testconfig.example.yaml) for the expanded form.

## pytest Plugin

Installing `agentic_hil` registers the `agentic_hil` pytest plugin, so CI regression suites can drive the same permission-gated tools without an MCP client.

The `agentic_hil` fixture uses the same discovered config or absolute-path override as every other entry point and verifies that its `workspace_root` matches the pytest rootdir. Tests using the fixture skip when no config exists and fail loudly when an available config is invalid. Pytest executes project code and is therefore not a sandbox or security boundary; real unattended hardware runners must still use OS isolation and host-managed invocation. COM and CAN sessions opened during a test are stopped afterwards so stimulus state cannot leak between tests. See [examples/nucleo-f446re_demo/](../examples/nucleo-f446re_demo/) for the complete loop on real hardware.
