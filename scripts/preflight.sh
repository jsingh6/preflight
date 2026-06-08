#!/usr/bin/env bash
# Usage: ./scripts/preflight.sh <owner/repo> <pr-number>
# Runs a local dry-run review against any public GitHub PR.
set -euo pipefail

REPO="${1:?Usage: preflight.sh owner/repo pr-number}"
PR="${2:?Usage: preflight.sh owner/repo pr-number}"

SHA=$(gh api repos/"$REPO"/pulls/"$PR" --jq '.head.sha')

GITHUB_REPOSITORY="$REPO" \
PR_NUMBER="$PR" \
PR_HEAD_SHA="$SHA" \
DRY_RUN=true \
node "$(dirname "$0")/review.js"
