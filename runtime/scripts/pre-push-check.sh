#!/usr/bin/env bash
# Pre-push gate. Heavy suites (full pytest, native OpenCode, S3) run in CI (ci.yml).
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
EXPECTED_REMOTE="git@github.com:ackstorm/ach-runtime.git"
fail() { echo "PRE-PUSH FAIL: $*" >&2; exit 1; }

PUSH_HEAD="$(git rev-parse HEAD)"
BASE_REF="${PRE_PUSH_BASE_REF:-origin/main}"
GITLEAKS_LOG_OPTS=""
if git rev-parse --verify "$BASE_REF" >/dev/null 2>&1; then
  GITLEAKS_LOG_OPTS="--log-opts=$(git merge-base "$BASE_REF" "$PUSH_HEAD")..${PUSH_HEAD}"
fi

# 1. gitleaks (only commits being pushed; full history when there is no base ref)
docker run --rm -v "$PWD:/repo:ro" zricethezav/gitleaks:v8.21.2 \
  detect --source=/repo --redact --no-banner --config=/repo/.gitleaks.toml \
  $GITLEAKS_LOG_OPTS || fail "gitleaks"

# 2. large tracked files (>2MB)
while IFS= read -r -d '' f; do
  [ -f "$f" ] || continue
  sz=$(stat -c%s "$f"); [ "$sz" -gt 2097152 ] && fail "large file $f ($sz B)"
done < <(git ls-files -z)

# 3. sensitive filenames
if git ls-files | grep -E '(\.env$|\.pem$|\.key$|id_rsa|kubeconfig|credentials\.json)'; then
  fail "sensitive file tracked"
fi

# 4. LICENSE + README, origin (warn-only)
[ -f LICENSE ] && [ -f README.md ] || echo "WARN: LICENSE/README missing" >&2
url=$(git remote get-url origin 2>/dev/null || echo "")
[ "$url" = "$EXPECTED_REMOTE" ] || echo "WARN: origin is '$url' (expected $EXPECTED_REMOTE)" >&2

# 5. SPDX headers on non-empty src/**/*.py
while IFS= read -r f; do
  [ -s "$f" ] || continue
  head -1 "$f" | grep -qx "# SPDX-License-Identifier: MIT" || fail "missing SPDX header: $f"
done < <(git ls-files 'src/**/*.py')

# 6. fast containerized checks: schema drift + config tests
make schema-check || fail "schema drift (run: make gen-schema)"
make test-config || fail "config tests"

echo "pre-push: all gates passed."
