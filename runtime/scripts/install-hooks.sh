#!/usr/bin/env bash
set -euo pipefail
ln -sf ../../scripts/pre-push-check.sh "$(git rev-parse --git-path hooks)/pre-push"
echo "Installed pre-push hook."
