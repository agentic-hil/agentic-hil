# Running Hardware Tests

Two entry points drive the same permission-gated tools without an MCP client:
the test reactor executes a reviewable YAML plan, and the pytest plugin puts the
bench behind a fixture.

## Test Reactor

Write a hardware test as a plan, not a script: one reviewable YAML file describes flash → stimulate → break → dump, and the reactor guarantees it is either executed exactly as written or rejected before the first hardware action. No half-run plans, no leftover breakpoints, no orphaned debug or UART sessions. The same file behaves identically on your bench and in CI, and it diffs like code in a pull request.

The test reactor executes a strict, sequential YAML or JSON test plan against the named devices in the authoritative config. Every step names the configured entry it drives with one key (`device: <name>`), and the configuration is what knows whether that name is a `debuggers`, a `com_ports` or a `can_buses` entry. The version 2 keys `debugger:`, `port_id:` and `bus_id:` stay valid as readable aliases; a step writes one or the other, never both, and a step that names its device twice is refused rather than resolved by precedence. Every device kind the config models can take part in a plan, and each kind answers for its own actions, permissions, session order and cleanup. The name may be omitted on a debugger step while the config declares exactly one probe; with several, the plan must name one, because picking a board for the author is how the wrong board gets flashed. Every probe carries its own permissions, so a step is judged by the grants of the board it names. Probes other than the bound one run on their own service under one shared project lease. Typed debug actions currently require OpenOCD; flash/UART-only plans can use the other backends.

A step action is a string a device class declares on the method that implements it, so a capability is one decorated method and a device kind is a class of them. A string a kind does not declare is answered `not_supported` naming the kind, by construction rather than by a special case. `delay` is declared once on the base class and is therefore served by every kind: it drives nothing, but it is still a step and appears in the run's result, because a wait that left no trace would be a gap in the record of what a test did.

Feedback steps say what they are claiming, not merely that they read something. `uart_read` with a `comparator:` reads until the claim is met or its timeout passes: `equals:` for a whole value rather than a substring (any one complete line of what was received matches as soon as its terminator arrives, and the whole of what was received matches on the last pass, once the timeout has run out and nothing further is coming, so a value the board never terminates cannot pass while it is still being printed), `pattern:` for a Python regular expression under `re.search`, and `pattern:` with `range:` for a number captured by the pattern's one capture group and held to inclusive bounds. A claim that goes unmet fails with the tail of what the port did say and, for a range, with the value it did capture, so a reading that was merely out of range and a board that said nothing read differently. Without a comparator the step is a plain read. The comparator is an object so that preprocessing a bench eventually needs can be added as further keys without invalidating plans written today.

`can_read` takes the same comparator over the medium a bus has. A frame arrives complete and with an identifier on it, so the claim is two things at once: `id:` (optionally widened by `id_mask:` into a family of identifiers) says which frame the plan is waiting for, and `equals:`, `pattern:` or `pattern:` with `range:` says what that frame must carry, read against the payload as hexadecimal. The identifier is required rather than optional, because a bus carries every node's traffic and a payload matched without saying whose frame it was is a green another ECU can produce; frames the filter rejects are passed over and nothing is captured from them. The step reads again until a frame meets the claim or its `timeout_s` passes, and an unmet claim fails with the last frames the bus did carry, so a plan waiting on the wrong identifier and a node that never sent anything read differently. Without a comparator it is the version 2 step unchanged: one read, answered exactly as `can_read` answers it.

`read_symbol` claims a value in target memory, which is the first thing this format asserts about the board's own state rather than about what it sent. It reads one allowed symbol through the debug session the plan opened, exactly as the `debug_symbol_value` tool reads it: the same `debug.allowed_symbols`, the same `debug.max_dump_size_bytes` on the resolved size, and the same resolution, which asks the image's debug information first and falls back to the ELF's own symbol table where there is no type to work with, so an assembly-defined object is readable too. Its `comparator:` is a numeric one, because a word of memory has no text to match: `equals:` for one value, `range:` for inclusive bounds, and `mask:` beside `equals:` for `(value & mask) == equals`, which asserts one flag of a status word without stating the rest of it. `signed: true` picks the signed reading, since the tool returns both and above the top bit the two disagree, and a mask is a claim about bits, so `signed` is refused beside it. A claim that goes unmet fails with the value it did read, the bytes it decoded and, for a mask, the masked value it compared. Without a comparator the step is a plain read whose value the report keeps.

That decoded integer exists only at 1, 2, 4 and 8 bytes, so a plan claiming a number at any other width is refused rather than answered with an invented one. A step may state `size_bytes:`, which is both an assertion (a firmware that turned a `uint32_t` into a `uint16_t` fails the step instead of having its new value read as the old one) and what makes the width knowable before the run: a declared width that no integer reading exists at, and one above this bench's `debug.max_dump_size_bytes`, are refused at preflight, where nothing has been opened. A plan that declares no width is refused at runtime instead, with the reason stated and the bytes it did read. There is no `timeout_s` on this step and no waiting: a halted target's memory does not change under it, so the symbol is read once and judged once.

Close steps are optional: end-of-run cleanup closes every session the run opened, so a plan that never closes is complete. Write an explicit close where the plan means to close, reconfigure and reopen a line or a bus mid-run.

A plan states a cycle once and says how often it runs it. `repeat` is a block step the reactor serves itself: it routes to no device, the steps nested under its own `steps:` are the subset that repeats, and it is bounded by `count:` (iterations), by `duration_s:` (wall time), or by both, where the first bound reached ends the loop. At least one bound is required, so a plan cannot loop unbounded. Both bounds are asked between iterations and nowhere else, so a cycle is never cut in half and a block always runs at least one whole iteration however small its `duration_s` is; reaching a bound is the green exit. The nested list has the same shape as the plan's own, so a `repeat` may contain a `repeat`.

Nothing is opened, closed or reset between iterations: a port opened before the loop is still open inside it, a port opened inside the loop is opened once and closed by end-of-run cleanup like any other, and the loop adds no cleanup of its own. There is no retry and no until: a nested step that fails ends the run exactly as a failed top-level step does, with the same cleanup, the same report and the same recovery action after it. The report says where it stopped: `failed_step` is the top-level number of the block, `step_error_type` is the inner step's own error, and the block's record carries the step records of every iteration beside `exit_reason`, `iterations_run`, `elapsed_s`, and, on a failure, `failed_iteration` and `failed_nested_step`. Validation, the version gate, preflight and the run's lock declaration all descend into the nested steps, so a nested step is refused at the path that names its nesting (`steps[2].steps[1].timeout_s`) and a device a plan touches only inside a loop is a device the run locks.

```yaml
version: 4
steps:
  - {device: dut_uart, action: uart_open}
  - action: repeat
    count: 1000
    steps:
      - {device: dut_can, action: can_send, frame_id: "0x100", data_hex: "01"}
      - {device: dut, action: delay, duration_ms: 500}
      - {device: dut_uart, action: uart_read, comparator: {equals: "cycle done"}, timeout_s: 2}
  - {device: dut, action: reset}
```

Before the first hardware action, the reactor validates every device name, permission, session order, artifact, breakpoint symbol, and dump path. A plan that contradicts the bus it declared is refused there as well: `can_send` on a bus configured `listen_only: true` can never work, because that flag is the claim that observing the bus sends nothing. `uart_write` on a port whose entry does not grant `permissions.allow_write` is refused by name in the same pass, as is a `range:` whose pattern captures nothing to put in it, and a `read_symbol` whose declared width the bench's `debug.max_dump_size_bytes` will not read or the format cannot decode a number from. Execution is fail-fast, each reactor-created breakpoint is removed after use, and debug, UART and CAN sessions opened by the runner are closed even when a step raises an exception. Breakpoint, dump and read symbols must be present in `debug.allowed_symbols` unless `allow_all_symbols: true` is explicitly set.

The run pipeline is deliberately simple. It validates everything, then executes, then always cleans up:

![Test reactor pipeline: plan → preflight (no hardware touched; any finding rejects the whole plan) → sequential fail-fast execution → guaranteed cleanup → structured report](diagrams/reactor-pipeline.svg#only-light)
![Test reactor pipeline: plan → preflight (no hardware touched; any finding rejects the whole plan) → sequential fail-fast execution → guaranteed cleanup → structured report](diagrams/reactor-pipeline-dark.svg#only-dark)

```yaml
# .agentic-hil/testconfig.yaml
version: 5
name: capture-state
steps:
  - {device: dut, action: flash, image_path: build/app.elf}
  - {device: dut_uart, action: uart_open}
  - {device: dut_can, action: can_open}
  - {device: dut_uart, action: uart_write, text: "capture\n"}
  - {device: dut_uart, action: uart_read, comparator: {pattern: "temp=(\\d+)C", range: {min: 20, max: 30}}, timeout_s: 5}
  - {device: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {device: dut, action: run_until_breakpoint, location: capture_done, timeout_s: 5}
  - {device: dut_can, action: can_read, comparator: {id: "0x201", pattern: "^02(..)$", range: {min: 20, max: 30}}, timeout_s: 5}
  - {device: dut, action: read_symbol, symbol: capture_count, size_bytes: 4, comparator: {equals: 64}}
  - {device: dut, action: read_symbol, symbol: status_word, size_bytes: 4, comparator: {mask: 0x0f, equals: 5}}
  - {device: dut, action: dump_memory, symbol: capture_buffer, output_path: build/capture.hex}
  - {device: dut, action: debug_stop}
```

`dut_can` above is the `listen_only: true` bus from the [configuration example](configuration.md#what-it-declares), so the plan only reads it. A `can_send` step belongs on a bus whose entry sets `listen_only: false` and grants `allow_write`. The plan closes neither the port nor the bus, because it does not have to. Cleanup does.

A plan already written against `version: 2`, `version: 3` or `version: 4` keeps loading and behaving exactly as it did; the older `uart_expect` action stays valid too. A plan is held to what its own `version:` contains, so a `version: 3` plan reaching for the version 4 block step, or a `version: 4` plan reaching for the version 5 `read_symbol` action, is refused by name rather than working on one install and failing on an older one for no stated reason.

`.agentic-hil/testconfig.yaml` and `--test-config` select only this test plan: ordered test steps and the device names they run on. They contain no hardware resources or permissions. The reactor gets all hardware settings from the discovered authoritative config or its `AGENTIC_HIL_CONFIG` override:

```text
agentic-hil test-reactor --test-config .agentic-hil/testconfig.yaml
```

See [`examples/testconfig.example.yaml`](https://github.com/agentic-hil/agentic-hil/blob/master/examples/testconfig.example.yaml) for the expanded form.

An agent writing a plan reads the format over the connection it already has, not out of this page and never out of the installed package. `agentic-hil://reference/test-plan` is the whole of it: where the file goes, how `test_config_path` and `workspace_root` resolve its path, which version admits which step, every step with the entry it routes to and its required and optional keys, the comparator families with their rules, and two plans that run. `agentic-hil://reference/test-plan-schema` serves the schema itself, and the document is generated from that schema at read time, so neither can drift from what the reactor validates against.

### The two ways to run a plan

An agent runs a plan with the `test_reactor_run` MCP tool; an operator runs it with `agentic-hil test-reactor` at a shell. Both reach the same code, so a plan behaves identically whichever one started it: the same preflight before the first hardware action, the same devices locked for the same duration, the same permission judged per step, the same report at the end. The tool takes `test_config_path` where the command takes `--test-config`, holds it to `workspace_root` the same way, and `detach: true` where the command takes `--detach`. `test_reactor_status` and `test_reactor_stop` answer as `test-reactor-status` and `test-reactor-stop` do.

The tool is the route for an agent because it is the only one an operator can see and audit: a plan run through a shell is a shell command, judged by whatever the agent host makes of it, while a plan run through the tool is coordinated and reported like every other hardware action here. A plan is also a run in its own right, so it needs no `bench_run_start` around it, and a call made while the bench is held by something else is refused with the holder named rather than queued.

### Detached runs, status and a cooperative stop

A time-bounded endurance plan runs for hours, and a caller that has to sit in front of it for that long is a caller that cannot do anything else. `--detach` runs the plan in its own process and returns at once with a run handle and the path the report will be written to:

```text
agentic-hil test-reactor --detach
agentic-hil test-reactor-status --run run-3f9c2a1b4e6d8071
agentic-hil test-reactor-stop   --run run-3f9c2a1b4e6d8071
```

The start command answers as soon as the run holds the devices it declared, so a handle it printed is a handle with the bench. The plan is loaded before the worker is started, so a plan that does not load is refused immediately rather than behind a second command. The synchronous invocation is the default and does exactly what it always did; it is registered under a handle too, so a plan running in one terminal can be stopped from another. Over MCP the same three are `test_reactor_run` with `detach: true`, `test_reactor_status` with `run`, and `test_reactor_stop` with `run`; the handle is the same handle, so a run an agent detached is one an operator can ask about and end from a shell, and the other way round.

`test-reactor-status` answers for one handle: running, with the step and, inside a `repeat`, the iteration it is on; finished or stopped, with the report path and the verdict; or worker gone. Without `--run` it lists the runs the bench still has records of. Whether the process behind a handle is still there is asked of the operating system rather than of a process id: a run holds a lock for as long as it runs, so a lock that can be taken is a run whose process has ended, however it ended.

`test-reactor-stop` is cooperative and nothing else. It writes a request; the run reads it between its steps and inside a `delay`, which waits in slices for exactly this reason and still waits its full duration when nobody asks it to stop. The run then finishes the step it is in, closes its devices in the usual cleanup order and writes its report. A stopped run is not a passed run: `ok: false` with `error_type: run_stopped`, the step it stopped after, and the records of everything that did run, so the caller decides what a partial run is worth. It is not a failed run either: its devices were closed and confirmed by the same cleanup a passing run uses, so there is no incident and no recovery action.

Killing the worker instead is the case this exists to replace. A process that dies without releasing anything is the dead-owner case the bench already handles: the status command names it (`worker_gone`) rather than guessing what the run had reached, a stop is refused because there is nobody left to honour it, and `agentic-hil lease-status` is what reads and heals the bench. Locks are machine-wide and unchanged by any of this: a detached run holds what it declared, and every other caller is refused with the owner named.

## pytest Plugin

Installing `agentic_hil` registers the `agentic_hil` pytest plugin, so CI regression suites can drive the same permission-gated tools without an MCP client.

The `agentic_hil` fixture uses the same discovered config or absolute-path override as every other entry point and verifies that its `workspace_root` matches the pytest rootdir. Tests using the fixture skip when no config exists and fail loudly when an available config is invalid. Pytest executes project code and is therefore not a sandbox or security boundary; real unattended hardware runners must still use OS isolation and host-managed invocation. COM and CAN sessions opened during a test are stopped afterwards so stimulus state cannot leak between tests. See [examples/nucleo-f446re_demo/](https://github.com/agentic-hil/agentic-hil/tree/master/examples/nucleo-f446re_demo) for the complete loop on real hardware.

### `--agentic-hil-config` is deprecated, and it fails rather than degrades

The plugin accepts a `--agentic-hil-config` option and a matching `agentic_hil_config` ini key. Both are deprecated, and neither is the way to select a configuration. They are accepted only while they resolve to the configuration discovery has already found; a path that resolves anywhere else **fails the session** before the first test runs, with `cannot change policy authority` and both paths printed. It does not warn and carry on, and it does not fall back to the discovered file.

That is deliberate. These options live in the repository, in a command line or a `pytest.ini` a pull request can edit, and the authoritative configuration deliberately does not. An option that could point the suite at a different policy would let repository-controlled data decide what this bench may be told to do; one that silently fell back to the discovered file would run the suite under a policy nobody in that pull request asked for, and report it as a pass. Failing is the only remaining answer, so it is the one you get.

There are two supported ways to say which configuration a run uses, and neither is an option on this command line:

* **Say nothing.** The authoritative configuration is discovered from the pytest rootdir, which is the project root, exactly as `doctor`, `mcp-stdio` and `test-reactor` discover it from their own project working directory. This is what a CI job wants: remove the option and the ini key, and the plugin finds the file the rest of the tooling finds.
* **Set `AGENTIC_HIL_CONFIG`** to an absolute path when an operator-controlled override is wanted, in the runner's own environment rather than in a repository-controlled file. See [Where it is found](configuration.md#where-it-is-found).

A suite that passes one of the deprecated selectors and resolves it to the discovered file still runs, unchanged. It is the only case in which they do anything at all, which is why removing them costs that suite nothing.
