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

A cycle carries two versions. The release commit sets the final number, and the first commit after it moves the distribution version to the next patch with a `.dev0` suffix, so a released `1.2.3` becomes `1.2.4.dev0` on the next commit. Only the two positions that identify the built artifact move with it: `pyproject.toml` and `src/agentic_hil/__init__.py`. Every other position `python tools/check_version_consistency.py --list` prints states a floor, an install pin or a published manifest, so it keeps naming the release a reader can actually install. Without the suffix, `src/agentic_hil` moves for a whole cycle while the version string stands still and a working tree becomes indistinguishable from the release it shadows: an install from it reports the released number, `agentic-hil upgrade` calls it `already_current`, and the install eval refuses to run a local matrix at all rather than report this tree as that release. The gate holds both shapes. On a release commit the two versions are one string and every check is the one it always was; between releases each position is compared against the version it is supposed to carry, `--release-tag` is refused outright because a tag never carries a suffix, and a tree whose package has moved past its release without saying so is refused where the release tag is present to prove it.

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
2. Refresh the pinned Astral uv bootstrap in both installers, as described under "The uv Installer Pin" below.
3. Run ruff check src tests evals tools and pytest.
4. Run python -m build (or uv build) and inspect the packaged files.
5. Open a pull request and let Required CI pass; the same check runs there, so a forgotten position is red before a release exists.
6. Create a GitHub Release with a strict SemVer vX.Y.Z tag that exactly matches pyproject.toml.
7. Let the publish workflow re-run the same check with the release tag before it builds, checks, and publishes to PyPI.
8. Let the workflow verify the PyPI ownership marker and publish the matching `server.json` through GitHub OIDC.
9. Verify: uvx --from agentic-hil agentic-hil --version resolves the new version from PyPI.
10. Verify the release appears as `io.github.agentic-hil/agentic-hil` in the MCP Registry API.
11. Attach the one-line installers *and* their checksums to the release, all four taken from the tagged commit: `install.sh`, `install.ps1`, and `sha256sum install.sh > install.sh.sha256` and `sha256sum install.ps1 > install.ps1.sha256`, uploaded under those names, in that format, because the verify-first path in docs/installation.md feeds them straight to `sha256sum -c`. The scripts belong there because the checksum can only speak for the file published beside it: the default branch moves between releases, so a recipe that pairs a release checksum with a default-branch script fails on the first fix that lands after a release.
12. Start from GitHub auto-generated release notes, then edit for clarity.
13. Move the tree to the next development version in pyproject.toml and src/agentic_hil/__init__.py, in the first commit after the release. Every other position keeps naming the release just published.
```

## The uv Installer Pin

Both one-line installers can bootstrap Astral's `uv` on a machine that has
neither `uv` nor a new-enough Python. They do not fetch that bootstrap from the
moving `https://astral.sh/uv/install.sh`, which serves whatever is current at
the second it is asked. Each script names one uv release in its URL, carries the
SHA-256 of exactly those bytes as a constant, and refuses to execute a download
that is not those bytes. `install.sh` holds `UV_INSTALLER_VERSION` and
`UV_INSTALLER_SHA256`; `install.ps1` holds `$UvInstallerVersion` and
`$UvInstallerSha256`. All four move together, and the two scripts pin the same
uv release; a static test refuses a tree where they disagree.

Refreshing the pin is a release chore and not an install-time one. An installer
that went looking for a newer uv on the operator's machine would be back to
executing bytes nobody here has read, which is the whole thing the pin exists to
stop. So the pin ages deliberately between releases, and a release is where it
is brought forward, by a person who can look at what changed:

```text
1. Read https://github.com/astral-sh/uv/releases and take the current stable tag.
2. Download both installers at that version and hash them:
     curl -LsSf -o uv-install.sh  https://astral.sh/uv/<version>/install.sh
     curl -LsSf -o uv-install.ps1 https://astral.sh/uv/<version>/install.ps1
     sha256sum uv-install.sh uv-install.ps1
3. Put the version and the two digests into install.sh and install.ps1. Never one
   without the other: a version without its digest fails every install, which is
   the intended failure mode and not a thing to work around.
4. Run pytest tests/test_install_scripts.py, which checks the URL shape, the
   presence of the digest check in both scripts, and that both name the same uv.
```

A stale pin is safe, not broken: it installs an older uv, which then upgrades
itself or is upgraded by the operator. The failure worth designing for is the
other one, so a digest mismatch stops the install outright and prints the
expected digest, the digest found, and the sentence that the pin may be stale.

The pin covers the installer, not the uv binaries that installer goes on to
fetch. Astral's POSIX installer carries a SHA-256 per release artifact and
verifies the archive it downloaded, which is the second layer and the hop our
pin cannot reach. Their PowerShell installer, as of the pinned release, has no
checksum step at all, so on Windows our pin is the only integrity check between
`astral.sh` and an executed script. If that ever changes on their side, the
comment in `install.ps1` that says so is what needs correcting with it.

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

One check reads git rather than files: the rule that a tree whose
`src/agentic_hil` has moved past its release must carry a development version
needs the release tag to compare against. The pre-merge job therefore checks out
full history, and where the tag is genuinely absent (a fork, a shallow clone, an
unpacked sdist) the check reports that it could not establish this and does not
fail.

## Repository Protection

Keep `master` protected with required status checks (`Required CI`). Pull requests from agent-driven development are reviewed and approved by the repository owner, so a required approval count of 1 works even with a single human maintainer. Block force pushes and branch deletion. Dismiss stale approvals when new commits are pushed.
