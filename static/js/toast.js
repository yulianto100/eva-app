var popup = null;
var popupReady = false;

function renderPopup(msg) {
  clearTimeout(popup && popup.timer);
  popup.innerHTML = '<div id="eva-toast-message" style="margin-bottom:16px;position:relative;z-index:1;text-shadow:0 1px 0 rgba(255,255,255,0.45);">' + msg + '</div>' +
    '<button id="eva-toast-close" style="position:relative;z-index:1;background:rgba(255,255,255,0.24);backdrop-filter:blur(14px) saturate(1.6);-webkit-backdrop-filter:blur(14px) saturate(1.6);border:1px solid rgba(7,84,71,0.22);border-radius:12px;padding:7px 20px;cursor:pointer;color:#075447;font-weight:800;box-shadow:0 10px 24px rgba(7,84,71,0.10), inset 0 1px 0 rgba(255,255,255,0.55);">Tutup</button>';
  popup.style.background = 'linear-gradient(135deg, rgba(220,248,204,0.26), rgba(176,236,205,0.16))';
  popup.style.color = '#075447';
  popup.style.borderColor = 'rgba(7,84,71,0.22)';
  popup.style.display = 'block';
  var closeBtn = document.getElementById('eva-toast-close');
  if (closeBtn) closeBtn.onclick = function() {
    clearTimeout(popup.timer);
    popup.style.display = 'none';
  };
}

function showPopup(msg, isErr) {
  if (!popup || !popupReady) {
    console.warn('[showPopup] Popup not ready, using fallback alert');
    alert(msg);
    return;
  }
  renderPopup(msg);
}

function showPersistentPopup(msg) {
  if (!popup || !popupReady) {
    console.warn('[showPersistentPopup] Popup not ready, using fallback alert');
    alert(msg);
    return;
  }
  renderPopup(msg);
}

function updatePersistentPopup(msg) {
  if (!popup || !popupReady) return;
  renderPopup(msg);
}

function closePersistentPopup(delayMs) {
  if (!popup) return;
  clearTimeout(popup.timer);
  var close = function() { popup.style.display = 'none'; };
  if (Number(delayMs) > 0) {
    popup.timer = setTimeout(close, Number(delayMs));
  } else {
    close();
  }
}

if (document.body) {
  initPopup();
} else {
  document.addEventListener('DOMContentLoaded', initPopup);
}

function initPopup() {
  if (popupReady) return;
  popup = document.createElement('div');
  popup.id = 'eva-toast';
  popup.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);padding:24px 34px;background:linear-gradient(135deg, rgba(220,248,204,0.26), rgba(176,236,205,0.16));color:#075447;border:1px solid rgba(7,84,71,0.22);border-radius:22px;box-shadow:0 24px 70px rgba(7,84,71,0.18), inset 0 1px 0 rgba(255,255,255,0.58);z-index:99999;display:none;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-weight:800;text-align:center;min-width:320px;max-width:min(540px,calc(100vw - 36px));line-height:1.45;overflow:hidden;backdrop-filter:blur(22px) saturate(1.65);-webkit-backdrop-filter:blur(22px) saturate(1.65);';
  document.body.appendChild(popup);
  popupReady = true;
}
