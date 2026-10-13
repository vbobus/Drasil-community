const result = JSON.parse(document.getElementById('oauth-result').textContent);
document.getElementById('close-popup').addEventListener('click', () => window.close());
if (window.opener && !window.opener.closed) {
  window.opener.postMessage(result.payload, result.targetOrigin);
}
if (result.ok) window.setTimeout(() => window.close(), 900);
