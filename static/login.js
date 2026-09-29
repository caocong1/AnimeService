'use strict';
document.querySelector('#login-form').addEventListener('submit', async e => {
  e.preventDefault();
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
