# The `agentic-hil/run` GitHub Action: Design

Design for an official action that runs one named test plan against the bench
already configured on a self-hosted runner, and leaves behind evidence a
reviewer can read without access to that bench. This document settles the
inputs, the outputs, the trust rule and the failure modes; the implementation
follows it. Nothing here changes the CLI or the authoritative configuration.

## What it does

One step in a workflow, on a runner that is physically attached to hardware:

```yaml
jobs:
  hil:
    runs-on: [self-hosted, agentic-hil, nucleo-f446re]
    if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
    timeout-minutes: 20
    concurrency:
      group: bench-nucleo-f446re
      cancel-in-progress: false
    steps:
      - uses: actions/checkout@<commit-sha>
      - run: cmake --preset Debug && cmake --build --preset Debug
      - uses: agentic-hil/run@<commit-sha>
        with:
          plan: testconfig.yaml
          version: "<exact released version>"
          bench: local
          drives-hardware: true
```

The action installs the pinned Agentic HIL version, checks the bench with
`agentic-hil doctor`, runs `agentic-hil test-reactor --test-config <plan>
--junit-xml <path>`, and turns the resulting JSON report into a job summary, a
JSON run summary, and a copy of the run's event logs. The JUnit XML file is the
command's own, not the action's: the action passes the path.

It deliberately does not: choose a plan, write or select a configuration, grant
a permission, install a debugger toolchain, or clear a quarantine.

## What it is not

**There is no hosted or remote bench, by design.** The `bench` input exists so
that this is stated rather than implied: `local` is the only accepted value, and
anything else (a hostname, a bench identifier, `remote`, `auto`) is refused
before anything is installed, with an error that says remote execution is not
available. Agentic HIL's whole safety model rests on one machine holding one
physical device under an operating-system lock, with the policy file on that
machine and outside the repository (see [safety model](safety-model.md)).
Routing a run to a bench elsewhere would need a trust and transport story that
does not exist here, and an input that quietly accepted a hostname would be a
promise the product cannot keep.

**The authoritative configuration never comes from the repository.** It is not
an input, not a file the workflow writes, and not something the action
generates. It is discovered on the runner exactly as every other entry point
discovers it, or selected by an `AGENTIC_HIL_CONFIG` the operator set in the
runner's own environment (see [the authoritative
configuration](configuration.md)). This is already enforced below the action: a
configuration stored inside the workspace is refused at load
(`config_invalid`, "The authoritative config must be stored outside the
workspace"), and so is one whose `workspace_root` is not the directory being
run in. A pull request therefore cannot redirect policy to a file it committed.
The action's contribution is to make the configuration that *was* in force
visible in the summary, by digest.

## The trust rule

Hardware runs only when all four hold. The first two belong to the repository
owner and the runner operator; the action cannot create them, only rely on them.
The last two are the action's own refusals, and they are the second line.

1. **Self-hosted runner, owned by the repository or organisation.** Only such a
   runner has a bench. Runner groups and the repository's fork-PR approval
   settings decide who can reach it, and those settings, not the action, are
   what stops fork code from executing at all.
2. **Explicit workflow opt-in.** The job carries `drives-hardware: true`. There
   is no default that reaches hardware, so no workflow acquires a bench by
   copying a snippet that looked harmless.
3. **The action refuses a hosted runner.** `RUNNER_ENVIRONMENT` must read
   `self-hosted`. On `github-hosted` the action stops before installing
   anything.
4. **The action refuses an untrusted event.** Untrusted means: a
   `pull_request` whose head repository is not this repository; any
   `pull_request_target`; any `workflow_run`. The last two are refused outright
   rather than inspected, because both give base-repository trust to code that
   came from a head repository, which is the exact shape this rule exists to
   stop. A workflow that needs a fork's changes tested on hardware pushes them
   to a branch in this repository first, where a human has looked at them.

Every refusal is the same shape: the action stops before installing Agentic HIL,
writes a job summary saying which rule refused and why, sets
`outcome: refused`, and fails the step. It never degrades to "run it anyway
without hardware", because a green job that did not test the board is the false
green the whole product is built to avoid.

## Inputs

| Input | Required | Default | Meaning |
|---|---|---|---|
| `plan` | yes | n/a | Path to the test plan, relative to `working-directory`. Must resolve inside the workspace. |
| `version` | yes | n/a | The exact Agentic HIL version to run. An exact version only: a range, `latest`, or a git reference is refused. |
| `bench` | no | `local` | Only `local` is accepted. Any other value is refused (see above). |
| `drives-hardware` | yes | n/a | Must be literally `true`. The opt-in. |
| `working-directory` | no | `.` | The project root. Must be the configuration's `workspace_root`; a mismatch is refused below the action with `config_invalid`. |
| `wait-s` | no | `0` | Passed to `--wait-s`. Bounded wait for a device another run holds, instead of failing immediately. |
| `doctor` | no | `true` | Run `agentic-hil doctor` first and fail the job on a red result, before the plan gets near the board. |
| `require-clean-bench` | no | `true` | Fail the job when the run left the bench held: a `recovery` block whose outcome is not a clean one, or a standing quarantine. |
| `junit` | no | `true` | Write the JUnit XML file. |
| `artifact-name` | no | `agentic-hil-run` | Base name for the uploaded artifact. |
| `upload-artifacts` | no | `true` | Upload the evidence bundle. When false the paths are still emitted as outputs, so a workflow can upload them its own way. |

Deliberately absent: any input that names a configuration file, a device, a
permission, a probe, a serial port, a CAN channel, a baudrate, or a bench
location. All of those are the runner's, and the reason is in [the portable
test plan](test-plan-contract.md).

## Outputs

| Output | Value |
|---|---|
| `outcome` | `success`, `failure`, or `refused` |
| `error-type` | The report's `error_type`, or the action's own refusal code |
| `failed-step` | The 1-based index of the step that failed, when there was one |
| `plan-name` | The plan's `name`, or the file stem |
| `config-digest` | `config_in_force.digest` (which policy the run was decided by) |
| `report-path` | The run report JSON |
| `summary-path` | The JSON run summary |
| `junit-path` | The JUnit XML file, when written |
| `logs-path` | The directory the event logs were collected into |

## What the job leaves behind

### Job summary

Written to `GITHUB_STEP_SUMMARY`: the plan name and outcome, a table of executed
steps (index, route, action, result, and `elapsed_ms` where the step result
carries one), the failing step's `error_type` and `summary` in full, the
configuration digest with whether it had already diverged from the file on disk,
the bench identity block, and the `recovery` block when the run failed.

The summary is world-readable on a public repository, so it carries no probe
serial, no device path, no executable path, and no absolute path from outside
the workspace. `config_in_force.path` and `debugger_info`'s `executable` and
`probe_id` are dropped; the digest and the logical device names are what
identify the bench.

### Artifacts

One bundle, uploaded with `if: always()` so a failed or cancelled run still
produces it:

- **`junit.xml`**: the run report as JUnit, for the test-report UIs that
  consume it. The mapping is below.
- **`logs/`**, the JSONL event logs the run wrote under the configuration's
  `logs.directory` (`.agentic-hil/logs/` by default): one `com-*.jsonl` per
  serial session and one `can-*.jsonl` per bus session, plus the debugger
  backend's own per-invocation log files. These are the workspace mirrors. The
  canonical, hash-chained copies stay under `state_root` on the runner and are
  not uploaded: they are the trusted ledger, and shipping them to an artifact
  store would be publishing the thing they exist to be checked against.
- **`report.json`**: the run report as written, including `config_in_force`,
  every step result, `cleanup`, and `recovery`.
- **`run-summary.json`**: the summary described next.

### The JSON run summary

The one file a downstream consumer should need. Its shape:

```json
{
  "outcome": "failure",
  "plan": { "path": "testconfig.yaml", "name": "nucleo-f446re-hello-world", "sha256": "..." },
  "firmware": {
    "repository": "owner/name",
    "commit": "<the checked-out commit>",
    "head_commit": "<the pull request head commit, when the event has one>",
    "ref": "refs/heads/..."
  },
  "tools": {
    "agentic_hil": "<the pinned version, as agentic-hil --version reported it>",
    "python": "3.x.y",
    "debuggers": { "dut": { "backend": "openocd", "version": "<the backend's own version line>" } }
  },
  "bench": {
    "config_digest": "...",
    "digest_algorithm": "...",
    "diverged_from_file": false,
    "runner": { "name": "<runner name>", "labels": ["self-hosted", "..."] },
    "target": { "name": "...", "controller": "..." },
    "devices": { "debuggers": ["dut"], "com_ports": ["dut_uart"], "can_buses": ["dut_can"] }
  },
  "run": {
    "failed_step": 4,
    "error_type": "uart_expect_timeout",
    "cleanup_ok": true,
    "audit_ok": true,
    "recovery": { "attempted": true, "outcome": "...", "auto_recover_policy": "..." }
  }
}
```

Firmware commit comes from the event: `GITHUB_SHA` is the checked-out commit,
which for a `pull_request` is the merge commit, so the pull request's head
commit is recorded beside it rather than instead of it. Tool versions come from
`agentic-hil --version` after installation and from `agentic-hil doctor`, whose
per-debugger `debugger_info` result carries the backend's own version line.
Bench identity is the configuration digest plus the logical device names and the
target, never the hardware identities, for the reason given under the job
summary. That exclusion has to include the lock keys a refused run reports in
`declared_devices`: they are keyed on the physical device (`probe:<serial>`,
`com:<device>`, `can:<adapter>:<channel>`), so they carry exactly the
identities the rest of this file is careful not to publish. When a run is
refused because a device is busy, the summary says which logical name was
unavailable, not which key.

### The JUnit mapping

The mapping is implemented, and it is the CLI's: `agentic-hil test-reactor
--junit-xml <path>` writes it, so the action passes a path and maps nothing. See
[testing.md](testing.md#a-junit-report-for-ci) for the flag itself.

JUnit has no vocabulary for "the plan was refused before anything ran", so the
mapping states it explicitly rather than approximating:

- One `<testsuite>` per run, named after the plan.
- One `<testcase>` per plan step, named `<index>.<route>.<action>`, in plan
  order.
- An executed step that succeeded is a passing case. The step that failed
  carries `<failure type="<error_type>" message="<summary>">` with the step
  result as the body.
- Steps after the failing one never ran, and are `<skipped>` with a message
  saying the run stopped earlier, not passes.
- A plan refused at preflight produces a suite whose every case is `<skipped>`,
  plus one `<testcase name="preflight">` carrying `<error>` with the report's
  `validation_error`: the field, the route, the action and the summary. No
  hardware was touched, and a reader must not mistake that for a bench failure.
- A cleanup failure adds a final `<testcase name="cleanup">` with a `<failure>`,
  because a run whose sessions did not close is not a passing run whatever its
  steps did.
- An audit failure (`audit_ok: false`) adds `<testcase name="audit">` with a
  `<failure>`, for the same reason.
- `time` is set from the step result's `elapsed_ms` where the backend reports
  one, and omitted otherwise. The run report carries no universal per-step
  duration, and inventing one would put a number in a test report that nothing
  measured.

Three things the implementation settled that the mapping above left open. A run
refused before it reached a plan at all, by a missing configuration or one bound
to another workspace, writes the same `preflight` case with the refusal as its
`<error>` and no step cases, because there was no plan to name them from. A run
that was stopped on request keeps the steps it ran as passes and marks the rest
`<skipped>` with the stop as the reason, because a stop is not a failure here and
nothing is invented to make it look like one. And the suite's `timestamp` and
`time` are read off the step results the run recorded, so a document written
minutes after a run still says when the run happened.

One run leaves no document, and it is the run that prints no JSON either: an
interrupted one, the cancelled job and the dead runner of the table above. Its
record is the report the reactor commits before the interrupt goes on out of the
process, which the action already reads from `reports.directory` for exactly
this case. Everything the command answers at all, refusals included, writes the
file.

## Failure modes

| Condition | Where it is caught | Job result |
|---|---|---|
| `bench` is not `local` | Action, before install | `refused`, `bench_not_supported` |
| Hosted runner | Action, before install | `refused`, `runner_not_self_hosted` |
| Fork `pull_request`, any `pull_request_target`, any `workflow_run` | Action, before install | `refused`, `untrusted_event` |
| `drives-hardware` not `true` | Action, before install | `refused`, `opt_in_missing` |
| `version` is not an exact version | Action, before install | `refused`, `version_not_pinned` |
| Installed version does not match `version` | Action, after install | `refused`, `version_mismatch` |
| Plan file missing, or outside the workspace | Action, then the reactor's own `test_config_invalid` | `failure` |
| No configuration on the runner, or one bound to another workspace, or one inside the workspace | `doctor`, then the reactor | `failure`, `config_invalid` |
| Debugger toolchain missing or unusable | `doctor` | `failure`, before any plan step |
| Plan contradicts the bench (unknown device name, missing permission, disallowed symbol, artifact outside the allowed roots, session order, `can_send` on a listen-only bus) | Reactor preflight | `failure`, `test_config_invalid`, no hardware touched |
| A device is held by another run | Coordination | `failure`, `device_busy` naming the holder; `wait-s` bounds the waiting |
| A resource is quarantined | Coordination | `failure`, `resource_quarantined`; the summary carries the `quarantine_guidance` |
| A step failed on the board | Reactor | `failure` with `failed_step` and `step_error_type`; the `recovery` block says what state the bench was left in |
| Sessions did not close | Reactor cleanup | `failure`, `cleanup_failed` |
| The audit ledger could not be written | Report layer | `failure`, whatever the steps did |
| Job cancelled or the runner died | The reactor's cleanup, then the operating system's lock | Artifacts still uploaded; the bench lock is released when the process dies, with no quarantine and no manual recovery |

Two rules about what the action must never do on failure. It must not run
`agentic-hil recover`: that command requires `--confirm-safe-state`, which is an
operator's statement that they have physically checked the bench, and a workflow
cannot make that statement. And it must not retry a failed run automatically:
`retry_safe` is a per-result claim about one call, not about a plan, and a
re-run that flashes a board whose state is unknown is exactly the situation
quarantine exists for. Both belong to a person, with
[TROUBLESHOOTING.md](https://github.com/agentic-hil/agentic-hil/blob/master/TROUBLESHOOTING.md)
in front of them.

## Mechanics

The action is a composite action. Not a Docker container action: a container on
a self-hosted runner would need the USB devices, the serial nodes and the CAN
interfaces passed through, and Docker container actions are Linux-only, while
the benches this serves run on all three platforms.

It runs `agentic-hil test-reactor --test-config <plan> --junit-xml <path>
[--wait-s N]` in `working-directory` and reads the single JSON object the
command writes to standard output. That is the report, and the CLI's exit
status is already the composite success predicate: a run is successful only
when `ok` is true and nothing else in the result contradicts it, so the action
does not re-derive success from `ok` alone. It also reads the persisted report
from `reports.directory` when the process was interrupted and printed nothing.
The JUnit file is written by the same command, on the refusing and failing
paths as well as the passing one, so the upload step has something to take
whatever the run did.

Concurrency is the workflow's to declare. The bench lock is machine-wide and
independent of the repository, so a second job on the same runner is refused
with `device_busy` naming the holder rather than corrupting anything; a
`concurrency` group per bench turns that refusal into a queue.

## Decisions the implementation must make

Two of these have since been decided, and both went the way that keeps the
action small. **Whether JUnit belongs in the CLI**: it does.
`agentic-hil test-reactor --junit-xml <path>` writes the mapping above, so the
action passes a path and holds no copy of it, the same file reaches anyone
running the reactor by hand, and the mapping is under this project's own tests
instead of inside a workflow file. The case against, that a CI file format has
no place in a tool whose output is otherwise one JSON object, is answered by the
flag being opt-in: without it the command writes nothing new, and with it the
JSON result, the report files and the exit code are unchanged. And
**skipped-step reconstruction**, which the first decision settled with it: the
document is written from inside the run that already loaded the plan and is
handed the loaded steps, so the action never reads a plan file, there is no
second reader of the plan schema outside the package, and the report did not
have to grow a step count that no other reader wanted.

The rest are genuinely open, and each changes the action's shape:

- **Install or require.** Whether the action installs the pinned version into a
  throwaway environment under the runner's temp directory on every run, or
  requires the operator to have installed it and only verifies the version. The
  first keeps the workflow the single source of truth for the version and needs
  network access on a machine that may be deliberately offline; the second
  matches operators who pin their whole toolchain and makes `version` a check
  rather than an instruction. A hybrid (install only when the present version
  does not match) has the worst property of both, which is that the workflow
  cannot tell which happened, unless the run summary says so.
- **Who uploads.** Whether the action calls the upload action itself, or only
  emits paths and leaves uploading to the workflow. Calling it pins a third-party
  action inside ours and makes the version we pin everyone's problem; emitting
  paths means every consumer writes the same three lines and some of them forget
  `if: always()`.
- **Whether `doctor` should run at all by default.** It spawns the debugger
  toolchain and takes seconds, and the reactor's own preflight already refuses
  everything `doctor` would refuse about the configuration. What `doctor` adds
  is an earlier, clearer failure when the toolchain or the board is the problem,
  before a plan is half-validated.
- **How much of the report the job summary shows.** A long plan makes a long
  table, and GitHub truncates step summaries. Either the summary is bounded to
  the failing step plus a count, or it is complete and risks being cut in the
  middle.
- **The environment the action trusts.** `AGENTIC_HIL_CONFIG` is meant to be the
  runner operator's, set in the runner service environment; a workflow can set
  the same variable on the job or the step, and the action cannot tell the two
  apart. The product refuses an in-workspace configuration and a mismatched
  `workspace_root`, which closes the dangerous case, but the action still has to
  decide whether it reports the variable's presence in the summary, refuses when
  it is set at step level in a way it can detect, or says nothing and relies on
  the digest.

## Related

- [The portable test plan, the bench configuration, and the run attestation](test-plan-contract.md): what the plan may say, and what the report attests.
- [Running hardware tests](testing.md): the reactor and the pytest plugin, which is the other way a CI job can drive this bench.
- [Safety model](safety-model.md) and [security design](security-design.md): the locks, the incidents, and the audit chain the artifacts above are evidence from.
