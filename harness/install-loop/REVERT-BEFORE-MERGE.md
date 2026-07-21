# Revert before merging feature/smooth-installation → master

This branch carries TEMPORARY doc edits that force agents to install from THIS
branch instead of PyPI/master, so the install-prompt loop actually exercises the
branch. Undo ALL of them before merging to master.

## Temporary changes to restore
1. **AI_AGENT_QUICKSTART.md** — the PyPI fast path was removed; every install
   command is pinned to
   `git+https://github.com/agentic-hil/agentic-hil@feature/smooth-installation`.
   Restore the PyPI steps (pip/uvx/pipx/`uv tool` with the bare `agentic-hil`
   package name) and the plain repo URL. Marked with an HTML comment
   `<!-- TEMP feature/smooth-installation ... -->`.
2. **README.md** — same: PyPI install commands replaced with the branch git
   source (`pip install`, `uvx --from`, `uv tool install`).

Nothing on master should contain `@feature/smooth-installation` or
`TEMP feature/smooth-installation` after the revert.

## KEEP — do NOT revert (permanent improvements on this branch)
These are real fixes/features, not TEMP scaffolding. They must survive the merge:
- `agentic-hil setup` one-shot command (`src/agentic_hil/cli.py`, `config.py`, tests) — feat commit.
- `.mcp.json` resolvable-command fix (`src/agentic_hil/cli.py`) — fix commit.
- The **"do not reach for a bare `pip install`"** guidance block in
  `AI_AGENT_QUICKSTART.md` (steers agents to uv; avoids the pip-missing dead end).
- The `agentic-hil setup --agent <agent>` "Configure Each Project" section.

## Revert method (surgical — NOT a file restore)
`git checkout master -- AI_AGENT_QUICKSTART.md` would ALSO wipe the keepers above.
Instead revert only the package-source swap:
```
git log --oneline --grep TEMP
git revert <temp-commit-sha>     # then re-apply any keeper hunks it undoes, or
# manually: replace every `git+https://…/agentic-hil@feature/smooth-installation`
#           with the PyPI form (`agentic-hil` name / plain repo URL),
#           restore the PyPI fast-path steps, remove the `TEMP …` HTML comment.
```
Verify: no `@feature/smooth-installation` or `TEMP feature/smooth-installation`
remains, AND the setup section + pip-guidance block are still present.

## Not shipped (no revert needed on master)
`harness/install-loop/run-install-prompt.sh` (its `BRANCH` default) and the
harness READMEs live only under `harness/` and never reach end users.
