# CI examples: running a plan on a self-hosted bench

Two worked files, shipped in this repository and runnable as written once the
runner is set up and the release they pin is published:

- [`examples/ci/github-actions.yml`](https://github.com/agentic-hil/agentic-hil/blob/master/examples/ci/github-actions.yml):
  a GitHub Actions workflow with a hardware job on a self-hosted runner and a
  simulator job on a hosted runner beside it.
- [`examples/ci/gitlab-ci.yml`](https://github.com/agentic-hil/agentic-hil/blob/master/examples/ci/gitlab-ci.yml):
  the same two jobs for GitLab CI.

Copy one into your own repository, change the runner labels, the plan paths,
the build command and the pinned version, and nothing else. This page is what
the two files assume: what the runner has to provide, what the jobs leave
behind, and which half of the trust rule a workflow can state at all.

Both examples exist because the projects that need them do not all use the
planned [`agentic-hil/run` action](github-action-design.md). The action, when
it ships, will collapse most of the hardware job into one step; the refusals,
the concurrency group and the evidence bundle stay exactly as they are here,
because they are properties of the bench and not of the action.

## What the runner has to provide

The runner is a machine with a board physically attached to it, and everything
below is set up once, by the person who owns that machine. None of it is done
by the workflow, and none of it can be.

- **The board and the permissions to reach it.** The debug probe and the serial
  port, with the runner's own operating-system user holding the rights to open
  both. A run that cannot open the probe fails at its first hardware action,
  and no CI setting can grant what the operating system withheld.
- **The debugger toolchain and the build toolchain.** OpenOCD or another
  supported backend, plus whatever your firmware build needs. The examples call
  CMake with a preset because that is what the
  [STM32 starter](https://github.com/agentic-hil/stm32-starter) does; that step
  is yours to replace.
- **Agentic HIL, installed for the runner's user, at the version the workflow
  pins.** The examples install nothing on the bench. They read
  `agentic-hil --version` and stop when it is not the pinned release, which is
  the check that keeps the evidence placeable: a bench that quietly drifted a
  release ahead produces reports nobody can compare with last month's. Installing
  is the operator's, because a bench may be deliberately offline and because an
  installation a workflow could move is an installation nobody pinned.
- **`agentic-hil init`, run once, in the runner's own checkout.** The
  authoritative configuration is discovered outside the repository and binds a
  mandatory absolute `workspace_root`. Run `init` in the working directory the
  runner will actually use, under the runner's own work directory, and nowhere
  else: a configuration bound to a different directory is refused rather than
  reused. A configuration kept elsewhere can be selected with an absolute
  `AGENTIC_HIL_CONFIG` in the runner service's environment, but its
  `workspace_root` still has to be that checkout. See
  [Configuration](configuration.md).
- **`bash`.** Both examples use it for the steps that loop over plans. On a
  Windows bench that is the shell Git for Windows installs, which the GitHub
  runner already selects for `shell: bash`.

The hosted simulator job needs none of this. It installs the pinned release
from the package index and loads each plan through the reactor's own loader with
`agentic-hil check-plan`, and that is the whole of it. That command is the
loadability check the job exists for: it calls the same `load_test_config` the
bench would, so a plan it accepts is one the reactor can load, where a
schema-only reader would pass a plan using a key from a later plan version, or
one with the duplicate keys the real loader refuses, and let the failure reach
the bench.

## The version pin

Both files carry one variable, `AGENTIC_HIL_VERSION`, set to the exact release
these examples are written for:

```yaml
AGENTIC_HIL_VERSION: "0.21.2"
```

An exact version, never a range, never `latest`, and never a git reference. A
version a resolver picked is a version nobody reviewed, and it would mean the
hosted job and the bench were checking different code. This repository's own
version gate holds that string to the release this source tree builds toward,
which is the release that first exposes the `check-plan` and `run-evidence`
commands these examples invoke rather than the release before it, so the pin
names the distribution the examples were tested against and cannot drift off it:
`python tools/check_version_consistency.py --list` prints all three files among
the positions a release stamps.

That "builds toward" is why the caveat above says *once the release they pin is
published*. On a release commit the release being cut is the one the examples
pin, so the file a reader receives in that release pins a version already on the
index. On `master` between releases the pin names the next release, which the
index does not carry yet: copying the simulator job before that release exists
installs nothing, and the previous release does not expose the commands these
examples were written to show. Pinning the previous release instead would name a
distribution a reader can install today but that rejects `check-plan` and
`run-evidence` at argument parsing, which is the worse of the two. The publish
workflow closes the loop rather than leaving it to trust: after PyPI accepts a
release, `tools/verify_published_examples.py` installs exactly the pinned
distribution and confirms its CLI reports that version and answers every command
the examples invoke, so a release whose artifact does not match its own examples
is an alarm on the release and not a stranger's failed copy.

The same rule reaches everything else the jobs fetch. Actions are pinned by
commit SHA with the tag they stood for in a trailing comment, not by tag. No
step pipes a script from the network into a shell. If you give the GitLab
simulator job an `image:`, pin it by digest, because a floating tag is the same
unversioned download in another spelling.

## What the jobs leave behind

Two artifacts per plan, and they answer different questions.

`agentic-hil test-reactor --test-config <plan> --junit-xml <path>` writes a
JUnit XML file, which is what a test-report UI reads: GitHub's checks
annotations, GitLab's merge request test widget, and every dashboard that
consumes JUnit. The GitLab example declares it under `artifacts:reports:junit`,
so the widget picks it up. The mapping from a run report to JUnit, including
what a refused plan and a failed cleanup look like there, is specified in
[the action design](github-action-design.md#the-junit-mapping); the file is
written on every red path too, so a job never has to explain a missing
artifact.

`agentic-hil run-evidence --report <run report> --out <dir>` turns the run
report into the bundle a reviewer without access to the bench can read. In the
output directory:

- `run-summary.json`: the one file a downstream consumer should need. The
  outcome, the plan and its digest, the firmware commit, the tool versions, the
  bench identity by configuration digest, and the failing step with its error
  type. Its shape is specified in
  [the action design](github-action-design.md#the-json-run-summary).
- `job-summary.md`: the same run as prose and a step table, for a human. The
  GitHub example appends it to `$GITHUB_STEP_SUMMARY`, so it is on the job's own
  page; GitLab has no equivalent, so that example prints it into the log and
  keeps it as an artifact.
- `logs/`: the workspace copies of the run's event logs, one file per serial
  session and one per bus session, plus the debugger backend's own logs.

Both of those, and the run report itself, are uploaded with `if: always()` on
GitHub and `when: always` on GitLab. The red run is the one whose evidence
matters most, and a job that uploads only on success is a job that keeps
exactly the evidence nobody needs.

Two things stay on the bench. The canonical, hash-chained copies of the logs
and reports live under the configuration's `state_root`, outside the workspace:
they are the ledger the uploaded mirrors are checked against, and shipping them
to an artifact store would publish the thing they exist to verify. And the
bench's hardware identities stay out of the summary, because it is
world-readable on a public repository: the configuration digest and the logical
device names are what identify a bench there, never a probe serial or a device
path.

### Give every plan its own report

Both examples capture each run's report from `--json` into its own file, named
after the plan under `artifacts/reports/`, and feed that file to `run-evidence`
in the same step as the plan it belongs to, rather than reading the shared
`.agentic-hil/reports/last-report.json`. A plan refused before its first step,
or refused for having no configuration at all, writes nothing to that shared
file, so a job that read it would build the refused plan's evidence out of the
previous plan's report, and a first plan refused would leave the bundle empty.
`--json` is written on every path, a refusal included, and names the plan it is
about, so the capture is always this plan's own. Collecting the evidence in the
same step, into a directory named after the plan, is also what keeps a job that
runs three plans from ending with the evidence of only one.

## Which of the trust rule's conditions a workflow can state

[The trust rule](github-action-design.md#the-trust-rule) is four conditions.
Only the last two belong to the workflow file, and the examples state them.
The first two are settings on the repository or the project, and no `if:` and
no `rules:` can stand in for them.

| Condition | GitHub Actions | GitLab CI | What actually enforces it |
|---|---|---|---|
| 1. A self-hosted runner, owned by the repository or organisation | `runs-on: [self-hosted, agentic-hil, <board>]` selects one, and cannot prove who owns it | `tags:` select the runner registered with those tags | Runner groups and which repositories may use them; on GitLab, a project-owned or group-owned runner and no shared runners for this project |
| 2. Explicit opt-in to hardware | The workflow file is the opt-in: a repository with no such file reaches no bench | The same | Review of the file, and branch protection on the branch it lives in. The planned action makes this a `drives-hardware: true` input, which is a claim a reviewer can grep for |
| 3. Refuse a hosted runner | The `Refuse a hosted runner` step: `RUNNER_ENVIRONMENT` must read `self-hosted` | No equivalent variable exists; the tags are the whole of the selection, and `agentic-hil doctor` is what fails on a machine with no bench | The step itself, as a second line under `runs-on` |
| 4. Refuse an untrusted event | The job's `if:`: no fork `pull_request`, no `pull_request_target`, no `workflow_run` | The job's `rules:`: no fork merge request, no `external_pull_request_event`, no `pipeline` | The `if:` and the `rules:`, plus the repository's fork-pull-request approval setting, which is what stops fork code from executing at all |

The two halves fail differently, and that is why both are needed. A repository
setting stops the job from ever starting; the `if:` and the `rules:` stop a job
that started anyway, because somebody added a trigger, copied the file into
another workflow, or changed a runner group. Neither one is the other's backup
plan: they refuse at different moments, and the examples carry the refusals in
the file so that copying the file copies them.

`pull_request_target` and `workflow_run` are refused by name rather than
inspected. Both hand base-repository trust to code that arrived from a head
repository, which is the exact shape the rule exists to stop, and there is no
inspection that makes them safe. GitLab's `external_pull_request_event` and
`pipeline` sources are refused for the same reason. A fork's change reaches the
bench by being pushed to a branch in this repository first, where a human has
looked at it.

## What the examples deliberately do not do

- **They never write the authoritative configuration.** It is not an input, not
  a file the job creates, and not something a plan can name. A configuration
  stored inside the workspace is refused at load, and so is one whose
  `workspace_root` is not the directory being run in, so a pull request cannot
  redirect policy to a file it committed.
- **They never run `agentic-hil recover`.** That command requires
  `--confirm-safe-state`, which is an operator's statement that they have
  physically checked the bench, and a pipeline cannot make that statement.
- **They never retry a failed run.** A re-run that flashes a board whose state
  is unknown is exactly the situation quarantine exists for. A red run belongs
  to a person, with
  [TROUBLESHOOTING.md](https://github.com/agentic-hil/agentic-hil/blob/master/TROUBLESHOOTING.md)
  in front of them.
- **They never degrade to a run without hardware.** There is no fallback path
  that turns a bench failure green, because a green job that did not test the
  board is the false green the whole product exists to avoid.

## Related

- [The `agentic-hil/run` GitHub Action design](github-action-design.md): the
  trust rule, the inputs, the evidence bundle, and the failure modes in full.
- [Running hardware tests](testing.md): the reactor, the plans, the JUnit
  output, and the pytest plugin, which is the other way a CI job drives a bench.
- [The portable test plan](test-plan-contract.md): what a plan may state, what
  only the bench configuration binds, and what a run attests.
- [Configuration](configuration.md): the authoritative file, `workspace_root`,
  and `AGENTIC_HIL_CONFIG`.
- [Safety model](safety-model.md): the locks a concurrency group turns into a
  queue, and what happens when a job is cancelled mid-run.
