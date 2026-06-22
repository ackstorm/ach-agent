# Contributing to ach-agent

Thank you for considering contributing!  
See our [Code of Conduct](CODE_OF_CONDUCT.md).

## How to Contribute

1. Fork the repo and create your branch.
2. Make your changes.
3. Run tests and linters.
4. Submit a pull request, referencing any relevant issues.

## Issue Reporting

- For bugs, use the bug report template.
- For features, use the feature request template.

## Pull Requests

- Please review the [PR process](.github/PULL_REQUEST_TEMPLATE.md).
- All PRs require at least one review.

## Development

1. Install git hooks: `make hooks`
2. Run the full gate locally: `make verify` (lint + mypy + unit + conformance + secrets)
3. All commands run inside the devtools container — no host pip/venv required.
   Use `./scripts/dev.sh <command>` directly or `make <target>` for named targets.

## Changelog

Add your entry under `## [unreleased]` in `CHANGELOG.md` before submitting a PR.
Releases are cut by a maintainer via `make release-bump` / `make release-cut`.

## Proposal Process
Before we introduce significant changes to the project we want to gather feedback
from the community to ensure that we progress in the right direction before we
develop and release big changes. Significant changes include for example:

* proposing breaking changes
* changing the behavior of the service significantly
* adding new authentication or authorization flows

For large changes, open a discussion or issue first before submitting a PR.
