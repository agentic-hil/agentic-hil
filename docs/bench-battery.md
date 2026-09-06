# The bench battery

`tools/bench_battery.py` runs this project's own bench checks against the
Agentic HIL you have installed and a project directory of yours. It answers one
question: does this installation, on this machine, with this board in front of
it, do what the release says it does.

It is the operator's counterpart to the two published test tiers. The container
tier proves the code against the real tools with no hardware, and runs in CI.
The bench tier needs a probe and a board and runs where one is. The battery asks
the same questions through the product's own commands, so a bench that has no
checkout of this repository can still be measured, and the result can be sent to
somebody who does have one.

## Running it

```
python tools/bench_battery.py --project ~/work/my-firmware
python tools/bench_battery.py --project ~/work/my-firmware --hardware
```

Without `--hardware` it runs what needs no probe. With it, it also runs what
needs the probe and the board.

Options:

- `--project` (required): the project directory the checks run in. Its own plans
  are checked and its own reports are written, exactly as for any run.
- `--product`: the installed `agentic-hil` to measure. Defaults to whatever is
  on `PATH`, which is usually what you want: the point is to measure the
  installation you have, not one built for the occasion.
- `--hardware`: also run the checks that reach the board.
- `--out`: where the report goes. Defaults to `bench-battery.json` in the
  current directory. Its directory is created if it is not there.
- `--keep`: leave the plans and the artifacts the battery wrote in your project
  instead of removing them, which is what you want when a check failed and you
  are about to read the file that failed it.

Before a run with `--hardware`, build your project's firmware. The plan check
runs the project's own `testconfig.yaml`, which flashes the image that plan
names; a plan that cannot find its image fails, and the report says so.

The battery measures Agentic HIL 0.21.2 and newer. `check-plan` and
`run-evidence` are not in an older release, so it says which release it found
and stops rather than reporting a healthy bench as broken.

The exit codes, so it can be a step in a script:

| Code | What it means |
| --- | --- |
| `0` | every check that ran passed |
| `1` | at least one check failed, and the report says which |
| `2` | the battery could not be set up, its arguments were wrong, or it stopped on a failure of its own, and the bench was not measured |

## What it checks

Always:

| Check | What it establishes |
| --- | --- |
| `version` | the installed product answers with a version |
| `com-ports` | the host's ports are listed, the ones carrying a USB identity in full and the rest collapsed to one line with their count |
| `debugger-probes` | probe discovery answers, exits zero, and every probe it lists carries an identifier. How far the count reaches is recorded rather than required, because two of the backends say nothing about it |
| `check-plan-strict` | a plan naming a device the configuration does not declare fails the board-free gate, headed by its outcome |
| `check-plan` | every plan your project ships, in the root or in a subdirectory and under either extension, loads through the reactor's loader |

With `--hardware`:

| Check | What it establishes |
| --- | --- |
| `doctor` | the configuration loads and every device it declares names hardware |
| `probe-inventory` | the attached probe is enumerated and carries a serial |
| `reset` | a real reset over the probe succeeds, and whatever the debugger said on the way is carried as evidence rather than read as a verdict |
| `plan` | your project's own plan is green on the board |
| `failing-claim` | a plan whose claim cannot hold is headed by the run's own outcome, not by a refusal |
| `permission-refusal` | a permission a plan needs is named at every level of the refusal, with the line that opens it, and is granted back |
| `run-evidence` | the job summary prints one digest prefix and an elapsed time in every step row |

## Reading the report

`bench-battery.json` carries one entry per check:

```json
{
  "name": "probe-inventory",
  "question": "the attached probe is enumerated and carries a serial",
  "command": ["agentic-hil", "debugger-probes", "--json"],
  "exit_code": 0,
  "judged": {"probes": 1, "probes_carrying_a_serial": 1},
  "outcome": "pass",
  "decisive_line": "1 probe(s) with a serial were enumerated"
}
```

- `question` says what the check is for, in words, so an entry can be read
  without the source beside it.
- `command` is what ran. Every check goes through an `agentic-hil` command:
  nothing here opens a debugger, a serial device or a CAN interface of its own,
  because a hardware action outside the tool is one nothing validated against
  the configuration, nothing locked against a second run and nothing recorded in
  the audit chain.
- `exit_code` is what that command exited on, which is a fact about the run
  separate from the verdict: several of these checks exist because a command was
  once exiting the wrong way over an answer that was right.
- `judged` is what was read out of the answer, named field by field.
- `outcome` is `pass`, `fail`, or `skip`. A skip is a check that had nothing to
  ask: no serial port declared, no plan in the project, no run for the evidence
  to be built from. Skips do not fail the battery, and the reason is in
  `decisive_line`.
- `decisive_line` is the line the verdict was read off, so a failure can be acted
  on from the report alone.

At the top, `ok` is the whole run, `counts` is the tally, and `product_version`
is the release the report is about. Put that number beside the report when you
send it on: a battery result is a statement about one release on one machine.

## What it touches

- **Your configuration is not read and not replaced.** The battery redirects the
  configuration root and the state root into a temporary directory and writes
  its own configuration there with `agentic-hil init`, which reads the attached
  bench and binds the probe and the port it publishes. Whatever configuration
  your project already has stays where it is, unread. Both platforms' variables
  are redirected, and the redirect is checked rather than assumed: if the
  configuration `init` selects is not under the battery's own root, the battery
  names that file and runs no check at all.
- **Your installation is not replaced.** uv's tool directory, its bin directory
  and its cache are redirected into that temporary directory too. Nothing here
  upgrades anything, and that is the point: an installed product is what is being
  measured.
- **The device locks are shared, deliberately.** `HOME` is left alone, because
  the machine-wide device locks live under it and they are what keeps this run
  off a board another run is holding. If a nightly job or another operator has
  the board, the battery waits or is refused by name rather than meeting them on
  the probe.
- **Your project directory is used as it stands.** The product writes its reports
  and logs under `.agentic-hil` there, as it does for any run. The battery writes
  its own plans as `bench-battery-*.yaml` beside them and its own artifacts, the
  plan report and the evidence bundle built from it, under
  `bench-battery-artifacts/`. Both are removed before the battery returns unless
  you pass `--keep`. The permission check revokes one permission in its own
  configuration and grants it back before the check returns.

## What the report does not carry

No probe serial and no port identity. What is recorded about hardware is that an
identity was there and how many, never what it said.

No absolute path either. The product is recorded by its file name and your
project by the name of its directory, and the paths this machine's own
directories would have put into a decisive line are replaced by a label. A
report is meant to be sendable, and the identifiers of somebody's bench, the
account name among them, are not part of what it has to say.
