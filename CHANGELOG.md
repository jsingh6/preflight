# Changelog

## [0.2.0] - 2026-06-26

### Changed
- PR findings are now posted as **inline review comments** attached to the specific diff line, instead of a single PR-level timeline comment. Findings without a resolvable line fall back to a regular comment.
- Fixed PR comment format: the flagged code block was positioned after the `> Suggestion:` line, making it appear the suggestion was just repeating the existing code. It now appears before under a `**Flagged code:**` label.

## [0.1.0] - 2026-06-20

- Initial release: two-pass review loop, chain-of-custody verifier, retry logic, supervisor dedup, PR comment posting.
