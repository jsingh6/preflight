"""
Review runner — orchestrates the full agentic review loop.

Flow per pass:
  build prompt → model call → verifier → retry loop (up to MAX_RETRIES)
Two passes run sequentially; findings are merged and deduplicated by supervisor.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import anthropic

from ..config_loader import PreflightConfig
from . import prompt_builder, verifier
from .verifier import Corpus


_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 2048


@dataclass
class ReviewResult:
    findings: list          # verified, deduped findings
    dropped: list           # rejected or withdrawn findings
    stats: dict             # input/output tokens, retries, elapsed time


def run(
    diff: str,
    context_files: dict,    # filename → content
    config: PreflightConfig,
    changed_files: list,
    api_key: str = None,
) -> ReviewResult:
    """Full review loop: prompt → 2 passes → verify + retry → supervisor merge."""
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    prompt = prompt_builder.build(diff, context_files, config, changed_files)
    active_rules = prompt.active_rules
    corpus = Corpus(diff, context_files)
    active_rule_ids = {r.id: r for r in active_rules}

    stats = {"passes": 2, "retries": 0, "input_tokens": 0, "output_tokens": 0}
    t_start = time.monotonic()

    pass_outputs = []

    for _ in range(2):
        raw = _call(client, prompt.system, prompt.user, stats)
        output = verifier.run(raw, active_rules, diff, context_files, changed_files)

        # Retry loop: keep retrying until no retriable findings remain.
        # Each iteration snapshots pending so resolve() can safely mutate the list.
        while True:
            batch = list(output.retryable)
            if not batch:
                break
            for retry_prompt, state in batch:
                stats["retries"] += 1
                revised_raw = _call(client, prompt.system, retry_prompt, stats)
                output.resolve(revised_raw, state, corpus, active_rule_ids)

        # Anything still pending has exhausted retries — drop it
        for state in list(output.pending):
            output.dropped.append({**state.finding, "_drop_reason": "retries exhausted"})
        output.pending.clear()

        pass_outputs.append(output)

    # Supervisor merge: combine both passes, deduplicate, sort by severity
    all_findings = pass_outputs[0].passed + pass_outputs[1].passed
    all_dropped  = pass_outputs[0].dropped + pass_outputs[1].dropped

    merged = _dedup(all_findings)
    _sort_by_severity(merged)

    stats["elapsed_s"] = round(time.monotonic() - t_start, 2)

    return ReviewResult(findings=merged, dropped=all_dropped, stats=stats)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _call(client: anthropic.Anthropic, system: str, user: str, stats: dict) -> str:
    response = client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    stats["input_tokens"]  += response.usage.input_tokens
    stats["output_tokens"] += response.usage.output_tokens
    if not response.content:
        return "[]"
    first = response.content[0]
    return first.text if first.type == "text" else "[]"


def _dedup(findings: list) -> list:
    """
    Remove duplicates across passes.
    Two findings collide when they share rule_id + file + approximate line (±3).
    """
    unique = []
    for f in findings:
        if not _matches_any(f, unique):
            unique.append(f)
    return unique


def _matches_any(finding: dict, others: list) -> bool:
    rule     = finding.get("rule_id")
    ffile    = finding.get("file")
    line     = finding.get("line")
    evidence = (finding.get("evidence") or "").strip()

    for other in others:
        if other.get("rule_id") != rule:
            continue
        if other.get("file") != ffile:
            continue

        # Same evidence string → same logical finding regardless of line number
        other_evidence = (other.get("evidence") or "").strip()
        if evidence and other_evidence and (
            evidence == other_evidence
            or evidence in other_evidence
            or other_evidence in evidence
        ):
            return True

        other_line = other.get("line")
        if line is None and other_line is None:
            return True
        if line is None or other_line is None:
            # One has no line — same rule+file+similar evidence already handled above;
            # if evidence differs it may be a distinct finding, so don't collapse.
            continue
        try:
            if abs(int(line) - int(other_line)) <= 10:
                return True
        except (TypeError, ValueError):
            pass
    return False


_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _sort_by_severity(findings: list) -> None:
    findings.sort(
        key=lambda f: (
            _SEVERITY_RANK.get(f.get("severity", ""), 3),
            f.get("file", ""),
            f.get("line") or 0,
        )
    )
