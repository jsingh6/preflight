# Preflight

AI code review that finds real bugs before they ship. Runs two independent passes with Claude, verifies every finding against the actual diff, and posts a PR comment only when it has something worth saying.

## How it works

```
diff → prompt builder → 2 model passes → verifier loop → supervisor merge → PR comment
```

1. **Stack detection** — reads the diff's file extensions to load the right rule set (iOS, Android, TypeScript, Python, Go)
2. **Two passes** — the model reviews the diff twice independently for better recall
3. **Chain-of-custody verification** — every finding must cite a rule ID and quote verbatim evidence from the diff. Findings that fabricate evidence are retried or dropped.
4. **Retry loop** — the verifier tells the model exactly what was wrong; the model corrects or withdraws
5. **Supervisor merge** — duplicates across passes are collapsed by rule + file + evidence
6. **PR comment** — findings posted in question form with a suggestion, not "this is wrong, fix it"

## Example

This finding was caught on a real open-source PR ([refinedev/refine#7425](https://github.com/refinedev/refine/pull/7425)):

> **`packages/mui/src/hooks/useDataGrid/index.ts`** · `preflight/typescript/useeffect-missing-deps` · HIGH
>
> Wondering if there could be an issue with `[muiCrudFilters, preferredPermanentFilters, columnsTypes.cur…` here?
>
> `columnsTypes.current` is a ref's `.current` property used in a `useMemo` dependency array. Refs are mutable objects whose `.current` value can change without React notifying the component, so `columnsTypes.current` captured at memo-creation time may become stale.
>
> **Suggestion:** could this be restructured to avoid the problem described above?

## Usage

```bash
# Review the current branch against main
git diff main...HEAD | python3 preflight.py -

# Review a saved diff and post to a PR
python3 preflight.py changes.diff --pr https://github.com/org/repo/pull/123

# Fetch a PR diff from GitHub and review it directly
gh pr diff https://github.com/org/repo/pull/123 | python3 preflight.py - --pr https://github.com/org/repo/pull/123

# Force a specific stack (useful for remote diffs without a local checkout)
python3 preflight.py changes.diff --platform typescript
```

## Setup

```bash
pip install anthropic pyyaml
export ANTHROPIC_API_KEY=sk-ant-...
```

## Rule sets

Built-in rules ship for four stacks. The right set is loaded automatically from the diff's file extensions.

| Stack | Extensions | Example rules |
|---|---|---|
| iOS / Swift | `.swift` `.m` | retain cycles, weak delegates, Timer leaks, force unwraps |
| Android / Kotlin | `.kt` `.java` | context leaks, unregistered receivers, WebView misconfig |
| TypeScript / JS | `.ts` `.tsx` `.js` | floating promises, missing deps, `any` casts, dangerouslySetInnerHTML |
| Python | `.py` | bare except, swallowed exceptions, SQL injection, mutable defaults |

## Repo config

Add `.preflight.yml` to override severity, ignore paths, or add guidelines:

```yaml
settings:
  sensitivity: strict   # strict | balanced | broad
  max_findings: 5

guidelines: |
  All database queries must use parameterized statements.
  Network calls must be made off the main thread.

rules:
  preflight/ios/force-unwrap:
    severity: ignore          # suppress a rule
  preflight/typescript/any-cast:
    severity: high            # escalate severity
    paths_ignore:
      - "**/__tests__/**"     # ignore in test files
```

## CLI flags

| Flag | Description |
|---|---|
| `DIFF` | Diff file to review, or `-` for stdin |
| `--pr URL` | Post findings as a PR comment |
| `--repo PATH` | Repo root for config and stack detection (default: cwd) |
| `--platform STACK` | Force rule set: `ios`, `android`, `typescript`, `python`, `go` |
| `--context FILE` | Extra file to include as context (repeatable) |
| `--output json` | Machine-readable output |
| `--no-color` | Disable ANSI colors |
