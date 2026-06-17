#!/usr/bin/env bash
# Fail if a PR changes production code under mail_semantic_search/ without
# bumping `version` in pyproject.toml. Enforces the AGENTS.md release rule that
# the version bump must ride in the same change it gates — see
# "Releasing a new version (maintainers)".
#
# It guarantees a bump *happens*; it deliberately does not judge whether the
# semver level (patch/minor/major) is correct. That stays a human call.
#
# Usage: require-version-bump.sh [BASE_REF]   (default: origin/main)
set -euo pipefail

BASE_REF="${1:-origin/main}"

# Read the `version = "..."` value from pyproject.toml at a given git revision,
# or from the working tree when the argument is empty.
read_version() {
  local source
  if [ -z "${1:-}" ]; then
    source="$(cat pyproject.toml)"
  else
    source="$(git show "$1:pyproject.toml")"
  fi
  printf '%s\n' "$source" \
    | grep -m1 -E '^[[:space:]]*version[[:space:]]*=' \
    | sed -E 's/.*=[[:space:]]*"([^"]+)".*/\1/'
}

changed_files="$(git diff --name-only "${BASE_REF}...HEAD")"

if ! grep -qE '^mail_semantic_search/' <<<"$changed_files"; then
  echo "No production code under mail_semantic_search/ changed; version bump not required."
  exit 0
fi

old_version="$(read_version "$BASE_REF")"
new_version="$(read_version "")"

echo "Base (${BASE_REF}) version: ${old_version:-<none>}"
echo "Head version:              ${new_version:-<none>}"

if [ -z "$new_version" ]; then
  echo "::error::Could not read version from pyproject.toml." >&2
  exit 1
fi

if [ "$old_version" = "$new_version" ]; then
  {
    echo "::error::Version bump required."
    echo "This PR changes production code under mail_semantic_search/ but the"
    echo "version in pyproject.toml is still ${new_version}. Bump it (semver per"
    echo "AGENTS.md) in the same commit as the change it gates."
  } >&2
  exit 1
fi

echo "Version bumped ${old_version} -> ${new_version}. OK."
