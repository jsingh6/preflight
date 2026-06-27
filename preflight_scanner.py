#!/usr/bin/env python3
"""
Preflight Daily Scanner
Finds the best open PRs to run Preflight against across TypeScript and Swift repos.
Zero tokens spent — uses GitHub REST API only.
Run: python3 preflight_scanner.py
Then pick a candidate and run the printed command.
"""

import urllib.request
import urllib.error
import json
import os
from datetime import datetime, timezone

# ── Repo targets ─────────────────────────────────────────────────────────────
# Only include repos whose primary language is TypeScript or Swift.
# Mixed-language or polyglot monorepos dilute the signal.

TYPESCRIPT_REPOS = [
    "refinedev/refine",
    "shadcn-ui/ui",
    "vitejs/vite",
    "TanStack/query",
    "TanStack/router",
    "trpc/trpc",
    "colinhacks/zod",
    "pmndrs/zustand",
    "pmndrs/jotai",
    "drizzle-team/drizzle-orm",
    "vercel/swr",
]

SWIFT_REPOS = [
    # Libraries
    "Alamofire/Alamofire",
    "Kingfisher/Kingfisher",
    "pointfreeco/swift-composable-architecture",
    "nicklockwood/SwiftFormat",
    "Moya/Moya",
    "apple/swift-collections",
    "SwiftUIX/SwiftUIX",
    # Real Swift product apps
    "mozilla-mobile/firefox-ios",
    "iina/iina",
    "signalapp/Signal-iOS",
]

# ── Scoring signals ───────────────────────────────────────────────────────────

TS_HOT_KEYWORDS = [
    "useEffect", "useState", "useMemo", "useCallback", "useRef",
    "async", "await", "Promise", "catch", "auth", "reset", "token",
    "fetch", "axios", "error", "retry", "interval", "timeout",
]

SWIFT_HOT_KEYWORDS = [
    "@escaping", "weak self", "completion", "delegate", "Timer",
    "DispatchQueue", "async", "await", "guard", "retain", "URLSession",
    "NotificationCenter", "addObserver", "removeObserver",
    "Task", "actor", "Sendable", "continuation", "try?", "as!",
    "nonisolated", "MainActor", "withCheckedContinuation",
]

# Title keywords that signal low-value PRs
SKIP_KEYWORDS_IN_TITLE = [
    "readme", "typo", "spelling", "docs only", "documentation",
    "chore:", "style:", "ci:", "bump version", "dependabot",
    "add note", "update readme", "changelog",
]

SKIP_LABELS = {"wontfix", "invalid", "duplicate", "on hold"}

# Extensions that count as "primary language" for each platform
TS_EXTENSIONS   = (".ts", ".tsx")
SWIFT_EXTENSIONS = (".swift",)

# TS/Swift additions must be at least this fraction of total PR additions.
MIN_LANGUAGE_FRACTION = 0.5

# Minimum TS/Swift lines added to be worth reviewing (filters trivial patches).
MIN_LANG_ADDITIONS = 50

# If test/spec additions exceed this fraction of TS/Swift additions, the PR is
# mostly new tests with little production logic change — less interesting.
MAX_TEST_FRACTION = 0.70

# Paths that indicate example or demo code — lower signal for real bugs.
EXAMPLE_PATHS = ("examples/", "demo/", "fixtures/", "playground/", "sample/", "docs/")

# Structural keywords whose presence in the diff means real logic changed,
# not just value tweaks or renames.
TS_STRUCTURAL   = ["function ", "class ", "interface ", "type ", "const ", "=>"]
SWIFT_STRUCTURAL = ["func ", "class ", "struct ", "protocol ", "extension ", "actor ", "enum "]

# ── GitHub API helpers ────────────────────────────────────────────────────────

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def gh_get(url):
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print(f"  ⚠️  Rate limited on {url} — set GITHUB_TOKEN env var for higher limits")
        return None
    except Exception:
        return None


def get_open_prs(repo):
    url = f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=30&sort=updated&direction=desc"
    return gh_get(url) or []


def get_pr_files(repo, pr_number):
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files?per_page=100"
    return gh_get(url) or []


def get_pr_reviews(repo, pr_number):
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"
    return gh_get(url) or []


def get_pr_comments(repo, pr_number):
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    return gh_get(url) or []

# ── Scoring ───────────────────────────────────────────────────────────────────

def score_pr(pr, repo, platform):
    """Returns (score, reasons, hot_files) or None to skip."""
    title    = pr.get("title", "").lower()
    labels   = {l["name"].lower() for l in pr.get("labels", [])}
    comments = pr.get("comments", 0)
    pr_number = pr["number"]

    if labels & SKIP_LABELS:
        return None
    if any(kw in title for kw in SKIP_KEYWORDS_IN_TITLE):
        return None
    if pr.get("draft"):
        return None
    if comments > 5:
        return None

    score   = 0
    reasons = []

    # Copilot review status
    reviews = get_pr_reviews(repo, pr_number)
    copilot_reviewed = False
    copilot_quota_hit = False
    for r in (reviews or []):
        user = r.get("user", {}).get("login", "")
        if "copilot" in user.lower():
            body_text = r.get("body", "").lower()
            if "quota" in body_text or "unable to review" in body_text:
                copilot_quota_hit = True
            else:
                copilot_reviewed = True

    if copilot_quota_hit:
        score += 20
        reasons.append("Copilot hit quota — no automated review")
    elif not copilot_reviewed:
        score += 10
        reasons.append("No Copilot review")
    else:
        score -= 10

    # Skip if Preflight already commented
    for c in (get_pr_comments(repo, pr_number) or []):
        if "posted by preflight" in (c.get("body") or "").lower():
            return None

    if comments == 0:
        score += 15
        reasons.append("No comments yet")
    elif comments <= 2:
        score += 5
        reasons.append(f"Only {comments} comment(s)")

    files = get_pr_files(repo, pr_number)
    if not files:
        return None

    keywords = TS_HOT_KEYWORDS if platform == "typescript" else SWIFT_HOT_KEYWORDS
    ext      = TS_EXTENSIONS   if platform == "typescript" else SWIFT_EXTENSIONS

    hot_files          = []
    keyword_hits       = []
    structural_hits    = []
    lang_additions     = 0   # additions in TS/Swift source files (non-test)
    lang_deletions     = 0   # deletions in TS/Swift source files (non-test)
    test_additions     = 0   # additions in TS/Swift test files
    total_additions    = 0   # additions across all files
    example_only       = True  # flips False when a non-example source file is found

    structural_kws = TS_STRUCTURAL if platform == "typescript" else SWIFT_STRUCTURAL

    for f in files:
        filename   = f.get("filename", "")
        patch      = f.get("patch", "") or ""
        additions  = f.get("additions", 0)
        deletions  = f.get("deletions", 0)
        total_additions += additions

        if not any(filename.endswith(e) for e in ext):
            continue

        is_test    = any(x in filename for x in ["spec.", "test.", "__tests__", ".test.", "Tests/", "tests/"])
        is_example = any(filename.startswith(p) or f"/{p}" in filename for p in EXAMPLE_PATHS)

        if is_test:
            test_additions += additions
            continue

        lang_additions += additions
        lang_deletions += deletions
        hot_files.append(filename)

        if not is_example:
            example_only = False

        added_lines = [l[1:] for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++")]
        added_text  = "\n".join(added_lines)

        keyword_hits.extend(kw for kw in keywords if kw in added_text)
        structural_hits.extend(kw for kw in structural_kws if kw in added_text)

    if not hot_files:
        return None

    # PR is mostly non-TS/Swift — skip
    all_additions = lang_additions + test_additions
    if total_additions > 0 and all_additions / total_additions < MIN_LANGUAGE_FRACTION:
        return None

    # Too small to be interesting
    if lang_additions < MIN_LANG_ADDITIONS:
        return None

    # Mostly test additions — less interesting for finding production bugs
    if lang_additions > 0 and test_additions / (lang_additions + test_additions) > MAX_TEST_FRACTION:
        return None

    # All source files are in example/demo directories — lower real-bug signal
    if example_only:
        score -= 10
        reasons.append("Example/demo code only")

    # Keyword density (additions only, so config-line noise doesn't inflate)
    unique_hits = list(set(keyword_hits))
    if len(unique_hits) >= 4:
        score += 25
        reasons.append(f"High signal density: {', '.join(unique_hits[:4])}")
    elif len(unique_hits) >= 2:
        score += 12
        reasons.append(f"Signal keywords: {', '.join(unique_hits[:3])}")
    elif len(unique_hits) == 1:
        score += 5
        reasons.append(f"Signal keyword: {unique_hits[0]}")
    else:
        return None

    # Structural change bonus — real logic modified, not just values/renames
    unique_structural = list(set(structural_hits))
    if len(unique_structural) >= 3:
        score += 15
        reasons.append("Structural changes (functions/types modified)")
    elif unique_structural:
        score += 7
        reasons.append("Some structural changes")

    # Multi-file source bonus — cross-file changes are harder to reason about
    if len(hot_files) >= 4:
        score += 10
        reasons.append(f"{len(hot_files)} source files changed")
    elif len(hot_files) >= 2:
        score += 5
        reasons.append(f"{len(hot_files)} source files changed")

    # Real refactor bonus — deletions signal existing code being reworked
    if lang_deletions >= 30:
        score += 8
        reasons.append(f"Significant refactor ({lang_deletions} lines removed)")
    elif lang_deletions >= 10:
        score += 3
        reasons.append(f"{lang_deletions} lines removed")

    # Diff size on TS/Swift source additions only
    if 50 <= lang_additions <= 400:
        score += 10
        reasons.append(f"Good diff size ({lang_additions} lines)")
    elif lang_additions > 400:
        score -= 5
        reasons.append(f"Large diff ({lang_additions} lines) — may produce noise")

    if any(x in title for x in ["fix", "bug", "patch", "issue", "error", "broken"]):
        score += 8
        reasons.append("Bug fix PR")

    return score, reasons, hot_files[:3]

# ── Main ──────────────────────────────────────────────────────────────────────

def scan_repos(repos, platform):
    candidates = []
    for repo in repos:
        print(f"  Scanning {repo}...")
        prs = get_open_prs(repo)
        for pr in prs:
            result = score_pr(pr, repo, platform)
            if result is None:
                continue
            score, reasons, files = result
            candidates.append({
                "repo":     repo,
                "number":   pr["number"],
                "title":    pr["title"],
                "url":      pr["html_url"],
                "score":    score,
                "reasons":  reasons,
                "files":    files,
                "platform": platform,
                "comments": pr.get("comments", 0),
            })
    return sorted(candidates, key=lambda x: x["score"], reverse=True)


def print_results(candidates, platform, top_n=3):
    label = "TypeScript" if platform == "typescript" else "Swift"
    print(f"\n{'='*60}")
    print(f"  {label} — Top Candidates")
    print(f"{'='*60}")

    shown = [c for c in candidates if c["platform"] == platform][:top_n]

    if not shown:
        print("  No strong candidates found today.")
        return

    for i, c in enumerate(shown, 1):
        print(f"\n  #{i}  [{c['score']} pts]  PR #{c['number']} — {c['repo']}")
        print(f"       {c['title']}")
        print(f"       {c['url']}")
        print(f"       Files: {', '.join(c['files'])}")
        print(f"       Why: {' · '.join(c['reasons'])}")
        print(f"\n       Run:")
        print(f"       gh pr diff {c['url']} | python3 preflight.py - --pr {c['url']} --platform {c['platform']}")


def main():
    print(f"\n🔍 Preflight Daily Scanner  —  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    if not GITHUB_TOKEN:
        print("\n⚠️  No GITHUB_TOKEN set. Rate limit is 60 req/hour unauthenticated.")
        print("   Set it with: export GITHUB_TOKEN=ghp_yourtoken\n")

    print("\n── TypeScript repos ──")
    ts_candidates = scan_repos(TYPESCRIPT_REPOS, "typescript")

    print("\n── Swift repos ──")
    swift_candidates = scan_repos(SWIFT_REPOS, "ios")

    all_candidates = ts_candidates + swift_candidates

    print_results(all_candidates, "typescript")
    print_results(all_candidates, "ios")

    print(f"\n{'='*60}")
    print("  Pick one command above and run it from your preflight directory.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
