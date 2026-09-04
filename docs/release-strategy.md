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

A cycle carries three version roles. The release commit sets the final number, and the first commit after it moves the distribution version to the next patch with a `.dev0` suffix, so a released `1.2.3` becomes `1.2.4.dev0` on the next commit. Two positions identify the built artifact and move with it: `pyproject.toml` and `src/agentic_hil/__init__.py`. Three more *anticipate* it -- the CI-example pins in `examples/ci/*.yml` and `docs/ci-examples.md` -- and move with it too, because they carry the release this tree builds toward, which is the distribution version without its `.devN`. They anticipate rather than name the last release because they demonstrate `check-plan` and `run-evidence`, commands a release adds: pinned to the previous release they would name a distribution that rejects those commands at argument parsing, so they name the release that first ships them, which the index carries once that release is cut and the publish workflow downloads and checks then (`tools/verify_published_examples.py`; see "Verifying the CI example pin after publication" below). Every other position `python tools/check_version_consistency.py --list` prints states a floor, an install pin or a published manifest for a release already cut, so it keeps naming a release a reader can install today. Without the suffix, `src/agentic_hil` moves for a whole cycle while the version string stands still and a working tree becomes indistinguishable from the release it shadows: an install from it reports the released number, `agentic-hil upgrade` calls it `already_current`, and the install eval refuses to run a local matrix at all rather than report this tree as that release. The gate holds all three. On a release commit the distribution, release and anticipated versions are one string and every check is the one it always was; between releases each position is compared against the version it is supposed to carry, `--release-tag` is refused outright because a tag never carries a suffix, and a tree whose package has moved past its release without saying so is refused where the release tag is present to prove it.

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

## Verifying the CI example pin after publication

The shipped CI examples pin `AGENTIC_HIL_VERSION` to the release this tree builds toward, which is the release that first exposes the `check-plan` and `run-evidence` commands they invoke. `tests/test_ci_examples.py` proves, against this checkout's `build_parser`, that the examples invoke only commands the code defines and that the pin equals that anticipated release. That is everything a pull request can establish: it has no network, and between releases the pin names a release the index does not carry yet, so the checkout stands in for the artifact.

Exactly one moment closes that gap: after the release job has published to PyPI, the pin is a real distribution. So the publish workflow's `verify-published-examples` job runs `tools/verify_published_examples.py`, which reads the exact string the examples carry, confirms both examples agree on it and that it is the release this tree builds toward, installs that distribution from the index into an isolated environment, and asserts the installed CLI reports that version and answers `--help` for every command the examples invoke. The development checkout stops standing in for the published artifact; the artifact answers for itself.

It runs after the PyPI upload, which is the point of no return, so it is an alarm and not a gate: a published distribution cannot be inspected before it is published, and a version cannot be re-uploaded. What it turns into a red release is a release whose own examples name an artifact it did not ship or whose CLI does not expose what those examples run -- which is the failure a stranger who copied the file would otherwise be the first to hit.

## Release Checklist

Before creating a release:

```text
1. Bump the version in every position `python tools/check_version_consistency.py --list` prints, then run `python tools/check_version_consistency.py` until it is silent.
2. Refresh the pinned Astral uv bootstrap in both installers, as described under "The uv Installer Pin" below.
3. Re-check every host registration block in docs/mcp-hosts.md against the documentation it links, then move the date at the top of that page, as described under "The Host Documentation Check" below.
4. Run ruff check src tests evals tools and pytest.
5. Run python -m build (or uv build) and inspect the packaged files.
6. Open a pull request and let Required CI pass; the same check runs there, so a forgotten position is red before a release exists.
7. Create a GitHub Release with a strict SemVer vX.Y.Z tag that exactly matches pyproject.toml.
8. Let the publish workflow re-run the same check with the release tag before it builds, checks, and publishes to PyPI.
9. Let the workflow verify the PyPI ownership marker and publish the matching `server.json` through GitHub OIDC.
10. Let the workflow install the exact `agentic-hil==X.Y.Z` the CI examples pin and confirm the published CLI reports that version and answers every command the examples invoke (`tools/verify_published_examples.py`), so a release whose artifact does not match its own examples is an alarm on the release rather than a stranger's failed copy.
11. Verify: uvx --from agentic-hil agentic-hil --version resolves the new version from PyPI.
12. Verify the release appears as `io.github.agentic-hil/agentic-hil` in the MCP Registry API.
13. Attach the one-line installers *and* their checksums to the release, all four taken from the tagged commit: `install.sh`, `install.ps1`, and `sha256sum install.sh > install.sh.sha256` and `sha256sum install.ps1 > install.ps1.sha256`, uploaded under those names, in that format, because the verify-first path in docs/installation.md feeds them straight to `sha256sum -c`. The scripts belong there because the checksum can only speak for the file published beside it: the default branch moves between releases, so a recipe that pairs a release checksum with a default-branch script fails on the first fix that lands after a release.
14. Start from GitHub auto-generated release notes, then edit for clarity.
15. Move the tree to the next development version in pyproject.toml and src/agentic_hil/__init__.py, in the first commit after the release, and re-pin the CI examples to the release that development version now anticipates -- the three positions `python tools/check_version_consistency.py --list` marks as tracking the release this tree builds toward. Every other position keeps naming the release just published.
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

## The Host Documentation Check

`docs/mcp-hosts.md` prints one registration block per MCP host and links the
upstream page each block was read from. Host configuration formats are exactly
the kind of thing that moves without asking us, so the page carries the date it
was last checked, and that sentence is what tells a reader how far to trust the
syntax under each heading. Written once, it then survived every release and
every edit of its own file, which is how a date stops being evidence and starts
inviting trust it cannot support.

So the check is a release chore, done by a person who can read what changed:

```text
1. Open every page linked from a "Sources:" line in docs/mcp-hosts.md.
2. Compare each block against the shape that page documents now: the container
   key, the transport field, whether `cwd` and `enabled` are still keys, and
   which file the host reads them from. Where upstream moved, move our block and
   the prose around it; where it did not, leave both alone.
3. Follow a redirect to its destination and write the destination into the link.
   A permanent redirect is upstream saying the page has moved.
4. Move the date at the top of the page to the day the check was done, and only
   then: the date speaks for the blocks, not for the release.
5. Run pytest tests/test_tool_annotations.py tests/test_agentic_hil.py -k "host_documentation or host_guide",
   which holds that page's tool table and its three generated registration
   blocks against what this package advertises and writes.
```

A host whose documentation is unreachable that day is not silently blessed.
Scope the sentence to the hosts that were checked and name the one that could
not be, because a guide that says which of its blocks were verified is worth
more than one that claims all of them.

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
