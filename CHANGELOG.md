# Changelog

All notable changes to Agentic Hardware-in-the-Loop (Agentic HIL) will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning while pre-1.0 changes may still move quickly.

## [Unreleased]

### Added

- `setup` and `init` now bootstrap an attached STM32 bench when the project ships `agentic-hil.config.example.yaml`: they locate STM32CubeProgrammer, enumerate exactly one ST-Link, identify its target through a fixed HOTPLUG-only command, correlate its virtual COM port by USB serial number, and write those host-specific values into the external authoritative config. This bootstrap path exists before runtime permissions do and never exposes reset, halt, erase, flash, raw commands, or serial I/O.

### Changed

- A project hardware profile can request the probe, flash, reset, artifact, and UART permissions that the generated authoritative config needs. Raw debugger commands and mass erase remain disabled regardless of project input. Projects without the profile retain the previous deny-by-default starter config, and setup never replaces an existing operator config.
- Windows discovery now recognizes versioned STM32CubeCLT installations under `C:\ST` in addition to PATH, standalone CubeProgrammer, and CubeIDE-bundled installations.

## [0.6.0] - 2026-08-02

An unattended loop no longer stops at every quarantine. The owning process clears the failure classes it can actually verify, and the audit records what verified them.

### Added

- **The owning process recovers its own quarantines.** A blocked hardware call attempts recovery before refusing: it reaps this owner's leftover debugger processes, verifies the safe state, writes a `recovery: machine_attested` report, and then runs. Previously a quarantine could only be cleared by a person running `agentic-hil recover` — for every class of fault, including ones no person can add information to, such as an OpenOCD process that died mid-cleanup.
- **`recovery.auto_recover` policy** (`off`, `readonly`, `reset_halt`; defaults to `reset_halt`) with `recovery.max_attempts` (default 3) bounding attempts per incident. `readonly` reaps processes and re-reads the probe, which connects without resetting on every backend and therefore cannot alter the state it attests to. `reset_halt` additionally drives a reset-into-halt, which is what settles an unconfirmed flash, reset, or debug-session start. Set `readonly` if anything on the bench reacts to a target reset.
- **`lease-status` reports why, not just that.** New `cleanup_reasons`, `auto_recoverable`, and `auto_recover_policy` fields answer whether to retry or fetch an operator. `cleanup_reasons` also travels on lease-carrying tool results and into the project record, so a second process sees it. The reason previously existed only inside the on-disk marker's `errors[]`, so classifying an incident meant opening state files under the state root.
- **Machine-recovery provenance in reports:** `safe_state_predicate` (`readonly_probe` or `reset_halt`), `auto_recover_policy`, and `auto_recover_policy_source` (`config` or `default`). A config that never names the policy gets the default plus a one-time `warnings` entry the first time recovery resets its target.

### Changed

- One-shot debugger failures no longer share one undifferentiated quarantine reason. Read-only calls (`debugger_probes_list`, `probe_target`) raise `debugger_readonly_result_unconfirmed`, which a re-read settles; `flash_firmware` and `reset_target` keep `debugger_result_unconfirmed`, which needs the `reset_halt` predicate because either may have left the target running.
- A one-shot call that quarantined kept its lease registered but unreachable, so nothing in the process could ever resolve it. The lease is now retained for recovery and released once the safe state is verified.
- Under the default policy a failed `debug_start_session` load is settled by machine recovery instead of waiting for an operator. Recovery halts and never runs the target, so control is not handed back to a partially written image; flashing and running it again stays an explicit, permissioned act. Set `recovery.auto_recover: "readonly"` to keep the previous behaviour.

Excluded from machine recovery regardless of policy: a broken audit (`*_audit_broken`), an incident adopted from an owner that died, a mix of reasons, a still-active sibling lease, an incident whose resources this owner no longer fully holds, and any bench without `allow_probe`. `reset_halt` degrades to `readonly` when the bound probe lacks `allow_reset`. The `machine_attested` report is written before the state transition, so a failed audit write aborts the recovery instead of silently unblocking hardware.

### Fixed

- The CAN receive-queue drain budget bounds time spent talking to the adapter instead of also covering the `queue_clear_start` audit write that precedes it. The one-second deadline was armed before that write, so a slow first append — a cold runner creating the JSONL log while a scanner holds it — could consume the whole budget and break the loop before a single frame was read, failing `can_session_start` with `can_queue_clear_limit` and `frames_drained: 0` on a queue that was never touched.

## [0.5.0] - 2026-08-02

### Changed

- **BREAKING — permissions moved onto the device they authorize.** The top-level `permissions:` block is gone. A debug probe carries `allow_probe`, `allow_flash`, `allow_reset`, `allow_raw_debugger_commands`, and `allow_mass_erase` under `debuggers.<name>.permissions`; a serial port and a CAN bus each carry `allow_read`/`allow_write` under `com_ports.<name>.permissions` and `can_buses.<name>.permissions`. Every flag stays deny-by-default and now has exactly one owner, so enabling flash on a bring-up board cannot grant it on a second board sharing the project config. Loading a config that still declares the old block fails closed with a per-flag migration map instead of a bare unknown-field error.
- **BREAKING — every debug probe is a named `debuggers` entry.** The single top-level `debugger:` block is gone; the name is the routing key a test plan step uses. A project with exactly one probe binds it automatically and needs no name anywhere. The MCP tool surface drives that single bound probe: with several configured, debugger tools refuse with `not_supported` and point at `agentic-hil test-reactor`, which routes per step by name. Two entries that resolve to the same physical probe are rejected at load. A probe_id is required before a board is actually driven rather than at load, so the ids can still be discovered — it is the only field the backends turn into `adapter serial` / `sn=` / `--uid`, so without it two names would drive whichever probe is plugged in.
- **BREAKING — the `devices:` block is gone.** It only bound a debugger to a UART, and both are now addressed by their own names. Test-plan steps carry `debugger: <name>` for flash/debug actions and `port_id: <name>` for UART actions instead of `device: <name>`. A per-board target override moved from `devices.<name>.target` to `debuggers.<name>.target`.
- **BREAKING — the test plan format is version 2.** Version 1 steps addressed a `device:` that no longer exists. A version 1 plan is refused by name, with the key that replaced it for each action, rather than failing on a bare version mismatch.

### Fixed

- A pyOCD `probe_id` is resolved to the one full probe UID it selects before any board is driven. pyOCD matches `--uid` as a case-insensitive *substring* of a probe's unique ID and strips an optional `<type>:` prefix first, so a configured value could select a board the operator never named and two different values could select one board. Zero matches and ambiguous matches are refused before the command runs; config load itself stays hardware-free.
- Config load rejects one `probe_id` that contains another, and treats `stlink:ABC` and `ABC` as the same selector, since that is how pyOCD reads them.
- A project with several probes and no `probe_id` yet now loads, so `agentic-hil debugger-probes` can be used to discover the ids. Requiring the id at load made the documented bootstrap circular: the ids could only be learned from a config that would not load without them. The requirement moved to the point where a board is actually driven, and the refusal names the discovery command and OpenOCD's inability to enumerate.
- A firmware artifact reached through a symlink or junction that leaves the workspace is now refused by name. Containment was computed lexically, so such a path was reported as `within_workspace: true` and fell back to the misleading "must be a single-link regular file" wording — on exactly the case where naming the reason matters most.
- A debug dump into an allowed artifact root that is itself a link inside the workspace is no longer falsely refused. `validate_output_path` resolves symlinks while `artifacts.allowed_roots` are pinned lexically, so the two disagreed whenever a root was a link (pre-existing).
- A per-probe `debuggers.<name>.target` override is applied when that probe is bound, including the automatic binding of a single configured probe. It was parsed and then only honoured for probes other than the bound one, so on the supported single-probe path it was silently dropped.
- A firmware artifact outside `workspace_root` is now refused with that reason instead of "must be a single-link regular file" carrying a `backend_error` about an *output* file, and the `validation` block reports `within_workspace`. A debug dump whose `output_path` leaves the workspace is refused by the same check during reactor preflight rather than after the plan has already flashed and halted the board.
- `write_intel_hex_file` requires a `workspace=` argument and passes it to `atomic_write_text`, so the one write primitive an agent aims at a path of its own choosing carries containment at the write itself, not only in the caller's earlier directory check.
- `stage_for_backend` refuses an artifact whose content was never validated. With `validation.require_existing_file: false`, validation of a file that does not exist yet skips the size, format-plausibility and hash checks entirely; a file appearing before staging was then flashed unchecked.
- Refusals raised while staging an artifact no longer quarantine the probe lease. They happen before the backend is called, so they now carry `side_effect_committed: false` instead of being recorded as an unconfirmed hardware effect that puts a bench into recovery.
- `agentic-hil doctor` resolves and version-checks the debugger toolchain of every probe that grants `allow_probe`, reporting each result under `debuggers.<name>.check`. It previously checked only the bound probe, so a multi-probe project — where nothing is bound — reported `ok: true` with the check silently skipped and no named entry verified at all. The check proves each probe's toolchain resolves and runs; it does not open a probe or contact a target, so it cannot detect an unplugged board or a wrong `probe_id`.
- `agentic-hil debugger-probes` enumerates every probe-enabled debugger instead of refusing whenever no single probe is bound. Probe discovery is how an operator obtains the serial numbers a multi-probe config requires, so it has to work in exactly that config; each backend enumerates all attached probes anyway.
- Two debuggers naming the same `probe_id` are rejected even when their `resource_id` values differ. `resource_id` only renames the coordination lease, so it could previously mask two names pointing at one physical board.
- `classify_last_error` works without a bound probe. It reads the last recorded failure of any tool, so a UART-only or multi-probe project can ask what went wrong instead of being told the tool is unsupported.
- A COM port granted `allow_write` without `allow_read` can now actually be written to. Opening the session was gated on `allow_read` alone, and the session is the only route to the port, so the grant was unusable; the background reader and the buffer drain still require `allow_read`.
- A `run_until_breakpoint` step that omits `timeout_s` no longer fails its own contract. Execution sent an explicit `timeout_s: null` that the `debug_continue` schema rejects, while preflight had validated only the call the plan actually described.
- A flash step refused for `allow_mass_erase` says so, instead of naming the `allow_raw_debugger_commands` flag the operator can see is `false`.
- Backend `likely_causes` and error summaries name `debuggers.<name>.executable`, `.flash_address`, `.target_type` and `.probe_id` instead of the removed singleton `debugger.*` keys, so following the remediation advice no longer produces a config that cannot load.

### Removed

- **BREAKING — test adapters.** The `adapters:` config section, the seven `adapter_*` MCP tools, the adapter bridge service, and the simulated NTC example are removed until the first real adapter exists. CAN process bridges are unaffected.

## [0.4.0] - 2026-07-31

### Added

- `agentic-hil setup --agent <agent>`: one-shot project setup (authoritative config + skill install + MCP registration + doctor) in a single command. It prepares a safe external `state_root` and, before the fail-closed trust validators run, silently tightens the operator's own group/other-writable directories along the config, state, and MCP-launcher chains so setup succeeds on default umask-002 / private-group homes without hand-fixing permissions. Only user-owned components are changed — the walk stops at the first foreign-owned or symlinked ancestor, never touching shared or system directories — and every change is reported in the result's `permission_changes`.
- Per-agent USER-level MCP registration by `setup`: Codex `~/.codex/config.toml`, opencode `~/.config/opencode/opencode.json`, and Claude Code `~/.claude.json`. Each file is merged directly with secure atomic writes outside the repository to preserve the policy trust boundary; registration is idempotent and fails closed on unsafe files.
- Claude Code plugin (`plugins/agentic-hil/` plus `.claude-plugin/marketplace.json`): distributes the version-matched setup skill without persisting an unverifiable portable MCP command. The skill requires an exact persistent package install and delegates user-level MCP registration to `setup`, which records the verified absolute console-script path. The repo is also discoverable by the Vercel `skills` CLI (`npx skills add`) for cross-agent skill distribution.
- Containerized installation evaluation (`evals/install/`): a real agent CLI installs Agentic HIL from the documented guide, then a root-owned verifier judges the result with the home and workspace read-only, without network or credentials, from host-generated evidence rather than from what the agent reported. Every combination runs at least twice and disagreeing repetitions trigger further runs, because a run that passes because the agent did nothing is a real failure mode. Hardware commands are shadowed on `PATH`, so reaching for one is recorded and fails the run. `*-without-skill` cases uninstall the bundled skill between the installing session and the measured one, which is what separates its contribution from the tool descriptions and the registration.

### Changed

- The bundled skill is named `agentic-hil` and is written around routing: a request-to-tool table over the whole tool surface, the raw commands that are never a substitute, and what to do when a tool returns `permission_denied` — report the refusal, diagnose through the gate, and ask before changing a permission. `setup` removes the copy installed under the superseded name, but only a file carrying this project's own frontmatter, so a foreign skill that happens to use that name is left alone.
- MCP tool descriptions name the raw commands they replace, which is the one routing surface every session sees without a skill being discovered first, and the only one available to an agent with no skill mechanism.
- The VMware install-loop harness is retired in favour of the container evaluation. The hardware test plans, per-backend configuration templates, and MCP probe move to `evals/hil/`, together with what a containerized hardware executor needs: device passthrough and one run at a time.
- MCP registration stores only a verified absolute persistent console-script path and rejects bare PATH commands, transient runners, workspace virtual environments, unsafe symlink chains, and unsafe ownership or permissions. Stable user-owned pipx/uv-tool links remain supported after their link and resolved target chains pass validation.
- A refusal says that it is the answer. `initialize` returns server instructions, so a session knows before its first decision that hardware requests are answered by these tools; a permission refusal, an MCP entry conflict, and a skill conflict each carry a `next_step` naming whose the file is and that editing it, deleting the entry, or rerunning with `--force` is not a resolution. Measured: with wording that only named the obstacle, ten evaluation runs rewrote the operator's MCP entry by hand and reported success.
- `doctor` reports how the running package was installed. An editable installation copies nothing, so the code enforcing the hardware policy is whatever a source tree holds at the time.
- Setup restricts the agent CLI's own write tools on the policy files as its last step, rather than using a path rule that would also stop `mcp-stdio` from reading the file it must read.
- `AI_AGENT_QUICKSTART.md` leads with `agentic-hil setup`, documents the MCP registration as user-level per agent, and adds guidance for pip-less environments (Ubuntu 24.04+/PEP-668).

### Removed

- `llms.txt`. Nothing linked to it, nothing served it, and its own onward references were bare filenames an external fetcher could not resolve — so it was a third copy of the installation instructions with no reader to justify it, and its tool list had silently fallen to 15 of 36 tools.

### Fixed

- `doctor` reports the command a registration would accept — the verified persistent executable — instead of the bare name `agentic-hil`, which is the one form registration refuses because `PATH` decides it afresh every session. When no trusted executable exists yet it says so rather than printing something that would be rejected.
- A forbidden root is matched in both of its shapes. macOS reports its temporary directory as `/var/folders` while a path under it resolves to `/private/var/folders`, so a command placed there was compared against a root it could never match and passed a check meant to reject it.
- The refusal of an untrusted MCP command names the offending directory, its mode, and its owner. One bad ancestor of a shared prefix condemns every launcher below it, and a caller cannot fix a directory the message does not identify.
- The Windows environment setup no longer reports "no untrusted writers" when `Get-Acl` is unavailable; it reads the same access rules through the .NET accessor instead of turning the preflight into a silent no-op.
- The packaged skill is recognized on a Windows checkout. git converts it to CRLF there and the frontmatter is matched line by line, so the installer read its own skill as a foreign one and refused to update it.
- A world-writable coordination lock directory is refused when hardware is acquired, which is where coordination state is created since it became lazy, rather than at construction where nothing was built yet.
- Rewriting the skill registration block no longer fails on Windows. The block names the skill's absolute path and `re.sub` reads escapes in a replacement string, so a second write of a path under `C:\Users\...` died on a bad escape. Only `skill-install` followed by `setup` took that branch, which is why a plain `setup` never hit it.
- A rejected setup keeps what an earlier command installed. Rollback restores the state setup found, so a skill from a standalone `skill-install` or a config from a standalone `init` survives on purpose, and the refusal now names a skill left behind without a registration to go with it.

## [0.3.0] - 2026-07-20

### Added

- Added cross-process hardware leases, persistent crash quarantine, `lease-status`, and incident-bound operator recovery through `recover --confirm-safe-state --quarantine-id <id>`.
- Versioned process bridges at protocol v2 with mandatory physical safe-state acknowledgement before resource release.
- Added a permission-gated test reactor with configured `Device` bindings, semantic preflight, duplicate-key rejection, and YAML/JSON sequences for flashing, UART lifecycle, run-to-breakpoint, and Intel HEX symbol dumps with exception-safe cleanup.
- Added multi-board support to the test reactor: a named `debuggers` map lets each device drive an independent debug probe (`devices.<id>.debugger` selects `true` for the top-level debugger or a named entry, with an optional per-device `target` that requires a named debugger), enforcing one device per probe — validated again after executable pinning so lexically distinct executables cannot silently share one physical probe — and running each named debugger on its own service under one shared project lease. `agentic-hil doctor` now reports each device's debugger selector as `"default"`, a named-debugger name, or `null` instead of a boolean. Added `agentic-hil test-schema` to emit the bundled test-plan schema.
- Added `debugger_probes_list` and `agentic-hil debugger-probes` for permission-gated enumeration of connected probe IDs through STM32CubeProgrammer or pyOCD.

### Fixed

- Made audit/target status part of overall tool success, validated MCP and direct tool arguments against one strict contract, and blocked later effects after unknown or unaudited outcomes.
- Pinned mandatory trusted `state_root` outside the workspace and made coordinator/service lifecycles terminal across threads and stale lease references.
- Moved canonical report state into config-and-workspace-namespaced user state, stopped importing workspace snapshots, and made process/session cleanup retryable across partial failures and interrupts.
- Reworked artifact/config/log I/O around bounded, nonblocking, single-descriptor checks and rejected non-finite configuration timeouts.
- Preserved final test-reactor failure reports after successful cleanup and kept `classify_last_error` anchored to the latest failure instead of later read-only status calls.
- Created nested debug-dump output directories only during real dump execution, while keeping test-reactor preflight read-only.
- Prevented COM, CAN, and adapter sessions from leaking when audit log paths become unavailable during session start.
- Made Windows directory-chain pinning sharing-compatible so concurrent Agentic HIL operations keep their rename/delete protection, made the Windows report-state lock block like the POSIX branch, kept coordination-poisoning failures from masking primary hardware errors, expanded `~` in state-root environment overrides before use, and released private staging directories after local artifact uploads.
- Made lease release a durable, retryable transition and hardware recovery convergent: partial persistence resumes idempotently, incident markers agree on their resource set, and a config-hash change now refuses recovery with `config_changed` until the operator reruns with the explicit `--accept-config-change` override instead of dead-ending. Wrote detailed hardware-effect logs canonically under `state_root` with a monotonic sequence and tamper-evident hash chain, exposing `canonical_audit` verification on reports; latched GDB audit-write failures permanently and reconciled breakpoint cleanup against the backend; and retained provisional raw handles for retryable close after constructor and interrupt faults.

### Security

- Enforced one live owner for each project and physical probe/COM/CAN/adapter resource across MCP, CLI, pytest, reactor, and direct Python service entry points; stale owners now require explicit safe-state recovery.
- Normalized symlink/reparse secure-I/O failures to structured `unsafe_configured_path` errors and avoided wrapping failed report reads as successful `get_last_report` calls.
- Rejected process bridge `args` entirely and kept process bridges pinned to operator-controlled executables.
- Bounded in-memory path locks to active operations and hardened child-process cleanup/decoding for debugger and bridge subprocesses.
- Replaced the project-local/two-file configuration model with one deny-by-default authoritative config outside the repository, discovered from the project root with an optional absolute-path `AGENTIC_HIL_CONFIG` override and bound to the project by mandatory `workspace_root`.
- Added explicit gates for debugger execution and target reset (`allow_reset`), deny-by-default config generation, startup pinning for debugger/GDB/bridge executables and OpenOCD scripts, deny-all empty symbol allowlists, and symlink-safe artifact/output paths.
- Revalidated firmware artifacts into private process staging before backend use, rejected multiply-linked inputs/outputs, switched structured writes to atomic replacement, disabled GDB auto-loading, and moved trusted subprocess working directories outside the workspace.
- Revalidated the trusted `state_root` and every derived coordination/audit/report directory for owner, type, and write mode on each open (rejecting sticky world-writable roots and foreign-owned subdirectories), and stopped emitting environment-derived absolute state-root/config paths and any secret-named field to operator/CLI/MCP output sinks.

## [0.2.3] - 2026-07-14

### Changed

- Updated canonical repository URLs after the transfer to `agentic-hil/agentic-hil`.
- Added official MCP Registry metadata, validation, and OIDC publishing after each successful PyPI release while retaining the host-independent local setup path.

## [0.2.2] - 2026-07-14

### Changed

- Canonicalized the Python distribution metadata spelling and all installation guidance to `agentic-hil`; Python imports, the pytest plugin, and the fixture remain `agentic_hil`.
- Renamed the CLI, MCP server, policy directory, package modules, examples, and documentation from the historical software name to Agentic HIL.
- Clarified that HardCI adapters are the reference hardware for Agentic HIL.

## [0.2.1] - 2026-07-09

### Changed

- Python package metadata and install guidance were prepared for the Agentic HIL migration; the CLI command, MCP server name, and repository URL use `agentic-hil`.
- Public project URLs, setup docs, issue templates, and release guidance were prepared for the canonical Agentic HIL repository.
- CLI setup hints and Codex skill-registration text use Agentic HIL naming instead of the legacy project command/prose.
- CI, CodeQL, and Scorecards workflows now run for the `master` default branch.

## [0.2.0] - 2026-07-06

### Added

- Debug sessions now classify abnormal target stops: unexpected breakpoints return `unexpected_breakpoint`, Cortex-M exception/fault contexts return `target_exception` with frame, signal, and suggested next actions, and session status picks up asynchronous stop records.
- Typed GDB/MI debug sessions for the OpenOCD backend: the eleven `debug_*` tools now run real sessions (start/stop/status, breakpoints by symbol or file:line, continue/halt with structured stop reasons, symbol resolution, Intel-HEX memory dumps) against OpenOCD's gdbserver, gated by `debug.allowed_symbols`, `debug.max_dump_size_bytes`, and the existing permission model (raw-command mode disables typed debugging; `load` mode requires flash permission). The session's gdbserver is pinned to `localhost` on an ephemeral port and torn down with the session.
- pyOCD debugger backend (`debugger.type: "pyocd"`) with probe/flash/reset support, `target_type` selection, and pyOCD-specific error classification.

### Changed

- Firmware flashing no longer resets the target by default. Pass `reset_after_flash: true` to `flash_firmware` to request the previous post-flash reset behavior.
- Removed the separate `permissions.allow_reset` policy field; reset remains a typed debugger action instead of a separately gated permission class.

### Fixed

- Hardened the MCP transport and sessions: bounded JSON-RPC message size, COM sessions recover after device disconnects, `com-stdio` relays board output while stdin is idle, artifact validation streams instead of loading whole files, and host serial-port discovery is gated by `allow_com_read`.

## [0.1.0] - 2026-07-05

First public release on PyPI.

### Added

- MCP stdio server exposing bounded tools for probing, flashing, resetting, artifact validation, serial and CAN stimuli/feedback, structured reports, and error classification, gated by a project-local `.agentic-hil/config.yaml` policy.
- Test-adapter layer for sensor/actuator/fault simulation with channel/fault allowlists, seven adapter MCP tools, a JSON-over-stdio bridge protocol, and an NTC simulator example.
- OpenOCD and STM32CubeProgrammer CLI (`stlink`) debugger backends with success-marker confirmation, structured error classification, and per-action logs.
- pytest plugin: installing `agentic_hil` registers the session-scoped `agentic_hil` fixture that drives the same policy-gated tools in CI regression suites, with per-test stimulus-session cleanup, rootdir-anchored config resolution, and skip-when-absent / fail-when-invalid config semantics.
- CLI commands: `init`, `doctor`, `com-ports`, `mcp-stdio`, `com-stdio`, `schema`, `mcp-config`, and `skill-install` for opencode, Claude Code, and Codex.
- Agent-first, no-admin installation flow: `AI_AGENT_QUICKSTART.md`, `llms.txt`, and `TROUBLESHOOTING.md`, built around `agentic_hil` package installs, the `agentic-hil` CLI command, and a repository-URL fallback.
- Nucleo-F446RE demo firmware (`examples/nucleo-f446re_demo/`) exercising the complete loop on real hardware: build → flash → reset → assert on the UART boot banner.
- PyPI trusted publishing with a release-tag/package-version guard and digital attestations; CI matrix across Linux/macOS/Windows and Python 3.10–3.13 with ruff linting.
