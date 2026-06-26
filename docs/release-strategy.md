# Release Strategy

AI-HIL currently has `0.1.0` published on npm, but no GitHub Releases. GitHub Releases are the canonical place for ready-to-use versions, release notes, and later downloadable artifacts.

Do not cut the next release for metadata-only or README-only cleanup. Batch hygiene work into the next release that delivers visible user value.

Use small releases while the project stabilizes, but only when each release has a clear user-facing reason:

```text
0.2.0  stable npm installation, CI, publishable package checks, documented golden path
0.3.0  demo hardening and at least one additional substantial workflow improvement
```

After `0.3.0`, use monthly or bi-monthly SemVer releases with GitHub auto-generated release notes.

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

Npm remains the primary installation channel. Add downloadable binary artifacts later only when they are reproducible and built by CI.

## Distribution Channels

Use npm first. AI-HIL is currently a Node.js CLI with TypeScript builds and a package `bin` mapping, so PyPI should stay out of the primary path unless the project intentionally adds a Python wrapper.

Publish npm releases through GitHub Actions trusted publishing with OIDC and provenance. Do not add long-lived npm automation tokens unless trusted publishing is unavailable for a documented reason.

Later packaging candidates are Homebrew, Scoop or WinGet, and optional single-file binaries.

## Release Checklist

Before creating a release:

```text
1. Update package.json and package-lock.json to the release version.
2. Run npm test.
3. Run npm pack --dry-run and inspect the packaged files.
4. Create a strict SemVer vX.Y.Z Git tag that exactly matches package.json.
5. Let the release workflow validate the tag, test, pack-check, and create the GitHub Release.
6. Confirm the npm publish workflow uses OIDC trusted publishing and provenance.
7. Confirm the version is not already published to npm.
8. Start from GitHub auto-generated release notes, then edit for clarity.
```
