/* Private DRM New + revision center. Each channel retains its own IDs/ACKs. */
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
    const history = new Map(), visible = new Set(), interval = 60000;
    const channels = {
      new: { endpoint: '/api/astri/drm-notifications', label: 'DRM', unread: 0 },
      revision: { endpoint: '/api/astri/drm-revisions', label: 'Revisi', unread: 0 },
    };
    for (const channel of Object.values(channels)) Object.assign(channel, {
      polling: false, lastPoll: -Infinity, loaded: false, loading: false,
      nextBefore: null, reading: false, readVersion: 0, error: '', historyError: '',
    });
    const native = typeof panel.showPopover === 'function';
    let readTimer = null;
    const text = value => typeof value === 'string' && value ? value : '—';
    const validItem = item => item && Number.isSafeInteger(item.id) && item.id > 0;
    const itemKey = (kind, id) => `${kind}:${id}`;
    const scopeName = value => ({ cluster: 'Cluster', subfeeder: 'Subfeeder', feeder: 'Feeder' }[String(value).toLowerCase()] || 'DRM');
    const dateText = value => {
      const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
      return match ? `${match[3]}/${match[2]}/${match[1]}` : text(value);
    };
    const isOpen = () => native ? panel.matches(':popover-open') : !panel.hidden;
    if (!native) { panel.removeAttribute('popover'); panel.hidden = true; }

    function updateUnread(kind, value, version = channels[kind].readVersion) {
      const channel = channels[kind];
      if (version !== channel.readVersion || !Number.isSafeInteger(value) || value < 0) return;
      channel.unread = value;
      const total = Object.values(channels).reduce((sum, item) => sum + item.unread, 0);
      count.textContent = total > 99 ? '99+' : String(total);
      count.hidden = total === 0;
      button.classList.toggle('has-new', total > 0);
      button.setAttribute('aria-label', total ? `Notifikasi, ${total} belum dibaca` : 'Buka notifikasi');
      announce.textContent = total ? `${total} notifikasi belum dibaca.` : '';
    }
    function feedback() {
      status.textContent = Object.values(channels).map(c => c.error).filter(Boolean).join(' ');
      historyStatus.textContent = Object.values(channels).map(c => c.historyError).filter(Boolean).join(' ')
        || (Object.values(channels).some(c => c.loading) ? 'Memuat…' : history.size ? '' : 'Belum ada notifikasi.');
      more.hidden = Object.values(channels).every(c => c.loaded && c.nextBefore === null);
      more.disabled = Object.values(channels).some(c => c.loading);
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
      if (readTimer !== null || !isOpen() || document.visibilityState !== 'visible') return;
      readTimer = window.setTimeout(() => { readTimer = null; markVisibleRead(); }, 150);
    }
    const observer = typeof IntersectionObserver === 'function' ? new IntersectionObserver(entries => {
      for (const entry of entries) {
        const key = entry.target.dataset.notificationKey;
        if (entry.isIntersecting && entry.intersectionRatio >= .6) visible.add(key); else visible.delete(key);
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
      for (const row of list.children) if (rowVisible(row, bounds)) visible.add(row.dataset.notificationKey);
      scheduleRead();
    }
    async function markVisibleRead() {
      if (!isOpen() || document.visibilityState !== 'visible') return;
      for (const [kind, channel] of Object.entries(channels)) {
        if (channel.reading || !isOpen() || document.visibilityState !== 'visible') continue;
        const bounds = body.getBoundingClientRect();
        const ids = [...list.children].filter(row => row.dataset.notificationKind === kind
          && visible.has(row.dataset.notificationKey) && rowVisible(row, bounds))
          .map(row => Number(row.dataset.notificationId)).filter(id => history.get(itemKey(kind, id))?.is_read === false).slice(0, 100);
        if (!ids.length) continue;
        channel.reading = true; ++channel.readVersion;
        try {
          const payload = await request(`${channel.endpoint}/read`, 'POST', { ids });
          if (payload.ok !== true || !Number.isSafeInteger(payload.unread_count) || payload.unread_count < 0) throw new Error('read');
          ++channel.readVersion;
          for (const id of ids) { const item = history.get(itemKey(kind, id)); if (item) item.is_read = true; }
          updateUnread(kind, payload.unread_count);
          channel.historyError = '';
          render([], kind);
        } catch { channel.historyError = `${channel.label}: belum dapat menandai dibaca.`; }
        finally { channel.reading = false; feedback(); }
      }
    }
    function opened() {
      button.setAttribute('aria-expanded', 'true'); position(); close.focus();
      if (Object.values(channels).some(c => !c.loaded)) loadHistory(); else { fallbackVisible(); scheduleRead(); }
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
    function revisionDetails(item) {
      const details = document.createElement('details'); details.className = 'eva-drm-revision';
      const summary = document.createElement('summary'); summary.textContent = `Lihat catatan · Penulis: ${text(item.author)}`;
      const note = document.createElement('p'); note.textContent = text(item.note);
      details.append(summary, note);
      try {
        const url = new URL(item.source_url);
        if (url.origin === 'https://astri.morarepublic.co.id' && !url.username && !url.password
            && ['cluster', 'subfeeder', 'feeder'].includes(item.scope)
            && url.pathname === `/material/${item.scope}/drm/requirement-document`) {
          const link = document.createElement('a'); link.href = url.href; link.target = '_blank';
          link.rel = 'noopener noreferrer'; link.textContent = 'Buka ASTRI'; details.append(link);
        }
      } catch { /* Invalid source URL remains plain notification text. */ }
      details.addEventListener('toggle', fallbackVisible);
      return details;
    }
    function render(items, kind) {
      for (const item of items) if (validItem(item)) {
        const key = itemKey(kind, item.id);
        history.set(key, { ...item, kind, is_read: history.get(key)?.is_read === true || item.is_read === true });
      }
      const expanded = new Set([...list.children].filter(row => row.querySelector('details')?.open).map(row => row.dataset.notificationKey));
      if (observer) observer.disconnect();
      visible.clear(); list.replaceChildren();
      const sorted = [...history.values()].sort((a, b) => Number(b.detected_at || 0) - Number(a.detected_at || 0)
        || String(b.requested_at || '').localeCompare(String(a.requested_at || '')) || b.id - a.id || a.kind.localeCompare(b.kind));
      for (const item of sorted) {
        const row = document.createElement('li');
        row.dataset.notificationId = String(item.id); row.dataset.notificationKind = item.kind;
        row.dataset.notificationKey = itemKey(item.kind, item.id);
        row.classList.toggle('is-unread', item.is_read === false);
        const heading = document.createElement('div'); heading.className = 'eva-drm-item-heading';
        const event = document.createElement('span'); event.className = 'eva-drm-event';
        event.textContent = item.kind === 'revision' ? 'Revisi' : 'DRM New';
        event.title = item.kind === 'revision' ? 'Catatan reviewer saat WO Closed' : 'Status DRM saat terdeteksi: New';
        const scope = document.createElement('span'); scope.className = 'eva-drm-scope'; scope.textContent = scopeName(item.scope);
        const code = document.createElement('strong'); code.textContent = text(item.code); heading.append(event, scope, code);
        const name = document.createElement('p'); name.className = 'eva-drm-item-name';
        name.textContent = text(item.name).replace(/_/g, ' '); name.title = text(item.name);
        const date = document.createElement('time'); date.textContent = dateText(item.requested_at);
        date.title = `${item.kind === 'revision' ? 'Tanggal catatan' : 'Tanggal DRM'}: ${text(item.requested_at)}`;
        row.append(heading, name, date);
        if (item.kind === 'revision') {
          const details = revisionDetails(item); details.open = expanded.has(row.dataset.notificationKey); row.append(details);
        }
        list.append(row);
        if (observer && item.is_read === false) observer.observe(row);
      }
      fallbackVisible();
    }
    async function request(url, method, payload) {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 55000);
      try {
        const init = { method, credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal };
        if (method !== 'GET' && method !== 'HEAD') {
          const csrf = document.querySelector('meta[name="eva-csrf"]')?.content || '';
          init.headers = { 'Content-Type': 'application/json', 'X-EVA-CSRF': csrf };
          if (payload !== undefined) init.body = JSON.stringify(payload);
        }
        const response = await window.fetch(url, init), data = await response.json();
        if (!response.ok || !data) throw new Error('request');
        return data;
      } finally { window.clearTimeout(timeout); }
    }
    async function pollChannel(kind, channel) {
      if (document.visibilityState !== 'visible' || channel.polling || channel.reading || Date.now() - channel.lastPoll < interval) return;
      channel.polling = true; channel.lastPoll = Date.now(); const version = channel.readVersion;
      try {
        const payload = await request(`${channel.endpoint}/poll`, 'POST');
        if (payload.ok !== true || !Array.isArray(payload.new_items)) throw new Error('poll');
        updateUnread(kind, payload.unread_count, version);
        channel.error = payload.linked === false ? `${channel.label}: hubungkan ASTRI untuk menerima notifikasi.` : '';
        if (payload.checked !== false && payload.new_items.length) render(payload.new_items, kind);
        fallbackVisible(); scheduleRead();
      } catch { channel.error = `${channel.label} belum tersedia. Riwayat tetap tersimpan.`; }
      finally { channel.polling = false; feedback(); }
    }
    function poll() { return Promise.all(Object.entries(channels).map(([kind, channel]) => pollChannel(kind, channel))); }
    async function loadChannel(kind, channel) {
      if (channel.loading || (channel.loaded && channel.nextBefore === null)) return;
      channel.loading = true; channel.historyError = ''; feedback();
      const before = channel.loaded ? `&before=${channel.nextBefore}` : '', version = channel.readVersion;
      try {
        const payload = await request(`${channel.endpoint}?limit=30${before}`, 'GET');
        if (!Array.isArray(payload.items)) throw new Error('history');
        render(payload.items, kind); updateUnread(kind, payload.unread_count, version);
        channel.nextBefore = Number.isSafeInteger(payload.next_before) && payload.next_before > 0 ? payload.next_before : null;
        channel.loaded = true;
        fallbackVisible(); scheduleRead();
      } catch { channel.historyError = `${channel.label}: riwayat belum tersedia. Coba lagi.`; }
      finally { channel.loading = false; feedback(); }
    }
    function loadHistory() { return Promise.all(Object.entries(channels).map(([kind, channel]) => loadChannel(kind, channel))); }
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
