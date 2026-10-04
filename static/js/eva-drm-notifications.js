/* Private history stays server-side; only poll.new_items may create a toast. */
(() => {
  function start() {
    const button = document.getElementById('evaDrmButton');
    const dialog = document.getElementById('evaDrmDialog');
    if (!button || !dialog) return;
    const count = document.getElementById('evaDrmCount');
    const list = document.getElementById('evaDrmList');
    const status = document.getElementById('evaDrmStatus');
    const historyStatus = document.getElementById('evaDrmHistoryStatus');
    const toastStatus = document.getElementById('evaDrmToastStatus');
    const close = document.getElementById('evaDrmClose');
    const more = document.getElementById('evaDrmMore');
    const history = new Map();
    const notified = new Set();
    let polling = false;
    let lastPoll = -Infinity;
    let total = 0;
    let loaded = false;
    let loading = false;
    let nextBefore = null;
    let opener;
    const endpoint = '/api/astri/drm-notifications';
    const interval = 60000;
    const text = value => typeof value === 'string' && value ? value : '—';
    const validItem = item => item && Number.isSafeInteger(item.id) && item.id > 0;

    function updateTotal(value) {
      if (Number.isSafeInteger(value) && value >= 0) total = Math.max(total, value);
      count.textContent = String(total);
      button.setAttribute('aria-label', `Notifikasi DRM, ${total} riwayat`);
    }

    function render(items) {
      for (const item of items) if (validItem(item)) history.set(item.id, item);
      list.replaceChildren();
      for (const item of [...history.values()].sort((a, b) => b.id - a.id)) {
        const row = document.createElement('li');
        const fields = document.createElement('dl');
        for (const [label, value] of [
          ['Lingkup', item.scope], ['Kode', item.code], ['Nama', item.name],
          ['Nomor WO', item.work_order_number], ['Tanggal DRM', item.requested_at]
        ]) {
          const term = document.createElement('dt');
          const detail = document.createElement('dd');
          term.textContent = label;
          detail.textContent = text(value);
          fields.append(term, detail);
        }
        row.append(fields);
        list.append(row);
      }
      if (history.size && historyStatus.textContent === 'Belum ada notifikasi DRM.') historyStatus.textContent = '';
    }

    function notify(item) {
      if (!validItem(item) || notified.has(item.id)) return;
      notified.add(item.id);
      try {
        if (!window.EvaNotifications || typeof window.EvaNotifications.show !== 'function') throw new Error('toast');
        window.EvaNotifications.show(
          [item.scope, item.code, item.name, item.requested_at].map(text).join(' · '),
          { title: 'DRM New', severity: 'info' }
        );
      } catch {
        toastStatus.textContent = 'Ada notifikasi DRM baru. Lihat riwayat di bawah; notifikasi singkat belum tersedia.';
      }
    }

    async function request(url, method) {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 55000);
      try {
        const response = await window.fetch(url, { method, credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal });
        const payload = await response.json();
        if (!response.ok || !payload) throw new Error('request');
        return payload;
      } finally {
        window.clearTimeout(timeout);
      }
    }

    async function poll() {
      if (document.visibilityState !== 'visible' || polling || Date.now() - lastPoll < interval) return;
      polling = true;
      lastPoll = Date.now();
      try {
        const payload = await request(`${endpoint}/poll`, 'POST');
        if (payload.ok !== true || !Array.isArray(payload.new_items)) throw new Error('poll');
        updateTotal(payload.total);
        if (payload.linked === false) {
          status.textContent = 'Akun ASTRI belum ditautkan. Riwayat tersimpan tetap tersedia.';
          return;
        }
        if (payload.checked === false) return;
        if (payload.new_items.length) render(payload.new_items);
        for (const item of payload.new_items) notify(item);
        status.textContent = '';
      } catch {
        status.textContent = 'Pemeriksaan DRM belum tersedia. Riwayat tersimpan tetap tersedia; akan dicoba kembali.';
      } finally {
        polling = false;
      }
    }

    async function loadHistory() {
      if (loading || (loaded && nextBefore === null)) return;
      loading = true;
      more.disabled = true;
      historyStatus.textContent = 'Memuat riwayat…';
      const before = loaded ? `&before=${nextBefore}` : '';
      try {
        const payload = await request(`${endpoint}?limit=30${before}`, 'GET');
        if (!Array.isArray(payload.items)) throw new Error('history');
        render(payload.items);
        updateTotal(payload.total);
        nextBefore = Number.isSafeInteger(payload.next_before) && payload.next_before > 0 ? payload.next_before : null;
        loaded = true;
        more.hidden = nextBefore === null;
        historyStatus.textContent = history.size ? '' : 'Belum ada notifikasi DRM.';
      } catch {
        historyStatus.textContent = 'Riwayat belum dapat dimuat. Riwayat yang sudah tampil tetap tersedia.';
        more.hidden = false;
      } finally {
        more.disabled = false;
        loading = false;
      }
    }

    button.addEventListener('click', event => {
      if (dialog.open) return;
      opener = event.currentTarget;
      dialog.showModal();
      document.body.classList.add('eva-drm-open');
      close.focus();
      if (!loaded) loadHistory();
    });
    close.addEventListener('click', () => dialog.close());
    dialog.addEventListener('cancel', event => {
      event.preventDefault();
      dialog.close();
    });
    dialog.addEventListener('close', () => {
      if (dialog.open) return;
      document.body.classList.remove('eva-drm-open');
      if (opener && opener.isConnected) opener.focus();
    });
    more.addEventListener('click', loadHistory);

    poll();
    window.setInterval(poll, interval);
    document.addEventListener('visibilitychange', poll);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
