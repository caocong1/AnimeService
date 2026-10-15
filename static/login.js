'use strict';
let pairingStarted = false;

// A Strict cookie may be absent on a navigation from another site. Once this
// page has loaded, a same-origin request can reuse the saved device session.
async function restorePairing() {
  try {
    const r = await fetch('/api/bootstrap', { credentials: 'same-origin', cache: 'no-store' });
    if (!r.ok) return;
    const d = await r.json();
    if (typeof d.token === 'string' && d.token && !pairingStarted) location.replace('/');
  } catch (_) {
    // An unavailable server does not mean the saved pairing has expired.
  }
}
restorePairing();

document.querySelector('#login-form').addEventListener('submit', async e => {
  e.preventDefault();
  pairingStarted = true;
  const button = e.target.querySelector('button'), message = document.querySelector('#login-message');
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  message.textContent = '';
  try {
    const r = await fetch('/api/access/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: e.target.code.value.replace(/[\s-]/g, '').toUpperCase() }),
    });
    const d = await r.json();
    if (!r.ok) throw Error(d.error || d.detail || '连接失败');
    location.replace('/');
  } catch (err) {
    message.textContent = err.message;
  } finally {
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
});
