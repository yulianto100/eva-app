(() => {
  'use strict';
  const fields = document.getElementById('fdtFields');
  const status = document.getElementById('abdCadStatus');
  const form = document.getElementById('abdCadForm');
  const add = document.getElementById('addFdt');
  const submit = document.getElementById('generate');
  if (!fields || !form || !add || !submit) return;
  let nextCard = 1;
  let busy = false;
  let actionResetTimer;
  const trash = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7M14 10v7"/></svg>';
  function actionState(state) {
    clearTimeout(actionResetTimer);
    submit.dataset.state = state;
    submit.textContent = ({ idle: 'Buat DWG + Excel', pending: 'Membuat DWG + Excel…', success: 'Selesai ✓', error: 'Gagal · Coba lagi' })[state];
    if (state === 'idle' && !busy) submit.disabled = false;
    if (state === 'success') actionResetTimer = setTimeout(() => actionState('idle'), 2500);
  }

  function feedback(message, error = false) {
    status.textContent = message;
    status.dataset.error = String(error);
  }

  function setExpanded(card, expanded) {
    const toggle = card.querySelector('.toggle-fdt');
    card.querySelector('.fdt-body').hidden = !expanded;
    toggle.setAttribute('aria-expanded', String(expanded));
    toggle.setAttribute('aria-label', `${expanded ? 'Ciutkan' : 'Perluas'} ${card.querySelector('.fdt-caption').textContent.replace('Nama ', '')}`);
  }

  function jobUrl(value) {
    if (typeof value !== 'string' || !value.trim()) throw new Error('Alamat hasil ABD CAD tidak valid.');
    const url = new URL(value, window.location.href);
    if (url.origin !== window.location.origin || url.username || url.password) throw new Error('Alamat hasil ABD CAD tidak valid.');
    return url.href;
  }

  function focusInvalid(input) {
    setExpanded(input.closest('.fdt-row'), true);
    input.focus();
    input.reportValidity();
  }

  function refresh() {
    const cards = [...fields.children];
    let total = 0;
    cards.forEach((card, index) => {
      const rows = [...card.querySelectorAll('.line-row')];
      total += rows.length;
      card.querySelector('.fdt-caption').textContent = `Nama FDT ${index + 1}`;
      const entered = rows.filter(row => row.querySelector('[name="fat_count"]').value !== '');
      const fatTotal = entered.reduce((sum, row) => sum + Number(row.querySelector('[name="fat_count"]').value), 0);
      card.querySelector('.fdt-summary').textContent = `${rows.length} LINE${entered.length ? ` · ${fatTotal} FAT` : ''}`;
      setExpanded(card, !card.querySelector('.fdt-body').hidden);
      card.querySelector('.toggle-fdt').disabled = busy;
      card.querySelector('.remove-fdt').disabled = busy || cards.length === 1;
      card.querySelector('.add-line').disabled = busy || rows.length === 5;
      card.querySelector('.line-count').textContent = `${rows.length} / 5 LINE`;
      rows.forEach((row, i) => {
        const name = 'ABCDE'[i];
        row.dataset.line = name;
        row.querySelector('.line-tag').textContent = `LINE ${name}`;
        row.querySelector('[name="fat_count"]').setAttribute('aria-label', `Total FAT LINE ${name} FDT ${index + 1}`);
        row.querySelector('[name="cable_cores"]').setAttribute('aria-label', `Kabel LINE ${name} FDT ${index + 1}`);
        const remove = row.querySelector('.remove-line');
        remove.setAttribute('aria-label', `Hapus LINE ${name}`);
        remove.hidden = i !== rows.length - 1;
        remove.disabled = busy || rows.length === 1;
        row.querySelector('[name="fat_count"]').max = Number(row.querySelector('[name="cable_cores"]').value) * 5 / 12;
      });
    });
    document.getElementById('fdtCount').textContent = `${cards.length} dari 10 FDT`;
    document.getElementById('batchSummary').textContent = `${cards.length} FDT · ${total} LINE`;
    submit.disabled = busy;
    add.disabled = busy || cards.length >= 10;
  }

  function addLine(card, cores = 48) {
    const list = card.querySelector('.line-list');
    if (list.children.length >= 5) return;
    const row = document.createElement('div');
    row.className = 'line-row';
    row.innerHTML = `<span class="line-tag"></span><input name="fat_count" type="number" min="0" max="20" step="1" aria-label="Total FAT" required><select name="cable_cores" required>${[12, 24, 36, 48].map(n => `<option value="${n}">${n} core</option>`).join('')}</select><button class="remove-line icon-button" type="button" title="Hapus LINE">${trash}</button>`;
    row.querySelector('[name="cable_cores"]').value = cores;
    row.querySelector('.remove-line').addEventListener('click', () => {
      if (list.children.length <= 1 || row !== list.lastElementChild || busy) return;
      row.remove();
      refresh();
      card.querySelector('.add-line').focus();
    });
    list.append(row);
    refresh();
  }

  function addFdt() {
    if (fields.children.length >= 10 || busy) return;
    [...fields.children].forEach(card => setExpanded(card, false));
    const bodyId = `fdt-lines-${nextCard++}`;
    const card = document.createElement('section');
    card.className = 'fdt-row';
    card.innerHTML = `<div class="fdt-heading"><label class="fdt-name"><span class="fdt-caption">Nama FDT</span><input name="fdt" type="text" required maxlength="31" pattern="[A-Za-z0-9][A-Za-z0-9._\\-]*" autocomplete="off" spellcheck="false"></label><span class="fdt-summary"></span><button type="button" class="remove-fdt" aria-label="Hapus FDT" title="Hapus FDT"><span>Hapus FDT</span>${trash}</button><button type="button" class="toggle-fdt icon-button" aria-expanded="true" aria-controls="${bodyId}" aria-label="Ciutkan FDT"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m6 15 6-6 6 6"/></svg></button></div>
      <div class="fdt-body" id="${bodyId}"><div class="line-head" aria-hidden="true"><span>LINE</span><span>Total FAT</span><span>KABEL</span><span></span></div><div class="line-list"></div>
      <div class="line-footer"><button type="button" class="add-line">+ Tambah LINE</button><span class="line-count"></span></div></div>`;
    card.querySelector('[name="fdt"]').placeholder = 'Contoh: PPI.010';
    fields.append(card);
    addLine(card);
    addLine(card);
    card.querySelector('.toggle-fdt').addEventListener('click', () => {
      if (!busy) setExpanded(card, card.querySelector('.fdt-body').hidden);
    });
    card.querySelector('.add-line').addEventListener('click', () => {
      if (busy) return;
      addLine(card);
      card.querySelector('.line-row:last-child [name="fat_count"]').focus();
    });
    card.querySelector('.remove-fdt').addEventListener('click', () => {
      if (fields.children.length <= 1 || busy) return;
      card.remove();
      refresh();
      add.focus();
    });
  }

  add.addEventListener('click', () => {
    addFdt();
    fields.lastElementChild.querySelector('[name="fdt"]').focus();
  });
  fields.addEventListener('input', () => { feedback(''); actionState('idle'); refresh(); });
  fields.addEventListener('change', refresh);
  addFdt();
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;
    const invalid = fields.querySelector('input:invalid, select:invalid');
    if (invalid) {
      focusInvalid(invalid);
      feedback(invalid.validationMessage, true);
      return;
    }
    const cards = [...fields.children];
    const fdts = cards.map(card => ({
      name: card.querySelector('[name="fdt"]').value.trim(),
      lines: [...card.querySelectorAll('.line-row')].map(row => ({
        name: row.dataset.line,
        fat_count: Number(row.querySelector('[name="fat_count"]').value),
        cable_cores: Number(row.querySelector('[name="cable_cores"]').value),
      })),
    }));
    const duplicate = fdts.findIndex((fdt, index) => fdts.slice(0, index).some(previous => previous.name.toLowerCase() === fdt.name.toLowerCase()));
    if (duplicate !== -1) {
      feedback('Nama FDT harus unik.', true);
      focusInvalid(cards[duplicate].querySelector('[name="fdt"]'));
      return;
    }
    const invalidTotal = fdts.findIndex(fdt => {
      const total = fdt.lines.reduce((sum, line) => sum + line.fat_count, 0);
      return total < 1 || total > 36 || fdt.lines.some(line => line.fat_count > Math.floor(line.cable_cores * 5 / 12));
    });
    if (invalidTotal !== -1) {
      feedback('Total 1–36 FAT per FDT. Maksimal 5 FAT per 12 core.', true);
      focusInvalid(cards[invalidTotal].querySelector('[name="fat_count"]'));
      return;
    }
    busy = true;
    actionState('pending');
    form.querySelectorAll('input, select, button').forEach(node => { node.disabled = true; });
    refresh();
    form.setAttribute('aria-busy', 'true');
    feedback('Membuat gambar dan tabel…');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15 * 60 * 1000);
    const request = { credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal };
    try {
      const tokenResponse = await fetch('/api/abd-cad/token', request);
      if (!tokenResponse.ok) throw new Error('Sesi ABD CAD tidak tersedia.');
      const { csrf_token } = await tokenResponse.json();
      let response = await fetch('/api/abd-cad/generate?async=1', {
        ...request, method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-ABD-CAD-CSRF': csrf_token },
        body: JSON.stringify({ fdts }),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.error || 'Gagal membuat ABD CAD.');
      }
      if (response.status === 202) {
        const job = await response.json();
        const statusUrl = jobUrl(job.status_url);
        const downloadUrl = jobUrl(job.download_url);
        let state = job;
        while (state.status === 'running') {
          feedback('Membuat gambar dan tabel… Proses tetap berjalan saat popup ditutup.');
          await new Promise(resolve => setTimeout(resolve, 2000));
          const progress = await fetch(statusUrl, request);
          if (!progress.ok) throw new Error('Status ABD CAD tidak tersedia. Coba lagi nanti.');
          state = await progress.json();
        }
        if (state.status === 'error') throw new Error(state.error || 'Gagal membuat ABD CAD.');
        if (state.status !== 'done') throw new Error('Status ABD CAD tidak valid.');
        response = await fetch(downloadUrl, request);
        if (!response.ok) throw new Error('Hasil ABD CAD tidak tersedia. Coba lagi nanti.');
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = 'ABD_CAD.zip';
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      actionState('success');
      feedback(`Selesai. ${fdts.length} FDT dalam satu DWG gabungan dan satu Excel (${fdts.length} sheet) di ABD_CAD.zip.`);
    } catch (error) {
      actionState('error');
      feedback(error.name === 'AbortError' ? 'Proses melebihi 15 menit. Coba lagi nanti.' : error.message === 'Failed to fetch' ? 'Koneksi terputus. Periksa jaringan lalu coba lagi.' : error.message || 'Gagal membuat ABD CAD.', true);
    } finally {
      clearTimeout(timeout);
      busy = false;
      form.querySelectorAll('input, select, button').forEach(node => { node.disabled = false; });
      form.setAttribute('aria-busy', 'false');
      refresh();
      if (submit.dataset.state === 'success') submit.disabled = true;
    }
  });
})();
