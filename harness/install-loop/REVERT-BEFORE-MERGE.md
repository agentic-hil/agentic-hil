# Pre-merge install-loop gate

There are no temporary product-document or package-source edits to revert. The
install-loop runner requires an explicit `BRANCH`, so a feature ref is selected
only by the operator when invoking the harness.

Before merging, verify that no temporary branch pin escaped into shipped product
documentation or metadata:

```bash
rg -n --glob '!harness/**' \
  'TEMP feature/smooth-installation|@feature/smooth-installation' \
  AI_AGENT_QUICKSTART.md README.md docs plugins .claude-plugin server.json
```

The expected result is no matches. Harness examples may still demonstrate an
explicit `BRANCH=feature/smooth-installation` override.

Do not restore a bare `uvx` MCP command or a plugin `.mcp.json`. The persistent
installed command and user-level registration are permanent trust-boundary
requirements, not temporary install-loop scaffolding.
