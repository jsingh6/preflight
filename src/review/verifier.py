"""
Verifies findings produced by the review pass.

Each finding goes through up to 5 checks. Any failure produces a specific
rejection reason and a retry prompt. The caller runs the loop — the Verifier
is pure logic with no API calls.

Loop contract:
    findings = model.call(review_prompt)
    output   = verifier.run(findings, ...)
    for retry_prompt, failed in output.retryable:
        revised = model.call(retry_prompt)
        output.resolve(revised, ...)
    post(output.passed)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


MAX_RETRIES = 2

REQUIRED_FIELDS = {"rule_id", "severity", "file", "evidence", "explanation"}
VALID_SEVERITIES = {"high", "medium", "low"}


# ── Result types ─────────────────────────────────────────────────────────────

@dataclass
class VerifyResult:
    passed: bool
    check: str = ""     # which check failed: "parse" | "rule_id" | "severity" | "evidence" | "file"
    reason: str = ""    # human-readable rejection reason for the retry prompt


@dataclass
class FindingState:
    finding: dict
    result: VerifyResult
    attempts: int = 1


@dataclass
class VerifierOutput:
    passed: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    pending: list[FindingState] = field(default_factory=list)  # need retry

    @property
    def retryable(self):
        """Yields (retry_prompt, state) for findings that can still be retried."""
        for state in self.pending:
            if state.attempts <= MAX_RETRIES:
                yield build_retry_prompt(state.finding, state.result), state

    def resolve(self, raw_response: str, state: FindingState, corpus: "Corpus", active_rule_ids: set):
        """Process model response to a retry prompt. Updates passed/dropped/pending."""
        self.pending.remove(state)

        revised = _parse_retry_response(raw_response)
        if revised is None or revised.get("withdrawn"):
            self.dropped.append({**state.finding, "_drop_reason": "withdrawn on retry"})
            return

        revised_state = FindingState(finding=revised, result=VerifyResult(passed=False), attempts=state.attempts + 1)
        result = verify(revised, corpus, active_rule_ids)
        if result.passed:
            self.passed.append(revised)
        elif revised_state.attempts > MAX_RETRIES:
            self.dropped.append({**revised, "_drop_reason": result.reason})
        else:
            revised_state.result = result
            self.pending.append(revised_state)


# ── Entry point ───────────────────────────────────────────────────────────────

def run(
    raw_response: str,
    active_rules: list,          # list[Rule] from prompt_builder
    diff: str,
    context_files: dict,         # filename → content
    changed_files: list[str],
) -> VerifierOutput:
    """
    Parse model output and verify every finding.
    Returns a VerifierOutput with passed, dropped, and pending (need retry) findings.
    """
    active_rule_ids = {r.id: r for r in active_rules}
    corpus = Corpus(diff, context_files)
    output = VerifierOutput()

    findings, parse_error = _parse_findings(raw_response)

    if parse_error:
        # Unparseable response — nothing to verify
        output.dropped.append({"_drop_reason": f"unparseable model output: {parse_error}"})
        return output

    known_files = set(changed_files) | set(context_files.keys())

    for finding in findings:
        result = verify(finding, corpus, active_rule_ids, known_files)
        if result.passed:
            output.passed.append(finding)
        else:
            output.pending.append(FindingState(finding=finding, result=result))

    return output


# ── Core verification ─────────────────────────────────────────────────────────

def verify(
    finding: dict,
    corpus: "Corpus",
    active_rule_ids: dict,
    known_files: set = None,
) -> VerifyResult:
    """Run all checks on a single finding. Returns on first failure."""

    # 1. Required fields present
    missing = REQUIRED_FIELDS - set(finding.keys())
    if missing:
        return VerifyResult(
            passed=False,
            check="parse",
            reason=f"Finding is missing required fields: {', '.join(sorted(missing))}.",
        )

    rule_id  = finding.get("rule_id", "")
    severity = finding.get("severity", "")
    evidence = finding.get("evidence", "")
    file     = finding.get("file", "")

    # 2. Rule ID must be in the active rule set
    if rule_id not in active_rule_ids:
        valid_ids = "\n".join(f"  - {rid}" for rid in sorted(active_rule_ids))
        return VerifyResult(
            passed=False,
            check="rule_id",
            reason=(
                f'Rule ID "{rule_id}" is not in the active rule set for this review.\n'
                f"Valid rule IDs are:\n{valid_ids}"
            ),
        )

    # 3. Severity must match rule definition
    rule = active_rule_ids[rule_id]
    if severity != rule.severity:
        return VerifyResult(
            passed=False,
            check="severity",
            reason=(
                f'Severity "{severity}" does not match rule definition. '
                f'Rule {rule_id} defines severity as "{rule.severity}".'
            ),
        )

    # 4. Evidence must exist verbatim in the corpus
    if not evidence or not evidence.strip():
        return VerifyResult(
            passed=False,
            check="evidence",
            reason="Evidence field is empty. You must quote verbatim code from the diff or context.",
        )

    if not corpus.contains(evidence):
        truncated = evidence[:120] + ("..." if len(evidence) > 120 else "")
        return VerifyResult(
            passed=False,
            check="evidence",
            reason=(
                f'Evidence not found verbatim in the provided diff or context:\n'
                f'  "{truncated}"\n'
                f"Check for extra whitespace, reformatted code, or code that is not in the provided material."
            ),
        )

    # 5. File must be a file Preflight actually provided
    if known_files and file and file not in known_files:
        return VerifyResult(
            passed=False,
            check="file",
            reason=(
                f'File "{file}" was not part of this review. '
                f"Only cite files from the diff or context."
            ),
        )

    return VerifyResult(passed=True)


# ── Retry prompt ──────────────────────────────────────────────────────────────

def build_retry_prompt(finding: dict, result: VerifyResult) -> str:
    rule_id    = finding.get("rule_id", "<unknown>")
    file_loc   = finding.get("file", "<unknown>")
    line_loc   = finding.get("line", "")
    location   = f"{file_loc}:{line_loc}" if line_loc else file_loc

    return f"""Your finding was rejected by the verifier.

Finding: {rule_id} at {location}
Rejection reason: {result.reason}

You have two options:
1. Correct the finding — fix only the field(s) identified above and respond with the complete corrected JSON object.
2. Withdraw the finding — if the finding cannot be supported with accurate evidence from the provided material, respond with: {{"withdrawn": true}}

Do not include explanation text or markdown fences. Respond with only the JSON object or {{"withdrawn": true}}.
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

class Corpus:
    """Normalised searchable text built from the diff and context files."""

    def __init__(self, diff: str, context_files: dict):
        # Raw combined text — for verbatim search
        self._raw = diff + "\n" + "\n".join(context_files.values())

        # Diff with leading +/- markers stripped — model quotes code without markers
        stripped_diff = "\n".join(_strip_diff_marker(line) for line in diff.splitlines())
        self._stripped = stripped_diff + "\n" + "\n".join(context_files.values())

        # Normalised (collapsed whitespace) — fallback for minor formatting differences
        self._normalised = _normalise(self._stripped)

    def contains(self, evidence: str) -> bool:
        needle = evidence.strip()
        if not needle:
            return False

        # Check 1: exact match in raw diff+context
        if needle in self._raw:
            return True

        # Check 2: exact match after stripping diff markers
        if needle in self._stripped:
            return True

        # Check 3: normalised match (handles minor whitespace differences)
        if _normalise(needle) in self._normalised:
            return True

        return False


def _strip_diff_marker(line: str) -> str:
    """Remove leading +/- diff markers and the single space on context lines."""
    if line.startswith(("+", "-")):
        return line[1:]
    if line.startswith(" "):
        return line[1:]
    return line


def _normalise(text: str) -> str:
    """Collapse all whitespace runs to a single space for fuzzy matching."""
    return re.sub(r"\s+", " ", text).strip()


def _parse_findings(raw: str) -> tuple:
    """
    Parse a JSON array from the model response.
    Returns (findings_list, error_message).
    error_message is None on success.
    """
    text = raw.strip()

    # Strip accidental markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    text = text.strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return [], f"JSON parse error: {e}"

    if not isinstance(parsed, list):
        return [], f"Expected a JSON array, got {type(parsed).__name__}"

    return parsed, None


def _parse_retry_response(raw: str) -> dict | None:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    text = text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    return None
