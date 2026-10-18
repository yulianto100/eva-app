/* Subscription page controller: fetch state, render QR, poll settlement. */
(() => {
  "use strict";

  const csrf = document.querySelector('meta[name="eva-csrf"]')?.content || "";
  const $ = (id) => document.getElementById(id);
  const pending = $("billing-pending");
  const success = $("billing-success");
  const historyRows = $("billing-history-rows");
  const planLine = $("billing-plan-line");

  let pollTimer = null;
  let currentInvoice = null;
  let lastPlan = null;
  let lastQuote = null;

  const rupiah = (n) => "Rp " + Number(n || 0).toLocaleString("id-ID");

  async function api(path, options = {}) {
    const opts = { credentials: "include", ...options };
    opts.headers = { Accept: "application/json", ...(options.headers || {}) };
    if (options.method && options.method !== "GET") {
      opts.headers["X-EVA-CSRF"] = csrf;
    }
    const res = await fetch(path, opts);
    let body = null;
    try { body = await res.json(); } catch (_e) { /* keep null */ }
    if (!res.ok) {
      const message = body && body.error ? body.error : "Permintaan gagal (" + res.status + ").";
      const error = new Error(message);
      error.status = res.status;
      error.body = body;
      throw error;
    }
    return body || {};
  }

  function fmtTime(ts) {
    if (!ts) return "—";
    try {
      return new Date(ts * 1000).toLocaleString("id-ID", {
        day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit",
      });
    } catch (_e) { return "—"; }
  }

  function fmtDate(ts) {
    if (!ts) return "—";
    try {
      return new Date(ts * 1000).toLocaleDateString("id-ID", {
        day: "numeric", month: "long", year: "numeric",
      });
    } catch (_e) { return "—"; }
  }

  function statusBadge(status) {
    const map = {
      activated: ["billing-badge--ok", "Aktif"],
      pending: ["billing-badge--wait", "Menunggu pembayaran"],
      creating: ["billing-badge--wait", "Menyiapkan QRIS"],
      expired: ["billing-badge--bad", "Kedaluwarsa"],
      failed: ["billing-badge--bad", "Gagal"],
      mismatch: ["billing-badge--bad", "Perlu pemeriksaan"],
      cancelled: ["billing-badge--bad", "Dibatalkan"],
    };
    const [cls, label] = map[status] || ["billing-badge--wait", status || "—"];
    return '<span class="billing-badge ' + cls + '">' + label + "</span>";
  }

  function renderQR(invoice) {
    const box = $("billing-qr");
    box.innerHTML = "";
    const text = String(invoice.qris_string || "").trim();
    if (!text) {
      box.textContent = "QR tidak tersedia — klik Cek sekarang atau muat ulang halaman.";
      return;
    }
    if (window.QRCode) {
      // qrcodejs renders to canvas/img inside the box; keep it scannable at 240px.
      new window.QRCode(box, { text, width: 220, height: 220, correctLevel: window.QRCode.CorrectLevel.M });
    } else {
      // No external QR generator (the payload must never leave our own origin):
      // link to the gateway's own QR page instead.
      const link = document.createElement("a");
      link.href = invoice.qris_url || "#";
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "Buka halaman QRIS";
      box.appendChild(link);
    }
  }

  function renderPending(invoice) {
    if (!invoice) {
      pending.hidden = true;
      currentInvoice = null;
      stopPoll();
      return;
    }
    currentInvoice = invoice;
    pending.hidden = false;
    success.hidden = true;
    $("billing-pending-plan").textContent = invoice.plan === "premium" ? "Premium" : "Standar";
    $("billing-pending-amount").textContent = rupiah(invoice.amount);
    // Kode unik: nominal QRIS = harga paket + kode kecil, supaya pembayaran
    // terverifikasi otomatis walau ada invoice lain dengan harga sama.
    const base = Number(invoice.base_price || 0);
    const unique = Number(invoice.amount || 0) - base;
    $("billing-unique-note").hidden = !(base > 0 && unique > 0);
    if (base > 0 && unique > 0) {
      $("billing-unique-note").textContent =
        "Nominal di atas = Rp " + base.toLocaleString("id-ID") + " + kode unik Rp " +
        unique.toLocaleString("id-ID") + ". Bayar persis sesuai nominal yang muncul di aplikasi bank — kode unik membuat pembayaranmu terverifikasi otomatis.";
    }
    const extra = $("billing-pending-extra");
    if (extra) {
      const upgrade = invoice.kind === "upgrade";
      extra.hidden = !upgrade;
      if (upgrade) {
        extra.textContent = "Upgrade Premium (selisih " + invoice.upgrade_days +
          " hari): setelah pembayaran terverifikasi, paket langsung jadi Premium. Tanggal habis tetap sama.";
      }
    }
    $("billing-pending-expiry").textContent = fmtTime(invoice.expires_at);
    $("billing-pending-status").outerHTML =
      '<span id="billing-pending-status" class="billing-badge billing-badge--wait">Menunggu pembayaran</span>';
    renderQR(invoice);
    startPoll();
  }

  function renderSuccess(plan) {
    stopPoll();
    pending.hidden = true;
    success.hidden = false;
    const label = plan && plan.label ? plan.label : "Premium";
    $("billing-success-line").textContent = plan && plan.until
      ? "Paket " + label + " · masa aktif sampai " + fmtDate(plan.until) + "."
      : "Paket " + label + " sudah aktif. Terima kasih!";
    renderCurrent(plan);
    renderPlans(plan);
  }

  function renderCurrent(plan) {
    const card = $("billing-current");
    if (!card) return;
    if (!plan || !plan.active) {
      card.hidden = true;
      planLine.hidden = false;
      planLine.textContent = "Belum ada paket aktif. Pilih paket di bawah untuk mengaktifkan akses penuh.";
      return;
    }
    card.hidden = false;
    planLine.hidden = true;
    const days = plan.until ? Math.max(0, Math.ceil((plan.until * 1000 - Date.now()) / 86400000)) : 0;
    $("billing-current-title").textContent = "Paket " + plan.label;
    $("billing-current-until").textContent =
      (plan.trial ? "Uji coba aktif sampai " : "Masa aktif sampai ") + fmtDate(plan.until) +
      (days ? " · sisa " + days + " hari" : "");
    const badge = $("billing-current-badge");
    badge.textContent = plan.trial ? "Uji coba" : "Aktif";
    badge.className = "billing-badge " + (plan.trial ? "billing-badge--wait" : "billing-badge--ok");
    $("billing-current-note").textContent = plan.trial
      ? "Pilih paket di bawah supaya akses berlanjut setelah uji coba berakhir."
      : (plan.plan === "standar"
        ? "Perpanjang Standar kapan saja, atau upgrade ke Premium dengan bayar selisih sisa hari."
        : "Perpanjang kapan saja — masa aktif otomatis bertambah 30 hari setiap pembayaran.");
  }

  function renderPlans(plan) {
    const isPaidPremium = !!(plan && plan.paid && plan.active && plan.plan === "premium");
    const isPaidStandar = !!(plan && plan.paid && plan.active && plan.plan === "standar");
    const stdBtn = document.querySelector('.billing-choose[data-plan="standar"]');
    const prmBtn = document.querySelector('.billing-choose[data-plan="premium"]');
    const upgradeNote = $("billing-upgrade-note");
    if (stdBtn) {
      stdBtn.disabled = isPaidPremium;
      stdBtn.textContent = isPaidPremium
        ? "Tidak tersedia saat Premium aktif"
        : (isPaidStandar ? "Perpanjang Standar — Rp 29.000" : "Pilih Standar — Rp 29.000");
    }
    if (prmBtn) {
      prmBtn.disabled = false;
      prmBtn.textContent = isPaidPremium
        ? "Perpanjang Premium — Rp 69.000"
        : (isPaidStandar && lastQuote ? "Upgrade ke Premium — " + rupiah(lastQuote.price) : "Pilih Premium — Rp 69.000");
    }
    if (upgradeNote) {
      upgradeNote.hidden = !(isPaidStandar && lastQuote);
      if (isPaidStandar && lastQuote) {
        upgradeNote.textContent = "Upgrade: bayar selisih untuk sisa " + lastQuote.days +
          " hari (" + rupiah(lastQuote.price) + "). Langsung jadi Premium sampai " +
          fmtDate(lastQuote.until) + " — tanggal habis tetap sama.";
      }
    }
  }

  function renderHistory(invoices) {
    if (!invoices || !invoices.length) {
      historyRows.innerHTML = '<tr><td colspan="5">Belum ada tagihan.</td></tr>';
      return;
    }
    historyRows.innerHTML = invoices.map((inv) => {
      const planLabel = inv.plan === "premium" ? "Premium" : "Standar";
      const activeUntil = inv.until_after ? fmtDate(inv.until_after) : "—";
      return "<tr><td>" + fmtTime(inv.created_at) + "</td><td>" + planLabel +
        "</td><td>" + rupiah(inv.amount) + "</td><td>" + statusBadge(inv.status) +
        "</td><td>" + activeUntil + "</td></tr>";
    }).join("");
  }

  async function refresh() {
    const data = await api("/api/billing/status");
    lastPlan = data.plan || null;
    lastQuote = data.upgrade || null;
    renderCurrent(data.plan);
    renderPlans(data.plan);
    renderHistory(data.invoices);
    if (data.invoice) {
      renderPending(data.invoice);
    } else {
      if (!pending.hidden) { pending.hidden = true; }
      if (data.plan && data.plan.paid && data.plan.active && !success.hidden) {
        renderSuccess(data.plan);
      }
      stopPoll();
    }
  }

  function startPoll() {
    stopPoll();
    pollTimer = window.setInterval(checkNow, 8000);
  }
  function stopPoll() {
    if (pollTimer) { window.clearInterval(pollTimer); pollTimer = null; }
  }

  async function checkNow() {
    if (!currentInvoice) return;
    try {
      const data = await api("/api/billing/payment/" + encodeURIComponent(currentInvoice.id));
      const inv = data.invoice || {};
      if (inv.status === "activated") {
        renderSuccess(data.plan);
        await refresh();
        return;
      }
      if (inv.status === "pending" || inv.status === "creating") {
        const badge = $("billing-pending-status");
        if (badge) badge.textContent = "Menunggu pembayaran";
        return;
      }
      // expired / failed / mismatch
      const badge = $("billing-pending-status");
      if (badge) {
        badge.className = "billing-badge billing-badge--bad";
        badge.textContent = inv.status === "expired" ? "Kedaluwarsa" : "Gagal";
      }
      $("billing-pending-note").textContent =
        inv.status === "expired"
          ? "Tagihan ini kedaluwarsa. Pilih paket lagi untuk membuat QRIS baru."
          : "Tagihan ini tidak bisa diproses. Pilih paket lagi untuk membuat QRIS baru.";
      stopPoll();
      currentInvoice = null;
    } catch (_e) { /* transient; keep polling */ }
  }

  async function checkout(plan) {
    const buttons = document.querySelectorAll(".billing-choose");
    buttons.forEach((b) => { b.disabled = true; });
    try {
      const data = await api("/api/billing/checkout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan }),
      });
      renderPending(data.invoice);
      pending.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      window.alert(err.message || "Tagihan belum bisa dibuat.");
    } finally {
      renderPlans(lastPlan);
    }
  }

  document.querySelectorAll(".billing-choose").forEach((btn) => {
    btn.addEventListener("click", () => checkout(btn.dataset.plan));
  });

  $("billing-recheck")?.addEventListener("click", async () => {
    const btn = $("billing-recheck");
    btn.disabled = true;
    try { await checkNow(); } finally { btn.disabled = false; }
  });

  $("billing-cancel")?.addEventListener("click", async () => {
    if (!currentInvoice) return;
    if (!window.confirm("Batalkan tagihan ini? QRIS tidak bisa dipakai lagi.")) return;
    try {
      await api("/api/billing/cancel/" + encodeURIComponent(currentInvoice.id), { method: "POST" });
    } catch (_e) { /* fall through to refresh */ }
    currentInvoice = null;
    await refresh();
  });

  refresh().catch((err) => {
    planLine.hidden = false;
    planLine.textContent = err.status === 401
      ? "Masuk dulu untuk melihat langganan."
      : "Status langganan belum bisa dimuat. Muat ulang halaman.";
  });
})();
