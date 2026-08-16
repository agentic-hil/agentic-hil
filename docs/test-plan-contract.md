# The Portable Test Plan, the Bench Configuration, and the Run Attestation

A hardware test is written once and run on more than one bench. That only works
if the three things involved stay separate: the plan says *what the test does*,
the authoritative configuration says *what this machine is and what it may do*,
and the run report says *what actually happened, under which policy*. Each is
owned by somebody different, and none of them is allowed to answer the other
two's question.

| Document | Owned by | Lives | Answers |
|---|---|---|---|
| Test plan (`testconfig.yaml`) | the firmware repository, reviewed in pull requests | inside `workspace_root` | which devices, which actions, in which order, and what the serial line must say |
| Authoritative configuration | the bench operator | outside the repository, discovered or `AGENTIC_HIL_CONFIG` | which hardware those names are, and what is permitted on it |
| Run report | Agentic HIL | `.agentic-hil/reports/` in the workspace, canonically under `state_root` | what ran, what the policy was, and what state the bench was left in |

The reference for the middle column is [the authoritative
configuration](configuration.md); the mechanics of a run are in [running
hardware tests](testing.md). This page is about the seam between them.

## What a plan may state

The plan schema is
[`src/agentic_hil/schemas/testconfig.schema.json`](https://github.com/agentic-hil/agentic-hil/blob/master/src/agentic_hil/schemas/testconfig.schema.json),
and it is closed: `additionalProperties: false` at the root and on every step,
so a key the schema does not name is a refusal rather than something ignored.
A plan document has exactly three root keys: `version:` (2, 3 or 4), an optional
`name` (defaulting to the file's stem), and `steps`, between 1 and 128 of them.

A step is a route field, an action, and that action's arguments. One step is
not: `repeat` is a block, and it is described below.

**Route fields name a configured entry, never a machine.** A debugger step
carries `debugger: <name of a debuggers entry>`, a serial step
`port_id: <name of a com_ports entry>`, a CAN step `bus_id: <name of a can_buses
entry>`. The value is a logical name matching `^[A-Za-z0-9_.-]+$` and nothing
more: no port number, no device path, no probe serial, no adapter channel.
`debugger` may be omitted while the project configures exactly one probe; with
several it must be named, because picking a board for the author is how the
wrong board gets flashed.

**Actions are the ones the device classes declare.** `flash` and `reset` on a
probe; `debug_start`, `run_until_breakpoint`, `dump_memory` and `debug_stop` for
a debug session; `uart_open`, `uart_write`, `uart_read`, `uart_expect` and
`uart_close` on a serial line; `can_open`, `can_send`, `can_read` and
`can_close` on a bus; and `delay`, declared once on the base class and served by
every kind. Each action is declared on the method that implements it, so the
authoritative list is the set of declarations; an action a kind does not
declare answers `not_supported` naming the kind. From plan format v3, `device:`
is the one routing key for every step; the v2 route keys stay valid as aliases.

**Arguments are the test's own parameters.** A firmware image path, a reset
mode, a breakpoint location, a symbol to dump and where to write it, a frame id
and its data bytes, a frame budget, a deadline. Paths are resolved inside
`workspace_root`, so `build/app.elf` means the same thing on every checkout of
the repository: it is a statement about the build layout, which travels with
the repository, not about a filesystem, which does not.

**Expectations are a `comparator`.** A feedback action (`uart_read` on a serial
line, `can_read` on a bus) takes an optional `comparator:` object: `equals` for
a complete decoded match, `pattern` for a Python regular expression (`re.search`
semantics), or `pattern` with a single capture group plus `range: {min, max}` to
bound the numeric value it extracts. Exactly one of `equals`/`pattern` per
comparator; a range without a capturing pattern, or a pattern that does not
compile, is refused before the run starts. A bus comparator also names the
identifier of the frame it is about: `id`, optionally widened by `id_mask` into
a family, and `extended` for which frame namespace it lives in (default `false`,
so a comparator that does not say waits for a standard frame; a standard `0x123`
and an extended `0x123` are two different frames and neither satisfies the
other's expectation). Naming it is required rather than optional, because a bus
carries every node's traffic and a payload matched without saying whose frame it
was is a green another ECU can produce; on that medium `equals` and `pattern`
read the payload as hexadecimal, and a `range` capture is read in that same base. The comparator
is an object deliberately, so preprocessing keys (scale, convert) can be added
later without breaking the format. `uart_expect` with `text`/`pattern` remains
valid as the v2 spelling. Every other step states its expectation by existing:
the step must succeed, and the plan stops at the first one that does not.

**One step is a block, not a route.** From plan format v4, `repeat` carries a
nested `steps:` list and no route field at all: it drives nothing, and the
reactor runs it rather than any device kind. `count:` bounds it by iterations
and `duration_s:` by wall time; at least one is required, because a plan may not
loop unbounded, and with both the first bound reached ends the loop. Both are
asked between iterations and nowhere else, so a cycle is never cut in half and a
block runs at least one whole iteration however small its `duration_s` is.
Reaching a bound is the green exit. The nested list is the same `#/$defs/steps`
the plan's own is, so a `repeat` may contain a `repeat`, and validation, the
version gate, preflight and the run's lock declaration all descend into it: a
nested step is refused at the path that names its nesting
(`steps[2].steps[1].timeout_s`), and a device a plan touches only inside a loop
is a device the run locks. The block adds nothing to the session model: nothing
is opened, closed or reset between iterations, and a session opened inside a
loop is opened once and closed by the same end-of-run cleanup as any other.
There is no retry, no until and no conditional exit; a nested step that fails
ends the run exactly as a failed top-level step does.

Nothing in that list identifies a machine. A plan is a document about firmware
and about the shape of a test, and it is reviewable as such:
[`examples/nucleo-f446re_demo/testconfig.yaml`](https://github.com/agentic-hil/agentic-hil/blob/master/examples/nucleo-f446re_demo/testconfig.yaml)
is five steps of flash, open, reset, and assert on a banner, and it names no COM
port, no baudrate and no probe serial anywhere. The expanded form of every
action is
[`examples/testconfig.example.yaml`](https://github.com/agentic-hil/agentic-hil/blob/master/examples/testconfig.example.yaml).

## What only the bench configuration binds

Everything the plan deliberately cannot say, the authoritative configuration
says, and it says it once for every plan that runs on that bench.

| Bound by the configuration | Examples |
|---|---|
| Hardware identity | `debuggers.<name>.probe_id`, `com_ports.<name>.device` / `serial_number` / `identity_source`, `can_buses.<name>.adapter` / `channel` |
| Toolchain | `debuggers.<name>.type`, `executable`, `interface_cfg`, `target_cfg`, `timeout_s` |
| Electrical and link settings | `baudrate`, `encoding`, `assert_dtr`, `assert_rts`, `bitrate`, `fd`, `listen_only`, `max_buffer_frames` |
| Permissions | every `permissions` block: `allow_flash`, `allow_reset`, `allow_raw_debugger_commands`, `allow_mass_erase`, per-port and per-bus `allow_write` (and `allow_read` / `allow_probe` on a configuration older than the read-free version, where exclusivity had not yet taken that load) |
| Reach | `workspace_root`, `state_root`, `artifacts.allowed_roots`, `allowed_extensions`, `debug.allowed_symbols`, `debug.allow_all_symbols` |
| Bench behaviour | `reports.directory`, `logs.directory`, `recovery.auto_recover`, the per-probe `target` override |

The split is enforced rather than conventional. The reactor's preflight walks
every step before the first hardware action and refuses the whole plan when the
two documents disagree: a name the configuration does not declare, an action a
permission does not allow, a breakpoint or dump symbol outside
`debug.allowed_symbols`, a firmware artifact outside the allowed roots or
extensions, a dump output path the artifact validator rejects, a session closed
before it was opened, a `can_send` on a bus configured `listen_only: true`.
Preflight builds nothing (no service, no lock, no directory), so a plan refused
there has touched no hardware at all, and the same plan asked twice gets the
same answer.

Some of the configuration's values reach into a plan without the plan naming
them. `can_read` without `max_frames` reads the bus's configured
`max_buffer_frames`, which is also its ceiling; a `uart_expect` reads the line
at the configured baudrate and decodes at the configured encoding. That is the
intended direction: the plan states the test, the bench supplies the physics.

## What the run attests

A run produces one JSON report, written to `.agentic-hil/reports/last-report.json`
(and `last-failure.json` when it failed) in the workspace, with the canonical
copy under the operator-pinned `state_root`. The workspace copies are untrusted
mirrors, as everything in the workspace is. Five parts of that report are the
attestation.

**Which policy was in force.** `config_in_force` carries the digest of the exact
configuration bytes the run was permitted by (`digest` under
`digest_algorithm`, beside the configuration's `path`), together with
`file_state`, `file_digest` and `diverged_from_file`: the comparison made
*inside* the run against what was on disk at that moment. A report that named
only the path would say nothing, because the path is constant over a project's
lifetime and the content is not. When a description reload has moved the loaded
document out from under the grants still being enforced, `permissions_source`
travels with the record and says so.

**What each step did.** `steps` is one entry per executed step
(`{index, route, action, result}`), where `result` is the tool result unchanged:
its `ok`, `error_type`, `summary`, its `log_path`, and the effect fields
(`side_effect_committed`, `side_effect_status`, `retry_safe`). `failed_step` and
`step_error_type` name where a fail-fast run stopped. A `repeat` block's entry
additionally carries `iterations`, one `{iteration, steps}` record per cycle
holding that cycle's step entries in the same shape, and its own `result` says
how the loop ended: `exit_reason` (`count`, `duration_s` or `step_failed`),
`iterations_run`, `elapsed_s`, and on a failure `failed_iteration`,
`failed_nested_step` and `failed_nested_action`. `failed_step` stays the
top-level number of the block and `step_error_type` the inner step's own error,
so the two questions a reader asks (which step, and what went wrong) keep their
answers whether or not the plan nests. A plan refused before execution reports
`validation_error` instead, naming the step index, the offending field, the
route and the action; and `steps` is empty, because none ran.

**Whether the hardware was ever reached.** Session results carry the contact
marker (`first_contact_at`, `first_contact_by`) recorded at the one point in a
backend where contact is provable; backend results carry `target_contacted`.
Absence is meaningful and is why the fields are omitted rather than null: a
failure with no marker is a failure that can be refused as never having reached
the board, instead of quarantining a bench nobody touched.

**Whether the ledger held.** `audit_ok`, with `audit_error` and `audit_errors`
when it did not. Every agent-initiated hardware effect is mirrored into a
tamper-evident canonical ledger under `state_root` before the workspace log is
written, and a run whose audit could not be written fails whatever the board
did. The trusted evidence pointer (`log_sequence`, `log_chain_sha256`, and
`workspace_log_verified`, which checks that every canonical line is still
present in the workspace mirror in order) is served by `get_last_report` and
`classify_last_error` rather than being baked into the report file, so it is
computed against the ledger at the moment it is read.

**What state the bench was left in.** A failed run carries a `recovery` block:
whether recovery was `attempted`, which `actions` ran, the `outcome`, the
`devices` involved, and the `auto_recover_policy` with whether it came from the
configuration or from the default. A bench that withholds recovery names the
setting that withheld it in `reason_not_attempted`. Recovering the bench never
un-fails a test (`ok` is not touched by this block); it only says whether the
next run can start.

Two more things are worth knowing when reading a report by hand. Every device
the plan names is locked machine-wide from before the first step to after the
last, so nothing can observe the board between two steps; when a device is held
by another run the plan does not start at all, and the report names the locks it
wanted in `declared_devices`. And `cleanup`, `cleanup_ok` and `cleanup_errors`
record the sessions the runner closed on the way out, debug sessions before
serial lines and CAN buses.

**A run that was asked to end says so.** Every run carries the handle it ran
under (`run`), and a run that was stopped reports `ok: false` with
`error_type: run_stopped`, `stopped: true` and `stopped_after_step`, keeping the
records of every step that did run. A stop is deliberately neither of the other
two verdicts: it is not a pass, because the plan did not finish, and it is not a
failure, because the devices were closed and confirmed by the same cleanup a
passing run uses, so a stopped run carries no `recovery` block and leaves no
incident. What a partial run is worth is the caller's decision, and the record
of what ran is what it decides from. See [running hardware
tests](testing.md#detached-runs-status-and-a-cooperative-stop) for the commands.

## How a plan travels

The plan is a repository file; the configuration is a machine file. Moving a
test to another bench is therefore not a change to the test:

1. The second bench declares its own `debuggers`, `com_ports` and `can_buses`
   entries under **the same logical names** the plan uses, pointing at whatever
   hardware that bench actually has: a different probe serial, `COM5` instead
   of a `/dev/serial/by-id/...` device, `peak` instead of `socketcan`.
2. It grants the permissions that bench is willing to give.
3. The same plan file runs.

Everything that differs between the two machines is in the file the plan never
reads, and everything the test asserts is in the file the machine never writes.
The one agreement the two ends must keep is the set of logical names; that is
the interface, and it is deliberately the only one.

## What is deliberately not portable

**Permissions do not travel.** A plan that flashes will run on a bench with
`allow_flash: true` and be refused, before any hardware action, on a bench
without it. This is the point rather than a limitation: a permission that
travelled with the test would be a test granting itself rights on a machine it
has never met. Permissions are the bench operator's statement about their own
hardware, and a plan cannot request, imply or widen one. The same holds for
`debug.allowed_symbols` and `artifacts.allowed_roots`: a plan naming a symbol a
bench has not allowed is refused, and the remedy is an operator decision at that
bench.

**Identities do not travel.** Probe serials, device paths, adapter channels and
executable locations are properties of one machine. Committing them to a plan
would make the repository carry another machine's hardware inventory, and would
make the plan wrong the first time a board is swapped.

**Two behaviours are backend-bound and therefore travel only as far as the
backend does.** The typed debug actions (`debug_start`, `run_until_breakpoint`,
`dump_memory`, `debug_stop`) currently require a debugger of type `openocd`,
and `reset` with `mode: init` is OpenOCD-only, refused by other backends with
their own `not_supported`. A flash-and-serial plan runs on all three backends; a
plan that opens a debug session states, implicitly, that its bench runs OpenOCD.

## Open

These are limits of what a plan can currently express, verified against the
schema and the reactor rather than inferred:

- **No expectation on a value in target memory.** The debugger's two feedback
  actions assert reaching a place, not reading a value: `run_until_breakpoint`
  asserts that the target stopped where the plan said, and `dump_memory` writes
  an Intel HEX file and succeeds on having written it. The backend half of this
  is no longer missing: `debug_symbol_value` returns an allowed symbol's bytes,
  as hex and as an unsigned and a signed integer at 1, 2, 4 or 8 bytes, so
  there is now something for a `read_symbol` action to call. What is still
  missing is the plan half, and it is more than the decorated method this
  paragraph used to promise: a `comparator:` here would judge a decoded number,
  while every comparator the format carries today judges text and reads a
  `range` out of a regular expression's capture group. Closing it means a
  numeric comparator, a schema entry for the action and the plan version that
  introduces it, and it is tracked separately from the tool.
- **No branch, no reuse, no parameters.** From v4 there is one control-flow
  construct, `repeat`, and it repeats: it does not decide. There is no branch,
  no retry, no until, no include, and no variable or substitution: an argument
  is the literal the file carries. Two plans that differ only in a firmware path
  are two files. Each step list holds at most 128 items, a block's own included.
- **No expected failure.** Every step must succeed. A plan cannot assert that an
  action is refused (that a write to a read-only bus fails, that an out-of-range
  input is rejected) because the first failing step ends the run.
- **No declared requirements.** The plan states no preconditions about the bench
  it needs: no "requires a CAN bus with `allow_write`", no "requires an OpenOCD
  probe". A plan that cannot run here is discovered at preflight, which is safe
  and early but is a refusal rather than a capability check, and the schema has
  nowhere to write the requirement.
- **No annotation that survives the run.** A step takes only its action's own
  keys, so there is no per-step description or tag. YAML comments document a
  plan for a reader; they do not reach the report, where a step is identified by
  its index, route and action.
