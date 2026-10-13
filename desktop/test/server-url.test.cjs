const assert = require('node:assert/strict');
const test = require('node:test');
const { isSafeExternalUrl, normalizeServerUrl } = require('../src/server-url.cjs');

test('normalizes secure server origins', () => {
  assert.equal(
    normalizeServerUrl(' https://drasil-vps.example.ts.net '),
    'https://drasil-vps.example.ts.net/',
  );
  assert.equal(normalizeServerUrl('https://example.com:8443/'), 'https://example.com:8443/');
});

test('allows plain HTTP only on loopback', () => {
  assert.equal(normalizeServerUrl('http://localhost:8000'), 'http://localhost:8000/');
  assert.equal(normalizeServerUrl('http://127.0.0.1:8000/'), 'http://127.0.0.1:8000/');
  assert.throws(() => normalizeServerUrl('http://100.64.0.2:8000'), /must use HTTPS/);
  assert.throws(() => normalizeServerUrl('http://drasil.example.ts.net'), /must use HTTPS/);
});

test('rejects ambiguous or privileged server addresses', () => {
  assert.throws(() => normalizeServerUrl(''), /Enter the HTTPS address/);
  assert.throws(() => normalizeServerUrl('javascript:alert(1)'), /must use HTTPS/);
  assert.throws(() => normalizeServerUrl('https://user:pass@example.com'), /credentials/);
  assert.throws(() => normalizeServerUrl('https://example.com/?target=other'), /query/);
  assert.throws(() => normalizeServerUrl('https://example.com/app'), /without an extra path/);
});

test('external links are restricted to HTTPS and loopback development', () => {
  assert.equal(isSafeExternalUrl('https://accounts.google.com/'), true);
  assert.equal(isSafeExternalUrl('http://localhost:8000/docs'), true);
  assert.equal(isSafeExternalUrl('http://example.com/'), false);
  assert.equal(isSafeExternalUrl('file:///C:/Windows/System32/calc.exe'), false);
  assert.equal(isSafeExternalUrl('not a URL'), false);
});
