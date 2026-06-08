#!/usr/bin/env bash
# Usage: ./scripts/preflight.sh <owner/repo> <pr-number>
# Runs a local dry-run review against any GitHub PR (public or private).
set -euo pipefail

REPO="${1:?Usage: preflight.sh owner/repo pr-number}"
PR="${2:?Usage: preflight.sh owner/repo pr-number}"

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "Error: ANTHROPIC_API_KEY is not set."
  echo "Export it first:  export ANTHROPIC_API_KEY=sk-ant-..."
  exit 1
fi

# Pull a token from gh CLI — works for both public and private repos
# as long as the user is authenticated with sufficient scope (repo).
GITHUB_TOKEN=$(gh auth token)

SHA=$(gh api repos/"$REPO"/pulls/"$PR" --jq '.head.sha')

GITHUB_TOKEN="$GITHUB_TOKEN" \
GITHUB_REPOSITORY="$REPO" \
PR_NUMBER="$PR" \
PR_HEAD_SHA="$SHA" \
DRY_RUN=true \
node "$(dirname "$0")/review.js"
