#!/usr/bin/env bash
# Point this clone's git hooks at bautin/hooks (gitleaks secret scan on every commit).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath bautin/hooks
echo "core.hooksPath -> bautin/hooks (gitleaks pre-commit active)"
