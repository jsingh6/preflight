"""
Builds system + user prompts for the review pass.

The system prompt injects the rule set for the stacks present in the diff,
plus any team guidelines. Findings must cite a rule ID and quote verbatim
evidence — this is the chain-of-custody requirement the Verifier checks.
"""

from dataclasses import dataclass
from pathlib import Path
import fnmatch
import json

from ..config_loader import PreflightConfig, Rule


# Maps file extension → stack name (matches keys in rules/ directory).
# Resolved against the repo's detected stacks so an iOS repo's .swift files
# map to "ios" while a server-side Swift repo's .swift files map to "swift".
_EXT_TO_STACK: dict[str, list[str]] = {
    ".swift": ["ios", "swift"],
    ".m":     ["ios"],
    ".mm":    ["ios"],
    ".kt":    ["android", "kotlin"],
    ".java":  ["android"],
    ".ts":    ["typescript"],
    ".tsx":   ["typescript"],
    ".js":    ["typescript"],
    ".jsx":   ["typescript"],
    ".mjs":   ["typescript"],
    ".py":    ["python"],
    ".go":    ["go"],
}

# Minimum severity included per sensitivity level.
_SENSITIVITY_THRESHOLD: dict[str, set[str]] = {
    "strict":   {"high"},
    "balanced": {"high", "medium"},
    "broad":    {"high", "medium", "low"},
}

_SENSITIVITY_INSTRUCTION: dict[str, str] = {
    "strict": (
        "Only report findings you are highly confident about. "
        "If you are uncertain whether the code path is reachable or the rule applies, do not report it."
    ),
    "balanced": (
        "Report findings where the evidence in the diff or context clearly supports the violation. "
        "Skip findings that require assumptions not grounded in the provided code."
    ),
    "broad": (
        "Report any plausible violation, including uncertain ones. "
        "For uncertain findings add an 'uncertain': true field and note what additional context "
        "would confirm or rule out the issue."
    ),
}


@dataclass
class ReviewPrompt:
    system: str
    user: str
    active_rules: list[Rule]   # rules injected — used by Verifier


def build(
    diff: str,
    context_files: dict[str, str],   # filename → file content
    config: PreflightConfig,
    changed_files: list[str],         # filenames from the diff
) -> ReviewPrompt:
    stacks_in_diff = _stacks_from_files(changed_files, config.platforms)
    active_rules   = _active_rules(config, stacks_in_diff, changed_files)
    guidelines     = config.guidelines.strip() if config.guidelines else ""

    system = _build_system_prompt(stacks_in_diff, active_rules, guidelines, config.sensitivity)
    user   = _build_user_prompt(diff, context_files)

    return ReviewPrompt(system=system, user=user, active_rules=active_rules)


# ── System prompt ────────────────────────────────────────────────────────────

def _build_system_prompt(
    stacks: list[str],
    rules: list[Rule],
    guidelines: str,
    sensitivity: str,
) -> str:
    _display = {"ios": "iOS", "android": "Android", "typescript": "TypeScript/JavaScript", "python": "Python", "go": "Go"}
    stacks_label = " / ".join(_display.get(s, s.upper()) for s in stacks) if stacks else "UNKNOWN"
    rule_block   = _format_rule_list(rules)
    sensitivity_note = _SENSITIVITY_INSTRUCTION.get(sensitivity, _SENSITIVITY_INSTRUCTION["balanced"])

    guidelines_section = ""
    if guidelines:
        guidelines_section = f"""
## Team Guidelines
Apply these as additional context. They do not need to be cited as rule IDs, but findings
that contradict them should be reflected in your explanation.

{guidelines}
"""

    finding_schema = json.dumps({
        "rule_id":     "<rule ID from the list above>",
        "severity":    "<high | medium | low>",
        "file":        "<filename>",
        "line":        "<line number as integer, or null>",
        "evidence":    "<verbatim quoted code from the diff or context — must exist word-for-word>",
        "explanation": "<why this specific code violates the rule>",
    }, indent=2)

    return f"""You are Preflight, a code review agent. Your job is to find real bugs — not style issues, not architecture opinions, not formatting.

You are reviewing {stacks_label} code.

## Rules

Every finding MUST:
1. Cite exactly one rule ID from the list below (no other rule IDs are valid)
2. Include verbatim quoted code from the diff or context as `evidence` — copy the exact characters
3. Use the severity defined by the rule, not your own judgment

{rule_block}
{guidelines_section}
## Sensitivity: {sensitivity.upper()}

{sensitivity_note}

## Output Format

Respond with a JSON array of findings. Each finding must match this structure exactly:

{finding_schema}

If you find no bugs, respond with an empty array: []

Do not include markdown fences, explanation text, or anything outside the JSON array.
"""


def _format_rule_list(rules: list[Rule]) -> str:
    if not rules:
        return "(no rules loaded — review based on general best practices)"

    lines = []
    for rule in sorted(rules, key=lambda r: ({"high": 0, "medium": 1, "low": 2}.get(r.severity, 3), r.id)):
        severity_tag = f"[{rule.severity.upper()}]"
        description  = rule.description.strip().splitlines()[0] if rule.description else "see rule documentation"
        lines.append(f"- {rule.id}  {severity_tag}\n  {description}")

    return "\n".join(lines)


# ── User prompt ──────────────────────────────────────────────────────────────

def _build_user_prompt(diff: str, context_files: dict[str, str]) -> str:
    context_section = ""
    if context_files:
        parts = []
        for filename, content in context_files.items():
            parts.append(f"### {filename}\n```\n{content}\n```")
        context_section = "\n## Context Files\n\n" + "\n\n".join(parts)

    return f"""## Diff

```diff
{diff}
```
{context_section}"""


# ── Rule selection ───────────────────────────────────────────────────────────

def _stacks_from_files(changed_files: list[str], detected_stacks: list[str]) -> list[str]:
    """
    Returns the subset of detected_stacks that are relevant to the changed files.
    Preserves the order from detected_stacks (most specific first).
    """
    relevant: set[str] = set()
    for filepath in changed_files:
        ext = Path(filepath).suffix.lower()
        for candidate_stack in _EXT_TO_STACK.get(ext, []):
            if candidate_stack in detected_stacks:
                relevant.add(candidate_stack)
                break   # take the first match (most specific)

    # Preserve detected_stacks ordering
    return [s for s in detected_stacks if s in relevant]


def _active_rules(
    config: PreflightConfig,
    stacks: list[str],
    changed_files: list[str],
) -> list[Rule]:
    """
    Returns rules that are:
    - Relevant to at least one stack in the diff
    - Not severity: ignore
    - Above the sensitivity threshold
    - Not path-ignored for all changed files
    """
    threshold = _SENSITIVITY_THRESHOLD.get(config.sensitivity, {"high", "medium"})
    active = []

    for rule in config.rules.values():
        if rule.severity == "ignore":
            continue
        if rule.severity not in threshold:
            continue
        if not _rule_applies_to_stacks(rule, stacks):
            continue
        if rule.paths_ignore and _all_files_ignored(changed_files, rule.paths_ignore):
            continue
        active.append(rule)

    return active


def _rule_applies_to_stacks(rule: Rule, stacks: list[str]) -> bool:
    """
    A rule applies if its ID namespace matches one of the active stacks.
    preflight/ios/...      → ios
    preflight/android/...  → android
    preflight/typescript/... → typescript
    custom/ios/...         → ios
    custom/eslint/...      → typescript (eslint rules are JS/TS)
    custom/swiftlint/...   → ios (swiftlint rules are Swift/iOS)
    """
    rule_id = rule.id.lower()

    for stack in stacks:
        if f"/{stack}/" in rule_id:
            return True

    # Source-based fallback for imported rules
    if rule.source.startswith("eslint:") and "typescript" in stacks:
        return True
    if rule.source.startswith("swiftlint:") and "ios" in stacks:
        return True
    if rule.source.startswith("android-lint:") and "android" in stacks:
        return True

    return False


def _all_files_ignored(changed_files: list[str], ignore_patterns: list[str]) -> bool:
    """Returns True only if EVERY changed file matches at least one ignore pattern."""
    for filepath in changed_files:
        if not any(fnmatch.fnmatch(filepath, pattern) for pattern in ignore_patterns):
            return False   # at least one file is not ignored
    return True
