#!/usr/bin/env node
/* Prints the release notes for the version about to be released.
 * Prefers the "Unreleased" section the Release Scribe flow wrote into CHANGELOG.md;
 * falls back to a compact commit list so a release never ships with empty notes. */
const fs = require('fs');
const { execSync } = require('child_process');

const changelog = fs.existsSync('CHANGELOG.md') ? fs.readFileSync('CHANGELOG.md', 'utf8') : '';
const m = changelog.match(/## \[?Unreleased\]?[^\n]*\n([\s\S]*?)(?=\n## |\s*$)/i);
const section = m && m[1].trim();

if (section) {
  process.stdout.write(section + '\n');
} else {
  let log = '';
  try {
    const last = execSync('git describe --tags --abbrev=0 2>/dev/null || true').toString().trim();
    log = execSync(`git log --no-merges --pretty='- %s (%h)' ${last ? last + '..HEAD' : ''}`).toString();
  } catch (_) {
    /* ignore */
  }
  process.stdout.write('### Changes\n' + (log || '- Maintenance release\n'));
}
