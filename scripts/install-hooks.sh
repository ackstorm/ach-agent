#!/usr/bin/env bash
set -euo pipefail

HOOKS_DIR="$(git rev-parse --git-path hooks)"
ln -sf ../../scripts/pre-push-check.sh "$HOOKS_DIR/pre-push"
echo "Installed pre-push hook."
