# Docker registration gates

Run from the repository with a Linux Docker engine:

```console
python tools/test_agent_registration.py
```

Both mandatory stages use the real Codex and Claude Code CLIs from the existing
npm lock, ordinary container users, fresh homes, no credentials, no host mounts
and no hardware. Docker builds have network access. Runtime networking differs
between the two stages:

- **Script installation (network enabled):** the container starts without
  Agentic HIL, uv, pipx, prepared wheels or a package cache. It runs the current
  checkout's unmodified `install.sh`, which bootstraps uv and downloads the
  published Agentic HIL package and its default `[can]` extra from PyPI. Four
  cases cover `sh install.sh --agent codex`, `sh install.sh --agent claude-code`
  and automatic agent detection without `--agent`, checked for both clients.
  The harness then checks what the script registered; it never invokes
  `agent-install` or `setup` to repair a missing script registration. The report
  records the installed release and the exact installer SHA-256 and rejects a
  script that differs from the checkout. Network/download failures fail the gate.
- **Current wheel (network disabled):** an image builds the **current working
  tree**, including uncommitted changes, into a wheel. Each case installs it
  through pipx from the prepared wheel directory, then checks the current
  implementation. This stage covers changes not yet available from PyPI.

Both stages must pass: **20 cases total**, with no switch to omit either stage.
The wheel stage requires all eight cases for each agent:

| Case | Required evidence |
| --- | --- |
| Fresh installation | `agent-install` succeeds; actual CLI `mcp get` and `mcp list` find the entry in two projects |
| Existing settings and repeat | Other settings and MCP entries survive; running the installer twice changes no registered files |
| First project setup | `setup` succeeds without attached hardware and retains the user integration |
| Refused project configuration | `setup` fails, reports separate scope results, and keeps the working user integration |
| Invalid client configuration | Nonzero exit, exact refusal, unchanged original bytes, no partial skill |
| Operator-owned conflicting entry | Nonzero exit, unchanged operator entry and complete rollback |
| Missing registration control | After a successful install, removing the entry makes verification fail |
| Broken launcher control | A still-present launcher that exits zero without serving MCP makes verification fail |

Positive cases verify the installed skill against the distribution's bundled
contents and check the user-level configuration. Development packages may carry
the last released skill version; the installed skill must still match the
distribution exactly.
Codex must report an enabled entry; Claude must report a **connected user-scope**
entry. The exact registered command must also answer an MCP initialization,
the complete tool contract with annotations, and a
`project_config_describe` call. A second, unconfigured project must expose MCP
and answer `config_file_not_found`, proving registration is independent of
project setup. The wheel stage uses the committed `tools.list.expected`; the
script stage checks against the installed published distribution's declaration
and requires the core configuration, debugger and flashing tools. A future
development tool therefore need not already exist on PyPI for the script to pass.

Docker absence, build/startup failures, timeouts, missing/duplicate reports,
missing cases and skipped cases all fail. `report.json`, `script-build.log`,
`script-container.log`, `wheel-build.log` and `wheel-container.log` are written
under `evals/install/artifacts/registration-gate/` (override with `--output`). The
script log includes install.sh's actual transcript. A failed rerun replaces any
old passing report.

The **Agent registration (Docker)** job runs on every CI push/PR without a path
filter and feeds **Required CI**. Skipped, cancelled and failed registration jobs
make that aggregate fail. Repository branch protection must require **Required
CI** to prevent merges; workflow code alone cannot enable branch protection.

This gate tests Linux CLI integration without model calls. It does not prove
that an already running desktop session reloads its configuration, that a model
chooses a tool, or that Windows-specific paths behave identically. The existing
model-driven installation evaluation covers agent behavior separately.
