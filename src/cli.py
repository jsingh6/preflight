"""
CLI entrypoint for Preflight.

Usage:
  python3 preflight.py [diff_file]         review a diff file
  git diff HEAD | python3 preflight.py -   pipe diff via stdin
  python3 preflight.py                     auto: git diff HEAD in --repo
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    args = _parse_args()

    diff = _get_diff(args)
    if not diff.strip():
        print("preflight: nothing to review (empty diff)")
        sys.exit(0)

    changed_files = _extract_changed_files(diff)

    repo_path = Path(args.repo).resolve()

    try:
        from .config_loader import load as load_config, PreflightConfig
        config = load_config(repo_path)
    except Exception as exc:
        _die(f"failed to load config: {exc}")

    # If explicit --platform given, rebuild config around those stacks.
    # Otherwise, if the diff's file types don't match any detected stack
    # (common when reviewing a remote diff without a local checkout),
    # supplement with stacks inferred from the diff's extensions.
    if args.platform:
        stacks = [s.strip() for s in args.platform.split(",")]
        config = PreflightConfig.defaults(stacks)
    else:
        diff_stacks = _stacks_from_extensions(changed_files)
        missing = [s for s in diff_stacks if s not in config.platforms]
        if missing:
            augmented = PreflightConfig.defaults(config.platforms + missing)
            # Preserve any .preflight.yml overrides already in config
            for rule_id, rule in config.rules.items():
                augmented.rules[rule_id] = rule
            augmented.guidelines = config.guidelines
            augmented.sensitivity = config.sensitivity
            config = augmented

    context_files: dict = {}
    for path_str in args.context or []:
        p = Path(path_str)
        if p.exists():
            context_files[str(p)] = p.read_text(errors="ignore")
        else:
            _warn(f"context file not found: {path_str}")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        _die("ANTHROPIC_API_KEY is not set")

    try:
        from .review import runner
        result = runner.run(diff, context_files, config, changed_files, api_key=api_key)
    except Exception as exc:
        _die(f"review failed: {exc}")

    if args.output == "json":
        print(json.dumps({
            "findings": result.findings,
            "dropped":  result.dropped,
            "stats":    result.stats,
        }, indent=2))
    else:
        _print_text(result, color=args.color and sys.stdout.isatty())

    if args.pr and result.findings:
        _post_pr_comment(args.pr, result)
    elif args.pr and not result.findings:
        print("preflight: no findings — skipping PR comment")

    sys.exit(1 if result.findings else 0)


# ── Argument parsing ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="preflight",
        description="AI code review — finds real bugs before they ship.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  preflight.py                     # auto git diff HEAD\n"
            "  preflight.py changes.diff        # diff file\n"
            "  git diff HEAD | preflight.py -   # stdin\n"
            "  preflight.py --output json       # machine-readable\n"
        ),
    )
    p.add_argument(
        "diff_file",
        nargs="?",
        metavar="DIFF",
        help="diff/patch file to review, or '-' for stdin (default: git diff HEAD)",
    )
    p.add_argument(
        "--repo",
        default=".",
        metavar="PATH",
        help="repo root for config and platform detection (default: cwd)",
    )
    p.add_argument(
        "--context",
        action="append",
        metavar="FILE",
        help="additional context file (repeatable)",
    )
    p.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    p.add_argument(
        "--no-color",
        dest="color",
        action="store_false",
        default=True,
        help="disable ANSI colors",
    )
    p.add_argument(
        "--platform",
        default=None,
        metavar="STACK",
        help="force rule set: ios, android, typescript, python, go (comma-separated)",
    )
    p.add_argument(
        "--pr",
        default=None,
        metavar="URL",
        help="post findings as a comment on this GitHub PR URL",
    )
    return p.parse_args()


# ── Diff acquisition ──────────────────────────────────────────────────────────

def _get_diff(args: argparse.Namespace) -> str:
    if args.diff_file == "-":
        return sys.stdin.read()

    if args.diff_file:
        p = Path(args.diff_file)
        if not p.exists():
            _die(f"diff file not found: {args.diff_file}")
        return p.read_text(errors="ignore")

    # Auto-detect: git diff HEAD
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=args.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout

        # Fallback: staged changes only
        result = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=args.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
    except FileNotFoundError:
        _die("git not found — provide a diff file or pipe via stdin")

    return ""


def _extract_changed_files(diff: str) -> list:
    files = []
    for line in diff.splitlines():
        if not line.startswith("+++ "):
            continue
        # Strip "+++ " and any trailing tab (git appends timestamp with -p 0)
        path = line[4:].split("\t")[0].strip()
        if path == "/dev/null":
            continue
        if path.startswith("b/"):
            path = path[2:]
        if path and path not in files:
            files.append(path)
    return files


_EXT_STACK = {
    ".swift": "ios",  ".m": "ios",  ".mm": "ios",
    ".kt":    "android", ".java": "android",
    ".ts":    "typescript", ".tsx": "typescript",
    ".js":    "typescript", ".jsx": "typescript", ".mjs": "typescript",
    ".py":    "python",
    ".go":    "go",
}


def _stacks_from_extensions(changed_files: list) -> list:
    seen: list = []
    for f in changed_files:
        stack = _EXT_STACK.get(Path(f).suffix.lower())
        if stack and stack not in seen:
            seen.append(stack)
    return seen


# ── Text output ───────────────────────────────────────────────────────────────

_SEVERITY_COLOR = {
    "high":   "\033[31m",   # red
    "medium": "\033[33m",   # yellow
    "low":    "\033[34m",   # blue
}
_RESET = "\033[0m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"


def _print_text(result, color: bool) -> None:
    findings = result.findings
    stats    = result.stats

    elapsed = f"{stats.get('elapsed_s', 0):.1f}s"
    tokens  = f"{stats.get('input_tokens', 0) + stats.get('output_tokens', 0):,} tokens"

    if not findings:
        print(f"Preflight: no issues found  ·  {elapsed}  ·  {tokens}")
        return

    count = len(findings)
    label = "1 finding" if count == 1 else f"{count} findings"
    print(f"\nPreflight: {_bold(label, color)}  ·  {elapsed}  ·  {tokens}\n")

    for f in findings:
        severity = f.get("severity", "low")
        rule_id  = f.get("rule_id", "")
        ffile    = f.get("file", "")
        line     = f.get("line")
        evidence = f.get("evidence", "").strip()
        explanation = f.get("explanation", "").strip()

        location = f"{ffile}:{line}" if line else ffile
        sev_tag  = f"[{severity.upper()}]"

        if color:
            col = _SEVERITY_COLOR.get(severity, "")
            sev_tag = f"{col}{_BOLD}{sev_tag}{_RESET}"

        print(f"  {sev_tag}  {location}")
        print(f"         {_dim(rule_id, color)}")
        if explanation:
            print(f"         {explanation}")
        if evidence:
            # Truncate long evidence to keep output scannable
            ev = evidence.replace("\n", " ↵ ")
            if len(ev) > 100:
                ev = ev[:97] + "..."
            print(f"         {_dim('Evidence: ' + repr(ev), color)}")
        print()

    # Summary line
    by_sev: dict = {}
    for f in findings:
        s = f.get("severity", "low")
        by_sev[s] = by_sev.get(s, 0) + 1

    parts = []
    for sev in ("high", "medium", "low"):
        if sev in by_sev:
            parts.append(f"{by_sev[sev]} {sev}")
    print("  " + "  ·  ".join(parts))
    print()


def _bold(text: str, color: bool) -> str:
    return f"{_BOLD}{text}{_RESET}" if color else text


def _dim(text: str, color: bool) -> str:
    return f"{_DIM}{text}{_RESET}" if color else text


# ── PR comment ───────────────────────────────────────────────────────────────

def _post_pr_comment(pr_url: str, result) -> None:
    body = _format_pr_comment(result)
    try:
        proc = subprocess.run(
            ["gh", "pr", "comment", pr_url, "--body", body],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            print(f"preflight: comment posted → {proc.stdout.strip()}")
        else:
            _warn(f"gh pr comment failed: {proc.stderr.strip()}")
    except FileNotFoundError:
        _warn("gh not found — install the GitHub CLI to post PR comments")


def _format_pr_comment(result) -> str:
    findings = result.findings
    count    = len(findings)
    label    = "1 potential issue" if count == 1 else f"{count} potential issues"

    lines = [
        f"**Preflight flagged {label} worth a look:**",
        "",
    ]

    for f in findings:
        severity    = f.get("severity", "low").upper()
        rule_id     = f.get("rule_id", "")
        ffile       = f.get("file", "")
        line        = f.get("line")
        evidence    = f.get("evidence", "").strip()
        explanation = f.get("explanation", "").strip()

        location = f"`{ffile}:{line}`" if line else f"`{ffile}`"
        lines.append(f"---")
        lines.append(f"**{location}** · `{rule_id}` · {severity}")
        lines.append("")

        if explanation:
            # Open with a question anchored to the evidence, then give the full explanation
            ev_short = (evidence[:60] + "…") if len(evidence) > 60 else evidence
            lines.append(f"Wondering if there could be an issue with `{ev_short}` here?")
            lines.append("")
            lines.append(explanation)
            lines.append("")
            lines.append(f"> **Suggestion:** could this be restructured to avoid the problem described above?")

        if evidence:
            ev = evidence.replace("\n", "\n  ")
            lines.append("")
            lines.append(f"```")
            lines.append(ev)
            lines.append(f"```")

        lines.append("")

    lines += [
        "---",
        "*Posted by [Preflight](https://github.com/jsingh6/preflight)*",
    ]

    return "\n".join(lines)



# ── Helpers ───────────────────────────────────────────────────────────────────

def _die(msg: str) -> None:
    print(f"preflight: error: {msg}", file=sys.stderr)
    sys.exit(2)


def _warn(msg: str) -> None:
    print(f"preflight: warning: {msg}", file=sys.stderr)
