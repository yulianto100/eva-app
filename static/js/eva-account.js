/* EVA account transport: same-origin CSRF, explicit logout; no token storage. */
(() => {
  const token = document.querySelector('meta[name="eva-csrf"]')?.content || '';
  const role = document.querySelector('meta[name="eva-role"]')?.content || 'user';
  const modifies = method => !['GET', 'HEAD', 'OPTIONS'].includes(String(method || 'GET').toUpperCase());
  const local = value => { try { return new URL(value, location.href).origin === location.origin; } catch { return false; } };
  let authGeneration = 0, stopIdle = () => {}, resumeIdle = () => {};
  const invalidateAuth = () => { ++authGeneration; stopIdle(); };
  // Read-only identity/generation observer; never exposes CSRF or session material.
  const userId = document.querySelector('meta[name="eva-user-id"]')?.content || '';
  Object.defineProperty(window, 'evaAuthContext', { value: Object.freeze({ userId, get generation() { return authGeneration; } }) });
  const expired = async (response, generation = authGeneration) => {
    if (response.status === 401 && generation === authGeneration) {
      let payload = {};
      try {
        if (response.clone) payload = await response.clone().json();
        else if (response.responseType === 'json') payload = response.response || {};
        else payload = JSON.parse(response.responseText || '{}');
      } catch {}
      if (generation === authGeneration && payload.code === 'EVA_LOGIN_REQUIRED') {
        invalidateAuth(); location.assign('/login');
      }
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
    const generation = authGeneration;
    const response = await originalFetch(input, init);
    return local(target) ? expired(response, generation) : response;
  };
  if (window.XMLHttpRequest) {
    const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      this.evaLocalMutation = local(url) && modifies(method);
      return open.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function (...args) {
      const generation = authGeneration;
      if (token && this.evaLocalMutation) this.setRequestHeader('X-EVA-CSRF', token);
      this.addEventListener('load', () => { if (this.evaLocalMutation) expired(this, generation); }, { once: true });
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
  // EVA idle state is independent of ASTRI presence and read-only polling.
  function initIdle() {
    if (!token) return;
    let timer, flushTimer, wakeTimer, inflight = null, active = true, retries = 0, readPending = false;
    let lastInput = null, inputVersion = 0, sentVersion = 0, lastProof = -Infinity, deadline = -Infinity;
    const documents = new WeakSet(), frames = new WeakSet();
    function scheduleInput() {
      if (!active || inflight || flushTimer || inputVersion === sentVersion || document.hidden) return;
      if (lastInput === null || performance.now() - lastInput >= 1800000) return;
      flushTimer = setTimeout(() => { flushTimer = null; request(true); }, 1000);
    }
    async function request(activity = false) {
      if (!active) return;
      if (inflight) { if (!activity) readPending = true; return; }
      const age = lastInput === null ? 1800 : (performance.now() - lastInput) / 1000;
      if (activity && (document.hidden || inputVersion === sentVersion || age < 0 || age >= 1800)) return;
      const generation = authGeneration, current = new AbortController(), started = performance.now();
      inflight = current; clearTimeout(wakeTimer); wakeTimer = null;
      const timeout = setTimeout(() => current.abort(), 10000);
      if (activity) sentVersion = inputVersion;
      try {
        const response = await fetch(activity ? '/auth/activity' : '/auth/me', {
          credentials:'same-origin',cache:'no-store',redirect:'error',signal:current.signal,
          ...(activity ? {method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({activity_age_seconds:age})} : {}),
        });
        const payload = await response.json();
        if (!active || generation !== authGeneration || current.signal.aborted) return;
        if (!response.ok) throw new Error('idle unavailable');
        const duration = payload.idle_expires_at - payload.server_now;
        if (typeof payload.server_now !== 'number' || !Number.isFinite(payload.server_now) || payload.server_now <= 0
            || typeof payload.idle_expires_at !== 'number' || !Number.isFinite(payload.idle_expires_at)
            || !Number.isFinite(duration) || duration <= 0 || duration > 1800) throw new Error('invalid idle proof');
        retries = 0; lastProof = performance.now(); clearTimeout(timer);
        deadline = started + duration * 1000;
        timer = setTimeout(() => request(), Math.max(0, deadline - performance.now()));
      } catch {
        // ponytail: two GET retries, then a visible/online wake; never replay a POST.
        if (active && generation === authGeneration && !activity && retries < 2) {
          clearTimeout(timer); timer = setTimeout(() => request(), 1000 * ++retries);
        }
      } finally {
        clearTimeout(timeout);
        if (inflight === current) {
          inflight = null;
          if (readPending) { readPending = false; clearTimeout(timer); request(); } else scheduleInput();
        }
      }
    }
    function wake() {
      if (!active || document.hidden || inflight || wakeTimer || performance.now() - lastProof < 1000) return;
      wakeTimer = setTimeout(() => { wakeTimer = null; retries = 0; request(); }, 50);
    }
    function bindFrame(frame) {
      if (!frames.has(frame)) {
        frames.add(frame); frame.addEventListener('load', () => bindFrame(frame));
      }
      // contentDocument enforces origin/sandbox boundaries, including srcdoc/about:blank.
      try { if (frame.contentDocument) bindDocument(frame.contentDocument); } catch {}
    }
    function scan(root) {
      if (root.nodeType === 1 && root.matches('iframe')) bindFrame(root);
      root.querySelectorAll?.('iframe').forEach(bindFrame);
    }
    function bindDocument(doc) {
      if (!active || documents.has(doc)) return;
      documents.add(doc);
      for (const type of ['pointerdown','keydown','touchstart','wheel']) {
        doc.addEventListener(type, event => {
          if (!active || !event.isTrusted || document.hidden || doc.hidden) return;
          const stamp = event.timeStamp + (doc.defaultView.performance.timeOrigin - performance.timeOrigin);
          if (!Number.isFinite(stamp) || stamp < 0 || stamp > performance.now()) return;
          lastInput = Math.max(lastInput ?? 0, stamp); ++inputVersion;
          if (deadline - performance.now() <= 2000) request(true); else scheduleInput();
        }, {capture:true,passive:true});
      }
      scan(doc);
      new MutationObserver(changes => {
        if (active) for (const change of changes) change.addedNodes.forEach(scan);
      }).observe(doc, {childList:true,subtree:true});
    }
    stopIdle = () => {
      active = false; clearTimeout(timer); clearTimeout(flushTimer); clearTimeout(wakeTimer);
      timer = flushTimer = wakeTimer = null; lastInput = null; sentVersion = inputVersion; readPending = false;
      inflight?.abort(); inflight = null;
    };
    resumeIdle = () => { active = true; retries = 0; lastProof = -Infinity; scan(document); request(); };
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        clearTimeout(flushTimer); flushTimer = null; lastInput = null; sentVersion = inputVersion;
      } else wake();
    });
    window.addEventListener('focus', wake); window.addEventListener('online', wake);
    window.addEventListener('pagehide', invalidateAuth);
    window.addEventListener('pageshow', event => { if (event.persisted) resumeIdle(); else wake(); });
    bindDocument(document); request();
  }
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
      const cells = Array.from({length:kind === 'convert' ? 5 : 6}, () => tr.appendChild(document.createElement('td')));
      if (kind === 'convert') {
        const filenames = (Array.isArray(item.original_filenames) ? item.original_filenames : [])
          .filter(name => typeof name === 'string' && !/\.pbf$/i.test(name.trim()));
        const fallback = typeof item.download_name === 'string' && !/\.pbf$/i.test(item.download_name.trim()) ? item.download_name : '';
        line(cells[0], filenames.join(', ') || fallback, true);
        line(cells[0], date(item.created_at) + ' WIB');
        const statusLabel = line(cells[1], statuses[item.status] || item.status, true);
        statusLabel.className = 'account-job-status';
        statusLabel.dataset.status = String(item.status || '');
        line(cells[1], Number.isFinite(item.progress) ? item.progress + '%' : '—');
        line(cells[1], [item.scope_type, item.network_type].filter(Boolean).join(' · '));
        line(cells[2], String(item.output_mode || '').replaceAll('_', ' + ').toUpperCase(), true).className = 'account-output-label';
        line(cells[3], item.error || item.message);
        const expected = typeof item.job_id === 'string' && /^[0-9a-f]{32}$/.test(item.job_id)
          ? '/api/account/history/' + item.job_id + '/download' : null;
        if (item.download_state === 'ready' && expected && item.download_url === expected) {
          const link = document.createElement('a');
          link.className = 'account-button account-secondary account-history-download';
          link.href = expected; link.textContent = 'Unduh hasil';
          cells[4].append(link);
        } else {
          const states = {expired:'Tautan kedaluwarsa', unavailable:'File tidak tersedia', pending:'Hasil belum tersedia', premium_required:'Perlu Premium atau Owner'};
          line(cells[4], Object.prototype.hasOwnProperty.call(states, item.download_state) ? states[item.download_state] : states.unavailable, true);
        }
        if (typeof item.download_expires_at === 'number' && date(item.download_expires_at) !== '—') {
          line(cells[4], 'Berlaku hingga ' + date(item.download_expires_at) + ' WIB');
        }
        const inputs = Array.isArray(item.input_downloads) ? item.input_downloads : [];
        if (inputs.length) {
          const wrap = document.createElement('div');
          wrap.className = 'account-input-downloads';
          const label = document.createElement('span');
          label.className = 'account-input-label';
          label.textContent = 'File asli:';
          wrap.append(label);
          inputs.forEach((f, index) => {
            if (!f || typeof f.url !== 'string') return;
            const a = document.createElement('a');
            a.className = 'account-button account-secondary account-input-download';
            a.href = f.url;
            a.textContent = 'Unduh file asli' + (inputs.length > 1 ? ' ' + (index + 1) : '');
            a.title = 'Unduh file asli yang Anda unggah (retensi 24 jam)';
            wrap.append(a);
          });
          cells[4].append(wrap);
        } else if (item.status === 'done' || item.status === 'completed') {
          const note = document.createElement('span');
          note.className = 'account-input-expired';
          note.textContent = 'File asli sudah dibersihkan (retensi 24 jam)';
          cells[4].append(note);
        }
      } else {
        line(cells[0], date(item.completed_at), true);
        line(cells[1], scopes[item.scope] || item.scope, true);line(cells[1], item.scope_code);line(cells[1], item.target_name);
        line(cells[2], item.work_order_number, true);line(cells[3], item.astri_username);
        line(cells[4], 'Total: ' + number(item.item_count), true);line(cells[4], 'Baru: ' + number(item.inserted_count));line(cells[4], 'Perbarui: ' + number(item.updated_count));line(cells[4], 'Sudah ada: ' + number(item.already_present_count));
        line(cells[5], item.initial_status);line(cells[5], item.final_status, true);
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
      headings(kind === 'convert' ? ['Pekerjaan / Dibuat (WIB)','Status / Target Type','Output','Pesan / Error','Unduh']
        : ['Selesai (WIB)','Target Type / Code / Name','Work Order Number','Akun ASTRI','Item','Status awal / akhir']);
      const params = new URLSearchParams({kind, page:String(page), q:query.value.trim(), scope:scope.value});
      if (kind === 'convert') params.set('status', status.value);
      try {
        const response = await fetch('/api/account/history?' + params, {credentials:'same-origin',cache:'no-store',redirect:'error',signal:current.signal});
        const payload = await response.json();
        if (version !== generation) return;
        if (!response.ok) throw new Error('history unavailable');
        const items = kind === 'convert' ? payload.jobs : payload.history;
        if (!Array.isArray(items) || items.length > 5 || items.some(item => !item || typeof item !== 'object')
            || !Number.isSafeInteger(payload.total) || payload.total < 0 || payload.page !== page || payload.page_size !== 5) throw new Error('invalid history');
        total = payload.total;rows.replaceChildren(...items.map(addRow));
        feedback.textContent = items.length ? '' : (kind === 'boq' ? 'Belum ada Add BOQ sukses terverifikasi milik akun ini.' : 'Belum ada job konversi pada filter ini.');
        summary.textContent = total + ' riwayat · Halaman ' + page + ' dari ' + Math.max(1,Math.ceil(total / 5));
      } catch {
        if (version !== generation) return;
        total = 0;rows.replaceChildren();feedback.textContent = 'Riwayat belum bisa dimuat. Klik Muat ulang untuk mencoba kembali.';
      } finally {
        clearTimeout(timeout);
        if (version === generation) {
          host.setAttribute('aria-busy', 'false');search.disabled = refresh.disabled = false;
          prev.disabled = page <= 1;next.disabled = page * 5 >= total;
        }
      }
    }
    tabs.forEach(tab => tab.addEventListener('click', () => {
      kind = tab.dataset.historyKind;page = 1;query.value = '';scope.value = '';status.value = '';
      tabs.forEach(button => button.setAttribute('aria-pressed', String(button === tab)));
      statusFilter.hidden = kind === 'boq';query.placeholder = kind === 'boq' ? 'Target, WO, atau job asal' : 'Nama file atau job ID';
      document.getElementById('account-history-note').textContent = kind === 'boq'
        ? 'Hanya Add BOQ milik akun ini yang sukses terverifikasi: In Progress menjadi Closed.'
        : 'Hasil konversi sendiri dapat diunduh oleh Premium dan Owner hingga 7 hari setelah selesai. Riwayat tetap tersimpan setelah tautan kedaluwarsa.';
      loadHistory();
    }));
    document.getElementById('account-history-filters').addEventListener('submit', event => {event.preventDefault();page = 1;loadHistory();});
    refresh.addEventListener('click', loadHistory);
    prev.addEventListener('click', () => {if(page > 1){page--;loadHistory();}});
    next.addEventListener('click', () => {if(page * 5 < total){page++;loadHistory();}});
    window.addEventListener('pagehide', () => {generation++;controller?.abort();}, {once:true});
    loadHistory();
  }
  function ready() {
    initIdle();
    if (token && document.body.hasAttribute('data-eva-billing-page')) {
      fetch('/api/astri/status', {credentials:'same-origin',cache:'no-store',redirect:'error',
        signal:AbortSignal.timeout(45000)}).catch(() => {});
    }
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
      const button = event.currentTarget; button.disabled = true; invalidateAuth();
      try {
        const response = await fetch('/auth/logout', { method: 'POST' });
        if (!response.ok) throw new Error('logout');
        location.assign('/login');
      } catch {
        resumeIdle(); button.disabled = false;
        const feedback = document.getElementById('eva-account-feedback');
        if (feedback) feedback.textContent = 'Keluar belum berhasil. Coba lagi.';
      }
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready, { once: true }); else ready();
})();

/* Support is isolated from personal account/history initialization. */
(() => {
  function initSupport() {
    if (!document.body.hasAttribute('data-eva-support-page')) return;
    const el = id => document.getElementById('support-' + id);
    const base = '/api/support/tickets';
    const statuses = {new:'Baru', in_progress:'Ditangani', resolved:'Selesai'};
    const deliveryAttention = {
      pending:'Tersimpan di EVA. Notifikasi Telegram menunggu pengiriman.',
      unknown:'Tersimpan di EVA. Pengiriman Telegram belum dapat dipastikan; tim support perlu memeriksa.',
      failed:'Tersimpan di EVA. Notifikasi Telegram belum terkirim; tim support perlu memeriksa.',
      expired:'Tersimpan di EVA. Lampiran kedaluwarsa sebelum notifikasi terkirim.'
    };
    const hex = value => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);
    const natural = value => Number.isSafeInteger(value) && value >= 0;
    const validTicket = t => t && hex(t.id) && typeof t.subject === 'string' && [...t.subject].length <= 120
      && typeof t.reference === 'string' && t.reference.length <= 80 && Object.hasOwn(statuses, t.status)
      && natural(t.version) && natural(t.unread_count)
      && (t.delivery_state === undefined || (typeof t.delivery_state === 'string' && (t.delivery_state === 'sent' || Object.hasOwn(deliveryAttention, t.delivery_state))));
    const stamp = value => {
      const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
      return value && !Number.isNaN(date.getTime()) ? new Intl.DateTimeFormat('id-ID', {timeZone:'Asia/Jakarta',dateStyle:'medium',timeStyle:'short'}).format(date) + ' WIB' : '';
    };
    const node = (tag, text, className) => {
      const n = document.createElement(tag); if (text !== undefined) n.textContent = text;
      if (className) n.className = className; return n;
    };
    let items = [], page = 1, total = 0, selected = null, cursor = 0, more = false;
    let listGeneration = 0, viewGeneration = 0, detailGeneration = 0, listController, detailController;
    let timer, stopped = false, readBusy = false, readQueued = false, acknowledged = 0, mutating = false;
    const rendered = new Map(), seenStarts = new Set(), replies = new Map(), reopenKeys = new Map(), requests = new Set();
    const newDraft = () => ({text:'', files:[], key:null, snapshot:null, uncertain:false});
    const createDraft = newDraft();
    const replyDraft = () => {if (!replies.has(selected.id)) replies.set(selected.id, newDraft()); return replies.get(selected.id);};
    function syncComposer() {
      document.body.toggleAttribute('data-support-busy', mutating);
      const resolved = !selected || selected.status === 'resolved', locked = mutating || (selected && replyDraft().uncertain);
      el('reply-message').disabled = el('reply-images').disabled = resolved || locked;
      el('reply-send').disabled = resolved || mutating;
      el('new').disabled = el('create-cancel').disabled = mutating;
      el('new-inline').disabled = mutating;
      el('reopen').disabled = mutating;
      document.querySelectorAll('.support-ticket').forEach(button => {button.disabled = mutating;});
      el('reply-previews').querySelectorAll('button').forEach(button => {button.disabled = resolved || locked;});
    }
    async function request(url, init = {}) {
      const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 30000);
      requests.add(controller);
      try {
        const response = await fetch(url, {credentials:'same-origin',cache:'no-store',redirect:'error', ...init, signal:init.signal || controller.signal});
        const payload = await response.json();
        if (!response.ok) { const error = new Error('support unavailable'); error.status = response.status; throw error; }
        return payload;
      } finally { clearTimeout(timeout); requests.delete(controller); }
    }
    function renderTickets() {
      const focused = el('tickets').contains(document.activeElement) ? document.activeElement.dataset.ticketId : null;
      el('tickets').replaceChildren(...items.map(ticket => {
        const li = node('li'), button = node('button', undefined, 'support-ticket');
        button.type = 'button'; button.dataset.ticketId = ticket.id; button.disabled = mutating;
        button.setAttribute('aria-pressed', String(el('create').hidden && selected?.id === ticket.id));
        button.append(node('strong', ticket.subject), node('span', ticket.reference + ' · ' + statuses[ticket.status]));
        if (ticket.unread_count) button.append(node('span', ticket.unread_count + ' belum dibaca', 'support-unread'));
        button.addEventListener('click', () => selectTicket(ticket)); li.append(button); return li;
      }));
      if (focused) [...el('tickets').querySelectorAll('button')].find(button => button.dataset.ticketId === focused)?.focus({preventScroll:true});
      el('summary').textContent = total + ' tiket · Halaman ' + page;
      el('prev').disabled = page <= 1; el('next').disabled = page * 20 >= total;
    }
    async function loadTickets() {
      if (stopped || document.hidden || mutating) return;
      const generation = ++listGeneration, requestedPage = page;
      listController?.abort(); listController = new AbortController();
      const controller = listController, timeout = setTimeout(() => controller.abort(), 30000);
      el('tickets').setAttribute('aria-busy','true');
      try {
        const payload = await request(base + '?page=' + requestedPage, {signal:controller.signal});
        if (generation !== listGeneration || stopped || document.hidden) return;
        if (!Array.isArray(payload.tickets) || payload.tickets.length > 20 || !payload.tickets.every(validTicket)
            || !natural(payload.total) || payload.page !== requestedPage || payload.page_size !== 20) throw new Error('invalid tickets');
        items = payload.tickets; total = payload.total; renderTickets();
        el('list-feedback').textContent = items.length ? '' : 'Belum ada tiket.';
      } catch {
        if (generation === listGeneration && !document.hidden && !stopped) el('list-feedback').textContent = 'Tiket belum bisa dimuat. Klik Muat ulang untuk mencoba kembali.';
      } finally {
        clearTimeout(timeout);
        if (generation === listGeneration) el('tickets').setAttribute('aria-busy','false');
      }
    }
    function updateTicket(ticket) {
      selected = ticket;
      el('thread-title').textContent = ticket.subject;
      el('ticket-reference').textContent = ticket.reference + ' · ' + stamp(ticket.created_at);
      el('ticket-status').textContent = statuses[ticket.status];
      const delivery = Object.hasOwn(deliveryAttention, ticket.delivery_state) ? deliveryAttention[ticket.delivery_state] : '';
      if (el('delivery').textContent !== delivery) el('delivery').textContent = delivery;
      el('delivery').hidden = !delivery;
      const resolved = ticket.status === 'resolved';
      el('resolved').hidden = el('reopen').hidden = !resolved;
      items = items.map(item => item.id === ticket.id ? ticket : item); renderTickets(); syncComposer();
    }
    function appendMessage(message, ticketId) {
      // ponytail: cursor polls return new messages; reselect/reload refreshes old-message expiry.
      if (rendered.has(message.id)) return;
      const article = node('article', undefined, 'support-message support-message-' + message.author);
      article.dataset.messageId = String(message.id);
      article.append(node('p', (message.author === 'operator' ? 'Tim EVA' : 'Anda') + ' · ' + stamp(message.created_at), 'support-message-meta'));
      article.append(node('p', message.text, 'support-message-text'));
      for (const attachment of message.attachments) {
        const expected = hex(attachment?.id) ? base + '/' + ticketId + '/attachments/' + attachment.id : null;
        if (!expected || attachment.url !== expected || !['image/png','image/jpeg','image/webp'].includes(attachment.mime)) continue;
        const link = node('a', undefined, 'support-image-link');
        link.href = expected; link.target = '_blank'; link.rel = 'noopener';
        const image = node('img'); image.src = expected; image.alt = 'Gambar lampiran dari ' + (message.author === 'operator' ? 'Tim EVA' : 'Anda'); image.loading = 'lazy';
        link.append(image, node('span','Buka gambar di tab baru')); article.append(link);
      }
      if (message.expired_attachment_count) article.append(node('p', message.expired_attachment_count + ' gambar kedaluwarsa.', 'account-note support-expired-attachments'));
      rendered.set(message.id, article); el('messages').append(article);
    }
    async function acknowledgeVisible() {
      if (readBusy) {readQueued = true;return;}
      if (stopped || document.hidden || !selected || el('conversation').hidden) return;
      const area = el('messages').getBoundingClientRect();
      let through = acknowledged;
      // Require a fully visible bubble, or the visible start then end of an oversized bubble.
      // Never acknowledge messages from unseen pages.
      const top = Math.max(0,area.top), bottom = Math.min(innerHeight,area.bottom);
      for (const [id, article] of rendered) {
        const r = article.getBoundingClientRect();
        if (r.top >= top && r.top < bottom && r.width > 0) seenStarts.add(id);
        if (r.bottom > top && r.bottom <= bottom && r.width > 0
            && (r.top >= top || (r.height > area.height && seenStarts.has(id)))) through = Math.max(through,id);
      }
      if (!through || through <= acknowledged) return;
      const id = selected.id, generation = viewGeneration, list = listGeneration, detail = detailGeneration; readBusy = true;
      try {
        const payload = await request(base + '/' + id + '/read', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({through})});
        if (generation !== viewGeneration || stopped || document.hidden || !natural(payload.unread_count)) return;
        acknowledged = through;
        if (list !== listGeneration || detail !== detailGeneration) return; // A newer snapshot may contain concurrent unread replies.
        selected.unread_count = payload.unread_count;
        items = items.map(t => t.id === id ? {...t,unread_count:payload.unread_count} : t); renderTickets();
      } catch { /* Read receipts retry on the next visible scroll/refresh; never infer a read from GET. */ }
      finally { readBusy = false; if (readQueued) {readQueued = false;requestAnimationFrame(acknowledgeVisible);} }
    }
    async function loadDetail(mode = 'poll') {
      if (!selected || stopped || document.hidden || mutating || !el('create').hidden) return;
      const id = selected.id, view = viewGeneration, generation = ++detailGeneration;
      const after = mode === 'initial' || (mode === 'poll' && more) ? 0 : cursor;
      detailController?.abort(); detailController = new AbortController();
      const controller = detailController, timeout = setTimeout(() => controller.abort(), 30000);
      el('conversation').setAttribute('aria-busy','true');
      try {
        const payload = await request(base + '/' + id + '?after=' + after, {signal:controller.signal});
        if (generation !== detailGeneration || view !== viewGeneration || stopped || document.hidden) return;
        if (!validTicket(payload.ticket) || payload.ticket.id !== id || !Array.isArray(payload.messages) || payload.messages.length > 100
            || typeof payload.has_more !== 'boolean' || !natural(payload.next_after)) throw new Error('invalid detail');
        let last = after;
        for (const m of payload.messages) {
          if (!m || !Number.isSafeInteger(m.id) || m.id <= last || !['user','operator'].includes(m.author)
              || typeof m.text !== 'string' || [...m.text].length > 4000 || !Array.isArray(m.attachments) || m.attachments.length > 3
              || (m.expired_attachment_count !== undefined && (!natural(m.expired_attachment_count) || m.expired_attachment_count > 3))) throw new Error('invalid message');
          last = m.id;
        }
        if (payload.next_after !== last || (payload.has_more && !payload.messages.length)) throw new Error('invalid cursor');
        updateTicket(payload.ticket);
        if (!(mode === 'poll' && more)) {
          payload.messages.forEach(m => appendMessage(m, id)); cursor = payload.next_after; more = payload.has_more;
          el('more').hidden = !more;
        }
        el('thread-feedback').textContent = '';
        requestAnimationFrame(() => { if (view === viewGeneration) acknowledgeVisible(); });
      } catch {
        if (generation === detailGeneration && view === viewGeneration && !document.hidden && !stopped) el('thread-feedback').textContent = 'Percakapan belum bisa dimuat. Klik Muat ulang untuk mencoba kembali.';
      } finally {
        clearTimeout(timeout);
        if (generation === detailGeneration) { el('conversation').setAttribute('aria-busy','false'); el('more').disabled = false; }
      }
    }
    function selectTicket(ticket) {
      if (mutating) return;
      if (selected) replyDraft().text = el('reply-message').value;
      ++viewGeneration; ++detailGeneration; detailController?.abort();
      selected = ticket; cursor = 0; more = false; acknowledged = 0; rendered.clear(); seenStarts.clear();
      el('messages').replaceChildren(); el('messages').scrollTop = 0;
      el('create').hidden = el('empty').hidden = true; el('conversation').hidden = false;
      el('reply-message').value = replyDraft().text; el('reply-feedback').textContent = replyDraft().uncertain ? 'Pengiriman belum dapat dipastikan. Coba kirim lagi dengan isi yang sama.' : '';
      el('reply-send').textContent = replyDraft().uncertain ? 'Coba kirim lagi' : 'Kirim balasan';
      renderPreviews('reply', replyDraft());
      updateTicket(ticket); el('thread-feedback').textContent = 'Memuat percakapan…'; el('more').hidden = true;
      loadDetail('initial'); el('thread-title').focus();
    }
    el('new').addEventListener('click', () => {
      ++viewGeneration; ++detailGeneration; detailController?.abort();
      el('create').hidden = false; el('conversation').hidden = el('empty').hidden = true;
      el('thread-title').textContent = 'Tiket baru'; renderTickets(); el('subject').focus();
    });
    el('new-inline').addEventListener('click', () => el('new').click());
    el('create-cancel').addEventListener('click', () => {
      el('create').hidden = true; el('empty').hidden = !!selected; el('conversation').hidden = !selected;
      if (selected) {el('thread-title').textContent = selected.subject;loadDetail('poll');} else el('thread-title').textContent = 'Percakapan tiket';
      renderTickets(); el('new').focus();
    });
    const previewURLs = {create:[],reply:[]};
    function renderPreviews(kind, draft) {
      previewURLs[kind].forEach(url => URL.revokeObjectURL(url)); previewURLs[kind] = [];
      el(kind + '-previews').replaceChildren(...draft.files.map((file, index) => {
        const li = node('li'), img = node('img'), url = URL.createObjectURL(file); previewURLs[kind].push(url);
        img.src = url; img.alt = 'Pratinjau gambar ' + (index + 1);
        const remove = node('button', 'Hapus', 'account-button account-secondary'); remove.type = 'button';
        remove.setAttribute('aria-label','Hapus gambar ' + (index + 1)); remove.disabled = mutating || draft.uncertain;
        remove.addEventListener('click', () => {draft.files.splice(index,1);renderPreviews(kind,draft);});
        li.append(img, node('span',file.name), remove); return li;
      }));
      const transfer = new DataTransfer(); draft.files.forEach(file => transfer.items.add(file));
      el(kind + '-images').files = transfer.files;
      el(kind + '-count').textContent = draft.files.length + ' / 3 gambar';
    }
    for (const kind of ['create','reply']) {
      el(kind + '-images').addEventListener('change', event => {
        const draft = kind === 'create' ? createDraft : replyDraft(), files = [...event.target.files];
        let error = '';
        if (files.length + draft.files.length > 3) error = 'Maksimal 3 gambar per pesan.';
        else if (files.some(file => !['image/png','image/jpeg','image/webp'].includes(file.type))) error = 'Gunakan gambar PNG, JPEG atau WebP.';
        else if (files.some(file => file.size > 5 * 1024 * 1024 || !file.size)) error = 'Setiap gambar harus berisi data dan maksimal 5 MiB.';
        el(kind + '-feedback').textContent = error;
        event.target.setAttribute('aria-invalid', String(!!error));
        if (!error) draft.files.push(...files);
        renderPreviews(kind,draft);
      });
      el(kind).addEventListener('submit', event => {event.preventDefault();sendMessage(kind);});
    }
    function lockCreate(locked) {
      for (const input of el('create').querySelectorAll('input,textarea,select,button')) input.disabled = locked;
      el('create-send').disabled = mutating;
      el('create-cancel').disabled = mutating || createDraft.uncertain;
    }
    function invalid(input, message, feedback) {
      input.setAttribute('aria-invalid','true'); feedback.textContent = message; input.focus();
    }
    async function sendMessage(kind) {
      if (mutating || (kind === 'reply' && (!selected || selected.status === 'resolved'))) return;
      const draft = kind === 'create' ? createDraft : replyDraft();
      const input = el(kind + '-message'), feedback = el(kind + '-feedback'), button = el(kind + '-send');
      if (!draft.snapshot) {
        el(kind).querySelectorAll('[aria-invalid]').forEach(n => n.removeAttribute('aria-invalid'));
        const text = input.value.trim(), subject = el('subject').value.trim(), job = el('job').value.trim();
        if (kind === 'create' && (!subject || [...subject].length > 120)) return invalid(el('subject'),'Isi subjek, maksimal 120 karakter.',feedback);
        if ([...text].length > 4000 || (!text && (kind === 'create' || !draft.files.length))) return invalid(input,'Isi pesan, maksimal 4000 karakter. Balasan dapat berisi gambar saja.',feedback);
        if (kind === 'create' && job && !hex(job)) return invalid(el('job'),'Job ID harus berisi 32 karakter heksadesimal kecil.',feedback);
        if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(subject + text)) return invalid(input,'Hapus karakter kontrol dari pesan atau subjek.',feedback);
        draft.key ||= crypto.randomUUID().replaceAll('-','');
        draft.snapshot = {text,subject,job,module:el('module').value,files:[...draft.files]};
      }
      const id = selected?.id, view = viewGeneration, snapshot = draft.snapshot;
      const form = new FormData(); form.set('message',snapshot.text); form.set('idempotency_key',draft.key);
      snapshot.files.forEach(file => form.append('images',file,file.name));
      if (kind === 'create') {form.set('subject',snapshot.subject);if(snapshot.job)form.set('job_id',snapshot.job);if(snapshot.module)form.set('module',snapshot.module);}
      mutating = true; ++detailGeneration; detailController?.abort(); ++listGeneration; listController?.abort(); syncComposer(); lockCreate(true);
      button.textContent = 'Menyimpan…'; feedback.textContent = 'Menyimpan ke EVA…'; el(kind).setAttribute('aria-busy','true');
      let accepted = null;
      try {
        const result = await request(kind === 'create' ? base : base + '/' + id + '/messages', {method:'POST',body:form});
        const payload = kind === 'create' ? result?.ticket : result?.message;
        if (view !== viewGeneration || stopped) throw new Error('obsolete result');
        if (kind === 'create' ? !validTicket(payload) : !payload || !Number.isSafeInteger(payload.id) || payload.id <= 0 || payload.author !== 'user'
            || typeof payload.text !== 'string' || !Array.isArray(payload.attachments)) throw new Error('invalid result');
        accepted = payload; Object.assign(draft,newDraft()); input.value = ''; renderPreviews(kind,draft);
        if (kind === 'create') {el('subject').value = '';el('module').value = '';el('job').value = '';}
        feedback.textContent = 'Tersimpan di EVA.';
      } catch (error) {
        // Keep payload/key immutable after unknown outcome, including a navigation abort.
        draft.uncertain = !error.status || error.status >= 500;
        if (!draft.uncertain) {draft.snapshot = null;draft.key = null;}
        if (kind === 'reply' && selected?.id !== id) return;
        feedback.textContent = draft.uncertain ? 'Pengiriman belum dapat dipastikan. Isi dan gambar tetap tersimpan di halaman ini. Coba kirim lagi dengan isi yang sama; jangan tutup halaman.'
          : error.status === 429 ? 'Batas pengiriman tercapai. Coba lagi nanti; isi dan gambar tidak dihapus.'
          : error.status === 409 ? 'Tiket atau permintaan berubah. Muat ulang status sebelum mencoba lagi; isi dan gambar tidak dihapus.'
          : 'Pesan belum disimpan. Periksa isian atau sesi, lalu coba lagi; isi dan gambar tidak dihapus.';
      } finally {
        mutating = false; syncComposer(); lockCreate(createDraft.uncertain); el(kind).setAttribute('aria-busy','false');
        button.textContent = draft.uncertain ? 'Coba kirim lagi' : kind === 'create' ? 'Kirim tiket' : 'Kirim balasan';
      }
      if (accepted && kind === 'create') {
        selectTicket(accepted); el('reply-feedback').textContent = 'Tersimpan di EVA.'; page = 1; loadTickets();
      } else if (accepted) { loadDetail(); }
    }
    el('reopen').addEventListener('click', async () => {
      if (!selected || el('reopen').disabled) return;
      const id = selected.id, view = viewGeneration;
      if (!reopenKeys.has(id)) reopenKeys.set(id,crypto.randomUUID().replaceAll('-',''));
      mutating = true; ++detailGeneration; detailController?.abort(); ++listGeneration; listController?.abort(); syncComposer();
      try {
        const result = await request(base + '/' + id + '/reopen', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({idempotency_key:reopenKeys.get(id)})});
        const payload = result?.ticket;
        if (view !== viewGeneration || stopped) return;
        if (!validTicket(payload) || payload.id !== id) throw new Error('invalid ticket');
        updateTicket(payload); reopenKeys.delete(id); el('reply-feedback').textContent = 'Tersimpan. Tiket dibuka kembali.';
      } catch { if (view === viewGeneration) el('thread-feedback').textContent = 'Pembukaan tiket belum dapat dipastikan. Coba Buka kembali lagi; permintaan yang sama tidak digandakan.'; }
      finally { mutating = false; syncComposer(); if (view === viewGeneration && selected.status !== 'resolved') el('reply-message').focus(); }
    });
    el('more').addEventListener('click', () => { el('more').disabled = true; loadDetail('more'); });
    el('messages').addEventListener('scroll', acknowledgeVisible, {passive:true});
    window.addEventListener('scroll', acknowledgeVisible, {passive:true});
    window.addEventListener('resize', acknowledgeVisible);
    el('refresh').addEventListener('click', () => {loadTickets();loadDetail();acknowledgeVisible();});
    el('prev').addEventListener('click', () => {if(page > 1){page--;loadTickets();}});
    el('next').addEventListener('click', () => {if(page * 20 < total){page++;loadTickets();}});
    function stopRequests() {
      clearInterval(timer); ++listGeneration; ++detailGeneration; listController?.abort(); detailController?.abort();
    }
    function startPolling() {
      if (document.hidden || stopped) return;
      loadTickets(); loadDetail(); clearInterval(timer);
      timer = setInterval(() => {loadTickets();loadDetail();acknowledgeVisible();}, 5000);
    }
    document.addEventListener('visibilitychange', () => {if(document.hidden) stopRequests();else startPolling();});
    window.addEventListener('pagehide', () => {
      stopped = true; ++viewGeneration; stopRequests(); requests.forEach(controller => controller.abort());
      Object.values(previewURLs).flat().forEach(url => URL.revokeObjectURL(url));
    });
    window.addEventListener('pageshow', event => {if(event.persisted){stopped = false;renderPreviews('create',createDraft);if(selected)renderPreviews('reply',replyDraft());startPolling();}});
    startPolling();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initSupport, {once:true}); else initSupport();
})();
