# Publishing ach-agent to public GitHub

Authoritative checklist for pushing this repo to `https://github.com/ackstorm/ach-agent`.

> The git history has already been **flattened to a single "Initial commit"** during release
> prep. A backup of the pre-flatten history is kept locally (see "Backup" below). Nothing has
> been pushed — the push is a deliberate, manual step.

## TL;DR

```bash
make hooks                  # install the pre-push gate
make verify                 # lint + mypy + unit + conformance + secrets — must pass
git remote add origin git@github.com:ackstorm/ach-agent.git   # if not already set
git push -u origin main     # first publication
```

## Prerequisites

- Docker running (all tooling runs in the devtools container).
- An SSH key registered with GitHub for `ackstorm`.
- The GitHub repo `ackstorm/ach-agent` created (empty), and the team
  `@ackstorm/ach-agent-maintainers` created (referenced by `.github/CODEOWNERS`).
- A clean working tree (`git status` empty).

## Gates (run before pushing)

`make verify` and the pre-push hook both enforce:

1. **gitleaks** — no secrets in the tree (`.gitleaks.toml`).
2. **trufflehog** — no verified secrets in history.
3. **large files** — nothing over 2 MB tracked.
4. **sensitive filenames** — no `.env` / `.pem` / `.key` / `kubeconfig` / credentials tracked.
5. **LICENSE + README** present.
6. **remote** — origin is `git@github.com:ackstorm/ach-agent.git` (warn-only).
7. **SPDX headers** — every `src/**/*.py` starts with `# SPDX-License-Identifier: Apache-2.0`.
8. **lint + tests + conformance** — `make _lint`, `make _test-fast`, `make _conformance`.

## Backup (pre-flatten history)

The original commit history is preserved locally on a backup ref created during the flatten:

```bash
git tag | grep pre-flatten     # e.g. backup/pre-flatten-YYYYMMDD
git branch -a | grep pre-flatten
```

Do **not** push the backup ref. Delete it once you are satisfied the public repo is correct.

## First publication

```bash
git remote add origin git@github.com:ackstorm/ach-agent.git   # or `git remote set-url`
git push -u origin main
```

## After publishing

- Enable branch protection on `main`: require PR review + passing `ci` checks before merge.
- Confirm GitHub Actions has `packages: write` (GHCR) — used by `release` and `devtools-image`.
- Cut the first release: `make release-bump VERSION=0.1.0` (already at 0.1.0; no-op),
  then `make release-cut VERSION=0.1.0` to trigger the `release` workflow.
- Enable GitHub Pages (source: `gh-pages` branch) for the `docs` workflow output.
