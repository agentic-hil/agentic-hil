# Substitute comparison protocol, 2026-10-06

Status: preregistration draft, no trials executed. Publish this document and the completed version manifest before the first timed trial. Any later change receives a new protocol revision; keep the original and affected trials.

Agentic Hardware-in-the-Loop (Agentic HIL) is compared with a coding agent using STM32CubeProgrammer and a serial tool. This first comparison covers these two paths only. It does not establish an advantage over PlatformIO, narrow MCP servers, pytest-embedded or Jumpstarter.

## Fixed task and paths

Use one physical NUCLEO-F446RE with its onboard ST-LINK, USB UART at 115200 baud and the same host for both paths. Source: [stm32-starter commit 084b252ecc4565d60aa52b32a18d3a0430d18724](https://github.com/agentic-hil/stm32-starter/tree/084b252ecc4565d60aa52b32a18d3a0430d18724), observed through GitHub's commits API on 2026-10-06. Pin this complete SHA, not `main`.

Give each fresh coding-agent session this same task:

> Build the supplied starter, demonstrate its shipped nominal and recovery behavior and its failing diagnostic assertion on the physical board, fix the firmware defect without changing the protocol or assertions, then rebuild and demonstrate all three assertions passing on the same firmware revision. Retain the board responses, firmware digest, commands, failures, cleanup state and final evidence.

The shipped defect is that firmware recognizes `DIAG ENABLE` while the protocol sends `DIAG ON`. Do not disclose that diagnosis in the trial prompt or provide previous trial transcripts.

| Path | Hardware interface | Test interface |
|---|---|---|
| A | Agentic HIL MCP, `stlink` backend using the same CubeProgrammer binary as B | Unmodified starter plans via `test_reactor_run` |
| B | STM32CubeProgrammer CLI and Python pyserial in a separately authorized environment | Translate the same plan steps to a small serial assertion script; preserve script and its creation time |

B may create scripts during its timed trial. It receives the starter plans as the common acceptance specification, but no Agentic HIL hardware calls or reports. Both paths receive the same starter source, build prerequisites, task and network access to their own public documentation.

Before any trial, the operator must explicitly authorize B's direct debugger and serial interface, with the same intended board and allowed physical operations as A. The starter's `AGENTS.md` requires Agentic HIL for every hardware action; putting B in another directory does not waive that instruction. Freeze and publish path-specific agent instructions: A retains the MCP-only rule; B carries the operator's recorded override for the specified baseline tools only. Do not grant raw operations silently or disable A's policy. Without that authorization, B is `blocked` and no comparative result is reported.

The task, source, model, assertions and host prerequisites match. The hardware-interface instruction differs by design and must appear in the manifest and every trial record; the contexts are not claimed to be identical.

Assertions retain the source plans' flash/open-UART-before-reset sequence, cleared input buffers, boot response and five-second read timeouts:

- Nominal: `STATUS` returns `"state":"READY","diagnostic":"NONE"`.
- Diagnostic: `DIAG ON` returns `"state":"DEGRADED","diagnostic":"E_SELF_TEST"`. Before the fix, retain its failed assertion and actual response.
- Recovery: send `DIAG ON`, consume its response separately, then send `DIAG CLEAR`; the new response contains `"state":"READY","diagnostic":"NONE"`. A buffered answer to the earlier command cannot satisfy the final assertion.

## Version and clean-state freeze

Select Claude Code 2.1.258 with requested model `claude-opus-5` and requested effort `low` for all six trials. Verify its availability and returned model identifier on the comparison host before publishing the manifest. If it is unavailable, revise the protocol before any trial; do not silently substitute it.

Agentic HIL release is pinned to [v0.23.0](https://github.com/agentic-hil/agentic-hil/releases/tag/v0.23.0), published 2026-10-05. Record the resolved package artifact and SHA-256 and full dependency lock. pyserial is pinned to 3.5. Installation availability and compatibility have not been tested here. Before trials, choose and publish the exact CubeProgrammer version and executable SHA-256; use it in A and B.

The manifest must also freeze host OS/build, Python, CMake, Ninja, GNU Arm compiler, USB driver, ST-LINK firmware, Claude Code CLI, Python dependency lock, documentation snapshots and protocol digest. These values remain unresolved until inspected on the selected host. No complete version matrix is claimed now.

Restore the same verified host snapshot or dedicated clean account state before each trial: identical preinstalled compiler/build tools, CubeProgrammer, Python, Claude Code and USB permissions; no starter clone/build output, path-specific package installation, MCP registration, authoritative bench configuration, serial script, conversation history or previous trial evidence. Authentication may persist and is recorded. Use identical dependency cache conditions, record warm/cold caches, and prohibit sharing files learned during previous trials. Agentic HIL installation/configuration and B's serial dependency/script setup are timed. Record preinstalled prerequisites separately; this is not blank-OS setup time.

A bench operator restores the board to the same known shipped image and performs a full power-cycle before each start, outside the clock. Verify the firmware digest and cleared UART, release all sessions and locks, and record this preparation. No concurrent bench job may run.

## Order, clock and outcomes

Execute six trials in order A1, B1, B2, A2, A3, B3. Each pair reverses order. Start a fresh agent session each time with the frozen common task and recorded path-specific instructions, and a maximum of 60 minutes. Do not retest a failed trial under its old ID.

Start event: task delivery to the fresh session after the clean-state checklist has passed. Record UTC and a monotonic timestamp. Record first green nominal assertion as a secondary milestone, not task completion. End event: the last fixed-firmware assertion passes and evidence has been written with firmware digest and final cleanup state. Elapsed time includes installation, configuration, builds, approvals, failures and retries after start.

Success requires the initial expected diagnostic failure, a firmware-only fix, all three final assertions on one rebuilt image and saved evidence. Missing initial failure, altered assertions or missing evidence cannot count as success. At 60 minutes record `timeout` with elapsed observation and unfinished step. Record `failed` for an earlier terminal error and `blocked` for permission or external prerequisite failure. Preserve all failures. Record external outages and recovery delay; do not subtract them or replace trials. Report censored observations separately, never as completed times. Publish all three observations per path, successful median/range with denominator, and failure counts; do not calculate an advantage from incomplete unequal samples.

## Human work, policy, recovery and maintenance

Log every post-start human action with UTC, duration, reason and category: approval, instruction, diagnosis, configuration, retry or physical intervention. Count decisions/interventions separately from shell commands; agent-issued commands are not manual steps. Shared host/board preparation stays outside the measured interval and is listed explicitly.

After the timed task, run separately recorded capability checks in authorized isolated test environments. Check whether a denied flash or UART write is rejected before hardware changes, with permission/configuration evidence. Never disable policy or issue mass erase/raw debugger commands to manufacture a result. If safe denial setup is unavailable, record `not_tested`, not a policy advantage.

Use the initial expected diagnostic failure to inspect failure reporting, serial closure, process cleanup, locks and readiness for the following run. Record automatic recovery actions, error type and manual repair, including elapsed time to next valid assertion. It is not a power-failure or hung-board test. Preserve actual received UART data. Record unsupported checks as `not_tested`.

Retain configuration and script files plus diffs. Count files/lines authored, dependencies, device-specific settings, repairs and human maintenance actions observed during these trials. Do not extrapolate long-term maintenance cost or claim it is zero. A later maintenance scenario requires separate preregistration.

## Per-trial record and acceptance checklist

Each trial gets its own directory and `trial.json` with these required fields:

```json
{
  "id": "A1",
  "path": "A",
  "protocol_sha256": "REQUIRED",
  "manifest_sha256": "REQUIRED",
  "starter_sha": "084b252ecc4565d60aa52b32a18d3a0430d18724",
  "operator_relationship": "maintainer",
  "clean_state_verified": false,
  "start_utc": null,
  "end_utc": null,
  "elapsed_s": null,
  "first_green_s": null,
  "outcome": "not_run",
  "initial_assertions": {"nominal": null, "diagnostic": null, "recovery": null},
  "final_assertions": {"nominal": null, "diagnostic": null, "recovery": null},
  "firmware_sha256": null,
  "human_actions": [],
  "policy_checks": [],
  "recovery_checks": [],
  "maintenance_changes": [],
  "artifact_manifest": [],
  "deviations": []
}
```

- Before start: published frozen manifest; clean-state and board-preparation evidence; no concurrent owner; correct path/model/effort.
- During trial: timestamped agent transcript and every tool/command result; initial assertion records; all failures/retries and human actions.
- At success: firmware diff only, rebuilt ELF digest, three matching final UART assertions, per-run evidence, cleanup/session/lock state.
- Before reporting: artifact filenames and SHA-256, readable raw UART and debugger logs, configuration/script snapshots, outcome and all deviations. A's canonical per-run reports must be collected, not only overwritten `last-report.json`. B must retain equivalent assertion records and command exit codes. Export JUnit when available and mark absent formats explicitly.

Keep private originals; publish redacted copies with a substitution manifest and hashes for both. Withhold credentials, account/host names and probe serials consistently without removing failures or UART responses.

These trials are run by the project maintainers. Familiarity and protocol ownership mean they are not independent proof. Independent reproduction requires a separate operator who reports affiliation and follows the frozen protocol. Publish losses and limitations before making any relative claim.
