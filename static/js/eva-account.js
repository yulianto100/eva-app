/* EVA account transport: same-origin CSRF, explicit logout; no token storage. */
(() => {
  const token = document.querySelector('meta[name="eva-csrf"]')?.content || '';
  const role = document.querySelector('meta[name="eva-role"]')?.content || 'user';
  const modifies = method => !['GET', 'HEAD', 'OPTIONS'].includes(String(method || 'GET').toUpperCase());
  const local = value => { try { return new URL(value, location.href).origin === location.origin; } catch { return false; } };
  const expired = async response => {
    if (response.status === 401) {
      let payload = {};
      try {
        if (response.clone) payload = await response.clone().json();
        else if (response.responseType === 'json') payload = response.response || {};
        else payload = JSON.parse(response.responseText || '{}');
      } catch {}
      if (payload.code === 'EVA_LOGIN_REQUIRED') location.assign('/login');
    }
    return response;
  };
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const target = typeof input === 'string' || input instanceof URL ? String(input) : input.url;
    const method = init.method || input.method || 'GET';
    if (token && local(target) && modifies(method)) {
      init = { ...init, redirect: 'error', headers: new Headers(init.headers || input.headers) };
      init.headers.set('X-EVA-CSRF', token);
    }
    const response = await originalFetch(input, init);
    return local(target) ? expired(response) : response;
  };
  if (window.XMLHttpRequest) {
    const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      this.evaLocalMutation = local(url) && modifies(method);
      return open.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function (...args) {
      if (token && this.evaLocalMutation) this.setRequestHeader('X-EVA-CSRF', token);
      this.addEventListener('load', () => { if (this.evaLocalMutation) expired(this); }, { once: true });
      return send.apply(this, args);
    };
  }
  document.addEventListener('submit', event => {
    const form = event.target;
    if (!token || !local(form.action || location.href) || !modifies(form.method)) return;
    let field = form.querySelector('input[name="eva_csrf"]');
    if (!field) { field = document.createElement('input'); field.type = 'hidden'; field.name = 'eva_csrf'; form.append(field); }
    field.value = token;
  }, true);
  function ready() {
    if (role !== 'owner') document.querySelectorAll('[data-eva-owner-only]').forEach(e => { e.hidden = true; });
    document.getElementById('eva-logout')?.addEventListener('click', async event => {
      const button = event.currentTarget; button.disabled = true;
      try {
        const response = await fetch('/auth/logout', { method: 'POST' });
        if (!response.ok) throw new Error('logout');
        location.assign('/login');
      } catch {
        button.disabled = false;
        const feedback = document.getElementById('eva-account-feedback');
        if (feedback) feedback.textContent = 'Keluar belum berhasil. Coba lagi.';
      }
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready, { once: true }); else ready();
})();
