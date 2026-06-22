#!/usr/bin/env bash
set -euo pipefail

VERSION="$(python .github/fetch_version.py)"

if git rev-parse --verify --quiet "refs/tags/${VERSION}" >/dev/null; then
  echo "Tag ${VERSION} already exists locally."
  exit 0
fi

git tag "${VERSION}"
git push origin "${VERSION}"
