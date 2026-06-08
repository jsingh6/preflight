'use strict';

// ── Config ──────────────────────────────────────────────────────────────────

const GITHUB_TOKEN    = process.env.GITHUB_TOKEN;
const ANTHROPIC_KEY   = process.env.ANTHROPIC_API_KEY;
const [OWNER, REPO]   = (process.env.GITHUB_REPOSITORY || '').split('/');
const PR_NUMBER       = parseInt(process.env.PR_NUMBER, 10);
const PR_HEAD_SHA     = process.env.PR_HEAD_SHA;

const GITHUB_API  = 'https://api.github.com';
const MODEL       = 'claude-sonnet-4-6';
const MAX_FILES   = 10;
const MAX_LINES   = 500;   // skip files with more changed lines than this

// ── File filters ─────────────────────────────────────────────────────────────

const SKIP_PATTERNS = [
  /[/\\]vendor[/\\]/,
  /\.generated\./,
  /[/\\]node_modules[/\\]/,
  /^package-lock\.json$/,
  /^go\.sum$/,
  /\.lock$/,
  /\.pb\.go$/,
  /_generated\.go$/,
];

function shouldSkip(filename) {
  return SKIP_PATTERNS.some(p => p.test(filename));
}

// ── GitHub API helpers ───────────────────────────────────────────────────────

async function ghGet(path) {
  const res = await fetch(`${GITHUB_API}${path}`, {
    headers: {
      Authorization: `Bearer ${GITHUB_TOKEN}`,
      Accept: 'application/vnd.github.v3+json',
    },
  });
  if (!res.ok) throw new Error(`GitHub GET ${path} → ${res.status} ${await res.text()}`);
  return res.json();
}

async function ghPost(path, body) {
  const res = await fetch(`${GITHUB_API}${path}`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${GITHUB_TOKEN}`,
      Accept: 'application/vnd.github.v3+json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`GitHub POST ${path} → ${res.status} ${await res.text()}`);
  return res.json();
}

// ── Context fetcher ──────────────────────────────────────────────────────────

async function fetchPRFiles() {
  const files = [];
  for (let page = 1; ; page++) {
    const batch = await ghGet(
      `/repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/files?per_page=100&page=${page}`
    );
    files.push(...batch);
    if (batch.length < 100) break;
  }
  return files;
}

async function fetchBlob(sha) {
  const blob = await ghGet(`/repos/${OWNER}/${REPO}/git/blobs/${sha}`);
  return Buffer.from(blob.content, 'base64').toString('utf-8');
}

// Parse patch to find which line numbers (in the new file) were actually changed.
function parseChangedLines(patch) {
  const changed = new Set();
  if (!patch) return changed;
  let line = 0;
  for (const row of patch.split('\n')) {
    const m = row.match(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (m) { line = parseInt(m[1], 10) - 1; continue; }
    if (row.startsWith('\\')) continue;            // "No newline at end of file"
    if (row.startsWith('+'))  changed.add(++line); // addition → new file line
    else if (!row.startsWith('-')) line++;          // context line
  }
  return changed;
}

async function buildFileContexts(toReview) {
  return Promise.all(toReview.map(async f => {
    let content = '';
    try {
      content = await fetchBlob(f.sha);
    } catch (e) {
      console.warn(`[ai-review] blob fetch failed for ${f.filename}: ${e.message}`);
    }
    return {
      filename:     f.filename,
      patch:        f.patch || '',
      content,
      changedLines: parseChangedLines(f.patch),
    };
  }));
}

// ── Anthropic helper ─────────────────────────────────────────────────────────

async function callClaude(system, user, maxTokens = 4096) {
  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': ANTHROPIC_KEY,
      'anthropic-version': '2023-06-01',
    },
    body: JSON.stringify({
      model: MODEL,
      max_tokens: maxTokens,
      system,
      messages: [{ role: 'user', content: user }],
    }),
  });
  if (!res.ok) throw new Error(`Anthropic → ${res.status} ${await res.text()}`);
  const data = await res.json();
  return data.content[0].text;
}

// ── Review call ──────────────────────────────────────────────────────────────

const REVIEW_SYSTEM =
  'You are a senior engineer doing code review. Find real bugs only — not style issues. ' +
  'Only flag issues in the changed lines. Be specific, cite exact line numbers and symbol names. ' +
  'Bug categories: null/nil dereference, unhandled errors, logic errors, race conditions, ' +
  'resource leaks, type mismatches, edge cases, security issues (injection, path traversal, unvalidated input). ' +
  'Respond ONLY with a valid JSON array, no preamble. ' +
  'Schema per element: {"file":string,"line":number,"severity":"high"|"medium"|"low","category":string,"title":string,"body":string}. ' +
  'If no bugs found, respond with exactly: []';

function buildContextBlock(contexts) {
  return contexts.map(c =>
    `### ${c.filename}\n#### Diff\n\`\`\`diff\n${c.patch}\n\`\`\`\n#### Full file\n\`\`\`\n${c.content}\n\`\`\``
  ).join('\n\n---\n\n');
}

function stripFences(text) {
  return text.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '').trim();
}

async function runReview(contextBlock) {
  const raw = await callClaude(REVIEW_SYSTEM, `Review these PR changes:\n\n${contextBlock}`);
  const parsed = JSON.parse(stripFences(raw));
  return Array.isArray(parsed) ? parsed : [];
}

// ── Verifier pass ────────────────────────────────────────────────────────────

async function verifyFindings(findings, contexts) {
  if (!findings.length) return [];

  const { verifyFindings: verify } = require('./verifier');
  return verify(findings, contexts, callClaude);
}

// ── Client-side line guard ───────────────────────────────────────────────────
// Drop any finding whose line number isn't in the actual diff — this is a
// last-resort safety net so we never post a comment on an unchanged line.

function guardLines(findings, contexts) {
  return findings.filter(f => {
    const ctx = contexts.find(c => c.filename === f.file);
    return ctx && ctx.changedLines.has(f.line);
  });
}

// ── GitHub comment poster ────────────────────────────────────────────────────

async function postReview(findings, summaryBody) {
  // Week 1 policy: inline comments for high + medium only.
  const postable = findings.filter(f => f.severity === 'high' || f.severity === 'medium');

  const comments = postable.map(f => ({
    path: f.file,
    line: f.line,
    side: 'RIGHT',
    body: [
      `**[${f.severity.toUpperCase()}] ${f.title}**`,
      '',
      f.body,
      '',
      `_Category: ${f.category}_`,
    ].join('\n'),
  }));

  await ghPost(`/repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/reviews`, {
    commit_id: PR_HEAD_SHA,
    body:      summaryBody,
    event:     'COMMENT',
    comments,
  });
}

async function postComment(body) {
  await ghPost(`/repos/${OWNER}/${REPO}/issues/${PR_NUMBER}/comments`, { body });
}

// ── Orchestrator ─────────────────────────────────────────────────────────────

async function main() {
  if (!ANTHROPIC_KEY) {
    console.warn('[ai-review] ANTHROPIC_API_KEY not set — skipping review');
    return;
  }

  try {
    // ── Step 1: fetch and filter PR files ───────────────────────────────────
    const allFiles = await fetchPRFiles();
    const eligible = allFiles.filter(
      f => f.status !== 'removed' && !shouldSkip(f.filename)
    );

    // Sort by change volume descending so we prioritise the most-touched files.
    const ranked = eligible
      .map(f => ({ ...f, volume: f.additions + f.deletions }))
      .sort((a, b) => b.volume - a.volume);

    const toReview = [];
    const skipped  = [];
    for (const f of ranked) {
      if (f.volume > MAX_LINES) {
        skipped.push(`\`${f.filename}\` — ${f.volume} changed lines (limit: ${MAX_LINES})`);
      } else if (toReview.length >= MAX_FILES) {
        skipped.push(`\`${f.filename}\` — exceeds ${MAX_FILES}-file cap`);
      } else {
        toReview.push(f);
      }
    }

    if (skipped.length > 0) {
      await postComment(
        `## AI Review: Files Skipped\n\n` +
        `The following files exceeded review limits and were not checked:\n\n` +
        skipped.map(s => `- ${s}`).join('\n') +
        `\n\n> Adjust \`.reviewbot.yaml\` to change thresholds.`
      );
    }

    if (toReview.length === 0) {
      console.log('[ai-review] No eligible files to review');
      return;
    }

    // ── Step 2: fetch full file contents via blob API ────────────────────────
    const contexts = await buildFileContexts(toReview);
    const contextBlock = buildContextBlock(contexts);

    // ── Step 3: first-pass review ────────────────────────────────────────────
    let rawFindings = [];
    try {
      rawFindings = await runReview(contextBlock);
    } catch (err) {
      console.warn('[ai-review] Review call failed:', err.message);
    }

    // ── Step 4: verifier pass ────────────────────────────────────────────────
    let findings = rawFindings;
    if (rawFindings.length > 0) {
      try {
        findings = await verifyFindings(rawFindings, contexts);
      } catch (err) {
        console.warn('[ai-review] Verifier failed, using raw findings:', err.message);
      }
    }

    // ── Step 5: client-side line guard ───────────────────────────────────────
    findings = guardLines(findings, contexts);

    // ── Step 6: PR summary (separate Claude call, same context) ─────────────
    let summaryBody = '## AI Code Review\n\n_Summary unavailable._';
    try {
      const summaryText = await callClaude(
        'You are a senior engineer. In 3-5 sentences explain what this PR does and why, ' +
        'based on the diff. Focus on intent, not mechanics. Plain text only, no markdown.',
        `PR changes:\n\n${contextBlock}`,
        512
      );
      const high = findings.filter(f => f.severity === 'high').length;
      const med  = findings.filter(f => f.severity === 'medium').length;
      const findingSummary = findings.length > 0
        ? `**Findings:** ${high} high, ${med} medium — see inline comments`
        : '**No bugs found** in the reviewed files.';

      summaryBody =
        `## AI Code Review\n\n${summaryText.trim()}\n\n${findingSummary}`;
    } catch (err) {
      console.warn('[ai-review] Summary call failed:', err.message);
    }

    // ── Step 7: post review ──────────────────────────────────────────────────
    await postReview(findings, summaryBody);
    console.log(`[ai-review] Done — ${findings.length} finding(s)`);

  } catch (err) {
    // Fail silently: never block a PR due to a review bot error.
    console.warn('[ai-review] Unhandled error (PR not blocked):', err.message);
  }
}

main();
