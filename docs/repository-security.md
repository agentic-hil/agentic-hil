# Repository Security

This repository uses file-based controls for dependency monitoring and release integrity:

- Dependabot monitors pip and GitHub Actions dependencies (`.github/dependabot.yml`).
- PyPI publishing uses GitHub Actions OIDC trusted publishing — no long-lived API tokens.
- The publish workflow refuses releases whose tag does not match the `pyproject.toml` version and generates digital attestations for the uploaded distributions.
- CI runs ruff and the full test suite across Linux/macOS/Windows and Python 3.10–3.13; a single `Required CI` gate aggregates the matrix for branch protection.
- Configuration and permission bypasses are treated as security vulnerabilities (see `SECURITY.md`).

Additional controls already in place:

- `.github/CODEOWNERS` routes ownership to the maintainer.
- CodeQL scans Python on pushes and pull requests.
- Dependency Review runs on pull requests and an OSSF Scorecard workflow runs on the default branch.

## Branch Protection

Branch protection is configured in GitHub repository settings, not in this source tree.

Use these settings for the default branch, `master`:

```text
require pull requests before merging
require status checks before merging (Required CI)
require branches to be up to date before merging
block force pushes
block branch deletion
required approval count: 1
dismiss stale approvals on new commits
```

Development on this repository is largely agent-driven, so pull-request authors and the reviewing maintainer are different identities — a required approval count of 1 stays workable even with a single human maintainer.
