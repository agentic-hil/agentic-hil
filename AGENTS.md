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

A project with no configuration answers every tool with `config_file_not_found`. `project_config_create` is the way out: it takes no arguments, generates the configuration for the workspace this server is bound to out of attached hardware, and writes every permission `false`, including `permissions.allow_config_write`. That last one is why it succeeds once and then refuses itself — the refusal comes out of the configuration, so what holds is exactly what a person reads there. It is not a way to grant yourself anything, and calling it again after the file is gone only regenerates the same restrictive skeleton: report the path and ask the operator for whatever the task needs beyond reading the bench. Never delete or move a configuration to get a different one.

Changing an existing configuration goes the same way — over MCP, never with your own file tools, which `setup` denies on that path for exactly this reason. Two permissions gate it, and the split matters: `permissions.allow_config_description_write` opens what the bench **is** (`target.*`, `debuggers.<n>.probe_id` / `executable` / `interface_cfg` / `target_cfg`, `com_ports.<n>.device` / `baudrate`, every `can_buses.<n>` field but its permissions), and `permissions.allow_config_permissions_write` opens the `permissions:` blocks. The first can be left open without handing over the second, so an operator who lets you enter a probe serial has not let you write `allow_flash` on yourself.

| Call | Answers |
|---|---|
| `project_config_describe` | which keys **this** caller may change in **this** state, which not, which permission would open a locked one, each key's current value and its shape from the schema. Needs no permission; reading is free |
| `project_config_set` | `{"changes": [{"key": "debuggers.dut.probe_id", "value": "…"}]}` — named keys, scalar values, all applied or none |
| `project_config_adopt_hardware` | reads the attached probe and fills in the identity keys that are still unset: probe id, the backend's executable, the detected controller, the probe's own COM device. Returns the plan; writes only with `{"apply": true}`, through `project_config_set` |

The value is a scalar and the key comes from a closed set, so there is no way to send a document or a subtree; that is what keeps the file something you did not author. A write is refused while a run or a session holds hardware (`config_write_in_open_run` — close it with `bench_run_stop`), the changed document is validated before it replaces the working one, and `provenance` records what moved, when, and through which call. The server keeps serving the configuration it loaded, so report `reload_required` and ask the operator to restart it.

A configuration written before the board was plugged in holds placeholders — `probe_id: null`, `executable: null`, `controller: "unknown-controller"`. Do not print those values for a person to retype: call `project_config_adopt_hardware`. It supplies no value of its own and its arguments only select (`probe_id` among several attached boards, `debugger_id` / `com_port_id` among configured entries). It fills a key only when the document has nothing there — absent, null, empty, or the placeholder the skeleton writes — and reports a key that already holds somebody's value under `kept` instead of replacing it. More than one attached probe, no probe, no COM port that carries the probe's serial: each is an answer that names what to do next, never a silent choice. Without `allow_config_description_write` the call is refused and still returns the exact keys and values, so what you hand the operator is one command rather than a serial to transcribe. The operator's own path is `agentic-hil adopt-hardware`.

Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

Use Agentic HIL MCP tools for hardware actions. Do not bypass them with raw OpenOCD commands, arbitrary debugger shells, direct serial-device access, or direct CAN-adapter access when an Agentic HIL tool is available.

If an Agentic HIL tool returns `permission_denied`, stop and ask the operator to review the authoritative config. Do not bypass it or replace the operator-controlled environment or MCP host registration. Reading a device needs no permission, so a `permission_denied` is always about writing or changing state — unless the configuration has no `version:` key, in which case it is read under version 1 where reading still needs `allow_probe` / `allow_read`.

A `device_busy` refusal is not a fault: another run holds that physical device for its whole duration, and the result names the holder. Wait for it or ask its owner; never delete a lock under `~/.agentic-hil/device-locks`. An `undeclared_device` refusal means the run reached for a device its test plan does not name — add it to the plan and rerun.

A sequence of hardware calls is one run, and it has to say so. `bench_run_start` takes every device you name — `{"kind": "debugger"|"uart"|"can", "id": "<config entry>"}`, the DUT is not a device — and holds them until `bench_run_stop`; without it each call holds its device only for its own duration and the board is free between two steps. Inside a run only the declared devices may be touched, a device already held fails the start immediately naming its holder, and nothing is left half-acquired. Two entries describing one physical unit collapse to one lock. Nothing times a run out: it ends when you close it, when the client disconnects, or when the server process dies. `bench_run_status` says whether one is open; `agentic-hil://reference/lease-lifecycle` has the rest.

Read facts about the server from its MCP resources, not from its source or its installed package. A failing result already carries `remediation` and `do_not` when a fix is known; `resources/list` carries the rest.

| Resource | Answers |
|---|---|
| `agentic-hil://reference/debugger-backends` | which `debuggers.<name>` field each of `openocd`, `stlink`, `pyocd` requires, discovers, or ignores; when `probe_id` and `flash_address` become mandatory |
| `agentic-hil://reference/target-support` | which field names the target per backend, known-good values, where pyOCD `target_type` values come from, how to find and install the CMSIS pack that provides one, and what `doctor`'s `target_support` statuses mean |
| `agentic-hil://reference/errors` | every `error_type` with its meaning, ordered fix, and the wrong fix; one entry at `.../errors/<error_type>[:<field-or-backend>]` |
| `agentic-hil://reference/platform-paths` | which locations pass the path trust check, how a Windows refusal names the package holding the right, why `AGENTIC_HIL_CONFIG` and a chosen `state_root` are the supported answer to a refused location |
| `agentic-hil://reference/lease-lifecycle` | which device a run locks and for how long, `device_busy` and `undeclared_device`, why a crashed run needs no recovery; lease states, the continue predicate, the recovery path |
| `agentic-hil://reference/config-schema` | the authoritative configuration JSON Schema |
| `agentic-hil://reference/config-shape` | which sections a configuration has and what each is for, which are required, a worked Nucleo-F446RE example; how to change one field-wise over MCP, which permission opens which key, and what deliberately cannot be done |

Continue after a result only when `ok` is true, `target_ok`, `audit_ok`, and `cleanup_ok` are not false, `cleanup_required` and `quarantined` are not true, `lease_state` is one of `null`, `active`, or `released` (any other value, including `stale`, blocks success), `side_effect_status` is not `unknown` or `partial`, and `hardware_state` is not `unknown`. The public `overall_success()` helper in `agentic_hil.report` encodes exactly this predicate. For quarantined hardware, stop effects and read `cleanup_reasons` plus `auto_recoverable` from `hardware_lease_status`. `auto_recoverable: true` means the running owner clears the incident itself on the next hardware call, so retry that call once: it reaps leftover debugger processes, verifies the safe state under the bench's `auto_recover_policy`, and proceeds. A refusal carrying `auto_recovery_attempted: true` means that already ran and did not confirm the safe state — do not retry it again. Otherwise ask the operator to inspect `agentic-hil lease-status`, physically confirm the current incident, and run `agentic-hil recover --confirm-safe-state --quarantine-id <id>`. If recovery returns `config_changed` (the authoritative config changed since the incident), the operator verifies the config delta and reruns with the explicit `--accept-config-change` override.

Install or update the local agent setup skill with:

```bash
agentic-hil skill-install --agent opencode
```
