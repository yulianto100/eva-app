/* RFS DOCUMENT: independent Cluster/Subfeeder/Feeder ABD KMZ card. */
(() => {
  "use strict";

  const ABD_TYPE = "as_built_drawing_kmz";
  const ABD_SCOPES = {
    cluster: { label: "Cluster", typeName: ABD_TYPE, slotLabel: "ABD Cluster KMZ", identityKey: "cluster_code" },
    subfeeder: { label: "Subfeeder", typeName: "as_built_drawing_subfeeder_kmz", slotLabel: "ABD Subfeeder KMZ", identityKey: "cluster_code" },
    feeder: { label: "Feeder", typeName: "as_built_drawing_kmz_main_feeder", slotLabel: "ABD Feeder KMZ", identityKey: "osp_route" },
  };
  const MAX_KMZ_BYTES = 30 * 1024 * 1024;
  const secondaryButton = "document-abd-button document-abd-button--secondary";
  const primaryButton = "document-abd-button document-abd-button--primary";
  const inputClass = "document-abd-control";

  const documentAbdState = {
    scope: "cluster",
    searchItems: [],
    match: null,
    slot: null,
    metadata: null,
    metadataError: "",
    metadataSaving: false,
    versionsLauncher: null,
    convert: null,
    converting: false,
  };
  let feedbackTimer = null;
  let feedbackSequence = 0;
  let detailRequestSequence = 0;
  let metadataRequestSequence = 0;

  function scopeSpec(scope = documentAbdState.scope) {
    return ABD_SCOPES[scope] || ABD_SCOPES.cluster;
  }

  function matchCode(match) {
    const spec = scopeSpec(match?.scope);
    return String(match?.display_code || match?.[spec.identityKey] || "");
  }

  function matchName(match) {
    return String(match?.display_name || match?.cluster_name || match?.osp_route_label || "");
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    }[char]));
  }

  async function parseJsonResponse(response) {
    let payload = {};
    try { payload = await response.json(); } catch (_error) { payload = {}; }
    if (!response.ok) throw new Error(payload.error || `Request gagal (${response.status}).`);
    return payload;
  }

  function showDocumentAbdFeedback(title, message, severity = "info", persistent = false) {
    const dialog = document.getElementById("documentAbdVersionsDialog");
    const modalNode = dialog?.open ? document.getElementById("documentAbdVersionsFeedback") : null;
    const node = modalNode || document.getElementById("documentAbdFeedback");
    if (!node) return { remove() {} };
    for (const feedbackNode of [document.getElementById("documentAbdFeedback"), document.getElementById("documentAbdVersionsFeedback")]) {
      if (!feedbackNode || feedbackNode === node) continue;
      feedbackNode.textContent = "";
      feedbackNode.classList.add("hidden");
    }
    const token = ++feedbackSequence;
    if (feedbackTimer) window.clearTimeout(feedbackTimer);
    const tone = ["success", "warning", "error"].includes(severity) ? severity : "info";
    node.className = `document-abd-feedback${modalNode ? " document-abd-dialog__feedback" : ""}`;
    node.dataset.tone = tone;
    node.setAttribute("role", tone === "error" ? "alert" : "status");
    node.textContent = `${title}: ${message}`;
    node.classList.remove("hidden");
    const remove = () => {
      if (token !== feedbackSequence) return;
      if (feedbackTimer) window.clearTimeout(feedbackTimer);
      feedbackTimer = null;
      node.textContent = "";
      node.classList.add("hidden");
    };
    if (!persistent) feedbackTimer = window.setTimeout(remove, 5000);
    return { remove };
  }

  function setDocumentAbdButtonLoading(button, label) {
    if (!button || button.dataset.documentAbdLoading === "1") return null;
    const original = {
      html: button.innerHTML,
      disabled: button.disabled,
      ariaBusy: button.getAttribute("aria-busy"),
      ariaDisabled: button.getAttribute("aria-disabled"),
      title: button.getAttribute("title"),
    };
    button.dataset.documentAbdLoading = "1";
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.setAttribute("aria-disabled", "true");
    button.setAttribute("title", label);
    button.classList.add("cursor-wait", "opacity-70");
    button.innerHTML = `<span class="inline-block animate-spin" aria-hidden="true">◌</span> ${escapeHtml(label)}`;
    return {
      restore() {
        button.innerHTML = original.html;
        button.disabled = original.disabled;
        button.classList.remove("cursor-wait", "opacity-70");
        if (original.ariaBusy === null) button.removeAttribute("aria-busy"); else button.setAttribute("aria-busy", original.ariaBusy);
        if (original.ariaDisabled === null) button.removeAttribute("aria-disabled"); else button.setAttribute("aria-disabled", original.ariaDisabled);
        if (original.title === null) button.removeAttribute("title"); else button.setAttribute("title", original.title);
        delete button.dataset.documentAbdLoading;
      },
    };
  }

  function documentAbdQuery(match) {
    const scope = match?.scope || documentAbdState.scope;
    const spec = scopeSpec(scope);
    return new URLSearchParams({
      scope,
      [spec.identityKey]: String(match?.[spec.identityKey] || ""),
      work_order_number: String(match?.work_order_number || ""),
      vendor_label: String(match?.vendor_label || ""),
    });
  }

  function matchIdentity(match) {
    return match ? documentAbdQuery(match).toString() : "";
  }

  function hasDirtyMetadata() {
    return Object.keys(changedMetadataValues()).length > 0;
  }

  function confirmDiscardMetadataChanges() {
    if (documentAbdState.metadataSaving) {
      showDocumentAbdFeedback("Data ASTRI", "Tunggu proses simpan dan GET read-back selesai.", "warning");
      return false;
    }
    return !hasDirtyMetadata() || window.confirm("Perubahan data ASTRI belum disimpan. Buang perubahan ini?");
  }

  function versionLabel(version) {
    const created = version?.created_at || "Tanggal tidak tersedia";
    const verified = version?.verified_at ? `, terverifikasi ${version.verified_at}` : "";
    return `${created}${verified}`;
  }

  function selectedTargetMarkup(match, spec) {
    const details = [
      ["WO", match.work_order_number],
      ["Vendor", match.vendor_label || "-"],
      ["Area", match.area || "-"],
      ["Operator", match.operator || "-"],
    ];
    return `
      <div class="document-abd-target__heading">
        <span class="document-abd-target__scope">${escapeHtml(spec.label)}</span>
        <strong>${escapeHtml(matchCode(match))}</strong>
        <span class="document-abd-target__name">${escapeHtml(matchName(match))}</span>
      </div>
      <dl class="document-abd-target__meta">
        ${details.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}
      </dl>`;
  }

  function selectedFile() {
    return document.querySelector("#documentAbdSlot input[data-document-abd-file]")?.files?.[0] || null;
  }

  function convertMatchesCurrentTarget() {
    const convert = documentAbdState.convert;
    if (!convert?.job_id) return false;
    return convert.identity === matchIdentity(documentAbdState.match);
  }

  function convertSectionMarkup(match, spec, versions) {
    if (!versions.length) {
      return `<div class="document-abd-empty document-abd-empty--compact"><strong>Convert EVA belum tersedia</strong><span>Belum ada versi KMZ di ASTRI untuk dikonversi.</span></div>`;
    }
    const newest = versions[0];
    const convert = convertMatchesCurrentTarget() ? documentAbdState.convert : null;
    const resultBlock = convert
      ? `<div class="document-abd-convert__result" data-document-abd-convert-result>
          <div class="document-abd-convert__result-head">
            <span class="document-abd-convert__badge">Siap upload</span>
            <strong>${escapeHtml(convert.download_name || "Hasil convert siap")}</strong>
          </div>
          <dl class="document-abd-convert__facts">
            <div><dt>Topologi</dt><dd>${escapeHtml(convert.network_type || "-")}</dd></div>
            <div><dt>Sumber ASTRI</dt><dd>${escapeHtml(convert.topology || "tidak tersedia")}</dd></div>
            <div><dt>Ukuran</dt><dd>${escapeHtml(String(convert.output_kb || "?"))} KB</dd></div>
          </dl>
          <p class="document-abd-convert__note">${escapeHtml(convert.message || "")}</p>
          <p class="document-abd-convert__hint">Hasil lengkap dan tombol unduh ada di panel Converter KMZ.</p>
          <div class="document-abd-convert__actions">
            <button type="button" data-document-abd-upload-converted class="${primaryButton}">Upload hasil ke ASTRI</button>
          </div>
        </div>`
      : "";
    return `
      <div class="document-abd-file-action document-abd-convert" data-document-abd-convert-card>
        <div class="document-abd-convert__head">
          <div class="document-abd-file-action__copy">
            <strong>Convert ke standar EVA</strong>
            <span class="document-abd-convert__source" title="${escapeHtml(newest.filename || "")}">Sumber: ${escapeHtml(newest.filename || "versi terbaru")}</span>
          </div>
          <button type="button" data-document-abd-convert class="${secondaryButton} document-abd-convert__cta">Convert ABD terbaru</button>
        </div>
        ${resultBlock}
      </div>`;
  }

  async function askTopology(spec, payload) {
    const options = Array.isArray(payload?.options) && payload.options.length
      ? payload.options
      : [{ value: "AE", label: "AE (Aerial)" }, { value: "UG", label: "UG (Underground)" }];
    const labels = options.map((option) => `${option.value} = ${option.label}`).join("\n");
    const answer = window.prompt(
      `Topologi ${spec.label} tidak tersedia di ASTRI.\n\nPilih topologi convert:\n${labels}\n\nKetik AE atau UG:`,
      "AE",
    );
    if (answer === null) return "";
    const normalized = String(answer).trim().toUpperCase();
    return options.some((option) => String(option.value).toUpperCase() === normalized) ? normalized : "";
  }

  async function convertLatestAbd(button) {
    const match = documentAbdState.match;
    const spec = scopeSpec(match?.scope);
    if (!match) {
      showDocumentAbdFeedback("Convert ABD", "Cari target RFS dulu.", "warning");
      return;
    }
    if (documentAbdState.converting) return;
    if (!confirmDiscardMetadataChanges()) return;
    documentAbdState.converting = true;
    const loading = setDocumentAbdButtonLoading(button, "Convert...");
    const progress = showDocumentAbdFeedback("Convert ABD", `Mengambil ABD terbaru ${matchCode(match)} dari ASTRI...`, "info", true);
    try {
      const body = {
        scope: match.scope || documentAbdState.scope,
        [spec.identityKey]: match[spec.identityKey],
        work_order_number: match.work_order_number,
        vendor_label: match.vendor_label,
        target_name: matchName(match),
      };
      let response = await fetch("/api/astri/rfs-documents/convert", {
        credentials: "include",
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      let payload = {};
      try { payload = await response.json(); } catch (_error) { payload = {}; }
      if (response.status === 409 && payload.code === "ASTRI_ABD_TOPOLOGY_CONFIRM_REQUIRED") {
        progress.remove();
        const chosen = await askTopology(spec, payload);
        if (!chosen) {
          showDocumentAbdFeedback("Convert ABD", "Convert dibatalkan, topologi belum dipilih.", "info");
          return;
        }
        response = await fetch("/api/astri/rfs-documents/convert", {
          credentials: "include",
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...body, network_type: chosen }),
        });
        try { payload = await response.json(); } catch (_error) { payload = {}; }
      }
      if (!response.ok) throw new Error(payload.error || `Convert ABD gagal (${response.status}).`);
      documentAbdState.convert = {
        identity: matchIdentity(match),
        job_id: payload.job_id,
        download_name: payload.download_name,
        download_url: payload.download_url,
        network_type: payload.network_type,
        topology: payload.topology,
        message: payload.message,
        output_kb: Math.round(Number(payload.output_bytes || 0) / 1024),
      };
      renderDetail();
      let published = false;
      if (typeof window.evaApplyAbdConvertResult === "function") {
        try {
          published = Boolean(window.evaApplyAbdConvertResult({
            ...payload,
            source_name: payload.source_document?.filename || "",
          }));
        } catch (_error) {
          published = false;
        }
      }
      showDocumentAbdFeedback(
        "Convert ABD selesai",
        published
          ? `${payload.download_name} siap (${payload.network_type}). Hasil lengkap ada di panel Converter KMZ, lalu upload ke ASTRI.`
          : `${payload.download_name} siap (${payload.network_type}). Cek hasil lalu upload ke ASTRI.`,
        "success",
      );
    } catch (error) {
      showDocumentAbdFeedback("Convert ABD gagal", error.message || "Convert ABD gagal.", "error", true);
    } finally {
      documentAbdState.converting = false;
      progress.remove();
      loading?.restore();
    }
  }

  async function uploadConvertedAbd(button) {
    const match = documentAbdState.match;
    const slot = documentAbdState.slot;
    const spec = scopeSpec(match?.scope);
    const convert = convertMatchesCurrentTarget() ? documentAbdState.convert : null;
    if (!match || !slot || !convert) {
      showDocumentAbdFeedback("Upload hasil convert", "Jalankan convert ABD dulu.", "warning");
      return;
    }
    if (!slot.upload_enabled) {
      showDocumentAbdFeedback("Upload hasil convert", `Upload ${spec.slotLabel} dinonaktifkan oleh ASTRI.`, "warning");
      return;
    }
    if (!confirmDiscardMetadataChanges()) return;
    const confirmation = window.confirm(
      `Upload hasil convert EVA ke ASTRI?\n\nTarget: ${matchCode(match)}\nWO: ${match.work_order_number}\nVendor: ${match.vendor_label}\nTopologi: ${convert.network_type}\nFile: ${convert.download_name}`,
    );
    if (!confirmation) {
      showDocumentAbdFeedback("Upload hasil convert", "Upload dibatalkan.", "info");
      return;
    }
    const loading = setDocumentAbdButtonLoading(button, "Mengupload...");
    const progress = showDocumentAbdFeedback("Upload hasil convert", `Mengirim ${convert.download_name} ke ASTRI...`, "info", true);
    try {
      const form = new FormData();
      form.append("scope", match.scope || documentAbdState.scope);
      form.append(spec.identityKey, match[spec.identityKey]);
      form.append("work_order_number", match.work_order_number);
      form.append("vendor_label", match.vendor_label);
      form.append("type_name", spec.typeName);
      form.append("confirm_upload", "1");
      form.append("convert_job_id", convert.job_id);
      const payload = await fetch("/api/astri/rfs-documents/upload", {
        credentials: "include",
        method: "POST",
        body: form,
      }).then(parseJsonResponse);
      documentAbdState.convert = null;
      await loadDetail(match);
      showDocumentAbdFeedback(
        "Upload hasil convert sukses",
        `${payload.document?.filename || convert.download_name} menjadi versi baru. ${payload.before_count} → ${payload.after_count} versi.`,
        "success",
      );
    } catch (error) {
      showDocumentAbdFeedback("Upload hasil convert gagal", error.message || "Upload hasil convert gagal.", "error", true);
    } finally {
      progress.remove();
      loading?.restore();
    }
  }

  function fileError(file) {
    if (!file) return "Pilih file KMZ dulu.";
    if (!String(file.name || "").toLowerCase().endsWith(".kmz")) return "Format file harus .kmz.";
    if (file.size <= 0) return "File upload kosong.";
    if (file.size > MAX_KMZ_BYTES) return "Maksimum ukuran file 30 MB.";
    return "";
  }

  function renderSearchResults() {
    const select = document.getElementById("documentAbdSearchSelect");
    const result = document.getElementById("documentAbdSearchResult");
    const items = documentAbdState.searchItems;
    if (!select) return;
    if (!items.length) {
      select.innerHTML = "";
      select.classList.add("hidden");
      if (result) {
        result.textContent = "Belum ada hasil pencarian.";
        result.dataset.state = "idle";
      }
      return;
    }
    select.innerHTML = items.map((item, index) => (
      `<option value="${index}">${escapeHtml(matchCode(item))} | ${escapeHtml(matchName(item))} | ${escapeHtml(item.work_order_number)}</option>`
    )).join("");
    select.classList.toggle("hidden", items.length < 2);
    if (result) {
      result.textContent = items.length === 1 ? "1 hasil dipilih otomatis." : `${items.length} hasil. Pilih target yang benar.`;
      result.dataset.state = "ready";
    }
  }

  function renderDetail() {
    const matchBox = document.getElementById("documentAbdMatchBox");
    const slotRoot = document.getElementById("documentAbdSlot");
    const workspace = document.getElementById("documentAbdWorkspace");
    const match = documentAbdState.match;
    const slot = documentAbdState.slot;
    const spec = scopeSpec(match?.scope);
    const searchButton = document.getElementById("documentAbdSearchButton");
    if (!matchBox || !slotRoot || !workspace) return;
    if (searchButton) searchButton.className = match ? secondaryButton : primaryButton;
    if (!match) {
      workspace.classList.add("hidden");
      matchBox.classList.add("hidden");
      slotRoot.innerHTML = "";
      if (document.getElementById("documentAbdVersionsDialog")?.open) renderVersionsDialog();
      renderMetadata();
      return;
    }
    workspace.classList.remove("hidden");
    matchBox.classList.remove("hidden");
    matchBox.innerHTML = selectedTargetMarkup(match, spec);
    if (!slot || slot.type_name !== spec.typeName) {
      slotRoot.innerHTML = `<div class="document-abd-empty" role="status"><strong>File belum tersedia</strong><span>${escapeHtml(spec.slotLabel)} tidak ditemukan pada target ini.</span></div>`;
      if (document.getElementById("documentAbdVersionsDialog")?.open) renderVersionsDialog();
      renderMetadata();
      return;
    }
    const versions = Array.isArray(slot.versions) ? slot.versions : [];
    const convertSection = convertSectionMarkup(match, spec, versions);
    const uploadSection = slot.upload_enabled
      ? `<div class="document-abd-file-action" data-document-abd-upload-card>
          <div class="document-abd-file-action__copy">
            <strong>Tambah versi</strong>
            <span>KMZ, maksimum 30 MB</span>
          </div>
          <label class="document-abd-file-picker">
            <input data-document-abd-file type="file" accept=".kmz" aria-label="Pilih file KMZ">
            <span id="documentAbdFileName">Pilih file KMZ</span>
          </label>
          <button type="button" data-document-abd-upload class="${secondaryButton}" disabled>Upload versi</button>
        </div>`
      : `<div class="document-abd-empty document-abd-empty--compact"><strong>Upload tidak tersedia</strong><span>ASTRI menonaktifkan upload pada target ini.</span></div>`;
    slotRoot.innerHTML = `
      <section class="document-abd-files" data-document-abd-slot="${escapeHtml(spec.typeName)}" aria-labelledby="documentAbdFilesTitle">
        <div class="document-abd-section-heading">
          <div>
            <span class="document-abd-eyebrow">Dokumen KMZ</span>
            <h4 id="documentAbdFilesTitle">Versi file</h4>
            <p>${versions.length ? `${versions.length} versi tersedia.` : "Belum ada versi tersimpan."}</p>
          </div>
          <button type="button" data-document-abd-open-versions class="${secondaryButton}" aria-controls="documentAbdVersionsDialog" aria-haspopup="dialog">
            Lihat versi
          </button>
        </div>
        ${convertSection}
        ${uploadSection}
      </section>`;
    if (document.getElementById("documentAbdVersionsDialog")?.open) renderVersionsDialog();
    renderMetadata();
  }

  function metadataFieldControl(field) {
    const key = escapeHtml(field.key || "");
    const value = String(field.value ?? "");
    if (!field.editable) {
      return `<div class="document-abd-readonly" data-document-abd-metadata-value="${key}">${escapeHtml(value || "-")}</div>`;
    }
    const options = Array.isArray(field.options) ? field.options : [];
    if (field.input_type === "select" && options.length) {
      const datalistId = `documentAbdMetadataOptions-${String(field.key || "").replace(/[^A-Za-z0-9_-]/g, "")}`;
      const showCodeLabel = ["olt_code", "feeder_code"].includes(String(field.key || ""));
      const selected = showCodeLabel ? options.find((option) => String(option.value ?? "") === value) : null;
      const displayValue = showCodeLabel ? String(selected?.label || value) : value;
      return `
        <input list="${escapeHtml(datalistId)}" data-document-abd-metadata-input="${key}" data-document-abd-original="${escapeHtml(value)}"${showCodeLabel ? ' data-document-abd-code-select="1"' : ""} value="${escapeHtml(displayValue)}" class="${inputClass}" autocomplete="off">
        <datalist id="${escapeHtml(datalistId)}">
          ${options.map((option) => showCodeLabel
            ? `<option value="${escapeHtml(option.label || option.value)}"></option>`
            : `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label || option.value)}</option>`).join("")}
        </datalist>`;
    }
    const type = ["number", "date"].includes(field.input_type) ? field.input_type : "text";
    const step = type === "number" ? ' step="1" min="0"' : "";
    return `<input type="${type}" data-document-abd-metadata-input="${key}" data-document-abd-original="${escapeHtml(value)}" value="${escapeHtml(value)}" class="${inputClass}"${step}>`;
  }

  function renderMetadata() {
    const root = document.getElementById("documentAbdMetadata");
    if (!root) return;
    const match = documentAbdState.match;
    const metadata = documentAbdState.metadata;
    if (!match) {
      root.innerHTML = "";
      return;
    }
    if (!metadata) {
      const isError = Boolean(documentAbdState.metadataError);
      const message = documentAbdState.metadataError || "Mengambil data terbaru dari ASTRI...";
      root.innerHTML = `<div class="document-abd-empty${isError ? " document-abd-empty--error" : ""}" role="status"><strong>${isError ? "Data gagal dimuat" : "Memuat data"}</strong><span>${escapeHtml(message)}</span></div>`;
      return;
    }
    const fields = Array.isArray(metadata.fields) ? metadata.fields : [];
    const targetSummaryKeys = new Set(["cluster_code", "name", "feeder_code", "route_name", "route_label", "work_order", "vendor", "area"]);
    const visibleFields = fields.filter((field) => field.editable || !targetSummaryKeys.has(String(field.key || "")));
    const editableCount = visibleFields.filter((field) => field.editable).length;
    root.innerHTML = `
      <section class="document-abd-metadata" aria-labelledby="documentAbdMetadataTitle">
        <div class="document-abd-section-heading">
          <div>
            <span class="document-abd-eyebrow">Data ASTRI</span>
            <h4 id="documentAbdMetadataTitle">Detail jaringan</h4>
            <p>${editableCount ? `${editableCount} field dapat diperbarui.` : "Semua field hanya-baca."}</p>
          </div>
          <button type="button" data-document-abd-metadata-refresh class="${secondaryButton}">Muat ulang</button>
        </div>
        <div class="document-abd-fields">
          ${visibleFields.map((field) => `
            <label class="document-abd-field${field.editable ? " document-abd-field--editable" : ""}">
              <span>${escapeHtml(field.label || field.key)}</span>
              ${metadataFieldControl(field)}
            </label>`).join("")}
        </div>
        ${editableCount ? `<div class="document-abd-savebar"><span>Perubahan hanya dikirim setelah konfirmasi.</span><button type="button" data-document-abd-metadata-save class="${primaryButton}" disabled>Simpan perubahan</button></div>` : ""}
      </section>`;
  }

  async function loadMetadata(match, { notify = false } = {}) {
    const target = match || documentAbdState.match;
    if (!target) throw new Error("Pilih hasil RFS dulu.");
    const requestSequence = ++metadataRequestSequence;
    const targetIdentity = matchIdentity(target);
    documentAbdState.metadata = null;
    documentAbdState.metadataError = "";
    renderMetadata();
    try {
      const payload = await fetch(`/api/astri/rfs-documents/metadata?${documentAbdQuery(target).toString()}`, {
        credentials: "include",
        cache: "no-store",
      }).then(parseJsonResponse);
      if (
        requestSequence !== metadataRequestSequence
        || !documentAbdState.match
        || matchIdentity(documentAbdState.match) !== targetIdentity
      ) return payload;
      documentAbdState.metadata = payload.metadata || null;
      if (!documentAbdState.metadata) throw new Error("Metadata ASTRI kosong.");
      if (notify) showDocumentAbdFeedback("Data ASTRI diperbarui", "Fresh GET authoritative berhasil.", "success");
      return payload;
    } catch (error) {
      if (requestSequence === metadataRequestSequence) {
        documentAbdState.metadataError = error.message || "Metadata ASTRI gagal dimuat.";
      }
      throw error;
    } finally {
      if (requestSequence === metadataRequestSequence) renderMetadata();
    }
  }

  function changedMetadataValues() {
    const values = {};
    document.querySelectorAll("#documentAbdMetadata [data-document-abd-metadata-input]").forEach((input) => {
      const key = input.dataset.documentAbdMetadataInput;
      const field = (documentAbdState.metadata?.fields || []).find((item) => item.key === key);
      const entered = String(input.value).trim();
      const original = String(input.dataset.documentAbdOriginal ?? "");
      let value = entered;
      if (input.dataset.documentAbdCodeSelect === "1") {
        const options = field?.options || [];
        const originalOption = options.find((option) => String(option.value) === original);
        const originalLabel = String(originalOption?.label || original);
        if (entered === original || entered === originalLabel) {
          value = original;
        } else {
          const matches = options.filter((option) => (
            String(option.label || option.value) === entered || String(option.value) === entered
          ));
          if (matches.length === 1) value = String(matches[0].value);
        }
      }
      if (value !== original) values[key] = value;
    });
    return values;
  }

  async function saveMetadata(button) {
    if (documentAbdState.metadataSaving) return;
    const match = documentAbdState.match;
    const metadata = documentAbdState.metadata;
    if (!match || !metadata?.version) {
      showDocumentAbdFeedback("Simpan Data ASTRI", "Refresh data ASTRI dulu.", "warning");
      return;
    }
    const values = changedMetadataValues();
    const changedKeys = Object.keys(values);
    if (!changedKeys.length) {
      showDocumentAbdFeedback("Simpan Data ASTRI", "Tidak ada perubahan.", "warning");
      return;
    }
    const labels = Object.fromEntries((metadata.fields || []).map((field) => [field.key, field.label || field.key]));
    const before = Object.fromEntries((metadata.fields || []).map((field) => [field.key, String(field.value ?? "")]));
    const lines = changedKeys.map((key) => `${labels[key]}: ${before[key] || "(kosong)"} → ${values[key] || "(kosong)"}`);
    const confirmed = window.confirm(
      `Simpan perubahan data ${scopeSpec(match.scope).label} ke ASTRI?\n\nTarget: ${matchCode(match)}\nWO: ${match.work_order_number}\nVendor: ${match.vendor_label}\n\n${lines.join("\n")}\n\nSukses hanya dinyatakan setelah GET read-back cocok.`,
    );
    if (!confirmed) {
      showDocumentAbdFeedback("Simpan Data ASTRI", "Perubahan dibatalkan.", "info");
      return;
    }
    const loading = setDocumentAbdButtonLoading(button, "Menyimpan...");
    const progress = showDocumentAbdFeedback("Simpan Data ASTRI", "Mengirim sekali, lalu memverifikasi GET read-back...", "info", true);
    const editorInputs = [...document.querySelectorAll("#documentAbdMetadata [data-document-abd-metadata-input]")];
    editorInputs.forEach((input) => { input.disabled = true; });
    documentAbdState.metadataSaving = true;
    let saved = false;
    try {
      const identity = Object.fromEntries(documentAbdQuery(match));
      const response = await fetch("/api/astri/rfs-documents/metadata", {
        method: "POST",
        credentials: "include",
        cache: "no-store",
        headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
        body: JSON.stringify({
          ...identity,
          version: metadata.version,
          values,
          confirm_metadata_update: true,
        }),
      });
      let payload = {};
      try { payload = await response.json(); } catch (_error) { payload = {}; }
      if (payload.metadata) documentAbdState.metadata = payload.metadata;
      if (!response.ok) throw new Error(payload.error || `Simpan gagal (${response.status}).`);
      saved = true;
      if (payload.accepted && payload.verified === false) {
        showDocumentAbdFeedback(
          "Update diterima ASTRI",
          "ASTRI mengembalikan success-process, tetapi GET belum sinkron. Refresh Data untuk verifikasi; jangan simpan ulang.",
          "warning",
          true,
        );
        return;
      }
      showDocumentAbdFeedback(
        "Data ASTRI tersimpan",
        `${changedKeys.length} field cocok pada fresh GET read-back${payload.reconciled ? " setelah hasil POST ambigu" : ""}.`,
        "success",
      );
    } catch (error) {
      showDocumentAbdFeedback("Simpan Data ASTRI gagal", error.message || "Update tidak dinyatakan sukses.", "error", true);
    } finally {
      documentAbdState.metadataSaving = false;
      progress.remove();
      if (saved || documentAbdState.metadata !== metadata) renderMetadata();
      else editorInputs.forEach((input) => { input.disabled = false; });
      loading?.restore();
    }
  }

  function ensureDocumentAbdVersionsDialog() {
    let dialog = document.getElementById("documentAbdVersionsDialog");
    if (dialog) return dialog;
    dialog = document.createElement("dialog");
    dialog.id = "documentAbdVersionsDialog";
    dialog.className = "document-abd-dialog";
    dialog.setAttribute("aria-labelledby", "documentAbdVersionsTitle");
    dialog.innerHTML = `
      <div class="document-abd-dialog__surface">
        <header class="document-abd-dialog__header">
          <div>
            <span class="document-abd-eyebrow">Dokumen KMZ</span>
            <h3 id="documentAbdVersionsTitle">Versi file</h3>
            <p id="documentAbdVersionsMeta">Daftar versi ASTRI</p>
          </div>
          <button type="button" data-document-abd-close class="document-abd-dialog__close" aria-label="Tutup daftar versi" title="Tutup">×</button>
        </header>
        <div id="documentAbdVersionsContent" class="document-abd-dialog__content" aria-live="polite"></div>
        <div id="documentAbdVersionsFeedback" class="hidden" role="status" aria-live="polite"></div>
      </div>
    `;
    document.body.appendChild(dialog);
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      dialog.close();
    });
    dialog.addEventListener("close", () => {
      const savedLauncher = documentAbdState.versionsLauncher;
      const launcher = savedLauncher?.isConnected
        ? savedLauncher
        : document.querySelector("#documentAbdSlot [data-document-abd-open-versions]");
      documentAbdState.versionsLauncher = null;
      if (launcher?.isConnected) window.setTimeout(() => launcher.focus(), 0);
    });
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) {
        dialog.close();
        return;
      }
      const close = event.target.closest("button[data-document-abd-close]");
      if (close) {
        dialog.close();
        return;
      }
      const button = event.target.closest("button[data-document-abd-download]");
      if (!button || button.disabled) return;
      if (button.dataset.documentAbdDownload) downloadVersion(button.dataset.documentAbdDownload, button);
    });
    return dialog;
  }

  function renderVersionsDialog() {
    const dialog = ensureDocumentAbdVersionsDialog();
    const title = document.getElementById("documentAbdVersionsTitle");
    const meta = document.getElementById("documentAbdVersionsMeta");
    const content = document.getElementById("documentAbdVersionsContent");
    const previousFocusKey = dialog.open ? document.activeElement?.dataset?.documentAbdFocusKey || "" : "";
    const match = documentAbdState.match;
    const slot = documentAbdState.slot;
    const spec = scopeSpec(match?.scope);
    if (!title || !meta || !content) return;
    if (!match || !slot || slot.type_name !== spec.typeName) {
      title.textContent = "Versi file";
      meta.textContent = `${spec.label} belum dipilih.`;
      content.innerHTML = `<div class="document-abd-empty"><strong>Belum ada target</strong><span>Pilih hasil RFS yang memiliki file KMZ.</span></div>`;
      return;
    }
    const versions = Array.isArray(slot.versions) ? slot.versions : [];
    title.textContent = `${spec.label} ${matchCode(match)}`;
    meta.textContent = `${matchName(match)}. ${versions.length} versi.`;
    const versionRows = versions.length
      ? `<div class="document-abd-version-list">${versions.map((version, index) => `
          <article class="document-abd-version-row">
            <div class="document-abd-version-row__index" aria-hidden="true">${index + 1}</div>
            <div class="document-abd-version-row__copy">
              <strong>${escapeHtml(version.filename)}</strong>
              <span>${escapeHtml(versionLabel(version))}</span>
            </div>
            <button type="button" data-document-abd-download="${escapeHtml(version.uuid)}" data-document-abd-focus-key="download:${escapeHtml(version.uuid)}" class="${secondaryButton}">Download</button>
          </article>
        `).join("")}</div>`
      : '<div class="document-abd-empty"><strong>Belum ada versi</strong><span>Upload tersedia bila diizinkan ASTRI.</span></div>';
    content.innerHTML = versionRows;
    if (previousFocusKey) {
      const escapedKey = window.CSS?.escape ? window.CSS.escape(previousFocusKey) : previousFocusKey.replace(/"/g, '\\\"');
      dialog.querySelector(`[data-document-abd-focus-key="${escapedKey}"]`)?.focus();
    }
  }

  function openVersionsDialog(button) {
    const spec = scopeSpec(documentAbdState.match?.scope);
    if (!documentAbdState.match || !documentAbdState.slot || documentAbdState.slot.type_name !== spec.typeName) {
      showDocumentAbdFeedback("Versi file", `Cari target ${spec.label} di RFS dulu.`, "warning");
      return;
    }
    const dialog = ensureDocumentAbdVersionsDialog();
    documentAbdState.versionsLauncher = button || document.activeElement;
    renderVersionsDialog();
    if (!dialog.open) dialog.showModal();
  }

  async function loadDetail(match, { notify = false } = {}) {
    const target = match || documentAbdState.match;
    const spec = scopeSpec(target?.scope);
    if (!target?.[spec.identityKey] || !target?.work_order_number || !target?.vendor_label) {
      throw new Error("Pilih hasil RFS dulu.");
    }
    const requestSequence = ++detailRequestSequence;
    const targetIdentity = matchIdentity(target);
    ++metadataRequestSequence;
    documentAbdState.metadata = null;
    documentAbdState.metadataError = "";
    const payload = await fetch(`/api/astri/rfs-documents/detail?${documentAbdQuery(target).toString()}`, {
      credentials: "include",
      cache: "no-store",
    }).then(parseJsonResponse);
    if (requestSequence !== detailRequestSequence) return payload;
    documentAbdState.match = { ...target, ...(payload.match || {}) };
    if (matchIdentity(documentAbdState.match) !== targetIdentity) {
      throw new Error("Identitas detail RFS Document berubah. Cari ulang data ASTRI.");
    }
    documentAbdState.slot = (payload.slots || []).find((item) => item?.type_name === spec.typeName) || null;
    renderDetail();
    try {
      await loadMetadata(documentAbdState.match);
    } catch (error) {
      showDocumentAbdFeedback("Data ASTRI gagal dimuat", error.message || "Metadata authoritative tidak tersedia.", "error");
    }
    if (notify) showDocumentAbdFeedback("Target diperbarui", `${documentAbdState.slot?.versions?.length || 0} versi KMZ dimuat.`, "success");
    return payload;
  }

  async function searchDocuments(button) {
    if (button?.dataset.documentAbdLoading === "1") return;
    const input = document.getElementById("documentAbdSearchInput");
    const query = input?.value?.trim() || "";
    if (!query) {
      showDocumentAbdFeedback("Pencarian", `Masukkan kode atau nama ${scopeSpec().label}.`, "warning");
      return;
    }
    if (!confirmDiscardMetadataChanges()) return;
    const loading = setDocumentAbdButtonLoading(button, "Mencari...");
    const progress = showDocumentAbdFeedback("Pencarian", "Mencari target di RFS ASTRI...", "info", true);
    try {
      const payload = await fetch(`/api/astri/rfs-documents/search?scope=${encodeURIComponent(documentAbdState.scope)}&q=${encodeURIComponent(query)}`, {
        credentials: "include",
        cache: "no-store",
      }).then(parseJsonResponse);
      documentAbdState.searchItems = payload.items || [];
      renderSearchResults();
      if (!documentAbdState.searchItems.length) {
        documentAbdState.match = null;
        documentAbdState.slot = null;
        documentAbdState.metadata = null;
        documentAbdState.metadataError = "";
        renderDetail();
        showDocumentAbdFeedback("Pencarian", "Target tidak ditemukan di RFS.", "warning");
        return;
      }
      await loadDetail(documentAbdState.searchItems[0]);
      showDocumentAbdFeedback("Target siap", `${documentAbdState.searchItems.length} hasil ditemukan. Versi KMZ dan data ASTRI dimuat.`, "success");
    } catch (error) {
      showDocumentAbdFeedback("Pencarian gagal", error.message || "RFS ASTRI tidak dapat dimuat.", "error");
    } finally {
      progress.remove();
      loading?.restore();
    }
  }

  async function selectSearchResult() {
    const select = document.getElementById("documentAbdSearchSelect");
    const item = documentAbdState.searchItems[Number(select?.value || 0)];
    if (!item) return;
    if (!confirmDiscardMetadataChanges()) {
      const currentIndex = documentAbdState.searchItems.findIndex((candidate) => (
        matchIdentity(candidate) === matchIdentity(documentAbdState.match)
      ));
      if (select && currentIndex >= 0) select.value = String(currentIndex);
      return;
    }
    const progress = showDocumentAbdFeedback("RFS Document", "Memuat semua versi ABD dari RFS...", "info", true);
    try {
      await loadDetail(item, { notify: true });
    } catch (error) {
      showDocumentAbdFeedback("RFS Document gagal", error.message || "Detail ABD dari RFS gagal dimuat.", "error");
    } finally {
      progress.remove();
    }
  }

  function filenameFromDisposition(value, fallback) {
    const match = String(value || "").match(/filename\*?=(?:UTF-8''|\"?)([^\";]+)/i);
    if (!match) return fallback;
    try { return decodeURIComponent(match[1].trim()); } catch (_error) { return match[1].trim(); }
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 750);
  }

  async function downloadVersion(uuidValue, button) {
    const match = documentAbdState.match;
    const version = (documentAbdState.slot?.versions || []).find((item) => item.uuid === uuidValue);
    if (!match || !version) {
      showDocumentAbdFeedback("Download RFS Document", "Versi dokumen sudah tidak tersedia. Refresh hasil RFS.", "warning");
      return;
    }
    const loading = setDocumentAbdButtonLoading(button, "Download...");
    try {
      const query = documentAbdQuery(match);
      query.set("type_name", scopeSpec(match?.scope).typeName);
      query.set("uuid", uuidValue);
      const response = await fetch(`/api/astri/rfs-documents/download?${query.toString()}`, {
        credentials: "include",
        cache: "no-store",
      });
      if (!response.ok) {
        let payload = {};
        try { payload = await response.json(); } catch (_error) { payload = {}; }
        throw new Error(payload.error || "Download RFS Document gagal.");
      }
      downloadBlob(await response.blob(), filenameFromDisposition(response.headers.get("Content-Disposition"), version.filename));
      showDocumentAbdFeedback("Download RFS Document dimulai", version.filename, "success");
    } catch (error) {
      showDocumentAbdFeedback("Download RFS Document gagal", error.message || "Download RFS Document gagal.", "error");
    } finally {
      loading?.restore();
    }
  }

  async function uploadVersion(button) {
    const match = documentAbdState.match;
    const slot = documentAbdState.slot;
    const spec = scopeSpec(match?.scope);
    const file = selectedFile();
    if (!match || !slot) {
      showDocumentAbdFeedback("Upload RFS Document", "Cari hasil RFS dulu.", "warning");
      return;
    }
    if (slot.type_name !== spec.typeName || !slot.upload_enabled) {
      showDocumentAbdFeedback("Upload RFS Document", `Upload ${spec.slotLabel} dinonaktifkan oleh ASTRI.`, "warning");
      return;
    }
    const validationError = fileError(file);
    if (validationError) {
      showDocumentAbdFeedback("Upload RFS Document", validationError, "warning");
      return;
    }
    if (!confirmDiscardMetadataChanges()) return;
    const uploadName = `${matchCode(match)} - ${matchName(match) || `NAMA ${spec.label.toUpperCase()}`}.kmz`;
    const confirmation = window.confirm(
      `Upload versi baru ${spec.slotLabel} ke ASTRI?\n\nTarget: ${matchCode(match)}\nWO: ${match.work_order_number}\nVendor: ${match.vendor_label}\nOperator: ${match.operator || "-"}\nFile: ${uploadName}`,
    );
    if (!confirmation) {
      showDocumentAbdFeedback("Upload RFS Document", "Upload dibatalkan.", "info");
      return;
    }
    const loading = setDocumentAbdButtonLoading(button, "Mengupload...");
    const progress = showDocumentAbdFeedback("Upload RFS Document", `Mengirim ${uploadName} ke ASTRI...`, "info", true);
    try {
      const form = new FormData();
      form.append("scope", match.scope || documentAbdState.scope);
      form.append(spec.identityKey, match[spec.identityKey]);
      form.append("work_order_number", match.work_order_number);
      form.append("vendor_label", match.vendor_label);
      form.append("type_name", spec.typeName);
      form.append("confirm_upload", "1");
      form.append("file", file);
      const payload = await fetch("/api/astri/rfs-documents/upload", {
        credentials: "include",
        method: "POST",
        body: form,
      }).then(parseJsonResponse);
      await loadDetail(match);
      showDocumentAbdFeedback(
        "Upload RFS Document sukses",
        `${payload.document?.filename || file.name} menjadi versi baru. ${payload.before_count} → ${payload.after_count} versi.`,
        "success",
      );
    } catch (error) {
      showDocumentAbdFeedback("Upload RFS Document gagal", error.message || "Upload RFS Document gagal.", "error");
    } finally {
      progress.remove();
      loading?.restore();
    }
  }

  function ensureDocumentAbdCard() {
    if (document.getElementById("documentAbdPanel")) return;
    const astriPanel = document.getElementById("astriIntegrationPanel");
    if (!astriPanel) return;
    const panel = document.createElement("section");
    panel.id = "documentAbdPanel";
    panel.className = "document-abd-panel";
    panel.setAttribute("aria-labelledby", "documentAbdTitle");
    panel.innerHTML = `
      <header class="document-abd-header">
        <div>
          <span class="document-abd-eyebrow">ASTRI</span>
          <h3 id="documentAbdTitle">RFS Document</h3>
          <p>Cari target RFS, periksa versi KMZ, lalu kelola data jaringan.</p>
        </div>
        <span class="document-abd-header__hint">Gunakan sesi login ASTRI aktif</span>
      </header>
      <div class="document-abd-search" role="search" aria-label="Cari RFS Document">
        <label class="document-abd-search__scope">
          <span>Jenis target</span>
          <select id="documentAbdScopeSelect" class="${inputClass}">
            ${Object.entries(ABD_SCOPES).map(([scope, spec]) => `<option value="${scope}"${scope === "cluster" ? " selected" : ""}>${escapeHtml(spec.label)}</option>`).join("")}
          </select>
        </label>
        <label class="document-abd-search__query">
          <span>Kode atau nama</span>
          <input id="documentAbdSearchInput" type="search" maxlength="120" class="${inputClass}" placeholder="Contoh: BKS003305 atau Setia Asih">
        </label>
        <button id="documentAbdSearchButton" type="button" class="${primaryButton}">Cari di RFS</button>
      </div>
      <div class="document-abd-search__results">
        <span id="documentAbdSearchResult" data-state="idle">Belum ada hasil pencarian.</span>
        <select id="documentAbdSearchSelect" class="hidden ${inputClass}" aria-label="Pilih hasil RFS"></select>
      </div>
      <div id="documentAbdFeedback" class="hidden" role="status" aria-live="polite"></div>
      <div id="documentAbdWorkspace" class="document-abd-workspace hidden">
        <section id="documentAbdMatchBox" class="document-abd-target hidden" aria-label="Target terpilih"></section>
        <div class="document-abd-workspace__grid">
          <div id="documentAbdSlot" aria-live="polite"></div>
          <div id="documentAbdMetadata" aria-live="polite"></div>
        </div>
      </div>
    `;
    astriPanel.insertAdjacentElement("afterend", panel);

    document.getElementById("documentAbdSearchButton")?.addEventListener("click", (event) => searchDocuments(event.currentTarget));
    document.getElementById("documentAbdScopeSelect")?.addEventListener("change", (event) => {
      const scope = event.currentTarget.value;
      if (!ABD_SCOPES[scope]) {
        event.currentTarget.value = documentAbdState.scope;
        return;
      }
      if (scope === documentAbdState.scope) return;
      if (!confirmDiscardMetadataChanges()) {
        event.currentTarget.value = documentAbdState.scope;
        return;
      }
      ++detailRequestSequence;
      ++metadataRequestSequence;
      documentAbdState.scope = scope;
      documentAbdState.searchItems = [];
      documentAbdState.match = null;
      documentAbdState.slot = null;
      documentAbdState.metadata = null;
      documentAbdState.metadataError = "";
      const input = document.getElementById("documentAbdSearchInput");
      if (input) input.placeholder = `Contoh: kode atau nama ${scopeSpec().label}`;
      renderSearchResults();
      renderDetail();
    });
    document.getElementById("documentAbdSearchInput")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        searchDocuments(document.getElementById("documentAbdSearchButton"));
      }
    });
    document.getElementById("documentAbdSearchSelect")?.addEventListener("change", selectSearchResult);
    document.getElementById("documentAbdMetadata")?.addEventListener("input", () => {
      const saveButton = document.querySelector("#documentAbdMetadata button[data-document-abd-metadata-save]");
      if (saveButton) saveButton.disabled = !hasDirtyMetadata();
    });
    document.getElementById("documentAbdMetadata")?.addEventListener("click", async (event) => {
      const refreshButton = event.target.closest("button[data-document-abd-metadata-refresh]");
      const saveButton = event.target.closest("button[data-document-abd-metadata-save]");
      if (refreshButton && !refreshButton.disabled) {
        if (!confirmDiscardMetadataChanges()) return;
        const loading = setDocumentAbdButtonLoading(refreshButton, "Memuat...");
        try {
          await loadMetadata(documentAbdState.match, { notify: true });
        } catch (error) {
          showDocumentAbdFeedback("Refresh Data ASTRI gagal", error.message || "Metadata authoritative tidak tersedia.", "error");
        } finally {
          loading?.restore();
        }
      }
      if (saveButton && !saveButton.disabled) await saveMetadata(saveButton);
    });
    window.addEventListener("beforeunload", (event) => {
      if (!hasDirtyMetadata()) return;
      event.preventDefault();
      event.returnValue = "";
    });
    document.getElementById("documentAbdSlot")?.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-document-abd-open-versions], button[data-document-abd-upload], button[data-document-abd-convert], button[data-document-abd-upload-converted]");
      if (!button || button.disabled) return;
      if (button.dataset.documentAbdOpenVersions !== undefined) openVersionsDialog(button);
      if (button.dataset.documentAbdUpload !== undefined) uploadVersion(button);
      if (button.dataset.documentAbdConvert !== undefined) convertLatestAbd(button);
      if (button.dataset.documentAbdUploadConverted !== undefined) uploadConvertedAbd(button);
    });
    document.getElementById("documentAbdSlot")?.addEventListener("change", (event) => {
      const input = event.target.closest("input[data-document-abd-file]");
      if (!input) return;
      const label = document.getElementById("documentAbdFileName");
      const uploadButton = document.querySelector("#documentAbdSlot button[data-document-abd-upload]");
      const error = fileError(input.files?.[0]);
      if (label) label.textContent = input.files?.[0]?.name || "Pilih file KMZ";
      if (uploadButton) uploadButton.disabled = Boolean(error);
      if (input.files?.[0] && error) showDocumentAbdFeedback("File tidak valid", error, "warning");
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ensureDocumentAbdCard);
  } else {
    ensureDocumentAbdCard();
  }
})();
