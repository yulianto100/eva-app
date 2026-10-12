/* Conversion failures only. No validation, source edits or network calls. */
(function () {
  "use strict";

  function safe(value) {
    return String(value == null ? "" : value)
      .replace(/\b(Bearer|Basic)\s+\S+/gi, "$1 [disembunyikan]")
      .replace(/((?:password|passwd|token|api[_-]?key|secret|cookie|ASTRIOSP|csrf_value|session[_-]?id)["']?\s*[=:]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;]+)/gi, "$1[disembunyikan]")
      .replace(/\b(?:sk|re)_[A-Za-z0-9_-]{16,}\b/g, "[disembunyikan]")
      .replace(/(?:\/app\/|\/opt\/eva\/|\/home\/)[^\s:,'"<>]+/g, "[path server]")
      .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, "").slice(0, 6000);
  }

  function describe(options = {}) {
    const payload = options.payload || {};
    const rawMessage = String(options.message || payload.error || payload.message || "Penyebab belum tersedia.");
    const terminalJob = options.phase === "job" && ["error", "failed"].includes(payload.status);
    const failedStandardizer = options.phase === "standardize" && Number(options.httpStatus) === 500 && /^Standardizer gagal:/i.test(rawMessage);
    if (!terminalJob && !failedStandardizer) return null;
    const message = safe(rawMessage);
    // Match the exception, not a filename that happens to contain 'timeout'.
    const reason = message.replace(/^KMZ standardization gagal untuk .*?\.km[lz]:\s*/i, "").replace(/^Standardizer gagal:\s*/i, "");
    const report = {
      title: "Konversi gagal", category: "unknown", stage: "Belum diketahui",
      cause: "Penyebab gagal belum diketahui.",
      steps: ["File belum perlu diubah. Salin pesan ini lalu laporkan bersama file sumber."],
      standard: "", location: "", retry: false,
      files: (Array.isArray(options.files) ? options.files : []).slice(0, 10).map(safe),
      jobId: safe(options.jobId || payload.job_id || ""), raw: message,
      scope: safe(payload.scope_type || options.scope || ""),
      network: safe(payload.network_type || options.network || ""),
    };
    const set = (category, cause, steps, standard = "") => Object.assign(report, { category, cause, steps, standard });
    const ids = reason.match(/\bIDs?\s*(?:=|:)?\s*(\[[\d,\s]+\]|\d+)/i)
      || reason.match(/(?:exhausted|missing IDs):\s*(\[[\d,\s]+\])/i);
    const hpName = reason.match(/^HP (.+?) has no BOUNDARY FAT/i);
    const fatName = reason.match(/^FAT(?: AREA)? (.+?) (?:has |cannot |serving |loses |misses |intersects |numerical )/i);
    if (ids && /^HP\b/i.test(reason)) report.location = "HP ID " + safe(ids[1]);
    else if (hpName) report.location = "HP " + safe(hpName[1]);
    else if (fatName && !/^batch$/i.test(fatName[1])) report.location = "FAT " + safe(fatName[1]);
    if (/KMZ tidak berisi file KML/i.test(reason)) {
      set("input", "Isi file KMZ: tidak ada file KML desain di dalamnya.",
        ["Di Google Earth, simpan ulang proyek sebagai KMZ. Unggah hasilnya, bukan ZIP yang hanya diganti namanya."],
        "KMZ harus berisi file KML desain.");
    } else if (/File is not a zip file|BadZipFile|Bad CRC|Truncated file header|KMZ kosong\/korup/i.test(reason)) {
      set("input", "File KMZ rusak atau unduhannya belum lengkap.",
        ["Unduh ulang file asli sampai selesai, atau simpan ulang proyek dari Google Earth."]);
    } else if (/(?:mismatched tag|not well-formed|unclosed token|unbound prefix|syntax error|no element found): line \d+/i.test(reason)) {
      set("input", "Isi file KML tidak bisa dibaca.", ["Buka desain di Google Earth, lalu simpan ulang sebagai KML/KMZ."]);
      report.location = safe(reason.match(/line \d+(?:, column \d+)?/i)?.[0]).replace(/line/i,"Baris").replace(/column/i,"kolom");
    } else if (/Cannot identify one source Subfeeder project for FDT Slack/i.test(reason)) {
      const project = report.files.length === 1 ? report.files[0].replace(/\.km[lz]$/i, "") : "KODE - NAMA PROYEK";
      set("input", "Susunan folder proyek Subfeeder belum bisa dikenali.",
        ["Gunakan satu proyek. Namanya sama dengan nama file, tanpa .kmz atau .kml.", "Folder aset berada di dalam proyek, seperti contoh ini. Jangan hapus aset desain."],
        "Nama proyek: " + project + "\n├── CABLE (jalur kabel)\n└── SLACK HANGER (titik slack)\n\nContoh susunan; bukan daftar semua folder wajib.");
      report.inspectSource = true;
    } else if (/Subfeeder pole naming membutuhkan INISIAL OLT dan CODING FDT/i.test(reason)) {
      set("configuration", "Data Coding FDT: inisial OLT atau kode FDT belum tersedia untuk penamaan pole.",
        ["Minta owner melengkapi inisial OLT dan kode FDT proyek ini di Coding FDT. Setelah lengkap, proses ulang."]);
    } else if (/Project root metadata|FDT folder has metadata|Internal Sling anchor|Subfeeder project folder missing/i.test(reason)) {
      set("system", "EVA gagal merapikan folder proyek. File belum tentu salah.",
        ["Jangan ubah atau hapus isi KMZ. Salin pesan ini lalu laporkan untuk diperbaiki di EVA."]);
      report.location = safe(reason.match(/FDT folder has metadata that cannot be discarded:\s*(.+)/i)?.[1] || "");
    } else if (/(?:HP|FAT|Slack hanger).*invalid coordinates/i.test(reason)) {
      set("input", "Koordinat salah satu aset tidak bisa dibaca.",
        ["Periksa koordinat aset di Google Earth. Perbaiki hanya koordinat yang memang salah; jangan pindahkan aset yang benar."],
        "Koordinat harus berupa angka bujur dan lintang yang valid.");
    } else if (/(?:HP|FAT AREA|FAT batch|FAT |Slack hanger ).*(?:exhausted|infeasible|overlap|cannot |no serving pole|kehilangan|invalid|missing IDs|shift|accounting|outside own cell|requires|memerlukan)/i.test(reason) || /FatAreaGeometryError/i.test(reason)) {
      set("geometry", "EVA gagal menyusun posisi aset di hasil DXF. File belum tentu salah.",
        ["Pastikan wilayah ROAD yang dipilih sesuai desain. Jika sudah benar, jangan pindahkan aset; salin pesan ini lalu laporkan."]);
    } else if (/template.*(?:not found|missing|belum tersedia)|sheet.*not found|Required ASTRI sheet|BOQ NRO.*(?:could not|missing|expected)|engine tidak tersedia/i.test(reason)) {
      set("system", "Template output di server belum tersedia atau tidak sesuai.",
        ["Template perlu diperbaiki di EVA. File KMZ tidak perlu diubah; salin pesan ini lalu laporkan."]);
    } else if (/KML\/KMZ file is required|File KML\/KMZ wajib|Minimal satu file input/i.test(reason)) {
      set("input", "File desain KML/KMZ belum diterima.", ["Pilih file desain KML/KMZ pada unggahan utama, lalu proses lagi."]);
    } else if (/HPDB\/ASTRI.*(?:missing or duplicated|no longer matches)|ASTRI CLUSTER harus|ASTRI FAT\/HOMEPASS tanpa/i.test(reason)) {
      set("system", "EVA gagal menghubungkan hasil KMZ dengan baris HPDB/ASTRI.",
        ["Jangan hapus HP atau FDT dari desain. Salin pesan ini lalu laporkan agar pemetaan EVA diperbaiki."]);
    } else if ([401, 403].includes(Number(options.httpStatus))) {
      set("access", "Sesi login atau izin akses tidak diterima.", ["Login ulang di EVA, lalu proses lagi. File tidak perlu diubah."]);
    } else if (Number(options.httpStatus) === 413 || /Ukuran tiap file maksimal|request entity too large/i.test(reason)) {
      set("input", "Ukuran file melebihi batas unggahan.", ["Simpan ulang tanpa gambar/media yang tidak dipakai, atau pisahkan per proyek. Jangan hapus aset desain."]);
    } else if (/Upload timeout|Upload gagal terkirim|Failed to fetch/i.test(reason) && options.phase !== "job") {
      set("uncertain", "Respons upload tidak diterima. Proses di server belum bisa dipastikan.",
        ["Cek Riwayat Proses dahulu. Jika belum ada proses baru dan koneksi sudah pulih, unggah ulang."]);
      report.title = "Respons belum diterima";
    } else if (/^(?:TimeoutError|ConnectTimeout|ReadTimeout|ConnectionError|Connection refused|Gagal download spreadsheet)\b|\b(?:request|connection|read|connect) timed out\b|^worker.*unavailable|^redis.*(?:unavailable|connection)/i.test(reason)) {
      set("temporary", "Koneksi atau layanan yang dibutuhkan tidak merespons.", ["Tunggu koneksi/layanan pulih, lalu proses lagi. File tidak perlu diubah."]);
      report.retry = options.phase === "job";
    }
    return report;
  }

  let last = null, lastKey = "", dialog = null, opener = null, overflow = "", generation = 0;
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = safe(text);
    if (className) node.className = className;
    return node;
  }
  function restorePage() {
    document.documentElement.style.overflow = overflow;
    if (opener?.isConnected && !opener.disabled) opener.focus({ preventScroll: true });
  }
  function close() {
    generation++;
    if (dialog?.open) { dialog.close(); restorePage(); }
  }
  function ensure() {
    if (dialog) return;
    dialog = element("dialog", undefined, "conversion-failure-dialog");
    dialog.id = "conversionFailureDialog";
    dialog.setAttribute("aria-labelledby", "conversionFailureTitle");
    dialog.setAttribute("aria-describedby", "conversionFailureCause");
    dialog.addEventListener("cancel", event => { event.preventDefault(); close(); });
    dialog.addEventListener("keydown", event => {
      // Keep main-page conversion shortcuts from submitting behind this popup.
      event.stopPropagation();
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); return; }
      if (event.key !== "Tab") return;
      const controls = Array.from(dialog.querySelectorAll('button:not([disabled]), [tabindex="0"]')).filter(n => n.getClientRects().length);
      const first = controls[0], end = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); end.focus(); }
      else if (!event.shiftKey && document.activeElement === end) { event.preventDefault(); first.focus(); }
    });
    dialog.addEventListener("close", () => {
      if (dialog.open) return;
      restorePage();
    });
    dialog.addEventListener("click", event => {
      if (event.target !== dialog) return;
      const r = dialog.getBoundingClientRect();
      if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) close();
    });
    document.body.appendChild(dialog);
    const button = element("button", "Kenapa gagal?", "secondary-button");
    button.id = "conversionFailureButton"; button.type = "button"; button.hidden = true;
    button.setAttribute("aria-haspopup", "dialog"); button.setAttribute("aria-controls", dialog.id);
    button.addEventListener("click", () => { if (last) render(last.report, last.options); });
    const anchor = document.getElementById("retryButton");
    if (anchor) anchor.insertAdjacentElement("afterend", button);
    else document.getElementById("uploadForm")?.appendChild(button);
  }

  async function sourceFolders(options, target, token) {
    if (typeof options.readSource !== "function") return;
    try {
      const text = await options.readSource();
      if (generation !== token || !dialog.open || !target.isConnected || typeof text !== "string") return;
      const root = new DOMParser().parseFromString(text, "application/xml");
      if (root.querySelector("parsererror")) return;
      const doc = root.getElementsByTagNameNS("*", "Document")[0];
      if (!doc) return;
      const names = Array.from(doc.children).filter(n => n.localName === "Folder").slice(0, 6)
        .map(n => Array.from(n.children).find(c => c.localName === "name")?.textContent || "(tanpa nama)");
      if (names.length) target.appendChild(element("p", "Folder paling luar di file ini: " + names.map(n => '"' + safe(n).slice(0, 100) + '"').join(", ") + ".", "conversion-failure-source"));
    } catch (_) { /* Guidance must never replace the original failure. */ }
  }

  function reportText(report) {
    return [report.title, report.cause, "File: " + report.files.join("; "), report.location,
      "ID proses: " + (report.jobId || "Belum tersedia"), ...report.steps, report.standard,
      "Error proses: " + report.raw].filter(Boolean).join("\n");
  }

  function render(report, options) {
    ensure();
    const token = ++generation, wasOpen = dialog.open;
    if (!wasOpen) opener = document.activeElement;
    const header = element("header"), title = element("h2", report.title); title.id = "conversionFailureTitle";
    const x = element("button", "×", "conversion-failure-close"); x.type = "button";
    x.setAttribute("aria-label", "Tutup penjelasan kegagalan"); x.setAttribute("data-conversion-failure-close", "");
    x.addEventListener("click", close); header.append(title, x);
    const body = element("div", undefined, "conversion-failure-body");
    if (report.files.length) body.appendChild(element("p", report.files.join("; "), "conversion-failure-file"));
    const wrong = element("section", undefined, "conversion-failure-where");
    const cause = element("p", report.cause); cause.id = "conversionFailureCause";
    wrong.append(element("h3", "Gagal di bagian ini"), cause);
    if (report.location) wrong.appendChild(element("p", report.location));
    body.appendChild(wrong);
    const correct = element("section", undefined, "conversion-failure-standard");
    correct.appendChild(element("h3", "Seharusnya begini"));
    report.steps.forEach(step => correct.appendChild(element("p", step)));
    if (report.standard) correct.appendChild(element("pre", report.standard));
    body.appendChild(correct);
    const footer = element("footer"), copied = element("span", "", "conversion-failure-note"); copied.setAttribute("role", "status");
    const copy = element("button", "Salin untuk bantuan", "secondary-button"); copy.type = "button";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(reportText(report)); if (generation === token) copied.textContent = "Pesan tersalin."; }
      catch (_) { if (generation === token) copied.textContent = "Salin tidak tersedia. Kirim foto popup ini."; }
    });
    footer.append(copy);
    if (report.category === "input" && document.getElementById("kmlFileInput")) {
      const choose = element("button", "Pilih file perbaikan", "primary-button"); choose.type = "button";
      choose.addEventListener("click", () => { close(); document.getElementById("kmlFileInput").click(); }); footer.append(choose);
    }
    footer.append(copied); dialog.replaceChildren(header, body, footer);
    if (!wasOpen) { overflow = document.documentElement.style.overflow; document.documentElement.style.overflow = "hidden"; dialog.showModal(); }
    x.focus({ preventScroll: true });
    if (report.inspectSource) sourceFolders(options, wrong, token);
  }

  function show(options = {}) {
    const report = describe(options); if (!report) return false;
    ensure(); const key = JSON.stringify([report.jobId, report.raw, options.phase]);
    if (key === lastKey) return true;
    lastKey = key; last = { report, options };
    document.getElementById("conversionFailureButton").hidden = false;
    const retry = document.getElementById("retryButton"); if (retry) retry.hidden = !report.retry;
    document.getElementById("retryBar")?.classList.remove("hidden"); render(report, options); return true;
  }
  function clear() {
    last = null; lastKey = ""; close();
    const button = document.getElementById("conversionFailureButton"); if (button) button.hidden = true;
    const retry = document.getElementById("retryButton"); if (retry) retry.hidden = false;
  }
  window.evaConversionFailure = { describe, show, clear };
})();
