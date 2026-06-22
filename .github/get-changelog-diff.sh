#!/usr/bin/env bash
set -euo pipefail

if git describe --tags --abbrev=0 --first-parent >/dev/null 2>&1; then
  LAST_TAGGED_COMMIT="$(git describe --tags --abbrev=0 --first-parent)"
  git --no-pager diff "$LAST_TAGGED_COMMIT" -- CHANGELOG.md
else
  git --no-pager diff HEAD -- CHANGELOG.md
fi
