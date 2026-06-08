#!/usr/bin/env bash
# Preflight — agent mode
# Usage: ./scripts/preflight-agent.sh <owner/repo> <pr-number>
#
# Runs Claude Code as the reviewer directly — no Anthropic API key needed.
# Requires: claude CLI (Claude Code), gh CLI authenticated with repo access.
# Works with public repos, private repos, and GitHub Enterprise (gh handles auth).
set -euo pipefail

REPO="${1:?Usage: preflight-agent.sh owner/repo pr-number}"
PR="${2:?Usage: preflight-agent.sh owner/repo pr-number}"

if [[ ! "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "Error: invalid repo format '$REPO'. Expected owner/repo."
  exit 1
fi
if [[ ! "$PR" =~ ^[0-9]+$ ]]; then
  echo "Error: invalid PR number '$PR'."
  exit 1
fi

if ! command -v claude &>/dev/null; then
  echo "Error: claude CLI not found. Install Claude Code: https://claude.ai/code"
  exit 1
fi

if ! command -v gh &>/dev/null; then
  echo "Error: gh CLI not found. Install: https://cli.github.com"
  exit 1
fi

if ! gh api "repos/$REPO" --silent 2>/dev/null; then
  echo "Error: gh cannot access $REPO. Run 'gh auth login' and ensure you have repo access."
  exit 1
fi

echo "[preflight] Starting agent review of $REPO#$PR ..."

claude -p "You are Preflight, an automated code reviewer. Review GitHub PR $REPO#$PR and post your findings as inline comments.

## Steps

1. Fetch the list of changed files:
   gh api repos/$REPO/pulls/$PR/files --paginate

2. Filter out files that should be skipped (do not review these):
   - Paths containing: vendor/, node_modules/
   - Filenames matching: *.generated.*, package-lock.json, go.sum, *.lock, *.pb.go, *_generated.go
   - Files where (additions + deletions) > 500
   - After filtering, take only the top 10 files ranked by (additions + deletions) descending

3. For each file to review, fetch its full content using the blob sha from step 1:
   gh api repos/$REPO/git/blobs/{sha} --jq '.content' | base64 -d

4. Study the diff patch for each file. Note exactly which line numbers in the new file were added or changed (lines starting with '+' in the patch, tracking the @@ hunk headers).

5. Review only the changed lines for real bugs — not style, not formatting. Check every category below:

   CORRECTNESS
   - null/nil dereference
   - off-by-one in loop bounds or index math
   - integer overflow/underflow in arithmetic or size calculations
   - wrong boolean logic (flipped condition, missing else branch, incorrect &&/||)
   - incorrect operator (= vs ==, reference vs value equality)
   - data truncation or precision loss in type conversions
   - dead/unreachable code that masks a missing case

   ERROR HANDLING
   - swallowed or ignored errors
   - missing error propagation
   - panic/throw on a recoverable error
   - resources not released on the error path (missing defer/finally/close)
   - no retry or fallback for transient failures

   CONCURRENCY
   - unsynchronized access to shared mutable state
   - deadlock from improper lock ordering
   - TOCTOU (check-then-act with a window for state change)
   - operations that must be atomic but are not
   - goroutine or thread leak (no exit condition)

   RESOURCE MANAGEMENT
   - unclosed files, DB connections, HTTP response bodies, or sockets
   - missing timeout on HTTP calls, DB queries, or lock acquisition
   - unbounded resource growth (no cap on goroutines, connections, or in-memory collections)

   SECURITY
   - SQL/command/LDAP injection
   - path traversal
   - SSRF (user-controlled URL fetched server-side)
   - authentication bypass
   - authorization bypass or missing privilege check
   - hardcoded credentials or secrets
   - sensitive data in logs, error messages, or URLs
   - weak or misused crypto (MD5/SHA1 for security, ECB mode, hardcoded IV, weak key derivation)
   - non-constant-time comparison of secrets
   - open redirect
   - unsafe deserialization

   API & CONTRACTS
   - incorrect use of a third-party API (wrong param order, missing required field)
   - violated function precondition or postcondition
   - missing input validation at a trust boundary
   - loading an unbounded dataset without pagination

6. Post a single PR review via:
   gh api repos/$REPO/pulls/$PR/reviews --method POST --input -

   Build the JSON payload with:
   - commit_id: the PR head SHA (fetch from gh api repos/$REPO/pulls/$PR --jq '.head.sha')
   - event: COMMENT
   - body: MUST start with exactly '## Preflight Review', followed by a blank line,
     then 2-3 sentences summarizing what the PR does, then a blank line,
     then exactly one of these findings lines:
       '**Findings:** N high, N medium, N low'   (if bugs found)
       '**No bugs found** in the reviewed files.' (if none)
   - comments: array of inline findings (high + medium severity only)
     Each comment needs: path, line (new-file line number), side: RIGHT, body

   Format each inline comment body as:
   **[SEVERITY] Title**

   Explanation citing exact symbol name and line.

   _Category: category_

7. If no bugs are found, post the review with an empty comments array.

## Rules
- Only comment on lines present in the diff. Never flag unchanged lines.
- Cite exact line numbers, function names, and variable names.
- Consolidate everything into a single gh api call — do not post multiple reviews.
- If files were skipped due to filters, list them in the summary body." \
  --allowedTools "Bash" \
  --max-turns 30

node "$(dirname "$0")/log-review.js" "$REPO" "$PR" "agent"
