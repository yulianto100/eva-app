/* Icon-only DRM center. Counts/read state belong to the authenticated user DB. */
(() => {
  function start() {
    const button = document.getElementById('evaDrmButton');
    const panel = document.getElementById('evaDrmDialog');
    if (!button || !panel) return;
    const count = document.getElementById('evaDrmCount');
    const list = document.getElementById('evaDrmList');
    const status = document.getElementById('evaDrmStatus');
    const historyStatus = document.getElementById('evaDrmHistoryStatus');
    const announce = document.getElementById('evaDrmAnnounce');
    const close = document.getElementById('evaDrmClose');
    const more = document.getElementById('evaDrmMore');
    const body = panel.querySelector('.eva-drm-body');
    const history = new Map(), visible = new Set();
    const endpoint = '/api/astri/drm-notifications', interval = 60000;
    const native = typeof panel.showPopover === 'function';
    let polling = false, lastPoll = -Infinity, loaded = false, loading = false;
    let nextBefore = null, reading = false, readTimer = null, readVersion = 0;
    const text = value => typeof value === 'string' && value ? value : '—';
    const validItem = item => item && Number.isSafeInteger(item.id) && item.id > 0;
    const scopeName = value => ({ cluster: 'Cluster', subfeeder: 'Subfeeder', feeder: 'Feeder' }[String(value).toLowerCase()] || 'DRM');
    const dateText = value => {
      const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
      return match ? `${match[3]}/${match[2]}/${match[1]}` : text(value);
    };
    const isOpen = () => native ? panel.matches(':popover-open') : !panel.hidden;
    if (!native) { panel.removeAttribute('popover'); panel.hidden = true; }

    function updateUnread(value, version = readVersion) {
      if (version !== readVersion || !Number.isSafeInteger(value) || value < 0) return;
      count.textContent = value > 99 ? '99+' : String(value);
      count.hidden = value === 0;
      button.classList.toggle('has-new', value > 0);
      button.setAttribute('aria-label', value ? `Notifikasi, ${value} belum dibaca` : 'Buka notifikasi');
      announce.textContent = value ? `${value} notifikasi belum dibaca.` : '';
    }
    function position() {
      if (!isOpen()) return;
      const rect = button.getBoundingClientRect(), width = Math.min(360, window.innerWidth - 24);
      const top = Math.max(12, Math.min(rect.bottom + 8, window.innerHeight - 160));
      panel.style.width = `${width}px`;
      panel.style.left = `${Math.max(12, Math.min(rect.right - width, window.innerWidth - width - 12))}px`;
      panel.style.top = `${top}px`;
      panel.style.maxHeight = `${Math.max(120, Math.min(440, window.innerHeight - top - 12))}px`;
    }
    function scheduleRead() {
      if (readTimer !== null || reading || !isOpen() || document.visibilityState !== 'visible') return;
      readTimer = window.setTimeout(() => { readTimer = null; markVisibleRead(); }, 150);
    }
    const observer = typeof IntersectionObserver === 'function' ? new IntersectionObserver(entries => {
      for (const entry of entries) {
        const id = Number(entry.target.dataset.notificationId);
        if (entry.isIntersecting && entry.intersectionRatio >= .6) visible.add(id); else visible.delete(id);
      }
      scheduleRead();
    }, { root: body, threshold: .6 }) : null;
    function rowVisible(row, bounds) {
      const rect = row.getBoundingClientRect();
      const height = Math.max(0, Math.min(rect.bottom, bounds.bottom, window.innerHeight) - Math.max(rect.top, bounds.top, 0));
      const width = Math.max(0, Math.min(rect.right, bounds.right, window.innerWidth) - Math.max(rect.left, bounds.left, 0));
      return rect.height > 0 && rect.width > 0 && height * width >= rect.height * rect.width * .6;
    }
    function fallbackVisible() {
      if (!isOpen()) return;
      visible.clear();
      const bounds = body.getBoundingClientRect();
      for (const row of list.children) {
        if (rowVisible(row, bounds)) visible.add(Number(row.dataset.notificationId));
      }
      scheduleRead();
    }
    async function markVisibleRead() {
      if (reading || !isOpen() || document.visibilityState !== 'visible') return;
      const bounds = body.getBoundingClientRect();
      const ids = [...list.children].filter(row => visible.has(Number(row.dataset.notificationId)) && rowVisible(row, bounds))
        .map(row => Number(row.dataset.notificationId)).filter(id => history.get(id)?.is_read === false).slice(0, 100);
      if (!ids.length) return;
      reading = true; ++readVersion;
      try {
        const payload = await request(`${endpoint}/read`, 'POST', { ids });
        if (payload.ok !== true || !Number.isSafeInteger(payload.unread_count) || payload.unread_count < 0) throw new Error('read');
        ++readVersion;
        for (const id of ids) { const item = history.get(id); if (item) item.is_read = true; }
        updateUnread(payload.unread_count);
        historyStatus.textContent = '';
        render([]);
      } catch { historyStatus.textContent = 'Belum dapat menandai dibaca. Coba lagi.'; }
      finally { reading = false; }
    }
    function opened() {
      button.setAttribute('aria-expanded', 'true'); position(); close.focus();
      if (!loaded) loadHistory(); else { fallbackVisible(); scheduleRead(); }
    }
    function closed() {
      button.setAttribute('aria-expanded', 'false');
      if (panel.contains(document.activeElement) || document.activeElement === document.body) button.focus();
    }
    function hide(restoreFocus = false) {
      if (!isOpen()) return;
      if (native) panel.hidePopover(); else { panel.hidden = true; closed(); }
      if (restoreFocus) button.focus();
    }
    function render(items) {
      for (const item of items) if (validItem(item)) history.set(item.id, { ...item, is_read: history.get(item.id)?.is_read === true || item.is_read === true });
      if (observer) observer.disconnect();
      visible.clear(); list.replaceChildren();
      for (const item of [...history.values()].sort((a, b) => b.id - a.id)) {
        const row = document.createElement('li');
        row.dataset.notificationId = String(item.id);
        row.classList.toggle('is-unread', item.is_read === false);
        const heading = document.createElement('div'); heading.className = 'eva-drm-item-heading';
        const event = document.createElement('span'); event.className = 'eva-drm-event'; event.textContent = 'DRM New'; event.title = 'Status DRM saat terdeteksi: New';
        const scope = document.createElement('span'); scope.className = 'eva-drm-scope'; scope.textContent = scopeName(item.scope);
        const code = document.createElement('strong'); code.textContent = text(item.code); heading.append(event, scope, code);
        const name = document.createElement('p'); name.className = 'eva-drm-item-name';
        name.textContent = text(item.name).replace(/_/g, ' '); name.title = text(item.name);
        const date = document.createElement('time'); date.textContent = dateText(item.requested_at); date.title = `Tanggal DRM: ${text(item.requested_at)}`;
        row.append(heading, name, date); list.append(row);
        if (observer && item.is_read === false) observer.observe(row);
      }
      fallbackVisible();
    }
    async function request(url, method, payload) {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 55000);
      try {
        const init = { method, credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal };
        if (payload) { init.headers = { 'Content-Type': 'application/json' }; init.body = JSON.stringify(payload); }
        const response = await window.fetch(url, init), data = await response.json();
        if (!response.ok || !data) throw new Error('request');
        return data;
      } finally { window.clearTimeout(timeout); }
    }
    async function poll() {
      if (document.visibilityState !== 'visible' || polling || reading || Date.now() - lastPoll < interval) return;
      polling = true; lastPoll = Date.now(); const version = readVersion;
      try {
        const payload = await request(`${endpoint}/poll`, 'POST');
        if (payload.ok !== true || !Array.isArray(payload.new_items)) throw new Error('poll');
        updateUnread(payload.unread_count, version);
        if (payload.linked === false) { status.textContent = 'Tautkan ASTRI untuk menerima notifikasi.'; return; }
        if (payload.checked !== false && payload.new_items.length) render(payload.new_items);
        status.textContent = ''; fallbackVisible(); scheduleRead();
      } catch { status.textContent = 'DRM belum tersedia. Riwayat tetap tersimpan.'; }
      finally { polling = false; }
    }
    async function loadHistory() {
      if (loading || (loaded && nextBefore === null)) return;
      loading = true; more.disabled = true; historyStatus.textContent = 'Memuat…';
      const before = loaded ? `&before=${nextBefore}` : '', version = readVersion;
      try {
        const payload = await request(`${endpoint}?limit=30${before}`, 'GET');
        if (!Array.isArray(payload.items)) throw new Error('history');
        render(payload.items); updateUnread(payload.unread_count, version);
        nextBefore = Number.isSafeInteger(payload.next_before) && payload.next_before > 0 ? payload.next_before : null;
        loaded = true; more.hidden = nextBefore === null;
        historyStatus.textContent = history.size ? '' : 'Belum ada notifikasi.';
        fallbackVisible(); scheduleRead();
      } catch { historyStatus.textContent = 'Riwayat belum tersedia. Coba lagi.'; more.hidden = false; }
      finally { more.disabled = false; loading = false; }
    }
    button.addEventListener('click', () => {
      if (isOpen()) { hide(true); return; }
      if (native) panel.showPopover(); else { panel.hidden = false; opened(); }
    });
    panel.addEventListener('toggle', event => { if (event.newState === 'open') opened(); else closed(); });
    close.addEventListener('click', () => hide(true)); more.addEventListener('click', loadHistory);
    body.addEventListener('scroll', fallbackVisible);
    if (!native) {
      document.addEventListener('click', event => { if (!panel.contains(event.target) && !button.contains(event.target)) hide(); });
      document.addEventListener('keydown', event => { if (event.key === 'Escape') hide(true); });
    }
    window.addEventListener('resize', () => { position(); fallbackVisible(); });
    window.addEventListener('scroll', () => { position(); fallbackVisible(); }, true);
    loadHistory(); poll(); window.setInterval(poll, interval);
    document.addEventListener('visibilitychange', () => { poll(); fallbackVisible(); scheduleRead(); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true }); else start();
})();
