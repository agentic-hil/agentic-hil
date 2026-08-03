# Agentic HIL Agent Instructions

Agentic HIL is the hardware gate. It discovers the project's authoritative configuration outside the repository; `AGENTIC_HIL_CONFIG` may override that location with an absolute path. Mandatory `workspace_root` binds either file to the current project, and mandatory `state_root` pins trusted report and hardware-lease state outside the workspace.

For installation and first-time setup, follow [AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) — everything installs user-local without admin rights.

Setup has two scopes. User scope is per user, per machine: all of it under the invoking user's home, invisible to other OS users on the host.

| Command | Scope | Does |
|---|---|---|
| `agentic-hil agent-install --agent <agent>` | user, once per user and agent | agent skill + user-level MCP registration; needs no workspace or config, and finishes even where the config location is refused |
| `agentic-hil init [--agent <agent>]` | project, once per project | writes the workspace's authoritative config, then `doctor` |
| `agentic-hil setup --agent <agent>` | both, in order | first run in one command; later projects for this user need `init` alone |

Neither half rolls back the other: `setup` with `ok: false` but `scopes.user.ok` true left a working agent installed — read that field before reporting nothing was installed.

Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

Use Agentic HIL MCP tools for hardware actions. Do not bypass them with raw OpenOCD commands, arbitrary debugger shells, direct serial-device access, or direct CAN-adapter access when an Agentic HIL tool is available.

If an Agentic HIL tool returns `permission_denied`, stop and ask the operator to review the authoritative config. Do not bypass it or replace the operator-controlled environment or MCP host registration. Reading a device needs no permission, so a `permission_denied` is always about writing or changing state — unless the configuration has no `version:` key, in which case it is read under version 1 where reading still needs `allow_probe` / `allow_read`.

A `device_busy` refusal is not a fault: another run holds that physical device for its whole duration, and the result names the holder. Wait for it or ask its owner; never delete a lock under `~/.agentic-hil/device-locks`. An `undeclared_device` refusal means the run reached for a device its test plan does not name — add it to the plan and rerun.

A sequence of hardware calls is one run, and it has to say so. `bench_run_start` takes every device you name — `{"kind": "debugger"|"uart"|"can", "id": "<config entry>"}`, the DUT is not a device — and holds them until `bench_run_stop`; without it each call holds its device only for its own duration and the board is free between two steps. Inside a run only the declared devices may be touched, a device already held fails the start immediately naming its holder, and nothing is left half-acquired. Two entries describing one physical unit collapse to one lock. Nothing times a run out: it ends when you close it, when the client disconnects, or when the server process dies. `bench_run_status` says whether one is open; `agentic-hil://reference/lease-lifecycle` has the rest.

Read facts about the server from its MCP resources, not from its source or its installed package. A failing result already carries `remediation` and `do_not` when a fix is known; `resources/list` carries the rest.

| Resource | Answers |
|---|---|
| `agentic-hil://reference/debugger-backends` | which `debuggers.<name>` field each of `openocd`, `stlink`, `pyocd` requires, discovers, or ignores; when `probe_id` and `flash_address` become mandatory |
| `agentic-hil://reference/target-support` | which field names the target per backend, known-good values, where pyOCD `target_type` values come from |
| `agentic-hil://reference/errors` | every `error_type` with its meaning, ordered fix, and the wrong fix; one entry at `.../errors/<error_type>[:<field-or-backend>]` |
| `agentic-hil://reference/platform-paths` | which locations pass the path trust check, and where the configuration and `state_root` belong |
| `agentic-hil://reference/lease-lifecycle` | which device a run locks and for how long, `device_busy` and `undeclared_device`, why a crashed run needs no recovery; lease states, the continue predicate, the recovery path |
| `agentic-hil://reference/config-schema` | the authoritative configuration JSON Schema |

Continue after a result only when `ok` is true, `target_ok`, `audit_ok`, and `cleanup_ok` are not false, `cleanup_required` and `quarantined` are not true, `lease_state` is one of `null`, `active`, or `released` (any other value, including `stale`, blocks success), `side_effect_status` is not `unknown` or `partial`, and `hardware_state` is not `unknown`. The public `overall_success()` helper in `agentic_hil.report` encodes exactly this predicate. For quarantined hardware, stop effects and read `cleanup_reasons` plus `auto_recoverable` from `hardware_lease_status`. `auto_recoverable: true` means the running owner clears the incident itself on the next hardware call, so retry that call once: it reaps leftover debugger processes, verifies the safe state under the bench's `auto_recover_policy`, and proceeds. A refusal carrying `auto_recovery_attempted: true` means that already ran and did not confirm the safe state — do not retry it again. Otherwise ask the operator to inspect `agentic-hil lease-status`, physically confirm the current incident, and run `agentic-hil recover --confirm-safe-state --quarantine-id <id>`. If recovery returns `config_changed` (the authoritative config changed since the incident), the operator verifies the config delta and reruns with the explicit `--accept-config-change` override.

Install or update the local agent setup skill with:

```bash
agentic-hil skill-install --agent opencode
```
