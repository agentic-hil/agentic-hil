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

## Easiest revert
The doc edits are isolated in the commit tagged `TEMP:` on this branch:
```
git log --oneline --grep TEMP
git revert <temp-commit-sha>
# or restore the two files from master:
git checkout master -- AI_AGENT_QUICKSTART.md README.md
```

## Not shipped (no revert needed on master)
`harness/install-loop/run-install-prompt.sh` (its `BRANCH` default) and the
harness READMEs live only under `harness/` and never reach end users.
