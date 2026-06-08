'use strict';
// Usage: node log-review.js <owner/repo> <pr-number> <mode>
// Fetches PR metadata + latest Preflight review from GitHub, appends to data/reviews.json.

const { execSync } = require('child_process');
const fs   = require('fs');
const path = require('path');

const [repo, pr, mode] = process.argv.slice(2);
if (!repo || !pr || !mode) {
  console.error('Usage: log-review.js owner/repo pr-number mode');
  process.exit(1);
}

function gh(endpoint) {
  return JSON.parse(execSync(`gh api ${endpoint}`, { encoding: 'utf-8' }));
}

const prData  = gh(`repos/${repo}/pulls/${pr}`);
const reviews = gh(`repos/${repo}/pulls/${pr}/reviews`);

const review = [...reviews].reverse().find(r => r.body && r.body.includes('Preflight'))
           || [...reviews].reverse().find(r => r.body);

let high = 0, medium = 0, low = 0;
if (review) {
  const body = review.body;
  const hm = body.match(/(\d+) high/i);   if (hm) high   = parseInt(hm[1], 10);
  const mm = body.match(/(\d+) medium/i); if (mm) medium = parseInt(mm[1], 10);
  const lm = body.match(/(\d+) low/i);    if (lm) low    = parseInt(lm[1], 10);
}

const entry = {
  repo,
  pr:          parseInt(pr, 10),
  title:       prData.title,
  url:         prData.html_url,
  reviewed_at: new Date().toISOString(),
  mode,
  findings:    { high, medium, low },
  merged:      prData.merged,
  pr_state:    prData.state,
};

const dataFile = path.join(__dirname, '..', 'data', 'reviews.json');
const log = JSON.parse(fs.readFileSync(dataFile, 'utf-8'));
log.unshift(entry);
fs.writeFileSync(dataFile, JSON.stringify(log, null, 2) + '\n');

console.log(`[preflight] Logged — ${high} high, ${medium} medium, ${low} low → ${prData.html_url}`);
