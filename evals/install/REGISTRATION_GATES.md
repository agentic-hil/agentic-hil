# Docker registration gates

Run from the repository with a Linux Docker engine:

```console
python tools/test_agent_registration.py
```

The image builds a wheel from the **current working tree**, including uncommitted
changes, and uses the real Codex and Claude Code CLIs from the existing npm lock.
Each case starts with a fresh home and installs that wheel through pipx. Tests
run as an ordinary user with networking disabled, no credentials, no host mounts
and no hardware. Only the image build needs network access.

Both agents must pass all eight cases:

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

Positive cases verify the installed skill against the wheel's bundled contents
and check the user-level configuration. Development packages may carry the last
released skill version; the installed skill must still match the wheel exactly.
Codex must report an enabled entry; Claude must report a **connected user-scope**
entry. The exact registered command must also answer an MCP initialization,
the complete committed `tools.list.expected` contract with annotations, and a
`project_config_describe` call. A second, unconfigured project must expose MCP
and answer `config_file_not_found`, proving registration is independent of
project setup.

Docker absence, build/startup failures, timeouts, missing/duplicate reports,
missing cases and skipped cases all fail. `report.json`, `build.log` and
`container.log` are written under `evals/install/artifacts/registration-gate/`
(override with `--output`). A failed rerun replaces any old passing report.

The **Agent registration (Docker)** job runs on every CI push/PR without a path
filter and feeds **Required CI**. Skipped, cancelled and failed registration jobs
make that aggregate fail. Repository branch protection must require **Required
CI** to prevent merges; workflow code alone cannot enable branch protection.

This gate tests Linux CLI integration without model calls. It does not prove
that an already running desktop session reloads its configuration, that a model
chooses a tool, or that Windows-specific paths behave identically. The existing
model-driven installation evaluation covers agent behavior separately.
