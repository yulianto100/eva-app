const DEFAULT_TIMEOUT_MS = 5200;
const SEVERITY_META = {
  success: { title: "Berhasil", role: "status" },
  info: { title: "Info", role: "status" },
  warning: { title: "Perhatian", role: "alert" },
  error: { title: "Gagal", role: "alert" },
};
function ensureNotificationRoot() {
  let root = document.getElementById("notificationRoot");
  if (root) return root;
  root = document.createElement("section");
  root.id = "notificationRoot";
  root.className = "notification-stack";
  root.setAttribute("aria-label", "Notifikasi aplikasi");
  document.body.appendChild(root);
  return root;
}
function normalizeSeverity(severity) {
  return Object.prototype.hasOwnProperty.call(SEVERITY_META, severity) ? severity : "info";
}
function notificationMessage(input) {
  if (typeof input === "string") return input.trim();
  if (input && typeof input.message === "string") return input.message.trim();
  return "Terjadi kendala. Coba lagi atau cek log proses.";
}
function showNotification(input, options = {}) {
  const severity = normalizeSeverity(options.severity || input?.severity || "info");
  const message = notificationMessage(input);
  const title = (options.title || input?.title || SEVERITY_META[severity].title).trim();
  const timeoutMs = Number.isFinite(options.timeoutMs) ? options.timeoutMs : DEFAULT_TIMEOUT_MS;
  const root = ensureNotificationRoot();
  const item = document.createElement("article");
  item.className = `notification notification-${severity}`;
  item.setAttribute("role", SEVERITY_META[severity].role);
  item.innerHTML = `<div class="notification-copy"><strong></strong><span></span></div><button type="button" class="notification-close" aria-label="Tutup notifikasi">×</button>`;
  item.querySelector("strong").textContent = title;
  item.querySelector("span").textContent = message;
  const close = () => item.remove();
  item.querySelector("button").addEventListener("click", close);
  root.appendChild(item);
  if (timeoutMs > 0) window.setTimeout(close, timeoutMs);
  return item;
}
function setInlineAlert(elementOrId, message, severity = "error") {
  const element = typeof elementOrId === "string" ? document.getElementById(elementOrId) : elementOrId;
  if (!element) return null;
  const normalized = normalizeSeverity(severity);
  element.textContent = notificationMessage(message);
  element.classList.remove("hidden", "alert-success", "alert-info", "alert-warning", "alert-error");
  element.classList.add(`alert-${normalized}`);
  element.setAttribute("role", normalized === "error" || normalized === "warning" ? "alert" : "status");
  return element;
}
function clearInlineAlert(elementOrId) {
  const element = typeof elementOrId === "string" ? document.getElementById(elementOrId) : elementOrId;
  if (!element) return null;
  element.textContent = "";
  element.classList.add("hidden");
  element.removeAttribute("role");
  return element;
}
function clearNotifications() { ensureNotificationRoot().innerHTML = ""; }
window.EvaNotifications = { show: showNotification, setInlineAlert, clearInlineAlert, clearAll: clearNotifications };
window.showNotification = showNotification;
window.setInlineAlert = setInlineAlert;
window.clearInlineAlert = clearInlineAlert;
window.clearNotifications = clearNotifications;
export { showNotification, setInlineAlert, clearInlineAlert, clearNotifications, ensureNotificationRoot, normalizeSeverity };
