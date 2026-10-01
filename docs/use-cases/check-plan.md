# Use case: check hardware test plans in CI without a board

**The problem.** Hardware test plans live next to the firmware and change with
it. A hosted CI runner has no board, so a broken plan is usually found on the
bench, later, as a red run that had nothing to do with the firmware. The
opposite failure is worse: a board-free job that turns green and is read as "the
hardware test passed".

**What Agentic Hardware-in-the-Loop (Agentic HIL) does.** `agentic-hil
check-plan` loads each plan through `load_test_config`, the loader the test
reactor uses on the bench, and touches no hardware. A plan it accepts is one the
reactor can load: the closed schema, the rejection of duplicate keys and
non-finite numbers, and the plan-version gates are the same ones the bench
applies. It runs no firmware and models no electrical behaviour, so a green run
says nothing about a board and everything about the plans. The second half of
this page is what only the board shows.

## On any machine

The [STM32 starter](https://github.com/agentic-hil/stm32-starter)'s three plans,
Agentic HIL 0.21.2 (the release the starter's lock file pins), on Windows,
2026-10-01, with no configuration in the workspace:

```bash
agentic-hil check-plan tests/hil/nominal.testconfig.yaml tests/hil/diagnostic.testconfig.yaml tests/hil/recovery.testconfig.yaml
```

```text
All 3 test plan(s) load through the reactor's loader.

plans
  - ok
      plan   tests/hil/nominal.testconfig.yaml
      name   nucleo-f446re-nominal-status
      steps  6
  - ok
      plan   tests/hil/diagnostic.testconfig.yaml
      name   nucleo-f446re-diagnostic-selftest
      steps  6
  - ok
      plan   tests/hil/recovery.testconfig.yaml
      name   nucleo-f446re-diagnostic-recovery
      steps  8
```

Exit 0. Inside a clone of the starter, `uv run agentic-hil check-plan` runs that
same release. It predates the device-name comparison further down, so its output
has no configuration block.

### A plan the reactor would refuse

A copy and paste left two `timeout_s` keys in one step:

```yaml
version: 3
name: duplicate-key
steps:
  - device: dut_uart
    action: uart_open
    clear_buffer: true
  - device: dut_uart
    action: uart_read
    comparator:
      pattern: "\"event\":\"boot\""
    timeout_s: 5
    timeout_s: 50
```

`yaml.safe_load` keeps the last value without a word, so a schema check on what
it read passes a step that waits fifty seconds, whatever its author meant. The
same release, the same day:

```text
1 of 2 test plan(s) would be refused by the reactor:
tests/hil/duplicate-key.testconfig.yaml.

plans
  - ok
      plan   tests/hil/nominal.testconfig.yaml
      name   nucleo-f446re-nominal-status
      steps  6
  - Test reactor configuration file is not valid YAML or JSON.
      error_type  test_config_invalid
      plan  tests/hil/duplicate-key.testconfig.yaml
```

Exit 1. Every plan is reported, so one run names all the refused plans rather
than stopping at the first. The output names the plan and the type of the
refusal; the line and the key the loader tripped over are not part of it.

### With a configuration, and `--strict`

From 0.21.4, `check-plan` also compares each plan's device names with the ones
the workspace's configuration declares, when it has one. The runs below used
Agentic HIL 0.21.5 on the same day, the nominal plan, and
`tests/hil/typo.testconfig.yaml`, a copy of it whose `uart_open` step names
`dut_uart2`. The configuration declares `dut` and `dut_uart`, the two names the
starter's bench uses. Without `--strict`:

```text
All 2 test plan(s) load through the reactor's loader, and 1 of them name device(s)
this workspace's configuration does not declare (dut_uart2), which --strict makes a
failure.

  strict  no

configuration
  This workspace's configuration was read, so each plan's device names were compared
  against what it declares.
  debuggers  dut
  com_ports  dut_uart

plans
  - ok
      plan   tests/hil/nominal.testconfig.yaml
      name   nucleo-f446re-nominal-status
      steps  6
  - Loads, and names 1 device(s) this workspace's configuration does not declare:
    dut_uart2.
      plan                  tests/hil/typo.testconfig.yaml
      name                  nominal-with-a-typo
      steps                 6
      unconfigured_devices  dut_uart2
```

Exit 0. The bench would refuse the second plan before its first step, but a
repository can hold plans for other benches, so this is a finding and not a
failure. `--strict` is for the job that means this bench. The same two plans
with it exit 1, and the output differs only in `strict  yes` and its heading:

```text
Failed: 1 of 2 test plan(s) name device(s) this workspace's configuration does not
declare (dut_uart2). All 2 of them load through the reactor's loader, and --strict
makes that finding a failure.
```

Without a configuration, which is the hosted case, both plans are reported `ok`,
and the output says what was not checked:

```text
All 2 test plan(s) load through the reactor's loader.

  strict  no

configuration
  Agentic HIL configuration file could not be found. No device names were compared
  against a configuration; the plans were checked for loadability only.
  error_type  config_file_not_found
```

The steps to create a configuration and the two plan rows follow. A
configuration that is present but cannot be read fails `--strict` as well,
because the comparison it asked for could not be made.

### In CI

- [`examples/ci/github-actions.yml`](https://github.com/agentic-hil/agentic-hil/blob/master/examples/ci/github-actions.yml)
  and [`examples/ci/gitlab-ci.yml`](https://github.com/agentic-hil/agentic-hil/blob/master/examples/ci/gitlab-ci.yml)
  ship a hosted `check-plan` job beside the bench job. It installs the pinned
  release and runs `agentic-hil check-plan tests/hil/*.testconfig.yaml`, on
  every pull request, forks included, because there is nothing in it for a fork
  to reach. [CI examples](../ci-examples.md) has the rest.
- The starter runs its board-free suite on a GitHub-hosted runner on every push
  and pull request and uploads the JUnit XML
  ([workflow runs](https://github.com/agentic-hil/stm32-starter/actions/workflows/check-plan.yml)).
  The suite calls the same loader, and also checks that the plans state the
  firmware's protocol and name no bench hardware: no COM port, no device path,
  no absolute path. Each test prints what its green is worth, here from the
  [Linux run](https://github.com/agentic-hil/stm32-starter/blob/main/validation/2026-09-15-newcomer-linux/README.md)
  of 2026-09-15 on 0.21.2:

```text
PASS  configuration and test semantics validated without a board
NEEDS PHYSICAL FIXTURE  electrical behavior not verified
```

## What only the board shows

All three starter plans pass every board-free check above, the diagnostic plan
included. On the board that plan fails until the firmware is fixed, here in the
Linux run of 2026-09-15 (0.21.5, abridged):

```text
Failed: comparator_unmet
...
          comparator
            pattern  "state":"DEGRADED","diagnostic":"E_SELF_TEST"
          received_tail
            text      {"state":"READY","diagnostic":"NONE"}
```

Nothing is wrong with the plan, so nothing that reads the plan can find this:
the firmware answers `DIAG ON` the wrong way, and only a board running that
firmware says so. [UART and CAN feedback](uart-and-can.md) walks through that
run and the fix.

`agentic-hil test-reactor` also checks what a board-free job cannot, before its
first hardware action: every device name against the configuration in force,
every step's permission, that the firmware image exists under the allowed
artifact roots, and the order of sessions. Run before the build, in the workspace and with the
configuration above, the nominal plan ends there (0.21.5, abridged):

```text
Refused: test_config_invalid

  Test reactor configuration failed semantic validation; no steps were executed.
...
  validation_error
    Firmware artifact does not exist.
    step    1
    field   steps[0].image_path
    route   dut
    action  flash
```

The starter's
[hardware workflow](https://github.com/agentic-hil/stm32-starter/actions/workflows/hardware-test.yml),
started by hand, runs the three plans on a self-hosted runner with the board
attached, and asserts that the diagnostic plan fails with `comparator_unmet` on
the firmware as shipped. It is not part of the pull request gate; the board-free
workflow is.

## Limits

- A green `check-plan` says nothing about the firmware, the wiring or the
  board.
- It checks no permissions and no artifact paths, and not whether the image
  exists; the preflight of `test-reactor` does.
- Without a configuration it compares no device names. With one, a name the
  configuration lacks is a finding, and a failure only under `--strict`.
- A refused plan is named with the type of its refusal, not with the line or the
  key that caused it.
- The device-name comparison and `--strict` need 0.21.4 or newer. The
  starter's lock file pins 0.21.2, which has neither.

## Try it

```bash
git clone https://github.com/agentic-hil/stm32-starter.git
cd stm32-starter
uv sync
uv run pytest -q -s
uv run agentic-hil check-plan tests/hil/nominal.testconfig.yaml tests/hil/diagnostic.testconfig.yaml tests/hil/recovery.testconfig.yaml
```

None of these needs a board. [Testing](../testing.md) has the plan format and
the reactor, and the [test plan contract](../test-plan-contract.md) what a plan
may state and what only the bench binds.
