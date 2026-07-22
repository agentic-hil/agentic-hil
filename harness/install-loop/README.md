# Install-prompt loop (first test)

Fire the canonical install prompt at each agent, watch it follow
`AI_AGENT_QUICKSTART.md`, then improve the install instructions where it stumbles.
Repeat until all three install cleanly. This is a **doc-optimization loop**, not a
pass/fail gate. No hardware is involved. Run it only inside the disposable VM
described in `VM-SETUP.md`.

## Branch-targeted

The runner requires `BRANCH` explicitly. The prompt installs that remote ref's
code and reads the same ref's `AI_AGENT_QUICKSTART.md`. That is what makes the
loop work: edit the docs, push the selected ref, and the next run sees them.
For example, while reviewing this feature branch:

```bash
export BRANCH=feature/smooth-installation
git push -u origin "$BRANCH"
```

Override the repository with `REPO=...` when needed.

## The loop

```
1. export BRANCH=<remote-ref> and the agent's auth (see below)
2. bash run-install-prompt.sh <agent>        # runs in a throwaway HOME
3. read transcripts/install-<agent>-*.log    # where did it get stuck / guess?
4. edit AI_AGENT_QUICKSTART.md (+ AGENTS.md, README.md)   # the artifact being optimized
5. git commit + push to the selected remote ref   # so the agent fetches the new docs
6. go to 2
```

Each run gets fresh HOME, XDG config/state/data/cache, pip/pipx/uv roots, and a
sanitized PATH. The selected agent executable is resolved first and invoked by
absolute path; an existing user-installed `agentic-hil` and real user config are
therefore unavailable. No reset is needed between runs.

## Auth (env, because HOME is redirected)

| Agent | Command run | Auth env |
|-------|-------------|----------|
| claude | `claude -p "<prompt>" --dangerously-skip-permissions` | `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`) |
| codex | `codex exec --dangerously-bypass-approvals-and-sandbox "<prompt>"` | `CODEX_API_KEY` |
| opencode | `opencode run --model $OPENCODE_MODEL "<prompt>"` | provider key for the model (e.g. `ANTHROPIC_API_KEY`) |

The `--dangerously-*` / bypass flags let the agent write files and use the
network. A redirected HOME is not a security sandbox, so run this only inside
the disposable VM.

## What "installs cleanly" means

The transcript should show the agent, with no human help:
- install agentic-hil user-local (no sudo, no `--break-system-packages`),
- run `agentic-hil setup --agent <this agent>`, which installs the skill,
  creates the external deny-by-default config, registers the user-level MCP
  server command, and runs `doctor`.

Signs the docs need work: the agent guesses a wrong command, uses sudo, tries to
clone the repo into the project, or gets stuck on PATH / PEP-668 / the runner
(uv/pipx) bootstrap.
