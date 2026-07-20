# Install-prompt loop (first test)

Fire the canonical install prompt at each agent, watch it follow
`AI_AGENT_QUICKSTART.md`, then improve the install instructions where it stumbles.
Repeat until all three install cleanly. This is a **doc-optimization loop**, not a
pass/fail gate. No hardware, no VM.

## Branch-targeted
The prompt points at the **`feature/smooth-installation` branch**, not master, so the agent
installs the branch's code and reads the branch's `AI_AGENT_QUICKSTART.md`. That
is what makes the loop work: edit the docs, push, and the next run sees them.
The branch must exist on the remote (`git push -u origin feature/smooth-installation`).
Override with `REPO=... BRANCH=...` in the environment.

## The loop
```
1. export the agent's auth (see below)
2. bash run-install-prompt.sh <agent>        # runs in a throwaway HOME
3. read transcripts/install-<agent>-*.log    # where did it get stuck / guess?
4. edit AI_AGENT_QUICKSTART.md (+ AGENTS.md, README.md)   # the artifact being optimized
5. git commit + push to the feature/smooth-installation branch   # so the agent fetches the new docs
6. go to 2
```
Each run gets a fresh `$HOME`, so agentic-hil installs "from nothing" every time
and your real machine is untouched — no reset between runs.

## Auth (env, because HOME is redirected)
| Agent | Command run | Auth env |
|-------|-------------|----------|
| claude | `claude -p "<prompt>" --dangerously-skip-permissions` | `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`) |
| codex | `codex exec --dangerously-bypass-approvals-and-sandbox "<prompt>"` | `CODEX_API_KEY` |
| opencode | `opencode run --model $OPENCODE_MODEL "<prompt>"` | provider key for the model (e.g. `ANTHROPIC_API_KEY`) |

The `--dangerously-*` / bypass flags are needed so the agent may actually run the
install (write files, network); safe here because the run is in a throwaway HOME.

## What "installs cleanly" means
The transcript should show the agent, with no human help:
- install agentic-hil user-local (no sudo, no `--break-system-packages`),
- `agentic-hil skill-install --agent <this agent>`,
- `agentic-hil init` / `doctor` / `mcp-config` for the project.

Signs the docs need work: the agent guesses a wrong command, uses sudo, tries to
clone the repo into the project, or gets stuck on PATH / PEP-668 / the runner
(uv/pipx) bootstrap.
