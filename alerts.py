import json
import logging
import os
import urllib.request

LOGGER = logging.getLogger(__name__)
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
ALERT_TELEGRAM_CHAT_ID = os.getenv("ALERT_TELEGRAM_CHAT_ID", "")
ALERT_ENABLED = os.getenv("ALERT_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


def send_alert(title: str, message: str, severity: str = "error") -> None:
    """Kirim alert ke Telegram, Slack, atau generic webhook."""
    if not ALERT_ENABLED or not ALERT_WEBHOOK_URL:
        return

    text = f"[{severity.upper()}] {title}\n{message}".strip()
    if "api.telegram.org" in ALERT_WEBHOOK_URL:
        if not ALERT_TELEGRAM_CHAT_ID:
            LOGGER.warning("ALERT_TELEGRAM_CHAT_ID belum diset, alert Telegram dilewati.")
            return
        payload = {
            "chat_id": ALERT_TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }
    else:
        payload = {"text": text, "severity": severity}

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        ALERT_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status >= 400:
                LOGGER.warning("Webhook alert membalas status HTTP %s", response.status)
    except Exception as exc:
        LOGGER.warning("Gagal mengirim alert: %s", exc)


def alert_job_failed(job_id: str, error_message: str, duration_seconds: float) -> None:
    """Alert khusus untuk job yang gagal."""
    send_alert(
        title=f"Job Failed: {job_id[:8]}",
        message=f"Error: {error_message}\nDuration: {duration_seconds:.1f}s",
        severity="error",
    )


def alert_server_unhealthy(reason: str) -> None:
    """Alert kalau health check gagal."""
    send_alert(
        title="EVA Converter Unhealthy",
        message=reason,
        severity="warning",
    )

