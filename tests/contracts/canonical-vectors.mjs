// Проверь канонические байты независимо от Python — тем же Node, что у клиента.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

function ordered(value) {
  if (Array.isArray(value)) return value.map(ordered);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, ordered(value[key])]));
  }
  return value;
}

const prefixes = {
  operation: 'gym:sync-operation:v1\n',
  proposal: 'gym:program-change:v1\n',
  request: 'gym:request:v1\n',
};
const { vectors } = JSON.parse(readFileSync(new URL('../../contracts/canonical-vectors.json', import.meta.url), 'utf8'));
for (const vector of vectors) {
  const canonical = JSON.stringify(ordered(vector.value));
  assert.equal(canonical, vector.canonical_text);
  const digest = createHash('sha256').update(prefixes[vector.kind] + canonical, 'utf8').digest('hex');
  assert.equal(digest, vector.sha256);
}
console.log(`Canonical vectors: ${vectors.length} passed in Node`);
