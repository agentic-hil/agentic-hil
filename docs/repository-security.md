# Repository Security

This repository uses file-based controls for review routing, dependency monitoring, and release integrity:

- `.github/CODEOWNERS` routes ownership to the current maintainer.
- Dependabot monitors npm and GitHub Actions dependencies.
- Dependency Review blocks vulnerable dependency changes in pull requests.
- CodeQL scans JavaScript and TypeScript.
- OSSF Scorecard publishes repository hardening signals.
- npm publishing uses GitHub Actions OIDC provenance.
- GitHub Releases generate a CycloneDX SBOM and artifact attestations.
- `npm-shrinkwrap.json` freezes the dependency tree for the published CLI.

## Branch Protection

Branch protection is configured in GitHub repository settings, not in this source tree.

Use these settings for `master` while AI-HIL has one maintainer:

```text
require pull requests before merging
require status checks before merging
require branches to be up to date before merging
block force pushes
block branch deletion
required approval count: 0
```

Do not require human approvals yet. With only one maintainer, required approvals can block routine maintenance because the PR author cannot provide an independent review.

Once a second maintainer is available, change the protection rule to require at least one approving review and dismiss stale approvals when new commits are pushed.
