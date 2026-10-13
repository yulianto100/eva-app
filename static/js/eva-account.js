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
  function initPersonalHistory() {
    const host = document.querySelector('.account-history');
    if (!host) return;
    const query = document.getElementById('account-history-query');
    const scope = document.getElementById('account-history-scope');
    const status = document.getElementById('account-history-status');
    const statusFilter = document.getElementById('account-history-status-filter');
    const search = document.getElementById('account-history-search');
    const refresh = document.getElementById('account-history-refresh');
    const prev = document.getElementById('account-history-prev');
    const next = document.getElementById('account-history-next');
    const rows = document.getElementById('account-history-rows');
    const head = document.getElementById('account-history-head');
    const feedback = document.getElementById('account-history-feedback');
    const summary = document.getElementById('account-history-summary');
    const tabs = [...host.querySelectorAll('[data-history-kind]')];
    const formatter = new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Jakarta', dateStyle:'short', timeStyle:'short', hourCycle:'h23'});
    const scopes = {cluster:'Cluster', subfeeder:'Subfeeder', feeder:'Feeder'};
    const statuses = {done:'Selesai', completed:'Selesai', error:'Gagal', failed:'Gagal', cancelled:'Dibatalkan', queued:'Antrean', running:'Diproses', processing:'Diproses', retrying:'Retry otomatis'};
    let kind = 'convert', page = 1, total = 0, controller = null, generation = 0;
    const text = v => v === null || v === undefined || v === '' ? '—' : String(v);
    const number = v => Number.isSafeInteger(v) && v >= 0 ? String(v) : '—';
    function line(parent, value, strong = false) {
      const element = document.createElement(strong ? 'strong' : 'span');
      element.textContent = text(value); parent.append(element); return element;
    }
    function date(value) {
      if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return '—';
      const parsed = new Date(value < 1e12 ? value * 1000 : value);
      return Number.isNaN(parsed.getTime()) ? '—' : formatter.format(parsed);
    }
    function headings(labels) {
      const row = document.createElement('tr');
      labels.forEach(label => { const th = document.createElement('th');th.scope = 'col';th.textContent = label;row.append(th); });
      head.replaceChildren(row);
    }
    function addRow(item) {
      const tr = document.createElement('tr');
      const cells = Array.from({length:kind === 'convert' ? 6 : 7}, () => tr.appendChild(document.createElement('td')));
      if (kind === 'convert') {
        line(cells[0], date(item.created_at), true); line(cells[0], item.job_id);
        line(cells[1], (Array.isArray(item.original_filenames) ? item.original_filenames : []).join(', ') || item.download_name, true);
        line(cells[2], statuses[item.status] || item.status, true); line(cells[2], Number.isFinite(item.progress) ? item.progress + '%' : '—');
        line(cells[3], item.scope_type, true);line(cells[3], item.network_type);
        line(cells[4], item.output_mode);
        line(cells[5], item.error || item.message);
      } else {
        line(cells[0], date(item.completed_at), true);
        line(cells[1], scopes[item.scope] || item.scope, true);line(cells[1], item.scope_code);line(cells[1], item.target_name);
        line(cells[2], item.work_order_number, true);line(cells[3], item.astri_username);
        line(cells[4], 'Total: ' + number(item.item_count), true);line(cells[4], 'Baru: ' + number(item.inserted_count));line(cells[4], 'Perbarui: ' + number(item.updated_count));line(cells[4], 'Sudah ada: ' + number(item.already_present_count));
        line(cells[5], item.initial_status);line(cells[5], item.final_status, true);line(cells[6], item.source_job_id);
      }
      return tr;
    }
    async function loadHistory() {
      const version = ++generation;
      controller?.abort(); controller = new AbortController();
      const current = controller;
      const timeout = setTimeout(() => current.abort(), 30000);
      host.setAttribute('aria-busy', 'true'); search.disabled = refresh.disabled = prev.disabled = next.disabled = true;
      feedback.textContent = 'Memuat riwayat…'; rows.replaceChildren();summary.textContent = '';
      headings(kind === 'convert' ? ['Dibuat (WIB) / Job ID','File','Status / Progress','Target Type / Jaringan','Output','Pesan / Error']
        : ['Selesai (WIB)','Target Type / Code / Name','Work Order Number','Akun ASTRI','Item','Status awal / akhir','Job asal']);
      const params = new URLSearchParams({kind, page:String(page), q:query.value.trim(), scope:scope.value});
      if (kind === 'convert') params.set('status', status.value);
      try {
        const response = await fetch('/api/account/history?' + params, {credentials:'same-origin',cache:'no-store',redirect:'error',signal:current.signal});
        const payload = await response.json();
        if (version !== generation) return;
        if (!response.ok) throw new Error('history unavailable');
        const items = kind === 'convert' ? payload.jobs : payload.history;
        if (!Array.isArray(items) || items.length > 20 || items.some(item => !item || typeof item !== 'object')
            || !Number.isSafeInteger(payload.total) || payload.total < 0 || payload.page !== page || payload.page_size !== 20) throw new Error('invalid history');
        total = payload.total;rows.replaceChildren(...items.map(addRow));
        feedback.textContent = items.length ? '' : (kind === 'boq' ? 'Belum ada Add BOQ sukses terverifikasi milik akun ini.' : 'Belum ada job konversi pada filter ini.');
        summary.textContent = total + ' riwayat · Halaman ' + page + ' dari ' + Math.max(1,Math.ceil(total / 20));
      } catch {
        if (version !== generation) return;
        total = 0;rows.replaceChildren();feedback.textContent = 'Riwayat belum bisa dimuat. Klik Muat ulang untuk mencoba kembali.';
      } finally {
        clearTimeout(timeout);
        if (version === generation) {
          host.setAttribute('aria-busy', 'false');search.disabled = refresh.disabled = false;
          prev.disabled = page <= 1;next.disabled = page * 20 >= total;
        }
      }
    }
    tabs.forEach(tab => tab.addEventListener('click', () => {
      kind = tab.dataset.historyKind;page = 1;query.value = '';scope.value = '';status.value = '';
      tabs.forEach(button => button.setAttribute('aria-pressed', String(button === tab)));
      statusFilter.hidden = kind === 'boq';query.placeholder = kind === 'boq' ? 'Target, WO, atau job asal' : 'Nama file atau job ID';
      document.getElementById('account-history-note').textContent = kind === 'boq'
        ? 'Hanya Add BOQ milik akun ini yang sukses terverifikasi: In Progress menjadi Closed.'
        : 'Job konversi milik akun ini yang masih tersimpan di EVA.';
      loadHistory();
    }));
    document.getElementById('account-history-filters').addEventListener('submit', event => {event.preventDefault();page = 1;loadHistory();});
    refresh.addEventListener('click', loadHistory);
    prev.addEventListener('click', () => {if(page > 1){page--;loadHistory();}});
    next.addEventListener('click', () => {if(page * 20 < total){page++;loadHistory();}});
    window.addEventListener('pagehide', () => {generation++;controller?.abort();}, {once:true});
    loadHistory();
  }
  function ready() {
    if (role !== 'owner') document.querySelectorAll('[data-eva-owner-only]').forEach(e => { e.hidden = true; });
    if (document.body.hasAttribute('data-eva-account-page')) {
      const refresh = document.getElementById('account-astri-refresh');
      const status = document.getElementById('account-astri-status');
      const badge = document.getElementById('account-astri-badge');
      const username = document.getElementById('account-astri-username');
      const manage = document.getElementById('account-astri-manage');
      async function checkConnection() {
        if (refresh.disabled) return;
        refresh.disabled = true;
        status.setAttribute('aria-busy', 'true');
        status.textContent = 'Memeriksa koneksi ASTRI…';
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 45000);
        try {
          const response = await fetch('/api/astri/status', {
            credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal,
          });
          if (!response.ok) throw new Error('status unavailable');
          const payload = await response.json();
          if (!payload || typeof payload !== 'object' || typeof payload.connected !== 'boolean') throw new Error('invalid status');
          const connected = payload.connected === true && payload.auth_state === 'connected';
          const authState = connected ? 'connected'
            : payload.auth_state === 'connected' ? 'unavailable' : payload.auth_state;
          username.textContent = payload.linked && typeof payload.username === 'string' && payload.username
            ? payload.username : 'Belum dihubungkan';
          const states = {
            connected: ['Terhubung', 'Koneksi ASTRI terverifikasi. EVA menjaganya di background.'],
            first_login: ['Belum terhubung', 'Hubungkan akun ASTRI sekali melalui panel DRM APD.'],
            credentials_invalid: ['Password perlu diperbarui', 'ASTRI menolak password tersimpan. Masukkan password baru dengan username yang sama.'],
            credentials_missing: ['Login perlu diperbarui', 'Password tersimpan belum tersedia. Lengkapi login akun yang sama di panel DRM APD.'],
            reconnecting: ['Menghubungkan', 'EVA sedang menghubungkan kembali akun ASTRI.'],
            unavailable: ['Sedang diperiksa', 'Koneksi ASTRI belum dapat dipastikan. Tidak perlu login ulang; login tersimpan tidak dihapus.'],
          };
          const copy = Object.prototype.hasOwnProperty.call(states, authState) ? states[authState] : states.unavailable;
          badge.textContent = copy[0];
          badge.dataset.connected = String(connected);
          status.textContent = copy[1];
          manage.textContent = authState === 'first_login' ? 'Hubungkan ASTRI'
            : authState === 'credentials_invalid' ? 'Perbarui password ASTRI' : 'Kelola koneksi ASTRI';
        } catch {
          badge.textContent = 'Sedang diperiksa';
          badge.dataset.connected = 'false';
          status.textContent = 'Status ASTRI belum bisa dibaca. Coba cek kembali; login tersimpan tidak dihapus.';
          manage.textContent = 'Kelola koneksi ASTRI';
        } finally {
          clearTimeout(timeout);
          refresh.disabled = false;
          status.setAttribute('aria-busy', 'false');
        }
      }
      refresh.addEventListener('click', checkConnection);
      checkConnection();
      initPersonalHistory();
    }
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
