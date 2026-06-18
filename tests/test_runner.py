"""
End-to-end tests for the review runner.

All tests mock the Anthropic client so no real API calls are made.
The verifier runs for real — evidence must exist verbatim in the diff.
"""

import json
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, "/Users/jsingh6/Desktop/preflight")

from src.config_loader import PreflightConfig, Rule
from src.review import runner
from src.review.runner import ReviewResult


# ── Shared fixtures ───────────────────────────────────────────────────────────

# A diff whose added lines contain the evidence strings used in findings below.
DIFF = """\
--- a/Foo.swift
+++ b/Foo.swift
@@ -1,5 +1,9 @@
 class ViewController: UIViewController {
+    func setup() {
+        viewModel.onUpdate = {
+            self.tableView.reloadData()
+        }
+    }
 }
"""

_RULE = Rule(
    id="preflight/ios/retain-cycle-closure",
    severity="high",
    description="Closure captures self strongly — retain cycle.",
)

CONFIG = PreflightConfig(
    platforms=["ios"],
    rules={"preflight/ios/retain-cycle-closure": _RULE},
    guidelines="",
    sensitivity="balanced",
)

CHANGED_FILES = ["Foo.swift"]

VALID_FINDING = {
    "rule_id": "preflight/ios/retain-cycle-closure",
    "severity": "high",
    "file": "Foo.swift",
    "line": 4,
    # Verbatim from the diff (normalised whitespace match via Corpus check 3)
    "evidence": "self.tableView.reloadData()",
    "explanation": "Strong self capture inside closure creates a retain cycle.",
}


def _resp(text, in_tokens=100, out_tokens=50):
    """Build a mock Anthropic response object."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    resp.usage.input_tokens = in_tokens
    resp.usage.output_tokens = out_tokens
    return resp


def _json(findings):
    return json.dumps(findings)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRunner(unittest.TestCase):

    @patch("src.review.runner.anthropic.Anthropic")
    def test_happy_path_deduplicates_across_passes(self, mock_cls):
        """Both passes return the same valid finding — output contains exactly one."""
        client = MagicMock()
        mock_cls.return_value = client
        client.messages.create.side_effect = [
            _resp(_json([VALID_FINDING])),   # pass 1
            _resp(_json([VALID_FINDING])),   # pass 2
        ]

        result = runner.run(DIFF, {}, CONFIG, CHANGED_FILES)

        self.assertIsInstance(result, ReviewResult)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0]["rule_id"], "preflight/ios/retain-cycle-closure")
        self.assertEqual(result.stats["retries"], 0)
        self.assertEqual(client.messages.create.call_count, 2)

    @patch("src.review.runner.anthropic.Anthropic")
    def test_retry_corrects_bad_evidence(self, mock_cls):
        """
        Pass 1 returns a finding with fabricated evidence — verifier rejects it.
        The retry corrects the evidence — finding passes and appears in output.
        Pass 2 returns nothing.
        """
        client = MagicMock()
        mock_cls.return_value = client

        bad_finding = {**VALID_FINDING, "evidence": "this text is not in the diff"}
        corrected_finding = VALID_FINDING

        client.messages.create.side_effect = [
            _resp(_json([bad_finding])),             # pass 1 — fails verification
            _resp(json.dumps(corrected_finding)),    # retry — corrected, passes
            _resp(_json([])),                         # pass 2 — no findings
        ]

        result = runner.run(DIFF, {}, CONFIG, CHANGED_FILES)

        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0]["rule_id"], "preflight/ios/retain-cycle-closure")
        self.assertEqual(result.stats["retries"], 1)
        self.assertEqual(client.messages.create.call_count, 3)

    @patch("src.review.runner.anthropic.Anthropic")
    def test_retry_withdrawal_moves_finding_to_dropped(self, mock_cls):
        """
        Pass 1 returns a finding with bad evidence.
        Model responds to retry with {"withdrawn": true}.
        Finding ends up in dropped, not in findings.
        """
        client = MagicMock()
        mock_cls.return_value = client

        bad_finding = {**VALID_FINDING, "evidence": "does not exist in diff"}

        client.messages.create.side_effect = [
            _resp(_json([bad_finding])),             # pass 1 — fails verification
            _resp(json.dumps({"withdrawn": True})),  # retry — withdrawn
            _resp(_json([])),                         # pass 2 — no findings
        ]

        result = runner.run(DIFF, {}, CONFIG, CHANGED_FILES)

        self.assertEqual(len(result.findings), 0)
        self.assertEqual(len(result.dropped), 1)
        self.assertEqual(result.dropped[0].get("_drop_reason"), "withdrawn on retry")
        self.assertEqual(result.stats["retries"], 1)

    @patch("src.review.runner.anthropic.Anthropic")
    def test_no_findings_returns_empty_result(self, mock_cls):
        """Both passes return [] — ReviewResult has no findings or dropped."""
        client = MagicMock()
        mock_cls.return_value = client
        client.messages.create.side_effect = [
            _resp(_json([])),   # pass 1
            _resp(_json([])),   # pass 2
        ]

        result = runner.run(DIFF, {}, CONFIG, CHANGED_FILES)

        self.assertEqual(result.findings, [])
        self.assertEqual(result.dropped, [])
        self.assertEqual(result.stats["retries"], 0)

    @patch("src.review.runner.anthropic.Anthropic")
    def test_token_stats_accumulate_across_passes(self, mock_cls):
        """input/output token counts from all API calls are summed in stats."""
        client = MagicMock()
        mock_cls.return_value = client

        bad_finding = {**VALID_FINDING, "evidence": "not in diff"}

        client.messages.create.side_effect = [
            _resp(_json([bad_finding]),  in_tokens=200, out_tokens=80),   # pass 1
            _resp(json.dumps(VALID_FINDING), in_tokens=150, out_tokens=60),  # retry
            _resp(_json([]),             in_tokens=200, out_tokens=10),   # pass 2
        ]

        result = runner.run(DIFF, {}, CONFIG, CHANGED_FILES)

        self.assertEqual(result.stats["input_tokens"],  200 + 150 + 200)
        self.assertEqual(result.stats["output_tokens"],  80 +  60 +  10)

    @patch("src.review.runner.anthropic.Anthropic")
    def test_findings_sorted_high_before_medium(self, mock_cls):
        """Output findings are sorted high → medium → low."""
        client = MagicMock()
        mock_cls.return_value = client

        medium_rule = Rule(
            id="preflight/ios/delegate-strong",
            severity="medium",
            description="Delegate not weak.",
        )
        config = PreflightConfig(
            platforms=["ios"],
            rules={
                "preflight/ios/retain-cycle-closure": _RULE,
                "preflight/ios/delegate-strong": medium_rule,
            },
            guidelines="",
            sensitivity="balanced",
        )

        diff = DIFF + "+    var delegate: UserDelegate?\n"

        medium_finding = {
            "rule_id": "preflight/ios/delegate-strong",
            "severity": "medium",
            "file": "Foo.swift",
            "line": 10,
            "evidence": "var delegate: UserDelegate?",
            "explanation": "Delegate is not weak.",
        }

        client.messages.create.side_effect = [
            _resp(_json([medium_finding, VALID_FINDING])),  # pass 1
            _resp(_json([])),                                # pass 2
        ]

        result = runner.run(diff, {}, config, CHANGED_FILES)

        self.assertEqual(len(result.findings), 2)
        self.assertEqual(result.findings[0]["severity"], "high")
        self.assertEqual(result.findings[1]["severity"], "medium")


if __name__ == "__main__":
    unittest.main()
