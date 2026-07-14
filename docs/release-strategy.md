# Release Strategy

Agentic Hardware-in-the-Loop (Agentic HIL) publishes through PyPI and GitHub Releases. GitHub Releases trigger publishing and carry release notes; PyPI is the canonical installation channel (`pip install agentic-hil`, `uv tool install agentic-hil`, `pipx install agentic-hil`).

Do not cut the next release for metadata-only or README-only cleanup. Batch hygiene work into the next release that delivers visible user value.

Use small releases while the project stabilizes, but only when each release has a clear user-facing reason. After the early releases, move to monthly or bi-monthly SemVer releases with GitHub auto-generated release notes as the starting point.

## Versioning

Use SemVer for user-visible behavior:

```text
patch  docs, metadata, packaging hygiene, compatible bug fixes
minor  new MCP tools, new supported workflows, compatible config additions
major  breaking CLI, config, MCP, or report schema changes
```

Keep releases small enough that each one has a clear theme and an obvious rollback path.

## Release Notes

Each GitHub Release should include:

```text
what changed
how to install or upgrade
validated workflows
known limitations
links to relevant docs
```

## Distribution Channels

PyPI first. Publishing runs through GitHub Actions trusted publishing with OIDC (`.github/workflows/workflow.yml`) — no long-lived PyPI API tokens. The workflow builds sdist and wheel, validates them with twine, and refuses releases whose tag does not match the `pyproject.toml` version.

After PyPI accepts a release, the same workflow verifies the package's `mcp-name` ownership marker and publishes `server.json` to the preview MCP Registry through GitHub Actions OIDC. No MCP Registry secret is stored. The release tag, Python package version, top-level server version, and package version in `server.json` must match exactly. The registry is an additional discovery channel; the documented local CLI and MCP configuration path remains authoritative and host-independent.

If MCP Registry publication fails after PyPI succeeds, re-run only the failed job or manually dispatch the release workflow from the protected default branch. Manual dispatch from any other ref is skipped; the valid recovery path skips PyPI and republishes only the already released, synchronized registry metadata.

Naming is part of the release contract: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

Later packaging candidates are Homebrew, Scoop or WinGet, and conda-forge — add them only when they are reproducible and built by CI.

## Release Checklist

Before creating a release:

```text
1. Update pyproject.toml, `src/agentic_hil/__init__.py`, the bundled skill, `server.json`, and CHANGELOG.md together.
2. Run ruff check src tests examples and pytest.
3. Run python -m build (or uv build) and inspect the packaged files.
4. Merge to master and let the CI matrix pass.
5. Create a GitHub Release with a strict SemVer vX.Y.Z tag that exactly matches pyproject.toml.
6. Let the publish workflow validate the tag, build, check, and publish to PyPI.
7. Let the workflow verify the PyPI ownership marker and publish the matching `server.json` through GitHub OIDC.
8. Verify: uvx --from agentic-hil agentic-hil --version resolves the new version from PyPI.
9. Verify the release appears as `io.github.agentic-hil/agentic-hil` in the MCP Registry API.
10. Start from GitHub auto-generated release notes, then edit for clarity.
```

## Repository Protection

Keep `master` protected with required status checks (`Required CI`). Pull requests from agent-driven development are reviewed and approved by the repository owner, so a required approval count of 1 works even with a single human maintainer. Block force pushes and branch deletion. Dismiss stale approvals when new commits are pushed.
