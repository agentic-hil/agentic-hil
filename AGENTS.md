# Agentic HIL Agent Instructions

Agentic HIL is the hardware gate. It discovers the project's authoritative configuration outside the repository; `AGENTIC_HIL_CONFIG` may override that location with an absolute path. Mandatory `workspace_root` binds either file to the current project, and mandatory `state_root` pins trusted report and hardware-lease state outside the workspace.

For installation and first-time setup, follow [AI_AGENT_QUICKSTART.md](AI_AGENT_QUICKSTART.md) — everything installs user-local without admin rights.

Setting up has two scopes. `agentic-hil agent-install --agent <agent>` installs the agent skill and the user-level MCP registration once per machine and agent, needs no workspace and no configuration, and finishes even where the configuration location is refused. `agentic-hil init [--agent <agent>]` binds one project: it writes that workspace's authoritative configuration and verifies it with `doctor`. `agentic-hil setup --agent <agent>` runs both, so a first run stays one command; a second project on the same machine needs `init` alone. Neither half rolls back the other, so a `setup` that reports `ok: false` with `scopes.machine.ok` true has left a working agent behind — read that field before concluding nothing was installed.

Names: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

Use Agentic HIL MCP tools for hardware actions. Do not bypass them with raw OpenOCD commands, arbitrary debugger shells, direct serial-device access, or direct CAN-adapter access when an Agentic HIL tool is available.

If an Agentic HIL tool returns `permission_denied`, stop and ask the operator to review the authoritative config. Do not bypass it or replace the operator-controlled environment or MCP host registration.

Read facts about the server from its MCP resources, not from its source or its installed package. A failing result already carries `remediation` and `do_not` when a fix is known; `resources/list` carries the rest.

| Resource | Answers |
|---|---|
| `agentic-hil://reference/debugger-backends` | which `debuggers.<name>` field each of `openocd`, `stlink`, `pyocd` requires, discovers, or ignores; when `probe_id` and `flash_address` become mandatory |
| `agentic-hil://reference/target-support` | which field names the target per backend, known-good values, where pyOCD `target_type` values come from |
| `agentic-hil://reference/errors` | every `error_type` with its meaning, ordered fix, and the wrong fix; one entry at `.../errors/<error_type>[:<field-or-backend>]` |
| `agentic-hil://reference/platform-paths` | which locations pass the path trust check, and where the configuration and `state_root` belong |
| `agentic-hil://reference/lease-lifecycle` | lease states, the continue predicate, the recovery path |
| `agentic-hil://reference/config-schema` | the authoritative configuration JSON Schema |

Continue after a result only when `ok` is true, `target_ok`, `audit_ok`, and `cleanup_ok` are not false, `cleanup_required` and `quarantined` are not true, `lease_state` is one of `null`, `active`, or `released` (any other value, including `stale`, blocks success), `side_effect_status` is not `unknown` or `partial`, and `hardware_state` is not `unknown`. The public `overall_success()` helper in `agentic_hil.report` encodes exactly this predicate. For quarantined hardware, stop effects and read `cleanup_reasons` plus `auto_recoverable` from `hardware_lease_status`. `auto_recoverable: true` means the running owner clears the incident itself on the next hardware call, so retry that call once: it reaps leftover debugger processes, verifies the safe state under the bench's `auto_recover_policy`, and proceeds. A refusal carrying `auto_recovery_attempted: true` means that already ran and did not confirm the safe state — do not retry it again. Otherwise ask the operator to inspect `agentic-hil lease-status`, physically confirm the current incident, and run `agentic-hil recover --confirm-safe-state --quarantine-id <id>`. If recovery returns `config_changed` (the authoritative config changed since the incident), the operator verifies the config delta and reruns with the explicit `--accept-config-change` override.

Install or update the local agent setup skill with:

```bash
agentic-hil skill-install --agent opencode
```
