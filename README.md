# Preflight

<img width="780" height="900" alt="C _ PR check" src="https://github.com/user-attachments/assets/a6c9446f-53b3-4fbb-852b-271a9b84ed55" />
A GitHub Actions workflow that reviews every PR for real bugs using Claude.

## What it does

On every PR open or update:
1. Fetches the diff and full file contents via GitHub API (no repo clone)
2. Sends context to Claude for a bug-focused review (null dereferences, unhandled errors, race conditions, security issues, etc.)
3. Runs a verifier pass to drop hallucinated findings
4. Posts inline comments on the PR for high/medium severity bugs
5. Posts a top-level summary explaining what the PR does and why

## Setup

### 1. Add the secret

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|------|-------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |

`GITHUB_TOKEN` is provided automatically by GitHub Actions — no extra configuration needed.

### 2. Copy the workflow files

```bash
# From the root of your target repo:
cp -r .github scripts .reviewbot.yaml.example /path/to/your-repo/
```

Or copy the three files manually:
- `.github/workflows/ai-review.yml`
- `scripts/review.js`
- `scripts/verifier.js`

### 3. Open a PR

The workflow triggers automatically on the next `opened` or `synchronize` event.

## Opting in / out

**Opt a file type out:** Add patterns to `.reviewbot.yaml` (copy from `.reviewbot.yaml.example`).

**Opt out a full directory:**
```yaml
# .reviewbot.yaml
ignore_paths:
  - "legacy/**"
  - "docs/**"
```

**Disable for a single PR:** Add `[skip ai-review]` anywhere in the PR title or description (planned feature; not yet implemented — disable by closing and reopening without the workflow).

## Review limits (cost control)

| Limit | Default | Why |
|-------|---------|-----|
| Max files per PR | 10 | Keeps token budget under ~80k |
| Max changed lines per file | 500 | Avoids reviewing generated diffs |
| Severity posted in Week 1 | high + medium | Establish signal before enabling low |

Files that exceed these limits are listed in a PR comment so the author knows what was skipped.

## Cost and performance

- Target: **< $0.60 per review** using `claude-sonnet-4-20250514`
- Target: **< 5 minutes** total (GitHub Actions timeout set to 10 min, run step to 5 min)
- Two Claude calls per review: review pass + verifier pass
- One additional call for the PR summary

## Severity policy

| Severity | Week 1 | After baseline |
|----------|--------|----------------|
| high     | posted | posted |
| medium   | posted | posted |
| low      | silent | posted |

Change the filter in `scripts/review.js` → `postReview()` when ready to enable low.

## Files

```
.github/
  workflows/
    ai-review.yml        # Workflow trigger and job definition
scripts/
  review.js              # Context fetcher, review call, comment poster
  verifier.js            # Verifier pass (drops hallucinated findings)
.reviewbot.yaml.example  # Copy to .reviewbot.yaml to customize
```

## Troubleshooting

**No comments posted:** Check the Actions run log for `[ai-review]` lines. Common causes:
- `ANTHROPIC_API_KEY` secret not set (skips silently by design)
- All changed files matched skip patterns
- Claude returned no high/medium findings

**Review failed partway:** The bot fails silently by design — it will never block a merge. Check the run log for the error message.

**Wrong model:** Update `MODEL` in `scripts/review.js` to use a different Claude model.
