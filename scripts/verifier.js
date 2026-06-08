'use strict';

const VERIFY_SYSTEM =
  'You are a code review verifier. Given a list of findings and the actual file contents, ' +
  'check each finding against four criteria: ' +
  '(1) The cited file exists in the provided context. ' +
  '(2) The cited line number falls within the file\'s changed lines. ' +
  '(3) The cited symbol or code pattern is actually present at or near that line. ' +
  '(4) The described bug is plausible given what the code actually does. ' +
  'Return ONLY the findings that pass ALL four checks as a valid JSON array. ' +
  'Drop any finding that references a non-existent file, impossible line number, ' +
  'absent symbol, or clearly wrong description. ' +
  'Respond ONLY with a valid JSON array, no preamble or explanation.';

// contexts: [{ filename, content, changedLines: Set<number> }]
// callClaude: (system, user, maxTokens?) => Promise<string>  (injected from review.js)
async function verifyFindings(findings, contexts, callClaude) {
  if (!findings.length) return [];

  // Build a compact representation so the verifier has all it needs without
  // re-sending the full diff (already paid for in the review call).
  const contextSummary = contexts.map(c => {
    const lineList = Array.from(c.changedLines).sort((a, b) => a - b);
    // Truncate content to 8 000 chars per file to keep this call cheap.
    const snippet = c.content.length > 8000
      ? c.content.slice(0, 8000) + '\n... (truncated)'
      : c.content;
    return [
      `### ${c.filename}`,
      `Changed lines: ${lineList.join(', ')}`,
      '```',
      snippet,
      '```',
    ].join('\n');
  }).join('\n\n');

  const user =
    `Findings to verify:\n${JSON.stringify(findings, null, 2)}\n\nCode context:\n${contextSummary}`;

  const raw = await callClaude(VERIFY_SYSTEM, user, 2048);
  const stripped = raw.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '').trim();
  const verified = JSON.parse(stripped);
  return Array.isArray(verified) ? verified : [];
}

module.exports = { verifyFindings };
