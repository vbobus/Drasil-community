const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);

function normalizeServerUrl(value) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error('Enter the HTTPS address of your Drasil server.');
  }

  let parsed;
  try {
    parsed = new URL(value.trim());
  } catch {
    throw new Error('Enter a complete server address, such as https://drasil.example.ts.net.');
  }

  const isLoopbackHttp = parsed.protocol === 'http:' && LOOPBACK_HOSTS.has(parsed.hostname);
  if (parsed.protocol !== 'https:' && !isLoopbackHttp) {
    throw new Error('Remote servers must use HTTPS. Plain HTTP is allowed only for localhost.');
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('The server address cannot contain credentials, a query, or a fragment.');
  }
  if (parsed.pathname !== '/' && parsed.pathname !== '') {
    throw new Error('Use the server origin without an extra path.');
  }

  return `${parsed.origin}/`;
}

function isSafeExternalUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:'
      || (parsed.protocol === 'http:' && LOOPBACK_HOSTS.has(parsed.hostname));
  } catch {
    return false;
  }
}

module.exports = { isSafeExternalUrl, normalizeServerUrl };
