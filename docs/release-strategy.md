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

PyPI first. Publishing runs through GitHub Actions trusted publishing with OIDC (`.github/workflows/workflow.yml`): no long-lived PyPI API tokens. The synchronized package, registry, marketplace, plugin, changelog, install-eval, troubleshooting, and bundled-skill contracts are already settled before the merge by `tools/check_version_consistency.py` in CI; the release job runs the same module again with `--release-tag`, which is the one comparison a pull request cannot make. It then builds sdist and wheel and validates them with twine.

After PyPI accepts a release, the same workflow verifies the package's `mcp-name` ownership marker and publishes `server.json` to the preview MCP Registry through GitHub Actions OIDC. No MCP Registry secret is stored. The release tag, Python package version, top-level server version, and package version in `server.json` must match exactly. The registry is an additional discovery channel; the documented local CLI and MCP configuration path remains authoritative and host-independent.

If MCP Registry publication fails after PyPI succeeds, re-run only the failed job or manually dispatch the release workflow from the protected default branch. Manual dispatch from any other ref is skipped; the valid recovery path skips PyPI and republishes only the already released, synchronized registry metadata.

Naming is part of the release contract: the Python distribution/install target, CLI command, repository URL, and MCP server name use `agentic-hil`. Python imports, pytest plugin names, fixtures, and Python examples use `agentic_hil`.

Later packaging candidates are Homebrew, Scoop or WinGet, and conda-forge; add them only when they are reproducible and built by CI.

## Release Checklist

Before creating a release:

```text
1. Bump the version in every position `python tools/check_version_consistency.py --list` prints, then run `python tools/check_version_consistency.py` until it is silent.
2. Run ruff check src tests evals tools and pytest.
3. Run python -m build (or uv build) and inspect the packaged files.
4. Open a pull request and let Required CI pass; the same check runs there, so a forgotten position is red before a release exists.
5. Create a GitHub Release with a strict SemVer vX.Y.Z tag that exactly matches pyproject.toml.
6. Let the publish workflow re-run the same check with the release tag before it builds, checks, and publishes to PyPI.
7. Let the workflow verify the PyPI ownership marker and publish the matching `server.json` through GitHub OIDC.
8. Verify: uvx --from agentic-hil agentic-hil --version resolves the new version from PyPI.
9. Verify the release appears as `io.github.agentic-hil/agentic-hil` in the MCP Registry API.
10. Start from GitHub auto-generated release notes, then edit for clarity.
```

## What the Release Gate Checks, and When

Step 1 deliberately names no files. An enumeration kept by hand in this document
drifted: it listed seven positions while the release carried a version in twelve
files, and the two install-eval matrices it omitted sat at a stale version for
two releases, aborting every run before it started. The list now lives in
`tools/check_version_consistency.py`, which is the check that enforces it, so
this document cannot fall behind it. `--list` prints every position and what
carries the version there; a file that starts carrying the version and is named
in neither the check nor its declared exceptions is itself an error.

That check is the single enforcement point for version agreement. It reads files
and compares strings (no secret, no OIDC, no tag), so it runs pre-merge as the
`Release metadata consistency` job of `.github/workflows/ci.yml`, on every push
and every pull request, with no `paths:` filter: the failure it exists to catch
is a change that *forgets* a file, and a paths filter keys on the files a pull
request touched. `Required CI` depends on it, so a version that does not agree
with itself cannot reach `master`.

Three things still cannot be established before the merge, and no rearrangement
of the workflow changes that:

```text
release tag vs pyproject.toml   the tag does not exist until the release is created;
                                checked at release with --release-tag, and again by
                                publish-mcp-registry before it publishes server.json
PyPI mcp-name ownership marker  needs the artifact PyPI has accepted
MCP Registry acceptance         needs the published PyPI release to verify against
```

Everything else the release job used to discover for the first time at
`release: published` (every position `--list` prints, the three JSON manifests
byte for byte against their contracts, and the sweep that refuses a file
carrying the version that no check covers) is now settled before the merge.
That matters because publishing is the point of no return: a PyPI version
cannot be re-uploaded.

## Repository Protection

Keep `master` protected with required status checks (`Required CI`). Pull requests from agent-driven development are reviewed and approved by the repository owner, so a required approval count of 1 works even with a single human maintainer. Block force pushes and branch deletion. Dismiss stale approvals when new commits are pushed.
